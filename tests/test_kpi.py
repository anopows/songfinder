import json, sqlite3, sys, time
from test_web import client, verify, create
from songfinder import kpi, store, api

def totals():
    with store.db() as c: return dict(c.execute('SELECT metric,sum(value) FROM kpi_daily GROUP BY metric'))

def test_flow_finish_feedback_and_export(client, tmp_path, monkeypatch):
    verify(client); jid=create(client,10)
    assert client.put(f'/api/clips/{jid}/parts/0',content=b'0123456789').status_code==200
    assert client.post(f'/api/clips/{jid}/complete',json={}).status_code==200
    with store.db() as c:
        c.execute('UPDATE jobs SET started=? WHERE id=?',(time.time(),jid))
        assert store.finish(c,jid,result={'verdict':'exact'})
        assert not store.finish(c,jid,result={'verdict':'exact'})
    for answer in ('yes','yes','no'):
        assert client.post(f'/api/clips/{jid}/feedback',json={'answer':answer}).status_code==200
    got=totals()
    assert all(got[k]==1 for k in ('session.verified','uniq.verified','clip.reserved','clip.queued','job.done.exact','feedback.no'))
    assert got['clip.bytes']==10 and got['feedback.yes']==0
    assert sum(v for k,v in got.items() if k.startswith('process_s.le.'))==1
    from songfinder import kpi_export
    monkeypatch.setattr(sys,'argv',['kpi_export',str(tmp_path/'stats.json')]); kpi_export.main()
    payload=json.loads((tmp_path/'stats.json').read_text())
    assert payload['schema']==1 and payload['site']=='songfinder' and payload['gauges']['queue']==0
    assert payload['days'][kpi.day_at()]['job.done.exact']==1

def test_expired_and_instrumentation_failure(client, monkeypatch):
    verify(client);jid=create(client)
    with store.db() as c:
        c.execute('UPDATE jobs SET updated=? WHERE id=?',(time.time()-400,jid))
        store.expire_uploads(c,time.time())
    assert totals()['job.done.upload_expired']==1 and 'job.done.error' not in totals()
    monkeypatch.setattr(kpi,'bump',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('failed')))
    jid=create(client)
    assert store.get(jid)['status']=='uploading'
    assert client.post(f'/api/clips/{jid}/cancel',json={}).status_code==200

def test_home_when_database_write_locked(client):
    lock=sqlite3.connect(store.ROOT/'jobs.sqlite3');lock.execute('BEGIN IMMEDIATE')
    try:
        started=time.monotonic(); assert client.get('/').status_code==200
        assert time.monotonic()-started<2
    finally: lock.rollback();lock.close()


def test_purge_prunes_and_recovery_does_not_time_downtime(client):
    from songfinder import worker
    verify(client);jid=create(client)
    with store.db() as c:
        c.execute("INSERT INTO kpi_seen VALUES(?,?,?)",(kpi.day_at(time.time()-86400),'uniq.visitors','old'))
        c.execute("UPDATE jobs SET status='working',started=?,recoveries=2 WHERE id=?",(time.time()-3600,jid))
    worker.recover()
    got=totals();assert got['job.done.error']==1
    assert not any(k.startswith(('queue_s.le.','process_s.le.')) for k in got)
    store.purge()
    with store.db() as c: assert c.execute("SELECT 1 FROM kpi_seen WHERE hash='old'").fetchone() is None

def test_exporter_reports_configured_caps(client, tmp_path, monkeypatch):
    from songfinder import kpi_export
    monkeypatch.setenv('SONGFINDER_AUDD_DAILY','7')
    monkeypatch.setattr(sys,'argv',['kpi_export',str(tmp_path/'stats.json')]); kpi_export.main()
    g=json.loads((tmp_path/'stats.json').read_text())['gauges']
    assert g['queue_limit']==store.MAX_WAITING and g['budget_cap.audd']==7
    assert g['budget_cap.shazam']==400 and g['budget_blocked_until.shazam'] is None
