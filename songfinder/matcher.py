"""Speed- and pitch-tolerant matching of a clip against one reference recording.

Time model:  reference_time = offset + rate * clip_time
Pitch model: reference_band = clip_band + pitch_bins   (BPO bands per octave)

Three steps:
  screen    - each clip window is correlated against the whole reference over a
              coarse rate grid; the best few positions per window are kept.
  consensus - windows matched independently must fall on one line (same rate,
              offset and pitch). A wrong recording almost never manages that.
  verify    - the whole clip is scored along the fitted line, refined, compared
              with deliberately wrong offsets, and split into segments.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.fft

from .features import BPO, FPS, half_rate

FPS_LO = FPS / 2

# Decision thresholds. On 40 dance-class videos every confirmed recording scored at
# least 0.64 full-clip similarity; other performances of the same chart scored up
# to 0.44. EXACT_SIM sits in that gap. Synthetic positives in the tests score 0.57+.
EXACT_SIM, EXACT_Z, EXACT_WINDOWS, EXACT_COVERAGE, EXACT_OVERLAP_S = 0.55, 8.0, 3, 0.6, 20.0
PROB_SIM, PROB_Z = 0.22, 6.0
QUICK_REJECT = 0.22      # bulk scans: drop a reference when no sampled window reaches this
SEGMENT_S = 5.0
SEGMENT_OK = 0.15
# The same recording stays on its time line to within a frame. Another band playing the
# same arrangement drifts by tenths of a second, however well the notes line up.
MAX_TIMING_SPREAD_S = 0.08
LAG_SPAN_S = 0.6


@dataclass
class Hyp:
    window: int
    clip_start: float
    clip_end: float
    clip_c: float
    ref_c: float
    rate: float
    shift: int
    score: float

    @property
    def ref_start(self) -> float:
        return self.ref_c - self.rate * (self.clip_c - self.clip_start)

    @property
    def ref_end(self) -> float:
        return self.ref_c + self.rate * (self.clip_end - self.clip_c)


@dataclass
class Fit:
    rate: float
    offset: float
    shift: int
    n_windows: int
    score_sum: float
    inliers: list[Hyp] = field(default_factory=list)


@dataclass
class Verification:
    rate: float
    offset: float
    pitch_bins: float
    similarity: float
    z: float
    null_median: float
    null_spread: float
    coverage: float
    overlap_s: float
    segments: list[tuple[float, float, float]] = field(default_factory=list)
    timing_spread: float = 0.0          # how far 5 s segments wander off the fitted line (s)

    @property
    def clip_pitch_semitones(self) -> float:
        """How far the clip's pitch sits from the reference (negative = lower)."""
        return -self.pitch_bins * 12.0 / BPO


@dataclass
class Evaluation:
    decision: str                      # 'exact' | 'probable' | 'related' | 'none'
    fit: Fit | None = None
    verification: Verification | None = None
    basis: str = "audio"               # 'audio', or 'recogniser' when only Shazam could confirm
    consensus: dict | None = None      # recogniser basis: positions, rate, offset

    @property
    def rank_key(self) -> tuple:
        order = {"exact": 3, "probable": 2, "related": 1, "none": 0}[self.decision]
        if self.decision == "exact" and self.basis != "audio":
            order = 2.5
        sim = self.verification.similarity if self.verification else 0.0
        n = self.fit.n_windows if self.fit else 0
        s = self.fit.score_sum if self.fit else 0.0
        return (order, round(sim, 4), n, s)


def _interp_time(q: np.ndarray, x: np.ndarray) -> np.ndarray:
    i = np.clip(np.floor(x).astype(np.int64), 0, q.shape[1] - 2)
    f = (x - i).astype(np.float32)
    return q[:, i] * (1.0 - f) + q[:, i + 1] * f


