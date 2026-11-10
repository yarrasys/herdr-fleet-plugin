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
    click() { if(!this.disabled && this.listeners.click)return this.listeners.click({target:this}); }
    setAttribute(name,value) { this.attrs[name]=String(value); }
    getAttribute(name) { return this.attrs[name]; }
    append(...items) { this.children.push(...items); }
    insertBefore(item,before) { const index=this.children.indexOf(before); if(index<0)this.children.push(item);else this.children.splice(index,0,item); }
    replaceChildren(...items) { this.children=items; }
    querySelector() { return null; }
    getBoundingClientRect() { return {left:0,width:100,height:360}; }
    focus() { document.activeElement=this; }
    animate() { return {finished:Promise.resolve()}; }
    remove() {}
  }
  const elements=new Map();
  const $=id=>{if(!elements.has(id))elements.set(id,new Element()); return elements.get(id);};
  const documentListeners={};
  const document={getElementById:$,createElement:tag=>new Element(tag),createTextNode:text=>text,
    body:new Element(),activeElement:null,hidden:false,title:'',addEventListener(name,fn){documentListeners[name]=fn;},querySelectorAll(){return [];}};
  for(const id of ['composer-title','questions']) {$(id).parentNode=new Element();$(id).parentNode.parentNode=new Element();}
  const listeners={},timers=new Map(),requests=[];
  const models=[];
  const conversation={messages:[],intents:[],truncated:false,incomplete:false,revision:1};
  let timer=0,ident=0,application;
  const deferred=[],watchResponses=[];
  let sessionResponder=async()=>({ok:true,json:async()=>({csrf:'csrf-current',demo:false})});
  const data={demo:false,revision:1,fleets:['team','docs'].map(id=>({id,label:id,stale:false,owner:[],events:[],
    seats:['sample-dev','sample-reviewer'].map(name=>({name,kind:'claude',state:'idle',view:id+name,queued:0,fyi:0}))}))};
  class Terminal {loadAddon(){}open(){}reset(){}resize(){}write(){} get buffer(){return {active:{length:0}};}}
  const context={document,location:{hash:'',pathname:'/'},history:{replaceState(){context.location.hash='';}},
    ConversationView:{mount(){return {render(model){models.push(model);},destroy(){}};}},
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
      if(url.startsWith('/api/conversation?'))return {ok:true,json:async()=>conversation};
      if(url.startsWith('/api/list?'))return {ok:true,json:async()=>({items:[]})};
      if(url==='/api/action')return new Promise(resolve=>deferred.push(result=>resolve({ok:true,json:async()=>result})));
      if(url==='/api/watch')return new Promise(resolve=>watchResponses.push(resolve));
      throw new Error('Unexpected fixture request '+url);
    }};
  // Only export lexical handles; every function under test is the real shipped function.
  vm.runInNewContext(source.replace(/\}\(\)\);\s*$/, 'globalThis.application = {state, send, selectedChanged, selectFleet, restoreDraft, saveDraft, bootstrap, refresh, draftKey, watch, chooseReply}; }());'),context);
  application=context.application;
  application.state.data=data;application.state.csrf='csrf-initial';
  application.state.fleet=application.state.targetFleet='team';application.state.seat='sample-dev';
  return {app:application,$,data,requests,deferred,watchResponses,listeners,timers,context,models,conversation,documentListeners,
    session(fn){sessionResponder=fn;},
    select(fleet,seat,reply=null){application.selectedChanged(fleet,seat); application.chooseReply(reply);},
    text(text){$('draft').value=text;application.saveDraft();}};
}
const confirmed={status:'delivered',certainty:'confirmed',id:'20261101T100000Z-aabbcc'};
const uncertain={status:'unconfirmed',certainty:'unconfirmed',id:'20261101T100001Z-ddeeff'};
const flush=async()=>{for(let i=0;i<24;i++)await Promise.resolve();};

