"""TMDB 官方 API 客户端(异步 + 同步, 基于 httpx)

架构(2026-09 定稿): Web 浏览/缺失/屏蔽/隐藏 全部走本地 TMDB 缓存。
本客户端是 TMDB 的**唯一数据源**;organize/NFO 阶段同样以它为准
(detail 形状即 NFO/organize 所需的规整结构)。

⚠️ 网络事实(本机实测 2026-09):
  - api.themoviedb.org 直连 000(被墙/DNS 污染, 解析到被阻断 IP)
  - **api.tmdb.org(TMDB 备用域)可达**(无 key 返回 401 = 路由通)
  → 默认主用 api.tmdb.org, 失败自动回退 api.themoviedb.org。
  - 系统 http_proxy 会把内网地址塞进代理;本客户端用 trust_env=False 不走系统代理,
    与 clouddrive 客户端保持一致(避免 502)。

无 API key 时所有接口优雅降级: 返回 None / 空列表, 并置 last_error 供上层提示。
"""
import asyncio
import urllib.parse

import httpx

from lib.config import load_config
from lib import cache_stats as _cache_stats
from lib import titles

# 主用备用域, 回退官方主域。host 不带协议。
_HOSTS = ("api.tmdb.org", "api.themoviedb.org")
TMDB_IMG = "https://image.tmdb.org/t/p/"
_IMG_SIZES = {"w92": "w92/", "w154": "w154/", "w185": "w185/",
              "w300": "w300/", "w342": "w342/", "w500": "w500/", "w780": "w780/",
              "w1280": "w1280/", "h632": "h632/", "original": "original/"}

# 本地缓存用: 语言(中文优先)
_DEFAULT_LANG = "zh-CN"

# 进程内: 最近一次错误(供上层 UI 提示"未配置 TMDB key"等)
last_error: str = ""


def _cfg_tmdb(cfg):
    return cfg.get("tmdb", {}) or {}


def _key(cfg):
    return (_cfg_tmdb(cfg).get("api_key") or "").strip()


def _hosts(cfg):
    t = _cfg_tmdb(cfg)
    hosts = t.get("hosts")
    if hosts and isinstance(hosts, list) and hosts:
        return [h.rstrip("/") for h in hosts]
    return list(_HOSTS)


def _lang(cfg):
    return _cfg_tmdb(cfg).get("language") or _DEFAULT_LANG


def _img(path, size="w300"):
    if not path:
        return ""
    if path.startswith("http"):
        return path
    suffix = _IMG_SIZES.get(size)
    if not suffix:
        suffix = "w300/"
    # TMDB_IMG 以 / 结尾, suffix 以 / 结尾, path 去前导 / → 无双重斜杠
    return TMDB_IMG + suffix + path.lstrip("/")


async def _tmdb_get(cfg, path, params=None, timeout=30):
    """对多个候选 host 依次尝试 GET。成功返回 json, 全失败抛最后异常。

    这里是**全部 TMDB HTTP 的唯一出口** —— 走到这里就说明内存/本地缓存都没接住,
    所以记一次 `miss`(见 lib/cache_stats.py 的命中定义)。
    """
    key = _key(cfg)
    if not key:
        raise RuntimeError("TMDB 未配置 api_key(管理 → 通用 → TMDB, 或 config.example.json 的 tmdb.api_key)")
    _cache_stats.miss("tmdb")
    params = dict(params or {})
    params["api_key"] = key
    last_exc = None
    for host in _hosts(cfg):
        url = f"https://{host}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                r = await client.get(url, params=params)
                if r.status_code == 401:
                    raise RuntimeError("TMDB api_key 无效(401)")
                if r.status_code == 404:
                    return None  # 资源不存在(非网络错误), 直接返回
                r.raise_for_status()
                return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.PoolTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            continue  # 网络类错误 → 换下一个 host
        except RuntimeError:
            raise  # key 无效这类业务错误不重试
    raise RuntimeError(f"TMDB 所有 host 均不可达: {last_exc}")


