"""Deterministic, hardware-free checks of conservative FM candidate detection."""

import copy
import unittest

import numpy as np
from scipy import signal

from original_station_scan import (
    MAX_CANDIDATES, StationScanner, merge_candidates, scan_centers,
)


RATE = 2_400_000
COUNT = 3 * 32768


def noise(seed=17, amplitude=0.025):
    rng = np.random.default_rng(seed)
    return amplitude * (rng.standard_normal(COUNT) + 1j * rng.standard_normal(COUNT))


def fm(offset_hz, amplitude=0.6, seed=41):
    """FM of band-limited program-like random audio, not a broad noise impostor."""
    rng = np.random.default_rng(seed)
    audio_rate = 48_000
    audio = rng.standard_normal(int(np.ceil(COUNT / 50)) + 600)
    audio = signal.lfilter(signal.firwin(129, 12_000, fs=audio_rate), [1.0], audio)
    audio = audio[256:]
    audio /= np.std(audio)
    modulation = signal.resample_poly(audio, 50, 1)[:COUNT]
    instantaneous = offset_hz + 32_000 * np.clip(modulation, -2.4, 2.4)
    return amplitude * np.exp(2j * np.pi * np.cumsum(instantaneous) / RATE)


def cw(offset_hz, amplitude=0.6):
    return amplitude * np.exp(2j * np.pi * offset_hz * np.arange(COUNT) / RATE)


def candidate(frequency, score=12, width=120000):
    return {"frequency_hz": frequency, "score_db": score, "bandwidth_hz": width}