for(const modifier of ['metaKey','ctrlKey']) {
 test('composer '+modifier+' Enter clicks Send once and uses its existing pending intent',async()=>{
  const f=fixture();f.text('Synthetic keyboard message');await f.app.refresh();
  let prevented=0;
  const event={key:'Enter',[modifier]:true,preventDefault(){prevented++;}};
  f.$('draft').listeners.keydown?.(event);await flush();
  assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1,modifier+' sends through the actual button');
  assert.equal(prevented,1,'shortcut suppresses the textarea newline');
  f.$('draft').listeners.keydown?.(event);
  f.$('draft').listeners.keydown?.({...event,repeat:true});await flush();
  assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1,'pending and repeated shortcuts cannot duplicate');
  f.deferred.shift()(confirmed);await flush();
  assert.equal(f.$('draft').value,'');
 });
}

test('composer ignores multiline editing, extra modifiers, repeats and composition without preventing defaults',async()=>{
 const f=fixture();f.text('First line\nSecond line');await f.app.refresh();
 const ignored=[{}, {shiftKey:true}, {ctrlKey:true,shiftKey:true}, {metaKey:true,shiftKey:true},
  {ctrlKey:true,altKey:true}, {metaKey:true,altKey:true}, {metaKey:true,repeat:true},
  {ctrlKey:true,isComposing:true}, {metaKey:true,keyCode:229}, {ctrlKey:true,key:'Escape'}];
 for(const keys of ignored) {
  let prevented=false;
  f.$('draft').listeners.keydown({key:'Enter',...keys,preventDefault(){prevented=true;}});
  assert.equal(prevented,false,JSON.stringify(keys));
 }
 await flush();assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0);
 assert.equal(f.$('draft').value,'First line\nSecond line');assert.equal(f.app.state.intent,null);
});

test('composer shortcut cannot activate disabled Send or send empty text, and has no global scope',async()=>{
 const f=fixture();f.text('Retained disabled draft');await f.app.refresh();
 f.$('send').disabled=true;
 let prevented=false;
 const event={key:'Enter',ctrlKey:true,preventDefault(){prevented=true;}};
 f.$('draft').listeners.keydown(event);await flush();
 assert.equal(prevented,false);assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0);
 assert.equal(f.$('draft').value,'Retained disabled draft');
 f.$('send').disabled=false;
 f.listeners.keydown?.({...event,target:f.$('terminal-screen')});
 f.documentListeners.keydown?.({...event,target:f.$('other-input')});await flush();
 assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0,'shortcut outside textarea sends nothing');
 f.text(' \n ');f.$('draft').listeners.keydown(event);await flush();
 assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0,'empty Send validation is reused');
 assert.equal(f.app.state.intent,null);assert.equal(f.$('draft').value,' \n ');
});

test('composer shortcut hint names both platforms and Demo keyboard reply stays local',async()=>{
 const html=fs.readFileSync(path.join(__dirname,'../herdr_fleet/dashboard_static/index.html'),'utf8');
 assert.match(html,/<button[^>]*id="send"[^>]*aria-keyshortcuts="Meta\+Enter Control\+Enter"/);
 const hint=html.match(/<span[^>]*id="send-hint"[^>]*>[^<]*<\/span>/)[0];
 assert.doesNotMatch(hint,/\bhidden\b/);assert.match(hint,/Ctrl\+Enter \(Windows\/Linux\)/);
 assert.match(hint,/Command\+Enter \(Mac\)/);
 const f=fixture(),original=f.context.fetch;
 await f.app.refresh();assert.match(f.$('send-hint').textContent,/sends/);
 f.data.demo=true;f.data.hold=true;f.data.stages=['Sample'];f.data.stage=0;
 f.select('team','sample-dev','20261101T100010Z-667788');f.text('Local keyboard Demo reply');await f.app.refresh();
 f.context.fetch=async(url,options)=>{
  if(url==='/api/demo/reply'){f.requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({demo:true})};}
  return original(url,options);
 };
 assert.match(f.$('send-hint').textContent,/records a sample reply/);
 assert.match(f.$('send-hint').textContent,/Ctrl\+Enter \(Windows\/Linux\)/);
 assert.match(f.$('send-hint').textContent,/Command\+Enter \(Mac\)/);
 f.$('draft').listeners.keydown({key:'Enter',metaKey:true,preventDefault(){}});await flush();
 assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0);
 assert.deepEqual(f.requests.find(r=>r.url==='/api/demo/reply').body,{});
 assert.ok(f.models.at(-1).messages.some(m=>m.status==='local'&&m.body==='Local keyboard Demo reply'));
});

