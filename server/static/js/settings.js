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
// 分类键由 lib/classify.py 级联引擎生成, 键不可增删, 只能改目录名 + 媒体库根。
// 保存写回数据库 app_config(热加载, 无需重启, 下次整理立即生效)。
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

// ---- 通用: 全局开关 + config 全量编辑 ----
// 配置真相源是数据库 app_config: 运行期只读库, 配置文件仅首启一次性导入(2026-09-26)。
// GET /api/config 返回的密钥是掩码(••••••), 保存时后端把"没改过的掩码"还原成原值,
// 所以表单里直接回显掩码即可, 明文令牌不进浏览器。
let _cfgData = null;    // 服务端返回的(掩码后)整份配置
let _cfgDirty = false;

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
  {title: '磁力搜索源', desc: '两个源相互独立, 都启用时按「优先源」选择。',
   fields: [
    {p: 'bitmagnet.enabled', label: 'Bitmagnet(原生 GraphQL)', type: 'checkbox', desc: '有 seeders/leechers 与 TMDB 元数据'},
    {p: 'bitmagnet.url', label: 'Bitmagnet 地址'},
    {p: 'bitmagnet.limit', label: '返回条数', type: 'number'},
    {p: 'bitmagnet_next_web.enabled', label: 'Bitmagnet-Next-Web', type: 'checkbox', desc: '改版站 REST 源, 通常更快'},
    {p: 'bitmagnet_next_web.base', label: '站点 Base'},
    {p: 'bitmagnet_next_web.limit', label: '返回条数', type: 'number'},
    {p: 'search.primary', label: '优先源', type: 'select',
     options: [['next_web', 'next_web(更快)'], ['native', 'native(原生)']]},
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
    {p: 'clouddrive2.local_root', label: '本地挂载点', type: 'list', desc: '每行一个; 仅用于 MediaInfo 探测写 NFO'},
  ]},
  {title: 'WebDAV', desc: 'CD2 自带 WebDAV, 无本地挂载时用它读文件探测媒体信息。',
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
    {p: 'organize.tmm_locked', label: 'NFO 写 tmm_locked', type: 'checkbox'},
    {p: 'organize.wikidata', label: '反查 wikidata Q-id', type: 'checkbox'},
  ]},
  {title: '目录与存储', desc: '本机路径; state 目录放队列, 数据库路径用 MEDIA_AUTO_DB 指定。',
   fields: [
    {p: 'library_root', label: '库根路径'},
    {p: 'state_dir', label: '状态目录'},
  ]},
  {title: 'tinyMediaManager', desc: '手动刮削/刷新用的命令(整理已自带 NFO, 通常不需要改)。',
   fields: [
    {p: 'tinymediamanager.movie_cmd', label: '电影命令'},
    {p: 'tinymediamanager.tv_cmd', label: '剧集命令'},
    {p: 'tinymediamanager.docker_exec', label: 'docker exec 前缀'},
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
  }else{
    const type = f.type === 'number' ? 'number' : (f.type === 'password' ? 'password' : 'text');
    const val = v == null ? '' : (Array.isArray(v) ? v.join(',') : String(v));
    const ph = f.type === 'password' ? (v ? '' : '未设置') : '';
    input = `<input class="cfg-in" type="${type}" data-cfg="${f.p}" value="${esc(val)}" placeholder="${ph}" ${on} autocomplete="off">`;
  }
  return `<div class="cfg-field"><span class="cfg-lab">${esc(f.label)}</span>${input}${desc}</div>`;
}
function _cfgRender(){
  const box = $('#cfgGroups'); if(!box) return;
  box.innerHTML = CFG_GROUPS.map(g => `
    <section class="cfg-group">
      <h4 class="cfg-gtitle">${esc(g.title)}</h4>
      ${g.desc ? `<p class="cfg-gdesc">${esc(g.desc)}</p>` : ''}
      <div class="cfg-grid">${g.fields.map(_cfgField).join('')}</div>
    </section>`).join('');
}
function _cfgTouch(){
  _cfgDirty = true;
  const bar = $('#cfgBar'); if(bar){
    bar.style.display = 'flex';
    const m = bar.querySelector('.cat-barmsg');
    if(m) m.innerHTML = `${icon('alert')}有未保存的配置变更 — 保存后立即热加载生效(无需重启)`;
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
function cfgAdvToggle(d){
  const ta = $('#cfgJson');
  if(d.open && ta && !_cfgDirty) ta.value = JSON.stringify(_cfgData || {}, null, 2);
  else if(d.open && ta && !ta.value.trim()) ta.value = JSON.stringify(_cfgData || {}, null, 2);
}
async function cfgSave(){
  const btn = $('#cfgSaveBtn'); if(btn) btn.disabled = true;
  try{
    let data;
    const adv = $('#cfgAdv');
    if(adv && adv.open){
      try{ data = JSON.parse($('#cfgJson').value); }
      catch(e){ toast('JSON 语法错误: ' + e.message); if(btn) btn.disabled = false; return; }
      if(!data || typeof data !== 'object' || Array.isArray(data)){ toast('配置必须是 JSON 对象'); if(btn) btn.disabled = false; return; }
    }else{
      data = _cfgApplyTo(JSON.parse(JSON.stringify(_cfgData || {})));
    }
    const r = await api('/api/config', {method: 'PUT', body: JSON.stringify({config: data})});
    toast(r.msg || '已保存');
    const adv2 = $('#cfgAdv'); if(adv2) adv2.open = false;
    await _cfgLoad();
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
function cfgImport(input){
  const file = input.files && input.files[0]; input.value = '';
  if(!file) return;
  const rd = new FileReader();
  rd.onload = () => {
    try{
      const data = JSON.parse(rd.result);
      if(!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('必须是 JSON 对象');
      _cfgData = data;
      const adv = $('#cfgAdv');
      if(adv){ adv.open = true; const ta = $('#cfgJson'); if(ta) ta.value = JSON.stringify(data, null, 2); }
      else _cfgRender();
      _cfgTouch();
      toast('已读入文件, 点「保存配置」写入数据库');
    }catch(e){ toast('导入失败: ' + e.message); }
  };
  rd.readAsText(file);
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
      <p class="cfg-note">配置存在数据库里(运行期不读任何配置文件)。
        改完点底部「保存配置」立即热加载生效, <b>无需重启</b>; 密钥显示为 •••••• 表示未修改。</p>
      <div class="cfg-actions">
        <button class="ghost sm" onclick="cfgExport()">导出 JSON</button>
        <label class="ghost sm" style="cursor:pointer">导入 JSON
          <input type="file" accept=".json,application/json" hidden onchange="cfgImport(this)"></label>
        <span class="cfg-note" id="cfgPath"></span>
      </div>
    </div>
    <div id="cfgGroups"><div class="empty"><span class="spin"></span>加载配置…</div></div>

    <details class="cfg-adv" id="cfgAdv" ontoggle="cfgAdvToggle(this)">
      <summary>高级 — 直接编辑整份 JSON</summary>
      <textarea id="cfgJson" spellcheck="false" oninput="_cfgTouch()"></textarea>
      <p class="cfg-desc">分类目录(categories)、各种 _comment 说明字段也在这里编辑。
        JSON 语法错误会在保存时被拦下。</p>
    </details>

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
