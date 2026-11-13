import json, time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from songfinder import api, store

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(api,'DEV',True)
    monkeypatch.setattr(api,'SESSION_SECRET','test'*16)
    monkeypatch.setattr(api,'ADMIN_HASH',api.PasswordHasher().hash('test-password'))
    api.locks.clear()
    with TestClient(api.app,base_url=api.ORIGIN) as client:
        client.headers.update({'Origin':api.ORIGIN,'X-Songfinder':'1'})
        yield client

def verify(c):assert c.post('/api/session',json={}).status_code==200

def create(c,size=10):
    r=c.post('/api/clips',json={'size':size,'name':'test.wav','title':''})
    assert r.status_code==200,r.text
    return r.json()['id']

def test_upload_requires_valid_session_and_origin(client):
    assert client.post('/api/clips',json={'size':10}).status_code==401
    verify(client)
    assert client.post('/api/clips',json={'size':10},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/clips',json={'size':store.MAX_BYTES+1}).status_code==413
    client.cookies.set('sf_session','visitor:9999999999:forged:bad')
    assert client.post('/api/clips',json={'size':10}).status_code==401

def test_chunk_order_retry_and_completion(client,monkeypatch):
    monkeypatch.setattr(store,'PART_BYTES',8)
    verify(client);jid=create(client,10)
    assert client.put(f'/api/clips/{jid}/parts/1',content=b'ij').status_code==409
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'abcdefgh').status_code==200
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'abcdefgh').status_code==200
    assert client.post(f'/api/clips/{jid}/complete',json={}).status_code==409
    assert client.put(f'/api/clips/{jid}/parts/1',content=b'i').status_code==400
    assert (store.ROOT/'uploads'/jid).read_bytes()==b'abcdefgh'
    assert client.put(f'/api/clips/{jid}/parts/1',content=b'ij').status_code==200
    assert client.post(f'/api/clips/{jid}/complete',json={}).status_code==200
    assert (store.ROOT/'uploads'/jid).read_bytes()==b'abcdefghij'
    assert store.get(jid)['status']=='queued'
    client.cookies.clear();verify(client)
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'abcdefgh').status_code==403

def test_public_result_does_not_expose_audio_or_secrets(client):
    verify(client);jid=create(client)
    store.update(jid,report=json.dumps({'private':'secret'}),error='private trace')
    body=client.get('/api/clips/'+jid).json()
    assert not {'owner','report','error'} & body.keys()
    assert client.get(f'/api/admin/clips/{jid}/audio').status_code==401
    assert client.get('/audio/'+jid+'.m4a').status_code==404

def test_daily_budget_is_atomic_and_survives_reload(client):
    with ThreadPoolExecutor(max_workers=10) as pool:
        delays=list(pool.map(lambda _:store.budget_delay('audd',5,0),range(20)))
    assert delays.count(0)==5 and delays.count(-1)==15
    store.init()
    assert store.budget_delay('audd',5,0)==-1
    assert store.budget_delay('shazam',400,15)==0
    assert store.budget_delay('shazam',400,15)>0

def test_rate_limit_cannot_be_reset_with_new_cookie(client):
    verify(client)
    for _ in range(5):create(client)
    verify(client)
    assert client.post('/api/clips',json={'size':10}).status_code==429

def test_retention_removes_audio_but_keeps_result_until_day_90(client):
    verify(client);jid=create(client)
    audio=store.ROOT/'audio'/f'{jid}.m4a';audio.write_bytes(b'private')
    store.update(jid,status='done')
    created=store.get(jid)['created']
    store.purge(created+15*86400)
    assert not audio.exists() and store.get(jid)
    store.purge(created+91*86400)
    assert store.get(jid) is None

def test_admin_auth_and_expired_audio(client):
    verify(client);jid=create(client)
    assert client.post('/api/admin/login',json={'password':'wrong'}).status_code==401
    assert client.post('/api/admin/login',json={'password':'test-password'}).status_code==200
    assert client.get('/api/admin/clips').status_code==200
    audio=store.ROOT/'audio'/f'{jid}.m4a';audio.write_bytes(b'private')
    assert client.get(f'/api/admin/clips/{jid}/audio').content==b'private'
    with store.db() as c:c.execute('UPDATE jobs SET audio_created=? WHERE id=?',(time.time()-15*86400,jid))
    assert client.get(f'/api/admin/clips/{jid}/audio').status_code==404

def test_turnstile_requires_live_verification(client,monkeypatch):
    monkeypatch.setattr(api,'DEV',False)
    assert client.post('/api/session',json={}).status_code==400
