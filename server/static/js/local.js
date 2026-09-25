// MediaAuto 前端 — local.js
// 本地库(2026-09 起并入「设置」页 → 「本地库」子页签): TMDB/Jellyfin 同步 + 缓存/库/项浏览
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

// ---- 本地库(Jellyfin / TMDB 同步状态与浏览) ----
// 2026-09-21: 原先 4 个零散同步按钮已收敛 —— 同步统一由「作业」子页签管理
// (见 jobs.js / lib/jobs.py), 这里只保留"看状态 + 看同步记录"。
function loadLocal(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="toolbar">
      <button class="mbtn ghost" onclick="renderManage('jobs')">${icon('clock')}手动执行 / 改周期 →「作业」</button>
      <button class="mbtn ghost" onclick="loadSyncLogs()">${icon('refresh')}同步记录</button>
    </div>
    <div id="syncLogs" class="logbox"></div>
    <nav class="subtabs" id="localSubtabs">
      <button data-ls="tmdb" class="active" onclick="switchLocal('tmdb')">TMDB 缓存</button>
      <button data-ls="libraries" onclick="switchLocal('libraries')">Jellyfin 库</button>
      <button data-ls="items" onclick="switchLocal('items')">Jellyfin 项</button>
    </nav>
    <div id="localBody"></div>`;
  loadSyncLogs();
  switchLocal('tmdb');
}
function switchLocal(sub){
  document.querySelectorAll('#localSubtabs button').forEach(b=>b.classList.toggle('active', b.dataset.ls===sub));
  if(sub==='tmdb') loadLocalTmdb();
  else if(sub==='libraries') loadLocalLibraries();
  else if(sub==='items') loadLocalItems();
}
async function loadLocalTmdb(){
  const el = $('#localBody'); el.innerHTML='<div class="empty"><span class="spin"></span>读取本地 TMDB 缓存…</div>';
  try{
    const tv = await api('/api/browse?kind=tv&size=1');
    const mv = await api('/api/browse?kind=movie&size=1');
    el.innerHTML = `<div class="toolbar">
      <span style="font-size:13px">本地 TMDB 缓存: 剧集 <b>${tv.total}</b> 部 · 电影 <b>${mv.total}</b> 部</span>
    </div>
    <div style="background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;font-size:13px;line-height:1.9">
      <b>同步是全自动的</b> —— 不要再手动按顺序点按钮了。<br>
      后台按「<b>作业</b>」里的周期运行: <b>Jellyfin 最近新增扫描</b>(每 5 分钟)负责新入库作品与
      新分集, <b>Jellyfin 全库扫描</b>(每日 03:00)兜底重建, <b>同步媒体可用性</b>(每日 05:00)
      把已删除的标记出库, <b>TMDB 元数据同步</b>(每日 03:30)补详情/演员/分集结构。<br>
      <span style="color:var(--muted)">需要立刻跑一次、或想改周期, 都去
      <b>管理 → 作业</b>; 那里还有缓存命中统计。<br>
      未配置 TMDB key 时: TMDB 同步会提示去 themoviedb.org 免费注册获取。</span>
    </div>`;
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}
async function loadSyncLogs(){
  const el = $('#syncLogs'); if(!el) return;
  try{
    const logs = await api('/api/sync/logs?limit=10');
    if(!logs.length){ el.innerHTML='<div class="empty" style="padding:18px">尚无同步记录,点上方按钮开始同步</div>'; return; }
    el.innerHTML = '<table><thead><tr><th>来源</th><th>范围</th><th>状态</th><th>条目</th><th>开始</th><th>结束</th></tr></thead><tbody>'
      + logs.map(l=>`<tr><td data-th="来源">${esc(l.source)}</td><td data-th="范围">${esc(l.scope)}</td><td data-th="状态">${statusBadge(l.status)}</td><td data-th="条目">${l.items_synced}</td><td data-th="开始">${fmtTime(l.started_at)}</td><td data-th="结束">${fmtTime(l.finished_at)}</td></tr>`).join('')
      + '</tbody></table>';
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}
async function loadLocalLibraries(){
  const el = $('#localBody'); el.innerHTML='<div class="empty"><span class="spin"></span>读取本地 Jellyfin 媒体库…</div>';
  try{
    const rows = await api('/api/local/libraries');
    LIBRARIES = rows||[];
    if(!rows.length){ el.innerHTML='<div class="empty">本地库暂无 Jellyfin 媒体库,先点「同步 Jellyfin」</div>'; return; }
    el.innerHTML = `<div class="toolbar">共 ${rows.length} 个媒体库</div><table><thead><tr><th>名称</th><th>类型</th><th>影片数</th><th>路径</th></tr></thead><tbody>`
      + rows.map(r=>`<tr><td data-th="名称">${esc(r.name)}</td><td data-th="类型">${esc(r.type)}</td><td data-th="影片数">${r.item_count}</td><td data-th="路径"><small style="color:var(--muted)">${esc((r.locations||'').split('\n')[0])}</small></td></tr>`).join('')
      + '</tbody></table>';
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}
async function loadLocalItems(){
  const el = $('#localBody'); el.innerHTML='<div class="empty"><span class="spin"></span>读取本地 Jellyfin 媒体项…</div>';
  try{
    const libOpts = `<option value="">全部媒体库</option>` + LIBRARIES.map(l=>`<option value="${l.library_id}">${esc(l.name)}</option>`).join('');
    const rows = await api('/api/local/items?limit=500');
    if(!rows.length){ el.innerHTML='<div class="empty">本地库暂无 Jellyfin 媒体项,先点「同步 Jellyfin」</div>'; return; }
    el.innerHTML = `<div class="toolbar">共 ${rows.length} 个媒体项
        <select id="libFilter" onchange="filterItems()">${libOpts}</select></div>
        <table><thead><tr><th>名称</th><th>类型</th><th>年份</th><th>TMDB</th><th>IMDB</th></tr></thead><tbody id="itemsTbody">`
      + rows.map(r=>`<tr data-lib="${esc(r.library_id)}"><td data-th="名称">${esc(r.name)}</td><td data-th="类型">${esc(r.type)}</td><td data-th="年份">${r.year||''}</td><td data-th="TMDB">${esc(r.tmdb_id)}</td><td data-th="IMDB">${esc(r.imdb_id)}</td></tr>`).join('')
      + '</tbody></table>';
    el._allItems = rows;
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}
function filterItems(){
  const f = $('#libFilter').value;
  const rows = $('#localBody')._allItems||[];
  const tbody = $('#itemsTbody');
  if(!tbody) return;
  const shown = rows.filter(r=> !f || r.library_id===f);
  tbody.innerHTML = shown.map(r=>`<tr><td data-th="名称">${esc(r.name)}</td><td data-th="类型">${esc(r.type)}</td><td data-th="年份">${r.year||''}</td><td data-th="TMDB">${esc(r.tmdb_id)}</td><td data-th="IMDB">${esc(r.imdb_id)}</td></tr>`).join('');
}