test('fast confirmed response and pending double clicks create one submission and clear only the successful draft',async()=>{
  const f=fixture();f.text('Synthetic same draft');
  const first=f.app.send();await flush();
  const second=f.app.send();await flush();
  assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1,'pending duplicate click');
  f.deferred.shift()(confirmed);await first;await second;
  const third=f.app.send();await flush();
  const actions=f.requests.filter(r=>r.url==='/api/action');
  assert.equal(actions.length,1,'confirmed unchanged draft must not submit another intent');
  await third;
  assert.equal(f.$('draft').value,'');
  assert.ok(f.models.at(-1).messages.some(row=>row.id===confirmed.id&&row.status==='delivered'));
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
    assert.equal(f.app.state.intent,null,'completed origin retired into its conversation');
    assert.ok(f.models.at(-1).messages.some(row=>row.id===confirmed.id&&row.body==='Origin draft'));
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
  assert.equal(f.app.state.intent,null,'confirmed old intent retired, newer text retained');
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

test('selected conversation polls initial and late replies/ack without mailbox or refresh clicks',async()=>{
  const f=fixture();
  const outgoing={id:'20261101T100000Z-aabbcc',from:'owner',to:'sample-dev',body:'Question',
    time:'2026-11-01T10:00:00+00:00',status:'delivered',certainty:'confirmed',receipts:[]};
  f.conversation.messages=[outgoing];
  await f.app.refresh();
  assert.equal(f.models.at(-1)?.messages[0]?.body,'Question','initial stored conversation loaded');
  f.conversation.messages=[{...outgoing,receipts:[{kind:'ack',by:'sample-dev',time:'2026-11-01T10:00:01+00:00',exact:true}]},
    {id:'20261101T100002Z-ddeeff',from:'sample-dev',to:'owner',body:'Actual reply',reply_to:outgoing.id,
      time:'2026-11-01T10:00:02+00:00',status:'received',certainty:'confirmed',receipts:[]}];
  await f.app.refresh();
  assert.equal(f.models.at(-1).messages.length,2);
  assert.equal(f.models.at(-1).messages[0].receipts[0].kind,'ack');
  assert.equal(f.models.at(-1).messages[1].body,'Actual reply');
  assert.equal(f.timers.size,0,'refresh does not allocate another scheduler');
});

test('outgoing Sending row is immediate and confirmed completion permits next normal equal-text message',async()=>{
  const f=fixture();f.text('Same deliberate message');
  const first=f.app.send();await flush();
  assert.equal(f.models.at(-1)?.messages.at(-1)?.status,'sending');
  f.deferred.shift()(confirmed);await first;
  assert.equal(f.$('draft').value,'');
  f.text('Same deliberate message');
  const second=f.app.send();await flush();
  const actions=f.requests.filter(r=>r.url==='/api/action');
  assert.equal(actions.length,2);assert.notEqual(actions[0].body.intent,actions[1].body.intent);
  f.deferred.shift()(confirmed);await second;
});

test('lost response reconciles only by exact intent association and positive receipts, without resend',async()=>{
  const f=fixture(),original=f.context.fetch;
  f.context.fetch=async(url,options)=>{
    if(url!=='/api/action')return original(url,options);
    const body=JSON.parse(options.body);f.requests.push({url,body});
    throw new Error('Synthetic lost HTTP response');
  };
  f.text('Owner question');await f.app.send();
  const intent=f.app.state.intent.id;
  assert.equal(f.models.at(-1).messages[0].status,'unconfirmed');
  f.conversation.messages=[{id:confirmed.id,from:'owner',to:'sample-dev',body:'Owner question',
    status:'unconfirmed',certainty:'unconfirmed',receipts:[],time:'2026-11-01T10:00:00+00:00'}];
  f.conversation.intents=[{intent,id:confirmed.id,status:'unconfirmed',certainty:'unconfirmed',completed:false}];
  await f.app.refresh();
  assert.equal(f.app.state.intent.id,intent,'progress alone remains uncertain');
  f.conversation.messages[0]={...f.conversation.messages[0],status:'delivered',certainty:'confirmed',
    receipts:[{kind:'delivered',time:'2026-11-01T10:00:01+00:00',exact:true}]};
  await f.app.refresh();
  assert.equal(f.app.state.intent,null);assert.equal(f.$('draft').value,'');
  assert.equal(f.models.at(-1).messages.length,1,'stored and optimistic rows join exactly');
  assert.equal(f.models.at(-1).messages[0].supersedes,'intent:'+intent);
  assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1);
});

