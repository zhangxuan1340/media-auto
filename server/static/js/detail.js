// MediaAuto 前端 — detail.js
// 详情弹窗: 导航栈/侧滑返回/作品详情/演员页/磁力搜索与推送/分集扫种子
// 全局作用域(classic script), 依赖 common.js 工具函数, 由 index.html 按序加载

function openCard(it){
  // 有上级视图(搜索页/演员页) → 压栈, 详情页"返回"逐级回到上级
  if(_ctxStack.length){
    const ctx = _ctxStack.pop();   // 消费, 防重复压栈
    _navStack.push(()=>{ _openModal(); _ctxRebuild(ctx); });
  }
  openDetailLocal(it.kind, it.tmdbId, true);   // 用户进详情 = 新视图, history 压一层
}

// 导航上下文栈: 元素 = {type:'search',q} | {type:'person',id} —— 进入新视图时消费栈顶重建上级
let _navStack = [];          // 详情页的"返回目标"栈(重渲染闭包), 返回键逐级回退
let _ctxStack = [];          // 当前顶层视图(搜索页/演员页)重建上下文, 从它点进详情时消费
let _curDetail = null;       // 当前详情页 {kind,tmdbId}: 从详情点演员/导演进演员页时, "返回"回该详情
const _detailPayload = {};    // {tmdbId: 详情完整载荷} —— 详情刚拉过, 后续「扫种子」等直接复用
function _ctxRebuild(ctx){
  if(ctx && ctx.type === 'person') openPersonPage(ctx.id, true);
  else if(ctx && ctx.type === 'search') openTitleSearch(ctx.q);
}
// 演员页
async function openPersonPage(id, rebuild=false){
  // 从作品"返回"时恢复的滚动位置(openCardIdxP 离开前存好 —— 此时 modal 还是演员页)
  const savedScroll = _personScroll[id] || 0;
  _openModal(!rebuild);      // 新视图压 history; 从详情/作品"返回"重建时不压(否则要多退好几层)
  if(!rebuild){
    // 压"返回"目标: 从搜索/演员等顶层视图来 → 回该视图; 从详情页来(点演职员) → 回当前详情
    if(_ctxStack.length){
      const ctx = _ctxStack.pop();
      _navStack.push(()=>{ _openModal(); _ctxRebuild(ctx); });
    } else if(_curDetail){
      const dd = _curDetail;
      _navStack.push(()=>{ openDetailLocal(dd.kind, dd.tmdbId, false); });
    }
  }
  _syncCloseBtn();   // 栈定案(新压/重建不压) → 同步右上按钮 返回/X
  _navTitle('演员');
  _personCurId = id;
  const cached = _personCache.has(id);
  let p = _personCache.get(id) || null;   // 命中缓存: 返回时不重新请求(无闪烁/无刷新)
  $('#mBody').innerHTML = p ? '' : '<div class="empty"><span class="spin"></span>加载演员…</div>';
  if(!p){
    try{
      p = await api(`/api/person/${id}`);
    }catch(e){
      $('#mBody').innerHTML=`<div class="empty">演员加载失败: ${e.message}</div>`;
      return;
    }
    _personCache.set(id, p);
  }
  const facts = [
    p.birthday ? (p.deathday ? `${p.birthday} – ${p.deathday}` : `生于 ${p.birthday}`) : null,
    p.placeOfBirth, p.imdbId ? 'IMDb '+p.imdbId : null,
  ].filter(Boolean);
  const bio = p.biography
    ? `<p class="person-bio">${esc(p.biography)}</p>`
    : '<p class="person-bio" style="color:var(--muted)">暂无简介</p>';
  // 作品卡片统一进 _personWorks, 点击按索引 openCard, 避免 JSON 内联到 HTML 属性
  const works = (kindLabel, list, baseIdx) => list.length
    ? `<div class="person-works">${list.map((c,i)=>{
        const poster = c.poster ? `<img class="poster" src="${img(c.poster)}" loading="lazy" alt=""/>` : '<div class="poster empty">无海报</div>';
        const badge = c.kind==='tv' ? '<span class="badge">剧集</span>' : '<span class="badge">电影</span>';
        const vote = c.vote ? `<span class="badge">${icon('star')}${c.vote}</span>` : '';
        // 库内已有的标"库内"(绿色); 库外不标(2026-09-19 用户要求: 存在写存在就行, 不存在不用标注)
        const lib = c.inLibrary ? '<span class="badge lib">库内</span>' : '';
        return `<div class="card" style="--i:${(baseIdx+i)%12}" onclick="openCardIdxP(${baseIdx+i})">${poster}
          <div class="meta"><div class="title">${esc(c.title)}</div>
          <div class="sub"><span>${esc(c.year||'')}</span></div>
          <div>${badge}${vote}${lib}</div>
          ${c.character?`<div class="char">饰：${esc(c.character)}</div>`:''}</div></div>`;
      }).join('')}</div>`
    : `<div class="person-empty">${kindLabel}: 暂无</div>`;
  const movies = p.movies||[], tv = p.tv||[];
  _personWorks = movies.concat(tv);
  _ctxStack = [{type:'person', id}];   // 点作品进详情后, 返回可回到本页
  $('#mBody').innerHTML = `
    <div class="person-head">
      ${p.profile ? `<img class="person-avatar" src="${img(p.profile)}" alt=""/>` : '<div class="person-avatar ph">?</div>'}
      <div style="flex:1;min-width:0">
        <h2 class="person-name">${esc(p.name)}</h2>
        ${facts.length?`<div class="person-facts">${facts.map(esc).join('<span>·</span>')}</div>`:''}
      </div>
      <button class="mbtn" id="trackPersonBtn" data-track-key="person_${id}" data-track-label="追踪新作" data-name="${esc(p.name)}" style="white-space:nowrap" onclick="toggleTrackPerson(${id}, this)">${icon('bell')}<span>追踪新作</span></button>
    </div>
    ${bio}
    <h3>${icon('film')} 电影 (${movies.length})</h3>
    ${works('电影', movies, 0)}
    <h3 style="margin-top:14px">${icon('tv')} 剧集 (${tv.length})</h3>
    ${works('剧集', tv, movies.length)}`;
  _refreshTrackBtns();   // 按最新状态刷新"追踪新作"按钮文案
  // 缓存命中(从作品详情"返回")→ 恢复滚动位置, 不跳回顶部
  if(cached){ $('#modal').scrollTop = savedScroll; $('#modalBg').scrollTop = savedScroll; }
  delete _personScroll[id];   // 消费即删: 避免下次重新打开时误恢复到旧位置
}
let _personWorks = [];
const _personCache = new Map();   // 演员页数据缓存: 返回时不重新请求, 避免整页刷新
let _personCurId = null;          // 当前演员页的 person id
const _personScroll = {};         // 演员页滚动位置: 点作品离开时存, 返回时恢复
function openCardIdxP(i){
  const c=_personWorks[i]; if(!c) return;
  // 此时 modal 还是演员页 —— 记下滚动位置, 从作品详情"返回"时恢复
  if(_personCurId!=null) _personScroll[_personCurId] = $('#modal').scrollTop + $('#modalBg').scrollTop;
  openCard(c);
}
// ---- 移动端侧滑返回: 应用内视图挂到浏览器 history ----
// iOS Safari 侧滑 = 浏览器后退 = popstate。每个应用内视图(详情/搜索页)打开时 pushState 压一层,
// 侧滑先逐级回退(详情→搜索页→关闭), 全部走完才真正退到上一个网页。
// 不靠手动计数: popstate 后按「modal 是否开 + _navStack 是否空」决定回退目标, 天然幂等。
let _histToken = '';         // 最近一次压栈的标记(调试用; 回退判定不依赖它)
let _popGuard = 0;           // 侧滑防抖: iOS 快速侧滑可能连发 popstate, 250ms 内忽略重复
window.addEventListener('popstate', ()=>{
  const modalOpen = $('#modalBg').classList.contains('show');
  if(!modalOpen) return;                 // 没有应用内视图 → 让浏览器正常后退(退到其他网页)
  const now = Date.now();
  if(now - _popGuard < 250) return;      // 防抖
  _popGuard = now;
  if(_navStack.length){ _navStack.pop()(); }  // 详情且有上级搜索页 → 回搜索页
  else closeModal();                     // 最底层视图 → 关闭详情
  _syncCloseBtn();                       // 回退后栈变浅 → 右上按钮可能从 返回 变回 X
});
function _histPush(){
  _histToken = 'm' + Date.now() + Math.random().toString(36).slice(2,7);
  try{ history.pushState({ma: _histToken}, ''); }catch(e){}
}
function _openModal(pushHist){
  // pushHist=true 仅「用户打开新视图」时传(列表/搜索页→详情、进入搜索页);
  // 原地刷新(分集同步后重拉)、侧滑重建搜索页 不压 —— 否则侧滑要多退好几层才走。
  $('#modalBg').classList.add('show');
  if(pushHist) _histPush();
  // 锁死背景页滚动 —— 否则手机下拉会带动底下的页面, 而非滚动详情(用户反馈的"拉的是底下的页面")
  document.body.style.overflow = 'hidden';
  // 滚动容器: 移动端=.modal(整页), 桌面=.modal-bg(弹窗外滚) —— 两个都回顶
  $('#modal').scrollTop = 0;
  $('#modalBg').scrollTop = 0;
  _navTitle('详情');
}
function _navTitle(t){ const el=$('#mNavTitle'); if(el) el.textContent = t||'详情'; }
// 右上角上下文按钮同步: 有上级视图 → 返回箭头(逐级回退); 最底层 → X(关闭)。
// 移动端左上返回键(_navBack)与桌面右上按钮共用 _navBack(), 图标状态与移动端行为一致。
function _syncCloseBtn(){
  const b = $('#mClose'); if(!b) return;
  const hasNav = _navStack.length > 0;
  b.classList.toggle('has-nav', hasNav);
  b.title = hasNav ? '返回上一级' : '关闭详情';
}
function _navBack(){
  // 有上级视图(如 搜索页) → 回退并重建; 否则关闭整个详情页
  if(_navStack.length){ const r = _navStack.pop(); r(); }
  else closeModal();
  _syncCloseBtn();
}

