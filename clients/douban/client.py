"""豆瓣联想搜索客户端(取国内译名)

为什么需要它: TMDB 的 zh-CN 标题常常是**空的** —— 实测 Bad Sisters(tmdb 199318):
`?language=zh-CN` 返回英文名, translations 里大陆那条 name 为空, 只有台译「不良姐妹」,
而整理/命名/NFO 要的正是国内叫法「坏姐妹」。豆瓣没有官方 API, 只有 subject_suggest
这个联想接口还比较稳(必须带浏览器 UA + Referer, 否则 Access denied)。

⚠️ 只读不缓存: 结果由 lib/titles.resolve 落进 tmdb_media(title + title_checked),
    每条作品最多查一次; 网络失败不算"查过", 下次同步会重试。
"""
import httpx

_URL = "https://movie.douban.com/j/subject_suggest"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 进程内最近一次错误(排障用, 不参与业务判断)
last_error: str = ""


def suggest(query: str, timeout: float = 10.0) -> list:
    """联想搜索, 返回原始条目列表。

    条目形如: {id, title, sub_title, year, episode, type, img, url}
      title     中文名(剧集带季后缀, 如 "坏姐妹 第一季")
      sub_title 原名(英文/原文), 是匹配最可靠的信号
    失败(网络/反爬/格式异常)一律抛异常, 由调用方区分「没有」和「查不了」。
    """
    global last_error
    q = (query or "").strip()
    if not q:
        return []
    # trust_env=False: 不吃系统 http_proxy(内网地址进代理会 502, 与 TMDB 客户端一致)
    with httpx.Client(trust_env=False, timeout=timeout,
                      headers={"User-Agent": _UA,
                               "Referer": "https://movie.douban.com/",
                               "Accept-Language": "zh-CN,zh;q=0.9"}) as client:
        resp = client.get(_URL, params={"q": q})
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        last_error = f"豆瓣返回格式异常: {type(data).__name__}"
        raise RuntimeError(last_error)
    return data
