// MediaAuto 前端 — browse.js
// 浏览(2026-09 起并入「设置」页 → 「浏览」子页签): 本地 TMDB 缓存筛选
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

// 浏览(本地 TMDB 缓存)
let BROWSE = {kind:'tv', q:'', genre:0, year:0, status:'all', page:1};
async function loadBrowse(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="toolbar">
    <select onchange="BROWSE.kind=this.value;BROWSE.genre=0;BROWSE.year=0;BROWSE.page=1;loadBrowseFilters();loadBrowseData()">
      <option value="tv">剧集</option><option value="movie">电影</option>
    </select>
    <select id="bStatus" onchange="BROWSE.status=this.value;BROWSE.page=1;loadBrowseData()">
      <option value="all">全部</option><option value="inlibrary">库内</option>
      <option value="complete">完整</option><option value="missing">未拥有</option>
    </select>
    <select id="bGenre" onchange="BROWSE.genre=+this.value;BROWSE.page=1;loadBrowseData()"><option value="0">全部类型</option></select>
    <select id="bYear" onchange="BROWSE.year=+this.value;BROWSE.page=1;loadBrowseData()"><option value="0">全部年份</option></select>
    <input id="bQ" type="text" placeholder="搜片名(回车)" style="padding:7px 10px;border:1px solid var(--line);border-radius:7px;font-size:13px;width:180px"
      onkeydown="if(event.key==='Enter'){BROWSE.q=this.value;BROWSE.page=1;loadBrowseData()}"/>
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
async function loadBrowseData(){
  const el = $('#browseBody'); if(!el) return;
  el.innerHTML='<div class="empty"><span class="spin"></span>加载…</div>';
  try{
    const d = await api(`/api/browse?kind=${BROWSE.kind}&q=${encodeURIComponent(BROWSE.q)}&genre=${BROWSE.genre}&year=${BROWSE.year}&status=${BROWSE.status}&page=${BROWSE.page}`);
    $('#bCount').textContent = `共 ${d.total} 部 · 第 ${d.page} 页`;
    if(!d.items.length){ el.innerHTML='<div class="empty">没有匹配的作品</div>'; return; }
    CARD_LIST = d.items;
    el.innerHTML = `<div class="grid">${d.items.map((it,i)=>cardHTML(it,i)).join('')}</div>`
      + (d.page>1?`<div class="toolbar" style="justify-content:center"><button class="ghost" onclick="BROWSE.page--;loadBrowseData()">上一页</button></div>`:'');
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}

