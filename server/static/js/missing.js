// MediaAuto 前端 — missing.js
// 缺失页: 剧集分集缺失 / 电影未拥有 —— **分页 + 滚动加载**(整表由后端 30s 缓存切片)
// 全局作用域(classic script), 依赖 common.js 工具函数 + trending.js 的滚动容器探测,
// 由 index.html 按序加载。
// 旧版一次拉全表(tv 2200+ 部)再整表 innerHTML, 页面要卡好几秒 —— 现在每次只铺
// limit 张卡, 滚到底再续下一页(与热门榜同一套滚动口径)。

const MS = {kind:'tv', offset:0, limit:50, loading:false, hasMore:true,
            total:0, missingSum:0, unknown:0, list:[], reqId:0, epBanner:''};


async function loadMissing(){
  // 2026-09 二版: 缺失并入「管理」页第 1 个子页签, 渲染到 #manageBody(由 settings.js 路由)
  const el = $('#manageBody');
  el.innerHTML = `<div class="toolbar"><span style="color:var(--muted);font-size:13px">
    基于本地 TMDB 缓存 + Jellyfin 分集明细。<b>请先在「本地库」同步 TMDB 与分集明细。</b></span></div>
    <nav class="subtabs">
      <button id="ms-tv" class="active" onclick="loadMissingKind('tv')">剧集(分集缺失)</button>
      <button id="ms-movie" onclick="loadMissingKind('movie')">电影(未拥有)</button>
    </nav><div id="missingBody"></div>`;
  // 进入缺失页 → 自动刷新轮数重新计, 取消上一轮挂起的刷新
  _msRefreshRounds = 0;
  if(_msRefreshTimer){ clearTimeout(_msRefreshTimer); _msRefreshTimer = null; }
  loadMissingKind('tv');
}

async function loadMissingKind(kind){
  const el = $('#missingBody'); if(!el) return;
  $('#ms-tv').classList.toggle('active', kind==='tv');
  $('#ms-movie').classList.toggle('active', kind==='movie');
  // 切页签/自动刷新 → 分页状态全清, 从第一页重铺(防旧页数据串台)
  MS.kind = kind; MS.offset = 0; MS.loading = false; MS.hasMore = true;
  MS.total = 0; MS.missingSum = 0; MS.unknown = 0; MS.epBanner = '';
  MS.reqId++;
  MS.list.length = 0;
  CARD_LIST = MS.list;                 // 卡片点击 → openCardIdx(i) 走同一数组
  el.innerHTML = '<div class="empty"><span class="spin"></span>加载缺失…</div>';

  if(kind==='tv'){
    try{
      const st = await api('/api/jf-episodes-status');
      if(st && !st.synced){
        MS.epBanner = `<div style="background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px;font-size:13px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span>${icon('alert',18)} Jellyfin 分集明细未同步 —— 下方只列「未拥有」的剧集, 无法逐集对比。同步后才能看到精确到 SxxExx 的分集缺失。</span>
          <button class="ghost sm" id="missEpSync" onclick="syncEpisodesFromMissing()">${icon('refresh')}同步分集明细(约1分钟)</button></div>`;
      }
    }catch(e){}
  }
  await msLoadPage();
}

// 拉一页(offset 在 MS 里), 往 #msGrid 后面追加; 首页则整块铺骨架
async function msLoadPage(){
  if(MS.loading || !MS.hasMore) return;
  MS.loading = true;
  const myReq = MS.reqId;
  let ok = false;                       // 本轮是否真的拿到新数据(决定要不要继续续页)
  try{
    const d = await api(`/api/missing?kind=${MS.kind}&offset=${MS.offset}&limit=${MS.limit}`);
    if(myReq !== MS.reqId) return;                 // 已切页签/重置 → 丢弃过期响应
    MS.total = d.total; MS.missingSum = d.missingSum || 0; MS.unknown = d.unknownCount || 0;
    const items = d.items || [];
    const first = !MS.list.length;
    if(first && !items.length){
      $('#missingBody').innerHTML = MS.epBanner + `<div class="empty">${icon('check',20)} 本地没有缺失的${MS.kind==='tv'?'剧集(分集)':'电影'}<br><span style="font-size:12px">若无 TMDB 数据, 请先在「本地库」点「同步 TMDB」</span></div>`;
      MS.hasMore = false;
      return;
    }
    if(!items.length){
      // 总数说还有、这页却一条都吐不出来 → 没东西可续了。不设 false 的话,
      // _msMaybeMore 的"没满一屏就续页"会拿同一个 offset 无限重复打接口。
      MS.hasMore = false;
      _msFoot();
      return;
    }
    ok = true;
    const base = MS.list.length;
    items.forEach(x=>MS.list.push(x));
    MS.offset = MS.list.length;
    MS.hasMore = MS.list.length < MS.total;
    if(first){
      $('#missingBody').innerHTML = MS.epBanner
        + `<div id="msHead" style="color:var(--muted);font-size:12px;margin-bottom:10px"></div>
           <div class="grid" id="msGrid"></div><div id="msFoot"></div>`;
      $('#msGrid').innerHTML = items.map((it,i)=>cardHTML(it, base+i)).join('');
    }else{
      const grid = $('#msGrid');
      if(grid) grid.insertAdjacentHTML('beforeend', items.map((it,i)=>cardHTML(it, base+i)).join(''));
    }
    _msHead();
    _msFoot();
    if(MS.unknown) _msAutoRefresh(MS.kind);   // 后台正在回填集号 → 自动刷新直到补齐
  }catch(e){
    if(myReq !== MS.reqId) return;
    if(!MS.list.length) $('#missingBody').innerHTML = MS.epBanner + `<div class="empty">加载失败: ${esc(e.message)}</div>`;
    else _msFoot(true);
  }finally{
    if(myReq === MS.reqId) MS.loading = false;
    // 只有**成功拿到新数据**才考虑续页: 旧写法无条件续 —— 请求一失败就立刻再打一次,
    // 变成"报错→重试→报错"的空转, 会把后端刷挂。
    // 不满一屏(结果少/大屏)滚不动 → 继续续页, 直到铺满或到底
    if(ok && myReq === MS.reqId) _msMaybeMore();
  }
}

