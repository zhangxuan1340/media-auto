"""Seerr 客户端(异步 + 同步, 基于 httpx)

封装 Seerr REST API (Overseerr / Jellyseerr / 合并版 Seerr 通用):
  - 缺失剧集:    /api/v1/request                  (从请求队列筛出"还没下完"的 TV)
  - 热门剧集/电影: /api/v1/discover/{tv|movies}    (⚠️ 不是 /tmdb/{kind}/popular,那个路径不存在)
  - 详情:         /api/v1/movie/{tmdbId} 与 /api/v1/tv/{tmdbId}
                  ⚠️ 不是 /api/v1/tmdb/{kind}/{id}(实测该路径 404)
                  返回里含 imdbId / externalIds.imdbId —— 重命名成 title.year.ttXXXX 就靠它
  - 搜索:         /api/v1/search?query=...
                  ⚠️ query 必须是 %20 编码;传 + 会被拒(400 "must be url encoded")

同时提供「同步友好」的结构化函数(fetch_missing_requests / fetch_popular / resolve_meta),
把数据规整为可直接写入本地 SQLite 或直接用于重命名的字典。
"""
import urllib.parse

import httpx

from lib.config import load_config

TMDB_IMG = "https://image.tmdb.org/t/p/w300"
_SEERR_STATUS = {
    1: "待审批", 2: "已批准", 3: "已拒绝", 4: "已就绪",
    5: "部分就绪", 6: "失败", 7: "处理中",
}

# 「热门/发现」的真实端点: Overseerr / Jellyseerr / Seerr 都是 /api/v1/discover/*。
# 注意: 不存在 /api/v1/tmdb/{movie,tv}/popular —— /api/v1/tmdb/{kind}/{id} 是 TMDB 详情代理,
#       最后一段必须是数字 tmdbId;传 "popular" 会直接 404。
_DISCOVER_PATH = {"movie": "/api/v1/discover/movies", "tv": "/api/v1/discover/tv"}
# 「详情」的真实端点: /api/v1/movie/{id} 与 /api/v1/tv/{id}(实测可用)。
_DETAIL_PATH = {"movie": "/api/v1/movie/{}", "tv": "/api/v1/tv/{}"}


def _discover_path(kind):
    path = _DISCOVER_PATH.get(kind)
    if not path:
        raise ValueError("kind 仅支持 movie / tv")
    return path


def _detail_path(kind, tmdb_id):
    tpl = _DETAIL_PATH.get(kind)
    if not tpl:
        raise ValueError("kind 仅支持 movie / tv")
    return tpl.format(tmdb_id)


def _qs(path, **params):
    """把查询参数拼成 URL。**空格用 %20 而不是 +** —— Seerr 会拒绝 + 编码。

    所以这里不用 httpx 的 params(其 urlencode 语义随版本可能变成 +),而是自己 quote。
    """
    pairs = [(k, v) for k, v in params.items() if v is not None and v != ""]
    if not pairs:
        return path
    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in pairs)
    return f"{path}?{qs}"


def _poster(path):
    if not path:
        return ""
    return path if path.startswith("http") else TMDB_IMG + path


def _headers(cfg):
    return {"X-Api-Key": cfg.get("seerr", {}).get("api_key", ""), "Accept": "application/json"}


def _base(cfg):
    return (cfg.get("seerr", {}).get("url", "") or "").rstrip("/")


async def _seerr_get(cfg, path, params=None):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_base(cfg) + path, headers=_headers(cfg), params=params)
        r.raise_for_status()
        return r.json()


def _seerr_get_sync(cfg, path, params=None, timeout=30):
    """同步版(供 CLI 脚本用)。path 里应已包含编码好的 query(见 _qs)。"""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(_base(cfg) + path, headers=_headers(cfg), params=params)
        r.raise_for_status()
        return r.json()


def _year_of(item):
    d = item.get("releaseDate") or item.get("firstAirDate") or item.get("airDate") or ""
    return (d or "")[:4]



def _norm_card(item, kind):
    title = item.get("title") or item.get("name") or "未知"
    date = item.get("releaseDate") or item.get("firstAirDate") or ""
    return {
        "tmdbId": item.get("id"),
        "kind": kind,
        "title": title,
        "year": (date or "")[:4],
        "overview": (item.get("overview") or "")[:240],
        "poster": _poster(item.get("posterPath") or item.get("poster_path")),
        "vote": round(float(item.get("voteAverage") or item.get("vote_average") or 0), 1),
        "popularity": round(float(item.get("popularity") or 0), 1),
    }


