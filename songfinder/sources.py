"""Catalogue connectors: find recordings of a title and resolve their audio.

Every source implements
    search(title, artist, limit) -> list[Candidate]
    resolve(candidate) -> stream URL or file path that FFmpeg can read
and can be switched off without touching the matcher.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from . import budget
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

from .net import fetch, fetch_json


class NoAudio(RuntimeError):
    pass


@dataclass
class Candidate:
    source: str
    url: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float | None = None
    released: str = ""
    audio_kind: str = "full"          # 'full' or 'preview'
    audio_url: str | None = None
    wanted: str = ""                  # the title we searched for
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.url}"

    @property
    def label(self) -> str:
        who = self.artist or "unknown artist"
        what = self.title or self.url
        return f"{who} - {what}"


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).split()


def clean_channel(name: str) -> str:
    """YouTube "Artist - Topic" channels name the artist; "Release - Topic" names nobody."""
    name = re.sub(r"\s+-\s+Topic$", "", (name or "").strip())
    return "" if name.lower() in ("release", "various artists - topic") else name


def artist_variants(credit: str) -> list[str]:
    """The credit as given, without featured players, and the lead name alone.

    "Woody Herman And His Orchestra Feat. Muriel Lane" -> [that, "Woody Herman And His
    Orchestra", "Woody Herman"]. Catalogues spell long credits differently, so a search
    with the full string alone often finds nothing.
    """
    credit = (credit or "").strip()
    out = [credit]
    base = re.split(r"\s*[\(\[]?\b(?:feat\.?|featuring|ft\.|with)\s", credit, maxsplit=1, flags=re.I)[0]
    out.append(base.strip(" ,-([&"))
    lead = re.split(r"\s+(?:and|&)\s+(?:his|her|their|the)\b|\s*[,/&+]\s*|\s+and\s+|\s+x\s+", base,
                    maxsplit=1, flags=re.I)[0]
    lead = re.sub(r"\b(?:orchestra|quartet+e?|quintet+e?|trio|sextet+e?|band|big band)$", "", lead,
                  flags=re.I).strip(" ,-")
    out.append(lead)
    seen, result = set(), []
    for v in out:
        if v and len(v) >= 3 and v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)
    return result


def title_matches(wanted: str, got: str) -> bool:
    w, g = tokens(wanted), set(tokens(got))
    return bool(w) and all(t in g for t in w)


# ------------------------------------------------------------------ Bandcamp
class Bandcamp:
    name, audio_kind = "bandcamp", "full"
    SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

    def search(self, title: str, artist: str | None = None, limit: int = 150) -> list[Candidate]:
        # The JSON endpoint returns at most 50 rows per query and has no paging.
        # Different phrasings rank differently, so their union reaches further.
        queries = [title, f'"{title}"']
        if artist:
            queries.insert(0, f"{title} {artist}")
        else:
            queries += [f"{title} {word}" for word in
                        ('the','and','band','jazz','live','trio','quartet','orchestra','swing','hot','six')]
        # fetch_json uses the shared per-host request spacing in net.py.
        seen: dict[str, Candidate] = {}
        for q in queries:
            data = fetch_json(self.SEARCH, {"search_text": q, "search_filter": "t",
                                            "full_page": True, "fan_id": None})
            for r in data.get("auto", {}).get("results", []):
                url = r.get("item_url_path") or ""
                if r.get("type") != "t" or not url or url in seen:
                    continue
                if not title_matches(title, r.get("name", "")):
                    continue
                seen[url] = Candidate(self.name, url, r.get("name", ""), r.get("band_name", ""),
                                      r.get("album_name") or "", wanted=title)
        return list(seen.values())[:limit]

    def from_url(self, url: str, wanted: str = "") -> Candidate:
        return Candidate(self.name, url.split("?")[0].split("#")[0], wanted=wanted)

    def resolve(self, c: Candidate) -> str:
        page = fetch(c.url).decode("utf-8", "replace")
        m = re.search(r'data-tralbum="([^"]+)"', page)
        if not m:
            raise NoAudio("no track data on page")
        data = json.loads(html.unescape(m.group(1)))
        tracks = data.get("trackinfo") or []
        if not tracks:
            raise NoAudio("no tracks on page")
        t = tracks[0]
        stream = (t.get("file") or {}).get("mp3-128")
        c.title = c.title or t.get("title", "")
        c.artist = t.get("artist") or data.get("artist") or c.artist
        c.duration = t.get("duration") or c.duration
        ld = re.search(r'<script type="application/ld\+json">\s*(.*?)</script>', page, re.S)
        if ld:
            try:
                j = json.loads(ld.group(1))
                c.album = c.album or (j.get("inAlbum") or {}).get("name", "")
                c.released = c.released or (j.get("datePublished") or "")[:11].strip()
            except (ValueError, AttributeError):
                pass
        if not stream:
            raise NoAudio("track is not streamable")
        return stream


# ---------------------------------------------------------- Internet Archive
class Archive:
    name, audio_kind = "archive", "full"
    AUDIO_EXT = (".mp3", ".ogg", ".flac", ".m4a", ".wav")

    def search(self, title: str, artist: str | None = None, limit: int = 100) -> list[Candidate]:
        q = f'title:("{title}") AND mediatype:(audio)'
        if artist:
            q += f' AND creator:("{artist}")'
        params = [("q", q), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "creator"),
                  ("fl[]", "date"), ("sort[]", "downloads desc"), ("rows", str(limit)),
                  ("output", "json")]
        data = fetch_json("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params))
        out = []
        for d in data.get("response", {}).get("docs", []):
            creator = d.get("creator", "")
            if isinstance(creator, list):
                creator = ", ".join(creator)
            name = d.get("title", "")
            if isinstance(name, list):
                name = name[0]
            out.append(Candidate(self.name, f"https://archive.org/details/{d['identifier']}",
                                 name, creator, released=str(d.get("date", ""))[:10], wanted=title))
        return out

    def from_url(self, url: str, wanted: str = "") -> Candidate:
        ident = url.split("/details/")[1].split("/")[0].split("?")[0]
        return Candidate(self.name, f"https://archive.org/details/{ident}", wanted=wanted)

    @staticmethod
    def _seconds(value) -> float | None:
        try:
            parts = [float(p) for p in str(value).split(":")]
        except ValueError:
            return None
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))

    def resolve(self, c: Candidate) -> str:
        ident = c.url.rsplit("/", 1)[1]
        meta = fetch_json(f"https://archive.org/metadata/{ident}")
        md = meta.get("metadata", {})
        c.title = c.title or str(md.get("title", ""))
        files = [f for f in meta.get("files", []) if f.get("name", "").lower().endswith(self.AUDIO_EXT)]
        if not files:
            raise NoAudio("item has no audio files")
        named = [f for f in files
                 if title_matches(c.wanted, f.get("title", "") + " " + re.sub(r"[_\-.]", " ", f["name"]))]
        stems = {os.path.splitext(f["name"])[0] for f in files}
        pool = named or (files if len(stems) <= 3 else [])
        if not pool:
            raise NoAudio("no file in this item matches the title")
        pool.sort(key=lambda f: (not f["name"].lower().endswith(".mp3"), len(f["name"])))
        pick = pool[0]
        c.duration = self._seconds(pick.get("length")) or c.duration
        return f"https://archive.org/download/{ident}/{urllib.parse.quote(pick['name'])}"


# ------------------------------------------------ YouTube / SoundCloud (yt-dlp)
class YtDlp:
    audio_kind = "full"

    def __init__(self, name: str, prefix: str):
        self.name, self.prefix = name, prefix

    @staticmethod
    def available() -> bool:
        return shutil.which("yt-dlp") is not None

    def search(self, title: str, artist: str | None = None, limit: int = 40) -> list[Candidate]:
        if not self.available():
            raise NoAudio("yt-dlp is not installed")
        # A quoted query reaches niche releases that a loose one buries; the loose one adds reach.
        queries = [f'"{title}" "{artist}"', f"{title} {artist}"] if artist else [title]
        out: dict[str, Candidate] = {}
        last_error = ""
        for query in queries:
            p = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-single-json", "--no-warnings",
                                f"{self.prefix}{limit}:{query}"], capture_output=True, timeout=budget.timeout(120))
            if p.returncode != 0:
                err = p.stderr.decode("utf-8", "replace").strip().splitlines()
                last_error = err[-1] if err else "search failed"
                continue
            for e in json.loads(p.stdout).get("entries") or []:
                url = e.get("webpage_url") or e.get("url")
                dur = e.get("duration")
                name = e.get("title") or ""
                if not url or url in out or not title_matches(title, name):
                    continue
                if dur is not None and not 45 <= dur <= 900:
                    continue                       # shorts and long mixes
                channel = clean_channel(e.get("channel") or e.get("uploader") or "")
                out[url] = Candidate(self.name, url, name, channel, duration=dur, wanted=title)
        if not out and last_error:
            raise NoAudio(last_error)
        return list(out.values())

    def from_url(self, url: str, wanted: str = "") -> Candidate:
        return Candidate(self.name, url, wanted=wanted)

    def resolve(self, c: Candidate) -> bytes:
        """Return the encoded audio itself, held in memory only.

        The stream URLs these sites hand out refuse other clients, so yt-dlp
        has to do the download; it writes to a pipe, never to disk.
        """
        if not self.available():
            raise NoAudio("yt-dlp is not installed")
        if not c.title:                              # links given by the user carry no metadata
            m = subprocess.run(["yt-dlp", "--no-warnings", "--no-playlist", "--skip-download",
                                "--print", "%(title)s\t%(uploader)s\t%(duration)s", c.url],
                               capture_output=True, timeout=budget.timeout(90))
            parts = m.stdout.decode("utf-8", "replace").strip().rsplit("\t", 2)
            if len(parts) == 3:
                c.title, c.artist = parts[0], c.artist or parts[1]
                try:
                    c.duration = float(parts[2])
                except ValueError:
                    pass
        p = subprocess.run(["yt-dlp", "-f", "bestaudio[ext=webm]/bestaudio/best", "--no-warnings",
                            "--no-playlist", "--no-progress", "--max-filesize", "40M",
                            "-o", "-", c.url], capture_output=True, timeout=budget.timeout(240))
        if p.returncode != 0 or len(p.stdout) < 20000:
            err = p.stderr.decode("utf-8", "replace").strip().splitlines()
            raise NoAudio(err[-1] if err else "no audio returned")
        return p.stdout


# ------------------------------------------------------ preview-only sources
class Deezer:
    name, audio_kind = "deezer", "preview"

    def search(self, title: str, artist: str | None = None, limit: int = 100) -> list[Candidate]:
        q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
        out, index = [], 0
        while len(out) < limit:
            url = "https://api.deezer.com/search/track?" + urllib.parse.urlencode(
                {"q": q, "limit": 100, "index": index})
            data = fetch_json(url)
            rows = data.get("data") or []
            for r in rows:
                if r.get("preview") and title_matches(title, r.get("title", "")):
                    out.append(Candidate(self.name, r.get("link", ""), r.get("title", ""),
                                         (r.get("artist") or {}).get("name", ""),
                                         (r.get("album") or {}).get("title", ""),
                                         r.get("duration"), audio_kind="preview",
                                         audio_url=r["preview"], wanted=title))
            if not data.get("next") or not rows:
                break
            index += 100
        return out[:limit]

    def resolve(self, c: Candidate) -> str:
        if not c.audio_url:
            raise NoAudio("no preview")
        return c.audio_url

    def by_isrc(self, isrc: str, wanted: str = "") -> Candidate | None:
        """The exact recording an ISRC names, with its Deezer link and preview."""
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", isrc or ""):
            return None
        r = fetch_json(f"https://api.deezer.com/track/isrc:{isrc}")
        if not r or r.get("error") or not r.get("link"):
            return None
        return Candidate(self.name, r["link"], r.get("title", ""), (r.get("artist") or {}).get("name", ""),
                         (r.get("album") or {}).get("title", ""), r.get("duration"),
                         r.get("release_date", ""), audio_kind="preview",
                         audio_url=r.get("preview") or None, wanted=wanted, extra={"isrc": isrc})


class ITunes:
    name, audio_kind = "itunes", "preview"

    def search(self, title: str, artist: str | None = None, limit: int = 200) -> list[Candidate]:
        term = f"{title} {artist}" if artist else title
        params = {"term": term, "entity": "song", "limit": min(limit, 200)}
        if not artist:
            params["attribute"] = "songTerm"
        data = fetch_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(params))
        out = []
        for r in data.get("results", []):
            if r.get("previewUrl") and title_matches(title, r.get("trackName", "")):
                ms = r.get("trackTimeMillis")
                out.append(Candidate(self.name, r.get("trackViewUrl", ""), r.get("trackName", ""),
                                     r.get("artistName", ""), r.get("collectionName", ""),
                                     ms / 1000 if ms else None, (r.get("releaseDate") or "")[:10],
                                     audio_kind="preview", audio_url=r["previewUrl"], wanted=title))
        return out

    def resolve(self, c: Candidate) -> str:
        if not c.audio_url:
            raise NoAudio("no preview")
        return c.audio_url


class Direct:
    """A local file or a direct audio URL supplied by the user."""
    name, audio_kind = "file", "full"

    def search(self, *a, **k) -> list[Candidate]:
        return []

    def from_url(self, url: str, wanted: str = "") -> Candidate:
        return Candidate(self.name, url, title=os.path.basename(urllib.parse.urlsplit(url).path),
                         audio_url=url, wanted=wanted)

    def resolve(self, c: Candidate) -> str:
        return c.audio_url or c.url


class Hinted(Direct):
    """The 30 s store preview that a recogniser attached to its answer."""
    name, audio_kind = "shazam", "preview"


SOURCES = {
    "shazam": Hinted(),
    "bandcamp": Bandcamp(),
    "archive": Archive(),
    "youtube": YtDlp("youtube", "ytsearch"),
    "soundcloud": YtDlp("soundcloud", "scsearch"),
    "deezer": Deezer(),
    "itunes": ITunes(),
    "file": Direct(),
}
DEFAULT_SOURCES = ["bandcamp", "archive", "youtube", "soundcloud", "deezer", "itunes"]

_URL = re.compile(r"""https?://[^\s"'<>)\]]+""")
_AUDIO_FILE = re.compile(r"\.(mp3|m4a|flac|ogg|opus|wav|aac)$", re.I)


def candidate_from_ref(ref: str, wanted: str = "") -> Candidate | None:
    """Recognise one URL or local path as a candidate, or return None."""
    if not ref.startswith(("http://", "https://")):
        return SOURCES["file"].from_url(ref, wanted) if os.path.isfile(ref) else None
    parts = urllib.parse.urlsplit(ref)
    host, path = (parts.hostname or "").lower(), parts.path
    if "/track/" in path and "archive.org" not in host and "youtube" not in host:
        return SOURCES["bandcamp"].from_url(ref, wanted)       # includes custom Bandcamp domains
    if host.endswith("archive.org") and "/details/" in path:
        return SOURCES["archive"].from_url(ref, wanted)
    if host.endswith(("youtube.com", "youtu.be")) and ("watch" in path or host.endswith("youtu.be")):
        return SOURCES["youtube"].from_url(ref, wanted)
    if host.endswith("soundcloud.com") and path.count("/") >= 2:
        return SOURCES["soundcloud"].from_url(ref, wanted)
    if _AUDIO_FILE.search(path):
        return SOURCES["file"].from_url(ref, wanted)
    return None


def candidates_from_text(text: str, wanted: str = "") -> list[Candidate]:
    """Pull every recognisable track link out of pasted text or saved HTML."""
    seen: dict[str, Candidate] = {}
    for raw in _URL.findall(html.unescape(text)):
        c = candidate_from_ref(raw.rstrip(".,;"), wanted)
        if c and c.key not in seen:
            seen[c.key] = c
    return list(seen.values())
