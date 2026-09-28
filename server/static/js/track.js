// MediaAuto 前端 — track.js
// 追踪: 演员新作/剧集新季 → 自动推磁力
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

// ===== 追踪(演员新作 / 剧集新季 → 自动推磁力) =====
// 2026-09 起作为「管理」页的一个子页签: loadTrack 渲染完整块到 #manageBody
let TRACK_CHECKING = false;
// 大小范围状态(每档: 不限=空 / 自定义=最小–最大 GB)。渲染时由后端值推导, 保存时由 UI 推导。
let TRACK_SIZE = { '4k': {min:'', max:''}, '1080': {min:'', max:''} };
// 推送筛选可选项(与后端 track_check.RESOLUTION_OPTIONS / SOURCE_OPTIONS 同口径)
const TF_RES = [['2160p','2160p (4K)'],['1080p','1080p'],['720p','720p'],['480p','480p']];
const TF_SRC = [['REMUX','REMUX'],['BLURAY','BluRay'],['WEBDL','WEB-DL'],['WEBRIP','WEBRip'],
                ['HDTV','HDTV'],['DVD','DVD']];
// 当前筛选(读设置时由后端值填, 保存时从 DOM 推导)
let TRACK_FILTER = { resolutions: [], sources: [], groups: [] };
// 一组复选(片源/分辨率): 行标题 + 勾选项
const tfRow = (label, opts, cls) => `
    <div class="tf-row">
      <span class="tier-label">${label}</span>
      <div class="tf-opts">${opts.map(([v, l]) =>
        `<label class="tf-opt"><input type="checkbox" class="${cls}" value="${v}"
           onchange="trackFilterPreview()"><span>${l}</span></label>`).join('')}</div>
    </div>`;
