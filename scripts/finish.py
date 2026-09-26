#!/usr/bin/env python3
"""
media-auto / finish —— Jellyfin 刷新
===============================================================
下载并归位完成后触发 Jellyfin 媒体库刷新, 让新文件入列。

tinyMediaManager 已停用(2026-09-26): 刮削/NFO 全部由 scripts/organize.py
自带生成, 不再调用 tMM; 旧参数仍被接受但无实际作用, 保持命令行兼容。

用法:
  python3 scripts/finish.py            # 刷新 Jellyfin
  python3 scripts/finish.py --no-refresh
"""
import argparse
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
import classify  # noqa: E402


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
    ap = argparse.ArgumentParser(description='Jellyfin 刷新(tMM 已停用, NFO 由 organize 自带生成)')
    ap.add_argument('--all', action='store_true', help='兼容旧参数, 无实际作用')
    ap.add_argument('--movie', action='store_true', help='兼容旧参数, 无实际作用')
    ap.add_argument('--tv', action='store_true', help='兼容旧参数, 无实际作用')
    ap.add_argument('--refresh-only', action='store_true', help='兼容旧参数')
    ap.add_argument('--no-scrape', action='store_true', help='兼容旧参数(tMM 已停用)')
    ap.add_argument('--no-refresh', action='store_true', help='不刷新 Jellyfin')
    args = ap.parse_args()

    config = classify.load_config()
    if not args.no_refresh:
        refresh_jellyfin(config)


if __name__ == '__main__':
    main()
