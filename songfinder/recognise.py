"""Fast path: ask Shazam for a title, trying speed-corrected versions of the clip.

Opt-in only. The fingerprint is computed on this machine by the `shazamio`
library and only that fingerprint is sent, never the audio. A hit is treated
as a title hint: the matcher still has to confirm it against real audio.
"""
from __future__ import annotations

import asyncio
import io
import time
import re
import subprocess
import wave
from dataclasses import dataclass
from typing import Callable
from . import budget

SNIPPET_S = 14.0
# Clip speeds to undo, most likely first. Shazam tolerates a few percent by itself.
RATES = [1.0, 0.95, 0.90, 0.85, 1.05, 0.80, 1.10, 0.75]
# Measured: Shazam accepts about four requests in quick succession, then refuses for up
# to two minutes. Spacing requests out avoids most refusals; the waits cover the rest.
THROTTLE_WAITS_S = (120.0, 180.0, 300.0)  # back-off after each refusal (HTTP 429)
MIN_GAP_S = 15.0                          # between any two requests, across clips
COOLDOWN_S = 600.0                        # after Shazam gives up on us, leave it alone this long
ANSWER_DIR: str | None = None             # set by the CLI; recogniser answers are kept here
REQUEST_GATE = None                     # web worker injects persistent shared limits
REQUEST_BLOCK = None
ANSWER_MAX_AGE_S = 30 * 86400
_last_request = 0.0
_blocked_until = 0.0


@dataclass
class Hit:
    title: str
    artist: str
    url: str                 # the recogniser's page for the track
    preview_url: str         # store preview (often 90 s now), if Shazam lists one
    mode: str                # how the clip was corrected: 'stretch' or 'resample'
    rate: float              # clip speed assumed for that correction
    clip_start: float
    hits: int = 1
    isrc: str = ""
    links: list = None       # [(label, url)] for listening: Apple Music, Spotify search, Shazam
    service: str = "Shazam"


def _answer_path(service: str, payload: bytes) -> str | None:
    if not ANSWER_DIR:
        return None
    import hashlib
    import os
    return os.path.join(ANSWER_DIR, service, hashlib.sha1(payload).hexdigest() + ".json")


def cached_answer(service: str, payload: bytes):
    """A previous answer for exactly this snippet, so re-runs send nothing."""
    import json as _json
    import os
    path = _answer_path(service, payload)
    if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < ANSWER_MAX_AGE_S:
        try:
            with open(path, encoding="utf-8") as fh:
                return _json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def store_answer(service: str, payload: bytes, answer) -> None:
    import json as _json
    import os
    path = _answer_path(service, payload)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(answer, fh)
    os.replace(tmp, path)


def available() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except ImportError:
        return False


