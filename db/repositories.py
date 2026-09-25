"""数据访问层: 对 db/models 的 upsert 与查询封装。

所有写操作都接收调用方传入的 SQLAlchemy session(便于在一个事务里批量提交)。
读操作供 API / 脚本查询本地库。
"""
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from db.models import (
    JellyfinItem,
    JellyfinLibrary,
    JfEpisode,
    Media,
    SeerrEpisode,
    SeerrPopular,
    SeerrRequest,
    Season,
    OrganizeLog,
    SyncLog,
    SyncState,
    TmdbBlocklist,
    TmdbIdCorrection,
    TmdbMedia,
    TmdbSeason,
    TmdbSetting,
)


# ---------------------------------------------------------------------------
# TMDB ID 校准映射(本地权威修正层, 见 TmdbIdCorrection 注释)
# ---------------------------------------------------------------------------
def get_tmdb_id_correction(session, item_id: str):
    """该 Jellyfin item 是否有校准映射; 有则返回行(含 new_tmdb_id), 无返回 None。"""
    return (session.query(TmdbIdCorrection)
            .filter(TmdbIdCorrection.item_id == str(item_id)).one_or_none())


def set_tmdb_id_correction(session, item_id: str, *, kind, old_tmdb_id,
                           new_tmdb_id, name="", year="", method="title"):
    """写入/更新一条校准映射。"""
    row = get_tmdb_id_correction(session, item_id)
    if row is None:
        row = TmdbIdCorrection(item_id=str(item_id))
        session.add(row)
    row.kind = kind
    row.old_tmdb_id = str(old_tmdb_id or "")
    row.new_tmdb_id = str(new_tmdb_id or "")
    row.name = name or ""
    row.year = year or ""
    row.method = method or "title"
    session.flush()
    return row


def drop_tmdb_id_correction(session, item_id: str):
    """撤销一条校准映射(删除行)。"""
    row = get_tmdb_id_correction(session, item_id)
    if row is not None:
        session.delete(row)
        session.flush()
    return row is not None


# ---------------------------------------------------------------------------
# Seerr 式 Media/Season 可用性(1:1 移植; 写逻辑在 scripts/sync_jf_scanner.py)
# ---------------------------------------------------------------------------
def get_media(session, tmdb_id, media_type):
    """按 (tmdb_id, media_type) 找一行(= Seerr getExisting)。带季级联加载。"""
    return (
        session.query(Media)
        .options(joinedload(Media.seasons))
        .filter_by(tmdb_id=tmdb_id, media_type=media_type)
        .one_or_none()
    )


def get_media_by_jellyfin_id(session, jellyfin_media_id):
    """按 jellyfin_media_id 反查(availability-sync 用它确认条目还在不在 Jellyfin)。"""
    return (
        session.query(Media)
        .options(joinedload(Media.seasons))
        .filter_by(jellyfin_media_id=jellyfin_media_id)
        .one_or_none()
    )


def save_media(session, media: Media):
    session.add(media)
    session.flush()
    return media


def set_season_status(session, media: Media, season_number: int, status: int):
    """取(或建)某季的 Season 行并置 status。返回 Season 行。"""
    s = next((x for x in media.seasons if x.season_number == season_number), None)
    if s is None:
        s = Season(media=media, season_number=season_number)
        session.add(s)
        media.seasons.append(s)
        session.flush()
    s.status = status
    return s


def delete_seasons(session, media: Media, season_numbers):
    """删除指定季的 Season 行。"""
    for n in season_numbers:
        for s in list(media.seasons):
            if s.season_number == n:
                session.delete(s)
                media.seasons.remove(s)
    session.flush()


def get_media_ids_available(session, media_type=""):
    """status/季级任一为 AVAILABLE/PARTIALLY_AVAILABLE 的 media 行(availability-sync 输入)。"""
    from db.models import MediaStatus
    q = session.query(Media).options(joinedload(Media.seasons))
    if media_type:
        q = q.filter_by(media_type=media_type)
    return q.all()


