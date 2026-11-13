"""Command line: `songfinder identify` and `songfinder match`."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time

from . import __version__
from .audio import DecodeError
from .cache import FeatureCache
from .features import BPO
from .matcher import Query, describe_speed
from .pipeline import Result, evaluate_candidate, gather, identify, prepare_query, rank
from . import recognise as recognise_mod
from .links import HOW_TEXT, Spotify, build_links, format_links
from .recognise import RATES, clean_title, recognise, recognise_audd

LINK_SEARCH_LIMIT = 40       # candidates checked when hunting a full-length copy
VERSION_SEARCH_LIMIT = 120   # candidates checked across other versions of a title
from .sources import (DEFAULT_SOURCES, SOURCES, Candidate, candidate_from_ref,
                      artist_variants, candidates_from_text, clean_channel, title_matches)


def _mmss(t: float) -> str:
    sign = "-" if t < 0 else ""
    t = abs(t)
    return f"{sign}{int(t // 60)}:{t % 60:04.1f}"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _query_opts(a) -> dict:
    return {"rate_min": a.rate_min, "rate_max": a.rate_max, "free_pitch": a.free_pitch}


def _load_query(a) -> Query:
    _log(f"Clip: {a.clip}")
    q = prepare_query(a.clip, **_query_opts(a))
    _log(f"  {q.duration:.1f} s of audio, {len(q.windows)} windows of {q.window_len:.0f} s, "
         f"speeds {q.rates[0] * 100:.0f}%-{q.rates[-1] * 100:.0f}%")
    return q


def result_dict(r: Result) -> dict:
    c = r.candidate
    d = {"source": c.source, "url": c.url, "title": c.title, "artist": c.artist, "album": c.album,
         "released": c.released, "duration_s": c.duration, "audio": c.audio_kind,
         "decision": r.decision, "error": r.error or None, "skipped": r.skipped}
    ev = r.evaluation
    if ev and ev.fit:
        d["windows_on_one_line"] = ev.fit.n_windows
        d["windows"] = [{"clip_start_s": h.clip_start, "clip_end_s": h.clip_end,
                         "reference_start_s": round(h.ref_start, 2),
                         "reference_end_s": round(h.ref_end, 2),
                         "rate": round(h.rate, 4), "score": round(h.score, 3)}
                        for h in ev.fit.inliers]
    if ev and ev.verification:
        v = ev.verification
        d.update({"rate": round(v.rate, 5), "reference_offset_s": round(v.offset, 3),
                  "clip_pitch_semitones": round(v.clip_pitch_semitones, 3),
                  "similarity": round(v.similarity, 4), "noise_floor": round(v.null_median, 4),
                  "z": round(v.z, 1), "coverage": round(v.coverage, 3),
                  "overlap_s": round(v.overlap_s, 1),
                  "segments": [[a, b, round(s, 3)] for a, b, s in v.segments]})
    return d


def print_match(r: Result, n_windows_total: int) -> None:
    c, ev = r.candidate, r.evaluation
    v, fit = ev.verification, ev.fit
    extra = ", ".join(x for x in (c.album, c.released) if x)
    print(f"  {c.title or '(untitled)'} - {c.artist or 'unknown artist'}" + (f"  ({extra})" if extra else ""))
    print(f"  {c.url}")
    if c.audio_kind == "preview":
        print("  Note: matched against a store preview, not the full track.")
    print(f"  {describe_speed(v)}")
    if c.audio_kind == "preview":
        print("  (The clip's position is not known within the full track, only within the preview.)")
    else:
        print(f"  The clip starts at {_mmss(v.offset)} in the recording.")
    ok = sum(s >= 0.15 for *_, s in v.segments)
    print(f"  Evidence: similarity {v.similarity:.2f} against a noise floor of {v.null_median:.2f} "
          f"(z = {v.z:.0f}); {fit.n_windows} of {n_windows_total} windows on one line; "
          f"{ok} of {len(v.segments)} five-second segments agree.")
    for h in fit.inliers:
        print(f"    clip {_mmss(h.clip_start)}-{_mmss(h.clip_end)}  ->  recording "
              f"{_mmss(h.ref_start)}-{_mmss(h.ref_end)}   score {h.score:.2f}")


def print_report(ranked: list[Result], q: Query) -> int:
    scored = [r for r in ranked if r.evaluation and not r.skipped]
    # Among exact matches, lead with a full-length source rather than a preview.
    scored.sort(key=lambda r: (r.decision == "exact", r.decision == "exact"
                               and r.candidate.audio_kind == "full"), reverse=True)
    top = scored[0] if scored else None
    print()
    if top and top.decision == "exact":
        print("EXACT MATCH")
        print_match(top, len(q.windows))
        # Another copy of the same recording scores about as well; a lower score is another version.
        top_sim = top.evaluation.verification.similarity
        same = [r for r in scored[1:] if r.decision in ("exact", "probable")
                and r.evaluation.verification.similarity >= top_sim - 0.15]
        shown: set[str] = {top.candidate.source}
        first = True
        for r in same:                      # best link per other source
            if r.candidate.source in shown:
                continue
            shown.add(r.candidate.source)
            if first:
                print("\nSame recording elsewhere:")
                first = False
            v = r.evaluation.verification
            print(f"  {r.candidate.source:<10} {r.decision:<9} {v.similarity:.2f}  "
                  f"{r.candidate.label[:60]}  {r.candidate.url}")
        more = len(same) - (len(shown) - 1)
        if more > 0:
            print(f"  (+{more} more matching upload(s); see --json)")
        code = 0
    elif top and top.decision == "probable":
        print("PROBABLE MATCH (not enough evidence to call it exact)")
        print_match(top, len(q.windows))
        code = 0
    else:
        print("NO MATCH among the candidates checked.")
        code = 1
    rest = [r for r in scored if r is not top and r.decision in ("related", "none")][:5]
    if rest:
        print("\nBest of the rest (for comparison, not matches):")
        for r in rest:
            ev = r.evaluation
            sim = ev.verification.similarity if ev.verification else 0.0
            w = ev.fit.n_windows if ev.fit else 0
            peak = max((h.score for h in ev.fit.inliers), default=0.0) if ev.fit else 0.0
            tag = ""
            if r.decision == "related":
                tag = f"  [same arrangement, other recording: timing wanders {ev.verification.timing_spread:.2f} s]"
            print(f"  full-clip {sim:.2f}, {w} window(s), best window {peak:.2f}   {r.candidate.label[:70]}{tag}")
    errors = [r for r in ranked if r.error]
    if errors:
        print(f"\n{len(errors)} candidate(s) could not be checked "
              f"(first: {errors[0].candidate.url} - {errors[0].error}).")
    return code


def _sources(a) -> list[str] | None:
    if a.sources == "none":
        return []
    sources = [s for s in (a.sources.split(",") if a.sources else DEFAULT_SOURCES) if s]
    unknown = [s for s in sources if s not in DEFAULT_SOURCES]
    if unknown:
        _log(f"Unknown source(s): {', '.join(unknown)}")
        return None
    return sources


def run_identify(a, clip: str, cache: FeatureCache, sources: list[str], *, log=None, on_result=None, should_stop=None):
    """All stages for one clip. Returns (ranked results, query, notes for the report)."""
    emit = log or _log
    def checkpoint():
        if should_stop and should_stop():
            raise TimeoutError("Search budget reached")
    checkpoint()
    emit(f"Clip: {clip}")
    q = prepare_query(clip, **_query_opts(a))
    emit(f"  {q.duration:.1f} s of audio, {len(q.windows)} windows of {q.window_len:.0f} s, "
         f"speeds {q.rates[0] * 100:.0f}%-{q.rates[-1] * 100:.0f}%")
    ranked: list[Result] = []
    seen: set[str] = set()
    notes = {"route": "", "hints": [], "fingerprints": 0, "links": [], "audd_calls": 0}
    searched_titles: set[str] = set()

    def check(candidates, label: str, route: str, quick: bool = False, cached: bool = False) -> None:
        nonlocal ranked
        checkpoint()
        fresh = []
        for c in candidates:
            if c.key not in seen:
                fresh.append(c)
        # The network candidate cap must not discard known reference recordings.
        # Cached comparisons remain bounded by the job's wall-clock deadline.
        if a.max_candidates and not cached:
            fresh = fresh[:a.max_candidates]
        seen.update(c.key for c in fresh)
        if not fresh:
            return
        had = found()
        emit(f"Checking {len(fresh)} candidates ({label}):")
        ranked = rank(ranked + identify(q, fresh, cache, workers=a.workers,
                                        exhaustive=a.exhaustive, quick=quick, log=emit))
        if found() and not had:
            notes["route"] = route
        if on_result:
            on_result(q, ranked, notes)

    def found() -> bool:
        """The recording is confirmed by audio (a full track, or a long enough preview)."""
        return not a.exhaustive and any(r.decision == "exact" for r in ranked)

    def found_full() -> bool:
        return not a.exhaustive and any(r.decision == "exact" and r.candidate.audio_kind == "full"
                                        for r in ranked)

    def index(title: str | None = None):
        out = []
        for m in cache.entries():
            if m.get("source") not in SOURCES or (title and not title_matches(title, m.get("title", ""))):
                continue
            out.append(Candidate(m["source"], m.get("url", ""), m.get("title", ""), m.get("artist", ""),
                                 m.get("album", ""), m.get("duration"), m.get("released", ""),
                                 m.get("audio_kind", "full")))
        return out

    def full_link_search(title: str, artist: str) -> None:
        """Confirmed against a preview only: look for the same recording as a full track.

        Focused on title and artist, full-audio sources only, and bounded; failing
        here costs nothing, since the recording is already identified.
        """
        full = [n for n in sources if SOURCES[n].audio_kind == "full"]
        if not full or found_full():
            return
        emit(f'Confirmed against a preview; looking for a full-length "{title}" by {artist}:')
        check(gather(title, artist, full, emit)[:LINK_SEARCH_LIMIT], "full-length copies", notes["route"])

    def ask_shazam(rates, budget: int, label: str, patience: int = 3) -> None:
        if budget <= 0:
            return
        emit(f"Asking Shazam, {label} (fingerprints only, no audio is sent):")
        try:
            hits, calls = recognise(clip, q.duration, rates=rates, max_calls=budget, log=emit,
                                    patience=patience)
        except RuntimeError as e:
            emit(f"  {e}")
            return
        notes["fingerprints"] += calls
        follow(hits)

    def ask_audd() -> None:
        emit("Asking AudD (sends a 15 s audio excerpt):")
        hits, calls = recognise_audd(clip, q.duration, rates=[0.9, 1.1], max_calls=a.audd_calls, log=emit)
        notes["audd_calls"] = notes.get("audd_calls", 0) + calls
        follow(hits)

    def follow(hits) -> None:
        """Verify a recogniser's answers against audio; find links once one is confirmed."""
        for h in hits[:2]:
            title = clean_title(h.title)
            notes["hints"].append({"title": h.title, "artist": h.artist, "url": h.url, "isrc": h.isrc,
                                   "assumed_rate": h.rate, "mode": h.mode, "links": h.links or [],
                                   "service": h.service})
            route = f"{h.service} ({'unmodified' if h.rate == 1.0 else f'corrected for {h.rate * 100:.0f}%'})"
            # Audio the recogniser points at directly: the local index, its preview, the ISRC's Deezer entry.
            check(index(title), f'local index entries titled "{title}"', route, cached=True)
            direct = []
            if h.preview_url:
                direct.append(Candidate("shazam", h.url or h.preview_url, h.title, h.artist,
                                        audio_kind="preview", audio_url=h.preview_url, wanted=title))
            try:
                dz = SOURCES["deezer"].by_isrc(h.isrc, title) if "deezer" in sources else None
            except Exception:
                dz = None
            if dz:
                direct.append(dz)
                notes["hints"][-1]["links"] = list(h.links or []) + [("Deezer", dz.url)]
            if not found():
                check(direct, f'{h.service}\'s own audio for "{h.title}"', route)
            if found():
                full_link_search(title, h.artist)
                return
            # Not confirmed: search the catalogues by title and artist, simplifying the credit
            # ("X And His Orchestra Feat. Y" -> "X") when the full string finds nothing.
            for artist in artist_variants(h.artist) if sources else []:
                if found():
                    break
                emit(f'Searching for "{title}" by {artist}:')
                check(gather(title, artist, sources, emit), "catalogue search", route)
            if found():
                return
        # A recogniser often names the right tune but another band's version of it.
        # Search the versions of each suggested title and let the matcher choose.
        for title in dict.fromkeys(clean_title(h.title) for h in hits[:2]):
            if not sources or found() or title.lower() in searched_titles:
                continue
            searched_titles.add(title.lower())
            emit(f'Searching other versions of "{title}":')
            check(gather(title, None, sources, emit)[:VERSION_SEARCH_LIMIT], "other versions of the title",
                  "Shazam title, then other versions", quick=True)

    # 1. Links supplied by the user, then a catalogue search if a title is known.
    given = []
    for path in a.urls or []:
        with open(path, encoding="utf-8", errors="replace") as fh:
            got = candidates_from_text(fh.read(), a.title or "")
        emit(f"  {len(got)} track links read from {path}")
        given += got
    check(given, "your links", "your links")
    if a.title:
        check(index(a.title), "local index entries for the title", "local index", quick=True, cached=True)
    if a.title and sources and not found():
        emit(f'Searching for "{a.title}"' + (f" by {a.artist}" if a.artist else "") + ":")
        check(gather(a.title, a.artist, sources, emit), "catalogue search", "title search",
              quick=not a.artist)

    use_shazam = a.shazam and not a.title
    # 2. Cheapest first: one round of Shazam on the unmodified clip (a few seconds).
    if use_shazam and not found():
        ask_shazam([1.0], min(3, a.shazam_calls), "clip as recorded", patience=1)
    # 3. Everything matched before: the local index. No network, any speed.
    if not found() and (a.index or not (a.title or given)):
        check(index(), "recordings already in the local index", "local index", quick=True, cached=True)
    # 4. Still nothing: Shazam again with the clip corrected for other speeds.
    if use_shazam and not found():
        ask_shazam(RATES[1:], a.shazam_calls - notes["fingerprints"], "speed-corrected")
    # 5. Shazam does not know it: a second recogniser with another catalogue.
    if a.audd and not a.title and not found():
        ask_audd()
    if not notes["route"] and any(r.decision in ("exact", "probable") for r in ranked):
        notes["route"] = "preview only"
    return ranked, q, notes, bool(seen)


