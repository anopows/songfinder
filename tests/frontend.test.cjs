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
