// MediaAuto 前端 — common.js
// 公共: 图标/工具函数(api,esc,toast,img)/登录/启动/搜索解析/通用卡片/页签切换
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

const $ = s => document.querySelector(s);

// ---- 内联 SVG 图标库(Lucide 风格线性图标, 替代 emoji; 继承当前文字颜色) ----
const ICON_PATHS = {
  search:  '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  movie:   '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/>',
  tv:      '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="m17 2-5 5-5-5"/>',
  puzzle:  '<path d="M19.4 14a2.5 2.5 0 0 0-.45-4.95 2.5 2.5 0 0 0-2.45-3.05 2.5 2.5 0 0 0-3 2.45V10a1 1 0 0 0-1 1v1a1 1 0 0 0 1 1h.5a2.5 2.5 0 0 0 2.45 3.05 2.5 2.5 0 0 0 4.95.45z"/>',
  magnet:  '<path d="M6 15a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2z"/><path d="M6 9h4M14 9h4"/>',
  ban:     '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
  star:    '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
  check:   '<path d="M20 6 9 17l-5-5"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
  film:    '<rect x="2" y="2" width="20" height="20" rx="2.18"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
  x:       '<path d="M18 6 6 18M6 6l12 12"/>',
  back:    '<path d="m15 18-6-6 6-6"/>',
  copy:    '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  folder:  '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  list:    '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  play:    '<path d="m6 3 14 9-14 9V3z"/>',
  external:'<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  flame:   '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  shield:  '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  image:   '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
  clock:   '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  database:'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5M3 12a9 3 0 0 0 18 0"/>',
  box:     '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
  alert:   '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
  info:    '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  cloud:   '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  eye:     '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  trash:   '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>',
  move:    '<path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20"/>',
  file:    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  users:   '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  award:   '<circle cx="12" cy="8" r="6"/><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11"/>',
  bell:    '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  edit:    '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
};
function icon(name, size){
  const p = ICON_PATHS[name] || ICON_PATHS.info;
  const s = size ? ` style="width:${size};height:${size}"` : '';
  return `<svg class="icn" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"${s}>${p}</svg>`;
}
/* ===== 夜间模式(2026-09-19 用户要求: 按时间自动切换, 全元素支持) =====
   规则: 跟随系统(auto, 默认) 在 19:00–07:00 之间 = 夜间; 也可手动锁定 浅色/深色。
   首屏应用见 index.html <head> 内联脚本(防闪烁); 这里负责定时自动切换 + 手动切换 + 持久化。 */
let THEME_PREF = 'auto';
const THEME_DARK_START = 19, THEME_DARK_END = 7;   // 21 点进入夜间, 7 点回到日间
function themePref(){ try{ return localStorage.getItem('media_theme') || 'auto'; }catch{ return 'auto'; } }
function themeIsDark(pref){
  pref = pref || themePref();
  if(pref === 'dark') return true;
  if(pref === 'light') return false;
  const h = new Date().getHours();
  return h >= THEME_DARK_START || h < THEME_DARK_END;
}
function applyTheme(){
  const dark = themeIsDark(THEME_PREF);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  const m = document.querySelector('meta[name="theme-color"]');
  if(m) m.setAttribute('content', dark ? '#0b0e14' : '#eef2f9');
}
function setTheme(pref){
  THEME_PREF = pref;
  try{ localStorage.setItem('media_theme', pref); }catch{}
  applyTheme();
  _armThemeTimer();
  // 若「通用」页开着, 同步分段高亮
  const seg = $('#themeSeg');
  if(seg) seg.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.pref === pref));
}
// 自动模式: 精确在 07:00 / 19:00 边界处触发一次切换(到点后重排), 不轮询
function _armThemeTimer(){
  if(THEME_PREF !== 'auto') return;
  const now = new Date();
  let target = new Date(now);
  const isDayToNight = now.getHours() < THEME_DARK_START;
  target.setHours(isDayToNight ? THEME_DARK_START : THEME_DARK_END, 0, 0, 0);
  if(target <= now) target.setDate(target.getDate() + 1);
  const ms = target - now;
  clearTimeout(window._themeTimer);
  window._themeTimer = setTimeout(()=>{ applyTheme(); _armThemeTimer(); }, Math.min(ms, 30 * 60 * 1000));
}
function initTheme(){
  THEME_PREF = themePref();
  applyTheme();
  _armThemeTimer();
}
let CATEGORIES = [];
let PUSH_CONTENT_TYPE = 'movie';
let LIBRARIES = [];
let CARD_LIST = [];

