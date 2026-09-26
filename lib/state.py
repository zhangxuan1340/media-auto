#!/usr/bin/env python3
"""队列状态管理: 在【项目根】的 state/queue.json 记录每个下载任务的进度。

位置固定 = MEDIA_AUTO_DIR(或 lib/config.project_root())/state/,与配置文件无关。

任务结构:
{
  "info_hash": "...",
  "magnet": "magnet:?xt=urn:btih:...",
  "title": "...",
  "content_type": "movie"|"tv",
  "language": "ja",
  "countries": ["JP"],
  "adult": false,
  "pushed_at": 169...,
  "cd2_status": "Pending|Downloading|Finished|Error",
  "downloaded_path": "/Offline/xxx.mkv",
  "target_folder": "JpKrMovie",
  "done": false,
  "finished_at": null
}
"""
import json
import os


def _state_dir():
    from .config import project_root  # noqa: PLC0415  避免模块级依赖
    sd = os.path.join(project_root(), 'state')
    os.makedirs(sd, exist_ok=True)
    return sd


def queue_path():
    return os.path.join(_state_dir(), 'queue.json')


def load_queue():
    p = queue_path()
    if not os.path.exists(p):
        return []
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_queue(data):
    p = queue_path()
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _dedup_key(t):
    # 有 info_hash 用 hash 去重;没有(hash 解析失败)用完整 magnet 去重,
    # 避免多个"hash=None"的任务被误判为重复而只留一条。
    ih = t.get('info_hash')
    if ih:
        return ('ih', str(ih).lower())
    return ('mg', t.get('magnet', ''))


def add_task(task):
    q = load_queue()
    key = _dedup_key(task)
    if any(_dedup_key(t) == key for t in q):
        return False
    q.append(task)
    save_queue(q)
    return True


def update_task(info_hash, **fields):
    q = load_queue()
    for t in q:
        if t.get('info_hash') == info_hash:
            t.update(fields)
            save_queue(q)
            return True
    return False


def find_task(info_hash):
    for t in load_queue():
        if t.get('info_hash') == info_hash:
            return t
    return None
