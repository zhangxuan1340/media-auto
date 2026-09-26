#!/usr/bin/env python3
"""
media-auto / diao_search —— 从 Bitmagnet-Next-Web 站点提取种子信息
=================================================================
这一类站点(如 https://your-site.example.com)是社区改版的 Bitmagnet 前端(Bitmagnet-Next-Web,
Next.js + 直连 Postgres + pg_trgm 索引),对外暴露的是一套 **REST 接口**,
而不是原生 Bitmagnet 的 GraphQL(/graphql)。二者是两套东西,分开配置:

  原生 Bitmagnet        : 配置段 "bitmagnet"            (GraphQL, 带 seeders + TMDB 分类元数据)
  Bitmagnet-Next-Web    : 配置段 "bitmagnet_next_web"   (REST, 只有 hash/name/size/magnet/files)

站点接口(无需鉴权,无需特殊 UA):
  GET {base}/api/search?keyword=<关键词>&offset=<跳条数>&limit=<每页数>   # limit 最大 10,超过 400
    -> {"data":{"keywords":[...],"torrents":[{hash,name,size,magnet_uri,single_file,
        files_count,files[{index,path,size,extension}],created_at,updated_at}],
        "total_count":<int|null>,"has_more":<bool>},"message":"success","status":200}

  ⚠️ 分页坑(2026-09 实测 your-site.example.com):
    - `page` 参数**被站点忽略**(page=1/2/5 返回完全相同的前 10 条)——只能用 `offset`。
    - `has_more` **恒为 true**(翻过实际结果数也不变)——不可信,用"本页不满 limit 条"判断到底。
    - `total_count` 恒为 null。
  本模块 collect() 已封装好 offset 翻页 + hash 去重,外部不用关心这些坑。
  GET {base}/api/detail?hash=<info_hash>  -> {"data":{<单个 torrent>}}
  GET {base}/api/stats                    -> {"data":{size,updated_at,total_count,...}}

地址优先级: 命令行 --base > 配置的 bitmagnet_next_web.base > 环境变量 DIAO_BASE > 内置默认。

用法:
  python3 scripts/diao_search.py --query "求救信号 2026"
  python3 scripts/diao_search.py --query "Mayday" --limit 30          # 自动按每页 10 翻页
  python3 scripts/diao_search.py --query "Mayday" --limit 30 --json   # 输出 JSON
  python3 scripts/diao_search.py --query "Mayday" --all               # 翻完全部页(最多 20 页)
  python3 scripts/diao_search.py --query "Mayday" --magnet-only        # 只要磁力链(可喂给 push.py / CD2)
  python3 scripts/diao_search.py --query "Mayday" --hash-prefix 0879f776 --hash-prefix 3f7573eb
  python3 scripts/diao_search.py --query "Mayday" --min-size 5000000000
  python3 scripts/diao_search.py --detail 0879f776e948ce1f93d026fa3601aea2dc770ad3
  python3 scripts/diao_search.py --stats
  python3 scripts/diao_search.py --base https://另一个同类站点 --query "Mayday"   # 临时换站点

依赖: 仅 Python 标准库(urllib);配置复用项目 lib.config(只读数据库)。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 允许直接 `python3 scripts/diao_search.py` 运行:把项目根加入 sys.path 以复用 lib.config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import httputil  # noqa: E402
try:
    from lib.config import load_config
except Exception:  # 兜底:脱离项目目录时仍可跑
    def load_config(path=None):
        return {}

CONFIG_KEY = "bitmagnet_next_web"
DEFAULT_BASE = "https://your-site.example.com"
UA = os.environ.get("DIAO_UA", "media-auto/1.0")
PAGE_SIZE = 10          # 站点硬上限: limit 最大 10,超过 400
MAX_PAGES = 20          # --all / 翻页上限,防止无限翻


def resolve_settings(cfg, cli_base=None, cli_limit=None):
    """按优先级解析 base 与 limit: 命令行 > 数据库配置 > 环境变量 > 内置默认。"""
    nw = (cfg.get(CONFIG_KEY) or {})
    base = cli_base or nw.get("base") or os.environ.get("DIAO_BASE") or DEFAULT_BASE
    try:
        limit = int(cli_limit or nw.get("limit") or 0) or PAGE_SIZE
    except Exception:
        limit = PAGE_SIZE
    return base, limit


def _no_proxy_opener():
    # ⚠️ 不走系统 HTTP 代理: Bitmagnet 源(内网 IP 或 your-site.example.com)被塞进系统代理会 502/连不上
    # (与 clouddrive 客户端 _clean_env、tmdb 客户端 trust_env=False 同一类坑)。
    return httputil.no_proxy_opener()


def _get(base, path, params=None, timeout=30):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with _no_proxy_opener().open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError(f"返回不是 JSON(可能被拦截): {raw[:200]}")


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _norm(t):
    """把站点原始 torrent 规整成统一字段。"""
    return {
        "hash": t.get("hash", ""),
        "name": t.get("name", ""),
        "size": t.get("size", 0),
        "size_text": human_size(t.get("size", 0)),
        "magnet": t.get("magnet_uri", ""),
        "single_file": t.get("single_file"),
        "files_count": t.get("files_count", 0),
        "files": t.get("files") or [],
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }


def search(base, keyword, offset=0, limit=PAGE_SIZE):
    """取一页(limit 压到 <=10, offset 跳条数)。返回 (torrents, has_more, total_count, keywords)。

    ⚠️ 站点实测(2026-09): **`page` 参数被忽略**(page=1/2/5 返回完全相同的 10 条),
    真正生效的分页参数是 **`offset`**(与 limit 配合, 连续无重复)。
    且 `has_more` 恒为 true(即使翻过实际结果数), **不可信** —— 调用方必须
    用"本页返回条数 < limit"判断到底(见 collect)。
    """
    limit = max(1, min(int(limit or PAGE_SIZE), PAGE_SIZE))
    offset = max(0, int(offset or 0))
    data = _get(base, "/api/search", {"keyword": keyword, "offset": offset, "limit": limit})
    d = data.get("data") or {}
    torrents = [_norm(t) for t in (d.get("torrents") or [])]
    return torrents, bool(d.get("has_more")), d.get("total_count"), (d.get("keywords") or [])


def collect(base, keyword, want=PAGE_SIZE, start_offset=0, max_pages=MAX_PAGES):
    """按 offset 翻页收集最多 want 条。
    返回 (torrents, total_count, keywords, has_more, end_offset)。

    has_more = 最后一页是否"满页"(满页 = 后面很可能还有; 不满/空 = 到底)。
    end_offset = 实际翻到的 offset, 供调用方续翻(换算下一页)。
    按 hash 去重(站点排序在两次请求间可能微调, 去重保证续翻不重复)。
    """
    out, seen, offset, tc, kw, full = [], set(), int(start_offset or 0), None, None, False
    for _ in range(max_pages):
        if len(out) >= want:
            break
        chunk, _more, tc, kw = search(base, keyword, offset=offset, limit=PAGE_SIZE)
        for t in chunk:
            if t["hash"] and t["hash"] in seen:
                continue
            if t["hash"]:
                seen.add(t["hash"])
            out.append(t)
        if not chunk:
            break  # 空页 = 到底
        full = len(chunk) >= PAGE_SIZE
        offset += len(chunk)
        if not full:
            break  # 不满页 = 到底
    return out[:want], tc, kw, (full and len(out) >= want), offset


def detail(base, info_hash):
    data = _get(base, "/api/detail", {"hash": info_hash})
    t = (data.get("data") or {})
    return _norm(t) if t else None


def stats(base):
    return (_get(base, "/api/stats").get("data") or {})


def _filter(items, hash_prefixes, min_size, max_size):
    if hash_prefixes:
        pf = tuple(p.lower() for p in hash_prefixes)
        items = [t for t in items if t["hash"].lower().startswith(pf)]
    if min_size:
        items = [t for t in items if (t.get("size") or 0) >= min_size]
    if max_size:
        items = [t for t in items if (t.get("size") or 0) <= max_size]
    return items


def main():
    ap = argparse.ArgumentParser(description="从 Bitmagnet-Next-Web 站点提取种子信息")
    ap.add_argument("--query", "-q", help="搜索关键词")
    ap.add_argument("--base", default=None,
                    help=f"站点根地址(默认取配置 {CONFIG_KEY}.base,再退到 {DEFAULT_BASE})")
    ap.add_argument("--page", type=int, default=1, help="起始页码(默认 1)")
    ap.add_argument("--limit", type=int, default=None,
                    help=f"想要的结果条数(默认取配置 {CONFIG_KEY}.limit,再退到 {PAGE_SIZE};内部按每页 10 翻页)")
    ap.add_argument("--all", action="store_true", help=f"翻完全部页(最多 {MAX_PAGES} 页)")
    ap.add_argument("--detail", metavar="HASH", help="查单个 info_hash 的详情")
    ap.add_argument("--stats", action="store_true", help="查看站点索引规模")
    ap.add_argument("--hash-prefix", action="append", default=[], help="只保留 hash 前缀命中的(可重复)")
    ap.add_argument("--min-size", type=int, default=0, help="最小字节数过滤")
    ap.add_argument("--max-size", type=int, default=0, help="最大字节数过滤")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--magnet-only", action="store_true", help="只输出磁力链")
    args = ap.parse_args()

    cfg = load_config()
    base, limit = resolve_settings(cfg, args.base, args.limit)

    try:
        if args.stats:
            print(json.dumps(stats(base), ensure_ascii=False, indent=2))
            return

        if args.detail:
            t = detail(base, args.detail)
            if not t:
                print("未找到该 hash", file=sys.stderr)
                sys.exit(2)
            print(json.dumps(t, ensure_ascii=False, indent=2) if args.json
                  else f"{t['hash']}\n{t['name']}\n{t['size_text']}\t{t['files_count']} 个文件\n{t['magnet']}")
            return

        if not args.query:
            ap.error("需要 --query,或使用 --detail / --stats")

        start_offset = max(0, int(args.page or 1) - 1) * PAGE_SIZE  # --page 按 10 条/页折算 offset
        if args.all:
            items, total, keywords, has_more, _end = collect(
                base, args.query, want=PAGE_SIZE * MAX_PAGES, start_offset=start_offset)
        else:
            items, total, keywords, has_more, _end = collect(
                base, args.query, want=max(1, limit), start_offset=start_offset)

        items = _filter(items, args.hash_prefix, args.min_size, args.max_size)

        if args.json:
            print(json.dumps({
                "base": base, "query": args.query, "keywords": keywords,
                "total_count": total, "has_more": has_more, "count": len(items),
                "torrents": items,
            }, ensure_ascii=False, indent=2))
            return

        if args.magnet_only:
            for t in items:
                if t["magnet"]:
                    print(t["magnet"])
            return

        if not items:
            print("无结果(或被过滤掉)", file=sys.stderr)
            return
        for i, t in enumerate(items, 1):
            print(f"{i:>2}. [{t['size_text']}] {t['name']}")
            print(f"    hash: {t['hash']}")
            if t["magnet"]:
                print(f"    magnet: {t['magnet']}")
        print(f"\n共 {len(items)} 条"
              + (f",站点报告 total_count={total}" if total is not None else "")
              + (", 还有更多(has_more=true)" if has_more else "")
              + f"  [源: {base}]")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
