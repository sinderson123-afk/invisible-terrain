"""Isolated desktop-switch tests: no Wallpaper Engine or SDR process runs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from original_desktop import DesktopError, DesktopWallpaper


class DesktopWallpaperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exe = self.root / "Wallpaper Engine" / "wallpaper64.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.project = self.root / "Original Terrain" / "project.json"
        self.project.parent.mkdir()
        self.entry = self.project.with_name("index.html")
        self.entry.write_text("<!doctype html>", encoding="utf-8")
        self.project.write_text(json.dumps({"type": "web", "file": "index.html"}), encoding="utf-8")
        self.original = self.root / "old wallpaper" / "scene.pkg"
        self.original.parent.mkdir()
        self.original.touch()
        self.alternative = self.root / "another wallpaper.html"
        self.alternative.touch()
        self.journal_path = self.root / "session.json"
        self.config = self.exe.parent / "config.json"
        self.set_selection(self.original)
        self.client = self.new_client()

    def new_client(self):
        return DesktopWallpaper(self.exe, self.project, self.journal_path, confirmation_timeout=0)

    def set_selection(self, file, account="abc"):
        config = {account: {"general": {"wallpaperconfig": {
            "selectedwallpapers": {"Monitor0": {"file": str(file)}}}}}}
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def journal(self):
        return json.loads(self.journal_path.read_text(encoding="utf-8"))

    def fake_cli(self, args, **kwargs):
        self.assertIsInstance(args, list)
        self.assertEqual(args[:3], [str(self.exe), "-control", "openWallpaper"])
        self.assertEqual(args[-2:], ["-monitor", "0"])
        self.assertEqual(kwargs["cwd"], self.exe.parent)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 8)
        self.assertIn("creationflags", kwargs)
        self.assertTrue(self.journal_path.exists(), "Backup must precede the command")
        requested = Path(args[4])
        self.set_selection(self.entry if requested == self.project else requested)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    @patch("original_desktop.subprocess.run")
    def test_apply_backs_up_exact_path_before_cli(self, run):
        run.side_effect = self.fake_cli
        state = self.client.apply()
        self.assertEqual(state["state"], "active")
        self.assertTrue(state["is_original"])
        self.assertTrue(state["can_restore"])
        record = self.journal()
        self.assertEqual(record["original"], str(self.original))
        self.assertEqual(record["account"], "abc")
        self.assertEqual(record["monitor"], 0)
        self.assertEqual(record["history"], [])
        self.assertEqual(run.call_count, 1)

    @patch("original_desktop.subprocess.run")
    def test_repeated_apply_does_not_backup_original_itself(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        before = self.journal()["original"]
        self.new_client().apply()
        self.assertEqual(self.journal()["original"], before)
        self.assertEqual(run.call_count, 1)

    @patch("original_desktop.subprocess.run")
    def test_another_instance_restores_exact_previous_file(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        state = self.new_client().restore()
        self.assertTrue(state["restored"])
        self.assertFalse(state["recoverable"])
        self.assertEqual(state["current_file"], str(self.original))
        self.assertEqual(run.call_count, 2)

    @patch("original_desktop.subprocess.run")
    def test_failed_apply_keeps_backup_and_retry_reuses_it(self, run):
        run.side_effect = subprocess.TimeoutExpired("wallpaper64", 8)
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertEqual(self.journal()["state"], "apply_pending")
        before = self.journal()["started_at"]
        run.side_effect = self.fake_cli
        self.new_client().apply()
        self.assertEqual(self.journal()["started_at"], before)
        self.assertEqual(self.journal()["original"], str(self.original))

    @patch("original_desktop.subprocess.run")
    def test_manual_wallpaper_change_is_not_overwritten_and_history_survives(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        self.set_selection(self.alternative)
        with self.assertRaises(DesktopError):
            self.new_client().apply()
        result = self.new_client().restore()
        self.assertEqual(result["state"], "user_changed")
        self.assertEqual(result["current_file"], str(self.alternative))
        self.assertEqual(run.call_count, 1)
        self.new_client().apply()
        journal = self.journal()
        self.assertEqual(journal["original"], str(self.alternative))
        self.assertEqual(journal["history"][0]["original"], str(self.original))
        self.assertEqual(journal["history"][0]["state"], "user_changed")

    @patch("original_desktop.subprocess.run")
    def test_ambiguous_account_refused_before_backup_or_cli(self, run):
        settings = json.loads(self.config.read_text(encoding="utf-8"))
        settings["other-user"] = settings["abc"]
        self.config.write_text(json.dumps(settings), encoding="utf-8")
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertFalse(self.journal_path.exists())
        run.assert_not_called()

    @patch("original_desktop.subprocess.run")
    def test_missing_previous_asset_retains_restore_pending(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        self.original.unlink()
        with self.assertRaises(DesktopError):
            self.client.restore()
        self.assertEqual(self.journal()["state"], "restore_pending")
        self.assertEqual(run.call_count, 1)

    @patch("original_desktop.subprocess.run")
    def test_malformed_backup_is_never_replaced(self, run):
        invalid = '{"unexpected":"retain me"}'
        self.journal_path.write_text(invalid, encoding="utf-8")
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertEqual(self.journal_path.read_text(encoding="utf-8"), invalid)
        run.assert_not_called()

    @patch("original_desktop.subprocess.run")
    def test_original_without_backup_refuses_self_backup(self, run):
        self.set_selection(self.entry)
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertFalse(self.journal_path.exists())
        run.assert_not_called()

    @patch("original_desktop.subprocess.run")
    def test_zero_exit_without_confirmation_is_not_reported_success(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
        before = self.config.read_bytes()
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertEqual(self.journal()["state"], "apply_pending")
        self.assertEqual(self.config.read_bytes(), before, "Adapter must never write WE config")
        self.assertFalse(self.client.status()["is_original"])

    @patch("original_desktop.subprocess.run")
    def test_account_change_blocks_restore_command(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        self.set_selection(self.entry, "different-user")
        with self.assertRaises(DesktopError):
            self.new_client().apply()
        state = self.new_client().restore()
        self.assertEqual(state["state"], "user_changed")
        self.assertEqual(self.journal()["account"], "abc")
        self.assertEqual(run.call_count, 1)

    @patch("original_desktop.subprocess.run")
    def test_threaded_instances_share_lock(self, run):
        def delayed_cli(*args, **kwargs):
            time.sleep(0.04)
            return self.fake_cli(*args, **kwargs)
        run.side_effect = delayed_cli
        errors = []
        def apply():
            try:
                self.new_client().apply()
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=apply)
        second = threading.Thread(target=apply)
        first.start()
        second.start()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.journal()["original"], str(self.original))

    @patch("original_desktop.subprocess.run")
    def test_project_json_selected_path_is_also_exact_target(self, run):
        run.side_effect = self.fake_cli
        self.client.apply()
        self.set_selection(self.project)
        self.assertTrue(self.new_client().status()["is_original"])
        self.new_client().restore()
        self.assertEqual(self.journal()["state"], "restored")

    @patch("original_desktop.subprocess.run")
    def test_equivalent_path_cannot_be_saved_as_its_own_backup(self, run):
        alias = self.entry.parent / ".." / self.entry.parent.name / self.entry.name
        self.set_selection(alias)
        self.assertTrue(self.client.status()["is_original"])
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertFalse(self.journal_path.exists())
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows short-name regression")
    @patch("original_desktop.subprocess.run")
    def test_windows_short_path_is_the_same_wallpaper(self, run):
        import ctypes
        function = ctypes.windll.kernel32.GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(str(self.entry.resolve()), buffer, len(buffer))
        if not count or count >= len(buffer) or buffer.value == str(self.entry.resolve()):
            self.skipTest("This filesystem does not provide a distinct short pathname")
        self.set_selection(buffer.value)
        self.assertTrue(self.client.status()["is_original"])
        with self.assertRaises(DesktopError):
            self.client.apply()
        self.assertFalse(self.journal_path.exists())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