def _tmdb_get_sync(cfg, path, params=None, timeout=30):
    """同步版(CLI / organize 线程用)。同样记一次 `miss`(见本文件 _tmdb_get)。"""
    key = _key(cfg)
    if not key:
        raise RuntimeError("TMDB 未配置 api_key(管理 → 通用 → TMDB, 或 config.example.json 的 tmdb.api_key)")
    _cache_stats.miss("tmdb")
    params = dict(params or {})
    params["api_key"] = key
    last_exc = None
    for host in _hosts(cfg):
        url = f"https://{host}{path}"
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                r = client.get(url, params=params)
                if r.status_code == 401:
                    raise RuntimeError("TMDB api_key 无效(401)")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.PoolTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            continue
        except RuntimeError:
            raise
    raise RuntimeError(f"TMDB 所有 host 均不可达: {last_exc}")


# ---------------------------------------------------------------------------
# 搜索(两段式搜索第一步: 先解析成具体影片/剧集)
# ---------------------------------------------------------------------------
def _norm_search_card(item, kind):
    title = item.get("title") or item.get("name") or "未知"
    date = item.get("release_date") or item.get("first_air_date") or ""
    return {
        "tmdbId": item.get("id"),
        "kind": kind,
        "title": title,
        "year": (date or "")[:4],
        "overview": (item.get("overview") or "")[:240],
        "poster": _img(item.get("poster_path")),
        "vote": round(float(item.get("vote_average") or 0), 1),
        "vote_count": item.get("vote_count") or 0,
    }


def _is_ghost(card):
    """TMDB 幽灵条目: 无年份 + 无评分 + 无海报 —— 通常是未上映的系列第二部
    (例: 「保镖恋人」的 Ébano 版, 与已上映的 Marfil 版共用一个中文译名)。
    这种条目混进搜索候选会让一个片名出现两张"长得一样"的卡, 极易误点。"""
    return not card.get("year") and not card.get("vote") and not card.get("poster")


async def search_cards(cfg, query, limit=8):
    """按片名搜【具体影片/剧集】。返回 movie+tv 混合卡片, 按 (kind,tmdbId) 去重, 最多 limit。

    ⚠️ 幽灵条目过滤: 若结果里存在【有效条目】(有年份或评分或海报), 则**丢弃**幽灵条目
    —— 避免「保镖恋人」这种同名系列把未上映的第二部(无年份/无分/无海报)也列出来
    (一个片名只该给一条能看的)。若结果全是幽灵(老片元数据残缺), 保留不丢,
    宁可多给不漏。
    """
    try:
        data = await _tmdb_get(cfg, "/3/search/multi",
                               {"query": query, "language": _lang(cfg), "include_adult": "false"})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    out, seen, ghosts = [], set(), []
    for x in (data.get("results") or []):
        mt = x.get("media_type") or x.get("mediaType")
        if mt not in ("movie", "tv"):
            continue
        card = _norm_search_card(x, mt)
        if not card["tmdbId"]:
            continue
        key = (card["kind"], card["tmdbId"])
        if key in seen:
            continue
        seen.add(key)
        (ghosts if _is_ghost(card) else out).append(card)
    if out:
        out = out[:limit]      # 有有效条目 → 幽灵(未上映第二部)不进候选
    else:
        out = ghosts[:limit]   # 全是幽灵 → 保留(老片元数据残缺的情况)
    return out


def search_cards_sync(cfg, query, limit=8):
    return _run_async(search_cards(cfg, query, limit))


