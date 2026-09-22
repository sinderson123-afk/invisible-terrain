'use strict';
// Isolated UI contract tests. No receiver, sound device, network, or wallpaper is opened.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = path.join(__dirname, 'original_wallpaper');
const html = fs.readFileSync(path.join(directory, 'index.html'), 'utf8');
const model = fs.readFileSync(path.join(directory, 'model.js'), 'utf8');
const source = fs.readFileSync(path.join(directory, 'app.js'), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const flush = async () => {for (let i = 0; i < 16; i++) await Promise.resolve();};
const baseAudio = () => ({enabled:false, muted:false, volume:.15, status:'disabled', error:null});
const baseState = () => ({status:'live', demo:false, frequency_hz:99200000, sequence:0, generation:1, stream_id:'test', audio:baseAudio()});

function harness(initial = baseState(), protocol = 'http:') {
  const elements = new Map(), timers = new Map(), calls = [];
  let nextTimer = 0, heldPost = null;
  const server = {state:clone(initial), holdNextPost:false, failNextState:false};
  function element(id) {
    const classes = new Set(), listeners = {};
    return {id, value:'', textContent:'', disabled:false, hidden:false, checked:false, title:'', listeners, attributes:{}, children:[],
      classList:{add:name => classes.add(name), remove:name => classes.delete(name), toggle(name, force) {const on = force === undefined ? !classes.has(name) : force;if (on) classes.add(name);else classes.delete(name);}},
      setAttribute(name, value) {this.attributes[name] = value;}, removeAttribute(name) {delete this.attributes[name];},
      addEventListener(type, listener) {listeners[type] = listener;},
      append(...nodes) {this.children.push(...nodes);}, replaceChildren(...nodes) {this.children = nodes;},
      focus() {document.activeElement = this;}, click() {if (!this.disabled) listeners.click?.({target:this});},
      getContext:() => ({setTransform(){}}),
      get innerHTML() {throw new Error('Network state must not be inserted as HTML');},
      set innerHTML(value) {throw new Error('Network state must not be inserted as HTML');}
    };
  }
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const node = element(match[1]);
    node.value = /\bvalue="([^"]*)"/.exec(match[0])?.[1] || '';
    elements.set(node.id, node);
  }
  const document = {getElementById:id => {assert.ok(elements.has(id), `DOM element ${id} exists`);return elements.get(id);},
    body:element('body'), activeElement:null, hidden:false, addEventListener(){}, createElement:() => element(null)};
  const context = {
    document, location:{protocol}, innerWidth:1920, innerHeight:1080, devicePixelRatio:1,
    performance:{now:() => 100}, matchMedia:() => ({matches:false}), AbortController,
    setTimeout(callback, delay) {const id = ++nextTimer;timers.set(id, {callback, delay});return id;},
    clearTimeout:id => timers.delete(id), requestAnimationFrame(){}, addEventListener(){},
    async fetch(url, options) {
      calls.push({url, options});
      if (url.endsWith('/api/state')) {
        if (server.failNextState) {server.failNextState = false;throw new Error('Test service disconnected');}
        return {ok:true, json:async () => clone(server.state)};
      }
      if (url.endsWith('/api/session')) return {ok:true, json:async () => ({token:'local-test-token'})};
      assert.ok(url.endsWith('/api/control'));
      const payload = JSON.parse(options.body);
      if (server.holdNextPost) {
        server.holdNextPost = false;
        await new Promise(resolve => {heldPost = resolve;});
      }
      if (payload.action === 'audio') {
        const {action, ...extra} = payload;
        Object.assign(server.state.audio, extra);
      }
      if (payload.action === 'scan') server.state.scan.status = 'scanning';
      if (payload.action === 'scan_cancel') server.state.scan.status = 'restoring';
      if (payload.action === 'station_save') {
        const favorite = server.state.radio.favorites.find(item => item.frequency_hz === payload.frequency_hz);
        if (favorite) favorite.name = payload.name;
        else server.state.radio.favorites.push({id:'saved-'+payload.frequency_hz, frequency_hz:payload.frequency_hz, name:payload.name || (payload.frequency_hz/1e6)+' MHz'});
        server.state.radio.revision++;
      }
      if (payload.action === 'station_remove') {
        server.state.radio.favorites = server.state.radio.favorites.filter(item => item.id !== payload.id);
        server.state.radio.revision++;
      }
      return {ok:true, json:async () => ({ok:true})};
    }
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(model, context);
  vm.runInContext(source, context);
  return {server, document, calls,
    get:id => elements.get(id),
    async event(id, type, extra = {}) {elements.get(id).listeners[type]({target:elements.get(id), ...extra});await flush();},
    async nodeEvent(node, type, extra = {}) {node.listeners[type]({target:node, ...extra});await flush();},
    async poll() {
      const found = [...timers].find(([, timer]) => timer.delay === 200 || timer.delay === 1000);
      assert.ok(found, 'Only the existing state poll schedules an update');
      timers.delete(found[0]);await found[1].callback();await flush();
    },
    async release() {assert.ok(heldPost);heldPost();heldPost = null;await flush();},
    posts:() => calls.filter(call => call.options.method === 'POST').map(call => JSON.parse(call.options.body))
  };
}

