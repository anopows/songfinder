"""Listening links for an identified recording, Spotify first.

Every link says how it is known to be the same recording:
  audio       its preview was compared with the matched recording (or the clip)
  isrc        same ISRC as the recogniser's answer; no preview to compare
  matched     the matcher confirmed the clip against this very audio
  recogniser  Shazam or AudD named this release
  search      a search page, used only when nothing better exists

Spotify tracks come from two places: a Spotify link a recogniser returns
(AudD does), and Spotify's official Web API when SPOTIFY_CLIENT_ID and
SPOTIFY_CLIENT_SECRET are set (a free app at developer.spotify.com). A Spotify
track whose preview contradicts the matched recording is never listed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
from typing import Callable

from .audio import DecodeError, decode
from .features import features
from .matcher import Query
from .net import fetch, fetch_json
from .recognise import clean_title
from .sources import artist_variants, clean_channel, title_matches, tokens

PLATFORM_ORDER = ["spotify", "apple_music", "bandcamp", "youtube", "deezer", "soundcloud",
                  "archive", "recogniser", "spotify_search"]
HOW_ORDER = ["audio", "isrc", "matched", "recogniser", "search"]
LABELS = {"spotify": "Spotify", "apple_music": "Apple Music", "bandcamp": "Bandcamp",
          "youtube": "YouTube", "deezer": "Deezer", "soundcloud": "SoundCloud",
          "archive": "Internet Archive", "recogniser": "Recogniser page",
          "spotify_search": "Spotify (search)"}
HOW_TEXT = {"audio": "preview audio matches", "isrc": "same ISRC, no preview to compare",
            "matched": "clip matched against this audio", "recogniser": "named by the recogniser",
            "search": "search page only"}
PREVIEW_MIN_SIM = 0.5     # a store preview must match at least this well to count as the same audio
MAX_SPOTIFY_CHECKS = 8
PER_PLATFORM = 2


def platform_of(url: str, source: str = "") -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    path = urllib.parse.urlsplit(url).path
    if host == "open.spotify.com":
        return "spotify" if path.startswith("/track/") else "spotify_search"
    if host.endswith(("music.apple.com", "itunes.apple.com")):
        return "apple_music"
    if host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if host.endswith("deezer.com"):
        return "deezer"
    if host.endswith("soundcloud.com"):
        return "soundcloud"
    if host.endswith("archive.org"):
        return "archive"
    if source == "bandcamp" or host.endswith("bandcamp.com"):
        return "bandcamp"
    return "recogniser"


def clean_url(url: str) -> str:
    """Drop tracking parameters, keeping the ones that name the item."""
    parts = urllib.parse.urlsplit(url)
    keep = {"music.apple.com": {"i"}, "itunes.apple.com": {"i"}, "www.youtube.com": {"v"},
            "youtube.com": {"v"}, "m.youtube.com": {"v"}}.get((parts.hostname or "").lower(), set())
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k in keep])
    return urllib.parse.urlunsplit(parts._replace(query=query, fragment=""))


def spotify_track_id(url: str) -> str | None:
    m = re.match(r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]{22})", url or "")
    if m:
        return m.group(1)
    m = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", url or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------- Spotify
class Spotify:
    """Spotify's official Web API with an app's client credentials."""
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.client_id = client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        self._token, self._expires = "", 0.0

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _bearer(self) -> str:
        if self._token and time.monotonic() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = fetch(self.TOKEN_URL, data=b"grant_type=client_credentials",
                     headers={"Authorization": f"Basic {basic}",
                              "Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(body)
        self._token = data["access_token"]
        self._expires = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token

    def search(self, isrc: str = "", title: str = "", artist: str = "", limit: int = 8,
               album: str = "") -> list[dict]:
        """By ISRC, then title with each simpler form of the credit, then title and album."""
        queries = []
        if isrc:
            queries.append(("isrc", f"isrc:{isrc}"))
        if title:
            for credit in artist_variants(artist) if artist else []:
                queries.append(("search", f'track:"{title}" artist:"{credit}"'))
            if album:
                queries.append(("search", f'track:"{title}" album:"{album}"'))
            if not artist and not album:
                queries.append(("search", f'track:"{title}"'))
        out: dict[str, dict] = {}
        for kind, q in queries:
            url = self.SEARCH_URL + "?" + urllib.parse.urlencode({"q": q, "type": "track", "limit": limit})
            data = fetch_json(url, headers={"Authorization": f"Bearer {self._bearer()}"})
            for t in (data.get("tracks") or {}).get("items") or []:
                if not t.get("id") or t["id"] in out:
                    continue
                if kind == "search" and title and not title_matches(title, t.get("name", "")):
                    continue
                t_isrc = (t.get("external_ids") or {}).get("isrc", "")
                out[t["id"]] = {"id": t["id"], "url": f"https://open.spotify.com/track/{t['id']}",
                                "title": t.get("name", ""),
                                "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
                                "album": (t.get("album") or {}).get("name", ""),
                                "isrc": t_isrc, "same_isrc": bool(isrc) and t_isrc == isrc,
                                "preview_url": t.get("preview_url") or ""}
        return list(out.values())


def spotify_preview(track_id: str) -> str:
    """The 30 s preview that Spotify's public embed player plays, if it has one."""
    page = fetch(f"https://open.spotify.com/embed/track/{track_id}").decode("utf-8", "replace")
    m = re.search(r'"audioPreview":\{"url":"(https://p\.scdn\.co/mp3-preview/[A-Za-z0-9]+)', page)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- verification
