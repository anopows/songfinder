"""Synthetic music and degradations for testing the matcher without real audio."""
from __future__ import annotations

import numpy as np
import scipy.signal

SR = 22050


def make_score(seed: int, bars: int = 48) -> list[tuple[float, float, int, int]]:
    """Random tune: (onset_beats, length_beats, midi_note, voice)."""
    rng = np.random.default_rng(seed)
    scale = np.array([0, 2, 4, 5, 7, 9, 11])
    notes = []
    pitch = 67
    for bar in range(bars):
        root = 48 + int(rng.choice([0, 5, 7, 9, 2]))
        for beat in range(4):                                   # bass
            notes.append((bar * 4 + beat, 0.9, root + (7 if beat == 2 else 0) - 12, 0))
        for third in (0, 4, 7):                                 # chord
            notes.append((bar * 4 + 1, 0.4, root + third, 1))
            notes.append((bar * 4 + 3, 0.4, root + third, 1))
        t = 0.0
        while t < 4:                                            # melody
            length = float(rng.choice([0.5, 0.5, 1.0, 1.5]))
            pitch = int(np.clip(pitch + rng.integers(-4, 5), 60, 84))
            pitch = 60 + 12 * ((pitch - 60) // 12) + int(scale[np.argmin(np.abs(scale - (pitch - 60) % 12))])
            if rng.random() > 0.15:
                notes.append((bar * 4 + t, length * 0.9, pitch, 2))
            t += length
    return notes


def render(score, bpm: float = 180.0, timbre: int = 0, drift: float = 0.0,
           jitter: float = 0.0, seed: int = 0) -> np.ndarray:
    """Additive synthesis. `drift` and `jitter` imitate a different human performance."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    last_beat = int(max(o + l for o, l, *_ in score)) + 2
    # beat -> seconds map with a slow random tempo walk
    step = beat * np.exp(np.cumsum(rng.normal(0, drift, last_beat * 4)))
    beat_time = np.concatenate([[0.0], np.cumsum(step)]) / 4
    harmonics = [(1.0, 0.5, 0.3, 0.15, 0.1), (1.0, 0.15, 0.5, 0.05, 0.25)][timbre]
    y = np.zeros(int((beat_time[-1] + 2) * SR), np.float32)
    for onset, length, midi, voice in score:
        t0 = np.interp(onset * 4, np.arange(beat_time.size), beat_time) + rng.normal(0, jitter)
        dur = length * beat
        n = int(dur * SR)
        a = int(max(t0, 0) * SR)
        t = np.arange(n) / SR
        f0 = 440.0 * 2 ** ((midi - 69) / 12)
        tone = sum(h * np.sin(2 * np.pi * f0 * (i + 1) * t) for i, h in enumerate(harmonics))
        env = np.minimum(t / 0.01, 1) * np.exp(-t / (0.5 if voice == 2 else 0.25))
        y[a:a + n] += (tone * env * (0.5 if voice == 1 else 1.0)).astype(np.float32)[:len(y) - a]
    return y / np.max(np.abs(y))


def varispeed(y: np.ndarray, rate: float) -> np.ndarray:
    """Play `y` at `rate` times its speed by resampling: pitch moves with speed."""
    n_out = int(len(y) / rate)
    return np.interp(np.arange(n_out) * rate, np.arange(len(y)), y).astype(np.float32)


def room_recording(y: np.ndarray, seed: int = 1, snr_db: float = 6.0,
                   speech: bool = True) -> np.ndarray:
    """Reverb, band-limiting, noise and speech-like bursts: a phone in a dance hall."""
    rng = np.random.default_rng(seed)
    ir_t = np.arange(int(0.35 * SR)) / SR
    ir = rng.normal(0, 1, ir_t.size) * np.exp(-ir_t / 0.08)
    ir[0] = 4.0
    out = scipy.signal.fftconvolve(y, ir)[:len(y)]
    sos = scipy.signal.butter(2, [180, 5000], "bandpass", fs=SR, output="sos")
    out = scipy.signal.sosfilt(sos, out)
    out /= np.sqrt(np.mean(out ** 2))
    noise = rng.normal(0, 1, len(out))
    noise = scipy.signal.lfilter([1], [1, -0.9], noise)
    noise /= np.sqrt(np.mean(noise ** 2))
    out = out + noise * 10 ** (-snr_db / 20)
    if speech:
        t = np.arange(len(out)) / SR
        f0 = 150 + 50 * np.sin(2 * np.pi * 0.7 * t) + 20 * np.sin(2 * np.pi * 3.1 * t)
        phase = 2 * np.pi * np.cumsum(f0) / SR
        voice = sum(np.sin(k * phase) / k for k in range(1, 12))
        gate = (np.sin(2 * np.pi * 0.23 * t + 1.0) > 0.2) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)) ** 2
        voice = voice * gate
        out = out + voice / (np.sqrt(np.mean(voice ** 2)) + 1e-9) * 0.8
    return (out / np.max(np.abs(out))).astype(np.float32)
