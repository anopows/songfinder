"""Durable jobs, privacy retention and shared recognition budgets."""
from __future__ import annotations
import contextlib, hashlib, hmac, json, os, secrets, shutil, sqlite3, time
from pathlib import Path

ROOT = Path(os.environ.get('SONGFINDER_DATA', 'web-data-public')).resolve()
MAX_BYTES = 500 * 1024 * 1024
PART_BYTES = 8 * 1024 * 1024
MAX_WAITING = 10

@contextlib.contextmanager
def db():
    con = sqlite3.connect(ROOT / 'jobs.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def init():
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in ('uploads', 'audio', 'cache'):
        (ROOT / name).mkdir(exist_ok=True)
    with db() as c:
        c.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
          status TEXT NOT NULL, owner TEXT NOT NULL, size INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0,
          name TEXT NOT NULL, hint TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '{}',
          report TEXT NOT NULL DEFAULT '{}', feedback TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created);
        CREATE TABLE IF NOT EXISTS limits(key TEXT NOT NULL, ts REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS limits_key ON limits(key,ts);
        CREATE TABLE IF NOT EXISTS service_budget(service TEXT PRIMARY KEY, day INTEGER NOT NULL,
          used INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('ip_salt', secrets.token_hex(32)))

def get(jid):
    with db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    return dict(row) if row else None

def update(jid, **values):
    allowed = {'status','received','steps','result','report','feedback','error','owner'}
    if not values.keys() <= allowed: raise ValueError('Invalid fields')
    values['updated'] = time.time()
    with db() as c:
        c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(),jid))

def event(jid, message):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT steps FROM jobs WHERE id=?',(jid,)).fetchone()
        if not row:return
        steps=json.loads(row[0])
        if not steps or steps[-1]!=message:
            steps=(steps+[message])[-20:]
            c.execute('UPDATE jobs SET steps=?,updated=? WHERE id=?',(json.dumps(steps),time.time(),jid))

def ip_key(ip):
    with db() as c:salt=c.execute("SELECT value FROM meta WHERE key='ip_salt'").fetchone()[0]
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()

def allow(key, cap, seconds):
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM limits WHERE ts<?',(now-86400,))
        n=c.execute('SELECT count(*) FROM limits WHERE key=? AND ts>?',(key,now-seconds)).fetchone()[0]
        if n>=cap:return False
        c.execute('INSERT INTO limits VALUES (?,?)',(key,now))
        return True

def reserve_job(owner, iphash, size, name, hint):
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM limits WHERE ts<?',(now-86400,))
        for key,cap,window in [('clip:'+iphash,5,3600),('clip:'+iphash,20,86400),('global',100,86400)]:
            count=c.execute('SELECT count(*) FROM limits WHERE key=? AND ts>?',(key,now-window)).fetchone()[0]
            if count>=cap:raise ValueError('The upload limit has been reached. Please try again later.')
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('uploading','queued','working')").fetchone()[0]>=MAX_WAITING:
            raise ValueError('The queue is full. Please try again in a few minutes.')
        jid=secrets.token_urlsafe(16)
        c.execute('INSERT INTO jobs(id,created,updated,status,owner,size,name,hint) VALUES (?,?,?,?,?,?,?,?)',
                  (jid,now,now,'uploading',owner,size,name[:120],hint[:120]))
        c.executemany('INSERT INTO limits VALUES (?,?)',[('clip:'+iphash,now),('global',now)])
    return jid

def budget_delay(service, cap, gap):
    """Reserve a real outbound call atomically, or return delay/-1 (daily cap)."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        now=time.time();day=int(now//86400)
        c.execute('INSERT OR IGNORE INTO service_budget(service,day) VALUES (?,?)',(service,day))
        r=dict(c.execute('SELECT * FROM service_budget WHERE service=?',(service,)).fetchone())
        used=r['used'] if r['day']==day else 0
        if used>=cap:return -1
        wait=max(r['next_at'],r['blocked_until'])-now
        if wait>0:return wait
        c.execute('UPDATE service_budget SET day=?,used=?,next_at=? WHERE service=?',(day,used+1,now+gap,service))
        return 0

def block(service, seconds):
    with db() as c:c.execute('UPDATE service_budget SET blocked_until=max(blocked_until,?) WHERE service=?',(time.time()+seconds,service))

def purge(now=None):
    now=now or time.time()
    with db() as c:
        rows=c.execute('SELECT id,created,status,updated FROM jobs').fetchall()
        for r in rows:
            jid=r['id']
            if r['created']<now-14*86400:
                (ROOT/'audio'/f'{jid}.m4a').unlink(missing_ok=True)
            if r['status']=='uploading' and r['updated']<now-3600:
                (ROOT/'uploads'/jid).unlink(missing_ok=True)
                c.execute("UPDATE jobs SET status='done',result=?,updated=? WHERE id=?",
                    (json.dumps({'verdict':'error','message':'The upload expired. Please upload again.'}),now,jid))
            if r['created']<now-90*86400 and r['status']=='done':
                c.execute('DELETE FROM jobs WHERE id=?',(jid,))
        c.execute('DELETE FROM limits WHERE ts<?',(now-86400,))
        # Owner cookies are no longer useful once a clip has completed.
        c.execute("UPDATE jobs SET owner='' WHERE status='done' AND updated<?",(now-86400,))
    known={r['id'] for r in rows}
    for path in (ROOT/'uploads').iterdir():
        if path.name not in known and path.stat().st_mtime<now-3600:
            if path.is_dir():shutil.rmtree(path)
            else:path.unlink(missing_ok=True)
    cache_files=sorted((p for p in (ROOT/'cache').rglob('*') if p.is_file()),key=lambda p:p.stat().st_mtime)
    total=sum(p.stat().st_size for p in cache_files)
    for p in cache_files:
        if total<=4*1024**3:break
        total-=p.stat().st_size;p.unlink(missing_ok=True)
