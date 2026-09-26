// MediaAuto 前端 — setup.js
// 首次启动引导(强制分步, 2026-09-26): 配置从 config.example.json 播种时
// setup_done=0, 登录后直接进引导; 每步只问"最小必填集"并写库热加载,
// 可逐步跳过, 全部参数之后都能在「管理 → 通用」里补。
// 依赖 common.js 的 api/toast/esc 与 settings.js 的 _cfgGet/_cfgSet(同为全局作用域)。

const SETUP_PAGES = [
  {key: 'password', title: '设置管理员密码', feature: '登录 Web 控制台',
   note: '当前是初始账号。改掉默认密码, 否则任何人都能进你的控制台。',
   fields: [
     {p: 'web.auth.username', label: '用户名'},
     {p: 'web.auth.password', label: '新密码', type: 'password', desc: '至少 6 位; 留空 = 保持不变'},
   ]},
  {key: 'jellyfin', title: '连接 Jellyfin', feature: '媒体库同步、缺失检测、入库后刷新',
   note: '地址填到主机名/端口即可(如 http://nas:8096)。',
   fields: [
     {p: 'jellyfin.url', label: 'Jellyfin 地址'},
     {p: 'jellyfin.token', label: 'API Key', type: 'password', desc: '控制台 → 高级 → API 密钥'},
   ]},
  {key: 'tmdb', title: '配置 TMDB', feature: '元数据刮削、中文名反查、缺失检测',
   note: '免费注册 themoviedb.org → 头像 → Settings → API。留空则这些功能自动降级。',
   fields: [
     {p: 'tmdb.api_key', label: 'TMDB API Key', type: 'password'},
   ]},
  {key: 'cd2', title: '连接 CloudDrive2', feature: '推送离线下载、整理归位',
   note: '地址每行一个, 按顺序尝试; 令牌在 CD2 网页端生成。',
   fields: [
     {p: 'clouddrive2.hosts', label: 'CD2 地址', type: 'list'},
     {p: 'clouddrive2.token', label: 'API 令牌 / JWT', type: 'password'},
   ]},
  {key: 'library', title: '指定媒体库根', feature: '整理时把媒体归位到这个目录',
   note: '一般是云盘上的媒体目录, 如 /Cloud。目标路径 = 根目录 / 分类目录。',
   fields: [
     {p: 'library_root', label: '库根路径'},
     {p: 'organize.cloud_root', label: '整理目标根路径'},
   ]},
];

let _setupCfg = null;      // 整份配置(掩码), 引导过程中直接编辑
let _setupStep = 0;
let _setupMeta = null;     // /api/config/setup 返回

function _setupField(f){
  const v = _cfgGet(_setupCfg, f.p);
  const desc = f.desc ? `<small class="cfg-fdesc">${esc(f.desc)}</small>` : '';
  const on = 'oninput=""';
  if(f.type === 'list'){
    const txt = Array.isArray(v) ? v.join('\n') : (v == null ? '' : String(v));
    return `<div class="cfg-field"><span class="cfg-lab">${esc(f.label)}</span>
      <textarea class="cfg-in cfg-list" rows="4" data-setup="${f.p}" data-kind="list" oninput="">${esc(txt)}</textarea>${desc}</div>`;
  }
  const type = f.type === 'password' ? 'password' : 'text';
  const val = v == null ? '' : String(v);
  const ph = f.type === 'password' ? (v ? '已设置(留空保持不变)' : '未设置') : '';
  return `<div class="cfg-field"><span class="cfg-lab">${esc(f.label)}</span>
    <input class="cfg-in" type="${type}" data-setup="${f.p}" value="${esc(val)}" placeholder="${ph}" autocomplete="off">${desc}</div>`;
}

function _setupApply(){
  document.querySelectorAll('#setupBody [data-setup]').forEach(el => {
    const p = el.dataset.setup;
    const v = el.dataset.kind === 'list'
      ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
      : el.value;
    _cfgSet(_setupCfg, p, v);
  });
}

