"""Thread-safe FM favorites and safe receiver preferences.

The versioned JSON format rejects unknown fields and duplicate JSON keys. A
damaged or unsupported file is never overwritten: back it up and repair/remove
it, then restart the bridge. Listening enabled/muted flags are intentionally not
part of the format. Path=None is an entirely in-memory store for tests.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import threading
import unicodedata


class StationStore:
    VERSION = 1
    MAX_FAVORITES = 100
    MAX_FILE_BYTES = 131_072
    DEFAULT_VOLUME = 0.15

    def __init__(self, path=None):
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._favorites = []
        self._last_frequency = None
        self._volume = self.DEFAULT_VOLUME
        self._revision = 0
        self._error = None
        self._write_blocked = False
        if self._path is not None:
            self._load()

    @staticmethod
    def _frequency(value, *, favorite=False):
        low, high = (87_500_000, 108_000_000) if favorite else (500_000, 6_000_000_000)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"频率必须是 {low}–{high} 范围内的整数 Hz")
        return value

    @staticmethod
    def _name(value, frequency):
        if not isinstance(value, str):
            raise ValueError("电台名称必须是文本")
        # Check before stripping: line breaks/tabs are not accepted in names.
        if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
            raise ValueError("电台名称不能包含控制字符")
        value = value.strip() or f"{frequency / 1_000_000:.1f} MHz"
        if len(value) > 60:
            raise ValueError("电台名称最多 60 个字符")
        return value

    @staticmethod
    def _validated_volume(value):
        if (type(value) not in (int, float) or not 0 <= value <= 1
                or not math.isfinite(value)):
            raise ValueError("音量必须是 0–1 范围内的有限数字")
        return float(value)

    @staticmethod
    def _unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复的 JSON 字段：{key}")
            result[key] = value
        return result

    @staticmethod
    def _fields(value, fields):
        if type(value) is not dict or set(value) != set(fields):
            raise ValueError("电台数据字段缺失或包含未知字段")

    def _load(self):
        try:
            with self._path.open("rb") as source:
                raw = source.read(self.MAX_FILE_BYTES + 1)
            if len(raw) > self.MAX_FILE_BYTES:
                raise ValueError("电台数据文件过大")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=self._unique_keys)
            self._fields(data, {"version", "favorites", "last_frequency_hz", "volume", "revision"})
            if type(data["version"]) is not int or data["version"] != self.VERSION:
                raise ValueError("不支持的电台数据版本")
            if type(data["revision"]) is not int or data["revision"] < 0:
                raise ValueError("无效的电台数据修订号")
            if type(data["favorites"]) is not list or len(data["favorites"]) > self.MAX_FAVORITES:
                raise ValueError("电台收藏列表无效或超过 100 个")
            favorites, seen = [], set()
            for item in data["favorites"]:
                self._fields(item, {"id", "frequency_hz", "name"})
                frequency = self._frequency(item["frequency_hz"], favorite=True)
                if type(item["id"]) is not str or item["id"] != str(frequency) or item["id"] in seen:
                    raise ValueError("电台标识无效或重复")
                name = self._name(item["name"], frequency)
                favorites.append(dict(id=str(frequency), frequency_hz=frequency, name=name))
                seen.add(item["id"])
            frequency = data["last_frequency_hz"]
            if frequency is not None:
                self._frequency(frequency)
            volume = self._validated_volume(data["volume"])
            # Commit only after the entire file has passed validation.
            self._favorites = sorted(favorites, key=lambda item: item["frequency_hz"])
            self._last_frequency = frequency
            self._volume = volume
            self._revision = data["revision"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            self._write_blocked = True
            self._error = f"无法读取电台记忆；原文件未改动，请先备份并修复文件后重启：{exc}"

    def snapshot(self):
        with self._lock:
            return dict(favorites=[dict(item) for item in self._favorites],
                        last_frequency_hz=self._last_frequency,
                        volume=self._volume, error=self._error, revision=self._revision)

    def _ensure_writable(self):
        if self._write_blocked:
            raise RuntimeError(self._error)

    def _persist(self, data):
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        descriptor = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent)
            output = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None  # The file object now owns the descriptor.
            with output:
                json.dump(data, output, ensure_ascii=False, allow_nan=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            temporary = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass  # Preserve the original write exception, if any.

    def _commit(self, favorites, frequency, volume):
        if (favorites == self._favorites and frequency == self._last_frequency
                and volume == self._volume):
            return
        data = dict(version=self.VERSION, favorites=favorites,
                    last_frequency_hz=frequency, volume=volume, revision=self._revision + 1)
        try:
            self._persist(data)
        except (OSError, ValueError, UnicodeError) as exc:
            self._error = f"保存电台记忆失败；现有记忆保持不变：{exc}"
            raise OSError(self._error) from exc
        self._favorites = favorites
        self._last_frequency = frequency
        self._volume = volume
        self._revision = data["revision"]
        self._error = None

    def save(self, frequency_hz, name):
        with self._lock:
            self._ensure_writable()
            frequency = self._frequency(frequency_hz, favorite=True)
            station_id = str(frequency)
            name = self._name(name, frequency)
            favorites = [dict(item) for item in self._favorites if item["id"] != station_id]
            if len(favorites) >= self.MAX_FAVORITES:
                raise ValueError("最多收藏 100 个电台，请先删除不需要的收藏")
            favorites.append(dict(id=station_id, frequency_hz=frequency, name=name))
            favorites.sort(key=lambda item: item["frequency_hz"])
            self._commit(favorites, self._last_frequency, self._volume)

    def remove(self, station_id):
        with self._lock:
            self._ensure_writable()
            if type(station_id) is not str or not station_id.isascii() or not station_id.isdecimal():
                raise ValueError("电台标识必须是频率对应的数字文本")
            # IDs are canonical decimal Hz, not array indexes or file paths.
            if len(station_id) > 9:
                raise ValueError("无效的电台标识")
            frequency = self._frequency(int(station_id), favorite=True)
            if str(frequency) != station_id:
                raise ValueError("无效的电台标识")
            favorites = [dict(item) for item in self._favorites if item["id"] != station_id]
            self._commit(favorites, self._last_frequency, self._volume)

    def remember(self, frequency_hz=None, volume=None):
        with self._lock:
            self._ensure_writable()
            frequency = (self._last_frequency if frequency_hz is None
                         else self._frequency(frequency_hz))
            new_volume = self._volume if volume is None else self._validated_volume(volume)
            self._commit([dict(item) for item in self._favorites], frequency, new_volume)