async function api(path, opts={}){
  const res = await fetch(path, Object.assign({credentials:'same-origin',headers:{'Content-Type':'application/json'}}, opts));
  if(res.status===401){ showLogin(); throw new Error('unauth'); }
  if(!res.ok){ let m=''; try{m=(await res.json()).detail}catch{} throw new Error(m||res.statusText); }
  return res.json();
}
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2200); }

function fmtTime(d){ if(!d) return '—'; try{ return new Date(d).toLocaleString('zh-CN'); }catch{ return String(d); } }
function statusBadge(s){
  if(s==='success') return '<span class="badge ok">成功</span>';
  if(s==='error') return '<span class="badge err">失败</span>';
  if(s==='running') return '<span class="badge run">运行中</span>';
  return `<span class="badge">${s||''}</span>`;
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// 图片本地缓存代理: 开关打开时, TMDB CDN 图片改走 /api/img/<base64url>, 磁盘缓存+ETag 省带宽
let IMG_CACHE = true;
function b64url(s){ const b=btoa(unescape(encodeURIComponent(s))); return b.replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''); }
function img(url){
  if(!url) return url;
  if(IMG_CACHE && /^https?:\/\/image\.tmdb\.org\//.test(url)) return `/api/img/${b64url(url)}`;
  return url;
}

// 初始页签: 支持 ?tab=xxx(PWA manifest shortcuts / 深链), 默认 movie
// 管理页可带子页签: ?tab=manage:missing → 返回 [tab, sub]
function _initialTab(){
  const t = new URLSearchParams(location.search).get('tab') || 'movie';
  const parts = t.split(':');
  const tab = ['movie','tv','organize','manage'].includes(parts[0]) ? parts[0] : 'movie';
  return [tab, parts[1] || null];
}
async function boot(){
  try{ const me = await api('/api/auth/me'); if(me.authed){ await loadImgSetting(); $('#login').style.display='none'; $('#app').style.display='block'; const [tab,sub]=_initialTab(); switchTab(tab, sub); _maybeSetup(); } else showLogin(); }
  catch{ showLogin(); }
}
// 首次启动引导(setup_done=0 时弹出分步引导; 已完成则静默返回)
function _maybeSetup(){ if(typeof maybeStartSetup === 'function') maybeStartSetup().catch(()=>{}); }
async function loadImgSetting(){
  try{ const st = await api('/api/settings'); IMG_CACHE = st.image_cache; }catch{ IMG_CACHE = true; }
}
function showLogin(){ $('#app').style.display='none'; $('#login').style.display='flex'; $('#loginErr').textContent=''; }
async function logout(){ try{ await api('/api/auth/logout',{method:'POST'}); }catch{} showLogin(); }

$('#loginForm').addEventListener('submit', async e=>{
  e.preventDefault();
  try{
    await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('#lu').value,password:$('#lp').value})});
    $('#login').style.display='none'; $('#app').style.display='block'; const [tab,sub]=_initialTab(); switchTab(tab, sub); _maybeSetup();
  }catch(err){ $('#loginErr').textContent = err.message==='unauth'?'用户名或密码错误':'登录失败'; }
});

async function freeSearch(){
  const q = $('#globSearch').value.trim(); if(!q) return;
  openTitleSearch(q, true);   // 顶栏搜索 = 用户打开新视图, history 压一层
}