test('monotonic draft/reply/read generations preserve ABA edits and retire only submitted choices',async()=>{
  const f=fixture();f.select('team','sample-dev','20261101T100003Z-112233');
  f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();
  f.text('Draft');const pending=f.app.send();await flush();
  f.text('Other text');f.text('Draft');
  f.deferred.shift()(confirmed);await pending;
  assert.equal(f.$('draft').value,'Draft','ABA edit must survive');
  assert.equal(f.app.state.reply,null,'submitted reply selection retired');
  assert.equal(f.$('read-with-reply').checked,false);
  const next=f.app.send();await flush();
  const payload=f.requests.filter(r=>r.url==='/api/action').at(-1).body;
  assert.equal(payload.reply_to,undefined);assert.equal(payload.read,undefined);
  f.deferred.shift()(confirmed);await next;
});

test('refused/expired/dropped outcomes keep draft and require an explicit uncertain/new-attempt decision',async()=>{
  for(const status of ['refused','expired','dropped']) {
    const f=fixture();f.text('Retained draft');const pending=f.app.send();await flush();
    f.deferred.shift()({...confirmed,status});await pending;
    assert.equal(f.$('draft').value,'Retained draft');assert.ok(f.app.state.intent);
  }
  const f=fixture();f.text('Uncertain draft');let pending=f.app.send();await flush();
  f.deferred.shift()(uncertain);await pending;
  pending=f.app.send();await flush();
  const actions=f.requests.filter(r=>r.url==='/api/action');
  assert.equal(actions[0].body.intent,actions[1].body.intent,'ordinary uncertain retry preserves id');
  f.deferred.shift()(uncertain);await pending;
  f.$('new-attempt').listeners.click();pending=f.app.send();await flush();
  assert.notEqual(f.requests.filter(r=>r.url==='/api/action').at(-1).body.intent,actions[0].body.intent);
  f.deferred.shift()(confirmed);await pending;
});

test('late selected-thread and auth-epoch responses cannot replace the current conversation',async()=>{
  const f=fixture(),original=f.context.fetch,pending=[];
  f.context.fetch=async(url,options)=>url.startsWith('/api/conversation?')?
    new Promise(resolve=>pending.push(value=>resolve({ok:true,json:async()=>value}))):original(url,options);
  const old=f.app.refresh();await flush();
  f.select('team','sample-reviewer');const current=f.app.refresh();await flush();
  pending[1]({messages:[{id:'20261101T100001Z-334455',from:'sample-reviewer',to:'owner',body:'Current',receipts:[]}],intents:[]});await current;
  pending[0]({messages:[{id:confirmed.id,from:'sample-dev',to:'owner',body:'Old',receipts:[]}],intents:[]});await old;
  assert.equal(f.models.at(-1).peer,'sample-reviewer');assert.equal(f.models.at(-1).messages[0].body,'Current');
  const stale=f.app.refresh();await flush();f.app.state.authEpoch++;
  pending[2]({messages:[],intents:[]});await stale;
  assert.equal(f.models.at(-1).messages[0].body,'Current');
});

test('visible history refreshes automatically and late old-target responses cannot overwrite it',async()=>{
  const f=fixture(),original=f.context.fetch,pending=[];
  f.app.state.tab='messages';f.app.state.messageMailbox='seat';
  f.context.fetch=async(url,options)=>url.startsWith('/api/list?')?
    new Promise(resolve=>pending.push(body=>resolve({ok:true,json:async()=>({items:[body]})}))):original(url,options);
  const first=f.app.refresh();await flush();
  f.select('team','sample-reviewer');const second=f.app.refresh();await flush();
  pending[1]({id:'20261101T100002Z-445566',sender:'owner',to:'sample-reviewer',box:'cur',body:'Reviewer current'});await second;
  pending[0]({id:confirmed.id,sender:'owner',to:'sample-dev',box:'cur',body:'Dev stale'});await first;
  const body=f.$('message-output').children[0].children[1].textContent;
  assert.equal(body,'Reviewer current');
});

