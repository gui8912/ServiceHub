"""设置 —— runtime/settings.json 的唯一读写入口。

为什么单独抽一个模块：
  旧版把设置读写塞在 server.HubContext 里，只能存一个 auto_open，加一项就要改 HTTP 层，
  而且没有任何校验（网页传什么就存什么，传 "false" 字符串会被当成 True）。
  这里统一做三件事：默认值、类型校验、原子落盘。
"""
import copy   # modified: 2.6.3 默认值里有 list/dict，浅拷贝会共享可变对象
import json
import os
import tempfile   # modified: 2.6.3 唯一临时文件名，两个实例同时写不互相覆盖
import threading

# 默认值就是"什么都没配过"时的行为，全部可在网页「设置」里改
DEFAULTS = {
    'auto_open': True,        # 服务启动后自动打开它的网页
    'browser': 'auto',        # auto=系统默认；其余是 browsers.discover() 给出的 id；custom=自定义路径
    'browser_path': '',       # browser=custom 时用的可执行文件
    'theme': 'system',        # system / light / dark
    'view': 'card',           # card / list
    'refresh_ms': 2000,       # 网页自动刷新间隔（毫秒）
    # modified: 背景光斑漂移动画，默认关。
    # 部分内核（实测 Edge + Intel 集显 + 高 DPI）合成这种持续运动的图层时会整屏闪烁，
    # 关掉后页面彻底静止；喜欢"呼吸感"的可以在设置里打开。
    'bg_motion': False,
    # 下面三项是「怎么看」的偏好，不是「服务是什么」的定义，所以放在设置里而不是
    # services.json。好处：手工改注册表、从 GitHub 重新 clone 都不会把它们冲掉；
    # 服务被删掉后残留的 id 在渲染时直接忽略，不需要清理。
    'hidden_ids': [],         # 首页隐藏的服务 id（不进卡片/列表，仍可在设置里恢复）
    'group_order': [],        # 分组显示顺序；未列出的分组按出现顺序接在后面
    'density': 'comfortable', # comfortable=舒适（等宽四列） / compact=紧凑（信息更少）
    # modified: 卡片拖拽的两个落点。都只在本机生效，不写 services.json——
    # 网页里改配置一旦写坏，用户就得手改 JSON 才能救回来；注册表保持"出厂设置"更安全。
    'service_group': {},      # {服务id: 分组名} 拖到别的组后在这里记一笔，覆盖 services.json 的 group
    'service_order': {},      # {分组名: [服务id, ...]} 组内先后顺序，未列出的排在后面
}

THEMES = ('system', 'light', 'dark')
VIEWS = ('card', 'list')
DENSITIES = ('comfortable', 'compact')
REFRESH_MIN = 500
REFRESH_MAX = 60000
LIST_LIMIT = 300      # hidden_ids / group_order 各自最多存多少条
MAP_LIMIT = 200       # service_group / service_order 最多存多少个键


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


def _as_id_list(value, limit=LIST_LIMIT):
    """字符串数组校验：非数组回落到 []，逐项修剪、去重、限长，非字符串项直接丢弃。
    网页传什么就存什么的话，一个手抖的 {"hidden_ids": "abc"} 就能前端 forEach 抛异常。"""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:64]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _as_id_map(value, limit=MAP_LIMIT):
    """{服务id: 分组名} 校验：非 dict 回落到 {}，键值都必须是字符串，空串丢弃。
    用于 service_group（卡片被拖到别的分组后记的那一笔）。
    同样不能照单全收：网页传个 {"a": ["x"]} 会让前端分组比较全部失效。"""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        k = key.strip()[:64]
        v = val.strip()[:64]
        if k and v:
            out[k] = v
        if len(out) >= limit:
            break
    return out


def _as_order_map(value, limit=MAP_LIMIT):
    """{分组名: [服务id, ...]} 校验：外层非 dict 回落 {}，每个值走 _as_id_list。
    用于 service_order（组内先后顺序）。键是用户可以自己起的组名，所以不限制字符集，
    只做长度截断——组名里带空格、斜杠都能正常用。"""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, val in value.items():
        if not isinstance(key, str):
            continue
        k = key.strip()[:64]
        if not k:
            continue
        out[k] = _as_id_list(val)
        if len(out) >= limit:
            break
    return out


