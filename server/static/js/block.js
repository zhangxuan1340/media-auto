// MediaAuto 前端 — block.js
// 屏蔽(2026-09 起并入「设置」页 → 「屏蔽」子页签) + 全局开关(toggle 供 设置/通用 调用)
// 通用开关的渲染在 settings.js 的 renderGeneral(), 这里 loadBlock 只渲染屏蔽列表到 #manageBody
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

// ---- 自定义输入弹窗(替代浏览器原生 prompt) ----
// 根因: iOS Safari 完全不支持 prompt()(点按钮静默返回 null 不弹窗), 导致移动端"屏蔽"看似没做;
// 电脑端 prompt 能弹但是系统灰框, 与液态玻璃 UI 割裂。这里用有样式的玻璃卡片输入弹窗,
// 叠在详情弹窗之上(z-index 60 > modal-bg 50), 移动端/电脑端行为一致。
// 用法: const reason = await promptInput({title:'屏蔽原因(可选)', placeholder:'如: 不感兴趣 / 重复', ...});
//   返回 Promise<string|null>: 点"确定"/输入框回车 → 输入值; 点"取消"/Esc/点遮罩 → null。
function promptInput(opts={}){
  return new Promise(resolve=>{
    const bg = document.createElement('div');
    bg.className = 'prompt-bg';
    bg.innerHTML = `
      <div class="prompt-card" role="dialog" aria-modal="true">
        <h4>${icon(opts.icon||'info')}${esc(opts.title||'请输入')}</h4>
        ${opts.sub?`<p class="sub">${esc(opts.sub)}</p>`:''}
        <input type="text" id="promptInputEl" value="${esc(opts.value||'')}" placeholder="${esc(opts.placeholder||'')}" autocomplete="off">
        <div class="btns">
          <button class="ghost" id="promptCancelBtn">${esc(opts.cancelText||'取消')}</button>
          <button class="mbtn" id="promptOkBtn">${esc(opts.okText||'确定')}</button>
        </div>
      </div>`;
    document.body.appendChild(bg);
    const input = $('#promptInputEl');
    let done = false;
    const finish = (val)=>{
      if(done) return; done = true;
      document.removeEventListener('keydown', onKey);
      bg.remove();
      resolve(val);
    };
    const onKey = (e)=>{
      if(e.key === 'Escape'){ e.stopPropagation(); finish(null); }
      else if(e.key === 'Enter'){ e.preventDefault(); finish(input.value.trim()); }
    };
    // 只绑在弹窗自身: 点遮罩(背景)取消, 点卡片不取消
    bg.addEventListener('mousedown', e=>{ if(e.target === bg) finish(null); });
    $('#promptOkBtn').addEventListener('click', ()=>finish(input.value.trim()));
    $('#promptCancelBtn').addEventListener('click', ()=>finish(null));
    document.addEventListener('keydown', onKey, true);
    // 聚焦并全选(移动端自动弹键盘); 给一帧让动画先起
    requestAnimationFrame(()=>{ input.focus(); input.select && input.select(); });
  });
}

// ---- 自定义确认弹窗(替代浏览器原生 confirm) ----
// 根因同 promptInput: 原生 confirm 在部分 iOS / 内嵌 webview 环境不可靠 —— 可能静默返回 false
// 不弹框, 导致"点了没任何反应"(整理页「执行选中/执行全部」2026-10-04 报障)。这里复用同一套
// 玻璃卡片样式(.prompt-bg/.prompt-card), 移动端/电脑端行为一致, 返回可靠的 Promise<boolean>。
// 用法: const ok = await confirmBox({title:'执行整理 2 个条目', sub:'将删除广告…', okText:'执行', icon:'play'});
//   点"确定"/回车 → true; 点"取消"/Esc/点遮罩 → false。
function confirmBox(opts={}){
  return new Promise(resolve=>{
    const bg = document.createElement('div');
    bg.className = 'prompt-bg';
    bg.innerHTML = `
      <div class="prompt-card" role="dialog" aria-modal="true">
        <h4>${icon(opts.icon||'alert')}${esc(opts.title||'请确认')}</h4>
        ${opts.sub?`<p class="sub">${esc(opts.sub)}</p>`:''}
        <div class="btns">
          <button class="ghost" id="confirmBoxCancel">${esc(opts.cancelText||'取消')}</button>
          <button class="mbtn${opts.danger?' danger':''}" id="confirmBoxOk">${esc(opts.okText||'确定')}</button>
        </div>
      </div>`;
    document.body.appendChild(bg);
    let done = false;
    const finish = (v)=>{
      if(done) return; done = true;
      document.removeEventListener('keydown', onKey, true);
      bg.remove();
      resolve(v);
    };
    const onKey = (e)=>{
      if(e.key === 'Escape'){ e.stopPropagation(); finish(false); }
      else if(e.key === 'Enter'){ e.preventDefault(); finish(true); }
    };
    bg.addEventListener('mousedown', e=>{ if(e.target === bg) finish(false); });
    $('#confirmBoxOk').addEventListener('click', ()=>finish(true));
    $('#confirmBoxCancel').addEventListener('click', ()=>finish(false));
    document.addEventListener('keydown', onKey, true);
    requestAnimationFrame(()=>{ try{ $('#confirmBoxOk').focus(); }catch(_){} });
  });
}

