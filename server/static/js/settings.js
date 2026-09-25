// MediaAuto 前端 — settings.js
// 管理页(2026-09 二版): 原「设置」改名「管理」(更通用), 并把原「缺失」页并入为第一个子页签。
// 子页签: 缺失 / 通用 / 追踪 / 本地库 / 浏览 / 屏蔽。
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载。
// 各子页签的实际渲染函数分散在 missing.js/block.js/track.js/local.js/browse.js,
// 这里只做"容器骨架 + 子页签切换路由", 不重复实现业务逻辑。

let MANAGE_SUB = 'missing';   // 当前管理子页签(默认「缺失」: 它是高频入口, 且是原独立页签)
const MANAGE_TABS = [
  ['missing',   '缺失',     'alert'],
  ['general',   '通用',     'sliders'],
  ['category',  '分类规则', 'folder'],
  ['download',  '下载',     'download'],
  ['track',     '追踪',     'bell'],
  ['local',     '本地库',   'database'],
  ['jobs',      '作业',     'clock'],
  ['browse',    '浏览',     'movie'],
  ['block',     '屏蔽',     'ban'],
];

// 进入「管理」页签: 铺子页签导航 + 渲染指定子页签(sub 缺省用当前 MANAGE_SUB)
function renderManage(sub){
  const el = $('#tab-manage');
  if(sub && MANAGE_TABS.some(([k])=>k===sub)) MANAGE_SUB = sub;
  el.innerHTML = `
    <nav class="subtabs" id="manageSubtabs">
      ${MANAGE_TABS.map(([k,label,ic])=>`<button data-sub="${k}" class="${k===MANAGE_SUB?'active':''}" onclick="switchManageSub('${k}')">${icon(ic)}${label}</button>`).join('')}
    </nav>
    <div id="manageBody" class="settings-body"></div>`;
  switchManageSub(MANAGE_SUB);
}
function switchManageSub(sub){
  MANAGE_SUB = sub;
  const nav = $('#manageSubtabs');
  if(nav) nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active', b.dataset.sub===sub));
  const body = $('#manageBody');
  if(!body) return;
  // 内容切换动画(重触发 fadeUp)
  body.classList.remove('tab-anim'); void body.offsetWidth; body.classList.add('tab-anim');
  // 下载子页签有自动轮询, 切走时停掉(由 downloads.js 暴露的钩子)
  if(window._dlOnSubChange) window._dlOnSubChange(sub);
  // 各子页签委托给对应模块的顶层渲染函数(它们各自渲染完整块到 #manageBody)
  if(sub==='missing') loadMissing();
  else if(sub==='general') renderGeneral();
  else if(sub==='category') loadCategoryRules();
  else if(sub==='download') loadDownloads(false);
  else if(sub==='track') loadTrack();
  else if(sub==='local') loadLocal();
  else if(sub==='jobs') loadJobs();
  else if(sub==='browse') loadBrowse();
  else if(sub==='block') loadBlock();
}

