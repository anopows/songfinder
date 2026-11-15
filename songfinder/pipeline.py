"""Clip -> candidates -> evaluations -> ranked result."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from . import budget
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .audio import DecodeError, decode, is_url
from .cache import FeatureCache
from .features import FPS, features
from .matcher import Evaluation, Query
from .net import Blocked, wait_for_slot
from .sources import SOURCES, Candidate, NoAudio, tokens

MAX_REFERENCE_SECONDS = 900          # ignore anything past 15 minutes of a reference
_per_source = {name: threading.Semaphore(2) for name in SOURCES}


@dataclass
class Result:
    candidate: Candidate
    evaluation: Evaluation | None = None
    error: str = ""
    cached: bool = False
    skipped: bool = False
    issue: str = ""

    @property
    def decision(self) -> str:
        return self.evaluation.decision if self.evaluation else "none"


def prepare_query(clip: str, **query_opts) -> Query:
    return Query(features(decode(clip)), **query_opts)


def _decode_stream(stream) -> np.ndarray:
    """Decode a resolved reference; space out stream requests and retry once if throttled."""
    remote = isinstance(stream, str) and is_url(stream)
    for attempt in (0, 1):
        if remote:
            wait_for_slot(stream)
        try:
            return decode(stream, max_seconds=MAX_REFERENCE_SECONDS)
        except DecodeError as e:
            if remote and attempt == 0 and any(code in str(e) for code in ("429", "503")):
                budget.sleep(8)
                continue
            raise
    raise RuntimeError("unreachable")


def reference_features(c: Candidate, cache: FeatureCache) -> tuple[np.ndarray, bool]:
    hit = cache.get(c.key)
    if hit:
        feats, meta = hit
        for name in ("title", "artist", "album", "released", "duration"):
            if not getattr(c, name) and meta.get(name):
                setattr(c, name, meta[name])
        return feats, True
    with _per_source[c.source]:
        stream = SOURCES[c.source].resolve(c)
        y = _decode_stream(stream)
    feats = features(y)
    del y                                 # audio lives only for this call
    cache.put(c.key, feats, {"url": c.url, "source": c.source, "title": c.title,
                             "artist": c.artist, "album": c.album, "released": c.released,
                             "duration": c.duration or feats.shape[1] / FPS,
                             "audio_kind": c.audio_kind})
    return feats, False


def evaluate_candidate(query: Query, c: Candidate, cache: FeatureCache, quick: bool = False) -> Result:
    try:
        feats, cached = reference_features(c, cache)
        evaluation=query.evaluate(feats,quick=quick)
        if evaluation.decision=='exact':cache.protect(c.key)
        return Result(c,evaluation,cached=cached)
    except budget.Expired:
        raise
    except Blocked as e:
        return Result(c, error=f"blocked: {e}",issue="blocked")
    except NoAudio as e:
        return Result(c, error=f"no audio: {e}")
    except Exception as e:                # one bad candidate must not end the run
        return Result(c, error=f"{type(e).__name__}: {e}",issue="reference_unavailable")


def gather(title: str, artist: str | None, sources: list[str],
           log: Callable[[str], None], on_issue=None) -> list[Candidate]:
    """Search all sources at once; interleave full-audio sources, previews last."""
    found: dict[str, list[Candidate]] = {}

    def run(name: str):
        try:
            budget.check()
            return name, SOURCES[name].search(title, artist), ""
        except budget.Expired:
            raise
        except Exception as e:
            return name, [], f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        for future in as_completed([pool.submit(copy_context().run, run, name) for name in sources]):
            try:
                name, cands, err = future.result()
            except budget.Expired:
                continue
            found[name] = cands
            if err and on_issue:on_issue(name,"catalogue_unavailable")
            log(f"  {name:<10} {len(cands):>4} candidates" + (f"   ({err})" if err else ""))
    full = [n for n in sources if SOURCES[n].audio_kind == "full"]
    prev = [n for n in sources if SOURCES[n].audio_kind != "full"]
    ordered: list[Candidate] = []
    for group in (full, prev):
        lists = [found.get(n, []) for n in group]
        for i in range(max((len(l) for l in lists), default=0)):
            ordered.extend(l[i] for l in lists if i < len(l))
    return ordered


def identify(query: Query, candidates: list[Candidate], cache: FeatureCache,
             workers: int = 4, exhaustive: bool = False, quick: bool = False,
             log: Callable[[str], None] = lambda s: None, on_issue=None, on_result=None) -> list[Result]:
    """Evaluate candidates; stop at the first exact match unless `exhaustive`.

    `quick` is for long, unfocused lists (the whole index, every version of a title).
    """
    unique: dict[str, Candidate] = {}
    for c in candidates:
        unique.setdefault(c.key, c)
    todo = list(unique.values())
    stop = threading.Event()

    def work(c: Candidate) -> Result:
        if stop.is_set():
            return Result(c, skipped=True)
        budget.check()
        result=evaluate_candidate(query,c,cache,quick=quick)
        if result.issue and on_issue:on_issue(c.source,result.issue)
        return result

    results: list[Result] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(copy_context().run, work, c) for c in todo]
        done = 0
        try:
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                if r.skipped:
                    continue
                if on_result:
                    on_result(r)
                done += 1
                log(_progress_line(done, len(todo), r))
                if r.decision == "exact" and not exhaustive:
                    stop.set()
        finally:
            stop.set()
            for fut in futures:
                fut.cancel()

    winner = best(results)
    if winner and winner.decision == "exact" and not exhaustive:
        # Look for the same recording on other platforms among what we skipped.
        # One attempt per source that has no exact match yet is enough for a link.
        artist = set(tokens(winner.candidate.artist))
        have = {r.candidate.source for r in results if r.decision == "exact"}
        tried: set[str] = set()
        for r in list(results):
            c = r.candidate
            if not r.skipped or c.source in have or c.source in tried or not artist:
                continue
            if len(artist & set(tokens(c.artist + " " + c.title))) < max(1, len(artist) // 2):
                continue
            tried.add(c.source)
            new = evaluate_candidate(query, c, cache)
            results[results.index(r)] = new
            log(_progress_line(0, 0, new))
    log(f"  evaluated {sum(not r.skipped for r in results)} of {len(todo)} "
        f"in {time.monotonic() - t0:.0f} s")
    return rank(results)


def _progress_line(i: int, n: int, r: Result) -> str:
    head = f"  [{i:>3}/{n}]" if n else "  [also ]"
    c = r.candidate
    if r.error:
        return f"{head} {c.source:<10} {'-':<9}       {c.label[:60]}   ({r.error[:60]})"
    ev = r.evaluation
    sim = ev.verification.similarity if ev.verification else 0.0
    n_w = ev.fit.n_windows if ev.fit else 0
    return f"{head} {c.source:<10} {ev.decision:<9} {sim:4.2f} w{n_w} {c.label[:60]}"


def rank(results: list[Result]) -> list[Result]:
    def key(r: Result):
        return r.evaluation.rank_key if r.evaluation else (-1, 0, 0, 0)
    return sorted(results, key=key, reverse=True)


def best(results: list[Result]) -> Result | None:
    ranked = [r for r in rank(results) if r.evaluation]
    return ranked[0] if ranked else None
