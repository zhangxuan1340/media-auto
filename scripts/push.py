#!/usr/bin/env python3
"""
media-auto / push —— 推送到 CloudDrive2 离线下载
===============================================
把磁力链推给 CD2 离线下载,并把任务(含元数据)写入队列,供 check 阶段轮询与分类。

⚠️ 链接分隔铁律:每个链接【单独一次 AddOfflineFiles 调用】,绝不把多个链接拼进同一次。
   即使同一次调用里传了多个链接(用 --batch),也只用换行 \\n 连接,绝不用逗号/空格。
   这样可彻底避免"多个链接被当成一个 → 任务失败"。

用法:
  # 单个(推荐,最清晰)
  python3 scripts/push.py --magnet "magnet:?xt=urn:btih:XXXX" --title "..." --content-type movie --language ja

  # 一次传多个链接(每个仍单独一次调用,互不合并)
  python3 scripts/push.py --magnet "magnet:?xt=urn:btih:AAA" --magnet "magnet:?xt=urn:btih:BBB" --language en

  # 从文件读取(每行一个链接)
  python3 scripts/push.py --magnets-file magnets.txt --content-type tv

  # 把整段粘贴(含换行/逗号/空格混排)拆开,逐个推
  echo 'magnet:?xt=urn:btih:AAA,magnet:?xt=urn:btih:BBB' | python3 scripts/push.py --stdin
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clients.clouddrive import client as cd2  # noqa: E402
from lib import classify, state  # noqa: E402
from scripts import search as bm_search  # noqa: E402  (parse_info_hash 唯一实现)


def parse_info_hash(magnet):
    # 唯一实现在 scripts/search.py(parse_info_hash), 这里只是转发保持旧调用点不变
    return bm_search.parse_info_hash(magnet)


def parse_dn(magnet):
    """从 magnet 的 dn= 参数取一个可读标题(用于缺省标题)。"""
    m = re.search(r'[?&]dn=([^&]+)', magnet or '')
    if not m:
        return ''
    import urllib.parse
    return urllib.parse.unquote(m.group(1))


def build_task(magnet, meta, to_folder):
    info_hash = parse_info_hash(magnet)
    title = meta.get('title') or parse_dn(magnet) or (info_hash or '未知')
    return {
        'info_hash': info_hash,
        'magnet': magnet,
        'title': title,
        'content_type': meta.get('content_type', 'movie'),
        'language': (meta.get('language') or '').lower(),
        'countries': meta.get('countries', []),
        'adult': bool(meta.get('adult', False)),
        'to_folder': to_folder,
        'pushed_at': int(time.time()),
        'cd2_status': 'Pending',
        'downloaded_path': '',
        'target_folder': '',
        'done': False,
        'finished_at': None,
    }


def main():
    ap = argparse.ArgumentParser(description='推送到 CloudDrive2 离线下载')
    ap.add_argument('--magnet', action='append', default=[],
                    help='磁力链,可重复多次(每个单独一次调用)')
    ap.add_argument('--magnets-file', default='', help='每行一个磁力链的文本文件')
    ap.add_argument('--stdin', action='store_true', help='从 stdin 读取(纯链接文本/JSON)')
    ap.add_argument('--title', default='', help='标题(单个链接时直接用;多链接作为兜底)')
    ap.add_argument('--content-type', choices=['movie', 'tv'], default='movie')
    ap.add_argument('--language', default='')
    ap.add_argument('--countries', default='', help='逗号分隔的国家代码')
    ap.add_argument('--adult', action='store_true')
    ap.add_argument('--to-folder', default='', help='覆盖默认离线目录')
    ap.add_argument('--batch', action='store_true',
                    help='把多个链接用换行 \\n 一次调用推(默认每个链接单独调用)')
    args = ap.parse_args()

    config = classify.load_config()
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))   # 项目根

    # ---- 1. 收集原始链接(多来源) ----
    raw_links = list(args.magnet)
    if args.magnets_file:
        with open(args.magnets_file, 'r', encoding='utf-8') as f:
            raw_links.append(f.read())
    if args.stdin:
        data = sys.stdin.read()
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                raw_links.append(obj.get('magnet', ''))
                args.title = args.title or obj.get('title', '')
                args.content_type = obj.get('content_type', args.content_type)
                args.language = args.language or obj.get('language', '')
            elif isinstance(obj, list):
                for it in obj:
                    raw_links.append(it.get('magnet', '') if isinstance(it, dict) else str(it))
        except json.JSONDecodeError:
            raw_links.append(data)  # 当纯链接文本处理

    # ---- 2. 拆成干净的单个链接(去重/去空) ----
    magnets = cd2.split_urls(raw_links)
    if not magnets:
        print('ERROR: 未提供任何有效 magnet', file=sys.stderr)
        sys.exit(1)

    to_folder = args.to_folder or config.get('clouddrive2', {}).get('offline_root', '/Offline')
    meta = {
        'title': args.title,
        'content_type': args.content_type,
        'language': args.language,
        'countries': [c.strip().upper() for c in args.countries.split(',') if c.strip()],
        'adult': args.adult,
    }

    print(f'待推送链接数: {len(magnets)}  ->  {to_folder}')

    # ---- 3. 推送(默认每个链接单独一次调用,杜绝合并) ----
    if args.batch:
        # 批量模式:统一用换行连接,一次调用。仅在你确认 CD2 接受换行时使用。
        res = cd2.add_offline_batch(config, magnets, to_folder, base_dir=base)
        print('CD2 批量返回:', json.dumps(res, ensure_ascii=False))
        for mg in magnets:
            state.add_task(build_task(mg, meta, to_folder))
        print(f'已入队 {len(magnets)} 个任务(批量调用)')
        return

    ok = 0
    for i, mg in enumerate(magnets, 1):
        try:
            res = cd2.add_offline(config, mg, to_folder, base_dir=base)
            print(f'[{i}/{len(magnets)}] ✓ {mg[:50]}... -> CD2: {json.dumps(res, ensure_ascii=False)[:80]}')
            state.add_task(build_task(mg, meta, to_folder))
            ok += 1
        except Exception as e:
            print(f'[{i}/{len(magnets)}] ✗ 失败: {mg[:50]}... 原因: {e}', file=sys.stderr)
    print(f'完成: 成功 {ok}/{len(magnets)} 个已入队')


if __name__ == '__main__':
    main()
