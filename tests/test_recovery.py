import json, sys, time
import pytest
from test_web import client, verify, create
from songfinder import api, store, worker

def retained(c):
    verify(c);jid=create(c)
    (store.ROOT/'audio'/f'{jid}.m4a').write_bytes(b'retained-audio')
    store.update(jid,status='done')
    return jid

def test_renewal_preserves_owner_but_a_shared_link_does_not(client):
    jid=retained(client);owner=store.get(jid)['owner']
    client.cookies.delete('sf_session');verify(client)
    assert api.read_token(client.cookies.get('sf_owner'),'owner')==owner
    assert client.get('/api/clips/'+jid).json()['can_retry']
    response=client.post(f'/api/clips/{jid}/retry',json={'title':'Correct title','artist':'Right performer'})
    assert response.status_code==200,response.text
    retry=store.get(response.json()['id'])
    assert (retry['audio_id'],retry['audio_created'])==(jid,store.get(jid)['created'])
    assert retry['hint']=='Correct title' and retry['artist']=='Right performer' and retry['status']=='queued'
    client.cookies.clear();verify(client)
    assert not client.get('/api/clips/'+jid).json()['can_retry']
    assert client.post(f'/api/clips/{jid}/retry',json={}).status_code==403
    assert client.post(f"/api/clips/{retry['id']}/cancel",json={}).status_code==403

def test_retry_cannot_extend_audio_retention(client):
    jid=retained(client)
    with store.db() as c:c.execute('UPDATE jobs SET audio_created=? WHERE id=?',(time.time()-13*86400,jid))
    r=client.post(f'/api/clips/{jid}/retry',json={});assert r.status_code==200
    new=store.get(r.json()['id']);store.update(new['id'],status='done')
    assert new['audio_created']==store.get(jid)['audio_created']
    store.purge(time.time()+2*86400)
    assert not (store.ROOT/'audio'/f'{jid}.m4a').exists()
    assert client.post(f"/api/clips/{new['id']}/retry",json={}).status_code==403

def test_cancel_upload_and_retained_audio_search(client):
    verify(client);jid=create(client)
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'0123456789').status_code==200
    assert client.post(f'/api/clips/{jid}/cancel',json={}).status_code==200
    assert store.get(jid)['status']=='done' and not (store.ROOT/'uploads'/jid).exists()
    assert client.post(f'/api/clips/{jid}/complete',json={}).status_code==200
    assert store.get(jid)['status']=='done'
    source=retained(client)
    r=client.post(f'/api/clips/{source}/retry',json={});retry=r.json()['id']
    assert client.post(f'/api/clips/{retry}/cancel',json={}).status_code==200
    assert (store.ROOT/'audio'/f'{source}.m4a').exists()

def test_active_cancellation_kills_bounded_child(client):
    verify(client);jid=create(client);store.update(jid,status='working',cancel_requested=1)
    start=time.monotonic()
    with pytest.raises(worker.Cancelled):worker.run_bounded([sys.executable,'-c','import time;time.sleep(60)'],60,jid=jid)
    assert time.monotonic()-start<3

def test_only_homepage_is_indexable(client):
    home=client.get('/');assert home.headers['x-robots-tag']=='index, follow'
    assert 'og:title' in home.text and 'rel="canonical"' in home.text
    for path in ('/c/'+'a'*22,'/admin','/api/bootstrap'):
        r=client.get(path);assert r.headers['x-robots-tag']=='noindex, nofollow'
        assert 'og:title' not in r.text and 'rel="canonical"' not in r.text
    assert '<loc>'+api.ORIGIN+'/</loc>' in client.get('/sitemap.xml').text

def test_owner_can_delete_parent_without_removing_retry_audio(client):
    jid=retained(client);retry=client.post(f'/api/clips/{jid}/retry',json={}).json()['id']
    client.post('/api/admin/login',json={'password':'test-password'})
    assert client.post(f'/api/admin/clips/{jid}/delete',json={}).status_code==200
    assert (store.ROOT/'audio'/f'{jid}.m4a').exists()
    store.update(retry,status='done')
    assert client.post(f'/api/admin/clips/{retry}/delete',json={}).status_code==200
    assert not (store.ROOT/'audio'/f'{jid}.m4a').exists()

def test_database_backup_can_be_restored_without_changing_live_data(client,tmp_path):
    from songfinder.maintenance import backup
    jid=retained(client);destination=tmp_path/'private-backup.sqlite3'
    assert backup(destination)
    assert destination.stat().st_mode & 0o777==0o600
    assert store.get(jid)['status']=='done'
    with pytest.raises(FileExistsError):backup(destination)

def test_metrics_require_admin_and_are_aggregate(client):
    assert client.get('/api/admin/metrics').status_code==401
    retained(client);client.post('/api/admin/login',json={'password':'test-password'})
    report=client.get('/api/admin/metrics').json()
    assert report['jobs']==1 and 'owner' not in json.dumps(report)

def test_historical_result_is_relabelled_from_confirmed_report(client):
    jid=retained(client)
    report={'results':[{'decision':'exact','title':'A Song','artist':'Right Band','url':'https://example.bandcamp.com/track/a-song','audio':'full','similarity':.9}],
            'shazam_hints':[{'title':'A Song','artist':'Wrong Band'}]}
    store.update(jid,report=json.dumps(report),result=json.dumps({'verdict':'exact','artist':'Wrong Band'}))
    assert client.get('/api/clips/'+jid).json()['artist']=='Right Band'
