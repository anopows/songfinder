import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from songfinder import cli, recognise


@pytest.fixture
def audd_setup(monkeypatch):
    monkeypatch.setattr(recognise, '_variant', lambda *a, **kw: b'public audio')
    monkeypatch.setattr(recognise, 'cached_answer', lambda *a: None)
    monkeypatch.setattr(recognise, 'store_answer', lambda *a: None)
    monkeypatch.setattr(recognise, 'REQUEST_GATE', None)


def test_cli_fallback_calls_real_audd_with_deadline(audd_setup, monkeypatch):
    query = SimpleNamespace(F=None, duration=30, windows=[1, 2, 3], window_len=5, rates=[.7, 1.4])
    monkeypatch.setattr(cli, 'prepare_query', lambda *a, **kw: query)
    monkeypatch.setattr(cli, '_query_opts', lambda *a: {})
    cache = SimpleNamespace(entries=lambda: iter(()), shortlist=lambda f: [])
    options = SimpleNamespace(title=None, artist=None, urls=[], max_candidates=80,
                              exhaustive=False, workers=2, shazam=False,
                              shazam_calls=0, index=False, audd=True, audd_calls=1)
    seen = []
    def response(request, timeout):
        seen.append(timeout)
        return io.BytesIO(json.dumps({'status': 'success', 'result': None}).encode())
    with patch('urllib.request.urlopen', response):
        _, _, notes, _ = cli.run_identify(options, 'sample.wav', cache, [],
                                          log=lambda s: None, remaining=lambda: 12)
    assert len(seen) == 1 and 11 < seen[0] <= 12
    assert notes['audd_calls'] == 1
    assert notes['provider_issues'] == []


@pytest.mark.parametrize('mode,expected', [
    ('budget', 'budget'), ('deadline', 'budget'),
    ('unavailable', 'unavailable'), ('refused', 'refused'),
])
def test_audd_reports_limits_and_errors(audd_setup, monkeypatch, mode, expected):
    issues = []
    if mode == 'budget':
        monkeypatch.setattr(recognise, 'REQUEST_GATE', lambda provider: False)
    def response(request, timeout):
        if mode == 'unavailable':
            raise OSError('offline')
        return io.BytesIO(json.dumps({'status': 'error', 'error': 'refused'}).encode())
    with patch('urllib.request.urlopen', response):
        hits, calls = recognise.recognise_audd('sample.wav', 30, max_calls=1,
            on_issue=lambda *issue: issues.append(issue),
            remaining=lambda: 0 if mode == 'deadline' else 20)
    assert hits == []
    assert calls == (0 if mode in ('budget', 'deadline') else 1)
    assert issues == [('audd', expected)]
