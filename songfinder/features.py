"""Log-frequency spectrogram features used for all matching.

On this representation a speed change is a stretch of the time axis and a
pitch change is a vertical shift, so both can be searched cheaply.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import scipy.fft
import scipy.signal

from .audio import SR

FPS = 25                 # feature frames per second
HOP = SR // FPS          # 882 samples
WIN = 4096               # analysis window (186 ms)
NFFT = 8192              # zero-padded so the lowest bands still span FFT bins
BPO = 24                 # bands per octave (2 per semitone)
N_BINS = 132             # 110 Hz .. about 4.8 kHz
FMIN = 110.0
FEATURE_VERSION = "lf132-25fps-v1"   # bump when anything above changes


@lru_cache(maxsize=1)
def _filterbank() -> np.ndarray:
    freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
    logf = np.log2(np.maximum(freqs, 1e-9))
    centres = np.log2(FMIN) + np.arange(-1, N_BINS + 1) / BPO
    fb = np.zeros((N_BINS, freqs.size), np.float32)
    for k in range(N_BINS):
        lo, c, hi = centres[k], centres[k + 1], centres[k + 2]
        w = np.clip(np.minimum((logf - lo) / (c - lo), (hi - logf) / (hi - c)), 0, None)
        fb[k] = w / w.sum()
    return fb


def band_spectrogram(y: np.ndarray) -> np.ndarray:
    """Magnitude in N_BINS log-spaced bands, shape (frames, N_BINS)."""
    y = np.asarray(y, np.float32)
    pad = WIN // 2
    yp = np.concatenate([np.zeros(pad, np.float32), y, np.zeros(pad + HOP, np.float32)])
    n_frames = 1 + len(y) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(yp, WIN)[::HOP][:n_frames]
    window = scipy.signal.get_window("hann", WIN).astype(np.float32)
    fb_t = _filterbank().T
    out = np.empty((n_frames, N_BINS), np.float32)
    for a in range(0, n_frames, 256):          # chunked to bound memory
        mag = np.abs(scipy.fft.rfft(frames[a:a + 256] * window, NFFT, axis=1))
        out[a:a + 256] = mag @ fb_t
    return out


def features(y: np.ndarray) -> np.ndarray:
    """Normalised features, shape (N_BINS, frames) at FPS. Silent frames are 0."""
    p = band_spectrogram(y)
    frame_energy = p.sum(axis=1)
    positive = frame_energy[frame_energy > 0]
    if positive.size == 0:
        return np.zeros((N_BINS, p.shape[0]), np.float32)
    active = frame_energy > 1e-3 * np.median(positive)
    z = np.log1p(p / np.median(p[p > 0]))
    z -= z[active].mean(axis=0)                 # remove stationary colouration (room, mic, EQ)
    z -= z.mean(axis=1, keepdims=True)          # remove per-frame level
    z /= np.maximum(np.linalg.norm(z, axis=1, keepdims=True), 1e-6)
    z[~active] = 0.0                            # silence must not correlate with silence
    return np.ascontiguousarray(z.T, dtype=np.float32)


def half_rate(f: np.ndarray) -> np.ndarray:
    """Average frame pairs (FPS/2) and renormalise; used for fast screening."""
    t = f.shape[1] // 2 * 2
    lo = 0.5 * (f[:, 0:t:2] + f[:, 1:t:2])
    n = np.linalg.norm(lo, axis=0, keepdims=True)
    return np.where(n > 0.2, lo / np.maximum(n, 1e-6), 0.0).astype(np.float32)
