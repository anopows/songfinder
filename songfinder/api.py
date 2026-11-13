"""Public HTTP boundary. The separately supervised worker handles all media."""
from __future__ import annotations
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
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','X-Robots-Tag':'noindex, nofollow',
        'Permissions-Policy':'camera=(), microphone=(self), geolocation=()',
        'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; img-src 'self' data:; media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
    return response

def ip(request):
    host=request.client.host if request.client else ''
    if host in ('127.0.0.1','::1'):host=request.headers.get('x-real-ip',host)
    return host

def token(role):
    data=f'{role}:{int(time.time()+3600)}:{secrets.token_urlsafe(24)}'
    sig=hmac.new((SESSION_SECRET or 'local-test-only').encode(),data.encode(),hashlib.sha256).hexdigest()
    return data+':'+sig

def session(request,role='visitor'):
    value=request.cookies.get('sf_admin' if role=='admin' else 'sf_session','')
    try:
        data,sig=value.rsplit(':',1)
        kind,expires,_=data.split(':')
        good=hmac.new((SESSION_SECRET or 'local-test-only').encode(),data.encode(),hashlib.sha256).hexdigest()
        if kind!=role or int(expires)<time.time() or not hmac.compare_digest(sig,good):raise ValueError()
        return hashlib.sha256(value.encode()).hexdigest()
    except (ValueError,TypeError):raise HTTPException(401,'Please verify to continue.' if role=='visitor' else 'Please sign in.')

def set_cookie(response,role):
    response.set_cookie('sf_admin' if role=='admin' else 'sf_session',token(role),max_age=3600,
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
async def page():return FileResponse(STATIC/'index.html',media_type='text/html')

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
    response=JSONResponse({'ok':True});set_cookie(response,'visitor');return response

@app.post('/api/clips')
async def create(request:Request):
    owner=session(request);body=await small_json(request)
    size=body.get('size')
    if isinstance(size,bool) or not isinstance(size,int) or not 0<size<=store.MAX_BYTES:
        raise HTTPException(413,'Choose a file up to 500 MB.')
    if shutil.disk_usage(store.ROOT).free<6*1024**3:raise HTTPException(503,'Storage is busy. Please try again later.')
    name=body.get('name','');hint=body.get('title','')
    if not isinstance(name,str) or not isinstance(hint,str):raise HTTPException(400,'Invalid clip details.')
    try:jid=store.reserve_job(owner,store.ip_key(ip(request)),size,name,hint.strip())
    except ValueError as e:raise HTTPException(429,str(e))
    return {'id':jid,'part_bytes':store.PART_BYTES}

@app.put('/api/clips/{jid}/parts/{part}')
async def upload(jid:str,part:int,request:Request):
    owner=session(request)
    existing=job_or_404(jid)
    if existing["owner"]!=owner:raise HTTPException(403,"This upload belongs to another session.")
    async with locks.setdefault(jid,asyncio.Lock()):
        job=job_or_404(jid)
        if job['owner']!=owner:raise HTTPException(403,'This upload belongs to another session.')
        if job['status']!='uploading' or part<0:raise HTTPException(409,'This upload is already complete.')
        offset=part*store.PART_BYTES
        expected=min(store.PART_BYTES,job['size']-offset)
        if expected<=0 or offset>job['received']:raise HTTPException(409,'Please send the next part in order.')
        path=store.ROOT/'uploads'/jid
        # Only append the next part. Repeating a committed part is safely idempotent.
        duplicate=offset<job['received'];received=0
        try:
            with open(path,'r+b' if path.exists() else 'w+b') as f:
                f.seek(offset)
                async with asyncio.timeout(90):
                    async for piece in request.stream():
                        received+=len(piece)
                        if received>expected:raise HTTPException(413,'Upload part is too large.')
                        if not duplicate:f.write(piece)
                if received!=expected:raise HTTPException(400,'Upload part was interrupted. Try again.')
                if not duplicate:f.flush();os.fsync(f.fileno())
        except BaseException:
            if not duplicate and path.exists():
                with path.open('r+b') as f:f.truncate(job['received'])
            raise
        if not duplicate:store.update(jid,received=offset+received)
        return {'received':job['received'] if duplicate else offset+received}

@app.post('/api/clips/{jid}/complete')
async def complete(jid:str,request:Request):
    owner=session(request)
    existing=job_or_404(jid)
    if existing["owner"]!=owner:raise HTTPException(403,"This upload belongs to another session.")
    async with locks.setdefault(jid,asyncio.Lock()):
        job=job_or_404(jid)
        if job['owner']!=owner:raise HTTPException(403,'This upload belongs to another session.')
        if job['status']!='uploading':return {'id':jid}
        if job['received']!=job['size']:raise HTTPException(409,'Upload has not finished.')
        store.update(jid,status='queued')
    locks.pop(jid,None)
    return {'id':jid}

@app.get('/api/clips/{jid}')
async def status(jid:str):
    j=job_or_404(jid)
    out={k:j[k] for k in ('id','created','status','name','hint','feedback')}
    out['steps']=json.loads(j['steps']);out.update(json.loads(j['result']))
    with store.db() as c:out['ahead']=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working') AND created<?",(j['created'],)).fetchone()[0]
    return out

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

@app.get('/api/admin/clips')
async def admin_clips(request:Request):
    session(request,'admin')
    with store.db() as c:rows=c.execute('SELECT id,created,status,name,hint,result,feedback,error FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
    return {'clips':[dict(r) for r in rows]}

@app.get('/api/admin/clips/{jid}')
async def admin_clip(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    return {k:j[k] for k in ('id','created','steps','report','error')}

@app.get('/api/admin/clips/{jid}/audio')
async def audio(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    path=store.ROOT/'audio'/f'{jid}.m4a'
    if j['created']<time.time()-14*86400 or not path.is_file():raise HTTPException(404,'Audio has expired or is unavailable.')
    return FileResponse(path,media_type='audio/mp4')

@app.post('/api/admin/clips/{jid}/delete')
async def delete(jid:str,request:Request):
    session(request,'admin');j=job_or_404(jid)
    if j['status']!='done':raise HTTPException(409,'Wait for this search to finish.')
    (store.ROOT/'audio'/f'{jid}.m4a').unlink(missing_ok=True)
    (store.ROOT/'uploads'/jid).unlink(missing_ok=True)
    with store.db() as c:c.execute('DELETE FROM jobs WHERE id=?',(jid,))
    return {'ok':True}
