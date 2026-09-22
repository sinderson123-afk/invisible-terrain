"""Favorites, persistence and cancellable single-owner scans: no native hardware."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from original_bridge import BridgeServer, ConflictError, ReceiverController
from original_stations import StationStore
from test_original_bridge import FakeSource, FakeProcessor, FakeWallpaper, wait_for
from test_original_bridge_audio import FakeAudio


class FakeScanner:
    def analyze(self, iq, rate, center):
        return [{"frequency_hz": 93_400_000, "score_db": 20.0, "bandwidth_hz": 120_000}]


class StationBridgeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.store = StationStore()
        self.audio = FakeAudio()
        self.controller = self.make_controller()

    def make_controller(self, factory=None, plan=None, store=None, frequency=99_200_000):
        return ReceiverController(frequency=frequency, station_store=store or self.store,
            source_factory=factory or (lambda f, d: FakeSource(f, d, self.calls)),
            processor_factory=FakeProcessor, wallpaper_factory=FakeWallpaper,
            audio_factory=lambda: self.audio, scanner_factory=FakeScanner,
            scan_plan_factory=plan or (lambda: [90_050_000, 91_650_000]), scan_settle_blocks=0)

    def tearDown(self):
        self.controller.shutdown(restore_wallpaper=False)

    def live(self, demo=False):
        self.controller.request({"action": "start", "demo": demo})
        wait_for(lambda: self.controller.snapshot()["status"] == "live")

    def test_favorite_dedup_rename_remove_without_hardware(self):
        for name in ("广播一", "改名"):
            self.controller.request({"action": "station_save", "frequency_hz": 93_400_000, "name": name})
        state = self.controller.snapshot()["radio"]
        self.assertEqual(len(state["favorites"]), 1)
        self.assertEqual(state["favorites"][0]["name"], "改名")
        self.controller.request({"action": "station_remove", "id": state["favorites"][0]["id"]})
        self.assertEqual(self.controller.snapshot()["radio"]["favorites"], [])
        self.assertEqual(self.calls, [])

    def test_invalid_station_commands_rejected(self):
        for payload in ({"action":"station_save"}, {"action":"station_remove"},
                        {"action":"station_save","frequency_hz":93400000,"name":"x","extra":1},
                        {"action":"station_save","frequency_hz":144000000,"name":"x"},
                        {"action":"scan","demo":True}, {"action":"scan_cancel","x":1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.controller.request(payload)

    def test_memory_restores_frequency_volume_but_never_playback(self):
        self.controller.shutdown(restore_wallpaper=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stations.json"
            self.controller = self.make_controller(store=StationStore(path))
            self.controller.request({"action":"tune","frequency_hz":93400000})
            self.controller.request({"action":"audio","volume":0.07,"enabled":True,"muted":True})
            self.controller.shutdown(restore_wallpaper=False)
            self.audio = FakeAudio()
            self.controller = self.make_controller(store=StationStore(path), frequency=None)
            state = self.controller.snapshot()
            self.assertEqual(state["frequency_hz"], 93400000)
            self.assertEqual(state["audio"]["volume"], 0.07)
            self.assertFalse(state["audio"]["enabled"])
            self.assertFalse(state["audio"]["muted"])
            self.assertEqual(self.calls, [])

    def test_failed_tune_does_not_replace_last_good_station(self):
        self.controller.shutdown(restore_wallpaper=False)
        def source(f, d):
            if f == 95_000_000:
                raise RuntimeError("test open failed")
            return FakeSource(f, d, self.calls)
        self.controller = self.make_controller(factory=source)
        self.live()
        self.controller.request({"action":"tune","frequency_hz":95000000})
        wait_for(lambda: self.controller.snapshot()["status"] == "error")
        self.assertEqual(self.store.snapshot()["last_frequency_hz"], 99200000)

    def test_scan_requires_real_live_and_cannot_start_twice(self):
        with self.assertRaises(ConflictError):
            self.controller.request({"action":"scan"})
        self.live(demo=True)
        with self.assertRaises(ConflictError):
            self.controller.request({"action":"scan"})
        self.assertIsNone(self.store.snapshot()["last_frequency_hz"])

    def test_completed_scan_restores_station_and_single_owner(self):
        self.live()
        self.controller.request({"action":"audio","enabled":True})
        before = self.controller.snapshot()["generation"]
        self.controller.request({"action":"scan"})
        self.assertIsNone(self.audio.sources[-1][0])
        wait_for(lambda: self.controller.snapshot()["scan"]["status"] == "complete")
        state = self.controller.snapshot()
        self.assertEqual(state["status"], "live")
        self.assertEqual(state["frequency_hz"], 99200000)
        self.assertEqual(state["generation"], before + 2)
        self.assertEqual(state["scan"]["progress"], 1)
        self.assertEqual(len(state["scan"]["candidates"]), 1)
        self.assertEqual(self.store.snapshot()["last_frequency_hz"], 99200000)
        self.assertTrue(state["audio"]["enabled"])
        self.assertTrue(all(f is None or f == 99200000 for f, _, _ in self.audio.sources))
        self.assertEqual({call[-1] for call in self.calls}, {self.controller.thread.ident})
        opened = 0
        for call in self.calls:
            if call[0] == "open":
                opened += 1
            elif call[0] == "close":
                opened -= 1
            self.assertIn(opened, (0, 1))

    def test_cancel_restores_original_and_rejects_tuning_during_scan(self):
        self.live()
        self.controller.request({"action":"scan"})
        with self.assertRaises(ConflictError):
            self.controller.request({"action":"tune","frequency_hz":93400000})
        with self.assertRaises(ConflictError):
            self.controller.request({"action":"scan"})
        self.controller.request({"action":"scan_cancel"})
        wait_for(lambda: self.controller.snapshot()["scan"]["status"] == "cancelled")
        self.assertEqual(self.controller.snapshot()["status"], "live")
        self.assertEqual(self.controller.snapshot()["frequency_hz"], 99200000)

    def test_stop_supersedes_scan_without_reopening_original(self):
        self.live()
        self.controller.request({"action":"scan"})
        self.controller.request({"action":"stop"})
        wait_for(lambda: self.controller.snapshot()["status"] == "idle")
        self.assertEqual(self.controller.snapshot()["scan"]["status"], "cancelled")
        self.assertEqual(self.controller.snapshot()["frequency_hz"], 99200000)
        self.assertEqual(len([c for c in self.calls if c[:2] == ("open",99200000)]), 1)

    def test_scan_failure_still_restores_receiver(self):
        self.controller.shutdown(restore_wallpaper=False)
        def source(f, d):
            if f == 90_050_000:
                raise RuntimeError("scan-only failure")
            return FakeSource(f, d, self.calls)
        self.controller = self.make_controller(factory=source)
        self.live()
        self.controller.request({"action":"scan"})
        wait_for(lambda: self.controller.snapshot()["scan"]["status"] == "error")
        self.assertEqual(self.controller.snapshot()["status"], "live")
        self.assertEqual(self.controller.snapshot()["frequency_hz"], 99200000)
        self.assertIn("scan-only failure", self.controller.snapshot()["scan"]["error"])

    def test_memory_write_failure_does_not_stop_radio(self):
        self.store.remember = lambda **_: (_ for _ in ()).throw(OSError("disk full"))
        self.live()
        wait_for(lambda: self.controller.snapshot()["sequence"] > 0)
        self.controller.request({"action":"audio","volume":0.08})
        self.assertEqual(self.controller.snapshot()["status"], "live")

    def test_close_failure_does_not_leave_scan_permanently_active(self):
        self.controller.shutdown(restore_wallpaper=False)
        class CloseFailure(FakeSource):
            def close(self):
                super().close()
                raise RuntimeError("test close failed")
        self.controller = self.make_controller(factory=lambda f,d: CloseFailure(f,d,self.calls))
        self.live()
        self.controller.request({"action":"scan"})
        wait_for(lambda: self.controller.snapshot()["status"] == "error")
        self.assertEqual(self.controller.snapshot()["scan"]["status"], "error")
        self.controller.request({"action":"stop"})
        self.assertEqual(self.controller.snapshot()["status"], "idle")
        self.assertNotIn(self.controller.snapshot()["scan"]["status"], ("scanning","restoring"))

    def test_cancel_during_restore_reports_cancelled(self):
        self.controller.shutdown(restore_wallpaper=False)
        entered, release = threading.Event(), threading.Event()
        original_opens = 0
        def source(f, d):
            nonlocal original_opens
            if f == 99200000:
                original_opens += 1
                if original_opens == 2:
                    entered.set(); release.wait(2)
            return FakeSource(f, d, self.calls)
        self.controller = self.make_controller(factory=source)
        self.live()
        self.controller.request({"action":"scan"})
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.controller.snapshot()["scan"]["status"], "restoring")
            self.controller.request({"action":"scan_cancel"})
        finally:
            release.set()
        wait_for(lambda: self.controller.snapshot()["status"] == "live")
        self.assertEqual(self.controller.snapshot()["scan"]["status"], "cancelled")

    def test_inflight_scan_read_stop_cancel_and_shutdown(self):
        for action in ("scan_cancel", "stop", "shutdown"):
            with self.subTest(action=action):
                self.controller.shutdown(restore_wallpaper=False)
                entered, release = threading.Event(), threading.Event()
                class BlockedSource(FakeSource):
                    def read_iq(inner, count):
                        if inner.frequency != 99200000:
                            entered.set(); release.wait(2)
                        return super().read_iq(count)
                self.calls = []
                self.audio = FakeAudio()
                self.controller = self.make_controller(factory=lambda f,d: BlockedSource(f,d,self.calls))
                self.live()
                self.controller.request({"action":"audio","enabled":True})
                self.controller.request({"action":"scan"})
                shutdown_thread = None
                try:
                    self.assertTrue(entered.wait(2))
                    if action == "shutdown":
                        shutdown_thread = threading.Thread(target=lambda: self.controller.shutdown(restore_wallpaper=False))
                        shutdown_thread.start()
                        self.assertTrue(self.controller._closing.wait(1))
                    else:
                        self.controller.request({"action":action})
                    self.assertIsNone(self.audio.sources[-1][0])
                finally:
                    release.set()
                if shutdown_thread is not None:
                    shutdown_thread.join(2)
                    self.assertFalse(shutdown_thread.is_alive())
                else:
                    wait_for(lambda: self.controller.snapshot()["status"] == ("live" if action == "scan_cancel" else "idle"))
                self.assertEqual({c[-1] for c in self.calls}, {self.controller.thread.ident})
                self.assertEqual(len([c for c in self.calls if c[:2] == ("open",99200000)]), 2 if action == "scan_cancel" else 1)
                self.assertTrue(all(f is None or f == 99200000 for f,_,_ in self.audio.sources))

    def test_new_actions_require_token_and_null_origin_does_not_leak_favorites(self):
        self.controller.request({"action":"station_save","frequency_hz":93400000,"name":"私人备注"})
        server = BridgeServer(0, controller=self.controller)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.01})
        thread.start()
        try:
            for action in ("scan", "scan_cancel", "station_save", "station_remove"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("POST", "/api/control", json.dumps({"action":action}), {"Content-Type":"application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read(); conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/state", headers={"Origin":"null"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            state = json.loads(response.read())
            self.assertNotIn("radio", state)
            self.assertNotIn("私人备注", json.dumps(state, ensure_ascii=False))
            conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
