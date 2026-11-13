"""Public HTTP boundary. The separately supervised worker handles all media."""
from __future__ import annotations
import html, statistics
import asyncio, base64, contextlib, hashlib, hmac, json, os, secrets, shutil, time
from pathlib import Path
import httpx
from argon2 import PasswordHasher
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from . import store

ORIGIN=os.environ.get('SONGFINDER_ORIGIN','http://127.0.0.1:8792').rstrip('/')
SITE_KEY=os.environ.get('SONGFINDER_TURNSTILE_SITE_KEY','')
SECRET=os.environ.get('SONGFINDER_TURNSTILE_SECRET','')
SESSION_SECRET=os.environ.get('SONGFINDER_SESSION_SECRET','')
ADMIN_HASH=os.environ.get('SONGFINDER_ADMIN_HASH','')
DEV=os.environ.get('SONGFINDER_DEVELOPMENT')=='1' and ORIGIN.startswith('http://127.0.0.1:')
STATIC=Path(__file__).parent/'static'
locks={}
UPLOAD_STALL_SECONDS=90

@contextlib.asynccontextmanager
async def job_lock(jid):
    # Count holders AND waiters; removing a held lock would allow concurrent writes.
    state=locks.setdefault(jid,{'lock':asyncio.Lock(),'users':0})
    state['users']+=1
    try:
        async with state['lock']:yield
    finally:
        state['users']-=1
        if state['users']==0 and locks.get(jid) is state:locks.pop(jid,None)

async def disk_call(fn,*args):
    # A cancelled request must wait for its disk write before rolling the part back.
    task=asyncio.create_task(asyncio.to_thread(fn,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@contextlib.asynccontextmanager
async def lifespan(app):
    if not DEV and (len(SESSION_SECRET)<32 or not SITE_KEY or not SECRET or not ADMIN_HASH):
        raise RuntimeError('Public deployment requires session, admin and Turnstile secrets')
    store.init()
    yield

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.exception_handler(HTTPException)
async def error(request,exc):return JSONResponse({'error':exc.detail},status_code=exc.status_code)

@app.middleware('http')
async def boundary(request, call_next):
    if request.method not in ('GET','HEAD'):
        if request.headers.get('origin')!=ORIGIN or request.headers.get('x-songfinder')!='1':
            return JSONResponse({'error':'Please submit from the Songfinder page.'},status_code=403)
        try: size=int(request.headers.get('content-length','0'))
        except ValueError:size=-1
        limit=store.PART_BYTES if request.method=='PUT' else 8192
        if size<0 or size>limit:return JSONResponse({'error':'Request is too large.'},status_code=413)
    response=await call_next(request)
    # Renew signed visitor verification during activity, without changing ownership.
    # Expired verification still requires Turnstile; the separate owner cookie survives.
    value=request.cookies.get('sf_session','')
    if request.url.path!='/api/session' and read_token(value,'visitor'):
        if int(value.split(':')[1])-time.time()<1800:
            identity=owner_identity(request) or hashlib.sha256(value.encode()).hexdigest()
            set_cookie(response,'visitor')
            set_cookie(response,'owner',identity,14*86400)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','X-Robots-Tag':'index, follow' if request.url.path in ('/','/robots.txt','/sitemap.xml') else 'noindex, nofollow',
        'Permissions-Policy':'camera=(), microphone=(self), geolocation=()',
        'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; img-src 'self' data:; media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
    return response

def ip(request):
    host=request.client.host if request.client else ''
    if host in ('127.0.0.1','::1'):host=request.headers.get('x-real-ip',host)
    return host

def token(role, identity=None, ttl=3600):
    data=f'{role}:{int(time.time()+ttl)}:{identity or secrets.token_urlsafe(24)}'
    sig=hmac.new((SESSION_SECRET or 'local-test-only').encode(),data.encode(),hashlib.sha256).hexdigest()
    return data+':'+sig

def read_token(value,role):
    try:
        data,sig=value.rsplit(':',1);kind,expires,identity=data.split(':')
        good=hmac.new((SESSION_SECRET or 'local-test-only').encode(),data.encode(),hashlib.sha256).hexdigest()
        if kind!=role or int(expires)<time.time() or not hmac.compare_digest(sig,good):raise ValueError()
        return identity
    except (ValueError,TypeError):return None

def owner_identity(request):
    return read_token(request.cookies.get('sf_owner',''),'owner')

def session(request,role='visitor'):
    value=request.cookies.get('sf_admin' if role=='admin' else 'sf_session','')
    if not read_token(value,role):raise HTTPException(401,'Please verify to continue.' if role=='visitor' else 'Please sign in.')
    return (owner_identity(request) if role=='visitor' else None) or hashlib.sha256(value.encode()).hexdigest()

def set_cookie(response,role,identity=None,ttl=3600):
    name={'admin':'sf_admin','owner':'sf_owner','visitor':'sf_session'}[role]
    response.set_cookie(name,token(role,identity,ttl),max_age=ttl,
                        secure=ORIGIN.startswith('https:'),httponly=True,samesite='strict',path='/')

async def small_json(request):
    body=bytearray()
    async for piece in request.stream():
        body.extend(piece)
        if len(body)>8192:raise HTTPException(413,'Request is too large.')
    try:
        data=json.loads(body)
        if not isinstance(data,dict):raise ValueError()
        return data
    except (ValueError,TypeError):raise HTTPException(400,'Invalid request.')

async def verify(request,value):
    if DEV:return
    if not isinstance(value,str) or not value or len(value)>2048:raise HTTPException(400,'Please complete the verification.')
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',data={
                'secret':SECRET,'response':value,'remoteip':ip(request)})
            answer=r.json()
        if not answer.get('success') or answer.get('hostname')!=ORIGIN.split('://')[1]:
            raise HTTPException(400,'Verification expired. Please try again.')
    except (httpx.HTTPError,ValueError):raise HTTPException(503,'Verification is unavailable. Please try again shortly.')

