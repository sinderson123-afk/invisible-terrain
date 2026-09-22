"""Receive-only IQ sources, independent of any GUI or installation directory.

Native RTL-SDR uses the user's own librtlsdr; optional SoapySDR uses its Python
bindings and an independently installed device module. No drivers are bundled.
The Soapy adapter is protocol-tested, not a claim of validation on every radio.

API references (no third-party implementation code is copied):
https://github.com/pothosware/SoapySDR/wiki/PythonSupport
https://pothosware.github.io/SoapySDR/doxygen/latest/classSoapySDR_1_1Device.html
https://github.com/osmocom/rtl-sdr/blob/master/include/rtl-sdr.h
"""

from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np


MIN_SAMPLE_RATE = 1_800_000
MAX_SAMPLE_RATE = 10_000_000
MAX_READ_SAMPLES = 262_144
READ_TIMEOUT_SECONDS = 1.0
MAX_READ_ATTEMPTS = 128


class SourceError(RuntimeError):
    """A receiver could not be selected, configured, or read safely."""


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an integer")
    if not math.isfinite(value) or int(value) != value or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return int(value)


@dataclass(frozen=True)
class SourceConfig:
    backend: str = "rtl"
    device_index: int = 0
    rtl_library: str | Path | None = None
    device_args: str = ""
    sample_rate: int = 2_400_000
    channel: int = 0
    gain: float | None = None
    antenna: str | None = None

    def __post_init__(self):
        if self.backend not in ("rtl", "soapy"):
            raise ValueError("backend must be rtl or soapy; use --demo for simulation")
        for name, minimum, maximum in (
            ("device_index", 0, 2**32 - 1), ("channel", 0, 255),
            ("sample_rate", MIN_SAMPLE_RATE, MAX_SAMPLE_RATE),
        ):
            object.__setattr__(self, name, _integer(getattr(self, name), name, minimum, maximum))
        if not isinstance(self.device_args, str) or len(self.device_args) > 4096 or any(
            ord(char) < 32 for char in self.device_args
        ):
            raise ValueError("device_args must be a short, single-line SoapySDR argument string")
        if self.rtl_library is not None:
            if not isinstance(self.rtl_library, (str, Path)) or not str(self.rtl_library).strip():
                raise ValueError("rtl_library must be a nonempty filesystem path")
            if "\0" in str(self.rtl_library):
                raise ValueError("rtl_library must not contain NUL")
        if self.gain is not None:
            if isinstance(self.gain, bool) or not isinstance(self.gain, (int, float)) or not math.isfinite(self.gain):
                raise ValueError("gain must be a finite number in dB")
            object.__setattr__(self, "gain", float(self.gain))
        if self.antenna is not None and (
            not isinstance(self.antenna, str) or not self.antenna.strip()
            or len(self.antenna) > 256 or any(ord(char) < 32 for char in self.antenna)
        ):
            raise ValueError("antenna must be a nonempty single-line name")
        if self.backend == "rtl":
            if self.sample_rate > 3_200_000:
                raise ValueError("Native RTL-SDR supports at most 3.2 MS/s; 2.4 MS/s is recommended")
            if self.channel != 0 or self.antenna is not None or self.device_args:
                raise ValueError("Native RTL-SDR uses RX channel 0; antenna/device_args require --backend soapy")
        elif self.device_index != 0 or self.rtl_library is not None:
            raise ValueError("Select SoapySDR devices with device_args, not device_index/rtl_library")


def _config(config):
    if config is None:
        return SourceConfig()
    if not isinstance(config, SourceConfig):
        raise TypeError("config must be a SourceConfig")
    return config


def _frequency(value):
    return _integer(value, "frequency", 1, 100_000_000_000)


def _read_count(value):
    return _integer(value, "read count", 1, MAX_READ_SAMPLES)


def _actual_rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SourceError("Driver returned an invalid sample rate")
    if not MIN_SAMPLE_RATE <= value <= MAX_SAMPLE_RATE:
        raise SourceError(f"Driver negotiated {value} samples/s; supported workload is 1.8–10 MS/s")
    if int(value) != value:
        raise SourceError("Fractional-Hz sample rates are not supported; request a rate the driver reports as integer Hz")
    return int(value)


