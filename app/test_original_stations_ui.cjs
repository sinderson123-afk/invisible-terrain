'use strict';
// UI contract tests share the hardware-free DOM and fetch harness with audio tests.
const assert = require('node:assert/strict');
const {harness, baseState, flush, html, source} = require('./test_original_audio_ui.cjs');
const stationState = () => ({...baseState(), radio:{favorites:[], last_frequency_hz:93400000, volume:.15, error:null, revision:0}, scan:{status:'idle', progress:0, current_hz:null, candidates:[], error:null}});
const rows = ui => ui.get('station-list').children.filter(node => node.className.startsWith('station-row'));
const title = row => row.children[0].children[0].textContent;
const meta = row => row.children[0].children[1].textContent;
const action = (row, text) => row.children[1].children.find(button => button.textContent === text);

(async () => {
  const ui = harness(stationState());await flush();
  assert.equal(ui.posts().length, 0, 'No automatic scan, tuning, favorites, or audio on page load');
  assert.equal(ui.calls.length, 1, 'No extra polling chain or session request');
  assert.equal(ui.get('scan-start').disabled, false);
  assert.equal(ui.get('scan-cancel').disabled, true);
  assert.equal(ui.get('station-save').disabled, false);
  assert.match(ui.get('station-memory-note').textContent, /93\.400 MHz/);
  assert.match(html, /<details id="station-directory"[^>]*>/);
  assert.doesNotMatch(/<details id="station-directory"[^>]*>/.exec(html)[0], /\bopen\b/, 'Directory starts collapsed');
  assert.match(html, /id="station-name"[^>]+maxlength="60"/);

  ui.get('station-name').value = '本地广播';
  ui.document.activeElement = ui.get('station-name');
  await ui.poll();
  assert.equal(ui.get('station-name').value, '本地广播', 'Polling preserves current naming input');
  await ui.event('station-save', 'click');
  assert.deepEqual(ui.posts(), [{action:'station_save', frequency_hz:99200000, name:'本地广播'}]);
  assert.equal(ui.get('station-name').value, '');
  const write = ui.calls.find(call => call.options.method === 'POST');
  assert.equal(write.options.headers['X-SDR-Token'], 'local-test-token');
  await ui.poll();
  assert.equal(rows(ui).length, 1);
  assert.equal(title(rows(ui)[0]), '★ 本地广播');
  assert.match(meta(rows(ui)[0]), /99\.200 MHz/);
  await ui.nodeEvent(action(rows(ui)[0], '调到这里'), 'click');
  assert.deepEqual(ui.posts().at(-1), {action:'tune', frequency_hz:99200000});
  assert.equal(ui.posts().filter(item => ['start','audio'].includes(item.action)).length, 0, 'Selecting a station never enables receiving or sound');

  const markup = '<img src=x onerror=alert(1)>';
  await ui.nodeEvent(action(rows(ui)[0], '改名'), 'click');
  assert.equal(ui.get('station-name').value, '本地广播');
  assert.equal(ui.get('station-save').textContent, '保存名称');
  ui.get('station-name').value = markup;
  ui.server.state.frequency_hz = 100000000;await ui.poll();
  assert.equal(ui.get('station-name').value, markup, 'Poll and changing RF frequency cannot overwrite a rename');
  await ui.event('station-save', 'click');
  assert.deepEqual(ui.posts().at(-1), {action:'station_save', frequency_hz:99200000, name:markup}, 'Rename remains attached to its original frequency');
  await ui.poll();
  assert.equal(title(rows(ui)[0]), '★ '+markup, 'Station names render as literal text');
  assert.equal(ui.server.state.radio.favorites.length, 1, 'Renaming does not duplicate the station');

  ui.server.state.radio.favorites.push({id:'station-934', frequency_hz:93400000, name:'日间电台'});
  ui.server.state.scan = {status:'complete', progress:1, current_hz:null, error:null, candidates:[
    {frequency_hz:99200000, score_db:9.5, bandwidth_hz:180000},
    {frequency_hz:93400000, score_db:12.4, bandwidth_hz:200000},
    {frequency_hz:101700000, score_db:16.1, bandwidth_hz:220000}
  ]};
  await ui.poll();
  assert.equal(rows(ui).length, 3, 'Same-frequency favorite and candidate merge into one row');
  assert.match(meta(rows(ui)[0]), /^93\.400 MHz/);
  assert.match(meta(rows(ui)[0]), /相对强度 12\.4 dB/);
  assert.equal(title(rows(ui)[2]), '广播候选', 'No invented station names');
  ui.get('station-sort').value = 'signal';await ui.event('station-sort', 'change');
  assert.match(meta(rows(ui)[0]), /^101\.700 MHz/);
  ui.get('station-search').value = '日间';ui.document.activeElement = ui.get('station-search');
  await ui.event('station-search', 'input');await ui.poll();
  assert.equal(ui.get('station-search').value, '日间');assert.equal(rows(ui).length, 1);
  assert.equal(title(rows(ui)[0]), '★ 日间电台');
  ui.get('station-search').value = '101.7';await ui.event('station-search', 'input');
  assert.equal(rows(ui).length, 1);
  ui.get('station-only-favorites').checked = true;await ui.event('station-only-favorites', 'change');
  assert.equal(rows(ui).length, 0);
  ui.get('station-search').value = '';await ui.event('station-search', 'input');
  assert.equal(rows(ui).length, 2);
  ui.get('station-only-favorites').checked = false;await ui.event('station-only-favorites', 'change');
  const candidate = rows(ui).find(row => !title(row).startsWith('★'));
  await ui.nodeEvent(action(candidate, '收藏'), 'click');
  assert.match(ui.get('station-edit-label').textContent, /101\.700 MHz/);
  const beforeCancelEdit = ui.posts().length;
  ui.get('station-name').value = '未保存的文字';await ui.event('station-edit-cancel', 'click');
  assert.equal(ui.get('station-name').value, '');assert.equal(ui.posts().length, beforeCancelEdit);
  const favoriteRow = rows(ui).find(row => title(row) === '★ 日间电台');
  await ui.nodeEvent(action(favoriteRow, '删除'), 'click');
  assert.deepEqual(ui.posts().at(-1), {action:'station_remove', id:'station-934'});
  assert.equal(ui.server.state.radio.favorites.length, 1);

  // Cancellation is accepted even during the scan start POST, then sent in order.
  ui.server.state.scan.status = 'idle';ui.server.state.status = 'live';await ui.poll();
  ui.server.holdNextPost = true;await ui.event('scan-start', 'click');
  assert.equal(ui.get('scan-cancel').disabled, false);
  const beforeCancel = ui.posts().length;
  await ui.event('scan-cancel', 'click');
  assert.equal(ui.posts().length, beforeCancel);
  assert.equal(ui.get('scan-cancel').textContent, '等待取消…');
  await ui.release();
  assert.deepEqual(ui.posts().slice(-2), [{action:'scan'}, {action:'scan_cancel'}]);
  ui.server.state.status = 'scanning';ui.server.state.scan.status = 'scanning';
  ui.server.state.scan.progress = .375;ui.server.state.scan.current_hz = 96000000;
  ui.server.state.audio.enabled = true;await ui.poll();
  assert.match(ui.get('scan-status').textContent, /38%.*96\.000 MHz/);
  assert.equal(ui.get('scan-progress').value, .375);
  for (const id of ['scan-start','tune','up','down','frequency','mode','station-save'])assert.equal(ui.get(id).disabled, true, `${id} blocked while scanning`);
  assert.equal(ui.get('start').textContent, '停止接收');assert.equal(ui.get('start').disabled, false);
  assert.equal(ui.get('audio-toggle').disabled, false, 'Audio can still be stopped while scanning');
  const beforeBlockedTune = ui.posts().length;
  await ui.event('frequency', 'keydown', {key:'Enter'});
  assert.equal(ui.posts().length, beforeBlockedTune, 'Keyboard Enter cannot bypass the scanning tune lock');
  await ui.event('audio-toggle', 'click');
  assert.deepEqual(ui.posts().at(-1), {action:'audio', enabled:false});
  await ui.event('start', 'click');assert.deepEqual(ui.posts().at(-1), {action:'stop'});
  assert.ok(rows(ui).every(row => row.children[1].children.every(button => button.disabled)));

  // Queuing audio stop alongside cancellation must not drop either command.
  const queuedState = stationState();queuedState.audio.enabled = true;
  const queued = harness(queuedState);await flush();queued.server.holdNextPost = true;
  await queued.event('scan-start', 'click');await queued.event('scan-cancel', 'click');
  await queued.event('audio-toggle', 'click');await queued.release();await flush();
  assert.deepEqual(queued.posts().map(item => item.action), ['scan','scan_cancel','audio']);
  assert.deepEqual(queued.posts().at(-1), {action:'audio', enabled:false});

  const legacy = harness(baseState());await flush();
  for(const id of ['scan-start','scan-cancel','station-search','station-sort','station-only-favorites','station-name','station-save'])assert.equal(legacy.get(id).disabled, true);
  assert.match(legacy.get('station-service-note').textContent, /待更新/);
  assert.equal(legacy.get('tune').disabled, false);assert.equal(legacy.get('audio-toggle').disabled, false);
  await legacy.event('scan-start', 'click');await legacy.event('scan-cancel', 'click');await legacy.event('station-save', 'click');
  assert.equal(legacy.posts().length, 0);

  for (const initial of [stationState(), baseState()]) {
    const wallpaper = harness(initial, 'file:');await flush();
    assert.equal(wallpaper.get('station-directory').hidden, true);
    await wallpaper.event('scan-start', 'click');await wallpaper.event('scan-cancel', 'click');await wallpaper.event('station-save', 'click');
    assert.equal(wallpaper.posts().length, 0, 'File-mode wallpaper is always read-only, even without private radio data');
  }
  for (const override of [{demo:true}, {status:'idle'}, {status:'starting'}]) {
    const blocked = harness({...stationState(), ...override});await flush();
    assert.equal(blocked.get('scan-start').disabled, true);
    await blocked.event('scan-start', 'click');assert.equal(blocked.posts().length, 0);
  }
  ui.server.failNextState = true;await ui.poll();
  assert.equal(rows(ui).length, 0, 'Service loss does not leave old station controls actionable');
  assert.match(ui.get('station-service-note').textContent, /未连接/);
  assert.equal(ui.get('scan-cancel').disabled, true);
  assert.doesNotMatch(source, /\binnerHTML\b|\bsetInterval\b/);
  console.log('Station UI: safe startup, authenticated writes, search/sort/favorites, literal names, persistent edit focus, queued scan cancellation, scanning locks, legacy compatibility, and read-only wallpaper passed.');
})().catch(error => {console.error(error);process.exitCode = 1;});