async function openDetailLocal(kind, tmdbId, pushHist){
  PUSH_CONTENT_TYPE = kind;
  _openModal(pushHist);
  _syncCloseBtn();   // 栈由调用方预设(openCard 消费 _ctxStack / openPersonPage 压详情目标) → 此处定案
  _hero('加载中…');
  $('#mBody').innerHTML='<div class="empty"><span class="spin"></span>加载详情…</div>';
  let d;
  try{
    d = await api(`/api/browse/detail/${kind}/${tmdbId}`);
  }catch(e){
    // 本地缓存没有(热门/搜索命中的片未必在 Jellyfin 库, 不在同步种子范围)
    // → 现拉 TMDB 并写入本地缓存
    if(e.message && e.message.includes('本地缓存无')){
      _hero('本地没有, 现拉 TMDB…');
      $('#mBody').innerHTML='<div class="empty"><span class="spin"></span>本地缓存没有, 正在从 TMDB 实时拉取…</div>';
      try{
        d = await api(`/api/browse/detail/${kind}/${tmdbId}/pull`,{method:'POST'});
      }catch(e2){
        $('#mBody').innerHTML=`<div class="empty">实时拉取失败: ${esc(e2.message)}<br><span style="font-size:12px">若未配置 tmdb.api_key, 请到「管理 → 通用 → TMDB」填写</span></div>`;
        return;
      }
    } else {
      $('#mBody').innerHTML=`<div class="empty">详情加载失败: ${e.message}</div>`;
      return;
    }
  }
  renderDetailLocal(d, kind, tmdbId);
}

