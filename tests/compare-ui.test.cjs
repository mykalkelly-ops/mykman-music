const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

function setup() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '4', textContent: '', innerHTML: '', style: {}, dataset: {}, disabled: false});
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => []},
    localStorage: {getItem: () => null, setItem: () => {}, removeItem: () => {}},
    sessionStorage: {getItem: () => null}, window: {}, console, URLSearchParams,
    fetch: async () => {throw Error('offline');}
  });
  let script = readFileSync('app/templates/compare.html', 'utf8').split('<script>')[1].split('</script>')[0];
  script = script.replace(/loadQueue\(\);\s*loadPrompt\(\);\s*loadReview\(\);\s*$/, '');
  vm.runInContext(script, context);
  vm.runInContext('renderQueue = () => {}; refreshCount = () => {};', context);
  return {context, element, run: code => vm.runInContext(code, context)};
}

test('refill handles network failure and unlocks retry', async () => {
  const s = setup();
  await s.run('loadQueue(true)');
  assert.equal(s.element('refill').disabled, false);
  assert.match(s.element('save-status').textContent, /offline/);
  assert.equal(s.run('queueBusy'), false);
});

test('skip payload is distinct and double clicks submit once', async () => {
  const s = setup();
  let count = 0, body, complete;
  s.context.fetch = async (path, options) => {
    count++; body = JSON.parse(options.body);
    await new Promise(resolve => complete = resolve);
    return {ok: true, json: async () => ({queue: {pairs: []}})};
  };
  s.run('queue = [{a:{id:1},b:{id:2}}]');
  const first = s.run("resolve(0, 'skip')");
  await s.run("resolve(0, 'skip')");
  complete();
  await first;
  assert.equal(count, 1);
  assert.equal(body.skip, true);
  assert.equal(body.winner_id, null);
  assert.equal(s.run('queueBusy'), false);
});

test('refill preserves unfinished difficulty and nostalgia', async () => {
  const s = setup();
  s.run('queue = [{a:{id:1},b:{id:2},difficulty:"hard",nostalgiaA:true}]');
  s.context.fetch = async () => ({ok: true, json: async () => ({pairs:[{a:{id:1},b:{id:2}}], total_comparisons:2})});
  await s.run('loadQueue(true)');
  assert.equal(s.run('queue[0].difficulty'), 'hard');
  assert.equal(s.run('queue[0].nostalgiaA'), true);
});

test('Apple pagination follows all pages and fails closed', async () => {
  const s = setup();
  s.run("appleMusicRequest = async path => ({response:{ok:true},payload:path.endsWith('page2')?{data:[{id:'b'}]}:{data:[{id:'a'}],next:'/v1/me/page2'}})");
  assert.equal((await s.run("allApplePages('/v1/me/page1')")).length, 2);
  s.run("appleMusicRequest = async () => ({response:{ok:false,status:403},payload:{}})");
  await assert.rejects(s.run("allApplePages('/v1/me/page1')"), /403/);
});

test('Apple addition failure is never reported as success', async () => {
  const s = setup();
  s.run("queue=[{a:{id:1,apple_library_id:'a'},b:{id:2,apple_library_id:'b'}}]; ensureComparisonPlaylistId=async()=> 'p'; currentPlaylistTrackIds=async()=>[]; addTracksToComparisonPlaylist=async()=>false;");
  await s.run('syncComparisonPlaylist()');
  assert.match(s.element('apple-playlist-status').textContent, /did not accept/);
});

test('unlinked XML songs do not trigger Apple requests', async () => {
  const s = setup();
  s.run('queue=[{a:{id:1},b:{id:2}}]');
  await s.run('syncComparisonPlaylist()');
  assert.match(s.element('apple-playlist-status').textContent, /still rank/);
});
