import time
from types import SimpleNamespace

import pytest

from songfinder import budget, cli, job, pipeline, recognise, service
from songfinder.sources import Candidate, SOURCES


def test_comparison_deadline_cancels_queued_work_and_keeps_completed_results(monkeypatch):
    def evaluate(*args, **kwargs):
        budget.sleep(.008)
        return pipeline.Result(args[1], error='no match')
    monkeypatch.setattr(pipeline, 'evaluate_candidate', evaluate)
    completed = []
    start = time.monotonic()
    with pytest.raises(budget.Expired), budget.limit(.05):
        pipeline.identify(None, [Candidate('bandcamp', str(i)) for i in range(1000)], None,
                          workers=2, on_result=completed.append)
    assert 0 < len(completed) < 20
    assert time.monotonic()-start < .5


def test_catalogue_deadline_preserves_fast_source_results(monkeypatch):
    wanted = Candidate('itunes', 'https://example.test/song')
    monkeypatch.setattr(SOURCES['itunes'], 'search', lambda *a: [wanted])
    def slow(*a):
        budget.sleep(10)
    monkeypatch.setattr(SOURCES['bandcamp'], 'search', slow)
    start = time.monotonic()
    with budget.limit(.04):
        found = pipeline.gather('Song', None, ['bandcamp', 'itunes'], lambda s: None)
    assert found == [wanted]
    assert time.monotonic()-start < .5


def test_shared_provider_cooldown_never_sleeps_through_the_search(monkeypatch):
    monkeypatch.setattr(job.store, 'budget_delay', lambda *a: 120)
    monkeypatch.setattr(job.budget, 'sleep', lambda *a: pytest.fail('must move to fallback'))
    assert not job.wait_for_recognition_slot('shazam', time.monotonic()+300)


def test_audd_precedes_speed_retries_and_diagnostics_are_persisted(monkeypatch):
    q = SimpleNamespace(F=None, duration=30, windows=[1, 2, 3], window_len=5, rates=[.7, 1.4])
    monkeypatch.setattr(cli, 'prepare_query', lambda *a, **kw: q)
    monkeypatch.setattr(cli, '_query_opts', lambda a: {})
    cache = SimpleNamespace(entries=lambda: iter(()), shortlist=lambda f: [])
    order, snapshots = [], []
    def shazam(*a, **kw):
        order.append('original' if kw['rates'] == [1.] else 'speed')
        return [], 0
    def audd(*a, **kw):
        order.append('audd')
        kw['on_attempt']('calls')
        kw['on_attempt']('responses')
        return [], 1
    monkeypatch.setattr(cli, 'recognise', shazam)
    monkeypatch.setattr(cli, 'recognise_audd', audd)
    options = SimpleNamespace(title=None, artist=None, urls=[], max_candidates=80,
        exhaustive=False, workers=2, shazam=True, shazam_calls=8, audd=True, audd_calls=2, index=True)
    cli.run_identify(options, 'sample', cache, [], log=lambda s: None,
        remaining=lambda: 200, on_result=lambda q, ranked, notes: snapshots.append(notes['audd_calls']))
    assert order == ['original', 'audd', 'speed']
    assert 1 in snapshots


def test_service_report_keeps_audd_counts_and_stage_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'load_env_file', lambda *a: None)
    def identify(*args, **kw):
        notes = dict(route='', hints=[], fingerprints=3, audd_calls=2,
                     recognition={'audd': {'calls': 2, 'responses': 2}},
                     stages=[{'name': 'AudD fallback', 'seconds': .1, 'limited': False}],
                     search_limits=['local index fallback'])
        kw['on_result'](SimpleNamespace(duration=30), [], notes)
        return [], SimpleNamespace(duration=30), notes, True
    monkeypatch.setattr(cli, 'run_identify', identify)
    monkeypatch.setattr(cli, '_report', lambda *a: {})
    snapshots = []
    service.identify_clip('sample', options=service.Options(cache_dir=str(tmp_path)), on_result=snapshots.append)
    assert snapshots[0]['audd_calls'] == 2
    assert snapshots[0]['recognition']['audd']['responses'] == 2
    assert snapshots[0]['search_limits'] == ['local index fallback']
