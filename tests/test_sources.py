"""Offline tests for link recognition, title matching and the feature cache."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from songfinder.cache import FeatureCache  # noqa: E402
from songfinder.sources import artist_variants, candidates_from_text, title_matches  # noqa: E402


class SourceTests(unittest.TestCase):
    def test_title_matching_ignores_case_accents_and_extras(self):
        self.assertTrue(title_matches("Everybody Loves My Baby", "everybody loves my baby (Live)"))
        self.assertTrue(title_matches("Everybody Loves My Baby",
                                      "Everybody Loves My Baby, But My Baby Don't Love Nobody But Me"))
        self.assertTrue(title_matches("Café del Mar", "CAFE DEL MAR - remix"))
        self.assertFalse(title_matches("Everybody Loves My Baby", "Everybody Loves Me"))
        self.assertFalse(title_matches("", "anything"))

    def test_artist_credit_is_simplified_for_searching(self):
        self.assertEqual(artist_variants("Woody Herman And His Orchestra Feat. Muriel Lane & Steady Nelson"),
                         ["Woody Herman And His Orchestra Feat. Muriel Lane & Steady Nelson",
                          "Woody Herman And His Orchestra", "Woody Herman"])
        self.assertEqual(artist_variants("Mary Lou Williams & Andy Kirk & His Twelve Clouds of Joy"),
                         ["Mary Lou Williams & Andy Kirk & His Twelve Clouds of Joy", "Mary Lou Williams"])
        self.assertEqual(artist_variants("Andreas Hofschneider Quartett"),
                         ["Andreas Hofschneider Quartett", "Andreas Hofschneider"])
        self.assertEqual(artist_variants("Ethel Ennis"), ["Ethel Ennis"])

    def test_links_are_recognised_in_pasted_text_and_html(self):
        text = """
        <a href="https://someband.bandcamp.com/track/a-song?from=search&amp;search_item_id=1">x</a>
        https://music.customdomain.com/track/another-song,
        see https://www.youtube.com/watch?v=abcdefghijk and https://youtu.be/abcdefghijk
        https://archive.org/details/some_item_78rpm
        https://soundcloud.com/artist/track-name
        https://example.com/files/take3.mp3
        https://bandcamp.com/search?q=ignored  https://someband.bandcamp.com/album/not-a-track
        https://someband.bandcamp.com/track/a-song
        """
        got = candidates_from_text(text, "A Song")
        by_source = {}
        for c in got:
            by_source.setdefault(c.source, []).append(c.url)
        self.assertEqual(by_source["bandcamp"], ["https://someband.bandcamp.com/track/a-song",
                                                 "https://music.customdomain.com/track/another-song"])
        self.assertEqual(len(by_source["youtube"]), 2)
        self.assertEqual(by_source["archive"], ["https://archive.org/details/some_item_78rpm"])
        self.assertEqual(len(by_source["soundcloud"]), 1)
        self.assertEqual(by_source["file"], ["https://example.com/files/take3.mp3"])
        self.assertTrue(all(c.wanted == "A Song" for c in got))

    def test_cache_round_trip_and_version_guard(self):
        with tempfile.TemporaryDirectory() as d:
            cache = FeatureCache(d)
            feats = np.random.default_rng(0).normal(size=(8, 50)).astype(np.float32)
            self.assertIsNone(cache.get("k"))
            cache.put("k", feats, {"title": "T"})
            got, meta = cache.get("k")
            self.assertEqual(meta["title"], "T")
            np.testing.assert_allclose(got, feats, atol=2e-3)
            self.assertIsNone(FeatureCache(None).get("k"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