function renderDetailLocal(d, kind, tmdbId){
  // 供"详情→点演员→返回"定位回该详情; title/originalTitle/year 给「改标题 → 重搜磁力/重命名」用
  _curDetail = {kind, tmdbId, title: d.title, originalTitle: d.originalTitle, englishTitle: d.englishTitle, year: d.year, inLibrary: !!d.inLibrary};
  _detailPayload[String(tmdbId)] = d;   // 快照:「扫种子」等后续操作复用, 不再重复请求
  _ctxStack = [];                // 详情是叶子视图: 清掉上级视图上下文, 避免点演员时误用陈旧的搜索/演员上下文
  // 2026-09: 移除顶部 hero 背景横条 —— 标题/年份/评分/类型全部由正文 .dtitle 承载(桌面+移动统一, 不再占篇幅)
  _navTitle(d.title);
  const genres = (d.genres||[]).map(g=>`<span>${esc(g)}</span>`).join('');
  const poster = d.poster ? `<img class="poster" src="${img(d.poster)}" alt=""/>` : '';
  // 分集缺失块(剧集)
  let seasonHTML = '';
  if(kind==='tv'){
    if(d.episodesSynced===false){
      // ⚠️ Jellyfin 分集明细未同步: 不能逐集对比(否则在库剧全判"缺全部集")。
      // 显示提示+同步按钮, 同步完成后自动刷新真实缺失。
      seasonHTML = `<h3>${icon('tv')}分集状态</h3>
        <div class="empty" style="padding:16px">
          <div style="font-size:13px">尚未同步 Jellyfin 分集明细, 无法判断分集缺失(避免误报"缺全部集")。</div>
          <button class="mbtn ghost" id="epSyncBtn" style="margin-top:10px" onclick="syncEpisodesAndRefresh('${kind}',${tmdbId})">${icon('refresh')}同步分集明细并刷新</button>
          <div id="epSyncNote" style="font-size:12px;color:var(--muted);margin-top:8px"></div>
        </div>`;
    } else if(d.seasons){
    // 逐季折叠: 每季一条(完整=绿勾, 不完整=进度横条), 点开看逐集(拥有打勾/缺失空圈)
    const seasonBlocks = d.seasons.map(s=>{
      const S = String(s.number).padStart(2,'0');
      // numbersKnown=false: 该季没有 TMDB 真实集号 → 不猜缺/多, 显示"编号未同步"
      const known = s.numbersKnown !== false;
      const complete = known && s.missing===0 && s.expected>0;
      const pct = s.expected>0 ? Math.round(s.have/s.expected*100) : 0;
      const barColor = !known ? 'var(--muted)' : (complete ? 'var(--ok)' : (s.have>0 ? 'var(--warn)' : 'var(--err)'));
      const autoOpen = !!(s.inProduction && s.missing>0);   // 在播且缺集: 默认展开(与自动扫种子联动)
      const missingTag = !known
        ? '<span class="badge warn" title="TMDB 真实集号尚未同步, 暂不判断缺/多(避免误报)">编号未同步</span>'
        : (s.missing>0 ? `<span class="badge err">缺${s.missing}</span>` : (complete?'<span class="badge ok">完整</span>':''));
      return `<div class="seas" id="seas-${s.number}" data-open="${autoOpen?'1':'0'}">
        <div class="seas-head" onclick="toggleSeason(${tmdbId},${s.number},this)">
          <span class="seas-check${complete?' ok':''}">${icon('check')}</span>
          <span class="seas-name">S${S} ${esc(s.name||'')}${s.inProduction?' <span class="badge pend">在播</span>':''}${s.extra>0?` <span class="badge warn">多${s.extra}</span>`:''}</span>
          ${missingTag}
          <span class="seas-count" title="已有 / 应有">${s.have}/${s.expected}</span>
          <span class="seas-bar" title="拥有进度 ${pct}%"><i style="width:${pct}%;background:${barColor}"></i></span>
          <span class="seas-chev"><svg class="icn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
        </div>
        <div class="seas-body">
          <div class="seas-eps" id="seasonEps-${s.number}"><div class="empty" style="padding:10px"><span class="spin"></span>加载分集…</div></div>
          ${s.missing>0?`<div class="seas-foot">
            <button class="mbtn sm" onclick="scanSeasonSeads(${tmdbId},${s.number},this)">${icon('magnet')}扫种子</button>
            <div id="seasonSeeds-${s.number}" class="magbox" style="display:none"></div></div>`:''}
        </div></div>`;
    }).join('');
    seasonHTML = `<h3>${icon('tv')}分集状态(本地 Jellyfin vs TMDB)</h3>
      <div class="seas-summary">已有 ${d.haveEpisodes} 集 / 应有 ${d.tmdbEpisodes} 集${d.numbersSynced===false?` · <span class="badge warn" title="TMDB 真实集号尚未同步, 暂不判断缺/多(避免误报)">编号未同步</span>`:''}${d.missingCount?` · <span style="color:var(--err)">缺 ${d.missingCount} 集</span>`:''}${d.extraCount?` · <span style="color:var(--warn)">多 ${d.extraCount} 集(版本差异)</span>`:''} · 点季展开逐集明细</div>
      ${seasonBlocks}`;
    // 默认展开的季(在播且缺集): 立即加载逐集; 自动扫种子(最多 2 个, 避免一次打太多搜索)
    // ⚠️ 函数名是 scanSeasonSeads(与定义/手动按钮 onclick 一致), 不是 Seeds
    d.seasons.filter(s=>s.inProduction && s.missing>0).slice(0,2)
      .forEach(s=>{ loadSeasonEps(tmdbId, s.number); scanSeasonSeads(tmdbId, s.number, null, true); });
    }
  }
  // 2026-09 三版头部(iOS 26): 评分独立玻璃徽章; 元信息网格化(短字段进 facts 卡,
  // 长字段 原名/导演/主演 仍走 meta-line); 简介超长折叠; 各分区 .rise 逐段浮现。
  // 2026-09: TMDB/IMDb 直接做进 facts 超链接(不再单独跳转按钮行)。
  const _tmdbUrl = d.tmdbId ? `https://www.themoviedb.org${kind==='tv'?'/tv/':'/movie/'}${d.tmdbId}` : null;
  const _imdbUrl = d.imdb_id ? `https://www.imdb.com/title/${d.imdb_id}/` : null;
  // 地区: 主口径是服务端 regionKey/regionLabel(lib.classify 同一套, 含用户在
  // 「分类规则」里配的地区档 —— 归类和显示必须同一口径, 前端复刻一份必然漂移)。
  // 老缓存没有这两个字段时才退回本页硬编码的语言/国家映射(纯兜底)。
  const _CNAME = {'CN':'中国','HK':'香港','TW':'台湾','US':'美国','GB':'英国','FR':'法国','DE':'德国','JP':'日本','KR':'韩国','TH':'泰国','VN':'越南','ID':'印尼','MY':'马来西亚','SG':'新加坡','PH':'菲律宾'};
  const _region = (function(){
    const cs = (d.countries||[]).map(c=>String(c).toUpperCase());
    const names = [...new Set(cs.map(c=>_CNAME[c]).filter(Boolean))];
    if (d.regionKey) return { key: d.regionKey, label: d.regionLabel || '其他', names };
    const lang = (d.originalLanguage||'').toLowerCase();
    const LANG_REGION = {'en':'En','fr':'En','de':'En','es':'En','it':'En','pt':'En','ru':'En','nl':'En','pl':'En','sv':'En','da':'En','no':'En','fi':'En','tr':'En','el':'En','cs':'En','hu':'En','ro':'En','ja':'JpKr','ko':'JpKr','th':'Sea','vi':'Sea','id':'Sea','ms':'Sea','tl':'Sea','my':'Sea','zh':'Cn','cn':'Cn','yue':'Hk'};
    const COUNTRY_REGION = {'CN':'Cn','HK':'Hk','TW':'Hk','US':'En','GB':'En','FR':'En','DE':'En','CA':'En','AU':'En','JP':'JpKr','KR':'JpKr','TH':'Sea','VN':'Sea','ID':'Sea','MY':'Sea','SG':'Sea','PH':'Sea'};
    const NAME = {'Cn':'国片','En':'欧美','JpKr':'日韩','Hk':'港片','Sea':'东南亚','Ot':'其他'};
    let key = 'Ot';
    if (lang==='yue' || cs.includes('HK') || cs.includes('TW')) key = 'Hk';
    else if (LANG_REGION[lang]) key = LANG_REGION[lang];
    else { for (const c of cs){ if (COUNTRY_REGION[c]){ key = COUNTRY_REGION[c]; break; } } }
    return { key, label: NAME[key]||'其他', names };
  })();
  const _regionVal = _region.label + (_region.names.length? ' · ' + _region.names.join('/') : '');
  const facts = [
    ['地区', _regionVal],
    ['上映', d.premiered], ['时长', d.runtime?d.runtime+' 分钟':null],
    ['分级', d.certification],
    d.tmdbId?['TMDB', d.tmdbId, _tmdbUrl]:null,
    d.imdb_id?['IMDb', d.imdb_id, _imdbUrl]:null,
    ['公司', (d.studios||[]).slice(0,3).join('、')],
  ].filter(p=>p&&p[1]!=null&&p[1]!=='');
  const longPairs = [
    [d.originalTitle && d.originalTitle!==d.title ? '原名':null, d.originalTitle],
    ['导演', (d.directors||[]).map(x=>x.name).join('、')],
    ['主演', (d.cast||[]).slice(0,6).map(x=>x.name).join('、')],
  ];
  const ov = d.overview||'';
  const ovLong = ov.length>180;
  $('#mBody').innerHTML = `
    <div class="dhead rise" style="--i:0">
      ${poster?`<div class="dhead-poster"><img src="${img(d.poster)}" alt=""/></div>`:''}
      <div class="dhead-info">
        <div class="dhead-top">
          <div class="tt"><h2 class="dhead-title">${esc(d.title)}</h2></div>
          ${d.vote?`<span class="dhead-score" title="TMDB 评分">${icon('star')}${d.vote}</span>`:''}
        </div>
        <div class="dhead-meta">${kind==='tv'?'剧集':'电影'}${d.year?` · ${d.year}`:''}${d.inProduction?' · <span class="badge pend">在播</span>':''} · ${libBadge(d)}</div>
        ${facts.length?`<div class="dhead-facts">${facts.map(p=>`<div class="fact${String(p[1]).length>18?' long':''}"><span class="k">${p[0]}</span><span class="v" title="${esc(p[1])}">${p[2]?`<a class="fact-link" href="${esc(p[2])}" target="_blank" rel="noopener noreferrer">${esc(p[1])}${icon('external')}</a>`:esc(p[1])}</span></div>`).join('')}</div>`:''}
        ${_detailLinks(kind, d)}
        ${genres?`<div class="genres">${genres}</div>`:''}
        ${_metaLine(longPairs)}
        <div class="dhead-actions">
          <button class="ghost" onclick="askBlock('${kind}',${tmdbId}, this.dataset.title, this)" data-title="${esc(d.title)}">${icon('ban')}屏蔽此作品</button>
          <button class="act" onclick="toggleTitleEdit()">${icon('edit')}改标题</button>
          ${kind==='tv'?`<button class="mbtn" data-track-key="show_${tmdbId}" data-track-label="追踪新季" data-name="${esc(d.title)}" onclick="toggleTrackShow(${tmdbId}, this)">${icon('bell')}<span>追踪新季</span></button>`:''}
          ${d.inLibrary?`<button class="act" onclick="renameToTitle('${kind}',${tmdbId}, this)">${icon('move')}按新标题重命名</button>`:''}
        </div>
        <div class="title-edit" id="titleEdit" style="display:none">
          <input id="titleInput" type="text" maxlength="512" placeholder="中文标题(留空 = 还原自动译名)"
                 onkeydown="if(event.key==='Enter'){event.preventDefault();saveTitle('${kind}',${tmdbId},document.getElementById('titleSaveBtn'));}"/>
          <button class="mbtn sm" id="titleSaveBtn" onclick="saveTitle('${kind}',${tmdbId}, this)">保存</button>
          <button class="act sm" onclick="toggleTitleEdit()">取消</button>
        </div>
        ${d.inLibrary?`<div id="nfoBox" class="nfo-box" title="库内媒体 NFO 上次生成/更新时间, 可手动重新生成">
          <span class="nfo-date" id="nfoDate">NFO 查询中…</span>
          <button class="nfo-update" id="nfoUpdateBtn" onclick="updateNfo('${kind}',${tmdbId}, this)" disabled>${icon('refresh')}更新 NFO</button>
        </div>`:''}
      </div>
    </div>
    ${ov?`<p class="overview${ovLong?' clamp':''}" id="ovP">${esc(ov)}</p>${ovLong?'<button class="overview-more" id="ovMore" onclick="toggleOverview(this)">展开全部 <svg class="icn" style="width:13px;height:13px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button>':''}`:'<p class="overview" style="color:var(--muted)">（无简介）</p>'}
    <div class="rise" style="--i:1">${_personStrip(d.directors||[], '导演')}${_personStrip((d.cast||[]).slice(0,12), '主演')}</div>
    ${seasonHTML?`<div class="rise" style="--i:2">${seasonHTML}</div>`:''}
    <div class="rise" style="--i:${seasonHTML?3:2}">
      <h3>${icon('magnet')}磁力链接</h3>
      <div id="resBox"><div class="empty"><span class="spin"></span>搜索磁力中…</div></div>
    </div>`;
  // 分集折叠: data-open 的季(在播且缺集)初始展开
  document.querySelectorAll('.seas[data-open="1"]').forEach(b=>{ b.classList.add('open'); b._loaded = true; });
  searchMagnets(d.title, d.title, null, null, {title: d.title, originalTitle: d.originalTitle, englishTitle: d.englishTitle, year: d.year, tmdbId: d.tmdbId, ctype: kind==='tv'?'tv_show':'movie'});
  // NFO 更新功能: 库内作品才展示 NFO 日期 + 更新按钮, 详情渲染后异步拉取上次更新时间
  if(d.inLibrary){ loadNfoInfo(kind, tmdbId); }
}