def job_or_404(jid):
    if len(jid)!=22 or not all(c.isalnum() or c in '-_' for c in jid):raise HTTPException(404,'Clip not found.')
    job=store.get(jid)
    if not job or job['created']<time.time()-90*86400:raise HTTPException(404,'This result has expired or does not exist.')
    return job

@app.get('/')
@app.get('/c/{jid}')
@app.get('/example')
async def page(request:Request):
    content=(STATIC/'index.html').read_text()
    if request.url.path=='/':
        origin=html.escape(ORIGIN,quote=True)
        content=content.replace('<meta name="robots" content="noindex">',
            '<meta name="robots" content="index,follow">'
            '<meta name="description" content="Find the recording behind a video or audio clip, including slowed-down music. Get listening links and playback speed.">'
            f'<link rel="canonical" href="{origin}/">'
            '<meta property="og:type" content="website">'
            '<meta property="og:title" content="song·finder — Find the recording behind your clip">'
            '<meta property="og:description" content="Upload a video or recording. Find the song, a listening link, and its speed in your clip.">'
            f'<meta property="og:url" content="{origin}/">'
            f'<meta property="og:image" content="{origin}/share.png">'
            '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            '<meta name="twitter:card" content="summary_large_image">')
    return HTMLResponse(content)

@app.get('/robots.txt')
async def robots():
    return HTMLResponse(f'User-agent: *\nAllow: /$\nDisallow: /c/\nDisallow: /api/\nDisallow: /admin\nSitemap: {ORIGIN}/sitemap.xml\n',media_type='text/plain')

@app.get('/sitemap.xml')
async def sitemap():
    return HTMLResponse('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+html.escape(ORIGIN)+'/</loc></url></urlset>',media_type='application/xml')

@app.get('/api/example')
async def example():return JSONResponse(json.loads((STATIC/'example.json').read_text()))

@app.get('/favicon.svg')
@app.get('/favicon.ico')
async def favicon():return FileResponse(STATIC/'favicon.svg',media_type='image/svg+xml')

@app.get('/share.png')
async def share_image():return FileResponse(STATIC/'share.png',media_type='image/png')

@app.get('/app.js')
async def script():return FileResponse(STATIC/'app.js',media_type='text/javascript')

@app.get('/healthz')
async def health():
    with store.db() as c:
        r=c.execute("SELECT value FROM meta WHERE key='worker_heartbeat'").fetchone()
        active=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working')").fetchone()[0]
    ok=bool(r and time.time()-float(r[0])<60)
    return JSONResponse({'ok':ok,'queue':active},status_code=200 if ok else 503)

@app.get('/api/bootstrap')
async def bootstrap(request:Request):
    try:session(request);valid=True
    except HTTPException:valid=False
    return {'site_key':SITE_KEY,'max_bytes':store.MAX_BYTES,'part_bytes':store.PART_BYTES,'verified':valid}

@app.post('/api/session')
async def make_session(request:Request):
    if not store.allow('session:'+store.ip_key(ip(request)),20,3600):raise HTTPException(429,'Please try again later.')
    body=await small_json(request)
    await verify(request,body.get('token'))
    identity=owner_identity(request)
    if not identity:
        try:identity=session(request)  # preserve an upload started before this release
        except HTTPException:identity=secrets.token_hex(32)
    response=JSONResponse({'ok':True});set_cookie(response,'visitor')
    set_cookie(response,'owner',identity,14*86400);return response

@app.post('/api/clips')
async def create(request:Request):
    owner=session(request);body=await small_json(request)
    size=body.get('size')
    if isinstance(size,bool) or not isinstance(size,int) or not 0<size<=store.MAX_BYTES:
        raise HTTPException(413,'Choose a file up to 500 MB.')
    name=body.get('name','');hint=body.get('title','');artist=body.get('artist','')
    if not isinstance(name,str) or not isinstance(hint,str) or not isinstance(artist,str):raise HTTPException(400,'Invalid clip details.')
    try:jid=store.reserve_job(owner,store.ip_key(ip(request)),size,name,hint.strip(),artist.strip())
    except ValueError as e:raise HTTPException(429,str(e))
    return {'id':jid,'part_bytes':store.PART_BYTES}

@app.put('/api/clips/{jid}/parts/{part}')
async def upload(jid:str,part:int,request:Request):
    owner=session(request)
    existing=job_or_404(jid)
    if existing["owner"]!=owner:raise HTTPException(403,"This upload belongs to another session.")
    async with job_lock(jid):
        job=job_or_404(jid)
        if job['owner']!=owner:raise HTTPException(403,'This upload belongs to another session.')
        if job['status']!='uploading' or part<0:raise HTTPException(409,"The upload stopped for too long. Please choose the file again." if job['status']=='done' else 'This upload is already complete.')
        offset=part*store.PART_BYTES
        expected=min(store.PART_BYTES,job['size']-offset)
        if expected<=0 or offset>job['received']:raise HTTPException(409,'Please send the next part in order.')
        path=store.ROOT/'uploads'/jid
        # Only append the next part. Repeating a committed part is safely idempotent.
        duplicate=offset<job['received'];received=0
        store.update(jid)  # receiving a bounded chunk counts as upload activity
        try:
            with open(path,'r+b' if path.exists() else 'w+b',buffering=0) as f:
                f.seek(offset)
                stream=request.stream().__aiter__();activity=time.monotonic()
                while True:
                    try:piece=await asyncio.wait_for(anext(stream),UPLOAD_STALL_SECONDS)
                    except StopAsyncIteration:break
                    except TimeoutError:raise HTTPException(408,'Upload stopped receiving data. Please try again.')
                    received+=len(piece)
                    if received>expected:raise HTTPException(413,'Upload part is too large.')
                    if not duplicate:await disk_call(f.write,piece)
                    if time.monotonic()-activity>30:
                        await asyncio.to_thread(store.update,jid);activity=time.monotonic()
                if received!=expected:raise HTTPException(400,'Upload part was interrupted. Try again.')
                if not duplicate:
                    await disk_call(f.flush)
                    await disk_call(os.fsync,f.fileno())
        except BaseException:
            if not duplicate and path.exists():
                with path.open('r+b') as f:await disk_call(f.truncate,job['received'])
            raise
        if not duplicate:store.update(jid,received=offset+received)
        return {'received':job['received'] if duplicate else offset+received}

@app.post('/api/clips/{jid}/complete')
async def complete(jid:str,request:Request):
    owner=session(request)
    existing=job_or_404(jid)
    if existing["owner"]!=owner:raise HTTPException(403,"This upload belongs to another session.")
    async with job_lock(jid):
        job=job_or_404(jid)
        if job['owner']!=owner:raise HTTPException(403,'This upload belongs to another session.')
        if job['status']!='uploading':return {'id':jid}
        try:store.enqueue(jid)
        except ValueError as e:raise HTTPException(409,str(e))
    return {'id':jid}

@app.get('/api/clips/{jid}')
async def status(jid:str,request:Request):
    j=job_or_404(jid)
    out={k:j[k] for k in ('id','created','status','name','hint','feedback')}
    owns=owner_identity(request)==j['owner'] and bool(j['owner'])
    if not owns:
        try:owns=session(request)==j['owner']
        except HTTPException:pass
    out['can_retry']=bool(owns and j['status']=='done' and j['audio_created']>=time.time()-14*86400 and (store.ROOT/'audio'/f"{j['audio_id']}.m4a").is_file())
    out['can_cancel']=bool(owns and j['status']!='done')
    out['artist_hint']=j['artist'] if owns else ''
    if owns:out.update(received=j['received'],clip_key=j['audio_id'])
    out['steps']=json.loads(j['steps']);out.update(json.loads(j['result']))
    # Reformat stored reports so historical results also get the identity fix.
    report=json.loads(j['report'])
    if report.get('results') and out.get('verdict') in ('exact','probable'):
        from .presentation import _summarise
        out.update(_summarise(report))
    with store.db() as c:out['ahead']=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working') AND created<?",(j['created'],)).fetchone()[0]
    return out

@app.post('/api/clips/{jid}/retry')
async def retry(jid:str,request:Request):
    owner=session(request);j=job_or_404(jid);body=await small_json(request)
    if j['owner']!=owner:raise HTTPException(403,'Only the uploader can retry this clip.')
    title=body.get('title','');artist=body.get('artist','')
    if not isinstance(title,str) or not isinstance(artist,str):raise HTTPException(400,'Invalid search details.')
    try:new_id=store.reserve_job(owner,store.ip_key(ip(request)),0,j['name'],title.strip(),artist.strip(),retry_from=jid)
    except ValueError as e:raise HTTPException(409,str(e))
    return {'id':new_id}

@app.post('/api/clips/{jid}/cancel')
async def cancel(jid:str,request:Request):
    owner=session(request);j=job_or_404(jid)
    if j['owner']!=owner:raise HTTPException(403,'Only the uploader can cancel this search.')
    async with job_lock(jid):
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            if j['status'] in ('uploading','queued'):
                c.execute("UPDATE jobs SET status='done',cancel_requested=1,result=?,updated=?,finished=? WHERE id=?",
                          (json.dumps({'verdict':'cancelled','message':'This search was cancelled.'}),time.time(),time.time(),jid))
                (store.ROOT/'uploads'/jid).unlink(missing_ok=True)
            elif j['status']=='working':c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?',(jid,))
    return {'ok':True}

@app.post('/api/clips/{jid}/feedback')
async def feedback(jid:str,request:Request):
    session(request);j=job_or_404(jid);body=await small_json(request)
    if j['status']!='done' or body.get('answer') not in ('yes','no'):raise HTTPException(400,'Invalid feedback.')
    if not store.allow('feedback:'+store.ip_key(ip(request)),30,3600):raise HTTPException(429,'Please try later.')
    store.update(jid,feedback=body['answer']);return {'ok':True}

@app.get('/admin')
async def admin_page():return FileResponse(STATIC/'admin.html',media_type='text/html')

@app.post('/api/admin/login')
async def admin_login(request:Request):
    if not store.allow('admin:'+store.ip_key(ip(request)),5,900):raise HTTPException(429,'Please wait 15 minutes before trying again.')
    body=await small_json(request)
    await verify(request,body.get('token'))
    try:
        if not isinstance(body.get('password'),str):raise ValueError()
        await asyncio.to_thread(PasswordHasher().verify,ADMIN_HASH,body['password'])
    except Exception:raise HTTPException(401,'Incorrect password.')
    response=JSONResponse({'ok':True});set_cookie(response,'admin');return response

@app.get('/api/admin/metrics')
async def metrics(request:Request):
    session(request,'admin')
    with store.db() as c:
        rows=c.execute('SELECT status,created,started,finished,result,report FROM jobs WHERE created>?',(time.time()-7*86400,)).fetchall()
        budgets=[dict(r) for r in c.execute('SELECT service,day,used,blocked_until FROM service_budget')]
    counts={};waits=[];durations=[];unavailable=0
    for row in rows:
        verdict=json.loads(row['result']).get('verdict',row['status'])
        counts[verdict]=counts.get(verdict,0)+1
        unavailable+=any(isinstance(issue,dict) and issue.get('kind') for issue in json.loads(row['report']).get('provider_issues',[]))
        if row['started']:waits.append(max(0,row['started']-row['created']))
        if row['started'] and row['finished']:durations.append(max(0,row['finished']-row['started']))
    return {'days':7,'jobs':len(rows),'outcomes':counts,'source_issues':unavailable,
            'median_queue_seconds':round(statistics.median(waits),1) if waits else None,
            'median_processing_seconds':round(statistics.median(durations),1) if durations else None,
            'p95_processing_seconds':round(sorted(durations)[min(len(durations)-1,int(len(durations)*.95))],1) if durations else None,
            'provider_budgets':budgets}

@app.get('/api/admin/clips')
async def admin_clips(request:Request):
    session(request,'admin')
    with store.db() as c:rows=c.execute('SELECT id,created,audio_created,status,name,hint,result,feedback,error FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
    return {'clips':[dict(r) for r in rows]}

@app.get('/api/admin/clips/{jid}')
async def admin_clip(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    return {k:j[k] for k in ('id','created','steps','report','error')}

@app.get('/api/admin/clips/{jid}/audio')
async def audio(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    path=store.ROOT/'audio'/f"{j['audio_id']}.m4a"
    if j['audio_created']<time.time()-14*86400 or not path.is_file():raise HTTPException(404,'Audio has expired or is unavailable.')
    return FileResponse(path,media_type='audio/mp4')

@app.post('/api/admin/clips/{jid}/delete')
async def delete(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    if j['status']!='done':raise HTTPException(409,'Wait for this search to finish.')
    (store.ROOT/'uploads'/jid).unlink(missing_ok=True)
    with store.db() as c:
        c.execute('DELETE FROM jobs WHERE id=?',(jid,))
        if not c.execute('SELECT 1 FROM jobs WHERE audio_id=?',(j['audio_id'],)).fetchone():
            (store.ROOT/'audio'/f"{j['audio_id']}.m4a").unlink(missing_ok=True)
    return {'ok':True}
