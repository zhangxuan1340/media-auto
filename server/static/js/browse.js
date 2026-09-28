// MediaAuto 前端 — browse.js
// 浏览(2026-09 起并入「管理」页 → 「浏览」子页签): 本地 TMDB 缓存筛选
// 全局作用域(classic script), 依赖 common.js 工具函数 + trending.js 的滚动容器探测,
// 由 index.html 按序加载
//
// 2026-09 分页改造: 旧版只有「上一页」(第 1 页连下一页都没有), 且每次请求后端都要
// 重拉整表 tmdb_media —— 改成 48 张/页 + 滚到底自动续(与热门榜同一套滚动口径)。

let BROWSE = {kind:'tv', q:'', genre:0, year:0, status:'all', page:1, size:48,
              list:[], loading:false, hasMore:true, total:0, reqId:0};

async function loadBrowse(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="toolbar">
    <select onchange="BROWSE.kind=this.value;BROWSE.genre=0;BROWSE.year=0;loadBrowseFilters();loadBrowseData()">
      <option value="tv">剧集</option>
      <option value="movie">电影</option>
    </select>
    <select id="bStatus" onchange="BROWSE.status=this.value;loadBrowseData()">
      <option value="all">全部</option>
      <option value="inlibrary">库内</option>
      <option value="complete">完整</option>
      <option value="missing">未拥有</option>
    </select>
    <select id="bGenre" onchange="BROWSE.genre=+this.value;loadBrowseData()"><option value="0">全部类型</option></select>
    <select id="bYear" onchange="BROWSE.year=+this.value;loadBrowseData()"><option value="0">全部年份</option></select>
    <input id="bQ" type="text" placeholder="搜片名(回车)" style="padding:7px 10px;border:1px solid var(--line);border-radius:7px;font-size:13px;width:180px"
      onkeydown="if(event.key==='Enter'){BROWSE.q=this.value;loadBrowseData()}"/>
    <span id="bCount" style="color:var(--muted);font-size:12px"></span>
    <span style="flex:1"></span>
    <span style="color:var(--muted);font-size:12px">数据来自本地 TMDB 缓存 · 在「本地库」同步</span>
  </div><div id="browseBody"></div>`;
  $('#bGenre').value = '0';
  loadBrowseData();
  loadBrowseFilters();
}
async function loadBrowseFilters(){
  try{
    const gs = await api(`/api/browse/genres?kind=${BROWSE.kind}`);
    const sel = $('#bGenre'); if(!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="0">全部类型</option>' + gs.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('');
    sel.value = cur;
  }catch{}
  // 年份选项来自本地缓存里实际存在的年份(选哪个精准过滤哪个, 不会选到库里没有的年份)
  try{
    const ys = await api(`/api/browse/years?kind=${BROWSE.kind}`);
    const sel = $('#bYear'); if(!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="0">全部年份</option>' + (ys.years||[]).map(y=>`<option value="${y}">${y}</option>`).join('');
    // 当前选中的年份不在新列表里(切了 剧集/电影) → 重置为全部
    sel.value = [...sel.options].some(o=>o.value===cur) ? cur : '0';
    if(sel.value !== cur){ BROWSE.year = +sel.value; }
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
    const d = await api(`/api/browse?kind=${BROWSE.kind}&q=${encodeURIComponent(BROWSE.q)}&genre=${BROWSE.genre}&year=${BROWSE.year}&status=${BROWSE.status}&page=${BROWSE.page}&size=${BROWSE.size}`);
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
window.addEventListener('scroll', ()=>{
  if(MANAGE_SUB !== 'browse') return;
  const mb = document.getElementById('modalBg');
  if(mb && mb.classList.contains('show')) return;
  if(!BROWSE.list.length || !BROWSE.hasMore || BROWSE.loading) return;
  if(typeof _trendNearBottom === 'function' && _trendNearBottom()) browseMore();
}, {passive:true});
