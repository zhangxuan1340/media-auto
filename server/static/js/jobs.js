// MediaAuto 前端 — jobs.js
// 管理页「作业」子页签: 作业表 + 缓存表(对齐 Seerr 的 Settings → Jobs & Cache)
//   作业表: 作业名 | 作业类型 | 下一次执行时间 | 编辑 | 执行
//   缓存表: 缓存名 | 击中数 | 失误数 | 键数 | 键储存大小 | 值储存大小 | 清除缓存
// 关键语义(与 Seerr 一致): **手动运行任务不会改变它的时间表**。
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

let _jobsTimer = null;

// 进入子页签时渲染(由 settings.js switchManageSub 路由)
async function loadJobs(){
  const el = $('#manageBody');
  el.innerHTML = `<div class="empty"><span class="spin"></span>读取作业状态…</div>`;
  try{
    const [jobs, cache] = await Promise.all([api('/api/jobs'), api('/api/cache')]);
    el.innerHTML = `
      <div class="toolbar">
        <span style="font-size:13px">共 <b>${jobs.length}</b> 个作业 · 到点自动执行</span>
        <button class="mbtn ghost sm" onclick="loadJobs()">${icon('refresh')}刷新</button>
      </div>
      <p class="hint-line">维护任务按下面的周期定期执行, 也可以随时手动触发 ——
        <b>手动运行不会改变它的时间表</b>。</p>
      ${_jobsTable(jobs)}
      ${_cacheTable(cache)}`;
  }catch(e){
    el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
}

function _jobsTable(jobs){
  return `<h3 class="sec-title">${icon('clock')}作业</h3>
    <table><thead><tr>
      <th>作业名</th><th>类型</th><th>下一次执行时间</th><th>周期</th><th>上次运行</th><th></th>
    </tr></thead><tbody>`
    + jobs.map(j=>{
      const bits = [];
      if(j.isScheduleCustom) bits.push(`<span class="badge warn">已自定义</span>`);
      if(j.running) bits.push(`<span class="badge ok">运行中</span>`);
      else if(j.lastStatus==='error') bits.push(`<span class="badge err" title="${esc(j.lastError||'')}">上次失败</span>`);
      return `<tr>
        <td data-th="作业名">${esc(j.name)}${bits.length?' '+bits.join(' '):''}
          <div class="row-sub">${esc(j.desc||'')}</div></td>
        <td data-th="类型">${j.type==='command'?'命令':'程序'}</td>
        <td data-th="下一次执行时间">${_relTime(j.nextExecutionTime)}</td>
        <td data-th="周期"><code class="cron">${esc(j.cronSchedule)}</code>
          <div class="row-sub">${esc(j.describe||'')}</div></td>
        <td data-th="上次运行">${j.lastRunTime?fmtTime(j.lastRunTime):'—'}</td>
        <td data-th="">
          <button class="mbtn ghost sm" onclick="editJobSchedule('${j.id}')">编辑</button>
          <button class="mbtn sm" onclick="runJob('${j.id}')"${j.running?' disabled':''}>执行</button>
        </td>
      </tr>`;
    }).join('')
    + `</tbody></table>`;
}

function _cacheTable(c){
  const rows = (c.apiCaches||[]).slice();
  const img = (c.imageCache||{}).tmdb;
  if(img) rows.push({id:'image', name:'The Movie Database (tmdb) 图片', stats:img});
  return `<h3 class="sec-title">${icon('database')}缓存</h3>
    <p class="hint-line">击中 = 直接由本地缓存供上(未走网络); 失误 = 实际请求了上游。
      统计为进程内计数, 服务重启后归零。</p>
    <table><thead><tr>
      <th>缓存名</th><th>击中数</th><th>失误数</th><th>键数</th>
      <th>键储存大小</th><th>值储存大小</th><th></th>
    </tr></thead><tbody>`
    + rows.map(r=>{
      const s = r.stats||{};
      return `<tr>
        <td data-th="缓存名">${esc(r.name)}</td>
        <td data-th="击中数">${(s.hits||0).toLocaleString()}</td>
        <td data-th="失误数">${(s.misses||0).toLocaleString()}</td>
        <td data-th="键数">${(s.keys||0).toLocaleString()}</td>
        <td data-th="键储存大小">${fmtBytes(s.ksize||0)}</td>
        <td data-th="值储存大小">${fmtBytes(s.vsize||0)}</td>
        <td data-th=""><button class="mbtn ghost err sm" onclick="flushCache('${r.id}','${esc(r.name)}')">清除缓存</button></td>
      </tr>`;
    }).join('')
    + `</tbody></table>`;
}

// ---- 相对时间(Seerr 显示"3分钟后"这类) ----
function _relTime(iso){
  if(!iso) return '—';
  const t = new Date(iso).getTime();
  if(isNaN(t)) return '—';
  const diff = Math.round((t - Date.now())/1000);
  const abs = Math.abs(diff);
  let txt;
  if(abs < 45) txt = '现在';
  else if(abs < 3600) txt = Math.round(abs/60) + ' 分钟';
  else if(abs < 86400) txt = Math.round(abs/3600) + ' 小时';
  else txt = Math.round(abs/86400) + ' 天';
  if(txt === '现在') return '<b>即将执行</b>';
  return diff > 0 ? `<b>${txt}后</b>` : `${txt}前(待补跑)`;
}

function fmtBytes(n){
  n = Number(n)||0;
  if(n < 1024) return n + ' B';
  const u = ['KB','MB','GB','TB'];
  let i = -1;
  do{ n /= 1024; i++; }while(n >= 1024 && i < u.length-1);
  return n.toFixed(2) + ' ' + u[i];
}

// ---- 执行(手动触发; 不改变时间表) ----
async function runJob(id){
  try{
    await api('/api/jobs/'+encodeURIComponent(id)+'/run', {method:'POST'});
    toast('已触发执行, 稍后在下方或「同步记录」查看结果');
    setTimeout(()=>{ if(window.MANAGE_SUB==='jobs') loadJobs(); }, 1500);
  }catch(e){ toast('执行失败: '+e.message); }
}

// ---- 编辑周期(自定义 modal, 移动端禁用 prompt) ----
async function editJobSchedule(id){
  let jobs = [];
  try{ jobs = await api('/api/jobs'); }catch(e){ toast('读取失败: '+e.message); return; }
  const j = jobs.find(x=>x.id===id);
  if(!j) return;
  const val = await promptInput({
    title:`修改「${j.name}」执行周期`, icon:'clock',
    sub:`6 段 cron: 秒 分 时 日 月 周。例: 0 */5 * * * * (每 5 分钟) / 0 0 3 * * * (每日 03:00)`,
    value:j.cronSchedule, placeholder:'0 */5 * * * *', okText:'保存'
  });
  if(val === null) return;
  try{
    await api('/api/jobs/'+encodeURIComponent(id)+'/schedule',
              {method:'POST', body: JSON.stringify({schedule: val})});
    toast('周期已更新');
    loadJobs();
  }catch(e){ toast('保存失败: '+e.message); }
}

// ---- 清空缓存 ----
async function flushCache(id, name){
  if(!confirm(`确定清空「${name}」?`)) return;
  try{
    const r = await api('/api/cache/'+encodeURIComponent(id)+'/flush', {method:'POST'});
    toast(`已清空, 移除 ${r.removed} 项`);
    loadJobs();
  }catch(e){ toast('清空失败: '+e.message); }
}
