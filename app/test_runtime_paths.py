"""Isolated state paths and read-only Wallpaper Engine discovery tests."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import original_desktop
import runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_override_resolution_and_read_do_not_create_directory(self):
        destination = self.root / "personal data" / "terrain"
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_DATA_DIR": str(destination)}):
            self.assertEqual(runtime_paths.data_dir(), destination.resolve())
            self.assertEqual(runtime_paths.state_path("radio_stations.json"), destination / "radio_stations.json")
        self.assertFalse(destination.exists())

    def test_explicit_creation_only_creates_directory_not_file(self):
        destination = self.root / "state"
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_DATA_DIR": str(destination)}):
            filename = runtime_paths.state_path("radio_stations.json", create=True)
        self.assertTrue(destination.is_dir())
        self.assertFalse(filename.exists())

    def test_windows_local_appdata(self):
        with patch("runtime_paths.sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(runtime_paths.data_dir(), self.root / "InvisibleTerrain")
        self.assertFalse((self.root / "InvisibleTerrain").exists())

    def test_windows_home_fallback(self):
        with patch("runtime_paths.sys.platform", "win32"), patch("runtime_paths.Path.home", return_value=self.root):
            self.assertEqual(runtime_paths.data_dir(), self.root / "AppData" / "Local" / "InvisibleTerrain")

    def test_xdg_directory(self):
        with patch("runtime_paths.sys.platform", "linux"), patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            self.assertEqual(runtime_paths.data_dir(), self.root / "invisible-terrain")

    def test_xdg_home_fallback(self):
        with patch("runtime_paths.sys.platform", "linux"), patch("runtime_paths.Path.home", return_value=self.root):
            self.assertEqual(runtime_paths.data_dir(), self.root / ".local" / "state" / "invisible-terrain")

    def test_filename_validation_is_cross_platform(self):
        for invalid in (None, "", ".", "..", "../secret", "folder/file", r"folder\file", "C:secret", "bad\x00name",
                        "file. ", "file.", " spaced", "CON", "nul.json", "COM1", "LPT9.log"):
            with self.subTest(name=invalid), self.assertRaises(ValueError):
                runtime_paths.state_path(invalid, create=True)

    def test_import_does_not_create_state_directory(self):
        destination = self.root / "must-not-exist"
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_DATA_DIR": str(destination)}):
            importlib.reload(runtime_paths)
        self.assertFalse(destination.exists())


class WallpaperDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.registry = patch("original_desktop._registry_steam_roots", return_value=[])
        self.registry_mock = self.registry.start()
        self.addCleanup(self.registry.stop)

    def install(self, library, filename="wallpaper64.exe"):
        executable = library / "steamapps" / "common" / "wallpaper_engine" / filename
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        return executable.resolve()

    def library_file(self, steam, contents, folder="steamapps"):
        target = steam / folder / "libraryfolders.vdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8-sig")
        return target

    def test_explicit_executable_wins(self):
        executable = self.install(self.root / "custom library")
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_WALLPAPER_EXE": str(executable)}):
            self.assertEqual(original_desktop.find_wallpaper_executable(), executable)
        self.registry_mock.assert_not_called()

    def test_missing_explicit_executable_does_not_select_another_install(self):
        self.registry_mock.return_value = [self.root]
        self.install(self.root)
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_WALLPAPER_EXE": str(self.root / "missing.exe")}):
            self.assertIsNone(original_desktop.find_wallpaper_executable())
        self.registry_mock.assert_not_called()

    def test_registry_install_location(self):
        executable = self.install(self.root / "steam")
        self.registry_mock.return_value = [self.root / "steam"]
        self.assertEqual(original_desktop.find_wallpaper_executable(), executable)

    def test_standard_program_files_location(self):
        executable = self.install(self.root / "Steam")
        with patch.dict(os.environ, {"ProgramFiles(x86)": str(self.root)}):
            self.assertEqual(original_desktop.find_wallpaper_executable(), executable)

    def test_modern_library_folder_with_spaces_and_unicode(self):
        steam = self.root / "steam"
        library = self.root / "游戏 library"
        executable = self.install(library)
        encoded = str(library).replace("\\", "\\\\")
        self.library_file(steam, '"libraryfolders" { "1" { "path" "' + encoded + '" "apps" { "431960" "123" } } }')
        self.registry_mock.return_value = [steam]
        self.assertEqual(original_desktop.find_wallpaper_executable(), executable)

    def test_legacy_library_folder(self):
        steam = self.root / "steam"
        library = self.root / "legacy library"
        executable = self.install(library)
        encoded = str(library).replace("\\", "\\\\")
        self.library_file(steam, '"LibraryFolders" { "1" "' + encoded + '" }', folder="config")
        self.registry_mock.return_value = [steam]
        self.assertEqual(original_desktop.find_wallpaper_executable(), executable)

    def test_64_bit_preferred_and_32_bit_fallback(self):
        library = self.root / "steam"
        executable32 = self.install(library, "wallpaper32.exe")
        self.registry_mock.return_value = [library]
        self.assertEqual(original_desktop.find_wallpaper_executable(), executable32)
        executable64 = self.install(library)
        self.assertEqual(original_desktop.find_wallpaper_executable(), executable64)

    def test_missing_and_unreadable_vdf_are_not_fatal(self):
        self.registry_mock.return_value = [self.root / "missing"]
        self.assertIsNone(original_desktop.find_wallpaper_executable())
        self.registry_mock.return_value = [self.root]
        invalid = self.library_file(self.root, "")
        invalid.write_bytes(b"\xff\xfeinvalid")
        self.assertIsNone(original_desktop.find_wallpaper_executable())

    def test_no_install_does_not_launch_or_create_state(self):
        destination = self.root / "data"
        with patch.dict(os.environ, {"INVISIBLE_TERRAIN_DATA_DIR": str(destination)}), patch("original_desktop.subprocess.run") as run:
            client = original_desktop.DesktopWallpaper()
            state = client.status()
        self.assertFalse(state["available"])
        self.assertEqual(state["state"], "error")
        self.assertEqual(client.session_file, destination / "original_wallpaper_session.json")
        self.assertFalse(destination.exists())
        run.assert_not_called()

    def test_explicit_journal_directory_created_only_by_save(self):
        journal = self.root / "nested" / "state" / "session.json"
        client = original_desktop.DesktopWallpaper(executable=self.root / "missing.exe", session_file=journal)
        client.status()
        self.assertFalse(journal.parent.exists())
        client._save({"example": "journal"})
        self.assertTrue(journal.is_file())
        self.assertFalse(journal.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
