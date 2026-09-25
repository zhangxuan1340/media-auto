#!/usr/bin/env python3
"""
media-auto / sync_jf_scanner —— Jellyfin 扫描器
================================================================
「Jellyfin 扫描 → 推导可用性 → 写 Media/Season」引擎,
写本地 SQLite 的 `media` / `season` 表(可用性镜像, 见 db/models.py)。

核心不变量(这是"一个作品只有一行"的根本保证):
  1. **必须有 TMDB ID 才写库** —— 取 ProviderIds.Tmdb, 没有就 IMDb→TMDB 反查,
     再没有就 **跳过(抛 NoTmdbIdError)**。绝不为
     无 TMDB ID 的条目写空壳(这正是 MediaAuto 当年 /pull 幽灵 bug 的反面)。
  2. **按 (tmdb_id, media_type) 找行** —— getExisting(); 有则更新状态, 无则新建。
     并发写同一 ID 用 per-id 锁。
  3. **只在扫描时写, 从不在用户查看时写**。
  4. 季状态由「Jellyfin 实有集(jf_episode) vs TMDB 应有集(all_seasons)」逐季推导,
     作品状态由季状态 rollup。

状态枚举 MediaStatus 数字值见 db/models.py。
4K 分支: MediaAuto 无 4K 库拆分, status_4k 镜像 status(单库假设, 已注明)。

⚠️ **写入模型(2026-09-22 定稿)** —— 本模块**从不删除任何行**:
  · 分集(jf_episode)只做**按剧替换**: 刷新"这份快照覆盖到的剧", 未覆盖的原样保留,
    **不再有"清空整表再重建"**(旧写法一次能把所有剧的分集一起踩少);
  · 增量(recent)是**窗口式重读**(最新 N 条), **没有游标** —— 写入幂等, 重复处理
    零代价, 也就没有"游标越过 → 永久漏"这种失败模式;
  · 「某作品/某季是否已从库里消失」本模块**不判**(它只做"入库置 AVAILABLE"),
    那是 scripts/availability_sync.py 的专职, 且走 fail-safe 单查。

用法:
  python3 scripts/sync_jf_scanner.py --mode full      # 全量扫描(重推所有可用性)
  python3 scripts/sync_jf_scanner.py --mode recent    # 增量(窗口式重读最新条目/分集)
  python3 scripts/sync_jf_scanner.py --mode recent --limit 50   # 调试
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.jellyfin import client as jf
from clients.tmdb import client as tmdb
from db.database import SessionLocal, init_db
from db import repositories as repo
from db.models import JellyfinItem, JfEpisode, Media, MediaStatus, MediaType, Season

# 增量窗口大小: 每轮重读"最新 N 条"(/Items/Latest?Limit=12)。
# 没有游标 —— 见模块 docstring 的写入模型说明。
RECENT_ITEM_WINDOW = 300   # 最新 N 条条目(Movie/Series)
RECENT_EP_WINDOW = 300     # 最新 N 条分集 → 反推涉及的剧(发现"分集后补齐"的半成品)

# 并发: 网络拉 TMDB 季结构(限速 ~10/s, 5 并发安全); 写库串行(单 session 线程)
TMDB_CONCURRENCY = 5
# 未定状态剧"兜底重算"的入库新鲜度门限(天): 只有"有实有集"或"刚入库"的才重算
REPAIR_FRESH_DAYS = 1
# 兜底重算每轮上限(防御: 万一本地 media 被清空, 也不至于一轮里全量重推)
REPAIR_MAX = 50
# 4K 开关(MediaAuto 无 4K 拆分, 恒 False; 保留位以备日后 4K 分支)
ENABLE_4K = False


# ---------------------------------------------------------------------------
# 并发锁(per-id): 同一 tmdb_id 串行处理
# ---------------------------------------------------------------------------
class _IdLock:
    def __init__(self):
        self._locks = {}

    async def dispatch(self, tmdb_id, fn):
        lock = self._locks.get(tmdb_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tmdb_id] = lock
        async with lock:
            return await fn()


# ---------------------------------------------------------------------------
# extract_ids() —— 从条目里提取 TMDB/IMDb ID
# ---------------------------------------------------------------------------
def _correction_tmdb_id(session, item):
    """查本地 TMDB ID 校准映射(scripts/calibrate_jf_ids --audit 写入)。
    有则返回正确 tmdb_id(str), 无则 None。
    这是**本地权威修正层**: Jellyfin 侧挂错 TMDB ID 且 IMDb 反查也帮不上忙时,
    靠片名搜索确认的映射在此生效, 使修正在每次全量/增量扫描后不回退。"""
    item_id = str(item.get("item_id") or "").strip()
    if not item_id:
        return None
    row = repo.get_tmdb_id_correction(session, item_id)
    return str(row.new_tmdb_id) if (row and row.new_tmdb_id) else None


async def extract_ids(cfg, item, id_lock, sem, session=None):
    """从 Jellyfin 条目解析权威 TMDB ID。

    顺序: 本地校准映射(最高优先) → ProviderIds.Tmdb → (IMDb → TMDB /find 反查)
    → 都没有则抛 NoTmdbIdError(调用方捕获后跳过)。
    返回 (tmdb_id:int, imdb_id:str)。
    """
    # 本地校准映射优先(修 Jellyfin 挂错且 IMDb 反查无效的情况)
    if session is not None:
        corrected = _correction_tmdb_id(session, item)
        if corrected:
            return int(corrected), str(item.get("imdb_id") or "").strip()
    tmdb_id = str(item.get("tmdb_id") or "").strip()
    imdb_id = str(item.get("imdb_id") or "").strip()

    if not tmdb_id.isdigit():
        # 有 IMDb 但没有 TMDB → 走 IMDb→TMDB 反查
        if imdb_id.startswith("tt"):
            from scripts.calibrate_jf_ids import _find_by_imdb
            found, found_type = await _find_by_imdb(cfg, imdb_id, sem)
            if found:
                return int(found), imdb_id
        raise NoTmdbIdError(f"无 TMDB ID(item {item.get('item_id')} {item.get('name')})")
    return int(tmdb_id), imdb_id


class NoTmdbIdError(Exception):
    """拿不到 TMDB ID 时抛出(调用方捕获后跳过该条目)。"""


# ---------------------------------------------------------------------------
# 实有集来源: jf_episode(本地全量分集, 由 sync_jellyfin --scope episodes 维护)
# ---------------------------------------------------------------------------
def _load_episode_index(session):
    """返回 (series_to_tmdb, tmdb_to_series_ids, eps_by_series)。
    eps_by_series: {series_item_id: {season: set(episode)}}。"""
    series_to_tmdb = {}
    for iid, tid in session.query(JellyfinItem.item_id, JellyfinItem.tmdb_id).filter(
            JellyfinItem.type == "Series").all():
        if tid:
            series_to_tmdb[iid] = str(tid)
    tmdb_to_series = {}
    for iid, tid in series_to_tmdb.items():
        tmdb_to_series.setdefault(tid, set()).add(iid)
    eps_by_series = {}
    for sid, s, e in session.query(JfEpisode.series_id, JfEpisode.season,
                                   JfEpisode.episode).all():
        eps_by_series.setdefault(sid, {}).setdefault(s, set()).add(e)
    return series_to_tmdb, tmdb_to_series, eps_by_series


def _unsettled_item_ids(session, tmdb_to_series, eps_by_series):
    """"值得当轮重算"的条目 → Jellyfin item_id 列表(剧与电影都有)。

    这些条目属于"明明在库里, 可用性却推导不出来 / 本地干脆查无此剧":
      a) 剧状态卡在 UNKNOWN/PROCESSING —— 条目/分集入库先后错位、TMDB 暂时性失败、
         A 层镜像曾缺行等。一旦算成 AVAILABLE / PARTIALLY_AVAILABLE 就自动离开本集合,
         所以每轮规模很小且**自我收敛**。
      b) A 层有这部剧、但 media 里连行都没有 —— 说明它**从未被任何一次扫描推导过**。
         典型成因: 入库时 DateCreated 非单调(时间戳偏早), 于是它排在 recent 的
         "最新 N 条"窗口之外, 分集又不算"新"(北辙南辕就是这类)。不补的话本地永远
         显示"不在库"。
      c) 同 b, 但针对**电影**: 电影没有"分集"这个第二信号可以兜底, 一旦 DateCreated
         偏早落出窗口, recent 就看不到它, 只能等次日 03:00 全量扫描 —— 用户视角就是
         "刚入库的电影本地库一整天才出现"。这里从 A 层反向兜底, 让当轮就进本地库。

    只挑值得重算的, 避免无限空转: 本地已有实有集, 或刚入库(REPAIR_FRESH_DAYS 天内)。
    反之入库很久又一条分集都没有的"幽灵剧"(空目录 / TMDB ID 挂错)重推也推不出来,
    不再反复重算 —— 那类要人看。
    """
    fresh_after = datetime.now() - timedelta(days=REPAIR_FRESH_DAYS)
    out = []
    rows = (session.query(Media.tmdb_id, Media.media_added_at)
            .filter(Media.media_type == MediaType.TV,
                    Media.status.in_((MediaStatus.UNKNOWN, MediaStatus.PROCESSING)))
            .all())
    for tid, added in rows:
        if not tid:
            continue
        ids = tmdb_to_series.get(str(tid), set())
        has_eps = any(eps_by_series.get(sid) for sid in ids)
        added_naive = added.replace(tzinfo=None) if getattr(added, "tzinfo", None) else added
        fresh = added_naive is None or added_naive >= fresh_after
        if has_eps or fresh:
            out.extend(ids)
    # b) 从未建过 media 行的在库剧(见 docstring)
    known = {str(t) for (t,) in session.query(Media.tmdb_id)
             .filter(Media.media_type == MediaType.TV).all() if t}
    for tid, sids in tmdb_to_series.items():
        if tid and tid not in known:
            out.extend(sids)
    # c) 从未建过 media 行的在库电影(见 docstring)
    known_movies = {str(t) for (t,) in session.query(Media.tmdb_id)
                    .filter(Media.media_type == MediaType.MOVIE).all() if t}
    mv_rows = (session.query(JellyfinItem.item_id, JellyfinItem.tmdb_id)
               .filter(JellyfinItem.type == "Movie").all())
    for iid, tid in mv_rows:
        tid = str(tid or "")
        if tid and tid not in known_movies:
            out.append(str(iid))
    return list(dict.fromkeys(out))[:REPAIR_MAX]


def _items_from_db(session, item_ids):
    """从 A 层(jellyfin_item)重建条目 dict。

    供 recent 的 settle 检查使用: 某部剧这一轮"有新分集入库", 但它的 Series 项不在
    本批 items 里(条目级游标早翻过去了) —— 这时拿它的 item_id 从 A 层取条目信息,
    重跑一次可用性推导, 把新补的分集算进去。
    字段名与 clients.jellyfin.client._norm_items 对齐(下游 extract_ids 依赖)。
    """
    ids = [i for i in dict.fromkeys(item_ids or []) if i]
    if not ids:
        return []
    out = []
    for row in session.query(JellyfinItem).filter(JellyfinItem.item_id.in_(ids)).all():
        out.append({
            "item_id": row.item_id,
            "library_id": row.library_id or "",
            "name": row.name or "",
            "type": row.type or "",
            "year": row.year or 0,
            "path": row.path or "",
            "tmdb_id": row.tmdb_id or "",
            "imdb_id": row.imdb_id or "",
            "overview": row.overview or "",
            "date_created": row.date_created,
        })
    return out


# ---------------------------------------------------------------------------
# process_movie() —— 电影可用性写入
# ---------------------------------------------------------------------------
def _process_movie(session, tmdb_id, *, imdb_id="", jf_media_id="",
                   media_added_at=None, title="", year=""):
    """写/更新一部电影的可用性(非 4K 分支; hasFile=True, processing=False)。"""
    media = repo.get_media(session, tmdb_id, MediaType.MOVIE)
    if media is None:
        media = Media(tmdb_id=tmdb_id, media_type=MediaType.MOVIE,
                      imdb_id=imdb_id or None, status=MediaStatus.AVAILABLE,
                      status_4k=MediaStatus.AVAILABLE if ENABLE_4K else MediaStatus.UNKNOWN,
                      title=title, year=year)
        if jf_media_id:
            media.jellyfin_media_id = jf_media_id
        if media_added_at:
            media.media_added_at = media_added_at
        repo.save_media(session, media)
    else:
        changed = False
        # 已有行 status 非 AVAILABLE → 置 AVAILABLE(在库即得)
        if media.status != MediaStatus.AVAILABLE:
            media.status = MediaStatus.AVAILABLE
            changed = True
        if ENABLE_4K and media.status_4k != MediaStatus.AVAILABLE:
            media.status_4k = MediaStatus.AVAILABLE
            changed = True
        if jf_media_id and media.jellyfin_media_id != jf_media_id:
            media.jellyfin_media_id = jf_media_id
            changed = True
        if imdb_id and not media.imdb_id:
            media.imdb_id = imdb_id
            changed = True
        if media_added_at and not media.media_added_at:
            media.media_added_at = media_added_at
            changed = True
        if title and media.title != title:
            media.title = title
            changed = True
        if year and media.year != year:
            media.year = year
            changed = True
        if changed:
            repo.save_media(session, media)
    return media


# ---------------------------------------------------------------------------
# 季状态推导(季循环 + rollup, 非 4K 分支)
# ---------------------------------------------------------------------------
def _season_status(total_episodes, episodes, existing_status, processing=False):
    """单季状态: 全齐=AVAILABLE; 部分=PARTIALLY_AVAILABLE; 否则保持/UNKNOWN。"""
    complete = total_episodes > 0 and total_episodes == episodes
    if complete or (existing_status == MediaStatus.AVAILABLE):
        return MediaStatus.AVAILABLE
    if episodes > 0:
        return MediaStatus.PARTIALLY_AVAILABLE
    if processing and existing_status != MediaStatus.DELETED:
        return MediaStatus.PROCESSING
    if (not processing) and episodes == 0 and existing_status == MediaStatus.PROCESSING:
        return MediaStatus.UNKNOWN
    return existing_status if existing_status else MediaStatus.UNKNOWN


def _show_rollup(seasons, prev_status):
    """作品级 rollup(非 4K, nonSpecial, countsTowardsRollup)。
    seasons: [(season_number, status, total_episodes)](含 Specials)。"""
    non_special = [s for s in seasons if s[0] != 0]
    # countsTowardsRollup: 该季被扫描到(total_episodes>0) 或 状态非 UNKNOWN
    rollup = []
    for num, status, total in seasons:
        if num == 0:
            continue
        counts = (total > 0) or (status != MediaStatus.UNKNOWN)
        if counts:
            rollup.append(status)
    if rollup and all(s == MediaStatus.AVAILABLE for s in rollup):
        return MediaStatus.AVAILABLE
    if any(s in (MediaStatus.PARTIALLY_AVAILABLE, MediaStatus.AVAILABLE) for s in [s[1] for s in seasons]):
        return MediaStatus.PARTIALLY_AVAILABLE
    if (not seasons and prev_status != MediaStatus.DELETED) or \
            any(s == MediaStatus.PROCESSING for s in [x[1] for x in seasons]):
        return MediaStatus.PROCESSING
    if prev_status == MediaStatus.DELETED:
        return MediaStatus.DELETED
    return MediaStatus.UNKNOWN


def _process_show(session, tmdb_id, *, tvdb_id=None, imdb_id="", jf_media_id="",
                  media_added_at=None, seasons=None, eps_by_series=None,
                  series_ids=None, title="", year=""):
    """写/更新一部剧的可用性(非 4K)。

    seasons: TMDB 应有季 [{number, episodes(=episode_count)}]。
    eps_by_series / series_ids: 用来算每季 Jellyfin 实有集数。
    """
    seasons = seasons or []
    eps_by_series = eps_by_series or {}
    series_ids = series_ids or set()
    # 每季实有集数(合并该剧所有 series_id)
    actual = {}
    for sid in series_ids:
        for s, eps in eps_by_series.get(sid, {}).items():
            actual.setdefault(s, set()).update(eps)

    media = repo.get_media(session, tmdb_id, MediaType.TV)
    new_seasons = []
    if media is None:
        media = Media(tmdb_id=tmdb_id, media_type=MediaType.TV,
                      tvdb_id=tvdb_id, imdb_id=imdb_id or None,
                      status=MediaStatus.UNKNOWN, status_4k=MediaStatus.UNKNOWN,
                      title=title, year=year)
        repo.save_media(session, media)
    else:
        if title and media.title != title:
            media.title = title
        if year and media.year != year:
            media.year = year
    prev_status = media.status

    # 季循环: 逐季定状态
    for tseason in seasons:
        num = tseason["number"]
        total = tseason.get("episodes") or tseason.get("episode_count") or 0
        # totalEpisodes>0 才算; 否则季不可用(0 集)
        episodes = len(actual.get(num, set())) if total > 0 else 0
        existing_season = next((x for x in media.seasons
                                if x.season_number == num), None)
        if existing_season is not None:
            existing_season.status = _season_status(
                total, episodes, existing_season.status)
            if ENABLE_4K:
                existing_season.status_4k = _season_status(
                    total, episodes, existing_season.status_4k)
        else:
            new_seasons.append(Season(
                media=media, season_number=num,
                status=_season_status(total, episodes, MediaStatus.UNKNOWN),
                status_4k=(_season_status(total, episodes, MediaStatus.UNKNOWN)
                           if ENABLE_4K else MediaStatus.UNKNOWN),
            ))
    # 只有有实有集的季才把 jellyfinMediaId 记上
    if jf_media_id and any(
            (t.get("episodes") or t.get("episode_count") or 0) > 0 and
            len(actual.get(t["number"], set())) > 0 for t in seasons):
        media.jellyfin_media_id = jf_media_id
    if media_added_at and not media.media_added_at:
        media.media_added_at = media_added_at

    # rollup(作品级)
    all_seasons_now = [(s.season_number, s.status,
                        next((t.get("episodes") or t.get("episode_count") or 0
                              for t in seasons if t["number"] == s.season_number), 0))
                       for s in media.seasons]
    media.status = _show_rollup(all_seasons_now, prev_status)
    if ENABLE_4K:
        media.status_4k = _show_rollup(
            [(s.season_number, s.status_4k,
              next((t.get("episodes") or t.get("episode_count") or 0
                    for t in seasons if t["number"] == s.season_number), 0))
             for s in media.seasons],
            MediaStatus.UNKNOWN)

    # ── 所有权兜底 ──────────────────────────────────────────────────────────
    # 只要 Jellyfin 实有 ≥1 集, 该剧即"在库", 绝不能被判为 UNKNOWN(1) /
    # PROCESSING(3) 而落入"缺失 / 未拥有"列表。典型触发场景:
    #   • TMDB /3/tv/{id} 404(剧的 tmdb_id 在 TMDB 无 TV 记录, 季结构缺失)
    #   • Jellyfin 把集归到 Specials(季 0)或异季号, 逐季比对落空, media.seasons
    #     为空或全 UNKNOWN。
    # 这些情况下我们仍知道剧在库且有集, 保守置 PARTIALLY_AVAILABLE(4):
    # 既退出"缺失"判定, 又不谎报"完整"(无 TMDB 季基准无法确认齐全度)。
    total_actual = sum(len(v) for v in actual.values())
    if total_actual > 0 and media.status in (MediaStatus.UNKNOWN,
                                             MediaStatus.PROCESSING):
        # 完整性可确认(TMDB 有季基准) → 明确"不完整"(PARTIALLY_AVAILABLE=4);
        # 不可确认(TMDB 404/无季结构/季号错位) → "库内·完整性未知"(OWNED_UNVERIFIED=8),
        # 既不谎报完整也不错报缺失。
        new_status = (MediaStatus.PARTIALLY_AVAILABLE if seasons
                      else MediaStatus.OWNED_UNVERIFIED)
        media.status = new_status
        if ENABLE_4K:
            media.status_4k = new_status

    repo.save_media(session, media)
    return media


# ---------------------------------------------------------------------------
# run_scan() —— 扫描主流程
# ---------------------------------------------------------------------------
async def _process_item(session, cfg, item, sem, id_lock, eps_index):
    """处理一个 Jellyfin 条目(Movie→process_movie / Series→process_show)。"""
    typ = item.get("type")
    added_at = item.get("date_created")
    jf_media_id = str(item.get("item_id") or "")
    title = item.get("name") or ""
    year = str(item.get("year") or "")
    series_to_tmdb, tmdb_to_series, eps_by_series = eps_index
    try:
        tmdb_id, imdb_id = await extract_ids(cfg, item, id_lock, sem, session=session)
    except NoTmdbIdError:
        return "skipped_no_tmdb"

    if typ == "Movie":
        async def _w():
            return _process_movie(session, tmdb_id, imdb_id=imdb_id,
                                  jf_media_id=jf_media_id,
                                  media_added_at=added_at,
                                  title=title, year=year)
        await id_lock.dispatch(tmdb_id, _w)
        return "movie"

    if typ == "Series":
        # 应有季: TMDB all_seasons(带并发+重试; 失败 → 无季信息, 仍记作品级在库)
        tseasons = None
        async def _fetch_seasons():
            nonlocal tseasons
            for _ in range(2):
                try:
                    tseasons = await tmdb.all_seasons(cfg, tmdb_id) or []
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(1.0)
            return tseasons
        await id_lock.dispatch(tmdb_id, _fetch_seasons)
        tseasons = tseasons or []
        # 每季实有集: 由 jf_episode 按 series_id 聚合。
        # ⚠️ 必须把"正在处理的这个 Series 项自身的 item_id"并进来: 它本身就是
        #    jf_episode 里的 series_id(分集返回的 SeriesId == Series 项的 Id)。
        #    只靠 A 层(jellyfin_item)反查映射的话, 一旦 A 层缺行(漏同步/游标跳过),
        #    sids 为空 → 实有集全算成 0 → 状态落 UNKNOWN(1)、jellyfin_media_id
        #    也写不上, 明明在库却显示"未拥有"。
        sids = set(tmdb_to_series.get(str(tmdb_id), set()))
        if jf_media_id:
            sids.add(jf_media_id)
        async def _w2():
            return _process_show(session, tmdb_id, imdb_id=imdb_id,
                                 jf_media_id=jf_media_id,
                                 media_added_at=added_at, seasons=tseasons,
                                 eps_by_series=eps_by_series, series_ids=sids,
                                 title=title, year=year)
        await id_lock.dispatch(tmdb_id, _w2)
        return "show"
    return "skip_type"


async def _refresh_all_episodes(cfg, session):
    """全量刷新 jf_episode: 拉全部分集后 **按剧差量替换**(不清空整表)。

    与 `sync_jellyfin --scope episodes` 等价。返回分集条数。

    ⚠️ 与旧写法的根本差别 —— 这里**没有**任何"快照完整性"闸门, 也**不做**
    `wipe_jf_episodes`:
      · 不做整表清空 ⇒ 一份残缺列表再也不可能"把所有剧一起踩少";
      · 只替换"这份快照覆盖到的剧" ⇒ 影响面局限在那些剧, 且下次扫描会正常刷新;
      · 闸门存在的唯一理由(要 wipe)已经不存在, 所以闸门也一起删掉了。
    """
    eps = await jf.list_episodes(cfg)
    _n_series, n_eps = repo.replace_jf_episodes_grouped(session, eps)
    session.commit()
    return n_eps


async def _refresh_episodes_for_series(cfg, session, series_ids):
    """增量补分集: 只刷新给定系列(新入库的剧 / 本轮有新分集的剧), 不碰其他系列。

    **逐系列容错(重要)**: 单个系列拉取失败只跳过该系列并记数, 绝不让它中断整轮
    扫描 —— 典型失败:`/Shows/{id}/Episodes` 对**已从 Jellyfin 移除**的剧返回 404
    (A 层镜像里的陈旧行会走到这里)。一个 404 不该把整轮 5 分钟增量废掉。
    (逐条目 try/catch。)

    ⚠️ 这里**不再**判断"这份分集列表是不是残缺"(旧的五道防线 + 可疑队列已删除):
    既然写入是"只替换这一部剧"、不含任何整表清空, 一份残缺列表的最坏后果是
    **这一部剧**在下一轮刷新前少显示几集 —— 而窗口式增量每 5 分钟就重读最近一段、
    每日全量再覆盖全部, 它自己会收敛。
    用闸门去防"少写几条"不划算; 真正必须防的"删掉完整数据"已经由写入模型
    (纯 upsert / 按剧替换, 永不清表)从根上消除了。

    返回 (成功系列数, 失败系列数)。
    """
    ok = fail = 0
    for sid in series_ids:
        try:
            eps = await jf.list_episodes_for_series(cfg, sid)
        except Exception:  # noqa: BLE001
            fail += 1
            continue
        repo.replace_jf_episodes_for_series(session, sid, eps)
        ok += 1
    session.commit()
    return ok, fail


async def _run_async(cfg, mode="full", limit=0):
    """扫描主体。mode=full(全量) | recent(增量, 窗口式重读)。

    分集明细(jf_episode)随可用性扫描一起刷新, 不再单独跑:
      - recent: 窗口内新入库的剧 + 窗口内"有新分集的剧" → 即时补分集(按剧替换);
      - full:   全量刷新分集(日扫 03:00 兜底, 覆盖老剧新增集)。
    ⚠️ 两条路都不做整表清空、不设完整性闸门、不推游标 —— 见模块 docstring 的写入模型。
       扫库期因此也不再需要特殊处理(旧写法才需要靠闸门/跳过重建来避免"一次踩少全部")。
    """
    init_db()
    session = SessionLocal()
    id_lock = _IdLock()
    sem = asyncio.Semaphore(TMDB_CONCURRENCY)
    stats = {"movie": 0, "show": 0, "skipped_no_tmdb": 0, "skip_type": 0,
             "fail": 0, "settle": 0, "repair": 0, "settle_no_item": 0,
             "ep_ok": 0, "ep_fail": 0, "ep_full": 0}
    try:
        if mode == "recent":
            # 窗口式增量: 重读"最新 N 条"条目 + "最新 N 条"分集涉及的剧。
            # ⚠️ 没有游标: 写入是幂等的 upsert/按剧替换, 重复处理零代价, 因此不需要
            #    "上次同步到哪"这种状态, 也就没有"游标越过 → 某条永久漏掉"的失败模式。
            items = await jf.list_recent_items(cfg, limit=RECENT_ITEM_WINDOW)
            fetched_ids = {it.get("item_id") for it in items}

            # ── 分集侧窗口(settle 检查) ─────────────────────────────────────
            # 剧的"入库时间"是 Series 项的 DateCreated, 之后才搬进来的分集不会改变
            # 它 —— 只靠条目窗口看不到"先建了剧、分集后来才补齐"的半成品
            # (Series 项的时间戳很早, 排在窗口之外, 结果是实有集偏少 / 状态卡 UNKNOWN)。
            # 从分集自己的 DateCreated 侧再取一个窗口, 反推出涉及哪些剧。
            ep_series = await jf.list_recent_episode_series(cfg, limit=RECENT_EP_WINDOW)

            # 需要重拉分集的剧 = 窗口内的新剧 ∪ 窗口内有新分集的剧
            touch = [it.get("item_id") for it in items if it.get("type") == "Series"]
            touch = [s for s in dict.fromkeys(touch + ep_series) if s]
            if touch:
                stats["ep_ok"], stats["ep_fail"] = \
                    await _refresh_episodes_for_series(cfg, session, touch)

            # 实有集索引: 必须在上面补完分集之后再加载, 本轮推导才用得到新数据
            eps_index = _load_episode_index(session)
            _, tmdb_to_series, eps_by_series = eps_index

            # 需要补条目一起重算的剧:
            #   a) 有新分集、但不属于本批条目(settle)
            #   b) 状态仍未定的剧(repair) —— 兜底收口"在库却推不出可用性"
            ep_extra = [sid for sid in ep_series if sid not in fetched_ids]
            repair_extra = [sid for sid in _unsettled_item_ids(session, tmdb_to_series,
                                                               eps_by_series)
                            if sid not in fetched_ids and sid not in ep_extra]
            stats["settle"] = len(ep_extra)
            stats["repair"] = len(repair_extra)
            need = ep_extra + repair_extra
            if need:
                rows = _items_from_db(session, need)
                # 分集在、条目信息却拿不到(典型: A 层镜像还没补上这一行) → 记数便于
                # 诊断, 不静默吞掉。下一轮 A 层补齐后(every-5min 全库比对)即可重算。
                stats["settle_no_item"] = len(need) - len(rows)
                items = items + rows

            session.commit()
        else:
            items = await jf.list_items(cfg)
            # 全量扫描: 顺带全量刷新分集明细(兜底老剧新增集)。
            # ⚠️ 不做整表清空 ⇒ 扫库期也不再需要"跳过重建"了(少写几条无关紧要,
            #    下一轮/明天就补上; 而"一次踩少全部"这个风险已经不存在)。
            stats["ep_full"] = await _refresh_all_episodes(cfg, session)
            eps_index = _load_episode_index(session)

        if limit:
            items = items[:limit]

        for it in items:
            try:
                r = await _process_item(session, cfg, it, sem, id_lock, eps_index)
                stats[r] = stats.get(r, 0) + 1
            except Exception:  # noqa: BLE001
                stats["fail"] += 1
            session.commit()  # 逐条提交, 中断可恢复
        return stats
    finally:
        session.close()


def run(mode="full", limit=0):
    """同步入口(供 CLI 与 server 复用)。返回统计 dict。

    持全局同步锁: 本任务会写 `media` / `season` / `jellyfin_item`, 不串行化的话
    与"条目同步(items)" / "可用性对账" 并发跑会互相覆盖状态并争 SQLite 写锁。
    可重入, 调度器把"条目同步 + 本扫描"当成一个整体持锁时不会自锁。
    """
    from lib.sync_guard import SYNC_LOCK
    cfg = load_config()
    if not cfg.get("jellyfin", {}).get("url"):
        raise RuntimeError("config.json 未配置 jellyfin.url / jellyfin.token")
    with SYNC_LOCK:                      # 见 lib/sync_guard.py
        return asyncio.run(_run_async(cfg, mode=mode, limit=limit))


def main():
    ap = argparse.ArgumentParser(description="Jellyfin 可用性扫描")
    ap.add_argument("--mode", choices=["full", "recent"], default="full")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    try:
        stats = run(args.mode, limit=args.limit)
        print(f"扫描完成({args.mode}): {stats}")
    except Exception as e:  # noqa: BLE001
        print(f"扫描失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
