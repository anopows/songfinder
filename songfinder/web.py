"""Private prototype site: upload a clip, run `songfinder identify`, show the result.

Standard library only. One clip at a time, results kept as JSON files, uploads
deleted when the job ends. Not hardened for the public internet: no bot check,
no rate limits. Run it on a private network only.

    python -m songfinder.web --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 500 * 1024 * 1024
JOB_SECONDS = 180              # wall clock per clip; then report what was found
MAX_WAITING = 10
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")

DATA = "web-data"
jobs: dict[str, dict] = {}
lock = threading.Lock()
waiting: "queue.Queue[str]" = queue.Queue()

# Step shown to the visitor for each kind of log line. Engines are never named.
STEPS = [
    (re.compile(r"^Asking (Shazam|AudD)"), "Checking the big music catalogues"),
    (re.compile(r"local index"), "Comparing with recordings we already know"),
    (re.compile(r"^Confirmed against a preview"), "Looking for a full-length link"),
    (re.compile(r"^Searching "), "Searching for versions of the song"),
    (re.compile(r"^Checking \d+ candidates"), "Checking candidate recordings"),
]


def _path(jid: str, ext: str) -> str:
    return os.path.join(DATA, f"{jid}.{ext}")


def _save(job: dict) -> None:
    public = {k: v for k, v in job.items() if not k.startswith("_")}
    tmp = _path(job["id"], "tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(public, fh)
    os.replace(tmp, _path(job["id"], "json"))


def _load(jid: str) -> dict | None:
    with lock:
        if jid in jobs:
            return jobs[jid]
    try:
        with open(_path(jid, "json"), encoding="utf-8") as fh:
            job = json.load(fh)
    except (OSError, ValueError):
        return None
    if job.get("status") in ("queued", "working"):      # the server restarted mid-job
        job.update(status="done", verdict="error", message="This search was interrupted. Please upload again.")
    return job


def _step(job: dict, text: str) -> None:
    with lock:
        if not job["steps"] or job["steps"][-1] != text:
            job["steps"].append(text)


def _speed(rate: float, pitch: float) -> dict:
    if abs(rate - 1) < 0.015:
        return {"big": "Original", "note": "Played at the original speed.", "tip": ""}
    word = "Slowed down" if rate < 1 else "Sped up"
    how = "pitch kept" if abs(pitch) < 0.3 else "pitch moved with it"
    return {"big": f"{rate * 100:.1f}%", "note": f"{word}, {how}.",
            "tip": f"To hear it as in your clip, play the original at {rate:.2f}×."}


def _summarise(report: dict) -> dict:
    """Pick the best result from a songfinder JSON report and phrase it for the page."""
    order = {"exact": 2, "probable": 1}
    rs = [r for r in report.get("results", []) if not r.get("error") and r.get("decision") in order]
    rs.sort(key=lambda r: (order[r["decision"]], r["decision"] == "exact" and r.get("audio") == "full",
                           r.get("similarity", 0)), reverse=True)
    if not rs:
        return {"verdict": "none"}
    top = rs[0]
    title, artist = top.get("title") or "Untitled", top.get("artist") or ""
    for h in report.get("shazam_hints", []):        # a recogniser's credit beats an upload's channel name
        ht = re.sub(r"\s*[\(\[].*$", "", h.get("title", "")).strip().lower()
        if ht and ht in title.lower():
            title, artist = h["title"], h.get("artist") or artist
            break
    # The report's links are already ordered best first (Spotify first). One per
    # service, and never a recognition engine's own page.
    links, seen = [], set()
    for l in report.get("links", []):
        if not isinstance(l, dict) or not l.get("url"):
            continue
        platform = l.get("platform") or l.get("label", "")
        host = urlsplit(l["url"]).hostname or ""
        if platform == "recogniser" or "shazam." in host or "audd." in host or platform in seen:
            continue
        seen.add(platform)
        label = re.sub(r"\s*\(search\)$", "", l.get("label") or platform)
        links.append({"label": label, "url": l["url"], "search": l.get("how") == "search"})
    if not links:
        host = (urlsplit(top["url"]).hostname or "").removeprefix("www.")
        label = {"deezer.com": "Deezer", "youtube.com": "YouTube", "archive.org": "Internet Archive",
                 "soundcloud.com": "SoundCloud", "music.apple.com": "Apple Music",
                 "open.spotify.com": "Spotify"}.get(host, "Bandcamp" if host.endswith("bandcamp.com") else host)
        if "shazam." not in host:
            links.append({"label": label, "url": top["url"], "search": False})
    full = top.get("audio") == "full"
    off = max(top.get("reference_offset_s", 0.0), 0.0)
    return {"verdict": top["decision"], "title": title, "artist": artist, "album": top.get("album") or "",
            "links": links[:6], "preview_only": not full,
            "speed": _speed(top.get("rate", 1.0), top.get("clip_pitch_semitones", 0.0)),
            "starts_at": f"{int(off // 60)}:{int(off % 60):02d}" if full else "",
            "similarity": round(top.get("similarity", 0.0), 2)}


def _run(job: dict) -> None:
    jid, upload = job["id"], job["_upload"]
    sound, report = _path(jid, "m4a"), _path(jid, "report")
    try:
        got = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", upload, "-vn", "-ac", "1",
                              "-ar", "22050", "-t", "600", "-c:a", "aac", "-b:a", "96k", sound],
                             capture_output=True, text=True, timeout=120)
        os.remove(upload)
        if got.returncode != 0 or not os.path.exists(sound) or os.path.getsize(sound) < 2000:
            job.update(verdict="error", message="We couldn't read any sound from this file.")
            return
        _step(job, "Took the sound from your file")
        cmd = [sys.executable, "-m", "songfinder", "identify", sound, "--json", report,
               "--shazam", "--shazam-calls", "8", "--audd"]
        if job.get("hint"):
            cmd += ["--title", job["hint"]]
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        killer = threading.Timer(JOB_SECONDS, proc.kill)
        killer.start()
        tail = []
        for line in proc.stderr:
            line = line.strip()
            tail = (tail + [line])[-5:]
            for pattern, text in STEPS:
                if pattern.search(line):
                    _step(job, text)
                    break
        proc.wait()
        timed_out = not killer.is_alive()
        killer.cancel()
        if os.path.exists(report):
            with open(report, encoding="utf-8") as fh:
                job.update(_summarise(json.load(fh)))
        elif timed_out:
            job.update(verdict="none", message="We stopped after three minutes without a match.")
        elif proc.returncode == 2 and not any("Error" in t for t in tail):
            job.update(verdict="none")
        else:
            job.update(verdict="error", message="Something went wrong while searching.")
            print(f"[{jid}] exit {proc.returncode}: {tail}", file=sys.stderr, flush=True)
    except Exception as e:                              # never let one clip stop the worker
        job.update(verdict="error", message="Something went wrong while searching.")
        print(f"[{jid}] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    finally:
        for p in (upload, sound, report):
            with_suppress_remove(p)
        job.update(status="done", seconds=round(time.time() - job["created"]))
        _save(job)


def with_suppress_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def worker() -> None:
    while True:
        job = jobs[waiting.get()]
        job["status"] = "working"
        _run(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "songfinder-web"

    def log_message(self, *a):          # no access log
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/" or path.startswith("/c/"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._json(200, {"ok": True})
        m = re.match(r"^/api/clips/([^/]+)$", path)
        if m and ID_RE.match(m.group(1)):
            job = _load(m.group(1))
            if not job:
                return self._json(404, {"error": "We don't have this clip."})
            with lock:
                out = {k: v for k, v in job.items() if not k.startswith("_")}
                if job["status"] == "queued":
                    out["ahead"] = sum(1 for j in jobs.values()
                                       if j["status"] in ("queued", "working") and j["created"] < job["created"])
            return self._json(200, out)
        self._json(404, {"error": "Not found."})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != "/api/clips":
            return self._json(404, {"error": "Not found."})
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(411, {"error": "The upload has no size."})
        if size <= 0 or size > MAX_BYTES:
            return self._json(413, {"error": "The file is too large. The limit is 500 MB."})
        if waiting.qsize() >= MAX_WAITING:
            return self._json(503, {"error": "Busy right now. Please try again in a few minutes."})
        q = parse_qs(url.query)
        jid = secrets.token_urlsafe(16)
        upload = _path(jid, "upload")
        left = size
        with open(upload, "wb") as fh:
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left:
            with_suppress_remove(upload)
            return self._json(400, {"error": "The upload was cut off. Please try again."})
        job = {"id": jid, "status": "queued", "created": time.time(), "steps": [],
               "name": (q.get("name", [""])[0])[:120], "hint": (q.get("title", [""])[0]).strip()[:120],
               "_upload": upload}
        with lock:
            jobs[jid] = job
        _save(job)
        waiting.put(jid)
        self._json(200, {"id": jid})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex"><title>song·finder</title>
<style>
:root{color-scheme:dark;--bg:#0C0C0E;--s1:#18181C;--line:#303037;--tx:#F4F2EC;--mu:#8F8E96;--so:#B9B7BF;
--brass:#E8B44C;--ink:#1D1404;--warm:#F2D18B;--ok:#9ED7AE;--red:#F09A85;
--display:"Arial Black","Helvetica Neue",system-ui,sans-serif}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:460px;margin:0 auto;padding:20px 20px 32px;display:flex;flex-direction:column;gap:18px;min-height:100dvh}
a{color:var(--brass);text-decoration:none}
:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
.logo{font:900 19px/1 var(--display);letter-spacing:-.04em;color:var(--tx);padding:12px 0;display:inline-block}.logo i{font-style:normal;color:var(--brass)}
h1{margin:0;font:900 34px/1.04 var(--display);letter-spacing:-.03em;overflow-wrap:anywhere}
p{margin:0}.soft{color:var(--so);font-size:15px;line-height:1.45}.small{color:var(--mu);font-size:13px;line-height:1.5}
.btn{display:flex;align-items:center;justify-content:center;gap:10px;min-height:54px;padding:0 18px;border-radius:14px;border:1px solid var(--line);background:var(--s1);color:var(--tx);font:650 16px system-ui,sans-serif;cursor:pointer;width:100%}
.btn.brass{background:var(--brass);border-color:var(--brass);color:var(--ink);font-weight:700}
.btn:disabled{opacity:.45;cursor:default}
label{font-size:13px;font-weight:650;color:var(--so);display:block;margin-bottom:6px}
input[type=text]{width:100%;min-height:48px;border-radius:12px;border:1px solid var(--line);background:var(--s1);color:var(--tx);padding:0 14px;font:16px system-ui,sans-serif}
.card{padding:14px;border-radius:14px;background:var(--s1);display:flex;flex-direction:column;gap:6px}
.cap{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mu)}
.big{font:900 32px/1 var(--display);letter-spacing:-.03em}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.steps{padding:4px 16px;border-radius:16px;background:var(--s1)}
.steps div{display:flex;gap:12px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line);font-size:15px;font-weight:650}
.steps div:last-child{border-bottom:0;color:var(--warm)}
.dot{width:18px;height:18px;border-radius:50%;flex:none;border:2.4px solid var(--ok);background:var(--ok)}
.steps div:last-child .dot{background:none;border-color:var(--brass);border-right-color:rgba(232,180,76,.25);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.steps div:last-child .dot{animation:none}}
.tag{font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.tip{padding:12px 14px;border-radius:12px;background:rgba(232,180,76,.14);color:var(--warm);font-size:14px;line-height:1.45}
.msg{padding:12px 14px;border-radius:12px;background:rgba(240,154,133,.12);border:1px solid rgba(240,154,133,.35);color:var(--red);font-size:14px;line-height:1.45}
.links{display:flex;flex-direction:column;gap:8px}
progress{width:100%;height:6px;accent-color:var(--brass)}
</style></head><body><main>
<a class="logo" href="/">song<i>·</i>finder</a>

<section id="home" hidden style="display:flex;flex-direction:column;gap:18px">
<h1>What song was that?</h1>
<p class="soft">Upload a video or recording with music in it. You get the exact recording, a link to it, and whether it was slowed down or sped up.</p>
<div id="err" class="msg" hidden></div>
<input id="file" type="file" accept="video/*,audio/*" hidden>
<button id="pick" class="btn brass" type="button">Choose a video or recording</button>
<div id="up" hidden><p class="small" id="upt">Uploading…</p><progress id="bar" max="1" value="0"></progress></div>
<p class="small" style="text-align:center">Up to 500 MB. At least 20 seconds of music.</p>
<div><label for="title">Know the title? It helps with rare recordings.</label>
<input id="title" type="text" placeholder="Optional" autocomplete="off"></div>
<p class="small" style="margin-top:auto">Private test version. Your file is deleted as soon as the search ends. To find the song, a short excerpt of the sound may be sent to a music recognition service.</p>
</section>

<section id="work" hidden style="display:flex;flex-direction:column;gap:18px">
<h1>Looking for your song</h1>
<p class="small" id="wname"></p>
<div class="steps" id="steps" aria-live="polite"></div>
<p class="soft">Well-known songs take a few seconds. Rare or slowed-down ones can take up to three minutes. You can close this page and come back with the same link.</p>
</section>

<section id="res" hidden style="display:flex;flex-direction:column;gap:16px">
<div><p class="tag" id="tag"></p><h1 id="rtitle" style="margin-top:8px"></h1><p class="soft" id="rartist" style="margin-top:8px;font-size:16px"></p></div>
<p class="soft" id="rmsg" hidden></p>
<div class="links" id="links"></div>
<div class="grid" id="facts">
<div class="card"><span class="cap">Speed in your clip</span><span class="big" id="sbig" style="color:var(--brass)"></span><span class="small" id="snote"></span></div>
<div class="card"><span class="cap">Clip starts at</span><span class="big" id="start"></span><span class="small" id="startn"></span></div>
</div>
<p class="tip" id="tip" hidden></p>
<button class="btn" id="copy" type="button">Copy link to this result</button>
<a class="btn" href="/">Find another song</a>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
const show=name=>{for(const s of["home","work","res"]){$(s).hidden=s!==name;$(s).style.display=s===name?"flex":"none"}};
const id=location.pathname.startsWith("/c/")?location.pathname.slice(3):"";
function fail(text){show("home");$("err").textContent=text;$("err").hidden=false;$("pick").disabled=false;$("up").hidden=true}
$("pick").onclick=()=>$("file").click();
$("file").onchange=()=>{
  const f=$("file").files[0];if(!f)return;
  if(f.size>500*1024*1024)return fail("The file is too large. The limit is 500 MB.");
  $("err").hidden=true;$("pick").disabled=true;$("up").hidden=false;
  const x=new XMLHttpRequest();
  x.open("POST","/api/clips?name="+encodeURIComponent(f.name)+"&title="+encodeURIComponent($("title").value));
  x.upload.onprogress=e=>{if(e.lengthComputable){$("bar").value=e.loaded/e.total;$("upt").textContent="Uploading… "+Math.round(100*e.loaded/e.total)+"%"}};
  x.onload=()=>{let r={};try{r=JSON.parse(x.responseText)}catch(e){}
    if(x.status===200&&r.id){location.href="/c/"+r.id}else fail(r.error||"The upload failed. Please try again.")};
  x.onerror=()=>fail("The upload failed. Please check your connection and try again.");
  x.send(f);
};
function render(j){
  if(j.status!=="done"){
    show("work");$("wname").textContent=j.name||"";
    const steps=j.status==="queued"?[j.ahead>0?("Waiting: "+j.ahead+" clip"+(j.ahead>1?"s":"")+" ahead of you"):"Starting"]:(j.steps.length?j.steps:["Reading your file"]);
    $("steps").replaceChildren(...steps.map(t=>{const d=document.createElement("div"),o=document.createElement("span");o.className="dot";d.append(o,t);return d}));
    return false}
  show("res");const hit=j.verdict==="exact"||j.verdict==="probable";
  $("facts").hidden=!hit;$("links").replaceChildren();$("tip").hidden=true;$("rmsg").hidden=true;$("rartist").textContent="";
  if(hit){
    $("tag").textContent=j.verdict==="exact"?"Exact match":"Probable match";$("tag").style.color=j.verdict==="exact"?"var(--ok)":"var(--warm)";
    $("rtitle").textContent=j.title;$("rartist").textContent=[j.artist,j.album].filter(Boolean).join(" · ");
    j.links.forEach((l,i)=>{const a=document.createElement("a");a.className="btn"+(i?"":" brass");a.href=l.url;a.rel="noopener noreferrer";a.target="_blank";a.textContent=(l.search?"Search on ":(i?"Also on ":"Listen on "))+l.label;$("links").append(a)});
    $("sbig").textContent=j.speed.big;$("snote").textContent=j.speed.note;
    $("start").textContent=j.starts_at||"n/a";$("startn").textContent=j.starts_at?"in the recording.":"Only a 30-second sample was available to compare.";
    if(j.speed.tip){$("tip").textContent=j.speed.tip;$("tip").hidden=false}
    if(j.preview_only||j.verdict==="probable"){$("rmsg").textContent="Confirmed against a 30-second sample of the recording, not the full track.";$("rmsg").hidden=false}
  }else{
    const err=j.verdict==="error";
    $("tag").textContent=err?"Problem":"No match yet";$("tag").style.color=err?"var(--red)":"var(--mu)";
    $("rtitle").textContent=err?"That didn't work":"We couldn't find this one";
    $("rmsg").textContent=j.message||"The big music catalogues don't know it, and it isn't among the recordings we've checked before. If you know the title, upload again and add it.";$("rmsg").hidden=false}
  return true}
async function poll(){
  let j;try{const r=await fetch("/api/clips/"+id);j=await r.json();if(!r.ok)return fail(j.error||"We don't have this clip.")}catch(e){return setTimeout(poll,4000)}
  if(!render(j))setTimeout(poll,2000)}
$("copy").onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$("copy").textContent="Link copied"}catch(e){$("copy").textContent=location.href}};
if(id){show("work");poll()}else show("home");
</script></body></html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default="web-data", help="folder for result files and uploads in progress")
    a = p.parse_args()
    global DATA
    DATA = a.data
    os.makedirs(DATA, exist_ok=True)
    for name in os.listdir(DATA):                       # leftovers from an interrupted run
        if not name.endswith(".json"):
            with_suppress_remove(os.path.join(DATA, name))
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required")
    threading.Thread(target=worker, daemon=True).start()
    print(f"songfinder web on http://{a.host}:{a.port}", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