// ---- NFO 更新功能 ----
// 读取库内 NFO 的上次更新时间(writeTime), 填充到详情页 NFO 行
async function loadNfoInfo(kind, tmdbId){
  const dateEl = $('#nfoDate'), btn = $('#nfoUpdateBtn');
  if(!dateEl) return;
  try{
    const info = await api(`/api/nfo/info/${kind}/${tmdbId}`);
    if(!info.exists){
      dateEl.innerHTML = `${icon('file')} 尚未生成 NFO`;
      if(btn){ btn.disabled = false; btn.innerHTML = `${icon('refresh')}生成 NFO`;
               btn._hasFileinfo = false; }   // 没有 NFO → 必然没有 <fileinfo>
    } else {
      dateEl.innerHTML = `${icon('file')} 更新于 ${esc(info.updated_at_text || info.updated_at || '')}`;
      if(btn){ btn.disabled = false; btn.innerHTML = `${icon('refresh')}更新 NFO`;
               btn._hasFileinfo = info.has_fileinfo !== false; }
    }
  }catch(e){
    dateEl.innerHTML = `${icon('file')} NFO 查询失败`;
    if(btn){ btn.disabled = false; btn._hasFileinfo = null; }   // 未知 → 不自动带 probe
  }
}
// 手动重新生成 NFO 并写回 /Cloud(后端走中转+覆盖, 不破坏 /Cloud 禁删铁律)
// <fileinfo> 为空(存量条目整理时没探到) → 自动带 probe=1 现场补探测一次:
// 补到了就写入分辨率/编码/音轨/字幕, 读不到照旧留空并把原因提示出来。
async function updateNfo(kind, tmdbId, btn){
  const needProbe = btn && btn._hasFileinfo === false;
  // 竞态: 先抓 dateEl, await 完再写 —— 期间切到别的条目会把别人的更新时间写进来
  const dateEl = $('#nfoDate');
  if(btn){ btn.disabled = true;
           btn.innerHTML = `${icon('refresh')} ${needProbe ? '探测并更新中…' : '更新中…'}`; }
  try{
    const url = `/api/nfo/update/${kind}/${tmdbId}` + (needProbe ? '?probe=1' : '');
    const r = await api(url, {method:'POST'});
    let msg = 'NFO 已更新' + (r.updated_at_text?`（${r.updated_at_text}）`:'');
    if(needProbe){
      if(r.probed){
        msg += '，已补写 <fileinfo> 流信息';
        if(btn) btn._hasFileinfo = true;
      } else if(r.probe_attempted){
        // 只有"真的探过且没探到"才提示失败; probed=false 也可能= 没必要探(三义混同)
        msg += `；流信息仍为空: ${r.probe_error || 'WebDAV 与 CD2 下载通道都读不到该文件'}`;
      } else {
        // 现在已有 <fileinfo>(或非电影) → 不用再探, 也别报假错误
        if(btn) btn._hasFileinfo = (kind === 'tv') ? true : (r.has_fileinfo !== false);
      }
    }
    if(r.warning) msg += `；⚠ ${r.warning}`;
    toast(msg);
    const t = r.updated_at_text || r.updated_at;
    if(dateEl && t && dateEl.isConnected)
      dateEl.innerHTML = `${icon('file')} 更新于 ${esc(t)}`;
    if(btn){ btn.disabled = false; btn.innerHTML = `${icon('refresh')}更新 NFO`; }
  }catch(e){
    toast('更新失败: ' + e.message);
    if(btn){ btn.disabled = false; btn.innerHTML = `${icon('refresh')}更新 NFO`; }
  }
}

// ---- 标题: 手动覆盖 / 按新标题重命名 ----
// 详情页「改标题」: 内联输入框(移动端禁用 prompt, 一律走这里), 写 tmdb_media.custom_title
function toggleTitleEdit(){
  const box = $('#titleEdit'); if(!box) return;
  const open = box.style.display === 'none' || !box.style.display;
  box.style.display = open ? 'flex' : 'none';
  if(open){
    const i = $('#titleInput');
    if(i){ i.value = (_curDetail && _curDetail.title) || ''; i.focus(); i.select(); }
  }
}
async function saveTitle(kind, tmdbId, btn){
  const i = $('#titleInput'); if(!i) return;
  if(btn && btn.disabled) return;            // Enter 连按/重复点击 → 只发一次
  const title = (i.value || '').trim();
  const cur = (_curDetail && _curDetail.title) || '';
  if(title === cur){
    // 同值保存: 没改动就别写库 —— 否则会把当前**自动**译名钉成 custom_title,
    // 以后 TMDB/豆瓣出更好的中文名也不会再跟(2026-09-26 审查 P2)
    toast('标题没有改动' + (title ? `（当前就是「${title}」）` : ''));
    i.value = cur; return;
  }
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    const r = await api(`/api/media/title/${kind}/${tmdbId}`, {method:'PUT', body: JSON.stringify({title})});
    toast(title ? `标题已改为「${r.title}」(详情/库/种子搜索都用它)` : '已还原自动译名');
    if(_curDetail) _curDetail.title = r.title;
    const h = document.querySelector('.dhead-title'); if(h) h.textContent = r.title;
    const nv = $('#mNavTitle'); if(nv) nv.textContent = r.title || '详情';
    i.value = r.title || '';
    const blk = document.querySelector('[data-title][onclick*="askBlock"]');
    if(blk) blk.dataset.title = r.title || '';
    // 标题变了 → 磁力搜索用的关键词也换掉(避免还拿旧英文名搜);
    // englishTitle 也要带上, 否则 _magQueries 的英文查询词直接丢了
    searchMagnets(r.title, r.title, null, null, {
      title: r.title,
      originalTitle: (_curDetail && _curDetail.originalTitle) || '',
      englishTitle: (_curDetail && _curDetail.englishTitle) || '',
      year: (_curDetail && _curDetail.year) || '',
      tmdbId,
      ctype: kind==='tv'?'tv_show':'movie'});
  }catch(e){
    toast('保存失败: ' + e.message);
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存'; }
  }
}
// 「按新标题重命名」: 目录 → 文件 → NFO 一起改成当前标题(只改名, 绝不删除; 冲突跳过)
async function renameToTitle(kind, tmdbId, btn){
  const t = (_curDetail && _curDetail.title) || '';
  if(!confirm(`把库内目录、文件名、NFO 都改成「${t}」？\n只改名不删除; 目标名已存在的项会跳过。`)) return;
  const oldHtml = btn ? btn.innerHTML : '';
  if(btn){ btn.disabled = true; btn.innerHTML = `${icon('refresh')}重命名中…`; }
  try{
    const r = await api(`/api/media/rename/${kind}/${tmdbId}`, {method:'POST'});
    const nf = (r.files || []).length;
    let msg = r.dir_renamed ? '目录已改名' : '目录名已是最新';
    if(nf) msg += `, ${nf} 个文件已改名`;
    if(r.skipped && r.skipped.length) msg += `, 跳过 ${r.skipped.length} 项`;
    toast(msg);
    await openDetailLocal(kind, tmdbId, false);
  }catch(e){
    toast('重命名失败: ' + e.message);
    if(btn){ btn.disabled = false; btn.innerHTML = oldHtml; }
  }
}

// 详情页"外部跳转"行: 只剩 Jellyfin(库内作品有 jfUrl, 后端拼好完整链接)。
// TMDB/IMDb 已直接做进上方 facts 超链接, 不再重复出跳转按钮。
function _detailLinks(kind, d){
  const links = [];
  if(d.jfUrl){
    links.push(`<a class="ext-link" href="${esc(d.jfUrl)}" target="_blank" rel="noopener noreferrer">${icon('tv')}Jellyfin</a>`);
  }
  return links.length ? `<div class="dhead-links">${links.join('')}</div>` : '';
}

// 简介折叠/展开(>180 字默认折叠 4 行)
function toggleOverview(btn){
  const p = $('#ovP'); if(!p) return;
  const open = p.classList.toggle('clamp');
  btn.innerHTML = open
    ? '展开全部 <svg class="icn" style="width:13px;height:13px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>'
    : '收起 <svg class="icn" style="width:13px;height:13px;transform:rotate(180deg)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
}

// 分集明细未同步时: 触发全量分集同步(~1min/7.6万条) → 轮询完成 → 刷新详情显示真实缺失
async function syncEpisodesAndRefresh(kind, tmdbId){
  const btn = $('#epSyncBtn'), note = $('#epSyncNote');
  if(btn){ btn.disabled = true; btn.innerHTML = icon('refresh')+'同步中…'; }
  if(note) note.innerHTML = '正在全量拉取 Jellyfin 分集明细(约 7.6 万条, 1 分钟左右)…';
  try{
    await api('/api/sync/jellyfin?scope=episodes', {method:'POST'});
    // 轮询分集状态: 同步是后台线程, 最多等 ~3 分钟
    let tries = 0;
    const timer = setInterval(async ()=>{
      tries++;
      try{
        const st = await api('/api/jf-episodes-status');
        if(st && st.synced){
          clearInterval(timer);
          if(note) note.innerHTML = '分集明细已同步完成, 正在刷新…';
          await openDetailLocal(kind, tmdbId);   // 刷新详情(重新走 /api/browse/detail)
          return;
        }
      }catch(e){}
      if(tries > 90){ clearInterval(timer);
        if(note) note.innerHTML = '同步仍在进行(可在「本地库 → 同步记录」查看), 稍后手动刷新详情。';
        if(btn){ btn.disabled = false; btn.innerHTML = icon('refresh')+'同步分集明细并刷新'; }
      }
    }, 2000);
  }catch(e){
    if(note) note.innerHTML = '触发失败: ' + esc(e.message);
    if(btn){ btn.disabled = false; btn.innerHTML = icon('refresh')+'同步分集明细并刷新'; }
  }
}

// 2026-09: 顶部 hero 背景横条已移除(占篇幅无实际意义, 标题/年份/类型全部由正文 .dtitle 承载)。
// 保留 _hero 为 no-op —— 搜索页/演员页/详情加载态的历史调用(_hero('搜索: …')等)不再报错, 直接忽略。
function _hero(title, rowHTML, genresHTML){ /* no-op: hero 已移除 */ }
function _metaLine(pairs){
  const html = pairs.filter(p=>p[1]!=null&&p[1]!=='').map(p=>`<span>${p[0]}: <b>${esc(p[1])}</b></span>`).join('');
  return html?`<div class="meta-line">${html}</div>`:'';
}
// 详情页可点击的演职员横条: 头像+名字+角色, 点进演员页看作品(带图)
function _personStrip(list, label){
  const rows = (list||[]).filter(x=>x && x.tmdbid);
  if(!rows.length) return '';
  const chips = rows.map(x=>{
    const av = x.thumb ? `<img src="${img(x.thumb)}" loading="lazy" alt=""/>` : '<span class="ph">?</span>';
    const sub = (x.role||x.job||'');
    return `<div class="person-chip" onclick="openPersonPage(${x.tmdbid})">${av}
      <span class="pn">${esc(x.name)}</span>${sub?`<span class="pj">${esc(sub)}</span>`:''}</div>`;
  }).join('');
  return `<h3 style="margin:14px 0 6px">${label}</h3><div class="person-strip">${chips}</div>`;
}

