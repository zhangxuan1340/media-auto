// MediaAuto 前端 — trending.js
// 热门榜: TMDB 趋势榜 + 国家/类型筛选 + 滚动自动加载
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载
//
// 2026-09 二版: 电影/剧集拆成两个独立底部页签(不再用分段控件切换)。
// 双实例: TREND_MOVIE / TREND_TV 各自独立 page/genre/country/loading/列表,
// 互不干扰(切走再回来各页签保持上次位置)。
// 所有加载函数显式传实例 inst —— 因为 loadTrending 内有 await,
// 期间用户可能切到另一页签(别名 TREND 已指向别的实例), 闭包里再读 TREND 会写错实例。
// 页面元素 ID 按 kind 加后缀(trendBody-movie / tGenre-tv …), 两页签 DOM 可同时存在。

// ---- 双实例(电影 / 剧集) ----
// country/genre 任一非空 → 后端改走 discover 热度筛选榜(trending 接口不支持过滤)
// loading=防重入(滚动/手动并发), hasMore=是否还有下一页, 供滚动自动加载判断
// _pendingFirst: 切回页签时若上一笔在途请求还占着 loading 锁, 先置位,
// 等它在 finally 里作废落地(release 锁)后再补一次首屏, 避免首屏卡死在"加载热门榜…"
function _trendInst(kind, tabId){
  return {kind, tabId, window:'week', page:1, timer:null, hideLib:false, reqId:0, genre:0, country:'', loading:false, hasMore:true, _pendingFirst:false, list:[]};
}
const TREND_MOVIE = _trendInst('movie','tab-movie');
const TREND_TV    = _trendInst('tv','tab-tv');
let TREND = TREND_MOVIE;   // 当前激活页签实例别名(detail.js 滚动守卫用)
function _trendList(){ return TREND.list; }
// 当前可见的热门实例(滚动守卫用: 两页签不可能同时可见, 谁可见用谁)
function _visibleTrend(){
  const mv = document.getElementById('tab-movie');
  const tv = document.getElementById('tab-tv');
  if(mv && mv.style.display!=='none') return TREND_MOVIE;
  if(tv && tv.style.display!=='none') return TREND_TV;
  return null;
}
// 实例对应页签是否可见(自动刷新 / 不满一屏自动续 都只在可见时跑)
function _trendTabVisible(inst){
  const el = document.getElementById(inst.tabId);
  return !!(el && el.style.display !== 'none');
}
function trendCard(it, i, rank){
  // 海报网格卡片(复用 .card 样式, 与浏览/缺失页同风格): 一屏十几条, 不再一行一条
  const poster = it.poster ? `<img class="poster" src="${img(it.poster)}" loading="lazy" alt=""/>` : `<div class="poster empty">无海报</div>`;
  const vote = it.vote ? `<span class="badge">${icon('star')}${it.vote}</span>` : '';
  const flags = [
    libBadge(it),
    it.inProduction ? '<span class="badge pend">在播</span>' : '',
  ].filter(Boolean).join('');
  return `<div class="card tc" style="--i:${i%12}" onclick="openTrendIdx(${i})">
    <div class="tcrank">${rank}</div>
    <button class="tpush" title="进详情搜磁力" onclick="event.stopPropagation();pushTrendMagnet(${i},this)">${icon('magnet')}<span>种子</span></button>
    ${poster}
    <div class="meta"><div class="title">${esc(it.title)}</div>
      <div class="sub"><span>${esc(it.year||'')}</span></div>
      ${(vote||flags)?`<div class="flags">${vote}${flags}</div>`:''}</div></div>`;
}
function openTrendIdx(i){ const it=_trendList()[i]; if(it) openCard(it); }   // 列表可能已被重置(刷新/切页签), 防 undefined
async function loadTrending(inst){
  inst = inst || TREND;
  const el = document.getElementById(inst.tabId);
  if(!el) return;
  const k = inst.kind;   // kind 后缀: 所有元素 ID 按页签隔离(trendBody-movie / tGenre-tv …)
  // ⚠️ 关键: 在 await /api/settings 之前"同步"封住滚动监听, 杜绝竞态双行。
  // 此前 loadTrending 在锁外 await 才重置, 窗口内 detail.js 的滚动监听看到
  // (loading=false, hasMore=true, 旧列表非空) 会抢跑 _trendNextPage,
  // 把 page 顶到 2 并发起旧请求, 随后 loadTrending 重置 page=1 再加载 → 两路交错,
  // "加载热门榜…"与"加载中…"两行并存。
  inst.hasMore = false;                 // 同步封禁滚动监听(它在 guard 里查 hasMore)
  inst.list.length = 0;                 // 作废旧数据: 滚动 guard 的 !inst.list.length 也会拦
  inst._pendingFirst = false;
  ++inst.reqId;                          // 作废旧请求令牌: 在途旧响应落地时直接丢弃, 不再渲染
  try{ const st = await api('/api/settings'); inst.hideLib = !!st.hide_complete; }catch{ inst.hideLib = false; }
  // 2026-09 三版布局: 桌面一行(原样); 移动端 order 重排两行 ——
  // 行1: 电影热门榜 —— 已显示N条 [刷新]   行2: [日/周榜][全部类型][全部国家] 三下拉均分
  el.innerHTML = `<div class="toolbar trend-toolbar">
    <span class="tlabel">${k==='movie'?icon('film'):icon('tv')}<b>${k==='movie'?'电影':'剧集'}</b><span class="tsub">热门榜</span></span>
    <i class="tb-break" aria-hidden="true"></i>
    <select id="tWindow-${k}" onchange="loadTrendList(null,'${k}',{reset:1,window:this.value})"><option value="day"${inst.window==='day'?' selected':''}>日榜</option><option value="week"${inst.window==='week'?' selected':''}>周榜</option></select>
    <select id="tGenre-${k}" onchange="loadTrendList(null,'${k}',{reset:1,genre:+this.value})"><option value="0">全部类型</option></select>
    <select id="tCountry-${k}" onchange="loadTrendList(null,'${k}',{reset:1,country:this.value})"><option value="">全部国家</option></select>
    <span class="trend-spacer" style="flex:1"></span>
    <button class="ghost trend-refresh" onclick="loadTrendList(null,'${k}',{reset:1})">${icon('refresh')}刷新</button>
    <span id="trendInfo-${k}" class="trend-info" style="color:var(--muted);font-size:12px"></span>
  </div><div id="trendBody-${k}"><div class="empty"><span class="spin"></span>加载热门榜…</div></div>`;
  $('#tGenre-'+k).value = '0';
  // 每次进页签都从第 1 页开始(否则切走再回来会接着旧页码)
  inst.page = 1; inst.hasMore = true;
  if(inst.loading){
    // 切走时上一笔在途请求还没跑完(仍占着锁): 此时直接 loadTrendList 会被防重入拦掉,
    // 首屏会卡死在"加载热门榜…"。置标记, 等它在 finally 释放锁后再补首屏。
    inst._pendingFirst = true;
  }else{
    loadTrendList(null, k);
  }
  loadTrendFilters(k);
  // 10 分钟自动刷新(与后端缓存 TTL 一致, 回到第 1 页), 离开页签即停
  clearInterval(inst.timer);
  inst.timer = setInterval(()=>{ if(_trendTabVisible(inst)){ inst.page=1; loadTrendList(null, k); } }, 600000);
}
// (工具栏 onchange 直接调 loadTrendList(null,kind,opts); 见下)
async function loadTrendFilters(k){
  const inst = (k==='tv'?TREND_TV:TREND_MOVIE) || TREND;
  // 类型: 复用浏览页的 /api/browse/genres(同一 TMDB 类型表, 无重复实现)
  try{
    const gs = await api(`/api/browse/genres?kind=${inst.kind}`);
    const sel = $('#tGenre-'+k); if(!sel) return;
    const cur = sel.value || '0';
    sel.innerHTML = '<option value="0">全部类型</option>' + gs.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('');
    sel.value = [...sel.options].some(o=>o.value===cur) ? cur : '0';
    if(sel.value !== '0' && +sel.value !== inst.genre) inst.genre = +sel.value;
  }catch{}
  // 国家: TMDB /3/configuration(进程内拉一次即可, 国家列表基本不变)
  try{
    const cs = await api('/api/trending/countries');
    const sel = $('#tCountry-'+k); if(!sel) return;
    const cur = sel.value || '';
    const rows = (cs||[]).filter(c=>c.code);
    const common = ['CN','HK','TW','JP','KR','US','GB','FR','DE','IT','ES','RU','AU','CA','TH','MY','ID','SG','PH','IN','BR','MX'];
    // 常用国家置顶 + 其余按字母序, 避免翻几百条才找着中国
    const top = common.map(code=>rows.find(c=>c.code===code)).filter(Boolean);
    const rest = rows.filter(c=>!common.includes(c.code)).sort((a,b)=>a.name.localeCompare(b.name));
    sel.innerHTML = '<option value="">全部国家</option>'
      + [...top,...rest].map(c=>`<option value="${c.code}">${esc(c.name)}</option>`).join('');
    sel.value = [...sel.options].some(o=>o.value===cur) ? cur : '';
    if(sel.value && sel.value !== inst.country) inst.country = sel.value;
  }catch{}
  _syncTrendFilterUI(inst);
}
function _syncTrendFilterUI(inst){
  inst = inst || TREND;
  // 筛选模式下 trending 的日/周窗口不适用(后端走 discover 热度排序) → 禁用避免误导
  const w = $('#tWindow-'+inst.kind); if(!w) return;
  const filtered = !!(inst.genre || inst.country);
  w.disabled = filtered;
  w.title = filtered ? '选了类型/国家后按热度排序, 日榜/周榜不适用' : '';
}
async function loadTrendList(manual, k, opts){
  const inst = (k && (k==='tv'?TREND_TV:TREND_MOVIE)) || TREND;
  // 工具栏 onchange 直接传 opts: 写入 window/genre/country; reset=回第 1 页
  if(opts){
    if(opts.window!==undefined) inst.window = opts.window;
    if(opts.genre!==undefined) inst.genre = opts.genre;
    if(opts.country!==undefined) inst.country = opts.country;
    if(opts.reset){ inst.page = 1; _syncTrendFilterUI(inst); }
  }
  const kk = inst.kind;
  const el = $('#trendBody-'+kk); if(!el) return;
  const list = inst.list;
  if(inst.loading) return;              // 防重入: 滚动事件/手动刷新并发触发时忽略(根治"点好几次"的竞态)
  inst.loading = true;
  let _autoMore = false;   // "不满一屏需自动续"标记: 在 finally 释放锁后再消费(try 里直接递归会被防重入拦掉)
  const tok = ++inst.reqId;   // 请求令牌: 期间若发起更新请求(刷新/切榜单/自动刷新), 本次过期响应直接丢弃
  if(inst.page===1) el.innerHTML='<div class="empty"><span class="spin"></span>加载热门榜…</div>';
  else _setTrendFoot(el, '<span class="spin"></span>加载中…');   // 恒只一行(先清光再插, 杜绝并发叠加)
  try{
    const d = await api(`/api/trending/${inst.kind}?window=${inst.window}&page=${inst.page}&size=20&genre=${inst.genre||0}&country=${encodeURIComponent(inst.country||'')}`);
    if(tok !== inst.reqId) return;   // 过期响应 —— 不渲染(根治"旧响应把同页数据再追加一遍"的重复)
    const items = d.items||[];
    if(inst.page===1){
      list.length = 0; list.push(...items);
    } else {
      // 清掉所有旧指示行(⚠️ 必须全部清: 之前 querySelector 只删第一个,
      // 每页残留一行 → "每多一页多一个加载中"), 再按 tmdbId 去重追加
      _clearTrendFoot(el);
      const fresh = items.filter(it => !list.some(x => String(x.tmdbId)===String(it.tmdbId)));
      list.push(...fresh);
      items.length = fresh.length;
    }
    // 第 1 页整体重渲(包进 .grid 海报网格); 第 2 页起往同一个 grid 末尾追加
    const base = inst.page===1 ? 0 : list.length - items.length;
    const html = items.map((it,i)=>trendCard(it, base+i, base+i+1)).join('');
    if(inst.page===1){
      el.innerHTML = html ? `<div class="grid">${html}</div>` : trendEmptyMsg(inst);
    } else {
      (el.querySelector('.grid') || el).insertAdjacentHTML('beforeend', html);
    }
    inst.hasMore = !!d.hasMore;
    const _src = (inst.genre||inst.country) ? '热度榜·筛选' : `TMDB ${inst.window==='day'?'日榜':'周榜'}`;
    const _info = $('#trendInfo-'+kk);
    if(_info) _info.textContent = `${_src} · 已显示 ${list.length} 条${inst.hasMore?'':' · 已是榜单尾部'}`;
    // 底部提示行: 恒只保留一行(有旧行就原地改文案, 没有才追加) —— 杜绝逐页累加
    const _footHTML = inst.hasMore && items.length
      ? '<span class="spin"></span>滚动加载更多…'
      : (list.length ? '— 已显示全部 —' : '');
    _setTrendFoot(el, _footHTML);
    // 不满一屏(筛选结果少/大屏)时无法滚动 → 需要自动续下一页, 直到铺满或到底。
    // 判定必须是"列表确实没铺满视口"(scrollHeight<=clientHeight) —— 不能用
    // _trendNearBottom()(它在列表较短且停在顶部时恒为真, 会让滚动/每次渲染都误触发)。
    // (只在当前页签可见时续, 避免从其他页签/详情操作时在隐藏页签里空跑)
    // ⚠️ 不能在这里直接调 _trendNextPage(): 此刻 inst.loading 还是 true,
    // 递归的 loadTrendList 会被防重入检查拦掉 —— 置标记, 等 finally 释放锁后再续
    const _c = _trendScrollContainer();
    const _listShort = _c
      ? (el.scrollHeight <= _c.clientHeight + 40)
      : (document.documentElement.scrollHeight <= window.innerHeight + 40);
    if(inst.hasMore && items.length && _trendTabVisible(inst) && _listShort) _autoMore = true;
  }catch(e){
    if(tok !== inst.reqId) return;
    if(inst.page===1) el.innerHTML=`<div class="empty">加载失败: ${esc(e.message)}</div>`;
    else _clearTrendFoot(el);   // 失败也清光所有指示行(下一轮滚动会重新插)
  }finally{
    inst.loading = false;
    // 切回页签时若有在途旧请求占着锁, loadTrending 置了 _pendingFirst;
    // 此处释放锁后补一次首屏, 避免首屏卡死在"加载热门榜…"
    if(inst._pendingFirst){ inst._pendingFirst = false; inst.page = 1; loadTrendList(null, kk); }
  }
  if(_autoMore) _trendNextPage(kk);   // 锁已释放, 安全递归(每轮都是新的 loadTrendList 调用)
}
function trendEmptyMsg(inst){
  inst = inst || TREND;
  return inst.hideLib
    ? `<div class="empty">${icon('eye')} 隐藏"库内"已开启, 当前热门榜没有库外条目<br><span style="font-size:12px">到「管理 · 通用」关掉"隐藏已完整作品"可看全部热门</span></div>`
    : `<div class="empty">${icon('search')} 热门榜为空(未配置 TMDB key?)</div>`;
}
// 加载下一页。⚠️ page++ 必须在这里(旧 loadTrendMore 删除时曾丢失这一步,
// 导致滚动触发后 page 恒为 1 → 整页清空重渲, 用户看到"一上拉就刷新")。
// 调用方负责检查 inst.loading / inst.hasMore, 本函数不重复检查
// (loadTrendList 内部的"不满一屏自动续"在 loading 仍为 true 时调用, 不能查 loading)。
function _trendNextPage(k){
  const inst = (k && (k==='tv'?TREND_TV:TREND_MOVIE)) || TREND;
  inst.page++; loadTrendList(null, k);
}
// 清光榜单底部所有指示行(历史 bug: querySelector 只删第一个 → 每页残留一行)
function _clearTrendFoot(el){ el.querySelectorAll('.trend-foot').forEach(f=>f.remove()); }
// 统一设底部指示行: 先清光所有旧行, 再按内容插"至多一行"。
// 根治"滚动加载更多…"重复堆叠 —— 复用旧行改文案在并发/快速滚动下会漏建导致累加,
// 这里每次全清重建, 无论几路并发落地, 底部恒定 ≤1 行。空内容(到底/无)则不插。
function _setTrendFoot(el, html){
  _clearTrendFoot(el);
  if(html) el.insertAdjacentHTML('beforeend', `<div class="trend-foot">${html}</div>`);
}
// 找热门榜的滚动容器(#app 链上 overflow 为 auto/scroll 且内容超高者);
// 找不到(本应用 #app 无 overflow, 滚动实际在视口/documentElement) → 返回 null
function _trendScrollContainer(){
  let c = document.getElementById('app');
  while(c && c !== document.body){
    const ov = getComputedStyle(c).overflowY;
    if((ov==='auto'||ov==='scroll') && c.scrollHeight > c.clientHeight) return c;
    c = c.parentElement;
  }
  return null;
}
// 滚到接近底部 → 自动加载下一页(移动端上拉/桌面端滚动都走这); 不再需要手动点按钮
function _trendNearBottom(){
  const c = _trendScrollContainer();
  if(!c){
    const de = document.documentElement;
    return (window.innerHeight + window.scrollY) >= (de.scrollHeight - 400);
  }
  return (c.scrollTop + c.clientHeight) >= (c.scrollHeight - 400);
}
function pushTrendMagnet(i, btn){
  // 热门条目本身不带磁力链 → 进详情弹窗(会自动搜磁力列表, 在那里选链推送)
  const it = _trendList()[i]; if(it) openCard(it);
}