# ---------------------------------------------------------------------------
# 给前端的「展示型」接口(保持原有 shape)
# ---------------------------------------------------------------------------
async def popular(cfg, kind, page=1):
    """kind = 'movie' | 'tv' —— 走 /api/v1/discover/{movies|tv}(默认按 TMDB popularity 排序)。"""
    data = await _seerr_get(cfg, _discover_path(kind), {"page": page})
    results = data.get("results") or data.get("data", {}).get("results") or []
    return [_norm_card(x, kind) for x in results]


def _img(path, size="w300"):
    if not path:
        return ""
    return path if path.startswith("http") else f"https://image.tmdb.org/t/p/{size}{path}"


# TMDB crew 里哪些 job 归入 TMM 的 <crew>(制片/选角/导演)。参照 TMM 实际输出。
CREW_JOBS = {
    "Director", "Producer", "Executive Producer", "Co-Producer",
    "Associate Producer", "Co-Executive Producer", "Line Producer",
    "Casting", "Unit Production Manager", "Writer", "Screenplay", "Novel",
    "Original Music Composer", "Director of Photography",
}
PRODUCER_ROLES = {"Producer", "Executive Producer", "Co-Producer",
                  "Associate Producer", "Co-Executive Producer", "Line Producer"}


def _cast_list(credits, limit=0):
    """TMDB cast -> TMM <actor> 结构。"""
    out = []
    for c in (credits.get("cast") or []):
        if not c.get("name"):
            continue
        pid = c.get("id")
        out.append({
            "name": c.get("name"),
            "role": c.get("character") or "",
            "tmdbid": pid,
            "profile": f"https://www.themoviedb.org/person/{pid}" if pid else "",
            "thumb": _img(c.get("profilePath"), "h632"),
        })
        if limit and len(out) >= limit:
            break
    return out


def _crew_list(credits, jobs=None, limit=0):
    """TMDB crew -> TMM <crew> 结构(job 白名单过滤,保持 TMDB 原顺序)。"""
    jobs = jobs if jobs is not None else CREW_JOBS
    out = []
    for c in (credits.get("crew") or []):
        job = c.get("job") or ""
        if not c.get("name") or job not in jobs:
            continue
        pid = c.get("id")
        out.append({
            "name": c.get("name"),
            "job": job,
            "tmdbid": pid,
            "profile": f"https://www.themoviedb.org/person/{pid}" if pid else "",
            "thumb": _img(c.get("profilePath"), "h632"),
        })
        if limit and len(out) >= limit:
            break
    return out


def _directors(credits):
    """导演列表(带 profile/thumb,供 NFO 末尾 <crew> 用)。

    实测 TMM 的末尾 <crew> 段 = 【制片人】+【导演】,顺序为制片在前、导演在后,
    且**不含** Writer/Screenplay 等;所以这里只要能拼出同样结构就够了。
    """
    out = []
    for c in (credits.get("crew") or []):
        if c.get("job") != "Director" or not c.get("name"):
            continue
        pid = c.get("id")
        out.append({
            "name": c.get("name"),
            "job": "Director",
            "tmdbid": pid,
            "profile": f"https://www.themoviedb.org/person/{pid}" if pid else "",
            "thumb": _img(c.get("profilePath"), "h632"),
        })
    return out


def english_title_sync(cfg, kind, tmdb_id):
    """取条目在 TMDB 的**英文标题**(TMM 的 <english_title> 就是这个)。

    做法: 同一个详情接口加 `?language=en` —— 实测对中文片同样有效:
      movie/287703 -> 'Café. Waiting. Love'   tv/262741 -> 'Youthful Glory'
    与原文标题相同(本来就是英文)时返回那个原文标题。失败返回 ''。
    """
    if not tmdb_id:
        return ""
    try:
        d = _seerr_get_sync(cfg, _detail_path(kind, tmdb_id), {"language": "en"})
    except Exception:  # noqa: BLE001
        return ""
    media = d.get("media", d)
    return media.get("title") or media.get("name") or ""


