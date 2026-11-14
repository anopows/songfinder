"""Export aggregate counters without exposing an HTTP endpoint."""
import argparse
import shutil
import time
from . import kpi, store

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('path'); args=parser.parse_args()
    store.init(); now=time.time()
    with store.db() as c:
        gauges={}
        gauges['queue']=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working')").fetchone()[0]
        gauges['uploading']=c.execute("SELECT count(*) FROM jobs WHERE status='uploading'").fetchone()[0]
        gauges['jobs_retained']=c.execute('SELECT count(*) FROM jobs').fetchone()[0]
        heartbeat=c.execute("SELECT value FROM meta WHERE key='worker_heartbeat'").fetchone()
        gauges['worker_heartbeat_age_s']=max(0,int(now-float(heartbeat[0]))) if heartbeat else None
        gauges['queue_limit']=store.MAX_WAITING
        for service in store.BUDGET_DEFAULTS:
            gauges['budget_cap.'+service]=store.budget_cap(service)
            gauges['budget_used.'+service]=0
            gauges['budget_blocked.'+service]=0
            gauges['budget_blocked_until.'+service]=None
        for row in c.execute('SELECT * FROM service_budget'):
            if row['service'] not in ('shazam','audd'): continue
            gauges['budget_used.'+row['service']]=row['used'] if row['day']==int(now//86400) else 0
            gauges['budget_blocked.'+row['service']]=int(row['blocked_until']>now)
            gauges['budget_blocked_until.'+row['service']]=row['blocked_until'] if row['blocked_until']>now else None
        gauges['disk_free_bytes']=shutil.disk_usage(store.ROOT).free
        payload=kpi.export(c,'songfinder',gauges)
    kpi.write_json(args.path,payload)

if __name__=='__main__': main()
