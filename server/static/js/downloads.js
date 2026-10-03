// MediaAuto 前端 — downloads.js
// 管理页「下载」子页签: qBittorrent 配置/连接测试 + 任务进度 + 按作品聚合"下到第几集"。
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载。
// 自动刷新: 有"下载中"任务时 5 秒轮询 /api/qbit/torrents(纯读取, 轻量)。

let _dlTimer = null;        // 自动刷新定时器
let _dlConfigured = false;  // qbit 是否已配置
let _dlData = null;         // 最近一次 /api/qbit/torrents 的返回

function _dlStopTimer(){ if(_dlTimer){ clearInterval(_dlTimer); _dlTimer = null; } }
// ms 缺省 5000(有下载中的任务时勤刷); 出错时用更长间隔(15000)慢慢重试, 别把坏配置打成高频请求。
// ⚠️ 这里刻意不判 _dlConfigured: 读配置本身失败时 _dlConfigured 可能是 false,
//    判了就会导致"永远不重试"(用户 2026-09-21 反馈的错误赖着不走)。
function _dlStartTimer(ms){
  _dlStopTimer();
  _dlTimer = setInterval(()=>{
    // 仅当管理页仍在「下载」子页签时轮询; 后台标签页不白打接口(回前台时 visibilitychange 立即补一次)
    if(MANAGE_SUB !== 'download' || document.hidden) return;
    loadDownloads(true);   // silent=true: 不闪骨架
  }, ms || 5000);
}
// 切走下载子页签时停止轮询(由 settings.js switchManageSub 调用)
window._dlOnSubChange = (sub)=>{ if(sub !== 'download') _dlStopTimer(); };

// 后台标签页不轮询(见上); 切回前台立刻刷一次, 不用等下个 tick
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden || MANAGE_SUB !== 'download') return;
  if(_dlTimer) loadDownloads(true);
});

// 速度格式化
function _fmtSpeed(bps){
  if(bps==null) return '—';
  bps = +bps;
  if(bps < 1024) return bps + ' B/s';
  if(bps < 1024*1024) return (bps/1024).toFixed(1) + ' KB/s';
  return (bps/1024/1024).toFixed(2) + ' MB/s';
}

// ---- 进入下载子页签 ----
async function loadDownloads(silent){
  const el = $('#manageBody');
  if(!silent){
    el.innerHTML = `<div class="downloads"><div class="empty"><span class="spin"></span>加载下载任务…</div></div>`;
  }
  let cfg, data;
  try{ cfg = await api('/api/qbit/config'); }
  catch(e){
    // 读配置失败也要保留重试入口 + 后台自动重试, 不能把整页清成一句错误
    // (那样只能切页签才能恢复 —— 用户 2026-09-21 反馈)。
    _dlStartTimer(15000);
    el.innerHTML = `<div class="downloads"><div class="dl-warn">${icon('alert')}
      <span class="dl-warn-txt">读取 qBittorrent 配置失败: ${esc(e.message)}</span>
      <button class="ghost sm" onclick="loadDownloads()">${icon('refresh')}重试</button></div></div>`;
    return;
  }
  _dlConfigured = !!cfg.configured;
  if(!_dlConfigured){
    _dlStopTimer();
    _dlRender(el, cfg, null);
    return;
  }
  try{ data = await api('/api/qbit/torrents?limit=200'); }
  catch(e){ data = {ok:false, torrents:[], shows:[], transfer:{}, msg:e.message}; }
  _dlData = data;
  _dlRender(el, cfg, data);
  // 有下载中任务 → 起自动刷新; **出错也继续慢速重试**, 否则一次瞬时报错就永久停在错误上
  const active = (data.torrents||[]).some(t=>['downloading','stalledDownloading','checkingDownloading','allocating','metaDL','queuedDownloading','forcedDownloading'].includes(t.state));
  if(active) _dlStartTimer(5000);
  else if(!data.ok) _dlStartTimer(15000);
  else _dlStopTimer();
}

