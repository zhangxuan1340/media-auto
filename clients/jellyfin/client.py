"""Jellyfin 客户端(异步, 基于 httpx)

封装 Jellyfin REST API:
  - 媒体库列表: GET /Library/VirtualFolders
  - 媒体项列表: GET /Items  (ParentId + Recursive + 指定字段)

鉴权用标准 Emby/Jellyfin 头 `Authorization: MediaBrowser Token="<api_key>"`。

⚠️ 别再用 `X-Emby-Token`: Jellyfin 12.x 已不接受它(一律返回 401)。
  实测 12.0.0: `/System/Info` 带 X-Emby-Token -> 401,带 Authorization -> 200。
  老式 `X-Emby-Token` 在 10.x 上也已标记弃用,统一用 Authorization 对两个版本都通。

返回规整后的字段,便于写入本地 SQLite。

────────────────────────────────────────────────────────────────────────────
同步模型(2026-09-22 照 Seerr 重做, 见 .workbuddy/docs/Seerr同步模型研究.md)
────────────────────────────────────────────────────────────────────────────
本模块的取数接口设计遵循 Seerr 的两条铁律:

  1. **写入永远可加**: 拉到的数据只用于 upsert(新增/更新), 调用方**从不**以
     "远端列表里没有它"为理由删本地行。所以取数接口**不需要**"快照是否完整"
     这类判断 —— 一份残缺列表最多让数据少写几条, 下一轮就补上。
  2. **删除只由 fail-safe 的专职作业做**: 判断"某个东西是否已从库里消失"必须
     **按 ID 单查**(`item_exists`), 且**只有明确查不到**才算消失;
     任何请求失败/超时/形状异常一律返回"无法判断", 调用方必须当作"还在"。

因此这里**没有**游标(改用窗口式重读, 见 `list_recent_items`), 也**没有**
`snapshot_complete` / 扫库状态探测这类"闸门"辅助 —— 那些是为"先清空再重建"
的旧模型打的补丁, 已随旧模型一起删除。
"""
from datetime import datetime, timezone

import httpx

from lib.config import load_config

JF_IMAGE = "https://image.tmdb.org/t/p/w300"

# 增量窗口大小: 每轮重读"最新 N 条"(对应 Seerr `/Items/Latest?Limit=12`)。
# 没有游标 ⇒ 重复处理是幂等的、零代价; 少读到的条目由下一轮或每日全量扫到。
RECENT_WINDOW = 300


def auth_headers(cfg_or_token):
    """构造 Jellyfin 认证头。

    允许传 {"jellyfin": {...}} 配置字典,或直接传 token 字符串。
    """
    if isinstance(cfg_or_token, dict):
        token = (cfg_or_token.get("jellyfin", {}) or {}).get("token", "")
    else:
        token = cfg_or_token or ""
    return {
        "Authorization": f'MediaBrowser Token="{token}"',
        "Accept": "application/json",
    }


async def _jf_get(cfg, path, params=None):
    url = (cfg.get("jellyfin", {}).get("url", "")).rstrip("/") + path
    # trust_env=False: 不走系统/环境代理(本机 HTTP_PROXY 指向 127.0.0.1, 会把内网
    # Jellyfin 地址 nas.example.com 卡死, 与 TMDB 客户端一致)。
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.get(url, headers=auth_headers(cfg), params=params)
        r.raise_for_status()
        return r.json()


async def _jf_put(cfg, path, json_body):
    url = (cfg.get("jellyfin", {}).get("url", "")).rstrip("/") + path
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.put(url, headers=auth_headers(cfg), json=json_body)
        r.raise_for_status()
        return r.json() if r.content else {}


async def update_provider_ids(cfg, item_id, provider_ids):
    """PUT /Items/{id}: 只改 ProviderIds(如 TMDb/IMDb), 其余字段不动。

    用于「刮削 ID 校准」: 把 TMM/NFO 写错的 TMDB ID 修正为 TMDB 接口反查到的
    权威 ID。Jellyfin 保存 ProviderIds 后会按新 ID 重新拉取/关联元数据。
    """
    return await _jf_put(cfg, f"/Items/{item_id}", {"ProviderIds": provider_ids})


