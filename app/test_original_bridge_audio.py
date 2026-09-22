"""Audio integration contracts without native SDR or sound hardware."""
import http.client
import json
import threading
import time
import unittest

from original_bridge import BridgeServer, ReceiverController
from original_stations import StationStore
from test_original_bridge import FakeProcessor, FakeSource, FakeWallpaper, wait_for


class FakeAudio:
    def __init__(self):
        self.state = dict(enabled=False, muted=False, volume=0.15, status="off", error=None)
        self.sources = []
        self.blocks = []
        self.closed = False
        self.fail_submit = False

    def snapshot(self):
        return dict(self.state)

    def configure(self, **options):
        self.state.update(options)

    def set_source(self, frequency_hz, generation, demo=False):
        self.sources.append((frequency_hz, generation, demo))

    def submit(self, iq, sample_rate, frequency_hz, generation):
        if self.fail_submit:
            raise RuntimeError("audio callback unavailable")
        self.blocks.append((len(iq), sample_rate, frequency_hz, generation))

    def close(self):
        self.closed = True


class AudioBridgeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.audio = FakeAudio()
        self.wallpaper = FakeWallpaper()
        self.controller = ReceiverController(
            station_store=StationStore(),
            frequency=99_200_000, audio_factory=lambda: self.audio,
            source_factory=lambda f, d: FakeSource(f, d, self.calls),
            processor_factory=FakeProcessor, wallpaper_factory=lambda: self.wallpaper)

    def tearDown(self):
        self.controller.shutdown(restore_wallpaper=False)

    def test_audio_default_off_and_does_not_open_receiver(self):
        self.assertFalse(self.controller.snapshot()["audio"]["enabled"])
        self.controller.request(dict(action="audio", volume=0.2, muted=True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.snapshot()["audio"]["volume"], 0.2)

    def test_audio_receives_continuous_iq_before_fft_throttle(self):
        self.controller.request(dict(action="start"))
        wait_for(lambda: self.controller.snapshot()["sequence"] >= 3)
        state = self.controller.snapshot()
        self.assertGreater(len(self.audio.blocks), state["sequence"] * 3)
        self.assertIn((99_200_000, state["generation"], False), self.audio.sources)

    def test_controls_do_not_reopen_usb_or_reset_visual_history(self):
        self.controller.request(dict(action="start"))
        wait_for(lambda: self.controller.snapshot()["status"] == "live")
        before = self.controller.snapshot()["generation"]
        self.controller.request(dict(action="audio", enabled=True, volume=0.1))
        self.controller.request(dict(action="audio", muted=True))
        self.controller.request(dict(action="audio", enabled=False))
        self.assertEqual(len([c for c in self.calls if c[0] == "open"]), 1)
        self.assertEqual(self.controller.snapshot()["generation"], before)

    def test_tune_and_stop_invalidate_old_audio(self):
        self.controller.request(dict(action="start"))
        wait_for(lambda: self.controller.snapshot()["status"] == "live")
        self.controller.request(dict(action="tune", frequency_hz=99_300_000))
        generation = self.controller.snapshot()["generation"]
        self.assertIn((None, generation, False), self.audio.sources)
        wait_for(lambda: (99_300_000, generation, False) in self.audio.sources)
        self.controller.request(dict(action="stop"))
        self.assertIsNone(self.audio.sources[-1][0])
        wait_for(lambda: self.controller.snapshot()["status"] == "idle")

    def test_audio_failure_does_not_break_rf(self):
        self.audio.fail_submit = True
        self.controller.request(dict(action="start"))
        wait_for(lambda: self.controller.snapshot()["sequence"] >= 2)
        state = self.controller.snapshot()
        self.assertEqual(state["status"], "live")
        self.assertIsNone(state["error"])
        self.assertIn("audio callback", state["audio"]["error"])
        self.assertEqual(state["audio"]["status"], "error")

    def test_invalid_audio_options_are_rejected(self):
        for extra in ({}, {"volume": True}, {"volume": float("nan")},
                      {"volume": float("inf")}, {"volume": -0.01}, {"volume": 1.01},
                      {"volume": "0.2"}, {"enabled": 1}, {"muted": "false"},
                      {"frequency_hz": 99_200_000}, {"demo": False}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.controller.request(dict(action="audio", **extra))

    def test_shutdown_closes_audio_and_preserves_wallpaper(self):
        self.controller.shutdown(restore_wallpaper=False)
        self.assertTrue(self.audio.closed)
        self.assertEqual(self.wallpaper.restored, 0)

    def test_audio_http_keeps_token_and_origin_restrictions(self):
        server = BridgeServer(0, controller=self.controller)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        def post(headers):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                conn.request("POST", "/api/control", json.dumps(dict(action="audio", enabled=True)), headers)
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()
        try:
            headers = {"Content-Type": "application/json", "Origin": origin}
            self.assertEqual(post(headers), 403)
            headers["X-SDR-Token"] = self.controller.token
            for invalid_origin in ("null", "https://external.example"):
                self.assertEqual(post(dict(headers, Origin=invalid_origin)), 403)
            self.assertFalse(self.audio.state["enabled"])
            self.assertEqual(post(headers), 200)
            self.assertTrue(self.audio.state["enabled"])
            self.assertEqual(self.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
