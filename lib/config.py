#!/usr/bin/env python3
"""共享配置加载(脚本与 server 共用)

config.json 默认位于项目根目录(本文件的上两级),可通过环境变量覆盖:
  - MEDIA_AUTO_DIR:     项目根目录
  - MEDIA_AUTO_CONFIG:  配置文件完整路径
"""
import json
import os


def project_root():
    # lib/ -> 项目根目录
    return os.environ.get("MEDIA_AUTO_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def config_path():
    return os.environ.get("MEDIA_AUTO_CONFIG", os.path.join(project_root(), "config.json"))


def load_config(path=None):
    p = path or config_path()
    if not os.path.exists(p):
        return {}
    try:
        return json.loads(open(p, "r", encoding="utf-8").read())
    except Exception:
        return {}