# ---------------------------------------------------------------------------
# Seerr
# ---------------------------------------------------------------------------
def upsert_seerr_request(session, data: dict):
    obj = session.query(SeerrRequest).filter_by(request_id=data["request_id"]).one_or_none()
    if obj is None:
        obj = SeerrRequest(request_id=data["request_id"])
        session.add(obj)
    obj.tmdb_id = data.get("tmdb_id", 0)
    obj.media_type = data.get("media_type", "tv")
    obj.title = data.get("title", "")
    obj.year = data.get("year", "")
    obj.status = data.get("status", 0)
    obj.status_text = data.get("status_text", "")
    obj.requested_by = data.get("requested_by", "")
    session.flush()
    return obj


def upsert_seerr_episode(session, data: dict):
    obj = (
        session.query(SeerrEpisode)
        .filter_by(
            request_id=data["request_id"],
            season=data.get("season", 0),
            episode=data.get("episode", 0),
        )
        .one_or_none()
    )
    if obj is None:
        obj = SeerrEpisode(
            request_id=data["request_id"],
            season=data.get("season", 0),
            episode=data.get("episode", 0),
        )
        session.add(obj)
    obj.tmdb_id = data.get("tmdb_id", 0)
    obj.series_title = data.get("series_title", "")
    obj.episode_code = data.get("episode_code", "")
    obj.status = data.get("status", 0)
    session.flush()
    return obj


def upsert_seerr_popular(session, data: dict):
    obj = (
        session.query(SeerrPopular)
        .filter_by(tmdb_id=data["tmdb_id"], kind=data["kind"])
        .one_or_none()
    )
    if obj is None:
        obj = SeerrPopular(tmdb_id=data["tmdb_id"], kind=data["kind"])
        session.add(obj)
    obj.title = data.get("title", "")
    obj.year = data.get("year", "")
    obj.overview = data.get("overview", "")
    obj.poster = data.get("poster", "")
    obj.vote = data.get("vote", 0.0)
    obj.popularity = data.get("popularity", 0.0)
    session.flush()
    return obj


# ---------------------------------------------------------------------------
# TMDB 本地缓存(作品/分集)
# ---------------------------------------------------------------------------
def upsert_tmdb_media(session, data: dict):
    """写一条 tmdb_media。

    兼容两种入参形状:
      - 扁平: {kind, tmdb_id, title, year, ...}
      - 嵌套: {kind, tmdb_id, row: {title, year, ...}, seasons: [...]}   ← sync_tmdb 用
    """
    row = data.get("row") if isinstance(data.get("row"), dict) else data
    obj = (
        session.query(TmdbMedia)
        .filter_by(tmdb_id=data["tmdb_id"], kind=data["kind"])
        .one_or_none()
    )
    if obj is None:
        obj = TmdbMedia(tmdb_id=data["tmdb_id"], kind=data["kind"])
        session.add(obj)
    for field in ("title", "original_title", "english_title", "year", "overview", "poster", "backdrop",
                  "vote", "imdb_id", "tvdb_id", "status", "in_production",
                  "original_language", "countries", "genres", "genre_names", "cast_json",
                  "directors_json", "studios_json", "keywords_json", "certification",
                  "runtime", "premiered", "end_date", "number_of_seasons"):
        if field in row:
            setattr(obj, field, row[field])
    session.flush()
    return obj


def upsert_tmdb_seasons(session, tmdb_id, seasons):
    """seasons: [{number, name, episodes|episode_count, air_date, in_production}]

    ⚠️ tmdb.all_seasons() 用的键是 ``episodes``(整数集数), 这里同时兼容 ``episode_count``,
    否则剧集每季集数会全写成 0(缺失对比直接失效)。
    """
    for s in seasons:
        obj = (
            session.query(TmdbSeason)
            .filter_by(tmdb_id=tmdb_id, season_number=s["number"])
            .one_or_none()
        )
        if obj is None:
            obj = TmdbSeason(tmdb_id=tmdb_id, season_number=s["number"])
            session.add(obj)
        obj.name = s.get("name", "")
        obj.episode_count = s.get("episode_count", s.get("episodes", 0)) or 0
        obj.air_date = s.get("air_date", "")
        obj.in_production = bool(s.get("in_production"))
    session.flush()