async function openSearchOnly(q){
  PUSH_CONTENT_TYPE='movie';
  _openModal();
  _hero('搜索: '+q, '<div class="row">手动搜索结果</div>');
  _navTitle('搜索: '+q);
  $('#mBody').innerHTML = `<h3>${icon('magnet')}磁力链接</h3><div id="resBox"><div class="empty"><span class="spin"></span>搜索磁力中…</div></div>`;
  searchMagnets(q, q);
}

// 2026-09 四版: 默认「质量优先」—— 前排发布组(通用 → 种子抓取规则 search.group_priority)整批最前,
// 组内再比 2160p/HDR/H.265/字幕/国语 加分; 相关性=引擎原序(最快, 不拉全量), 大小/种子数=全局排序
const MAG_SORTS = [['quality','质量优先(前排组先)'],['relevance','相关性(最快)'],['size_desc','大小 ↓ 从大到小'],['size_asc','大小 ↑ 从小到大'],['seeders_desc','种子 ↓ 从多到少']];

// ---- 磁力多查询: 中文标题搜一遍 + 英文(原名)标题搜一遍, 合并去重 ----
// 单查中文片名常只命中中文站点/已洗码资源, 英文原名能命中发布组原盘(英文命名), 双查并集更全更准。
function _magKey(r){ return r.infoHash || ((r.name||'')+'|'+(r.size||'')); }
function _magSortCmp(sort){
  // 与后端 _apply_sort 同规则(seeders 缺失排末尾); relevance 返回空比较器
  if(sort==='quality') return (a,b)=>{
    // 前排组先(通用 → 种子抓取规则, 顺序 = 优先级), 组内/其余再按质量分 → 大小 → 种子数
    const at=a.groupRank==null?0:1, bt=b.groupRank==null?0:1;
    if(at!==bt) return bt-at;
    if(at===1 && a.groupRank!==b.groupRank) return a.groupRank-b.groupRank;
    const qs=(b.qualityScore||0)-(a.qualityScore||0);
    if(qs) return qs;
    const sz=(b.size||0)-(a.size||0);
    if(sz) return sz;
    const an=(a.seeders==null), bn=(b.seeders==null);
    if(an!==bn) return an?1:-1;
    return (b.seeders||0)-(a.seeders||0);
  };
  if(sort==='size_desc') return (a,b)=>(b.size||0)-(a.size||0);
  if(sort==='size_asc') return (a,b)=>(a.size||0)-(b.size||0);
  if(sort==='seeders_desc') return (a,b)=>{
    const an=(a.seeders==null), bn=(b.seeders==null);
    if(an!==bn) return an?1:-1;
    return (b.seeders||0)-(a.seeders||0);
  };
  return ()=>0;
}
function _magMerge(pages, cap, sort){
  // pages: 各查询的响应(数组序=查询序)。relevance: q1 原序在前 + q2 非重复追加;
  // 排序模式: 各页已按该序排好, 拼接后前端重排(量小 ≤200 条)即得合并集全局序。
  const all = [];
  for(const p of pages) for(const r of (p.items||[])) all.push(r);
  if(sort && sort!=='relevance') all.sort(_magSortCmp(sort));
  const seen = new Set(), out = [];
  for(const r of all){
    const k = _magKey(r);
    if(seen.has(k)) continue;
    seen.add(k);
    out.push(r);
    if(cap && out.length >= cap) break;
  }
  return out;
}
// 构造磁力查询组: [中文标题, 英文原标题] + 年份 + (剧集季标记 Sxx/Specials)
// 两者相同/缺英文时自动退化为单查询, 不重复打。
function _magQueries(title, originalTitle, year, seasonTag, englishTitle){
  const y = year ? ' '+year : '';
  const t = seasonTag ? ' '+seasonTag : '';
  const cn = `${title||''}${y}${t}`.trim();
  // 英文查询源: 优先真正的英文名(TMDB ?language=en); 缺失时退而用 originalTitle。
  // ⚠️ 中文片的 original_title 仍是中文(与 title 相同), 单靠它退化不出第二查,
  // 只有英文名才能命中英文命名的发布组原盘。
  let enSrc = '';
  if(englishTitle && englishTitle!==title) enSrc = englishTitle;
  else if(originalTitle && originalTitle!==title) enSrc = originalTitle;
  const en = enSrc ? `${enSrc}${y}${t}`.trim() : '';
  // 相关性优化: 有英文名(与中文不同)→ 只用「英文+年份」一组查询(配合后端 ctype 过滤, 精确且低噪);
  // 无英文名(纯中文标题)→ 回退中文查询。旧实现中英文各查一遍再合并, 英文片会把目标淹没在重复里。
  return [...new Set([en || cn].filter(Boolean))];
}
function _magSortHtml(box){
  const cur = box._sort || 'relevance';
  return `<select class="mag-sort" onchange="changeMagSort(this)">`
    + MAG_SORTS.map(([v,l])=>`<option value="${v}"${v===cur?' selected':''}>${l}</option>`).join('') + `</select>`;
}
function _grpBadge(r){
  // 前排发布组徽章(种子抓取规则): 通用页配置的组, 排序与自动推送都优先
  if(!r || r.groupRank == null) return '';
  return `<span class="qgrp" title="前排发布组 — 管理 → 通用 → 种子抓取规则配置(顺序 = 优先级);「质量优先」排序与追踪自动推送都先选它">前排${r.groupName?` · ${esc(r.groupName)}`:''}</span>`;
}
// 磁力来源标签: 后端把每个源打上 source 键, 多源时一条结果可能来自不同源
const _SRC_LABEL = {native: 'Bitmagnet', next_web: 'Bitmagnet-Next-Web', jackett: 'Jackett'};
function _srcLabel(k){ return _SRC_LABEL[k] || k; }
function _srcLabelFromPages(pages){
  // 汇总本次查询实际命中的所有源(用后端 sources 数组, 兼容旧 source 单值)
  const set = new Set();
  (pages || []).forEach(p => {
    const arr = (p && p.sources) ? p.sources : (p && p.source ? [p.source] : []);
    (arr || []).forEach(s => set.add(s));
  });
  return set.size ? [...set].map(_srcLabel).join(' / ') : '磁力源';
}
function _srcBadge(r){
  // 单条结果来源小徽章(多源合并时区分每条来自哪个源)
  const s = r && r.source;
  if(!s) return '';
  return `<span class="qsrc" title="来源: ${esc(_srcLabel(s))}">${esc(_srcLabel(s))}</span>`;
}
function _srcLabelFromItems(items){
  // 按实际返回条目的来源去重汇总(失败的源不会贡献条目, 也就不该出现在"来源"里)
  const set = new Set();
  (items || []).forEach(r => r && r.source && set.add(r.source));
  return set.size ? [...set].map(_srcLabel).join(' / ') : '';
}
function _aggWarnings(pages){
  // 多源部分失败时后端会带 warnings(失败的源名+原因), 聚合成一句给界面提示
  const out = [];
  (pages || []).forEach(p => (p && p.warnings || []).forEach(w => { if (out.indexOf(w) < 0) out.push(w); }));
  return out.join('; ');
}
async function searchMagnets(q, title, boxSel, limit, extra){
  limit = limit || 30;  // 默认拉 30 条(站点每页 10 条, 后端翻 3 页); 双查询并行, 首屏更快; 「加载更多」续翻
  extra = extra || {};
  const box = $(boxSel||'#resBox'); if(!box) return;
  box._boxSel = boxSel || '#resBox';
  const sort = box._sort || 'quality';   // 默认质量优先(用户指定: 2160p/HDR/字幕/国语靠前)
  box._sort = sort;
  const tok = (box._reqTok = (box._reqTok||0) + 1);  // 排序/查询竞态: 旧响应直接丢弃
  window._magBox = box;
  // 竞态与反馈(2026-09-30 报障「种子页很慢、加载更多点了没反应」):
  //   ① 上一轮的按钮/结果立刻清掉 —— 留在页面上的旧按钮还能点, 点了会拿旧页号去追加、
  //      随后又被本轮结果覆盖, 用户看到的就是"点了没反应";
  //   ② _loading 让首屏没回来时「加载更多」直接忽略;
  //   ③ 8 秒还没回 → 换文案提示站点慢, 免得干转圈像卡死。
  box._loading = true;
  box._loadingMore = false;
  box._emptyStreak = 0;
  clearTimeout(box._slowT);
  box.innerHTML = `<div class="empty"><span class="spin"></span><span class="magSpinTxt">搜索磁力中…</span></div>`;
  box._slowT = setTimeout(() => {
    const t = box.querySelector('.magSpinTxt');
    if(t) t.textContent = '站点较慢,仍在加载(可换「相关性」排序, 那个不抓全量)…';
  }, 8000);
  // 查询组: 中文标题一遍 + 英文(原名)标题一遍(+年份+季标记), 并行拉取合并去重
  const qs = _magQueries(extra.title || q, extra.originalTitle, extra.year, extra.seasonTag, extra.englishTitle);
  try{
    const ct = extra.ctype ? `&ctype=${encodeURIComponent(extra.ctype)}` : '';
    const pages = await Promise.all(qs.map(x => api(`/api/search?q=${encodeURIComponent(x)}&limit=${limit}&page=1&sort=${sort}${ct}`)));
    if(tok !== box._reqTok) return;
    const res = _magMerge(pages, limit, sort);
    const warns = _aggWarnings(pages);
    const src = _srcLabelFromItems(res) || _srcLabelFromPages(pages);
    const _multiSrc = new Set(res.map(r => r && r.source).filter(Boolean)).size > 1;
    if(!res.length){ box.innerHTML=`<div class="empty">${icon('search')} ${src} 没有命中</div>`; return; }
    const qTag = qs.length>1 ? ` · ${qs.length} 组查询` : '';
    // 窗口状态: 后端分段抓取(首屏 60 条), totalCount=已抓条数, exhausted=站点到底
    box._total = Math.max(0, ...pages.map(p => (p && p.totalCount) || 0));
    box._exhausted = pages.every(p => p && p.exhausted === true);
    box.innerHTML = `<div style="color:var(--muted);font-size:12px;margin:0 0 8px" class="magHead"><span class="magHeadTxt">来源: ${src} · 本页 ${res.length} 条${_magTotal(box)}${qTag}</span>${_magSortHtml(box)}</div>`
      + (warns ? `<div class="mag-warn">${icon('alert')} 部分源失败(已用可用的源继续): ${esc(warns)}</div>` : '')
      + res.map((r,i)=>`
      <div class="res"><div class="info">
        <div class="n">${r.golden?`<span class="qgold" title="${r.goldenBy?`金标: 自压组 ${esc(r.goldenBy)}, 默认带中文字幕+国语, 质量分 +20 排序优先`:'金标: 中文字幕+国语, 质量分 +20 排序优先'}">★ 金标${r.goldenBy?' · 自压':''}</span>`:''}${_grpBadge(r)}${_multiSrc?_srcBadge(r):''}${esc(r.name||'')}</div>
        ${_qualityTags(r.name, r)}
        ${_pushBadge(r)}
        <div class="s">${sort==='quality'&&r.qualityScore!=null?`<span class="qscore" title="质量分: 分辨率(名字写实才给分)/HDR/H.265/字幕/国语 加分 + 体积合理性(名不副实扣分), 分高排前">质 ${r.qualityScore}</span>`:''}<span class="sz">${icon('box')}${fmt(r.size)}</span>${_seedTags(r)}</div>
      </div><div class="res-btns">${_pushButtons(i, `pushMagnet(${i}, this)`, `pushQbit(${i}, this)`, r)}</div></div>`).join('')
      + (pages.some(p=>p.hasMore) ? `<div style="text-align:center;margin-top:10px"><button class="ghost" id="magMore" onclick="loadMoreMagnets(this)">加载更多…</button></div>` : '');
    // 记住查询组、排序与各查询的下一页号/是否还有, 供「加载更多」按后端 nextPage 续翻后合并去重。
    // ⚠️ 不能用写死的 _page=2: 后端 page 按 10 条/页折算 offset(relevance)或切片页号(排序),
    // 首次 limit=30 已翻 3 页, 返回的 nextPage=4。续翻必须用后端给的 nextPage, 否则页号错位、
    // 每次只追加 ~10 条(30 条里 20 条重复被去重), 表现为"加载更多点了没反应"。
    box._res = res;
    box._qs = qs;
    box._q = qs[0];
    box._limit = limit;
    box._extra = extra;
    box._pages = pages.map(p => (p && p.nextPage) ? p.nextPage : 2);  // 每查询独立的下一页号
    box._more  = pages.map(p => !!(p && p.hasMore));                  // 每查询是否还有更多
  }catch(e){
    if(tok === box._reqTok) box.innerHTML=`<div class="empty">搜索失败: ${e.message}</div>`;
  }finally{
    // 无论成功/失败/无命中都把"搜索中"状态收掉(旧响应不许动新一轮的状态)
    if(tok === box._reqTok){ box._loading = false; clearTimeout(box._slowT); }
  }
}
// 头部「共 N 条」: 只在站点已到底时给真总数(否则 totalCount 只是"已抓多少", 会误导)
function _magTotal(box){
  return (box._exhausted && box._total && box._total > (box._res||[]).length)
    ? ` · 共 ${box._total} 条` : '';
}
// 到底/去重后无新内容: 把按钮换成一行说明, 不要"点了没反应"式的静默消失
function _magEnd(box, btn){
  const n = (box._res||[]).length;
  const wrap = btn && btn.closest('div');
  if(wrap) wrap.innerHTML = `<span class="mag-end">没有更多了 · 已载 ${n} 条</span>`;
  else if(btn) btn.remove();
}
// 切换排序: 清空已加载, 按新排序重查第 1 页(非 relevance 后端分段抓取排序后切片, 带缓存)
function changeMagSort(sel){
  const box = window._magBox || $('#resBox');
  if(!box || !box._q) return;
  box._sort = sel.value;
  searchMagnets(box._q, box._q, box._boxSel, box._limit, box._extra);
}
async function loadMoreMagnets(btn){
  const box = window._magBox || $('#resBox');
  if(!box || !box._q || !box._pages){ return; }
  // 首屏还没回来 / 上一次还在拉 → 不接第二次点击(否则页号错乱、结果被覆盖 = "点了没反应")
  if(box._loading || box._loadingMore){ return; }
  box._loadingMore = true;
  const tok = box._reqTok;
  btn.disabled = true; btn.textContent = '加载中…';
  try{
    const sort = box._sort || 'quality';
    const qs = box._qs || [box._q];
    const ct = (box._extra && box._extra.ctype) ? `&ctype=${encodeURIComponent(box._extra.ctype)}` : '';
    // 每个查询用自己的 nextPage 续翻(后端 page 语义: relevance=10条/页折算offset, 排序=切片页号);
    // 已到底的查询(hasMore=false)不再发请求, 占位返回空页。
    const pages = await Promise.all(qs.map((x, i) =>
      (box._more && box._more.length===qs.length && box._more[i])
        ? api(`/api/search?q=${encodeURIComponent(x)}&limit=${box._limit}&page=${box._pages[i]}&sort=${sort}${ct}`)
        : Promise.resolve({items:[], hasMore:false, nextPage:(box._pages && box._pages[i]) || 2})
    ));
    box._loadingMore = false;
    if(tok !== box._reqTok){ return; }   // 期间换了查询/排序 → 新一轮自己渲染, 这里丢弃
    // 回写各查询的最新翻页状态, 供下一次点击使用
    box._pages = pages.map((p, i) => (p && p.nextPage) ? p.nextPage : ((box._pages && box._pages[i]) || 2));
    box._more  = pages.map(p => !!(p && p.hasMore));
    box._exhausted = pages.every(p => p && p.exhausted === true);
    box._total = Math.max(box._total || 0, ...pages.map(p => (p && p.totalCount) || 0));
    const seen = new Set(box._res.map(_magKey));
    const res = [];
    for(const p of pages) for(const r of (p.items||[])){
      const k = _magKey(r);
      if(seen.has(k)) continue;
      seen.add(k); res.push(r);
    }
    // 排序模式: 追加块内部按序排(与已展示块的全局衔接在极少数跨查询交错下可能有微小偏差, 可接受)
    if(sort && sort!=='relevance') res.sort(_magSortCmp(sort));
    if(!res.length){
      // 整页都是已展示过的去重项: 后面可能还有 → 自动续翻(最多 2 次), 否则给个明确结尾
      if(box._more.some(Boolean) && (box._emptyStreak||0) < 2){
        box._emptyStreak = (box._emptyStreak||0) + 1;
        btn.disabled = false; btn.textContent = '加载更多…';
        toast('这一页与已展示的重复,自动接着加载');
        setTimeout(()=>{ if(document.contains(btn) && !box._loading) loadMoreMagnets(btn); }, 500);
        return;
      }
      _magEnd(box, btn);
      return;
    }
    box._emptyStreak = 0;
    const base = box._res.length;  // 追加项的推送索引基数
    const html = res.map((r,i)=>`
      <div class="res"><div class="info">
        <div class="n">${_grpBadge(r)}${esc(r.name||'')}</div>
        ${_qualityTags(r.name, r)}
        ${_pushBadge(r)}
        <div class="s"><span class="sz">${icon('box')}${fmt(r.size)}</span>${_seedTags(r)}</div>
      </div><div class="res-btns">${_pushButtons(base+i, `pushMagnet(${base+i}, this)`, `pushQbit(${base+i}, this)`, r)}</div></div>`).join('');
    btn.closest('div').insertAdjacentHTML('beforebegin', html);
    box._res = box._res.concat(res);
    const headTxt = box.querySelector('.magHead .magHeadTxt');
    if(headTxt){
      const qTag = qs.length>1 ? ` · ${qs.length} 组查询` : '';
      headTxt.innerHTML = `来源: ${srcLabel(headTxt)} · 已载 ${box._res.length} 条${_magTotal(box)}${qTag}`;
    }
    if(!box._more.some(Boolean)) _magEnd(box, btn);
    else { btn.disabled = false; btn.textContent = '加载更多…'; }
  }catch(e){
    box._loadingMore = false;
    btn.disabled = false; btn.textContent = '重试加载';
    toast('加载失败: ' + e.message);
  }
}
function srcLabel(headTxt){
  const t = headTxt.innerHTML;
  const m = t.match(/来源:\s*([^·]+)/);
  return m ? m[1].trim() : '';
}

