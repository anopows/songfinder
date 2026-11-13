"""Durable jobs, privacy retention and shared recognition budgets."""
from __future__ import annotations
import contextlib, hashlib, hmac, json, os, secrets, shutil, sqlite3, time
from pathlib import Path

ROOT = Path(os.environ.get('SONGFINDER_DATA', 'web-data-public')).resolve()
MAX_BYTES = 500 * 1024 * 1024
PART_BYTES = 8 * 1024 * 1024
MAX_WAITING = 10
DISK_FLOOR = 6 * 1024**3
MAX_UPLOADS_PER_IP = 3
MAX_UPLOADS = 20
CACHE_BYTES = 4 * 1024**3
EMPTY_UPLOAD_SECONDS = 180
IDLE_UPLOAD_SECONDS = 300

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
        columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        for name,kind in [('artist',"TEXT NOT NULL DEFAULT ''"),('audio_id',"TEXT NOT NULL DEFAULT ''"),
                          ('audio_created','REAL NOT NULL DEFAULT 0'),('cancel_requested','INTEGER NOT NULL DEFAULT 0'),
                          ('started','REAL'),('finished','REAL'),('recoveries','INTEGER NOT NULL DEFAULT 0'),('upload_ip',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
        c.execute("UPDATE jobs SET audio_id=id,audio_created=created WHERE audio_id=''")
        c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('ip_salt', secrets.token_hex(32)))

def get(jid):
    with db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    return dict(row) if row else None

def update(jid, **values):
    allowed = {'status','received','steps','result','report','feedback','error','owner','cancel_requested','started','finished'}
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

def expire_uploads(c,now):
    stale=c.execute("SELECT id FROM jobs WHERE status='uploading' AND ((received=0 AND updated<?) OR updated<?)",
                    (now-EMPTY_UPLOAD_SECONDS,now-IDLE_UPLOAD_SECONDS)).fetchall()
    for row in stale:
        (ROOT/'uploads'/row['id']).unlink(missing_ok=True)
        c.execute("UPDATE jobs SET status='done',result=?,updated=?,finished=? WHERE id=?",
                  (json.dumps({'verdict':'error','message':'The upload stopped for too long. Please choose the file again.'}),now,now,row['id']))

def enqueue(jid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        job=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if job['status']!='uploading':return
        if job['received']!=job['size']:raise ValueError('Upload has not finished.')
        # Admission reserves completion too: never discard bytes already uploaded.
        c.execute("UPDATE jobs SET status='queued',updated=? WHERE id=?",(time.time(),jid))

def reserve_job(owner, iphash, size, name, hint, artist="", retry_from=None):
    now=time.time()
    # Expiry commits even when the following admission is rate-limited.
    with db() as c:
        c.execute('BEGIN IMMEDIATE');expire_uploads(c,now)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM limits WHERE ts<?',(now-86400,))
        limit_key=('retry:' if retry_from else 'clip:')+iphash
        for key,cap,window in [(limit_key,20 if retry_from else 5,3600),(limit_key,60 if retry_from else 20,86400),('global',100,86400)]:
            count=c.execute('SELECT count(*) FROM limits WHERE key=? AND ts>?',(key,now-window)).fetchone()[0]
            if count>=cap:raise ValueError("You've reached 5 searches this hour. Try again later." if not retry_from and window==3600 else 'You have reached the search limit. Try again later.')
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working')").fetchone()[0]>=MAX_WAITING:
            raise ValueError('Lots of searches right now. Try again in a few minutes.')
        if not retry_from:
            outstanding=c.execute("SELECT coalesce(sum(size-received),0) FROM jobs WHERE status='uploading'").fetchone()[0]
            if shutil.disk_usage(ROOT).free-outstanding-size<DISK_FLOOR:
                raise ValueError('Storage is busy. Please try again later.')
            if c.execute("SELECT count(*) FROM jobs WHERE status='uploading' AND upload_ip=?",(iphash,)).fetchone()[0]>=MAX_UPLOADS_PER_IP:
                raise ValueError('Finish or cancel another upload before starting one more.')
            if c.execute("SELECT count(*) FROM jobs WHERE status='uploading'").fetchone()[0]>=MAX_UPLOADS:
                raise ValueError('Uploads are busy. Please try again in a few minutes.')
        original=None
        if retry_from:
            original=c.execute('SELECT * FROM jobs WHERE id=?',(retry_from,)).fetchone()
            if not original or original['owner']!=owner or original['status']!='done':
                raise ValueError('This retry is unavailable.')
            if original['audio_created']<now-14*86400 or not (ROOT/'audio'/f"{original['audio_id']}.m4a").is_file():
                raise ValueError('The retained audio has expired. Please upload again.')
        jid=secrets.token_urlsafe(16)
        c.execute('INSERT INTO jobs(id,created,updated,status,owner,size,name,hint) VALUES (?,?,?,?,?,?,?,?)',
                  (jid,now,now,'uploading',owner,size,name[:120],hint[:120]))
        c.execute('UPDATE jobs SET artist=?,audio_id=?,audio_created=?,status=?,upload_ip=? WHERE id=?',
                  (artist[:120],original['audio_id'] if original else jid,
                   original['audio_created'] if original else now,'queued' if original else 'uploading',iphash,jid))
        c.executemany('INSERT INTO limits VALUES (?,?)',[(limit_key,now),('global',now)])
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
        expire_uploads(c,now)
        rows=c.execute('SELECT id,created,status,updated,audio_id,audio_created FROM jobs').fetchall()
        for r in rows:
            jid=r['id']
            if r['audio_created']<now-14*86400:
                (ROOT/'audio'/f"{r['audio_id']}.m4a").unlink(missing_ok=True)
            if r['created']<now-90*86400 and r['status']=='done':
                c.execute('DELETE FROM jobs WHERE id=?',(jid,))
        c.execute('DELETE FROM limits WHERE ts<?',(now-86400,))
        # Keep ownership only while the retained audio can be retried.
        c.execute("UPDATE jobs SET owner='',upload_ip='' WHERE status='done' AND audio_created<?",(now-14*86400,))
    known={r['id'] for r in rows}
    for path in (ROOT/'uploads').iterdir():
        if path.name not in known and path.stat().st_mtime<now-3600:
            if path.is_dir():shutil.rmtree(path)
            else:path.unlink(missing_ok=True)
    cache_files=sorted((p for p in (ROOT/'cache').rglob('*') if p.is_file() and p.suffix!='.keep' and not p.name.startswith('retrieval.sqlite3')),key=lambda p:p.stat().st_mtime)
    total=sum(p.stat().st_size for p in cache_files)
    for p in cache_files:
        if total<=CACHE_BYTES:break
        if p.suffix=='.npz' and p.with_suffix('.keep').exists():continue
        total-=p.stat().st_size;p.unlink(missing_ok=True)

    # Remove landmarks for evicted references; keep retrieval storage bounded too.
    from .retrieval import LandmarkIndex
    try:LandmarkIndex(ROOT/'cache').prune()
    except Exception:pass  # cache retrieval remains optional
