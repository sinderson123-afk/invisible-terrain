"""Hardware-independent, conservative broadcast-FM *candidate* detection.

This is a spectral heuristic, not an FM decoder, station directory, or reception
quality guarantee. A silent/narrowly modulated or weak station can be missed;
other sufficiently broad signals can be returned. The caller owns tuning,
settling, cancellation and all SDR access. It normally supplies three contiguous
32768-sample blocks at 2.4 MS/s per window.
"""

from __future__ import annotations

import math

import numpy as np


MIN_FREQUENCY_HZ = 87_500_000
MAX_FREQUENCY_HZ = 108_000_000
GRID_HZ = 100_000
MAX_CANDIDATES = 64


def scan_centers(sample_rate=2_400_000) -> list[int]:
    """13 overlapping 2.4-MS/s windows covering the entire supported FM band.

    Centers lie halfway between station-grid points. The overlap lets another
    window observe spectrum removed around a receiver's DC spur or band edge.
    """
    rate = _finite_number(sample_rate, "sample_rate")
    if not 1_800_000 <= rate <= 10_000_000:
        raise ValueError("scan sample rate must be between 1800000 and 10000000 Hz")
    if rate == 2_400_000:
        return [88_250_000 + 1_600_000 * index for index in range(13)]
    # Conservative overlap, keeping complete 180 kHz FM channels away from
    # the filter edges. Also bound window width to preserve weak-signal FFT
    # resolution across fast devices, rather than taking enormous strides.
    step = min(1_600_000, int((rate * 0.72 - 200_000) // GRID_HZ) * GRID_HZ)
    return list(range(88_050_000, 108_000_000, step)) + [107_950_000]


def _finite_number(value, label):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def merge_candidates(existing, new) -> list[dict]:
    """Merge without mutating inputs; strongest duplicate wins, at most 64.

    Adjacent 100-kHz reports are also suppressed because they commonly refer to
    opposite shoulders of one wide signal. Separate 200-kHz channels survive.
    Results are frequency-ordered after the strongest 64 have been selected.
    """
    by_frequency = {}
    for item in list(existing) + list(new):
        if not isinstance(item, dict):
            raise ValueError("candidate must be a dictionary")
        try:
            frequency = _finite_number(item["frequency_hz"], "frequency_hz")
            score = _finite_number(item["score_db"], "score_db")
            bandwidth = _finite_number(item["bandwidth_hz"], "bandwidth_hz")
        except KeyError as exc:
            raise ValueError("candidate fields are missing") from exc
        if (frequency != int(frequency) or int(frequency) % GRID_HZ
                or not MIN_FREQUENCY_HZ <= frequency <= MAX_FREQUENCY_HZ):
            raise ValueError("candidate frequency must be on the supported FM grid")
        if not 0 < bandwidth <= 2 * GRID_HZ:
            raise ValueError("candidate bandwidth must be between 0 and 200000 Hz")
        candidate = {"frequency_hz": int(frequency),
                     "score_db": float(score), "bandwidth_hz": int(bandwidth)}
        previous = by_frequency.get(int(frequency))
        if previous is None or score > previous["score_db"]:
            by_frequency[int(frequency)] = candidate
    selected = []
    for candidate in sorted(by_frequency.values(),
                            key=lambda item: (-item["score_db"], item["frequency_hz"])):
        if all(abs(candidate["frequency_hz"] - other["frequency_hz"]) >= 2 * GRID_HZ
               for other in selected):
            selected.append(candidate)
            if len(selected) == MAX_CANDIDATES:
                break
    return sorted(selected, key=lambda item: item["frequency_hz"])


class StationScanner:
    """Average short FFTs, then test width and excess energy at 100-kHz steps.

    A candidate must have at least 6 dB mean channel power above a robust local
    and window noise estimate. Width, effective spectral width and centrality
    tests reject isolated CW/spurs and neighboring-channel shoulders. No state
    is accumulated between analyze() calls; merge_candidates() combines windows.
    """

    FFT_SIZE = 4096
    MIN_SCORE_DB = 6.0
    HALF_CHANNEL_HZ = 90_000
    DC_GUARD_HZ = 12_000

    def analyze(self, iq, sample_rate, center_hz) -> list[dict]:
        rate = _finite_number(sample_rate, "sample_rate")
        center = _finite_number(center_hz, "center_hz")
        if not 400_000 <= rate <= 10_000_000:
            raise ValueError("sample_rate must be between 400000 and 10000000 Hz")
        if center <= 0 or center != int(center):
            raise ValueError("center_hz must be a positive integer frequency")
        try:
            samples = np.asarray(iq)
            if samples.ndim != 1 or (samples.size and not np.iscomplexobj(samples)):
                raise ValueError("IQ must be one-dimensional complex samples")
            with np.errstate(over="ignore", invalid="ignore"):
                samples = np.asarray(samples, dtype=np.complex64)
            if not np.isfinite(samples).all():
                raise ValueError("IQ must contain only finite complex64 samples")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("IQ must be finite one-dimensional complex samples") from exc
        if samples.size < self.FFT_SIZE:
            return []
        scale = max(float(np.max(np.abs(samples.real))),
                    float(np.max(np.abs(samples.imag))))
        if not scale:
            return []
        # Scaling does not change power ratios and avoids squaring huge inputs.
        samples = samples / scale
        hop = self.FFT_SIZE // 2
        frames = np.lib.stride_tricks.sliding_window_view(samples, self.FFT_SIZE)[::hop]
        windowed = frames * np.hanning(self.FFT_SIZE)
        spectrum = np.fft.fftshift(np.fft.fft(windowed, axis=1), axes=1)
        power = np.mean(spectrum.real ** 2 + spectrum.imag ** 2, axis=0)
        offsets = np.fft.fftshift(np.fft.fftfreq(self.FFT_SIZE, 1.0 / rate))
        bin_hz = rate / self.FFT_SIZE
        # Never inspect roll-off at the outer 150 kHz of a 2.4-MS/s window.
        usable_edge = min(1_050_000.0, rate * 0.44)
        usable = ((np.abs(offsets) < usable_edge)
                  & (np.abs(offsets) >= self.DC_GUARD_HZ))
        if not np.any(usable):
            return []
        # A perfectly noiseless tone/constant has only floating-point and window
        # leakage away from its line. Do not promote that numerical residue to
        # stations by dividing it by an almost-zero percentile. This bound is
        # 100 dB below the strongest FFT bin, well beneath an 8-bit SDR's useful
        # dynamic range, and also includes a DC spur removed from usable bins.
        floor = max(float(np.percentile(power[usable], 30)),
                    float(np.max(power)) * 1e-10, np.finfo(float).tiny)
        max_offset = min(950_000.0, usable_edge - self.HALF_CHANNEL_HZ)
        lower = max(MIN_FREQUENCY_HZ, center - max_offset)
        upper = min(MAX_FREQUENCY_HZ, center + max_offset)
        first = int(math.ceil(lower / GRID_HZ)) * GRID_HZ
        candidates = []
        for frequency in range(first, int(upper) + 1, GRID_HZ):
            relative = offsets - (frequency - center)
            channel = usable & (np.abs(relative) <= self.HALF_CHANNEL_HZ)
            if np.count_nonzero(channel) < 16:
                continue
            # A lower percentile leaves adjacent real stations out of the
            # floor estimate while following a sloping receiver noise floor.
            surroundings = usable & (np.abs(relative) >= 130_000) & (np.abs(relative) <= 350_000)
            local_floor = (float(np.percentile(power[surroundings], 30))
                           if np.any(surroundings) else floor)
            noise = max(floor, local_floor)
            band_power = power[channel]
            score = 10.0 * math.log10(max(float(np.mean(band_power)) / noise, 1e-300))
            if score < self.MIN_SCORE_DB:
                continue
            excess = np.maximum(band_power - noise, 0.0)
            total = float(np.sum(excess))
            if total <= 0:
                continue
            channel_offsets = relative[channel]
            # Effective width catches strong narrow tones even when Hann-window
            # leakage makes their above-noise footprint appear superficially wide.
            effective_width = total ** 2 / float(np.dot(excess, excess)) * bin_hz
            if effective_width < 10_000:
                continue
            occupied_bins = np.count_nonzero(band_power >= 3.0 * noise)
            if occupied_bins * bin_hz < 20_000:
                continue
            cumulative = np.cumsum(excess) / total
            low = int(np.searchsorted(cumulative, 0.05))
            high = min(int(np.searchsorted(cumulative, 0.95)), channel_offsets.size - 1)
            bandwidth = float(channel_offsets[high] - channel_offsets[low] + bin_hz)
            if bandwidth < 30_000:
                continue
            centroid = float(np.dot(channel_offsets, excess) / total)
            if abs(centroid) > 45_000:
                continue
            candidates.append({"frequency_hz": int(frequency),
                               "score_db": round(score, 2),
                               "bandwidth_hz": int(round(bandwidth))})
        return merge_candidates([], candidates)