// 两段式搜索第一步: 先经 TMDB 确认具体影片, 点卡片进详情再看磁力; 找不到影片才直搜磁力
async function openTitleSearch(q, pushHist){
  PUSH_CONTENT_TYPE='movie';
  _openModal(pushHist);
  _syncCloseBtn();   // 搜索页是顶层视图(栈一般为空) → 按钮回 X; 与栈状态保持一致
  _hero('搜索: '+q, '<div class="row">先确认是哪部影片, 点卡片看详情与磁力</div>');
  _navTitle(q);
  $('#mBody').innerHTML = `
    <div class="ts-row" style="display:flex;gap:8px;margin-bottom:14px">
      <input id="tsInput" value="${esc(q)}" placeholder="换个片名再搜"
        style="flex:1;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:16px" /* 16px 防 iOS 聚焦放大 */
        onkeydown="if(event.key==='Enter')openTitleSearch(this.value.trim())"/>
      <button class="mbtn"
        onclick="openTitleSearch($('#tsInput').value.trim())">搜索</button>
    </div>
    <div id="resBox"><div class="empty"><span class="spin"></span>正在解析影片与演员…</div></div>
    <div id="personSec"><div class="empty" style="padding:10px 0"><span class="spin"></span>正在搜演员…</div></div>`;
  // 影片与演员并行解析(演员段: 搜演员/导演, 点进演员页看作品)
  const personTask = (async()=>{
    try{
      const pr = await api(`/api/person/search?q=${encodeURIComponent(q)}`);
      const people = pr.results||[];
      const sec = $('#personSec'); if(!sec) return;
      if(!people.length){ sec.innerHTML = ''; return; }
      PERSON_LIST = people;
      sec.innerHTML = `<h3 style="margin:16px 0 8px">${icon('users')} 演员 / 导演 (${people.length})</h3>
        <div class="psearch-grid">${people.map((p,i)=>{
          const av = p.profile ? `<img class="psearch-av" src="${img(p.profile)}" loading="lazy" alt=""/>` : '<div class="psearch-av ph">?</div>';
          const kf = (p.knownFor||[]).slice(0,2).join(' · ');
          return `<div class="psearch-card" onclick="openPersonIdx(${i})">${av}
            <div class="psearch-info"><div class="psearch-name">${esc(p.name)}</div>
            ${kf?`<div class="psearch-kf">${esc(kf)}</div>`:''}</div></div>`;
        }).join('')}</div>`;
    }catch(e){ const sec=$('#personSec'); if(sec) sec.innerHTML = ''; }
  })();
  try{
    const d = await api(`/api/browse/search?q=${encodeURIComponent(q)}`);
    const cards = d.results||[];
    const box = $('#resBox'); if(!box) return;
    if(!cards.length){
      box.innerHTML = `<div style="color:var(--muted);font-size:12px;margin-bottom:8px">TMDB 没解析到这部影片, 直接搜磁力:</div>
        <div id="magFallback"><div class="empty"><span class="spin"></span>搜索磁力中…</div></div>`;
      searchMagnets(q, q, '#magFallback');
      await personTask;   // 影片没解析到 → 仍展示演员段(可能搜的是演员名)
      return;
    }
    CARD_LIST = cards;
    _ctxStack = [{type:'search', q}];  // 当前处于搜索页 → 点卡片/演员进详情后, 返回可回此页
    box.innerHTML = `<div style="color:var(--muted);font-size:12px;margin-bottom:8px">影片 · 共 ${cards.length} 个候选(点卡片看详情)</div>
      <div class="grid">${cards.map((c,i)=>cardHTML(c,i)).join('')}</div>`;
    await personTask;
  }catch(e){ const _rb = $('#resBox'); if(_rb) _rb.innerHTML = `<div class="empty">影片解析失败: ${e.message}</div>`; }
}
let PERSON_LIST = [];
function openPersonIdx(i){ const p=PERSON_LIST[i]; if(p) openPersonPage(p.id); }
// 页签切换: active 高亮 + 滑动指示器(桌面顶部胶囊 / 移动底部背景) + 内容淡入上移动画
// 4 页签(2026-09 二版): movie(电影热门) / tv(剧集热门) / organize(整理) / manage(管理, 内含 6 个子页签)
function switchTab(tab, sub){
  // ⚠️ 移动端: 详情/搜索是整页模态(z-index:50)盖住底部导航。若在模态内点底部页签,
  // 先关掉模态再切, 否则切的是底层页签、模态仍盖着, 看起来像"导航没了/点了没反应"。
  const _mb = document.getElementById('modalBg');
  if(_mb && _mb.classList.contains('show') && typeof closeModal === 'function'){ closeModal(); }
  const nav = document.querySelector('nav.tabs');
  nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active', b.dataset.tab===tab));
  _moveTabPill();   // 滑动指示器跟随 active 按钮
  ['movie','tv','organize','manage'].forEach(t=>{ $('#tab-'+t).style.display = (t===tab)?'block':'none'; });
  // 内容切换动画: 重触发 .tab-anim 的 fadeUp 关键帧(切走→切回都播放, 感知"切换"了)
  const pane = $('#tab-'+tab);
  if(pane){ pane.classList.remove('tab-anim'); void pane.offsetWidth; pane.classList.add('tab-anim'); }
  // 切离管理页 → 停掉「下载」子页签的自动轮询(由 downloads.js 暴露钩子)
  if(tab!=='manage' && window._dlOnSubChange) window._dlOnSubChange(tab);
  if(tab==='movie'){ TREND = TREND_MOVIE; loadTrending(); }
  else if(tab==='tv'){ TREND = TREND_TV; loadTrending(); }
  else if(tab==='organize') loadOrganize();
  else if(tab==='manage') renderManage(sub);
}
// 滑动指示器: 一个绝对定位的 .tab-pill 平滑移动到当前 active 按钮位置(桌面+移动通用)
function _moveTabPill(){
  const nav = document.querySelector('nav.tabs');
  if(!nav) return;
  const active = nav.querySelector('button.active');
  let pill = nav.querySelector('.tab-pill');
  if(!active){ if(pill) pill.style.opacity=0; return; }
  if(!pill){ pill = document.createElement('span'); pill.className='tab-pill'; nav.appendChild(pill); }
  const r = active.getBoundingClientRect();
  const nr = nav.getBoundingClientRect();
  pill.style.opacity = 1;
  pill.style.left = (r.left - nr.left + nav.scrollLeft) + 'px';
  pill.style.top = (r.top - nr.top + nav.scrollTop) + 'px';
  pill.style.width = r.width + 'px';
  pill.style.height = r.height + 'px';
}
window.addEventListener('resize', ()=>_moveTabPill());
window.addEventListener('load', ()=>_moveTabPill());

