/* Execute the packaged application itself in a deterministic DOM/network fixture.
 * Node is a development check only; the dashboard needs no Node runtime. */
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../herdr_fleet/dashboard_static/app.js'), 'utf8');

function fixture() {
  class Element {
    constructor(tag='div') {
      this.tagName=tag.toUpperCase(); this.textContent=''; this.value=''; this.children=[];
      this.dataset={}; this.listeners={}; this.attrs={}; this.hidden=false; this.disabled=false;
      this.style={setProperty(){}}; this.classList={toggle(){},contains(){return false;}};
      this.clientHeight=360; this.scrollTop=0;
    }
    addEventListener(name, fn) { this.listeners[name]=fn; }
    setAttribute(name,value) { this.attrs[name]=String(value); }
    getAttribute(name) { return this.attrs[name]; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children=items; }
    querySelector() { return null; }
    getBoundingClientRect() { return {left:0,width:100,height:360}; }
    focus() { document.activeElement=this; }
    animate() { return {finished:Promise.resolve()}; }
    remove() {}
  }
  const elements=new Map();
  const $=id=>{if(!elements.has(id))elements.set(id,new Element()); return elements.get(id);};
  const document={getElementById:$,createElement:tag=>new Element(tag),createTextNode:text=>text,
    body:new Element(),activeElement:null,hidden:false,title:'',addEventListener(){},querySelectorAll(){return [];}};
  for(const id of ['composer-title','questions']) {$(id).parentNode=new Element();$(id).parentNode.parentNode=new Element();}
  const listeners={},timers=new Map(),requests=[];
  let timer=0,ident=0,application;
  const deferred=[],watchResponses=[];
  let sessionResponder=async()=>({ok:true,json:async()=>({csrf:'csrf-current',demo:false})});
  const data={demo:false,revision:1,fleets:['team','docs'].map(id=>({id,label:id,stale:false,owner:[],events:[],
    seats:['sample-dev','sample-reviewer'].map(name=>({name,kind:'claude',state:'idle',view:id+name,queued:0,fyi:0}))}))};
  class Terminal {loadAddon(){}open(){}reset(){}resize(){}write(){} get buffer(){return {active:{length:0}};}}
  const context={document,location:{hash:'',pathname:'/'},history:{replaceState(){context.location.hash='';}},
    Terminal,FitAddon:{FitAddon:class {proposeDimensions(){return {cols:80,rows:24};}fit(){}}},
    ResizeObserver:class {observe(){}},matchMedia:()=>({matches:true,addEventListener(){}}),
    window:{addEventListener(name,fn){listeners[name]=fn;}},navigator:{},
    setInterval(fn){timers.set(++timer,fn);return timer;},clearInterval(id){timers.delete(id);},
    setTimeout(){return ++timer;},clearTimeout(){},AbortController,TextDecoder,Uint8Array,Map,Set,
    crypto:{randomUUID(){return 'intent-synthetic-'+(++ident);}},console,
    fetch:async(url,options={})=>{
      const body=options.body?JSON.parse(options.body):null;
      requests.push({url,body});
      if(url==='/api/session')return sessionResponder(body);
      if(url==='/api/snapshot')return {ok:true,json:async()=>data};
      if(url==='/api/action')return new Promise(resolve=>deferred.push(result=>resolve({ok:true,json:async()=>result})));
      if(url==='/api/watch')return new Promise(resolve=>watchResponses.push(resolve));
      throw new Error('Unexpected fixture request '+url);
    }};
  // Only export lexical handles; every function under test is the real shipped function.
  vm.runInNewContext(source.replace(/\}\(\)\);\s*$/, 'globalThis.application = {state, send, selectedChanged, selectFleet, restoreDraft, saveDraft, bootstrap, refresh, draftKey, watch}; }());'),context);
  application=context.application;
  application.state.data=data;application.state.csrf='csrf-initial';
  application.state.fleet=application.state.targetFleet='team';application.state.seat='sample-dev';
  return {app:application,$,data,requests,deferred,watchResponses,listeners,timers,context,
    session(fn){sessionResponder=fn;},
    select(fleet,seat,reply=null){application.selectedChanged(fleet,seat); application.saveDraft();application.state.reply=reply;application.restoreDraft();},
    text(text){$('draft').value=text;application.saveDraft();}};
}
const confirmed={status:'delivered',certainty:'confirmed',id:'20261101T100000Z-aabbcc'};
const uncertain={status:'unconfirmed',certainty:'unconfirmed',id:'20261101T100001Z-ddeeff'};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};

