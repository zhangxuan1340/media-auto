#!/usr/bin/env python3
"""
media-auto / search —— Bitmagnet 磁力搜索
========================================
通过 Bitmagnet 的 GraphQL 接口搜索磁力,按 seeders 排序挑选最优,并给出落库分类预览。

用法:
  python3 scripts/search.py --query "盗梦空间 2010" [--limit 20]
  python3 scripts/search.py --query " spirited away" --json

依赖: 仅 Python 标准库 (urllib)。Bitmagnet 地址取配置的 bitmagnet.url

⚠️ schema 自适应: Bitmagnet 不同版本 GraphQL 字段/参数名差异很大。
  - 新版:  torrentContent { search(input: {queryString, limit}) { items { title torrent{size magnetUri} content{...} } } }
  - 旧版:  torrentContent(search: {queryString, limit}) { results { name size magnetLink content{...} } }
  search() 首次调用时对每个 url 发一次 introspection 判定版本(进程级缓存),
  按版本选查询与字段解析, 归一化成同一结构返回, 调用方无感知。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
sys.path.insert(0, _ROOT)
import classify  # noqa: E402
from lib import httputil  # noqa: E402

# 旧版 Bitmagnet(torrentContent 带 search 参数, 返回 results)
QUERY_OLD = """
query ($q: String!, $limit: Int!) {
  torrentContent(search: {queryString: $q, limit: $limit}) {
    totalCount
    results {
      infoHash
      name
      size
      seeders
      leechers
      magnetLink
      content {
        type
        title
        releaseYear
        language
        overview
      }
    }
  }
}
"""

# 新版 Bitmagnet(torrentContent 无参, search 是子字段, 返回 items, size/magnet 在 torrent 下)
QUERY_NEW = """
query ($input: TorrentContentSearchQueryInput!) {
  torrentContent {
    search(input: $input) {
      items {
        infoHash
        title
        seeders
        leechers
        torrent { size magnetUri }
        content {
          type
          title
          releaseYear
          overview
          originalLanguage { name }
        }
      }
    }
  }
}
"""

# Bitmagnet 的 contentType 枚举 -> 我们的 content_type(大小写都覆盖)
TYPE_MAP = {
    'MOVIE': 'movie', 'FILM': 'movie', 'movie': 'movie',
    'SERIES': 'tv', 'TV': 'tv', 'SHOW': 'tv', 'tv_show': 'tv', 'tv': 'tv',
}

_dialect_cache = {}


def _no_proxy_opener():
    # ⚠️ 不走系统 HTTP 代理: Bitmagnet 是内网地址(172.16.x.x), 被系统代理拦截会 502/连不上
    return httputil.no_proxy_opener()


def parse_info_hash(magnet):
    """从磁力链提取 infoHash(全库唯一实现; push/pipeline 都从这里取)。"""
    m = re.search(r'urn:btih:([0-9a-fA-F]{32,40})', magnet or '')
    return m.group(1).lower() if m else None


def _detect_dialect(url):
    """判定某 Bitmagnet 端点的 GraphQL 版本('new'/'old'), 进程级缓存, 每 url 只探一次。"""
    if url in _dialect_cache:
        return _dialect_cache[url]
    intro = ('{ q: __type(name: "Query") { fields { name args { name } } } '
             'tc: __type(name: "TorrentContentQuery") { fields { name } } }')
    req = urllib.request.Request(
        url, data=json.dumps({'query': intro}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with _no_proxy_opener().open(req, timeout=15) as resp:
            d = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Bitmagnet schema 探测失败: HTTP {e.code}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Bitmagnet schema 探测失败: {e}')
    data = d.get('data') or {}
    tc_fields = {f.get('name') for f in (data.get('tc') or {}).get('fields') or []}
    kind = 'unknown'
    for f in data.get('q', {}).get('fields') or []:
        if f.get('name') == 'torrentContent' and any(
                a.get('name') == 'search' for a in f.get('args') or []):
            kind = 'old'
            break
    if kind == 'unknown':
        kind = 'new' if 'search' in tc_fields else 'unknown'
    _dialect_cache[url] = kind
    return kind


def _normalize(info_hash, name, size, seeders, leechers, magnet, content, language):
    """把不同版本的字段归一化成统一结构(调用方与 preview_classify 都按这个读)。"""
    content = dict(content or {})
    if language and not content.get('language'):
        content['language'] = language
    return {
        'infoHash': info_hash,
        'name': name,
        'size': size,
        'seeders': seeders,
        'leechers': leechers,
        'magnetLink': magnet or (f'magnet:?xt=urn:btih:{info_hash}' if info_hash else ''),
        'content': content,
    }


def search(config, query, limit=20, content_type=None):
    url = config.get('bitmagnet', {}).get('url', 'http://localhost:3333/graphql')
    kind = _detect_dialect(url)
    if kind == 'new':
        # 新版 schema 支持 facets 过滤(按 contentType 消噪); content_type 传 'movie'/'tv_show' 等
        inp = {'queryString': query, 'limit': limit}
        if content_type:
            inp['facets'] = {'contentType': {'filter': content_type}}
        gql, variables = QUERY_NEW, {'input': inp}
    elif kind == 'old':
        gql, variables = QUERY_OLD, {'q': query, 'limit': limit}
    else:
        raise RuntimeError('Bitmagnet schema 无法识别(torrentContent 结构未知), 请确认版本')

    body = json.dumps({'query': gql, 'variables': variables}).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with _no_proxy_opener().open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f'Bitmagnet 请求失败: HTTP {e.code} {detail}'.strip())
    except urllib.error.URLError as e:
        raise RuntimeError(f'Bitmagnet 请求失败: {e}')

    if 'errors' in data:
        raise RuntimeError(f'Bitmagnet GraphQL 错误: {data["errors"]}')

    raw = data.get('data') or {}
    out = []
    if kind == 'new':
        items = (raw.get('torrentContent') or {}).get('search', {}).get('items') or []
        for it in items:
            t = it.get('torrent') or {}
            c = it.get('content') or {}
            lang = (c.get('originalLanguage') or {}).get('name') or ''
            out.append(_normalize(
                it.get('infoHash'), it.get('title'), t.get('size'),
                it.get('seeders'), it.get('leechers'), t.get('magnetUri'), c, lang))
    else:
        results = (raw.get('torrentContent') or {}).get('results') or []
        for r in results:
            c = r.get('content') or {}
            out.append(_normalize(
                r.get('infoHash'), r.get('name'), r.get('size'),
                r.get('seeders'), r.get('leechers'), r.get('magnetLink'), c, c.get('language')))

    for o in out:
        o['content']['type'] = (o['content'].get('type') or '').upper()
    # 按 seeders 降序
    out.sort(key=lambda r: int(r.get('seeders') or 0), reverse=True)
    return out


def magnet(result):
    if result.get('magnetLink'):
        return result['magnetLink']
    ih = result.get('infoHash')
    if ih:
        return f'magnet:?xt=urn:btih:{ih}'
    return ''


def preview_classify(config, result):
    content = result.get('content') or {}
    bm_type = (content.get('type') or '').upper()
    ct = TYPE_MAP.get(bm_type, 'movie')
    lang = (content.get('language') or '').lower()
    media = {
        'title': content.get('title') or result.get('name', ''),
        'content_type': ct,
        'language': lang,
        'genres': [],
        'countries': [],
        'filename': result.get('name', ''),
    }
    return classify.classify(media, config)


def main():
    ap = argparse.ArgumentParser(description='Bitmagnet 磁力搜索')
    ap.add_argument('--query', required=True)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--json', action='store_true', help='输出完整 JSON')
    args = ap.parse_args()

    config = classify.load_config()
    results = search(config, args.query, args.limit)

    if args.json:
        out = []
        for r in results:
            out.append({
                'infoHash': r.get('infoHash'),
                'name': r.get('name'),
                'size': r.get('size'),
                'seeders': r.get('seeders'),
                'leechers': r.get('leechers'),
                'magnet': magnet(r),
                'preview': preview_classify(config, r),
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f'命中 {len(results)} 条,按种子数排序(取前 {min(10, len(results))}):\n')
    for i, r in enumerate(results[:10], 1):
        prev = preview_classify(config, r)
        print(f'{i:>2}. [{r.get("seeders")}↑/{r.get("leechers")}↓] {r.get("name")}')
        print(f'     大小:{r.get("size")}  落库预览:{prev["folder"]}  ({",".join(prev["reasons"])})')
        print(f'     磁力:{magnet(r)}')
    if results:
        best = results[0]
        print('\n最优(种子最多):')
        print(json.dumps({
            'infoHash': best['infoHash'],
            'magnet': magnet(best),
            'name': best.get('name'),
        }, ensure_ascii=False))


if __name__ == '__main__':
    main()