def _run_async(coro):
    """在任意上下文里跑一个协程: 无事件循环 → asyncio.run;
    已有运行中循环(如测试/嵌套) → 新线程里 asyncio.run。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _pick_card(cards, year=None, kind=None):
    """从 search_cards 结果里挑最合适的:
    类型匹配 > 年份完全匹配 > 年份最接近 > 越靠前越优先。"""
    best, best_key = None, None
    for i, x in enumerate(cards):
        y = x.get("year") or ""
        key = (
            0 if (kind and x.get("kind") == kind) else (1 if kind else 0),
            0 if (year and y == str(year)) else (1 if year else 0),
            (abs(int(year) - int(y)) if (year and str(y).isdigit()) else 999) if year else 999,
            i,
        )
        if best_key is None or key < best_key:
            best, best_key = x, key
    return best


def resolve_meta_sync(cfg, queries, year=None, kind=None):
    """organize 反查(走 TMDB 直连)。

    queries: 候选片名列表(或单个字符串)。返回:
      {tmdb_id, imdb_id, tvdb_id, title, year, kind, matched_query} 或 None。
    """
    if isinstance(queries, str):
        queries = [queries]
    for q in queries:
        if not q:
            continue
        try:
            cards = _run_async(search_cards(cfg, q, limit=8))
        except Exception:  # noqa: BLE001
            continue
        hit = _pick_card(cards, year=year, kind=kind)
        if not hit or not hit.get("tmdbId"):
            continue
        mt = hit.get("kind") or "movie"
        tid = hit.get("tmdbId")
        try:
            d = _run_async(detail(cfg, mt, tid))
        except Exception:  # noqa: BLE001
            d = None
        # ⚠️ original_language + countries 必须带上 —— organize 的地区分类(Cn/En/JpKr/Hk/Sea/Ot)
        # 依赖这两个字段(见 lib/classify._region), 漏了会把所有片一律误判成 Ot(其他)。
        # ⚠️ genres/certification/adult 也必须带(与本地缓存路径 resolve_from_local_cache 同口径, 2026-09-22 补):
        # 缺 genres → classify 的动画(genre 16)/纪录片(99)判定失效, 动漫/纪录落地区目录
        # (2026-09-22 《拯救甜甜圈:时空大营救》动漫落 EnMovie 实测)。
        # certification/adult 2026-09-22 起已不参与归类(成人内容改由 Jellyfin 按分级控制),
        # 仍照传一份供展示/兼容。
        # 用 genre_ids(整数 id, 语言无关, 与 tmdb_media.genres 列同口径); detail() 返回 genre_ids/genres(名)两者。
        meta = {
            "tmdb_id": (d.get("tmdb_id") if d else None) or tid,
            "imdb_id": (d.get("imdb_id") if d else None) or "",
            "tvdb_id": d.get("tvdb_id") if d else None,
            "title": (d.get("title") if d else None) or hit.get("title") or "",
            "originalTitle": d.get("originalTitle") if d else "",
            "year": (d.get("year") if d else None) or hit.get("year") or "",
            "kind": mt,
            "matched_query": q,
            "original_language": d.get("original_language") if d else "",
            "countries": list(d.get("countries") or []) if d else [],
            "genres": list(d.get("genre_ids") or []) if d else [],
            "certification": (d.get("certification") if d else "") or "",
            "adult": bool(d.get("adult")) if d else False,
        }
        return meta
    return None


# ---------------------------------------------------------------------------
# 热门 / 发现(Web 列表页)
# ---------------------------------------------------------------------------
async def trending(cfg, kind, time_window="week", page=1):
    """TMDB 趋势榜。kind=movie|tv, time_window=day|week。
    返回 cards 列表(本页原始卡片, TMDB 固定每页 20 条)。
    注意: trending 接口的 total_results 是封顶假值(恒 10000), 不可用于判断到底,
    是否还有下一页由调用方按"本页原始是否满 20 条"判断。"""
    try:
        data = await _tmdb_get(cfg, f"/3/trending/{kind}/{time_window}",
                               {"language": _lang(cfg), "page": page})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    return [_norm_search_card(x, kind) for x in (data.get("results") or [])]


async def discover_page(cfg, kind, page=1, genre_id=None, country=None,
                        sort_by="popularity.desc"):
    """TMDB 发现接口单页(带筛选)。返回 (cards, total_pages)。

    与 trending 不同, discover 的 total_pages 是真实值 —— 筛选后榜单到底
    判断用它(不再靠"满 20 条"猜)。country 为 TMDB 国家代码(如 CN/JP/US)。
    """
    params = {"language": _lang(cfg), "page": page,
              "include_adult": "false", "sort_by": sort_by}
    if genre_id:
        params["with_genres"] = genre_id
    if country:
        params["with_origin_country"] = country
    try:
        data = await _tmdb_get(cfg, f"/3/discover/{kind}", params)
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return [], 0
    if not data:
        return [], 0
    return ([_norm_search_card(x, kind) for x in (data.get("results") or [])],
            int(data.get("total_pages") or 0))


# 影视产地国家(ISO 3166-1 两位代码 → 中文名)。TMDB 无公开的"全部国家"端点
# (/3/configuration 只回 change_keys/images, 不含 countries 列表), 而
# discover 的 with_origin_country 参数就吃这套两位代码, 故内置固定表。
# 覆盖中日韩/港台/欧美/东南亚等主流影视产地; 代码集合基本不变, 无需实时拉。
_COUNTRY_TABLE = [
    ("CN", "中国"), ("HK", "中国香港"), ("TW", "中国台湾"), ("MO", "中国澳门"),
    ("JP", "日本"), ("KR", "韩国"),
    ("US", "美国"), ("GB", "英国"), ("FR", "法国"), ("DE", "德国"), ("IT", "意大利"),
    ("ES", "西班牙"), ("RU", "俄罗斯"), ("CA", "加拿大"), ("AU", "澳大利亚"),
    ("NL", "荷兰"), ("BE", "比利时"), ("SE", "瑞典"), ("NO", "挪威"), ("DK", "丹麦"),
    ("FI", "芬兰"), ("PT", "葡萄牙"), ("GR", "希腊"), ("IE", "爱尔兰"), ("AT", "奥地利"),
    ("CH", "瑞士"), ("BR", "巴西"), ("MX", "墨西哥"), ("AR", "阿根廷"),
    ("IN", "印度"), ("TH", "泰国"), ("MY", "马来西亚"), ("ID", "印度尼西亚"),
    ("SG", "新加坡"), ("PH", "菲律宾"), ("VN", "越南"),
    ("TR", "土耳其"), ("IL", "以色列"), ("ZA", "南非"), ("EG", "埃及"),
]


def countries(cfg=None):
    """影视产地国家列表(内置固定表), 返回 [{"code","name"}]。
    榜单页"按国家筛选"下拉用; 不依赖网络, 恒返回全表。"""
    return [{"code": code, "name": name} for code, name in _COUNTRY_TABLE]


async def discover(cfg, kind, page=1, genre_id=None, year=None,
                   with_genres=None, sort_by=None, person_id=None):
    """TMDB 发现/筛选。支持按类型/年份/演员筛选。"""
    params = {"language": _lang(cfg), "page": page,
              "include_adult": "false", "region": "", "without_genres": ""}
    if genre_id:
        params["with_genres"] = genre_id
    if year:
        if kind == "movie":
            params["primary_release_year"] = year
        else:
            params["first_air_date_year"] = year
    if sort_by:
        params["sort_by"] = sort_by
    try:
        data = await _tmdb_get(cfg, f"/3/discover/{kind}", params)
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    return [_norm_search_card(x, kind) for x in (data.get("results") or [])]


# ---------------------------------------------------------------------------
# 类型(genre)映射 —— 供筛选下拉
# ---------------------------------------------------------------------------
async def genre_map(cfg, kind):
    """返回 {genre_id: 中文名}。"""
    try:
        data = await _tmdb_get(cfg, f"/3/genre/{kind}/list", {"language": _lang(cfg)})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return {}
    return {str(g.get("id")): g.get("name") for g in (data.get("genres") or [])}


def genre_map_sync(cfg, kind):
    return _run_async(genre_map(cfg, kind))


# ---------------------------------------------------------------------------
# 详情(规整后的形状 → NFO/organize 直接可用)
# ---------------------------------------------------------------------------
def _year_of(d):
    return (d.get("release_date") or d.get("first_air_date") or "")[:4]


def _certification_tmdb(release_dates):
    """TMDB /release_dates → 'US:PG-13 / CN:IIA' 风格(与 TMM 输出一致)。"""
    by_cc = {}
    for r in (release_dates or []):
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


def _certification_tv(content_ratings):
    """TMDB /content_ratings → 'US:TV-MA / HK:M' 风格(与电影 release_dates 归一为同一串)。

    ⚠️ 剧集分级走 /tv/{id}/content_ratings(结构 {"results":[{iso_3166_1, rating}]}),
    与电影 /release_dates 完全不同 —— 此前 detail() 只对电影取分级, 剧集分级恒空
    (NFO 缺 <mpaa>, Jellyfin 侧拿不到分级做访问控制, 2026-09-22 修)。
    """
    rows = content_ratings.get("results") if isinstance(content_ratings, dict) else (content_ratings or [])
    by_cc = {}
    for r in (rows or []):
        cc = r.get("iso_3166_1") or ""
        rating = (r.get("rating") or "").strip()
        if cc and rating:
            by_cc.setdefault(cc, [])
            if rating not in by_cc[cc]:
                by_cc[cc].append(rating)
    for cc in ("US", "CN", "HK", "TW", "GB", "JP"):
        if by_cc.get(cc):
            return " / ".join(f"{cc}:{c}" for c in by_cc[cc][:3])
    if by_cc:
        cc = sorted(by_cc)[0]
        return f"{cc}:{by_cc[cc][0]}"
    return ""


async def _credits(cfg, kind, tmdb_id):
    try:
        return (await _tmdb_get(cfg, f"/3/{kind}/{tmdb_id}/credits")) or {}
    except Exception:  # noqa: BLE001
        return {}


async def _release_dates(cfg, tmdb_id):
    try:
        return (await _tmdb_get(cfg, f"/3/movie/{tmdb_id}/release_dates")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _cast_list(credits, limit=0):
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
            "thumb": _img(c.get("profile_path"), "h632"),
        })
        if limit and len(out) >= limit:
            break
    return out


CREW_JOBS = {
    "Director", "Producer", "Executive Producer", "Co-Producer",
    "Associate Producer", "Co-Executive Producer", "Line Producer",
    "Casting", "Unit Production Manager", "Writer", "Screenplay", "Novel",
    "Original Music Composer", "Director of Photography",
}
PRODUCER_ROLES = {"Producer", "Executive Producer", "Co-Producer",
                  "Associate Producer", "Co-Executive Producer", "Line Producer"}


def _crew_list(credits, jobs=None, limit=0):
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
            "thumb": _img(c.get("profile_path"), "h632"),
        })
        if limit and len(out) >= limit:
            break
    return out


def _directors(credits):
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
            "thumb": _img(c.get("profile_path"), "h632"),
        })
    return out


def _producers(credits):
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
            "thumb": _img(c.get("profile_path"), "h632"),
        })
    out.sort(key=lambda x: x.get("tmdbid") or 0)
    return out


def _trailer(videos):
    best, best_size = None, -1
    for v in (videos or []):
        if not isinstance(v, dict):
            continue
        if (v.get("type") or "").lower() not in ("trailer", "teaser"):
            continue
        size = int(v.get("size") or 0)
        if size > best_size:
            best, best_size = v, size
    if not best:
        return ""
    url = best.get("key") and f"https://www.youtube.com/watch?v={best['key']}" or ""
    return url


async def detail(cfg, kind, tmdb_id):
    """详情。kind=movie|tv。

    额外多带一个 `seasons` 字段(剧集每季 {number, name, episodes, air_date, in_production}),
    供本地「分集级缺失」精确对比。
    """
    try:
        media = await _tmdb_get(cfg, f"/3/{kind}/{tmdb_id}",
                                {"language": _lang(cfg),
                                 # 电影取 release_dates 算分级; 剧集取 content_ratings 算分级 ——
                                 # 两者字段结构不同, 分别用 _certification_tmdb / _certification_tv 归一(2026-09-22 补剧集分级)。
                                 # translations: 挑中文译名用(主标题 zh-CN 拿不到中文时兜底)
                                 "append_to_response": "credits,videos,keywords,external_ids,release_dates,translations"
                                 if kind == "movie" else
                                 "credits,videos,keywords,external_ids,content_ratings,translations"})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        raise
    if not media:
        return None

    credits = media.pop("credits", {}) or {}
    videos = media.pop("videos", {}) or {}
    keywords = media.pop("keywords", {}) or {}
    release_dates = media.pop("release_dates", {}) or {}
    content_ratings = media.pop("content_ratings", {}) or {}
    translations = media.pop("translations", {}) or {}

    imdb = (media.get("external_ids") or {}).get("imdb_id") or ""
    tvdb = (media.get("external_ids") or {}).get("tvdb_id")

    runtime = media.get("runtime")
    if not runtime:
        ert = media.get("episode_run_time") or []
        runtime = (ert or [None])[0]

    countries = [c.get("iso_3166_1") for c in (media.get("production_countries") or []) if c.get("iso_3166_1")]
    languages = [l.get("iso_639_1") for l in (media.get("spoken_languages") or []) if l.get("iso_639_1")]

    studios = [c.get("name") for c in (media.get("production_companies") or []) if c.get("name")]
    if kind == "tv":
        nets = [n.get("name") for n in (media.get("networks") or []) if n.get("name")]
        seen, merged = set(), []
        for s in nets + studios:
            if s not in seen:
                seen.add(s)
                merged.append(s)
        studios = merged

    coll = media.get("belongs_to_collection") or {}
    vids = videos.get("results") if isinstance(videos, dict) else videos

    # 剧集: 把 seasons 占位补全(nfo.build_tvshow_nfo 的 <namedseason> 需要;
    # 与 all_seasons 同结构 {number,name,episodes,air_date,in_production})
    if kind == "tv":
        seasons_out = []
        for s in (media.get("seasons") or []):
            seasons_out.append({
                "number": s.get("season_number"),
                "name": s.get("name") or "",
                "episodes": s.get("episode_count"),
                "air_date": s.get("air_date") or "",
                "in_production": bool(s.get("in_production")),
            })
        media_seasons = seasons_out
    else:
        media_seasons = []

    # 中文标题: TMDB 主标题(?language=zh-CN)经常本身就是英文 —— 实测 Bad Sisters
    # zh-CN 返回 "Bad Sisters"(大陆那条 translation 的 name 是空串)。
    #   大陆译名有 → 直接用;
    #   大陆为空   → 保持英文交给 lib/titles 去豆瓣拿国内译名, 台/港译名放 zh_fallback
    #                作为"豆瓣也查不到"时的退路(至少不是英文)。
    _cn_title = media.get("title") or media.get("name") or "未知"
    _zh_main, _zh_other = titles.pick_cn_titles(translations)
    if not titles.has_cn(_cn_title) and _zh_main:
        _cn_title = _zh_main
    _zh_fallback = _zh_other if _zh_other and _zh_other != _cn_title else ""

    return {
        "tmdbId": media.get("id") or tmdb_id,
        "kind": kind,
        "title": _cn_title,
        "zh_fallback": _zh_fallback,
        "originalTitle": media.get("original_title") or media.get("original_name") or "",
        "year": _year_of(media),
        "overview": media.get("overview") or "",
        "poster": _img(media.get("poster_path")),
        "backdrop": _img(media.get("backdrop_path"), "w780"),
        "vote": round(float(media.get("vote_average") or 0), 1),
        "genres": [g.get("name") for g in (media.get("genres") or []) if g.get("name")],
        "genre_ids": [g.get("id") for g in (media.get("genres") or []) if g.get("id")],
        "imdb_id": imdb,
        "tmdb_id": media.get("id") or tmdb_id,
        "tvdb_id": tvdb,
        "tagline": media.get("tagline") or "",
        "runtime": runtime,
        "premiered": media.get("release_date") or media.get("first_air_date") or "",
        "end_date": media.get("last_air_date") or "",
        "status": media.get("status") or "",
        "vote_count": media.get("vote_count") or 0,
        "certification": _certification_tv(content_ratings) if kind == "tv"
        else _certification_tmdb(release_dates.get("results") if isinstance(release_dates, dict) else release_dates),
        "countries": countries,
        "languages": languages,
        "original_language": media.get("original_language") or "",
        "adult": bool(media.get("adult")),
        "production_companies": [c.get("name") for c in (media.get("production_companies") or []) if c.get("name")],
        "studios": studios,
        # ⚠️ 电影 keywords 在 keywords.keywords; 剧集在 keywords.results —— 两个 key 都要兼容,
        # 否则剧集关键词恒空(2026-09-22 修)。
        "keywords": [k.get("name") for k in ((keywords.get("keywords") or keywords.get("results")) or [])
                     if isinstance(k, dict) and k.get("name")] if isinstance(keywords, dict) else [],
        "cast": _cast_list(credits),
        "directors": _directors(credits),
        "producers": _producers(credits),
        "trailer": _trailer(vids),
        "crew": _crew_list(credits),
        "collection": {
            "id": coll.get("id"),
            "name": coll.get("name") or "",
            "overview": coll.get("overview") or "",
        } if coll.get("id") else None,
        "seasons": media_seasons,
        "number_of_seasons": media.get("number_of_seasons"),
        "in_production": bool(media.get("in_production")),
        "homepage": media.get("homepage") or "",
    }


def detail_sync(cfg, kind, tmdb_id):
    """同步版(organize/NFO 线程用)。形状同 detail()。"""
    return _run_async(detail(cfg, kind, tmdb_id))


def english_title_sync(cfg, kind, tmdb_id):
    """英文名(?language=en 的 title/name)。"""
    try:
        media = _tmdb_get_sync(cfg, f"/3/{kind}/{tmdb_id}", {"language": "en"})
    except Exception:  # noqa: BLE001
        return ""
    if not media:
        return ""
    return media.get("title") or media.get("name") or ""


async def english_title(cfg, kind, tmdb_id):
    """异步版英文名(?language=en 的 title/name)。供 sync_tmdb 在事件循环里调用。
    失败/不存在返回空串, 不抛异常(英文名缺失只影响磁力双查, 不应阻断主同步)。"""
    try:
        media = await _tmdb_get(cfg, f"/3/{kind}/{tmdb_id}", {"language": "en"})
    except Exception:  # noqa: BLE001
        return ""
    if not media:
        return ""
    return media.get("title") or media.get("name") or ""


# ---------------------------------------------------------------------------
# 演员(搜索 + 详情页作品列表)
# ---------------------------------------------------------------------------
def _norm_person_card(x, kind, character=""):
    """combined_credits 里的一条作品 → 卡片(带角色名)。"""
    card = _norm_search_card(x, kind)
    if character:
        card["character"] = character
    return card


async def person_search(cfg, query, limit=8):
    """按名字搜演员/导演(/3/search/person)。返回 [{id, name, profile, knownFor, popularity}]。

    同名很多(如"刘德华"有几十个), 按 TMDB 热度降序, 让真正的明星排最前。
    翻 2 页(40 条)再排序, 避免热门人物被第 1 页的低热度同名挤掉。
    """
    results = []
    try:
        for page in (1, 2):
            data = await _tmdb_get(cfg, "/3/search/person",
                                   {"query": query, "language": _lang(cfg),
                                    "sort_by": "popularity.desc", "page": page})
            results.extend(data.get("results") or [])
            if not (data.get("results") or []):
                break
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    out = []
    for x in results:
        if not x.get("id") or x.get("adult"):
            continue
        kf = [k.get("title") or k.get("name") or ""
              for k in (x.get("known_for") or [])
              if (k.get("media_type") in ("movie", "tv") and (k.get("title") or k.get("name")))]
        out.append({
            "id": x.get("id"),
            "name": x.get("name") or "",
            "profile": _img(x.get("profile_path"), "w185"),
            "knownFor": list(dict.fromkeys(kf))[:4],
            "popularity": x.get("popularity") or 0,
        })
    out.sort(key=lambda p: p["popularity"], reverse=True)
    return out[:limit]


def person_search_sync(cfg, query, limit=8):
    return _run_async(person_search(cfg, query, limit))


async def person_credits(cfg, person_id, limit_per_kind=24):
    """演员的电影/剧集作品(combined_credits),带角色名,按上映/首播年份倒序。

    ⚠️ TMDB v3 /3/person/{id}/combined_credits 返回的是【扁平】的
    {"cast": [...], "crew": [...]} —— 每项自带 media_type(movie/tv),
    而不是 {"movie": [...], "tv": [...]}。按 media_type 拆分即可。
    返回 {"movies": [card], "tv": [card]} —— card 同 _norm_search_card 多一个 character。
    """
    try:
        # ⚠️ 必须带 language: 不传则 title/name 返回英文默认名(如 "Running Out of Time"),
        # 点进去详情(本地缓存中文)又变中文, 列表与详情语言不一致(用户报的 Bug)。
        data = await _tmdb_get(cfg, f"/3/person/{person_id}/combined_credits",
                               {"language": _lang(cfg)})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return {"movies": [], "tv": []}
    if not data:
        return {"movies": [], "tv": []}

    def _with_char(items, kind):
        out = []
        seen = set()
        for m in (items or []):
            if not m.get("id"):
                continue
            # ⚠️ 同一作品在 combined_credits 里可能有多条(不同角色, 如《柯南秀》的
            # "Self - Guest" 与 "Self")→ 按 tmdb id 去重, 只留首条(TMDB 按主次排序),
            # 否则演员页同一作品出现两张一模一样的卡片(2026-09-22 用户实测)。
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            char = m.get("character") or m.get("role") or ""
            out.append(_norm_person_card(m, kind, char))
        out.sort(key=lambda c: c.get("year") or "", reverse=True)
        return out[:limit_per_kind]

    movies, tv = [], []
    for c in (data.get("cast") or []):
        mt = c.get("media_type")
        if mt == "movie":
            movies.append(c)
        elif mt == "tv":
            tv.append(c)
    return {
        "movies": _with_char(movies, "movie"),
        "tv": _with_char(tv, "tv"),
    }


def person_credits_sync(cfg, person_id, limit_per_kind=24):
    return _run_async(person_credits(cfg, person_id, limit_per_kind))


# ---------------------------------------------------------------------------
# 剧集分集(本地「分集级缺失」精确对比的核心数据)
# ---------------------------------------------------------------------------
async def season_episodes(cfg, tmdb_id, season_number):
    """某季每集: [{episode, name, air_date, overview}]。"""
    try:
        data = await _tmdb_get(cfg, f"/3/tv/{tmdb_id}/season/{season_number}",
                               {"language": _lang(cfg)})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    return [{
        "episode": e.get("episode_number"),
        "name": e.get("name") or "",
        "air_date": e.get("air_date") or "",
        "overview": (e.get("overview") or "")[:200],
    } for e in (data.get("episodes") or [])]


async def all_seasons(cfg, tmdb_id):
    """剧集全部季(含每季集数 + in_production)。用于缺失对比的"应有集"基准。"""
    try:
        data = await _tmdb_get(cfg, f"/3/tv/{tmdb_id}", {"language": _lang(cfg)})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    if not data:
        # 404 / 资源不存在: 返回空列表而非崩溃(上层据此判"无季信息",
        # 不应把整部剧误判为 PROCESSING)。
        return []
    out = []
    for s in (data.get("seasons") or []):
        out.append({
            "number": s.get("season_number"),
            "name": s.get("name") or "",
            "episodes": s.get("episode_count"),
            "air_date": s.get("air_date") or "",
            "in_production": bool(s.get("in_production")),
        })
    return out


def all_seasons_sync(cfg, tmdb_id):
    return _run_async(all_seasons(cfg, tmdb_id))


# ---------------------------------------------------------------------------
# 人物(演员筛选/详情)
# ---------------------------------------------------------------------------
async def person_details(cfg, person_id):
    try:
        data = await _tmdb_get(cfg, f"/3/person/{person_id}", {"language": _lang(cfg)})
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return None
    return data


async def movie_credits_person(cfg, kind, tmdb_id):
    """某人出演/参与的作品(用于演员筛选)。"""
    try:
        data = await _tmdb_get(cfg, f"/3/person/{tmdb_id}/combined_credits")
    except Exception as e:  # noqa: BLE001
        global last_error
        last_error = str(e)
        return []
    out = []
    for m in (data.get("movie") or []):
        if m.get("id"):
            out.append(_norm_search_card(m, "movie"))
    for t in (data.get("tv") or []):
        if t.get("id"):
            out.append(_norm_search_card(t, "tv"))
    return out
