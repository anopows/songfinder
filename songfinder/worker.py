"""One durable queue worker, networkless upload decode and bounded search children."""
import fcntl, json, os, shutil, signal, subprocess, sys, time
from pathlib import Path
from . import store, kpi

def heartbeat():
    with store.db() as c:c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('worker_heartbeat',str(time.time())))

def terminate(p):
    try:os.killpg(p.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    p.wait()

class Cancelled(Exception):pass

def run_bounded(cmd,seconds,*,jid=None,**kwargs):
    p=subprocess.Popen(cmd,start_new_session=True,**kwargs);deadline=time.monotonic()+seconds
    try:
        while p.poll() is None:
            heartbeat()
            if jid and store.get(jid)['cancel_requested']:
                terminate(p);raise Cancelled()
            if time.monotonic()>deadline:
                terminate(p);raise TimeoutError('Processing time limit reached')
            time.sleep(.5)
        return p.returncode
    finally:
        # Also reap subprocesses left behind if a failed parent exited first.
        try:os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:pass

def decode(jid):
    upload=store.ROOT/'uploads'/jid;audio=store.ROOT/'audio'/f'{jid}.m4a'
    sandbox=os.environ.get('SONGFINDER_BWRAP','/opt/songfinder/bin/bwrap')
    output_dir=store.ROOT/'uploads'/f'{jid}.decode'
    output_dir.mkdir(mode=0o700,exist_ok=True)
    decoded=output_dir/'sound.m4a'
    errorlog=output_dir/'decode.log'
    if os.environ.get('SONGFINDER_DEVELOPMENT')=='1' and sys.platform=='darwin':
        prefix=[];src=str(upload);dest=str(decoded)
    else:
        prefix=[sandbox,'--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL',
            '--ro-bind','/usr','/usr','--ro-bind','/lib','/lib','--ro-bind','/lib64','/lib64',
            '--ro-bind','/etc/alternatives','/etc/alternatives','--ro-bind','/etc/ld.so.cache','/etc/ld.so.cache',
            '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--ro-bind',str(upload),'/input',
            '--bind',str(output_dir),'/output','--clearenv','--setenv','PATH','/usr/bin']
        src='/input';dest='/output/sound.m4a'
    cmd=prefix+['/usr/bin/ffmpeg' if prefix else 'ffmpeg','-nostdin','-v','error','-y',
        '-protocol_whitelist','file,pipe','-threads','1','-i',src,'-map','0:a:0','-vn','-ac','1',
        '-ar','22050','-t','600','-c:a','aac','-b:a','96k','-threads','1',dest]
    try:
        with errorlog.open('wb') as log:
            code=run_bounded(cmd,90,jid=jid,stdout=subprocess.DEVNULL,stderr=log)
        if code or not decoded.exists() or decoded.stat().st_size<15000:
            raise ValueError('Decode failed: '+errorlog.read_bytes()[-3000:].decode('utf8','replace'))
        decoded.replace(audio)
    finally:
        upload.unlink(missing_ok=True)
        shutil.rmtree(output_dir,ignore_errors=True)

def process(job):
    jid=job['id'];started=time.monotonic();stage='decode'
    try:
        if job['cancel_requested']:raise Cancelled()
        audio=store.ROOT/'audio'/f"{job['audio_id']}.m4a"
        if job['audio_created']<time.time()-14*86400:raise ValueError('Retained audio has expired')
        if not audio.is_file():
            if job['audio_id']!=jid or not (store.ROOT/'uploads'/jid).is_file():raise ValueError('Audio is unavailable')
            store.event(jid,'Reading the sound from your file');decode(jid)
        stage='search'
        store.event(jid,'Comparing your clip with recordings')
        logpath=store.ROOT/'uploads'/f'{jid}.log'
        try:
            with logpath.open('wb') as log:
                code=run_bounded([sys.executable,'-m','songfinder.job',jid],max(30,360-(time.monotonic()-started)),jid=jid,stdout=log,stderr=log)
            if code:
                # Detailed traces remain private. Bound their size before storing.
                detail=logpath.read_bytes()[-6000:].decode('utf8','replace')
                store.update(jid,error=detail)
                if not json.loads(store.get(jid)['result']).get('verdict'):
                    store.update(jid,result=json.dumps({'verdict':'error','message':'This search could not finish. Please try another clip.'}))
        finally:logpath.unlink(missing_ok=True)
    except Cancelled:
        store.update(jid,result=json.dumps({'verdict':'cancelled','message':'This search was cancelled.'}))
    except TimeoutError:
        result=json.loads(store.get(jid)['result'])
        if stage=='decode':
            result={'verdict':'error','error_kind':'file','message':'Reading the sound from this file took too long. Try a shorter video or an audio file.'}
        elif result.get('verdict') not in ('exact','probable','tune'):
            result={'verdict':'timeout','message':'We reached the search time limit without confirming a match. Try a clearer clip or add the song title.'}
        store.update(jid,result=json.dumps(result),error=stage+' time limit reached')
    except Exception as exc:
        message='We could not read the sound from this file. Try another video or recording.' if stage=='decode' else 'This search could not finish. You can try again using the retained audio.'
        store.update(jid,result=json.dumps({'verdict':'error','error_kind':'file' if stage=='decode' else 'search','message':message}),error=type(exc).__name__+': '+str(exc)[:1000])
    finally:
        (store.ROOT/'uploads'/jid).unlink(missing_ok=True)
        with store.db() as c: store.finish(c,jid)

def recover():
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        stale=c.execute("SELECT * FROM jobs WHERE status='working'").fetchall()
        for j in stale:
            audio=store.ROOT/'audio'/f"{j['audio_id']}.m4a"
            available=(audio.is_file() or (store.ROOT/'uploads'/j['id']).is_file()) and j['audio_created']>=time.time()-14*86400
            if available and not j['cancel_requested'] and j['recoveries']<2:
                c.execute("UPDATE jobs SET status='queued',started=NULL,finished=NULL,recoveries=recoveries+1,updated=? WHERE id=?",(time.time(),j['id']))
                store.kpi_count(c,kpi.bump,'job.recovered')
                continue
            result=json.loads(j['result'])
            if j['cancel_requested']:result={'verdict':'cancelled','message':'This search was cancelled.'}
            elif result.get('verdict') not in ('exact','probable','tune'):result={'verdict':'error','message':'This search was interrupted. Try again with the retained audio if available.'}
            store.finish(c,j['id'],result=result,timed=False)
            (store.ROOT/'uploads'/j['id']).unlink(missing_ok=True)

def main():
    store.init()
    if '--purge' in sys.argv:store.purge();return
    lease=(store.ROOT/'worker.lock').open('w')
    fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    recover()
    last_purge=0
    while True:
        heartbeat()
        if time.time()-last_purge>3600:store.purge();last_purge=time.time()
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:c.execute("UPDATE jobs SET status='working',updated=?,started=? WHERE id=?",(time.time(),time.time(),row['id']))
        if row:process(dict(row))
        else:time.sleep(1)

if __name__=='__main__':main()