class RtlSdrLibrary:
    """Small cdecl binding to user-installed librtlsdr; no device is opened here."""

    def __init__(self, path=None):
        selected = path or os.environ.get("INVISIBLE_TERRAIN_RTL_LIBRARY")
        if selected:
            resolved = Path(selected).expanduser().resolve()
            if not resolved.is_file():
                raise SourceError(f"RTL-SDR library does not exist: {resolved}")
            target = str(resolved)
        else:
            library_dir = Path(__file__).resolve().parent.parent / "lib"
            local = next((library_dir / name for name in (
                "rtlsdr.dll", "librtlsdr.so", "librtlsdr.dylib"
            ) if (library_dir / name).is_file()), None)
            target = str(local) if local else ctypes.util.find_library("rtlsdr")
            if not target and os.name == "nt":
                target = "rtlsdr.dll"
            if not target:
                raise SourceError("Install librtlsdr or set --rtl-library / INVISIBLE_TERRAIN_RTL_LIBRARY to its path")
        try:
            # librtlsdr exports C/cdecl functions, including on 32-bit Windows.
            self.dll = ctypes.CDLL(target)
            self._bind()
        except (AttributeError, OSError) as exc:
            raise SourceError(f"Cannot load RTL-SDR library {target}: {exc}. Check architecture and USB dependencies.") from exc

    def _bind(self):
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint32
        integer = ctypes.c_int
        specs = {
            "get_device_count": ([], uint),
            "get_device_name": ([uint], ctypes.c_char_p),
            "open": ([ctypes.POINTER(pointer), uint], integer),
            "close": ([pointer], integer),
            "set_center_freq": ([pointer, uint], integer),
            "get_center_freq": ([pointer], uint),
            "set_sample_rate": ([pointer, uint], integer),
            "get_sample_rate": ([pointer], uint),
            "set_tuner_gain_mode": ([pointer, integer], integer),
            "get_tuner_gains": ([pointer, ctypes.POINTER(integer)], integer),
            "set_tuner_gain": ([pointer, integer], integer),
            "reset_buffer": ([pointer], integer),
            "read_sync": ([pointer, pointer, integer, ctypes.POINTER(integer)], integer),
        }
        for name, (args, result) in specs.items():
            function = getattr(self.dll, "rtlsdr_" + name)
            function.argtypes = args
            function.restype = result

    def devices(self):
        result = []
        for index in range(int(self.dll.rtlsdr_get_device_count())):
            raw = self.dll.rtlsdr_get_device_name(index)
            label = raw.decode("utf-8", errors="replace") if raw else f"RTL-SDR #{index}"
            result.append({"backend": "rtl", "index": index, "label": label})
        return result


