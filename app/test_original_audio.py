"""No radio or speakers: exercise audio concurrency with injected fake devices."""
import threading
import time
import unittest

import numpy as np

from original_audio import AudioMonitor


def eventually(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for audio worker")


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created_on = threading.current_thread().name
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def abort(self):
        self.kwargs["finished_callback"]()

    def close(self):
        self.closed = True

    def render(self, frames=480, underflow=False):
        class Status:
            output_underflow = underflow
        output = np.full((frames, 1), np.nan, dtype=np.float32)
        self.kwargs["callback"](output, frames, None, Status())
        return output[:, 0]


class FakeDemod:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.resets = 0
        self.calls = 0

    def reset(self):
        self.resets += 1

    def process(self, iq):
        self.calls += 1
        # Most lifecycle tests need one full startup cushion. Timing-specific
        # tests below model the real 655/656-sample chunks explicitly.
        return np.full(AudioMonitor.PREBUFFER_SAMPLES, np.real(iq[0]),
                       dtype=np.float32)


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.streams = []
        self.demods = []
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.close()

    def monitor(self, stream_factory=None, demod_factory=None):
        def stream(**kwargs):
            result = FakeStream(**kwargs)
            self.streams.append(result)
            return result

        def demod(**kwargs):
            result = FakeDemod(**kwargs)
            self.demods.append(result)
            return result

        monitor = AudioMonitor(stream_factory=stream_factory or stream,
                               demod_factory=demod_factory or demod)
        self.monitors.append(monitor)
        return monitor

    def enable(self, monitor, frequency=99_200_000, generation=1):
        monitor.set_source(frequency, generation)
        monitor.configure(enabled=True)
        eventually(lambda: monitor.snapshot()["status"] == "buffering")

    def send(self, monitor, value=0.8, frequency=99_200_000, generation=1):
        monitor.submit(np.full(32768, complex(value), dtype=np.complex64),
                       2_400_000, frequency, generation)

    def test_disabled_default_opens_nothing(self):
        monitor = self.monitor()
        monitor.set_source(99_200_000, 1)
        self.send(monitor)
        time.sleep(0.03)
        self.assertEqual(self.streams, [])
        self.assertEqual(self.demods, [])
        state = monitor.snapshot()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["muted"])
        self.assertEqual(state["volume"], 0.15)
        self.assertEqual(state["status"], "disabled")

    def test_mono_worker_and_fade_in(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        output = self.streams[0].render()
        self.assertTrue(np.all(np.isfinite(output)))
        self.assertGreater(output[-1], output[0])
        self.assertLessEqual(float(output.max()), 0.151)
        self.assertTrue(np.all(np.diff(output) >= 0))
        self.assertEqual(self.streams[0].created_on, "radio-terrain-audio")
        self.assertEqual(self.streams[0].kwargs["channels"], 1)
        self.assertEqual(self.streams[0].kwargs["samplerate"], 48000)
        self.assertEqual(self.demods[0].kwargs["deemphasis_us"], 50.0)
        self.assertEqual(self.demods[0].resets, 1)
        self.assertEqual(monitor.snapshot()["status"], "playing")

    def test_disable_and_disconnect_immediately_silence(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        stream = self.streams[0]
        monitor.set_source(None, 2)
        self.assertTrue(monitor.snapshot()["enabled"])
        self.assertEqual(monitor.snapshot()["status"], "waiting")
        self.assertEqual(monitor.snapshot()["buffer_ms"], 0)
        self.assertTrue(np.all(stream.render() == 0))
        monitor.configure(enabled=False)
        self.assertEqual(monitor.snapshot()["status"], "disabled")
        eventually(lambda: stream.closed)

    def test_demo_and_out_of_band_do_not_create_streams(self):
        monitor = self.monitor()
        monitor.configure(enabled=True)
        monitor.set_source(99_200_000, 1, demo=True)
        self.send(monitor)
        self.assertEqual(monitor.snapshot()["status"], "demo")
        monitor.set_source(433_920_000, 2)
        self.send(monitor, frequency=433_920_000, generation=2)
        self.assertEqual(monitor.snapshot()["status"], "unsupported")
        time.sleep(0.03)
        self.assertFalse(self.streams)

    def test_mute_and_volume_do_not_reopen_device(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        self.streams[0].render()
        monitor.configure(volume=0.6, muted=True)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        output = self.streams[0].render()
        self.assertGreater(output[0], output[-1])
        self.assertEqual(output[-1], 0)
        self.assertEqual(monitor.snapshot()["status"], "muted")
        monitor.configure(muted=False)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        self.assertGreater(self.streams[0].render().max(), 0)
        self.assertEqual(len(self.streams), 1)

    def test_late_old_frequency_dsp_result_is_discarded(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowDemod(FakeDemod):
            def process(self, iq):
                if np.real(iq[0]) > 0:
                    entered.set()
                    release.wait(2)
                return super().process(iq)

        monitor = self.monitor(demod_factory=SlowDemod)
        self.enable(monitor)
        self.send(monitor, value=0.8)
        self.assertTrue(entered.wait(1))
        monitor.set_source(None, 2)
        monitor.set_source(100_000_000, 2)
        self.send(monitor, value=0.8)  # Wrong station/generation must be ignored.
        self.send(monitor, value=-0.8, frequency=100_000_000, generation=2)
        release.set()
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        output = self.streams[-1].render()
        self.assertTrue(np.all(output <= 0))
        self.assertLess(output.min(), 0)
        self.assertEqual(monitor.snapshot()["frequency_hz"], 100_000_000)

    def test_dependency_error_is_isolated_and_can_retry(self):
        attempts = []

        def factory(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ModuleNotFoundError("No module named sounddevice")
            result = FakeStream(**kwargs)
            self.streams.append(result)
            return result

        monitor = self.monitor(stream_factory=factory)
        monitor.set_source(99_200_000, 1)
        monitor.configure(enabled=True)
        eventually(lambda: monitor.snapshot()["status"] == "error")
        self.assertIn("sounddevice", monitor.snapshot()["error"])
        self.send(monitor)
        time.sleep(0.04)
        self.assertEqual(len(attempts), 1)
        monitor.configure(enabled=False)
        monitor.configure(enabled=True)
        eventually(lambda: monitor.snapshot()["status"] == "buffering")
        self.assertIsNone(monitor.snapshot()["error"])
        self.assertEqual(len(attempts), 2)

    def test_retune_reuses_stream_and_resets_demodulator(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        monitor.set_source(108_000_000, 2)
        self.assertEqual(monitor.snapshot()["buffer_ms"], 0)
        self.send(monitor, frequency=108_000_000, generation=2)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        self.assertEqual(len(self.streams), 1)
        self.assertEqual(len(self.demods), 1)
        self.assertEqual(self.demods[0].resets, 2)
        monitor.set_source(87_500_000, 3)
        self.send(monitor, frequency=87_500_000, generation=3)
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        self.assertEqual(self.demods[0].resets, 3)

    def test_dsp_error_is_isolated(self):
        class BrokenDemod(FakeDemod):
            def process(self, iq):
                raise RuntimeError("synthetic DSP failure")

        monitor = self.monitor(demod_factory=BrokenDemod)
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["status"] == "error")
        self.assertIn("synthetic DSP failure", monitor.snapshot()["error"])
        self.assertTrue(np.all(self.streams[0].render() == 0))

    def test_device_disconnection_is_reported(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.streams[0].kwargs["finished_callback"]()
        eventually(lambda: monitor.snapshot()["status"] == "error")
        self.assertIn("输出设备已停止", monitor.snapshot()["error"])

    def test_underruns_and_nonblocking_callback(self):
        monitor = self.monitor()
        self.enable(monitor)
        stream = self.streams[0]
        self.assertTrue(np.all(stream.render() == 0))
        before = monitor.snapshot()["underruns"]
        with monitor._condition:
            start = time.monotonic()
            output = stream.render()
            self.assertLess(time.monotonic() - start, 0.1)
        self.assertTrue(np.all(output == 0))
        self.assertGreater(monitor.snapshot()["underruns"], before)
        self.assertEqual(monitor.snapshot()["lock_underruns"], 1)
        self.assertEqual(monitor.snapshot()["pcm_underruns"], 0)
        self.assertEqual(monitor.snapshot()["driver_underruns"], 0)

    def feed_pcm(self, monitor, samples, value=0.8):
        # Deterministic scheduling model: no sleeping, radio, or real callback
        # timer. Only this test injects the PCM that the DSP would have emitted.
        pcm = np.full(samples, value, dtype=np.float32)
        with monitor._condition:
            monitor._pcm.append((pcm, 0))
            monitor._pcm_samples += samples
            monitor._have_audio = True

    def test_startup_prebuffer_waits_without_counting_intentional_silence(self):
        monitor = self.monitor()
        self.enable(monitor)
        stream = self.streams[0]
        for _ in range(3):
            self.feed_pcm(monitor, 480)
            self.assertTrue(np.all(stream.render() == 0))
            self.assertEqual(monitor.snapshot()["status"], "buffering")
            self.assertEqual(monitor.snapshot()["underruns"], 0)
        self.assertEqual(monitor.snapshot()["buffer_ms"], 30)
        self.feed_pcm(monitor, 480)
        output = stream.render()
        self.assertGreater(output[-1], output[0])
        self.assertGreater(output.max(), 0)
        self.assertEqual(monitor.snapshot()["status"], "playing")
        self.assertEqual(monitor.snapshot()["underruns"], 0)

    def test_shortage_rebuffers_once_and_fades_back_in(self):
        monitor = self.monitor()
        self.enable(monitor)
        stream = self.streams[0]
        self.feed_pcm(monitor, monitor.PREBUFFER_SAMPLES + 100)
        for _ in range(4):
            self.assertGreater(stream.render().max(), 0)
        self.assertTrue(np.all(stream.render() == 0))
        self.assertEqual(monitor.snapshot()["pcm_underruns"], 1)
        self.assertEqual(monitor.snapshot()["status"], "buffering")
        # Preserve the partial block without repeatedly consuming/recounting
        # every small arrival; deliberate rebuffering stays quiet.
        self.assertEqual(monitor._pcm_samples, 100)
        for _ in range(3):
            self.feed_pcm(monitor, 480)
            self.assertTrue(np.all(stream.render() == 0))
            self.assertEqual(monitor.snapshot()["pcm_underruns"], 1)
        self.feed_pcm(monitor, 480)
        output = stream.render()
        self.assertGreater(output[-1], output[0])
        self.assertLess(output[0], 0.001)
        self.assertEqual(monitor.snapshot()["status"], "playing")
        self.assertEqual(monitor.snapshot()["underruns"], 1)

    def test_driver_underrun_is_distinct_even_during_prebuffering(self):
        monitor = self.monitor()
        self.enable(monitor)
        self.assertTrue(np.all(self.streams[0].render(underflow=True) == 0))
        state = monitor.snapshot()
        self.assertEqual(state["driver_underruns"], 1)
        self.assertEqual(state["pcm_underruns"], 0)
        self.assertEqual(state["lock_underruns"], 0)
        self.assertEqual(state["underruns"], 1)

    def test_real_iq_chunk_cadence_with_jitter_has_no_steady_pcm_underruns(self):
        monitor = self.monitor()
        self.enable(monitor)
        stream = self.streams[0]
        # 32768 / 2400000 seconds per real RF read, decimated by 50.
        # Burst delivery is delayed by up to 18 ms, deliberately exceeding one
        # 10 ms callback period. Sort deliveries to preserve RF sample order.
        arrivals = []
        generated = 0
        for index in range(370):
            total = ((index + 1) * 32768 + 49) // 50
            count, generated = total - generated, total
            jitter_ms = (0, 4, 18, 0, 7, 0, 14)[index % 7]
            arrivals.append(((index + 1) * 32768 / 2400 + jitter_ms, count))
        # An RF thread cannot deliver a later block ahead of an earlier one.
        last_arrival = 0.0
        arrivals = [(last_arrival := max(last_arrival, at), count)
                    for at, count in arrivals]
        position = 0
        nonzero_callbacks = 0
        for now_ms in range(0, 5001, 10):
            while position < len(arrivals) and arrivals[position][0] <= now_ms:
                self.feed_pcm(monitor, arrivals[position][1])
                position += 1
            if np.any(stream.render()):
                nonzero_callbacks += 1
        self.assertGreater(nonzero_callbacks, 480)
        self.assertEqual(monitor.snapshot()["pcm_underruns"], 0)
        self.assertEqual(monitor.snapshot()["underruns"], 0)
        self.assertLess(monitor.snapshot()["buffer_ms"], 70)

    def test_retune_requires_fresh_prebuffer_and_clears_old_station(self):
        monitor = self.monitor()
        self.enable(monitor)
        stream = self.streams[0]
        self.feed_pcm(monitor, monitor.PREBUFFER_SAMPLES)
        self.assertGreater(stream.render().max(), 0)
        monitor.set_source(100_000_000, 2)
        self.assertEqual(monitor.snapshot()["buffer_ms"], 0)
        self.assertTrue(np.all(stream.render() == 0))
        self.feed_pcm(monitor, 960, value=-0.8)
        self.assertTrue(np.all(stream.render() == 0))
        self.feed_pcm(monitor, 960, value=-0.8)
        output = stream.render()
        self.assertTrue(np.all(output < 0))
        self.assertGreater(output[0], output[-1])
        self.assertEqual(monitor.snapshot()["pcm_underruns"], 0)

    def test_pcm_buffer_is_bounded_and_nonfinite_is_sanitized(self):
        class LongDemod(FakeDemod):
            def process(self, iq):
                result = np.full(25_000, 2.0, dtype=np.float32)
                result[-1] = np.nan
                result[-2] = np.inf
                return result

        monitor = self.monitor(demod_factory=LongDemod)
        self.enable(monitor)
        self.send(monitor)
        eventually(lambda: monitor.snapshot()["overruns"] > 0)
        self.assertLessEqual(monitor.snapshot()["buffer_ms"], 200)
        output = self.streams[0].render(frames=9600)
        self.assertTrue(np.all(np.isfinite(output)))
        self.assertLessEqual(np.abs(output).max(), 1)
        self.assertEqual(output[-1], 0)

    def test_iq_overflow_invalidates_inflight_result_and_copies_input(self):
        entered = threading.Event()
        release = threading.Event()
        seen = []

        class SlowDemod(FakeDemod):
            def process(self, iq):
                seen.append(float(np.real(iq[0])))
                if len(seen) == 1:
                    entered.set()
                    release.wait(2)
                return super().process(iq)

        monitor = self.monitor(demod_factory=SlowDemod)
        self.enable(monitor)
        block = np.ones(32768, dtype=np.complex64)
        monitor.submit(block, 2_400_000, 99_200_000, 1)
        self.assertTrue(entered.wait(1))
        block[:] = -1
        for _ in range(20):
            monitor.submit(block, 2_400_000, 99_200_000, 1)
        with monitor._condition:
            self.assertLessEqual(len(monitor._iq), monitor.MAX_IQ_BLOCKS)
        self.assertGreater(monitor.snapshot()["overruns"], 0)
        release.set()
        eventually(lambda: monitor.snapshot()["buffer_ms"] > 0)
        self.assertEqual(seen[0], 1)
        self.assertTrue(np.all(self.streams[0].render() <= 0))

    def test_close_is_bounded_even_when_driver_close_hangs(self):
        release = threading.Event()

        class HungClose(FakeStream):
            def close(self):
                release.wait(3)
                super().close()

        monitor = self.monitor(stream_factory=HungClose)
        self.enable(monitor)
        started = time.monotonic()
        monitor.close()
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(monitor.snapshot()["status"], "closed")
        release.set()
        eventually(lambda: not monitor._thread.is_alive())

    def test_invalid_controls_are_atomic(self):
        monitor = self.monitor()
        for volume in (True, float("nan"), float("inf"), -0.1, 1.1, "0.5"):
            with self.assertRaises(ValueError):
                monitor.configure(enabled=True, volume=volume)
            self.assertFalse(monitor.snapshot()["enabled"])
        for kwargs in ({"enabled": 1}, {"muted": "false"}):
            with self.assertRaises(ValueError):
                monitor.configure(**kwargs)


if __name__ == "__main__":
    unittest.main()