function _msHead(){
  const h = $('#msHead'); if(!h) return;
  const sumTxt = MS.kind==='tv'
    ? [MS.missingSum>0?`缺失 ${MS.missingSum} 集`:'', MS.unknown?`${MS.unknown} 部集号待同步`:''].filter(Boolean).join(' · ')
    : `未拥有 ${MS.total} 部`;
  h.innerHTML = `共 ${MS.total} 部 · 已显示 ${MS.list.length} 部${sumTxt?' · '+sumTxt:''}`;
}

function _msFoot(err){
  const f = $('#msFoot'); if(!f) return;
  if(err){ f.innerHTML = `<div class="empty" style="padding:10px"><button class="ghost sm" onclick="msLoadPage()">加载失败, 点此重试</button></div>`; return; }
  f.innerHTML = !MS.hasMore
    ? (MS.list.length ? '<div style="text-align:center;color:var(--muted);font-size:12px;padding:12px 0">— 已显示全部 —</div>' : '')
    : '<div style="text-align:center;color:var(--muted);font-size:12px;padding:12px 0"><span class="spin"></span> 滚动加载更多…</div>';
}

function _msMaybeMore(){
  if(!MS.hasMore || MS.loading || !MS.list.length) return;
  if(!document.getElementById('msGrid')) return;
  if(MANAGE_SUB !== 'missing') return;
  const c = (typeof _trendScrollContainer === 'function') ? _trendScrollContainer() : null;
  const short = c ? (document.getElementById('msGrid').parentElement.scrollHeight <= c.clientHeight + 40)
                  : (document.documentElement.scrollHeight <= window.innerHeight + 40);
  if(short) msLoadPage();
}

// 滚到底 → 自动续下一页(与热门榜共用同一滚动容器探测; 弹窗打开时不触发)
// rAF 节流: 一帧最多处理一次(滚轮/惯性滚动会把 scroll 事件打到每帧好几次)
let _msScrollRaf = 0;
window.addEventListener('scroll', ()=>{
  if(_msScrollRaf) return;
  _msScrollRaf = requestAnimationFrame(()=>{
    _msScrollRaf = 0;
    if(MANAGE_SUB !== 'missing') return;
    const mb = document.getElementById('modalBg');
    if(mb && mb.classList.contains('show')) return;
    if(!MS.list.length || !MS.hasMore || MS.loading) return;
    if(typeof _trendNearBottom === 'function' && _trendNearBottom()) msLoadPage();
  });
}, {passive:true});

// 集号待同步时的自动刷新: 每 15s 重拉一次(后端每次会再排一批回填), 最多 24 轮(6 分钟)
let _msRefreshTimer = null, _msRefreshRounds = 0;
function _msAutoRefresh(kind){
  if(_msRefreshTimer) return;
  if(_msRefreshRounds >= 24) return;
  _msRefreshTimer = setTimeout(()=>{
    _msRefreshTimer = null;
    // 标签页在后台 / 已切离缺失子页签 → 这一轮不发请求, 但**继续排下一轮**
    // (旧写法直接 return, 一次切走就把回填自动刷新永久停掉)
    if(document.hidden || MANAGE_SUB !== 'missing'){ _msAutoRefresh(kind); return; }
    _msRefreshRounds++;
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