def get_tmdb_media(session, kind: str = "", q: str = "", limit: int = 500, offset: int = 0,
                   hide_complete: bool = False, blocked: bool = False):
    """本地 TMDB 作品缓存查询(浏览/缺失页数据源)。"""
    qry = session.query(TmdbMedia)
    if kind in ("movie", "tv"):
        qry = qry.filter_by(kind=kind)
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(TmdbMedia.title.like(like), TmdbMedia.original_title.like(like)))
    rows = qry.order_by(TmdbMedia.synced_at.desc()).offset(offset).limit(limit).all()
    if not (hide_complete or blocked):
        return rows
    block_ids = get_blocklist_tmdb_ids(session, kind) if kind in ("movie", "tv") else set()
    out = []
    for r in rows:
        if blocked:
            if str(r.tmdb_id) in block_ids:
                out.append(r)
            continue
        if str(r.tmdb_id) in block_ids:
            continue
        if hide_complete:
            out.append(r)  # 完整度由上层结合 Jellyfin 判断
    return out


def get_tmdb_media_by_id(session, kind, tmdb_id):
    return (session.query(TmdbMedia)
            .filter_by(kind=kind, tmdb_id=tmdb_id).one_or_none())


def get_tmdb_seasons(session, tmdb_id):
    return (session.query(TmdbSeason)
            .filter_by(tmdb_id=tmdb_id)
            .order_by(TmdbSeason.season_number).all())


def count_tmdb_media(session, kind: str = ""):
    qry = session.query(TmdbMedia)
    if kind in ("movie", "tv"):
        qry = qry.filter_by(kind=kind)
    return qry.count()


# ---------------------------------------------------------------------------
# 本地屏蔽列表 / 设置
# ---------------------------------------------------------------------------
def block_tmdb(session, kind, tmdb_id, title: str = "", reason: str = ""):
    obj = (session.query(TmdbBlocklist)
           .filter_by(kind=kind, tmdb_id=tmdb_id).one_or_none())
    if obj is None:
        obj = TmdbBlocklist(kind=kind, tmdb_id=tmdb_id)
        session.add(obj)
    obj.title = title
    obj.reason = reason
    session.flush()
    return obj


def unblock_tmdb(session, kind, tmdb_id):
    return session.query(TmdbBlocklist).filter_by(kind=kind, tmdb_id=tmdb_id).delete()


def get_blocklist(session, kind: str = "", limit: int = 500):
    qry = session.query(TmdbBlocklist)
    if kind in ("movie", "tv"):
        qry = qry.filter_by(kind=kind)
    return qry.order_by(TmdbBlocklist.created_at.desc()).limit(limit).all()


def get_blocklist_tmdb_ids(session, kind: str):
    if kind not in ("movie", "tv"):
        return {str(r[0]) for r in
                session.query(TmdbBlocklist.tmdb_id).distinct().all()}
    rows = session.query(TmdbBlocklist.tmdb_id).filter_by(kind=kind).all()
    return {str(r[0]) for r in rows}


def get_setting(session, key: str, default: str = ""):
    row = session.query(TmdbSetting).filter_by(key=key).one_or_none()
    return row.value if row else default


def set_setting(session, key: str, value: str):
    row = session.query(TmdbSetting).filter_by(key=key).one_or_none()
    if row is None:
        row = TmdbSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------
def upsert_jellyfin_library(session, data: dict):
    obj = session.query(JellyfinLibrary).filter_by(library_id=data["library_id"]).one_or_none()
    if obj is None:
        obj = JellyfinLibrary(library_id=data["library_id"])
        session.add(obj)
    obj.name = data.get("name", "")
    obj.type = data.get("type", "")
    obj.locations = data.get("locations", "")
    obj.item_count = data.get("item_count", 0)
    session.flush()
    return obj


def upsert_jellyfin_item(session, data: dict):
    obj = session.query(JellyfinItem).filter_by(item_id=data["item_id"]).one_or_none()
    if obj is None:
        obj = JellyfinItem(item_id=data["item_id"])
        session.add(obj)
    obj.library_id = data.get("library_id", "")
    obj.name = data.get("name", "")
    obj.type = data.get("type", "")
    obj.year = data.get("year", 0)
    obj.path = data.get("path", "")
    obj.tmdb_id = data.get("tmdb_id", "")
    obj.imdb_id = data.get("imdb_id", "")
    obj.overview = data.get("overview", "")
    obj.date_created = data.get("date_created")
    session.flush()
    return obj


