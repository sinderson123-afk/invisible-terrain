"""Reversible, monitor-0-only installation of our original web wallpaper.

The Wallpaper Engine configuration and all existing wallpaper assets are read
only. A local journal is committed before sending a documented CLI command.
Multiple HTTP request threads and DesktopWallpaper instances share one lock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from runtime_paths import state_path

DEFAULT_PROJECT = Path(__file__).with_name("original_wallpaper") / "project.json"
_LOCK = threading.RLock()
_TERMINAL = {"restored", "user_changed"}
_PENDING = {"prepared", "active", "apply_pending", "restore_pending"}


class DesktopError(RuntimeError):
    pass


def _registry_steam_roots() -> list[Path]:
    """Read Steam's installation location without starting Steam or changing it."""
    try:
        import winreg
    except ImportError:
        return []
    roots = []
    for hive, value_names in ((winreg.HKEY_CURRENT_USER, ("SteamPath", "SteamExe")),
                              (winreg.HKEY_LOCAL_MACHINE, ("InstallPath",))):
        for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(hive, r"Software\Valve\Steam", 0, winreg.KEY_READ | view) as key:
                    for value_name in value_names:
                        try:
                            value, _kind = winreg.QueryValueEx(key, value_name)
                            if isinstance(value, str) and value.strip():
                                path = Path(value)
                                roots.append(path.parent if value_name == "SteamExe" else path)
                        except OSError:
                            pass
            except OSError:
                pass
    return roots


def _library_roots(steam_root: Path) -> list[Path]:
    """Read modern and legacy libraryfolders.vdf path entries, including Unicode."""
    roots = [steam_root]
    pattern = r'"(?:path|\d+)"\s*"((?:\\.|[^"\\])*)"'
    for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        library_file = steam_root / relative
        try:
            if library_file.stat().st_size > 1024 * 1024:
                continue
            contents = library_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for encoded in re.findall(pattern, contents, flags=re.IGNORECASE):
            value = re.sub(r'\\([\\"])', r'\1', encoded)
            candidate = Path(value)
            # Numeric app-size entries in modern VDF files are not paths.
            if candidate.is_absolute():
                roots.append(candidate)
    return roots