def clean_title(title: str) -> str:
    """Drop '(Remastered 2009)', '[Live]', '- Single Version' and similar tails."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    t = re.sub(r"\s+-\s+.*(remaster|version|mono|stereo|edit|live|take).*$", "", t, flags=re.I)
    return t.strip() or title.strip()


def _variant(clip: str, start: float, mode: str, rate: float, seconds: float = SNIPPET_S) -> bytes:
    """A snippet of the clip with its speed undone, as WAV bytes."""
    f = 1.0 / rate
    if abs(f - 1) < 1e-6:
        af = "anull"
    elif mode == "stretch":                 # tempo only, pitch untouched
        af = f"atempo={f:.5f}"
    else:                                   # tempo and pitch together
        af = f"asetrate={44100 * f:.1f},aresample=44100"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{seconds * f:.2f}",
           "-i", clip, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100", "-af", af,
           "-f", "s16le", "pipe:1"]
    pcm = subprocess.run(cmd, capture_output=True, timeout=budget.timeout(60)).stdout
    buf = io.BytesIO()                      # a piped WAV has no length header; build a proper one
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm)
    return buf.getvalue()


def _apple_link(url: str) -> str:
    """Drop affiliate and tracking parameters but keep `i`, which names the track."""
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k == "i"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(keep)))


def _parse(answer: dict) -> dict | None:
    track = (answer or {}).get("track")
    if not track or not track.get("title"):
        return None
    hub = track.get("hub") or {}
    preview = ""
    for action in hub.get("actions") or []:
        if action.get("type") == "uri" and str(action.get("uri", "")).startswith("http"):
            preview = action["uri"]
    links = []
    for option in hub.get("options") or []:              # "OPEN IN" Apple Music
        for action in option.get("actions") or []:
            uri = str(action.get("uri", ""))
            if ("music.apple.com" in uri and re.search(r"/(album|song)/", uri)
                    and not any(l[0] == "Apple Music" for l in links)):
                links.append(("Apple Music", _apple_link(uri)))
    for provider in hub.get("providers") or []:
        for action in provider.get("actions") or []:
            uri = str(action.get("uri", ""))
            if uri.startswith("spotify:search:"):
                links.append(("Spotify (search)", "https://open.spotify.com/search/" + uri.split(":", 2)[2]))
    if track.get("url"):
        links.append(("Shazam", track["url"]))
    return {"title": track["title"], "artist": track.get("subtitle", ""), "url": track.get("url", ""),
            "preview": preview, "isrc": track.get("isrc") or "", "links": links}


async def _run(clip: str, duration: float, rates: list[float], max_calls: int, pause: float,
               log: Callable[[str], None], patience: int = len(THROTTLE_WAITS_S), on_issue=None, remaining=None, on_attempt=None) -> list[Hit]:
    from aiohttp_retry import ExponentialRetry
    from shazamio import HTTPClient, Shazam
    from shazamio.exceptions import BadResponseStatus
    # The library's default retries a throttled request for many minutes without saying so.
    # Fail fast instead and handle throttling here, where it can be reported.
    shazam = Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(
        attempts=2, max_timeout=5, statuses={500, 502, 503, 504})))
    throttled = 0
    span = max(duration - SNIPPET_S * 1.4, 0.0)
    # At least two positions: one snippet can be spoilt by a teacher talking over it.
    fractions = (0.5, 0.1, 0.9) if span > 20 else (0.5, 0.0, 1.0) if span > 4 else (0.5,)
    starts = [span * p for p in fractions]
    plan = []
    for rate in rates:
        if rate == 1.0:
            plan += [("stretch", 1.0, s) for s in starts]
        else:                               # one position per speed keeps the request count low
            plan += [(mode, rate, starts[0]) for mode in ("stretch", "resample")]
    hits: dict[tuple[str, str], Hit] = {}
    calls = after_first_hit = 0
    try:
        for mode, rate, start in plan:
            budget.check()
            if calls >= max_calls or after_first_hit >= 4:
                break                           # the matcher verifies; no need to keep asking
            after_first_hit += bool(hits)
            data = _variant(clip, start, mode, rate)
            if len(data) < 50000:
                continue
            answer, gave_up = cached_answer("shazam", data), False
            if answer is not None and on_attempt:on_attempt("cache_hits")
            while answer is None:               # retry this same snippet after a refusal
                if calls >= max_calls:
                    gave_up = True
                    break
                if REQUEST_GATE and not await asyncio.to_thread(REQUEST_GATE, "shazam"):
                    if on_issue:on_issue("shazam","budget")
                    log("  Recognition budget reached; continuing with other sources")
                    gave_up = True
                    break
                global _last_request
                wait = _last_request + MIN_GAP_S - time.monotonic()
                if wait > 0:
                    if remaining and wait>=remaining():
                        if on_issue:on_issue('shazam','budget')
                        gave_up=True;break
                    await asyncio.sleep(wait)
                _last_request = time.monotonic()
                calls += 1
                if on_attempt:on_attempt("calls")
                try:
                    answer = await asyncio.wait_for(shazam.recognize(data), min(60,max(.01,remaining())) if remaining else 60)
                    if on_attempt:on_attempt("responses")
                    store_answer("shazam", data, answer)
                    break
                except BadResponseStatus as e:  # almost always HTTP 429: too many requests
                    if on_issue:on_issue('shazam','throttled')
                    if REQUEST_BLOCK:
                        REQUEST_BLOCK("shazam", 120 if throttled == 0 else 300)
                    if throttled >= patience and patience < len(THROTTLE_WAITS_S):
                        log("  Shazam is throttling; leaving it for a later stage")
                        gave_up = True
                        break
                    if throttled >= len(THROTTLE_WAITS_S):
                        global _blocked_until
                        _blocked_until = time.monotonic() + COOLDOWN_S
                        log(f"  Shazam keeps refusing; not asking it again for {COOLDOWN_S / 60:.0f} minutes")
                        gave_up = True
                        break
                    if remaining and THROTTLE_WAITS_S[throttled]>=remaining():
                        log('  Throttle wait exceeds the remaining search budget; moving on')
                        gave_up=True;break
                    log(f"  Shazam is throttling; waiting {THROTTLE_WAITS_S[throttled]:.0f} s")
                    await asyncio.sleep(THROTTLE_WAITS_S[throttled])
                    throttled += 1
                except budget.Expired:
                    raise
                except Exception as e:          # network trouble: stop asking
                    if on_issue:on_issue('shazam','unavailable')
                    log(f"  Shazam request failed ({type(e).__name__}: {e}); stopping")
                    gave_up = True
                    break
            if gave_up:
                break
            got = _parse(answer)
            if got:
                title, artist = got["title"], got["artist"]
                key = (title.lower(), artist.lower())
                if key in hits:
                    hits[key].hits += 1
                else:
                    hits[key] = Hit(title, artist, got["url"], got["preview"], mode, rate, start,
                                    isrc=got["isrc"], links=got["links"])
                    skew = ""
                    try:                        # Shazam reports how far off speed the match was
                        m = answer["matches"][0]
                        skew = f", skew time {m['timeskew'] * 100:+.1f}% freq {m['frequencyskew'] * 100:+.1f}%"
                    except (KeyError, IndexError, TypeError):
                        pass
                    log(f"  Shazam: \"{title}\" by {artist}  "
                        f"(clip at {start:.0f} s, corrected for {rate * 100:.0f}% speed by {mode}{skew})")
                if hits[key].hits >= 2 or rate == 1.0:
                    break                       # confirmed twice, or matched unmodified
    finally:
        try:
            await shazam.close()
            await shazam.http_client.close()
        except Exception:
            pass
    log(f"  {calls} fingerprint(s) sent, {len(hits)} distinct answer(s)")
    return sorted(hits.values(), key=lambda h: -h.hits), calls


def time_remaining(remaining=None):
    return budget.timeout(remaining() if remaining else 60)


def recognise(clip: str, duration: float, rates: list[float] | None = None, max_calls: int = 40,
              pause: float = 2.5, patience: int = len(THROTTLE_WAITS_S),
              log: Callable[[str], None] = lambda s: None, on_issue=None, remaining=None, on_attempt=None) -> tuple[list[Hit], int]:
    """Ask Shazam about the clip, assuming each of `rates` in turn (default: all of RATES).

    Returns the distinct answers, most repeated first, and how many fingerprints were sent.
    """
    if not available():
        raise RuntimeError("The Shazam fast path needs the optional package: pip install shazamio")
    left = _blocked_until - time.monotonic()
    if left > 0:
        if on_issue:on_issue('shazam','throttled')
        log(f"  Shazam refused recently; skipping it for another {left / 60:.0f} minute(s)")
        return [], 0
    return asyncio.run(_run(clip, duration, list(rates or RATES), max_calls, pause, log, patience,on_issue,remaining,on_attempt))


# ------------------------------------------------------------------------- AudD
AUDD_URL = "https://api.audd.io/"
AUDD_SNIPPET_S = 15.0


def _audd_hit(result: dict, mode: str, rate: float, start: float) -> Hit | None:
    if not result or not result.get("title"):
        return None
    apple = result.get("apple_music") or {}
    deezer = result.get("deezer") or {}
    spotify = result.get("spotify") or {}
    preview = ""
    for p in apple.get("previews") or []:
        preview = preview or p.get("url", "")
    preview = preview or deezer.get("preview", "")
    links = []
    if apple.get("url"):
        links.append(("Apple Music", _apple_link(apple["url"])))
    if (spotify.get("external_urls") or {}).get("spotify"):
        links.append(("Spotify", spotify["external_urls"]["spotify"]))
    if deezer.get("link"):
        links.append(("Deezer", deezer["link"]))
    if result.get("song_link"):
        links.append(("AudD", result["song_link"]))
    isrc = apple.get("isrc") or deezer.get("isrc") or (spotify.get("external_ids") or {}).get("isrc") or ""
    return Hit(result["title"], result.get("artist", ""), result.get("song_link", ""), preview,
               mode, rate, start, isrc=isrc, links=links, service="AudD")


def recognise_audd(clip: str, duration: float, rates: list[float] | None = None, max_calls: int = 3,
                   token: str | None = None, log: Callable[[str], None] = lambda s: None,
                   on_issue=None, remaining=None, on_attempt=None
                   ) -> tuple[list[Hit], int]:
    """Ask AudD, a second recogniser with a different catalogue.

    Unlike Shazam it receives a short audio excerpt. Without an API token
    (env AUDD_API_TOKEN) it allows only a handful of requests a day, so few are sent.
    """
    import json as _json
    import os
    import urllib.request
    import uuid
    token = token or os.environ.get("AUDD_API_TOKEN", "")
    span = max(duration - AUDD_SNIPPET_S * 1.4, 0.0)
    plan = [("stretch", 1.0, span * 0.5)]
    for rate in (rates or [])[:max(0, max_calls - 1)]:
        if rate != 1.0:
            plan.append(("resample", rate, span * 0.5))
    hits, calls = [], 0
    for mode, rate, start in plan[:max_calls]:
        wav = _variant(clip, start, mode, rate, seconds=AUDD_SNIPPET_S)
        boundary = uuid.uuid4().hex
        fields = {"return": "apple_music,spotify,deezer"}
        if token:
            fields["api_token"] = token
        body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                        for k, v in fields.items())
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
                 f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(AUDD_URL, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "songfinder/0.1"})
        answer = cached_answer("audd", wav)
        if answer is not None and on_attempt:on_attempt("cache_hits")
        if answer is None:
            if REQUEST_GATE and not REQUEST_GATE("audd"):
                if on_issue:on_issue("audd","budget")
                log("  Recognition budget reached; continuing without an excerpt")
                break
            if remaining and remaining()<=1:
                if on_issue:on_issue('audd','budget')
                break
            calls += 1
            if on_attempt:on_attempt("calls")
            try:
                with urllib.request.urlopen(req, timeout=min(60,max(1,remaining())) if remaining else 60) as resp:
                    answer = _json.loads(resp.read())
                if on_attempt:on_attempt("responses")
            except budget.Expired:
                raise
            except Exception as e:
                if on_issue:on_issue('audd','unavailable')
                log(f"  AudD request failed ({type(e).__name__}: {e})")
                break
            if answer.get("status") == "success":
                store_answer("audd", wav, answer)
        if answer.get("status") != "success":
            if on_issue:on_issue('audd','refused')
            log(f"  AudD refused: {str(answer.get('error'))[:120]}")
            break
        h = _audd_hit(answer.get("result"), mode, rate, start)
        if on_attempt:on_attempt('hits' if h else 'no_match')
        if h:
            log(f"  AudD: \"{h.title}\" by {h.artist}  (clip at {start:.0f} s, "
                f"corrected for {rate * 100:.0f}% speed by {mode})")
            hits.append(h)
            break
    log(f"  {calls} excerpt(s) sent to AudD, {len(hits)} answer(s)")
    return hits, calls
