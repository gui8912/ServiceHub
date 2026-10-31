"""设置 —— runtime/settings.json 的唯一读写入口。

为什么单独抽一个模块：
  旧版把设置读写塞在 server.HubContext 里，只能存一个 auto_open，加一项就要改 HTTP 层，
  而且没有任何校验（网页传什么就存什么，传 "false" 字符串会被当成 True）。
  这里统一做三件事：默认值、类型校验、原子落盘。
"""
import json
import os
import threading

# 默认值就是"什么都没配过"时的行为，全部可在网页「设置」里改
DEFAULTS = {
    'auto_open': True,        # 服务启动后自动打开它的网页
    'browser': 'auto',        # auto=系统默认；其余是 browsers.discover() 给出的 id；custom=自定义路径
    'browser_path': '',       # browser=custom 时用的可执行文件
    'theme': 'system',        # system / light / dark
    'view': 'card',           # card / list
    'refresh_ms': 2000,       # 网页自动刷新间隔（毫秒）
}

THEMES = ('system', 'light', 'dark')
VIEWS = ('card', 'list')
REFRESH_MIN = 500
REFRESH_MAX = 60000


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return default


def _as_int(value, default, low, high):
    try:
        num = int(float(value))
    except Exception:
        return default
    return max(low, min(high, num))


def _as_text(value, default='', limit=400):
    if not isinstance(value, str):
        return default
    return value.strip().strip('"').strip("'")[:limit]


class Settings:
    def __init__(self, path, logger):
        self.path = path
        self.logger = logger
        self.lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self._mtime = 0.0
        self.load()

    # ---------------------------------------------------------------- 读
    def load(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except Exception:
            return dict(self._data)     # 首次运行还没有设置文件，直接用默认值
        if not force and mtime == self._mtime:
            return dict(self._data)
        try:
            with open(self.path, 'r', encoding='utf-8-sig') as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError('设置文件顶层不是对象')
        except Exception as exc:
            self.logger.warn('设置文件读取失败，改用默认值：%s' % exc)
            return dict(self._data)
        with self.lock:
            self._data = self._normalize(raw)
            self._mtime = mtime
            return dict(self._data)

    def _normalize(self, raw):
        """只认识的字段才留，类型不对就回落到默认值——脏数据不该把界面搞崩。"""
        data = dict(DEFAULTS)
        if 'auto_open' in raw:
            data['auto_open'] = _as_bool(raw.get('auto_open'), DEFAULTS['auto_open'])
        if 'browser' in raw:
            data['browser'] = _as_text(raw.get('browser'), DEFAULTS['browser'], 60) or 'auto'
        if 'browser_path' in raw:
            data['browser_path'] = _as_text(raw.get('browser_path'), '', 400)
        if 'theme' in raw:
            theme = _as_text(raw.get('theme'), DEFAULTS['theme'], 20).lower()
            data['theme'] = theme if theme in THEMES else DEFAULTS['theme']
        if 'view' in raw:
            view = _as_text(raw.get('view'), DEFAULTS['view'], 20).lower()
            data['view'] = view if view in VIEWS else DEFAULTS['view']
        if 'refresh_ms' in raw:
            data['refresh_ms'] = _as_int(raw.get('refresh_ms'),
                                         DEFAULTS['refresh_ms'], REFRESH_MIN, REFRESH_MAX)
        return data

    def as_dict(self):
        with self.lock:
            return dict(self._data)

    def get(self, key, default=None):
        with self.lock:
            return self._data.get(key, default)

    # ---------------------------------------------------------------- 写
    def update(self, patch):
        """只更新传进来的字段，其余保持；返回更新后的完整设置。"""
        if not isinstance(patch, dict):
            return self.as_dict()
        with self.lock:
            merged = dict(self._data)
            merged.update(patch)
            self._data = self._normalize(merged)
            self._flush()
            return dict(self._data)

    def _flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)   # 原子替换：断电/崩溃也不会留下半个 JSON
            try:
                self._mtime = os.path.getmtime(self.path)
            except Exception:
                pass
        except Exception as exc:
            self.logger.error('设置保存失败：%r' % exc)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