class RtlSdrSource:
    def __init__(self, frequency, config=None, *, library=None):
        config = _config(config)
        if config.backend != "rtl":
            raise ValueError("RtlSdrSource requires backend rtl")
        frequency = _integer(frequency, "RTL-SDR frequency", 500_000, 1_766_000_000)
        self.library = library if library is not None else RtlSdrLibrary(config.rtl_library)
        self.lib = self.library.dll
        self.device = ctypes.c_void_p()
        self.warnings = []
        self.frequency = frequency
        self.sample_rate = config.sample_rate
        if config.device_index >= len(self.library.devices()):
            raise SourceError(f"RTL-SDR index {config.device_index} was not found; use --list-devices")
        try:
            rc = self.lib.rtlsdr_open(ctypes.byref(self.device), config.device_index)
            if rc != 0 or not self.device:
                raise SourceError(f"Cannot open RTL-SDR #{config.device_index} (code {rc}); close other receivers and check the device's USB driver")
            self._check(self.lib.rtlsdr_set_sample_rate(self.device, config.sample_rate), "set sample rate")
            self.sample_rate = _actual_rate(self.lib.rtlsdr_get_sample_rate(self.device))
            self._check(self.lib.rtlsdr_set_center_freq(self.device, frequency), "tune frequency")
            actual = int(self.lib.rtlsdr_get_center_freq(self.device))
            if actual <= 0:
                raise SourceError("RTL-SDR did not report a tuned frequency")
            self.frequency = actual
            self._configure_gain(config.gain)
            self._check(self.lib.rtlsdr_reset_buffer(self.device), "reset USB buffer")
            if self.sample_rate > 2_400_000:
                self.warnings.append("RTL-SDR rates above 2.4 MS/s may lose samples")
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _check(code, operation):
        if code != 0:
            raise SourceError(f"RTL-SDR could not {operation} (code {code})")

    def _configure_gain(self, gain):
        self._check(self.lib.rtlsdr_set_tuner_gain_mode(self.device, 0 if gain is None else 1), "set gain mode")
        if gain is None:
            return
        size = int(self.lib.rtlsdr_get_tuner_gains(self.device, None))
        if not 0 < size <= 1024:
            raise SourceError("RTL-SDR did not report supported manual gains")
        values = (ctypes.c_int * size)()
        returned = int(self.lib.rtlsdr_get_tuner_gains(self.device, values))
        if not 0 < returned <= size:
            raise SourceError("RTL-SDR returned invalid gain information")
        gains = list(values[:returned])
        target = gain * 10
        if not min(gains) <= target <= max(gains):
            raise SourceError(f"Gain must be between {min(gains) / 10} and {max(gains) / 10} dB for this tuner")
        selected = min(gains, key=lambda value: abs(value - target))
        self._check(self.lib.rtlsdr_set_tuner_gain(self.device, selected), "set gain")
        if selected != target:
            self.warnings.append(f"Manual gain rounded to supported value {selected / 10:g} dB")

    def read_iq(self, count):
        count = _read_count(count)
        if not self.device:
            raise SourceError("RTL-SDR source is closed")
        # Native USB transfers need 512-byte alignment. Reject unsupported
        # counts rather than over-reading and silently dropping IQ samples.
        if count % 256:
            raise ValueError("Native RTL-SDR read count must be a multiple of 256 complex samples")
        byte_count = count * 2
        buffer = (ctypes.c_ubyte * byte_count)()
        received = ctypes.c_int()
        self._check(self.lib.rtlsdr_read_sync(self.device, buffer, byte_count, ctypes.byref(received)), "read IQ")
        if not 0 < received.value <= byte_count or received.value % 2:
            raise SourceError("RTL-SDR returned an invalid IQ byte count")
        # A short native transfer is surfaced as short data, never padded. The
        # caller can discard it; native read_sync has no configurable timeout.
        raw = (np.ctypeslib.as_array(buffer)[:received.value].astype(np.float32) - 127.5) / 127.5
        return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64, copy=False)

    def close(self):
        if self.device:
            device, self.device = self.device, ctypes.c_void_p()
            self._check(self.lib.rtlsdr_close(device), "close device")


def _load_soapy():
    try:
        return importlib.import_module("SoapySDR")
    except (ImportError, OSError) as exc:
        raise SourceError("Install SoapySDR Python bindings and your receiver's Soapy driver module in this Python environment") from exc


def _range_contains(ranges, value):
    for interval in ranges:
        low, high = float(interval.minimum()), float(interval.maximum())
        if math.isfinite(low) and math.isfinite(high) and low <= value <= high:
            return True
    return False


