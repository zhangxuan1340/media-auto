// MediaAuto 前端 — organize.js
// 整理: 离线目录整理计划 + 执行 + 整理记录
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

// ---- 文件整理 ----
// 流程: 预览整理计划(清广告 + 改名 标题.年份.tt/tmdb号 + 归位) → 勾选执行
const KIND_CN = {movie:'电影', tv:'剧集'};

async function loadOrganize(){
  const el = $('#tab-organize'); el.innerHTML='<div class="empty"><span class="spin"></span>读取离线目录并反查 TMDB 元数据…</div>';
  try{
    const data = await api('/api/organize/files');
    const plans = data.plans||[];
    const stats = data.stats||{};
    const head = statsCard(data, stats);
    // 整理记录区(落库, 重启不丢) —— 失败明细/note 在这里看, 不再只丢 console
    const logsHtml = `<div style="margin-top:16px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <strong style="font-size:13px">整理记录</strong>
          <span style="color:var(--muted);font-size:12px">最近执行的历史(服务重启不丢)</span>
          <button class="ghost sm" style="margin-left:auto" onclick="loadOrganizeLogs()">刷新</button>
        </div>
        <div id="org-logs"><div class="empty" style="padding:10px"><span class="spin"></span>加载…</div></div>
      </div>`;
    if(!plans.length){ el.innerHTML = head + '<div class="empty">离线目录里还没有可整理的内容</div>' + logsHtml; loadOrganizeLogs(); return; }

    const rows = plans.map((p,idx)=>{
      const isMerge = p.status==='merge';
      const can = p.status==='ok' || isMerge;
      const adList = (p.ad_files||[]).concat(p.junk_files||[]);
      const ads = adList.length
        ? `<span title="${esc(adList.map(a=>a.name).join('\n'))}">${adList.length} 个</span>`
        : '—';
      const whyColor = isMerge ? 'var(--brand)' : 'var(--warn)';
      const skipWhy = (!can && p.reason)
        ? `<br><small style="color:var(--warn)">${esc(p.reason)}</small>`
        : (isMerge && p.reason ? `<br><small style="color:${whyColor}">${esc(p.reason)}</small>` : '');
      const dest = p.target
        ? `<div><code>${esc(p.new_name)}</code><br><small style="color:var(--muted)">→ ${esc(p.target_root)}</small>${skipWhy}</div>`
        : `<div><small style="color:var(--muted)">${esc(p.reason||'—')}</small></div>`;
      const kind = p.kind ? KIND_CN[p.kind]||p.kind : '?';
      const idtxt = p.imdb_id || (p.tmdb_id? 'tmdb'+p.tmdb_id : '—');
      const isManual = p.matched_query === 'manual';
      const matchBtn = `<button class="ghost sm" onclick="openMatch(${idx})" title="自动反查配错了? 手动指定正确条目">${icon('puzzle')}重匹配</button>`;
      const manualTag = isManual ? ' <span class="badge lib">手动</span>' : '';
      const previewBtn = p.preview ? `<span class="org-toggle" id="orgTog${idx}" onclick="toggleOrgFiles(${idx})">▸ 文件</span>` : '';
      const action = p.status==='ok'
        ? `<button class="mbtn" onclick="applyOne(${idx}, this)">执行</button>`
        : (isMerge
          ? `<button class="mbtn" onclick="applyOne(${idx}, this)">补季</button>`
          // 升级版 = 库里已有同名但这条规格更高: 不自动换(/Cloud 禁删), 仅随「执行全部」清广告
          : (p.status==='upgrade'
            ? `<small style="color:var(--warn)">升级版</small>`
            : `<small style="color:var(--muted)">跳过</small>`));
      return `<tr>
        <td data-th="原目录名">${can?`<input type="checkbox" class="opick" data-i="${idx}"> `:''}${esc(p.name)} ${previewBtn}</td>
        <td data-th="正片">${fmt(p.media_bytes)} <small style="color:var(--muted)">${p.media_count} 个文件</small></td>
        <td data-th="广告/杂项">${ads}</td>
        <td data-th="新目录/库">${dest}</td>
        <td data-th="类型/ID">${kind}${manualTag} <small style="color:var(--muted)">${esc(idtxt)}</small></td>
        <td data-th="操作"><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${action}${matchBtn}</div></td>
      </tr>` + (p.preview ? `<tr class="org-detail" id="orgDet${idx}" style="display:none"><td colspan="6">${filePreviewHTML(p.preview)}</td></tr>` : '');
    }).join('');

    const okCount = plans.filter(p=>p.status==='ok'||p.status==='merge').length;
    el.innerHTML = head + `<div class="toolbar">
        <button onclick="loadOrganize()">${icon('refresh')}重新扫描</button>
        <button class="ghost" onclick="pickAll()">全选可整理 (${okCount})</button>
        <button onclick="applySelected(this)">${icon('play')}执行选中</button>
        <button class="ghost" onclick="applyAll(this)">${icon('play')}执行全部可整理</button>
        <button class="ghost" onclick="finishAll('all')">${icon('refresh')}刷新 Jellyfin</button>
      </div>
      <table><thead><tr><th>原目录名</th><th>正片</th><th>广告/杂项</th><th>→ 新目录名 / 库</th><th>类型 / ID</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>
      <p style="color:var(--muted);font-size:12px;margin-top:8px">规则: 删纯推广名广告 + 非视频杂项,剥掉文件名里的推广块,目录改名为 <code>标题 (年份)</code>,视频改名 <code>标题 (年份) 质量标记</code>,在 /Temp 工作区整理好后归位到 ${esc(data.movie_root||'/Cloud')} 或 ${esc(data.tv_root||'/Cloud')},最后写 NFO。库里已有同名条目会显示「跳过」(原因见目标列)。删除走 CD2 回收站,可恢复。</p>
      ${logsHtml}`;
    el._plans = plans;
    loadOrganizeLogs();
  }catch(e){ el.innerHTML=`<div class="empty">加载失败: ${e.message}</div>`; }
}

