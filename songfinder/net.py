"""Polite HTTP: honest user agent, per-host spacing, back-off, no challenge solving."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .audio import USER_AGENT
from . import budget

# Minimum seconds between two requests to the same host.
MIN_INTERVAL = {"bandcamp.com": 1.0, "bcbits.com": 0.5, "archive.org": 0.5, "dzcdn.net": 1.0}
DEFAULT_INTERVAL = 0.3


def wait_for_slot(url: str) -> None:
    """Take a turn for this URL's host; also used before FFmpeg opens a stream."""
    _wait_for_slot(url)

_lock = threading.Lock()
_next_slot: dict[str, float] = {}


class Blocked(RuntimeError):
    """The site answered with a bot challenge or refusal. We stop; we never bypass it."""


def host_key(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for known in MIN_INTERVAL:
        if host == known or host.endswith("." + known):
            return known
    return host


def _wait_for_slot(url: str) -> None:
    key = host_key(url)
    gap = MIN_INTERVAL.get(key, DEFAULT_INTERVAL)
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(key, 0.0))
        _next_slot[key] = slot + gap
    if slot > now:
        budget.sleep(slot - now)


def fetch(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 25, retries: int = 2) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Not an http(s) URL: {url}")
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        _wait_for_slot(url)
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=budget.timeout(timeout)) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 401):
                raise Blocked(f"{host_key(url)} refused the request (HTTP {e.code})") from e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                budget.sleep(3.0 * (attempt + 1) ** 2)
                continue
            raise
        except budget.Expired:
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                budget.sleep(2.0 * (attempt + 1))
                continue
            raise
        if b"<title>Client Challenge" in body[:4000]:
            raise Blocked(f"{host_key(url)} answered with a bot challenge")
        return body
    raise RuntimeError("unreachable")


def fetch_json(url: str, payload: dict | None = None, headers: dict | None = None, **kw):
    data = None
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    return json.loads(fetch(url, data=data, headers=headers, **kw))