// 错误横幅上的「重试」按钮: 立即重跑一次(loadDownloads 内部会按结果重设轮询间隔)
function dlRetry(){ loadDownloads(); }

// ---- 渲染(错误条 + 配置表单 + 统计 + 作品聚合 + 任务明细) ----
// ⚠️ 局部更新: 轮询是 5s 一次, 旧写法每次都 innerHTML 整块重建 —— 用户正在表单里
// 输入的地址/账号/密码每 5 秒被抹掉一次、光标被弹走。现在:
//   · 配置没变 → 只换错误条和任务区(#dlLive), 表单 DOM 原样保留;
//   · 配置变了(保存后 cfg 不同)或整页被清过 → 才整块重画。
let _dlFormKey = null;   // 上次渲染表单时的 cfg 指纹
function _dlRender(el, cfg, data){
  const err = (data && !data.ok && data.msg)
    ? `<div class="dl-warn">${icon('alert')}<span class="dl-warn-txt">${esc(data.msg)}</span>
        <button class="ghost sm" onclick="dlRetry()">${icon('refresh')}重试</button></div>`
    : '';
  const live = (data && data.ok)
    ? `${_dlStats(data.transfer, data.torrents)}${_dlShows(data.shows)}${_dlTorrentList(data.torrents)}`
    : '';
  const key = JSON.stringify(cfg || {});
  const wrap = el.querySelector('.downloads');
  if(wrap && key === _dlFormKey){
    const slot = wrap.querySelector('.dl-errslot');
    if(slot) slot.innerHTML = err;
    const box = wrap.querySelector('#dlLive');
    if(box) box.innerHTML = live;
    return;
  }
  _dlFormKey = key;
  el.innerHTML = `
    <div class="downloads">
      <div class="dl-errslot">${err}</div>
      ${_dlConfigForm(cfg)}
      <div id="dlLive">${live}</div>
    </div>`;
}

// ---- qbit 配置表单(管理页顶部, 未配置时最显眼) ----
function _dlConfigForm(cfg){
  const pwVal = cfg.password_set ? '••••' : '';
  return `<div class="dl-cfg">
    <h3 class="set-h">${icon('download')}QBitTorrent 下载器</h3>
    <p class="dl-note">配置 WebUI 地址与账号后即可「推送 Qbit」, 下方实时显示下载任务与进度。
      推送的剧集会自动打标, 按作品聚合出"下到第几集"。保存后热加载生效, 无需重启。</p>
    <div class="dl-cfg-grid">
      <label class="dl-field"><span>WebUI 地址</span><input id="dlUrl" type="text" value="${esc(cfg.url)}" placeholder="http://192.168.1.200:8080" autocomplete="off"></label>
      <label class="dl-field"><span>账号</span><input id="dlUser" type="text" value="${esc(cfg.username)}" placeholder="admin" autocomplete="off"></label>
      <label class="dl-field"><span>密码</span><input id="dlPw" type="password" value="${esc(pwVal)}" placeholder="${cfg.password_set?'已设置, 留空保持不变':'WebUI 密码'}" autocomplete="new-password"></label>
      <label class="dl-field"><span>默认保存路径 <em>(可选)</em></span><input id="dlSave" type="text" value="${esc(cfg.save_path||'')}" placeholder="留空 = qBit 默认" autocomplete="off"></label>
      <label class="dl-field"><span>默认分类 <em>(可选)</em></span><input id="dlCat" type="text" value="${esc(cfg.category||'')}" placeholder="留空 = 默认分类" autocomplete="off"></label>
    </div>
    <div class="dl-cfg-bar">
      <button class="ghost sm" onclick="dlTest()">${icon('refresh')}连接测试</button>
      <button class="mbtn sm" id="dlSaveBtn" onclick="dlSave()">${icon('check')}保存配置</button>
      <span id="dlTestNote" class="dl-testnote"></span>
    </div>
  </div>`;
}

