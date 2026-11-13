import numpy as np
import synth
from songfinder.cache import FeatureCache
from songfinder.features import features
from songfinder.matcher import Query

def test_retrieval_survives_restart_and_slows_without_replacing_verification(tmp_path):
    cache=FeatureCache(str(tmp_path));refs=[]
    for seed in range(10,15):
        audio=synth.render(synth.make_score(seed,bars=24))
        refs.append(audio);cache.put(str(seed),features(audio),{'title':str(seed)})
    q=features(synth.varispeed(refs[3][10*synth.SR:35*synth.SR],.926))
    results=FeatureCache(str(tmp_path)).shortlist(q,limit=3)
    assert '13' in [r['key'] for r in results]
    assert Query(q).evaluate(cache.get('13')[0]).decision=='exact'
    (tmp_path/'retrieval.sqlite3').write_bytes(b'broken index')
    assert cache.shortlist(q)==[]
    assert cache.get('13') is not None  # full scan remains usable

def test_discovery_and_holdout_are_disjoint_and_confirmation_cannot_refit():
    f=features(synth.render(synth.make_score(3,bars=24)))
    q=Query(f)
    for a,b in q.windows:
        for c,d in q.holdouts:assert b<=c or d<=a
    fit=q.consensus(q.screen(f));original=q.verify(f,fit)
    altered=f.copy()
    for a,b in q.holdouts:
        start,end=int(a*25),int(b*25)
        altered[:,start:end]=np.roll(altered[:,start:end],20,axis=1)
    changed=q.verify(altered,fit)
    assert changed.rate==original.rate and changed.offset==original.offset and changed.pitch_bins==original.pitch_bins
    assert changed.holdout_similarity<original.holdout_similarity-.2
