"""Listening-link tests: Spotify first, and only Spotify tracks whose audio fits."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np
import scipy.io.wavfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from songfinder.cache import FeatureCache  # noqa: E402
from songfinder.features import features  # noqa: E402
from songfinder.links import build_links, clean_url, platform_of, spotify_track_id  # noqa: E402
from songfinder.matcher import Query  # noqa: E402

SR = synth.SR
SPOTIFY_A = "https://open.spotify.com/track/" + "A" * 22
SPOTIFY_B = "https://open.spotify.com/track/" + "B" * 22
SPOTIFY_C = "https://open.spotify.com/track/" + "C" * 22


class FakeSpotify:
    available = True

    def __init__(self, items):
        self.items = items

    def search(self, isrc="", title="", artist="", limit=8, album=""):
        return self.items


class LinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        score = synth.make_score(seed=1)
        cls.reference = synth.render(score, bpm=180)
        other = synth.render(synth.make_score(seed=42), bpm=176)

        def wav(name, y):
            path = os.path.join(d, name)
            scipy.io.wavfile.write(path, SR, (y * 32000).astype(np.int16))
            return path
        cls.same_preview = wav("same.wav", cls.reference[int(30 * SR):int(60 * SR)])
        cls.other_preview = wav("other.wav", other[int(30 * SR):int(60 * SR)])
        cls.cache = FeatureCache(os.path.join(d, "cache"))
        cls.ref_url = "https://someband.bandcamp.com/track/a-song"
        cls.cache.put("bandcamp:" + cls.ref_url, features(cls.reference), {"title": "A Song"})
        clip = synth.room_recording(cls.reference[int(10 * SR):int(50 * SR)])
        cls.query = Query(features(clip))
        cls.data = {
            "results": [{"source": "bandcamp", "url": cls.ref_url, "decision": "exact", "audio": "full",
                         "similarity": 0.8, "title": "A Song", "artist": "Some Band"}],
            "shazam_hints": [{"title": "A Song", "artist": "Some Band", "isrc": "USABC1234567",
                              "service": "Shazam",
                              "links": [["Apple Music", "https://music.apple.com/gb/album/x/1?i=2&mttnsub1=Shazam"],
                                        ["Spotify (search)", "https://open.spotify.com/search/A%20Song"],
                                        ["Shazam", "https://www.shazam.com/track/1/a-song"]]}],
        }

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_url_helpers(self):
        self.assertEqual(platform_of("https://open.spotify.com/track/" + "x" * 22), "spotify")
        self.assertEqual(platform_of("https://open.spotify.com/search/abc"), "spotify_search")
        self.assertEqual(platform_of("https://music.custom.com/track/x", "bandcamp"), "bandcamp")
        self.assertEqual(clean_url("https://music.apple.com/gb/album/x/1?i=2&mttnsub1=Shazam"),
                         "https://music.apple.com/gb/album/x/1?i=2")
        self.assertEqual(clean_url("https://www.youtube.com/watch?v=abc&t=5s"), "https://www.youtube.com/watch?v=abc")
        self.assertEqual(spotify_track_id("spotify:track:" + "Q" * 22), "Q" * 22)
        self.assertIsNone(spotify_track_id("https://open.spotify.com/search/abc"))

    def test_spotify_first_only_when_its_audio_fits(self):
        fake = FakeSpotify([
            {"id": "B" * 22, "url": SPOTIFY_B, "title": "A Song", "artist": "Cover Band",
             "same_isrc": False, "preview_url": self.other_preview},
            {"id": "A" * 22, "url": SPOTIFY_A, "title": "A Song", "artist": "Some Band",
             "same_isrc": False, "preview_url": self.same_preview},
        ])
        out = build_links(self.data, self.query, self.cache, fake)
        links = out["links"]
        self.assertEqual(links[0]["url"], SPOTIFY_A)
        self.assertEqual(links[0]["how"], "audio")
        self.assertNotIn(SPOTIFY_B, [l["url"] for l in links])
        self.assertEqual(out["rejected"][0]["url"], SPOTIFY_B)
        self.assertEqual([l["platform"] for l in links][:3], ["spotify", "apple_music", "bandcamp"])
        self.assertNotIn("spotify_search", [l["platform"] for l in links])

    def test_same_isrc_without_preview_is_kept_and_labelled(self):
        fake = FakeSpotify([{"id": "C" * 22, "url": SPOTIFY_C, "title": "A Song", "artist": "Some Band",
                             "same_isrc": True, "preview_url": "/nonexistent/preview.mp3"}])
        links = build_links(self.data, self.query, self.cache, fake)["links"]
        self.assertEqual((links[0]["url"], links[0]["how"]), (SPOTIFY_C, "isrc"))

    def test_recogniser_links_dropped_when_its_isrc_track_is_another_recording(self):
        fake = FakeSpotify([{"id": "B" * 22, "url": SPOTIFY_B, "title": "A Song", "artist": "Some Band",
                             "same_isrc": True, "preview_url": self.other_preview}])
        out = build_links(self.data, self.query, self.cache, fake)
        platforms = [l["platform"] for l in out["links"]]
        self.assertEqual(platforms[0], "bandcamp")
        self.assertNotIn("apple_music", platforms)       # it named the other release
        self.assertNotIn("spotify", platforms)

    def test_without_spotify_the_search_link_comes_last(self):
        links = build_links(self.data, self.query, self.cache, None)["links"]
        self.assertEqual(links[0]["platform"], "apple_music")
        self.assertEqual(links[-1]["platform"], "spotify_search")


if __name__ == "__main__":
    unittest.main(verbosity=2)
