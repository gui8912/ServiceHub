"""浏览器发现与「用指定浏览器打开网址」。

为什么要自己找浏览器而不是直接 webbrowser.open：
  webbrowser 只会用系统默认浏览器。用户装了 Edge / Chrome / ego lite 三个，
  日常习惯用一个、服务台想用另一个，必须能把 url 交给指定 exe。
  已装浏览器的权威来源是注册表 Clients\\StartMenuInternet（卸载后会自己消失），
  再补一份常见路径兜底（有些绿色版/便携版不注册）。
"""
import os
import re
import subprocess
import threading
import time
import webbrowser

try:
    import winreg
except Exception:            # 非 Windows 时降级为只用系统默认浏览器
    winreg = None

# 已知浏览器：id 固定，改设置后不会因为显示名变化而失效
KNOWN = [
    ('edge', 'Microsoft Edge', [
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ]),
    ('chrome', 'Google Chrome', [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe',
    ]),
    ('ego', 'ego lite', [
        r'C:\Program Files\Citro Labs\ego lite\Application\ego.exe',
    ]),
    ('firefox', 'Mozilla Firefox', [
        r'C:\Program Files\Mozilla Firefox\firefox.exe',
        r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe',
    ]),
]

# 注册表显示名 -> 稳定 id（认不出来就用 exe 文件名兜底）
NAME_TO_ID = {
    'microsoft edge': 'edge',
    'google chrome': 'chrome',
    'ego lite': 'ego',
    'mozilla firefox': 'firefox',
    '360se6': '360se',
}

# 注册表里也混着一些"自称浏览器"的桌面应用（Electron 壳、AI 助手），下拉框不该出现
SKIP_NAME_PARTS = ('copilot', 'ima.', 'iexplore', '.prog')

# 显示名美化：注册表键名往往就是 exe 名，直接显示不好看
DISPLAY_NAME = {
    '360se6': '360 安全浏览器',
}

_cache = {'at': 0.0, 'list': []}
_cache_lock = threading.Lock()
CACHE_SECONDS = 5.0


def _exe_from_command(cmd):
    """从 shell\\open\\command 的字符串里抠出 exe 路径。"""
    if not cmd:
        return ''
    cmd = cmd.strip()
    match = re.match(r'^"([^"]+)"', cmd)
    if match:
        return match.group(1)
    match = re.search(r'([A-Za-z]:\\[^"]*?\.exe)', cmd, re.I)
    if match:
        return match.group(1)
    return cmd.split(' ')[0]


def _read_registry():
    """读 HKLM / HKLM-WOW64 / HKCU 三处的 StartMenuInternet。"""
    if winreg is None:
        return []
    found = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Clients\StartMenuInternet'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Clients\StartMenuInternet'),
        (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Clients\StartMenuInternet'),
    ]
    for hive, base in roots:
        try:
            key = winreg.OpenKey(hive, base)
        except Exception:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
        except Exception:
            count = 0
        for idx in range(min(count, 64)):
            try:
                name = winreg.EnumKey(key, idx)
            except Exception:
                break
            if name.upper().startswith('IEXPLORE'):
                continue                      # IE 不算现代浏览器，别塞进下拉框
            low = name.lower()
            if any(part in low for part in SKIP_NAME_PARTS):
                continue                      # copilot / Electron 壳之类的"伪浏览器"
            cmd_key = None
            try:
                cmd_key = winreg.OpenKey(
                    hive, base + '\\' + name + r'\shell\open\command')
                cmd = winreg.QueryValueEx(cmd_key, '')[0]
            except Exception:
                continue
            finally:
                # modified: 2.6.3 外层 key 关了，循环里每个子项打开的
                # cmd_key 却从没关。discover(force=True) 每次都泄漏一批 HKEY，
                # 浏览器短暂找不到时设置页会反复触发全量重扫。
                if cmd_key is not None:
                    try:
                        winreg.CloseKey(cmd_key)
                    except Exception:
                        pass
            exe = _exe_from_command(cmd)
            if exe and os.path.exists(exe):
                found.append((name, exe))
        try:
            winreg.CloseKey(key)
        except Exception:
            pass
    return found


