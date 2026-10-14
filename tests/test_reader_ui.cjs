/* Run the real UI handlers against a controlled DOM and delayed HTTP replies.
 * Native layout, focus and clipboard behavior are checked separately in-browser.
 * No third-party development or plugin dependency is required.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');

function harness() {
  const elements = new Map(), requests = [], copied = [];
  class Element {
    constructor(name) {
      this.name = name; this.listeners = {}; this.children = [];
      this.textContent = ''; this.value = ''; this.disabled = false; this.dataset = {}; this.attributes = {};
      this.hidden = false; this.open = false; this.isConnected = true;
      this.classList = {add(){}, remove(){}, toggle(){}};
    }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    emit(type, event = {}) { return Promise.all((this.listeners[type] || []).map(handler => handler({target:this, ...event}))); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name,value) { this.attributes[name]=value; }
    getAttribute(name) { return this.attributes[name]; }
    scrollIntoView() {}
    focus() { document.activeElement = this; }
    closest() { return null; }
    showModal() { this.open = true; get('#reader-close').focus(); }
    close() { this.open = false; this.emit('close'); }
  }
  function get(selector) { if (!elements.has(selector)) elements.set(selector, new Element(selector)); return elements.get(selector); }
  const document = {querySelector:get, querySelectorAll:()=>[], createElement:name=>new Element(name),
    createTextNode:text=>Object.assign(new Element('text'),{textContent:text}), activeElement:null, body:new Element('body')};
  const fixtureHits = Array.from({length:6}, (_, index) => ({Title:`关键词 人工段落 ${index + 1}`,ContextData:{
    path:`samples/记录${index + 1}.md`, alias:'样例', heading:`章节 ${index + 1}`,
    text:`关键词 人工段落 ${index + 1}`, line_start:3 + index, line_end:3 + index, text_matches:[]
  }}));
  let configs=0;
  const sandbox = {document, window:{addEventListener(){}}, location:{hash:'',search:'',pathname:'/'},
    sessionStorage:{getItem:()=>'',setItem(){}}, URLSearchParams, TextEncoder, AbortController,
    crypto:webcrypto, structuredClone, setTimeout, clearTimeout, fixtureHits,
    navigator:{clipboard:{writeText:async text=>copied.push(text)}},
    fetch:(route, options) => {
      // The initial config request intentionally stays pending; tests supply an
      // already-rendered search snapshot, independent of config form controls.
      if (route === '/api/config' && ++configs===1) return new Promise(()=>{});
      return new Promise(resolve => requests.push({route, body:options.body ? JSON.parse(options.body) : undefined, signal:options.signal,
        succeed:data=>resolve({ok:true,json:async()=>data}), fail:message=>resolve({ok:false,json:async()=>({error:message})})}));
    }
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(source, context);
  vm.runInContext('serverData = {config:{editor:"default"}}; previewHits = fixtureHits; previewQuery = "@样例 关键词";', context);
  const opener = new Element('preview-opener');
  const open = (position=0)=>{ sandbox.testPosition = position; sandbox.testOpener = opener;
    vm.runInContext('openReader(fixtureHits[testPosition].ContextData, testOpener, testPosition)', context); };
  const reply = (request, position)=>request.succeed({before:null, selected:fixtureHits[position].ContextData, after:null});
  const content = ()=>JSON.stringify(get('#reader-content').children, (key, value)=>['listeners','classList'].includes(key)?undefined:value);
  return {get, requests, copied, document, fixtureHits, open, reply, content, opener, context};
}

function previewCopy(h) {
  const card = vm.runInContext('hitCard(fixtureHits[0], 0)', h.context);
  const copy = card.children.at(-1).children.find(child=>child.className==='copy-excerpt');
  return {card, copy};
}

function searchFixture(h) {
  h.context.searchFixtureData = {config:{editor:'default',roots:[{alias:'写作'},{alias:'工作'},{alias:'Straße'}]},
    root_states:[{alias:'写作',exists:true},{alias:'工作',exists:false},{alias:'Straße',exists:true}],stats:{files:3,paragraphs:9},index_status:{}};
  vm.runInContext('serverData=searchFixtureData; renderScopes(serverData)',h.context);
  return h.context.searchFixtureData;
}
function latestSearch(h) { return h.requests.filter(request=>request.route==='/api/search').at(-1); }
async function finishSearch(h, request, summary, results=[]) {
  const oldConfigs=h.requests.filter(request=>request.route==='/api/config').length;
  request.succeed({results,search_status:{summary,prepare_ms:0,index_ms:0,scope_ms:0,close_ms:0,format_ms:0,duration_ms:0,
    search:summary.matched===undefined ? undefined : {matched:summary.matched,returned:summary.returned,query_ms:0,ranking_ms:0,result_ms:0}}});
  await until(()=>h.requests.filter(request=>request.route==='/api/config').length>oldConfigs);
  h.requests.filter(request=>request.route==='/api/config').at(-1).succeed(h.context.searchFixtureData);
  await new Promise(resolve=>setImmediate(resolve));
}
const noMatch = {empty_reason:'no_match',scope:'写作',keywords:'雨夜 车站',matched:0,returned:0,limit:2,scope_files:1,scope_paragraphs:7,unavailable_roots:0,has_exclusions:false,skipped_total:0,index_issue_count:0};

test('example queries keep the chosen scope',async()=>{
  const h=harness(); searchFixture(h); h.get('#preview-query').value='@写作';
  vm.runInContext('useExample("雨夜")',h.context); await until(()=>latestSearch(h));
  assert.equal(latestSearch(h).body.query,'@写作 雨夜');
  await finishSearch(h,latestSearch(h),{...noMatch,keywords:'雨夜'});
});

test('a failed metadata refresh clears result cap hints and stale reading snapshots',async()=>{
  const h=harness(); searchFixture(h); h.get('#preview-query').value='雨夜';
  vm.runInContext('search("雨夜")',h.context); await until(()=>latestSearch(h));
  latestSearch(h).succeed({results:h.fixtureHits,search_status:{summary:{...noMatch,matched:9,returned:6}}});
  await until(()=>h.requests.some(request=>request.route==='/api/config'));
  h.requests.find(request=>request.route==='/api/config').fail('无法刷新配置');
  await until(()=>h.get('#preview-count').textContent.includes('未完成'));
  assert.equal(h.get('#search-limit-note').hidden,true);
  assert.equal(vm.runInContext('previewHits.length',h.context),0);
  assert.equal(h.get('#search-timings').hidden,true);
});

test('scope chips use saved roots and replace an existing selector without dropping keywords', async()=>{
  const h=harness(); searchFixture(h);
  assert.deepEqual(h.get('#search-scopes').children.map(button=>button.textContent),['全部目录','写作','工作','Straße']);
  h.get('#preview-query').value='sn ＠写作 雨夜 车站';
  await h.get('#search-scopes').children[2].emit('click'); await until(()=>latestSearch(h));
  assert.equal(h.get('#preview-query').value,'@工作 雨夜 车站');
  assert.equal(latestSearch(h).body.query,'@工作 雨夜 车站');
  assert.equal(h.get('#search-scopes').children[2].getAttribute('aria-pressed'),'true');
  await finishSearch(h,latestSearch(h),{...noMatch,scope:'工作'});
  await h.get('#search-scopes').children[0].emit('click'); await until(()=>h.requests.filter(request=>request.route==='/api/search').length===2);
  assert.equal(latestSearch(h).body.query,'雨夜 车站');
  await finishSearch(h,latestSearch(h),{...noMatch,scope:null});
});

test('choosing a scope without keywords does not request indexing or search', async()=>{
  const h=harness(); searchFixture(h); h.get('#preview-query').value='';
  await h.get('#search-scopes').children[1].emit('click');
  assert.equal(h.get('#preview-query').value,'@写作'); assert.equal(h.requests.length,0);
  assert.equal(h.document.activeElement,h.get('#preview-query'));
});

test('match count and result cap are distinct and exact-limit matches are not truncated',()=>{
  const h=harness(); h.context.countSummary={...noMatch,matched:8,returned:2};
  vm.runInContext('renderSearchCount(countSummary,fixtureHits.slice(0,2))',h.context);
  assert.match(h.get('#preview-count').textContent,/匹配 8 条，返回 2 条/);
  assert.equal(h.get('#search-limit-note').hidden,false);
  h.context.countSummary.matched=2;
  vm.runInContext('renderSearchCount(countSummary,fixtureHits.slice(0,2))',h.context);
  assert.equal(h.get('#search-limit-note').hidden,true);
});

test('unknown scope is not highlighted as all and backend canonical aliases survive config refresh', async()=>{
  const h=harness(); searchFixture(h); h.get('#preview-query').value='@未配置 雨夜';
  vm.runInContext('syncScopes(); search("@未配置 雨夜")',h.context); await until(()=>latestSearch(h));
  assert.ok(h.get('#search-scopes').children.every(button=>button.getAttribute('aria-pressed')==='false'));
  await finishSearch(h,latestSearch(h),{empty_reason:'unknown_alias',scope:null,keywords:'雨夜',requested_alias:'未配置'});
  assert.equal(h.get('#preview-results').children[0].children[0].textContent,'这个目录别名尚未配置');
  h.get('#preview-query').value='＠STRASSE 雨夜';
  vm.runInContext('search("＠STRASSE 雨夜")',h.context); await until(()=>h.requests.filter(request=>request.route==='/api/search').length===2);
  await finishSearch(h,latestSearch(h),{...noMatch,scope:'Straße',keywords:'雨夜'});
  assert.equal(h.get('#search-scopes').children[3].getAttribute('aria-pressed'),'true');
});

test('empty guidance distinguishes unavailable directories, empty content and global skipped files',()=>{
  const h=harness(); searchFixture(h);
  for (const [reason,title] of [['directories_unavailable','当前范围的目录不可用'],['no_paragraphs','当前范围还没有可检索段落'],['no_match','没有找到匹配段落']]) {
    h.context.emptySummary={...noMatch,empty_reason:reason,has_exclusions:true,index_issue_count:2,skipped_total:1};
    const section=vm.runInContext('emptySearch(emptySummary)',h.context);
    assert.equal(section.children[0].textContent,title);
    assert.match(section.children.find(child=>child.className==='empty-warning').textContent,/所有目录.*跳过 1 个文件/);
    assert.ok(section.children.at(-1).children.some(button=>button.textContent==='查看索引原因'));
    if (reason==='no_paragraphs') assert.match(section.children[1].textContent,/检查排除规则/);
  }
});

test('reducing keywords keeps scope and expanding scope keeps all keywords',async()=>{
  const h=harness(); searchFixture(h); h.context.emptySummary={...noMatch};
  const section=vm.runInContext('emptySearch(emptySummary)',h.context);
  await section.children.at(-1).children.find(button=>button.textContent==='只搜第一个词').emit('click');
  await until(()=>latestSearch(h)); assert.equal(latestSearch(h).body.query,'@写作 雨夜');
  await finishSearch(h,latestSearch(h),{...noMatch,keywords:'雨夜'});
  await section.children.at(-1).children.find(button=>button.textContent==='搜索全部目录').emit('click');
  await until(()=>h.requests.filter(request=>request.route==='/api/search').length===2);
  assert.equal(latestSearch(h).body.query,'雨夜 车站');
  await finishSearch(h,latestSearch(h),{...noMatch,scope:null});
});

test('late results from the old scope cannot overwrite new counts or empty guidance',async()=>{
  const h=harness(); searchFixture(h); h.get('#preview-query').value='@写作 雨夜';
  vm.runInContext('search("@写作 雨夜")',h.context); await until(()=>latestSearch(h)); const old=latestSearch(h);
  await h.get('#search-scopes').children[2].emit('click'); await until(()=>h.requests.filter(request=>request.route==='/api/search').length===2);
  await finishSearch(h,latestSearch(h),{...noMatch,scope:'工作',keywords:'雨夜'});
  old.succeed({results:h.fixtureHits,search_status:{summary:{...noMatch,matched:80,returned:6}}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(h.get('#preview-count').textContent,/@工作 · 匹配 0 条/);
  assert.equal(h.get('#preview-results').children[0].className,'search-empty');
  assert.equal(h.get('#search-limit-note').hidden,true);
});

test('preview card copy verifies the source and refuses a stale paragraph', async()=>{
  const h=harness(), {copy}=previewCopy(h);
  const failed=copy.emit('click'); await until(()=>h.requests.length===1);
  assert.equal(copy.disabled,true); h.requests[0].fail('资料已变化，请重新搜索。'); await failed;
  assert.equal(h.copied.length,0); assert.equal(copy.disabled,false);
  const valid=copy.emit('click'); await until(()=>h.requests.length===2); h.reply(h.requests[1],0); await valid;
  assert.deepEqual(h.copied,['关键词 人工段落 1\n\n出处：samples/记录1.md · 章节 1 · 第 3 行']);
});

test('preview card removal or a new query cancels a delayed clipboard write', async()=>{
  for (const changeQuery of [false,true]) {
    const h=harness(), {card,copy}=previewCopy(h);
    const pending=copy.emit('click'); await until(()=>h.requests.length===1);
    if (changeQuery) vm.runInContext('++searchNumber',h.context); else card.isConnected=false;
    h.reply(h.requests[0],0); await pending; assert.equal(h.copied.length,0);
  }
});
async function until(condition) {
  for (let i=0; i<200; i++) { if (condition()) return; await new Promise(resolve=>setImmediate(resolve)); }
  assert.fail('Expected asynchronous UI state was not reached');
}
async function loaded(h, request, position) { h.reply(request,position); await until(()=>!h.get('#reader-copy').disabled); }

test('reader traverses all returned hits, including beyond four preview cards', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1); await loaded(h,h.requests[0],0);
  assert.equal(h.get('#reader-position').textContent,'第 1 / 6 条');
  assert.equal(h.get('#reader-previous').disabled,true);
  assert.match(h.get('#reader-query').textContent,/@样例 关键词/);
  for (let position=1; position<6; position++) {
    await h.get('#reader-next').emit('click');
    assert.equal(h.get('#reader-copy').disabled,true);
    assert.equal(h.get('#reader-open').disabled,true);
    assert.equal(h.get('#reader-content').children.length,0);
    await until(()=>h.requests.length===position+1); await loaded(h,h.requests[position],position);
    assert.equal(h.get('#reader-position').textContent,`第 ${position+1} / 6 条`);
    assert.equal(h.get('#reader-title').textContent,`记录${position+1}.md`);
    assert.match(h.content(),new RegExp(`人工段落 ${position+1}`));
  }
  assert.equal(h.get('#reader-next').disabled,true);
  await h.get('#reader-next').emit('click'); assert.equal(h.requests.length,6);
});

test('late context replies cannot overwrite a newer result', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1);
  await h.get('#reader-next').emit('click'); await until(()=>h.requests.length===2);
  await h.get('#reader-next').emit('click'); await until(()=>h.requests.length===3);
  assert.equal(h.requests[0].signal.aborted,true);
  await loaded(h,h.requests[2],2);
  h.reply(h.requests[1],1); h.reply(h.requests[0],0); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.get('#reader-title').textContent,'记录3.md');
  assert.match(h.content(),/人工段落 3/); assert.doesNotMatch(h.content(),/人工段落 [12]/);
  const copy=h.get('#reader-copy').emit('click'); await until(()=>h.requests.length===4);
  assert.equal(h.requests[3].body.path,'samples/记录3.md');
  assert.equal(h.requests[3].body.line_start,5);
  h.reply(h.requests[3],2); await copy;
  assert.deepEqual(h.copied,['关键词 人工段落 3\n\n出处：samples/记录3.md · 章节 3 · 第 5 行']);
});

test('closing and reopening excludes old replies and restores the opening result focus', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1);
  await h.get('#reader-close').emit('click');
  assert.equal(h.document.activeElement,h.opener);
  h.open(3); await until(()=>h.requests.length===2); await loaded(h,h.requests[1],3);
  h.reply(h.requests[0],0); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.get('#reader-position').textContent,'第 4 / 6 条'); assert.match(h.content(),/人工段落 4/);
});

test('copy revalidates changed or missing sources and disables stale actions', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1); await loaded(h,h.requests[0],0);
  const copying=h.get('#reader-copy').emit('click'); await until(()=>h.requests.length===2);
  h.requests[1].fail('该段落已变化，请重新检索'); await copying;
  assert.deepEqual(h.copied,[]); assert.equal(h.get('#reader-copy').disabled,true);
  assert.equal(h.get('#reader-open').disabled,true); assert.equal(h.get('#reader-search').hidden,false);
  assert.equal(h.get('#reader-content').children.length,0);
  await h.get('#reader-next').emit('click'); await until(()=>h.requests.length===3);
  await loaded(h,h.requests[2],1); assert.match(h.content(),/人工段落 2/);
});

test('switching or closing before copy validation finishes cancels clipboard writes', async()=>{
  for (const action of ['switch','close']) {
    const h=harness(); h.open(); await until(()=>h.requests.length===1); await loaded(h,h.requests[0],0);
    const copying=h.get('#reader-copy').emit('click'); await until(()=>h.requests.length===2);
    await h.get(action==='switch'?'#reader-next':'#reader-close').emit('click');
    h.reply(h.requests[1],0); await copying;
    assert.deepEqual(h.copied,[]);
  }
});

test('Alt arrows navigate without replacing plain arrow or editable input behavior', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1);
  let prevented=0;
  const key=(options)=>h.get('#reader').emit('keydown',{key:'ArrowDown',altKey:true,preventDefault:()=>prevented++,...options});
  await key({altKey:false}); await key({ctrlKey:true});
  await key({target:{closest:()=>({})}}); await key({isComposing:true});
  assert.equal(h.requests.length,1); assert.equal(prevented,0);
  await key({}); await until(()=>h.requests.length===2);
  assert.equal(prevented,1); assert.equal(h.get('#reader-position').textContent,'第 2 / 6 条');
  await key({key:'ArrowUp'}); await until(()=>h.requests.length===3);
  assert.equal(h.get('#reader-position').textContent,'第 1 / 6 条');
});

test('starting a new query discards old result snapshots', async()=>{
  const h=harness(); h.open(); await until(()=>h.requests.length===1); await loaded(h,h.requests[0],0);
  vm.runInContext('resetPreview("新的检索")',h.context);
  assert.equal(h.get('#reader').open,false);
  assert.equal(vm.runInContext('previewHits.length',h.context),0);
  h.reply(h.requests[0],0); assert.equal(h.get('#reader').open,false);
});
