"""浏览/筛选/缺失/屏蔽/设置 路由 —— 基于本地 TMDB 缓存

数据全部来自本地 SQLite:
  - tmdb_media / tmdb_season : TMDB 元数据缓存(scripts/sync_tmdb.py 填充)
  - jellyfin_item + jf_episode : 本地媒体库 + 分集明细(增量/全量同步)
  - tmdb_blocklist / tmdb_setting : 屏蔽列表 / 设置

缺失逻辑(精确到集):
  - 电影: 不在 Jellyfin 库 = 缺失(未拥有)
  - 剧集: 逐季对比 Jellyfin 实有集(jf_episode) vs TMDB 应有集(tmdb_season)
          → 精确到 SxxExx;实有 > 应有 标记"集数偏多(版本问题)"
"""
import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config
from db.database import SessionLocal
from db import repositories as repo
from db.models import JellyfinItem
from lib import cache_stats

router = APIRouter(prefix="/api", tags=["browse"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _media_table_ready(session, kind):
    """可用性表(media)是否已有该类型数据(扫描跑过)。空表时回退旧算法,
    保证首次部署/扫描未完成前页面不是一片"缺失"。"""
    from db.models import Media, MediaType
    mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
    return session.query(Media.id).filter(Media.media_type == mt).first() is not None


def _jf_in_library_ids(session, kind):
    """该类型在库的 tmdb_id 集合 —— **以可用性表(media)为准**:
    状态 AVAILABLE / PARTIALLY_AVAILABLE = 在库(有真实文件)。严格只认 TMDB ID。

    media 表为空(扫描未跑)时回退旧口径(jellyfin_item 镜像), 保证可用性连续。
    Jellyfin 侧挂错 ID 的条目由 scripts/calibrate_jf_ids.py 用 TMDB 接口修正。
    """
    from db.models import Media, MediaStatus, MediaType
    mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
    rows = session.query(Media.tmdb_id).filter(
        Media.media_type == mt,
        Media.status.in_((MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
                          MediaStatus.OWNED_UNVERIFIED))).all()
    ids = {r[0] for r in rows}
    if not ids and not _media_table_ready(session, kind):
        # 回退: 旧镜像口径(媒体库里有该 TMDB ID 即算在库)
        jf_type = "Movie" if kind == "movie" else "Series"
        rows2 = session.query(JellyfinItem.tmdb_id).filter(
            JellyfinItem.type == jf_type, JellyfinItem.tmdb_id != "").all()
        ids = {str(r[0]) for r in rows2}
    else:
        ids = {str(i) for i in ids}
    return ids


def _media_complete_ids(session, kind):
    """作品级"完整" —— 以可用性表为准:
    电影: 在库即完整; 剧集: **所有非特别篇季** 都 AVAILABLE 才算完整
    (用户规则: 特别篇 S00 不算缺失, 不参与完整判定)。
    media 表为空时返回 None(调用方回退旧逻辑)。"""
    from db.models import Media, MediaStatus, MediaType, Season
    mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
    if not _media_table_ready(session, kind):
        return None
    if kind == "movie":
        rows = session.query(Media.tmdb_id).filter(
            Media.media_type == mt, Media.status == MediaStatus.AVAILABLE).all()
        return {r[0] for r in rows}
    q = (session.query(Media.tmdb_id, Season.season_number, Season.status)
         .join(Season, Season.media_id == Media.id)
         .filter(Media.media_type == mt))
    by_show = {}
    for tid, num, st in q.all():
        if num == 0:
            continue  # S00 特别篇不计
        by_show.setdefault(tid, []).append(st)
    return {tid for tid, sts in by_show.items()
            if sts and all(s == MediaStatus.AVAILABLE for s in sts)}


def _jf_item_id(session, kind, tmdb_id):
    """该作品在 Jellyfin 里的项 Id(详情页跳转 Jellyfin 用)。

    取自可用性表 media.jellyfin_media_id(扫描时写入)。未同步/不在库返回空串,
    前端据此不显示"在 Jellyfin 打开"按钮。"""
    from db.models import Media, MediaType
    mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
    row = session.query(Media.jellyfin_media_id).filter(
        Media.media_type == mt, Media.tmdb_id == tmdb_id).first()
    return (row[0] if row else "") or ""


def _jf_detail_url(cfg, item_id):
    """Jellyfin 详情页跳转链接: {jellyfin.url}/web/index.html#/details?id={item_id}。
    ⚠️ 必须用查询参数式 `#/details?id=`, 不能写 `#/details/{id}` 路径式 —— jellyfin-web
    路由表只注册了 {path:"details"}(query 传 id), 路径式未注册 → 「当前未找到页面」。
    (2026-09-22 实锤: 从 Jellyfin 12.1 服务端 bundle 里确认, 官方自身跳转也用 #/details?id=)
    无 item_id 或没配 jellyfin.url 返回空串(前端据此隐藏按钮)。"""
    if not item_id:
        return ""
    base = ((cfg or {}).get("jellyfin", {}) or {}).get("url", "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/web/index.html#/details?id={item_id}"


def _series_item_to_tmdb(session, tmdb_id=None):
    """{series_item_id: tmdb_id_str}。jf_episode.series_id 是 Series 项的 item_id。

    tmdb_id 给了 → 只查这一部剧(详情/单季页用)。
    """
    return repo.get_series_tmdb_map(session, tmdb_id)


def _jf_episode_map(session, series_ids=None):
    """{series_item_id: set((season, episode))} 本地实有集。

    series_ids 给了 → 只查这几部剧(全表 77,700 行 → 按 series_id 索引查是 0.1ms 级,
    实测全表 71ms; 详情/单季页传本剧的 series id, 缺失页全量构建才传 None)。
    """
    from db.models import JfEpisode
    q = session.query(JfEpisode.series_id, JfEpisode.season, JfEpisode.episode)
    if series_ids is not None:
        ids = [x for x in series_ids if x]
        if not ids:
            return {}
        q = q.filter(JfEpisode.series_id.in_(ids))
    out = {}
    for sid, s, e in q.all():
        out.setdefault(sid, set()).add((s, e))
    return out


def _jf_episodes_ready(session):
    """jf_episode 是否有数据 —— 分集级缺失的前提。

    ⚠️ 空表 = 未同步分集明细 → 此时"实有集"恒为空, 若照旧对比会把**所有在库剧判成缺全部集**
    (用户反馈"点开就算缺失")。调用方必须据此降级, 不再伪造分集缺失。
    """
    from db.models import JfEpisode
    return session.query(JfEpisode.id).limit(1).first() is not None


def _season_expected_numbers(season_row):
    """该季应有的集号集合。优先缓存的 episode_numbers, 否则估算 1..count(0 季含 0)。"""
    if season_row.episode_numbers:
        try:
            nums = json.loads(season_row.episode_numbers)
            if nums:
                return set(nums)
        except Exception:  # noqa: BLE001
            pass
    n = season_row.episode_count or 0
    if season_row.season_number == 0:
        return set(range(0, n))  # Specials 从 0 开始
    return set(range(1, n + 1))


def _abs_season_ranges(seasons):
    """各季在「绝对集号」方案下的集号集合(跨季连续累加, S0 不计入累计)。

    TMDB 对长篇动画按季分章但**集号跨季连续**: 《火影忍者:疾风传》(31910)
    S02 = 33..53、S20 = 414..500, 本地 Jellyfin 同源亦然; 而 tmdb_season.episode_numbers
    未回填时估算只有 1..count, 会把整剧每季都误判成"缺N多N"
    (用户反馈: 页面 缺468多468 而 Seerr 显示完整 —— 实测 2026-09-27)。
    S0 特别篇取 1..count(与 TMDB 实际一致, 估算的 0..n-1 是错的)。
    """
    cum = 0
    out = {}
    for se in sorted(seasons, key=lambda x: x.season_number or 0):
        cnt = se.episode_count or 0
        if not cnt and se.episode_numbers:
            try:
                cnt = len(json.loads(se.episode_numbers) or [])
            except Exception:  # noqa: BLE001
                cnt = 0
        if (se.season_number or 0) == 0:
            out[0] = set(range(1, cnt + 1))
            continue
        out[se.season_number] = set(range(cum + 1, cum + cnt + 1))
        cum += cnt
    return out


def _media_card(r, in_lib: bool, blocked: bool, complete: bool, extra=None):
    d = {
        "tmdbId": r.tmdb_id,
        "kind": r.kind,
        "title": r.title,
        "originalTitle": r.original_title,
        "englishTitle": r.english_title,
        "altTitles": [t for t in (r.alt_titles or "").split(",") if t],
        "year": r.year,
        "overview": (r.overview or "")[:240],
        "poster": r.poster,
        "vote": r.vote,
        "genreNames": [g for g in (r.genre_names or "").split(",") if g],
        "inLibrary": in_lib,
        "blocked": blocked,
        "complete": complete,
        "inProduction": r.in_production,
        "status": r.status,
    }
    if extra:
        d.update(extra)
    return d


def _real_numbers(season_row):
    """该季 TMDB 真实集号(已回填的 tmdb_season.episode_numbers)。没有则返回 None。"""
    if not season_row.episode_numbers:
        return None
    try:
        nums = json.loads(season_row.episode_numbers)
        if nums:
            return {int(n) for n in nums}
    except Exception:  # noqa: BLE001
        return None
    return None


def _series_missing(tmdb_id, seasons, s_map, ep_map, check_s0=False):
    """单部剧的分集缺失: 返回 (missing_count, extra_count, per_season, have_eps,
    tmdb_eps, numbers_synced)。

    s_map: {series_item_id: tmdb_id_str}; ep_map: {series_item_id: set((season,episode))}。
    由调用方预计算一次传入, 避免 O(n²) 重复查全表。
    check_s0: 开启时特别篇(S00)也计入作品级缺失(默认关, 与历史口径一致)。

    ⚠️ **没有真实集号就不猜**(2026-09-27 定的口径: 猜错缺/多, 问题就大了)。单季判定:
      ① 有 TMDB 真实集号(episode_numbers 已回填)→ 逐集精确比对;
      ② 本地该季一集都没有 → 缺全季(集数来自 TMDB, 任何编号方案下都成立);
      ③ 本地实有集号与「绝对集号区间」**完全一致** → 缺0/多0(完全对齐才敢下结论);
      ④ 其余(未回填又无法确证)→ 该季 numbersKnown=False, **不计入缺/多**;
         只要有季不可确证, 作品级 missing_count = None(前端显示"编号未同步")。
    绝不拿 1..count 估算去报"缺N"(火影疾风传 缺468多468 就是这么来的)。
    """
    # 找这部剧对应的 series item_id
    series_ids = [iid for iid, tid in s_map.items() if str(tid) == str(tmdb_id)]
    have = set()
    for iid in series_ids:
        have |= ep_map.get(iid, set())
    abs_ranges = _abs_season_ranges(seasons) if seasons else {}
    per_season, missing_count, extra_count, have_eps, tmdb_eps = [], 0, 0, 0, 0
    numbers_synced = True
    for se in seasons:
        est = _season_expected_numbers(se)     # 估算: 只用于「应有集数」展示与缺全季计数
        real = _real_numbers(se)               # TMDB 真实集号 | None
        alt = abs_ranges.get(se.season_number)
        have_se = {e for (s, e) in have if s == se.season_number}
        if real is not None:
            exp, known = real, True                                    # ①
        elif not have_se:
            exp, known = est, True                                     # ②
        elif alt is not None and have_se == alt:
            exp, known = alt, True                                     # ③
        else:
            exp, known = est, False                                    # ④
        miss = (exp - have_se) if known else set()
        extra = (have_se - exp) if known else set()
        per_season.append({
            "number": se.season_number, "name": se.name,
            "expected": len(exp), "have": len(have_se),
            "missing": len(miss) if known else 0,
            "extra": len(extra) if known else 0,
            "numbersKnown": known,
            "inProduction": bool(se.in_production),
            # 集号清单只有真实集号在手才给(否则给的也是猜的)
            "missingEpisodes": sorted(miss) if (known and real is not None) else [],
            "extraEpisodes": sorted(extra) if (known and real is not None) else [],
        })
        if se.season_number == 0 and not check_s0:
            # 特别篇(S00): 季行照常显示(进剧集详情可见缺哪集), 但**不计入作品级缺失**
            # (默认规则: 正剧缺集才算缺失, 特别篇不能算缺失 S00 季)。
            # check_missing_s0 开关打开后, S00 也计入作品级缺失。
            continue
        tmdb_eps += len(exp)
        have_eps += len(have_se)
        if known:
            missing_count += len(miss)
            extra_count += len(extra)
        else:
            numbers_synced = False
    if not numbers_synced:
        # 有季无法确证 → 作品级不报缺/多(与 jf_episode 未同步同一降级口径)
        missing_count = None
        extra_count = 0
    return missing_count, extra_count, per_season, have_eps, tmdb_eps, numbers_synced


async def _hydrate_episode_numbers(cfg, tmdb_id) -> int:
    """回填该剧中缺 tmdb_season.episode_numbers 的季(实现在 lib/episode_numbers)。

    没有真实集号就不猜缺失(「未回填 + 兜底不适用 → 编号未同步」), 所以开详情/
    缺失明细时先补集号: 一次回填永久缓存, 失败 600s 退避, 不阻断页面。
    """
    from lib import episode_numbers as epnums
    return await epnums.hydrate_show(cfg, tmdb_id)


def _persist_episode_numbers(tmdb_id, season_number, nums):
    """展开某季时顺手落库真实集号(0 额外 TMDB 请求; 实现在 lib/episode_numbers)。"""
    from lib import episode_numbers as epnums
    return epnums.persist_season_numbers(tmdb_id, season_number, nums)


def _in_library_series(tmdb_id) -> bool:
    """本地 Jellyfin 有没有这部剧(Series 项带该 TMDB ID)。

    详情页只给在库剧回填集号 —— 没看过的剧本地无分集, 逐季规则②本来就会判
    「已知、不报缺」, 再去 TMDB 拉集号是白花请求(刷热门/搜索时详情开得很勤)。
    口径与 get_series_tmdb_map 同源, 不会漏掉能算缺失的剧。
    """
    s = SessionLocal()
    try:
        return (s.query(JellyfinItem.item_id)
                .filter(JellyfinItem.type == "Series",
                        JellyfinItem.tmdb_id == str(tmdb_id))
                .first() is not None)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 热门(TMDB 趋势榜, 实时拉 + 短 TTL 进程内缓存, 不依赖本地同步)
# ---------------------------------------------------------------------------
_trending_cache: dict = {}   # {(kind, window, page): (ts, [raw], [deduped])}
_trend_seen: dict = {}       # {(kind, window) 或 fkey: set(去重键)} 跨页去重; 榜单刷新(第1页重新拉取)时重置
_TRENDING_TTL = 600          # 10 分钟
_TREND_SEEN_MAX = 200        # 去重集数量上限: 键是"每套筛选组合", 旧值只在同组合刷新时才清,
                             # 长期运行 + 用户试多种筛选会无界累积 → 超限清最早插入的(无害)。
def _trend_seen_get(key):
    """取/建某组合的跨页去重集, 顺带做容量保护(见 _TREND_SEEN_MAX)。"""
    s = _trend_seen.setdefault(key, set())
    if len(_trend_seen) > _TREND_SEEN_MAX:
        _trend_seen.pop(next(iter(_trend_seen)), None)
    return s
_TMDB_PAGE_SIZE = 20         # TMDB trending 固定每页 20 条


def _trend_dedup_key(c):
    """同一部片在 TMDB 榜单里可能有多个条目(不同 ID)→ 用 标题+年份 归一。"""
    return (str(c.get("title") or "").strip().lower(), str(c.get("year") or ""))


async def _trending_cached(cfg, kind, window, page, size):
    """返回 (deduped_cards, has_more)。
    has_more = 本页 TMDB 原始返回满 20 条(可能还有下一页); 不满/空 = 到底。
    (trending 的 total_results 恒为 10000 封顶假值, 不可用)
    跨页去重: 同 标题+年份 只保留榜单里排名靠前的那条(用户反馈重复条目);
    去重结果随缓存存, 刷新(缓存命中)时不会二次去重成空。"""
    key = (kind, window, page)
    now = time.time()
    hit = _trending_cache.get(key)
    if hit and now - hit[0] < _TRENDING_TTL:
        raw, deduped = hit[1], hit[2]
    else:
        from clients.tmdb import client as tmdb
        raw = await tmdb.trending(cfg, kind, time_window=window, page=page)
        if page == 1:  # 榜单刷新 → 重置跨页去重集
            _trend_seen.pop((kind, window), None)
        seen = _trend_seen_get((kind, window))
        deduped = []
        for c in raw[:size]:
            k = _trend_dedup_key(c)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(c)
        # ⚠️ TMDB 请求失败(限流 429/网络)时客户端返回空列表 —— 空结果不缓存,
        # 否则一次限流会把热门榜"空白 10 分钟"(2026-09-19 e2e 连测触发限流实测到)
        if raw:
            _trending_cache[key] = (now, raw, deduped)
            if len(_trending_cache) > 100:  # 防无限增长: 清最旧
                oldest = min(_trending_cache, key=lambda k: _trending_cache[k][0])
                _trending_cache.pop(oldest, None)
    has_more = len(raw) >= _TMDB_PAGE_SIZE
    return deduped, has_more


async def _discover_trending_cached(cfg, kind, window, genre, country, page, size,
                                    filters=None):
    """选了 国家/类型/日期/状态/分级/平台 筛选时 trending 接口不支持过滤 →
    走 discover(热度排序)。filters = 高级筛选 dict(见 clients.tmdb.discover_page)。
    同样 10 分钟缓存 + 跨页去重(筛选条件进缓存/去重键); discover 的 total_pages
    是真实值, 到底判断用它(不再靠"满 20 条"猜)。返回 (deduped, has_more)。"""
    from clients.tmdb import client as tmdb
    f = dict(filters or {})
    # ⚠️ 所有筛选条件都必须进 fkey: 少一个就会"换筛选吃到上一次的缓存页"
    fkey = (kind, window, genre or 0, country or "",
            f.get("date_from") or "", f.get("date_to") or "",
            str(f.get("status") or ""), f.get("cert") or "",
            int(f.get("provider") or 0), (f.get("watch_region") or "").upper())
    ckey = fkey + (page,)
    now = time.time()
    hit = _trending_cache.get(ckey)
    if hit and now - hit[0] < _TRENDING_TTL:
        raw, deduped, total_pages = hit[1], hit[2], hit[3]
    else:
        raw, total_pages = await tmdb.discover_page(
            cfg, kind, page=page, genre_id=genre or None, country=country or None,
            filters=f)
        if page == 1:
            _trend_seen.pop(fkey, None)
        seen = _trend_seen_get(fkey)
        deduped = []
        for c in raw[:size]:
            k = _trend_dedup_key(c)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(c)
        if raw:  # 同 trending: 空结果(TMDB 限流/失败)不缓存, 避免空白 10 分钟
            _trending_cache[ckey] = (now, raw, deduped, total_pages)
            if len(_trending_cache) > 100:
                oldest = min(_trending_cache, key=lambda k: _trending_cache[k][0])
                _trending_cache.pop(oldest, None)
    has_more = (page < total_pages) if total_pages else (len(raw) >= _TMDB_PAGE_SIZE)
    return deduped, has_more


# ⚠️ 必须声明在 /trending/{kind} 之前, 否则 "countries" 会被当作 kind 吃掉
@router.get("/trending/countries")
async def trending_countries():
    """榜单"按国家筛选"下拉: 影视产地国家表(TMDB 无全部国家端点, 用内置固定表)。"""
    from clients.tmdb import client as tmdb
    return tmdb.countries()


# ---- 高级筛选(日期范围 / 剧集状态 / 分级 / 流媒体平台) ----------------------
# trending 接口不支持这些过滤 → 任一非空就改走 discover(热度排序)。
# 校验集中在这里: 格式/取值不对一律 400, 绝不让 TMDB 静默忽略 ——
# 静默忽略 = "筛了等于没筛", 用户根本看不出来(正是这次要修的那类问题)。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# TMDB discover/tv with_status: 0 在播 / 1 计划 / 2 制作中 / 3 完结 / 4 取消 / 5 试播
_TV_STATUS = {"0", "1", "2", "3", "4", "5"}
_CERT_RE = re.compile(r"^[A-Z]{2}:[A-Za-z0-9\-\s]{1,16}$")


def _trend_filters(kind, date_from, date_to, status, cert, provider, watch_region):
    """校验 + 归一高级筛选 → dict(空值 = 不筛)。非法输入抛 HTTPException(400)。"""
    f = {"date_from": "", "date_to": "", "status": "", "cert": "",
         "provider": 0, "watch_region": ""}
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    for label, v in (("date_from", date_from), ("date_to", date_to)):
        if v and not _DATE_RE.match(v):
            raise HTTPException(400, f"{label} 需为 YYYY-MM-DD 形式(如 2024-01-01)")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "日期范围起止颠倒(起 ≤ 止)")
    f["date_from"], f["date_to"] = date_from, date_to

    status = (status or "").strip()
    if status:
        if kind != "tv":
            raise HTTPException(400, "状态筛选仅剧集可用(电影没有完结状态)")
        if status not in _TV_STATUS:
            raise HTTPException(400, "status 需为 0..5(0在播/1计划/2制作中/3完结/4取消/5试播)")
        f["status"] = status

    cert = (cert or "").strip()
    if cert:
        if kind != "movie":
            raise HTTPException(400, "分级筛选仅电影可用(TMDB discover/tv 无分级参数;"
                                     "剧集分级请在「管理 → 浏览」按本地缓存筛)")
        if not _CERT_RE.match(cert):
            raise HTTPException(400, "cert 需为 国家:分级 形式(如 US:PG-13)")
        f["cert"] = cert

    provider = int(provider or 0)
    if provider < 0:
        raise HTTPException(400, "provider 需为正整数(流媒体平台 id)")
    if provider:
        region = (watch_region or "").strip().upper()
        if region and not (len(region) == 2 and region.isalpha()):
            raise HTTPException(400, "watch_region 需为 2 字母国家代码(如 HK/US)")
        # TMDB 要求 with_watch_providers 与 watch_region 成对: 没给地区按默认地区兜底。
        # 默认不是 CN —— TMDB 没有中国大陆平台数据(watch_region=CN 恒 0 结果)。
        from clients.tmdb import client as _tmdb
        f["provider"], f["watch_region"] = provider, region or _tmdb.DEFAULT_WATCH_REGION
    return f


_FILTERS_TTL = 3600
_FILTERS_CACHE = {}   # {(kind, region): (ts, payload)} —— 平台/分级表基本不变


@router.get("/trending/filters")
async def trending_filters(kind: str = Query("movie"), region: str = Query(""),
                           cfg: dict = Depends(get_config)):
    """榜单高级筛选下拉一次取齐: 平台地区(内置表) + 该地区的流媒体平台 + 分级。

    分级只给电影 —— TMDB discover/tv 没有 certification 参数, 剧集分级在
    「管理 → 浏览」按本地 tmdb_media.certification 筛(那边数据是齐的)。
    平台/分级按 (kind, region) 进程内缓存 1 小时; 拉不到平台(TMDB 限流/无 key)
    时**不缓存**, 免得一次失败把下拉饿 1 小时。

    返回:
      region        实际生效的平台地区(请求里没给/给的地区已不在表里 → 默认地区)
      providers     该地区的流媒体平台;**空 = 该地区确实没有平台数据**
      providers_err 平台列表是拉取失败(限流/无 key)而非"没有数据" ——
                    前端据此决定是清空平台下拉还是保留旧选项
    """
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")
    from clients.tmdb import client as tmdb
    region = (region or "").strip().upper()
    if region and not (len(region) == 2 and region.isalpha()):
        raise HTTPException(400, "region 需为 2 字母国家代码(如 HK/US)")
    # 地区表里没有的地区(如 CN —— TMDB 没有中国大陆平台数据)→ 落到默认地区再取平台,
    # 免得"地区是 CN、平台列表却按别的地区拉"互相打架。回显 region 让前端跟着改。
    regions = tmdb.watch_regions()
    codes = [r["code"] for r in regions]
    if region not in codes:
        region = (tmdb.DEFAULT_WATCH_REGION if tmdb.DEFAULT_WATCH_REGION in codes
                  else (codes[0] if codes else tmdb.DEFAULT_WATCH_REGION))
    key = (kind, region)
    now = time.time()
    hit = _FILTERS_CACHE.get(key)
    if hit and now - hit[0] < _FILTERS_TTL:
        return hit[1]
    has_key = bool((cfg.get("tmdb", {}) or {}).get("api_key"))
    tmdb.last_error = ""
    providers = await tmdb.watch_providers(cfg, kind, region) if has_key else []
    # watch_providers 吞异常并写 last_error: 据此区分"该地区没有平台"和"没拉到"
    providers_err = (not has_key) or bool(tmdb.last_error)
    certs = await tmdb.certifications(cfg, kind) if (has_key and kind == "movie") else []
    payload = {"regions": regions, "region": region, "providers": providers,
               "providers_err": providers_err, "certifications": certs}
    # 有平台, 或"确实没平台(拉取成功)"→ 缓存; 拉取失败不缓存(否则一次限流饿下拉 1 小时)
    if providers or not providers_err:
        _FILTERS_CACHE[key] = (now, payload)
        if len(_FILTERS_CACHE) > 50:
            _FILTERS_CACHE.pop(min(_FILTERS_CACHE, key=lambda k: _FILTERS_CACHE[k][0]), None)
    return payload


@router.get("/trending/{kind}")
async def trending(kind: str, window: str = Query("week"),
                   page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=50),
                   genre: int = Query(0, ge=0),
                   country: str = Query("", max_length=2),
                   date_from: str = Query("", max_length=10),
                   date_to: str = Query("", max_length=10),
                   status: str = Query("", max_length=2),
                   cert: str = Query("", max_length=20),
                   provider: int = Query(0, ge=0),
                   watch_region: str = Query("", max_length=2),
                   cfg: dict = Depends(get_config)):
    """TMDB 热门榜(实时)。kind=movie|tv, window=day|week(不筛选时)。
    genre(类型)/ country(产地国)/ date_from~date_to(上映日期范围)/ status(剧集状态)/
    cert(分级, 仅电影)/ provider+watch_region(流媒体平台) 任一生效 → 改用
    discover 按热度排序的筛选榜(trending 接口不支持过滤)。
    不依赖本地缓存/同步 —— 手机上看热门影视用这个; 数据 10 分钟缓存一次。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")
    if window not in ("day", "week"):
        raise HTTPException(400, "window 仅支持 day / week")
    country = (country or "").strip().upper()
    if country and not (len(country) == 2 and country.isalpha()):
        raise HTTPException(400, "country 需为 2 字母国家代码(如 CN/JP/US)")
    f = _trend_filters(kind, date_from, date_to, status, cert, provider, watch_region)
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法拉取热门榜")
    if genre or country or any(f.values()):
        cards, has_more = await _discover_trending_cached(
            cfg, kind, window, genre, country, page, size, filters=f)
    else:
        cards, has_more = await _trending_cached(cfg, kind, window, page, size)
    # 标记哪些已在库(与浏览页一致的"库内/缺失"感), 不阻塞: 库没同步就是空
    # 「隐藏已完整/已入库」设置对热门页同样生效: 开启后库内条目不进列表
    def _mark():
        s = SessionLocal()
        try:
            hide = repo.get_setting(s, "hide_complete", "false") == "true"
            in_lib_ids = _jf_in_library_ids(s, kind)
            from db.models import Media, MediaType
            mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
            status_map = {str(r.tmdb_id): r.status for r in
                          s.query(Media.tmdb_id, Media.status)
                          .filter(Media.media_type == mt)}
            # ⚠️ 屏蔽列表必须在这里过滤: 之前热门榜漏了 block_ids(浏览/缺失页都有),
            # 导致详情页"屏蔽此作品"后榜单里照样出现 → 用户以为屏蔽没生效
            block_ids = repo.get_blocklist_tmdb_ids(s, kind)
            out = []
            for c in cards:
                if str(c.get("tmdbId")) in block_ids:
                    continue
                c["inLibrary"] = str(c.get("tmdbId")) in in_lib_ids
                c["availStatus"] = status_map.get(str(c.get("tmdbId")))
                if not (hide and c["inLibrary"]):
                    out.append(c)
            return {"items": out, "hasMore": has_more}
        finally:
            s.close()
    return await run_in_threadpool(_mark)


# ---------------------------------------------------------------------------
# 浏览 / 筛选
# ---------------------------------------------------------------------------
# tmdb_media 全量行缓存: 浏览页每翻一页都要把整表 ORM 拉出来(电影 4157 行冷查询
# ~1.2s), 滚动加载下一页时这就是主要卡顿 → 缓存复用, 失效戳 = (count, max(synced_at)):
# 同步任务新增/更新/删除必改其一 → 一变就重建, 其余请求 ~ms 级直接复用(比纯 TTL 更准)。
# ⚠️ 行必须在本次会话里"最后"查询(其后不 commit/rollback), 否则属性会被过期,
# 离开会话后再读会 DetachedInstanceError。
_TMDB_MEDIA_ROWS = {}


def _tmdb_media_rows(session, kind):
    from sqlalchemy import func
    from db.models import TmdbMedia
    cnt, mx = (session.query(func.count(TmdbMedia.id), func.max(TmdbMedia.synced_at))
               .filter(TmdbMedia.kind == kind).one())
    stamp = (cnt, str(mx))
    hit = _TMDB_MEDIA_ROWS.get(kind)
    if hit and hit[0] == stamp:
        return hit[1]
    rows = repo.get_tmdb_media(session, kind=kind, q="", limit=100000)
    _TMDB_MEDIA_ROWS[kind] = (stamp, rows)
    return rows


# 剧集完结状态筛选: 值 → (status, in_production) 判定。数据齐不齐只看本地缓存 ——
# 没缓存的作品在选了该筛选时一律排除(宁可少给, 不能拿"未知"当"符合")。
_TV_CSTATUS = {
    "ended": lambda st, inp: st == "Ended",
    "returning": lambda st, inp: st == "Returning Series" or bool(inp),
    "canceled": lambda st, inp: st == "Canceled",
}


def _year_int(v):
    """年份列是字符串(可能是 "2020" / "2020-05-01" / "")→ 取前 4 位整数, 取不到 = 0。"""
    try:
        return int(str(v or "")[:4])
    except ValueError:
        return 0


@router.get("/browse")
async def browse(kind: str = Query("movie"), q: str = Query(""),
                 genre: int = Query(0),
                 year_from: int = Query(0), year_to: int = Query(0),
                 cstatus: str = Query(""),   # 剧集完结状态: ended|returning|in_production|canceled
                 cert: str = Query(""),      # 分级 "US:TV-14"(本地 tmdb_media.certification)
                 status: str = Query("all"),  # all | missing | inlibrary | complete(库内可用性)
                 page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=96),
                 cfg: dict = Depends(get_config)):
    """浏览/筛选(作品清单来自可用性表 media, 展示元数据按需)。

    数据源:
      - 可用性(inLibrary/complete/缺失): 只认 media/season 表(全量权威, 含
        Jellyfin 里所有在库作品 —— 不再受"元数据缓存没同步到"的影响)。
      - 展示元数据(标题/海报/年份): tmdb_media 快速路径 → TMDB 实时(进程内缓存)。
        库里作品若在 tmdb_media 没缓存, 现从 TMDB 拉(只拉当前页), 绝不漏掉在库作品。

    高级筛选(全部走本地 tmdb_media, 零网络):
      year_from~year_to  年份范围(两端都给 = 闭区间; 没年份的作品进不了范围)
      cstatus            仅剧集: 完结状态(status/in_production 列)
      cert               分级: 与本地 certification 同格式, 按 "/" 拆开精确比对
    """
    if year_from and year_to and year_from > year_to:
        raise HTTPException(400, "年份范围起止颠倒(起 ≤ 止)")
    if cstatus:
        if kind != "tv":
            raise HTTPException(400, "完结状态筛选仅剧集可用")
        if cstatus not in ("ended", "returning", "canceled"):
            raise HTTPException(400, "cstatus 仅支持 ended|returning|canceled")
    def _db_q():
        s = SessionLocal()
        try:
            hide_complete = repo.get_setting(s, "hide_complete", "false") == "true"
            block_ids = repo.get_blocklist_tmdb_ids(s, kind)
            in_lib_ids = _jf_in_library_ids(s, kind)
            complete_ids = _media_complete_ids(s, kind)
            from db.models import Media, MediaType
            mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
            # 权威在库清单: media 表 —— 全量覆盖 Jellyfin 里所有在库作品,
            # 不再受"元数据缓存没同步到"影响。title/year 扫描时从 Jellyfin 带入, 0 API。
            # ⚠️ title/year/status 一次取齐(旧写法两遍全表扫, 白付一倍)。
            media_rows, status_map = {}, {}
            for _tid, _t, _y, _st in s.query(Media.tmdb_id, Media.title, Media.year,
                                             Media.status).filter(Media.media_type == mt):
                media_rows[str(_tid)] = (_t or "", _y or "")
                status_map[str(_tid)] = _st
            # 展示元数据快速路径: 走 stamp 缓存(count + max(synced_at) 命中就不再扫表)。
            # 旧写法每次请求全表 ORM 物化 movie 4157 行 ≈ 45ms —— 浏览页最大的一块 DB 开销。
            cached = {str(r.tmdb_id): r for r in _tmdb_media_rows(s, kind)}

            # ── 纯 CPU 段: 过滤 + 排序 + 切页(6k 条 ≈ 60ms)一并放进线程池 ──
            # 放在事件循环里会让同屏的其它请求(详情/磁力/轮询)跟着卡。
            def _meta_of(tid):
                """一个作品的展示 meta。cached 行优先(完整); 否则用 media 表 title/year 兜底。
                _cached=True 表示有完整元数据(可参与 genre 筛选 + 直接出卡片)。"""
                r = cached.get(tid)
                if r is not None:
                    return {"title": r.title or "", "originalTitle": r.original_title or "",
                            "year": r.year or "", "overview": (r.overview or "")[:240],
                            "poster": r.poster or "", "vote": r.vote or 0.0,
                            "genreNames": [g for g in (r.genre_names or "").split(",") if g],
                            "inProduction": bool(r.in_production), "status": r.status or "",
                            "_cached": True}
                t, y = media_rows.get(tid, ("", ""))
                return {"title": t, "originalTitle": t, "year": y, "overview": "",
                        "poster": "", "vote": 0.0, "genreNames": [],
                        "inProduction": False, "status": "", "_cached": False}

            all_ids = set(media_rows) | set(cached)
            recs = []
            for tid in all_ids:
                if tid in block_ids:
                    continue
                in_lib = tid in in_lib_ids
                # complete 口径: media 表空(回退)时"在库即完整"; 否则按季 rollup
                if in_lib and complete_ids is not None:
                    complete = tid in complete_ids
                else:
                    complete = in_lib if complete_ids is None else False
                meta = _meta_of(tid)
                # 筛选: q/年份范围 对 media 表回填的 title/year 生效(全在库可达);
                # genre / 完结状态 / 分级 需要元数据缓存(cached), 没缓存的作品选了就排除
                if q:
                    ql = q.lower()
                    if ql not in ((meta["title"] + " " + meta["originalTitle"]).lower()):
                        continue
                y = _year_int(meta["year"])
                if year_from and (not y or y < year_from):
                    continue
                if year_to and (not y or y > year_to):
                    continue
                if cstatus:
                    if not meta["_cached"] or \
                            not _TV_CSTATUS[cstatus](cached[tid].status or "",
                                                     bool(cached[tid].in_production)):
                        continue
                if cert:
                    have = {p.strip() for p in (cached[tid].certification or "").split("/")} \
                        if meta["_cached"] else set()
                    if cert not in have:
                        continue
                if genre:
                    if not meta["_cached"] or \
                            str(genre) not in (cached[tid].genres or "").split(","):
                        continue
                # "隐藏已完整作品"只作用于「全部」: 显式选了 库内/完整 却因为这个开关被清空
                # (旧行为)等于筛选器失效, 选了也没东西可看。
                if hide_complete and status == "all" and complete and in_lib:
                    continue
                if status == "missing" and in_lib:
                    continue
                if status == "inlibrary" and not in_lib:
                    continue
                if status == "complete" and not complete:
                    continue
                recs.append((tid, in_lib, complete, meta))

            # 排序: 有标题的按标题字母序, 无标题的殿后(按 tmdb_id 稳定)
            recs.sort(key=lambda x: ((not x[3]["title"]), x[3]["title"].lower(), x[0]))
            total = len(recs)
            start = (page - 1) * size
            page_rec = recs[start:start + size]
            return {
                "hide_complete": hide_complete, "block_ids": block_ids,
                "in_lib_ids": in_lib_ids, "complete_ids": complete_ids,
                "media_rows": media_rows, "cached": cached,
                "status_map": status_map, "total": total, "page_rec": page_rec,
            }
        finally:
            s.close()

    ctx = await run_in_threadpool(_db_q)
    status_map = ctx["status_map"]   # 可用性状态码(4/5/8), 供前端顶层徽章区分
    total = ctx["total"]
    page_rec = ctx["page_rec"]       # 已过滤 + 已排序 + 已切页(线程池里算好的当前页)

    # 展示元数据: cached 命中直接用; 未命中的对**当前页**批量现拉 TMDB(进程内 24h 缓存)
    from server import mediacache
    need_fetch = [tid for tid, _, _, m in page_rec if not m["_cached"]]
    fetched = {}
    if need_fetch and (cfg.get("tmdb", {}) or {}).get("api_key"):
        fetched = await mediacache.get_many(cfg, kind, [int(t) for t in need_fetch])

    cards = []
    for tid, in_lib, complete, meta in page_rec:
        if not meta["_cached"]:
            p = fetched.get(int(tid)) or {}
            if p.get("title") or p.get("poster"):
                meta = {"title": p.get("title") or meta["title"],
                        "originalTitle": p.get("originalTitle") or meta["originalTitle"],
                        "year": p.get("year") or meta["year"],
                        "overview": (p.get("overview") or "")[:240],
                        "poster": p.get("poster") or "", "vote": p.get("vote") or 0.0,
                        "genreNames": p.get("genres") or [],
                        "inProduction": bool(p.get("in_production")),
                        "status": p.get("status") or "", "_cached": True}
        cards.append({
            "tmdbId": int(tid), "kind": kind, "title": meta["title"],
            "originalTitle": meta["originalTitle"], "year": meta["year"],
            "overview": meta["overview"], "poster": meta["poster"],
            "vote": meta["vote"], "genreNames": meta["genreNames"],
            "inLibrary": in_lib, "blocked": False, "complete": complete,
            "availStatus": status_map.get(tid),
            "inProduction": meta["inProduction"], "status": meta["status"],
        })
    return {"total": total, "page": page, "items": cards}


def _region_fields(title, original_title, original_language, countries, cfg=None):
    """详情页「地区」= organize 归类同一套口径(lib.classify, 含用户配的地区档)。

    前端直接展示服务端给的 regionKey/regionLabel —— 不再自己复刻一份语言/国家映射
    (地区档可配置后, 复刻的那份必然与后端漂移)。text 用 片名 + 原名: 关键词档(港片/
    纪录片这类)靠标题命中, 组织整理时还会带上文件名, 这里没有文件名, 可能差一档。
    """
    from lib.classify import region_info
    try:
        ri = region_info(
            {"title": title or "", "filename": original_title or "",
             "language": original_language or "",
             "countries": list(countries or [])},
            cfg or {},
        )
        return {"regionKey": ri["key"], "regionLabel": ri["label"]}
    except Exception:  # noqa: BLE001  地区显示挂了不该拖垮整个详情
        return {"regionKey": "", "regionLabel": ""}


def _detail_payload(s, kind, tmdb_id, cfg=None):
    """把本地缓存的一行 tmdb_media 拼成详情弹窗形状(含剧集分集缺失)。
    无缓存行返回 None。

    englishTitle(磁力双查用): 优先取本地缓存列; 缺失时按需从 TMDB 拉一次并回填,
    让"中文片用英文标题搜一遍"在老数据上也能生效(首次点开补, 之后走缓存)。"""
    m = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
    if not m:
        return None
    english_title = m.english_title or ""
    if not english_title:
        if (m.original_language or "") == "en":
            english_title = m.title or ""
        elif cfg is not None:
            try:
                from clients.tmdb import client as _tmdb
                english_title = _tmdb.english_title_sync(cfg, kind, tmdb_id) or ""
                if english_title:
                    m.english_title = english_title
                    s.commit()
            except Exception:  # noqa: BLE001
                english_title = ""
    jf_item = _jf_item_id(s, m.kind, m.tmdb_id)
    d = {
        "tmdbId": m.tmdb_id, "kind": m.kind, "title": m.title,
        "originalTitle": m.original_title, "englishTitle": english_title, "year": m.year,
        # 磁力多标题匹配: 所有不同中文译名(繁/台/港) + 最大查询组数(系统参数, 0=全部)
        "altTitles": [t for t in (m.alt_titles or "").split(",") if t],
        "maxQueryGroups": int((cfg or {}).get("search", {}).get("max_query_groups", 0) or 0),
        # 地区判定所需原始字段(与 organize 同口径): 判港台(Hk)只能靠 countries,
        # 港片 original_language 常是 cn; 详情页据此显示地区, organize 据此分类(防鼠胆龙威类错配)。
        "originalLanguage": m.original_language or "",
        "countries": [c for c in (m.countries or "").split(",") if c],
        **_region_fields(m.title, m.original_title, m.original_language,
                         [c for c in (m.countries or "").split(",") if c], cfg),
        "overview": m.overview, "poster": m.poster, "backdrop": m.backdrop,
        "vote": m.vote, "genres": [g for g in (m.genre_names or "").split(",") if g],
        "certification": m.certification, "runtime": m.runtime,
        "premiered": m.premiered, "imdb_id": m.imdb_id,
        "inProduction": m.in_production, "status": m.status,
        # 严格只认 TMDB ID —— 用户规定不允许按剧名兜底; Jellyfin 侧挂错 ID 的
        # 条目由 scripts/calibrate_jf_ids.py 用 TMDB 接口校准修正
        "inLibrary": str(m.tmdb_id) in _jf_in_library_ids(s, m.kind),
        # Jellyfin 项 Id + 跳转链接(库内作品): 从可用性表 media 取, 无则空串
        "jfItemId": jf_item,
        "jfUrl": _jf_detail_url(cfg, jf_item),
    }
    try:
        d["cast"] = json.loads(m.cast_json or "[]")
    except Exception:  # noqa: BLE001
        d["cast"] = []
    try:
        d["directors"] = json.loads(m.directors_json or "[]")
    except Exception:  # noqa: BLE001
        d["directors"] = []
    try:
        d["studios"] = json.loads(m.studios_json or "[]")
    except Exception:  # noqa: BLE001
        d["studios"] = []
    if kind == "tv":
        seasons = repo.get_tmdb_seasons(s, tmdb_id)
        check_s0 = repo.get_setting(s, "check_missing_s0", "false") == "true"
        # ⚠️ 分集明细(jf_episode)未同步时, 不能做分集级缺失对比 —— 否则实有集恒空,
        # 会把在库的剧全部判成"缺全部集"(用户反馈"点开就算缺失")。此时降级: 不报假缺失。
        if not _jf_episodes_ready(s):
            # 作品级"应有集"口径与 _series_missing 一致: 默认不计 S00, check_s0 打开才计
            tmdb_eps = sum(len(_season_expected_numbers(se)) for se in seasons
                           if (se.season_number != 0) or check_s0)
            d["episodesSynced"] = False
            d["numbersSynced"] = False
            d["seasons"] = []
            d["haveEpisodes"] = 0
            d["tmdbEpisodes"] = tmdb_eps
            d["missingCount"] = None      # None = 未知(分集未同步), 前端据此显示提示而非"缺N集"
            d["extraCount"] = 0
        else:
            s_map = _series_item_to_tmdb(s, tmdb_id)
            ep_map = _jf_episode_map(s, list(s_map))
            mc, xc, per_season, have_eps, tmdb_eps, nsync = _series_missing(
                tmdb_id, seasons, s_map, ep_map, check_s0)
            d["episodesSynced"] = True
            d["numbersSynced"] = nsync    # False = 有季没有 TMDB 真实集号 → 不报缺/多
            d["seasons"] = per_season
            d["haveEpisodes"] = have_eps
            d["tmdbEpisodes"] = tmdb_eps
            d["missingCount"] = mc        # numbersSynced=False 时为 None
            d["extraCount"] = xc
    return d


@router.get("/browse/detail/{kind}/{tmdb_id}")
async def browse_detail(kind: str, tmdb_id: int, cfg: dict = Depends(get_config)):
    """详情弹窗数据(本地缓存)。含 分集缺失信息(剧集)。未缓存返回 404(前端会转现拉)。"""
    if kind == "tv":
        # 缺 episode_numbers 的季先回填 TMDB 真实集号 —— 没有真实集号就不报缺/多
        # (「编号未同步」)。已回填的剧不发任何请求; 不在库的剧不用回填(本地无分集
        # 本来就不报缺), 刷热门/搜索时详情开得勤, 别白打 TMDB。
        if await run_in_threadpool(_in_library_series, tmdb_id):
            await _hydrate_episode_numbers(cfg, tmdb_id)
    def _q():
        s = SessionLocal()
        try:
            d = _detail_payload(s, kind, tmdb_id, cfg)
            if d is None:
                raise HTTPException(404, "本地缓存无此条目(将自动现拉 TMDB)")
            cache_stats.hit("tmdb")     # 本地 tmdb_media 行直接供上, 没走网络
            return d
        finally:
            s.close()
    return await run_in_threadpool(_q)


# 逐季每集明细的进程内 TTL 缓存(TMDB 每集按季拉取, 一集一查太浪费; 展开过一次即缓存)
_season_ep_cache: dict = {}   # (tmdb_id, season) -> (ts, {episode: {name, air_date, overview}})
_SEASON_EP_TTL = 3600


@router.get("/browse/tv/{tmdb_id}/season/{season_number}/episodes")
async def browse_season_episodes(tmdb_id: int, season_number: int,
                                 cfg: dict = Depends(get_config)):
    """某季逐集明细(折叠列表用): [{episode, name, air_date, overview, have}]。

    - TMDB 每集(名/播出时间/简介)按季现拉(1 次 /tv/{id}/season/{n}, 3600s TTL 进程内缓存);
    - have = 本地 Jellyfin 实有(jf_episode); jf 未同步时 have 全 false(与缺失口径一致);
    - 集号以 TMDB 实际集为准(含跳集/特别篇 0 号); TMDB 拉不到时回退本地应有集号(无名无日期)。
    """
    from clients.tmdb import client as tmdb

    def _have_map():
        s = SessionLocal()
        try:
            if not _jf_episodes_ready(s):
                return None   # jf 未同步 → 无法判定实有, have 全 false
            s_map = _series_item_to_tmdb(s, tmdb_id)
            ep_map = _jf_episode_map(s, list(s_map))
            have = set()
            for iid, tid in s_map.items():
                if str(tid) == str(tmdb_id):
                    have |= ep_map.get(iid, set())
            return {e for (sn, e) in have if sn == season_number}
        finally:
            s.close()

    have_set = await run_in_threadpool(_have_map)

    key = (tmdb_id, season_number)
    now = time.time()
    hit = _season_ep_cache.get(key)
    info = hit[1] if hit and now - hit[0] < _SEASON_EP_TTL else None
    if info is None:
        eps = await tmdb.season_episodes(cfg, tmdb_id, season_number)
        info = {e.get("episode"): {
            "name": e.get("name") or "",
            "air_date": e.get("air_date") or "",
            # 简介限长: 逐集列表一屏几十集, 全文太长会撑爆页面 + 缓存
            "overview": (e.get("overview") or "")[:120],
        } for e in eps if e.get("episode") is not None}
        if len(_season_ep_cache) >= 200:
            _season_ep_cache.pop(next(iter(_season_ep_cache)))
        _season_ep_cache[key] = (now, info)
        # 同一次请求顺手回填真实集号(tmdb_season.episode_numbers) —— 缺失对比
        # 精确到集的前提, 0 额外 TMDB 调用; 已回填的季不写。
        await run_in_threadpool(_persist_episode_numbers, tmdb_id, season_number, sorted(info))

    if info:
        out = [{"episode": e,
                "name": info[e]["name"],
                "air_date": info[e]["air_date"],
                "overview": info[e]["overview"],
                "have": bool(have_set and e in have_set)}
               for e in sorted(info)]
    else:
        # TMDB 拉不到(无 key/限流): 回退本地缓存的应有集号, 集名/日期留空(前端显"第 N 集")
        s = SessionLocal()
        try:
            se = next((x for x in repo.get_tmdb_seasons(s, tmdb_id)
                       if x.season_number == season_number), None)
            nums = sorted(_season_expected_numbers(se)) if se else list(range(1, 13))
        finally:
            s.close()
        out = [{"episode": e, "name": "", "air_date": "", "overview": "",
                "have": bool(have_set and e in have_set)} for e in nums]
    return {"tmdbId": tmdb_id, "season": season_number, "episodes": out}


# 现拉 TMDB 详情的进程内 TTL 缓存。
# ⚠️ 不变量: **查看绝不写库** —— /pull 只现拉返回, 不落任何本地表;
# 本地库只由 Jellyfin 扫描(scripts/sync_jf_scanner.py)在扫描时写入。
_pull_detail_cache: dict = {}   # {(kind, tmdb_id): (ts, payload_or_None)}
_PULL_TTL = 3600


async def _pull_detail(kind: str, tmdb_id: int, cfg) -> dict:
    """现拉 TMDB 详情, 拼成与 _detail_payload **同形状**的返回 —— 但不写任何表。"""
    from clients.tmdb import client as tmdb
    key = (kind, tmdb_id)
    now = time.time()
    hit = _pull_detail_cache.get(key)
    if hit and now - hit[0] < _PULL_TTL:
        cache_stats.hit("tmdb")     # 进程内 TTL 缓存接住了, 没走网络
        return hit[1]
    meta = None
    for _ in range(2):
        try:
            meta = await tmdb.detail(cfg, kind, tmdb_id)
            if meta:
                break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1.0)
    if not meta:
        _pull_detail_cache[key] = (now, None)
        return None
    if len(_pull_detail_cache) > 400:
        oldest = min(_pull_detail_cache, key=lambda k: _pull_detail_cache[k][0])
        _pull_detail_cache.pop(oldest, None)
    english_title = meta.get("title") if (meta.get("original_language") or "") == "en" else ""
    if not english_title:
        try:
            english_title = await tmdb.english_title(cfg, kind, tmdb_id)
        except Exception:  # noqa: BLE001
            english_title = ""
    # 可用性判定走 media 表(不在库 = UNKNOWN/DELETED)
    s = SessionLocal()
    in_lib = False
    jf_item = ""
    avail_status = None
    try:
        in_lib = str(tmdb_id) in _jf_in_library_ids(s, kind)
        jf_item = _jf_item_id(s, kind, tmdb_id)
        from db.models import Media, MediaType
        mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
        mrow = s.query(Media.status).filter_by(tmdb_id=tmdb_id,
                                               media_type=mt).first()
        avail_status = mrow[0] if mrow else None
    finally:
        s.close()
    d = {
        "tmdbId": tmdb_id, "kind": kind,
        "title": meta.get("title", ""), "originalTitle": meta.get("originalTitle", ""),
        "englishTitle": english_title, "year": meta.get("year", ""),
        # 磁力多标题匹配: 现拉 TMDB 的 altTitles(繁/台/港) + 最大查询组数(0=全部)
        "altTitles": meta.get("altTitles") or [],
        "maxQueryGroups": int((cfg or {}).get("search", {}).get("max_query_groups", 0) or 0),
        "overview": meta.get("overview", ""), "poster": meta.get("poster", ""),
        "backdrop": meta.get("backdrop", ""), "vote": meta.get("vote", 0.0),
        "genres": list(meta.get("genres") or []),
        "certification": meta.get("certification", ""), "runtime": meta.get("runtime") or 0,
        "premiered": meta.get("premiered", ""), "imdb_id": meta.get("imdb_id", ""),
        # 地区判定所需原始字段(与 organize 同口径, 见 _detail_payload 注)
        "originalLanguage": meta.get("original_language") or "",
        "countries": meta.get("countries") or [],
        **_region_fields(meta.get("title", ""), meta.get("originalTitle", ""),
                         meta.get("original_language") or "",
                         meta.get("countries") or [], cfg),
        "inProduction": bool(meta.get("in_production")), "status": meta.get("status", ""),
        "inLibrary": in_lib,
        "availStatus": avail_status,
        "jfItemId": jf_item,
        "jfUrl": _jf_detail_url(cfg, jf_item),
        "cast": meta.get("cast") or [], "directors": meta.get("directors") or [],
        "studios": [x.get("name") if isinstance(x, dict) else x for x in (meta.get("studios") or [])],
    }
    if kind == "tv":
        # 分集缺失与缓存路径同算法(TMDB 应有集 vs Jellyfin 实有集); 特别篇不计作品级
        if await run_in_threadpool(_in_library_series, tmdb_id):
            # 在库剧先把 TMDB 真实集号回填进来, 否则这条现拉详情只敢显示「编号未同步」
            await _hydrate_episode_numbers(cfg, tmdb_id)
        try:
            tseasons = await tmdb.all_seasons(cfg, tmdb_id) or []
        except Exception:  # noqa: BLE001
            tseasons = []
        s = SessionLocal()
        try:
            # 回填后的真实集号(现拉的季对象本身不带 episode_numbers)
            persisted = {x.season_number: (x.episode_numbers or "")
                         for x in repo.get_tmdb_seasons(s, tmdb_id)}
            s_map = _series_item_to_tmdb(s, tmdb_id)
            ep_map = _jf_episode_map(s, list(s_map))
            check_s0 = repo.get_setting(s, "check_missing_s0", "false") == "true"

            class _TS:  # _season_expected_numbers 只读这些属性
                def __init__(self, number, name, count, in_prod):
                    self.season_number = number
                    self.name = name
                    self.episode_count = count
                    self.episode_numbers = persisted.get(number, "")
                    self.in_production = in_prod

            seasons = [_TS(t.get("number") or 0, t.get("name", ""),
                           t.get("episodes") or t.get("episode_count") or 0,
                           bool(t.get("in_production"))) for t in tseasons]
            mc, xc, per_season, have_eps, tmdb_eps, nsync = _series_missing(
                tmdb_id, seasons, s_map, ep_map, check_s0)
            d.update(episodesSynced=True, numbersSynced=nsync, seasons=per_season,
                     haveEpisodes=have_eps,
                     tmdbEpisodes=tmdb_eps, missingCount=mc, extraCount=xc)
        finally:
            s.close()
    _pull_detail_cache[key] = (now, d)
    return d


@router.post("/browse/detail/{kind}/{tmdb_id}/pull")
async def browse_detail_pull(kind: str, tmdb_id: int, cfg: dict = Depends(get_config)):
    """本地缓存没有时**现拉 TMDB 并返回**(与 GET 同形状)。

    ⚠️ **不落库** —— 查看不产生任何本地写入, 本地
    media/season 可用性只由 Jellyfin 扫描维护(根治"查看即污染"一类问题)。
    热门/搜索命中的库外作品靠这个接口点开也能看, 进程内缓存 1 小时省 API。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法现拉详情")
    d = await _pull_detail(kind, tmdb_id, cfg)
    if d is None:
        # ⚠️ 文案不能含"本地缓存无" —— 前端见该字样会自动重试 /pull 死循环
        raise HTTPException(404, "TMDB 无此条目(未上映或不存在)")
    d["pulled"] = True   # 前端提示"已现拉"
    return d


@router.get("/browse/search")
async def browse_search(q: str = Query(..., min_length=1), cfg: dict = Depends(get_config)):
    """按片名搜具体影片/剧集(主源 TMDB 直连), 结果叠加 库内/屏蔽 标注。

    前端两段式搜索第一步: 先确认是哪部影片, 点卡片进详情, 再在详情里看磁力。
    """
    if not q.strip():
        raise HTTPException(400, "查询词不能为空")
    from clients.tmdb import client as tmdb_client
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法搜索影片")
    try:
        cards = await tmdb_client.search_cards(cfg, q.strip())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TMDB 搜索失败: {e}")
    if not cards:
        return {"results": []}

    def _annotate():
        s = SessionLocal()
        try:
            return {
                k: {"ids": _jf_in_library_ids(s, k),
                    "block": repo.get_blocklist_tmdb_ids(s, k)}
                for k in ("movie", "tv")
            }
        finally:
            s.close()

    sets = await run_in_threadpool(_annotate)
    for c in cards:
        s = sets.get(c["kind"]) or {}
        tid = str(c.get("tmdbId") or "")
        c["inLibrary"] = tid in s.get("ids", set())
        c["blocked"] = tid in s.get("block", set())
    return {"results": cards}


# ---------------------------------------------------------------------------
# 演员(搜索 + 详情页: 头像/简介/作品)
# ---------------------------------------------------------------------------
@router.get("/person/search")
async def person_search_api(q: str = Query(..., min_length=1),
                            limit: int = Query(8, ge=1, le=20),
                            cfg: dict = Depends(get_config)):
    """按名字搜演员(TMDB /3/search/person)。前端两段式搜索的「演员」段。"""
    if not q.strip():
        raise HTTPException(400, "查询词不能为空")
    from clients.tmdb import client as tmdb_client
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法搜索演员")
    try:
        return {"results": await tmdb_client.person_search(cfg, q.strip(), limit=limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TMDB 演员搜索失败: {e}")


@router.get("/person/{person_id}")
async def person_detail_api(person_id: int, cfg: dict = Depends(get_config)):
    """演员详情: 头像/简介 + 电影/剧集作品(带角色名, 倒序)。

    详情页点演员/搜索演员后进入这个页面; 作品卡片可直接点开看详情与磁力。
    """
    from clients.tmdb import client as tmdb_client
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法加载演员")
    try:
        detail = await tmdb_client.person_details(cfg, person_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TMDB 演员详情失败: {e}")
    if not detail:
        raise HTTPException(404, "演员不存在")
    credits = await tmdb_client.person_credits(cfg, person_id)
    # 作品卡标"库内"(2026-09-19 用户要求: 演员页看作品, 库内已有的标"库内", 库外不标)。
    # 口径与 浏览/搜索 一致: 以可用性表(media) AVAILABLE/PARTIALLY 为准。
    def _mark_library():
        s = SessionLocal()
        try:
            inlib = {k: _jf_in_library_ids(s, k) for k in ("movie", "tv")}
        finally:
            s.close()
        for k in ("movies", "tv"):
            kind = "movie" if k == "movies" else "tv"
            for c in credits.get(k) or []:
                c["inLibrary"] = str(c.get("tmdbId") or "") in inlib[kind]
        return credits

    credits = await run_in_threadpool(_mark_library)
    return {
        "id": person_id,
        "name": detail.get("name") or "",
        "profile": (detail.get("profile_path")
                    and f"https://image.tmdb.org/t/p/w185{detail.get('profile_path')}") or "",
        "profileOriginal": (detail.get("profile_path")
                            and f"https://image.tmdb.org/t/p/original{detail.get('profile_path')}") or "",
        "biography": detail.get("biography") or "",
        "birthday": detail.get("birthday") or "",
        "deathday": detail.get("deathday") or "",
        "placeOfBirth": (detail.get("place_of_birth") or ""),
        "homepage": detail.get("homepage") or "",
        "imdbId": detail.get("imdb_id") or "",
        "movies": credits["movies"],
        "tv": credits["tv"],
    }


@router.get("/browse/genres")
async def browse_genres(kind: str = Query("movie"), cfg: dict = Depends(get_config)):
    """筛选下拉: TMDB 类型列表(实时拉 TMDB, 无 key 返回空)。"""
    def _q():
        from clients.tmdb import client as tmdb
        import asyncio
        try:
            m = asyncio.run(tmdb.genre_map(cfg, kind))
        except Exception:  # noqa: BLE001
            m = {}
        return [{"id": int(k), "name": v} for k, v in sorted(m.items(), key=lambda x: x[1])]
    return await run_in_threadpool(_q)


@router.get("/browse/years")
async def browse_years(kind: str = Query("movie")):
    """筛选下拉: 本地 TMDB 缓存里实际存在的年份(该 kind), 倒序。

    前端浏览页的年份选择器用 —— 只列库里真有的年份, 选哪个就精准过滤哪个,
    不会出现"选了个库里没有的年份, 结果永远是空"。"""
    from db.models import TmdbMedia
    def _q():
        s = SessionLocal()
        try:
            rows = s.query(TmdbMedia.year).filter(
                TmdbMedia.kind == kind, TmdbMedia.year != "").distinct().all()
            return [int(r[0]) for r in rows if (r[0] or "").strip().isdigit()]
        finally:
            s.close()
    years = await run_in_threadpool(_q)
    return {"years": sorted(set(years), reverse=True)}


# 常见 US 分级的显示顺序(宽松→严格), 让下拉是 G→PG→… 而不是字符串序;
# 不在这个表里的(各国本地分级)按字符串序垫底 —— 只求稳定, 不求完美。
_US_CERT_ORDER = ["G", "PG", "PG-13", "R", "NC-17", "NR", "TV-Y", "TV-Y7",
                  "TV-G", "TV-PG", "TV-14", "TV-MA"]
_CERT_COUNTRIES = ["US", "HK", "TW", "CN", "GB", "JP", "KR", "DE", "FR",
                   "ES", "IT", "AU", "CA", "IN", "BR"]


@router.get("/browse/certs")
async def browse_certs(kind: str = Query("movie")):
    """筛选下拉: 本地 TMDB 缓存里出现过的分级(该 kind), 形如 ["US:TV-14", …]。

    零网络 —— 直接 distinct tmdb_media.certification; 一条可能含多国
    ("US:R / HK:III")→ 按 "/" 拆开去重。排序: 常用国家在前, 同国家里 US 走
    宽松→严格固定序(其余按字符串序), 截前 60 条。剧集分级就靠这个筛 ——
    TMDB discover/tv 没有分级参数, 本地缓存是唯一可靠的口径。"""
    from db.models import TmdbMedia

    def _q():
        s = SessionLocal()
        try:
            rows = s.query(TmdbMedia.certification).filter(
                TmdbMedia.kind == kind, TmdbMedia.certification != "").distinct().all()
            return [r[0] for r in rows]
        finally:
            s.close()

    seen, out = set(), []
    for raw in await run_in_threadpool(_q):
        for part in str(raw or "").split("/"):
            v = part.strip()
            if not v or ":" not in v or v in seen:
                continue
            seen.add(v)
            out.append(v)
    cc_rank = {c: i for i, c in enumerate(_CERT_COUNTRIES)}
    us_rank = {c: i for i, c in enumerate(_US_CERT_ORDER)}
    out.sort(key=lambda v: (cc_rank.get(v.split(":")[0], 99),
                            us_rank.get(v, 99), v))
    return {"certs": out[:60]}


# ---------------------------------------------------------------------------
# 缺失页(电影未拥有 + 剧集分集级缺失)
# ---------------------------------------------------------------------------
@router.get("/missing")
async def missing(kind: str = Query("tv"), offset: int = Query(0, ge=0),
                  limit: int = Query(50, ge=1, le=200),
                  cfg: dict = Depends(get_config)):
    """缺失页(**分页**): {items,total,missingSum,unknownCount,offset,limit}。

    kind=tv: 分集级缺失(精确到 SxxExx); kind=movie: 不在库的电影。
    整表按 kind 缓存 30s 再切片 —— 全量重算(tv 逐剧 2200+ 次季查询 + 逐集对比,
    实测 ~1s)在滚动加载时每页都要付一次, 体验就是"一拉就卡"。
    """
    payload = await run_in_threadpool(_missing_payload, kind)
    # 有剧还缺 TMDB 真实集号 → 后台补(不阻塞本请求; 稳态 0 请求)。
    # 没有真实集号的剧在本轮只显示"编号未同步", 不报缺/多。
    await _kick_numbers_hydration(cfg)
    return {"items": payload["items"][offset:offset + limit],
            "total": payload["total"],
            "missingSum": payload["missingSum"],
            "unknownCount": payload["unknownCount"],
            "offset": offset, "limit": limit}


# 缺失页整表缓存: kind → (stamp, payload)。滚动加载靠它切片 —— 全量重算(tv 逐剧
# 2200+ 次季查询 + 逐集对比, 实测 ~1.4s)在每一页都付一次的话, 滚动就是"一拉就卡"。
# 失效靠 stamp 而不是 TTL: 无写入时永远不重算, 一旦同步/回填/改设置立刻反映(毫秒级查询)。
_MISSING_CACHE = {}


def _missing_stamp(s, kind):
    """会改变缺失清单的全部写入的失效戳(几条聚合查询, 毫秒级)。"""
    from sqlalchemy import func
    from db.models import JfEpisode, Media, MediaType, TmdbBlocklist, TmdbMedia, TmdbSeason
    mt = MediaType.MOVIE if kind == "movie" else MediaType.TV
    tm = s.query(func.count(TmdbMedia.id), func.max(TmdbMedia.synced_at)) \
          .filter(TmdbMedia.kind == kind).one()
    se = s.query(func.count(TmdbSeason.id), func.max(TmdbSeason.synced_at)).one()
    ep = s.query(func.count(JfEpisode.id), func.max(JfEpisode.synced_at)).one()
    bl = s.query(func.count(TmdbBlocklist.id)).filter(TmdbBlocklist.kind == kind).scalar()
    md = s.query(func.count(Media.id), func.max(Media.updated_at)) \
          .filter(Media.media_type == mt).one()
    return (tm, se, ep, bl, md,
            repo.get_setting(s, "hide_complete", "false"),
            repo.get_setting(s, "check_missing_s0", "false"))


def _missing_payload(kind):
    s = SessionLocal()
    try:
        stamp = _missing_stamp(s, kind)
    finally:
        s.close()
    hit = _MISSING_CACHE.get(kind)
    if hit and hit[0] == stamp:
        return hit[1]
    payload = _build_missing(kind)
    _MISSING_CACHE[kind] = (stamp, payload)
    return payload


def _seasons_by_tmdb(session, tmdb_ids):
    """{tmdb_id: [TmdbSeason…(按季号序)]} —— 一次 IN 查询代替逐剧 N+1。

    缺失页逐剧调 repo.get_tmdb_seasons 是 2200+ 次往返(实测 ~1.4s 是这条撑起来的),
    批量 IN 只要 0.1ms。
    """
    from db.models import TmdbSeason
    ids = list(dict.fromkeys(int(t) for t in tmdb_ids if t))
    if not ids:
        return {}
    out = {}
    for row in (session.query(TmdbSeason)
                .filter(TmdbSeason.tmdb_id.in_(ids))
                .order_by(TmdbSeason.season_number)):
        out.setdefault(row.tmdb_id, []).append(row)
    return out


def _build_missing(kind):
    """缺失页全量卡片 + 汇总(原 /missing 的主体逻辑, 只是挪出来以便缓存)。"""
    def _q():
        s = SessionLocal()
        try:
            hide_complete = repo.get_setting(s, "hide_complete", "false") == "true"
            check_s0 = repo.get_setting(s, "check_missing_s0", "false") == "true"
            block_ids = repo.get_blocklist_tmdb_ids(s, kind)
            if kind == "movie":
                in_lib = _jf_in_library_ids(s, "movie")
                out = []
                for r in _tmdb_media_rows(s, "movie")[:3000]:
                    if str(r.tmdb_id) in block_ids:
                        continue
                    if str(r.tmdb_id) in in_lib:
                        continue
                    out.append(_media_card(r, False, False, False))
                out.sort(key=lambda x: (x["inProduction"], x["title"]))
                return {"items": out, "total": len(out), "missingSum": 0, "unknownCount": 0}
            # tv: 分集级
            eps_ready = _jf_episodes_ready(s)
            s_map = _series_item_to_tmdb(s)
            ep_map = _jf_episode_map(s)
            in_lib = _jf_in_library_ids(s, "tv")
            out = []
            rows = _tmdb_media_rows(s, "tv")[:3000]     # stamp 缓存, 不再每次全表物化
            # 季表一次 IN 取齐(旧: 每部剧一次查询 → 2200+ 次往返)
            seasons_map = _seasons_by_tmdb(s, [r.tmdb_id for r in rows])
            for r in rows:
                if str(r.tmdb_id) in block_ids:
                    continue
                seasons = seasons_map.get(r.tmdb_id) or []
                if not seasons:
                    continue
                in_library = str(r.tmdb_id) in in_lib
                if not eps_ready:
                    # ⚠️ 分集明细未同步: 不能逐集对比(会把在库剧全判缺), 降级为库级 ——
                    # 只列"未拥有"的剧, 在库剧标 episodesSynced=False(前端提示去同步分集)
                    if in_library:
                        continue
                    out.append(_media_card(r, False, False, False,
                                           extra={"missingCount": None, "extraCount": 0,
                                                  "haveEpisodes": 0,
                                                  "tmdbEpisodes": sum(len(_season_expected_numbers(se)) for se in seasons
                                                                      if (se.season_number != 0) or check_s0),
                                                  "seasonCount": len(seasons),
                                                  "numbersSynced": False,
                                                  "episodesSynced": False}))
                    continue
                mc, xc, per_season, have_eps, tmdb_eps, nsync = _series_missing(
                    r.tmdb_id, seasons, s_map, ep_map, check_s0)
                if mc == 0 and xc == 0 and hide_complete:
                    continue
                out.append(_media_card(r, in_library, False, mc == 0,
                                       extra={"missingCount": mc, "extraCount": xc,
                                              "haveEpisodes": have_eps,
                                              "tmdbEpisodes": tmdb_eps,
                                              "seasonCount": len(seasons),
                                              "numbersSynced": nsync,
                                              "episodesSynced": True}))
            out.sort(key=lambda x: (-(x["missingCount"] or 0), x["inLibrary"], x["title"]))
            return {"items": out, "total": len(out),
                    # 汇总给页头: 缺失 N 集 + N 部集号待同步(未同步时 missingCount 为 None)
                    "missingSum": sum((x.get("missingCount") or 0) for x in out),
                    "unknownCount": sum(1 for x in out if x.get("missingCount") is None)}
        finally:
            s.close()
    return _q()


_hydrate_task = None   # 全局: 同一时刻只跑一个后台回填任务
_HYDRATE_BATCH = 200   # 每次请求最多给多少部剧排回填(大库别一次打爆 TMDB, 多开几次补齐)


def _ids_needing_hydration():
    """在库剧里仍缺 TMDB 集号的 tmdb_id 列表(只查库, 无网络), 最多 _HYDRATE_BATCH 个。"""
    from lib import episode_numbers as epnums
    s = SessionLocal()
    try:
        ids = set()
        for tid in repo.get_series_tmdb_map(s).values():
            try:
                ids.add(int(tid))
            except (TypeError, ValueError):
                continue
        return epnums.shows_needing_hydration(ids)[:_HYDRATE_BATCH]
    finally:
        s.close()


async def _kick_numbers_hydration(cfg):
    """后台回填在库剧的集号(一次补齐; 已在跑或无待补时什么都不做)。"""
    global _hydrate_task
    if _hydrate_task is not None and not _hydrate_task.done():
        return
    ids = await run_in_threadpool(_ids_needing_hydration)
    if not ids:
        return
    from lib import episode_numbers as epnums

    async def _bg():
        try:
            await epnums.hydrate_many(cfg, ids)
        except Exception:  # noqa: BLE001
            pass

    _hydrate_task = asyncio.create_task(_bg())


@router.get("/jf-episodes-status")
async def jf_episodes_status(cfg: dict = Depends(get_config)):
    """Jellyfin 分集明细同步状态。前端据此决定: 分集未同步时提示/触发同步, 而非显示假缺失。"""
    def _q():
        s = SessionLocal()
        try:
            from db.models import JfEpisode
            return {
                "synced": _jf_episodes_ready(s),
                "episodeCount": s.query(JfEpisode.id).count(),
            }
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.get("/missing/episodes/{tmdb_id}")
async def missing_episodes_detail(tmdb_id: int, cfg: dict = Depends(get_config)):
    """某剧集逐季/逐集缺失明细(精确到 SxxExx)。

    若某季没有缓存的 episode_numbers(无法精确到集号), 会尝试用 TMDB 现拉一次并回填;
    无 TMDB key 时用 1..count 估算(并走 _series_missing 的绝对集号兜底)。
    """
    await _hydrate_episode_numbers(cfg, tmdb_id)
    def _q():
        s = SessionLocal()
        try:
            media = repo.get_tmdb_media_by_id(s, "tv", tmdb_id)
            seasons = repo.get_tmdb_seasons(s, tmdb_id)
            if not media or not seasons:
                raise HTTPException(404, "本地缓存无此剧集(请先同步 TMDB)")
            s_map = _series_item_to_tmdb(s, tmdb_id)
            ep_map = _jf_episode_map(s, list(s_map))
            check_s0 = repo.get_setting(s, "check_missing_s0", "false") == "true"
            mc, xc, per_season, have_eps, tmdb_eps, nsync = _series_missing(
                tmdb_id, seasons, s_map, ep_map, check_s0)
            return {
                "tmdbId": tmdb_id, "title": media.title,
                "inLibrary": str(tmdb_id) in _jf_in_library_ids(s, "tv"),
                "haveEpisodes": have_eps, "tmdbEpisodes": tmdb_eps,
                "missingCount": mc, "extraCount": xc,
                "numbersSynced": nsync,
                "seasons": per_season,
            }
        finally:
            s.close()
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# 屏蔽列表
# ---------------------------------------------------------------------------
@router.get("/blocklist")
async def blocklist_get(kind: str = Query(""), cfg: dict = Depends(get_config)):
    def _q():
        s = SessionLocal()
        try:
            return [{"tmdbId": r.tmdb_id, "kind": r.kind, "title": r.title,
                     "reason": r.reason,
                     "createdAt": r.created_at.isoformat() if r.created_at else ""}
                    for r in repo.get_blocklist(s, kind)]
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.post("/blocklist")
async def blocklist_add(kind: str = Query(...), tmdb_id: int = Query(...),
                        title: str = Query(""), reason: str = Query("")):
    def _q():
        s = SessionLocal()
        try:
            repo.block_tmdb(s, kind, tmdb_id, title, reason)
            s.commit()
            return {"ok": True}
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.delete("/blocklist")
async def blocklist_del(kind: str = Query(...), tmdb_id: int = Query(...)):
    def _q():
        s = SessionLocal()
        try:
            repo.unblock_tmdb(s, kind, tmdb_id)
            s.commit()
            return {"ok": True}
        finally:
            s.close()
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
@router.get("/settings")
async def settings_get(cfg: dict = Depends(get_config)):
    def _q():
        s = SessionLocal()
        try:
            return {
                "hide_complete": repo.get_setting(s, "hide_complete", "false") == "true",
                # 图片缓存: 默认开(省带宽); 关 = 封面/演员图直连 TMDB CDN
                "image_cache": repo.get_setting(s, "image_cache", "true") == "true",
                # S0 特别篇是否计入缺失检测: 默认关(只算正剧季), 开 = S00 缺集也算缺失
                "check_missing_s0": repo.get_setting(s, "check_missing_s0", "false") == "true",
            }
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.post("/settings")
async def settings_set(hide_complete: bool = Query(None),
                       image_cache: bool = Query(None),
                       check_missing_s0: bool = Query(None)):
    def _q():
        s = SessionLocal()
        try:
            if hide_complete is not None:
                repo.set_setting(s, "hide_complete", "true" if hide_complete else "false")
            if image_cache is not None:
                repo.set_setting(s, "image_cache", "true" if image_cache else "false")
            if check_missing_s0 is not None:
                repo.set_setting(s, "check_missing_s0", "true" if check_missing_s0 else "false")
            s.commit()
            return {"ok": True}
        finally:
            s.close()
    return await run_in_threadpool(_q)