async function pushMagnet(i, btn){
  const boxEl = window._magBox || $('#resBox');
  const r = boxEl._res && boxEl._res[i]; if(!r||!r.magnet){ toast('缺少磁力链'); return; }
  const title = (r.name||'').split('/')[0].slice(0,80);
  btn.disabled=true; btn.textContent='推送中…';
  try{
    const ok = await api('/api/push',{method:'POST',body:JSON.stringify({
      magnet:r.magnet, title, content_type:PUSH_CONTENT_TYPE, language:'', toFolder:''
    })});
    // 标记本地状态, 之后换排序/加载更多重渲时按钮保持禁用
    if(!r.pushed) r.pushed = {cd2:false,qbit:false};
    r.pushed.cd2 = true;
    btn.innerHTML=icon('check')+'已推CD2'; btn.disabled=true; btn.style.background='var(--ok)';
    toast('已推送到 CloudDrive2 离线下载');
  }catch(e){ btn.disabled=false; btn.textContent='推送 CD2'; toast('失败: '+e.message); }
}

// 推送 Qbit: 打 tag `ma:{kind}:{tmdbId}` 便于下载页按作品聚合"下到第几集"。
// tmdbId 来自磁力列表的 box._extra.tmdbId(详情页传入); 搜索页直搜无 tmdbId 则不打 tag。
async function pushQbit(i, btn){
  const boxEl = window._magBox || $('#resBox');
  const r = boxEl._res && boxEl._res[i]; if(!r||!r.magnet){ toast('缺少磁力链'); return; }
  const extra = boxEl._extra || {};
  const tmdbId = extra.tmdbId || 0;
  const kind = (PUSH_CONTENT_TYPE || 'movie') === 'tv' ? 'tv' : 'movie';
  const title = (r.name||'').split('/')[0].slice(0,80);
  btn.disabled=true; btn.textContent='推送中…';
  try{
    await api('/api/qbit/push',{method:'POST',body:JSON.stringify({
      magnet:r.magnet, title, kind, tmdb_id:tmdbId, toFolder:''
    })});
    if(!r.pushed) r.pushed = {cd2:false,qbit:false};
    r.pushed.qbit = true;
    btn.innerHTML=icon('check')+'已推Qbit'; btn.disabled=true; btn.style.background='var(--ok)';
    toast(tmdbId?'已推送到 Qbit(已打标, 下载页可看进度)':'已推送到 Qbit');
  }catch(e){ btn.disabled=false; btn.textContent='推送 Qbit'; toast('失败: '+e.message); }
}

