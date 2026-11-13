const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const app=fs.readFileSync(path.join(__dirname,'../songfinder/static/app.js'),'utf8');
async function environment(){
 const nodes=new Map(),calls=[];let callback,feedbackCalls=0;
 const node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,disabled:false,value:'',style:{},dataset:{},files:[],textContent:'',replaceChildren(){},append(){}});return nodes.get(id)};
 const sandbox={console,setTimeout,clearTimeout,location:{pathname:'/',href:'http://local.test/'},navigator:{},
  localStorage:{getItem(){return '[]'},setItem(){}},
  document:{getElementById:node,createElement:()=>node('element'),querySelector:()=>null,head:{append(){}}},
  turnstile:{render(selector,options){callback=options.callback;return 7},reset(){}}
 };
 sandbox.window=sandbox;
 sandbox.fetch=async (url,options)=>{calls.push(url);
  if(url==='/api/bootstrap')return {json:async()=>({verified:true,site_key:'test-key',max_bytes:1024*1024})};
  if(url.endsWith('/feedback')&&feedbackCalls++===0)return {status:401,ok:false,json:async()=>({error:'Please verify to continue.'})};
  return {status:200,ok:true,json:async()=>({ok:true})};
 };
 vm.createContext(sandbox);vm.runInContext(app,sandbox);await new Promise(setImmediate);
 return {sandbox,nodes,node,calls,verify:async()=>callback('test-verification')};
}
test('expired feedback exposes verification and retries after renewal',async()=>{
 const e=await environment();const pending=vm.runInContext('api("/api/clips/test/feedback",{answer:"no"})',e.sandbox);
 await new Promise(setImmediate);assert.equal(e.node('verify').hidden,false);
 await e.verify();await pending;
 assert.equal(e.node('verify').hidden,true);
 assert.deepEqual(e.calls.slice(1),['/api/clips/test/feedback','/api/session','/api/clips/test/feedback']);
});
test('upload errors clear file selection so the same file can be chosen',async()=>{
 const e=await environment();e.node('file').value='same-file.wav';vm.runInContext('fail("Interrupted upload")',e.sandbox);
 assert.equal(e.node('file').value,'');assert.equal(e.node('pick').disabled,false);assert.equal(e.node('up').hidden,true);
});
test('upload size is checked against the server bootstrap limit',async()=>{
 const e=await environment();e.node('file').files=[{size:2*1024*1024,name:'too-large.wav'}];
 await e.node('file').onchange();assert.match(e.node('err').textContent,/limit is 1 MB/);
 assert.deepEqual(e.calls,['/api/bootstrap']);
});
test('file choice waits for confirmation and includes both clues',async()=>{
 const e=await environment();e.node('file').files=[{size:100,name:'public.wav'}];e.node('file').onchange();
 assert.equal(e.node('confirm').hidden,false);assert.deepEqual(e.calls,['/api/bootstrap']);
 e.node('title').value='Song';e.node('artist').value='Band';let created;
 e.sandbox.fetch=async(url,opt)=>{if(url==='/api/clips'){created=JSON.parse(opt.body);throw new Error('stop before upload')}return {ok:true,json:async()=>({})}};
 await e.node('confirmform').onsubmit({preventDefault(){}});
 assert.deepEqual(created,{size:100,name:'public.wav',title:'Song',artist:'Band'});
});
test('non-JSON gateway response gives a plain retry message',async()=>{
 const e=await environment();e.sandbox.fetch=async()=>({ok:false,status:502,json:async()=>{throw new SyntaxError('Unexpected <')}});
 await assert.rejects(vm.runInContext('api("/api/clips",{})',e.sandbox),/Please try again/);
});
test('lost upload response resumes from committed server offset',async()=>{
 const e=await environment();const puts=[];let offset=0;
 e.sandbox.setTimeout=resolve=>{resolve();return 0};
 e.sandbox.fetch=async(url,opt)=>{
  if(!opt)return {ok:true,json:async()=>({status:'uploading',received:offset})};
  puts.push(url);if(url.endsWith('/0')){offset=8;throw new Error('response lost after commit')}
  offset=10;return {ok:true,status:200,json:async()=>({received:10})};
 };
 await vm.runInContext('sendFile({size:10,slice:(a,b)=>({size:Math.min(10,b)-a})},{id:"test",part_bytes:8})',e.sandbox);
 assert.deepEqual(puts,['/api/clips/test/parts/0','/api/clips/test/parts/1']);assert.equal(e.node('bar').value,1);
});
test('retry family is folded into a single recent search',async()=>{
 const e=await environment();let saved='[]';e.sandbox.localStorage={getItem:()=>saved,setItem:(k,v)=>{saved=v}};
 vm.runInContext('remember({id:"original",clip_key:"original"});remember({id:"retry",clip_key:"original",title:"Song",verdict:"exact"})',e.sandbox);
 assert.equal(JSON.parse(saved).length,1);assert.equal(JSON.parse(saved)[0].id,'retry');
});
test('no-match copy opens retained-audio clues and hides match cards',async()=>{
 const e=await environment();
 vm.runInContext('render({id:"test",status:"done",verdict:"none",can_retry:true,steps:[]})',e.sandbox);
 assert.equal(e.node('rtitle').textContent,"We couldn't find this one");assert.equal(e.node('retry').open,true);assert.equal(e.node('facts').hidden,true);
});
