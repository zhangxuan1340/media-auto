// MediaAuto 前端 — browse.js
// 浏览(2026-09 起并入「管理」页 → 「浏览」子页签): 本地 TMDB 缓存筛选
// 全局作用域(classic script), 依赖 common.js 工具函数 + trending.js 的滚动容器探测、
// _trendOpts(下拉选项 HTML), 由 index.html 按序加载
//
// 2026-09 分页改造: 旧版只有「上一页」(第 1 页连下一页都没有), 且每次请求后端都要
// 重拉整表 tmdb_media —— 改成 48 张/页 + 滚到底自动续(与热门榜同一套滚动口径)。
//
// 2026-10-02 高级筛选 + 状态回显修复:
//   - 新增 年份范围(起/止)、剧集完结状态、分级 —— 全走本地 tmdb_media, 零网络;
//   - ⚠️ BROWSE 是模块级状态, 切子页签回来会重建工具栏: 值必须**从 BROWSE 回填**,
//     不能读 DOM 现值(重建出来的都是"全部")。旧代码读 DOM → 数据仍是筛过的、
//     筛选框却显示"全部", 根本看不出筛没筛(2026-10-02 用户反馈的 bug)。
//     同理, 选回"全部"时状态要同步清零(旧代码只在非空时写状态)。

let BROWSE = {kind:'tv', q:'', genre:0, yearFrom:0, yearTo:0, cstatus:'', cert:'',
              status:'all', page:1, size:48,
              list:[], loading:false, hasMore:true, total:0, reqId:0, certs:[]};

// 库内可用性(与后端 status 参数同口径)
const BROWSE_STATUS = [['all','全部'],['inlibrary','库内'],['complete','完整'],['missing','未拥有']];
// 剧集完结状态(本地 tmdb_media.status / in_production 列)
const BROWSE_CSTATUS = [['ended','已完结'],['returning','在播'],['canceled','已取消']];

// 换 kind: 类型/年份/分级/完结状态在另一个 kind 下不一定存在 → 一并清零再重建
function _browseKind(v){
  BROWSE.kind = v;
  BROWSE.genre = 0; BROWSE.yearFrom = 0; BROWSE.yearTo = 0;
  BROWSE.cstatus = ''; BROWSE.cert = ''; BROWSE.q = '';
  loadBrowse();
}
// 一键清空所有筛选(片名/类型/年份范围/完结状态/分级/库内状态)
function _browseReset(){
  Object.assign(BROWSE, {q:'', genre:0, yearFrom:0, yearTo:0, cstatus:'', cert:'', status:'all'});
  loadBrowse();
}