async def item_exists(cfg, item_id):
    """按 ID 单查一个条目是否还在 Jellyfin 库里 —— 三态返回。

    这是 Seerr `availabilitySync.mediaExistsInJellyfin()` 里 `getItemData(ratingKey)`
    的等价物, 是「fail-safe 消失检测」的地基:

      True  = 明确查到 → 还在库
      False = 明确查不到 → 库里确实没有它(**只有这一种情况才允许判"消失"**)
      None  = 无法判断(请求失败/超时/服务端错误/响应形状异常)
              → 调用方**必须当作"还在"**, 绝不据此判删
              (对应 Seerr: `catch` 里 `existsInJellyfin = true`)

    ⚠️ 为什么用 `/Items?ids=` 而不是 `/Items/{id}`: 本机 Jellyfin 12.x 对单条 GET
    一律 400(实测), 而 `ids` 过滤参数工作正常 —— 实测: 真实 ID → TotalRecordCount=1,
    不存在的 ID → 0。这个差异在 Seerr 里不存在(它用单条 GET), 是我们适配本机的唯一偏离。
    """
    if not item_id:
        return None
    try:
        data = await _jf_get(cfg, "/Items", {"ids": str(item_id), "Limit": 2})
    except Exception:  # noqa: BLE001
        return None
    items = (data or {}).get("Items")
    if items is None:
        return None          # 响应形状不对 → 不可判断(宁可当作"还在")
    return len(items) > 0


async def list_libraries(cfg):
    """返回虚拟媒体库列表。

    每个元素: {library_id, name, type, locations(list), item_count}
    type 取 CollectionType(movies/tvshows/music/...)。
    """
    data = await _jf_get(cfg, "/Library/VirtualFolders")
    out = []
    for lib in data or []:
        ctype = (lib.get("CollectionType") or "").lower()
        out.append({
            "library_id": lib.get("ItemId") or lib.get("Id") or "",
            "name": lib.get("Name") or "",
            "type": ctype,
            "locations": "\n".join(lib.get("Locations") or []),
            "item_count": 0,
        })
    return out


async def library_item_count(cfg, library_id):
    try:
        data = await _jf_get(cfg, "/Items", {
            "ParentId": library_id,
            "Recursive": "true",
            "Limit": 1,
            "IncludeItemTypes": "Movie,Series",
        })
        return (data or {}).get("TotalRecordCount", 0)
    except Exception:
        return 0


_PAGE_SIZE = 1000     # Jellyfin /Items 单页上限
_HARD_CAP = 100000    # 安全上限, 防止异常响应导致死循环


def parse_jf_time(s):
    """Jellyfin 时间字符串 -> datetime(UTC aware)。失败返回 None。

    实测格式: 2026-09-15T17:41:50.0000000Z(7 位小数 + Z)。

    ⚠️⚠️ **尺度陷阱(实测确认)**: Jellyfin 虽然把时间标成 `Z`(名义 UTC), 但值其实是
    **服务器本地墙钟时间**。证据: 用户 12:52 整理完成的剧, DateCreated 恰好是
    `12:52:05Z`, 而当时真实 UTC 是 04:52:05 —— 差了 8 小时(东八区)。
    本函数只做字符串解析, **不做时区换算**, 所以 DateCreated / `media_added_at`
    两者同尺度, 互相比较安全; 但**绝不能**与 `datetime.now(timezone.utc)` 混比。
    """
    if not s:
        return None
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


