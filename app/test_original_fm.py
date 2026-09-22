"""Synthetic-IQ tests only; no SDR or audio devices are opened."""

from __future__ import annotations

import argparse
import math
import time
import unittest

import numpy as np
from scipy import signal

from original_fm import WbfmDemodulator, _FirDecimator


IQ_RATE = 2_400_000
AUDIO_RATE = 48_000


def fm_tone(frequency=1000, seconds=0.2, amplitude=0.6, offset_hz=0):
    count = round(seconds * IQ_RATE)
    t = np.arange(count, dtype=np.float64) / IQ_RATE
    # Integral of a cosine modulation; no borrowed receiver implementation.
    phase = (75_000 * amplitude / frequency * np.sin(2 * np.pi * frequency * t)
             + 2 * np.pi * offset_hz * t)
    return np.exp(1j * phase).astype(np.complex64)


def tone_amplitude(samples, frequency):
    samples = samples[round(0.04 * AUDIO_RATE):]
    t = np.arange(samples.size) / AUDIO_RATE
    return float(2 * abs(np.mean(samples * np.exp(-2j * np.pi * frequency * t))))


class FirDecimatorTests(unittest.TestCase):
    def test_matches_causal_fir_with_arbitrary_boundaries(self):
        random = np.random.default_rng(823)
        samples = (random.normal(size=3007) + 1j * random.normal(size=3007)).astype(np.complex64)
        taps = signal.firwin(121, 0.07).astype(np.float32)
        expected = signal.lfilter(taps, [1.0], samples)[::10]
        decimator = _FirDecimator(taps, 10, np.complex64)
        result = []
        start = 0
        while start < samples.size:
            stop = min(samples.size, start + int(random.integers(1, 73)))
            result.append(decimator.process(samples[start:stop]))
            start = stop
        np.testing.assert_allclose(np.concatenate(result), expected, atol=3e-7)


class WbfmTests(unittest.TestCase):
    def test_1khz_tone_frequency_and_deviation_normalization(self):
        pcm = WbfmDemodulator().process(fm_tone())
        self.assertEqual(pcm.dtype, np.float32)
        self.assertEqual(pcm.shape, (9600,))
        steady = pcm[1920:]
        frequencies = np.fft.rfftfreq(steady.size, 1 / AUDIO_RATE)
        self.assertAlmostEqual(frequencies[np.argmax(abs(np.fft.rfft(steady)))], 1000, delta=7)
        # 50 us de-emphasis attenuates a 1 kHz cosine by approximately 0.954.
        self.assertAlmostEqual(tone_amplitude(pcm, 1000), 0.6 / math.sqrt(1 + (2 * math.pi * 1000 * 50e-6) ** 2), delta=0.02)

    def test_random_chunks_match_one_continuous_call(self):
        iq = fm_tone(seconds=0.123456, offset_hz=8000)
        expected = WbfmDemodulator().process(iq)
        decoder = WbfmDemodulator()
        random = np.random.default_rng(743)
        result = []
        start = 0
        # Include sub-decimator chunks, not just conveniently aligned reads.
        for count in [1, 2, 7, 9, 13, 50, 51, 3]:
            result.append(decoder.process(iq[start:start + count]))
            start += count
        while start < iq.size:
            stop = min(iq.size, start + int(random.integers(1, 8193)))
            result.append(decoder.process(iq[start:stop]))
            result.append(decoder.process(np.empty(0, dtype=np.complex64)))
            start = stop
        actual = np.concatenate(result)
        np.testing.assert_array_equal(actual, expected)

    def test_zero_input_is_finite_silent_and_output_rate_is_preserved(self):
        decoder = WbfmDemodulator()
        total_input = total_output = 0
        for count in [0, 1, 1, 17, 29, 3, 137, 65536, 3]:
            pcm = decoder.process(np.zeros(count, dtype=np.complex64))
            total_input += count
            total_output += pcm.size
            self.assertTrue(np.isfinite(pcm).all())
            self.assertTrue((pcm == 0).all())
            self.assertEqual(total_output, (total_input + 49) // 50)

    def test_reset_is_identical_to_a_new_decoder(self):
        iq = fm_tone(seconds=0.05)
        decoder = WbfmDemodulator()
        decoder.process(fm_tone(frequency=5100, seconds=0.02))
        decoder.reset()
        np.testing.assert_array_equal(decoder.process(iq), WbfmDemodulator().process(iq))

    def test_bad_input_is_rejected_without_changing_state(self):
        decoder = WbfmDemodulator()
        for invalid in [np.ones((2, 3), dtype=np.complex64), [1.0, 2.0], [complex(np.nan, 0)], [complex(0, np.inf)]]:
            with self.assertRaises(ValueError):
                decoder.process(invalid)
        iq = fm_tone(seconds=0.02)
        np.testing.assert_array_equal(decoder.process(iq), WbfmDemodulator().process(iq))

    def test_only_supported_rates_and_valid_deemphasis_are_accepted(self):
        for options in [{"input_rate": 1_024_000}, {"audio_rate": 44_100}, {"input_rate": True}, {"deemphasis_us": 0}, {"deemphasis_us": float("nan")}]:
            with self.assertRaises(ValueError):
                WbfmDemodulator(**options)
        WbfmDemodulator(deemphasis_us=75)

    def test_audio_lowpass_removes_stereo_pilot_and_high_frequency(self):
        base = tone_amplitude(WbfmDemodulator().process(fm_tone(amplitude=0.1)), 1000)
        for frequency in [19_000, 23_000]:
            pcm = WbfmDemodulator().process(fm_tone(frequency, amplitude=0.1))
            self.assertLess(tone_amplitude(pcm, frequency) / base, 0.003)

    def test_out_of_channel_station_is_rejected_before_demodulation(self):
        desired = fm_tone(amplitude=0.3, seconds=0.25)
        adjacent = fm_tone(frequency=3200, amplitude=0.4, seconds=0.25, offset_hz=300_000)
        alone = WbfmDemodulator().process(desired)
        mixed = WbfmDemodulator().process(desired + adjacent)
        useful = tone_amplitude(mixed, 1000)
        self.assertAlmostEqual(useful, tone_amplitude(alone, 1000), delta=0.015)
        self.assertLess(tone_amplitude(mixed, 3200) / useful, 0.01)

    def test_tuning_offset_dc_is_removed(self):
        pcm = WbfmDemodulator().process(fm_tone(amplitude=0.3, seconds=0.25, offset_hz=12_000))
        self.assertLess(abs(float(np.mean(pcm[4800:]))), 0.0005)


def benchmark(seconds=3.0, chunk_size=65_536):
    """Report wall/CPU time for pre-generated continuous IQ (no I/O)."""
    iq = fm_tone(seconds=seconds)
    decoder = WbfmDemodulator()
    start_wall, start_cpu = time.perf_counter(), time.process_time()
    output_samples = 0
    for start in range(0, iq.size, chunk_size):
        output_samples += decoder.process(iq[start:start + chunk_size]).size
    cpu, wall = time.process_time() - start_cpu, time.perf_counter() - start_wall
    print(f"DSP benchmark: {seconds:g}s IQ, chunk={chunk_size}, output={output_samples}; "
          f"wall={wall:.3f}s, CPU={cpu:.3f}s, throughput={iq.size / wall / 1e6:.2f} MS/s "
          f"({seconds / wall:.2f}x real time), CPU/audio-second={cpu / seconds:.3f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--benchmark", action="store_true")
    args, remaining = parser.parse_known_args()
    if args.benchmark:
        benchmark()
    else:
        unittest.main(argv=[__file__, *remaining])