test('fast confirmed response and double clicks retain one intent until explicit New attempt',async()=>{
  const f=fixture();f.text('Synthetic same draft');
  const first=f.app.send();await flush();
  const second=f.app.send();await flush();
  assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1,'pending duplicate click');
  f.deferred.shift()(confirmed);await first;await second;
  const third=f.app.send();await flush();
  const actions=f.requests.filter(r=>r.url==='/api/action');
  assert.equal(actions.length,1,'confirmed unchanged draft must not submit another intent');
  await third;
  f.$('new-attempt').listeners.click();
  const deliberate=f.app.send();await flush();
  const next=f.requests.filter(r=>r.url==='/api/action');
  assert.equal(next.length,2);assert.notEqual(next[0].body.intent,next[1].body.intent);
  f.deferred.shift()(confirmed);await deliberate;
});

test('deferred responses only update their originating target/fleet/reply draft',async()=>{
  for(const order of ['origin-first','other-first']) for(const change of ['target','fleet','reply']) {
    const f=fixture();f.text('Origin draft');
    const original=f.app.send();await flush();
    const origin=f.requests.find(r=>r.url==='/api/action').body.intent;
    if(change==='target')f.select('team','sample-reviewer');
    if(change==='fleet')f.select('docs','sample-dev');
    if(change==='reply')f.select('team','sample-dev','20261101T100003Z-112233');
    f.text('Other uncertain draft');
    const other=f.app.send();await flush();
    const otherId=f.app.state.intent.id;
    const finishOrigin=f.deferred.shift(),finishOther=f.deferred.shift();
    let previous;
    if(order==='other-first') {
      finishOther(uncertain);await other;previous=f.$('action-result').textContent;
      finishOrigin(confirmed);await original;
    } else {
      previous=f.$('action-result').textContent;finishOrigin(confirmed);await original;
      assert.equal(f.$('action-result').textContent,previous);
      finishOther(uncertain);await other;previous=f.$('action-result').textContent;
    }
    assert.equal(f.app.state.intent.id,otherId,change+' must retain its uncertain intent');
    assert.equal(f.$('draft').value,'Other uncertain draft');
    assert.equal(f.$('action-result').textContent,previous,'outcome provenance stays with selected draft');
    f.select('team','sample-dev');
    assert.equal(f.app.state.intent.id,origin,'origin intent remains retained');
  }
});

test('late old Watch failure after reauthentication cannot disconnect the replacement',async()=>{
  const f=fixture();
  const old=f.app.watch();await flush();
  let failRead;
  f.watchResponses.shift()({ok:true,body:{getReader(){return {read(){return new Promise((_,reject)=>{failRead=reject;});}};}}});
  await flush();
  f.context.location.hash='#new-capability';await f.listeners.hashchange();await flush();
  let aborted=0;
  const replacement={abort(){aborted++;}};
  f.app.state.watch=replacement;
  f.$('terminal-status').textContent='Replacement watch';
  failRead(new Error('Old stream failed'));await old;
  assert.equal(aborted,0);assert.equal(f.app.state.watch,replacement);
  assert.equal(f.$('terminal-status').textContent,'Replacement watch');
});

test('editing a pending draft or switching back does not lose its intent or later text',async()=>{
  const f=fixture();f.text('Original');
  const pending=f.app.send();await flush();
  const id=f.app.state.intent.id;
  f.text('New text while request is pending');
  f.$('new-attempt').listeners.click();
  assert.equal(f.app.state.intent.id,id,'pending action cannot be deliberately reset before outcome');
  f.select('team','sample-reviewer');f.select('team','sample-dev');
  f.deferred.shift()(confirmed);await pending;
  assert.equal(f.$('draft').value,'New text while request is pending');
  assert.equal(f.app.state.intent.id,id);
});

test('valid pasted capability recovers expired/revoked/restarted sessions with one timer and no stale Watch',async()=>{
  for(const reason of ['expired','revoked','restarted']) {
    const f=fixture();let stopped=0;
    f.app.state.csrf='stale-'+reason;
    f.app.state.watch={abort(){stopped++;}};
    f.app.state.tick=f.context.setInterval(()=>{});
    f.context.location.hash='#valid-synthetic-capability';
    await f.listeners.hashchange();await flush();
    assert.equal(f.requests.filter(r=>r.url==='/api/session').length,1,reason+' must exchange full capability');
    assert.equal(f.context.location.hash,'');
    assert.equal(f.app.state.csrf,'csrf-current');
    assert.equal(f.timers.size,1);assert.equal(stopped,1);assert.equal(f.app.state.watch,null);
  }
});

test('overlapping capability exchanges ignore late stale responses and retain one refresh timer',async()=>{
  const f=fixture(),exchanges=[];
  f.session(()=>new Promise(resolve=>exchanges.push(resolve)));
  f.context.location.hash='#first';f.listeners.hashchange();await flush();
  f.context.location.hash='#second';f.listeners.hashchange();await flush();
  assert.equal(exchanges.length,2);
  exchanges[1]({ok:true,json:async()=>({csrf:'new',demo:false})});await flush();
  exchanges[0]({ok:true,json:async()=>({csrf:'old',demo:false})});await flush();
  assert.equal(f.app.state.csrf,'new');assert.equal(f.timers.size,1);
});
