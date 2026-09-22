"""Loopback-only SDR data/control bridge for the original radio terrain wallpaper.

The HTTP server opens no receiver unless explicitly requested or started with
--auto-start. One non-daemon thread owns every native receiver operation,
including close/reopen when tuning Blog V4.
"""
from __future__ import annotations

import argparse
import copy
import csv
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
import webbrowser

from runtime_paths import state_path


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "original_wallpaper"
SAMPLE_RATE = 2_400_000
FFT_SIZE = 32768
APPLICATION = "original-radio-terrain"
PROTOCOL_VERSION = 2
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "model.js": "text/javascript; charset=utf-8",
    "preview.svg": "image/svg+xml",
    "project.json": "application/json; charset=utf-8",
}


def log(message):
    try:
        with state_path("original_bridge.log", create=True).open("a", encoding="utf-8") as stream:
            stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def validate_frequency(value):
    if type(value) is not int or not 500_000 <= value <= 6_000_000_000:
        raise ValueError("frequency_hz 必须是 500000–6000000000 范围内的整数；实际范围取决于设备")
    return value


def saved_frequency():
    return 100_000_000


def create_source(frequency, demo, config=None):
    from radio_sources import create_source as open_source
    return open_source(frequency, demo, config)


def create_processor():
    from original_spectrum import SpectrumProcessor
    return SpectrumProcessor(bins=256)


def create_audio():
    # No sound device is opened until the user explicitly enables listening.
    from original_audio import AudioMonitor
    return AudioMonitor()


def create_station_store():
    from original_stations import StationStore
    return StationStore(state_path("radio_stations.json"))


def create_station_scanner():
    from original_station_scan import StationScanner
    return StationScanner()


def station_scan_centers(sample_rate=SAMPLE_RATE):
    from original_station_scan import scan_centers
    return scan_centers(sample_rate)


def create_wallpaper():
    from original_desktop import DesktopWallpaper
    return DesktopWallpaper()


class ConflictError(RuntimeError):
    pass