test('mixed UTC/local-offset stored history and optimistic messages remain chronologically ordered',async()=>{
  const f=fixture();
  const earlier=new Date(Date.now()-60000+11*3600000).toISOString().replace('Z','+11:00');
  f.conversation.messages=[{id:'20261101T100000Z-aabbcc',from:'sample-dev',to:'owner',body:'Earlier local time',
    time:earlier,status:'received',certainty:'confirmed',receipts:[]}];
  await f.app.refresh();f.text('Newest optimistic row');
  const pending=f.app.send();await flush();
  assert.equal(f.models.at(-1).messages[0].body,'Earlier local time');
  assert.equal(f.models.at(-1).messages[1].body,'Newest optimistic row');
  f.deferred.shift()(confirmed);await pending;
});

test('inactive-thread metadata marks new activity without fetching its bodies or switching focus/target',async()=>{
 const f=fixture();f.select('team','sample-reviewer');await f.app.refresh();
 f.select('team','sample-dev');f.text('Keep current draft');await f.app.refresh();
 const before=f.requests.length;
 f.data.fleets[0].events=[{id:'20261101T100005Z-556677',kind:'send',from:'sample-reviewer',to:'owner',
   time:'2026-11-01T10:00:05+00:00',delivery:'queued'}];
 await f.app.refresh();
 const card=f.$('seats').children.find(node=>node.dataset.seat==='sample-reviewer').children.find(node=>node.className==='card');
 assert.ok(card.children.some(node=>node.className==='seat-new'&&/new/.test(node.textContent)));
 assert.equal(f.app.state.seat,'sample-dev');assert.equal(f.$('draft').value,'Keep current draft');
 assert.ok(f.requests.slice(before).filter(r=>r.url.startsWith('/api/conversation?')).every(r=>r.url.includes('seat=sample-dev')));
});

test('hidden-page work pauses and visibility return reconciles quietly without replacing input/focus',async()=>{
 const f=fixture();f.text('Keep focus and draft');f.$('draft').focus();
 f.context.document.hidden=true;const before=f.requests.length;await f.app.refresh();
 assert.equal(f.requests.length,before);
 f.context.document.hidden=false;await f.documentListeners.visibilitychange();await flush();
 assert.ok(f.models.some(m=>m.quiet===true));
 assert.equal(f.$('draft').value,'Keep focus and draft');assert.equal(f.context.document.activeElement,f.$('draft'));
});

test('Demo reply body stays local and never calls live action endpoint',async()=>{
 const f=fixture(),original=f.context.fetch;
 f.data.demo=true;f.data.hold=true;f.data.stages=['Sample'];f.data.stage=0;
 f.select('team','sample-dev','20261101T100010Z-667788');f.text('Local typed Demo reply');
 f.context.fetch=async(url,options)=>{
  if(url==='/api/demo/reply'){f.requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({demo:true})};}
  return original(url,options);
 };
 await f.app.send();
 assert.ok(f.models.at(-1).messages.some(m=>m.status==='local'&&m.body==='Local typed Demo reply'));
 assert.equal(f.requests.filter(r=>r.url==='/api/action').length,0);
 assert.deepEqual(f.requests.find(r=>r.url==='/api/demo/reply').body,{});
});

test('successful reply restores independent plain draft and preserves newer reply edits without overwriting its intent',async()=>{
 for(const editLater of [false,true]) {
  const f=fixture();f.text('Unsent plain draft');
  const plainIntent={id:'saved-uncertain-intent',fingerprint:'prior',result:uncertain};
  f.app.state.intent=plainIntent;f.app.saveDraft();
  f.app.chooseReply('20261101T100010Z-667788');f.text('Reply to question');
  const pending=f.app.send();await flush();
  if(editLater)f.text('Newer reply edit');
  f.deferred.shift()(confirmed);await pending;
  assert.equal(f.app.state.reply,null);
  assert.equal(f.$('draft').value,'Unsent plain draft');
  assert.equal(f.app.state.intent,plainIntent);
  if(editLater) {
   assert.equal(f.$('clear-reply').hidden,false,'saved newer reply edit remains reachable');
   f.$('clear-reply').listeners.click();
   assert.equal(f.$('draft').value,'Newer reply edit');
   assert.equal(f.app.state.reply,'20261101T100010Z-667788');
   f.$('clear-reply').listeners.click();
   assert.equal(f.$('draft').value,'Unsent plain draft');
   assert.equal(f.app.state.intent,plainIntent);
  }
 }
});