def _slug(exe):
    base = os.path.basename(exe).lower()
    for suffix in ('.exe',):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return re.sub(r'[^a-z0-9_-]+', '-', base) or 'browser'


def _scan():
    """扫一遍已装浏览器（调用方持 _cache_lock）。  # modified: 2.6.3 从 discover 抽出"""
    items = []
    seen = set()

    def add(bid, name, path, source):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.exists(path):
            return
        seen.add(key)
        items.append({'id': bid, 'name': name, 'path': path, 'source': source})

    for bid, name, paths in KNOWN:
        for path in paths:
            real = os.path.expandvars(path)
            if os.path.exists(real):
                add(bid, name, real, 'known')
                break

    for name, exe in _read_registry():
        bid = NAME_TO_ID.get(name.lower()) or _slug(exe)
        add(bid, DISPLAY_NAME.get(name.lower(), name), exe, 'registry')

    order = {bid: idx for idx, (bid, _n, _p) in enumerate(KNOWN)}
    items.sort(key=lambda it: (order.get(it['id'], 99), it['name'].lower()))
    return items


def discover(force=False):
    """返回已安装浏览器列表 [{id, name, path, source}]，去重且稳定排序。"""
    # modified: 2.6.3 原来"检查缓存"和"扫描+写回"分持两段锁，两个线程
    # 同时过期会各扫一遍并互相覆盖；空结果（一个浏览器都没探到）又永远
    # 不算命中缓存，每次调用都全量扫注册表。现在整段在一把锁里完成，
    # 用 at>0 区分"没扫过"和"扫过但结果为空"。
    with _cache_lock:
        if (not force and _cache['at'] > 0
                and (time.time() - _cache['at']) < CACHE_SECONDS):
            return [dict(item) for item in _cache['list']]
        items = _scan()
        _cache['at'] = time.time()
        _cache['list'] = [dict(it) for it in items]
        return items


def find(browser_id):
    for item in discover():
        if item['id'] == browser_id:
            return item
    return None


def resolve_info(settings):
    """把设置解析成「到底会用哪个浏览器」，连同退回原因一起给出。

    modified: 2.6.1 新增。原来只有 resolve() 返回 (id, exe)，一旦设置里选的浏览器
    没被探测到（ego 自更新时 Application\\ego.exe 会短暂消失），就静默退回系统默认，
    界面上只看到「已打开」，用户根本不知道用的是 Edge、更不知道为什么。
    这里把「用谁 / 为什么退回」显式化，供提示、日志和设置页共用。

    返回 dict：
      mode    auto | custom | known | missing
      id      设置里记的浏览器 id（auto/custom 时同 mode）
      name    给人看的名字（ego lite / 系统默认浏览器 / chrome.exe）
      path    实际要执行的 exe（auto 时为空）
      reason  空=正常；非空=为什么要退回系统默认
    """
    info = {'mode': 'auto', 'id': 'auto', 'name': '系统默认浏览器',
            'path': '', 'reason': '', 'fallback': False}
    if settings is None:  # 调用方漏传设置时按系统默认处理，别抛 AttributeError
        return info
    # modified: 2.6.3 设置里混进 1 / true / dict（或第三方直接调接口）时，
    # .strip() 会抛 AttributeError → /api/browser/test 必现 500。
    # 非字符串按"没配"处理，与 settings._as_text 的语义一致。
    def _text(value, fallback=''):
        return value.strip() if isinstance(value, str) else fallback

    bid = _text(settings.get('browser'), 'auto') or 'auto'
    path_setting = _text(settings.get('browser_path'))

    if bid == 'auto':
        return info
    if bid == 'custom':
        if not path_setting:
            info['mode'] = 'custom'
            info['id'] = 'custom'
            info['reason'] = '选了「自定义路径」但路径是空的'
            return info
        info.update({'mode': 'custom', 'id': 'custom', 'path': path_setting,
                     'name': os.path.basename(path_setting) or '自定义浏览器'})
        if not os.path.exists(path_setting):
            info['reason'] = '自定义浏览器路径不存在：%s' % path_setting
        return info

    item = find(bid)
    if not item:
        # modified: 缓存只有 5 秒，但浏览器正好在自更新时可能瞬间探不到。
        # 先强制重扫一次，别因为一次偶然的空结果就把用户踢回 Edge。
        discover(force=True)
        item = find(bid)
    if item:
        info.update({'mode': 'known', 'id': item['id'], 'name': item['name'], 'path': item['path']})
        return info

    # 设置里记的浏览器被卸载/正在更新：还有自定义路径就用它，否则退回系统默认
    if path_setting:
        info.update({'mode': 'custom', 'id': 'custom', 'path': path_setting,
                     'name': os.path.basename(path_setting) or '自定义浏览器'})
        info['reason'] = '没找到设置里选的浏览器「%s」，先用自定义路径' % bid
        return info
    info.update({'mode': 'missing', 'id': bid, 'name': bid})
    info['reason'] = '没找到设置里选的浏览器「%s」（可能正在更新或已卸载）' % bid
    return info