async function loadBrowse(){
  const el = $('#manageBody');
  const k = BROWSE.kind;
  el.innerHTML = `<div class="toolbar">
    <select id="bKind" onchange="_browseKind(this.value)">
      <option value="tv"${k==='tv'?' selected':''}>剧集</option>
      <option value="movie"${k==='movie'?' selected':''}>电影</option>
    </select>
    <select id="bStatus" onchange="BROWSE.status=this.value;loadBrowseData()">${_trendOpts(BROWSE_STATUS, BROWSE.status, '')}</select>
    <select id="bGenre" onchange="BROWSE.genre=+this.value;loadBrowseData()"><option value="0">全部类型</option></select>
    <select id="bYearFrom" aria-label="起始年" onchange="BROWSE.yearFrom=+this.value;loadBrowseData()"><option value="0">起始年</option></select>
    <select id="bYearTo" aria-label="截止年" onchange="BROWSE.yearTo=+this.value;loadBrowseData()"><option value="0">截止年</option></select>
    ${k==='tv' ? `<select id="bCstatus" aria-label="完结状态" onchange="BROWSE.cstatus=this.value;loadBrowseData()">${_trendOpts(BROWSE_CSTATUS, BROWSE.cstatus, '全部状态')}</select>` : ''}
    <select id="bCert" aria-label="分级" onchange="BROWSE.cert=this.value;loadBrowseData()"><option value="">全部分级</option></select>
    <input id="bQ" type="text" placeholder="搜片名(回车)" value="${esc(BROWSE.q||'')}" style="padding:7px 10px;border:1px solid var(--line);border-radius:7px;font-size:13px;width:180px"
      onkeydown="if(event.key==='Enter'){BROWSE.q=this.value;loadBrowseData()}"/>
    <button class="ghost" onclick="_browseReset()" title="清空片名/类型/年份/状态/分级">重置</button>
    <span id="bCount" style="color:var(--muted);font-size:12px"></span>
    <span style="flex:1"></span>
    <span style="color:var(--muted);font-size:12px">数据来自本地 TMDB 缓存 · 在「本地库」同步</span>
  </div><div id="browseBody"></div>`;
  loadBrowseData();
  loadBrowseFilters();
}
async function loadBrowseFilters(){
  // ⚠️ 选项到位后一律把值从 BROWSE 回填(不读 DOM), 且"选项里没有"要同步清零
  try{
    const gs = await api(`/api/browse/genres?kind=${BROWSE.kind}`);
    const sel = $('#bGenre');
    if(sel){
      sel.innerHTML = '<option value="0">全部类型</option>' + gs.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('');
      sel.value = String(BROWSE.genre || 0);
      if(sel.value !== String(BROWSE.genre || 0)) BROWSE.genre = 0;
      else BROWSE.genre = +sel.value;
    }
  }catch{}
  // 年份选项来自本地缓存里实际存在的年份(选哪个精准过滤哪个, 不会选到库里没有的年份)
  try{
    const ys = await api(`/api/browse/years?kind=${BROWSE.kind}`);
    const rows = (ys.years||[]).map(y=>[String(y), String(y)]);
    [['bYearFrom','起始年'],['bYearTo','截止年']].forEach(([id, all])=>{
      const sel = $('#'+id); if(!sel) return;
      sel.innerHTML = `<option value="0">${all}</option>` + _trendOpts(rows, id==='bYearFrom'?BROWSE.yearFrom:BROWSE.yearTo, '');
      if(id==='bYearFrom'){ sel.value = String(BROWSE.yearFrom||0); BROWSE.yearFrom = +sel.value || 0; }
      else { sel.value = String(BROWSE.yearTo||0); BROWSE.yearTo = +sel.value || 0; }
    });
  }catch{}
  // 分级: 本地 tmdb_media.certification 里出现过的值(剧集分级唯一可靠口径 ——
  // TMDB 发现榜没有剧集分级参数)
  try{
    const cs = await api(`/api/browse/certs?kind=${BROWSE.kind}`);
    BROWSE.certs = cs.certs || [];
    const sel = $('#bCert');
    if(sel && BROWSE.certs.length){
      sel.innerHTML = _trendOpts(BROWSE.certs.map(c=>[c,c]), BROWSE.cert, '全部分级');
      sel.value = BROWSE.cert || '';
      if(sel.value !== (BROWSE.cert || '')) BROWSE.cert = '';
    }
  }catch{}
}

// 首页/筛选变化: 清空重铺
async function loadBrowseData(){
  const el = $('#browseBody'); if(!el) return;
  BROWSE.page = 1; BROWSE.total = 0; BROWSE.loading = false;
  BROWSE.hasMore = true; BROWSE.reqId++;
  BROWSE.list.length = 0;
  CARD_LIST = BROWSE.list;                 // 卡片点击 → openCardIdx(i) 走同一数组
  el.innerHTML = '<div class="empty"><span class="spin"></span>加载…</div>';
  await browseFetch();
}
// 滚到底/点「加载更多」: 追加下一页
async function browseMore(){ await browseFetch(); }

