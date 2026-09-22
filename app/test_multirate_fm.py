"""Synthetic multirate DSP checks; no receiver or playback is opened."""
import math
import unittest

import numpy as np
from scipy import signal

from original_fm import WbfmDemodulator, _RationalChannel
from test_original_fm import tone_amplitude


class MultirateTests(unittest.TestCase):
    def test_resampler_matches_one_shot_causal_reference(self):
        rng = np.random.default_rng(402)
        samples = (rng.normal(size=4321) + 1j * rng.normal(size=4321)).astype(np.complex64)
        for rate in (1_800_000, 2_000_000, 2_048_000, 2_500_000, 3_000_000, 6_000_000, 10_000_000):
            with self.subTest(rate=rate):
                converter = _RationalChannel(rate)
                count = math.ceil(len(samples) * 240_000 / rate)
                expected = signal.upfirdn(converter.taps, samples, up=converter.up, down=converter.down)[:count]
                parts, start = [], 0
                while start < len(samples):
                    stop = min(len(samples), start + int(rng.integers(1, 111)))
                    parts.append(converter.process(samples[start:stop]))
                    start = stop
                np.testing.assert_allclose(np.concatenate(parts), expected, atol=3e-7)
                converter.reset()
                np.testing.assert_allclose(converter.process(samples), expected, atol=3e-7)

    def test_fm_pitch_volume_and_chunk_continuity_at_common_rates(self):
        for rate in (1_800_000, 2_000_000, 2_048_000, 2_500_000, 3_000_000, 6_000_000, 10_000_000):
            with self.subTest(rate=rate):
                t = np.arange(round(rate * .12)) / rate
                iq = np.exp(1j * 45 * np.sin(2 * np.pi * 1000 * t)).astype(np.complex64)
                whole = WbfmDemodulator(input_rate=rate).process(iq)
                self.assertEqual(whole.size, 5760)
                self.assertAlmostEqual(tone_amplitude(whole, 1000), .6 / math.sqrt(1 + (2 * math.pi * .05) ** 2), delta=.02)
                decoder = WbfmDemodulator(input_rate=rate)
                actual = np.concatenate([decoder.process(iq[i:i + 8189]) for i in range(0, iq.size, 8189)])
                np.testing.assert_allclose(actual, whole, atol=2e-6)

    def test_impractical_or_fractional_rates_fail_explicitly(self):
        for rate in (2_000_000.5, float('inf'), 2_400_001, 1_000_000, 11_000_000):
            with self.assertRaises(ValueError):
                WbfmDemodulator(input_rate=rate)

    def test_high_rate_prefilter_rejects_folded_and_adjacent_stations(self):
        rate = 10_000_000
        t = np.arange(round(rate * .10)) / rate
        desired = np.exp(1j * 22.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.complex64)
        reference = tone_amplitude(WbfmDemodulator(input_rate=rate).process(desired), 1000)
        for offset in (300_000, 1_910_000, 2_000_000, 2_090_000, 4_000_000, -2_000_000):
            other = np.exp(1j * (2 * np.pi * offset * t + 9 * np.sin(2 * np.pi * 3200 * t))).astype(np.complex64)
            pcm = WbfmDemodulator(input_rate=rate).process(desired + 10 * other)
            self.assertAlmostEqual(tone_amplitude(pcm, 1000), reference, delta=.01)
            self.assertLess(tone_amplitude(pcm, 3200) / reference, .005)


if __name__ == '__main__':
    unittest.main()