def preview_matches(preview_url: str, reference, clip: Query | None, reference_is_full: bool) -> str:
    """'yes', 'no' or 'unknown': is this preview the matched recording?

    The preview is compared with the matched reference (clean against clean) and,
    failing that, with the clip itself. A full-length reference that does not
    contain the preview means another recording; a preview-only reference may
    simply not overlap, so that case stays unknown.
    """
    try:
        pf = features(decode(preview_url, max_seconds=60))
    except DecodeError:
        return "unknown"
    reference_says_no = False
    if reference is not None:
        try:
            ev = Query(pf).evaluate(reference)
            v = ev.verification
            if ev.decision in ("exact", "probable") and v and v.similarity >= PREVIEW_MIN_SIM:
                return "yes"
            reference_says_no = reference_is_full
        except ValueError:
            pass
    if clip is not None:
        ev = clip.evaluate(pf)
        v = ev.verification
        if ev.decision in ("exact", "probable") and v and v.similarity >= PREVIEW_MIN_SIM:
            return "yes"
    return "no" if reference_says_no else "unknown"


# ------------------------------------------------------------------ assembling
def _hint_for(data: dict, top: dict) -> list[dict]:
    hints = data.get("shazam_hints") or []
    same = [h for h in hints if title_matches(clean_title(h.get("title", "")), top.get("title", ""))
            or title_matches(top.get("title", ""), clean_title(h.get("title", "")))]
    return same or hints[:1]