// ---- 分类规则: 分类键 → 目录名 映射(原 config.json 手改, 现管理页可视化) ----
// 分类键由 lib/classify.py 级联引擎生成, 键不可增删, 只能改目录名 + 媒体库根。
// 保存写回 config.json(mtime 热加载, 无需重启, 下次整理立即生效)。
// ⚠️ 改目录名【不迁移已有内容】: 新目录在下次整理时自动创建, 已归位内容需自行移动。
let _catData = null;   // {cloud_root, existing_checkable, rows:[{key,label,group,desc,folder,exists}]}
let _catDirty = {};    // key -> 新目录名(未保存的 diff)
async function loadCategoryRules(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="cat-rule"><div class="empty"><span class="spin"></span>加载分类规则…</div></div>`;
  try{
    _catData = await api('/api/organize/categories');
    _catDirty = {};
    _catRender();
  }catch(e){
    _catData = null;
    el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
}
function _catRoot(){ return (_catData && _catData.cloud_root) || '/Cloud'; }
function _catFolder(r){ return (_catDirty[r.key] || r.folder) || ''; }
function _catRow(r){
  const dirty = !!_catDirty[r.key] && _catDirty[r.key] !== r.folder;
  const exists = r.exists === true ? '<span class="badge ok">库内已有</span>'
    : r.exists === false ? '<span class="badge">未创建</span>'
    : '<span class="badge">未知</span>';
  const cur = _catFolder(r);
  const infoIco = `<svg class="icn" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"${r.desc?` title="${esc(r.desc)}"`:''}><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>`;
  return `<tr class="${dirty?'cat-dirty':''}">
    <td data-th="分类" class="cat-name"><b>${esc(r.label)} ${infoIco}</b><small>${esc(r.key)}</small></td>
    <td data-th="目录名"><input class="cat-in" type="text" value="${esc(cur)}" data-key="${r.key}" placeholder="${esc(r.folder)}" maxlength="64" autocomplete="off" oninput="_catInput(this)"></td>
    <td data-th="目标路径"><code class="cat-path">${esc(_catRoot())}/${esc(cur)}</code>${dirty?`<small class="cat-warn">${icon('alert')}旧目录 ${esc(r.folder)} 将不再接收新内容</small>`:''}</td>
    <td data-th="库内状态">${exists}</td></tr>`;
}
function _catInput(inp){
  const r = (_catData.rows||[]).find(x=>x.key===inp.dataset.key); if(!r) return;
  const v = inp.value.trim();
  if(v && v !== r.folder) _catDirty[r.key] = v;
  else delete _catDirty[r.key];
  // 只更新本行的 dirty 视觉(整表重绘会丢输入焦点)
  const tr = inp.closest('tr');
  if(tr){
    tr.classList.toggle('cat-dirty', !!_catDirty[r.key] && _catDirty[r.key] !== r.folder);
    const w = tr.querySelector('.cat-warn');
    if(!_catDirty[r.key] && w) w.remove();
    else if(_catDirty[r.key] && !w){
      const p = tr.querySelector('.cat-path');
      if(p) p.insertAdjacentHTML('afterend', `<small class="cat-warn">${icon('alert')}旧目录 ${esc(r.folder)} 将不再接收新内容</small>`);
    }
    const cp = tr.querySelector('.cat-path');
    if(cp) cp.textContent = `${_catRoot()}/${v||r.folder}`;
  }
  _catUpdateBar();
}
function _catUpdateBar(){
  const bar = $('#catBar'); if(!bar) return;
  const n = Object.keys(_catDirty).length;
  bar.style.display = n ? 'flex' : 'none';
  const m = bar.querySelector('.cat-barmsg');
  if(m) m.innerHTML = `${icon('alert')}${n} 处变更未保存 — 保存后对下次整理生效(无需重启)`;
}
function _catRender(){
  const el = $('#manageBody');
  if(!_catData) return;
  const groups = [['special','特殊类型 · 按优先级命中'],['region','按国家/地区']];
  let html = `<div class="cat-rule">
    <h3 class="set-h">分类 → 目录映射</h3>
    <p class="cat-note">内容先按「类型优先」级联判定分类, 再归位到 <code>${esc(_catRoot())}</code> 下的对应目录。
      目录名可直接编辑(回车无效, 需点保存); <b>改名不迁移已有内容</b> —— 新目录在下次整理时自动创建, 已归位的媒体需自行移动。
      ${!_catData.existing_checkable?'<span class="badge warn">云盘不可达, 库内状态暂无法检测</span>':''}</p>`;
  for(const [g, title] of groups){
    const rows = _catData.rows.filter(r=>r.group===g);
    html += `<table><tbody><tr class="cat-group"><td colspan="4" data-th="">${icon('folder')}${title} <small>(${rows.length})</small></td></tr>`
      + rows.map(_catRow).join('') + '</tbody></table>';
  }
  html += `<div class="cat-bar" id="catBar" style="display:none">
      <span class="cat-barmsg"></span>
      <button class="ghost sm" onclick="catReset()">重置</button>
      <button class="mbtn sm" id="catSaveBtn" onclick="catSave()">${icon('check')}保存变更</button>
    </div></div>`;
  el.innerHTML = html;
  _catUpdateBar();
}
function catReset(){
  _catDirty = {};
  if(_catData) _catRender();
}
async function catSave(){
  if(!_catData) return;
  const btn = $('#catSaveBtn'); if(btn) btn.disabled = true;
  try{
    const r = await api('/api/organize/categories', {method:'PUT', body: JSON.stringify({categories: _catDirty})});
    toast(r.msg || '已保存');
    await loadCategoryRules();
  }catch(e){
    toast('保存失败: ' + e.message);
    if(btn) btn.disabled = false;
  }
}

// ---- 通用: 全局开关(隐藏完整/图片缓存/S0 缺失检测) —— 自包含, 无独立模块 ----
async function renderGeneral(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="settings">
    <div class="setting" style="cursor:default">
      <span class="st-main">
        <span class="st-name">夜间模式</span>
        <span class="st-desc">跟随系统: 每天 19:00–07:00 自动切换夜间; 也可手动锁定浅色或深色</span>
      </span>
      <span class="tier-seg" id="themeSeg" style="margin-top:4px">
        <button data-pref="auto" onclick="setTheme('auto')">跟随</button>
        <button data-pref="light" onclick="setTheme('light')">浅色</button>
        <button data-pref="dark" onclick="setTheme('dark')">深色</button>
      </span>
    </div>
    <label class="setting">
      <input type="checkbox" id="hideComplete" onchange="toggleHideComplete(this.checked)">
      <span class="st-main">
        <span class="st-name">隐藏已完整作品</span>
        <span class="st-desc">开启: 已完整拥有的电影/剧集不在浏览、缺失列表显示</span>
      </span>
    </label>
    <label class="setting">
      <input type="checkbox" id="imageCache" onchange="toggleImageCache(this.checked)">
      <span class="st-main">
        <span class="st-name">图片本地缓存</span>
        <span class="st-desc">开启: 封面/演员图经本地缓存(未变不重复下载, 省服务器带宽); 关闭: 直连 TMDB</span>
      </span>
    </label>
    <label class="setting">
      <input type="checkbox" id="checkMissingS0" onchange="toggleCheckMissingS0(this.checked)">
      <span class="st-main">
        <span class="st-name">S0 特别篇计入缺失检测</span>
        <span class="st-desc">开启: 特别篇(S00)缺的集也算作品级缺失; 关闭(默认): 只算正剧季, 特别篇单独显示但不计缺失</span>
      </span>
    </label>
  </div>
  <div class="set-about">
    <div class="about-row"><span>应用</span><b>MediaAuto</b></div>
    <div class="about-row"><span>版本</span><b>1.2 · PWA</b></div>
    <div class="about-row"><span>说明</span><b>影音自动化控制台 · 对齐 iOS 26 / macOS 26 设计语言</b></div>
  </div>`;
  // 夜间模式分段高亮当前选择(默认 跟随系统)
  const tseg = $('#themeSeg');
  if(tseg) tseg.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.pref === THEME_PREF));
  try{
    const st = await api('/api/settings');
    $('#hideComplete').checked = st.hide_complete;
    $('#imageCache').checked = st.image_cache;
    $('#checkMissingS0').checked = !!st.check_missing_s0;
    IMG_CACHE = st.image_cache;
  }catch(e){ /* 开关初始化失败不阻塞 */ }
}
