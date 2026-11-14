import json, time
from test_web import client, verify, create
from songfinder import api, store, job
from songfinder.legal import LegalPages

def test_legal_and_removal_links(client, tmp_path, monkeypatch):
    directory=tmp_path/'legal';directory.mkdir()
    pages=LegalPages(directory);monkeypatch.setattr(api,'LEGAL',pages)
    monkeypatch.setattr(api,'ABUSE_EMAIL','abuse@example.test')
    assert client.get('/terms').status_code==404
    assert not client.get('/api/bootstrap').json()['legal_ready']
    for name in ('terms','privacy','legal'):
        (directory/(name+'.html')).write_text('<html><div id="report">{{OPERATOR}}</div></html>')
    pages._until=0
    assert client.get('/privacy').status_code==404
    for name in ('terms','privacy','legal'):
        (directory/(name+'.html')).write_text('<html><div id="report">Final contact</div></html>')
    pages._until=0
    assert client.get('/terms').headers['x-robots-tag']=='index, follow'
    assert client.get('/api/bootstrap').json()['abuse_email']=='abuse@example.test'
    assert '/privacy' in client.get('/sitemap.xml').text
    verify(client);jid=create(client)
    assert 'mailto:abuse@example.test?subject=Remove%20result%20'+jid in client.get('/c/'+jid).text

def test_zero_caps_block_without_consumption(client, monkeypatch):
    for name in ('shazam','audd'):
        monkeypatch.setenv('SONGFINDER_'+name.upper()+'_DAILY','0')
        assert store.budget_delay(name, store.budget_cap(name),15)==-1
        with store.db() as con:
            row=con.execute('SELECT used FROM service_budget WHERE service=?',(name,)).fetchone()
            assert row is None or row[0]==0

def test_job_zero_caps_do_not_wait_or_call_provider(client, monkeypatch):
    verify(client);jid=create(client)
    for name in ('shazam','audd'):monkeypatch.setenv('SONGFINDER_'+name.upper()+'_DAILY','0')
    def identify(path, **kwargs):
        for service in ('shazam','audd'):
            assert job.recognise.REQUEST_GATE(service) is False
        kwargs['on_result']({'verdict':'none','candidates':[]})
    monkeypatch.setattr(job,'identify_clip',identify)
    monkeypatch.setattr(job,'_summarise',lambda report:report)
    monkeypatch.setattr(job.time,'sleep',lambda _:(_ for _ in ()).throw(AssertionError('zero-cap job waited')))
    started=time.monotonic();job.main(jid)
    assert time.monotonic()-started<30
    assert json.loads(store.get(jid)['result'])['verdict']=='none'
