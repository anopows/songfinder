"""A web search must keep the whole cached index despite its network limit."""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from songfinder import cli


def test_cached_match_beyond_network_candidate_limit(monkeypatch):
    entries = [dict(source='bandcamp', url=f'https://band.bandcamp.com/track/{i}',
                    title='Everybody Loves My Baby', artist=f'Artist {i}')
               for i in range(120)]
    cache = SimpleNamespace(entries=lambda: iter(entries))
    query = SimpleNamespace(duration=42, windows=[1, 2, 3], window_len=10, rates=[.7, 1.4])
    monkeypatch.setattr(cli, 'prepare_query', lambda *args, **kwargs: query)
    monkeypatch.setattr(cli, '_query_opts', lambda a: {})
    monkeypatch.setattr(cli, 'rank', lambda values: values)
    checked = []

    def identify(q, candidates, cache, **kwargs):
        checked.extend(c.url for c in candidates)
        return [SimpleNamespace(decision='exact', candidate=c)
                for c in candidates if c.url.endswith('/119')]

    monkeypatch.setattr(cli, 'identify', identify)
    a = SimpleNamespace(title='Everybody Loves My Baby', urls=[], max_candidates=80,
                        exhaustive=False, workers=2, shazam=False, index=True, audd=False)
    ranked, _, notes, _ = cli.run_identify(a, 'public-test.wav', cache, [], log=lambda _: None)
    assert ranked[0].candidate.url.endswith('/119')
    assert len(checked) == 120
    assert notes['route'] == 'local index'
