const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
function setup() {
  const elements = {};
  const context = vm.createContext({Date, console, document: {getElementById: id => elements[id] ||= {style:{}, textContent:''}}, window:{}, fetch:async()=>{}, MusicKit:{getInstance:()=>({musicUserToken:'test'})}});
  const source = readFileSync('app/templates/apple_music.html','utf8').split('<script>')[1].split('</script>')[0];
  vm.runInContext(source, context);
  vm.runInContext("developerToken='test'; developerTokenAt=Date.now(); musicConfigured=true;",context);
  return {context, run: code=>vm.runInContext(code,context)};
}
test('library fetch includes every page',async()=>{
  const s=setup(); let calls=0;
  s.context.fetch=async()=>({ok:true,json:async()=>++calls===1?{data:[{id:'1'}],next:'/v1/me/library/playlists?offset=1'}:{data:[{id:'2'}]}});
  const rows=await s.run("fetchAllLibraryPages('/v1/me/library/playlists', {musicUserToken:'test'})");
  assert.equal(rows.length,2); assert.equal(calls,2);
});
test('failed later page clears old preview and unlocks retry',async()=>{
  const s=setup(); let calls=0;
  s.run("lastMonthPlaylistPayload=[{name:'old'}]");
  s.context.fetch=async()=>++calls===1?{ok:true,json:async()=>({data:[{id:'p',attributes:{name:'May 2026'}}],next:'/v1/me/library/playlists?offset=1'})}:{ok:false,status:503};
  await s.run('previewMonthPlaylistSync()');
  assert.equal(s.run('lastMonthPlaylistPayload'),null);
  assert.equal(s.run('syncBusy'),false);
});
test('track relationship fetch follows pagination',async()=>{
  const s=setup(); let calls=0;
  s.context.fetch=async()=>({ok:true,json:async()=>++calls===1?{data:[{id:'a'}],next:'/v1/me/library/playlists/p/tracks?offset=1'}:{data:[{id:'b'}]}});
  assert.equal((await s.run("fetchPlaylistTracks({musicUserToken:'test'}, 'p')")).length,2);
});

test('recovery inventory includes non-playlist songs without inferring completion',()=>{
  const s=setup();
  const report=s.run(`summarizeRecoveryLibrary([
    {id:'a',attributes:{artistName:'Artist',albumName:'Album',name:'Two',trackNumber:2}},
    {id:'b',attributes:{artistName:'Artist',albumName:'Album',name:'One',trackNumber:1}},
    {id:'a',attributes:{artistName:'Artist',albumName:'Album',name:'Two'}}
  ])`);
  assert.equal(report.tracks,2);
  assert.equal(report.albums[0].completion,'unknown');
  assert.equal(report.albums[0].tracks[0].title,'One');
  assert.equal(report.albums[0].tracks[0].completion,'unknown');
});

test('recovery scan follows full library pagination and performs only reads',async()=>{
  const s=setup(); let calls=[];
  s.context.fetch=async(url,options)=>{
    calls.push({url,method:options.method || 'GET'});
    return {ok:true,json:async()=>calls.length===1?{data:[{id:'a'}],next:'/v1/me/library/songs?offset=1'}:{data:[{id:'b'}]}};
  };
  await s.run('previewListeningRecovery()');
  assert.equal(calls.length,3);
  assert.ok(calls.every(c=>c.method==='GET'));
  assert.match(s.run("document.getElementById('recovery-status').textContent"),/Found 2 library tracks/);
});

test('recovery scan hides partial or stale results after API failure',async()=>{
  const s=setup(); let calls=0;
  s.context.fetch=async()=>++calls===1?{ok:true,json:async()=>({data:[{id:'a'}],next:'/v1/me/library/songs?offset=1'})}:{ok:false,status:503};
  await s.run('previewListeningRecovery()');
  assert.equal(s.run("document.getElementById('recovery-output').textContent"),'');
  assert.equal(s.run("document.getElementById('recovery-button').disabled"),false);
  assert.match(s.run("document.getElementById('recovery-status').textContent"),/Scan incomplete/);
});

test('recent history failure still permits a complete library inventory',async()=>{
  const s=setup();
  s.context.fetch=async url=>url.includes('/recent/')?{ok:false,status:503}:{ok:true,json:async()=>({data:[{id:'one',attributes:{name:'Song'}}]})};
  await s.run('previewListeningRecovery()');
  assert.equal(s.run('recoveryInventory.tracks.length'),1);
  assert.equal(s.run("document.getElementById('recovery-save').disabled"),false);
  assert.match(s.run("document.getElementById('recovery-status').textContent"),/history was unavailable/);
});

test('save sends inventory without Apple tokens and preserves retry after failure',async()=>{
  const s=setup(); let payload;
  s.run("recoveryInventory={tracks:[{library_id:'one',artist:'A',album:'B',title:'C'}],recent_catalog_ids:[],recent_library_ids:[]}");
  s.context.fetch=async(url,options)=>{payload=JSON.parse(options.body);return {ok:false,status:503};};
  await s.run('saveListeningRecovery()');
  assert.equal(payload.tracks[0].library_id,'one');
  assert.ok(!JSON.stringify(payload).includes('token'));
  assert.equal(s.run('recoveryBusy'),false);
  assert.equal(s.run("document.getElementById('recovery-save').disabled"),false);
  s.context.window.location={};
  s.context.fetch=async()=>({ok:true});
  await s.run('saveListeningRecovery()');
  assert.equal(s.context.window.location.href,'/listening');
});