// ---- 连接测试 ----
async function dlTest(){
  const note = $('#dlTestNote'); if(!note) return;
  note.innerHTML = `<span class="spin"></span> 测试中…`;
  try{
    // 若用户刚填了新地址/账号(还没保存), 先用当前输入做测试: 临时保存再测
    await _dlEnsureConfig();
    const r = await api('/api/qbit/status');
    if(r.ok){
      note.innerHTML = `<span class="ok">${icon('check')}已连接 ${esc(r.version||'')}</span>`;
      note.className = 'dl-testnote ok';
    } else {
      note.innerHTML = `<span class="err">${icon('alert')}${esc(r.msg||'连接失败')}</span>`;
      note.className = 'dl-testnote err';
    }
  }catch(e){
    note.innerHTML = `<span class="err">${icon('alert')}${esc(e.message)}</span>`;
    note.className = 'dl-testnote err';
  }
}

// ---- 保存配置 ----
async function dlSave(){
  const btn = $('#dlSaveBtn'); if(btn) btn.disabled = true;
  const note = $('#dlTestNote');
  const body = {
    url: $('#dlUrl').value.trim(),
    username: $('#dlUser').value.trim(),
    password: $('#dlPw').value,   // 空 = 不改动(后端判定)
    save_path: $('#dlSave').value.trim(),
    category: $('#dlCat').value.trim(),
  };
  try{
    const r = await api('/api/qbit/config', {method:'PUT', body: JSON.stringify(body)});
    toast(r.msg || '已保存');
    if(note){ note.innerHTML = `<span class="ok">${icon('check')}已保存</span>`; note.className = 'dl-testnote ok'; }
    await loadDownloads(false);   // 刷新整页(配置变了)
  }catch(e){
    toast('保存失败: ' + e.message);
    if(note){ note.innerHTML = `<span class="err">${esc(e.message)}</span>`; note.className = 'dl-testnote err'; }
    if(btn) btn.disabled = false;
  }
}

// 连接测试前: 若表单里填了地址/账号但和已保存的不一致, 先保存(否则测的还是旧配置)
async function _dlEnsureConfig(){
  const url = $('#dlUrl') && $('#dlUrl').value.trim();
  const user = $('#dlUser') && $('#dlUser').value.trim();
  const pw = $('#dlPw') && $('#dlPw').value;
  const save = $('#dlSave') && $('#dlSave').value.trim();
  const cat = $('#dlCat') && $('#dlCat').value.trim();
  if(!url || !user) return;
  // 有非空密码才更新(空 = 保持)
  await api('/api/qbit/config', {method:'PUT', body: JSON.stringify({
    url, username: user, password: pw, save_path: save, category: cat
  })}).catch(()=>{});
}