def _top(ranked: list[Result]) -> Result | None:
    scored = [r for r in ranked if r.evaluation and not r.skipped]
    scored.sort(key=lambda r: (r.decision == "exact", r.decision == "exact"
                               and r.candidate.audio_kind == "full"), reverse=True)
    return scored[0] if scored else None


def _report(clip: str, q: Query, ranked: list[Result], notes: dict, title, cache: FeatureCache,
            spotify: Spotify | None) -> dict:
    """Everything known about one clip, with listening links (Spotify first) once identified."""
    data = {"clip": os.path.basename(clip), "clip_seconds": round(q.duration, 2),
            "title": title, "bands_per_octave": BPO, "route": notes["route"],
            "shazam_hints": notes["hints"], "fingerprints_sent": notes["fingerprints"],
            "results": [result_dict(r) for r in ranked if not r.skipped]}
    data.update(build_links(data, q, cache, spotify, _log))
    return data


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _print_links(data: dict) -> None:
    if data.get("links"):
        print("\nListen to this recording (best first):")
        for line in format_links(data["links"]):
            print(line)


def _print_hints(ranked: list[Result], notes: dict, data: dict | None = None) -> None:
    top = _top(ranked)
    if data and top and top.decision == "exact":
        _print_links(data)
    if notes["hints"] and not (top and top.decision == "exact"):
        print("\nRecognisers suggested (not confirmed against audio):")
        for h in notes["hints"]:
            print(f"  \"{h['title']}\" by {h['artist']}  {h['url']}")


