#!/usr/bin/env python3
"""
media-auto / check —— 轮询离线完成 + 分类 + 移动到媒体库
========================================================
对队列里未完成的任务:
  1. 调 CD2 ListAllOfflineFiles(账户级分页,快) 查任务状态
  2. 完成后用 GetSubFiles 找到下载好的媒体文件
  3. 用分类引擎决定目标目录
  4. MoveFile 移动到 library_root/<分类目录>

注意: 【不要】用 ListOfflineFilesByPath 查状态 —— 它一次性返回该目录下全部离线任务,
      任务量大时单次调用可达数十秒(实测 4769 条约 49s),轮询不可用。

用法:
  python3 scripts/check.py --once
  python3 scripts/check.py --loop --interval 120   # 每 120s 轮询
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clients.clouddrive import client as cd2  # noqa: E402
from lib import classify, state  # noqa: E402

MEDIA_EXT = ('.mkv', '.mp4', '.ts', '.avi', '.mov', '.m2ts', '.iso', '.m2ts')


def _collect_media_files(config, path, base_dir, depth=1):
    """列出 path 下(最多再深入一层目录)的所有媒体文件,返回 [{name, fullPathName, size}]。"""
    files = []
    try:
        items = cd2.get_subfiles(config, path, base_dir=base_dir)
    except Exception as e:
        print(f'  ! GetSubFiles 失败 {path}: {e}')
        return files
    for it in items:
        name = it.get('name', '')
        fpath = it.get('fullPathName') or f"{path.rstrip('/')}/{name}"
        # 用 isDirectory 判定目录;protojson 输出的是枚举名("Directory"/"File"),
        # 不能拿 fileType 跟数值 0/1 比(旧写法会漏掉目录)。
        if cd2.is_dir(it):
            if depth > 0:  # Directory, 再下一层
                files += _collect_media_files(config, fpath, base_dir, depth - 1)
        elif name.lower().endswith(MEDIA_EXT):
            files.append({'name': name, 'path': fpath, 'size': int(it.get('size') or 0)})
    return files


def _pick_best(files, title):
    if not files:
        return None
    if title:
        t = title.lower()
        # 文件名包含标题关键词的优先
        matched = [f for f in files if t[:8] in f['name'].lower()]
        if matched:
            return max(matched, key=lambda f: f['size'])
    return max(files, key=lambda f: f['size'])


def process_task(config, task, base_dir, library_root):
    ih = task.get('info_hash') or ''
    to_folder = task.get('to_folder') or config.get('clouddrive2', {}).get('offline_root', '/Offline')

    # 查任务状态: 用账户级分页接口(find_offline,按 addTime 倒序从最新往前扫),
    # 【不要】用 ListOfflineFilesByPath —— 它一次性返回整个目录的全部任务,任务多时约需数十秒。
    try:
        hit = cd2.find_offline(config, info_hash=ih, name_contains=task.get('title'),
                               base_dir=base_dir)
    except Exception as e:
        print(f'  ! 查询离线任务失败: {e}')
        return False

    if not hit:
        print(f'  - 任务未出现在离线列表: {task.get("title") or ih}')
        return False

    status = hit.get('status')
    if cd2.is_error(status):
        print(f'  ! 离线任务出错: {task.get("title")} (status={status})')
        state.update_task(ih, cd2_status='Error')
        return False
    if not cd2.is_finished(status):
        print(f'  ~ 下载中({cd2.status_text(status)}): {task.get("title")}')
        state.update_task(ih, cd2_status=str(status))
        return False

    # 完成 -> 找文件
    files = _collect_media_files(config, to_folder, base_dir)
    best = _pick_best(files, task.get('title'))
    if not best:
        print(f'  ! 未在 {to_folder} 找到媒体文件: {task.get("title")}')
        return False

    # 分类
    media = {
        'title': task.get('title', ''),
        'content_type': task.get('content_type', 'movie'),
        'language': task.get('language', ''),
        'countries': task.get('countries', []),
        'adult': task.get('adult', False),
        'filename': best['name'],
    }
    cls = classify.classify(media, config)
    folder = cls['folder']
    dest = f"{library_root.rstrip('/')}/{folder}"

    print(f'  ✓ 完成: {best["name"]} -> 分类 {folder} ({",".join(cls["reasons"])})')
    mac = config.get('clouddrive2', {}).get('move_across_clouds', True)
    try:
        cd2.move_file(config, best['path'], dest, move_across_clouds=mac, base_dir=base_dir)
    except Exception as e:
        print(f'  ! 移动失败: {e}')
        return False
    state.update_task(ih, cd2_status='Finished', downloaded_path=best['path'],
                      target_folder=folder, done=True, finished_at=int(time.time()))
    print(f'    已移动到 {dest}')
    return True


def main():
    ap = argparse.ArgumentParser(description='轮询离线完成并分类移动')
    ap.add_argument('--once', action='store_true', default=True)
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--interval', type=int, default=120)
    args = ap.parse_args()

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))   # 项目根
    config = classify.load_config()
    library_root = config.get('library_root', '/media')

    def run_once():
        q = state.load_queue()
        pending = [t for t in q if not t.get('done')]
        print(f'=== check @ {time.strftime("%Y-%m-%d %H:%M:%S")}  待处理 {len(pending)}/{len(q)} ===')
        for t in pending:
            process_task(config, t, base, library_root)

    run_once()
    if args.loop:
        while True:
            time.sleep(args.interval)
            run_once()


if __name__ == '__main__':
    main()
