
const $=id=>document.getElementById(id);
const show=name=>{for(const s of["home","confirm","work","res"]){$(s).hidden=s!==name;$(s).style.display=s===name?"flex":"none"}};
const id=location.pathname.startsWith("/c/")?location.pathname.slice(3):location.pathname==="/example"?"example":"";
function fail(text){show("home");$("err").textContent=text;$("err").hidden=false;$("pick").disabled=!verified;$("up").hidden=true;$("file").value=""}
let bootstrap, verified=false, widget, verificationPromise, verificationResolve, verificationReject;
async function api(path,body,method="POST",renew=true){
 const r=await fetch(path,{method,headers:{"Content-Type":"application/json","X-Songfinder":"1"},body:JSON.stringify(body)});
 const value=await responseValue(r);
 if(r.status===401&&renew&&path!=="/api/session"){verified=false;await verification();return api(path,body,method,false)}
 if(!r.ok)throw new Error(value.error||"Please try again.");return value;
}
async function responseValue(response){
 try{return await response.json()}catch(e){throw new Error("We couldn't connect just now. Please try again.")}
}
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function verification(){
 if(verificationPromise)return verificationPromise;
 verified=false;$("pick").disabled=true;$("verify").hidden=false;
 $("sessionmsg").textContent="Verifying this browser…";
 verificationPromise=new Promise((resolve,reject)=>{verificationResolve=resolve;verificationReject=reject});
 const promise=verificationPromise;
 const success=async token=>{
  try{await api("/api/session",{token});verified=true;$("pick").disabled=false;$("verify").hidden=true;$("sessionmsg").textContent="";
   const resolve=verificationResolve;verificationPromise=null;resolve();
  }catch(e){failure(e.message)}
 };
 const failure=message=>{$("sessionmsg").textContent=message;const reject=verificationReject;verificationPromise=null;reject(new Error(message))};
 if(!bootstrap.site_key){success("");return promise}
 window.sfReady=()=>{widget=turnstile.render("#verify",{sitekey:bootstrap.site_key,theme:"dark",callback:success,
  "error-callback":()=>failure("Browser verification could not finish. Please try the action again."),
  "expired-callback":()=>{if(verificationPromise)turnstile.reset(widget)}})};
 if(widget!==undefined)turnstile.reset(widget);
 else if(window.turnstile)window.sfReady();
 else if(!document.querySelector('script[data-verification]')){
  const script=document.createElement("script");script.dataset.verification="1";
  script.src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=sfReady&render=explicit";
  script.onerror=()=>{script.remove();failure("Browser verification could not load. Please try again.")};document.head.append(script);
 }
 return promise;
}
async function start(){
 try{bootstrap=await (await fetch("/api/bootstrap")).json();verified=bootstrap.verified;$("limittext").textContent="Up to "+Math.floor(bootstrap.max_bytes/1024/1024)+" MB";
  if(verified){$("pick").disabled=false;$("verify").hidden=true}
  else verification().catch(()=>{});
  if(id){show("work");poll()}else{show("home");recent()}
 }catch(e){fail("The service is unavailable. Please refresh in a moment.")}
}
const verdictLabel=v=>({exact:"Exact match",probable:"Likely match",none:"No match",related:"Different recording",tune:"Possible song",unconfirmed:"Unconfirmed",unavailable:"Checking incomplete",timeout:"Out of time",cancelled:"Cancelled",error:"File problem"})[v]||"Searching";
async function recent(){
 let items=[];try{items=JSON.parse(localStorage.getItem("sf_recent")||"[]")}catch(e){}
 items=await Promise.all(items.filter(item=>/^[A-Za-z0-9_-]{22}$/.test(item.id)).map(async item=>{
  try{const r=await fetch("/api/clips/"+item.id);if(r.status===404)return null;if(!r.ok)return item;
   const j=await responseValue(r);return {...item,title:j.title,name:j.name,verdict:j.verdict,created:j.created,clip_key:j.clip_key||item.clip_key};
  }catch(e){return item}
 }));
 const families=new Set();items=items.filter(Boolean).sort((a,b)=>(b.created||0)-(a.created||0)).filter(item=>{const key=item.clip_key||item.id;if(families.has(key))return false;families.add(key);return true});
 try{localStorage.setItem("sf_recent",JSON.stringify(items))}catch(e){}
 $("recent").hidden=!items.length;$("recentlinks").replaceChildren();
 for(const item of items){if(!/^[A-Za-z0-9_-]{22}$/.test(item.id))continue;const a=document.createElement("a");a.href="/c/"+item.id;
  const date=new Date((item.created||Date.now()/1000)*1000).toLocaleDateString("en-GB",{day:"numeric",month:"short"});
  a.textContent=[item.title||item.name||"Your clip",verdictLabel(item.verdict),date].join(" · ");$("recentlinks").append(a)}
}
function remember(item){if(item.id==="example")return;try{let items=JSON.parse(localStorage.getItem("sf_recent")||"[]");
 const previous=items.find(x=>x.id===item.id)||{};item={...previous,...item};
 items=items.filter(x=>x.id!==item.id&&!(item.clip_key&&x.clip_key===item.clip_key));
 localStorage.setItem("sf_recent",JSON.stringify([item,...items].slice(0,8)))}catch(e){}}
