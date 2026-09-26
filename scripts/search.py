#!/usr/bin/env python3
"""
media-auto / search —— Bitmagnet 磁力搜索
========================================
通过 Bitmagnet 的 GraphQL 接口搜索磁力,按 seeders 排序挑选最优,并给出落库分类预览。

用法:
  python3 scripts/search.py --query "盗梦空间 2010" [--limit 20]
  python3 scripts/search.py --query " spirited away" --json

依赖: 仅 Python 标准库 (urllib)。Bitmagnet 地址取配置的 bitmagnet.url
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

QUERY = """
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

# Bitmagnet 的 contentType 枚举(MOVIE/SERIES/...) -> 我们的 content_type
TYPE_MAP = {
    'MOVIE': 'movie', 'FILM': 'movie',
    'SERIES': 'tv', 'TV': 'tv', 'SHOW': 'tv',
}


def _no_proxy_opener():
    # ⚠️ 不走系统 HTTP 代理: Bitmagnet 是内网地址(172.16.x.x), 被系统代理拦截会 502/连不上
    return httputil.no_proxy_opener()


def parse_info_hash(magnet):
    """从磁力链提取 infoHash(全库唯一实现; push/pipeline 都从这里取)。"""
    m = re.search(r'urn:btih:([0-9a-fA-F]{32,40})', magnet or '')
    return m.group(1).lower() if m else None


def search(config, query, limit=20):
    url = config.get('bitmagnet', {}).get('url', 'http://localhost:3333/graphql')
    body = json.dumps({'query': QUERY, 'variables': {'q': query, 'limit': limit}}).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with _no_proxy_opener().open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise RuntimeError(f'Bitmagnet 请求失败: {e}')

    if 'errors' in data:
        raise RuntimeError(f'Bitmagnet GraphQL 错误: {data["errors"]}')

    results = data.get('data', {}).get('torrentContent', {}).get('results', [])
    # 按 seeders 降序
    results.sort(key=lambda r: int(r.get('seeders') or 0), reverse=True)
    return results


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
            'infoHash': best.get('infoHash'),
            'magnet': magnet(best),
            'name': best.get('name'),
        }, ensure_ascii=False))


if __name__ == '__main__':
    main()
