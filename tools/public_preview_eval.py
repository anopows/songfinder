"""Evaluate local Apple preview files and matching metadata JSON; never publish audio."""
import argparse,hashlib,json,sys,time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from songfinder.audio import decode
from songfinder.features import features
from songfinder.matcher import Query
from songfinder.cache import FeatureCache
import synth
parser=argparse.ArgumentParser();parser.add_argument('--audio-dir',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();root=args.audio_dir;refs={p.stem:decode(str(p)) for p in root.glob('*.m4a')}
feats={k:features(v) for k,v in refs.items()};cache=FeatureCache(str(root/'cache'))
for k,f in feats.items():cache.put(k,f,{'title':k})
rows=[]
for k,audio in refs.items():
    for variant in ('clean','slowed','room'):
        clip=synth.varispeed(audio,.926) if variant=='slowed' else synth.room_recording(audio,seed=41,snr_db=10) if variant=='room' else audio
        query=Query(features(clip));start=time.monotonic();short=cache.shortlist(query.F);retrieval=time.monotonic()-start
        for reference,f in feats.items():
            start=time.monotonic();ev=query.evaluate(f);v=ev.verification
            rows.append(dict(query=k,reference=reference,variant=variant,expected_same_recording=k==reference,decision=ev.decision,
                seconds=round(time.monotonic()-start,4),retrieval_seconds=round(retrieval,4),retrieved=k in [m['key'] for m in short],
                similarity=round(v.similarity,4) if v else None))
            print(k,variant,reference,ev.decision,flush=True)
report=dict(kind='new-public-preview-pair-evaluation-v1',engine_sha256=hashlib.sha256((ROOT/'songfinder/matcher.py').read_bytes()).hexdigest(),
    sources=[dict(artist=(m:=json.loads((root/(k+'.json')).read_text()))['artistName'],title=m['trackName'],url=m['trackViewUrl'],sha256=hashlib.sha256((root/(k+'.m4a')).read_bytes()).hexdigest()) for k in refs],
    cases=rows,false_exact=sum(r['decision']=='exact' and not r['expected_same_recording'] for r in rows),
    positives=sum(r['expected_same_recording'] for r in rows),true_exact=sum(r['decision']=='exact' and r['expected_same_recording'] for r in rows),
    retrieval_recall=sum(r['retrieved'] for r in rows)/len(rows),provider_requests=0,
    limitation='Three unfamiliar public previews, source-labelled performers; positives are derived from identical audio. Not a blind human-labelled field set or cold discovery benchmark. No audio redistributed.')
args.output.write_text(json.dumps(report,indent=2)+'\n')
