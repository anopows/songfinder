import numpy as np
from songfinder.tune import compare
from songfinder.presentation import _summarise


def music(seed, seconds=36):
    rng = np.random.default_rng(seed)
    notes = rng.integers(0, 12, seconds*3)
    f = np.zeros((132, seconds*25), dtype=np.float32)
    for t in range(f.shape[1]):
        note = notes[min(len(notes)-1, t*3//25)]
        f[30+2*note:32+2*note, t] = 1
        f[54+2*note:56+2*note, t] = .5
    return f


def test_different_performance_can_supply_tune_lead():
    q = music(32)
    # Same sequence with a small live tempo fluctuation, a different octave
    # balance, and extra accompaniment, embedded in a longer recording.
    time = np.arange(q.shape[1]) + 2*np.sin(np.arange(q.shape[1])/70)
    r = np.array([np.interp(time, np.arange(q.shape[1]), band) for band in q])
    r[54:78] *= .3
    r += np.random.default_rng(14).random(r.shape)*.04
    r = np.concatenate([music(19, 8), r, music(21, 8)], axis=1)
    evidence = compare(q, r)
    assert evidence and evidence['basis'] == 'tune'
    assert evidence['phrase_order_gain'] >= .025


def test_common_pitch_distribution_and_random_order_are_not_tune_evidence():
    q = music(32)
    chunks = np.array_split(q, 12, axis=1)
    np.random.default_rng(5).shuffle(chunks)
    assert compare(q, np.concatenate(chunks, axis=1)) is None
    assert compare(q, music(73)) is None
    assert compare(np.ones_like(q), np.ones_like(q)) is None


def test_short_or_silent_input_has_no_tune_lead():
    assert compare(music(2, 10), music(2)) is None
    assert compare(np.zeros((132,900)), music(2)) is None


def test_tune_suggestion_keeps_reference_artist_distinct_from_clip():
    candidate = dict(title='A Song', artist='Reference Band',url='https://example.test/track',
                     decision='none',tune=dict(phrase_order_gain=.04))
    result = _summarise({'results':[candidate],'search_limits':['index']})
    assert result['verdict']=='tune' and result['title']=='A Song'
    assert result['reference_artist']=='Reference Band'
    assert 'artist' not in result and 'speed' not in result and 'starts_at' not in result


def test_exact_recording_takes_precedence_over_tune_lead():
    tune = dict(title='A Song',artist='Reference Band',url='https://example.test/a',decision='none',tune=dict(phrase_order_gain=.04))
    exact = dict(title='Exact Song',artist='Exact Band',url='https://example.test/b',decision='exact',audio='full')
    assert _summarise({'results':[tune,exact]})['verdict']=='exact'