// 顶层"库内状态"徽章: 完整 / 不完整 / 库内·未确认 / 未拥有
// availStatus 后端枚举: 5=完整 4=不完整 8=库内·完整性未知(无则按 inLibrary 兜底)
function libBadge(it){
  if(it.inLibrary){
    if(it.availStatus === 5) return '<span class="badge ok">完整</span>';
    if(it.availStatus === 4) return '<span class="badge warn">不完整</span>';
    if(it.availStatus === 8) return '<span class="badge info">库内·未确认</span>';
    return '<span class="badge lib">库内</span>';
  }
  return '<span class="badge warn">未拥有</span>';
}

function cardHTML(it, idx){
  const poster = it.poster ? `<img class="poster" src="${img(it.poster)}" loading="lazy"/>`
    : `<div class="poster empty">无海报</div>`;
  const badge = it.kind==='tv' ? '<span class="badge">剧集</span>' : '<span class="badge">电影</span>';
  const vote = it.vote ? `<span class="badge">${icon('star')}${it.vote}</span>` : '';
  const flags = [
    libBadge(it),
    (it.missingCount!=null && it.missingCount>0) ? `<span class="badge err">缺${it.missingCount}集</span>` : '',
    (it.extraCount!=null && it.extraCount>0) ? `<span class="badge warn">多${it.extraCount}集</span>` : '',
    // numbersSynced=false: TMDB 真实集号还没同步 → 后端不报缺/多(不猜), 这里如实提示
    it.numbersSynced === false ? '<span class="badge warn" title="TMDB 真实集号同步中, 暂不判断缺/多">集号待同步</span>' : '',
    it.inProduction ? '<span class="badge pend">在播</span>' : '',
  ].filter(Boolean).join('');
  return `<div class="card" style="--i:${idx%12}" onclick="openCardIdx(${idx})">
    ${poster}<div class="meta"><div class="title">${esc(it.title)}</div>
    <div class="sub"><span>${esc(it.year||'')}</span></div>
    <div>${badge}${vote}</div>
    ${flags?`<div class="flags">${flags}</div>`:''}</div></div>`;
}
function openCardIdx(i){ openCard(CARD_LIST[i]); }
