import asyncio,json,time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import numpy as np
import pytest
from test_web import client,verify,create
from songfinder import api,cli,store,service,recognise
from songfinder.presentation import _summarise
from songfinder.sources import Bandcamp,Candidate
from songfinder.cache import FeatureCache
from songfinder.retrieval import LandmarkIndex
from songfinder.matcher import Query

def options(**kw):
    d=dict(title=None,artist=None,urls=[],max_candidates=80,exhaustive=False,workers=2,
           shazam=True,shazam_calls=8,index=True,audd=True,audd_calls=2)
    return SimpleNamespace(**(d|kw))

def setup_search(monkeypatch,entries=()):
    q=SimpleNamespace(F=None,duration=30,windows=[1,2,3],window_len=5,rates=[.7,1.4])
    monkeypatch.setattr(cli,'prepare_query',lambda *a,**k:q)
    monkeypatch.setattr(cli,'_query_opts',lambda a:{})
    return SimpleNamespace(entries=lambda:iter(entries),shortlist=lambda f:[])

@pytest.mark.parametrize('title',['Ordinary Song','I Failed to Refuse'])
def test_clean_no_match_and_titles_do_not_report_source_errors(tmp_path,monkeypatch,title):
    setup_search(monkeypatch)
    monkeypatch.setattr(cli,'load_env_file',lambda *a:None)
    monkeypatch.setattr(cli,'gather',lambda *a,**k:[Candidate('bandcamp','https://example.test/track',title)])
    monkeypatch.setattr(cli,'identify',lambda *a,**k:[])
    monkeypatch.setattr(cli,'recognise',lambda *a,**k:([],0))
    monkeypatch.setattr(cli,'recognise_audd',lambda *a,**k:([],0))
    monkeypatch.setattr(cli,'_report',lambda *a:{'results':[]})
    report=service.identify_clip('public.wav',title=title,options=service.Options(cache_dir=str(tmp_path)))
    assert report['provider_issues']==[]
    assert _summarise(report)['verdict']=='none'
    assert _summarise({'provider_issues':['Checking 8 candidates (title):']})['verdict']=='none'

def test_full_scan_cannot_starve_speed_or_fallback_recognition(monkeypatch):
    entries=[dict(source='bandcamp',url=f'https://b.test/{i}',title='song') for i in range(5000)]
    cache=setup_search(monkeypatch,entries);stages=[]
    monkeypatch.setattr(cli,'recognise',lambda *a,**k:stages.append('original' if k['rates']==[1.] else 'corrected') or ([],1))
    monkeypatch.setattr(cli,'recognise_audd',lambda *a,**k:stages.append('fallback') or ([],1))
    monkeypatch.setattr(cli,'identify',lambda *a,**k:stages.append('full scan') or [])
    cli.run_identify(options(),'public.wav',cache,[],log=lambda s:None)
    assert stages==['original','corrected','fallback','full scan']

def test_expanded_bandcamp_query_discovers_rare_track_and_filters_title(monkeypatch):
    calls=[]
    def search(url,body):
        calls.append(body['search_text'])
        return {'auto':{'results':[{'type':'t','item_url_path':'https://rare.test/track','name':'Some Rare Song','band_name':'Unknown Quartet'},
                                   {'type':'t','item_url_path':'https://wrong.test/track','name':'Different Song'}] if body['search_text']=='Some Rare Song and' else []}}
    monkeypatch.setattr('songfinder.sources.fetch_json',search)
    found=Bandcamp().search('Some Rare Song')
    assert [c.url for c in found]==['https://rare.test/track']
    assert 'Some Rare Song the' in calls

def test_title_only_full_bandcamp_candidates_get_separate_allowance(monkeypatch):
    cache=setup_search(monkeypatch);checked=[]
    candidates=[Candidate('archive',f'https://a.test/{i}','Song') for i in range(80)]
    candidates += [Candidate('bandcamp',f'https://b.test/{i}','Song') for i in range(125)]
    monkeypatch.setattr(cli,'gather',lambda *a,**k:candidates)
    monkeypatch.setattr(cli,'identify',lambda q,c,*a,**k:checked.extend(c) or [])
    cli.run_identify(options(title='Song',shazam=False,audd=False),'public.wav',cache,['bandcamp','archive'],log=lambda s:None)
    assert len([c for c in checked if c.source=='bandcamp'])==125
    assert checked[0].source=='bandcamp' and len(checked)==155

def test_quiet_slots_replaced_by_active_disjoint_windows():
    rng=np.random.default_rng(782);f=rng.normal(size=(132,750)).astype('float32')
    f[:,:100]=0;f[:,320:370]=0
    q=Query(f)
    assert len(q.windows)>=3 and len(q.holdouts)>=3
    for a,b in q.windows+q.holdouts:
        assert np.mean(np.abs(f[:,int(a*25):int(b*25)]).sum(axis=0)>0)>=.55
    for a,b in q.windows:
        for c,d in q.holdouts:assert b<=c or d<=a
    assert q.evaluate(f).decision=='exact'