module.exports = {harness, baseState, baseAudio, flush, html, source};

if (require.main === module) (async () => {
  const ui = harness();await flush();
  assert.equal(ui.posts().length, 0, 'Page load never turns on sound');
  assert.equal(ui.calls.length, 1, 'Audio adds no session request or extra polling chain');
  assert.equal(ui.get('audio-toggle').disabled, false);
  assert.equal(ui.get('audio-toggle').textContent, '开启收听');
  assert.equal(ui.get('audio-mute').disabled, true);
  await ui.event('audio-toggle', 'click');
  assert.deepEqual(ui.posts(), [{action:'audio', enabled:true, muted:false}]);
  const sent = ui.calls.find(call => call.options.method === 'POST');
  assert.equal(sent.url, '/api/control');
  assert.equal(sent.options.headers['X-SDR-Token'], 'local-test-token');
  assert.equal(ui.get('audio-toggle').textContent, '停止收听');
  ui.server.state.audio.status = 'playing';await ui.poll();
  assert.equal(ui.get('audio-state').textContent, '单声道播放 · 15%');
  await ui.event('audio-mute', 'click');
  assert.deepEqual(ui.posts().at(-1), {action:'audio', muted:true});
  assert.equal(ui.get('audio-mute').attributes['aria-pressed'], 'true');
  assert.equal(ui.get('audio-state').textContent, '已静音');

  const beforeVolume = ui.posts().length;
  ui.document.activeElement = ui.get('audio-volume');
  ui.get('audio-volume').value = '64';
  for (let i = 0; i < 100; i++) await ui.event('audio-volume', 'input');
  await ui.poll();
  assert.equal(ui.posts().length, beforeVolume, 'Dragging emits no POST flood');
  assert.equal(ui.get('audio-volume').value, '64', 'Polling does not snap back the focused slider');
  assert.equal(ui.get('audio-volume-value').textContent, '64%');
  await ui.event('audio-volume', 'change');
  assert.deepEqual(ui.posts().at(-1), {action:'audio', volume:.64});
  ui.document.activeElement = null;await ui.poll();
  assert.equal(ui.get('audio-volume').value, '64');

  // Stopping remains possible while a different command occupies the shared command lock.
  ui.server.holdNextPost = true;
  await ui.event('tune', 'click');
  assert.equal(ui.get('audio-toggle').disabled, false);
  const beforeStop = ui.posts().length;
  await ui.event('audio-toggle', 'click');
  assert.equal(ui.posts().length, beforeStop, 'Queued stop waits for the existing request');
  assert.equal(ui.get('audio-toggle').textContent, '等待停止收听…');
  await ui.release();
  assert.deepEqual(ui.posts().at(-1), {action:'audio', enabled:false});
  assert.equal(ui.get('audio-toggle').textContent, '开启收听');
  assert.equal(ui.get('audio-state').textContent, '收听关闭');

  ui.server.state.audio = {...baseAudio(), enabled:true, status:'error', error:'<img src=x onerror=alert(1)>'};
  ui.server.state.frequency_hz = 145000000;ui.server.state.status = 'idle';await ui.poll();
  assert.equal(ui.get('audio-state').textContent, '音频异常');
  assert.ok(ui.get('audio-note').textContent.includes('<img src=x onerror=alert(1)>'));
  assert.equal(ui.get('audio-toggle').disabled, false, 'Out-of-band/stopped/error audio can always be stopped');
  await ui.event('audio-toggle', 'click');
  assert.equal(ui.get('audio-toggle').disabled, true, 'Cannot enable audio outside broadcast FM / live capture');

  const legacyState = baseState();delete legacyState.audio;
  const legacy = harness(legacyState);await flush();
  for (const id of ['audio-toggle', 'audio-volume', 'audio-mute']) assert.equal(legacy.get(id).disabled, true);
  assert.match(legacy.get('audio-note').textContent, /更新/);
  await legacy.event('audio-toggle', 'click');
  assert.equal(legacy.posts().length, 0);
  assert.equal(legacy.get('tune').disabled, false, 'Legacy SDR tuning remains available');

  const wallpaper = harness(baseState(), 'file:');await flush();
  for (const id of ['audio-toggle', 'audio-volume', 'audio-mute']) assert.equal(wallpaper.get(id).disabled, true);
  await wallpaper.event('audio-toggle', 'click');
  await wallpaper.event('audio-volume', 'change');
  assert.equal(wallpaper.posts().length, 0, 'File wallpaper remains read-only');
  assert.equal(wallpaper.calls.length, 1);
  assert.equal(wallpaper.calls[0].url, 'http://127.0.0.1:8766/api/state');

  for (const next of [{demo:true}, {status:'idle'}, {frequency_hz:87500000 - 1}, {frequency_hz:108000001}]) {
    const unsupported = harness({...baseState(), ...next});await flush();
    assert.equal(unsupported.get('audio-toggle').disabled, true);
    await unsupported.event('audio-toggle', 'click');
    assert.equal(unsupported.posts().length, 0);
  }
  for (const hz of [87500000,108000000]) {
    const boundary = harness({...baseState(), frequency_hz:hz});await flush();
    assert.equal(boundary.get('audio-toggle').disabled, false);
  }
  const labels = {disabled:'收听关闭',waiting:'等待采样',demo:'演示不播放',unsupported:'当前模式不支持',starting:'声音准备中',buffering:'声音缓冲中',playing:'单声道播放 · 15%',muted:'已静音',error:'音频异常',closed:'声音已关闭'};
  for (const [status, label] of Object.entries(labels)) {
    const next = baseState();next.audio = {...baseAudio(), enabled:status !== 'disabled', status};
    const variant = harness(next);await flush();
    assert.equal(variant.get('audio-state').textContent, label, `Backend status ${status} is represented`);
  }
  const invalidVolume = baseState();delete invalidVolume.audio.volume;
  const fallback = harness(invalidVolume);await flush();
  assert.equal(fallback.get('audio-volume').value, '15');
  assert.match(html, /id="audio-mute"[^>]+aria-pressed="false"[^>]*>静音/);
  const disconnectState = baseState();disconnectState.audio = {...baseAudio(), enabled:true, status:'playing', volume:.64};
  const disconnected = harness(disconnectState);await flush();
  assert.equal(disconnected.get('audio-state').textContent, '单声道播放 · 64%');
  disconnected.server.failNextState = true;await disconnected.poll();
  assert.equal(disconnected.get('audio-state').textContent, '音频状态未知');
  assert.equal(disconnected.get('audio-volume-value').textContent, '—', 'Disconnected service does not show stale volume as current');
  assert.equal(disconnected.get('audio-toggle').attributes['aria-pressed'], 'false');
  for (const id of ['audio-toggle', 'audio-volume', 'audio-mute']) assert.equal(disconnected.get(id).disabled, true);
  assert.doesNotMatch(source, /\b(?:AudioContext|webkitAudioContext|new Audio|setInterval)\b/);
  assert.doesNotMatch(html, /<audio\b/i);
  console.log('Original audio UI: safe startup, authenticated controls, queued stop, bounded volume, legacy support, FM bounds, and read-only wallpaper passed.');
})().catch(error => {console.error(error);process.exitCode = 1;});