// ---- 整理记录(落库日志, 失败明细可见) ----
async function loadOrganizeLogs(){
  const box = $('#org-logs'); if(!box) return;
  try{
    const data = await api('/api/organize/logs?limit=30');
    const logs = data.logs||[];
    if(!logs.length){ box.innerHTML = '<div class="empty" style="padding:10px">还没有整理记录</div>'; return; }
    box.innerHTML = logs.map(l=>{
      const stColor = l.status==='success' ? 'var(--ok)' : l.status==='error' ? 'var(--err)' : 'var(--warn)';
      const stTxt = l.status==='success' ? '成功' : l.status==='error' ? '失败' : '进行中';
      const t = (l.started_at||'').replace('T',' ').slice(5,16);
      const cnt = `共 ${l.done??l.total??0} · 成功 ${l.ok??0} · 失败 ${l.failed??0}${l.skipped?` · 跳过 ${l.skipped}`:''}`;
      const detail = (l.results||[]).map(r=>{
        const line = r.ok
          ? `<span style="color:var(--ok)">✓</span> ${esc(r.name)}${r.new_name?` → ${esc(r.new_name)}`:''}`
          : `<span style="color:var(--err)">✗</span> ${esc(r.name)} <span style="color:var(--err)">— ${esc(r.error||'未知错误')}</span>`;
        const note = (r.note||'') ? `<div style="color:var(--warn);font-size:11px;margin-left:14px">${esc(r.note)}</div>` : '';
        return line + note;
      }).join('<br>');
      return `<details style="margin-bottom:6px;border:1px solid var(--line);border-radius:6px;padding:6px 10px">
        <summary style="cursor:pointer;font-size:12px">
          <span style="color:${stColor}">●</span> ${t} · <span style="color:${stColor}">${stTxt}</span>
          <span style="color:var(--muted)">· ${esc(l.scope||'')}</span> · ${cnt}
        </summary>
        <div style="margin-top:6px;font-size:12px;line-height:1.7">${l.error?`<div style="color:var(--err)">任务级错误: ${esc(l.error)}</div><br>`:''}${detail||'<span style="color:var(--muted)">(无明细)</span>'}</div>
      </details>`;
    }).join('');
  }catch(e){
    box.innerHTML = `<div class="empty" style="padding:10px">加载失败: ${esc(e.message)}</div>`;
  }
}

