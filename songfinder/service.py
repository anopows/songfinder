"""Library entry point used by isolated web jobs; no CLI subprocess contract."""
from __future__ import annotations
import os, time
from dataclasses import dataclass
from types import SimpleNamespace
from . import cli, recognise
from .cache import FeatureCache
from .links import Spotify
from .sources import DEFAULT_SOURCES

@dataclass
class Options:
    cache_dir: str = '.songfinder-cache'
    sources: tuple = tuple(s for s in DEFAULT_SOURCES if s != 'youtube')
    shazam_calls: int = 8
    audd_calls: int = 2
    workers: int = 2
    seconds: int = 330

def identify_clip(audio_path, *, title=None, artist=None, options=None, on_event=None, on_result=None, should_stop=None):
    options=options or Options();started=time.monotonic()
    cli.load_env_file(cli.REPO_ENV);cli.load_env_file(os.path.expanduser('~/.env'))
    cache=FeatureCache(options.cache_dir);recognise.ANSWER_DIR=os.path.join(options.cache_dir,'answers')
    a=SimpleNamespace(title=title,artist=artist,urls=[],shazam=True,audd=options.audd_calls>0,
        shazam_calls=options.shazam_calls,audd_calls=options.audd_calls,workers=options.workers,
        rate_min=.7,rate_max=1.4,free_pitch=False,exhaustive=False,index=True,max_candidates=80)
    def stopped():return time.monotonic()-started>options.seconds or bool(should_stop and should_stop())
    def log(message):
        if stopped():raise TimeoutError('Search time limit reached')
        if on_event:on_event({'type':'progress','detail':message})
    def partial(q,ranked,notes):
        data={'clip_seconds':q.duration,'route':notes['route'],'shazam_hints':notes['hints'],
              'fingerprints_sent':notes['fingerprints'],'results':[cli.result_dict(r) for r in ranked if not r.skipped]}
        if on_result:on_result(data)
    ranked,q,notes,_=cli.run_identify(a,str(audio_path),cache,list(options.sources),log=log,on_result=partial,should_stop=stopped)
    partial(q,ranked,notes)
    data=cli._report(str(audio_path),q,ranked,notes,title,cache,Spotify())
    if on_result:on_result(data)
    return data
