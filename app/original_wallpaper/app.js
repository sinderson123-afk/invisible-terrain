/* Original procedural landscape. All moving geometry comes from received FFTs. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), canvas=$('terrain'), ctx=canvas.getContext('2d',{alpha:false});
  const fileMode=location.protocol==='file:', base=fileMode?'http://127.0.0.1:8766':'';
  const history=new TerrainHistory(48);
  let state=null, token=null, busy=false, frozen=false, paused=false, relief=1.1;
  let pendingAudio=null;
  let pendingScanCancel=false, inFlightAction=null, editingStation=null, stationListKey=null;
  let width=0,height=0,dpr=1,hover=null,dirty=true,lastRender=0,fps=30;
  let displayedTime=0,fromTime=0,toTime=0,transitionAt=0,lastArrival=0;
  let controlVisible=!fileMode, previousSource=null, observedGeneration=null, observedSequence=-1, observedStream=null, displayFrame=null;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(fileMode) document.body.classList.add('wallpaper-mode');
  function controls(show) {
    controlVisible=show;
    $('controls').classList.toggle('hidden',!show);
    $('show-controls').textContent=show?'收起控制台 ↘':'接收控制台 ↗';
    $('show-controls').setAttribute('aria-expanded',String(show));
  }
  controls(controlVisible);
  function message(text,error=false) { $('message').textContent=text; $('message').classList.toggle('error',error); }
  function frequency() {
    const mhz=Number($('frequency').value);
    if(!Number.isFinite(mhz)||mhz<.5||mhz>6000) throw new Error('频率应在 0.5–6000 MHz 之间；实际范围取决于设备');
    return Math.round(mhz*1e6);
  }
  async function request(path,options={}) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    try {
      const response=await fetch(base+path,{cache:'no-store',signal:controller.signal,...options});
      const data=await response.json();
      if(!response.ok) throw new Error(data.error||`接收服务返回 ${response.status}`);
      return data;
    } finally { clearTimeout(timer); }
  }
  async function session() {
    if(fileMode) return;
    const data=await request('/api/session'); token=data.token;
    if(!state && data.frequency_hz) $('frequency').value=(data.frequency_hz/1e6).toFixed(3);
  }
  async function command(action,extra={}) {
    if(busy||fileMode) return false;
    if(isScanning()&&['start','tune'].includes(action)) {message('请先取消扫描，再调谐其他频率。');return false;}
    busy=true;inFlightAction=action;updateButtons();
    try {
      if(!token) await session();
      await request('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-SDR-Token':token},body:JSON.stringify({action,...extra})});
      if(action==='audio' && audioState()) {
        // Reflect accepted controls immediately; only polling may confirm actual playback.
        Object.assign(state.audio,extra);
        if(extra.enabled===false)state.audio.status='disabled';
        else if(extra.enabled===true)state.audio.status='starting';
        renderAudio();
      }
      if(action==='scan'&&stationState())state.scan.status='scanning';
      message(({start:'正在打开接收器…',stop:'正在停止采样；保留最后的地形。',tune:'正在重新调谐；新频段将重新记录。',wallpaper:'已发送桌面切换指令。请露出桌面查看。',restore:'已请求恢复之前的壁纸。',audio:extra.enabled===false?(isScanning()?'已关闭声音；扫描继续。':'已关闭声音；地形继续记录。'):'已更新本机 FM 收听设置。',scan:'正在扫描 FM 广播候选；声音与地形记录暂时暂停。',scan_cancel:'已请求取消扫描，正在返回原频率。',station_save:'已保存到本机收藏。',station_remove:'已移除此条收藏。'})[action]||'操作完成');
      return true;
    } catch(error) { token=null; message(error.message,true);return false; }
    finally {
      busy=false;inFlightAction=null;updateButtons();
      if(pendingScanCancel){pendingScanCancel=false;command('scan_cancel');}
      // A stop clicked during tuning must not be silently discarded by the busy guard.
      else if(pendingAudio){const extra=pendingAudio;pendingAudio=null;command('audio',extra);}
    }
  }
  function audioState() {
    const audio=state&&state.audio;
    return audio&&typeof audio==='object'&&typeof audio.enabled==='boolean'?audio:null;
  }
  function canListen() {
    return state&&state.status==='live'&&!state.demo&&state.frequency_hz>=87500000&&state.frequency_hz<=108000000;
  }
  function audioCommand(extra) {
    if(fileMode||!audioState())return;
    if(busy){pendingAudio={...pendingAudio,...extra};updateButtons();return;}
    command('audio',extra);
  }
  function renderAudio() {
    const audio=audioState(), summary=$('audio-state'), note=$('audio-note');
    if(!audio) {
      summary.textContent=state?'音频服务待更新':'音频状态未知';
      note.textContent=state?'当前接收服务尚不支持声音，请更新并重启本机接收服务。':'连接本机接收服务后可控制声音；当前无法确认播放状态。';
      note.classList.remove('error');
      summary.removeAttribute('title');
      $('audio-volume').value='15';
      $('audio-volume-value').textContent='—';
      return;
    }
    const volume=Number.isFinite(audio.volume)?Math.round(Math.min(1,Math.max(0,audio.volume))*100):15;
    if(document.activeElement!==$('audio-volume')&&!busy) {
      $('audio-volume').value=String(volume);
      $('audio-volume-value').textContent=volume+'%';
    }
    const labels={off:'收听关闭',disabled:'收听关闭',waiting:'等待采样',demo:'演示不播放',unsupported:'当前模式不支持',starting:'声音准备中',buffering:'声音缓冲中',playing:'单声道播放',muted:'已静音',error:'音频异常',closed:'声音已关闭'};
    const quiet=audio.muted||volume===0;
    summary.textContent=!audio.enabled?(audio.status==='closed'?labels.closed:'收听关闭'):quiet&&['playing','muted'].includes(audio.status)?'已静音':(labels[audio.status]||'等待声音状态');
    if(audio.enabled&&audio.status==='playing'&&!quiet)summary.textContent+=' · '+volume+'%';
    const error=typeof audio.error==='string'?audio.error:'';
    summary.title=error;
    note.classList.toggle('error',!!error);
    if(error)note.textContent='声音已暂停：'+error+'。地形接收不受影响，可停止收听后重试。';
    else if(state.demo)note.textContent='演示信号不播放声音。请选择真实 SDR，并调到 87.5–108 MHz 的 FM 广播。';
    else if(state.frequency_hz<87500000||state.frequency_hz>108000000)note.textContent='收听仅支持 87.5–108 MHz 的普通 FM 广播；调回广播频段后再收听。';
    else if(isScanning())note.textContent='扫描期间暂停声音；结束后按原收听状态恢复。也可停止收听，结束后便不再出声。';
    else if(state.status!=='live')note.textContent='请先启动真实 SDR 接收，再开启收听。开机默认静音。';
    else note.textContent='普通 FM 广播 87.5–108 MHz · 单声道 · 开机静音；调台时声音短暂淡出。';
  }
  function updateButtons() {
    const scanning=isScanning(),running=state && ['starting','live','scanning','stopping'].includes(state.status);
    $('start').textContent=running?'停止接收':'启动接收';
    $('start').classList.toggle('danger',!!running);
    $('start').disabled=busy||!state||state.status==='stopping';
    $('mode').disabled=busy||!!running;
    for(const id of ['tune','down','up','wallpaper','restore']) $(id).disabled=busy||!state;
    for(const id of ['tune','down','up','frequency']) $(id).disabled=fileMode||busy||!state||scanning;
    const audio=audioState(), enabled=!!(audio&&audio.enabled), stopping=!!(pendingAudio&&pendingAudio.enabled===false);
    $('audio-toggle').textContent=stopping?'等待停止收听…':enabled?'停止收听':'开启收听';
    $('audio-toggle').setAttribute('aria-pressed',String(enabled));
    $('audio-toggle').classList.toggle('danger',enabled);
    $('audio-toggle').disabled=fileMode||!audio||stopping||(!enabled&&(busy||!canListen()));
    $('audio-volume').disabled=fileMode||!audio||busy;
    $('audio-mute').disabled=fileMode||!audio||busy||!enabled;
    $('audio-mute').textContent=audio&&audio.muted?'取消静音':'静音';
    $('audio-mute').setAttribute('aria-pressed',String(!!(audio&&audio.muted)));
    updateStationButtons();
  }
  function stationState() {
    return state&&state.radio&&Array.isArray(state.radio.favorites)&&state.scan&&Array.isArray(state.scan.candidates)?state.radio:null;
  }
  function isScanning() {
    return inFlightAction==='scan'||!!(state&&(state.status==='scanning'||state.scan&&['scanning','restoring'].includes(state.scan.status)));
  }
  function fmFrequency(hz) {return Number.isInteger(hz)&&hz>=87500000&&hz<=108000000;}
  function stationFrequency(hz) {return (hz/1e6).toFixed(3)+' MHz';}
  function updateStationButtons() {
    const radio=stationState(),available=!!radio&&!fileMode,scanning=isScanning();
    $('station-directory').hidden=fileMode;
    $('scan-start').disabled=!available||busy||scanning||state.status!=='live'||!!state.demo;
    $('scan-cancel').disabled=!available||!scanning||pendingScanCancel;
    $('scan-cancel').textContent=pendingScanCancel?'等待取消…':'取消扫描';
    for(const id of ['station-search','station-sort','station-only-favorites','station-name'])$(id).disabled=!available;
    const target=editingStation?editingStation.frequency_hz:state&&state.frequency_hz;
    $('station-save').disabled=!available||busy||!fmFrequency(target)||(!editingStation&&!!state.demo)||scanning;
    $('station-edit-cancel').disabled=!available||busy;
    $('station-edit-cancel').hidden=!editingStation;
    $('station-save').textContent=editingStation?'保存名称':'收藏当前频率';
    $('station-edit-label').textContent=(editingStation?'编辑收藏 · ':'收藏当前频率 · ')+(fmFrequency(target)?stationFrequency(target):'仅限 FM 广播');
    renderStationList();
  }
  function stationEntries() {
    const radio=stationState();if(!radio)return [];
    const entries=new Map();
    for(const candidate of state.scan.candidates) {
      if(!candidate||!fmFrequency(candidate.frequency_hz))continue;
      entries.set(candidate.frequency_hz,{frequency_hz:candidate.frequency_hz,score_db:Number.isFinite(candidate.score_db)?candidate.score_db:null,bandwidth_hz:Number.isFinite(candidate.bandwidth_hz)?candidate.bandwidth_hz:null,favorite:null});
    }
    for(const favorite of radio.favorites) {
      if(!favorite||!fmFrequency(favorite.frequency_hz)||typeof favorite.id!=='string')continue;
      const entry=entries.get(favorite.frequency_hz)||{frequency_hz:favorite.frequency_hz,score_db:null,bandwidth_hz:null,favorite:null};
      entry.favorite={id:favorite.id,name:typeof favorite.name==='string'?favorite.name:''};entries.set(favorite.frequency_hz,entry);
    }
    const query=$('station-search').value.trim().toLocaleLowerCase();
    const entriesFiltered=[...entries.values()].filter(entry=>(!$('station-only-favorites').checked||entry.favorite)&&(!query||(entry.favorite&&entry.favorite.name.toLocaleLowerCase().includes(query))||stationFrequency(entry.frequency_hz).toLocaleLowerCase().includes(query)));
    entriesFiltered.sort($('station-sort').value==='signal'?(a,b)=>(b.score_db??-Infinity)-(a.score_db??-Infinity)||a.frequency_hz-b.frequency_hz:(a,b)=>a.frequency_hz-b.frequency_hz);
    return entriesFiltered;
  }
  function beginStationEdit(entry) {
    if(fileMode||!stationState()||busy||isScanning())return;
    editingStation={frequency_hz:entry.frequency_hz};
    $('station-name').value=entry.favorite?entry.favorite.name:'';
    updateStationButtons();$('station-name').focus();
  }
  function renderStationList() {
    const radio=stationState(),entries=stationEntries(),disabled=fileMode||!radio||busy||isScanning();
    const key=JSON.stringify([entries,disabled,state&&state.frequency_hz,!!radio,$('station-search').value,$('station-only-favorites').checked]);
    if(key===stationListKey)return;stationListKey=key;
    const nodes=[];
    for(const entry of entries) {
      const row=document.createElement('div');row.className='station-row'+(state.frequency_hz===entry.frequency_hz?' current':'');
      const info=document.createElement('div');info.className='station-info';
      const title=document.createElement('span');title.className='station-title';
      title.textContent=(entry.favorite?'★ ':'')+(entry.favorite&&entry.favorite.name?entry.favorite.name:'广播候选');
      const meta=document.createElement('span');meta.className='station-meta';
      meta.textContent=stationFrequency(entry.frequency_hz)+(entry.score_db===null?' · 未扫描验证':' · 相对强度 '+entry.score_db.toFixed(1)+' dB')+(entry.bandwidth_hz===null?'':' · 宽约 '+Math.round(entry.bandwidth_hz/1000)+' kHz');
      info.append(title,meta);
      const actions=document.createElement('div');actions.className='station-actions';
      function button(label,handler) {const node=document.createElement('button');node.type='button';node.textContent=label;node.disabled=disabled;node.addEventListener('click',()=>{if(!node.disabled)handler();});actions.append(node);}
      button('调到这里',()=>{if(!isScanning()){$('frequency').value=(entry.frequency_hz/1e6).toFixed(3);command('tune',{frequency_hz:entry.frequency_hz});}});
      button(entry.favorite?'改名':'收藏',()=>beginStationEdit(entry));
      if(entry.favorite)button('删除',()=>command('station_remove',{id:entry.favorite.id}));
      row.append(info,actions);nodes.push(row);
    }
    if(!nodes.length) {
      const empty=document.createElement('p');empty.className='station-empty';
      empty.textContent=!radio?'连接新版接收服务后显示目录。':$('station-search').value||$('station-only-favorites').checked?'没有匹配项。试试其他名称或频率，或关闭“仅收藏”。':'还没有目录条目。可收藏当前频率，或主动扫描 FM 广播候选。';nodes.push(empty);
    }
    $('station-list').replaceChildren(...nodes);
    $('station-count').textContent=radio?entries.length+' 项 · 收藏 '+radio.favorites.length+' 项':'搜台与收藏';
  }
  function renderStations() {
    const radio=stationState(),note=$('station-service-note');
    if(!radio) {
      note.textContent=fileMode?'桌面只读；请在本机控制台管理电台。':state?'电台目录服务待更新：请重启新版接收后台。原有调频与收听仍可使用。':'本机接收服务未连接；暂时无法读取收藏或扫描状态。';
      note.classList.remove('error');$('scan-status').classList.remove('error');$('scan-status').textContent='扫描状态未知';$('scan-progress').value=0;return;
    }
    note.classList.toggle('error',!!radio.error);
    note.textContent=radio.error?'电台记忆异常：'+radio.error:'收藏与自定义名称仅供本机控制台使用；点击“调到这里”不会自动开启声音。';
    const scan=state.scan,progress=Number.isFinite(scan.progress)?Math.max(0,Math.min(1,scan.progress)):0;
    $('scan-progress').value=progress;
    const labels={idle:'尚未扫描',scanning:'扫描中 · '+Math.round(progress*100)+'%',restoring:'正在返回原频率',complete:'扫描完成 · '+scan.candidates.length+' 个候选',cancelled:'扫描已取消',error:'扫描失败'};
    $('scan-status').textContent=scan.error?labels.error+'：'+scan.error:(labels[scan.status]||'等待扫描状态')+(scan.status==='scanning'&&fmFrequency(scan.current_hz)?' · '+stationFrequency(scan.current_hz):'');
    $('scan-status').classList.toggle('error',!!scan.error);
    $('station-memory-note').textContent='频率、音量与收藏保存在本机；开机不会自动出声。'+(fmFrequency(radio.last_frequency_hz)?' 已记住 '+stationFrequency(radio.last_frequency_hz)+'。':'');
  }
  function renderState(next) {
    const previousStatus=state&&state.status;
    state=next;
    const live=next.status==='live';
    if(live) $('mode').value=next.demo?'demo':'real';
    const stale=live && performance.now()-lastArrival>2500;
    $('status-dot').className='dot '+(next.status==='error'?'error':live?(next.demo?'demo':'live'):'');
    let statusText=({idle:'已停止 · 地形留存',starting:'正在连接接收器',live:next.demo?'演示信号 · 非真实电波':'真实 SDR · 正在记录',scanning:'正在扫描 FM · 地形记录暂停',stopping:'正在释放接收器',error:'接收中断 · 地形已停留'})[next.status]||'等待接收';
    if(stale)statusText='暂无新采样 · 地形已停留';
    const receiver=next.receiver;
    if(receiver&&['rtl','soapy','demo'].includes(receiver.backend)&&Number.isFinite(receiver.sample_rate_hz)) {
      statusText+=' · '+({rtl:'RTL-SDR',soapy:'SoapySDR',demo:'DEMO'})[receiver.backend]+' / '+(receiver.sample_rate_hz/1e6).toFixed(3)+' MS/s';
    }
    $('status').textContent=statusText+(frozen?' · 画面定格':'');
    const f=frozen?displayFrame:next.frame;
    const center=f?f.frequency_hz:next.frequency_hz;
    if(Number.isFinite(center)) $('center').textContent=(center/1e6).toFixed(3);
    if(document.activeElement!==$('frequency') && !busy && Number.isFinite(next.frequency_hz)) $('frequency').value=(next.frequency_hz/1e6).toFixed(3);
    if(f) {
      $('peak').textContent=f.peak_hz==null?'未发现明显峰':`${(f.peak_hz/1e6).toFixed(3)} MHz`;
      $('bandwidth').textContent=((f.high_hz-f.low_hz)/1e6).toFixed(3)+' MHz';
    } else $('peak').textContent='—';
    $('history').textContent=history.duration.toFixed(1)+' s / 48 s';
    $('source-note').textContent=history.rows.length?(previousSource?'演示信号 · 合成数据':'真实采样 · 相对强度，非校准场强'):'未接收 · 不生成模拟地形';
    $('empty').classList.toggle('hidden',history.rows.length>0);
    if(next.error) message(next.error,true);
    else if(next.status==='live'&&previousStatus!=='live')message(next.demo?'演示中：当前为明确标注的合成信号。':'真实接收中。窄峰是集中信号，长脊是持续发射；只有新采样才留下记录。');
    if(!next.error&&next.status==='live'&&previousStatus!=='live'&&receiver&&Array.isArray(receiver.warnings)&&receiver.warnings.length) {
      message('接收提示：'+receiver.warnings.join('；'));
    }
    const wp=next.wallpaper;
    if(wp) $('desktop-state').textContent=wp.is_original?'原创地形已用于桌面':(wp.recoverable?'可恢复上次桌面':'原创实时绘制 · 无外部素材');
    renderAudio();
    renderStations();
    updateButtons();
  }
  async function poll() {
    try {
      const next=await request('/api/state');
      if(observedGeneration!==next.generation || observedStream!==next.stream_id) {
        history.clear();displayedTime=fromTime=toTime=0;hover=null;
        displayFrame=null;observedStream=next.stream_id;
        observedGeneration=next.generation;observedSequence=-1;dirty=true;
      }
      if(next.frame&&next.sequence!==observedSequence) {
        lastArrival=performance.now();observedSequence=next.sequence;
      }
      if(!frozen && next.status==='live' && history.ingest(next)) {
        displayFrame=next.frame;
        previousSource=!!next.demo;
        const now=performance.now();
        lastArrival=now;
        fromTime=history.rows.length===1?history.latest:displayedTime;
        toTime=history.latest;
        transitionAt=now;
        dirty=true;
      }
      renderState(next);
    } catch(error) {
      state=null;token=null;renderAudio();renderStations();updateButtons();
      $('status-dot').className='dot error';
      $('status').textContent='本机服务未连接 · 地形停留';
      message('请运行 start.cmd 或 app/original_bridge.py，并保持服务运行。'+(error.name==='AbortError'?' 连接超时。':''),true);
    } finally {setTimeout(poll,paused?1000:200);}
  }
  function resize() {
    width=innerWidth;height=innerHeight;dpr=Math.min(devicePixelRatio||1,1.5);
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
    ctx.setTransform(dpr,0,0,dpr,0,0);dirty=true;
  }
  function point(u,age,level=0) {
    const depth=Math.max(0,Math.min(1,age/48)),scale=1-.58*depth;
    return {x:width*.49+(u-.5)*width*.86*scale+width*.095*depth,
      y:height*.805-depth*height*.38-Math.pow(Math.max(0,level),1.15)*height*.315*scale*relief};
  }
  function traceRow(row,now) {
    const age=Math.max(0,now-row.time);ctx.beginPath();
    for(let i=0;i<row.levels.length;i++) {
      const p=point((i+.5)/row.levels.length,age,row.levels[i]);
      if(i===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);
    }
  }
  function draw(now) {
    ctx.fillStyle='#091514';ctx.fillRect(0,0,width,height);
    const light=ctx.createRadialGradient(width*.55,height*.55,0,width*.55,height*.55,width*.64);
    light.addColorStop(0,'#193327');light.addColorStop(.65,'#10241e');light.addColorStop(1,'#091514');
    ctx.fillStyle=light;ctx.fillRect(0,0,width,height);
    // Ground reference: straight frequency lines, never synthetic mountains.
    ctx.lineWidth=.6;ctx.strokeStyle='#33533d50';
    for(let i=0;i<=8;i++) {
      let a=point(i/8,0),b=point(i/8,48);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
    for(let age=0;age<=48;age+=8) {
      let a=point(0,age),b=point(1,age);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      ctx.fillStyle='#66846c';ctx.font='9px Consolas, monospace';ctx.textAlign='left';
      ctx.fillText(age?`−${age}s`:'NOW',b.x+9,b.y+3);
    }
    const rows=history.rows;
    // Oldest first: near silhouettes occlude older slopes naturally.
    for(let r=0;r<rows.length;r++) {
      const row=rows[r],age=Math.max(0,now-row.time);
      if(age>48)continue;
      const fade=1-age/48;
      traceRow(row,now);
      const end=point(1,age),start=point(0,age);
      ctx.lineTo(end.x,end.y);ctx.lineTo(start.x,start.y);ctx.closePath();
      ctx.fillStyle=`rgba(10,28,21,${.73+.15*fade})`;ctx.fill();
      traceRow(row,now);
      ctx.strokeStyle=`rgba(${Math.round(93+fade*71)},${Math.round(127+fade*38)},${Math.round(99+fade*14)},${.25+.42*fade})`;
      ctx.lineWidth=r===rows.length-1?1.45:.65;ctx.stroke();
      // Warm tips indicate actual strong spectral bins, not random sparkles.
      ctx.beginPath();let active=false;
      for(let i=0;i<row.levels.length;i++) {
        const p=point((i+.5)/row.levels.length,age,row.levels[i]);
        if(row.levels[i]>.30 || (i>0&&row.levels[i-1]>.30)) {
          if(!active && i>0){const prev=point((i-.5)/row.levels.length,age,row.levels[i-1]);ctx.moveTo(prev.x,prev.y);}
          else if(!active)ctx.moveTo(p.x,p.y);
          ctx.lineTo(p.x,p.y);active=true;
        }else active=false;
      }
      ctx.lineWidth=r===rows.length-1?1.55:.8;
      ctx.strokeStyle=`rgba(218,183,117,${.3+.42*fade})`;ctx.stroke();
    }
    if(rows.length) {
      const latest=rows[rows.length-1];ctx.font='10px Consolas,monospace';ctx.textAlign='center';
      for(let i=0;i<=4;i++) {
        const p=point(i/4,0);ctx.fillStyle='#8fa488';
        ctx.fillText(((latest.low+(latest.high-latest.low)*i/4)/1e6).toFixed(3),p.x,p.y+21);
      }
      if(hover) {
        const u=Math.min(1,Math.max(0,(hover.x-width*.49)/(width*.86)+.5));
        const i=Math.min(latest.levels.length-1,Math.floor(u*latest.levels.length));
        const p=point(u,0,latest.levels[i]),b=point(u,0);
        ctx.strokeStyle='#d8c28688';ctx.setLineDash([3,5]);ctx.lineWidth=.8;
        ctx.beginPath();ctx.moveTo(p.x,p.y-18);ctx.lineTo(b.x,b.y+5);ctx.stroke();ctx.setLineDash([]);
        ctx.beginPath();ctx.arc(p.x,p.y,3,0,Math.PI*2);ctx.fillStyle='#dec589';ctx.fill();
        $('hover-readout').hidden=false;
        $('hover-readout').textContent=((latest.low+u*(latest.high-latest.low))/1e6).toFixed(4)+' MHz · 相对山高 '+Math.round(latest.levels[i]*100)+'%';
      } else $('hover-readout').hidden=true;
    }
  }
  function animate(now) {
    requestAnimationFrame(animate);
    if(paused||document.hidden||now-lastRender<1000/fps)return;
    const fraction=reduced?1:Math.min(1,(now-transitionAt)/240);
    const t=fromTime+(toTime-fromTime)*(1-Math.pow(1-fraction,3));
    if(!dirty&&Math.abs(t-displayedTime)<.001)return;
    displayedTime=t;lastRender=now;dirty=false;draw(displayedTime);
  }
  $('show-controls').addEventListener('click',()=>controls(!controlVisible));
  $('start').addEventListener('click',()=>{
    try {
      if(state&&(['live','starting','scanning'].includes(state.status)||isScanning()))command('stop');
      else {frozen=false;$('freeze').textContent='定格画面';command('start',{frequency_hz:frequency(),demo:$('mode').value==='demo'});}
    }catch(error){message(error.message,true);}
  });
  function tune(delta=0){if(isScanning())return;try{const hz=frequency()+delta; if(hz<500000||hz>6000000000)throw new Error('频率超出应用范围');$('frequency').value=(hz/1e6).toFixed(3);command('tune',{frequency_hz:hz});}catch(error){message(error.message,true);}}
  $('tune').addEventListener('click',()=>tune());$('up').addEventListener('click',()=>tune(100000));$('down').addEventListener('click',()=>tune(-100000));
  $('frequency').addEventListener('keydown',e=>{if(e.key==='Enter')tune();});
  $('relief').addEventListener('input',e=>{relief=Number(e.target.value);dirty=true;});
  $('freeze').addEventListener('click',()=>{frozen=!frozen;$('freeze').textContent=frozen?'继续画面':'定格画面';dirty=true;if(state)renderState(state);});
  $('wallpaper').addEventListener('click',()=>command('wallpaper'));
  $('restore').addEventListener('click',()=>command('restore'));
  $('audio-toggle').addEventListener('click',()=>{
    const audio=audioState();
    if(audio&&(audio.enabled||canListen()))audioCommand(audio.enabled?{enabled:false}:{enabled:true,muted:false});
  });
  $('audio-mute').addEventListener('click',()=>{
    const audio=audioState();if(audio&&audio.enabled)audioCommand({muted:!audio.muted});
  });
  $('audio-volume').addEventListener('input',()=>{
    $('audio-volume-value').textContent=Math.round(Number($('audio-volume').value))+'%';
  });
  // Commit once on release / keyboard change, never once per input event or poll.
  $('audio-volume').addEventListener('change',()=>{
    const volume=Number($('audio-volume').value)/100;
    if(Number.isFinite(volume))audioCommand({volume:Math.min(1,Math.max(0,volume))});
  });
  $('scan-start').addEventListener('click',()=>{if(!$('scan-start').disabled)command('scan');});
  $('scan-cancel').addEventListener('click',()=>{
    if(fileMode||!stationState()||!isScanning()||pendingScanCancel)return;
    if(busy){pendingScanCancel=true;updateButtons();return;}command('scan_cancel');
  });
  for(const id of ['station-search','station-sort','station-only-favorites'])$(id).addEventListener(id==='station-search'?'input':'change',renderStationList);
  $('station-save').addEventListener('click',async()=>{
    if($('station-save').disabled)return;
    const target=editingStation?editingStation.frequency_hz:state.frequency_hz,name=$('station-name').value.trim(),edit=editingStation;
    if(await command('station_save',{frequency_hz:target,name})) {
      if(editingStation===edit&&$('station-name').value.trim()===name){editingStation=null;$('station-name').value='';updateStationButtons();}
    }
  });
  $('station-name').addEventListener('keydown',e=>{if(e.key==='Enter'&&!$('station-save').disabled)$('station-save').click();});
  $('station-edit-cancel').addEventListener('click',()=>{editingStation=null;$('station-name').value='';updateStationButtons();});
  canvas.addEventListener('pointermove',e=>{hover={x:e.clientX,y:e.clientY};dirty=true;});
  canvas.addEventListener('pointerleave',()=>{hover=null;dirty=true;});
  addEventListener('resize',resize);
  document.addEventListener('visibilitychange',()=>{dirty=true;});
  // Assigned synchronously so Wallpaper Engine's first property event is retained.
  window.wallpaperPropertyListener={
    applyUserProperties(p){if(p.relief){relief=Math.min(1.8,Math.max(.5,Number(p.relief.value)||1.1));$('relief').value=relief;}if(p.dimhud)document.body.classList.toggle('dim-hud',!!p.dimhud.value);dirty=true;},
    applyGeneralProperties(p){if(p.fps)fps=Math.max(1,Math.min(30,Number(p.fps)||30));},
    setPaused(value){paused=!!value;if(!paused){history.clear();dirty=true;}}
  };
  resize();updateButtons();requestAnimationFrame(animate);poll();
})();