# ---------------------------------------------------------------------------
# Jellyfin 分集明细(分集级缺失用)
# ---------------------------------------------------------------------------
# ⚠️ 刻意**不提供** wipe_jf_episodes / count_jf_episodes 这类"整表清空 + 体积校验"接口:
#    它们是 2026-09-22 之前"先清空再重建"模型的产物, 也正是"完整数据被洗成缺失数据"
#    的根源(一份残缺快照就能把所有剧的分集一起踩少)。分集批量写入一律走下面的
#    `replace_jf_episodes_grouped`(按剧替换, 不碰其他剧)。详见
#    .workbuddy/docs/Seerr同步模型研究.md。
def add_jf_episode(session, series_id: str, season: int, episode: int, name: str = ""):
    session.add(JfEpisode(series_id=series_id, season=season,
                          episode=episode, name=name))


def replace_jf_episodes_grouped(session, eps):
    """把一批分集按 series_id 分组, **逐剧替换**(不碰其他剧, 更不清空整表)。

    eps: [{series_id, season, episode, name}, ...](list_episodes 的输出)
    返回 (剧数, 分集数)。

    这是全项目**唯一**的分集批量写入方式。刻意不做"清空整表再重建":
    那样一份残缺列表(扫库期)就能把所有剧的分集一起踩少, 而且越同步越少。
    """
    by_series = {}
    for ep in eps:
        sid = ep.get("series_id") or ""
        if sid:
            by_series.setdefault(sid, []).append(ep)
    for sid, group in by_series.items():
        replace_jf_episodes_for_series(session, sid, group)
    return len(by_series), len(eps)


def replace_jf_episodes_for_series(session, series_id: str, eps):
    """增量替换某系列的分集: 先删该剧旧行, 再批量插入新集。供 recent 扫描即时补新剧分集。

    eps: [{series_id, season, episode, name}, ...](series_id 可省略, 用传入的 series_id)。
    """
    session.query(JfEpisode).filter_by(series_id=series_id).delete()
    for ep in eps:
        session.add(JfEpisode(
            series_id=series_id,
            season=int(ep.get("season") or 0),
            episode=int(ep.get("episode") or 0),
            name=ep.get("name") or "",
        ))


def get_jf_episodes_by_series(session, series_id):
    """[(season, episode)] 本地实有集。"""
    rows = (session.query(JfEpisode.season, JfEpisode.episode)
            .filter_by(series_id=series_id).all())
    return {(s, e) for s, e in rows}


def get_series_tmdb_map(session):
    """{series_item_id: tmdb_id_str} —— Series 项的 item_id → ProviderIds.Tmdb。"""
    rows = (session.query(JellyfinItem.item_id, JellyfinItem.tmdb_id)
            .filter(JellyfinItem.type == "Series").all())
    return {iid: str(tid) for iid, tid in rows if tid}


# ---------------------------------------------------------------------------
# SyncState(同步游标, 如 Jellyfin 增量同步的时间戳)
# ---------------------------------------------------------------------------
def get_sync_state(session, key: str):
    row = session.query(SyncState).filter_by(key=key).one_or_none()
    return row.value if row else ""


