"""Hardware-free lifecycle and loopback HTTP security tests."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from original_bridge import (APPLICATION, PROTOCOL_VERSION, BridgeServer,
    ConflictError, ReceiverController, StartupTasks, apply_startup_wallpaper,
    auto_start_receiver, is_existing_bridge, main)
from original_stations import StationStore


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Condition was not reached before timeout")


class FakeWallpaper:
    def __init__(self):
        self.applied = 0
        self.restored = 0

    def status(self):
        return {"available": True, "active": self.applied > self.restored}

    def apply(self):
        self.applied += 1

    def restore(self):
        self.restored += 1


class FakeSource:
    def __init__(self, frequency, demo, calls, fail_read=False):
        self.frequency = frequency
        self.sample_rate = 2_400_000
        self.calls = calls
        self.reading = False
        self.closed = False
        self.fail_read = fail_read
        self.calls.append(("open", frequency, demo, threading.get_ident()))

    def read_iq(self, count):
        if self.closed:
            raise AssertionError("Read after close")
        self.reading = True
        self.calls.append(("read", self.frequency, threading.get_ident()))
        try:
            time.sleep(0.004)
            if self.fail_read:
                raise RuntimeError("USB disconnected")
            return [complex(1, 0)] * count
        finally:
            self.reading = False

    def close(self):
        if self.reading or self.closed:
            raise AssertionError("Unsafe or duplicate receiver close")
        self.closed = True
        self.calls.append(("close", self.frequency, threading.get_ident()))


class FakeProcessor:
    def process(self, iq, sample_rate, frequency, timestamp):
        return {"frequency_hz": frequency, "sample_rate_hz": sample_rate,
                "low_hz": frequency - sample_rate / 2,
                "high_hz": frequency + sample_rate / 2,
                "levels": [0.25] * 256, "noise_floor_db": -70,
                "peak_hz": frequency + 10_000, "peak_db_above_noise": 12,
                "timestamp": timestamp}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.wallpaper = FakeWallpaper()
        self.controller = self.make_controller()

    def make_controller(self, factory=None):
        return ReceiverController(
            station_store=StationStore(),
            frequency=100_000_000,
            source_factory=factory or (lambda frequency, demo: FakeSource(frequency, demo, self.calls)),
            processor_factory=FakeProcessor, wallpaper_factory=lambda: self.wallpaper)

    def tearDown(self):
        self.controller.shutdown()

    def live(self):
        return self.controller.snapshot()["sequence"] > 0

    def test_start_is_idle_and_requires_explicit_source_selection(self):
        self.assertEqual(self.controller.snapshot()["status"], "idle")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.thread.daemon)
        self.controller.request({"action": "start"})
        wait_for(self.live)
        self.assertEqual(self.calls[0][0:3], ("open", 100_000_000, False))
        self.assertFalse(self.controller.snapshot()["demo"])
        with self.assertRaises(ConflictError):
            self.controller.request({"action": "start", "demo": True})

    def test_single_owner_tune_reopens_and_stop_preserves_last_frame(self):
        self.controller.request({"action": "start", "demo": True})
        wait_for(self.live)
        first = self.controller.snapshot()
        self.controller.request({"action": "tune", "frequency_hz": 101_100_000})
        wait_for(lambda: self.controller.snapshot()["sequence"] > first["sequence"])
        tuned = self.controller.snapshot()
        self.assertEqual(tuned["generation"], first["generation"] + 1)
        self.assertEqual(tuned["frame"]["frequency_hz"], 101_100_000)
        self.controller.request({"action": "stop"})
        wait_for(lambda: self.controller.snapshot()["status"] == "idle")
        stopped = self.controller.snapshot()
        time.sleep(0.12)
        self.assertEqual(self.controller.snapshot()["sequence"], stopped["sequence"])
        self.assertEqual(self.controller.snapshot()["frame"], stopped["frame"])
        opens = [call for call in self.calls if call[0] == "open"]
        closes = [call for call in self.calls if call[0] == "close"]
        self.assertEqual(len(opens), 2)
        self.assertEqual(len(closes), 2)
        self.assertEqual({call[-1] for call in self.calls}, {self.controller.thread.ident})

    def test_idle_tune_does_not_open_hardware(self):
        self.controller.request({"action": "tune", "frequency_hz": 144_000_000})
        state = self.controller.snapshot()
        self.assertEqual(state["frequency_hz"], 144_000_000)
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["status"], "idle")
        self.assertEqual(self.calls, [])

    def test_no_fake_fallback_when_open_fails(self):
        self.controller.shutdown()
        def fail(frequency, demo):
            self.calls.append((frequency, demo))
            raise RuntimeError("Device occupied")
        self.controller = self.make_controller(fail)
        self.controller.request({"action": "start"})
        wait_for(lambda: self.controller.snapshot()["status"] == "error")
        state = self.controller.snapshot()
        self.assertIn("occupied", state["error"])
        self.assertFalse(state["demo"])
        self.assertIsNone(state["frame"])
        self.assertEqual(self.calls, [(100_000_000, False)])

    def test_read_failure_closes_on_owner_thread(self):
        self.controller.shutdown()
        self.controller = self.make_controller(
            lambda frequency, demo: FakeSource(frequency, demo, self.calls, fail_read=True))
        self.controller.request({"action": "start"})
        wait_for(lambda: self.controller.snapshot()["status"] == "error")
        self.assertEqual([call[0] for call in self.calls], ["open", "read", "close"])
        self.assertEqual({call[-1] for call in self.calls}, {self.controller.thread.ident})

    def test_late_read_error_preserves_last_real_frame(self):
        self.controller.shutdown()
        opened = []
        def factory(frequency, demo):
            source = FakeSource(frequency, demo, self.calls)
            opened.append(source)
            return source
        self.controller = self.make_controller(factory)
        self.controller.request({"action": "start"})
        wait_for(self.live)
        before = self.controller.snapshot()
        opened[0].fail_read = True
        wait_for(lambda: self.controller.snapshot()["status"] == "error")
        after = self.controller.snapshot()
        self.assertEqual(after["frame"], before["frame"])
        self.assertEqual(after["sequence"], before["sequence"])
        self.assertFalse(after["demo"])
        self.assertIn("disconnected", after["error"])

    def test_stop_during_open_is_nonblocking_and_does_not_publish(self):
        self.controller.shutdown()
        entered, release = threading.Event(), threading.Event()
        def slow_open(frequency, demo):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test source timed out")
            return FakeSource(frequency, demo, self.calls)
        self.controller = self.make_controller(slow_open)
        try:
            self.controller.request({"action": "start"})
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            self.controller.request({"action": "stop"})
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertEqual(self.controller.snapshot()["status"], "stopping")
        finally:
            release.set()
        wait_for(lambda: self.controller.snapshot()["status"] == "idle")
        self.assertEqual(self.controller.snapshot()["sequence"], 0)
        self.assertEqual(len([call for call in self.calls if call[0] == "close"]), 1)

    def test_publish_rate_and_snapshot_are_bounded(self):
        self.controller.request({"action": "start", "demo": True})
        wait_for(self.live)
        first = self.controller.snapshot()["sequence"]
        time.sleep(0.35)
        state = self.controller.snapshot()
        self.assertGreaterEqual(state["sequence"] - first, 2)
        self.assertLessEqual(state["sequence"] - first, 4)
        state["frame"]["levels"][0] = 99
        self.assertNotEqual(self.controller.snapshot()["frame"]["levels"][0], 99)

    def test_validation_and_wallpaper_controls(self):
        for payload in ([], {}, {"action": "oops"}, {"action": "tune"},
                        {"action": "start", "frequency_hz": True},
                        {"action": "start", "frequency_hz": 100.5},
                        {"action": "start", "frequency_hz": 10},
                        {"action": "start", "demo": "false"},
                        {"action": "stop", "demo": False},
                        {"action": "stop", "frequency_hz": 100_000_000},
                        {"action": "start", "unknown": 3}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.controller.request(payload)
        self.controller.request({"action": "wallpaper"})
        self.controller.request({"action": "restore"})
        self.assertEqual(self.wallpaper.applied, 1)
        self.assertEqual(self.wallpaper.restored, 1)

    def test_auto_start_uses_real_device_and_saved_controller_frequency(self):
        self.controller.request({"action": "tune", "frequency_hz": 99_200_000})
        self.assertTrue(auto_start_receiver(self.controller))
        wait_for(self.live)
        self.assertEqual(self.calls[0][0:3], ("open", 99_200_000, False))
        self.assertFalse(self.controller.snapshot()["demo"])

    def test_keep_wallpaper_shutdown_releases_receiver_without_restoring(self):
        self.controller.request({"action": "start"})
        wait_for(self.live)
        self.controller.shutdown(restore_wallpaper=False)
        self.assertFalse(self.controller.thread.is_alive())
        self.assertEqual([call[0] for call in self.calls].count("close"), 1)
        self.assertEqual(self.wallpaper.restored, 0)
        # Duplicate cleanup must not undo the explicitly requested keep mode.
        self.controller.shutdown()
        self.assertEqual(self.wallpaper.restored, 0)

    def test_default_shutdown_still_restores_once(self):
        self.controller.request({"action": "start"})
        wait_for(self.live)
        self.controller.shutdown()
        self.assertFalse(self.controller.thread.is_alive())
        self.assertEqual([call[0] for call in self.calls].count("close"), 1)
        self.assertEqual(self.wallpaper.restored, 1)
        self.controller.shutdown()
        self.assertEqual(self.wallpaper.restored, 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.static = Path(self.temp.name)
        (self.static / "index.html").write_text("<h1>Radio terrain</h1>", encoding="utf-8")
        (self.static / "secret.txt").write_text("not public", encoding="utf-8")
        self.wallpaper = FakeWallpaper()
        self.calls = []
        self.controller = ReceiverController(frequency=100_000_000,
            station_store=StationStore(),
            source_factory=lambda f, d: FakeSource(f, d, self.calls),
            processor_factory=FakeProcessor, wallpaper_factory=lambda: self.wallpaper)
        self.server = BridgeServer(port=0, controller=self.controller, static_dir=self.static)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.controller.shutdown()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def control(self, payload, extra=None):
        headers = {"Content-Type": "application/json", "X-SDR-Token": self.controller.token,
                   "Origin": self.origin}
        headers.update(extra or {})
        return self.request("POST", "/api/control", json.dumps(payload), headers)

    def test_bind_and_safe_snapshot_without_token(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        code, headers, body = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertEqual(state["status"], "idle")
        self.assertEqual(set(state), {"application", "protocol_version", "stream_id", "status", "demo", "frequency_hz", "sequence", "generation", "frame", "error", "wallpaper", "audio", "radio", "scan", "receiver"})
        self.assertEqual(state["application"], APPLICATION)
        self.assertEqual(state["protocol_version"], PROTOCOL_VERSION)
        self.assertNotIn(self.controller.token, body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls, [])

    def test_file_wallpaper_may_only_read_state(self):
        code, headers, body = self.request("GET", "/api/state", headers={"Origin": "null"})
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "null")
        self.assertNotIn("wallpaper", json.loads(body))
        self.assertNotIn(self.controller.token, body.decode())
        for path in ("/api/session", "/", "/index.html"):
            code, headers, _ = self.request("GET", path, headers={"Origin": "null"})
            self.assertEqual(code, 403)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.control({"action": "start"}, {"Origin": "null"})[0], 403)

    def test_foreign_origin_and_rebinding_host_rejected(self):
        for path in ("/api/state", "/api/session", "/"):
            self.assertEqual(self.request("GET", path, headers={"Origin": "https://evil.example"})[0], 403)
            self.assertEqual(self.request("GET", path, headers={"Host": f"evil.example:{self.server.server_port}"})[0], 403)
        self.assertEqual(self.control({"action": "start"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "http://evil.example/api/session",
            headers={"Host": f"127.0.0.1:{self.server.server_port}"})[0], 400)

    def test_session_and_token_required_for_control(self):
        code, _, body = self.request("GET", "/api/session", headers={"Origin": self.origin})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["token"], self.controller.token)
        self.assertGreaterEqual(len(self.controller.token), 43)
        self.assertEqual(self.request("POST", "/api/control", '{"action":"start"}',
            {"Content-Type": "application/json", "Origin": self.origin})[0], 403)
        self.assertEqual(self.control({"action": "start"}, {"X-SDR-Token": "incorrect"})[0], 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.control({"action": "start", "demo": True})[0], 200)
        wait_for(lambda: self.controller.snapshot()["sequence"] > 0)
        self.assertEqual(self.control({"action": "start"})[0], 409)
        self.assertEqual(self.control({"action": "tune", "frequency_hz": 101_200_000})[0], 200)
        wait_for(lambda: self.controller.snapshot()["status"] == "live")
        self.assertEqual(self.control({"action": "stop"})[0], 200)
        wait_for(lambda: self.controller.snapshot()["status"] == "idle")

    def test_native_no_origin_with_token_is_allowed(self):
        code, _, _ = self.request("POST", "/api/control", '{"action":"tune","frequency_hz":101000000}',
            {"Content-Type": "application/json", "X-SDR-Token": self.controller.token})
        self.assertEqual(code, 200)
        self.assertEqual(self.controller.snapshot()["frequency_hz"], 101_000_000)

    def test_only_whitelisted_assets_are_served(self):
        code, headers, body = self.request("GET", "/?wallpaper=1")
        self.assertEqual(code, 200)
        self.assertIn(b"Radio terrain", body)
        self.assertIn("text/html", headers["Content-Type"])
        for path in ("/secret.txt", "/../settings.json", "/%2e%2e/settings.json",
                     "/original_bridge.py", "/assets/", "/style.css"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_bad_content_types_json_and_lengths_are_rejected(self):
        self.assertEqual(self.control({"action": "stop"}, {"Content-Type": "text/plain"})[0], 400)
        base = {"Content-Type": "application/json", "X-SDR-Token": self.controller.token}
        for body in ("{", "[]", "null", '{"action":"tune","frequency_hz":NaN}', "x" * 4097):
            self.assertEqual(self.request("POST", "/api/control", body, base)[0], 400)
        self.assertEqual(self.request("POST", "/api/control", "", base)[0], 400)
        self.assertEqual(self.request("POST", "/api/unknown", "{}", base)[0], 404)

    def test_cross_origin_preflight_only_allows_state_get(self):
        headers = {"Origin": "null", "Access-Control-Request-Method": "GET",
                   "Access-Control-Request-Private-Network": "true"}
        code, response_headers, _ = self.request("OPTIONS", "/api/state", headers=headers)
        self.assertEqual(code, 204)
        self.assertEqual(response_headers["Access-Control-Allow-Origin"], "null")
        self.assertEqual(response_headers["Access-Control-Allow-Private-Network"], "true")
        self.assertEqual(self.request("OPTIONS", "/api/session", headers=headers)[0], 403)
        headers["Access-Control-Request-Method"] = "POST"
        self.assertEqual(self.request("OPTIONS", "/api/control", headers=headers)[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/state", headers=headers)[0], 403)

    def test_existing_instance_detection_and_duplicate_open_are_read_only(self):
        port = self.server.server_port
        self.assertTrue(is_existing_bridge(port))
        with patch("original_bridge.webbrowser.open") as browser, \
                patch("original_bridge.launch_wallpaper_engine") as engine:
            self.assertEqual(main(["--port", str(port), "--auto-start", "--apply-wallpaper",
                                   "--keep-wallpaper", "--open"]), 0)
            browser.assert_called_once_with(f"http://127.0.0.1:{port}/", new=2)
            engine.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.wallpaper.applied, 0)
        self.assertEqual(self.wallpaper.restored, 0)
        self.assertEqual(self.controller.snapshot()["status"], "idle")


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.snapshot.return_value = {"wallpaper": {"available": True, "is_original": False}}
        self.cancelled = threading.Event()
        self.path_patch = patch("original_bridge.wallpaper_engine_executable", return_value=Path(__file__))
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()

    def test_running_engine_is_not_launched_again(self):
        with patch("original_bridge.wallpaper_engine_is_running", return_value=True), \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            self.assertTrue(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.1))
            launch.assert_not_called()
        self.controller.request.assert_called_once_with({"action": "wallpaper"})

    def test_missing_engine_is_launched_only_once_while_waiting(self):
        with patch("original_bridge.wallpaper_engine_is_running", side_effect=[False, False, True]), \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            self.assertTrue(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.2, retry_interval=0.01))
            launch.assert_called_once()
        self.controller.request.assert_called_once_with({"action": "wallpaper"})

    def test_original_desktop_does_not_switch_or_require_new_backup(self):
        self.controller.snapshot.return_value = {"wallpaper": {"available": True, "is_original": True}}
        with patch("original_bridge.wallpaper_engine_is_running", return_value=True), \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            self.assertTrue(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.1))
            launch.assert_not_called()
        self.controller.request.assert_not_called()

    def test_configuration_wait_and_apply_failure_are_retried(self):
        self.controller.snapshot.side_effect = [
            {"wallpaper": {"available": False, "message": "starting"}},
            {"wallpaper": {"available": True}}, {"wallpaper": {"available": True}}]
        self.controller.request.side_effect = [RuntimeError("not ready"), None]
        with patch("original_bridge.wallpaper_engine_is_running", return_value=True), \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            self.assertTrue(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.2, retry_interval=0.01))
            launch.assert_not_called()
        self.assertEqual(self.controller.request.call_count, 2)

    def test_cancelled_startup_does_nothing(self):
        self.cancelled.set()
        with patch("original_bridge.wallpaper_engine_is_running") as running, \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            self.assertFalse(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.1))
            running.assert_not_called()
            launch.assert_not_called()
        self.controller.request.assert_not_called()

    def test_failed_startup_is_bounded_and_does_not_launch_repeatedly(self):
        with patch("original_bridge.wallpaper_engine_is_running", return_value=False), \
                patch("original_bridge.launch_wallpaper_engine") as launch:
            before = time.monotonic()
            self.assertFalse(apply_startup_wallpaper(self.controller, self.cancelled, timeout=0.04, retry_interval=0.005))
            self.assertLess(time.monotonic() - before, 0.2)
            launch.assert_called_once()
        self.controller.request.assert_not_called()

    def test_startup_helpers_are_cancelled_and_joined(self):
        entered = threading.Event()
        def apply(controller, cancelled):
            entered.set()
            cancelled.wait(2)
        with patch("original_bridge.apply_startup_wallpaper", side_effect=apply), \
                patch("original_bridge.webbrowser.open") as browser:
            tasks = StartupTasks(self.controller, 8766)
            tasks.start(apply_wallpaper=True, open_browser=True)
            self.assertTrue(entered.wait(1))
            tasks.stop()
            self.assertTrue(all(not thread.is_alive() for thread in tasks.threads))
            browser.assert_not_called()

    def test_unknown_listener_is_not_treated_as_our_instance(self):
        with patch("original_bridge.BridgeServer", side_effect=OSError("port occupied")), \
                patch("original_bridge.is_existing_bridge", return_value=False), \
                patch("original_bridge.webbrowser.open") as browser:
            with self.assertRaises(OSError):
                main(["--open"])
            browser.assert_not_called()

    def test_wrong_application_or_protocol_is_rejected(self):
        for payload in ({"application": "unrelated", "protocol_version": PROTOCOL_VERSION},
                        {"application": APPLICATION, "protocol_version": 999}):
            response = Mock(status=200)
            response.read.return_value = json.dumps(payload).encode()
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch("original_bridge.build_opener", return_value=opener):
                self.assertFalse(is_existing_bridge(8766))
            opener.open.assert_called_once_with("http://127.0.0.1:8766/api/state", timeout=1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