async function loadBlock(){
  const el = $('#manageBody');
  el.innerHTML = `<h3 class="set-h">已屏蔽的作品</h3>
    <div style="font-size:12px;color:var(--muted);margin:-6px 0 14px">被屏蔽的作品会从热门榜、缺失、浏览列表里隐藏。在详情弹窗点「屏蔽」添加。</div>
    <div id="blockBody"><div class="empty"><span class="spin"></span>加载…</div></div>`;
  try{
    const list = await api('/api/blocklist');
    if(!list.length){ $('#blockBody').innerHTML='<div class="empty" style="padding:18px">尚未屏蔽任何作品。</div>'; }
    else $('#blockBody').innerHTML = '<table><thead><tr><th>片名</th><th>类型</th><th>原因</th><th>屏蔽时间</th><th></th></tr></thead><tbody>'
      + list.map(b=>`<tr><td data-th="片名">${esc(b.title)||b.tmdbId}</td><td data-th="类型">${b.kind==='tv'?'剧集':'电影'}</td><td data-th="原因">${esc(b.reason)||'—'}</td><td data-th="屏蔽时间">${fmtTime(b.createdAt)}</td><td data-th=""><button class="mbtn" onclick="unblock('${b.kind}',${b.tmdbId})">取消屏蔽</button></td></tr>`).join('')
      + '</tbody></table>';
  }catch(e){ $('#blockBody').innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}
async function toggleHideComplete(v){
  try{
    await api(`/api/settings?hide_complete=${v}`,{method:'POST'});
    toast(v?'已开启隐藏完整作品':'已关闭');
    TREND_MOVIE.hideLib = v; TREND_TV.hideLib = v;
    // 热门页(电影/剧集任一)若正显示, 立即按新设置刷新
    const _vt = _visibleTrend();
    if(_vt){ _vt.page = 1; loadTrendList(null, _vt.kind); }
    // 浏览子页签若正显示, 也刷新一次(隐藏已完整同样作用于浏览列表)
    if(typeof loadBrowseData==='function' && MANAGE_SUB==='browse'){
      loadBrowseData();
    }
  }
  catch(e){ toast('失败: '+e.message); }
}
async function toggleImageCache(v){
  try{
    await api(`/api/settings?image_cache=${v}`,{method:'POST'});
    IMG_CACHE = v;
    toast(v?'已开启图片本地缓存':'已关闭, 图片直连 TMDB');
    // 立即让当前页签按新开关重渲染(图片地址随之切换)
    const cur = document.querySelector('nav.tabs button.active');
    if(cur) switchTab(cur.dataset.tab);
  }
  catch(e){ toast('失败: '+e.message); }
}
async function toggleCheckMissingS0(v){
  try{
    await api(`/api/settings?check_missing_s0=${v}`,{method:'POST'});
    toast(v?'已开启 S0 缺失检测(特别篇缺集计入)':'已关闭 S0 缺失检测(只算正剧季)');
    // 管理-缺失 子页签若正显示, 立即按新口径刷新
    if(typeof loadMissing==='function' && MANAGE_SUB==='missing'){
      loadMissing();
    }
  }
  catch(e){ toast('失败: '+e.message); }
}
// 屏蔽入口: 先弹自定义输入框问原因(替代 prompt), 取消(null)则不动作
async function askBlock(kind, tmdbId, title, btn){
  const reason = await promptInput({
    title: '屏蔽此作品',
    icon: 'ban',
    sub: '屏蔽后将从热门榜、缺失、浏览列表里隐藏。原因可留空。',
    placeholder: '屏蔽原因(可选), 如: 不感兴趣 / 重复',
    okText: '屏蔽',
  });
  if(reason !== null) blockMedia(kind, tmdbId, title, reason, btn);
}
async function blockMedia(kind, tmdbId, title, reason, btn){
  if(btn){ btn.disabled=true; btn.innerHTML=icon('refresh')+'<span>屏蔽中…</span>'; }
  try{
    await api(`/api/blocklist?kind=${kind}&tmdb_id=${tmdbId}&title=${encodeURIComponent(title||'')}&reason=${encodeURIComponent(reason||'')}`,{method:'POST'});
    if(btn) btn.innerHTML=icon('check')+'<span>已屏蔽</span>';
    toast('已屏蔽');
    closeModal();
    // 热门榜现在应用屏蔽列表(后端已过滤), 当前页签是热门榜时立即刷新
    // ⚠️ 不能用 TREND_LIST(4 页签重构后已删除, 会抛 ReferenceError 被 catch 吞掉 →
    // 表现为"点屏蔽没反应/提示失败"), 必须用当前可见实例
    const _vt = _visibleTrend();
    if(_vt && _vt.list.length){ _vt.page = 1; loadTrendList(true, _vt.kind); }
  }
  catch(e){
    toast('屏蔽失败: '+e.message);
    if(btn){ btn.disabled=false; btn.innerHTML=icon('ban')+'<span>屏蔽此作品</span>'; }
  }
}
async function unblock(kind, tmdbId){
  try{ await api(`/api/blocklist?kind=${kind}&tmdb_id=${tmdbId}`,{method:'DELETE'}); toast('已取消屏蔽'); loadBlock(); }
  catch(e){ toast('失败: '+e.message); }
}