def find_wallpaper_executable() -> Path | None:
    """Locate Wallpaper Engine, read-only, or return None when unavailable.

    An explicit INVISIBLE_TERRAIN_WALLPAPER_EXE is authoritative: a typo does
    not silently select another installation. Discovery never launches software.
    """
    override = os.environ.get("INVISIBLE_TERRAIN_WALLPAPER_EXE", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    roots = _registry_steam_roots()
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        value = os.environ.get(variable, "").strip()
        if value:
            roots.append(Path(value) / "Steam")
    seen = set()
    for root in roots:
        for library in _library_roots(root):
            normalized = _normalized(library)
            if normalized in seen:
                continue
            seen.add(normalized)
            install = library / "steamapps" / "common" / "wallpaper_engine"
            for name in ("wallpaper64.exe", "wallpaper32.exe"):
                candidate = install / name
                if candidate.is_file():
                    return candidate.resolve()
    return None


def _normalized(path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _read_json(path: Path) -> dict:
    for attempt in range(4):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (OSError, ValueError) as exc:
            if attempt == 3:
                raise DesktopError(f"无法读取配置：{path}") from exc
            time.sleep(0.04)
    raise AssertionError("unreachable")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class DesktopWallpaper:
    """Switch to the original wallpaper, retaining the previous exact file.

    ``status`` is read-only and always returns a JSON-serializable dictionary.
    ``apply`` and ``restore`` may raise DesktopError; the journal remains usable
    after a failed command. Only Monitor0 is supported intentionally.
    """

    def __init__(self, executable=None, project=DEFAULT_PROJECT,
                 session_file=None, confirmation_timeout=2.0):
        self.executable = Path(executable) if executable is not None else find_wallpaper_executable()
        self.project = Path(project).resolve()
        self.config_path = self.executable.parent / "config.json" if self.executable is not None else None
        self.session_file = Path(session_file) if session_file is not None else state_path("original_wallpaper_session.json")
        self.confirmation_timeout = max(0.0, float(confirmation_timeout))

    def _target_files(self) -> set[str]:
        if self.executable is None or not self.executable.is_file():
            raise DesktopError("找不到 Wallpaper Engine；可设置 INVISIBLE_TERRAIN_WALLPAPER_EXE 指向其可执行文件")
        project = _read_json(self.project)
        entry = project.get("file")
        if project.get("type", "").casefold() != "web" or not isinstance(entry, str) or not entry:
            raise DesktopError("原创壁纸工程必须是有明确入口文件的 Web 壁纸")
        entry_path = Path(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            raise DesktopError("原创壁纸入口必须在自己的工程目录内")
        entry_path = (self.project.parent / entry_path).resolve()
        if entry_path.parent != self.project.parent or not entry_path.is_file():
            raise DesktopError("找不到原创壁纸入口，或入口不在工程目录内")
        return {_normalized(self.project), _normalized(entry_path)}

    def _selection(self) -> dict:
        config = _read_json(self.config_path)
        choices = []
        for name, account in config.items():
            if not isinstance(account, dict):
                continue
            general = account.get("general")
            if not isinstance(general, dict):
                continue
            wallpaperconfig = general.get("wallpaperconfig")
            if not isinstance(wallpaperconfig, dict):
                continue
            selected = wallpaperconfig.get("selectedwallpapers")
            if not isinstance(selected, dict):
                continue
            slot = selected.get("Monitor0")
            if isinstance(slot, dict) and isinstance(slot.get("file"), str) and slot["file"].strip():
                choices.append({"account": name, "file": slot["file"]})
        if len(choices) != 1:
            raise DesktopError("无法唯一确认 Monitor0 的壁纸账户；为保护原桌面，未执行切换")
        selection = choices[0]
        if not Path(selection["file"]).is_absolute():
            raise DesktopError("当前壁纸路径不是绝对路径，无法安全备份")
        return selection

    def _journal(self):
        if not self.session_file.exists():
            return None
        record = _read_json(self.session_file)
        if (record.get("version") != 1 or record.get("monitor") != 0
                or _normalized(record.get("target", "")) != _normalized(self.project)
                or record.get("state") not in _PENDING | _TERMINAL
                or not isinstance(record.get("account"), str)
                or not isinstance(record.get("original"), str)
                or not Path(record["original"]).is_absolute()
                or not isinstance(record.get("history", []), list)):
            raise DesktopError("桌面恢复记录不匹配或损坏；保留了原文件，请勿删除备份")
        return record

    def _save(self, record):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(self.session_file.suffix + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.session_file)

    def _open(self, file):
        if not Path(file).is_file():
            raise DesktopError(f"壁纸文件已经移动或不存在：{file}")
        result = subprocess.run(
            [str(self.executable), "-control", "openWallpaper", "-file", str(file), "-monitor", "0"],
            cwd=self.executable.parent, shell=False, capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise DesktopError(f"Wallpaper Engine 切换命令失败：{result.returncode}")

    def _confirm(self, account, accepted_files):
        deadline = time.monotonic() + self.confirmation_timeout
        while True:
            current = self._selection()
            if current["account"] == account and _normalized(current["file"]) in accepted_files:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def status(self):
        with _LOCK:
            result = {"state": "inactive", "available": False, "is_original": False,
                      "recoverable": False, "can_restore": False, "restored": False,
                      "monitor": 0, "current_file": None, "original_file": None,
                      "message": "尚未切换桌面"}
            try:
                targets = self._target_files()
                current = self._selection()
                record = self._journal()
                result.update(available=True, current_file=current["file"],
                              account=current["account"], is_original=_normalized(current["file"]) in targets)
                if record:
                    result.update(state=record["state"], original_file=record["original"],
                                  recoverable=record["state"] in _PENDING,
                                  restored=record["state"] == "restored")
                    result["can_restore"] = (result["recoverable"] and result["is_original"]
                                              and current["account"] == record["account"])
                    if current["account"] != record["account"] and result["recoverable"]:
                        result["message"] = "壁纸账户已变化；保留备份，不覆盖当前桌面"
                    elif result["can_restore"]:
                        result["message"] = "原创电波地形已应用；可恢复切换前的壁纸"
                    elif record["state"] == "restored":
                        result["message"] = "已恢复切换前的壁纸"
                    elif record["state"] == "user_changed":
                        result["message"] = "检测到你另选了壁纸，已保留该选择和旧备份"
                    else:
                        result["message"] = "存在待确认的桌面切换记录；原壁纸路径已备份"
                    if record.get("last_error"):
                        result["last_error"] = record["last_error"]
                elif result["is_original"]:
                    result["message"] = "桌面已经是原创电波地形，但没有可用的旧壁纸备份"
            except Exception as exc:
                result.update(state="error", message=str(exc))
            return result

    def apply(self):
        with _LOCK:
            targets = self._target_files()
            current = self._selection()
            record = self._journal()
            is_target = _normalized(current["file"]) in targets
            if record and record["state"] in _PENDING:
                if current["account"] != record["account"]:
                    raise DesktopError("壁纸账户已变化；请先恢复/结束旧会话，旧备份未覆盖")
                if is_target:
                    record.update(state="active", confirmed_at=_now())
                    record.pop("last_error", None)
                    self._save(record)
                    return self.status()
                if _normalized(current["file"]) != _normalized(record["original"]):
                    raise DesktopError("桌面已被更换；请先点击恢复以结束旧会话，不会覆盖你新选的壁纸")
            else:
                if is_target:
                    raise DesktopError("当前已经是原创壁纸，且没有未恢复的旧壁纸备份；请先手动选择另一张壁纸")
                if not Path(current["file"]).is_file():
                    raise DesktopError("当前壁纸文件不存在，不能创建可靠的恢复备份")
                history = list(record.get("history", [])) if record else []
                if record:
                    history.append({key: value for key, value in record.items() if key != "history"})
                record = {"version": 1, "state": "prepared", "monitor": 0,
                          "account": current["account"], "original": current["file"],
                          "target": str(self.project), "started_at": _now(), "history": history}
            record.update(state="prepared")
            record.pop("last_error", None)
            self._save(record)
            try:
                self._open(self.project)
                if not self._confirm(record["account"], targets):
                    raise DesktopError("切换命令已发送，但尚未从 Wallpaper Engine 配置确认；保留了恢复备份")
                record.update(state="active", confirmed_at=_now())
                self._save(record)
            except Exception as exc:
                record.update(state="apply_pending", last_error=str(exc))
                self._save(record)
                if isinstance(exc, DesktopError):
                    raise
                raise DesktopError(f"切换失败，原桌面备份已保留：{exc}") from exc
            return self.status()

    def restore(self):
        with _LOCK:
            record = self._journal()
            if not record or record["state"] in _TERMINAL:
                return self.status()
            targets = self._target_files()
            current = self._selection()
            if current["account"] != record["account"] or _normalized(current["file"]) not in targets:
                already_original = (current["account"] == record["account"]
                                    and _normalized(current["file"]) == _normalized(record["original"]))
                record.update(state="restored" if already_original else "user_changed",
                              ended_at=_now(), observed_file=current["file"],
                              observed_account=current["account"])
                record.pop("last_error", None)
                self._save(record)
                return self.status()
            if _normalized(record["original"]) in targets:
                raise DesktopError("备份指向原创壁纸自身；为避免错误恢复，未发送命令")
            record.update(state="restore_pending")
            self._save(record)
            try:
                self._open(record["original"])
                if not self._confirm(record["account"], {_normalized(record["original"])}):
                    raise DesktopError("恢复命令已发送，但尚未确认；请保持 Wallpaper Engine 运行后重试")
                record.update(state="restored", ended_at=_now())
                record.pop("last_error", None)
                self._save(record)
            except Exception as exc:
                record.update(state="restore_pending", last_error=str(exc))
                self._save(record)
                if isinstance(exc, DesktopError):
                    raise
                raise DesktopError(f"恢复失败，原桌面备份已保留：{exc}") from exc
            return self.status()