def test_concurrent_reservations_account_for_outstanding_bytes(client,monkeypatch):
    monkeypatch.setattr(store.shutil,'disk_usage',lambda p:SimpleNamespace(free=store.DISK_FLOOR+1000))
    def reserve(i):
        try:return store.reserve_job('owner',str(i),400,'public.wav','')
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=10) as pool:ids=list(pool.map(reserve,range(10)))
    assert sum(bool(i) for i in ids)==2
    j=next(i for i in ids if i);store.update(j,received=400)
    # The test's disk reading stays fixed: newly committed bytes aren't outstanding.
    assert reserve(11)

def test_retries_have_separate_more_generous_rate_limit(client):
    verify(client);jid=create(client);store.update(jid,status='done')
    (store.ROOT/'audio'/f'{jid}.m4a').write_bytes(b'public-sample')
    for _ in range(8):
        r=client.post(f'/api/clips/{jid}/retry',json={});assert r.status_code==200,r.text
        store.update(r.json()['id'],status='done')
    for _ in range(4):
        j=create(client);store.update(j,status='done')
    assert client.post('/api/clips',json={'size':10}).status_code==429

def test_prune_keeps_put_after_stale_snapshot(tmp_path):
    cache=FeatureCache(str(tmp_path));f=np.ones((132,100),np.float32)
    cache.put('old',f,{'title':'old'});snapshot={'old'}
    cache.put('new',f,{'title':'new'})
    LandmarkIndex(tmp_path).prune(snapshot)
    with LandmarkIndex(tmp_path).connect() as c:assert {r[0] for r in c.execute('select key from recordings')}=={'old','new'}

@pytest.mark.parametrize('stall',[False,True])
def test_stream_progress_deadline_and_rollback(client,monkeypatch,stall):
    verify(client);jid=create(client,4);owner=store.get(jid)['owner']
    monkeypatch.setattr(api,'UPLOAD_STALL_SECONDS',.06)
    class Request:
        cookies={'sf_session':api.token('visitor'),'sf_owner':api.token('owner',owner)}
        async def stream(self):
            for i in range(4):
                await asyncio.sleep(.12 if stall and i==2 else .025)
                yield b'x'
    async def run():
        start=time.monotonic()
        if stall:
            with pytest.raises(api.HTTPException) as e:await api.upload(jid,0,Request())
            assert e.value.status_code==408
            assert (store.ROOT/'uploads'/jid).read_bytes()==b''
            assert store.get(jid)['received']==0
        else:
            assert (await api.upload(jid,0,Request()))['received']==4
            assert time.monotonic()-start>.06
        assert not api.locks
    asyncio.run(run())

def test_throttle_wait_gives_up_before_budget_without_sleep(monkeypatch):
    from shazamio import Shazam
    from shazamio.exceptions import BadResponseStatus
    async def refused(self,data):raise BadResponseStatus('test refusal')
    async def no_sleep(delay):pytest.fail(f'Budget would be exceeded by sleep {delay}')
    monkeypatch.setattr(Shazam,'recognize',refused)
    monkeypatch.setattr(recognise,'_variant',lambda *a:b'x'*50001)
    monkeypatch.setattr(recognise,'cached_answer',lambda *a:None)
    monkeypatch.setattr(recognise,'REQUEST_GATE',None);monkeypatch.setattr(recognise,'REQUEST_BLOCK',None)
    monkeypatch.setattr(recognise,'_last_request',0)
    monkeypatch.setattr(recognise.asyncio,'sleep',no_sleep)
    issues=[]
    hits,calls=asyncio.run(recognise._run('public.wav',30,[1.],2,0,lambda s:None,remaining=lambda:10,on_issue=lambda *v:issues.append(v)))
    assert hits==[] and calls==1 and ('shazam','throttled') in issues

def test_catalogue_exception_emits_structured_signal(monkeypatch):
    from songfinder.pipeline import gather
    def broken(*a):raise ConnectionError('test offline')
    monkeypatch.setattr(Bandcamp,'search',broken);issues=[]
    assert gather('A Song',None,['bandcamp'],lambda s:None,on_issue=lambda *v:issues.append(v))==[]
    assert issues==[('bandcamp','catalogue_unavailable')]

def test_shared_status_never_exposes_resume_offset_or_family(client):
    verify(client);jid=create(client)
    assert 'received' in client.get('/api/clips/'+jid).json()
    client.cookies.clear();out=client.get('/api/clips/'+jid).json()
    assert 'received' not in out and 'clip_key' not in out

def test_static_public_example_does_not_expire_or_expose_audio(client,monkeypatch):
    future=time.time()+1000*86400
    monkeypatch.setattr(api.time,'time',lambda:future)
    out=client.get('/api/example').json()
    assert out['verdict']=='exact' and not out['can_retry']
    assert not {'owner','audio_id','report','received','clip_key'} & out.keys()
    assert client.get('/example').status_code==200
    assert 'summary_large_image' in client.get('/').text
    assert client.get('/favicon.ico').status_code==200
    assert client.get('/share.png').content.startswith(b'\x89PNG')