// 推送 Qbit(季扫描紧凑按钮): tmdbId 直接由调用方传入(季扫描天然知道作品 id)。
async function pushSeasonQbit(tmdbId, btn, i, season){
  const box = $(`#seasonSeeds-${season}`);
  const r = box && box._res && box._res[i]; if(!r||!r.magnet){ toast('缺少磁力链'); return; }
  const title = (r.name||'').split('/')[0].slice(0,80);
  btn.disabled=true; btn.textContent='推送中…';
  try{
    await api('/api/qbit/push',{method:'POST',body:JSON.stringify({
      magnet:r.magnet, title, kind:'tv', tmdb_id:tmdbId, toFolder:''
    })});
    if(!r.pushed) r.pushed = {cd2:false,qbit:false};
    r.pushed.qbit = true;
    btn.innerHTML=icon('check')+'已推Qbit'; btn.disabled=true; btn.style.background='var(--ok)';
    toast(`S${String(season).padStart(2,'0')} 已推送到 Qbit(下载页可看集数)`);
  }catch(e){ btn.disabled=false; btn.textContent='Qbit'; toast('失败: '+e.message); }
}

function fmt(b){ if(b==null) return '—'; b=+b; const u=['B','KB','MB','GB','TB']; let i=0; while(b>=1024&&i<u.length-1){b/=1024;i++;} return b.toFixed(1)+' '+u[i]; }
// 从磁力/资源名提取质量徽章(分辨率/编码/HDR/发布组), 让种子列表一眼分优劣。
// 返回徽章 HTML 串(无特征时返回空)。
function _qualityTags(name, r){
  const n = (name||''); const up = n.toUpperCase();
  const tags = [];
  // 分辨率 — 与后端 search.py::resolution 同规则: 出现 2160 才算 4K;
  // 只写 4K/UHD 却同时写了 1080(如《...【4K.SDR1080p】》)按 1080p 算,
  // 否则标签骗人(显示 2160p)、分数也骗人(拿满 48 分压过真 1080p)。
  const has2160 = /2160[PU]?|3840X?2160/.test(up);
  const has4k = /4K|UHD/.test(up);
  const has1080 = /1080[PI]|FHD|1080/.test(up);
  if(has2160 || (has4k && !has1080)) tags.push(['2160p','q-uhd']);
  else if(has1080) tags.push(['1080p','q-fhd']);
  else if(/720[PI]?|720/.test(up)) tags.push(['720p','q-hd']);
  else if(/480[PU]?|480|\bSD\b/.test(up)) tags.push(['480p','q-sd']);
  // 编码
  if(/AVC|H264|H\.?264|x264/.test(up)) tags.push(['H.264','q-codec']);
  else if(/AV1|AV1/.test(up)) tags.push(['AV1','q-codec']);
  else if(/HEVC|H265|H\.?265|x265/.test(up)) tags.push(['H.265','q-codec']);
  // HDR
  if(/HDR10\+|HDR10P/.test(up)) tags.push(['HDR10+','q-hdr']);
  else if(/HDR10|HDR/.test(up)) tags.push(['HDR','q-hdr']);
  // 音频
  if(/DOLBY.?ATMOS|ATMOS/.test(up)) tags.push(['Atmos','q-audio']);
  else if(/TRUEHD|DTS-HD|DTS HD/.test(up)) tags.push(['Hi-Res','q-audio']);
  // 发布组(末尾括号/点号分隔的最后一段, 2~3 个单词)
  const m = n.match(/(?:\.|\b)([A-Z][A-Z0-9]{1,20}(?:\s?[A-Z][A-Z0-9]{1,20}){0,2})$/);
  if(m && !/^(WEB|HDTV|BluRay|BRRip|DVDRip|HDR|HDR10|H265|H264|HEVC|AVC|x265|x264)$/.test(m[1].trim())) tags.push([m[1].trim(),'q-group']);
  // 体积可疑(与后端 search.py::_size_adjust 同口径): 名字吹 4K 却只有几十 MB
  if(r && r.sizeSuspect) tags.push(['体积可疑','q-bad','体积与名字标称的分辨率对不上(如 4K 只有几十 MB), 质量分已扣分']);
  if(!tags.length) return '';
  return `<div class="qtags">${tags.map(([t,c,ti])=>`<span class="qtag ${c}"${ti?` title="${ti}"`:''}>${t}</span>`).join('')}</div>`;
}
// 种子/下载数徽章(图标化, 数字直观)
function _seedTags(r){
  if(r.seeders==null && r.leechers==null) return '';
  const s = r.seeders??0, l = r.leechers??0;
  const cls = s>=100?'s-hot':(s>=10?'s-mid':'s-low');
  return `<div class="seedtags"><span class="seed ${cls}">${icon('download')}${s}</span><span class="leech">${icon('cloud')}${l}</span></div>`;
}
// 推送标记徽章(已推 CD2 / 已推 Qbit): 在种子信息区直观显示, 配合下方按钮禁用
function _pushBadge(r){
  if(!r || !r.pushed) return '';
  const b = [];
  if(r.pushed.cd2) b.push('<span class="pbadge cd2">已推 CD2</span>');
  if(r.pushed.qbit) b.push('<span class="pbadge qbit">已推 Qbit</span>');
  return b.length ? `<div class="ptags">${b.join('')}</div>` : '';
}
// 根据推送状态渲染双推送按钮: 已推的禁用并改文案, 避免重复点击
function _pushButtons(i, cd2Onclick, qbitOnclick, r){
  const p = (r && r.pushed) || {};
  const cd2 = p.cd2
    ? `<button class="push" disabled>${icon('check')}已推 CD2</button>`
    : `<button class="push" onclick="${cd2Onclick}">推送 CD2</button>`;
  const qbit = p.qbit
    ? `<button class="push qbit" disabled>${icon('check')}已推 Qbit</button>`
    : `<button class="push qbit" onclick="${qbitOnclick}">推送 Qbit</button>`;
  return cd2 + qbit;
}

