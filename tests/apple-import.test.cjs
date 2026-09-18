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