test('rolling recent window prunes reconciled completion bodies and never resurrects omitted history',async()=>{
 const f=fixture();
 for(let index=0;index<110;index++) {
  const id='20261101T100000Z-'+index.toString(16).padStart(6,'0');
  f.text('Message '+index);const pending=f.app.send();await flush();
  f.conversation.messages=[{id,from:'owner',to:'sample-dev',body:'Message '+index,status:'delivered',
    certainty:'confirmed',time:'2026-11-01T10:00:00+00:00',receipts:[{kind:'delivered',time:'2026-11-01T10:00:00+00:00',exact:true}]}];
  f.conversation.truncated=true;
  f.deferred.shift()({...confirmed,id});await pending;
 }
 assert.equal(f.models.at(-1).messages.length,1,'local completions must not defeat server history cap');
 assert.ok([...f.app.state.threads.values()].every(thread=>thread.local.size<=1),'completed body cache pruned');
 f.conversation.messages[0]={...f.conversation.messages[0],status:'expired',receipts:[{kind:'expired',time:'2026-11-01T10:01:00+00:00',exact:true}]};
 await f.app.refresh();
 assert.equal(f.models.at(-1).messages.length,1);assert.equal(f.models.at(-1).messages[0].status,'expired');
});

test('bounded Recent conversations reopens explicit cross-fleet thread without sending/reading and preserves independent drafts',async()=>{
 const f=fixture();
 f.select('docs','sample-dev');f.text('Source team to docs draft');
 const oldIntent={id:'cross-source-uncertain',fingerprint:'prior',result:uncertain};
 f.app.state.intent=oldIntent;f.app.saveDraft();await f.app.refresh();
 f.select('team','sample-reviewer');f.text('Other independent draft');await f.app.refresh();
 const recent=f.$('conversation-panel').children.find(element=>element.attrs['aria-label']==='Recent conversations');
 assert.ok(recent,'recent navigation exists outside the inert thread root');
 const target=recent.children.find(button=>button.textContent.includes('docs')&&button.textContent.includes('sample-dev'));
 assert.ok(target,'source/target/seat labelled cross-fleet thread retained');
 const calls=f.requests.length;target.listeners.click();await flush();
 assert.equal(f.app.state.fleet,'team');assert.equal(f.app.state.targetFleet,'docs');assert.equal(f.app.state.seat,'sample-dev');
 assert.equal(f.$('draft').value,'Source team to docs draft');assert.equal(f.app.state.intent,oldIntent);
 assert.ok(f.requests.slice(calls).every(request=>request.url!=='/api/action'),'selector has no write action');
 f.select('team','sample-reviewer');assert.equal(f.$('draft').value,'Other independent draft');
});

test('new activity in an unvisited declared seat gets a metadata badge without fetching that conversation',async()=>{
 const f=fixture();await f.app.refresh();
 f.data.fleets[0].events=[{id:'20261101T100005Z-778899',kind:'send',from:'sample-reviewer',to:'owner',
  time:'2026-11-01T10:00:05+00:00',delivery:'queued'}];
 const before=f.requests.length;await f.app.refresh();
 const card=f.$('seats').children.find(node=>node.dataset.seat==='sample-reviewer').children.find(node=>node.className==='card');
 assert.ok(card.children.some(node=>node.className==='seat-new'));
 assert.ok(f.requests.slice(before).filter(r=>r.url.startsWith('/api/conversation?')).every(r=>r.url.includes('seat=sample-dev')));
});

test('retained newer reply edit retires matching read choice while preserving a changed-after-submit read choice',async()=>{
 for(const changed of [false,true]) {
  const f=fixture();f.text('Independent plain draft');
  f.app.chooseReply('20261101T100010Z-667788');f.text('Submitted reply');
  f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();
  const pending=f.app.send();await flush();f.text('Newer saved reply edit');
  if(changed){f.$('read-with-reply').checked=false;f.$('read-with-reply').listeners.change();f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();}
  f.deferred.shift()(confirmed);await pending;
  f.$('clear-reply').listeners.click();
  assert.equal(f.$('draft').value,'Newer saved reply edit');
  assert.equal(f.$('read-with-reply').checked,changed,'retired read choice versus newer explicit read generation');
 }
});

