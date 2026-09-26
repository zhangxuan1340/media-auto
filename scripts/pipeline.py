#!/usr/bin/env python3
"""
media-auto / pipeline —— 一键全链路
==================================
搜索 → 选片 → 推离线 → 轮询完成+分类移动 → 刮削+刷新

用法:
  # 交互选片(列出结果,默认选种子最多的第 1 个)
  python3 scripts/pipeline.py --query "盗梦空间 2010"

  # 全自动: 选最优 + 等待下载完成 + 刮削刷新
  python3 scripts/pipeline.py --query "..." --auto --wait

  # 只搜索并推,不等待
  python3 scripts/pipeline.py --query "..." --auto --no-wait
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import search as bm_search  # noqa: E402
from clients.clouddrive import client as cd2  # noqa: E402
from lib import state, classify  # noqa: E402
from scripts import check as chk  # noqa: E402
from scripts import push  # noqa: E402  (build_task/parse_info_hash 与 Web 推送同一构造逻辑)


def push_selected(config, base, result, pick_meta=None):
    magnet = bm_search.magnet(result)
    content = result.get('content') or {}
    bm_type = (content.get('type') or '').upper()
    # 与 Web 推送(server/routers/cd2.py → scripts.push.build_task)共用同一任务构造,
    # 不再各写一份 task dict(字段/默认值漂移的隐患)。
    meta = {
        'title': (pick_meta or {}).get('title') or content.get('title') or result.get('name', ''),
        'content_type': bm_search.TYPE_MAP.get(bm_type, 'movie'),
        'language': (content.get('language') or '').lower(),
        'adult': bool((pick_meta or {}).get('adult', False)),
    }
    task = push.build_task(magnet, meta,
                           config.get('clouddrive2', {}).get('offline_root', '/Offline'))
    res = cd2.add_offline(config, magnet, task['to_folder'], base_dir=base)
    print('CD2 返回:', res)
    state.add_task(task)
    print(f'已入队: {task["title"]} ({task["content_type"]})')
    return task


def wait_loop(config, base, library_root, interval):
    print(f'=== 轮询下载(每 {interval}s) ===')
    while True:
        q = state.load_queue()
        pending = [t for t in q if not t.get('done')]
        if not pending:
            print('全部完成。')
            break
        for t in pending:
            chk.process_task(config, t, base, library_root)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description='media-auto 全链路')
    ap.add_argument('--query', required=True)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--pick', type=int, default=1, help='选第几个(从 1 开始)')
    ap.add_argument('--auto', action='store_true', help='直接选最优,不交互')
    ap.add_argument('--wait', dest='wait', action='store_true', help='等待下载完成后刮削刷新')
    ap.add_argument('--no-wait', dest='wait', action='store_false')
    ap.add_argument('--interval', type=int, default=120)
    ap.set_defaults(wait=False)
    args = ap.parse_args()

    config = classify.load_config()
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))   # 项目根
    library_root = config.get('library_root', '/media')

    results = bm_search.search(config, args.query, args.limit)
    if not results:
        print('未搜到结果。')
        return

    pick = 0 if args.auto else (args.pick - 1)
    if not args.auto:
        for i, r in enumerate(results[:10], 1):
            prev = bm_search.preview_classify(config, r)
            print(f'{i:>2}. [{r.get("seeders")}↑] {r.get("name")}  -> {prev["folder"]}')
        try:
            pick = int(input('选择序号(默认1): ') or '1') - 1
        except Exception:
            pick = 0
    pick = max(0, min(pick, len(results) - 1))
    chosen = results[pick]
    print(f'已选: {chosen.get("name")}')

    push_selected(config, base, chosen)

    if args.wait:
        wait_loop(config, base, library_root, args.interval)
        # 刮削 + 刷新
        import finish as fin
        print('=== 下载完成,开始刮削与刷新 ===')
        fin.run_tmm(config, 'movie')
        fin.run_tmm(config, 'tv')
        fin.refresh_jellyfin(config)


if __name__ == '__main__':
    main()
