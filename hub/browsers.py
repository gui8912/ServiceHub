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
            try:
                cmd_key = winreg.OpenKey(
                    hive, base + '\\' + name + r'\shell\open\command')
                cmd = winreg.QueryValueEx(cmd_key, '')[0]
            except Exception:
                continue
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


def discover(force=False):
    """返回已安装浏览器列表 [{id, name, path, source}]，去重且稳定排序。"""
    with _cache_lock:
        if not force and _cache['list'] and (time.time() - _cache['at']) < CACHE_SECONDS:
            return [dict(item) for item in _cache['list']]

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

    with _cache_lock:
        _cache['at'] = time.time()
        _cache['list'] = [dict(it) for it in items]
    return items


def find(browser_id):
    for item in discover():
        if item['id'] == browser_id:
            return item
    return None


def resolve(settings):
    """把设置解析成要用的浏览器：('auto', '') 或 (id, exe)。"""
    bid = (settings.get('browser') or 'auto').strip()
    if bid == 'auto':
        return 'auto', ''
    if bid == 'custom':
        path = (settings.get('browser_path') or '').strip()
        return ('custom', path) if path else ('auto', '')
    item = find(bid)
    if item:
        return item['id'], item['path']
    # 设置里记的浏览器被卸载了：退回自定义路径，再退回系统默认
    path = (settings.get('browser_path') or '').strip()
    return ('custom', path) if path else ('auto', '')


def open_url(url, settings, logger, force_auto=False):
    """用用户选定的浏览器打开网址。返回 (ok, message)。"""
    if not url:
        return False, '没有可打开的网址'
    bid, exe = ('auto', '') if force_auto else resolve(settings)
    if bid == 'auto' or not exe:
        try:
            webbrowser.open(url)
            return True, '已用系统默认浏览器打开'
        except Exception as exc:
            logger.error('系统默认浏览器打开失败：%r' % exc)
            return False, '打不开网页：%s' % exc
    if not os.path.exists(exe):
        logger.warn('浏览器不存在（%s），改用系统默认' % exe)
        try:
            webbrowser.open(url)
            return True, '指定的浏览器不在了，已用系统默认浏览器打开'
        except Exception as exc:
            return False, '打不开网页：%s' % exc
    try:
        subprocess.Popen([exe, url], close_fds=True)
        return True, '已用 %s 打开' % os.path.basename(exe)
    except Exception as exc:
        logger.error('调用 %s 失败：%r' % (exe, exc))
        try:
            webbrowser.open(url)
            return True, '指定浏览器调用失败，已用系统默认浏览器打开'
        except Exception:
            return False, '打不开网页：%s' % exc
