"""Visitor-safe result formatting and progress messages; no server or provider I/O."""
import re
from urllib.parse import urlsplit

STEPS = [
    (re.compile(r"^Asking (Shazam|AudD)"), "Checking the big music catalogues"),
    (re.compile(r"local index"), "Comparing with recordings we already know"),
    (re.compile(r"^Confirmed against a preview"), "Looking for a full-length link"),
    (re.compile(r"^Searching "), "Searching for versions of the song"),
    (re.compile(r"^Checking \d+ candidates"), "Checking candidate recordings"),
]

def _speed(rate: float, pitch: float) -> dict:
    if abs(rate - 1) < 0.015:
        return {"big": "Original speed", "note": "", "tip": ""}
    word = "Slowed down" if rate < 1 else "Sped up"
    how = "same pitch" if abs(pitch) < 0.3 else "pitch changed too"
    return {"big": f"{rate * 100:.1f}%", "note": f"{word}, {how}. Play the original at {rate:.2f}× to match your clip.", "tip": ""}


def _summarise(report: dict) -> dict:
    """Pick the best result from a songfinder JSON report and phrase it for the page."""
    order = {"exact": 2, "probable": 1}
    rs = [r for r in report.get("results", []) if not r.get("error") and r.get("decision") in order]
    rs.sort(key=lambda r: (order[r["decision"]], r["decision"] == "exact" and r.get("audio") == "full",
                           r.get("similarity", 0)), reverse=True)
    if not rs:
        tunes = [r for r in report.get('results', []) if r.get('tune') and not r.get('error')]
        tunes.sort(key=lambda r: r['tune']['phrase_order_gain'], reverse=True)
        if tunes:
            top = tunes[0]
            return {'verdict':'tune', 'title':top['title'],
                    'reference_artist':top.get('artist',''), 'reference_url':top['url'],
                    'message':'The musical phrases resemble this song, but we could not confirm the same recording. Compare the reference below.'}
        if any(r.get('decision')=='related' for r in report.get('results',[])):
            return {'verdict':'related','message':'We found a similar arrangement, but could not confirm the same recording. Add the performer or title to try again.'}
        if report.get('search_limits'):
            return {'verdict':'unavailable','message':'We checked several recordings but reached the comparison limit without a match. Add a title or performer to focus the next search.'}
        if any(isinstance(issue,dict) and issue.get('kind') for issue in report.get('provider_issues',[])):
            return {'verdict':'unavailable','message':'Some music services were unavailable or at their request limit. We could not confirm a recording. Please retry later.'}
        if report.get('shazam_hints'):
            return {'verdict':'unconfirmed','message':'A recognition service suggested a song, but the available audio did not confirm its recording. Add the performer or title to narrow the search.'}
        return {"verdict": "none"}
    top = rs[0]
    title, artist = top.get("title") or "Untitled", top.get("artist") or ""
    # Identity belongs to the audio-confirmed candidate. A recognizer can name
    # another performance of the same composition, so title agreement cannot
    # authorize replacing the winning artist or title.
    # The report's links are already ordered best first (Spotify first). One per
    # service, and never a recognition engine's own page.
    links, seen = [], set()
    for l in report.get("links", []):
        if not isinstance(l, dict) or not l.get("url"):
            continue
        platform = l.get("platform") or l.get("label", "")
        host = urlsplit(l["url"]).hostname or ""
        if platform == "recogniser" or "shazam." in host or "audd." in host or platform in seen:
            continue
        seen.add(platform)
        label = re.sub(r"\s*\(search\)$", "", l.get("label") or platform)
        links.append({"label": label, "url": l["url"], "search": l.get("how") == "search"})
    if not links:
        host = (urlsplit(top["url"]).hostname or "").removeprefix("www.")
        label = {"deezer.com": "Deezer", "youtube.com": "YouTube", "archive.org": "Internet Archive",
                 "soundcloud.com": "SoundCloud", "music.apple.com": "Apple Music",
                 "open.spotify.com": "Spotify"}.get(host, "Bandcamp" if host.endswith("bandcamp.com") else host)
        if "shazam." not in host:
            links.append({"label": label, "url": top["url"], "search": False})
    full = top.get("audio") == "full"
    off = max(top.get("reference_offset_s", 0.0), 0.0)
    return {"verdict": top["decision"], "title": title, "artist": artist, "album": top.get("album") or "",
            "links": links[:6], "preview_only": not full,
            "speed": _speed(top.get("rate", 1.0), top.get("clip_pitch_semitones", 0.0)),
            "starts_at": f"{int(off // 60)}:{int(off % 60):02d}" if full else "",
            "similarity": round(top.get("similarity", 0.0), 2)}
