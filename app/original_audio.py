"""Optional, isolated mono FM listening for the radio terrain receiver.

The receiver only copies IQ into a bounded queue.  A daemon worker owns DSP and
PortAudio; its callback only drains bounded PCM and applies a short gain ramp.
Importing this module, or leaving listening disabled, opens no audio device and
does not import numpy, scipy, or sounddevice.
"""
from __future__ import annotations

from collections import deque
import math
import threading


class AudioMonitor:
    AUDIO_RATE = 48_000
    BLOCK_SIZE = 480
    MAX_IQ_BLOCKS = 8
    MAX_IQ_SAMPLES = 262_144
    MAX_PCM_SAMPLES = 9_600  # At most 200 ms; do not accumulate stale radio.
    PREBUFFER_SAMPLES = 1_920  # 40 ms absorbs 13.65 ms IQ blocks + scheduling jitter.
    FADE_SAMPLES = 2_400  # 50 ms from silence to full-scale gain.

    def __init__(self, *, stream_factory=None, demod_factory=None):
        self._stream_factory = stream_factory
        self._demod_factory = demod_factory
        self._condition = threading.Condition(threading.Lock())
        self._enabled = False
        self._volume = 0.15
        self._muted = False
        self._frequency = None
        self._generation = 0
        self._demo = False
        self._closed = False
        self._epoch = 0
        self._gap_serial = 0
        self._failed_epoch = None
        self._error = None
        self._iq = deque()
        self._pcm = deque()
        self._pcm_samples = 0
        self._gain = 0.0
        self._stream_id = None
        self._stream_ready = False
        self._have_audio = False
        self._primed = False
        self._underruns = 0
        self._pcm_underruns = 0
        self._lock_underruns = 0
        self._driver_underruns = 0
        self._overruns = 0
        self._np = None
        self._ramp = None
        self._thread = threading.Thread(target=self._run,
                                        name="radio-terrain-audio", daemon=True)
        self._thread.start()

    def _eligible_locked(self):
        return (not self._closed and self._enabled and not self._demo
                and self._frequency is not None
                and 87_500_000 <= self._frequency <= 108_000_000
                and self._failed_epoch != self._epoch)

    def _status_locked(self):
        if self._closed:
            return "closed"
        if not self._enabled:
            return "disabled"
        if self._frequency is None:
            return "waiting"
        if self._demo:
            return "demo"
        if not 87_500_000 <= self._frequency <= 108_000_000:
            return "unsupported"
        if self._failed_epoch == self._epoch:
            return "error"
        if not self._stream_ready:
            return "starting"
        if not self._have_audio or not self._primed:
            return "buffering"
        return "muted" if self._muted or self._volume == 0 else "playing"

    def snapshot(self):
        with self._condition:
            return {
                "enabled": self._enabled,
                "volume": self._volume,
                "muted": self._muted,
                "status": self._status_locked(),
                "error": self._error,
                "sample_rate_hz": self.AUDIO_RATE,
                "frequency_hz": self._frequency,
                "buffer_ms": round(self._pcm_samples * 1000 / self.AUDIO_RATE, 2),
                "underruns": self._underruns,
                "pcm_underruns": self._pcm_underruns,
                "lock_underruns": self._lock_underruns,
                "driver_underruns": self._driver_underruns,
                "overruns": self._overruns,
            }

    def _invalidate_locked(self):
        self._epoch += 1
        self._iq.clear()
        self._pcm.clear()
        self._pcm_samples = 0
        self._gain = 0.0
        self._have_audio = False
        self._primed = False
        self._failed_epoch = None
        self._error = None
        self._condition.notify_all()

    def configure(self, enabled=None, volume=None, muted=None):
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        if muted is not None and type(muted) is not bool:
            raise ValueError("muted 必须是布尔值")
        if volume is not None:
            if (isinstance(volume, bool) or not isinstance(volume, (int, float))
                    or not math.isfinite(volume) or not 0 <= volume <= 1):
                raise ValueError("volume 必须是 0–1 范围内的有限数字")
        with self._condition:
            if self._closed:
                return
            if volume is not None:
                self._volume = float(volume)
            if muted is not None:
                self._muted = muted
            if enabled is not None and enabled != self._enabled:
                self._enabled = enabled
                self._invalidate_locked()
            self._condition.notify_all()

    def set_source(self, frequency_hz, generation, demo=False):
        """Invalidate old samples synchronously, including an in-flight DSP job."""
        with self._condition:
            if self._closed:
                return
            source = (frequency_hz, generation, bool(demo))
            if source != (self._frequency, self._generation, self._demo):
                self._frequency, self._generation, self._demo = source
                self._invalidate_locked()

    def submit(self, iq, sample_rate, frequency_hz, generation):
        """Never call a device or DSP here; stale/oversized input is discarded."""
        with self._condition:
            if (not self._eligible_locked() or frequency_hz != self._frequency
                    or generation != self._generation):
                return
            epoch = self._epoch
        try:
            if (not 0 < len(iq) <= self.MAX_IQ_SAMPLES
                    or getattr(iq, "ndim", 1) != 1
                    or isinstance(sample_rate, bool)
                    or not isinstance(sample_rate, (int, float))
                    or not math.isfinite(sample_rate) or sample_rate <= 0):
                return
            # The receiver may reuse its native buffer after submit returns.
            owned = iq.copy() if hasattr(iq, "copy") else tuple(iq)
        except Exception as exc:
            self._fail(epoch, exc)
            return
        with self._condition:
            if epoch != self._epoch or not self._eligible_locked():
                return
            if len(self._iq) >= self.MAX_IQ_BLOCKS:
                self._iq.popleft()
                self._gap_serial += 1
                self._overruns += 1
            self._iq.append((epoch, sample_rate, owned))
            self._condition.notify_all()

    def _fail(self, epoch, exc):
        with self._condition:
            if epoch != self._epoch or self._closed:
                return
            self._failed_epoch = epoch
            self._error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._iq.clear()
            self._pcm.clear()
            self._pcm_samples = 0
            self._gain = 0.0
            self._have_audio = False
            self._primed = False
            self._condition.notify_all()

    @staticmethod
    def _close_stream(stream):
        if stream is None:
            return
        # Driver calls stay on the daemon worker.  close() never waits for them
        # indefinitely, and the closed flag makes late callbacks output zero.
        try:
            abort = getattr(stream, "abort", None)
            if abort is not None:
                abort()
            else:
                stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _callback(self, stream_id, outdata, frames, timing, status):
        outdata.fill(0)
        # Never make the audio callback wait for the receiver/control thread.
        if not self._condition.acquire(blocking=False):
            self._underruns += 1
            self._lock_underruns += 1
            return
        try:
            if stream_id is not self._stream_id or not self._eligible_locked():
                return
            if getattr(status, "output_underflow", False):
                self._underruns += 1
                self._driver_underruns += 1
            if not self._primed:
                # Intentional startup/recovery silence is not an underrun.
                # Do not nibble each arriving IQ block before a useful cushion
                # has accumulated: RF supplies ~655 samples every 13.65 ms,
                # whereas this callback consumes 480 every 10 ms.
                if self._pcm_samples < max(self.PREBUFFER_SAMPLES, frames):
                    return
                self._primed = True
                self._gain = 0.0
            if self._pcm_samples < frames:
                self._underruns += 1
                self._pcm_underruns += 1
                self._primed = False
                self._gain = 0.0
                return
            written = 0
            while written < frames and self._pcm:
                pcm, offset = self._pcm[0]
                count = min(frames - written, len(pcm) - offset)
                outdata[written:written + count, 0] = pcm[offset:offset + count]
                written += count
                offset += count
                self._pcm_samples -= count
                if offset == len(pcm):
                    self._pcm.popleft()
                else:
                    self._pcm[0] = (pcm, offset)
            if written < frames:
                self._underruns += 1
                self._pcm_underruns += 1
            target = 0.0 if self._muted else self._volume
            np = self._np
            for start in range(0, frames, self.BLOCK_SIZE):
                count = min(self.BLOCK_SIZE, frames - start)
                delta = target - self._gain
                step = max(-1 / self.FADE_SAMPLES,
                           min(1 / self.FADE_SAMPLES, delta))
                gains = self._gain + self._ramp[:count] * step
                if delta >= 0:
                    np.minimum(gains, target, out=gains)
                else:
                    np.maximum(gains, target, out=gains)
                outdata[start:start + count, 0] *= gains
                self._gain = float(gains[-1])
            np.clip(outdata, -1.0, 1.0, out=outdata)
            # A subsequent burst begins with a fade instead of a full-level pop.
            if written < frames:
                self._gain = 0.0
                self._primed = False
        except Exception as exc:
            outdata.fill(0)
            self._failed_epoch = self._epoch
            self._error = f"音频回调异常: {type(exc).__name__}: {str(exc)[:200]}"
            self._iq.clear()
            self._pcm.clear()
            self._pcm_samples = 0
            self._gain = 0.0
            self._have_audio = False
            self._primed = False
            self._condition.notify_all()
        finally:
            self._condition.release()

    def _run(self):
        stream = None
        finished = None
        demod = None
        demod_rate = None
        demod_epoch = None
        demod_gap = None
        try:
            while True:
                with self._condition:
                    if self._closed:
                        break
                    epoch = self._epoch
                    eligible = self._eligible_locked()
                    if not eligible:
                        self._stream_ready = False
                        self._stream_id = None
                        if stream is None:
                            self._condition.wait(timeout=0.1)
                            continue
                    job = self._iq.popleft() if eligible and self._iq else None
                    gap = self._gap_serial
                if not eligible:
                    self._close_stream(stream)
                    stream = None
                    demod = None
                    demod_rate = None
                    continue
                try:
                    if stream is not None and finished.is_set():
                        raise RuntimeError("音频输出设备已停止；请关闭收听后重新开启")
                    if stream is None:
                        import numpy as np
                        self._np = np
                        self._ramp = np.arange(1, self.BLOCK_SIZE + 1,
                                               dtype=np.float32)
                        factory = self._stream_factory
                        if factory is None:
                            import sounddevice
                            factory = sounddevice.OutputStream
                        stream_id = object()
                        finished = threading.Event()
                        stream = factory(
                            samplerate=self.AUDIO_RATE, channels=1,
                            dtype="float32", blocksize=self.BLOCK_SIZE,
                            latency="low",
                            callback=lambda out, frames, timing, status, sid=stream_id:
                                self._callback(sid, out, frames, timing, status),
                            finished_callback=finished.set)
                        with self._condition:
                            self._stream_id = stream_id
                        stream.start()
                        with self._condition:
                            self._stream_ready = True
                    if job is None:
                        with self._condition:
                            if not self._iq and not self._closed:
                                self._condition.wait(timeout=0.05)
                        continue
                    job_epoch, rate, iq = job
                    with self._condition:
                        if job_epoch != self._epoch or not self._eligible_locked():
                            continue
                    if demod is None or demod_rate != rate:
                        factory = self._demod_factory
                        if factory is None:
                            from original_fm import WbfmDemodulator
                            factory = WbfmDemodulator
                        demod = factory(input_rate=rate, audio_rate=self.AUDIO_RATE,
                                        deemphasis_us=50.0)
                        demod_rate = rate
                        demod_epoch = None
                    if demod_epoch != job_epoch or demod_gap != gap:
                        demod.reset()
                        demod_epoch, demod_gap = job_epoch, gap
                    pcm = self._np.asarray(demod.process(iq), dtype=self._np.float32)
                    if pcm.ndim != 1:
                        raise ValueError("FM 解调器没有返回单声道音频")
                    # Sanitize off the callback thread; own any DSP scratch data.
                    pcm = self._np.nan_to_num(pcm, copy=True, nan=0.0,
                                              posinf=0.0, neginf=0.0)
                    self._np.clip(pcm, -1.0, 1.0, out=pcm)
                    with self._condition:
                        if (job_epoch != self._epoch or gap != self._gap_serial
                                or not self._eligible_locked() or not len(pcm)):
                            continue
                        if len(pcm) > self.MAX_PCM_SAMPLES:
                            pcm = pcm[-self.MAX_PCM_SAMPLES:].copy()
                            self._overruns += 1
                            self._gain = 0.0
                        while self._pcm and self._pcm_samples + len(pcm) > self.MAX_PCM_SAMPLES:
                            old, offset = self._pcm.popleft()
                            self._pcm_samples -= len(old) - offset
                            self._overruns += 1
                            self._gain = 0.0
                        self._pcm.append((pcm, 0))
                        self._pcm_samples += len(pcm)
                        self._have_audio = True
                except Exception as exc:
                    self._fail(epoch, exc)
                    with self._condition:
                        self._stream_ready = False
                        self._stream_id = None
                    self._close_stream(stream)
                    stream = None
                    demod = None
                    demod_rate = None
        finally:
            self._close_stream(stream)

    def close(self):
        with self._condition:
            if not self._closed:
                self._closed = True
                self._enabled = False
                self._stream_id = None
                self._stream_ready = False
                self._invalidate_locked()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.75)
