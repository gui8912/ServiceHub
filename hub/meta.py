"""版本号与更新时间 —— 服务卡片和列表要显示的两列信息。

取值优先级（不猜、不编，取不到就老实显示 —）：
  版本号    services.json 的 version  >  package.json 的 version  >  version.txt  >  —
  更新时间  services.json 的 updated  >  工作目录里最新一次文件改动时间  >  可执行文件时间  >  —

"更新时间"用目录内文件的最新修改时间，含义是"这个工具最后一次被改动是什么时候"，
比手动维护日期可靠——改了代码不用记得回来改日期。
"""
import json
import os
from datetime import datetime

SCAN_LIMIT = 800          # 一个目录最多看 800 个文件，防止超大目录拖慢启动


def _fmt(stamp):
    if not stamp:
        return ''
    try:
        return datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')
    except Exception:
        return ''


def _newest_mtime(folder):
    """目录内（不含子目录）文件的最新修改时间。"""
    newest = 0.0
    try:
        with os.scandir(folder) as entries:
            for idx, entry in enumerate(entries):
                if idx >= SCAN_LIMIT:  # modified: 原来 > 会多看一个，改成 >= 与注释一致
                    break
                try:
                    if entry.is_file(follow_symlinks=False):
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                        if mtime > newest:
                            newest = mtime
                except Exception:
                    continue
    except Exception:
        return 0.0
    return newest


def _read_json_version(path):
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            data = json.load(fh)
        ver = data.get('version') if isinstance(data, dict) else None
        return str(ver).strip() if ver else ''
    except Exception:
        return ''


def version_of(svc):
    explicit = str(svc.get('version') or '').strip()
    if explicit:
        return explicit
    cwd = svc.get('cwd')
    if cwd and os.path.isdir(cwd):
        pkg = os.path.join(cwd, 'package.json')
        if os.path.isfile(pkg):
            ver = _read_json_version(pkg)
            if ver:
                return ver
        txt = os.path.join(cwd, 'version.txt')
        if os.path.isfile(txt):
            try:
                with open(txt, 'r', encoding='utf-8', errors='replace') as fh:
                    ver = fh.readline().strip()
                if ver:
                    return ver[:20]
            except Exception:
                pass
    return '—'


def updated_of(svc):
    explicit = str(svc.get('updated') or '').strip()
    if explicit:
        return explicit
    cwd = svc.get('cwd')
    stamp = _newest_mtime(cwd) if cwd and os.path.isdir(cwd) else 0.0
    if not stamp:
        exe = svc.get('exe')
        if exe and os.path.isfile(exe):
            try:
                stamp = os.path.getmtime(exe)
            except Exception:
                stamp = 0.0
    return _fmt(stamp) or '—'