function statsCard(data, s){
  const q = s.quota||{};
  const quota = q.total!=null ? `配额 ${q.used??'-'}/${q.total??'-'}${q.left!=null?` (剩 ${q.left})`:''}` : '';
  const running = (s.running||[]).length;
  return `<div style="display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:8px">
      <span>离线根 <code>${esc(data.offline_root)}</code></span>
      ${quota?`<span>${esc(quota)}</span>`:''}
      ${running?`<span>进行中 ${running} 个</span>`:''}
    </div>`;
}
function pickAll(){ document.querySelectorAll('.opick').forEach(c=>c.checked=true); }
function _pickedNames(){
  const plans = $('#tab-organize')._plans||[];
  return Array.from(document.querySelectorAll('.opick')).filter(c=>c.checked)
    .map(c=>plans[+c.dataset.i]).filter(Boolean).map(p=>p.name);
}
let _applyTimer = null;
async function _apply(body, btn, label){
  if(btn){ btn.disabled=true; btn.textContent='启动中…'; }
  try{
    const r = await api('/api/organize/apply',{method:'POST',body:JSON.stringify(body)});
    if(btn){ btn.textContent='整理中…'; }
    _pollJob(r.job_id, btn, label);
  }catch(e){ if(btn){ btn.disabled=false; btn.innerHTML=label; } toast('失败: '+e.message); }
}
function _pollJob(jobId, btn, label){
  clearInterval(_applyTimer);
  _applyTimer = setInterval(async ()=>{
    if(document.hidden) return;   // 后台标签页不轮询(整理在后端继续跑, 回前台下一拍接着刷)
    try{
      const s = await api(`/api/organize/apply/${jobId}`);
      if(s.status==='running'){
        if(btn) btn.textContent=`整理中… ${s.done}/${s.count||'?'}`;
        return;
      }
      clearInterval(_applyTimer);
      if(btn){ btn.disabled=false; btn.innerHTML=label; }
      if(s.status==='error'){ toast('整理失败: '+(s.error||'未知错误')); await loadOrganize(); return; }
      const okN = (s.results||[]).filter(x=>x.ok).length;
      const skipped = (s.results||[]).filter(x=>x.skipped).length;
      const errs = (s.results||[]).filter(x=>!x.ok);
      const note = s.note ? ` · ${s.note}` : '';
      toast(`完成 ${okN}/${s.count} 个${skipped?`(${skipped} 个已存在跳过)`:''}${errs.length?` / 失败 ${errs.length}`:''}${note}`);
      if(errs.length) console.warn('整理失败明细', errs);
      await loadOrganize();
    }catch(e){
      clearInterval(_applyTimer);
      if(btn){ btn.disabled=false; btn.innerHTML=label; }
      toast('失败: '+e.message);
    }
  }, 1500);
}
async function applyOne(idx, btn){
  const p = ($('#tab-organize')._plans||[])[idx]; if(!p) return;
  await _apply({names:[p.name]}, btn, '执行');
}
async function applySelected(btn){
  const names = _pickedNames();
  if(!names.length){ toast('请先勾选要整理的条目'); return; }
  // 用自定义确认弹窗而非原生 confirm: iOS 等环境原生 confirm 可能被静默吞掉 → 点了没反应
  if(!await confirmBox({icon:'play', okText:'执行整理',
    title:`执行整理 ${names.length} 个条目`,
    sub:'将删除广告文件、重命名目录并移动到媒体库。'})) return;
  // limit 必须带上: 服务端会按它截断, 不传就吃默认值 → 勾多了被静默截断
  await _apply({names, limit:names.length}, btn, icon('play')+'执行选中');
}
async function applyAll(btn){
  // 口径与服务端 _worker 一致: ok/merge 归位, duplicate/upgrade 只清广告
  const runSt = ['ok','merge','duplicate','upgrade'];
  const plans = $('#tab-organize')._plans||[];
  const n = plans.filter(p=>runSt.includes(p.status)).length;
  const moveN = plans.filter(p=>p.status==='ok'||p.status==='merge').length;
  if(!n){ toast('没有可整理的条目'); return; }
  if(!await confirmBox({icon:'play', okText:'全部执行',
    title:`执行全部 ${n} 个条目`,
    sub:`其中 ${moveN} 个改名归位, 其余仅清广告。将删除广告文件、重命名目录并移动到媒体库。`})) return;
  await _apply({all:true, limit:n}, btn, '▶▶ 执行全部可整理');
}
async function finishAll(mode){
  try{ const ok = await api('/api/organize/finish',{method:'POST',body:JSON.stringify({mode})}); toast(ok.msg||'已触发'); }
  catch(e){ toast('失败: '+e.message); }
}