def resolve(settings):
    """兼容旧调用：把设置解析成 (id, exe)。需要退回原因请用 resolve_info()。"""
    info = resolve_info(settings)
    if info['mode'] == 'auto' or not info['path']:
        return 'auto', ''
    return info['id'], info['path']


def _default_browser(url, logger):
    """退回系统默认浏览器。返回 (ok, 错误信息)。"""
    try:
        # modified: 2.6.3 webbrowser.open() 失败时返回 False 而不一定抛异常，
        # 原来一律当成成功，界面显示"已打开"、实际什么都没弹出来。
        opened = webbrowser.open(url)
    except Exception as exc:
        logger.error('系统默认浏览器打开失败：%r' % exc)
        return False, str(exc)
    if not opened:
        logger.warn('系统默认浏览器未能打开 %s' % url)
        return False, '系统默认浏览器未能打开（webbrowser.open 返回 False）'
    return True, ''


def open_url_ex(url, settings, logger, force_auto=False):
    """用用户选定的浏览器打开网址。返回 (ok, message, info)。

    modified: 2.6.1 消息里始终带上「实际用了哪个浏览器」，退回系统默认时明确写出来。
    以前无论走哪条路都只回「已打开」，前端 toast 又把 message 换成「已打开 URL」，
    于是「设置的是 ego、看到的是 Edge」这种情况完全没有线索。
    """
    info = {'mode': 'auto', 'id': 'auto', 'name': '系统默认浏览器',
            'path': '', 'reason': '', 'fallback': False}
    if not url:
        return False, '没有可打开的网址', info
    if not force_auto:
        info = resolve_info(settings)
        info['fallback'] = False
    reason = info.get('reason') or ''
    if reason:
        logger.warn(reason)

    # 1) 明确要用某个 exe
    if info.get('path'):
        exe, name = info['path'], info['name']
        if not os.path.exists(exe):
            info['fallback'] = True
            ok, err = _default_browser(url, logger)
            if not ok:
                return False, '打不开网页：%s' % err, info
            return True, '⚠ 找不到 %s（%s），已改用系统默认浏览器打开 %s' % (name, exe, url), info
        try:
            # modified: 2.6.3 自定义路径指向 .bat/控制台程序时别弹黑框
            subprocess.Popen([exe, url], close_fds=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return True, '已用 %s 打开 %s' % (name, url), info
        except Exception as exc:
            logger.error('调用 %s 失败：%r' % (exe, exc))
            info['fallback'] = True
            ok, err = _default_browser(url, logger)
            if not ok:
                return False, '用 %s 打开失败：%s' % (name, err), info
            return True, '⚠ 用 %s 打开失败（%s），已改用系统默认浏览器打开 %s' % (name, exc, url), info

    # 2) 系统默认
    ok, err = _default_browser(url, logger)
    if not ok:
        return False, '打不开网页：%s' % err, info
    if reason:
        info['fallback'] = True
        return True, '⚠ %s，已改用系统默认浏览器打开 %s' % (reason, url), info
    return True, '已用系统默认浏览器打开 %s' % url, info


def open_url(url, settings, logger, force_auto=False):
    """用用户选定的浏览器打开网址。返回 (ok, message)。"""
    ok, message, _info = open_url_ex(url, settings, logger, force_auto=force_auto)
    return ok, message