def build_links(data: dict, clip: Query | None, cache, spotify: Spotify | None = None,
                log: Callable[[str], None] = lambda s: None) -> dict:
    """Ordered listening links for the best exact result in a report, Spotify first."""
    results = [r for r in data.get("results", []) if not r.get("error")]
    exact = [r for r in results if r["decision"] == "exact"]
    if not exact:
        return {"links": [], "rejected": []}
    exact.sort(key=lambda r: (r["audio"] == "full", r["similarity"]), reverse=True)
    top = exact[0]
    hints = _hint_for(data, top)
    links: list[dict] = []
    rejected: list[dict] = []

    def add(platform: str, url: str, how: str, note: str = "") -> None:
        url = clean_url(url)
        if url and not any(l["url"] == url for l in links):
            links.append({"platform": platform, "label": LABELS[platform], "url": url, "how": how,
                          "note": note})

    # Audio the clip itself was matched against, plus other copies scoring as well.
    for r in results:
        if r["decision"] == "exact" or (r["decision"] == "probable"
                                        and r["similarity"] >= top["similarity"] - 0.15):
            p = platform_of(r["url"], r["source"])
            add(p, r["url"], "matched" if p != "recogniser" else "recogniser")

    # What the recognisers named, and Spotify candidates to check.
    spotify_cands: list[dict] = []
    isrc = ""
    for h in hints:
        isrc = isrc or h.get("isrc", "")
        for label, url in h.get("links") or []:
            if platform_of(url) == "apple_music" and not re.search(r"/(album|song)/", url):
                continue                             # e.g. Apple's "subscribe" page
            tid = spotify_track_id(url)
            if tid:
                spotify_cands.append({"id": tid, "url": f"https://open.spotify.com/track/{tid}",
                                      "same_isrc": False, "from": h.get("service", "recogniser")})
            else:
                add(platform_of(url), url, "search" if "search" in label.lower() else "recogniser")
    for pair in data.get("links") or []:                 # reports written before this module
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            tid = spotify_track_id(pair[1])
            if tid:
                spotify_cands.append({"id": tid, "url": f"https://open.spotify.com/track/{tid}",
                                      "same_isrc": False, "from": "recogniser"})
            else:
                add(platform_of(pair[1]), pair[1], "search" if "search" in pair[0].lower() else "recogniser")
    if spotify and spotify.available:
        raw_title = hints[0]["title"] if hints else top.get("title", "")
        title = clean_title(raw_title)
        artist = hints[0].get("artist", "") if hints else clean_channel(top.get("artist", ""))
        if not artist:                           # "Tea for Two (feat. Luca Filastro)" still names someone
            m = re.search(r"\b(?:feat\.?|featuring|ft\.)\s+([^\)\]]+)", raw_title, re.I)
            artist = m.group(1).strip() if m else ""
        album = top.get("album", "")
        try:
            found = spotify.search(isrc=isrc, title=title, artist=artist, album=album)
            lead = set(tokens(artist_variants(artist)[-1])) if artist else set()
            album_words = set(tokens(album))
            for c in found:                          # same album, then the named artist, first
                c["artist_overlap"] = len(lead & set(tokens(c.get("artist", ""))))
                c["same_album"] = bool(album_words) and album_words <= set(tokens(c.get("album", "")))
            log(f"  Spotify: {len(found)} candidate track(s)")
            spotify_cands += found
        except Exception as e:
            log(f"  Spotify search failed ({type(e).__name__}: {e})")

    # Check Spotify candidates against the audio, best leads first.
    reference = None
    hit = cache.get(f"{top['source']}:{top['url']}") if cache else None
    if hit:
        reference = hit[0]
    seen: set[str] = set()
    spotify_cands.sort(key=lambda c: (not c.get("same_album"), not c.get("same_isrc"),
                                      c.get("from") is None, -c.get("artist_overlap", 0)))
    checked = 0
    for c in spotify_cands:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if checked >= MAX_SPOTIFY_CHECKS:
            break
        checked += 1
        try:
            preview = c.get("preview_url") or spotify_preview(c["id"])
        except Exception:
            preview = ""
        verdict = preview_matches(preview, reference, clip, top["audio"] == "full") if preview else "unknown"
        label = f"{c.get('artist', '')} - {c.get('title', '')}".strip(" -")
        if verdict == "yes":
            add("spotify", c["url"], "audio", label)
        elif verdict == "no":
            rejected.append({"url": c["url"], "reason": "preview is a different recording", "track": label,
                             "same_isrc": bool(c.get("same_isrc"))})
        elif c.get("same_isrc"):
            add("spotify", c["url"], "isrc", label)
        elif c.get("from") in ("AudD", "Shazam", "recogniser"):
            add("spotify", c["url"], "recogniser", label)
        else:
            rejected.append({"url": c["url"], "reason": "no preview to compare", "track": label})
    if rejected:
        log(f"  Spotify: {len(rejected)} candidate(s) left out ({rejected[0]['reason']})")

    # The recogniser's ISRC names a release whose audio is not the matched recording:
    # then its other links (Apple Music, Deezer, its own page) point there too.
    if any(r.get("same_isrc") for r in rejected):
        links = [l for l in links if l["how"] != "recogniser"]
        log("  The recogniser's release is a different recording; its links are left out")

    # Spotify first, then by how well each link is established.
    links.sort(key=lambda l: (PLATFORM_ORDER.index(l["platform"]), HOW_ORDER.index(l["how"])))
    per: dict[str, int] = {}
    kept = []
    for l in links:
        per[l["platform"]] = per.get(l["platform"], 0) + 1
        if per[l["platform"]] <= PER_PLATFORM:
            kept.append(l)
    if any(l["platform"] == "spotify" for l in kept):
        kept = [l for l in kept if l["platform"] != "spotify_search"]
    return {"links": kept, "rejected": rejected}


def format_links(links: list[dict]) -> list[str]:
    return [f"  {l['label']:<17} {l['url']}   ({HOW_TEXT[l['how']]})" for l in links]