class SoapySource:
    """One RX channel, CF32 only; no TX stream or write API is ever used.

    Calls are owned by the bridge's receiver thread, not thread-safe. Stream
    reads have a deadline and attempt cap. A misbehaving native driver that
    ignores timeoutUs cannot be interrupted safely from Python.
    """

    def __init__(self, frequency, config=None, *, soapy=None):
        config = SourceConfig(backend="soapy") if config is None else _config(config)
        if config.backend != "soapy":
            raise ValueError("SoapySource requires backend soapy")
        frequency = _frequency(frequency)
        self.soapy = soapy if soapy is not None else _load_soapy()
        self.device = None
        self.stream = None
        self.active = False
        self.warnings = []
        self.frequency = frequency
        self.sample_rate = config.sample_rate
        self.channel = config.channel
        self.overflows = 0
        self.discontinuities = 0
        self.timeouts = 0
        self.rx = self.soapy.SOAPY_SDR_RX
        matches = list(self.soapy.Device.enumerate(config.device_args))
        if not matches:
            raise SourceError("No matching SoapySDR receiver; install its module and use --list-devices")
        if len(matches) != 1:
            raise SourceError("Multiple SoapySDR devices match; provide more specific --device-args (for example driver and serial)")
        try:
            self.device = self.soapy.Device(matches[0])
            d = self.device
            if config.channel >= int(d.getNumChannels(self.rx)):
                raise SourceError(f"RX channel {config.channel} does not exist on this device")
            if not _range_contains(d.getFrequencyRange(self.rx, self.channel), frequency):
                raise SourceError(f"{frequency} Hz is outside the reported RX frequency ranges")
            if config.antenna is not None:
                if config.antenna not in d.listAntennas(self.rx, self.channel):
                    raise SourceError(f"RX antenna {config.antenna!r} was not reported by this device")
                d.setAntenna(self.rx, self.channel, config.antenna)
            # Let the driver negotiate supported rates, then use the readback
            # everywhere downstream rather than pretending the request worked.
            d.setSampleRate(self.rx, self.channel, config.sample_rate)
            self.sample_rate = _actual_rate(d.getSampleRate(self.rx, self.channel))
            if self.sample_rate != config.sample_rate:
                self.warnings.append(f"Sample rate negotiated to {self.sample_rate} samples/s")
            d.setFrequency(self.rx, self.channel, frequency)
            tuned = float(d.getFrequency(self.rx, self.channel))
            if not math.isfinite(tuned) or not _range_contains(d.getFrequencyRange(self.rx, self.channel), tuned):
                raise SourceError("Driver returned an invalid tuned frequency")
            self.frequency = int(round(tuned))
            self._configure_gain(config.gain)
            self.stream = d.setupStream(self.rx, self.soapy.SOAPY_SDR_CF32, [self.channel])
            if self.stream is None:
                raise SourceError("SoapySDR failed to create a CF32 receive stream")
            # Activation can partially succeed before a driver reports failure;
            # cleanup must still attempt deactivation.
            self.active = True
            result = d.activateStream(self.stream)
            if result != 0:
                raise SourceError(f"SoapySDR stream activation failed (code {result})")
        except Exception as exc:
            self._cleanup(suppress=True)
            if isinstance(exc, (ValueError, SourceError)):
                raise
            raise SourceError(f"Could not configure SoapySDR receiver: {exc}") from exc

    def _configure_gain(self, gain):
        d = self.device
        automatic = bool(d.hasGainMode(self.rx, self.channel))
        if gain is None:
            if automatic:
                try:
                    d.setGainMode(self.rx, self.channel, True)
                    return
                except Exception:
                    # Best-effort disabling avoids retaining a partly enabled
                    # AGC state when its implementation rejects the request.
                    try:
                        d.setGainMode(self.rx, self.channel, False)
                    except Exception:
                        pass
            self.warnings.append("Automatic gain unavailable; retaining device default gain. Use --gain if reception is weak or overloaded.")
            return
        bounds = d.getGainRange(self.rx, self.channel)
        if not _range_contains([bounds], gain):
            raise SourceError("Manual gain is outside this device's reported RX gain range")
        if automatic:
            d.setGainMode(self.rx, self.channel, False)
        d.setGain(self.rx, self.channel, gain)

    def read_iq(self, count):
        count = _read_count(count)
        if self.device is None or self.stream is None:
            raise SourceError("SoapySDR source is closed")
        output = np.empty(count, dtype=np.complex64)
        filled = 0
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        timeout = self.soapy.SOAPY_SDR_TIMEOUT
        overflow = self.soapy.SOAPY_SDR_OVERFLOW
        for _ in range(MAX_READ_ATTEMPTS):
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                break
            result = self.device.readStream(
                self.stream, [output[filled:]], count - filled,
                timeoutUs=max(1, min(100_000, int(remaining_time * 1_000_000))),
            )
            received = int(result.ret)
            if received == overflow:
                self.overflows += 1
                # The owner uses this counter to reset stateful FM filters and
                # audio generation before consuming samples following a gap.
                self.discontinuities += 1
                # Never join samples across a known gap into one FFT/FM block.
                filled = 0
                continue
            if received in (timeout, 0):
                self.timeouts += 1
                continue
            if received < 0 or received > count - filled:
                raise SourceError(f"SoapySDR read failed or returned an invalid count (code {received})")
            filled += received
            if filled == count:
                if not np.isfinite(output).all():
                    raise SourceError("SoapySDR returned non-finite IQ samples")
                return output
        raise SourceError(f"SoapySDR did not deliver a complete IQ block within the read budget ({filled}/{count} samples)")

    def _cleanup(self, suppress=False):
        device, stream, active = self.device, self.stream, self.active
        self.device = self.stream = None
        self.active = False
        first_error = None
        if device is not None and stream is not None:
            if active:
                try:
                    result = device.deactivateStream(stream)
                    if result != 0:
                        first_error = SourceError(f"SoapySDR deactivation failed (code {result})")
                except Exception as exc:
                    first_error = exc
            try:
                device.closeStream(stream)
            except Exception as exc:
                first_error = first_error or exc
        # The Python binding's Device destructor calls unmake; do not call the
        # C++ factory directly, which would risk releasing the same device twice.
        if first_error is not None and not suppress:
            raise SourceError(f"SoapySDR cleanup failed: {first_error}") from first_error

    def close(self):
        self._cleanup()