class ReceiverController:
    """Thread-safe state; HTTP threads only post validated commands."""

    def __init__(self, frequency=None, source_factory=None, processor_factory=None,
                 wallpaper_factory=None, audio_factory=None, station_store=None,
                 scanner_factory=None, scan_plan_factory=None, scan_settle_blocks=12,
                 source_config=None):
        from radio_sources import SourceConfig
        self.source_config = source_config or SourceConfig()
        self._stations = station_store if station_store is not None else create_station_store()
        preferences = self._stations.snapshot()
        frequency = (preferences["last_frequency_hz"] or saved_frequency()) if frequency is None else validate_frequency(frequency)
        self.token = secrets.token_urlsafe(32)
        self._source_factory = source_factory or partial(create_source, config=self.source_config)
        self._processor_factory = processor_factory or create_processor
        self._wallpaper_factory = wallpaper_factory or create_wallpaper
        self._wallpaper = None
        self._wallpaper_lock = threading.Lock()
        self._lock = threading.Lock()
        self._commands = queue.Queue()
        self._command_id = 0
        self._closing = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._scanner_factory = scanner_factory or create_station_scanner
        self._scan_plan_factory = scan_plan_factory
        self._actual_sample_rate = self.source_config.sample_rate
        self._source_warnings = []
        self._scan_settle_blocks = scan_settle_blocks
        self._scan_cancel = threading.Event()
        self._scan_original_frequency = None
        self._scan = {"status": "idle", "progress": 0.0, "current_hz": None,
                      "candidates": [], "error": None}
        self._audio = None
        self._audio_fault = None
        try:
            self._audio = (audio_factory or create_audio)()
        except Exception as exc:
            # Optional audio must never prevent RF/visualization startup.
            self._audio_fault = str(exc)
            log(f"audio unavailable: {exc}")
        if self._audio is not None:
            self._audio_call("configure", volume=preferences["volume"])
        self._state = {"application": APPLICATION, "protocol_version": PROTOCOL_VERSION,
                       "stream_id": secrets.token_hex(12), "status": "idle", "demo": False, "frequency_hz": frequency,
                       "sequence": 0, "generation": 0, "frame": None, "error": None}
        self.thread = threading.Thread(target=self._run, name="radio-terrain-receiver", daemon=False)
        self.thread.start()

    def _get_wallpaper(self):
        # The caller holds _wallpaper_lock.
        if self._wallpaper is None:
            self._wallpaper = self._wallpaper_factory()
        return self._wallpaper

    def snapshot(self):
        with self._lock:
            state = copy.deepcopy(self._state)
            state["scan"] = copy.deepcopy(self._scan)
            state["receiver"] = {"backend": "demo" if state["demo"] else self.source_config.backend,
                                 "sample_rate_hz": self._actual_sample_rate,
                                 "channel": self.source_config.channel,
                                 "warnings": list(self._source_warnings)}
        state["radio"] = self._stations.snapshot()
        state["audio"] = self._audio_snapshot()
        try:
            with self._wallpaper_lock:
                state["wallpaper"] = self._get_wallpaper().status()
        except Exception as exc:
            state["wallpaper"] = {"available": False, "error": str(exc)}
        return state

    def _remember(self, **settings):
        try:
            self._stations.remember(**settings)
        except Exception as exc:
            # Read-only/full disks must not tear down an otherwise working SDR.
            log(f"radio preferences not saved: {exc}")

    def _audio_call(self, method, *args, **kwargs):
        if self._audio is None or (method == "submit" and self._audio_fault):
            return
        try:
            return getattr(self._audio, method)(*args, **kwargs)
        except Exception as exc:
            if self._audio_fault != str(exc):
                log(f"audio {method} failed; RF continues: {exc}")
            self._audio_fault = str(exc)

    def _audio_snapshot(self):
        fallback = {"enabled": False, "muted": False, "volume": 0.15,
                    "status": "off", "error": None, "sample_rate_hz": 48000,
                    "frequency_hz": None, "buffer_ms": 0,
                    "underruns": 0, "overruns": 0}
        result = self._audio_call("snapshot")
        if isinstance(result, dict):
            fallback.update(result)
        if self._audio_fault:
            fallback.update(status="error", error=self._audio_fault)
        return fallback

    def session(self):
        with self._lock:
            return {"token": self.token, "frequency_hz": self._state["frequency_hz"]}

    def request(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        if self._closing.is_set():
            raise ConflictError("服务正在退出")
        action = payload.get("action")
        if action in ("station_save", "station_remove"):
            allowed = {"action", "frequency_hz", "name"} if action == "station_save" else {"action", "id"}
            if set(payload) != allowed:
                raise ValueError("收藏请求字段不完整或包含未知字段")
            if action == "station_save":
                self._stations.save(payload["frequency_hz"], payload["name"])
            else:
                self._stations.remove(payload["id"])
            return
        if action in ("scan", "scan_cancel"):
            if set(payload) != {"action"}:
                raise ValueError("搜台请求不接受附加字段")
            with self._lock:
                if action == "scan_cancel":
                    self._scan_cancel.set()
                    return
                if self._state["status"] != "live" or self._state["demo"]:
                    raise ConflictError("请先启动真实 SDR 接收，再搜台")
                self._scan_cancel.clear()
                self._scan_original_frequency = self._state["frequency_hz"]
                self._scan = {"status": "scanning", "progress": 0.0, "current_hz": None,
                              "candidates": [], "error": None}
                self._state.update(status="scanning", frame=None, generation=self._state["generation"] + 1)
                self._command_id += 1
                self._audio_call("set_source", None, self._state["generation"], False)
                self._commands.put_nowait(("scan", self._state["frequency_hz"], False,
                                          self._state["generation"], self._command_id))
            return
        if payload.get("action") == "audio":
            if set(payload) - {"action", "enabled", "muted", "volume"}:
                raise ValueError("音频请求包含不支持的字段")
            options = {key: payload[key] for key in ("enabled", "muted", "volume") if key in payload}
            if not options:
                raise ValueError("音频请求至少需要 enabled、muted 或 volume")
            for key in ("enabled", "muted"):
                if key in options and type(options[key]) is not bool:
                    raise ValueError(f"{key} 必须是布尔值")
            if "volume" in options:
                value = options["volume"]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("volume 必须是 0–1 范围内的有限数字")
            if self._closing.is_set():
                raise ConflictError("服务正在退出")
            if self._audio is None:
                raise RuntimeError(self._audio_fault or "音频组件不可用")
            self._audio.configure(**options)
            self._audio_fault = None
            if "volume" in options:
                self._remember(volume=options["volume"])
            return
        if set(payload) - {"action", "frequency_hz", "demo"}:
            raise ValueError("请求包含不支持的字段")
        action = payload.get("action")
        if action not in ("start", "stop", "tune", "wallpaper", "restore"):
            raise ValueError("不支持的 action")
        frequency = payload.get("frequency_hz")
        if "frequency_hz" in payload:
            frequency = validate_frequency(frequency)
        if "demo" in payload and type(payload["demo"]) is not bool:
            raise ValueError("demo 必须是布尔值")
        if "demo" in payload and action != "start":
            raise ValueError("只有 start 可以选择演示模式")
        if action == "tune" and frequency is None:
            raise ValueError("tune 需要 frequency_hz")
        if frequency is not None and action not in ("start", "tune"):
            raise ValueError("只有 start 和 tune 可以设置频率")
        if self._closing.is_set():
            raise ConflictError("服务正在退出")
        if action in ("wallpaper", "restore"):
            with self._wallpaper_lock:
                if self._closing.is_set():
                    raise ConflictError("服务正在退出")
                wallpaper = self._get_wallpaper()
                wallpaper.apply() if action == "wallpaper" else wallpaper.restore()
            return
        with self._lock:
            if self._closing.is_set():
                raise ConflictError("服务正在退出")
            status = self._state["status"]
            if status == "scanning" and action != "stop":
                raise ConflictError("正在搜台，请先取消搜台或停止接收")
            if action == "start":
                if status not in ("idle", "error"):
                    raise ConflictError("接收器正在工作或切换，请先停止")
                self._state.update(status="starting", error=None,
                                   demo=payload.get("demo", False),
                                   frequency_hz=frequency or self._state["frequency_hz"],
                                   generation=self._state["generation"] + 1, frame=None)
            elif action == "tune":
                if status in ("starting", "stopping"):
                    raise ConflictError("接收器正在切换，请稍后调谐")
                self._state.update(frequency_hz=frequency, error=None, frame=None,
                                   generation=self._state["generation"] + 1)
                if status in ("idle", "error"):
                    self._state["status"] = "idle"
                    if not self._state["demo"]:
                        self._remember(frequency_hz=frequency)
                    return
                self._state["status"] = "starting"
            else:
                if status in ("idle", "error"):
                    self._state.update(status="idle", error=None)
                    self._scan_cancel.set()
                    if self._scan["status"] in ("scanning", "restoring"):
                        self._scan.update(status="cancelled", current_hz=None)
                    return
                if status == "stopping":
                    return
                self._state["status"] = "stopping"
                self._scan_cancel.set()
                if status == "scanning":
                    self._state["frequency_hz"] = self._scan_original_frequency
                    self._scan.update(status="cancelled", current_hz=None)
            self._command_id += 1
            command = (action, self._state["frequency_hz"],
                       self._state["demo"], self._state["generation"], self._command_id)
            # Invalidate old PCM immediately. Audio device operations remain
            # on the audio worker, not on HTTP or USB threads.
            self._audio_call("set_source", None, self._state["generation"], self._state["demo"])
            self._commands.put_nowait(command)

    def _scan_current(self, operation):
        return not self._closing.is_set() and operation == self._command_id

    def _scan_on_worker(self, original_frequency, generation, operation):
        """Only the existing USB owner enters here; no parallel native handle."""
        source = None
        scan_error = None
        candidates = []
        try:
            from original_station_scan import merge_candidates
            import numpy as np
            scanner = self._scanner_factory()
            centers = list(self._scan_plan_factory() if self._scan_plan_factory
                           else station_scan_centers(self._actual_sample_rate))
            if not centers:
                raise ValueError("搜台窗口为空")
            for index, center in enumerate(centers):
                if self._scan_cancel.is_set() or not self._scan_current(operation):
                    break
                with self._lock:
                    if not self._scan_current(operation):
                        break
                    self._scan["current_hz"] = int(center)
                try:
                    source = self._source_factory(int(center), False)
                    if source.sample_rate < self._actual_sample_rate * 0.99:
                        raise RuntimeError("扫描期间设备采样率下降；请重新启动接收后再扫描，以免遗漏频段")
                    with self._lock:
                        if self._scan_current(operation):
                            self._state["frequency_hz"] = int(source.frequency)
                    blocks = []
                    for block in range(self._scan_settle_blocks + 3):
                        if self._scan_cancel.is_set() or not self._scan_current(operation):
                            break
                        iq = source.read_iq(FFT_SIZE)
                        if block >= self._scan_settle_blocks:
                            blocks.append(iq)
                    if len(blocks) == 3 and not self._scan_cancel.is_set() and self._scan_current(operation):
                        found = scanner.analyze(np.concatenate(blocks), source.sample_rate, source.frequency)
                        candidates = merge_candidates(candidates, found)
                        with self._lock:
                            if self._scan_current(operation):
                                self._scan.update(candidates=copy.deepcopy(candidates), progress=(index + 1) / len(centers))
                finally:
                    if source is not None:
                        previous, source = source, None
                        previous.close()
        except Exception as exc:
            scan_error = str(exc)
            log(f"FM scan failed: {exc}")
        cancelled = self._scan_cancel.is_set() or not self._scan_current(operation)
        with self._lock:
            if not self._scan_current(operation):
                self._scan.update(status="cancelled", current_hz=None, error=scan_error)
                return None, generation
            self._scan.update(status="restoring", current_hz=None, error=scan_error)
            self._state.update(frequency_hz=original_frequency, frame=None,
                               generation=self._state["generation"] + 1)
            generation = self._state["generation"]
        # Scan and cancel both restore the pre-scan station. Stop/shutdown do not.
        try:
            source = self._source_factory(original_frequency, False)
        except Exception as exc:
            with self._lock:
                self._scan.update(status="error", error=f"恢复原频率失败：{exc}")
            raise
        with self._lock:
            if self._scan_current(operation):
                self._actual_sample_rate = source.sample_rate
                self._source_warnings = list(getattr(source, "warnings", []))
                self._state.update(status="live", frequency_hz=int(source.frequency))
                self._scan.update(status="error" if scan_error else "cancelled" if cancelled or self._scan_cancel.is_set() else "complete")
                self._audio_call("set_source", int(source.frequency), generation, False)
            else:
                self._scan.update(status="cancelled", current_hz=None)
        return source, generation

    def _run(self):
        source = None
        processor = None
        generation = -1
        operation = -1
        last_frame = 0.0
        discontinuities = 0
        while not self._closing.is_set():
            try:
                try:
                    command = self._commands.get(timeout=0.2) if source is None else self._commands.get_nowait()
                except queue.Empty:
                    command = None
                if self._closing.is_set():
                    break
                if command is not None:
                    action, frequency, demo, generation, operation = command
                    with self._lock:
                        if operation != self._command_id:
                            continue
                    if source is not None:
                        # No HTTP or UI thread can close a handle during read_iq.
                        previous, source = source, None
                        previous.close()
                    if action == "stop":
                        processor = None
                        with self._lock:
                            if operation == self._command_id:
                                self._state.update(status="idle", error=None)
                        log("receiver stopped; USB released")
                        continue
                    if action == "scan":
                        source, generation = self._scan_on_worker(frequency, generation, operation)
                        discontinuities = getattr(source, "discontinuities", 0)
                        processor = self._processor_factory() if source is not None else None
                        last_frame = 0.0
                        continue
                    # Reopening on tune avoids this machine's Blog V4 / WinUSB
                    # control-transfer error -9 after native streaming starts.
                    source = self._source_factory(frequency, demo)
                    discontinuities = getattr(source, "discontinuities", 0)
                    processor = self._processor_factory()
                    last_frame = 0.0
                    with self._lock:
                        if operation == self._command_id and self._state["status"] == "starting":
                            self._actual_sample_rate = source.sample_rate
                            self._source_warnings = list(getattr(source, "warnings", []))
                            self._state.update(status="live", frequency_hz=int(source.frequency))
                            self._audio_call("set_source", int(source.frequency), generation, demo)
                            if not demo:
                                self._remember(frequency_hz=int(source.frequency))
                    log(f"{action} demo={demo} frequency={source.frequency} generation={generation}")
                if source is None:
                    continue
                # Keep draining USB between published frames instead of sleeping
                # while the native receive buffer accumulates stale samples.
                iq = source.read_iq(FFT_SIZE)
                dropped = getattr(source, "discontinuities", 0)
                if dropped != discontinuities:
                    discontinuities = dropped
                    # Known overflow breaks continuous RF history and the FM
                    # phase/filter state. Never bridge it as uninterrupted IQ.
                    with self._lock:
                        if operation == self._command_id and self._state["status"] == "live":
                            self._state["generation"] += 1
                            generation = self._state["generation"]
                            self._state["frame"] = None
                            self._audio_call("set_source", int(source.frequency), generation, demo)
                    processor = self._processor_factory()
                    last_frame = 0.0
                now = time.monotonic()
                if not self._closing.is_set():
                    # Every continuous IQ block reaches audio BEFORE the 10 Hz
                    # visualization gate; published FFT magnitudes are not audio.
                    self._audio_call("submit", iq, source.sample_rate, source.frequency, generation)
                if self._closing.is_set() or now - last_frame < 0.1:
                    continue
                frame = processor.process(iq, source.sample_rate, source.frequency, now)
                last_frame = now
                with self._lock:
                    if self._state["status"] == "live" and self._state["generation"] == generation:
                        self._state["frame"] = frame
                        self._state["sequence"] += 1
            except Exception as exc:
                log(traceback.format_exc())
                self._audio_call("set_source", None, generation)
                if source is not None:
                    try:
                        previous, source = source, None
                        previous.close()
                    except Exception:
                        log(traceback.format_exc())
                processor = None
                with self._lock:
                    # Stop commands remain queued and will return to idle after
                    # this error, without ever opening a synthetic fallback.
                    if operation == self._command_id:
                        self._state.update(status="error", error=str(exc))
                        if self._scan["status"] in ("scanning", "restoring"):
                            self._scan.update(status="error", current_hz=None, error=str(exc))
        if source is not None:
            try:
                source.close()
            except Exception as exc:
                log(f"receiver close failed: {exc}")
                with self._lock:
                    self._state.update(status="error", error=str(exc))
        with self._lock:
            if self._state["status"] != "error":
                self._state["status"] = "idle"
        self._audio_call("set_source", None, generation)

    def shutdown(self, restore_wallpaper=True):
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._closing.set()
            self._scan_cancel.set()
            self._audio_call("set_source", None, self._state["generation"])
            with self._lock:
                if self._state["status"] in ("live", "starting"):
                    self._state["status"] = "stopping"
            # Safe shutdown waits for the native read to return before closing USB.
            self.thread.join()
            self._audio_call("close")
            if not restore_wallpaper:
                log("server shutdown: receiver released; keep-wallpaper mode retained desktop")
            else:
                try:
                    with self._wallpaper_lock:
                        self._get_wallpaper().restore()
                except Exception as exc:
                    log(f"wallpaper restore pending: {exc}")
            self._shutdown_complete = True


def wallpaper_engine_executable():
    """Discover the user's installation without writing its configuration."""
    from original_desktop import find_wallpaper_executable
    executable = find_wallpaper_executable()
    if executable is None:
        raise RuntimeError("未找到 Wallpaper Engine；可设置 INVISIBLE_TERRAIN_WALLPAPER_EXE")
    return executable


def wallpaper_engine_is_running():
    executable = wallpaper_engine_executable()
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {executable.name}", "/FO", "CSV", "/NH"],
        shell=False, capture_output=True, text=True, timeout=2,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(f"无法检查 Wallpaper Engine 进程（{result.returncode}）")
    return any(row and row[0].casefold() == executable.name.casefold()
               for row in csv.reader(io.StringIO(result.stdout)))


def launch_wallpaper_engine():
    executable = wallpaper_engine_executable()
    if not executable.is_file():
        raise RuntimeError("找不到本机 Wallpaper Engine 安装程序")
    kwargs = {}
    if hasattr(subprocess, "STARTUPINFO"):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE: no surprise interactive startup window.
        kwargs["startupinfo"] = startup
    # The engine is intentionally persistent. Only our startup helper thread
    # is cancelled/joined when the bridge exits; a user-owned engine is never
    # terminated or restarted by the bridge.
    return subprocess.Popen(
        [str(executable)], cwd=executable.parent, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def apply_startup_wallpaper(controller, cancelled, timeout=30.0, retry_interval=1.0):
    """Bounded startup retries; cancellable waits, no repeated engine launches.

    An already-running adapter operation has its own finite subprocess timeout;
    shutdown joins it before optionally restoring the previous desktop.
    """
    deadline = time.monotonic() + min(max(float(timeout), 0.0), 30.0)
    launched = False
    last_error = "Wallpaper Engine 尚未就绪"
    try:
        if not wallpaper_engine_executable().is_file():
            raise RuntimeError("找不到本机 Wallpaper Engine 安装路径")
        while not cancelled.is_set() and time.monotonic() < deadline:
            try:
                if not wallpaper_engine_is_running():
                    if not launched and not cancelled.is_set():
                        launch_wallpaper_engine()
                        launched = True
                        log("startup wallpaper: launched the local engine hidden")
                    last_error = "正在等待 Wallpaper Engine 启动"
                else:
                    if cancelled.is_set():
                        break
                    state = controller.snapshot()["wallpaper"]
                    if not state.get("available"):
                        last_error = state.get("message") or state.get("error") or "正在等待壁纸配置就绪"
                    elif state.get("is_original"):
                        log("startup wallpaper: original terrain already selected; no switch needed")
                        return True
                    elif not cancelled.is_set():
                        controller.request({"action": "wallpaper"})
                        log("startup wallpaper: applied original terrain successfully")
                        return True
            except Exception as exc:
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancelled.wait(min(retry_interval, remaining)):
                break
    except Exception as exc:
        last_error = str(exc)
    if cancelled.is_set():
        log("startup wallpaper: cancelled during service shutdown")
    else:
        log(f"startup wallpaper failed; HTTP controls remain available: {last_error}")
    return False


def auto_start_receiver(controller):
    # Explicitly select real hardware even if the preceding manual session
    # used demo mode. Missing hardware becomes an honest, retryable error.
    try:
        controller.request({"action": "start", "demo": False})
        log(f"startup RF: requested real receiver at {controller.session()['frequency_hz']} Hz")
        return True
    except Exception as exc:
        log(f"startup RF request failed; HTTP controls remain available: {exc}")
        return False


class StartupTasks:
    def __init__(self, controller, port, cancelled=None):
        self.controller = controller
        self.port = port
        self.cancelled = cancelled if cancelled is not None else threading.Event()
        self.threads = []

    def start(self, auto_start=False, apply_wallpaper=False, open_browser=False):
        if auto_start:
            auto_start_receiver(self.controller)
        if open_browser:
            def open_later():
                if not self.cancelled.wait(0.15):
                    try:
                        webbrowser.open(f"http://127.0.0.1:{self.port}/", new=2)
                    except Exception as exc:
                        log(f"could not open control page: {exc}")
            self._spawn("radio-terrain-open", open_later)
        if apply_wallpaper:
            self._spawn("radio-terrain-startup-wallpaper",
                        lambda: apply_startup_wallpaper(self.controller, self.cancelled))

    def _spawn(self, name, target):
        thread = threading.Thread(target=target, name=name, daemon=False)
        self.threads.append(thread)
        thread.start()

    def stop(self):
        self.cancelled.set()
        for thread in self.threads:
            thread.join()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_existing_bridge(port):
    if not 1 <= port <= 65535:
        return False
    try:
        # Do not consult HTTP proxy environment variables or follow redirects
        # from an unrelated service that happens to occupy the chosen port.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as response:
            if response.status != 200:
                return False
            raw = response.read(65_537)
        if len(raw) > 65_536:
            return False
        state = json.loads(raw)
        return (isinstance(state, dict) and state.get("application") == APPLICATION
                and state.get("protocol_version") == PROTOCOL_VERSION)
    except Exception:
        return False


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port=8766, controller=None, static_dir=STATIC_DIR, source_config=None, frequency=None):
        self.controller = controller
        self.static_dir = Path(static_dir)
        # The address is intentionally not configurable: never expose RF
        # controls, session token, or wallpaper switching to a LAN interface.
        super().__init__(("127.0.0.1", port), BridgeHandler)
        if self.controller is None:
            try:
                self.controller = ReceiverController(source_config=source_config, frequency=frequency)
            except BaseException:
                self.server_close()
                raise


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "RadioTerrain/2"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Polling at 5 Hz should not fill a persistent log with successful GETs.
        if args and str(args[1] if len(args) > 1 else "") not in ("200", "204"):
            log(format % args)

    def _route(self):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if port == 80:
            allowed.update(("localhost", "127.0.0.1"))
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            self._json(403, {"error": "无效的本机 Host"})
            return None
        target = urlsplit(self.path)
        if target.scheme or target.netloc:
            self._json(400, {"error": "只接受相对请求路径"})
            return None
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self._json(403, {"error": "无效的 Origin"})
            return None
        origin = origins[0] if origins else None
        expected = "http://" + hosts[0].lower()
        allow_null = target.path == "/api/state" and self.command in ("GET", "OPTIONS")
        if origin is not None and origin != expected and not (allow_null and origin == "null"):
            self._json(403, {"error": "只允许本机同源控制页面"})
            return None
        return target.path, origin

    def _send(self, status, data, content_type="application/json; charset=utf-8", origin=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if origin == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _json(self, status, payload, origin=None, extra=None):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, data, origin=origin, extra=extra)

    def do_GET(self):
        route = self._route()
        if route is None:
            return
        path, origin = route
        if path == "/api/state":
            state = self.server.controller.snapshot()
            if origin == "null":
                # File-based wallpaper renderers need RF data, not filesystem
                # paths or account names from the desktop restore journal.
                state.pop("wallpaper", None)
                state.pop("radio", None)
            self._json(200, state, origin=origin)
            return
        if path == "/api/session":
            self._json(200, self.server.controller.session())
            return
        name = "index.html" if path == "/" else path[1:]
        if name not in STATIC_FILES:
            self._json(404, {"error": "Not found"})
            return
        try:
            data = (self.server.static_dir / name).read_bytes()
        except OSError:
            self._json(404, {"error": "Not found"})
            return
        self._send(200, data, STATIC_FILES[name])

    def do_OPTIONS(self):
        route = self._route()
        if route is None:
            return
        path, origin = route
        if path != "/api/state" or self.headers.get("Access-Control-Request-Method") != "GET":
            self._json(403, {"error": "不允许跨源控制"})
            return
        if self.headers.get("Access-Control-Request-Headers"):
            self._json(403, {"error": "不允许跨源自定义请求头"})
            return
        extra = {"Access-Control-Allow-Methods": "GET"}
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            extra["Access-Control-Allow-Private-Network"] = "true"
        self._send(204, b"", origin=origin, extra=extra)

    def do_POST(self):
        route = self._route()
        if route is None:
            return
        path, _ = route
        if path != "/api/control":
            self._json(404, {"error": "Not found"})
            return
        tokens = self.headers.get_all("X-SDR-Token", [])
        if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode("utf-8"), self.server.controller.token.encode("ascii")):
            self._json(403, {"error": "缺少有效的本机会话令牌"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"error": "Content-Type 必须是 application/json"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "需要单一 Content-Length"})
            return
        try:
            length = int(lengths[0])
            if not 1 <= length <= 4096:
                raise ValueError("请求正文必须在 1–4096 字节内")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求正文不完整")
            payload = json.loads(raw.decode("utf-8"))
            self.server.controller.request(payload)
        except ConflictError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, UnicodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            log(traceback.format_exc())
            self._json(500, {"error": str(exc)})
        else:
            self._json(200, {"ok": True})