def _producers(credits):
    """TMDB crew -> TMM 独立 <producer tmdbid="..."> 块(与 <crew> 里的制片条目并存)。

    TMM 实测格式(等一个人咖啡):
      <producer tmdbid="1012319">
        <name>九把刀</name><role>Producer</role><thumb>..</thumb><profile>..</profile>
      </producer>
    注意: 这里 <role> 写的是 job 名(Producer),**不是**角色名;且没有 <tmdbid> 子元素(id 作属性)。
    """
    out = []
    for c in (credits.get("crew") or []):
        job = c.get("job") or ""
        if job not in PRODUCER_ROLES or not c.get("name"):
            continue
        pid = c.get("id")
        out.append({
            "name": c.get("name"),
            "role": job,
            "tmdbid": pid,
            "profile": f"https://www.themoviedb.org/person/{pid}" if pid else "",
            "thumb": _img(c.get("profilePath"), "h632"),
        })
    # TMM 实测按 TMDB 人物 id 升序输出(等一个人咖啡: 1012319 九把刀 -> 1241106 柴智屏)
    out.sort(key=lambda x: x.get("tmdbid") or 0)
    return out


def _trailer(media):
    """从相关视频里挑预告片,返回 TMM <trailer> 要写的 URL(拿不到返回 '')。

    Seerr 把预告片放在 `relatedVideos`(不是 TMDB 的 `videos`,后者实测恒为 null)。
    优先 type=Trailer,同类型时取分辨率最高的。JELLYFIN profile 写纯 URL。
    """
    vids = media.get("relatedVideos") or media.get("videos") or []
    if isinstance(vids, dict):
        vids = vids.get("results") or []
    best, best_size = None, -1
    for v in vids:
        if not isinstance(v, dict):
            continue
        if (v.get("type") or "").lower() not in ("trailer", "teaser"):
            continue
        size = int(v.get("size") or 0)
        if size > best_size:
            best, best_size = v, size
    if not best:
        return ""
    url = best.get("url") or ""
    if not url and (best.get("site") or "").lower() == "youtube" and best.get("key"):
        url = f"https://www.youtube.com/watch?v={best['key']}"
    return url


def _certification(media):
    """从 releases.results 里取分级(优先 US / CN / HK / TW,否则第一个非空)。

    TMM 格式: 'US:PG-13'(多个用 ' / ' 连接)。拿不到就返回空串(参考 NFO 里就是空标签)。
    """
    rel = media.get("releases") or {}
    results = rel.get("results") if isinstance(rel, dict) else None
    if not results:
        return ""
    by_cc = {}
    for r in results:
        cc = r.get("iso_3166_1") or ""
        certs = [x.get("certification") for x in (r.get("release_dates") or [])]
        certs = [c for c in certs if c]
        if cc and certs:
            by_cc[cc] = certs
    for cc in ("US", "CN", "HK", "TW", "GB", "JP"):
        if by_cc.get(cc):
            seen, uniq = set(), []
            for c in by_cc[cc]:
                if c not in seen:
                    seen.add(c)
                    uniq.append(c)
            return " / ".join(f"{cc}:{c}" for c in uniq[:3])
    if by_cc:
        cc = sorted(by_cc)[0]
        return f"{cc}:{by_cc[cc][0]}"
    return ""