def cmd_identify(a) -> int:
    sources = _sources(a)
    if sources is None:
        return 2
    cache = FeatureCache(None if a.no_cache else a.cache_dir)
    ranked, q, notes, checked = run_identify(a, a.clip, cache, sources)
    if not checked and not notes["hints"]:
        _log("Nothing to check. Give --title, --urls, or --shazam, or build up the local index.")
        return 2
    data = _report(a.clip, q, ranked, notes, a.title, cache, Spotify())
    code = print_report(ranked, q)
    _print_hints(ranked, notes, data)
    if a.json:
        _write_json(a.json, data)
        _log(f"Wrote {a.json}")
    return code


def cmd_batch(a) -> int:
    """Identify many clips; write one JSON and text report each, plus summary.md."""
    sources = _sources(a)
    if sources is None:
        return 2
    cache = FeatureCache(None if a.no_cache else a.cache_dir)
    spotify = Spotify()
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for n, clip in enumerate(a.clips, 1):
        name = os.path.splitext(os.path.basename(clip))[0]
        group = os.path.basename(os.path.dirname(os.path.abspath(clip)))
        stem = os.path.join(a.out, f"{group}_{name}")
        if a.resume and os.path.exists(stem + ".json"):
            with open(stem + ".json", encoding="utf-8") as fh:
                rows.append(_summary_row(group, name, json.load(fh)))
            continue
        _log(f"\n===== [{n}/{len(a.clips)}] {group}/{name}")
        t0 = time.monotonic()
        a.clip = clip
        try:
            ranked, q, notes, _ = run_identify(a, clip, cache, sources)
        except (DecodeError, ValueError) as e:
            _log(f"Error: {e}")
            rows.append([group, name, "error", str(e), "", "", "", "", ""])
            continue
        data = _report(clip, q, ranked, notes, a.title, cache, spotify)
        _write_json(stem + ".json", data)
        with open(stem + ".txt", "w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
            print_report(ranked, q)
            _print_hints(ranked, notes, data)
        row = _summary_row(group, name, data)
        rows.append(row)
        _log(f"  -> {row[2]}: {row[3]}  ({time.monotonic() - t0:.0f} s)")
    _write_summary(a.out, rows)
    counts = {v: sum(r[2] == v for r in rows) for v in ("exact", "probable", "none", "error")}
    print(f"{len(rows)} clips: " + ", ".join(f"{n} {v}" for v, n in counts.items()))
    print(f"Summary: {os.path.join(a.out, 'summary.md')}")
    return 0 if counts["none"] + counts["error"] == 0 else 1


SUMMARY_HEAD = ["Folder", "Video", "Verdict", "Recording", "Clip speed", "Pitch (semitones)",
                "Clip starts at", "Found by", "Link", "Link checked by"]


def _write_summary(out: str, rows: list) -> None:
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("| " + " | ".join(SUMMARY_HEAD) + " |\n|" + " --- |" * len(SUMMARY_HEAD) + "\n")
        for r in rows:
            r = list(r) + [""] * (len(SUMMARY_HEAD) - len(r))
            fh.write("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n")


def cmd_relink(a) -> int:
    """Rebuild listening links for clips that already have a report; no re-identification."""
    cache = FeatureCache(None if a.no_cache else a.cache_dir)
    spotify = Spotify()
    if not spotify.available:
        _log("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET are not set: Spotify tracks come only from "
             "recogniser answers.")
    rows = []
    for n, clip in enumerate(a.clips, 1):
        name = os.path.splitext(os.path.basename(clip))[0]
        group = os.path.basename(os.path.dirname(os.path.abspath(clip)))
        stem = os.path.join(a.out, f"{group}_{name}")
        if not os.path.exists(stem + ".json"):
            _log(f"[{n}/{len(a.clips)}] {group}/{name}: no report, skipped")
            continue
        with open(stem + ".json", encoding="utf-8") as fh:
            data = json.load(fh)
        if any(r["decision"] == "exact" for r in data.get("results", [])):
            _log(f"[{n}/{len(a.clips)}] {group}/{name}")
            q = prepare_query(clip, **_query_opts(a))
            data.update(build_links(data, q, cache, spotify, _log))
            _write_json(stem + ".json", data)
            if os.path.exists(stem + ".txt"):
                with open(stem + ".txt", encoding="utf-8") as fh:
                    text = fh.read()
                for marker in ("\nListen to this recording (best first):", "\nListen to this recording:"):
                    if marker in text:
                        text = text[:text.index(marker)]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    _print_links(data)
                with open(stem + ".txt", "w", encoding="utf-8") as fh:
                    fh.write(text.rstrip("\n") + "\n" + buf.getvalue())
        rows.append(_summary_row(group, name, data))
    _write_summary(a.out, rows)
    first = [r[8] for r in rows if r[2] == "exact"]
    n_sp = sum("open.spotify.com/track/" in u for u in first)
    print(f"{len(first)} identified clips: Spotify first for {n_sp}.")
    print(f"Summary: {os.path.join(a.out, 'summary.md')}")
    return 0


def _summary_row(group: str, name: str, data: dict) -> list:
    rs = [r for r in data.get("results", []) if not r.get("error")]
    order = {"exact": 2, "probable": 1, "none": 0}
    rs.sort(key=lambda r: (order.get(r["decision"], 0), r["decision"] == "exact" and r["audio"] == "full",
                           r.get("similarity", 0)), reverse=True)
    top = rs[0] if rs and rs[0]["decision"] in ("exact", "probable") else None
    if not top:
        hint = "; ".join(f"{h.get('service', 'Shazam')} suggested \"{h['title']}\" by {h['artist']}"
                         for h in data.get("shazam_hints", []))
        return [group, name, "none", hint or "-", "", "", "", "", ""]
    who = f"{clean_channel(top['artist']) or 'unknown artist'} - {top['title']}"
    # Uploads carry channel names ("SingTheClassics65", "Release - Topic"). When Shazam named
    # the recording and the matched audio carries that title, Shazam's credit is the better name.
    for h in data.get("shazam_hints", []):
        if title_matches(clean_title(h["title"]), top["title"]):
            who = f"{h['artist']} - {clean_title(h['title'])}"
            break
    who = who[:90]
    note = "" if top["audio"] == "full" else " (confirmed on store preview)"
    link, how = top["url"], "matched"
    built = [l for l in data.get("links") or [] if isinstance(l, dict)]
    if built:                               # listening links, Spotify first
        link, how = built[0]["url"], HOW_TEXT[built[0]["how"]]
    return [group, name, top["decision"], who + note, f"{top['rate'] * 100:.1f}%",
            f"{top['clip_pitch_semitones']:+.2f}",
            _mmss(top["reference_offset_s"]) if top["audio"] == "full" else "(preview)",
            data.get("route", ""), link, how]


def cmd_match(a) -> int:
    q = _load_query(a)
    cache = FeatureCache(None if a.no_cache else a.cache_dir)
    c = candidate_from_ref(a.reference)
    if c is None:
        _log(f"Not a readable file or recognised track link: {a.reference}")
        return 2
    r = evaluate_candidate(q, c, cache)
    if r.error:
        _log(f"Could not read the reference: {r.error}")
        return 2
    code = print_report([r], q)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(result_dict(r), fh, indent=2)
    return code


# Absolute, so it is found whatever folder songfinder is started from.
REPO_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def load_env_file(path: str = REPO_ENV) -> None:
    """Read KEY=value lines (Spotify and AudD credentials) without overriding the environment."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
            if key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "AUDD_API_TOKEN") and value:
                os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    load_env_file(REPO_ENV)                           # the repository's .env first,
    load_env_file(os.path.expanduser("~/.env"))       # then the home folder
    p = argparse.ArgumentParser(prog="songfinder", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, clip=True):
        if clip:
            sp.add_argument("clip", help="video or audio file to identify")
        sp.add_argument("--rate-min", type=float, default=0.70, help="slowest speed to try (default 0.70)")
        sp.add_argument("--rate-max", type=float, default=1.40, help="fastest speed to try (default 1.40)")
        sp.add_argument("--free-pitch", action="store_true",
                        help="also try pitch shifts unrelated to the speed change (slower)")
        sp.add_argument("--cache-dir", default=".songfinder-cache",
                        help="where reference features are kept (default ./.songfinder-cache)")
        sp.add_argument("--no-cache", action="store_true", help="do not read or write the feature cache")
        if clip:
            sp.add_argument("--json", help="write full results to this JSON file")

    def search_opts(sp):
        sp.add_argument("--title", help="song title, if known")
        sp.add_argument("--artist", help="narrow the search to an artist")
        sp.add_argument("--sources", help=f"comma-separated; default {','.join(DEFAULT_SOURCES)}; "
                                          "'none' to use only --urls")
        sp.add_argument("--urls", action="append",
                        help="text or saved HTML file containing track links to check (repeatable)")
        sp.add_argument("--exhaustive", action="store_true",
                        help="check every candidate instead of stopping at the first exact match")
        sp.add_argument("--index", action="store_true",
                        help="also check every recording already in the local cache "
                             "(automatic when no title or links are given)")
        sp.add_argument("--shazam", action="store_true",
                        help="no title? send speed-corrected fingerprints (not audio) to Shazam; "
                             "needs `pip install shazamio`")
        sp.add_argument("--shazam-calls", type=int, default=30,
                        help="most fingerprints to send per clip (default 30)")
        sp.add_argument("--audd", action="store_true",
                        help="if Shazam has no answer, send a 15 s excerpt to AudD (a few free requests a "
                             "day; set AUDD_API_TOKEN for more)")
        sp.add_argument("--audd-calls", type=int, default=2, help="most AudD requests per clip (default 2)")
        sp.add_argument("--max-candidates", type=int)
        sp.add_argument("--workers", type=int, default=8)

    si = sub.add_parser("identify", help="search catalogues for the exact recording in a clip")
    common(si)
    search_opts(si)
    si.set_defaults(func=cmd_identify)

    sb = sub.add_parser("batch", help="identify many clips and write a summary table")
    sb.add_argument("clips", nargs="+", help="video or audio files")
    sb.add_argument("--out", required=True, help="folder for per-clip reports and summary.md")
    sb.add_argument("--resume", action="store_true", help="skip clips that already have a report")
    common(sb, clip=False)
    search_opts(sb)
    sb.set_defaults(func=cmd_batch)

    sr = sub.add_parser("relink", help="rebuild listening links (Spotify first) for existing batch reports")
    sr.add_argument("clips", nargs="+", help="the clips the reports were made for")
    sr.add_argument("--out", required=True, help="folder holding the batch reports")
    common(sr, clip=False)
    sr.set_defaults(func=cmd_relink)

    sm = sub.add_parser("match", help="compare a clip with one reference file or track link")
    common(sm)
    sm.add_argument("reference", help="audio file, or a Bandcamp/YouTube/SoundCloud/archive.org link")
    sm.set_defaults(func=cmd_match)

    a = p.parse_args(argv)
    if not getattr(a, "no_cache", False):
        recognise_mod.ANSWER_DIR = os.path.join(a.cache_dir, "answers")
    try:
        return a.func(a)
    except (DecodeError, ValueError) as e:
        _log(f"Error: {e}")
        return 2
    except KeyboardInterrupt:
        _log("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
