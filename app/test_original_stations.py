"""No SDR/audio dependencies: favorites persistence and validation contracts."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from original_stations import StationStore


class StationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "preferences.json"

    def _write_fixture(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _valid_fixture(self, **changes):
        data = dict(version=1, favorites=[], last_frequency_hz=None, volume=0.15, revision=0)
        data.update(changes)
        return data

    def test_safe_defaults_and_no_creation_until_changed(self):
        store = StationStore(self.path)
        self.assertEqual(store.snapshot(), dict(favorites=[], last_frequency_hz=None,
                         volume=0.15, error=None, revision=0))
        self.assertFalse(self.path.exists())
        store.remember()
        self.assertFalse(self.path.exists())

    def test_memory_store_saves_renames_sorts_and_removes(self):
        store = StationStore()
        store.save(99_200_000, "电台 B")
        store.save(93_400_000, "")
        self.assertEqual(store.snapshot()["favorites"][0]["name"], "93.4 MHz")
        store.save(93_400_000, "  本地广播  ")
        self.assertEqual(len(store.snapshot()["favorites"]), 2)
        self.assertEqual(store.snapshot()["favorites"][0]["name"], "本地广播")
        store.remove("99200000")
        state = store.snapshot()
        self.assertEqual(len(state["favorites"]), 1)
        store.remove("99200000")
        self.assertEqual(store.snapshot(), state)

    def test_persistent_utf8_round_trip_and_revision(self):
        store = StationStore(self.path)
        store.save(93_400_000, "中文电台")
        store.remember(frequency_hz=145_500_000, volume=0.25)
        self.assertEqual(StationStore(self.path).snapshot(), store.snapshot())
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("中文电台", raw)
        self.assertNotIn("enabled", raw)
        self.assertNotIn("muted", raw)
        self.assertEqual(store.snapshot()["revision"], 2)

    def test_noop_does_not_rewrite_or_increment(self):
        store = StationStore(self.path)
        store.save(93_400_000, "新闻")
        state = store.snapshot()
        with patch("original_stations.os.replace", side_effect=AssertionError("unexpected write")):
            store.save(93_400_000, "新闻")
            store.remember(volume=0.15)
        self.assertEqual(store.snapshot(), state)

    def test_snapshot_is_independent(self):
        store = StationStore()
        store.save(93_400_000, "新闻")
        state = store.snapshot()
        state["favorites"][0]["name"] = "changed"
        state["favorites"].clear()
        self.assertEqual(store.snapshot()["favorites"][0]["name"], "新闻")

    def test_invalid_inputs_never_partially_change_state(self):
        store = StationStore()
        invalid_frequencies = [True, None, "93400000", 93_400_000.0, 87_499_999, 108_000_001]
        invalid_names = [None, 3, "a" * 61, "广播\n", "\t广播", "a\x7fb", "a\u202eb", "\ud800"]
        invalid_ids = [None, 93400000, "../../file", "093400000", "999", "９３４０００００", "9" * 1000]
        initial = store.snapshot()
        for value in invalid_frequencies:
            with self.subTest(frequency=value), self.assertRaises(ValueError):
                store.save(value, "x")
        for value in invalid_names:
            with self.subTest(name=repr(value)), self.assertRaises(ValueError):
                store.save(93_400_000, value)
        for value in invalid_ids:
            with self.subTest(id=value), self.assertRaises(ValueError):
                store.remove(value)
        for value in [True, "0.2", -0.1, 1.1, 10 ** 1000, float("nan"), float("inf")]:
            with self.subTest(volume=value), self.assertRaises(ValueError):
                store.remember(frequency_hz=93_400_000, volume=value)
        for value in [True, "93400000", 1.2, 499_999, 6_000_000_001]:
            with self.subTest(remember=value), self.assertRaises(ValueError):
                store.remember(frequency_hz=value, volume=0.2)
        self.assertEqual(store.snapshot(), initial)

    def test_valid_boundary_values(self):
        store = StationStore()
        store.save(87_500_000, "低")
        store.save(108_000_000, "高")
        for frequency, volume in [(500_000, 0), (1_766_000_000, 1)]:
            store.remember(frequency_hz=frequency, volume=volume)
            self.assertEqual(store.snapshot()["last_frequency_hz"], frequency)
            self.assertEqual(store.snapshot()["volume"], volume)

    def test_favorite_limit_still_allows_rename(self):
        store = StationStore()
        for i in range(100):
            store.save(87_500_000 + i * 100_000, str(i))
        store.save(87_500_000, "改名")
        with self.assertRaises(ValueError):
            store.save(108_000_000, "超限")
        self.assertEqual(len(store.snapshot()["favorites"]), 100)
        self.assertEqual(store.snapshot()["favorites"][0]["name"], "改名")

    def test_corrupt_file_is_preserved_and_writes_are_blocked(self):
        for raw in [b"{broken", b"\xff", b"[]", b" " * 131_073]:
            with self.subTest(raw=raw[:20]):
                self.path.write_bytes(raw)
                store = StationStore(self.path)
                self.assertTrue(store.snapshot()["error"])
                self.assertEqual(store.snapshot()["favorites"], [])
                self.assertEqual(store.snapshot()["volume"], 0.15)
                for action in [lambda: store.save(93_400_000, "新闻"),
                               lambda: store.remember(volume=0.2),
                               lambda: store.remove("93400000")]:
                    with self.assertRaises(RuntimeError):
                        action()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_strict_file_schema_validation_and_whole_file_commit(self):
        favorite = dict(id="93400000", frequency_hz=93_400_000, name="新闻")
        bad_data = [
            self._valid_fixture(version=2), self._valid_fixture(version=True),
            self._valid_fixture(enabled=True), self._valid_fixture(revision=-1),
            self._valid_fixture(revision=True), self._valid_fixture(volume=float("nan")),
            self._valid_fixture(last_frequency_hz=True), self._valid_fixture(favorites={}),
            self._valid_fixture(favorites=[favorite, favorite]),
            self._valid_fixture(favorites=[dict(favorite, unknown="x")]),
            self._valid_fixture(favorites=[dict(favorite, id="wrong")]),
            self._valid_fixture(favorites=[favorite, dict(id="1", frequency_hz=1, name="bad")]),
        ]
        for data in bad_data:
            with self.subTest(data=data):
                self._write_fixture(data)
                before = self.path.read_bytes()
                state = StationStore(self.path).snapshot()
                self.assertTrue(state["error"])
                self.assertEqual(state["favorites"], [])
                self.assertIsNone(state["last_frequency_hz"])
                self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_json_keys_are_rejected(self):
        raw = json.dumps(self._valid_fixture())[:-1] + ', "volume": 1}'
        self.path.write_text(raw, encoding="utf-8")
        self.assertIn("重复", StationStore(self.path).snapshot()["error"])

    def test_write_failure_keeps_memory_disk_and_cleans_temporary(self):
        store = StationStore(self.path)
        store.save(93_400_000, "原电台")
        before, raw = store.snapshot(), self.path.read_bytes()
        with patch("original_stations.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                store.remember(frequency_hz=99_200_000, volume=0.9)
        after = store.snapshot()
        self.assertTrue(after.pop("error"))
        before.pop("error")
        self.assertEqual(after, before)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        store.remember(frequency_hz=99_200_000, volume=0.2)
        self.assertIsNone(store.snapshot()["error"])
        self.assertEqual(StationStore(self.path).snapshot(), store.snapshot())

    def test_failed_save_and_remove_are_atomic(self):
        store = StationStore(self.path)
        store.save(93_400_000, "原电台")
        with patch("original_stations.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                store.save(93_400_000, "新名称")
            with self.assertRaises(OSError):
                store.remove("93400000")
        self.assertEqual(store.snapshot()["favorites"][0]["name"], "原电台")

    def test_flush_failure_preserves_last_good_file(self):
        store = StationStore(self.path)
        store.remember(volume=0.2)
        raw = self.path.read_bytes()
        with patch("original_stations.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save(93_400_000, "新闻")
        self.assertEqual(store.snapshot()["favorites"], [])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_unreadable_file_uses_safe_defaults_and_blocks_overwrite(self):
        self._write_fixture(self._valid_fixture(volume=0.9))
        with patch("original_stations.Path.open", side_effect=PermissionError("denied")):
            store = StationStore(self.path)
        self.assertEqual(store.snapshot()["volume"], 0.15)
        self.assertTrue(store.snapshot()["error"])
        with self.assertRaises(RuntimeError):
            store.remember(volume=0.2)

    def test_concurrent_updates_are_serialized(self):
        store = StationStore(self.path)
        def save(i):
            store.save(87_500_000 + i * 100_000, f"电台 {i}")
            store.remember(volume=i / 100)
            return store.snapshot()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(save, range(30)))
        self.assertEqual(len(store.snapshot()["favorites"]), 30)
        self.assertEqual(StationStore(self.path).snapshot(), store.snapshot())
        self.assertTrue(all(state["error"] is None for state in snapshots))

    def test_nested_directory_is_created(self):
        path = self.path.parent / "config" / "radio" / "stations.json"
        store = StationStore(path)
        store.remember(volume=0.2)
        self.assertEqual(StationStore(path).snapshot(), store.snapshot())


if __name__ == "__main__":
    unittest.main()