// ---- 手动重匹配(自动反查错配时, 如 基督山伯爵.2024 被反查到 1961 版同名条目) ----
// 弹窗复用全局 .modal(detail.js 的 #modalBg/#modal); 候选来自 GET /api/organize/match,
// 选定后 POST /api/organize/match 持久化到 DB(重启不丢), 之后每次扫描/执行都以手动指定为准。
let _matchCtx = null;    // {name, query}
let _matchCands = [];    // 候选卡片
let _matchSel = -1;      // 选中下标
let _matchKind = '';     // 候选过滤: '' = 全部, movie, tv
function openMatch(idx){
  const p = (($('#tab-organize')._plans)||[])[idx]; if(!p) return;
  _matchCtx = {name: p.name}; _matchSel = -1; _matchKind = p.kind || '';
  _openModal(false);   // 不压 history(弹窗内操作, 返回键不该多退一层)
  _navTitle('重新匹配');
  _syncCloseBtn();
  const isManual = p.matched_query === 'manual';
  $('#mBody').innerHTML = `
    <div class="row" style="color:var(--muted);font-size:13px;line-height:1.6;margin-bottom:14px">
      原目录名 <b style="color:var(--text)">${esc(p.name)}</b>
      ${p.tmdb_id?` · 当前匹配 <b>${esc(p.title||'')}</b> (${esc(p.year||'?')}) ${isManual?'<span class="badge lib">手动指定</span>':''}`:' · 当前未匹配到条目'}<br>
      <small>自动反查把该片名配错了? 搜索并手动选定正确的版本, 保存后每次扫描/执行都以它为准。</small>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <input id="matchQ" value="${esc((p.matched_query==='manual')?'':p.name)}" placeholder="换个片名/原名再搜"
        style="flex:1;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:16px"
        onkeydown="if(event.key==='Enter')matchSearch()">
      <select id="matchKind" style="font-size:16px" onchange="_matchKind=this.value;matchSearch()">
        <option value="">全部类型</option><option value="movie">电影</option><option value="tv">剧集</option>
      </select>
      <button class="mbtn" onclick="matchSearch()">搜索</button>
    </div>
    <div id="matchCands"><div class="empty"><span class="spin"></span>点击「搜索」拉取候选…</div></div>
    ${isManual?'<button class="ghost" style="margin-top:14px" onclick="matchClear()">'+icon('trash')+'取消手动指定(恢复自动反查)</button>':''}
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="mbtn" id="matchSaveBtn" disabled onclick="matchSave()">${icon('check')}保存手动匹配</button>
      <button class="ghost" onclick="closeModal()">取消</button>
    </div>`;
  const mk = $('#matchKind'); if(mk) mk.value = _matchKind;
  const mq = $('#matchQ'); if(mq && !mq.value.trim()) mq.value = p.name;
  matchSearch();   // 自动先搜一遍(用条目名)
}
async function matchSearch(){
  if(!_matchCtx) return;
  const box = $('#matchCands'); if(!box) return;
  const q = ($('#matchQ')||{}).value || _matchCtx.name;
  _matchKind = ($('#matchKind')||{}).value || '';
  box.innerHTML = '<div class="empty"><span class="spin"></span>搜索候选中…</div>';
  try{
    const d = await api(`/api/organize/match?name=${encodeURIComponent(_matchCtx.name)}&query=${encodeURIComponent(q)}`);
    _matchCands = (d.results||[]).filter(c => !_matchKind || c.kind===_matchKind);
    if(!_matchCands.length){ box.innerHTML = '<div class="empty">没有候选(换个关键词/类型试试)</div>'; return; }
    box.innerHTML = _matchCands.map((c,i)=>{
      const k = c.kind==='tv' ? '<span class="badge">剧集</span>' : '<span class="badge">电影</span>';
      const poster = c.poster ? `<img src="${img(c.poster)}" style="width:44px;height:60px;object-fit:cover;border-radius:8px;background:var(--poster-bg)" loading="lazy"/>` : '';
      return `<div class="res match-cand" onclick="matchPick(${i})" style="cursor:pointer">
        ${poster}
        <div class="info"><div class="n">${esc(c.title)} <small style="color:var(--muted)">(${esc(c.year||'?')})</small></div>
          <div class="s">${k}${c.vote?` <span class="badge">${icon('star')}${c.vote}</span>`:''}${c.imdb_id?` <small style="color:var(--muted)">${esc(c.imdb_id)}</small>`:''}</div></div>
      </div>`;
    }).join('');
    _matchSel = -1;
    const sb = $('#matchSaveBtn'); if(sb) sb.disabled = true;
  }catch(e){ box.innerHTML = `<div class="empty">搜索失败: ${esc(e.message)}</div>`; }
}
function matchPick(i){
  _matchSel = i;
  document.querySelectorAll('#matchCands .match-cand').forEach((el,j)=>el.style.outline = (j===i)?'2px solid var(--brand)':'none');
  const sb = $('#matchSaveBtn'); if(sb) sb.disabled = false;
}
async function matchSave(){
  if(_matchSel<0 || !_matchCands[_matchSel]) return;
  const c = _matchCands[_matchSel];
  const btn = $('#matchSaveBtn'); if(btn) btn.disabled = true;
  try{
    const r = await api('/api/organize/match',{method:'POST',body:JSON.stringify({
      name: _matchCtx.name, tmdb_id: c.tmdbId, kind: c.kind, title: c.title, year: c.year||''
    })});
    toast(r.msg || '已保存手动匹配');
    closeModal();
    await loadOrganize();
  }catch(e){ toast('保存失败: '+e.message); if(btn) btn.disabled = false; }
}
async function matchClear(){
  if(!_matchCtx) return;
  try{
    const r = await api('/api/organize/match',{method:'POST',body:JSON.stringify({name: _matchCtx.name, tmdb_id: 0})});
    toast(r.msg || '已取消手动指定');
    closeModal();
    await loadOrganize();
  }catch(e){ toast('操作失败: '+e.message); }
}

