"""Conservative tune leads across performances; never exact recording evidence.

A flexible pitch-class path must beat phrase-shuffled controls and remain close
to one tempo progression. Same chords alone are insufficient. This first pass
checks the same key, 20–120 second clips, and references already being evaluated.
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter1d
from . import budget
from .features import FPS

TUNE_FPS = 5


def chroma(features):
    upper = np.maximum(0, features[30:102])
    c = upper.reshape(3, 12, 2, -1).max(axis=2).sum(axis=0)
    c = gaussian_filter1d(c, 2, axis=1)[:, ::FPS // TUNE_FPS]
    return c / np.maximum(np.linalg.norm(c, axis=0, keepdims=True), 1e-9)


def align(q, r, trace=False):
    """Subsequence DTW: every query frame contributes; local rate stays 0.5–2."""
    costs = 1 - q.T @ r
    n, m = costs.shape
    if n < 2 or m < n // 2 + 2:
        return 1., None
    d = np.full((n, m), np.inf)
    d[0] = costs[0]
    for i in range(1, n):
        budget.check()
        d[i, 1:] = d[i-1, :-1] + costs[i, 1:]
        d[i, 2:] = np.minimum(d[i, 2:], d[i-1, :-2] + costs[i, 2:])
        if i >= 2:
            d[i, 1:] = np.minimum(d[i, 1:], d[i-2, :-1] + costs[i-1, 1:] + costs[i, 1:])
    j = int(d[-1].argmin())
    cost = float(d[-1, j] / n)
    if not trace or not np.isfinite(cost):
        return cost, None
    i = n-1
    path = []
    while i > 0 and j > 0:
        path.append((i, j))
        choices = [(d[i-1, j-1] + costs[i, j], 1, 1)]
        if j >= 2:
            choices.append((d[i-1, j-2] + costs[i, j], 1, 2))
        if i >= 2:
            choices.append((d[i-2, j-1] + costs[i-1, j] + costs[i, j], 2, 1))
        _, di, dj = min(choices)
        i -= di
        j -= dj
    path.append((i, j))
    return cost, np.asarray(path[::-1])


def compare(query, reference):
    seconds = query.shape[1] / FPS
    if not 20 <= seconds <= 120 or reference.shape[1] < 20*FPS:
        return None
    q, r = chroma(query), chroma(reference)
    if (np.linalg.norm(q, axis=0) > .5).mean() < .9:
        return None
    cost, path = align(q, r, trace=True)
    if cost > .20 or path is None:
        return None
    rate, offset = np.polyfit(path[:, 0], path[:, 1], 1)
    drift = float(np.std(path[:, 1] - (offset + rate*path[:, 0])) / TUNE_FPS)
    if not .7 <= rate <= 1.4 or drift > .4:
        return None
    controls = []
    for seed in range(6):
        chunks = np.array_split(q, max(6, round(seconds/3)), axis=1)
        np.random.default_rng(seed).shuffle(chunks)
        controls.append(align(np.concatenate(chunks, axis=1), r)[0])
    gain = float(np.median(controls) - cost)
    if gain < .025:
        return None
    return {'basis': 'tune', 'similarity': round(1-cost, 4),
            'phrase_order_gain': round(gain, 4), 'timing_drift_s': round(drift, 3),
            'compared_seconds': round(seconds, 1)}