async function loadTrack(){
  const el = $('#manageBody');
  const tier = (key, label, cls) => `
    <div class="size-tier ${cls}">
      <span class="tier-label"><span class="dot"></span>${label}</span>
      <div class="tier-seg" id="tierSeg-${key}">
        <button data-mode="any" onclick="trackTierMode('${key}','any')">不限大小</button>
        <button data-mode="range" onclick="trackTierMode('${key}','range')">自定义范围</button>
      </div>
      <div class="tier-range" id="tierRange-${key}">
        <input id="trackMin-${key}" type="number" min="0" step="0.5" inputmode="decimal" placeholder="最小">
        <span class="sep">–</span>
        <input id="trackMax-${key}" type="number" min="0" step="0.5" inputmode="decimal" placeholder="最大">
        <span class="unit">GB</span>
      </div>
    </div>`;
  el.innerHTML = `<div class="settings">
    <label class="setting">
      <input type="checkbox" id="trackAutoPush" onchange="saveTrackSetting('auto_push', this.checked)">
      <span class="st-main">
        <span class="st-name">自动推送</span>
        <span class="st-desc">检测到新增且命中下方「推送筛选 + 大小范围」时, 自动推磁力到离线下载。默认关; 单条可在追踪列表里覆盖。</span>
      </span>
    </label>
  </div>
  <div class="sizecard">
    <div class="sc-head"><span class="sc-name">推送筛选</span><span style="font-size:11px;color:var(--muted)">全部命中才推</span></div>
    <div class="sc-desc">片源 / 分辨率 / 发布组同时命中才推。名字里认不出的一律不推(不猜); 某项不勾 = 该维度不限。</div>
    ${tfRow('分辨率', TF_RES, 'tf-res')}
    ${tfRow('片源', TF_SRC, 'tf-src')}
    <div class="tf-row">
      <span class="tier-label">发布组</span>
      <input id="trackGroups" type="text" oninput="trackFilterPreview()"
        placeholder="留空 = 不限, 多个用逗号分隔, 如 SPARK,Sai,NTb"
        style="flex:1;min-width:220px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:13px">
    </div>
    <div class="size-foot">
      <span class="size-preview" id="filterPreview"></span>
      <button class="mbtn" onclick="saveTrackFilter()">保存筛选</button>
    </div>
  </div>
  <div class="sizecard">
    <div class="sc-head"><span class="sc-name">磁力大小范围</span><span style="font-size:11px;color:var(--muted)">单位 GB</span></div>
    <div class="sc-desc">大小区间只对 <b>2160p</b> 与 <b>1080p</b> 两档生效, 其它分辨率(720p/480p)不设区间。「不限大小」= 该档不设限制;「自定义范围」= 只推最小–最大之间的磁力。</div>
    ${tier('4k','4K','tier-4k')}
    ${tier('1080','1080p','tier-1080')}
    <div class="size-foot">
      <span class="size-preview" id="sizePreview"></span>
      <button class="mbtn" onclick="saveTrackSize()">保存范围</button>
    </div>
  </div>
  <button class="toolbar-btn-full" onclick="runTrackCheck()">${icon('refresh')}立即检查全部追踪<span style="color:var(--muted);font-size:12px;font-weight:400">后台每 6 小时自动检查一次</span></button>
  <h3>追踪列表</h3>
  <div id="trackBody"><div class="empty"><span class="spin"></span>加载…</div></div>`;
  try{
    const st = await api('/api/track/settings');
    // ⚠️ 竞态守卫: await 期间可能已切走页签, 追踪页 DOM 被 renderManage 覆盖移除
    if(!$('#trackBody') || !$('#trackAutoPush')) return;
    $('#trackAutoPush').checked = st.auto_push;
    TRACK_SIZE['4k'] = {min: st.size_4k_min_gb ?? '', max: st.size_4k_max_gb ?? ''};
    TRACK_SIZE['1080'] = {min: st.size_1080_min_gb ?? '', max: st.size_1080_max_gb ?? ''};
    ['4k','1080'].forEach(k=>{
      const hasRange = TRACK_SIZE[k].min !== '' || TRACK_SIZE[k].max !== '';
      trackTierMode(k, hasRange ? 'range' : 'any', true);
      $('#trackMin-'+k).value = TRACK_SIZE[k].min;
      $('#trackMax-'+k).value = TRACK_SIZE[k].max;
    });
    // 推送筛选: 后端给的是数组(分辨率从未设置过时 = 默认两档), 勾选状态据此回填
    TRACK_FILTER = {
      resolutions: st.resolutions || [],
      sources: st.sources || [],
      groups: st.groups || [],
    };
    document.querySelectorAll('input.tf-res').forEach(i=>{ i.checked = TRACK_FILTER.resolutions.includes(i.value); });
    document.querySelectorAll('input.tf-src').forEach(i=>{ i.checked = TRACK_FILTER.sources.includes(i.value); });
    $('#trackGroups').value = TRACK_FILTER.groups.join(',');
    trackFilterPreview();
    const list = await api('/api/track');
    renderTrackList(list);
  }catch(e){ $('#trackBody').innerHTML = `<div class="empty">加载失败: ${e.message}</div>`; }
}
// 档位模式切换: any=不限 / range=自定义(最小–最大); silent=仅初始化 UI 不重写状态
function trackTierMode(key, mode, silent){
  const seg = $('#tierSeg-'+key), range = $('#tierRange-'+key);
  seg.querySelectorAll('button').forEach(b=>b.classList.toggle('active', b.dataset.mode===mode));
  range.classList.toggle('on', mode==='range');
  if(mode==='range'){
    if(!silent){ TRACK_SIZE[key] = {min: $('#trackMin-'+key).value, max: $('#trackMax-'+key).value}; }
    // 自动聚焦第一个空输入, 减少一次点击
    const min = $('#trackMin-'+key), max = $('#trackMax-'+key);
    if(!silent && !min.value && document.activeElement!==min && document.activeElement!==max) min.focus();
  }else if(!silent){
    TRACK_SIZE[key] = {min:'', max:''};
  }
  _trackSizePreview();
}
// 实时预览当前配置(改输入/切模式即更新, 保存前就能看懂规则)
function _trackSizePreview(){
  const el = $('#sizePreview'); if(!el) return;
  const fmt = k => {
    const s = TRACK_SIZE[k];
    if(s.min==='' && s.max==='') return `${k==='4k'?'4K':'1080p'} 不限大小`;
    return `${k==='4k'?'4K':'1080p'} ${s.min===''?'0':s.min} – ${s.max===''?'∞':s.max} GB`;
  };
  el.innerHTML = `推送规则: <b>${fmt('4k')}</b> · <b>${fmt('1080')}</b>`;
}
document.addEventListener('input', e=>{
  if(e.target && e.target.id && /^track(Min|Max)-(4k|1080)$/.test(e.target.id)){
    // ⚠️ key 必须取最后一个 '-' 之后: slice(-3) 对 trackMin-4k 取到 "-4k"、对 trackMax-1080 取到 "080", 都不是合法档位
    const key = e.target.id.split('-').pop(), isMin = e.target.id.includes('Min');
    TRACK_SIZE[key][isMin?'min':'max'] = e.target.value;
    _trackSizePreview();
  }
});
function renderTrackList(list){
  const body = $('#trackBody');
  if(!body) return;   // 竞态: await 期间已切走页签, 追踪列表 DOM 已被移除
  if(!list.length){
    body.innerHTML = `<div class="empty" style="padding:18px">尚未追踪任何演员/剧集。<br>
      <span style="font-size:12px">在<b>演员页</b>点「追踪新作」, 或在<b>剧集详情页</b>点「追踪新季」。</span></div>`;
    return;
  }
  body.innerHTML = `<table><thead><tr><th>名称</th><th>类型</th><th>自动推送</th><th>上次检查</th><th>上次结果</th><th></th></tr></thead><tbody>`
    + list.map(t=>{
      const push = t.autoPush===null ? `<span class="badge">跟随全局</span>` : (t.autoPush?'<span class="badge ok">开</span>':'<span class="badge err">关</span>');
      const next = t.autoPush===null?'on':(t.autoPush?'off':'follow');
      const nextLabel = t.autoPush===null?'关(覆盖)':(t.autoPush?'跟随全局':'开(覆盖)');
      return `<tr>
        <td data-th="名称">${esc(t.name)}<span style="color:var(--muted);font-size:11px"> #${t.refId}</span></td>
        <td data-th="类型">${t.kind==='person'?'演员新作':'剧集新季'}</td>
        <td data-th="自动推送">${push} <button class="mbtn sm" onclick="setTrackPush('${t.kind}',${t.refId},'${next}')">${nextLabel}</button></td>
        <td data-th="上次检查">${fmtTime(t.lastCheckedAt)}</td>
        <td data-th="上次结果" style="font-size:12px;color:var(--muted)">${esc(t.lastResult||'')}</td>
        <td data-th=""><button class="mbtn sm danger" onclick="delTrack('${t.kind}',${t.refId})">移除</button></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}
async function saveTrackSetting(key, val){
  try{
    if(key==='auto_push'){ await api(`/api/track/settings?auto_push=${val}`,{method:'POST'}); toast(val?'已开启自动推送':'已关闭自动推送'); }
  }catch(e){ toast('失败: '+e.message); }
}
async function saveTrackSize(){
  // 从 TRACK_SIZE 状态推导(切"不限"时输入框的值不进请求, 靠 clear= 清掉旧值)
  const params = new URLSearchParams();
  const cleared = [];
  for(const k of ['4k','1080']){
    const s = TRACK_SIZE[k];
    const pfx = k==='4k' ? 'size_4k' : 'size_1080';
    if(s.min !== '' && s.min != null) params.set(pfx+'_min_gb', s.min); else cleared.push(pfx+'_min');
    if(s.max !== '' && s.max != null) params.set(pfx+'_max_gb', s.max); else cleared.push(pfx+'_max');
  }
  if(cleared.length) params.set('clear', cleared.join(','));
  try{ await api(`/api/track/settings?${params.toString()}`,{method:'POST'}); toast('已保存大小范围'); }
  catch(e){ toast('失败: '+e.message); }
}
// ---- 推送筛选(片源/分辨率/发布组): 读状态 / 预览 / 保存 ----
function _tfChecked(cls){
  return Array.from(document.querySelectorAll('input.'+cls)).filter(i=>i.checked).map(i=>i.value);
}
// 改勾选/改发布组输入即更新预览, 保存前就能看懂推送规则
function trackFilterPreview(){
  const el = $('#filterPreview'); if(!el) return;
  const res = _tfChecked('tf-res'), src = _tfChecked('tf-src');
  const grp = ($('#trackGroups').value || '').split(',').map(s=>s.trim()).filter(Boolean);
  TRACK_FILTER = {resolutions: res, sources: src, groups: grp};
  const label = (list, all) => list.length ? list.join('/') : all;
  el.innerHTML = `筛选: 分辨率 <b>${label(res,'全部档')}</b> · 片源 <b>${label(src,'不限')}</b>`
    + ` · 发布组 <b>${label(grp,'不限')}</b>`
    + (res.some(v=>v!=='2160p' && v!=='1080p') ? `<br><span style="font-size:11px">720p/480p 没有大小区间, 见下方卡片</span>` : '');
}
async function saveTrackFilter(){
  const params = new URLSearchParams();
  params.set('resolutions', _tfChecked('tf-res').join(','));
  params.set('sources', _tfChecked('tf-src').join(','));
  params.set('groups', ($('#trackGroups').value || '').split(',').map(s=>s.trim()).filter(Boolean).join(','));
  try{ await api(`/api/track/settings?${params.toString()}`,{method:'POST'}); toast('已保存推送筛选'); trackFilterPreview(); }
  catch(e){ toast('失败: '+e.message); }
}
async function setTrackPush(kind, refId, mode){
  try{ await api(`/api/track/auto_push?kind=${kind}&ref_id=${refId}&mode=${mode}`,{method:'POST'}); loadTrack(); }
  catch(e){ toast('失败: '+e.message); }
}
async function delTrack(kind, refId){
  if(!confirm('移除该追踪?')) return;
  try{ await api(`/api/track?kind=${kind}&ref_id=${refId}`,{method:'DELETE'}); toast('已移除'); loadTrack(); }
  catch(e){ toast('失败: '+e.message); }
}
async function runTrackCheck(){
  if(TRACK_CHECKING){ toast('检查进行中…'); return; }
  TRACK_CHECKING = true;
  const btn = $('#trackCheckBtn');
  if(btn) btn.disabled = true;
  try{
    const r = await api('/api/track/check',{method:'POST'});
    // 轮询
    for(let i=0;i<120;i++){
      await new Promise(res=>setTimeout(res, 2000));
      const st = await api(`/api/track/check/status?job=${r.job}`);
      if(st.status==='done' || st.status==='error') break;
    }
    const st = await api(`/api/track/check/status?job=${r.job}`);
    if(st.status==='error') toast('检查失败: '+(st.error||'').slice(0,80));
    else if(st.result) toast(`检查完成: 新增 ${st.result.new_total}, 推送 ${st.result.pushed}`);
    loadTrack();
  }catch(e){ toast('失败: '+e.message); }
  finally{ TRACK_CHECKING = false; if(btn) btn.disabled = false; }
}
// 详情页/演员页「追踪」按钮: 点一下切换 追踪/停止
async function toggleTrackPerson(personId, btn){
  try{
    const st = await api(`/api/track/has?kind=person&ref_id=${personId}`);
    if(st.tracked){ await api(`/api/track?kind=person&ref_id=${personId}`,{method:'DELETE'}); toast('已停止追踪该演员'); }
    else{ const name = btn && btn.dataset.name ? btn.dataset.name : ''; await api(`/api/track?kind=person&ref_id=${personId}&name=${encodeURIComponent(name)}`,{method:'POST'}); toast('已追踪该演员: 出新电影会自动检查'); }
    _refreshTrackBtns();
  }catch(e){ toast('失败: '+e.message); }
}
async function toggleTrackShow(tmdbId, btn){
  try{
    const st = await api(`/api/track/has?kind=show&ref_id=${tmdbId}`);
    if(st.tracked){ await api(`/api/track?kind=show&ref_id=${tmdbId}`,{method:'DELETE'}); toast('已停止追踪该剧'); }
    else{ const name = btn && btn.dataset.name ? btn.dataset.name : ''; await api(`/api/track?kind=show&ref_id=${tmdbId}&name=${encodeURIComponent(name)}`,{method:'POST'}); toast('已追踪: 出新季会自动检查'); }
    _refreshTrackBtns();
  }catch(e){ toast('失败: '+e.message); }
}
// 页面内所有追踪按钮按最新状态刷新文案
async function _refreshTrackBtns(){
  try{
    const list = await api('/api/track');
    const set = {};
    list.forEach(t=>{ set[t.kind+'_'+t.refId]=true; });
    document.querySelectorAll('[data-track-key]').forEach(b=>{
      const on = !!set[b.dataset.trackKey];
      b.classList.toggle('ghost', !on);
      b.querySelector('span') && (b.querySelector('span').textContent = on ? '已追踪, 点击取消' : b.dataset.trackLabel);
    });
  }catch{}
}