function _setupValidate(page){
  const vals = {};
  document.querySelectorAll('#setupBody [data-setup]').forEach(el => vals[el.dataset.setup] = el.value.trim());
  if(page.key === 'password'){
    const pwd = vals['web.auth.password'] || '';
    if(pwd && pwd !== '••••••' && pwd.length < 6) return '密码至少 6 位';
    if(!(vals['web.auth.username'] || '').length) return '用户名不能为空';
  }
  if(page.key === 'jellyfin'){
    const url = vals['jellyfin.url'] || '';
    if(url && !/^https?:\/\//.test(url)) return 'Jellyfin 地址要以 http(s):// 开头';
  }
  if(page.key === 'library'){
    for(const p of ['library_root', 'organize.cloud_root']){
      const v = vals[p] || '';
      if(v && !v.startsWith('/')) return '路径要以 / 开头(如 /Cloud)';
    }
  }
  return null;
}

function _setupRender(){
  const page = SETUP_PAGES[_setupStep];
  const last = _setupStep === SETUP_PAGES.length - 1;
  const checks = (_setupMeta && _setupMeta.checks) || {};
  const dots = SETUP_PAGES.map((_, i) => `<span class="setup-dot ${i === _setupStep ? 'on' : (i < _setupStep ? 'done' : '')}"></span>`).join('');
  const doneBadge = Object.values(checks).filter(Boolean).length;
  document.getElementById('setupBody').innerHTML = `
    <div class="setup-dots">${dots}</div>
    <h2>${esc(page.title)}</h2>
    <p class="setup-feature">${icon('check')}配好这一步才能用: <b>${esc(page.feature)}</b></p>
    <p class="setup-note">${esc(page.note)}</p>
    <div class="setup-fields">${page.fields.map(_setupField).join('')}</div>
    <div class="setup-bar">
      <span class="setup-prog">第 ${_setupStep + 1} / ${SETUP_PAGES.length} 步 · 已完成 ${doneBadge} 项</span>
      <span class="grow"></span>
      <button class="ghost sm" onclick="setupSkip()">跳过</button>
      <button class="mbtn sm" id="setupNext" onclick="setupNext()">${last ? '完成引导' : '下一步'}</button>
    </div>`;
  // 首步把焦点放到密码框
  const first = document.querySelector('#setupBody .cfg-in');
  if(first) first.focus();
}

async function setupNext(){
  const page = SETUP_PAGES[_setupStep];
  const err = _setupValidate(page);
  if(err){ toast(err); return; }
  const btn = document.getElementById('setupNext');
  if(btn) btn.disabled = true;
  try{
    _setupApply();
    const r = await api('/api/config', {method: 'PUT', body: JSON.stringify({config: _setupCfg})});
    if(_setupStep === SETUP_PAGES.length - 1){
      await api('/api/config/setup/done', {method: 'POST', body: '{}'});
      _setupClose();
      toast(r.msg || '引导完成, 配置已生效');
      return;
    }
    _setupStep++;
    _setupRender();
  }catch(e){
    toast('保存失败: ' + e.message);
    if(btn) btn.disabled = false;
  }
}

async function setupSkip(){
  if(_setupStep === SETUP_PAGES.length - 1){ await setupFinishLater(); return; }
  _setupStep++;
  _setupRender();
}

async function setupFinishLater(){
  try{
    await api('/api/config/setup/done', {method: 'POST', body: '{}'});
    _setupClose();
    toast('已跳过引导 — 随时可在「管理 → 通用」里补配置');
  }catch(e){ toast('操作失败: ' + e.message); }
}

function _setupClose(){
  const ov = document.getElementById('setupOv');
  if(ov) ov.remove();
}

// 登录成功后调用: 已完成 → 返回 true(不打扰); 未完成 → 拉起引导并返回 false
async function maybeStartSetup(){
  let meta;
  try{ meta = await api('/api/config/setup'); }
  catch(e){ return true; }   // 拿不到状态不阻塞主流程
  if(meta.done) return true;
  _setupMeta = meta;
  try{
    const r = await api('/api/config');
    _setupCfg = r.config;
  }catch(e){
    toast('读取配置失败, 跳过引导: ' + e.message);
    return true;
  }
  _setupStep = 0;
  let ov = document.getElementById('setupOv');
  if(!ov){
    ov = document.createElement('div');
    ov.id = 'setupOv';
    ov.className = 'setup-ov';
    ov.innerHTML = `<div class="setup-card">
      <div class="setup-head">
        <span class="setup-brand">MediaAuto</span>
        <button class="ghost sm" onclick="setupFinishLater()" title="稍后在「管理 → 通用」里补">稍后配置</button>
      </div>
      <div id="setupBody"></div>
    </div>`;
    document.body.appendChild(ov);
  }
  ov.style.display = 'flex';
  _setupRender();
  return false;
}
