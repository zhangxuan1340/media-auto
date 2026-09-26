#!/usr/bin/env python3
"""
media-auto / finish —— TinyMediaManager 刮削重命名 + Jellyfin 刷新
===============================================================
下载并归位完成后:
  1. 跑 tMM 命令行刮削(更新数据源→刮削新增→重命名归位)
  2. 触发 Jellyfin 媒体库刷新,让新文件入列

用法:
  python3 scripts/finish.py --all            # 电影+剧集都刮,然后刷新 Jellyfin
  python3 scripts/finish.py --movie --no-refresh
  python3 scripts/finish.py --refresh-only
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
import classify  # noqa: E402


def run_tmm(config, which):
    tmm = config.get('tinymediamanager', {})
    exec_tmpl = tmm.get('docker_exec', 'docker exec -i tinymediamanager /app/tinyMediaManager')
    if which == 'movie':
        cmd_suffix = tmm.get('movie_cmd', 'movie -u -n -r')
    else:
        cmd_suffix = tmm.get('tv_cmd', 'tvshow -u -n -r')
    cmd = f'{exec_tmpl} {cmd_suffix}'
    print(f'[tMM] {cmd}')
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print('  ! tMM 超时(>30min)')
        return False
    if proc.returncode != 0:
        print(f'  ! tMM 返回非零: {proc.stderr.strip()[:500]}')
        return False
    print('  ✓ tMM 完成')
    return True


def refresh_jellyfin(config):
    jf = config.get('jellyfin', {})
    url = jf.get('url', '').rstrip('/')
    token = jf.get('token', '')
    library_id = jf.get('library_id', '')
    if library_id:
        endpoint = f'{url}/Items/{library_id}/Refresh'
    else:
        endpoint = f'{url}/Library/Refresh'
    # ⚠️ Jellyfin 12.x 已不接受 X-Emby-Token(一律 401),必须用标准 Emby 头。
    req = urllib.request.Request(
        endpoint, data=b'{}', method='POST',
        headers={'Authorization': f'MediaBrowser Token="{token}"',
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f'  ✓ Jellyfin 刷新已触发 ({resp.status}): {endpoint}')
            return True
    except urllib.error.URLError as e:
        print(f'  ! Jellyfin 刷新失败: {e}')
        return False


def main():
    ap = argparse.ArgumentParser(description='tMM 刮削 + Jellyfin 刷新')
    ap.add_argument('--all', action='store_true', help='电影+剧集')
    ap.add_argument('--movie', action='store_true')
    ap.add_argument('--tv', action='store_true')
    ap.add_argument('--refresh-only', action='store_true', help='只刷 Jellyfin,不跑 tMM')
    ap.add_argument('--no-scrape', action='store_true')
    ap.add_argument('--no-refresh', action='store_true')
    args = ap.parse_args()

    config = classify.load_config()

    if not args.refresh_only and not args.no_scrape:
        if args.all or args.movie:
            run_tmm(config, 'movie')
        if args.all or args.tv:
            run_tmm(config, 'tv')
    elif args.refresh_only:
        pass

    if not args.no_refresh:
        refresh_jellyfin(config)


if __name__ == '__main__':
    main()