def _norm_detail(data, kind, tmdb_id):
    """把 Seerr 详情响应规整成统一结构(含 imdb_id + NFO 所需全部字段)。

    字段来源(实测):
      - 电影: imdbId 在顶层; TV: 在 externalIds.imdbId, tvdb 在 externalIds.tvdbId
      - 分级: 只有 movie 有 releases;TV 拿不到(留空,与 TMM 输出一致)
      - 国名/语种: TMDB 返回英文,交给 lib/nfo.py 映射成中文
      - belongsToCollection: Seerr 实测恒为 null,故 collection 一般为空
    """
    media = data.get("media", data)
    ext = media.get("externalIds") or {}
    imdb = media.get("imdbId") or ext.get("imdbId") or ""
    tmdb = media.get("id") or tmdb_id
    credits = media.get("credits") or {}

    # TV 的"时长"是 episodeRunTime 数组;电影是 runtime
    runtime = media.get("runtime")
    if not runtime:
        ert = media.get("episodeRunTime") or []
        runtime = (ert or [None])[0]

    countries = [c.get("iso_3166_1") for c in (media.get("productionCountries") or []) if c.get("iso_3166_1")]
    languages = [l.get("iso_639_1") for l in (media.get("spokenLanguages") or []) if l.get("iso_639_1")]

    # studio: 电影用制片公司;剧集用电视台 + 制片公司
    studios = [c.get("name") for c in (media.get("productionCompanies") or []) if c.get("name")]
    if kind == "tv":
        nets = [n.get("name") for n in (media.get("networks") or []) if n.get("name")]
        seen, merged = set(), []
        for s in nets + studios:
            if s not in seen:
                seen.add(s)
                merged.append(s)
        studios = merged

    coll = media.get("belongsToCollection") or {}
    seasons = [{
        "number": s.get("seasonNumber"),
        "name": s.get("name") or "",
        "episodes": s.get("episodeCount"),
    } for s in (media.get("seasons") or [])]

    return {
        "tmdbId": tmdb,
        "kind": kind,
        "title": media.get("title") or media.get("name") or "未知",
        "originalTitle": media.get("originalTitle") or media.get("originalName") or "",
        "year": _year_of(media),
        "overview": media.get("overview") or "",
        "poster": _poster(media.get("posterPath") or media.get("poster_path")),
        "backdrop": _poster(media.get("backdropPath") or media.get("backdrop_path")),
        "vote": round(float(media.get("voteAverage") or media.get("vote_average") or 0), 1),
        "genres": [g.get("name") for g in (media.get("genres") or []) if g.get("name")],
        # 重命名要用的 ID
        "imdb_id": imdb,
        "tmdb_id": tmdb,
        "tvdb_id": ext.get("tvdbId"),
        # ---- 以下为 NFO 生成所需 ----
        "tagline": media.get("tagline") or "",
        "runtime": runtime,
        "premiered": media.get("releaseDate") or media.get("firstAirDate") or "",
        "end_date": media.get("lastAirDate") or "",
        "status": media.get("status") or "",
        "vote_count": media.get("voteCount") or media.get("vote_count") or 0,
        "certification": _certification(media),
        "countries": countries,
        "languages": languages,
        # ---- 以下供 lib/classify.py 决定媒体库目录(CnMovie/EnShow/JpKrShow...) ----
        # original_language 是 TMDB 的原始语言(zh/ja/ko/en/th...),分类比 spokenLanguages 更准
        "original_language": media.get("originalLanguage") or media.get("original_language") or "",
        "adult": bool(media.get("adult")),
        "production_companies": [c.get("name") for c in (media.get("productionCompanies") or []) if c.get("name")],
        "studios": studios,
        "keywords": [k.get("name") for k in (media.get("keywords") or []) if k.get("name")],
        "cast": _cast_list(credits),
        "directors": _directors(credits),
        "producers": _producers(credits),
        "trailer": _trailer(media),
        "crew": _crew_list(credits),
        "collection": {
            "id": coll.get("id"),
            "name": coll.get("name") or "",
            "overview": coll.get("overview") or "",
        } if coll.get("id") else None,
        "seasons": seasons,
        "number_of_seasons": media.get("numberOfSeasons"),
        "homepage": media.get("homepage") or "",
    }


async def detail(cfg, kind, tmdb_id):
    """详情。端点 /api/v1/movie/{id} 或 /api/v1/tv/{id}(⚠️ 不是 /api/v1/tmdb/...)。"""
    data = await _seerr_get(cfg, _detail_path(kind, tmdb_id))
    return _norm_detail(data, kind, tmdb_id)


def detail_sync(cfg, kind, tmdb_id):
    return _norm_detail(_seerr_get_sync(cfg, _detail_path(kind, tmdb_id)), kind, tmdb_id)


# ---------------------------------------------------------------------------
# 搜索 / 反查(把"种子名 / 目录名"解析成 TMDB/IMDB 元数据)
# ---------------------------------------------------------------------------
def search_sync(cfg, query, page=1):
    """搜索(同步)。query 里的空格会编码成 %20 —— 不能用 +,Seerr 会 400。"""
    data = _seerr_get_sync(cfg, _qs("/api/v1/search", query=query, page=page))
    return data.get("results") or []


def _norm_search_card(item):
    """Seerr /api/v1/search 结果 -> 影片卡片(只要 movie/tv, 过滤 person/collection)。"""
    mt = (item.get("mediaType") or "").lower()
    if mt not in ("movie", "tv"):
        return None
    return {
        "tmdbId": item.get("id"),
        "kind": mt,
        "title": item.get("title") or item.get("name") or "未知",
        "year": _year_of(item),
        "overview": (item.get("overview") or "")[:240],
        "poster": _poster(item.get("posterPath") or item.get("poster_path")),
        "vote": round(float(item.get("voteAverage") or item.get("vote_average") or 0), 1),
    }