async def list_items(cfg, library_id=None, limit=None, types="Movie,Series"):
    """返回媒体项(电影/剧集), **自动翻页拉全量**(Jellyfin /Items 单页最多 1000)。

    limit: 最多取多少条(None = 全部, 受 _HARD_CAP 安全上限保护)。
    每个元素: {item_id, library_id, name, type, year, path, tmdb_id, imdb_id, overview}

    ⚠️ 这里**不返回**"接口自报总数"之类的完整性信号: 取数方一律只做 upsert,
    不据列表形状删任何东西, 所以完整性无关紧要(见模块 docstring)。
    """
    start = 0
    raw = []
    while len(raw) < _HARD_CAP and (limit is None or len(raw) < limit):
        params = {
            "Recursive": "true",
            "Fields": "ProviderIds,Path,ProductionYear,Overview,Genres,DateCreated",
            "IncludeItemTypes": types,
            "StartIndex": start,
            "Limit": _PAGE_SIZE,
        }
        if library_id:
            params["ParentId"] = library_id
        data = await _jf_get(cfg, "/Items", params)
        results = (data or {}).get("Items") or []
        if not results:
            break
        raw.extend(results)
        start += len(results)
        total = (data or {}).get("TotalRecordCount") or 0
        if total and start >= total:
            break  # 已取完
    return _norm_items(raw, library_id)


async def list_episodes(cfg, library_id=None):
    """拉取全部分集(Episode), 自动翻页。用于「分集级缺失」。

    每个元素: {series_id, season(ParentIndexNumber), episode(IndexNumber), name}
    ⚠️ 量很大(实测 ~7.7 万), 单页 1000, 约 4 分钟。series_id 是 Jellyfin Series
    项的 item_id(与 list_items 返回的 item_id 一致, 可查 tmdb_id)。
    """
    out = []
    start = 0
    while len(out) < _HARD_CAP:
        params = {
            "Recursive": "true",
            "Fields": "SeriesId,IndexNumber,ParentIndexNumber",
            "IncludeItemTypes": "Episode",
            "StartIndex": start,
            "Limit": _PAGE_SIZE,
        }
        if library_id:
            params["ParentId"] = library_id
        data = await _jf_get(cfg, "/Items", params)
        results = (data or {}).get("Items") or []
        if not results:
            break
        for it in results:
            out.append({
                "series_id": it.get("SeriesId") or it.get("ParentId") or "",
                "season": int(it.get("ParentIndexNumber") or 0),
                "episode": int(it.get("IndexNumber") or 0),
                "name": it.get("Name") or "",
            })
        start += len(results)
        total = (data or {}).get("TotalRecordCount") or 0
        if total and start >= total:
            break
    return out


async def list_episodes_for_series(cfg, series_id):
    """拉取单个剧集系列的全部分集(用于「增量补分集」, 避免全量 7.7 万条重建)。

    走 Jellyfin 专用端点 /Shows/{seriesId}/Episodes, 只返回该剧的分集, 高效。
    每个元素: {series_id, season(ParentIndexNumber), episode(IndexNumber), name}
    """
    out = []
    start = 0
    while len(out) < _HARD_CAP:
        params = {
            "Fields": "SeriesId,IndexNumber,ParentIndexNumber",
            "StartIndex": start,
            "Limit": _PAGE_SIZE,
        }
        data = await _jf_get(cfg, f"/Shows/{series_id}/Episodes", params)
        results = (data or {}).get("Items") or []
        if not results:
            break
        for it in results:
            out.append({
                "series_id": it.get("SeriesId") or series_id,
                "season": int(it.get("ParentIndexNumber") or 0),
                "episode": int(it.get("IndexNumber") or 0),
                "name": it.get("Name") or "",
            })
        start += len(results)
        total = (data or {}).get("TotalRecordCount") or 0
        if total and start >= total:
            break
    return out