def main(argv=None):
    parser = argparse.ArgumentParser(description="原创电波地形：本机 SDR 数据桥")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--duration", type=float, help="限时服务测试，秒；默认不自动启动接收")
    parser.add_argument("--auto-start", action="store_true", help="服务启动后自动接收真实 SDR，不使用演示模式")
    parser.add_argument("--apply-wallpaper", action="store_true", help="启动时应用原创壁纸；需要时启动本机 Wallpaper Engine")
    parser.add_argument("--keep-wallpaper", action="store_true", help="退出时停止接收，但保留原创桌面")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="打开本机控制页；已有实例时只打开页面")
    parser.add_argument("--backend", choices=("rtl", "soapy"), default="rtl")
    parser.add_argument("--device-index", type=int, default=0, help="RTL-SDR device index")
    parser.add_argument("--rtl-library", help="Explicit librtlsdr DLL/shared library path")
    parser.add_argument("--device-args", default="", help="SoapySDR selection, e.g. driver=hackrf,serial=...")
    parser.add_argument("--sample-rate", type=int, default=SAMPLE_RATE, help="Requested IQ samples/s (default 2400000)")
    parser.add_argument("--channel", type=int, default=0, help="Receive channel (SoapySDR)")
    parser.add_argument("--gain", type=float, help="Manual receive gain in dB; default AGC when supported")
    parser.add_argument("--antenna", help="SoapySDR receive antenna name")
    parser.add_argument("--frequency", type=int, help="Initial center frequency in Hz; default remembered or 100000000")
    parser.add_argument("--list-devices", action="store_true", help="Discover devices and exit without starting reception/server")
    parser.add_argument("--demo", action="store_true", help="Explicitly start synthesized IQ (no RF hardware or sound)")
    args = parser.parse_args(argv)
    from radio_sources import SourceConfig, enumerate_devices
    try:
        config = SourceConfig(backend=args.backend, device_index=args.device_index,
                              rtl_library=args.rtl_library, device_args=args.device_args,
                              sample_rate=args.sample_rate, channel=args.channel,
                              gain=args.gain, antenna=args.antenna)
        if args.frequency is not None:
            validate_frequency(args.frequency)
    except ValueError as exc:
        parser.error(str(exc))
    if args.demo and args.auto_start:
        parser.error("--demo 与 --auto-start 不能同时使用")
    if args.list_devices:
        print(json.dumps(enumerate_devices(config), ensure_ascii=False, indent=2), flush=True)
        return 0
    if not 0 <= args.port <= 65535:
        parser.error("port 必须在 0–65535 之间")
    if args.duration is not None and not 0.1 <= args.duration <= 3600:
        parser.error("duration 必须在 0.1–3600 秒之间")
    try:
        server = BridgeServer(args.port, source_config=config, frequency=args.frequency)
    except OSError:
        if not is_existing_bridge(args.port):
            raise
        log(f"existing bridge confirmed on 127.0.0.1:{args.port}; no duplicate receiver or wallpaper action")
        print("A terrain service already owns this port. Device options were NOT applied; stop it first or use another port.", flush=True)
        if args.open_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/", new=2)
        return 0
    finished = threading.Event()
    startup = StartupTasks(server.controller, server.server_port, finished)
    if args.duration is not None:
        def expire():
            if not finished.wait(args.duration):
                server.shutdown()
        threading.Thread(target=expire, name="radio-terrain-duration", daemon=True).start()
    print(f"Radio terrain ready: http://127.0.0.1:{server.server_port}/ (receiver idle)", flush=True)
    log(f"server listening 127.0.0.1:{server.server_port}; receiver idle; keep_wallpaper={args.keep_wallpaper}")
    try:
        if args.demo:
            server.controller.request({"action": "start", "demo": True})
        startup.start(auto_start=args.auto_start, apply_wallpaper=args.apply_wallpaper,
                      open_browser=args.open_browser)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        finished.set()
        server.server_close()
        startup.stop()
        server.controller.shutdown(restore_wallpaper=not args.keep_wallpaper)
        log("server stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(traceback.format_exc())
        print(f"Radio terrain failed: {exc}", flush=True)
        raise SystemExit(1)