def set_sync_state(session, key: str, value: str):
    row = session.query(SyncState).filter_by(key=key).one_or_none()
    if row is None:
        row = SyncState(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    session.flush()
    return row


# ---------------------------------------------------------------------------
# ⚠️ 这里原本有 wipe_jellyfin_items(全量同步前清空 jellyfin_item) —— 已删除。
#    "先清空再重建"是 2026-09-22 之前 A 层同步的写法, 也正是「完整数据变缺失」的
#    根源: Jellyfin 扫库期返回的**残缺列表**会被当成权威, 把 6000+ 条镜像洗成几百条,
#    下游可用性对账随之把在库作品判成"未拥有"。现在 A 层同步是**纯 upsert**
#    (scripts/sync_jellyfin._sync_items), 从不删行。刻意不再提供这个接口,
#    以免有人重新引入。详见 .workbuddy/docs/Seerr同步模型研究.md。
# ---------------------------------------------------------------------------


def wipe_seerr_data(session):
    """清空 Seerr 表(全量同步前调用)。"""
    session.query(SeerrEpisode).delete()
    session.query(SeerrRequest).delete()
    session.query(SeerrPopular).delete()


# ---------------------------------------------------------------------------
# SyncLog
# ---------------------------------------------------------------------------
def log_sync_start(session, source: str, scope: str) -> SyncLog:
    log = SyncLog(source=source, scope=scope, status="running")
    session.add(log)
    session.flush()
    return log


def log_sync_end(session, log: SyncLog, status: str, items_synced: int = 0, error: str = ""):
    log.status = status
    log.items_synced = items_synced
    log.error = error
    log.finished_at = datetime.now()
    session.flush()


def log_organize_start(session, source: str = "web", scope: str = "") -> OrganizeLog:
    # ⚠️ started_at 显式用本地时间: 列默认是 func.now()(SQLite 下为 UTC), 而
    # finished_at 用 datetime.now()(本地) → 会差 8 小时, 前端「整理记录」时间显示错乱。
    log = OrganizeLog(source=source, scope=scope, status="running",
                      started_at=datetime.now())
    session.add(log)
    session.flush()
    return log


def log_organize_result(session, log: OrganizeLog, result: dict):
    """追加一条条目结果(JSON 存), 并累计 done/ok/failed/skipped。"""
    import json as _json
    try:
        results = _json.loads(log.results or "[]")
    except Exception:  # noqa: BLE001
        results = []
    results.append(result)
    log.results = _json.dumps(results, ensure_ascii=False)
    log.done = len(results)
    log.ok = sum(1 for r in results if r.get("ok"))
    log.failed = sum(1 for r in results if not r.get("ok"))
    log.skipped = sum(1 for r in results if r.get("skipped"))


def log_organize_end(session, log: OrganizeLog, status: str, error: str = "",
                     total: int = 0, trim: int = 500):
    log.status = status
    log.error = error
    if total:
        log.total = max(log.total, total)
    log.finished_at = datetime.now()
    session.flush()
    # 防膨胀: 只保留最近 trim 条(明细在 results 里, 量大时裁掉最老的)
    if trim and log.id:
        keep = session.query(OrganizeLog.id).order_by(OrganizeLog.id.desc()).limit(trim)
        session.query(OrganizeLog).filter(~OrganizeLog.id.in_(keep)).delete(synchronize_session=False)
    session.flush()


def list_organize_logs(session, limit: int = 20):
    rows = (session.query(OrganizeLog)
            .order_by(OrganizeLog.id.desc()).limit(limit).all())
    out = []
    import json as _json
    for r in rows:
        try:
            results = _json.loads(r.results or "[]")
        except Exception:  # noqa: BLE001
            results = []
        out.append({
            "id": r.id, "source": r.source, "scope": r.scope, "status": r.status,
            "total": r.total, "done": r.done, "ok": r.ok, "failed": r.failed,
            "skipped": r.skipped, "error": r.error,
            "started_at": r.started_at.isoformat() if r.started_at else "",
            "finished_at": r.finished_at.isoformat() if r.finished_at else "",
            "results": results,
        })
    return out


# ---------------------------------------------------------------------------
# 查询(供前端 / API)
# ---------------------------------------------------------------------------
def get_requests(session, q: str = "", limit: int = 200):
    qry = session.query(SeerrRequest)
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(SeerrRequest.title.like(like), SeerrRequest.year.like(like)))
    return qry.order_by(SeerrRequest.synced_at.desc()).limit(limit).all()


def get_popular(session, kind: str = "", limit: int = 200):
    qry = session.query(SeerrPopular)
    if kind:
        qry = qry.filter_by(kind=kind)
    return qry.order_by(SeerrPopular.popularity.desc()).limit(limit).all()


def get_libraries(session):
    return session.query(JellyfinLibrary).order_by(JellyfinLibrary.name).all()


def get_items(session, library_id: str = "", q: str = "", limit: int = 500):
    qry = session.query(JellyfinItem)
    if library_id:
        qry = qry.filter_by(library_id=library_id)
    if q:
        like = f"%{q}%"
        qry = qry.filter(
            or_(
                JellyfinItem.name.like(like),
                JellyfinItem.tmdb_id.like(like),
                JellyfinItem.imdb_id.like(like),
            )
        )
    return qry.order_by(JellyfinItem.name).limit(limit).all()