async def list_seasons(cfg, series_id):
    """拉取一部剧的**季号列表**(GET /Shows/{seriesId}/Seasons)。

    返回 [季号(int), ...](如 [1, 2, 3, 4, 5]), 不含 Specials(季 0)。

    用途: 判断"某季是否还存在于库里"(Jellyfin 只在某季真有文件时才建那个 Season 项)。
    ⚠️ 调用方要把**异常**当成"无法判断"处理, 绝不能在请求失败时认定"季没了"
    (Seerr `seasonExistsInJellyfin` 的 catch 就是 `seasonExistsInJellyfin = true`)。
    """
    out = []
    data = await _jf_get(cfg, f"/Shows/{series_id}/Seasons",
                         {"Fields": "IndexNumber", "Limit": 200})
    for it in ((data or {}).get("Items") or []):
        n = it.get("IndexNumber")
        if n is None:
            continue
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            continue
    return out


async def list_recent_items(cfg, limit=RECENT_WINDOW, types="Movie,Series"):
    """增量窗口: 按 DateCreated 倒序取**最新 N 条**条目(**没有时间下界**)。

    这是 Seerr `getRecentlyAdded()`(`GET /Items/Latest?Limit=12&ParentId=`)的等价物。

    为什么不要游标(旧设计用 DateCreated 游标 + `next_cursor` 推进):
      · Jellyfin 的 DateCreated 取自文件/扫描时间, **不保证单调** —— 后入库的条目
        可能带更早的时间戳, 游标一旦越过那个时刻, 这条就**永久漏掉**;
      · 写入本来就是幂等的 upsert(只增改、不删除), 所以"多读一遍"零代价,
        完全不需要用游标来"避免重复"。

    也就是说: 宁可每轮多读几十条重复的, 也不要为了省这点流量去冒"永久漏"的风险。
    少读到的(窗口之外的)由每日全量扫描兜底 —— 与 Seerr 的行为一致。
    """
    data = await _jf_get(cfg, "/Items", {
        "Recursive": "true",
        "Fields": "ProviderIds,Path,ProductionYear,Overview,Genres,DateCreated",
        "IncludeItemTypes": types,
        "SortBy": "DateCreated",
        "SortOrder": "Descending",
        "StartIndex": 0,
        "Limit": max(1, min(int(limit or RECENT_WINDOW), _PAGE_SIZE)),
    })
    return _norm_items((data or {}).get("Items") or [], None)


async def list_recent_episode_series(cfg, limit=RECENT_WINDOW):
    """增量窗口(分集): 按 DateCreated 倒序取最新 N 条分集 → 去重后的 series_id 列表。

    用途: 发现"剧先入库、分集后来才补齐"的半成品 —— 那类剧的 Series 项
    DateCreated 很早(增量窗口看不到它), 但它的**分集**是新的, 于是能从分集侧发现。

    同样**不设时间下界、不做游标**: 每轮重读最近一段, 拉到就重刷那些剧的分集。
    """
    data = await _jf_get(cfg, "/Items", {
        "Recursive": "true",
        "Fields": "SeriesId,DateCreated",
        "IncludeItemTypes": "Episode",
        "SortBy": "DateCreated",
        "SortOrder": "Descending",
        "StartIndex": 0,
        "Limit": max(1, min(int(limit or RECENT_WINDOW), _PAGE_SIZE)),
    })
    out, seen = [], set()
    for it in ((data or {}).get("Items") or []):
        sid = it.get("SeriesId") or ""
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _norm_items(raw, library_id):
    out = []
    for it in raw:
        providers = it.get("ProviderIds") or {}
        out.append({
            "item_id": it.get("Id") or "",
            "library_id": library_id or it.get("ParentId") or "",
            "name": it.get("Name") or "",
            "type": it.get("Type") or "",
            "year": int(it.get("ProductionYear") or 0),
            "path": it.get("Path") or "",
            "tmdb_id": str(providers.get("Tmdb") or providers.get("Tvdb") or ""),
            "imdb_id": str(providers.get("Imdb") or ""),
            "overview": (it.get("Overview") or "")[:500],
            "date_created": parse_jf_time(it.get("DateCreated")),  # 作品入库时间(Seerr mediaAddedAt 等价)
        })
    return out
