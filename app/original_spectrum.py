"""Measured IQ -> a calm, frequency-addressable spectrum for the original V2.

No acquisition, UI, Wallpaper Engine control, or synthetic signal generation
belongs here. Levels are *relative spectral contrast*, not calibrated field
strength, and their display gate intentionally hides ordinary FFT noise peaks.
"""

from __future__ import annotations

import math

import numpy as np


class SpectrumProcessor:
    """Process finite one-dimensional complex IQ arrays of at least 64 samples.

    ``levels`` run from ``low_hz`` to ``high_hz`` in equal-width cells. Each
    cell retains its strongest FFT bin so narrow carriers survive reduction.
    A small center/DC rejection region is left quiet rather than inventing
    received content. ``peak_hz`` is None when no convincing peak is measured.

    Invalid input raises ValueError without altering state. On unchanged
    tuning, a duplicate/backwards timestamp returns a copy of the last frame.
    Changing center frequency, sample rate, or FFT length resets all filters.
    """

    ATTACK_SECONDS = 0.15
    RELEASE_SECONDS = 0.8
    NOISE_SECONDS = 0.7
    DISPLAY_GATE_DB = 10.0
    DISPLAY_RANGE_DB = 38.0
    PEAK_GATE_DB = 16.5
    PEAK_HYSTERESIS_DB = 3.0

    def __init__(self, bins: int = 256):
        if isinstance(bins, bool) or not isinstance(bins, (int, np.integer)) or not 8 <= bins <= 4096:
            raise ValueError("bins must be an integer between 8 and 4096")
        self.bins = int(bins)
        self._tuning: tuple[float, float, int] | None = None
        self._last: dict | None = None
        self._levels: np.ndarray | None = None
        self._noise: float | None = None
        self._window: np.ndarray | None = None

    @staticmethod
    def _copy(frame: dict) -> dict:
        return {**frame, "levels": list(frame["levels"])}

    def process(self, iq, sample_rate, frequency, timestamp) -> dict:
        """Return a JSON-safe frame; all frequencies and timestamps use SI units.

        Noise is the 40th percentile of Hann-windowed FFT-bin power within
        the usable passband, exponentially smoothed in time. The power scale
        uses coherent Hann normalization; it is receiver-dependent, not dBm.
        """
        try:
            samples = np.asarray(iq)
            rate, center, now = float(sample_rate), float(frequency), float(timestamp)
            if (
                samples.ndim != 1
                or samples.size < 64
                or not np.iscomplexobj(samples)
                or not np.isfinite(samples).all()
                or not math.isfinite(rate)
                or rate <= 0.0
                or rate / samples.size <= 0.0
                or not math.isfinite(center)
                or center < 0.0
                or not math.isfinite(now)
                or not math.isfinite(center + rate * 0.46)
            ):
                raise ValueError("invalid IQ or metadata")
            # Convert before computing; integer/object and malformed inputs
            # must fail without changing a valid previous processing state.
            samples = np.asarray(samples, dtype=np.complex128)
            if not np.isfinite(samples).all():
                raise ValueError("IQ exceeds the working numeric range")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("expected finite complex IQ and valid frequency/rate/time") from exc

        tuning = (center, rate, samples.size)
        reset = tuning != self._tuning
        if not reset and self._last is not None and now <= self._last["timestamp"]:
            return self._copy(self._last)

        window = np.hanning(samples.size) if reset or self._window is None else self._window
        # Scaling first keeps even finite, unusually large input from
        # overflowing mean/FFT. The offset restores its original dB scale.
        scale = max(float(np.max(np.abs(samples.real))), float(np.max(np.abs(samples.imag))))
        scale = scale if scale > 0.0 else 1.0
        # Component-wise division also handles subnormal input: NumPy's
        # complex-by-real division can overflow its reciprocal internally.
        normalized = samples.real / scale + 1j * (samples.imag / scale)
        normalized = normalized - np.mean(normalized)
        spectrum = np.fft.fftshift(np.fft.fft(normalized * window)) / float(window.sum())
        amplitude = np.maximum(np.abs(spectrum), 1e-150)
        power_db = 20.0 * np.log10(amplitude) + 20.0 * math.log10(scale)
        power_db = np.clip(power_db, -3000.0, 3000.0)
        fractional_offsets = np.fft.fftshift(np.fft.fftfreq(samples.size))
        offsets = fractional_offsets * rate
        fft_width = rate / samples.size
        # Reject a few bins even at coarse FFT resolutions, while avoiding a
        # fixed-width hole that would eat a narrow receiver passband.
        dc_width = max(2.0 * fft_width, min(2500.0, rate * 0.005))
        usable = (np.abs(offsets) < rate * 0.46) & (np.abs(offsets) >= dc_width)
        current_noise = float(np.percentile(power_db[usable], 40.0))
        elapsed = 0.0 if reset or self._last is None else now - self._last["timestamp"]
        noise = current_noise if reset or self._noise is None else (
            self._noise + (-math.expm1(-elapsed / self.NOISE_SECONDS)) * (current_noise - self._noise)
        )

        low, high = center - rate * 0.46, center + rate * 0.46
        indices = np.floor((fractional_offsets[usable] + 0.46) / 0.92 * self.bins).astype(int)
        indices = np.clip(indices, 0, self.bins - 1)
        cells = np.full(self.bins, -np.inf)
        np.maximum.at(cells, indices, power_db[usable])
        target = np.clip((cells - noise - self.DISPLAY_GATE_DB) / self.DISPLAY_RANGE_DB, 0.0, 1.0)
        if reset or self._levels is None:
            levels = target
        else:
            tau = np.where(target > self._levels, self.ATTACK_SECONDS, self.RELEASE_SECONDS)
            alpha = -np.expm1(-elapsed / tau)
            levels = self._levels + alpha * (target - self._levels)

        usable_indices = np.flatnonzero(usable)
        strongest = int(usable_indices[np.argmax(power_db[usable])])
        chosen = strongest
        # Retain an existing plausible peak if two stations trade places by
        # only a few dB. Tracking follows measured neighboring bins, not a
        # smoothed frequency that could point between actual stations.
        previous_peak = None if reset or self._last is None else self._last["peak_hz"]
        if previous_peak is not None:
            previous_index = int(round((previous_peak - center) / fft_width)) + samples.size // 2
            nearby = np.arange(max(0, previous_index - 2), min(samples.size, previous_index + 3))
            nearby = nearby[usable[nearby]]
            if nearby.size:
                candidate = int(nearby[np.argmax(power_db[nearby])])
                if (
                    power_db[candidate] >= power_db[strongest] - self.PEAK_HYSTERESIS_DB
                    and power_db[candidate] - current_noise >= self.PEAK_GATE_DB
                ):
                    chosen = candidate
        # Detection uses the current floor so sudden gain/noise changes do
        # not report a phantom station while the display floor catches up.
        peak_contrast = float(max(0.0, power_db[chosen] - current_noise))
        credible_peak = peak_contrast >= self.PEAK_GATE_DB
        frame = {
            "frequency_hz": center,
            "sample_rate_hz": rate,
            "low_hz": low,
            "high_hz": high,
            "levels": np.clip(levels, 0.0, 1.0).tolist(),
            "noise_floor_db": float(noise),
            "peak_hz": float(center + offsets[chosen]) if credible_peak else None,
            "peak_db_above_noise": peak_contrast if credible_peak else 0.0,
            "timestamp": now,
        }
        self._tuning, self._window = tuning, window
        self._levels, self._noise, self._last = levels, float(noise), frame
        return self._copy(frame)