class DemoSource:
    """Synthetic RF scene: no radio library loading or hardware access."""

    def __init__(self, frequency, sample_rate=2_400_000):
        self.frequency = _frequency(frequency)
        self.sample_rate = _integer(sample_rate, "sample_rate", MIN_SAMPLE_RATE, MAX_SAMPLE_RATE)
        self.phase = np.zeros(3, dtype=np.float64)
        self.tick = 0
        self.closed = False
        self.warnings = []
        self.random = np.random.default_rng()

    def read_iq(self, count):
        count = _read_count(count)
        if self.closed:
            raise SourceError("Demo source is closed")
        self.tick += 1
        n = np.arange(count, dtype=np.float64)
        offsets = np.array([-0.28, 0.09, 0.33]) * self.sample_rate
        offsets[1] += math.sin(self.tick / 18) * 12_000
        signal = np.zeros(count, dtype=np.complex128)
        for index, (offset, amplitude) in enumerate(zip(offsets, (0.16, 0.42, 0.24))):
            step = 2 * math.pi * offset / self.sample_rate
            phase = self.phase[index] + step * n
            if index == 1:
                phase += 0.75 * np.sin(2 * math.pi * n * 1300 / self.sample_rate + self.tick / 7)
            signal += amplitude * np.exp(1j * phase)
            self.phase[index] = (phase[-1] + step) % (2 * math.pi)
        noise = self.random.normal(0, 0.045, count) + 1j * self.random.normal(0, 0.045, count)
        time.sleep(count / self.sample_rate * 0.55)
        return (signal + noise).astype(np.complex64)

    def close(self):
        self.closed = True


def create_source(frequency, demo, config=None):
    config = _config(config)
    if not isinstance(demo, bool):
        raise TypeError("demo must be a boolean")
    if demo:
        return DemoSource(frequency, config.sample_rate)
    if config.backend == "rtl":
        return RtlSdrSource(frequency, config)
    return SoapySource(frequency, config)


def enumerate_devices(config=None):
    """Explicit discovery; never starts an RX stream or transmits anything.

    Soapy modules may probe hardware during enumeration. Device arguments and
    serials are returned only to the CLI caller, not automatically made public.
    """
    config = _config(config)
    if config.backend == "rtl":
        return RtlSdrLibrary(config.rtl_library).devices()
    soapy = _load_soapy()
    return [
        {"backend": "soapy", "label": str(item.get("label", item.get("driver", "SoapySDR"))),
         "args": {str(key): str(value) for key, value in dict(item).items()}}
        for item in soapy.Device.enumerate(config.device_args)
    ]