class StationScanTests(unittest.TestCase):
    def setUp(self):
        self.scanner = StationScanner()

    def frequencies(self, samples, center=93_850_000):
        return [item["frequency_hz"] for item in self.scanner.analyze(samples, RATE, center)]

    def test_centers_cover_band_and_avoid_station_grid(self):
        centers = scan_centers()
        self.assertEqual(len(centers), 13)
        self.assertTrue(all(center % 100_000 == 50_000 for center in centers))
        self.assertEqual(set(np.diff(centers)), {1_600_000})
        for frequency in range(87_500_000, 108_000_001, 100_000):
            self.assertTrue(any(abs(frequency - center) <= 950_000 for center in centers))

    def test_broad_fm_detected_and_quantized(self):
        result = self.scanner.analyze(fm(-447_000) + noise(), RATE, 93_850_000)
        self.assertEqual([item["frequency_hz"] for item in result], [93_400_000])
        self.assertGreater(result[0]["score_db"], 6)
        self.assertGreater(result[0]["bandwidth_hz"], 30_000)

    def test_white_noise_not_reported(self):
        for seed in range(5):
            self.assertEqual(self.frequencies(noise(seed=seed, amplitude=1)), [])

    def test_cw_and_very_strong_cw_not_reported(self):
        for offset in (-450_000, -430_500, 52_337):
            self.assertEqual(self.frequencies(cw(offset) + noise(amplitude=0.0001)), [])

    def test_noiseless_cw_or_dc_numerical_residue_is_not_a_station(self):
        for offset in (-450_000, -430_500, 52_337, 0):
            self.assertEqual(self.frequencies(cw(offset)), [])

    def test_narrow_fm_and_two_narrow_lines_not_reported(self):
        time = np.arange(COUNT) / RATE
        narrow_fm = np.exp(2j * np.pi * (-450_000 * time
                          + 2_000 / (2 * np.pi * 1_000) * np.sin(2 * np.pi * 1_000 * time)))
        self.assertEqual(self.frequencies(narrow_fm + noise()), [])
        self.assertEqual(self.frequencies(cw(-480_000) + cw(-420_000) + noise()), [])

    def test_wide_fm_tone_modulation_is_accepted(self):
        time = np.arange(COUNT) / RATE
        for tone_hz in (1_000, 3_000, 7_000):
            phase = 2 * np.pi * (-450_000 * time + 65_000 / (2 * np.pi * tone_hz)
                                 * np.sin(2 * np.pi * tone_hz * time))
            self.assertEqual(self.frequencies(0.6 * np.exp(1j * phase) + noise()), [93_400_000])

    def test_dc_and_zero_not_reported(self):
        self.assertEqual(self.frequencies(np.ones(COUNT, dtype=np.complex64) + noise()), [])
        self.assertEqual(self.frequencies(np.zeros(COUNT, dtype=np.complex64)), [])

    def test_station_near_dc_still_detected(self):
        result = self.frequencies(fm(-50_000) + 2 + noise())
        self.assertEqual(result, [93_800_000])

    def test_edges_are_excluded_but_overlap_recovers_station(self):
        # 89.4 MHz is too close to the edge of the first window, but safely
        # inside the following window. It must not be invented at 89.2 MHz.
        self.assertEqual(self.frequencies(fm(1_150_000) + noise(), 88_250_000), [])
        self.assertEqual(self.frequencies(fm(-450_000) + noise(), 89_850_000), [89_400_000])

    def test_band_endpoints_and_outside_band(self):
        self.assertEqual(self.frequencies(fm(-750_000) + noise(), 88_250_000), [87_500_000])
        self.assertEqual(self.frequencies(fm(550_000) + noise(), 107_450_000), [108_000_000])
        self.assertEqual(self.frequencies(fm(-950_000) + noise(), 88_250_000), [])

    def test_two_adjacent_200khz_stations_survive(self):
        samples = fm(-450_000, seed=41) + fm(-250_000, amplitude=0.4, seed=71) + noise()
        self.assertEqual(self.frequencies(samples), [93_400_000, 93_600_000])

    def test_several_stations_without_shoulder_duplicates(self):
        samples = (fm(-750_000) + fm(-450_000, amplitude=0.4, seed=71)
                   + fm(650_000, amplitude=0.5, seed=89) + noise())
        self.assertEqual(self.frequencies(samples), [93_100_000, 93_400_000, 94_500_000])

    def test_input_validation_and_short_empty_buffers(self):
        for invalid in ([1, 2, 3], [[1j]], [complex(float("nan"), 0)],
                        [complex(0, float("inf"))], [complex(1e100, 0)]):
            with self.assertRaises(ValueError):
                self.scanner.analyze(invalid, RATE, 93_850_000)
        self.assertEqual(self.scanner.analyze([], RATE, 93_850_000), [])
        self.assertEqual(self.scanner.analyze(np.ones(100, complex), RATE, 93_850_000), [])
        for bad in (True, float("nan"), float("inf"), 0, 100_000):
            with self.assertRaises(ValueError):
                self.scanner.analyze([], bad, 93_850_000)
        for bad in (True, float("nan"), float("inf"), -1, 93_850_000.1):
            with self.assertRaises(ValueError):
                self.scanner.analyze([], RATE, bad)

    def test_calls_do_not_share_state(self):
        samples = fm(-450_000) + noise()
        first = self.scanner.analyze(samples, RATE, 93_850_000)
        self.scanner.analyze(noise(), RATE, 100_050_000)
        self.assertEqual(first, self.scanner.analyze(samples, RATE, 93_850_000))


class MergeTests(unittest.TestCase):
    def test_duplicate_strongest_wins_and_inputs_untouched(self):
        old = [candidate(93_400_000, 10)]
        new = [candidate(93_400_000, 15), candidate(94_000_000, 8)]
        saved = copy.deepcopy((old, new))
        self.assertEqual(merge_candidates(old, new), [new[0], new[1]])
        self.assertEqual((old, new), saved)

    def test_suppresses_100khz_shoulders_preserves_200khz(self):
        result = merge_candidates([], [candidate(93_300_000, 10), candidate(93_400_000, 20),
                                       candidate(93_500_000, 9), candidate(93_600_000, 15)])
        self.assertEqual([item["frequency_hz"] for item in result], [93_400_000, 93_600_000])

    def test_limit_retains_strongest_and_returns_frequency_order(self):
        candidates = [candidate(frequency, index) for index, frequency
                      in enumerate(range(87_500_000, 108_000_001, 200_000))]
        result = merge_candidates([], candidates)
        self.assertEqual(len(result), MAX_CANDIDATES)
        self.assertEqual(result, candidates[-MAX_CANDIDATES:])

    def test_invalid_candidates_rejected(self):
        for invalid in (None, {}, candidate(93_450_000), candidate(1),
                        candidate(93_400_000, float("nan")), candidate(93_400_000, width=0)):
            with self.assertRaises(ValueError):
                merge_candidates([], [invalid])


if __name__ == "__main__":
    unittest.main()
