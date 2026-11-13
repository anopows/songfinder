"""Reproducible offline evaluation. Synthetic results are not catalogue accuracy.

Run with --output report.json. A real-audio manifest can supply cases with query,
reference (local paths), expected (exact/related/none), and id; audio stays local.
"""
import argparse, hashlib, json, resource, statistics, sys, time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from songfinder.features import features
from songfinder.matcher import Query
from songfinder.audio import decode
import synth

# Frozen before this release's verification changes. Do not tune on these seeds.
SEEDS=(6079,7919,104729)
def synthetic():
    for seed in SEEDS:
        score=synth.make_score(seed=seed,bars=32)
        ref=synth.render(score,bpm=173)
        clip=ref[7*synth.SR:42*synth.SR]
        for kind,rate in [('clean',1.0),('room-slow',.926)]:
            q=synth.varispeed(clip,rate)
            if kind!='clean':q=synth.room_recording(q,seed=seed)
            yield f'{seed}-{kind}',q,ref,'exact'
        cover=synth.render(score,bpm=173,timbre=1,drift=.003,jitter=.02,seed=seed+1)
        yield f'{seed}-alternate',clip,cover,'related'
        other=synth.render(synth.make_score(seed=seed+13,bars=32),bpm=173)
        yield f'{seed}-absent',clip,other,'none'

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();start=time.monotonic()
    if a.manifest:
        manifest=json.loads(a.manifest.read_text())
        cases=((c['id'],decode(str(a.manifest.parent/c['query'])),decode(str(a.manifest.parent/c['reference'])),c['expected']) for c in manifest['cases'])
        kind='external-labelled-audio'
    else:cases=synthetic();kind='frozen-synthetic-v1'
    rows=[]
    for name,q,r,expected in cases:
        t=time.monotonic();ev=Query(features(q)).evaluate(features(r));v=ev.verification
        ok=ev.decision=='exact' if expected=='exact' else ev.decision not in ('exact','probable')
        rows.append(dict(id=name,expected=expected,decision=ev.decision,passed=ok,seconds=round(time.monotonic()-t,3),
            query_sha256=hashlib.sha256(q.tobytes()).hexdigest(),reference_sha256=hashlib.sha256(r.tobytes()).hexdigest(),
            similarity=round(v.similarity,4) if v else None,holdout_similarity=round(v.holdout_similarity,4) if v and hasattr(v,'holdout_similarity') else None))
        print(name,ev.decision,ok,flush=True)
    latency=[r['seconds'] for r in rows]
    data=dict(kind=kind,cases=rows,passed=sum(r['passed'] for r in rows),total=len(rows),false_exact=sum(r['expected']!='exact' and r['decision']=='exact' for r in rows),
        exact_recall=sum(r['expected']=='exact' and r['decision']=='exact' for r in rows)/max(1,sum(r['expected']=='exact' for r in rows)),
        median_seconds=statistics.median(latency),p95_seconds=float(np.percentile(latency,95)),elapsed_seconds=round(time.monotonic()-start,2),
        max_rss_platform_units=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,provider_requests=0,
        limitation='Offline pair verification; no discovery, catalogue coverage or blind real-recording accuracy measured.')
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(data,indent=2)+'\n')
    return int(data['passed']!=data['total'])
if __name__=='__main__':raise SystemExit(main())