class Query:
    """A clip prepared once and matched against many references."""

    def __init__(self, feats: np.ndarray, rate_min: float = 0.70, rate_max: float = 1.40,
                 rate_step: float = 0.015, free_pitch: bool = False, max_windows: int = 6):
        if not 0 < rate_min <= 1.0 <= rate_max:
            raise ValueError("Need rate_min <= 1 <= rate_max")
        self.F = feats
        self.B, self.TQ = feats.shape
        self.duration = self.TQ / FPS
        if self.duration < 6:
            raise ValueError("Clip is too short: need at least 6 seconds of audio")
        k_lo = math.floor(math.log(rate_min) / math.log1p(rate_step))
        k_hi = math.ceil(math.log(rate_max) / math.log1p(rate_step))
        self.rates = (1.0 + rate_step) ** np.arange(k_lo, k_hi + 1)   # contains exactly 1.0
        c_max = BPO * math.log2(max(1.0 / self.rates.min(), self.rates.max()))
        self.m = int(math.ceil(c_max)) + 3      # band margin reserved for pitch shifts
        if self.B - 2 * self.m < 40:
            raise ValueError("Rate range too wide for the feature band count")
        self.Qc = np.ascontiguousarray(feats[self.m:self.B - self.m])
        self.Qlo = np.ascontiguousarray(half_rate(feats)[self.m:self.B - self.m])
        self.shifts: list[list[int]] = []
        for r in self.rates:
            if free_pitch:
                self.shifts.append(list(range(-(self.m - 3), self.m - 2)))
            else:   # pitch follows speed (resampled playback) or stays put (time-stretch)
                c = -BPO * math.log2(r)
                self.shifts.append(sorted({math.floor(c), math.ceil(c), 0}))
        self._pick_windows(max_windows)

    # ---------------------------------------------------------------- windows
    def _pick_windows(self, max_windows: int) -> None:
        d = self.duration
        length = 10.0 if d >= 30 else max(4.0, d / 3)
        n = int(min(max_windows, max(1, d // length)))
        if n < 3 and d >= 8:
            n = 3                                   # overlap rather than lose the consensus test
        starts = np.linspace(0.0, d - length - 2.0 / FPS_LO, n) if n > 1 else np.array([0.0])
        self.window_len = length
        self.windows: list[tuple[float, float]] = []
        self.W: list[list[np.ndarray]] = []
        self.Eq: list[list[float]] = []
        t_lo = self.Qlo.shape[1]
        for t0 in starts:
            a, b = int(t0 * FPS_LO), int((t0 + length) * FPS_LO)
            seg = self.Qlo[:, a:b]
            if np.mean(np.abs(seg).sum(axis=0) == 0) > 0.5:
                continue                            # mostly silence
            per_rate, energy = [], []
            for r in self.rates:
                n_frames = int(length * r * FPS_LO)
                x = np.minimum(t0 * FPS_LO + np.arange(n_frames) / r, t_lo - 1.001)
                w = _interp_time(self.Qlo, x)
                per_rate.append(w)
                energy.append(float(np.sum(w * w, dtype=np.float64)))
            self.windows.append((float(t0), float(t0 + length)))
            self.W.append(per_rate)
            self.Eq.append(energy)
        if not self.windows:
            raise ValueError("Clip contains no usable audio")

    # ----------------------------------------------------------------- screen
    def screen(self, ref: np.ndarray, top_k: int = 3, only: list[int] | None = None) -> list[Hyp]:
        """Best positions of every window (or just `only`) in the reference (features at FPS)."""
        r_lo = half_rate(ref)
        b, tr = r_lo.shape
        m = self.m
        n_max = max(w.shape[1] for w in self.W[0])
        n_min = min(w.shape[1] for w in self.W[0])
        if tr < n_min + 1:
            return []
        nfft = scipy.fft.next_fast_len(tr + n_max, real=True)
        rf = scipy.fft.rfft(r_lo, nfft, axis=1)
        band_cum = np.concatenate([np.zeros((1, tr)), np.cumsum(r_lo.astype(np.float64) ** 2, axis=0)])
        time_cum: dict[int, np.ndarray] = {}

        def window_energy(s: int, n: int) -> np.ndarray:
            if s not in time_cum:
                fe = band_cum[b - m + s] - band_cum[m + s]
                time_cum[s] = np.concatenate([[0.0], np.cumsum(fe)])
            cs = time_cum[s]
            return cs[n:] - cs[:-n]

        hyps: list[Hyp] = []
        for wi, (t0, t1) in enumerate(self.windows):
            if only is not None and wi not in only:
                continue
            found = []
            for ri, rate in enumerate(self.rates):
                q = self.W[wi][ri]
                n = q.shape[1]
                if n > tr:
                    continue
                qf = np.conj(scipy.fft.rfft(q, nfft, axis=1))
                eq = self.Eq[wi][ri]
                for s in self.shifts[ri]:
                    spec = np.einsum("bk,bk->k", qf, rf[m + s:b - m + s])
                    corr = scipy.fft.irfft(spec, nfft)[:tr - n + 1]
                    er = window_energy(s, n)
                    score = np.where(er > 0.15 * n, corr / np.sqrt(np.maximum(er * eq, 1e-9)), 0.0)
                    k = int(np.argmax(score))
                    found.append((float(score[k]), k, float(rate), s, n))
            found.sort(reverse=True)
            kept: list[Hyp] = []
            for sc, k, rate, s, n in found:
                ref_c = (k + (n - 1) / 2) / FPS_LO
                if any(abs(ref_c - h.ref_c) < 1.0 for h in kept):
                    continue
                clip_c = t0 + (n - 1) / (2 * rate * FPS_LO)
                kept.append(Hyp(wi, t0, t1, clip_c, ref_c, rate, s, sc))
                if len(kept) == top_k:
                    break
            hyps.extend(kept)
        return hyps

    # -------------------------------------------------------------- consensus
    @staticmethod
    def consensus(hyps: list[Hyp]) -> Fit | None:
        if not hyps:
            return None
        windows = sorted({h.window for h in hyps})
        best: tuple | None = None
        for seed in hyps:
            inliers = [seed]
            for w in windows:
                if w == seed.window:
                    continue
                ok = [g for g in hyps if g.window == w
                      and abs(g.shift - seed.shift) <= 1
                      and abs(math.log(g.rate / seed.rate)) <= 0.035
                      and abs(g.ref_c - (seed.ref_c + seed.rate * (g.clip_c - seed.clip_c)))
                      <= 0.25 + 0.015 * abs(g.clip_c - seed.clip_c)]
                if ok:
                    inliers.append(max(ok, key=lambda g: g.score))
            key = (len(inliers), sum(h.score for h in inliers))
            if best is None or key > best[0]:
                best = (key, seed, inliers)
        (n, total), seed, inliers = best
        rate, offset = seed.rate, seed.ref_c - seed.rate * seed.clip_c
        if n >= 2:
            x = np.array([h.clip_c for h in inliers])
            y = np.array([h.ref_c for h in inliers])
            slope, icpt = np.polyfit(x, y, 1)
            if abs(math.log(slope / seed.rate)) <= 0.03:
                rate, offset = float(slope), float(icpt)
        shift = int(round(float(np.median([h.shift for h in inliers]))))
        return Fit(rate, offset, shift, n, total, sorted(inliers, key=lambda h: h.window))

    # ----------------------------------------------------------------- verify
    def _ref_bands(self, ref: np.ndarray, p: float) -> np.ndarray:
        m, b = self.m, self.B
        p = float(np.clip(p, -(m - 1.5), m - 1.5))
        k = math.floor(p)
        f = p - k
        a = ref[m + k:b - m + k]
        if f > 1e-6:
            a = (1.0 - f) * a + f * ref[m + k + 1:b - m + k + 1]
        return a

    def _aligned(self, rp: np.ndarray, rate: float, offset: float):
        """Clip resampled onto the reference frames it overlaps."""
        tr = rp.shape[1]
        j0 = max(0, math.ceil(offset * FPS))
        j1 = min(tr, math.floor((offset + rate * (self.TQ - 1) / FPS) * FPS) + 1)
        if j1 - j0 < 2 * FPS:
            return None
        x = (np.arange(j0, j1) / FPS - offset) / rate * FPS
        return _interp_time(self.Qc, x), rp[:, j0:j1], x

    def _sim(self, rp: np.ndarray, rate: float, offset: float) -> tuple[float, float]:
        al = self._aligned(rp, rate, offset)
        if al is None:
            return 0.0, 0.0
        q, r, x = al
        den = math.sqrt(float(np.sum(q * q, dtype=np.float64)) * float(np.sum(r * r, dtype=np.float64)))
        sim = float(np.sum(q * r, dtype=np.float64)) / den if den > 1e-9 else 0.0
        return sim, (x[-1] - x[0]) / FPS

    def verify(self, ref: np.ndarray, fit: Fit) -> Verification:
        tc = self.duration / 2                      # pivot rate changes around the clip centre

        def search(rp, rates, centres):
            best = (-1.0, fit.rate, fit.offset)
            for r in rates:
                for c in centres:
                    off = c - r * tc
                    s, _ = self._sim(rp, r, off)
                    if s > best[0]:
                        best = (s, float(r), float(off))
            return best

        p = float(fit.shift)
        rp = self._ref_bands(ref, p)
        c0 = fit.offset + fit.rate * tc
        _, rate, off = search(rp, fit.rate * (1 + np.arange(-10, 11) * 0.0015),
                              c0 + np.arange(-8, 9) * 0.04)
        best_p = (-1.0, p)
        for dp in np.arange(-6, 7) * 0.25:          # fractional pitch
            s, _ = self._sim(self._ref_bands(ref, p + dp), rate, off)
            if s > best_p[0]:
                best_p = (s, p + dp)
        p = best_p[1]
        rp = self._ref_bands(ref, p)
        c0 = off + rate * tc
        sim, rate, off = search(rp, rate * (1 + np.arange(-10, 11) * 0.0002),
                                c0 + np.arange(-6, 7) * 0.01)
        _, overlap = self._sim(rp, rate, off)

        # Same line shifted to wrong positions gives this reference's own noise floor.
        null = []
        for k in range(12):
            for sign in (-1, 1):
                s, ov = self._sim(rp, rate, off + sign * (2.0 + 1.7 * k))
                if ov >= 0.5 * overlap:
                    null.append(s)
        if len(null) >= 4:
            med = float(np.median(null))
            spread = float(1.4826 * np.median(np.abs(np.array(null) - med))) + 1e-3
        else:                                        # reference barely longer than the clip
            med, spread = 0.0, 0.03
        z = (sim - med) / spread

        segments = []
        al = self._aligned(rp, rate, off)
        if al is not None:
            q, r, x = al
            t = x / FPS
            for a in np.arange(0.0, self.duration, SEGMENT_S):
                sel = (t >= a) & (t < a + SEGMENT_S)
                if sel.sum() < FPS:                  # under a second of overlap
                    continue
                qs, rs = q[:, sel], r[:, sel]
                den = math.sqrt(float(np.sum(qs * qs)) * float(np.sum(rs * rs)))
                if den > 1e-6:
                    segments.append((float(a), float(min(a + SEGMENT_S, self.duration)),
                                     float(np.sum(qs * rs)) / den))
        coverage = float(np.mean([s >= SEGMENT_OK for *_, s in segments])) if segments else 0.0
        return Verification(rate, off, p, sim, z, med, spread, coverage, overlap, segments,
                            self._timing_spread(rp, rate, off))

    def _timing_spread(self, rp: np.ndarray, rate: float, off: float) -> float:
        """Let every 5 s segment slide a little and see where it fits best.

        Returns the 90th percentile of how far the well-matching segments sit
        from their common position. About 0 for the same recording.
        """
        n_seg = int(math.ceil(self.duration / SEGMENT_S))
        lags = np.arange(-LAG_SPAN_S, LAG_SPAN_S + 1e-9, 1.0 / FPS)
        sims = np.zeros((len(lags), n_seg))
        for li, d in enumerate(lags):
            al = self._aligned(rp, rate, off + d)
            if al is None:
                continue
            q, r, x = al
            seg = np.clip((x / FPS / SEGMENT_S).astype(np.int64), 0, n_seg - 1)
            num = np.bincount(seg, weights=np.einsum("bt,bt->t", q, r), minlength=n_seg)
            dq = np.bincount(seg, weights=np.einsum("bt,bt->t", q, q), minlength=n_seg)
            dr = np.bincount(seg, weights=np.einsum("bt,bt->t", r, r), minlength=n_seg)
            cnt = np.bincount(seg, minlength=n_seg)
            den = np.sqrt(dq * dr)
            sims[li] = np.where((den > 1e-6) & (cnt >= FPS), num / np.maximum(den, 1e-6), 0.0)
        best = sims.max(axis=0)
        good = best >= max(SEGMENT_OK, 0.5 * float(np.median(best[best > 0])) if np.any(best > 0) else 1.0)
        if good.sum() < 2:
            return 0.0
        best_lag = lags[sims.argmax(axis=0)][good]
        return float(np.percentile(np.abs(best_lag - np.median(best_lag)), 90))

    # --------------------------------------------------------------- evaluate
    def evaluate(self, ref: np.ndarray, quick: bool = False) -> Evaluation:
        """Full evaluation. With `quick`, first test a third of the windows and stop
        if none of them finds anything; used when scanning hundreds of references."""
        n = len(self.windows)
        if quick and n >= 5:
            sample = self.screen(ref, only=list(range(1, n, 3)))
            if max((h.score for h in sample), default=0.0) < QUICK_REJECT:
                return Evaluation("none", self.consensus(sample))
        fit = self.consensus(self.screen(ref))
        if fit is None:
            return Evaluation("none")
        if fit.n_windows < 2 and fit.score_sum < 0.30:
            return Evaluation("none", fit)           # a lone weak window is not worth verifying
        ver = self.verify(ref, fit)
        return Evaluation(decide(fit, ver), fit, ver)


def decide(fit: Fit, ver: Verification) -> str:
    strong = ver.similarity >= PROB_SIM and ver.z >= PROB_Z
    if strong and ver.timing_spread > MAX_TIMING_SPREAD_S:
        return "related"                # same arrangement, but not this recording
    if (ver.similarity >= EXACT_SIM and ver.z >= EXACT_Z and fit.n_windows >= EXACT_WINDOWS
            and ver.coverage >= EXACT_COVERAGE and ver.overlap_s >= EXACT_OVERLAP_S):
        return "exact"
    return "probable" if strong else "none"


def describe_speed(ver: Verification) -> str:
    """Plain-language reading of rate and pitch for the person practising."""
    coupled = 12.0 * math.log2(ver.rate)
    pitch = ver.clip_pitch_semitones
    pct = ver.rate * 100
    if abs(ver.rate - 1) < 0.004 and abs(pitch) < 0.25:
        return "The clip plays the recording at its original speed and pitch."
    if abs(ver.rate - 1) < 0.015:
        return (f"The clip plays at {pct:.1f}% of this release's speed, so effectively the original "
                "tempo. Differences this small are normal between transfers of the same recording.")
    if abs(pitch - coupled) < 0.3:
        how = f"pitch moved with it ({pitch:+.2f} semitones), as with resampled or turntable-style playback"
    elif abs(pitch) < 0.3:
        how = "pitch is unchanged, so it was time-stretched"
    else:
        how = f"pitch is shifted separately by {pitch:+.2f} semitones"
    return (f"The clip plays the recording at {pct:.1f}% speed; {how}. "
            f"Set the original to {ver.rate:.2f}x to practise at the clip's tempo, "
            f"or play the clip at {1 / ver.rate:.2f}x to hear the original tempo.")
