"""Streaming, hardware-independent broadcast-FM mono demodulation.

The receiver must be centered on the station: this module consumes its *full,
continuous* normalized complex IQ stream, not the sparse samples used by a
spectrum display. No receiver, playback device, networking or thread is owned
here. Call ``reset`` after a retune or a dropped IQ block.

All filter delay lines and decimation phases survive arbitrary input chunk
boundaries. The FIR decimators use SciPy's polyphase ``upfirdn`` implementation;
they do not compute the discarded high-rate filter outputs.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal


class _FirDecimator:
    """Causal FIR followed by samples 0, factor, 2*factor, ... of the stream."""

    def __init__(self, taps: np.ndarray, factor: int, dtype):
        self.taps = np.asarray(taps, dtype=np.float32)
        self.factor = factor
        self.dtype = np.dtype(dtype)
        self.reset()

    def reset(self):
        self._history = np.zeros(self.taps.size - 1, dtype=self.dtype)
        self._phase = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        count = samples.size
        if not count:
            return np.empty(0, dtype=self.dtype)
        history_size = self._history.size
        # Put the old samples at a phase-correct position in upfirdn's local
        # stream. Leading padding does not reach any output that we retain.
        padding = (self._phase - history_size) % self.factor
        extended = np.empty(padding + history_size + count, dtype=self.dtype)
        extended[:padding] = 0
        extended[padding:padding + history_size] = self._history
        extended[padding + history_size:] = samples
        first_input = (-self._phase) % self.factor
        output_count = max(0, (count - 1 - first_input) // self.factor + 1)
        first_output = (padding + history_size + first_input) // self.factor
        filtered = signal.upfirdn(self.taps, extended, down=self.factor)
        result = filtered[first_output:first_output + output_count].copy()
        self._history = extended[-history_size:].copy()
        self._phase = (self._phase + count) % self.factor
        return result


class _RationalChannel:
    """Streaming causal rational FIR resampling, with no per-chunk tail flush.

    Retain only input samples needed by the next output. Absolute input/output
    clocks select the same samples as a single upfirdn call, even for one-sample
    chunks. Padding aligns a local convolution with the global downsample grid.
    """

    def __init__(self, input_rate, output_rate=240_000):
        divisor = math.gcd(input_rate, output_rate)
        self.up, self.down = output_rate // divisor, input_rate // divisor
        if self.up > 240 or self.down > 10_000:
            raise ValueError("FM sample rate must have a bounded rational ratio to 240000 Hz (use an integer-kHz device rate)")
        count = int(math.ceil(321 * input_rate * self.up / 2_400_000)) | 1
        self.taps = (signal.firwin(count, 105_000, fs=input_rate * self.up,
                                  window=("kaiser", 7.5)) * self.up).astype(np.float32)
        self.history_size = (count - 1 + self.up - 1) // self.up
        self.reset()

    def reset(self):
        self.total = 0
        self.history = np.zeros(self.history_size, dtype=np.complex64)

    def process(self, samples):
        if not samples.size:
            return np.empty(0, dtype=np.complex64)
        begin, end = self.total, self.total + samples.size
        first = (begin * self.up + self.down - 1) // self.down
        last = (end * self.up + self.down - 1) // self.down
        padding = (begin - self.history_size) % self.down
        extended = np.concatenate((np.zeros(padding, dtype=np.complex64), self.history, samples))
        base = (begin - self.history_size - padding) * self.up // self.down
        filtered = signal.upfirdn(self.taps, extended, up=self.up, down=self.down)
        result = filtered[first - base:last - base].copy()
        self.history = extended[-self.history_size:].copy()
        self.total = end
        return result


class WbfmDemodulator:
    """1.8–10 MS/s centered complex IQ -> 48 kHz mono float32 PCM.

    A 75 kHz instantaneous frequency deviation corresponds to PCM amplitude
    one *before* audio filtering/de-emphasis. There is no automatic gain,
    squelch, volume or clipping here: those are playback policy, not decoding.
    China/Europe use 50 microsecond de-emphasis (the default); 75 microseconds
    can be explicitly selected for countries using that broadcast standard.

    Input must be integer Hz with a bounded rational ratio (integer-kHz rates
    satisfy this); output is 48 kHz. The original 2.4 MS/s path is preserved.
    FIR startup
    transients are intentional; the playback layer should fade in after a
    reset. Empty chunks are harmless and invalid input leaves state unchanged.
    """

    CHANNEL_RATE = 240_000
    DEVIATION_HZ = 75_000.0

    def __init__(self, input_rate=2_400_000, audio_rate=48_000,
                 deemphasis_us=50.0):
        if (isinstance(input_rate, bool) or not isinstance(input_rate, (int, float))
                or not math.isfinite(input_rate) or input_rate != int(input_rate)
                or not 1_800_000 <= input_rate <= 10_000_000
                or isinstance(audio_rate, bool) or audio_rate != 48_000):
            raise ValueError("WBFM needs integer 1800000–10000000 Hz IQ and 48000 Hz audio")
        try:
            tau = float(deemphasis_us) * 1e-6
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("deemphasis_us must be a finite positive number") from exc
        if not math.isfinite(tau) or not 1e-6 <= tau <= 1e-3:
            raise ValueError("deemphasis_us must be between 1 and 1000")
        self.input_rate = int(input_rate)
        self.audio_rate = int(audio_rate)
        self.deemphasis_us = float(deemphasis_us)
        self._prefilter = None
        channel_input_rate = self.input_rate
        # Fast devices need not run the narrow channel FIR at their full rate.
        # A cheap wide anti-alias stage first reduces to roughly 2–3 MS/s; the
        # following narrow FIR still removes adjacent FM channels. Preserve
        # exact integer clocks and use the general path when no divisor fits.
        if self.input_rate >= 4_000_000:
            factor = next((n for n in range(5, 1, -1)
                           if self.input_rate % (n * 1000) == 0
                           and self.input_rate // n >= 1_800_000), None)
            if factor is not None:
                channel_input_rate = self.input_rate // factor
                taps = signal.firwin(12 * factor + 1, channel_input_rate * .25,
                                     fs=self.input_rate, window=("kaiser", 8.0))
                self._prefilter = _FirDecimator(taps, factor, np.complex64)
        # +/-90 kHz useful station band; suppress aliases at/above +/-120 kHz.
        # Keep normal mono audio through 15 kHz; reject 19 kHz stereo pilot
        # and 38 kHz stereo subchannel before reducing the audio rate.
        audio_taps = signal.firwin(321, 16_500, fs=self.CHANNEL_RATE,
                                   window=("kaiser", 8.0))
        if channel_input_rate == 2_400_000:
            channel_taps = signal.firwin(321, 105_000, fs=channel_input_rate,
                                        window=("kaiser", 7.5))
            self._channel = _FirDecimator(channel_taps, 10, np.complex64)
        else:
            self._channel = _RationalChannel(channel_input_rate)
        self._audio = _FirDecimator(audio_taps, 5, np.float32)
        pole = math.exp(-1.0 / (self.audio_rate * tau))
        self._deemphasis_b = np.array([1.0 - pole], dtype=np.float64)
        self._deemphasis_a = np.array([1.0, -pole], dtype=np.float64)
        # DC from oscillator/tuning error is not audible program content.
        dc_pole = math.exp(-2.0 * math.pi * 30.0 / self.audio_rate)
        dc_gain = (1.0 + dc_pole) / 2.0
        self._dc_b = np.array([dc_gain, -dc_gain], dtype=np.float64)
        self._dc_a = np.array([1.0, -dc_pole], dtype=np.float64)
        self.reset()

    def reset(self):
        """Forget old station samples, filter memories and output alignment."""
        self._channel.reset()
        if self._prefilter is not None:
            self._prefilter.reset()
        self._audio.reset()
        self._previous_phase = np.float32(0)
        self._deemphasis_state = np.zeros(1, dtype=np.float64)
        self._dc_state = np.zeros(1, dtype=np.float64)

    def process(self, iq) -> np.ndarray:
        """Consume every new IQ sample exactly once; return zero or more PCM samples."""
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
        if not samples.size:
            return np.empty(0, dtype=np.float32)
        if self._prefilter is not None:
            samples = self._prefilter.process(samples)
        channel = self._channel.process(samples)
        if not channel.size:
            return np.empty(0, dtype=np.float32)
        # Phase subtraction avoids squaring the IQ amplitude (and thus avoids
        # product overflow). Wrap to the unambiguous [-pi, pi) phase interval.
        phases = np.angle(channel)
        differences = np.empty_like(phases)
        differences[0] = phases[0] - self._previous_phase
        np.subtract(phases[1:], phases[:-1], out=differences[1:])
        self._previous_phase = phases[-1]
        differences += np.float32(math.pi)
        np.remainder(differences, np.float32(2.0 * math.pi), out=differences)
        differences -= np.float32(math.pi)
        differences *= np.float32(self.CHANNEL_RATE / (2.0 * math.pi * self.DEVIATION_HZ))
        audio = self._audio.process(differences)
        if not audio.size:
            return np.empty(0, dtype=np.float32)
        audio, self._deemphasis_state = signal.lfilter(
            self._deemphasis_b, self._deemphasis_a, audio,
            zi=self._deemphasis_state)
        audio, self._dc_state = signal.lfilter(
            self._dc_b, self._dc_a, audio, zi=self._dc_state)
        return audio.astype(np.float32)
