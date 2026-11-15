"""One bounded search process. Parent kills its entire process group at deadline."""
import json, os, sys, time
from . import store, recognise, budget
from .service import identify_clip, Options
from .presentation import STEPS, _summarise

def wait_for_recognition_slot(service, deadline):
    # Cooldowns belong to later jobs; do not spend this search waiting them out.
    while time.monotonic() < deadline:
        budget.check()
        delay = store.budget_delay(service, store.budget_cap(service), 15 if service == 'shazam' else 2)
        if delay < 0 or delay > 15 or time.monotonic()+delay > deadline:
            return False
        if delay == 0:
            return True
        budget.sleep(min(delay, 1))
    return False


def main(jid):
    job=store.get(jid);deadline=time.monotonic()+320
    gate = lambda service: wait_for_recognition_slot(service, deadline)
    recognise.REQUEST_GATE=gate;recognise.REQUEST_BLOCK=store.block
    def event(e):
        line=e.get('detail','')
        for pattern,text in STEPS:
            if pattern.search(line):store.event(jid,text);break
    def result(report):
        store.update(jid,result=json.dumps(_summarise(report)),report=json.dumps(report))
    opts=Options(cache_dir=str(store.ROOT/'cache'),sources=tuple(os.environ.get('SONGFINDER_SOURCES','bandcamp,archive,deezer,itunes').split(',')))
    try:
        identify_clip(store.ROOT/'audio'/f"{job['audio_id']}.m4a",title=job['hint'] or None,artist=job['artist'] or None,options=opts,on_event=event,on_result=result)
    except TimeoutError:
        current=json.loads(store.get(jid)['result'])
        if current.get('verdict') not in ('exact','probable','tune'):
            current={'verdict':'timeout','message':'We reached the search time limit. Add a title or performer and try again.'}
        store.update(jid,result=json.dumps(current),error='Search time limit reached')

if __name__=='__main__':main(sys.argv[1])
