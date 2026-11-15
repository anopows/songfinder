"""Lossy landmark retrieval; every returned reference still needs audio verification.

Triplets use relative pitch and relative time. Votes propose recordings, never a
verdict. Missing/corrupt indexes fall back to the ordinary feature catalogue.
"""
from collections import Counter, defaultdict
from contextlib import closing
import hashlib, json, math, sqlite3
from pathlib import Path
import numpy as np
from . import budget
from scipy.ndimage import maximum_filter
from .features import BPO, FPS, FEATURE_VERSION

VERSION='triplet-v1:'+FEATURE_VERSION

def landmarks(f, expand=False):
    peaks=[]
    # Four prominent local peaks per second, independent of absolute loudness.
    maxima=(f==maximum_filter(f,size=(5,7))) & (f>.08)
    for a in range(0,f.shape[1],FPS):
        band,t=np.where(maxima[:,a:a+FPS]);amp=f[band,t+a]
        for i in np.argsort(amp)[-4:]:peaks.append((int(t[i]+a),int(band[i]),float(amp[i])))
    peaks.sort()
    for i,(ta,fa,_) in enumerate(peaks):
        nearby=[]
        for tb,fb,amp in peaks[i+1:]:
            if tb-ta>3*FPS:break
            if 2<tb-ta and abs(fb-fa)<48:nearby.append((tb,fb,amp))
        ends=sorted((p for p in nearby if p[0]-ta>=FPS//2),key=lambda p:p[2],reverse=True)[:2]
        for tc,fc,_ in ends:
            middle=sorted((p for p in nearby if p[0]<tc),key=lambda p:p[2],reverse=True)[:2]
            for tb,fb,_ in middle:
                df1=round((fb-fa)*12/BPO)+32;df2=round((fc-fa)*12/BPO)+32
                ratio=round((tb-ta)/(tc-ta)*31)
                for r in range(max(0,ratio-1),min(31,ratio+1)+1) if expand else (ratio,):
                    yield (df1<<14 | df2<<6 | r,ta,fa,tc-ta)

class LandmarkIndex:
    def __init__(self,root):self.root=Path(root);self.path=self.root/'retrieval.sqlite3'
    def connect(self):
        con=sqlite3.connect(self.path,timeout=20)
        con.execute('PRAGMA foreign_keys=ON');return con
    def init(self):
        with closing(self.connect()) as c, c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS recordings(id INTEGER PRIMARY KEY, key TEXT UNIQUE, meta TEXT, version TEXT);
            CREATE TABLE IF NOT EXISTS hashes(h INTEGER, recording INTEGER REFERENCES recordings(id) ON DELETE CASCADE,t INTEGER,f INTEGER,dt INTEGER);
            CREATE INDEX IF NOT EXISTS hash_lookup ON hashes(h);
            CREATE INDEX IF NOT EXISTS hash_recording ON hashes(recording);
            ''')
    def put(self,key,f,meta):
        rows=list(landmarks(f));self.init()
        with closing(self.connect()) as c,c:
            c.execute('DELETE FROM recordings WHERE key=?',(key,))
            rid=c.execute('INSERT INTO recordings(key,meta,version) VALUES (?,?,?)',(key,json.dumps(meta),VERSION)).lastrowid
            c.executemany('INSERT INTO hashes VALUES (?,?,?,?,?)',((h,rid,t,b,dt) for h,t,b,dt in rows))
    def prune(self,live_keys=None):
        if not self.path.exists():return
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            for key, in c.execute('SELECT key FROM recordings').fetchall():
                digest=hashlib.sha1(key.encode()).hexdigest()
                path=self.root/digest[:2]/(digest+'.npz')
                if not path.is_file():c.execute('DELETE FROM recordings WHERE key=?',(key,))

    def shortlist(self,f,limit=12):
        if not self.path.exists():return []
        query=defaultdict(list)
        for h,t,b,dt in landmarks(f,expand=True):query[h].append((t,b,dt))
        votes=defaultdict(Counter)
        with closing(self.connect()) as c:
            keys=list(query)
            for a in range(0,len(keys),300):
                budget.check()
                batch=keys[a:a+300]
                for h,rid,t,b,dt in c.execute('SELECT * FROM hashes WHERE h IN ('+','.join('?'*len(batch))+')',batch):
                    budget.check()
                    for qt,qb,qdt in query[h]:
                        rate=dt/qdt
                        if not .69<=rate<=1.42 or abs(b-qb)>18:continue
                        votes[rid][(round(rate/.025),round((t-rate*qt)/FPS/.6))]+=1
            scored=[]
            for rid,bins in votes.items():
                (r,o),n=bins.most_common(1)[0]
                scored.append((n+bins[(r,o-1)]+bins[(r,o+1)],rid))
            out=[]
            for n,rid in sorted(scored,reverse=True)[:limit]:
                row=c.execute('SELECT meta FROM recordings WHERE id=? AND version=?',(rid,VERSION)).fetchone()
                if row and n>=2:out.append(json.loads(row[0]))
            return out

def rebuild(root):
    """Explicit maintenance operation, outside the request's time budget."""
    from .cache import FeatureCache
    cache=FeatureCache(str(root));idx=LandmarkIndex(root);count=0;idx.init()
    live=set()
    for meta in cache.entries():
        hit=cache.get(meta['key'])
        if not hit:continue
        idx.put(meta['key'],hit[0],meta);live.add(meta['key']);count+=1
        if count%100==0:print(f'Indexed {count} references',flush=True)
    idx.prune()
    print(f'Indexed {count} references',flush=True)

if __name__=='__main__':
    import sys
    rebuild(sys.argv[1])
