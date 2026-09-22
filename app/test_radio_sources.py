"""Hardware-free receiver contract tests with fake native/Soapy drivers."""

import ctypes
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import radio_sources as sources
from radio_sources import DemoSource, RtlSdrSource, SoapySource, SourceConfig, SourceError


class FakeRange:
    def __init__(self, low, high):
        self.low, self.high = low, high

    def minimum(self):
        return self.low

    def maximum(self):
        return self.high


class FakeSoapyDevice:
    def __init__(self):
        self.calls = []
        self.frequency = 93_400_000
        self.rate = 2_400_000
        self.negotiated_rate = None
        self.channels = 1
        self.ranges = [FakeRange(50_000_000, 1_700_000_000)]
        self.gain_range = FakeRange(-10, 60)
        self.agc = True
        self.fail = None
        self.activation = 0
        self.deactivation = 0
        self.reads = []
        self.default_read = None
        self.stream = object()

    def record(self, operation, *args):
        self.calls.append((operation, *args))
        if operation == self.fail:
            raise RuntimeError(f"fake failure: {operation}")

    def rx(self, operation, direction, channel, *args):
        if direction != 1:
            raise AssertionError("Only receive direction is permitted")
        self.record(operation, direction, channel, *args)

    def getNumChannels(self, direction):
        self.record("getNumChannels", direction)
        if direction != 1:
            raise AssertionError("RX channels only")
        return self.channels

    def getFrequencyRange(self, direction, channel):
        self.rx("getFrequencyRange", direction, channel)
        return self.ranges

    def listAntennas(self, direction, channel):
        self.rx("listAntennas", direction, channel)
        return ["RX", "RX2"]

    def setAntenna(self, direction, channel, name):
        self.rx("setAntenna", direction, channel, name)

    def setSampleRate(self, direction, channel, value):
        self.rx("setSampleRate", direction, channel, value)
        self.rate = self.negotiated_rate if self.negotiated_rate is not None else value

    def getSampleRate(self, direction, channel):
        self.rx("getSampleRate", direction, channel)
        return self.rate

    def setFrequency(self, direction, channel, value):
        self.rx("setFrequency", direction, channel, value)
        self.frequency = value

    def getFrequency(self, direction, channel):
        self.rx("getFrequency", direction, channel)
        return self.frequency

    def hasGainMode(self, direction, channel):
        self.rx("hasGainMode", direction, channel)
        return self.agc

    def setGainMode(self, direction, channel, value):
        self.rx("setGainMode", direction, channel, value)

    def getGainRange(self, direction, channel):
        self.rx("getGainRange", direction, channel)
        return self.gain_range

    def setGain(self, direction, channel, value):
        self.rx("setGain", direction, channel, value)

    def setupStream(self, direction, format_name, channels):
        if direction != 1 or format_name != "CF32":
            raise AssertionError("Only RX CF32 is permitted")
        self.record("setupStream", direction, format_name, channels)
        return self.stream

    def activateStream(self, stream):
        self.record("activateStream", stream)
        return self.activation

    def deactivateStream(self, stream):
        self.record("deactivateStream", stream)
        return self.deactivation

    def closeStream(self, stream):
        self.record("closeStream", stream)

    def readStream(self, stream, buffers, count, timeoutUs):
        self.record("readStream", count, timeoutUs)
        if len(buffers) != 1 or buffers[0].dtype != np.complex64:
            raise AssertionError("One complex64 receive buffer required")
        item = self.reads.pop(0) if self.reads else self.default_read
        if item is None:
            item = count
        if isinstance(item, np.ndarray):
            buffers[0][:len(item)] = item
            return SimpleNamespace(ret=len(item))
        if item > 0 and item <= count:
            buffers[0][:item] = 0.25 + 0.5j
        return SimpleNamespace(ret=item)


