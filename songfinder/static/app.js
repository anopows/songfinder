
const $=id=>document.getElementById(id);
const show=name=>{for(const s of["home","work","res"]){$(s).hidden=s!==name;$(s).style.display=s===name?"flex":"none"}};
const id=location.pathname.startsWith("/c/")?location.pathname.slice(3):"";
function fail(text){show("home");$("err").textContent=text;$("err").hidden=false;$("pick").disabled=false;$("up").hidden=true}
let bootstrap, verified=false, widget;
async function api(path,body,method="POST"){
 const r=await fetch(path,{method,headers:{"Content-Type":"application/json","X-Songfinder":"1"},body:JSON.stringify(body)});
 const value=await r.json();if(!r.ok)throw new Error(value.error||"Please try again.");return value;
}
function verification(){
 if(!bootstrap.site_key){api("/api/session",{}).then(()=>{verified=true;$("pick").disabled=false});return}
 window.sfReady=()=>{widget=turnstile.render("#verify",{sitekey:bootstrap.site_key,theme:"dark",callback:async token=>{
  try{await api("/api/session",{token});verified=true;$("pick").disabled=false;$("err").hidden=true}
  catch(e){fail(e.message);$("pick").disabled=true;turnstile.reset(widget)}
 },"error-callback":()=>{$("err").textContent="Verification could not load. Please refresh the page.";$("err").hidden=false}})};
 const script=document.createElement("script");script.src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=sfReady&render=explicit";document.head.append(script);
}
async function start(){
 try{bootstrap=await (await fetch("/api/bootstrap")).json();verified=bootstrap.verified;
  if(verified)$("pick").disabled=false;else verification();
  if(id){if(!verified)$("feedback").append($("verify"));show("work");poll()}else{show("home");recent()}
 }catch(e){fail("The service is unavailable. Please refresh in a moment.")}
}
function recent(){
 let items=[];try{items=JSON.parse(localStorage.getItem("sf_recent")||"[]")}catch(e){}
 $("recent").hidden=!items.length;$("recentlinks").replaceChildren();
 for(const item of items){if(!/^[A-Za-z0-9_-]{22}$/.test(item.id))continue;const a=document.createElement("a");a.href="/c/"+item.id;a.textContent=item.title||item.name||"Your clip";$("recentlinks").append(a)}
}
function remember(item){try{let items=JSON.parse(localStorage.getItem("sf_recent")||"[]");items=items.filter(x=>x.id!==item.id);localStorage.setItem("sf_recent",JSON.stringify([item,...items].slice(0,8)))}catch(e){}}
$("pick").onclick=()=>$("file").click();
$("file").onchange=async()=>{
 const f=$("file").files[0];if(!f)return;
 if(f.size>500*1024*1024)return fail("The file is too large. The limit is 500 MB.");
 $("err").hidden=true;$("pick").disabled=true;$("up").hidden=false;
 try{
  if(!bootstrap.site_key&&!verified)await api("/api/session",{});
  const job=await api("/api/clips",{size:f.size,name:f.name,title:$("title").value});
  for(let offset=0,n=0;offset<f.size;offset+=job.part_bytes,n++){
   const body=f.slice(offset,offset+job.part_bytes);let done=false;
   for(let attempt=0;attempt<3&&!done;attempt++){
    try{const r=await fetch("/api/clips/"+job.id+"/parts/"+n,{method:"PUT",headers:{"X-Songfinder":"1"},body});
     if(!r.ok){const value=await r.json();throw new Error(value.error||"Upload failed.")}done=true;
    }catch(e){if(attempt===2)throw e;await new Promise(resolve=>setTimeout(resolve,1000*(attempt+1)))}
   }
   $("bar").value=Math.min(offset+body.size,f.size)/f.size;$("upt").textContent="Uploading… "+Math.round(100*$("bar").value)+"%";
  }
  await api("/api/clips/"+job.id+"/complete",{});remember({id:job.id,name:f.name});location.href="/c/"+job.id;
 }catch(e){fail(e.message);if(!verified)$("pick").disabled=true}
};
function render(j){
  if(j.status!=="done"){
    show("work");$("wname").textContent=j.name||"";
    const steps=j.status==="queued"?[j.ahead>0?("Waiting: "+j.ahead+" clip"+(j.ahead>1?"s":"")+" ahead of you"):"Starting"]:(j.steps.length?j.steps:["Reading your file"]);
    $("steps").replaceChildren(...steps.map(t=>{const d=document.createElement("div"),o=document.createElement("span");o.className="dot";d.append(o,t);return d}));
    return false}
  remember({id:j.id,name:j.name,title:j.title});
  show("res");const hit=j.verdict==="exact"||j.verdict==="probable";
  $("facts").hidden=!hit;$("links").replaceChildren();$("tip").hidden=true;$("rmsg").hidden=true;$("rartist").textContent="";
  if(hit){
    $("tag").textContent=j.verdict==="exact"?"Exact match":"Probable match";$("tag").style.color=j.verdict==="exact"?"var(--ok)":"var(--warm)";
    $("rtitle").textContent=j.title;$("rartist").textContent=[j.artist,j.album].filter(Boolean).join(" · ");
    j.links.filter(l=>/^https?:\/\//.test(l.url)).forEach((l,i)=>{const a=document.createElement("a");a.className="btn"+(i?"":" brass");a.href=l.url;a.rel="noopener noreferrer";a.target="_blank";a.textContent=(l.search?"Search on ":(i?"Also on ":"Listen on "))+l.label;$("links").append(a)});
    $("sbig").textContent=j.speed.big;$("snote").textContent=j.speed.note;
    $("start").textContent=j.starts_at||"n/a";$("startn").textContent=j.starts_at?"in the recording.":"Only a sample was available to compare.";
    if(j.speed.tip){$("tip").textContent=j.speed.tip;$("tip").hidden=false}
    if(j.preview_only||j.verdict==="probable"){$("rmsg").textContent=j.preview_only?"Matched against a sample of the recording, not the full track.":"The audio is similar, but there is not enough evidence to call this an exact match.";$("rmsg").hidden=false}
  }else{
    const err=j.verdict==="error";
    $("tag").textContent=err?"Problem":"No match yet";$("tag").style.color=err?"var(--red)":"var(--mu)";
    $("rtitle").textContent=err?"That didn't work":"We couldn't find this one";
    $("rmsg").textContent=j.message||"We could not confirm a recording from the sources available. Try a clearer clip with more music, or upload again with the title if you know it.";$("rmsg").hidden=false}
  $("evidence").hidden=!hit;$("why").textContent=hit?"We compared the sound in your clip with "+(j.preview_only?"a sample of":"the full")+" recording, checking that musical passages agree at the same speed. This is an estimate; please listen and let us know if it is right.":"";
  $("feedback").hidden=!hit;$("feedbackmsg").textContent=j.feedback?"Thanks for your feedback.":"";
  return true}
async function poll(){
  let j;try{const r=await fetch("/api/clips/"+id);j=await r.json();if(!r.ok)return fail(j.error||"We don't have this clip.")}catch(e){return setTimeout(poll,4000)}
  if(!render(j))setTimeout(poll,2000)}
$("copy").onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$("copy").textContent="Link copied"}catch(e){$("copy").textContent=location.href}};
$("workcopy").onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$("workcopy").textContent="Link copied"}catch(e){$("workcopy").textContent=location.href}};
for(const answer of ["yes","no"])$(answer).onclick=async()=>{try{await api("/api/clips/"+id+"/feedback",{answer});$("feedbackmsg").textContent="Thanks for your feedback."}catch(e){$("feedbackmsg").textContent=e.message}};
start();