class Settings:
    def __init__(self, path, logger):
        self.path = path
        self.logger = logger
        self.lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)   # modified: 2.6.3
        self._mtime = 0.0
        self.last_error = ''   # modified: 2.6.0 最近一次落盘失败原因；空串 = 上次写入正常
        self.load()

    # ---------------------------------------------------------------- 读
    def load(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except Exception:
            return copy.deepcopy(self._data)     # 首次运行还没有设置文件，直接用默认值
        if not force and mtime == self._mtime:
            return copy.deepcopy(self._data)
        try:
            with open(self.path, 'r', encoding='utf-8-sig') as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError('设置文件顶层不是对象')
        except Exception as exc:
            self.logger.warn('设置文件读取失败，沿用当前设置：%s' % exc)  # modified: 注释与实现对齐
            return copy.deepcopy(self._data)
        with self.lock:
            self._data = self._normalize(raw)
            self._mtime = mtime
            return copy.deepcopy(self._data)

    def _normalize(self, raw):
        """只认识的字段才留，类型不对就回落到默认值——脏数据不该把界面搞崩。"""
        data = copy.deepcopy(DEFAULTS)   # modified: 2.6.3
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
        if 'bg_motion' in raw:  # modified
            data['bg_motion'] = _as_bool(raw.get('bg_motion'), DEFAULTS['bg_motion'])
        if 'hidden_ids' in raw:
            data['hidden_ids'] = _as_id_list(raw.get('hidden_ids'))
        if 'group_order' in raw:
            data['group_order'] = _as_id_list(raw.get('group_order'))
        if 'density' in raw:
            density = _as_text(raw.get('density'), DEFAULTS['density'], 20).lower()
            data['density'] = density if density in DENSITIES else DEFAULTS['density']
        # modified: 卡片拖拽的两个落点（跨组归属 + 组内顺序）
        if 'service_group' in raw:
            data['service_group'] = _as_id_map(raw.get('service_group'))
        if 'service_order' in raw:
            data['service_order'] = _as_order_map(raw.get('service_order'))
        return data

    def as_dict(self):
        with self.lock:
            # modified: 2.6.3 浅拷贝会把内部 list/dict 的引用交出去，
            # 调用方一改就污染了进程内的设置。
            return copy.deepcopy(self._data)

    def get(self, key, default=None):
        with self.lock:
            val = self._data.get(key, default)
            return copy.deepcopy(val) if isinstance(val, (list, dict)) else val

    # ---------------------------------------------------------------- 写
    def update(self, patch):
        """只更新传进来的字段，其余保持；返回更新后的完整设置。"""
        if not isinstance(patch, dict):
            return self.as_dict()
        with self.lock:
            previous = copy.deepcopy(self._data)
            merged = dict(self._data)
            merged.update(patch)
            self._data = self._normalize(merged)
            # modified: 2.6.3 落盘失败时把内存也回滚，否则界面显示新值、
            # 磁盘还是旧值，重启后静默回退（2.6.0 只修了"别报成功"）。
            if not self._flush():
                self._data = previous
            return copy.deepcopy(self._data)

    def _flush(self):
        """原子落盘；返回 True/False（2.6.3）。"""
        tmp = ''
        try:
            # modified: 2.6.3 makedirs 原来在 try 外：目录变成同名文件时
            # 异常直接冒泡，last_error 都没设，server 的 500 判断形同虚设。
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            # modified: 固定名 settings.json.tmp 在两个实例同时写时会互相覆盖，
            # 用 mkstemp 拿唯一名字，仍靠 os.replace 原子替换。
            fd, tmp = tempfile.mkstemp(prefix='.settings-', suffix='.tmp',
                                       dir=folder or '.')
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            tmp = ''
            try:
                self._mtime = os.path.getmtime(self.path)
            except Exception:
                pass
            self.last_error = ''
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.logger.error('设置保存失败：%r' % exc)
            if tmp:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
            return False