async function browseFetch(){
  if(BROWSE.loading || !BROWSE.hasMore) return;
  BROWSE.loading = true;
  const myReq = BROWSE.reqId;
  const el = $('#browseBody');
  try{
    const q = new URLSearchParams();
    q.set('kind', BROWSE.kind); q.set('q', BROWSE.q || '');
    q.set('genre', String(BROWSE.genre || 0));
    if(BROWSE.yearFrom) q.set('year_from', String(BROWSE.yearFrom));
    if(BROWSE.yearTo) q.set('year_to', String(BROWSE.yearTo));
    if(BROWSE.cstatus) q.set('cstatus', BROWSE.cstatus);
    if(BROWSE.cert) q.set('cert', BROWSE.cert);
    q.set('status', BROWSE.status);
    q.set('page', String(BROWSE.page)); q.set('size', String(BROWSE.size));
    const d = await api(`/api/browse?${q.toString()}`);
    if(myReq !== BROWSE.reqId) return;                  // 已重置/切筛选 → 丢弃过期响应
    const items = d.items || [];
    BROWSE.total = d.total || 0;
    const first = !BROWSE.list.length;
    const base = BROWSE.list.length;
    items.forEach(x=>BROWSE.list.push(x));
    BROWSE.page++;
    BROWSE.hasMore = items.length > 0 && BROWSE.list.length < BROWSE.total;
    const cnt = $('#bCount');
    if(cnt) cnt.textContent = `共 ${BROWSE.total} 部 · 已显示 ${BROWSE.list.length} 部`;
    if(first && !items.length){ el.innerHTML = '<div class="empty">没有匹配的作品</div>'; BROWSE.hasMore = false; return; }
    if(first){
      el.innerHTML = `<div class="grid" id="bGrid"></div><div id="bFoot"></div>`;
      $('#bGrid').innerHTML = items.map((it,i)=>cardHTML(it, base+i)).join('');
    }else{
      const g = $('#bGrid');
      if(g) g.insertAdjacentHTML('beforeend', items.map((it,i)=>cardHTML(it, base+i)).join(''));
    }
    _browseFoot();
  }catch(e){
    if(myReq !== BROWSE.reqId) return;
    if(!BROWSE.list.length) el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
    else _browseFoot(true);
  }finally{
    if(myReq === BROWSE.reqId) BROWSE.loading = false;
    _browseMaybeMore();
  }
}

function _browseFoot(err){
  const f = document.getElementById('bFoot'); if(!f) return;
  if(err){
    f.innerHTML = '<div class="empty" style="padding:10px"><button class="ghost sm" onclick="browseMore()">加载失败, 点此重试</button></div>';
    return;
  }
  f.innerHTML = !BROWSE.hasMore
    ? (BROWSE.list.length ? '<div style="text-align:center;color:var(--muted);font-size:12px;padding:12px 0">— 已显示全部 —</div>' : '')
    : '<div style="text-align:center;color:var(--muted);font-size:12px;padding:12px 0"><span class="spin"></span> 滚动加载更多…'
      + '<button class="ghost sm" style="margin-left:8px" onclick="browseMore()">加载更多</button></div>';
}

// 不满一屏(筛选结果少/大屏)滚不动 → 继续续页, 直到铺满或到底
function _browseMaybeMore(){
  if(!BROWSE.hasMore || BROWSE.loading || !BROWSE.list.length) return;
  if(!document.getElementById('bGrid')) return;
  if(MANAGE_SUB !== 'browse') return;
  const c = (typeof _trendScrollContainer === 'function') ? _trendScrollContainer() : null;
  const short = c ? (document.getElementById('bGrid').parentElement.scrollHeight <= c.clientHeight + 40)
                  : (document.documentElement.scrollHeight <= window.innerHeight + 40);
  if(short) browseMore();
}

// 滚到底 → 自动续下一页(与热门榜共用滚动容器探测; 详情弹窗打开时不触发)
// rAF 节流: 一帧最多处理一次(惯性滚动会把 scroll 打到每帧多次)
let _bScrollRaf = 0;
window.addEventListener('scroll', ()=>{
  if(_bScrollRaf) return;
  _bScrollRaf = requestAnimationFrame(()=>{
    _bScrollRaf = 0;
    if(MANAGE_SUB !== 'browse') return;
    const mb = document.getElementById('modalBg');
    if(mb && mb.classList.contains('show')) return;
    if(!BROWSE.list.length || !BROWSE.hasMore || BROWSE.loading) return;
    if(typeof _trendNearBottom === 'function' && _trendNearBottom()) browseMore();
  });
}, {passive:true});