async def search_cards(cfg, query, limit=8):
    """按片名搜【具体影片/剧集】(Seerr /api/v1/search 是 TMDB 代理, 中文可搜)。

    返回 movie+tv 混合卡片, 按 (kind, tmdbId) 去重, 最多 limit 张。
    供前端"先确认影片、再看磁力"的两段式搜索用。
    """
    data = await _seerr_get(cfg, _qs("/api/v1/search", query=query, page=1))
    results = data.get("results") or []
    out, seen = [], set()
    for x in results:
        card = _norm_search_card(x)
        if not card or not card["tmdbId"]:
            continue
        key = (card["kind"], card["tmdbId"])
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
        if len(out) >= limit:
            break
    return out


def _pick_result(results, year=None, kind=None):
    """从搜索结果里挑最合适的 movie/tv(排除 person/collection 等非影视结果)。

    排序偏好: 类型匹配 > 年份完全匹配 > 年份最接近 > 结果越靠前越优先。
    """
    best, best_key = None, None
    for i, x in enumerate(results):
        mt = x.get("mediaType")
        if mt not in ("movie", "tv"):
            continue
        y = _year_of(x)
        key = (
            0 if (kind and mt == kind) else (1 if kind else 0),
            0 if (year and y == str(year)) else (1 if year else 0),
            (abs(int(year) - int(y)) if (year and str(y).isdigit()) else 999) if year else 999,
            i,
        )
        if best_key is None or key < best_key:
            best_key, best = key, x
    return best


def resolve_meta(cfg, queries, year=None, kind=None):
    """用若干候选查询名去 Seerr 反查条目,返回带 imdb/tmdb 的元数据;全失败返回 None。

    queries 可以是单个字符串或列表(见 lib/naming.title_query_candidates)。
    返回: {tmdb_id, kind, title, year, imdb_id, tvdb_id, matched_query}
    """
    if isinstance(queries, str):
        queries = [queries]
    for q in queries:
        if not q:
            continue
        try:
            results = search_sync(cfg, q)
        except Exception:  # noqa: BLE001
            continue
        hit = _pick_result(results, year=year, kind=kind)
        if not hit:
            continue
        mt = hit.get("mediaType")
        tid = hit.get("id")
        try:
            meta = detail_sync(cfg, mt, tid)
        except Exception:  # noqa: BLE001
            meta = {"tmdb_id": tid, "imdb_id": "", "tvdb_id": None,
                    "title": hit.get("title") or hit.get("name") or "",
                    "year": _year_of(hit), "kind": mt}
        meta["kind"] = mt
        meta["matched_query"] = q
        return meta
    return None


def _episode_code(sn, en):
    try:
        return f"S{int(sn):02d}E{int(en):02d}"
    except Exception:
        return f"S{sn}E{en}"


# ---------------------------------------------------------------------------
# Seerr 3.x 媒体库快照 / 屏蔽列表
# ---------------------------------------------------------------------------
_MEDIA_PAGE = 1000   # /api/v1/media 的 take 实测支持到 1000
_HARD_CAP = 100000


async def fetch_media_snapshot(cfg):
    """拉取 Seerr 媒体库全量快照(GET /api/v1/media 自动翻页)。

    ⚠️ Seerr 3.x 的 /api/v1/request 是"活动请求队列"——请求被 Radarr/Sonarr
    接手后队列就清空(实测长期 0 条),**缺失/请求中/失败的真正数据在本接口**。
    实测 6228 条 ~1s,每次同步全量快照即可(数据量小,无增量必要)。

    每个元素: {tmdb_id, media_type('movie'|'tv'), status, status_4k,
               in_jellyfin(bool), has_requests(bool)}
    """
    out = []
    skip = 0
    while skip < _HARD_CAP:
        data = await _seerr_get(cfg, "/api/v1/media", {"take": _MEDIA_PAGE, "skip": skip})
        results = data.get("results") or []
        if not results:
            break
        for x in results:
            out.append({
                "tmdb_id": x.get("tmdbId") or 0,
                "media_type": (x.get("mediaType") or "").lower(),
                "status": x.get("status") or 0,
                "status_4k": x.get("status4k") or 0,
                "in_jellyfin": bool(x.get("jellyfinMediaId")),
                "has_requests": bool(x.get("requests")),
            })
        total = (data.get("pageInfo") or {}).get("results") or 0
        skip += len(results)
        if total and skip >= total:
            break
    return out


