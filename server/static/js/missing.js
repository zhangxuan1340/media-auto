// MediaAuto 前端 — missing.js
// 缺失页: 剧集分集缺失 / 电影未拥有
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载


async function loadMissing(){
  // 2026-09 二版: 缺失并入「管理」页第 1 个子页签, 渲染到 #manageBody(由 settings.js 路由)
  const el = $('#manageBody');
  el.innerHTML = `<div class="toolbar"><span style="color:var(--muted);font-size:13px">
    基于本地 TMDB 缓存 + Jellyfin 分集明细。<b>请先在「本地库」同步 TMDB 与分集明细。</b></span></div>
    <nav class="subtabs">
      <button id="ms-tv" class="active" onclick="loadMissingKind('tv')">剧集(分集缺失)</button>
      <button id="ms-movie" onclick="loadMissingKind('movie')">电影(未拥有)</button>
    </nav><div id="missingBody"></div>`;
  _msRefreshRounds = 0;                      // 重新进入缺失页 → 自动刷新轮数重新计
  if(_msRefreshTimer){ clearTimeout(_msRefreshTimer); _msRefreshTimer = null; }
  loadMissingKind('tv');
}
async function loadMissingKind(kind){
  const el = $('#missingBody'); if(!el) return;
  $('#ms-tv').classList.toggle('active', kind==='tv');
  $('#ms-movie').classList.toggle('active', kind==='movie');
  el.innerHTML='<div class="empty"><span class="spin"></span>加载缺失…</div>';
  let epBanner = '';
  if(kind==='tv'){
    try{
      const st = await api('/api/jf-episodes-status');
      if(st && !st.synced){
        epBanner = `<div style="background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px;font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span>${icon('alert',18)} Jellyfin 分集明细未同步 —— 下方只列「未拥有」的剧集, 无法逐集对比。同步后才能看到精确到 SxxExx 的分集缺失。</span>
          <button class="ghost sm" id="missEpSync" onclick="syncEpisodesFromMissing()">${icon('refresh')}同步分集明细(约1分钟)</button></div>`;
      }
    }catch(e){}
  }
  try{
    const list = await api(`/api/missing?kind=${kind}`);
    if(!list.length){
      el.innerHTML = epBanner + `<div class="empty">${icon('check',20)} 本地没有缺失的${kind==='tv'?'剧集(分集)':'电影'}<br><span style="font-size:12px">若无 TMDB 数据, 请先在「本地库」点「同步 TMDB」</span></div>`;
      return;
    }
    CARD_LIST = list;
    // 集号未同步的剧: 后端**不报缺/多**(没有 TMDB 真实集号就不猜), 这里如实提示并稍后自动刷新
    const unk = kind==='tv' ? list.filter(x=>x && x.numbersSynced===false).length : 0;
    const sum = kind==='tv' ? list.reduce((a,x)=>a+(x.missingCount||0),0) : list.length;
    const sumTxt = kind==='tv'
      ? [sum>0?`缺失 ${sum} 集`:'', unk?`${unk} 部集号待同步`:''].filter(Boolean).join(' · ')
      : `未拥有 ${list.length} 部`;
    el.innerHTML = epBanner + `<div style="color:var(--muted);font-size:12px;margin-bottom:10px">共 ${list.length} 部${sumTxt?' · '+sumTxt:''}</div>
      <div class="grid">${list.map((it,i)=>cardHTML(it,i)).join('')}</div>`;
    if(unk) _msAutoRefresh(kind);   // 后台正在回填集号 → 自动刷新直到补齐
  }catch(e){ el.innerHTML=epBanner+`<div class="empty">加载失败: ${e.message}</div>`; }
}

// 集号待同步时的自动刷新: 每 15s 重拉一次(后端每次会再排一批回填), 最多 24 轮(6 分钟)
let _msRefreshTimer = null, _msRefreshRounds = 0;
function _msAutoRefresh(kind){
  if(_msRefreshTimer) return;
  if(_msRefreshRounds >= 24) return;
  _msRefreshTimer = setTimeout(()=>{
    _msRefreshTimer = null; _msRefreshRounds++;
    const tab = document.getElementById('ms-'+kind);
    const body = document.getElementById('missingBody');
    if(!tab || !tab.classList.contains('active') || !body) return;  // 已切走/离开 → 不刷
    loadMissingKind(kind);
  }, 15000);
}
// 缺失页触发分集同步: 完成后回到当前缺失页签刷新
async function syncEpisodesFromMissing(){
  const btn = $('#missEpSync'); if(btn){ btn.disabled=true; btn.innerHTML=icon('refresh')+'同步中…'; }
  try{
    await api('/api/sync/jellyfin?scope=episodes', {method:'POST'});
    toast('已触发分集明细同步, 完成后自动刷新缺失页');
    let tries = 0;
    const timer = setInterval(async ()=>{
      tries++;
      try{
        const st = await api('/api/jf-episodes-status');
        if(st && st.synced){ clearInterval(timer); toast('分集明细已同步'); loadMissing(); return; }
      }catch(e){}
      if(tries > 90){ clearInterval(timer); toast('同步仍在进行, 稍后手动刷新'); loadMissing(); }
    }, 2000);
  }catch(e){ toast('触发失败: '+e.message); if(btn){ btn.disabled=false; btn.innerHTML=icon('refresh')+'同步分集明细(约1分钟)'; } }
}