def get_sync_logs(session, source: str = "", limit: int = 50):
    qry = session.query(SyncLog)
    if source:
        qry = qry.filter_by(source=source)
    return qry.order_by(SyncLog.started_at.desc()).limit(limit).all()


# ---------------------------------------------------------------------------
# 热门榜标注用: 已有库 / 已有请求 的 tmdb id 集合
# ---------------------------------------------------------------------------
_KIND_TO_JF_TYPE = {"movie": "Movie", "tv": "Series"}


def get_tmdb_ids_in_library(session, kind: str):
    """本地 Jellyfin 库里已有的 tmdb id 集合(kind='movie'|'tv')。"""
    jf_type = _KIND_TO_JF_TYPE.get(kind)
    if not jf_type:
        return set()
    rows = session.query(JellyfinItem.tmdb_id).filter(
        JellyfinItem.type == jf_type, JellyfinItem.tmdb_id != "").distinct().all()
    return {str(r[0]) for r in rows if r[0]}


def get_pending_request_tmdb_ids(session, kind: str):
    """Seerr 请求队列里还没下完的 tmdb id 集合(kind='movie'|'tv')。

    status 4=已就绪 / 3=已拒绝 不算"请求中"。
    """
    mt = kind if kind in ("movie", "tv") else ""
    rows = session.query(SeerrRequest.tmdb_id).filter(
        SeerrRequest.media_type == mt,
        SeerrRequest.status.notin_((4, 3)),
        SeerrRequest.tmdb_id != 0,
    ).distinct().all()
    return {str(r[0]) for r in rows if r[0]}


# ---------------------------------------------------------------------------
# 推送标记(避免重复推 CD2 / Qbit)
# ---------------------------------------------------------------------------
def get_push_states(session, hashes):
    """批量查推送标记。返回 {info_hash(小写): {"cd2": bool, "qbit": bool}}。

    合并 state/queue.json 历史: 在本表建立之前已经推到 CD2 的任务(info_hash 在队列里),
    也视为已推 CD2 —— 否则老用户的历史推送会显得"没标记"。
    """
    from db.models import PushRecord

    lower = [h.lower() for h in hashes if h]
    out = {}
    if lower:
        rows = session.query(PushRecord).filter(PushRecord.info_hash.in_(lower)).all()
        for r in rows:
            out[r.info_hash.lower()] = {
                "cd2": bool(r.pushed_cd2),
                "qbit": bool(r.pushed_qbit),
            }
    # 合并 queue.json 历史 CD2 命中(队列里存在的任务 = 已成功推到 CD2)
    try:
        from server.config import CONFIG_PATH
        from lib import state
        for t in state.load_queue(str(CONFIG_PATH)):
            h = (t.get("info_hash") or "").lower()
            if not h:
                continue
            cur = out.get(h, {"cd2": False, "qbit": False})
            cur["cd2"] = True
            out[h] = cur
    except Exception:  # noqa: BLE001  队列读不到不影响主流程
        pass
    return out


def mark_pushed(session, info_hash, magnet: str = "", title: str = "", target: str = "cd2"):
    """标记某磁力已推送到 cd2 或 qbit。info_hash 解析失败(None)时直接跳过(无法定位)。

    target: "cd2" | "qbit"。两个目标独立置位(先推 CD2、后推 Qbit 互不覆盖)。
    返回 PushRecord 行(已 flush, 需调用方 commit), 或 None(无 hash)。
    """
    if not info_hash:
        return None
    from db.models import PushRecord
    h = info_hash.lower()
    row = session.query(PushRecord).filter_by(info_hash=h).one_or_none()
    if row is None:
        row = PushRecord(info_hash=h, magnet=magnet, title=title)
        session.add(row)
    else:
        if magnet and not row.magnet:
            row.magnet = magnet
        if title and not row.title:
            row.title = title
    if target == "qbit":
        row.pushed_qbit = True
        row.qbit_at = datetime.now()
    else:
        row.pushed_cd2 = True
        row.cd2_at = datetime.now()
    session.flush()
    return row