test('snapshot service failure marks retained conversation stale while preserving rows/draft/focus',async()=>{
 const f=fixture(),original=f.context.fetch;
 f.conversation.messages=[{id:confirmed.id,from:'sample-dev',to:'owner',body:'Known answer',status:'received',
  certainty:'confirmed',receipts:[],time:'2026-11-01T10:00:00+00:00'}];
 await f.app.refresh();f.text('Retained draft');f.$('draft').focus();
 f.context.fetch=async(url,options)=>{if(url==='/api/snapshot')throw new Error('Service unreachable');return original(url,options);};
 await f.app.refresh();
 assert.equal(f.models.at(-1).state,'stale');assert.match(f.models.at(-1).note,/Disconnected/);
 assert.equal(f.models.at(-1).messages[0].body,'Known answer');assert.equal(f.$('draft').value,'Retained draft');
 assert.equal(f.context.document.activeElement,f.$('draft'));
});

test('offline send then same-intent online success replaces stale failure text with truthful active result',async()=>{
 const f=fixture(),original=f.context.fetch;let offline=true;
 f.context.fetch=async(url,options)=>{
  if(url==='/api/action'&&offline){f.requests.push({url,body:JSON.parse(options.body)});throw new Error('Failed to fetch');}
  return original(url,options);
 };
 f.text('Kept question');await f.app.send();
 assert.match(f.$('action-result').textContent,/uncertain/);const intent=f.app.state.intent.id;
 offline=false;const pending=f.app.send();await flush();f.deferred.shift()(confirmed);await pending;
 assert.equal(f.requests.filter(r=>r.url==='/api/action').at(-1).body.intent,intent);
 assert.equal(f.$('draft').value,'');assert.doesNotMatch(f.$('action-result').textContent,/uncertain or rejected|Draft and intent retained/i);
 assert.match(f.$('action-result').textContent,/delivered/);
});

for(const returnedBefore of [false,true]) for(const originChanged of [false,true]) {
 test('read retirement matrix: '+(returnedBefore?'returned before completion':'inactive origin')+' / '+(originChanged?'origin choice changed':'origin choice unchanged'),async()=>{
  const f=fixture();f.text('Origin independent plain');
  const originReply='20261101T100010Z-667788',otherReply='20261101T100011Z-8899aa';
  f.app.chooseReply(originReply);f.text('Origin submitted reply');
  f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();
  const pending=f.app.send();await flush();f.text('Origin newer saved edit');
  if(originChanged){f.$('read-with-reply').checked=false;f.$('read-with-reply').listeners.change();f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();}
  f.select('team','sample-reviewer',otherReply);f.text('Other active reply');
  f.$('read-with-reply').checked=true;f.$('read-with-reply').listeners.change();
  f.$('read-with-reply').checked=false;f.$('read-with-reply').listeners.change();
  if(returnedBefore)f.select('team','sample-dev',originReply);
  f.deferred.shift()(confirmed);await pending;
  if(returnedBefore) {
   assert.equal(f.$('draft').value,'Origin newer saved edit');
   assert.equal(f.$('read-with-reply').checked,originChanged,'active returned DOM must match origin choice retirement');
   f.select('team','sample-reviewer',otherReply);
   assert.equal(f.$('draft').value,'Other active reply');assert.equal(f.$('read-with-reply').checked,false);
   f.select('team','sample-dev',originReply);
  } else {
   assert.equal(f.$('draft').value,'Other active reply');assert.equal(f.$('read-with-reply').checked,false);
   f.select('team','sample-dev');f.$('clear-reply').listeners.click();
  }
  assert.equal(f.$('draft').value,'Origin newer saved edit');
  assert.equal(f.$('read-with-reply').checked,originChanged,'restored stored choice agrees with live DOM');
  f.select('team','sample-reviewer',otherReply);
  assert.equal(f.$('draft').value,'Other active reply');assert.equal(f.$('read-with-reply').checked,false);
 });
}