async def fetch_blocklist(cfg):
    """拉取 Seerr 屏蔽列表(GET /api/v1/blocklist, 匿名可读, 实测 200)。

    每个元素: {tmdb_id, media_type('movie'|'tv'), title, blocked_by, created_at}
    """
    out = []
    skip = 0
    while skip < _HARD_CAP:
        data = await _seerr_get(cfg, "/api/v1/blocklist", {"take": _MEDIA_PAGE, "skip": skip})
        results = data.get("results") or []
        if not results:
            break
        for x in results:
            user = x.get("user") or {}
            out.append({
                "tmdb_id": x.get("tmdbId") or 0,
                "media_type": (x.get("mediaType") or "").lower(),
                "title": x.get("title") or "",
                "blocked_by": user.get("displayName") or user.get("email") or "",
                "created_at": x.get("createdAt") or "",
            })
        total = (data.get("pageInfo") or {}).get("results") or 0
        skip += len(results)
        if total and skip >= total:
            break
    return out


async def missing_episodes(cfg, take=100, skip=0):
    """给前端的「缺失剧集」列表(缺失分集以 SxxExx 字符串呈现)。"""
    rich = await fetch_missing_requests(cfg, take, skip)
    out = []
    for r in rich:
        missing = [e["episode_code"] for e in r["episodes"]] or ["(整剧待下载)"]
        out.append({
            "requestId": r["request_id"],
            "tmdbId": r["tmdb_id"],
            "title": r["title"],
            "year": r["year"],
            "status": r["status"],
            "statusText": r["status_text"],
            "missing": missing[:40],
            "missingCount": len(missing),
            "searchQuery": f"{r['title']} {r['year']}".strip(),
        })
    return out


# ---------------------------------------------------------------------------
# 同步友好: 结构化数据(可直接 upsert 到 SQLite)
# ---------------------------------------------------------------------------
async def fetch_missing_requests(cfg, take=200, skip=0):
    """返回还没下完的 TV 请求,带「分集级」结构。

    每个元素:
      {request_id, tmdb_id, media_type:'tv', title, year, status, status_text,
       requested_by, episodes:[{season, episode, episode_code, status}]}
    """
    data = await _seerr_get(cfg, "/api/v1/request", {"take": take, "skip": skip})
    results = data.get("results") or data.get("data", {}).get("results") or []
    out = []
    for r in results:
        if (r.get("type") or "").lower() != "tv":
            continue
        status = r.get("status")
        if status in (4, 3):  # 已就绪 / 已拒绝 -> 跳过
            continue
        media = r.get("media") or {}
        date = media.get("releaseDate") or media.get("firstAirDate") or ""
        episodes = []
        for s in (r.get("seasons") or []):
            sn = s.get("seasonNumber")
            for e in (s.get("episodes") or []):
                if e.get("status") not in (4, None):
                    episodes.append({
                        "season": sn,
                        "episode": e.get("episodeNumber"),
                        "episode_code": _episode_code(sn, e.get("episodeNumber")),
                        "status": e.get("status"),
                    })
        # 未给分集结构时,用请求级状态兜底为整剧
        if not episodes and status not in (4,):
            episodes = [{"season": 0, "episode": 0, "episode_code": "(整剧)", "status": status}]
        out.append({
            "request_id": r.get("id"),
            "tmdb_id": media.get("tmdbId") or media.get("id") or 0,
            "media_type": "tv",
            "title": media.get("title") or r.get("title") or "未知",
            "year": (date or "")[:4],
            "status": status,
            "status_text": _SEERR_STATUS.get(status, str(status)),
            "requested_by": str(r.get("requestedBy") or ""),
            "episodes": episodes,
        })
    return out


async def fetch_popular(cfg, kind, page=1):
    """返回热门榜(结构化, kind='movie'|'tv');走 /api/v1/discover/{movies|tv}。"""
    data = await _seerr_get(cfg, _discover_path(kind), {"page": page})
    results = data.get("results") or data.get("data", {}).get("results") or []
    return [_norm_card(x, kind) for x in results]
