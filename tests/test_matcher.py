"""Matcher tests on synthetic audio. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from songfinder.features import features  # noqa: E402
from songfinder.matcher import Query  # noqa: E402

SR = synth.SR


class MatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.score = synth.make_score(seed=1)
        cls.reference = synth.render(cls.score, bpm=180)
        cls.ref_feats = features(cls.reference)
        # Clip: 45 s starting 12 s into the reference, slowed to 92.6% by resampling,
        # then recorded "in a room" with noise and speech on top.
        cls.rate, cls.offset = 0.926, 12.0
        excerpt = cls.reference[int(cls.offset * SR):int((cls.offset + 45 * cls.rate) * SR)]
        cls.clip = synth.room_recording(synth.varispeed(excerpt, cls.rate))
        cls.query = Query(features(cls.clip))

    def test_resampled_slowdown_is_exact(self):
        ev = self.query.evaluate(self.ref_feats)
        self.assertEqual(ev.decision, "exact", ev)
        v = ev.verification
        self.assertAlmostEqual(v.rate, self.rate, delta=0.003)
        self.assertAlmostEqual(v.offset, self.offset, delta=0.15)
        self.assertAlmostEqual(v.clip_pitch_semitones, 12 * math.log2(self.rate), delta=0.3)
        self.assertGreaterEqual(ev.fit.n_windows, 3)
        self.assertLessEqual(v.timing_spread, 0.04)

    def test_time_stretch_keeps_pitch(self):
        # Reference re-rendered 10% faster with unchanged pitch = a pitch-preserving stretch.
        rate, offset = 1.10, 20.0
        fast = synth.render(self.score, bpm=180 * rate)
        a = int(offset / rate * SR)
        clip = synth.room_recording(fast[a:a + 40 * SR], seed=3)
        ev = Query(features(clip)).evaluate(self.ref_feats)
        self.assertEqual(ev.decision, "exact", ev)
        self.assertAlmostEqual(ev.verification.rate, rate, delta=0.004)
        self.assertAlmostEqual(ev.verification.offset, offset, delta=0.2)
        self.assertAlmostEqual(ev.verification.clip_pitch_semitones, 0.0, delta=0.3)

    def test_different_song_is_rejected(self):
        other = features(synth.render(synth.make_score(seed=99), bpm=176))
        self.assertEqual(self.query.evaluate(other).decision, "none")

    def test_other_performance_of_same_tune_is_rejected(self):
        # Same notes, different timbre, human timing and a drifting tempo.
        cover = synth.render(self.score, bpm=172, timbre=1, drift=0.004, jitter=0.02, seed=7)
        ev = self.query.evaluate(features(cover))
        self.assertNotIn(ev.decision, ("exact", "probable"), ev)

    def test_tight_cover_with_slow_tempo_drift_is_only_related(self):
        # A band recreating the arrangement almost perfectly: same notes and timbre,
        # human-tight timing (15 ms) and a slowly wandering tempo. It must not pass as the same recording.
        cover = synth.render(self.score, bpm=180, drift=0.002, jitter=0.015, seed=11)
        ev = self.query.evaluate(features(cover))
        self.assertNotIn(ev.decision, ("exact", "probable"), ev)

    def test_thirty_second_preview_still_matches(self):
        preview = self.reference[int(20 * SR):int(50 * SR)]
        ev = self.query.evaluate(features(preview))
        self.assertIn(ev.decision, ("exact", "probable"), ev)
        self.assertAlmostEqual(ev.verification.rate, self.rate, delta=0.004)

    def test_silence_does_not_match(self):
        import numpy as np
        quiet = np.zeros(60 * SR, np.float32)
        self.assertEqual(self.query.evaluate(features(quiet)).decision, "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