// ---- 全局统计条 ----
function _dlStats(transfer, torrents){
  transfer = transfer || {};
  torrents = torrents || [];
  const downloading = torrents.filter(t=>['downloading','stalledDownloading','checkingDownloading','allocating','metaDL','queuedDownloading','forcedDownloading'].includes(t.state));
  const seeding = torrents.filter(t=>['uploading','stalledUploading','queuedUploading','forcedUploading'].includes(t.state));
  const dl = _fmtSpeed(transfer.download_payload_rate);
  const up = _fmtSpeed(transfer.upload_payload_rate);
  const stats = [
    ['下载', dl, 'brand'], ['上传', up, ''],
    ['下载中', String(downloading.length), downloading.length?'warn':''],
    ['做种中', String(seeding.length), ''],
    ['总任务', String(torrents.length), ''],
  ];
  return `<div class="dl-stats">${stats.map(([k,v,c])=>`
    <div class="dl-stat${c?' '+c:''}"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('')}</div>`;
}

// ---- 按作品聚合(下到第几集) ----
function _dlShows(shows){
  if(!shows || !shows.length){
    return `<div class="dl-sec"><h3 class="set-h">${icon('tv')}作品下载进度</h3>
      <div class="empty" style="padding:12px">暂无打标的 qBit 任务 —— 从作品详情页「推送 Qbit」后, 这里会按作品聚合显示下到第几集。</div></div>`;
  }
  const rows = shows.map(s=>{
    const kindLabel = s.kind==='tv' ? '剧集' : '电影';
    const openDetail = s.tmdb_id ? `onclick="openDetailLocal('${s.kind}',${s.tmdb_id},true)"` : '';
    const eps = [];
    // 集号徽章: 做种=绿勾(已下载), 下载中=蓝(进行中)
    (s.done_eps||[]).forEach(e=>eps.push(`<span class="dl-ep done" title="第 ${e} 集已下载">E${e}</span>`));
    (s.down_eps||[]).forEach(e=>{
      if(!(s.done_eps||[]).includes(e)) eps.push(`<span class="dl-ep down" title="第 ${e} 集下载中">E${e}</span>`);
    });
    const epHtml = eps.length ? `<div class="dl-eps">${eps.join('')}</div>` : '';
    const badge = (s.downloading>0) ? `<span class="badge run">下载中 ${s.downloading}</span>` :
                  (s.seeding>0) ? `<span class="badge ok">做种 ${s.seeding}</span>` : '';
    return `<div class="dl-show" ${openDetail}>
      <div class="dl-show-head">
        <span class="dl-show-title" title="${esc(s.title||'tmdb '+s.tmdb_id)}">${esc(s.title||('tmdb '+s.tmdb_id))}</span>
        <span class="badge">${kindLabel}</span>${badge}
        ${s.max_episode?`<span class="dl-show-ep">已到 E${s.max_episode}</span>`:''}
      </div>
      ${epHtml}
      <div class="dl-show-foot">任务 ${s.torrents} 个${s.downloading?` · 下载中 ${s.downloading}`:''}${s.seeding?` · 做种 ${s.seeding}`:''}</div>
    </div>`;
  }).join('');
  return `<div class="dl-sec"><h3 class="set-h">${icon('tv')}作品下载进度 <small>(${shows.length})</small></h3>${rows}</div>`;
}

// ---- 任务明细列表 ----
function _dlTorrentList(torrents){
  if(!torrents || !torrents.length){
    return `<div class="dl-sec"><h3 class="set-h">${icon('list')}全部任务</h3>
      <div class="empty" style="padding:12px">qBit 暂无任务</div></div>`;
  }
  const rows = torrents.map(t=>{
    const p = t._progress||0;
    const barColor = t._status==='已暂停' ? 'var(--muted)' :
                     (t.state==='error'||t.state==='missingFiles') ? 'var(--err)' :
                     (t.state==='uploading'||t.state==='stalledUploading') ? 'var(--ok)' : 'var(--brand)';
    const speed = (t.state==='downloading'||t.state==='stalledDownloading')
      ? _fmtSpeed(t.dlspeed) : (t.state==='uploading'||t.state==='stalledUploading') ? _fmtSpeed(t.upspeed) : '';
    const tag = (t.tags||'');
    return `<div class="dl-torrent">
      <div class="dl-torrent-top">
        <span class="dl-torrent-name" title="${esc(t.name||'')}">${esc(t.name||'(无名称)')}</span>
        <span class="dl-torrent-status" style="color:${barColor}">${esc(t._status||t.state)}${speed?` · ${speed}`:''}</span>
      </div>
      <div class="dl-torrent-bar" title="进度 ${p}%"><i style="width:${p}%;background:${barColor}"></i></div>
      <div class="dl-torrent-meta">
        <span>${esc(fmt(t.size))}</span>
        <span>${t.num_seeds??0}/${t.num_leechs??0} 种子/下载</span>
        ${tag?`<span class="dl-torrent-tag">${esc(tag)}</span>`:''}
      </div>
    </div>`;
  }).join('');
  return `<div class="dl-sec"><h3 class="set-h">${icon('list')}全部任务 <small>(${torrents.length})</small></h3>${rows}</div>`;
}
