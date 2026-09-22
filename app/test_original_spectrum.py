"""Isolated synthetic fixtures; never open a receiver or change a wallpaper."""

import json
import math
import unittest

import numpy as np

from original_spectrum import SpectrumProcessor


class SpectrumProcessorTests(unittest.TestCase):
    RATE = 2_400_000
    CENTER = 100_000_000
    N = 32768

    def setUp(self):
        self.rng = np.random.default_rng(410)
        self.processor = SpectrumProcessor()
        self.time_axis = np.arange(self.N) / self.RATE

    def noise(self, strength=0.025):
        return strength * (self.rng.standard_normal(self.N) + 1j * self.rng.standard_normal(self.N))

    def frame(self, iq, timestamp=0.0, center=None):
        return self.processor.process(iq, self.RATE, self.CENTER if center is None else center, timestamp)

    def test_white_noise_is_quiet_and_not_a_station(self):
        for step in range(8):
            result = self.frame(self.noise(), step * 0.1)
            self.assertLess(np.mean(result["levels"]), 0.08)
            self.assertLess(max(result["levels"]), 0.25)
            self.assertIsNone(result["peak_hz"])
            self.assertEqual(result["peak_db_above_noise"], 0)

    def test_narrow_carrier_survives_reduction_and_has_correct_frequency(self):
        offset = 231 * self.RATE / self.N
        iq = self.noise() + 0.1 * np.exp(2j * np.pi * offset * self.time_axis)
        result = self.frame(iq)
        self.assertAlmostEqual(result["peak_hz"], self.CENTER + offset, delta=self.RATE / self.N)
        cell = int((offset + self.RATE * 0.46) / (self.RATE * 0.92) * 256)
        self.assertGreater(result["levels"][cell], 0.7)
        self.assertGreater(result["peak_db_above_noise"], 35)

    def test_dc_and_out_of_passband_carriers_are_rejected(self):
        iq = self.noise() + (3 + 4j)
        iq += np.exp(2j * np.pi * (self.RATE * 0.48) * self.time_axis)
        result = self.frame(iq)
        self.assertIsNone(result["peak_hz"])
        self.assertLess(max(result["levels"]), 0.25)

    def test_tuning_change_resets_smoothing_and_accepts_new_clock(self):
        carrier = self.noise() + np.exp(2j * np.pi * 200_000 * self.time_axis)
        self.frame(carrier, timestamp=20)
        noise = self.noise()
        retuned = self.frame(noise, timestamp=1, center=self.CENTER + 500_000)
        fresh = SpectrumProcessor().process(noise, self.RATE, self.CENTER + 500_000, 1)
        self.assertEqual(retuned, fresh)

    def test_levels_are_bounded_json_safe_and_output_is_not_shared(self):
        fixtures = [np.zeros(self.N, dtype=complex), self.noise(), self.noise() * 1e290,
                    np.full(self.N, 1e-320 + 1e-320j)]
        for step, iq in enumerate(fixtures):
            result = self.frame(iq, timestamp=step)
            self.assertEqual(len(result["levels"]), 256)
            self.assertTrue(all(math.isfinite(v) and 0 <= v <= 1 for v in result["levels"]))
            json.dumps(result, allow_nan=False)
            result["levels"][0] = 900
            duplicate = self.frame(iq, timestamp=step)
            self.assertLessEqual(duplicate["levels"][0], 1)

    def test_attack_and_release_are_time_based(self):
        noise = self.noise()
        carrier = noise + 0.1 * np.exp(2j * np.pi * 200_000 * self.time_axis)
        quiet = self.frame(noise, 0)
        rising = self.frame(carrier, 0.05)
        settled = self.frame(carrier, 2)
        falling = self.frame(noise, 2.05)
        position = int(np.argmax(settled["levels"]))
        self.assertGreater(rising["levels"][position], quiet["levels"][position])
        self.assertLess(rising["levels"][position], settled["levels"][position])
        self.assertLess(falling["levels"][position], settled["levels"][position])
        self.assertGreater(falling["levels"][position], settled["levels"][position] * 0.85)

    def test_peak_hysteresis_does_not_jump_between_nearly_equal_stations(self):
        a = np.exp(2j * np.pi * (1500 * self.RATE / self.N) * self.time_axis)
        b = np.exp(2j * np.pi * (-2700 * self.RATE / self.N) * self.time_axis)
        first = self.frame(self.noise() + 0.12 * a + 0.1 * b, 0)
        next_frame = self.frame(self.noise() + 0.1 * a + 0.12 * b, 0.1)
        stronger = self.frame(self.noise() + 0.04 * a + 0.2 * b, 0.2)
        self.assertEqual(first["peak_hz"], next_frame["peak_hz"])
        self.assertNotEqual(first["peak_hz"], stronger["peak_hz"])

    def test_invalid_input_and_nonmonotonic_time_do_not_corrupt_state(self):
        valid = self.noise()
        expected = self.frame(valid, 10)
        invalid = [np.zeros(63, dtype=complex), np.zeros((128, 2), dtype=complex), np.zeros(128),
                   np.full(128, complex(float("nan"), 0)), np.full(128, complex(float("inf"), 0)), None]
        for iq in invalid:
            with self.subTest(iq_type=type(iq)), self.assertRaises(ValueError):
                self.frame(iq, 11)
        for metadata in [(0, self.CENTER, 11), (float("inf"), self.CENTER, 11),
                         (self.RATE, -1, 11), (self.RATE, self.CENTER, float("nan"))]:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.processor.process(valid, *metadata)
        self.assertEqual(self.frame(valid * 2, 9), expected)
        self.assertEqual(self.frame(valid * 2, 10), expected)
        for bins in [0, 7, 4097, 256.5, True]:
            with self.subTest(bins=bins), self.assertRaises(ValueError):
                SpectrumProcessor(bins)


if __name__ == "__main__":
    unittest.main()