class FakeSoapy:
    SOAPY_SDR_RX = 1
    SOAPY_SDR_CF32 = "CF32"
    SOAPY_SDR_TIMEOUT = -1
    SOAPY_SDR_OVERFLOW = -4

    def __init__(self):
        self.hardware = FakeSoapyDevice()
        self.matches = [{"driver": "fake", "serial": "one", "label": "Fake receiver"}]
        self.enumerated = []
        self.constructed = []
        owner = self

        class Factory:
            @staticmethod
            def enumerate(args):
                owner.enumerated.append(args)
                return owner.matches

            def __new__(cls, args):
                owner.constructed.append(args)
                return owner.hardware

        self.Device = Factory


class FakeRtl:
    def __init__(self):
        self.dll = self
        self.calls = []
        self.fail = None
        self.open_result = 0
        self.rate = 2_400_000
        self.frequency = 93_400_000
        self.gain_values = [0, 90, 140, 280]
        self.received = None

    def devices(self):
        return [{"backend": "rtl", "index": 0, "label": "Fake RTL"}]

    def record(self, operation, *args):
        self.calls.append((operation, *args))
        return -5 if self.fail == operation else 0

    def rtlsdr_open(self, target, index):
        self.record("open", index)
        ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
        return self.open_result

    def rtlsdr_close(self, device):
        return self.record("close")

    def rtlsdr_set_sample_rate(self, device, rate):
        self.rate = rate
        return self.record("rate", rate)

    def rtlsdr_get_sample_rate(self, device):
        return self.rate

    def rtlsdr_set_center_freq(self, device, frequency):
        self.frequency = frequency
        return self.record("frequency", frequency)

    def rtlsdr_get_center_freq(self, device):
        return self.frequency

    def rtlsdr_set_tuner_gain_mode(self, device, manual):
        return self.record("gain_mode", manual)

    def rtlsdr_get_tuner_gains(self, device, buffer):
        if buffer is not None:
            for index, gain in enumerate(self.gain_values):
                buffer[index] = gain
        return len(self.gain_values)

    def rtlsdr_set_tuner_gain(self, device, gain):
        return self.record("gain", gain)

    def rtlsdr_reset_buffer(self, device):
        return self.record("reset")

    def rtlsdr_read_sync(self, device, buffer, count, received):
        for index in range(count):
            buffer[index] = 0 if index % 2 == 0 else 255
        ctypes.cast(received, ctypes.POINTER(ctypes.c_int))[0] = count if self.received is None else self.received
        return self.record("read", count)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_frozen(self):
        config = SourceConfig()
        self.assertEqual(config.sample_rate, 2_400_000)
        with self.assertRaises(FrozenInstanceError):
            config.channel = 1

    def test_invalid_configuration(self):
        invalid = [
            {"backend": "tx"}, {"backend": "demo"}, {"sample_rate": True},
            {"sample_rate": 1_799_999}, {"sample_rate": float("nan")},
            {"sample_rate": 3_200_001}, {"sample_rate": 2_400_000.1},
            {"device_index": -1}, {"channel": 1}, {"device_args": "driver=x"},
            {"antenna": "RX"}, {"rtl_library": ""}, {"rtl_library": 5},
            {"gain": True}, {"gain": float("inf")},
            {"backend": "soapy", "device_index": 1},
            {"backend": "soapy", "rtl_library": "example.dll"},
            {"backend": "soapy", "channel": -1},
            {"backend": "soapy", "device_args": "driver=x\nserial=y"},
            {"backend": "soapy", "antenna": ""},
            {"backend": "soapy", "sample_rate": 10_000_001},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                SourceConfig(**fields)

    def test_valid_soapy_settings(self):
        config = SourceConfig(backend="soapy", device_args="driver=fake,serial=one", sample_rate=10_000_000, channel=1, gain=5, antenna="RX")
        self.assertEqual(config.gain, 5.0)

    def test_factory_routes_without_hardware(self):
        with patch.object(sources, "RtlSdrSource") as rtl, patch.object(sources, "SoapySource") as soapy:
            sources.create_source(93_400_000, False)
            rtl.assert_called_once()
            sources.create_source(93_400_000, False, SourceConfig(backend="soapy"))
            soapy.assert_called_once()

    def test_demo_never_loads_drivers(self):
        with patch.object(sources, "RtlSdrLibrary", side_effect=AssertionError("hardware")), patch.object(sources, "_load_soapy", side_effect=AssertionError("hardware")):
            demo = sources.create_source(93_400_000, True, SourceConfig(backend="soapy"))
            with patch.object(sources.time, "sleep"):
                iq = demo.read_iq(1024)
            self.assertEqual(iq.dtype, np.complex64)
            self.assertEqual(len(iq), 1024)
            self.assertTrue(np.isfinite(iq).all())
            demo.close()
            demo.close()
            with self.assertRaises(SourceError):
                demo.read_iq(10)

    def test_factory_rejects_wrong_types(self):
        with self.assertRaises(TypeError):
            sources.create_source(93_400_000, "false")
        with self.assertRaises(TypeError):
            sources.create_source(93_400_000, False, {})

    def test_read_allocation_limits(self):
        demo = DemoSource(93_400_000)
        for count in (0, -1, True, 1.5, sources.MAX_READ_SAMPLES + 1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                demo.read_iq(count)

    def test_explicit_missing_library_does_not_fall_back(self):
        with patch.object(sources.ctypes, "CDLL") as loader:
            with self.assertRaises(SourceError):
                sources.RtlSdrLibrary(Path(__file__).parent / "missing-test-library.dll")
            loader.assert_not_called()


class RtlTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeRtl()

    def source(self, **kwargs):
        return RtlSdrSource(93_400_000, SourceConfig(**kwargs), library=self.driver)

    def test_native_configuration_conversion_and_close(self):
        source = self.source()
        iq = source.read_iq(256)
        np.testing.assert_array_equal(iq, np.full(256, -1 + 1j, dtype=np.complex64))
        self.assertIn(("gain_mode", 0), self.driver.calls)
        source.close()
        source.close()
        self.assertEqual(self.driver.calls.count(("close",)), 1)
        with self.assertRaises(SourceError):
            source.read_iq(256)

    def test_missing_index_rejected_before_open(self):
        with self.assertRaises(SourceError):
            self.source(device_index=1)
        self.assertEqual(self.driver.calls, [])

    def test_native_invalid_frequency_before_open(self):
        for frequency in (0, True, 1_766_000_001, 6_000_000_000):
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                RtlSdrSource(frequency, library=self.driver)
        self.assertEqual(self.driver.calls, [])

    def test_failed_initialization_releases_handle(self):
        for stage in ("rate", "frequency", "gain_mode", "reset"):
            with self.subTest(stage=stage):
                self.driver = FakeRtl()
                self.driver.fail = stage
                with self.assertRaises(SourceError):
                    self.source()
                self.assertEqual(self.driver.calls.count(("close",)), 1)

    def test_partially_failed_open_releases_handle(self):
        self.driver.open_result = -12
        with self.assertRaises(SourceError):
            self.source()
        self.assertEqual(self.driver.calls.count(("close",)), 1)

    def test_manual_gain_uses_supported_value(self):
        source = self.source(gain=13)
        self.assertIn(("gain_mode", 1), self.driver.calls)
        self.assertIn(("gain", 140), self.driver.calls)
        self.assertTrue(source.warnings)
        source.close()

    def test_manual_gain_rejects_out_of_range(self):
        with self.assertRaises(SourceError):
            self.source(gain=100)
        self.assertIn(("close",), self.driver.calls)

    def test_native_malformed_counts_rejected(self):
        source = self.source()
        for count in (0, -1, 511, 514):
            with self.subTest(count=count), self.assertRaises(SourceError):
                self.driver.received = count
                source.read_iq(256)
        source.close()

    def test_native_short_read_is_not_padded(self):
        source = self.source()
        self.driver.received = 128
        self.assertEqual(len(source.read_iq(256)), 64)
        source.close()

    def test_native_unaligned_read_rejected_without_losing_samples(self):
        source = self.source()
        with self.assertRaises(ValueError):
            source.read_iq(100)
        self.assertFalse(any(call[0] == "read" for call in self.driver.calls))
        self.assertEqual(len(source.read_iq(256)), 256)
        source.close()

    def test_library_loading_prefers_explicit_then_environment(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(sources.ctypes, "CDLL") as loader, patch.object(sources.RtlSdrLibrary, "_bind"):
            with patch.dict(sources.os.environ, {"INVISIBLE_TERRAIN_RTL_LIBRARY": "environment.dll"}):
                sources.RtlSdrLibrary("explicit.dll")
                self.assertTrue(loader.call_args.args[0].endswith("explicit.dll"))
                sources.RtlSdrLibrary()
                self.assertTrue(loader.call_args.args[0].endswith("environment.dll"))

    def test_discovery_does_not_open(self):
        with patch.object(sources, "RtlSdrLibrary", return_value=self.driver):
            self.assertEqual(sources.enumerate_devices()[0]["index"], 0)
        self.assertEqual(self.driver.calls, [])


class SoapyTests(unittest.TestCase):
    def setUp(self):
        self.module = FakeSoapy()
        self.driver = self.module.hardware

    def source(self, **kwargs):
        return SoapySource(93_400_000, SourceConfig(backend="soapy", **kwargs), soapy=self.module)

    def test_receive_only_contract_and_close(self):
        source = self.source(device_args="driver=fake,serial=one")
        self.assertEqual(self.module.enumerated, ["driver=fake,serial=one"])
        self.assertEqual(self.module.constructed, self.module.matches)
        self.assertIn(("setupStream", 1, "CF32", [0]), self.driver.calls)
        self.assertIn(("setGainMode", 1, 0, True), self.driver.calls)
        iq = source.read_iq(1024)
        self.assertEqual(len(iq), 1024)
        source.close()
        source.close()
        self.assertEqual(sum(call[0] == "closeStream" for call in self.driver.calls), 1)
        self.assertIsNone(source.device)
        with self.assertRaises(SourceError):
            source.read_iq(10)

    def test_ambiguous_or_absent_device_never_opens(self):
        for matches in ([], [{"driver": "a"}, {"driver": "b"}]):
            with self.subTest(matches=matches), self.assertRaises(SourceError):
                self.module.matches = matches
                self.source()
        self.assertEqual(self.module.constructed, [])

    def test_wrong_channel_or_frequency_rejected_before_stream(self):
        self.driver.channels = 0
        with self.assertRaises(SourceError):
            self.source()
        self.driver.channels = 1
        self.driver.ranges = [FakeRange(100_000_000, 200_000_000)]
        with self.assertRaises(SourceError):
            self.source()
        self.assertFalse(any(call[0] == "setupStream" for call in self.driver.calls))

    def test_empty_frequency_ranges_are_not_assumed_supported(self):
        self.driver.ranges = []
        with self.assertRaises(SourceError):
            self.source()

    def test_negotiated_rate_not_requested_rate_is_exposed(self):
        self.driver.negotiated_rate = 3_000_000
        source = self.source()
        self.assertEqual(source.sample_rate, 3_000_000)
        self.assertTrue(source.warnings)
        source.close()

    def test_invalid_negotiated_rate_rejected(self):
        for rate in (0, 1_700_000, 10_000_001, float("nan"), 2_400_000.25):
            with self.subTest(rate=rate), self.assertRaises(SourceError):
                self.driver.negotiated_rate = rate
                self.source()
        self.assertFalse(any(call[0] == "setupStream" for call in self.driver.calls))

    def test_manual_gain_and_antenna(self):
        source = self.source(gain=25, antenna="RX2")
        self.assertIn(("setGainMode", 1, 0, False), self.driver.calls)
        self.assertIn(("setGain", 1, 0, 25.0), self.driver.calls)
        self.assertIn(("setAntenna", 1, 0, "RX2"), self.driver.calls)
        source.close()

    def test_invalid_gain_and_antenna_rejected(self):
        for options in ({"gain": 99}, {"antenna": "TX"}):
            with self.subTest(options=options), self.assertRaises(SourceError):
                self.source(**options)

    def test_absent_or_broken_agc_keeps_defaults_with_warning(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                self.driver.agc = broken
                self.driver.fail = "setGainMode" if broken else None
                source = self.source()
                self.assertTrue(source.warnings)
                self.assertFalse(any(call[0] == "setGain" for call in self.driver.calls))
                source.close()

    def test_cleanup_after_activation_error(self):
        self.driver.activation = -5
        with self.assertRaises(SourceError):
            self.source()
        self.assertTrue(any(call[0] == "deactivateStream" for call in self.driver.calls))
        self.assertTrue(any(call[0] == "closeStream" for call in self.driver.calls))

    def test_initialization_errors_before_stream_do_not_leak(self):
        for stage in ("getNumChannels", "getFrequencyRange", "setSampleRate", "setFrequency", "setupStream"):
            with self.subTest(stage=stage):
                self.driver = self.module.hardware = FakeSoapyDevice()
                self.driver.fail = stage
                with self.assertRaises(SourceError):
                    self.source()
                self.assertFalse(any(call[0] == "activateStream" for call in self.driver.calls))

    def test_cleanup_still_closes_after_deactivation_error(self):
        source = self.source()
        self.driver.fail = "deactivateStream"
        with self.assertRaises(SourceError):
            source.close()
        self.assertIsNone(source.device)
        self.assertTrue(any(call[0] == "closeStream" for call in self.driver.calls))
        source.close()

    def test_partial_reads_are_accumulated(self):
        source = self.source()
        self.driver.reads = [4, 3, 3]
        iq = source.read_iq(10)
        self.assertEqual(len(iq), 10)
        self.assertEqual([call[1] for call in self.driver.calls if call[0] == "readStream"], [10, 6, 3])
        source.close()

    def test_timeout_then_read_recovers(self):
        source = self.source()
        self.driver.reads = [-1, 0, 10]
        self.assertEqual(len(source.read_iq(10)), 10)
        self.assertEqual(source.timeouts, 2)
        source.close()

    def test_overflow_discards_partial_block(self):
        source = self.source()
        self.driver.reads = [np.ones(4, dtype=np.complex64), -4, np.full(10, 2 + 3j, dtype=np.complex64)]
        np.testing.assert_array_equal(source.read_iq(10), np.full(10, 2 + 3j, dtype=np.complex64))
        self.assertEqual(source.overflows, 1)
        self.assertEqual(source.discontinuities, 1)
        source.close()

    def test_no_progress_has_attempt_bound(self):
        source = self.source()
        self.driver.default_read = -1
        with patch.object(sources, "MAX_READ_ATTEMPTS", 3), self.assertRaises(SourceError):
            source.read_iq(10)
        self.assertEqual(source.timeouts, 3)
        source.close()

    def test_wall_clock_deadline_bounds_reads(self):
        source = self.source()
        self.driver.reads = [1]
        with patch.object(sources.time, "monotonic", side_effect=[0, 0.1, 1.1]), self.assertRaises(SourceError):
            source.read_iq(10)
        self.assertEqual(sum(call[0] == "readStream" for call in self.driver.calls), 1)
        source.close()

    def test_driver_errors_bad_counts_and_nonfinite_samples(self):
        for result in (-2, 11, np.full(10, np.nan, dtype=np.complex64)):
            with self.subTest(result=result):
                source = self.source()
                self.driver.reads = [result]
                with self.assertRaises(SourceError):
                    source.read_iq(10)
                source.close()

    def test_discovery_only_no_constructor_or_stream(self):
        with patch.object(sources, "_load_soapy", return_value=self.module):
            results = sources.enumerate_devices(SourceConfig(backend="soapy", device_args="driver=fake"))
        self.assertEqual(results[0]["args"]["serial"], "one")
        self.assertEqual(self.module.enumerated, ["driver=fake"])
        self.assertEqual(self.module.constructed, [])
        self.assertEqual(self.driver.calls, [])

    def test_missing_optional_binding_has_actionable_error(self):
        with patch.object(sources.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(SourceError, "Python bindings"):
                sources.enumerate_devices(SourceConfig(backend="soapy"))


if __name__ == "__main__":
    unittest.main()