test('Recent recall clears client activity even when no matching new row can be safely attributed',async()=>{
 const f=fixture();f.select('docs','sample-dev');f.text('Retained cross-source draft');await f.app.refresh();
 f.select('team','sample-reviewer');f.text('Independent current draft');await f.app.refresh();f.$('draft').focus();
 f.data.fleets[0].events=[{id:'20261101T100005Z-99aabb',kind:'send',from:'sample-dev',to:'owner',
  time:'2026-11-01T10:00:05+00:00',delivery:'queued'}];
 await f.app.refresh();
 const nav=f.$('conversation-panel').children.find(element=>element.attrs['aria-label']==='Recent conversations');
 const target=nav.children.find(button=>button.textContent.includes('docs'));
 assert.match(target.textContent,/1 new|Activity \+1/);
 const before=f.requests.length,focused=f.context.document.activeElement;
 target.listeners.click();await flush();
 const recalled=nav.children.find(button=>button.textContent.includes('docs'));
 assert.doesNotMatch(recalled.textContent,/1 new|Activity \+1/);
 assert.equal(f.models.at(-1).messages.length,0,'no unsafe unlinked row fabricated');
 assert.equal(f.$('draft').value,'Retained cross-source draft');assert.equal(f.context.document.activeElement,focused);
 assert.ok(f.requests.slice(before).every(request=>request.url!=='/api/action'));
 f.select('team','sample-reviewer');assert.equal(f.$('draft').value,'Independent current draft');
});

for(const ownIncomplete of [false,true]) {
 test('lost-response evidence scope: '+(ownIncomplete?'selected row incomplete stays uncertain':'excluded history warning permits exact valid completion'),async()=>{
  const f=fixture(),original=f.context.fetch;
  f.context.fetch=async(url,options)=>{if(url==='/api/action'){f.requests.push({url,body:JSON.parse(options.body)});throw new Error('Lost response');}return original(url,options);};
  f.text('Validated outgoing question');await f.app.send();const intent=f.app.state.intent.id;
  f.conversation.messages=[{id:confirmed.id,from:'owner',to:'sample-dev',body:'Validated outgoing question',
   time:'2026-11-01T10:00:00+00:00',status:'delivered',certainty:'confirmed',incomplete:ownIncomplete,
   receipts:[{kind:'delivered',time:'2026-11-01T10:00:01+00:00',exact:true}]}];
  f.conversation.intents=[{intent,id:confirmed.id,status:'unconfirmed',certainty:'unconfirmed',completed:false}];
  f.conversation.incomplete=true;await f.app.refresh();
  if(ownIncomplete) {
   assert.equal(f.app.state.intent.id,intent);assert.equal(f.$('draft').value,'Validated outgoing question');
   assert.equal(f.models.at(-1).messages[0].certainty,'unconfirmed');
  } else {
   assert.equal(f.app.state.intent,null);assert.equal(f.$('draft').value,'');
   assert.equal(f.models.at(-1).state,'ready');assert.match(f.models.at(-1).note,/incomplete/);
   assert.equal(f.models.at(-1).messages[0].certainty,'confirmed');
  }
  assert.equal(f.models.at(-1).messages.length,1);assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1);
 });
}

test('lost response with conflicting NEW/CUR identity cannot retire from a positive journal result',async()=>{
 const f=fixture(),original=f.context.fetch;
 f.context.fetch=async(url,options)=>{if(url==='/api/action'){f.requests.push({url,body:JSON.parse(options.body)});throw new Error('Lost response');}return original(url,options);};
 f.text('Owner draft at conflicting full ID');await f.app.send();const intent=f.app.state.intent.id;
 f.conversation.messages=[{id:confirmed.id,from:'owner',to:'sample-dev',body:null,body_available:false,
  time:'2026-11-01T10:00:00+00:00',status:'delivered',certainty:'unconfirmed',incomplete:true,
  receipts:[{kind:'delivered',time:'2026-11-01T10:00:01+00:00',exact:true}]}];
 f.conversation.intents=[{intent,id:confirmed.id,status:'delivered',certainty:'confirmed',completed:true}];
 f.conversation.incomplete=true;await f.app.refresh();
 assert.equal(f.app.state.intent.id,intent);assert.equal(f.$('draft').value,'Owner draft at conflicting full ID');
 assert.equal(f.models.at(-1).messages[0].certainty,'unconfirmed');
 assert.equal(f.requests.filter(r=>r.url==='/api/action').length,1,'no replay or replacement submission');
});
