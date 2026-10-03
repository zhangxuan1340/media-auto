#!/usr/bin/env python3
"""
media-auto / jackett_search —— Jackett 种子聚合引擎搜索
=========================================================
Jackett 是一个索引器聚合代理: 你在 Jackett 里配好一堆站点(公开/半私有/私有),
它对外统一暴露一套 Torznab 接口。这里查它的 **Torznab** 端点(官方文档明确、
`?apikey=` 鉴权、支持 `all` 聚合),把结果规整成与 bitmagnet / bitmagnet_next_web
一致的结构(hash/name/size/seeders/leechers/magnet),供 server/routers/search.py
作为第 3 个磁力源并入多源搜索。

Torznab 端点:
  GET {base}/api/v2.0/indexers/{indexer}/results/torznab?apikey=<key>&t=search&q=<query>&limit=<n>

  - {indexer} 默认 `all`(一次聚合 Jackett 里所有已配置站, 上限 1000 条);
    也可填具体站名(如 `thepiratebay`)或 filter 表达式(如 `type:public+lang:cn`)。
  - 返回 `<rss><channel><item>...</item></channel></rss>`; 每个 item:
    <title> 名称 / <link> 或 <guid> 磁力(magnet:?...) / <size> 字节 /
    <seeders> <leechers> / <category> / <description>。

地址/密钥优先级: 配置的 jackett.base / jackett.apikey(命令行 --base/--key 可临时覆盖)。

用法:
  python3 scripts/jackett_search.py --query "盗梦空间 2010"
  python3 scripts/jackett_search.py --query "Inception" --limit 30 --json
  python3 scripts/jackett_search.py --query "..." --indexer all   # 指定索引器

依赖: 仅 Python 标准库(urllib + xml.etree);不走系统代理(内网 Jackett 同 bitmagnet)。
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# 允许直接 `python3 scripts/jackett_search.py` 运行:把项目根加入 sys.path 以复用 lib
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from lib import httputil  # noqa: E402
try:
    from lib.config import load_config
except Exception:  # 兜底:脱离项目目录时仍可跑
    def load_config(path=None):
        return {}

CONFIG_KEY = "jackett"
DEFAULT_INDEXER = "all"
UA = os.environ.get("JACKETT_UA", "media-auto/1.0")
_HASH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{32,40})", re.I)


def resolve_settings(cfg, cli_base=None, cli_key=None, cli_indexer=None):
    """解析 (base, apikey, indexer, limit)。命令行 > 数据库配置 > 内置默认。"""
    j = (cfg or {}).get(CONFIG_KEY) or {}
    base = str(cli_base or j.get("base") or os.environ.get("JACKETT_BASE") or "").strip().rstrip("/")
    apikey = str(cli_key or j.get("apikey") or os.environ.get("JACKETT_API_KEY") or "").strip()
    indexer = str(cli_indexer or j.get("indexer") or DEFAULT_INDEXER).strip() or DEFAULT_INDEXER
    try:
        limit = int(j.get("limit") or 30)
    except Exception:
        limit = 30
    return base, apikey, indexer, max(1, limit)


def _opener():
    # 不走系统代理: Jackett 常在内网, 被系统代理拦截会 502/连不上
    return httputil.no_proxy_opener()


def parse_info_hash(magnet):
    """从磁力链提取 infoHash(与 scripts/search.py::parse_info_hash 同口径)。"""
    m = _HASH_RE.search(magnet or "")
    return m.group(1).lower() if m else None


def _xtag(item, tag):
    """取 item 下某子元素文本(兼容默认命名空间与 torznab 命名空间前缀)。"""
    el = item.find(tag)
    if el is not None and (el.text or "").strip():
        return el.text.strip()
    for ch in item:
        if isinstance(ch.tag, str) and ch.tag.split("}")[-1] == tag and (ch.text or "").strip():
            return ch.text.strip()
    return ""


def _to_int(s):
    try:
        return int(str(s).replace(",", "").strip() or 0)
    except Exception:
        return None


def _extract_magnet(item):
    """磁力链: 优先 <link>/<guid> 里的 magnet:, 否则扫描所有子元素找 magnet: 开头者。"""
    for tag in ("link", "guid"):
        v = _xtag(item, tag)
        if v.startswith("magnet:"):
            return v
    for ch in item:
        if (ch.text or "").strip().startswith("magnet:"):
            return ch.text.strip()
    return ""


def search(cfg, query, limit=30):
    """按 query 搜 Jackett(Torznab), 返回统一 item 列表(与 bitmagnet 同结构)。

    每个 item: {hash, name, size, seeders, leechers, magnet, indexer}。
    只保留能取到磁力链的条目(无磁力/只有 .torrent 下载的丢弃, 我们靠磁力推 CD2)。
    """
    base, apikey, indexer, _lim = resolve_settings(cfg)
    if not base:
        raise RuntimeError("Jackett 未配置地址: 管理 → 通用 → 磁力搜索源 → Jackett 地址")
    url = f"{base}/api/v2.0/indexers/{urllib.parse.quote(indexer, safe='')}/results/torznab"
    params = {"apikey": apikey, "t": "search", "q": query, "limit": max(1, int(limit))}
    url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Accept": "application/xml, application/json, */*"})
    try:
        with _opener().open(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jackett HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Jackett 请求失败: {e}")

    # Torznab 是 XML; 若站点/反代误给 JSON(极少见) → 当作错误
    head = raw[:1].lstrip()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise RuntimeError(f"Jackett 返回不是 Torznab XML: {raw[:160]!r}")
    items = root.findall(".//item")
    out = []
    for it in items:
        title = _xtag(it, "title")
        magnet = _extract_magnet(it)
        if not magnet:
            continue   # 无磁力链的条目无法推送 CD2(见 docstring), 直接丢弃
        size = _to_int(_xtag(it, "size"))
        out.append({
            "hash": parse_info_hash(magnet) or "",
            "name": title,
            "size": size if size else 0,
            "seeders": _to_int(_xtag(it, "seeders")),
            "leechers": _to_int(_xtag(it, "leechers")),
            "magnet": magnet,
            "indexer": _xtag(it, "description") or "",
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Jackett 种子聚合引擎搜索")
    ap.add_argument("--query", "-q", required=True, help="搜索关键词")
    ap.add_argument("--limit", type=int, default=None, help="返回条数(默认取配置 jackett.limit)")
    ap.add_argument("--base", default=None, help="Jackett 根地址(覆盖配置)")
    ap.add_argument("--key", dest="key", default=None, help="API Key(覆盖配置)")
    ap.add_argument("--indexer", default=None, help="索引器, 默认 all")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()

    cfg = load_config()
    # CLI 覆盖配置值(注入配置段, 让 search() 内部 resolve_settings 用命令行值)
    if args.base or args.key or args.indexer:
        cfg = dict(cfg)
        j = dict(cfg.get(CONFIG_KEY) or {})
        if args.base:
            j["base"] = args.base
        if args.key:
            j["apikey"] = args.key
        if args.indexer:
            j["indexer"] = args.indexer
        cfg[CONFIG_KEY] = j
    base, apikey, indexer, lim = resolve_settings(cfg)
    limit = args.limit or lim
    try:
        items = search(cfg, args.query, limit)
    except Exception as e:  # noqa: BLE001
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({"base": base, "indexer": indexer, "count": len(items),
                          "results": items}, ensure_ascii=False, indent=2))
        return
    if not items:
        print("无结果(或该索引器无磁力)", file=sys.stderr)
        return
    for i, t in enumerate(items, 1):
        print(f"{i:>2}. [{t['seeders']}↑/{t['leechers']}↓] {t['name']}")
        if t["magnet"]:
            print(f"     磁力: {t['magnet']}")
    print(f"\n共 {len(items)} 条  [源: {base} / {indexer}]")


if __name__ == "__main__":
    main()