// ---- 整理计划「预期文件列表」折叠预览(后端 preview_plan_files 已算好每文件预期改名/落点) ----
function toggleOrgFiles(idx){
  const det = $('#orgDet'+idx); if(!det) return;
  const tog = $('#orgTog'+idx);
  const open = det.style.display === 'none';
  det.style.display = open ? '' : 'none';
  if(tog) tog.textContent = open ? '▾ 文件' : '▸ 文件';
}
function filePreviewHTML(prev){
  const files = prev.media||[];
  const del = prev.deletes||[];
  const keep = prev.keeps||[];
  let h = `<div class="org-files">`;
  if(prev.target){
    h += `<div class="of-head">预期落点 <code>${esc(prev.target)}</code>`;
    if(prev.merge_existing) h += ` <span class="badge warn">并入已有剧目录(按季合并)</span>`;
    if(prev.quality) h += ` <span class="of-q">质量标记: ${esc(prev.quality)}</span>`;
    else h += ` <span class="of-q muted">质量标记为文件名推断(执行时若可探测会更准)</span>`;
    h += `</div>`;
  }
  if(files.length){
    h += `<div class="of-sub">媒体文件 · 预期改名 / 落点</div>`;
    h += files.map(f=>{
      const same = f.dst === f.src;
      const dstShow = f.dst_rel || f.dst;
      return `<div class="of-row">
        <span class="of-src" title="${esc(f.src)}">${esc(f.src)}</span>
        <span class="of-arrow">→</span>
        <span class="of-dst${same?' same':''}" title="${esc(f.dst_path)}">${esc(dstShow)}</span>
        ${f.note?`<span class="of-note">${esc(f.note)}</span>`:''}
      </div>`;
    }).join('');
  }
  if(del.length){
    h += `<div class="of-sub">将删除 · 广告/杂项(进 CD2 回收站可恢复)</div>`;
    h += `<div class="of-chips">${del.map(n=>`<span class="of-chip del">${esc(n)}</span>`).join('')}</div>`;
  }
  if(keep.length){
    h += `<div class="of-sub">保留 · 库内资产</div>`;
    h += `<div class="of-chips">${keep.slice(0,40).map(n=>`<span class="of-chip">${esc(n)}</span>`).join('')}${keep.length>40?`<span class="of-chip">…等 ${keep.length} 个</span>`:''}</div>`;
  }
  if(!files.length && !del.length && !keep.length){
    h += `<div class="of-empty">无可用预览信息</div>`;
  }
  h += `</div>`;
  return h;
}