let selectedFile,uploadJob,uploadBusy=false;
function choose(f){
 if(!f||uploadBusy)return;
 if(f.size>bootstrap.max_bytes)return fail("The file is too large. The limit is "+Math.floor(bootstrap.max_bytes/1024/1024)+" MB.");
 if(!f.size)return fail("That file is empty. Please choose another file.");
 if(uploadJob)api("/api/clips/"+uploadJob.id+"/cancel",{}).catch(()=>{});
 selectedFile=f;uploadJob=null;$("title").disabled=false;$("artist").disabled=false;$("bar").value=0;$("upt").textContent="Uploading…";$("err").hidden=true;$("uploaderr").hidden=true;$("up").hidden=true;
 $("find").textContent="Find the song";$("find").disabled=false;$("changefile").disabled=false;
 $("filename").textContent=f.name;$("filesize").textContent=(f.size/1024/1024).toFixed(1)+" MB";show("confirm");
}
$("pick").onclick=()=>$('file').click();
$("changefile").onclick=()=>{$("file").value="";$("file").click()};
$("file").onchange=()=>choose($("file").files[0]);
$("pick").ondragover=event=>{event.preventDefault();$("pick").classList.add("dragging")};
$("pick").ondragleave=()=>$("pick").classList.remove("dragging");
$("pick").ondrop=event=>{event.preventDefault();$("pick").classList.remove("dragging");choose(event.dataTransfer.files[0])};
async function uploadOffset(job){
 const r=await fetch("/api/clips/"+job.id);const state=await responseValue(r);
 if(!r.ok)throw new Error(state.error||"Please try again.");
 if(state.status==="done")throw new Error("The upload stopped for too long. Please choose the file again.");
 if(!Number.isInteger(state.received))throw new Error("Please verify this browser and try again.");
 return state.received;
}
async function sendFile(f,job){
 let offset=await uploadOffset(job),failures=0;
 while(offset<f.size){
  try{
   const r=await fetch("/api/clips/"+job.id+"/parts/"+Math.floor(offset/job.part_bytes),{method:"PUT",headers:{"X-Songfinder":"1"},body:f.slice(offset,offset+job.part_bytes)});
   if(r.status===401){await verification();continue}
   const value=await responseValue(r);if(!r.ok)throw new Error(value.error||"Upload interrupted. Please try again.");
   if(!Number.isInteger(value.received)||value.received<=offset)throw new Error("Upload interrupted. Please try again.");
   offset=value.received;failures=0;
  }catch(e){
   if(++failures>4)throw e;
   $("upt").textContent="Connection interrupted. Resuming upload…";await delay(Math.min(8000,1000*2**(failures-1)));
   // The previous response may have been lost after its part was committed.
   // Resume only at the server's committed, ordered boundary.
   try{offset=await uploadOffset(job)}catch(statusError){if(failures===4)throw statusError}
  }
  $("bar").value=offset/f.size;$("upt").textContent="Uploading… "+Math.round(100*offset/f.size)+"%";
 }
}
$("confirmform").onsubmit=async event=>{
 event.preventDefault();if(!selectedFile||uploadBusy)return;
 uploadBusy=true;$("find").disabled=true;$("changefile").disabled=true;$("uploaderr").hidden=true;$("up").hidden=false;
 try{
  if(!verified)await verification();
  if(!uploadJob)uploadJob=await api("/api/clips",{size:selectedFile.size,name:selectedFile.name,title:$("title").value,artist:$("artist").value});
  $("title").disabled=true;$("artist").disabled=true;
  await sendFile(selectedFile,uploadJob);
  await api("/api/clips/"+uploadJob.id+"/complete",{});
  remember({id:uploadJob.id,clip_key:uploadJob.id,name:selectedFile.name,created:Date.now()/1000});location.href="/c/"+uploadJob.id;
 }catch(e){$("uploaderr").textContent=e.message;$("uploaderr").hidden=false;$("find").textContent=uploadJob?"Resume upload":"Find the song";
  $("find").disabled=false;$("changefile").disabled=false;$("file").value=""}
 finally{uploadBusy=false}
};
const noMatchCopy={
 none:["We couldn't find this one","Nothing we checked matched your clip. If you know the title or performer, add it below and search again."],
 unconfirmed:["We have a lead, but no match","A likely title came up, but no recording of it matched your clip. Add the performer to narrow it down."],
 related:["Same tune, different recording","We found this song played by someone else, but not the recording in your clip. Add the performer and search again."],
 unavailable:["We couldn't finish checking","Some music sources didn't answer. Your audio is saved, so try again in a few minutes."],
 timeout:["This one ran out of time","The search needed more than six minutes. Add a title or performer and search again."],
 error:["We couldn't read that file","Try a different video, or an audio recording."],
 cancelled:["Search cancelled",""]
};
let currentClipKey;
function render(j){
  currentClipKey=j.clip_key;
  $("cancel").hidden=!j.can_cancel;
  $("retry").hidden=!j.can_retry;
  if(!$("retry").dataset.loaded){$("retrytitle").value=j.hint||"";$("retryartist").value=j.artist_hint||"";$("retry").dataset.loaded="1"}
  if(j.status!=="done"){
    show("work");$("wname").textContent=j.name||"";
    const steps=j.status==="queued"?[j.ahead>0?(j.ahead+" search"+(j.ahead>1?"es":"")+" ahead of yours"):"Starting"]:(j.steps.length?j.steps:["Reading your file"]);
    $("steps").replaceChildren(...steps.map(t=>{const d=document.createElement("div"),o=document.createElement("span");o.className="dot";d.append(o,t);return d}));
    return false}
  remember({id:j.id,name:j.name,title:j.title,verdict:j.verdict,created:j.created,clip_key:j.clip_key});
  show("res");const hit=j.verdict==="exact"||j.verdict==="probable";
  $("facts").hidden=!hit;$("links").replaceChildren();$("rmsg").hidden=true;$("rartist").textContent="";
  if(hit){
    $("tag").textContent=j.verdict==="exact"?"Exact match":"Likely match";$("tag").style.color=j.verdict==="exact"?"var(--ok)":"var(--warm)";
    $("rtitle").textContent=j.title;$("rartist").textContent=[j.artist,j.album].filter(Boolean).join(" · ");
    j.links.filter(l=>/^https?:\/\//.test(l.url)).forEach((l,i)=>{const a=document.createElement("a");a.className="btn"+(i?"":" brass");a.href=l.url;a.rel="noopener noreferrer";a.target="_blank";a.textContent=(l.search?"Search on ":(i?"Also on ":"Listen on "))+l.label;$("links").append(a)});
    $("sbig").textContent=j.speed.big;$("snote").textContent=j.speed.note;
    $("start").textContent=j.starts_at||"n/a";$("startn").textContent=j.starts_at?"into the recording":"Start not available from a short sample";
    const notes=[];
    if(j.preview_only)notes.push("Checked against a short sample, not the full track.");
    if(j.verdict==="probable")notes.push("This sounds like the same recording, but we couldn't confirm it. Have a listen.");
    $("rmsg").textContent=notes.join(" ");$("rmsg").hidden=!notes.length;
  }else if(j.verdict==="tune"){
    $("tag").textContent="Possible song";$("tag").style.color="var(--warm)";
    $("rtitle").textContent=j.title;$("rartist").textContent=j.reference_artist?"Compared with a recording by "+j.reference_artist:"";
    $("rmsg").textContent=j.message;$("rmsg").hidden=false;
    if(/^https?:\/\//.test(j.reference_url||"")){const a=document.createElement("a");a.className="btn brass";a.href=j.reference_url;a.rel="noopener noreferrer";a.target="_blank";a.textContent="Compare reference recording";$("links").append(a)}
    $("retry").open=true;
  }else{
    const copy=noMatchCopy[j.verdict]||noMatchCopy.none;
    $("tag").textContent="";$("rtitle").textContent=copy[0];$("rmsg").textContent=j.message||copy[1];$("rmsg").hidden=!copy[1];
    if(j.verdict==="error"&&j.error_kind!=="file"){
      $("rtitle").textContent="We couldn't finish checking";$("rmsg").textContent=j.message||"Please try again in a few minutes.";$("rmsg").hidden=false;
    }
    $("retry").open=true;
  }
  $("evidence").hidden=!hit;$("why").textContent=hit?"We compared the sound in your clip with "+(j.preview_only?"a sample of":"the full")+" recording, checking that musical passages agree at the same speed. This is an estimate; please listen and let us know if it is right.":"";
  $("feedback").hidden=!hit||id==="example";$("feedbackmsg").textContent=j.feedback==="yes"?"Thanks, good to know.":j.feedback==="no"?"Thanks. Add a clue below and we’ll look again.":"";if(j.feedback==="no")$("retry").open=true;
  return true}
async function poll(){
  let j;try{const r=await fetch(id==="example"?"/api/example":"/api/clips/"+id);j=await responseValue(r);if(!r.ok)return fail(j.error||"We don't have this clip.")}catch(e){return setTimeout(poll,4000)}
  if(!render(j))setTimeout(poll,2000)}
$("copy").onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$("copy").textContent="Link copied"}catch(e){$("copy").textContent=location.href}};
$("workcopy").onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$("workcopy").textContent="Link copied"}catch(e){$("workcopy").textContent=location.href}};
for(const answer of ["yes","no"])$(answer).onclick=async()=>{try{await api("/api/clips/"+id+"/feedback",{answer});$("feedbackmsg").textContent=answer==="yes"?"Thanks, good to know.":"Thanks. Add a clue below and we’ll look again.";
 if(answer==="no"&&!$("retry").hidden){$("retry").open=true;$("retry").scrollIntoView({behavior:"smooth",block:"nearest"});$("retrytitle").focus()}}catch(e){$("feedbackmsg").textContent=e.message}};
$("retryform").onsubmit=async event=>{
 event.preventDefault();$("retrybutton").disabled=true;$("retrymsg").textContent="Starting a new search…";
 try{const job=await api("/api/clips/"+id+"/retry",{title:$("retrytitle").value,artist:$("retryartist").value});remember({id:job.id,clip_key:currentClipKey||id,created:Date.now()/1000});location.href="/c/"+job.id}
 catch(e){$("retrymsg").textContent=e.message;$("retrybutton").disabled=false}
};
$("cancel").onclick=async()=>{
 $("cancel").disabled=true;
 try{await api("/api/clips/"+id+"/cancel",{});$("cancelmsg").textContent="Stopping this search…";poll()}
 catch(e){$("cancelmsg").textContent=e.message;$("cancel").disabled=false}
};
start();
