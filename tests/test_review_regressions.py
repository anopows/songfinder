import json,time
from types import SimpleNamespace
import pytest
from test_web import client,verify,create
from songfinder import api,cli,store,worker,service
from songfinder.cache import FeatureCache
import numpy as np

def test_empty_and_idle_uploads_expire_during_reservation(client,monkeypatch):
    now=time.time()
    old=[store.reserve_job('owner',ip,10,'test.wav','') for ip in ('ip-a','ip-b') for _ in range(3)]
    # Abandoned transfers do not consume the search queue's ten slots.
    newcomer=store.reserve_job('new','ip-c',10,'test.wav','')
    assert store.get(newcomer)['status']=='uploading'
    with pytest.raises(ValueError,match='Finish or cancel'):store.reserve_job('owner','ip-a',10,'test.wav','')
    idle=old[0];store.update(idle,received=5)
    (store.ROOT/'uploads'/idle).write_bytes(b'12345')
    monkeypatch.setattr(store.time,'time',lambda:now+181)
    store.reserve_job('new','ip-d',10,'test.wav','')
    assert all(store.get(j)['status']=='done' for j in old[1:])
    assert store.get(idle)['status']=='uploading'
    monkeypatch.setattr(store.time,'time',lambda:now+301)
    store.reserve_job('new','ip-e',10,'test.wav','')
    assert store.get(idle)['status']=='done' and not (store.ROOT/'uploads'/idle).exists()

def test_legacy_ten_abandoned_reservations_do_not_block_new_visitor(client):
    # Existing rows predating the per-IP cap still cannot monopolize the search queue.
    for i in range(10):store.reserve_job('legacy',str(i),10,'test.wav','')
    assert store.get(store.reserve_job('visitor','fresh',10,'test.wav',''))['status']=='uploading'

def test_admitted_upload_completes_even_when_queue_fills(client,monkeypatch):
    monkeypatch.setattr(store,'MAX_WAITING',1)
    first=store.reserve_job('owner','a',10,'a','');second=store.reserve_job('owner','b',10,'b','')
    store.update(first,received=10);store.enqueue(first);store.update(second,received=10)
    store.enqueue(second)
    assert store.get(second)['status']=='queued'
    with pytest.raises(ValueError,match='Lots of searches'):store.reserve_job('owner','c',10,'c','')

def test_upload_survives_expired_verification_and_reverification(client,monkeypatch):
    monkeypatch.setattr(store,'PART_BYTES',8);verify(client);jid=create(client,10)
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'abcdefgh').status_code==200
    owner=store.get(jid)['owner'];future=time.time()+3601
    monkeypatch.setattr(api.time,'time',lambda:future)
    assert client.put(f'/api/clips/{jid}/parts/1',content=b'ij').status_code==401
    verify(client)
    assert client.put(f'/api/clips/{jid}/parts/1',content=b'ij').status_code==200
    assert client.post(f'/api/clips/{jid}/complete',json={}).status_code==200
    assert store.get(jid)['owner']==owner
    assert (store.ROOT/'uploads'/jid).read_bytes()==b'abcdefghij'

def test_activity_renews_cookie_without_changing_owner(client,monkeypatch):
    verify(client);jid=create(client);before=client.cookies.get('sf_session');owner=store.get(jid)['owner']
    future=time.time()+1900;monkeypatch.setattr(api.time,'time',lambda:future)
    assert client.get('/api/clips/'+jid).status_code==200
    after=client.cookies.get('sf_session')
    assert after!=before and int(after.split(':')[1])>future+3500
    assert api.read_token(client.cookies.get('sf_owner'),'owner')==owner

def test_title_miss_falls_back_to_recognition_in_order(monkeypatch):
    stages=[]
    query=SimpleNamespace(F=None,duration=42,windows=[1,2,3],window_len=5,rates=[.7,1.4])
    cache=SimpleNamespace(entries=lambda:iter([]),shortlist=lambda f:[])
    monkeypatch.setattr(cli,'prepare_query',lambda *a,**k:query)
    monkeypatch.setattr(cli,'_query_opts',lambda a:{})
    monkeypatch.setattr(cli,'gather',lambda *a,**k:stages.append('title') or [])
    monkeypatch.setattr(cli,'recognise',lambda *a,**k:stages.append('recognise') or ([],1))
    monkeypatch.setattr(cli,'recognise_audd',lambda *a,**k:stages.append('fallback') or ([],1))
    options=SimpleNamespace(title='mistpyed title',artist=None,urls=[],max_candidates=80,exhaustive=False,workers=2,
                            shazam=True,shazam_calls=8,index=True,audd=True,audd_calls=2)
    cli.run_identify(options,'public.wav',cache,['bandcamp'],log=lambda s:None)
    assert stages[0]=='title' and stages[1:]==['recognise','fallback','recognise']

def test_worker_restart_resumes_retained_audio_without_decoding(client,monkeypatch):
    verify(client);jid=create(client);store.update(jid,status='working')
    path=store.ROOT/'audio'/f'{jid}.m4a';path.write_bytes(b'retained')
    worker.recover();assert store.get(jid)['status']=='queued'
    monkeypatch.setattr(worker,'decode',lambda jid:pytest.fail('retained audio must not be decoded again'))
    monkeypatch.setattr(worker,'run_bounded',lambda *a,**k:0)
    worker.process(store.get(jid));assert store.get(jid)['status']=='done' and path.exists()

def test_decode_timeout_is_a_file_problem(client,monkeypatch):
    verify(client);jid=create(client);(store.ROOT/'uploads'/jid).write_bytes(b'file')
    def timeout(jid):raise TimeoutError()
    monkeypatch.setattr(worker,'decode',timeout);worker.process(store.get(jid))
    result=json.loads(store.get(jid)['result'])
    assert result['verdict']=='error' and 'file took too long' in result['message']
    assert 'search time' not in result['message'].lower()

def test_cache_pruning_preserves_seeded_and_confirmed_references(client,monkeypatch):
    cache=FeatureCache(str(store.ROOT/'cache'))
    f=np.ones((132,200),np.float32)
    cache.put('seed',f,{'title':'rare recording'});cache.protect('seed')
    cache.put('disposable',f,{'title':'unconfirmed'})
    monkeypatch.setattr(store,'CACHE_BYTES',1)
    store.purge()
    assert cache.get('seed') is not None and cache.get('disposable') is None

def test_provider_budget_exhaustion_reaches_visitor_status(tmp_path,monkeypatch):
    from songfinder.presentation import _summarise
    monkeypatch.setattr(cli,'load_env_file',lambda *a:None)
    def search(a,clip,cache,sources,**kw):
        kw['log']('  Recognition budget reached; continuing without an excerpt')
        return [],SimpleNamespace(duration=30),dict(route='',hints=[],fingerprints=0,provider_issues=[{'source':'audd','kind':'budget'}]),False
    monkeypatch.setattr(cli,'run_identify',search)
    monkeypatch.setattr(cli,'_report',lambda *a:{'results':[]})
    report=service.identify_clip('public.wav',options=service.Options(cache_dir=str(tmp_path)))
    result=_summarise(report)
    assert result['verdict']=='unavailable'
    assert 'Shazam' not in result['message'] and 'AudD' not in result['message']