// ---- 缺失季自动扫种子: 按季构造搜索词组, 结果渲染进季行的种子列 ----
// 用户指定(2026-09-17): 每季扫 4 组 = {剧名+年份+Sxx, 剧名+Sxx} × {中文, 英文}。
// 带年份的那组精度高(防同名撞季), 不带年份的组召回高(发布组整季包常省略年份);
// 中英各一组覆盖中文/英文命名的发布组。季号统一两位(S01/S23), season 0 用 Specials。
// ⚠️ 只用于剧集的季扫描; 电影/单集磁力搜索仍走 _magQueries(2 组: 中英各 1)。
function _seasonSeedQueries(season, d){
  const S = String(season).padStart(2,'0');
  const tail = season===0 ? 'Specials' : `S${S}`;
  const title = d ? (d.title||'') : '';
  const originalTitle = d ? (d.originalTitle||'') : '';
  const englishTitle = d ? (d.englishTitle||'') : '';
  const year = d && d.year ? d.year : '';
  // 英文源: 优先 TMDB ?language=en 的真英文名; 缺失且 original 与中文不同(如西/日语原名)才退用 original
  let enSrc = '';
  if(englishTitle && englishTitle!==title) enSrc = englishTitle;
  else if(originalTitle && originalTitle!==title) enSrc = originalTitle;
  const combos = [];
  const push = (t, withYear) => { if(!t) return; combos.push(`${t}${withYear&&year?' '+year:''} ${tail}`.trim()); };
  push(title, true);   // 中文+年份+Sxx
  push(title, false);  // 中文+Sxx
  push(enSrc, true);   // 英文+年份+Sxx
  push(enSrc, false);  // 英文+Sxx
  return [...new Set(combos)];
}
// ---- 分集折叠: 点开某季才拉逐集明细(1 次 TMDB 按季查询, 后端 1h 缓存) ----
function toggleSeason(tmdbId, season, head){
  const block = head.closest('.seas'); if(!block) return;
  const open = block.classList.toggle('open');
  if(open && !block._loaded){ loadSeasonEps(tmdbId, season); }
}
async function loadSeasonEps(tmdbId, season){
  const block = $(`#seas-${season}`); const box = $(`#seasonEps-${season}`);
  if(!box) return; if(block) block._loaded = true;
  try{
    const d = await api(`/api/browse/tv/${tmdbId}/season/${season}/episodes`);
    const eps = d.episodes||[];
    if(!eps.length){ box.innerHTML = `<div class="empty" style="padding:10px">TMDB 无该季分集信息</div>`; return; }
    box.innerHTML = eps.map(e=>{
      const name = e.name || `第 ${e.episode} 集`;
      // 拥有=绿勾(实心); 缺失=空圈(不打勾)
      const tick = e.have
        ? `<span class="ep-tick ok" title="库内已有">${icon('check')}</span>`
        : `<span class="ep-tick miss" title="缺失"></span>`;
      const date = e.air_date ? `<span class="ep-date" title="播出时间">${esc(e.air_date)}</span>` : '';
      const desc = e.overview ? `<div class="ep-desc" title="${esc(e.overview)}">${esc(e.overview)}</div>` : '';
      return `<div class="ep-row${e.have?' has':' miss'}">
        <div class="ep-top">${tick}
          <span class="ep-no">E${e.episode}</span>
          <span class="ep-name" title="${esc(name)}">${esc(name)}</span>
          ${date}${e.have?'':'<span class="badge miss-b">缺</span>'}</div>
        ${desc}</div>`;
    }).join('');
  }catch(e){ box.innerHTML = `<div class="empty" style="padding:10px;color:var(--err)">加载失败: ${esc(e.message)}</div>`; }
}
async function scanSeasonSeads(tmdbId, season, btn, silent){
  const box = $(`#seasonSeeds-${season}`); if(!box) return;
  if(btn){ btn.disabled=true; btn.textContent='扫描中…'; }
  box.style.display='block';
  box.innerHTML='<div class="empty"><span class="spin"></span>扫种中…</div>';
  // 取剧名: 用刚打开的详情快照(旧写法注释说"从已渲染详情; 避免再请求", 代码却每次都
  // 再打一遍 /api/browse/detail —— 详情页本来就刚拉过, 纯重复请求)
  let d = _detailPayload[String(tmdbId)] || null;
  if(!d){ try{ d = await api(`/api/browse/detail/tv/${tmdbId}`); }catch{} }
  const qs = _seasonSeedQueries(season, d);
  try{
    // 中文标题 + 英文原名 各扫一遍, 合并去重(整季包常按英文原名发布, 单查中文会漏)
    const pages = await Promise.all(qs.map(x => api(`/api/search?q=${encodeURIComponent(x)}&limit=20`)));
    const res = _magMerge(pages, 20, 'relevance');
    const warns = _aggWarnings(pages);
    const src = _srcLabelFromItems(res) || _srcLabelFromPages(pages);
    const _multiSrc = new Set(res.map(r => r && r.source).filter(Boolean)).size > 1;
    if(!res.length){ box.innerHTML=`<div style="font-size:12px;color:var(--muted)">${src} 无「${esc(qs[0])}」命中${warns?` · <span class="mag-warn" style="margin:0">部分源失败: ${esc(warns)}</span>`:''}</div>`; }
    else{
      const qTag = qs.length>1 ? ` · ${qs.length} 组查询` : '';
      const qShow = qs.join(' / ');
      box.innerHTML = `<div style="font-size:11px;color:var(--muted);margin-bottom:4px">${src} · ${res.length} 条${qTag} · 词: ${esc(qShow)}</div>`
        + (warns ? `<div class="mag-warn">${icon('alert')} 部分源失败(已用可用的源继续): ${esc(warns)}</div>` : '')
        + res.map((r,i)=>`
        <div class="res res-compact"><div class="info">
          <div class="n">${_multiSrc?_srcBadge(r):''}${esc(r.name||'')}</div>
          ${_qualityTags(r.name, r)}
          ${_pushBadge(r)}
          <div class="s"><span class="sz">${fmt(r.size)}</span>${_seedTags(r)}</div></div>
          <div class="res-btns">${_pushButtons(i, `pushSeasonMagnet(${tmdbId},this,${i},'${season}')`, `pushSeasonQbit(${tmdbId},this,${i},'${season}')`, r)}</div></div>`).join('');
      box._res = res;
    }
  }catch(e){ box.innerHTML=`<div style="font-size:12px;color:var(--err)">扫描失败: ${e.message}</div>`; }
  if(btn){ btn.disabled=false; btn.innerHTML=icon('magnet')+'扫种子'; }
}
async function pushSeasonMagnet(tmdbId, btn, i, season){
  const box = $(`#seasonSeeds-${season}`);
  const r = box && box._res && box._res[i]; if(!r||!r.magnet){ toast('缺少磁力链'); return; }
  const title = (r.name||'').split('/')[0].slice(0,80);
  btn.disabled=true; btn.textContent='推送中…';
  try{
    await api('/api/push',{method:'POST',body:JSON.stringify({
      magnet:r.magnet, title, content_type:'tv', language:'', toFolder:''
    })});
    if(!r.pushed) r.pushed = {cd2:false,qbit:false};
    r.pushed.cd2 = true;
    btn.innerHTML=icon('check')+'已推CD2'; btn.disabled=true; btn.style.background='var(--ok)';
    toast(`S${String(season).padStart(2,'0')} 已推送到 CD2 离线下载`);
  }catch(e){ btn.disabled=false; btn.textContent='推送'; toast('失败: '+e.message); }
}

function closeModal(){ const m=$('#modalBg'); m.classList.remove('show'); m.scrollTop=0; $('#modal').scrollTop=0; _navStack=[]; _ctxStack=[]; _personWorks=[]; _curDetail=null; _personCache.clear(); _personCurId=null; for(const k in _personScroll) delete _personScroll[k]; document.body.style.overflow=''; }
$('#modalBg').addEventListener('click', e=>{ if(e.target===$('#modalBg')) closeModal(); });

// 热门榜滚动自动加载: 滚动容器(#app 或视口)接近底部且有下一页 → 自动续
// (移动端上拉/桌面端滚到底都走这里; 防重入由 inst.loading 保证, 不会重复请求;
//  必须走 _trendNextPage 递增页码 —— 直接调 loadTrendList 会停在第 1 页反复重渲)
// 2026-09 二版: 电影/剧集是两个独立页签+独立实例, 守卫查"当前可见的热门实例"
// rAF 节流: 一帧最多处理一次(惯性滚动会把 scroll 打到每帧多次)
let _trScrollRaf = 0;
window.addEventListener('scroll', ()=>{
  if(_trScrollRaf) return;
  _trScrollRaf = requestAnimationFrame(()=>{
    _trScrollRaf = 0;
    const inst = _visibleTrend();
    if(!inst) return;
    if($('#modalBg').classList.contains('show')) return;   // 详情弹窗内滚动会冒泡到 window, 不能触发榜单加载
    if(inst.loading || !inst.hasMore || !inst.list.length) return;
    if(_trendNearBottom()) _trendNextPage(inst.kind);
  });
}, {passive:true});

