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

// ---- 分类规则: 分类键 → 目录名 映射(现管理页可视化) ----
// 分类键两类: 特殊类型键(Dm/Jl/Xr/Sp/Mu)固定, 只能改目录名; 地区键 = <地区档>Movie/Show,
// 随下面的「地区档」表增删改。保存写回数据库 app_config(热加载, 无需重启, 下次整理生效)。
// ⚠️ 改目录名/改地区档【不迁移已有内容】: 新目录在下次整理时自动创建, 已归位内容需自行移动。
let _catData = null;   // {cloud_root, existing_checkable, rows:[...], regions:{order,items}}
let _catDirty = {};    // key -> 新目录名(未保存的 diff)
async function loadCategoryRules(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="cat-rule"><div class="empty"><span class="spin"></span>加载分类规则…</div></div>`;
  try{
    _catData = await api('/api/organize/categories');
    _catDirty = {};
    _rgInit();
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
  const nc = Object.keys(_catDirty).length;
  const rg = _rgChanged();
  const n = nc + (rg ? 1 : 0);
  bar.style.display = n ? 'flex' : 'none';
  const m = bar.querySelector('.cat-barmsg');
  if(m){
    const parts = [];
    if(nc) parts.push(`${nc} 个目录名`);
    if(rg) parts.push('地区档');
    m.innerHTML = `${icon('alert')}${parts.join(' + ')} 变更未保存 — 保存后对下次整理生效(无需重启)`;
  }
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
  html += _rgRender();
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
  _rgInit();
  if(_catData) _catRender();
}
async function catSave(){
  if(!_catData) return;
  const btn = $('#catSaveBtn'); if(btn) btn.disabled = true;
  try{
    const body = {categories: _catDirty};
    if(_rgChanged()){
      body.regions = _rgWork;
      // 地区档变了 → 分类键集合跟着变; 为将消失的键改的目录名没有意义, 一并丢掉
      const valid = new Set(['DmMovie','DmShow','JlShow','XrShow','SpShow','MuShow'].concat(_rgKeys()));
      const keep = {};
      Object.entries(_catDirty).forEach(([k, v]) => { if(valid.has(k)) keep[k] = v; });
      body.categories = keep;
    }
    const r = await api('/api/organize/categories', {method:'PUT', body: JSON.stringify(body)});
    toast(r.msg || '已保存');
    await loadCategoryRules();
  }catch(e){
    toast('保存失败: ' + e.message);
    if(btn) btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 地区档(上面那张表): 归属可改 + 可新增/删除档 —— 每档生成 <键>Movie / <键>Show 两个分类键
// 判定顺序(后端 lib.classify._region): 关键词 → 优先国家 → 语言 → 国家 → 兜底 Ot
// ---------------------------------------------------------------------------
let _rgWork = null;      // {order:[...], items:{...}} 未保存的工作副本(服务端规范化过的)
let _rgNew = new Set();  // 本次会话新增、还没保存的档(键可编辑; 保存后就变成普通档)
const _RG_SPECIAL = ['Dm', 'Jl', 'Xr', 'Sp', 'Mu'];
const _RG_KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,14}$/;

function _rgClone(o){ try { return JSON.parse(JSON.stringify(o)); } catch { return null; } }
function _rgInit(){
  _rgWork = (_catData && _catData.regions && _catData.regions.items)
    ? _rgClone(_catData.regions) : null;
  _rgNew = new Set();
}
function _rgChanged(){
  if(!_rgWork) return false;
  const base = (_catData && _catData.regions) || null;
  return JSON.stringify(_rgWork) !== JSON.stringify(base);
}
function _rgKeys(){   // 当前工作副本会生成的分类键(地区部分)
  const out = [];
  if(_rgWork) for(const k of _rgWork.order) out.push(k + 'Movie', k + 'Show');
  return out;
}
function _rgPrioText(prio){
  return Object.entries(prio || {})
    .map(([c, ls]) => (ls && ls.length ? c + '=' + ls.join('/') : c)).join(' ');
}
function _rgPrioParse(s){
  // "HK TW=zh/cn/yue" → {HK:[], TW:['zh','cn','yue']}; 逗号/空白都当分隔
  const out = {};
  String(s || '').replace(/\s*=\s*/g, '=').split(/[\s,]+/).filter(Boolean).forEach(t => {
    const i = t.indexOf('=');
    const c = (i < 0 ? t : t.slice(0, i)).trim().toUpperCase();
    if(!c) return;
    out[c] = i < 0 ? [] : t.slice(i + 1).split(/[\/,]+/).filter(Boolean)
      .map(x => x.toLowerCase());
  });
  return out;
}
function _rgSplitCodes(s){ return String(s || '').split(/[\s,]+/).map(x => x.trim().toUpperCase()).filter(Boolean); }
function _rgSplitWords(s){ return String(s || '').split(',').map(x => x.trim()).filter(Boolean); }
function _rgKeyErr(k){
  if(!_RG_KEY_RE.test(k)) return '字母开头, 只能含字母/数字/下划线, ≤15 字符';
  if(_RG_SPECIAL.includes(k)) return '与特殊类型(动画/纪录片/综艺/体育/音乐)撞车';
  if(_rgWork && _rgWork.order.filter(x => x === k).length > 1) return '键重复';
  return '';
}
function _rgRow(k, idx, n){
  const it = (_rgWork && _rgWork.items[k]) || {};
  const isNew = _rgNew.has(k);
  const ot = k === 'Ot';
  const cells = [
    ['键', `<input class="cat-in rg-key${isNew && _rgKeyErr(k) ? ' rg-bad' : ''}" type="text" value="${esc(k)}"
       ${isNew ? '' : "readonly title='档键保存后不可改(改键等于换一个分类目录)'"}
       maxlength="15" autocomplete="off" data-k="${esc(k)}" data-f="_key" oninput="_rgInput(this)">
       <small class="rg-keys">${esc(k)}Movie / ${esc(k)}Show</small>`],
    ['档名', `<input class="cat-in" type="text" value="${esc(it.label || '')}" maxlength="30" autocomplete="off"
       data-k="${esc(k)}" data-f="label" placeholder="如 港台" oninput="_rgInput(this)">`],
    ['显示名', `<input class="cat-in" type="text" value="${esc(it.display || '')}" maxlength="30" autocomplete="off"
       data-k="${esc(k)}" data-f="display" placeholder="详情页地区名, 空=用档名" oninput="_rgInput(this)">`],
    ['语言', `<input class="cat-in" type="text" value="${esc((it.languages || []).join(' '))}" autocomplete="off"
       data-k="${esc(k)}" data-f="languages" placeholder="zh cn yue" oninput="_rgInput(this)">`],
    ['国家', `<input class="cat-in" type="text" value="${esc((it.countries || []).join(' '))}" autocomplete="off"
       data-k="${esc(k)}" data-f="countries" placeholder="CN TW HK" oninput="_rgInput(this)">`],
    ['关键词', `<input class="cat-in" type="text" value="${esc((it.keywords || []).join(', '))}" autocomplete="off"
       data-k="${esc(k)}" data-f="keywords" placeholder="港片, 香港电影" oninput="_rgInput(this)">`],
    ['优先', `<input class="cat-in" type="text" value="${esc(_rgPrioText(it.prio))}" autocomplete="off"
       data-k="${esc(k)}" data-f="prio" placeholder="HK TW=zh/cn/yue" oninput="_rgInput(this)">
       <small class="rg-keys">先于语言判; = 后是允许的语言</small>`],
  ];
  const ops = `<td data-th="操作" class="rg-ops">
      <button class="ghost sm" onclick="_rgMove('${esc(k)}',-1)" ${idx === 0 ? 'disabled' : ''}>上</button>
      <button class="ghost sm" onclick="_rgMove('${esc(k)}',1)" ${idx === n - 1 ? 'disabled' : ''}>下</button>
      <button class="ghost sm" onclick="_rgDel('${esc(k)}')" ${ot ? 'disabled' : ''}>${ot ? '兜底' : icon('trash') + '删'}</button>
    </td>`;
  return `<tr class="${isNew ? 'cat-dirty' : ''}" data-k="${esc(k)}">`
    + cells.map(([th, c]) => `<td data-th="${th}">${c}</td>`).join('') + ops + '</tr>';
}
function _rgRender(){
  if(!_rgWork) return '';
  const order = _rgWork.order || [];
  const heads = ['键', '档名', '显示名', '语言', '国家', '关键词', '优先'];
  return `
    <h3 class="set-h">地区档 <small class="rg-sub">(${order.length} 档 → ${order.length * 2} 个地区分类键)</small></h3>
    <p class="cat-note">地区档决定「按国家/地区」那组分类键: 每档生成 <code>键Movie</code> / <code>键Show</code>。
      判定顺序:<b>关键词</b> → <b>优先国家</b>(= 后写允许的语言, 空=不限) → <b>语言</b> → <b>国家</b> → 兜底 <code>Ot</code>。
      语言/国家空格分隔, 关键词用逗号分隔。同一个国家或语言<b>只能属于一个档</b> ——
      把台湾单拆一档时, 先把 TW 从港台档的「国家」和「优先」里删掉, 再填到新档。
      改归属或删档<b>不迁移已归位内容</b>, 旧目录留原地由你决定怎么并。</p>
    <table class="rg-table">
      <thead><tr>${heads.map(h => `<th>${h}</th>`).join('')}<th>操作</th></tr></thead>
      <tbody>${order.map((k, i) => _rgRow(k, i, order.length)).join('')}</tbody>
    </table>
    <div class="rg-add"><button class="ghost sm" onclick="_rgAdd()">+ 新增地区档</button>
      <span class="rg-hint">新增后保存, 才会在下面的目录映射里出现对应行</span></div>`;
}
function _rgInput(inp){
  if(!_rgWork) return;
  const k = inp.dataset.k, f = inp.dataset.f;
  const it = _rgWork.items[k];
  if(f === '_key'){
    const v = inp.value.trim();
    inp.classList.toggle('rg-bad', !!v && !!_rgKeyErr(v) && v !== k);
    if(!v || v === k || !it || _rgKeyErr(v)) return;
    // 只有本次新增的档能改键(改了立刻换掉模型里的键)
    if(!_rgNew.has(k)) return;
    delete _rgWork.items[k];
    _rgWork.items[v] = it;
    _rgWork.order = _rgWork.order.map(x => (x === k ? v : x));
    _rgNew.delete(k); _rgNew.add(v);
    const tr = inp.closest('tr');
    if(tr){
      tr.dataset.k = v;
      inp.dataset.k = v;
      tr.querySelectorAll('[data-k]').forEach(el => { el.dataset.k = v; });
      const kd = tr.querySelector('.rg-keys');
      if(kd) kd.textContent = `${v}Movie / ${v}Show`;
      tr.querySelectorAll('.rg-ops button').forEach(b => {
        b.setAttribute('onclick', b.getAttribute('onclick').replace(`('${k}'`, `('${v}'`));
      });
    }
    _catUpdateBar();
    return;
  }
  if(!it) return;
  if(f === 'prio') it.prio = _rgPrioParse(inp.value);
  else if(f === 'languages' || f === 'countries') it[f] = _rgSplitCodes(inp.value);
  else if(f === 'keywords') it[f] = _rgSplitWords(inp.value);
  else it[f] = String(inp.value).trim();
  _catUpdateBar();
}
function _rgMove(k, dir){
  if(!_rgWork) return;
  const o = _rgWork.order, i = o.indexOf(k), j = i + dir;
  if(i < 0 || j < 0 || j >= o.length) return;
  [o[i], o[j]] = [o[j], o[i]];
  _catRender();
}
function _rgDel(k){
  if(!_rgWork || k === 'Ot') return;
  const i = _rgWork.order.indexOf(k);
  if(i < 0) return;
  const folderKeys = [k + 'Movie', k + 'Show'].filter(x => _catDirty[x]);
  const tip = folderKeys.length
    ? `删除地区档 ${k}? 本页里为它改过的目录名(${folderKeys.join(' / ')})会一并放弃。`
    : `删除地区档 ${k}? 生成的 ${k}Movie / ${k}Show 分类键会消失, 已归位到这些目录的内容不会被移动。`;
  if(!window.confirm(tip)) return;
  _rgWork.order.splice(i, 1);
  delete _rgWork.items[k];
  _rgNew.delete(k);
  folderKeys.forEach(x => delete _catDirty[x]);
  _catRender();
}
function _rgAdd(){
  if(!_rgWork) return;
  let n = 1, k = 'R1';
  while(_rgWork.order.includes(k) || _RG_SPECIAL.includes(k) || !_RG_KEY_RE.test(k)){
    n++; k = 'R' + n;
    if(n > 99) return;
  }
  let label = '新地区', i = 2;
  while(Object.values(_rgWork.items).some(x => (x.label || '') === label)){
    label = '新地区' + i; i++;
  }
  _rgWork.order.push(k);
  _rgWork.items[k] = {label, display: '', languages: [], countries: [], keywords: [], prio: {}};
  _rgNew.add(k);
  _catRender();
  const tr = document.querySelector(`.rg-table tr[data-k="${k}"]`);
  if(tr) tr.querySelector('.rg-key, input').focus();
}

// ---- 通用: 全局开关 + config 全量编辑 ----
// 配置真相源是数据库 app_config: 运行期只读库, 配置文件仅首启一次性导入(2026-09-26)。
// GET /api/config 返回的密钥是掩码(••••••), 保存时后端把"没改过的掩码"还原成原值,
// 所以表单里直接回显掩码即可, 明文令牌不进浏览器。
let _cfgData = null;    // 服务端返回的(掩码后)整份配置
let _cfgDirty = false;
let _cfgTab = 0;        // 当前配置分组页签(顶部页签, 一次只显示一组)
try{ _cfgTab = Number(localStorage.getItem('cfgTab')) || 0; }catch(e){ _cfgTab = 0; }

// 表单分组: p = 配置里的点路径; type 缺省 text | number | password | checkbox | select | list(多行=数组)
const CFG_GROUPS = [
  {title: 'Web 控制台', desc: '监听地址与登录账号。环境变量 WEB_USER / WEB_PASS 会覆盖这里的账号密码。',
   fields: [
    {p: 'web.host', label: '监听地址', desc: '0.0.0.0 = 允许外网访问'},
    {p: 'web.port', label: '端口', type: 'number'},
    {p: 'web.auth.username', label: '登录用户名'},
    {p: 'web.auth.password', label: '登录密码', type: 'password'},
  ]},
  {title: 'Jellyfin', desc: '媒体库同步、缺失检测、入库后刷新。',
   fields: [
    {p: 'jellyfin.url', label: '地址', desc: '如 http://nas:8096'},
    {p: 'jellyfin.token', label: 'API Key', type: 'password', desc: 'Jellyfin → 控制台 → 高级 → API 密钥'},
    {p: 'jellyfin.library_id', label: '默认媒体库 ID', desc: '留空 = 用全部库'},
  ]},
  {title: 'TMDB', desc: '元数据主源: 刮削、中文名反查、缺失检测。api_key 留空则相关功能优雅降级。',
   fields: [
    {p: 'tmdb.api_key', label: 'API Key', type: 'password', desc: 'themoviedb.org → 头像 → Settings → API'},
    {p: 'tmdb.language', label: '语言'},
    {p: 'tmdb.hosts', label: 'Host 候选', type: 'list', desc: '每行一个(不带协议), 按顺序尝试'},
  ]},
  {title: '磁力搜索源', desc: '三个源各自独立, 想开哪个就勾哪个, 可单开也可多开。多开时系统并行查所有启用的源, 结果按 InfoHash 去重后自动合并; 某源查失败不影响其它源。地址旁可切 http/https, 或点「检测」自动找出能连通的协议。',
   fields: [
    {p: '', label: 'Bitmagnet(原生 GraphQL)', type: 'section', desc: '自带 seeders/leechers 与 TMDB 元数据, 适合自托管。'},
    {p: 'bitmagnet.enabled', label: '启用 Bitmagnet', type: 'checkbox'},
    {p: 'bitmagnet.url', label: '地址', probe: 'graphql',
     desc: 'GraphQL 端点, 如 http://192.168.1.100:3333/graphql'},
    {p: 'bitmagnet.limit', label: '返回条数', type: 'number'},
    {p: '', label: 'Bitmagnet-Next-Web(改版站 REST)', type: 'section', desc: '改版站接口, 通常更快, 适合无原生端点时。'},
    {p: 'bitmagnet_next_web.enabled', label: '启用 Bitmagnet-Next-Web', type: 'checkbox'},
    {p: 'bitmagnet_next_web.base', label: '站点 Base', probe: 'rest',
     desc: '改版站根地址, 如 https://your-site.example.com(不内置站点)'},
    {p: 'bitmagnet_next_web.limit', label: '返回条数', type: 'number'},
    {p: '', label: 'Jackett(种子聚合引擎)', type: 'section', desc: '一次聚合它在 Jackett 里配置的所有站点, 用 Torznab 接口。需在 Jackett 中先配好各站并生成 API Key。'},
    {p: 'jackett.enabled', label: '启用 Jackett', type: 'checkbox'},
    {p: 'jackett.base', label: '地址', probe: 'jackett',
     desc: 'Jackett 根地址, 如 http://192.168.1.100:9091'},
    {p: 'jackett.apikey', label: 'API Key', type: 'password', desc: 'Jackett 控制台 → Settings 中各 Indexer 对应的 API Key'},
    {p: 'jackett.indexer', label: 'Indexer', desc: 'all = 聚合全部已配置站(默认); 也可填具体站名或 filter 表达式'},
    {p: 'jackett.limit', label: '返回条数', type: 'number'},
    {p: 'search.max_query_groups', label: '最多查询组数', type: 'number',
     desc: '详情页磁力搜索最多用几个不同标题(简/繁/台/港/原始/英文)并行查。0=全部标题都查(覆盖最全, 首屏略慢); 填 N=最多取 N 个不同标题(更快)'},
  ]},
  {title: '种子抓取规则', desc: '前排发布组整批排最前; 金标组加质量分 +20; 降权发布组减 -20 压到后段(如 BTM/俄语组无中文字幕)。详情页「质量优先」搜索与追踪自动推送都按这里配置。',
   fields: [
    {p: 'search.group_priority', label: '前排发布组(行序 = 优先级)', type: 'list',
     desc: '每行一个组名, 大小写不敏感, 如 FRDS / Beitai / HHD; 命中要求组名与标题其余部分分开(-Beitai、[FRDS]、 HHD 都算, CHDRip 不算 CHD)'},
    {p: 'search.golden_groups', label: '金标自压组', type: 'list',
     desc: '这些组的种子默认金标(自压片源必然带中文字幕+国语, 文件名不一定写明), 质量分 +20'},
    {p: 'search.group_demote', label: '降权发布组', type: 'list',
     desc: '每行一个组名, 大小写不敏感, 如 BTM / 某俄语组; 命中的种子质量分 -20(压到后段), 适合默认无中文字幕的片源; 留空 = 不降分'},
  ]},
  {title: 'CloudDrive2', desc: '离线下载与归位移动。hosts 按顺序尝试, 连不上自动换下一个。',
   fields: [
    {p: 'clouddrive2.hosts', label: '地址候选', type: 'list', desc: '每行一个, 可带 http(s)://'},
    {p: 'clouddrive2.token', label: 'API 令牌 / JWT', type: 'password'},
    {p: 'clouddrive2.offline_root', label: '离线下载目录'},
    {p: 'clouddrive2.staging_dir', label: '受保护路径的中转目录', desc: '写进 /Cloud 前的落脚点, 必须在可删区'},
    {p: 'clouddrive2.cloud_name', label: '云盘名'},
    {p: 'clouddrive2.cloud_account_id', label: '账号 ID', desc: '留空 = 自动发现'},
    {p: 'clouddrive2.insecure', label: '自签证书 insecure', type: 'checkbox'},
    {p: 'clouddrive2.timeout', label: '超时(秒)', type: 'number'},
    {p: 'clouddrive2.no_delete_paths', label: '禁止删除的路径', type: 'list', desc: '每行一个; 只允许往里移动'},
  ]},
  {title: 'WebDAV', desc: 'CD2 自带 WebDAV: 读文件 / 探测媒体信息写 <fileinfo> 的主通道。',
   fields: [
    {p: 'webdav.enabled', label: '启用', type: 'checkbox'},
    {p: 'webdav.base', label: '地址'},
    {p: 'webdav.account_root', label: '账号根路径'},
    {p: 'webdav.user', label: '用户名'},
    {p: 'webdav.password', label: '密码', type: 'password'},
    {p: 'webdav.timeout', label: '探测超时(秒)', type: 'number'},
  ]},
  {title: 'qBittorrent', desc: '详情页「推送 Qbit」与管理页「下载」用; 留空地址 = 未启用。',
   fields: [
    {p: 'qbit.url', label: 'WebUI 地址'},
    {p: 'qbit.username', label: '账号'},
    {p: 'qbit.password', label: '密码', type: 'password'},
    {p: 'qbit.save_path', label: '默认保存路径'},
    {p: 'qbit.category', label: '默认分类'},
  ]},
  {title: '整理与归位', desc: '分类目录名在「分类规则」子页签改; 这里是整理行为本身。',
   fields: [
    {p: 'organize.enabled', label: '启用整理', type: 'checkbox'},
    {p: 'organize.cloud_root', label: '媒体库根目录', desc: '目标 = 此路径 / 分类目录, 如 /Cloud'},
    {p: 'organize.folder_template', label: '目录名模板', desc: '占位符 {title} {year} {imdb} {tmdb} {quality}'},
    {p: 'organize.movie_file_template', label: '电影文件名模板'},
    {p: 'organize.on_conflict', label: '目标已存在时', type: 'select',
     options: [['skip', 'skip(跳过并报告)'], ['merge', 'merge(并入已有目录)']]},
    {p: 'organize.min_match_score', label: '最低匹配度(0~1)', type: 'number', desc: '低于它不改名, 防错配'},
    {p: 'organize.scan_depth', label: '扫描递归层数', type: 'number'},
    {p: 'organize.write_nfo', label: '写 NFO', type: 'checkbox'},
    {p: 'organize.rename_media_file', label: '电影视频改名', type: 'checkbox'},
    {p: 'organize.probe_media', label: 'MediaInfo 探测(写 fileinfo)', type: 'checkbox'},
    {p: 'organize.clean_media_names', label: '剥文件名推广块', type: 'checkbox'},
    {p: 'organize.keep_subtitles', label: '保留字幕', type: 'checkbox'},
    {p: 'organize.clean_unresolved', label: '未反查到也清广告', type: 'checkbox'},
    {p: 'organize.wikidata', label: '反查 wikidata Q-id', type: 'checkbox'},
  ]},
  {title: '目录与存储', desc: '本机路径; state 目录放队列, 数据库路径用 MEDIA_AUTO_DB 指定。',
   fields: [
    {p: 'library_root', label: '库根路径'},
    {p: 'state_dir', label: '状态目录'},
  ]},
];

function _cfgGet(obj, path){
  return path.split('.').reduce((o, k) => (o && typeof o === 'object') ? o[k] : undefined, obj);
}
function _cfgSet(obj, path, val){
  const ks = path.split('.'); let o = obj;
  for(let i = 0; i < ks.length - 1; i++){
    if(typeof o[ks[i]] !== 'object' || o[ks[i]] === null) o[ks[i]] = {};
    o = o[ks[i]];
  }
  o[ks[ks.length - 1]] = val;
}
function _cfgField(f){
  const v = _cfgGet(_cfgData, f.p);
  const desc = f.desc ? `<small class="cfg-fdesc">${esc(f.desc)}</small>` : '';
  const on = `oninput="_cfgTouch()" onchange="_cfgTouch()"`;
  if(f.type === 'section'){
    // 区块小标题: 跨整行, 把该分组拆成若干张"源卡片", 每个源独立成块
    const hint = f.desc ? `<small class="cfg-section-hint">${esc(f.desc)}</small>` : '';
    return `<div class="cfg-field cfg-section"><span class="cfg-section-title">${esc(f.label)}</span>${hint}</div>`;
  }
  if(f.type === 'checkbox'){
    return `<label class="cfg-check"><input type="checkbox" data-cfg="${f.p}" ${v?'checked':''} ${on}>
      <span>${esc(f.label)}</span></label>${desc}`;
  }
  let input;
  if(f.type === 'select'){
    input = `<select class="cfg-in" data-cfg="${f.p}" ${on}>`
      + (f.options || []).map(([val, lab]) => `<option value="${esc(val)}" ${v===val?'selected':''}>${esc(lab)}</option>`).join('')
      + `</select>`;
  }else if(f.type === 'list'){
    const txt = Array.isArray(v) ? v.join('\n') : (v == null ? '' : String(v));
    input = `<textarea class="cfg-in cfg-list" rows="3" data-cfg="${f.p}" data-kind="list" ${on}>${esc(txt)}</textarea>`;
  }else if(f.probe){
    // 带协议探测的地址框: 输入 + http/https 切换 + 「检测」自动找能连通的协议
    const val = v == null ? '' : String(v);
    input = `<div class="cfg-probe">
      <input class="cfg-in" type="text" data-cfg="${f.p}" value="${esc(val)}"
        placeholder="http(s)://主机[:端口][/路径]" ${on} autocomplete="off"
        onblur="cfgAutoProbe('${f.p}','${f.probe}')">
      <div class="cfg-seg">
        <button type="button" onclick="cfgScheme('${f.p}','http')">http</button>
        <button type="button" onclick="cfgScheme('${f.p}','https')">https</button>
      </div>
      <button type="button" class="mbtn sm" onclick="cfgProbe('${f.p}','${f.probe}',this)">检测</button>
    </div>`;
  }else{
    const type = f.type === 'number' ? 'number' : (f.type === 'password' ? 'password' : 'text');
    const val = v == null ? '' : (Array.isArray(v) ? v.join(',') : String(v));
    const ph = f.type === 'password' ? (v ? '' : '未设置') : '';
    input = `<input class="cfg-in" type="${type}" data-cfg="${f.p}" value="${esc(val)}" placeholder="${ph}" ${on} autocomplete="off">`;
  }
  return `<div class="cfg-field"><span class="cfg-lab">${esc(f.label)}</span>${input}${desc}</div>`;
}
// ---- 地址框的协议处理(http/https 切换 + 自动探测) ----
function _probeEl(p){
  return document.querySelector(`#manageBody [data-cfg="${p}"]`);
}
// 手动切协议: 把输入框的协议头换掉(没写协议就补上)
function cfgScheme(p, scheme){
  const el = _probeEl(p); if(!el) return;
  const raw = (el.value || '').trim().replace(/^https?:\/\//i, '');
  el.value = raw ? `${scheme}://${raw}` : `${scheme}://`;
  _cfgTouch();
}
// 失焦时若没写协议头 → 自动按 https → http 检测(写了就尊重用户的选择, 不动)
function cfgAutoProbe(p, kind){
  const el = _probeEl(p); if(!el) return;
  const raw = (el.value || '').trim();
  if(!raw || /^https?:\/\//i.test(raw)) return;
  cfgProbe(p, kind);
}
// 后端探测: 收到 HTTP 响应即算通, 返回第一个通的地址写回输入框
async function cfgProbe(p, kind, btn){
  const el = _probeEl(p); if(!el) return;
  const raw = (el.value || '').trim();
  if(!raw){ toast('先填地址再检测'); return; }
  const old = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = '检测中…'; }
  try{
    const r = await api(`/api/search/probe?url=${encodeURIComponent(raw)}&kind=${encodeURIComponent(kind || 'rest')}`);
    if(r.ok){
      el.value = r.url;
      _cfgTouch();
      toast(`协议可用: ${r.url}`);
    }else{
      const detail = (r.tried || []).map(t => `${t.url} → ${t.detail}`).join('; ');
      toast('两个协议都不通: ' + detail.slice(0, 180));
    }
  }catch(e){ toast('检测失败: ' + e.message); }
  finally{ if(btn){ btn.disabled = false; btn.textContent = old || '检测'; } }
}

function _cfgRender(){
  const box = $('#cfgGroups'); if(!box) return;
  if(_cfgTab < 0 || _cfgTab >= CFG_GROUPS.length) _cfgTab = 0;
  // 顶部页签: 一次只显示一组(其余分组仍留在 DOM 里, 保存时一并收集, 隐藏页签的改动不会丢)
  box.innerHTML = `
    <nav class="cfg-tabs" id="cfgTabs">
      ${CFG_GROUPS.map((g, i) => `<button class="${i === _cfgTab ? 'active' : ''}"
        onclick="cfgSwitchTab(${i})">${esc(g.title)}</button>`).join('')}
    </nav>
    <div class="cfg-panels">
      ${CFG_GROUPS.map((g, i) => `
      <section class="cfg-group${i === _cfgTab ? ' active' : ''}">
        <h4 class="cfg-gtitle">${esc(g.title)}</h4>
        ${g.desc ? `<p class="cfg-gdesc">${esc(g.desc)}</p>` : ''}
        <div class="cfg-grid">${g.fields.map(_cfgField).join('')}</div>
      </section>`).join('')}
    </div>`;
  cfgScrollTab();
}
// 切换配置分组页签(记住上次位置; 隐藏页签里的输入值仍在 DOM, 保存时照常收集)
function cfgSwitchTab(i){
  if(!(i >= 0 && i < CFG_GROUPS.length)) return;
  _cfgTab = i;
  try{ localStorage.setItem('cfgTab', String(i)); }catch(e){ /* 隐私模式忽略 */ }
  const box = $('#cfgGroups'); if(!box) return;
  box.querySelectorAll('.cfg-panels .cfg-group').forEach((s, k) => s.classList.toggle('active', k === i));
  box.querySelectorAll('.cfg-tabs button').forEach((b, k) => b.classList.toggle('active', k === i));
  cfgScrollTab();
}
function cfgScrollTab(){
  const btns = document.querySelectorAll('#cfgTabs button');
  if(btns[_cfgTab] && btns[_cfgTab].scrollIntoView) btns[_cfgTab].scrollIntoView({block: 'nearest', inline: 'center'});
}
function _cfgTouch(){
  _cfgDirty = true;
  const bar = $('#cfgBar'); if(bar){
    bar.style.display = 'flex';
    const m = bar.querySelector('.cat-barmsg');
    if(m) m.innerHTML = `${icon('alert')}有未保存的配置变更 — 保存后立即热加载生效(无需重启)`;
    // 保存按钮在 cfgSave() 里会被置灰到"保存成功/失败"为止; 失败路径会复位, 但成功路径
    // 走的是 _cfgLoad() 重绘表单, 按钮本身不重绘 → 一直灰着。这里"只要又有编辑就放行",
    // 保证上一次保存之后还能再存第二次(2026-09-29 修: 加了组点不了保存配置)。
    const b = bar.querySelector('#cfgSaveBtn');
    if(b) b.disabled = false;
  }
}
// 把表单里的值写进 data(点路径 → 值, 类型按字段声明转换)
function _cfgApplyTo(data){
  document.querySelectorAll('#manageBody [data-cfg]').forEach(el => {
    const p = el.dataset.cfg;
    let v;
    if(el.type === 'checkbox') v = el.checked;
    else if(el.dataset.kind === 'list') v = el.value.split('\n').map(s => s.trim()).filter(Boolean);
    else if(el.type === 'number') v = el.value === '' ? '' : Number(el.value);
    else v = el.value;
    _cfgSet(data, p, v);
  });
  return data;
}
async function _cfgLoad(){
  try{
    const r = await api('/api/config');
    _cfgData = r.config;
    _cfgDirty = false;
    const p = $('#cfgPath');
    if(p) p.textContent = `配置存储: 数据库 app_config → ${r.db}`;
    _cfgRender();
    const bar = $('#cfgBar'); if(bar) bar.style.display = 'none';
  }catch(e){
    const box = $('#cfgGroups');
    if(box) box.innerHTML = `<div class="empty">加载配置失败: ${esc(e.message)}</div>`;
  }
}
function cfgReset(){ if(_cfgData){ _cfgRender(); _cfgDirty = false; const b = $('#cfgBar'); if(b) b.style.display = 'none'; } }
async function cfgSave(){
  const btn = $('#cfgSaveBtn'); if(btn) btn.disabled = true;
  try{
    const data = _cfgApplyTo(JSON.parse(JSON.stringify(_cfgData || {})));
    const r = await api('/api/config', {method: 'PUT', body: JSON.stringify({config: data})});
    toast(r.msg || '已保存');
    await _cfgLoad();
    // 成功也要放回可点状态: cfgBar 是静态 HTML(不会被 _cfgLoad 重绘), 置灰后留着会
    // 让"下次编辑"出现一个点不动的保存按钮(2026-09-29 修)。
    if(btn) btn.disabled = false;
  }catch(e){
    toast('保存失败: ' + e.message);
    if(btn) btn.disabled = false;
  }
}
function cfgExport(){
  fetch('/api/config/export', {credentials: 'same-origin'}).then(async res => {
    if(!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'mediaauto-config.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast('已导出(文件里含令牌, 注意保管)');
  }).catch(e => toast('导出失败: ' + e.message));
}

async function renderGeneral(){
  const el = $('#manageBody');
  el.innerHTML = `
    <div class="settings">
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

    <div class="cfg-head">
      <h3 class="set-h">全部配置</h3>
      <p class="cfg-note">配置只有两个入口: 首次初始化引导, 和这里。存于数据库, 运行期不读任何配置文件。
        上方页签切换分组(一次只看一组), 改完点底部「保存配置」立即热加载生效, <b>无需重启</b>;
        密钥显示为 •••••• 表示未修改。<b>切页签不会丢改动</b>, 保存是一起提交的。</p>
      <div class="cfg-actions">
        <button class="ghost sm" onclick="cfgExport()">导出备份</button>
        <span class="cfg-note" id="cfgPath"></span>
      </div>
    </div>
    <div id="cfgGroups"><div class="empty"><span class="spin"></span>加载配置…</div></div>


    <div class="cat-bar" id="cfgBar" style="display:none">
      <span class="cat-barmsg"></span>
      <button class="ghost sm" onclick="cfgReset()">重置</button>
      <button class="mbtn sm" id="cfgSaveBtn" onclick="cfgSave()">${icon('check')}保存配置</button>
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
  await _cfgLoad();
}
