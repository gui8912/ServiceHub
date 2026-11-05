# ServiceHub 2.6.3 完整合并源码

生成日期：2026-11-05。按原文件路径分节；修改与新增行保留源文件里的 `modified` 标注。

## 文件目录

- `ServiceHub.py`
- `hub/__init__.py`
- `hub/actions.py`
- `hub/autostart.py`
- `hub/browsers.py`
- `hub/logger.py`
- `hub/meta.py`
- `hub/monitor.py`
- `hub/registry.py`
- `hub/server.py`
- `hub/settings.py`
- `hub/tray.py`
- `hub/ui/index.html`
- `hub/ui/style.css`
- `hub/ui/app.js`
- `services.json`
- `services.local.example.json`
- `启动服务台.bat`
- `setup-deps.bat`
- `svc.cmd`
- `svc.ps1`


---

## `ServiceHub.py`

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务台 ServiceHub —— 本地服务一键启停工作台（Python 版，替代旧 PowerShell/WinForms 版）。

用法：
    pythonw ServiceHub.py                常驻运行（默认，无控制台窗口）
    python  ServiceHub.py --selftest     跑一遍自检并打印报告
    python  ServiceHub.py --no-tray      不起托盘图标
    python  ServiceHub.py --no-open      启动后不打开浏览器
    python  ServiceHub.py --port 9520    指定起始端口（端口被占时自动往上找）

设计：界面在浏览器里，后台采集线程每秒刷新快照，services.json 是唯一注册表，
设置存 runtime\\settings.json（浏览器、主题、视图、刷新间隔）。
"""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(APP_DIR, 'runtime', 'site-packages')
# 依赖随工具自带（runtime\\site-packages），不污染系统 Python，也不受系统环境变动影响
if os.path.isdir(VENDOR_DIR):
    sys.path.insert(0, VENDOR_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from hub import browsers                  # noqa: E402
from hub.actions import Actions          # noqa: E402
from hub.logger import Logger            # noqa: E402
from hub.monitor import Monitor          # noqa: E402
from hub.registry import Registry        # noqa: E402
from hub.server import APP_VERSION, HubContext, HubServer  # noqa: E402
from hub.settings import Settings        # noqa: E402

LOG_DIR = os.path.join(APP_DIR, 'logs')
RUNTIME_DIR = os.path.join(APP_DIR, 'runtime')
CONFIG_FILE = os.path.join(APP_DIR, 'services.json')
SETTINGS_FILE = os.path.join(RUNTIME_DIR, 'settings.json')
PORT_FILE = os.path.join(RUNTIME_DIR, 'port.txt')
PID_FILE = os.path.join(RUNTIME_DIR, 'hub.pid')
DEFAULT_PORT = 9500


# ================================================================ 工具
def _port_alive(port, timeout=0.3):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def find_existing(start, end):
    """同端口上已经有实例在跑的话返回它的端口，避免开出第二个工作台。"""
    for port in range(start, end + 1):
        if not _port_alive(port):
            continue
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=0.5) as resp:
                data = resp.read().decode('utf-8', errors='replace')
            if '"pong"' in data:
                return port
        except Exception:
            continue
    return None


def write_runtime_files(port):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    try:
        with open(PORT_FILE, 'w', encoding='utf-8') as fh:
            fh.write(str(port))
        with open(PID_FILE, 'w', encoding='utf-8') as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass


def build_context():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = Logger(LOG_DIR, 'hub')
    logger.clean_old(keep_days=14)
    registry = Registry(CONFIG_FILE, logger)
    settings = Settings(SETTINGS_FILE, logger)
    monitor = Monitor(registry, logger)
    monitor.auto_open = bool(settings.get('auto_open', True))
    actions = Actions(registry, monitor, logger, APP_DIR, LOG_DIR, settings)
    monitor.on_open = actions.open_url
    ctx = HubContext(registry, monitor, actions, logger, APP_DIR, LOG_DIR, settings)
    return ctx


def open_panel(ctx, url=None):
    """用设置里选定的浏览器打开面板（默认系统浏览器）。"""
    url = url or ctx.hub_url()
    ok, message = browsers.open_url(url, ctx.settings, ctx.logger)
    # modified: 2.6.1 把「实际用了哪个浏览器」写进日志。以前只回一句「已打开」，
    # 设了 ego 却弹出 Edge 时日志里查不到任何线索，只能靠猜。
    ctx.logger.act('打开面板：%s → %s' % (url, message))
    if not ok:
        ctx.logger.error('打开面板失败：%s' % message)
    return ok, message


# ================================================================ 自检
def run_selftest(ctx):
    """不启界面，把关键链路跑一遍：采集 → 状态判定 → 设置 → HTTP → 真实服务启停。"""
    report = []
    failures = 0
    # 自检会真的启停一次服务，但绝不能顺手弹浏览器窗口——用户跑 --selftest 只想看报告。
    # 用 suppress_open 而不是 auto_open：HTTP 段会读 /api/settings，那里会把 auto_open 同步回
    # 设置里的值，用 auto_open 抑制会被它冲掉（实测弹了两次才定位到）。
    prev_auto_open = ctx.monitor.auto_open
    prev_suppress = ctx.monitor.suppress_open
    ctx.monitor.suppress_open = True
    ctx.monitor.auto_open = False

    def add(title, passed, detail=''):
        nonlocal failures
        report.append('[%s] %s%s' % ('OK  ' if passed else 'FAIL', title, ('  ' + detail) if detail else ''))
        if not passed:
            failures += 1

    add('依赖 psutil', ctx.monitor.psutil_error is None, ctx.monitor.psutil_error or '')

    services = ctx.registry.services()
    add('注册表 services.json', bool(services), '共 %d 条' % len(services))
    if services:
        bad_args = [s['id'] for s in services if not isinstance(s.get('args'), list)]
        add('args 均为数组（单元素数组退化成字符串是旧版踩过的坑）', not bad_args, '异常：%s' % bad_args)
        ids = [s['id'] for s in services]
        add('id 无重复', len(ids) == len(set(ids)))
        # modified: registry 会把推不出的值填成 '—'，以前只判空，'—' 也算"有"，这条永远通过
        no_ver = [s['id'] for s in services if not s.get('version') or s.get('version') == '—']
        no_upd = [s['id'] for s in services if not s.get('updated') or s.get('updated') == '—']
        add('每条都有版本号与更新时间', not no_ver and not no_upd,
            '缺版本：%s；缺时间：%s' % (no_ver or '无', no_upd or '无'))
        bad_paths = []
        for item in services:
            exe = item.get('exe') or ''
            cwd = item.get('cwd') or ''
            if not exe or not os.path.exists(exe):
                bad_paths.append('%s(exe)' % item['id'])
            elif cwd and not os.path.isdir(cwd):
                bad_paths.append('%s(cwd)' % item['id'])
        add('每条的可执行文件与工作目录都存在', not bad_paths, '异常：%s' % (bad_paths or '无'))

    # modified: 2.6.3 Windows 冷启动时，psutil 第一次枚举 600+ 进程的命令行要
    # 预热 COM/WMI，本机实测约 20s，第二次仅 200~300ms。自检关心的是"每轮采集"
    # 的稳态开销（后台线程按刷新间隔反复跑），先预热一次再计时；冷启动耗时单独
    # 写进报告供排查，避免把一次性的系统预热误判成采集逻辑的性能故障。
    cold = time.perf_counter()
    ctx.monitor.refresh()
    report.append('[INFO] 首次采集（含 psutil 冷预热）%.1f ms' %
                  ((time.perf_counter() - cold) * 1000))  # modified
    began = time.perf_counter()
    ctx.monitor.refresh()
    cost = (time.perf_counter() - began) * 1000
    add('稳态采集一轮耗时 < 500ms', cost < 500, '%.1f ms' % cost)  # modified

    view = ctx.monitor.view()['services']
    add('状态条目数与注册表一致', len(view) == len(services), '%d / %d' % (len(view), len(services)))
    states = {}
    for row in view:
        states[row['state']] = states.get(row['state'], 0) + 1
    add('状态判定完成', True, '；'.join('%s=%d' % kv for kv in states.items()))
    fields = all(row.get('version') and row.get('version') != '—'
                 and row.get('updated') and row.get('updated') != '—' for row in view)  # modified
    add('状态行带版本/更新时间字段', fields)
    add('状态行带"未安装"标记', all('missing' in row for row in view))
    missing_rows = [row['id'] for row in view if row.get('missing')]
    add('本机没有路径失效的条目（有则界面显示"未安装"）', not missing_rows,
        '缺失：%s' % (missing_rows or '无'))

    # ---- 本机差异层 services.local.json：不改主表也能覆盖/隐藏/整份替换
    try:
        merged = ctx.registry._apply_local(
            {'services': [{'id': 'a', 'exe': 'x'}, {'id': 'b', 'exe': 'y'}]},
            {'services': [{'id': 'a', 'exe': 'z'}, {'id': 'c', 'exe': 'w'}], 'remove': ['b']})
        got = [(s['id'], s['exe']) for s in merged['services']]
        add('差异层：覆盖字段 + 追加 + 隐藏', got == [('a', 'z'), ('c', 'w')], str(got))
        replaced = ctx.registry._apply_local({'services': [{'id': 'a'}]},
                                             {'mode': 'replace', 'services': [{'id': 'n'}]})
        add('差异层：replace 整份替换', [s['id'] for s in replaced['services']] == ['n'])
    except Exception as exc:
        add('差异层合并逻辑', False, repr(exc))

    # ---- 设置：写入 → 落盘 → 重读
    origin = ctx.settings.as_dict()
    try:
        ctx.settings.update({'theme': 'dark', 'view': 'list', 'refresh_ms': 3000})
        fresh = Settings(SETTINGS_FILE, ctx.logger)
        saved = fresh.as_dict()
        add('设置可落盘并被重新读回',
            saved.get('theme') == 'dark' and saved.get('view') == 'list' and saved.get('refresh_ms') == 3000,
            'theme=%s view=%s refresh_ms=%s' % (saved.get('theme'), saved.get('view'), saved.get('refresh_ms')))
        bad = ctx.settings.update({'theme': 'rainbow', 'view': 'grid', 'refresh_ms': 999999})
        add('非法设置被纠正（不是照单全收）',
            bad.get('theme') == 'system' and bad.get('view') == 'card' and bad.get('refresh_ms') <= 60000,
            'theme=%s view=%s refresh_ms=%s' % (bad.get('theme'), bad.get('view'), bad.get('refresh_ms')))
    finally:
        ctx.settings.update(origin)

    found = browsers.discover()
    add('发现已安装浏览器', len(found) >= 1,
        '、'.join('%s(%s)' % (b['name'], b['id']) for b in found) or '一个都没找到')

    ui_files = ['index.html', 'style.css', 'app.js']
    missing = [fn for fn in ui_files
               if not os.path.isfile(os.path.join(APP_DIR, 'hub', 'ui', fn))]
    add('网页资源齐全', not missing, '缺：%s' % missing)

    # ---- HTTP 往返
    server = None
    try:
        server = HubServer(ctx, (9590, 9610))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.3)
        base = 'http://127.0.0.1:%d' % ctx.port
        for title, path in (('GET /', '/'), ('GET /api/status', '/api/status'),
                            ('GET /api/settings', '/api/settings'),
                            ('GET /static/app.js', '/static/app.js')):
            try:
                with urllib.request.urlopen(base + path, timeout=2) as resp:
                    body = resp.read()
                add(title, bool(body), '%d 字节' % len(body))
            except Exception as exc:
                add(title, False, str(exc))

        import json as _json
        with urllib.request.urlopen(base + '/api/status', timeout=2) as resp:
            payload = _json.loads(resp.read().decode('utf-8'))
        add('/api/status 返回服务列表', len(payload.get('services', [])) == len(services))
        with urllib.request.urlopen(base + '/api/settings', timeout=2) as resp:
            payload = _json.loads(resp.read().decode('utf-8'))
        add('/api/settings 同时返回浏览器列表',
            isinstance(payload.get('browsers'), list) and isinstance(payload.get('about'), dict),
            '%d 个浏览器' % len(payload.get('browsers') or []))

        # 不带 X-ServiceHub 头的 POST 必须被拒（防跨站偷偷启停）
        req = urllib.request.Request(base + '/api/action',
                                     data=_json.dumps({'id': 'docscan', 'action': 'stop'}).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(req, timeout=2)
            add('跨站 POST 被拒绝', False, '居然成功了')
        except urllib.error.HTTPError as exc:
            add('跨站 POST 被拒绝', exc.code == 403, 'HTTP %d' % exc.code)

        server.shutdown()
        server.server_close()  # modified: 把监听 socket 也关掉，自检进程别留半开端口
        add('HTTP 服务可正常关闭', True)
    except Exception as exc:
        add('HTTP 往返', False, repr(exc))
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()  # modified
            except Exception:
                pass

    # ---- 真实服务：动态端口捕获全链路（启动 → 抓端口 → 停止）
    target = 'rename-cn'
    svc = ctx.registry.get(target)
    if svc:
        try:
            if ctx.monitor.psutil_error is None:
                result = ctx.actions.start(target)
                add('启动 %s' % target, result.get('ok', False), result.get('message', ''))
                add('启动后登记了"就绪自动开网页"', target in ctx.monitor.pending_open)
                deadline = time.time() + 25
                port = None
                while time.time() < deadline:
                    ctx.monitor.refresh()
                    port = ctx.monitor.dynamic_port.get(target)
                    if port:
                        break
                    time.sleep(0.5)
                add('捕获动态端口', bool(port), '端口=%s' % port)
                if port:
                    listening = port in ctx.monitor._snapshot.listen
                    add('动态端口确实在监听', listening, '监听表含 %s = %s' % (port, listening))
                    add('自动网页地址可用', bool(ctx.monitor.resolve_url(svc)), str(ctx.monitor.resolve_url(svc)))
                ctx.monitor.pending_open.pop(target, None)   # 自检不弹浏览器
                stopped = ctx.actions.stop(target, force=True)
                add('停止 %s' % target, stopped.get('ok', False), stopped.get('message', ''))
                deadline = time.time() + 8
                row = None
                while time.time() < deadline:
                    ctx.monitor.refresh()          # 停完要重新采集，只读缓存会看到上一轮
                    row = ctx.monitor.service_row(target)
                    if row and not row.get('running'):
                        break
                    time.sleep(0.5)
                add('停止后状态归零', bool(row) and not row.get('running'), json_state(row))
            else:
                add('动态端口全链路', False, 'psutil 不可用，跳过')
        except Exception as exc:
            add('动态端口全链路', False, repr(exc))
    else:
        add('存在测试目标 %s' % target, False, '注册表里没有')

    report.append('')
    report.append('结论：%s（失败 %d 项）' % ('全部通过' if failures == 0 else '有失败项', failures))
    text = '\n'.join(report)
    ctx.monitor.auto_open = prev_auto_open
    ctx.monitor.suppress_open = prev_suppress
    print(text)
    try:
        with open(os.path.join(LOG_DIR, 'selftest-report.txt'), 'w', encoding='utf-8') as fh:
            fh.write('ServiceHub 自检报告  %s  v%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), APP_VERSION))
            fh.write(text + '\n')
    except Exception:
        pass
    return 0 if failures == 0 else 1


def json_state(row):
    if not row:
        return '无状态'
    return '%s/%s/running=%s' % (row.get('state'), row.get('portText'), row.get('running'))


# ================================================================ 主流程
def main(argv=None):
    parser = argparse.ArgumentParser(description='服务台 ServiceHub')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='起始端口，被占时自动 +1')
    parser.add_argument('--tries', type=int, default=20, help='端口重试范围大小')
    parser.add_argument('--no-tray', action='store_true', help='不起托盘图标')
    parser.add_argument('--no-open', action='store_true', help='启动后不自动打开浏览器')
    parser.add_argument('--selftest', action='store_true', help='跑自检并打印报告')
    args = parser.parse_args(argv)
    if not (1 <= args.port <= 65535):  # modified: 越界端口会在 bind 时抛 OverflowError，提前拦下
        parser.error('--port 必须在 1-65535 之间')
    if args.tries < 1:  # modified
        parser.error('--tries 必须大于 0')
    # modified: 2.6.3 只校验了起始端口，--port 65535 --tries 2 会让探测
    # 循环走到 65536，socket.bind 抛 OverflowError（不是 OSError）导致启动崩溃。
    tries = max(1, args.tries)
    if args.port + tries - 1 > 65535:
        parser.error('--port 加 --tries 的端口范围不能超过 65535')

    ctx = build_context()

    if args.selftest:
        ctx.logger.info('===== 自检开始 v%s =====' % APP_VERSION)
        code = run_selftest(ctx)
        ctx.logger.info('===== 自检结束，失败项 %d =====' % code)
        return code

    port_range = (args.port, args.port + tries - 1)

    existing = find_existing(*port_range)
    if existing:
        url = 'http://127.0.0.1:%d/' % existing
        ctx.logger.info('服务台已在运行（端口 %d），直接打开 %s' % (existing, url))
        if not args.no_open:
            open_panel(ctx, url)
        return 0

    server = HubServer(ctx, port_range)
    write_runtime_files(ctx.port)
    ctx.monitor.start()

    hub_url = ctx.hub_url()

    def request_exit():
        threading.Timer(0.4, server.shutdown).start()

    tray_icon = None
    if not args.no_tray:
        from hub.tray import start_tray
        tray_icon = start_tray(ctx, hub_url, lambda: open_panel(ctx),
                               ctx.actions.start_all, ctx.actions.stop_all, request_exit)
        ctx.tray = bool(tray_icon)

    if ctx.tray:
        ctx.logger.ok('服务台已常驻托盘：%s（右键托盘图标退出）' % hub_url)
    else:
        ctx.logger.ok('服务台已常驻后台：%s（网页里点"退出服务台"结束）' % hub_url)

    if not args.no_open:
        # 等一小会儿让首个快照先算完，网页首屏不至于空白
        threading.Timer(0.6, lambda: open_panel(ctx)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.monitor.shutdown()
        server.server_close()
        for path in (PORT_FILE, PID_FILE):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        ctx.logger.act('服务台网页服务已退出')
        # 兜底：托盘消息循环线程有时不等人，进程会变成"端口已关但还活着"的僵尸，
        # 下次启动就查不到旧实例。这里主动把自己结束掉（日志每行都即时落盘，不会丢）。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

---

## `hub/__init__.py`

```python
"""服务台 ServiceHub —— Python 版核心包。

设计要点（相对旧 PowerShell/WinForms 版）：
  1. 采集用 psutil（一次 process_iter + 一次 net_connections，实测 60ms 左右），
     结果放在后台线程每秒刷新一次的快照里；HTTP 接口只读快照，永远不会被拖动或卡顿。
  2. 界面改成浏览器网页，不再有 WinForms 的消息泵坑（首次点击丢事件、隐藏窗体不派发）。
  3. 注册表仍然只有 services.json 一个，格式与旧版完全兼容，命令行入口 svc 不受影响。
"""
```

---

## `hub/actions.py`

```python
"""启停动作 —— 网页上每一次点击最终都落到这里。

几个和旧版不同的做法：
  1. 不再包一层 cmd.exe。旧版是为了把 stdout 重定向到日志才用 cmd，实际上 subprocess
     直接传文件句柄更干净，还顺带解决了"中文路径参数被代码页吃掉"的老问题。
  2. 强制给子进程设 PYTHONUNBUFFERED=1。动态端口是靠读日志捕获的，Python 输出一旦块缓冲，
     服务起来了端口号却还在缓冲区里，网页就永远抓不到——旧版为此等得很不稳。
  3. 停止时用 psutil 递归杀进程树，比 PowerShell 的 Stop-Process 更彻底（含子进程里的服务）。
"""
import os
import subprocess
import threading  # modified: 启停动作串行化，避免双击/API 并发拉起两份
import time
from datetime import datetime

from . import browsers
from .logger import Logger  # modified: 复用日志尾部读取（大文件不全量载入）
from .monitor import READY_TIMEOUT  # modified: 2.6.3 自动打开的等待上限只留一个定义

try:
    import psutil
except Exception:
    psutil = None

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class Actions:
    def __init__(self, registry, monitor, logger, appdir, logdir, settings=None):
        self.registry = registry
        self.monitor = monitor
        self.logger = logger
        self.appdir = appdir
        self.logdir = logdir
        self.settings = settings
        os.makedirs(self.logdir, exist_ok=True)
        self.tool_runs = {}       # id -> {'popen','logfile','started'}
        self.lock = threading.RLock()  # modified: 同一服务的启停动作串行执行

    # ================================================================ 基础工具
    def _kick_refresh(self):
        """modified: 2.6.3 请求后台采集线程尽快刷一轮（自检桩没有这个方法就跳过）。"""
        kick = getattr(self.monitor, 'request_refresh', None)
        if callable(kick):
            kick()

    def _workdir(self, svc):
        cwd = svc.get('cwd')
        if cwd and os.path.isdir(cwd):
            return cwd
        if cwd:
            self.logger.warn('%s 工作目录不存在：%s，改用 %s' % (svc.get('name'), cwd, self.appdir))
        return self.appdir

    def _logpath(self, svc):
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        base = os.path.join(self.logdir, '%s-%s' % (svc['id'], stamp))
        path = base + '.log'
        idx = 1
        # modified: 2.6.0 同一秒内停止再启动（或工具连点）时，文件名原来会撞车，
        # 前一次输出被覆盖，排查"上一次为什么崩"就没证据了。撞名就加 -1/-2 序号。
        while os.path.exists(path):
            path = '%s-%d.log' % (base, idx)
            idx += 1
        return path

    def _env(self):
        # modified: 2.6.3 原来用 setdefault：用户环境里若已有
        # PYTHONUNBUFFERED='' 或 PYTHONIOENCODING='gbk'，注释里声称的"强制"
        # 根本没生效，动态端口仍会卡在块缓冲里。直接赋值，这才是文件头
        # 第 2 条设计要达到的效果。
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    def _spawn(self, svc, logfile):
        """起一个子进程，输出直接写进日志文件。

        句柄用完立刻在父进程关掉：close_fds=True 时子进程拿到的是自己的副本，
        父进程不关的话每启动一次就漏一个文件句柄（旧版一晚上漏了几十个）。
        """
        cmd = [svc.get('exe')] + list(svc.get('args') or [])
        fh = open(logfile, 'w', encoding='utf-8', errors='replace')
        try:
            return subprocess.Popen(
                cmd,
                cwd=self._workdir(svc),
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._env(),
                close_fds=True,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            )
        finally:
            try:
                fh.close()
            except Exception:
                pass

    # ================================================================ 启动
    def start(self, sid):
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        if svc.get('kind') == 'tool':
            self.logger.warn('%s 是一次性工具，请用"运行"而不是"启动"' % svc.get('name'))
            return {'ok': False, 'message': '%s 是一次性工具，点"运行"执行' % svc.get('name')}

        with self.lock:  # modified: 检查 + 拉起 + 登记必须原子，否则 API 并发会起两份
            # modified: 缓存里的 running 可能滞后 1 秒，以托管记录为准再挡一次
            if self.monitor.managed_alive(sid):
                return {'ok': False, 'message': '%s 已在运行，跳过启动' % svc.get('name')}

            row = self.monitor.service_row(sid)
            # modified: 2.6.3 视图最多滞后 1 秒。刚点完「停止」紧接着「启动」时，
            # view 仍写着"运行中"，会被误拦成"已在运行"，按钮看起来失灵。
            # 只在"这一行是本台托管的、但托管记录已清"时放行，外部实例照旧保护。
            stale_managed = bool(row and row.get('managed') and not self.monitor.managed_alive(sid))
            if row and row.get('running') and not stale_managed:
                return {'ok': False, 'message': '%s 已在运行（%s），跳过启动' % (svc.get('name'), row.get('state'))}

            exe = svc.get('exe')
            if not exe or not os.path.exists(exe):
                return {'ok': False, 'message': '找不到可执行文件：%s' % exe}
            cwd = svc.get('cwd')  # modified
            if cwd and not os.path.isdir(cwd):  # modified: 工作目录失效就别拉了，直接说原因
                return {'ok': False, 'message': '工作目录不存在：%s' % cwd}

            # modified: 2.6.2 端口被别的程序占着时当场拦下。
            # 否则新进程 bind 失败秒退，界面只看到"启动中 → 已停止"，
            # 用户根本不知道是视频号录制器和澄文扫描台同时写死了 8765。
            conflict = self.monitor.port_conflict(svc)
            if conflict:
                owner = '%s（PID %s）' % (conflict.get('name') or '未知程序', conflict.get('pid'))
                self.logger.warn('%s 无法启动：端口 %d 已被 %s 占用'
                                 % (svc.get('name'), conflict['port'], owner))
                return {'ok': False,
                        'message': '端口 %d 已被 %s占用，%s 无法启动；请先停掉占用它的程序或改用其他端口'
                                   % (conflict['port'], owner, svc.get('name'))}

            # 上一轮的动态端口、待开网页标记和退出码不能带过来
            self.monitor.dynamic_port.pop(sid, None)
            self.monitor.pending_open.pop(sid, None)
            self.monitor.last_exit.pop(sid, None)  # modified

            logfile = self._logpath(svc)
            may_have_url = bool(svc.get('url') or svc.get('urlPattern'))
            try:
                popen = self._spawn(svc, logfile)
            except Exception as exc:
                self.logger.error('%s 启动失败：%r' % (svc.get('name'), exc))
                return {'ok': False, 'message': '启动失败：%s' % exc}

            self.monitor.managed[sid] = {
                'popen': popen,
                'pid': popen.pid,
                'started': time.time(),
                'logfile': logfile,
            }

            # modified: 等 0.3 秒看它有没有立刻崩（脚本路径写错、端口被占最常见）。
            # 以前一律报"已启动"，用户看到"启动中"再变"已停止"，不知道发生了什么。
            time.sleep(0.3)
            try:
                code = popen.poll()
            except Exception:
                code = None
            if code is not None:
                self.monitor.managed.pop(sid, None)
                self.monitor.dynamic_port.pop(sid, None)
                self.monitor.pending_open.pop(sid, None)
                self.monitor.last_exit[sid] = code
                tail = ' / '.join(Logger.tail_file(logfile, 3))
                self.logger.error('%s 启动后立即退出（退出码 %s）：%s' % (svc.get('name'), code, tail))
                return {'ok': False,
                        'message': '%s 启动后立即退出（退出码 %s）。%s' % (svc.get('name'), code, tail)}

            # 动态端口服务此刻还没把端口写进日志，先按"将来会有网页"登记，
            # 等采集线程就绪回调时再开——旧版在这里直接取 URL 必然是 None。
            if may_have_url:
                # modified: 2.6.3 与 monitor.READY_TIMEOUT 对齐，别两处各写一个 60
                self.monitor.pending_open[sid] = time.time() + READY_TIMEOUT
            self._kick_refresh()
            self.logger.ok('已启动 %s  PID=%d  日志：%s' % (svc.get('name'), popen.pid, logfile))
            return {'ok': True, 'message': '已启动 %s（PID %d）' % (svc.get('name'), popen.pid)}

    # ================================================================ 一次性工具
    def run_tool(self, sid):
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        if svc.get('kind') != 'tool':  # modified: proc 应该走"启动"，别把常驻服务当一次性脚本跑
            return {'ok': False, 'message': '%s 是常驻服务，请用"启动"' % svc.get('name')}
        exe = svc.get('exe')
        if not exe or not os.path.exists(exe):
            return {'ok': False, 'message': '找不到可执行文件：%s' % exe}
        cwd = svc.get('cwd')  # modified
        if cwd and not os.path.isdir(cwd):  # modified
            return {'ok': False, 'message': '工作目录不存在：%s' % cwd}
        with self.lock:  # modified
            # modified: 2.6.3 日志路径原来在锁外生成：同一秒里两次请求都算出
            # 同一个文件名，前一个工具又恰好秒退，第二份就会把它的日志截断。
            # 和 tool_runs 的检查放进同一临界区才真正防住。
            logfile = self._logpath(svc)
            # modified: 2.6.0 同一个工具还在跑就别再拉一份。
            # NAPS2、佳能扫描助手这类 GUI 工具连点几下「运行」会连开几个窗口，
            # 用户往往以为第一次没点上。已退出的记录顺手清掉，tool_runs 不会越攒越多。
            prev = self.tool_runs.get(sid)
            if prev:
                proc = prev.get('popen')
                try:
                    alive = proc is not None and proc.poll() is None
                except Exception:
                    alive = False
                if alive:
                    return {'ok': False,
                            'message': '%s 正在运行中（PID %s），请先关闭它再运行'
                                       % (svc.get('name'), proc.pid)}
                self.tool_runs.pop(sid, None)
            try:
                popen = self._spawn(svc, logfile)
            except Exception as exc:
                self.logger.error('%s 运行失败：%r' % (svc.get('name'), exc))
                return {'ok': False, 'message': '运行失败：%s' % exc}
            # modified: 2.6.3 启动即崩要当场说清（退出码非 0 才算，
            # 退出码 0 的"启动器秒退"是正常的，不能误报失败）。
            time.sleep(0.2)
            try:
                code = popen.poll()
            except Exception:
                code = None
            if code is not None and code != 0:
                self.tool_runs.pop(sid, None)
                tail = ' / '.join(Logger.tail_file(logfile, 3))
                self.logger.error('%s 运行后立即退出（退出码 %s）：%s' % (svc.get('name'), code, tail))
                return {'ok': False,
                        'message': '%s 运行后立即退出（退出码 %s）。%s' % (svc.get('name'), code, tail)}
            self.tool_runs[sid] = {'popen': popen, 'logfile': logfile, 'started': time.time()}
        self.logger.ok('工具已启动 PID=%d，输出见 %s' % (popen.pid, logfile))
        return {'ok': True, 'message': '%s 已在后台运行，日志：%s' % (svc.get('name'), os.path.basename(logfile))}

    # ================================================================ 停止
    def stop(self, sid, force=False):
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        if svc.get('kind') == 'tool':
            return {'ok': False, 'message': '一次性工具没有停止动作'}

        with self.lock:  # modified: 与 start/restart 串行，避免"刚停又被拉起"
            row = self.monitor.service_row(sid)
            # modified: 2.6.3 与 start 对称：刚启动 1 秒内 view 还是"已停止"，
            # 这时点「停止」会被回一句"当前未运行"，进程却继续跑。
            if not self.monitor.managed_alive(sid) and (not row or not row.get('running')):
                return {'ok': False, 'message': '%s 当前未运行' % svc.get('name')}

            rec = self.monitor.managed.get(sid)
            if rec and self.monitor.managed_alive(sid):
                killed = self._kill_tree(rec.get('pid'), svc.get('name'))
                self.monitor.managed.pop(sid, None)
                self.monitor.dynamic_port.pop(sid, None)
                self.monitor.pending_open.pop(sid, None)
                self.monitor.last_exit.pop(sid, None)  # modified: 主动停止不算异常退出
                self._kick_refresh()
                self.logger.ok('%s 已停止' % svc.get('name'))
                # modified: 2.6.3 没杀干净时别一口咬定"已停止"，如实提示
                return {'ok': True,
                        'message': ('%s 已停止' % svc.get('name')) if killed
                                   else ('%s 可能仍有进程没退出，请稍后刷新确认' % svc.get('name'))}

            # 不是本工作台拉起的：先确认（网页会弹二次确认再带 force 回来）
            pids = row.get('markerPids') or []
            if not pids:
                return {'ok': False, 'message': '%s 未找到可结束的特征进程（可能是外部端口占用）' % svc.get('name')}
            if not force:
                # modified: 2.6.3 确认文案补上进程名与命令行特征，用户按下
                # "确定"之前能看出到底要结束谁，不再只给一串裸 PID。
                marker = str(svc.get('marker') or '')
                return {
                    'ok': False,
                    'needConfirm': True,
                    'pids': pids,
                    'message': '%s 不是本工作台启动的（PID: %s，特征：%s），确认要结束这些进程吗？'
                               % (svc.get('name'), ', '.join(str(p) for p in pids), marker or '无'),
                }
            # modified: 2.6.3 marker 命中的 PID 可能已经退出并被系统复用。
            # 杀之前重查命令行特征，不匹配就跳过，别把新进程当成旧服务。
            failed = []
            for pid in pids:
                if not self._kill_tree(int(pid), svc.get('name'), svc.get('marker')):
                    failed.append(pid)
            self.monitor.dynamic_port.pop(sid, None)
            self.monitor.pending_open.pop(sid, None)
            self._kick_refresh()
            self.logger.ok('%s 已结束外部进程：%s' % (svc.get('name'), pids))
            msg = '%s 已结束' % svc.get('name')
            if failed:
                msg += '（%d 个 PID 已失效或特征不符，未结束：%s）' % (
                    len(failed), ', '.join(str(p) for p in failed))
            return {'ok': True, 'message': msg}

    def restart(self, sid, force=False):
        # modified: 新增动作，等价于"先停再起"；PM2 / Supervisor 都有，日常改完代码不用点两次
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        if svc.get('kind') == 'tool':
            return {'ok': False, 'message': '%s 是一次性工具，没有重启动作' % svc.get('name')}

        with self.lock:  # modified: 2.6.0 停止、等待、再启动必须是一个整体。
            # 原来 stop/start 各自持锁，中间那段"等进程退出"是敞开的：
            # 重启等待期间另一个请求（自动轮询外的手动启动、批量启动）可能插进来，
            # 把刚停掉的服务又拉起来，随后这里再 start 一次，状态就乱了。
            # RLock 可重入，stop/start 内部的 with self.lock 不会死锁。
            row = self.monitor.service_row(sid)
            if row and row.get('running'):
                result = self.stop(sid, force=force)  # modified: 外部实例先弹确认，别直接杀
                if not result.get('ok'):
                    if result.get('needConfirm'):
                        return result  # modified: 把确认请求原样交给前端
                    return {'ok': False, 'message': '重启失败（停止阶段）：%s' % result.get('message')}
                deadline = time.time() + 6
                while time.time() < deadline:
                    row = self.monitor.service_row(sid)
                    # modified: 2.6.3 原来在这里直接调 monitor.refresh()：HTTP
                    # 线程和后台采集线程同时算 rows、写 _view 和弹浏览器。
                    # 改为只置事件让后台线程立刻刷一轮，采集仍单线程。
                    if not row or not row.get('running'):
                        break
                    if not row.get('external') and not self.monitor.managed_alive(sid):
                        break   # 本台托管的已停干净，不必等旧视图刷新
                    self._kick_refresh()
                    time.sleep(0.3)
            time.sleep(0.2)
            result = self.start(sid)
            if result.get('ok'):
                return {'ok': True, 'message': '%s 已重启' % svc.get('name')}
            return {'ok': False, 'message': '重启失败（启动阶段）：%s' % result.get('message')}

    def _kill_tree(self, pid, name, expect_marker=None):
        """结束进程树。返回 True=已结束/本来就不存在，False=拒绝或没杀干净。  # modified

        # modified: 2.6.3 外部实例的 PID 来自 1 秒前的 markerPids，可能已被
        系统复用。传 expect_marker 时重查一次命令行，不符就拒绝结束。
        """
        if not pid:
            return False
        pid = int(pid)
        if pid == os.getpid():
            self.logger.error('拒绝自杀：%s 的 PID 指向服务台本身' % name)
            return False
        self.logger.act('结束进程树 PID=%d（%s）' % (pid, name))
        if psutil is None:
            # modified: 2.6.3 pythonw 下别弹黑色控制台窗口
            result = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=CREATE_NO_WINDOW)
            return result.returncode == 0
        try:
            proc = psutil.Process(pid)
            if expect_marker:
                try:
                    cmd = ' '.join(proc.cmdline() or []).lower()
                except Exception:
                    cmd = ''
                if str(expect_marker).strip().lower() not in cmd:
                    self.logger.warn('PID=%d 的命令行已不含特征「%s」，跳过（可能已被复用）'
                                     % (pid, expect_marker))
                    return False
            targets = proc.children(recursive=True) + [proc]
        except Exception as exc:
            self.logger.warn('PID=%d 已不存在：%r' % (pid, exc))
            return True
        for item in targets:
            try:
                item.kill()
            except Exception:
                pass
        # 等它真的死掉再返回：不等的话网页刷新时还能看到"运行中"，用户会以为没停掉
        try:
            _gone, alive = psutil.wait_procs(targets, timeout=3)
        except Exception:
            alive = []
        if alive:
            self.logger.warn('%s 仍有 %d 个进程没退出' % (name, len(alive)))
            return False
        return True

    # ================================================================ 打开网页
    def open_url(self, sid):
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        # modified: 2.6.3 统一以状态行的 url 为准。以前这里直接调
        # resolve_url(svc)，2.6.2 在状态层做的"冲突时 url=None"被整条绕过：
        # 点「打开网页」仍会打开占用端口的另一个程序，自动打开同样如此。
        row = self.monitor.service_row(sid)
        conflict = row.get('portConflict') if row else None
        if conflict:
            return {'ok': False,
                    'message': '端口 %s 正被 %s（PID %s）占用，%s 的网页入口暂不可用'
                               % (conflict.get('port'), conflict.get('name') or '未知程序',
                                  conflict.get('pid') or '?', svc.get('name'))}
        url = (row.get('url') if row else None) or self.monitor.resolve_url(svc)
        if not url:
            return {'ok': False, 'message': '%s 没有网页入口' % svc.get('name')}
        # modified: 2.6.1 用 open_url_ex 拿到「实际用了哪个浏览器 / 是否退回系统默认」。
        # 原来统一返回「已打开 URL」，把 browsers 给出的真实原因（找不到 ego、调用失败）
        # 全吞掉了——用户看到 Edge 却没有任何提示，只能猜是按钮坏了。
        ok, message, info = browsers.open_url_ex(url, self.settings, self.logger)
        if ok:
            self.logger.ok('已打开：%s（%s）' % (url, message))
        else:
            self.logger.error('打开网页失败：%s' % message)
        return {
            'ok': ok,
            'message': message,
            'id': sid,
            'url': url,
            'browser': info.get('name') or '',
            'fallback': bool(info.get('fallback')),
        }

    # ================================================================ 批量
    def start_all(self):
        started, skipped, failed = [], [], []
        for svc in self.registry.services():
            if svc.get('kind') == 'tool':
                skipped.append(svc.get('name'))
                continue
            if not svc.get('autostart', True):
                skipped.append(svc.get('name'))
                continue
            row = self.monitor.service_row(svc['id'])
            if row and row.get('running'):
                skipped.append(svc.get('name'))
                continue
            result = self.start(svc['id'])
            (started if result['ok'] else failed).append(svc.get('name'))
        parts = ['已启动：%s' % '、'.join(started)] if started else []
        if skipped:
            parts.append('跳过：%s' % '、'.join(skipped))
        if failed:
            parts.append('失败：%s' % '、'.join(failed))
        return {'ok': True, 'message': '；'.join(parts) or '没有可启动的服务'}

    def stop_all(self):
        # modified: 2.6.0 「全部停止」只动本工作台拉起的进程，不再 force 强杀外部实例。
        # 旧写法是 stop(..., force=True)：用户在别的窗口手动起的服务、或别的工具拉起的
        # 同名进程，也会被一并结束。面板是"我的工作台"，不该替用户决定别的进程生死。
        # 外部运行的服务只列名提示；要结束仍可点该卡片的「停止」，走二次确认。
        stopped, failed, external = [], [], []
        for svc in self.registry.services():
            if svc.get('kind') == 'tool':
                continue
            sid = svc['id']
            if not self.monitor.managed_alive(sid):  # modified
                row = self.monitor.service_row(sid)
                if row and row.get('running'):
                    external.append(svc.get('name'))  # modified: 不是本台拉起的，跳过
                continue
            result = self.stop(sid, force=True)
            (stopped if result['ok'] else failed).append(svc.get('name'))
        parts = ['已停止：%s' % '、'.join(stopped)] if stopped else []
        if failed:
            parts.append('失败：%s' % '、'.join(failed))
        if external:
            parts.append('外部运行未动：%s' % '、'.join(external))  # modified
        return {'ok': True, 'message': '；'.join(parts) or '没有本工作台启动的服务在运行',
                'external_skipped': external}  # modified: 附加字段，旧调用方不受影响

    # ================================================================ 日志
    def latest_log(self, sid):
        """取某个服务最近一次输出的日志路径（先看本次会话，再看磁盘上最新的）。"""
        rec = self.monitor.managed.get(sid)
        if rec and rec.get('logfile') and os.path.exists(rec['logfile']):
            return rec['logfile']
        run = self.tool_runs.get(sid)
        if run and run.get('logfile') and os.path.exists(run['logfile']):
            return run['logfile']
        try:
            cand = [os.path.join(self.logdir, fn) for fn in os.listdir(self.logdir)
                    if fn.startswith(sid + '-') and fn.endswith('.log')]
            # modified: 2.6.3 列目录后文件可能刚好被清掉，原来 max() 一抛
            # OSError 就把整批候选吞成"暂无日志"。逐个取 mtime，跳过失效项。
            stamps = []
            for path in cand:
                try:
                    stamps.append((os.path.getmtime(path), path))
                except OSError:
                    continue
            if stamps:
                return max(stamps)[1]
        except Exception:
            pass
        return None

    def read_log(self, sid, lines=200):
        path = self.latest_log(sid)
        if not path or not os.path.exists(path):
            return {'ok': False, 'message': '暂无日志'}
        try:
            data = Logger.tail_file(path, lines)  # modified: 大日志只读尾部，不再整份进内存
            return {'ok': True, 'logfile': os.path.basename(path), 'lines': data}
        except Exception as exc:
            return {'ok': False, 'message': '读取日志失败：%s' % exc}
```

---

## `hub/autostart.py`

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开机自启 —— 只写当前用户的 HKCU\\...\\Run，不碰系统级、不需要管理员权限。

为什么用 HKCU Run，而不是计划任务 / Windows 服务：
  · 计划任务要走 schtasks，杀软常拦，参数也啰嗦；
  · 注册成服务要管理员权限，还会让"点退出"失去意义；
  · HKCU 是用户级、免提权、随用户登录拉起，正好匹配"托盘常驻"的定位；
  · 关掉只需要删一个值，不残留任何东西。

注册表位置：
  HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  值名 ServiceHub，类型 REG_SZ，内容形如
      "C:\\...\\pythonw.exe" "D:\\Tools\\服务台\\ServiceHub.py" --no-open

为什么用 pythonw.exe：python.exe 会带一个黑框，开机弹一下很难看。
为什么带 --no-open：开机时只让托盘出现，不要抢着把浏览器弹出来。
"""

import os
import sys

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'ServiceHub'

try:
    import winreg
except Exception:       # 非 Windows 平台
    winreg = None

SUPPORTED = winreg is not None


def _pythonw():
    """优先返回同目录下的 pythonw.exe；找不到就退回当前解释器。"""
    exe = sys.executable or ''
    folder, name = os.path.split(exe)
    if name.lower() in ('python.exe', 'pythonw.exe'):
        cand = os.path.join(folder, 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return exe


def command(appdir):
    """拼出开机要执行的命令行（带引号，路径含空格/中文都不会断）。"""
    script = os.path.join(appdir, 'ServiceHub.py')
    return '"%s" "%s" --no-open' % (_pythonw(), script)


def current():
    """读注册表里已登记的命令；没登记或读不到则返回空串。"""
    if winreg is None:
        return ''
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    except OSError:
        return ''
    try:
        try:
            value = winreg.QueryValueEx(key, VALUE_NAME)[0]
        except OSError:
            return ''
        return str(value or '').strip()
    finally:
        winreg.CloseKey(key)


def is_enabled():
    return bool(current())


def enable(appdir):
    """登记开机自启。返回 (成功?, 给用户看的一句话)。"""
    if winreg is None:
        return False, '只有 Windows 才支持写注册表自启'
    cmd = command(appdir)
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError as exc:
        return False, '打不开注册表启动项：%s' % exc
    try:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as exc:
        return False, '写入注册表失败：%s' % exc
    finally:
        winreg.CloseKey(key)
    return True, '已设为开机自启：下次登录 Windows 时托盘自动出现（不会自动弹浏览器）'


def disable():
    """取消开机自启。返回 (成功?, 给用户看的一句话)。"""
    if winreg is None:
        return False, '只有 Windows 才支持写注册表自启'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return True, '开机自启本来就是关着的'
    try:
        winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, '开机自启本来就是关着的'
    except OSError as exc:
        return False, '删除注册表值失败：%s' % exc
    finally:
        winreg.CloseKey(key)
    return True, '已取消开机自启'


def status(appdir=None):
    """给界面用的完整状态。

    stale 表示"注册表里登记的命令"和"当前程序路径"对不上——
    典型场景是把 D:\\Tools\\服务台 整个目录挪走了，这时自启会指向旧路径，
    界面上要提示用户重新勾一次（重新勾就等于重写命令）。
    """
    cmd = current()
    want = command(appdir) if appdir else ''
    return {
        'supported': SUPPORTED,
        'enabled': bool(cmd),
        'command': cmd,
        'expected': want,
        'stale': bool(cmd and want and cmd != want),
    }
```

---

## `hub/browsers.py`

```python
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
```

---

## `hub/logger.py`

```python
"""日志：服务台自身写一份，子进程输出另存一份。

为什么不直接用 logging：这里要保证"任何时刻都能拿到最近 N 行给网页看"，
自己实现反而更简单可靠，也避免别人的全局 logging 配置把我们污染。
"""
import os
import threading
import time
from datetime import datetime

LEVEL_ORDER = {'DEBUG': 10, 'INFO': 20, 'ACT': 25, 'OK': 30, 'WARN': 40, 'ERROR': 50}


class Logger:
    """按天切分的文件日志，UTF-8，写完立即 flush（排障时不能少最后一屏）。"""

    def __init__(self, logdir, name='hub'):
        self.logdir = logdir
        self.name = name
        self.lock = threading.Lock()
        os.makedirs(logdir, exist_ok=True)
        self._date = datetime.now().strftime('%Y%m%d')  # modified
        self.path = os.path.join(logdir, '%s-%s.log' % (name, self._date))  # modified

    def _roll_if_needed(self):
        # modified: 以前 path 在 __init__ 就定死，服务台连跑几天日志全写进同一天
        day = datetime.now().strftime('%Y%m%d')
        if day != self._date:
            self._date = day
            self.path = os.path.join(self.logdir, '%s-%s.log' % (self.name, day))

    # ---------------------------------------------------------------- 写入
    def write(self, level, message):
        line = '%s  %-5s  %s' % (datetime.now().strftime('%H:%M:%S'), level, message)
        with self.lock:
            self._roll_if_needed()  # modified
            try:
                with open(self.path, 'a', encoding='utf-8') as fh:
                    fh.write(line + '\n')
            except Exception:
                pass
        return line

    def debug(self, msg):
        self.write('DEBUG', msg)

    def info(self, msg):
        self.write('INFO', msg)

    def act(self, msg):
        self.write('ACT', msg)

    def ok(self, msg):
        self.write('OK', msg)

    def warn(self, msg):
        self.write('WARN', msg)

    def error(self, msg):
        self.write('ERROR', msg)

    # ---------------------------------------------------------------- 读取
    @staticmethod
    def tail_file(path, lines=200, max_bytes=256 * 1024):
        # modified: 大日志不再整份读进内存，只从尾部取 256KB 再按行切。
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, 'rb') as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - max_bytes)
                fh.seek(start)
                # modified: 2.6.3 从 256KB 边界起读，第一行通常是半截日志，
                # 甚至落在中文字符中间被 decode 成 U+FFFD。丢掉首段残行。
                if start > 0:
                    fh.readline()
                data = fh.read().decode('utf-8', errors='replace')
            rows = data.splitlines()
            if len(rows) > lines:
                rows = rows[-lines:]
            return rows
        except Exception as exc:
            return ['读取日志失败：%s' % exc]

    def tail(self, lines=200):
        """返回最近 lines 行（给网页日志窗用）。"""
        with self.lock:  # modified: 跨天切换 path 时避免读到一半文件换了
            self._roll_if_needed()
            path = self.path
        return self.tail_file(path, lines)

    def clean_old(self, keep_days=14):
        """清理超过保留天数的旧日志，只在启动时跑一次。  # modified: 子进程日志也一起清"""
        now = time.time()
        limit = keep_days * 86400
        try:
            for fn in os.listdir(self.logdir):
                full = os.path.join(self.logdir, fn)
                if not os.path.isfile(full):
                    continue
                if not fn.endswith('.log'):  # modified: 以前只清 hub-*.log，子进程日志会无限长
                    continue
                if now - os.path.getmtime(full) > limit:
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass
```

---

## `hub/meta.py`

```python
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
```

---

## `hub/monitor.py`

```python
"""状态采集引擎 —— 后台线程每秒一轮，接口只读结果。

为什么这样分层：
  旧 WinForms 版把进程扫描放在界面定时器里，一轮 700ms，界面周期性假死；后来改成
  PowerShell 后台 Runspace 才解决。Python 版直接在自己的线程里做，天然不占界面，
  而且 process_iter 一轮实测 60ms 左右（PowerShell WMI 是 600ms 量级），成本可忽略。
"""
import os
import re
import threading
import time

try:
    import psutil
except Exception as exc:  # 依赖随工具自带，理论上不会走到这里
    psutil = None
    _IMPORT_ERROR = str(exc)

REFRESH_INTERVAL = 1.0
READY_TIMEOUT = 60.0        # 启动后等网页地址的最长时间（动态端口服务起得慢）


def _safe_int(value):
    """把注册表里的端口等字段安全转成 int；非法值返回 None。  # modified

    services.json 是手改的，port 写成 "87a"、""、true 都可能。以前直接 int()
    会让整轮采集抛异常，界面停在上一帧，看起来像"服务台卡死"。
    # modified: 2.6.3 补上范围校验。0 / -1 / 70000 这类能转成 int 的数根本
    不可能有人监听，旧逻辑会让它一直走 expects_port 分支，进程明明活着却
    永远显示「启动中」；resolve_url 还能拼出 http://127.0.0.1:-1/。
    """
    try:
        if isinstance(value, bool):
            return None
        num = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if 1 <= num <= 65535 else None


class Snapshot:
    """一次采集的原始材料。"""

    def __init__(self):
        self.stamp = 0.0
        self.procs = []        # [(pid, name, cmdline)]
        self.by_pid = {}       # pid -> name
        self.ppid = {}         # pid -> 父进程（2.6.2：判断端口监听者是不是本服务的子进程）
        self.cmd_by_pid = {}   # pid -> 小写命令行（2.6.2：端口归属校验用，避免每条服务重复拼串）
        self.listen = {}       # 端口 -> pid
        self.listen_by_pid = {}  # pid -> [端口]（动态端口服务靠它反查外部实例）
        self.cost_ms = 0.0


class Monitor:
    def __init__(self, registry, logger):
        self.registry = registry
        self.logger = logger
        self.lock = threading.RLock()
        self.managed = {}          # id -> {'popen','pid','started','logfile'}
        self.dynamic_port = {}     # id -> 端口号（动态端口服务）
        self.pending_open = {}     # id -> 截止时间（启动后自动打开网页）
        self.last_exit = {}        # id -> 上次托管进程退出码（排查"启动即崩"用）  # modified
        self._patterns = {}        # urlPattern -> 已编译正则（每秒采集，别重复编译）  # modified
        self.auto_open = True
        self.suppress_open = False  # 自检/调试用：为 True 时一律不弹浏览器（不受设置同步影响）
        self._snapshot = Snapshot()
        self._view = {'generated': 0, 'services': []}
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()   # modified: 2.6.3 让 HTTP 线程"请求刷新"而不是自己采集
        self.on_open = None        # 回调：自动打开网页（由 Actions.open_url 提供）
        self.psutil_error = None if psutil else _IMPORT_ERROR

    # ================================================================ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='monitor', daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()

    def request_refresh(self):
        """modified: 2.6.3 请求后台线程尽快刷新一次。

        以前 restart() 在 HTTP 请求线程里直接调 refresh()：两个线程同时算
        rows / 写 _view / 触发自动打开，界面可能回退一帧，pending_open 也可能
        被弹两次。这里只置一个事件，采集仍严格由 _loop 单线程完成。
        """
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                self.logger.error('采集异常：%r' % exc)
            self._wake.clear()
            self._wake.wait(REFRESH_INTERVAL)

    # ================================================================ 采集
    def _take_snapshot(self):
        snap = Snapshot()
        began = time.perf_counter()
        snap.stamp = time.time()
        if psutil is None:
            snap.cost_ms = 0.0
            return snap
        attrs = ['pid', 'ppid', 'name', 'cmdline']
        try:
            for proc in psutil.process_iter(attrs=attrs):
                try:
                    info = proc.info
                    pid = info.get('pid')
                    if pid is None:
                        continue
                    cmd = info.get('cmdline') or []
                    joined = ' '.join(cmd)
                    snap.procs.append((pid, info.get('name') or '', joined))
                    snap.by_pid[pid] = info.get('name') or ''
                    snap.ppid[pid] = info.get('ppid')
                    snap.cmd_by_pid[pid] = joined.lower()
                except Exception:
                    continue
        except Exception as exc:
            self.logger.warn('进程快照失败：%r' % exc)
        try:
            for conn in psutil.net_connections(kind='tcp'):
                try:
                    if conn.status == 'LISTEN' and conn.laddr:
                        # modified: 2.6.3 同一端口常有 IPv4 + IPv6 两条 LISTEN。
                        # 原来 setdefault 会把第一条（权限不足时 pid=None）固化，
                        # 第二条带真实 PID 的反而被丢掉，端口冲突检测就此失效。
                        port = conn.laddr.port
                        if snap.listen.get(port) is None:
                            snap.listen[port] = conn.pid
                        if conn.pid:
                            snap.listen_by_pid.setdefault(conn.pid, []).append(port)
                except Exception:
                    continue
        except Exception as exc:
            self.logger.warn('端口快照失败：%r' % exc)
        snap.cost_ms = (time.perf_counter() - began) * 1000
        return snap

    def refresh(self):
        snap = self._take_snapshot()
        services = self.registry.services()
        with self.lock:
            self._snapshot = snap

        # 进程自己退了（崩溃/被手动关掉）就把托管记录清掉，否则界面会一直挂着旧 PID
        self._reap_dead()

        # 逐条算状态。这一段不持锁：算一行只要微秒级，但不能让 /api/status 排在后面等
        rows = []
        for svc in services:
            # 动态端口服务：每轮都试着从子进程日志里抠端口号，给 _compute 用
            if svc.get('urlPattern'):
                try:
                    self.capture_dynamic_port(svc)
                except Exception as exc:
                    self.logger.debug('捕获动态端口失败：%r' % exc)
            rows.append(self._compute(svc, snap))

        # 整体换页，读接口永远看到完整的一致快照
        with self.lock:
            self._view = {'generated': snap.stamp, 'services': rows, 'cost_ms': snap.cost_ms}
        self._fire_auto_open({svc['id']: svc for svc in services})

    def _reap_dead(self):
        for sid, rec in list(self.managed.items()):
            popen = rec.get('popen')
            if popen is None:
                continue
            try:
                code = popen.poll()
            except Exception:
                code = 0
            if code is None:
                continue
            with self.lock:
                # modified: 2.6.3 stop() 与这里用的是两把锁。用户点停止后
                # _reap_dead 可能已经取过快照，随后把主动 kill 的退出码写回
                # last_exit，界面就误报"上次异常退出"。写之前复核记录还在。
                if self.managed.get(sid) is not rec:
                    continue
                self.managed.pop(sid, None)
                self.dynamic_port.pop(sid, None)
                self.pending_open.pop(sid, None)
                self.last_exit[sid] = code
            self.logger.warn('%s 的进程已自行退出（退出码 %s），托管记录已清理' % (sid, code))

    # ================================================================ 状态判定
    def managed_alive(self, sid):
        rec = self.managed.get(sid)
        if not rec:
            return False
        popen = rec.get('popen')
        if popen is not None:
            try:
                return popen.poll() is None
            except Exception:
                return False
        pid = rec.get('pid')
        return bool(pid and self._snapshot.by_pid.get(pid))

    def _marker_pids(self, svc, snap):
        """命令行特征命中的 PID 列表（2.6.2 从 _compute 抽出来，启动前预检也要用）。"""
        marker = str(svc.get('marker') or '').strip().lower()
        if not marker:
            return []
        # modified: 2.6.3 exe 写成带引号的路径时 basename 会得到 node.exe"，
        # marker 永远命中不了，服务被误判成"已停止"。
        exe_leaf = os.path.basename(str(svc.get('exe') or '').strip('"').strip()).lower()
        pids = []
        for pid, name, cmd in snap.procs:
            if exe_leaf and (name or '').lower() != exe_leaf:
                continue
            if marker in (cmd or '').lower():
                pids.append(pid)
        return pids

    def _is_descendant(self, pid, ancestors, snap, depth=6):
        """pid 是否是 ancestors 里某个进程的后代（2.6.2）。

        uvicorn --reload、multiprocessing 这类服务，真正监听端口的是子进程，
        子进程命令行里没有 marker。只按命令行判断会把"自己人"误判成外部占用。
        """
        seen = set()
        current = pid
        parents = getattr(snap, 'ppid', None) or {}   # modified: 自检的 FakeSnap 没有这个字段
        for _ in range(depth):
            parent = parents.get(current)
            if not parent or parent in seen:
                return False
            if parent in ancestors:
                return True
            seen.add(parent)
            current = parent
        return False

    def _owner_name(self, svc, pid, snap):
        """占用端口的进程叫什么（2.6.2）。

        优先认成"另一个服务"：同一个进程按 exe 名只会显示 pythonw.exe，
        用户还得自己猜是哪个工具；用 marker 对上号就能直接写「视频号录制器」。
        """
        cmd = (getattr(snap, 'cmd_by_pid', None) or {}).get(pid, '')
        leaf = self._by_pid(snap, pid)
        try:
            for other in self.registry.services():
                if other.get('id') == svc.get('id'):
                    continue
                om = str(other.get('marker') or '').strip().lower()
                oexe = os.path.basename(str(other.get('exe') or '')).lower()
                if om and oexe == leaf and om in cmd:
                    return other.get('name') or om
        except Exception:
            pass
        return (getattr(snap, 'by_pid', None) or {}).get(pid) or '未知程序'

    def _by_pid(self, snap, pid):
        # modified: 自检的 FakeSnap 可能没有 by_pid，统一走安全访问器
        return ((getattr(snap, 'by_pid', None) or {}).get(pid) or '').lower()

    def _owns_port(self, svc, snap, pid, marker_pids, rec):
        """监听该端口的进程是不是本服务（2.6.2）。

        以前只看"端口有人听"：视频号录制器和澄文扫描台都用 8765 时，后者的卡片
        会把录制器认成自己（端口响着、markerPids 里甚至是别人的 PID），
        点「停止」直接把另一个工具杀掉，点「打开网页」打开的是别人的页面。
        拿不到 PID（权限不足）时按"是它的"处理，宁可漏报也不误杀。
        """
        if pid is None:
            return True
        # modified: 没采到任何进程信息时（psutil 缺权限/自检桩数据）无从判断，按"是它的"处理
        if not getattr(snap, 'procs', None):
            return True
        if rec and pid == rec.get('pid'):
            return True
        if pid in marker_pids:
            return True
        # modified: 2.6.3 子进程链判断原先被写在"exe 名相同"的 if 里：
        # uvicorn --reload 的 worker 叫 python.exe、父进程是 pythonw.exe 时
        # 明明是自己人却被判成端口冲突；无 marker 的服务更是完全不查后代。
        # 先查后代链，再退回命令行/exe 名判断。
        owners = set(marker_pids)
        if rec and rec.get('pid'):
            owners.add(rec['pid'])
        if self._is_descendant(pid, owners, snap):
            return True
        marker = str(svc.get('marker') or '').strip().lower()
        leaf = os.path.basename(str(svc.get('exe') or '').strip('"').strip()).lower()
        # modified: 2.6.3 既没 marker 又没 exe 时根本无从判断归属，
        # 沿用 2.6.2「宁可漏报不误杀」的原则按"是它的"处理，别把
        # 自己抓到的动态端口判成冲突、导致永远不再读日志。
        if not marker and not leaf:
            return True
        if marker:
            return marker in (getattr(snap, 'cmd_by_pid', None) or {}).get(pid, '')
        # 没配 marker 的服务（如本机 OCR）只能按可执行文件名判断
        return bool(leaf and leaf == self._by_pid(snap, pid))

    def port_conflict(self, svc):
        """启动前预检：固定端口是否正被别的程序占着（2.6.2）。

        返回 {'port','pid','name'} 或 None。端口被占时新进程只会 bind 失败秒退，
        与其让用户看"启动中 → 已停止"，不如当场说清是谁占的。
        """
        port = _safe_int(svc.get('port'))
        if not port:
            return None
        snap = self._take_snapshot()
        # modified: 2.6.3 listen 里 key 存在但值为 None，表示"有人听、拿不到 PID"，
        # 这种情形照样不该报冲突（宁可漏报不误杀），但和"没人听"要分开判断。
        if port not in snap.listen:
            return None
        pid = snap.listen.get(port)
        if pid is None:
            return None
        rec = self.managed.get(svc['id'])
        if self._owns_port(svc, snap, pid, self._marker_pids(svc, snap), rec):
            return None
        return {'port': port, 'pid': pid, 'name': self._owner_name(svc, pid, snap)}

    def _compute(self, svc, snap):
        sid = svc['id']
        row = {
            'id': sid,
            'name': svc.get('name', sid),
            'group': svc.get('group', '未分组'),
            'desc': svc.get('desc', ''),
            'kind': svc.get('kind', 'proc'),
            'version': svc.get('version') or '—',
            'updated': svc.get('updated') or '—',
            'confirm': svc.get('confirm') or '',
            'state': '已停止',
            'running': False,
            'ready': False,
            'managed': False,
            'external': False,
            'autostart': bool(svc.get('autostart', True)),
            'port': None,
            'portText': '—',
            'url': None,
            'pid': None,
            'uptime': 0,
            'lastExit': None,   # modified: 上次托管进程退出码，仅异常时给前端提示
        }
        # 可执行文件不在本机：从 GitHub 拉到笔记本、或工具被挪走后旧路径失效时，
        # 界面要直说"未安装"，而不是显示"已停止"让人以为点一下就能起来。
        exe = str(svc.get('exe') or '')
        cwd = str(svc.get('cwd') or '')  # modified
        # modified: 以前只查 exe，cwd 断了（junction 失效 / 工具被挪走）仍显示"已停止"，
        # 点启动后脚本找不到、进程秒退，用户不知道为什么。cwd 一起查，直接说"未安装"。
        row['missing'] = ((bool(exe) and not os.path.exists(exe))
                          or (bool(cwd) and not os.path.isdir(cwd)))
        if row['kind'] == 'tool':
            row['state'] = '未安装' if row['missing'] else '工具'
            row['portText'] = '—'
            row['url'] = svc.get('url')
            return row

        # 本服务台拉起的进程
        rec = self.managed.get(sid)
        alive = self.managed_alive(sid)
        if rec:
            row['pid'] = rec.get('pid')
            if alive:
                row['uptime'] = int(time.time() - rec.get('started', time.time()))
        last_exit = self.last_exit.get(sid)  # modified
        if last_exit is not None:  # modified
            row['lastExit'] = last_exit  # modified

        # 命令行特征：不论 health 是 cmdline 还是 port，只要有 marker 就比。
        # 动态端口服务以前只认自己拉起的进程，外部先起了一份就完全看不见，
        # 界面上显示"已停止"、实际上端口被占（2026-10-31 实测踩到）。
        # modified: 2.6.2 抽到 _marker_pids()，启动前预检复用同一套判断
        marker_pids = self._marker_pids(svc, snap)
        marker_hit = bool(marker_pids)

        # 端口：固定端口 > 日志里抓到的动态端口 > 外部进程实际在听的端口
        port_val = svc.get('port')
        if not port_val and sid in self.dynamic_port:
            port_val = self.dynamic_port.get(sid)
        if not port_val and marker_pids:
            for pid in marker_pids:
                ports = snap.listen_by_pid.get(pid) or []
                if ports:
                    port_val = sorted(ports)[0]
                    break
        port_num = _safe_int(port_val)  # modified: 非法端口转 None，不再让整轮采集抛异常
        listen_pid = snap.listen.get(port_num) if port_num else None  # modified: 2.6.2
        port_ok = bool(port_num and port_num in snap.listen)  # modified
        # modified: 2.6.2 端口响着 ≠ 是本服务在听。
        # 视频号录制器（旧版也写死 8765）和澄文扫描台撞端口时，澄文卡片会
        # 把录制器认成"外部运行"、markerPids 还是录制器的 PID：点停止杀错人、
        # 点打开网页进错页面。监听者不是自己人时，按「端口冲突」如实显示。
        conflict_info = None   # modified: 2.6.3 原来叫 port_conflict，和方法同名易误读
        if port_ok and not self._owns_port(svc, snap, listen_pid, marker_pids, rec):
            conflict_info = {'port': port_num, 'pid': listen_pid,
                             'name': self._owner_name(svc, listen_pid, snap)}
            port_ok = False

        detected = bool(alive or port_ok or marker_hit)
        # 动态端口服务也算"要端口"：没抓到端口之前只能算启动中，不能报"运行中"
        # （旧 PowerShell 版在这一步会提前判定就绪，结果是对方 pages 都还没监听就提示可用）
        expects_port = bool(port_val or svc.get('urlPattern'))
        # modified: 2.6.0 端口被手写成非数字（"87a"）时，port_num 为 None、port_ok 永远 False，
        # 旧逻辑 expects_port 恒为 True → 进程明明活着，状态却永远停在「启动中」。
        # 非法端口按"没有端口可探"处理，让状态如实显示；portText 仍原样显示非法值，
        # 用户一眼能看出是配置写错了，而不是服务没起来。
        if port_val and port_num is None and not svc.get('urlPattern'):
            expects_port = False
        ready = detected and (port_ok or not expects_port)

        if detected and ready:
            if alive:
                row['state'] = '运行中'
                row['managed'] = True
            else:
                row['state'] = '外部运行'
                row['external'] = True
            row['running'] = True
            row['ready'] = True
        elif detected:
            # 进程起来了但端口还没通 —— 这是"启动中"，不能说成"外部运行"
            row['state'] = '启动中'
            row['running'] = True
            row['managed'] = bool(alive)

        if port_num:  # modified
            row['port'] = port_num  # modified
            row['portText'] = str(port_num)  # modified
        elif svc.get('urlPattern'):
            row['portText'] = '动态'
        elif port_val:  # modified: 配了端口但不是合法数字，原样显示便于发现配置错误
            row['portText'] = str(port_val)  # modified

        if svc.get('url'):
            row['url'] = svc['url']
        elif svc.get('urlPattern'):
            # 动态端口服务只在真的跑着时才给地址，停了就别留一个点不开的链接
            row['url'] = ('http://127.0.0.1:%d/' % port_num) if (row['running'] and port_num) else None  # modified
        else:
            row['url'] = self.resolve_url(svc)

        # modified: 2.6.2 端口被别人占着时单独给一个状态，别混进"运行中/外部运行"。
        # modified: 2.6.3 修回归：服务压根没启动（detected=False）但端口被别的程序
        # 占着时，旧代码无条件把 state 改成「端口冲突」，界面常驻红色告警、
        # running 却是 False，「停止」点不动、「全部启动」又把它算进去。
        # 现在只有本服务确实有进程/特征命中时才升级为冲突态；无论哪种情况，
        # 冲突详情都挂到 portConflict 上（胶囊 title 会说明是谁占着），url 一律掐掉。
        if conflict_info:
            row['portConflict'] = conflict_info
            row['url'] = None   # 这个地址现在通向别人的程序，别给「打开网页」
            if detected:
                row['state'] = '端口冲突'
                row['running'] = bool(alive or marker_hit)
                row['ready'] = False
                row['managed'] = bool(alive)
                row['external'] = False
        elif row['missing'] and not row['running']:
            row['state'] = '未安装'   # modified: 2.6.2 从函数末尾挪上来，端口冲突优先

        # 外部运行 + 端口型服务：把监听端口的进程找出来，停止时才有 PID 可杀
        if row['external'] and port_num and not marker_pids:  # modified: 2.6.0 用已校验的 port_num
            # modified: 2.6.0 原来这里写 int(port_val)——port 在 services.json 里被手写成
            # "87a" 这类非法值时，port_num 已是 None 安全落地，这行却又 int() 一次，
            # 直接 ValueError 让整轮 refresh 抛异常，界面永远停在上一帧（看起来像卡死）。
            listen_pid = snap.listen.get(port_num)
            if listen_pid:
                marker_pids = [listen_pid]
        if marker_pids:
            row['markerPids'] = marker_pids
        return row

    def resolve_url(self, svc):
        if svc.get('url'):
            return svc['url']
        port = _safe_int(self.dynamic_port.get(svc['id']))  # modified
        if port and svc.get('urlPattern'):
            return 'http://127.0.0.1:%d/' % port  # modified
        # modified: 2.6.1 固定端口 + 注册表里明确标了 "web": true 的服务（如本机 OCR 8111），
        # 运行中直接由端口拼出地址。以前只认 url/urlPattern，这类服务即便跑着，
        # 「打开网页」也永远是灰的——用户点了没反应，看起来就是按钮坏了。
        # 没标 web 的一律不猜：数据库、消息队列的端口不是网页。
        if svc.get('web'):
            fixed = _safe_int(svc.get('port'))
            try:
                snap = self._snapshot
                listening = bool(fixed and fixed in snap.listen)
            except Exception:
                listening = False
            # modified: 2.6.3 原来只判"端口有人听"，端口被别的程序占着时会
            # 拼出指向别人的地址。open_url / 自动打开都可能走进去，状态层的
            # url=None 防护被整条绕过。这里补一次归属校验。
            if listening and self._owns_port(
                    svc, snap, snap.listen.get(fixed),
                    self._marker_pids(svc, snap), self.managed.get(svc['id'])):
                return 'http://127.0.0.1:%d/' % fixed
        return None

    # ================================================================ 就绪回调
    def _fire_auto_open(self, by_id):
        """登记过"启动后自动打开网页"的服务，一旦拿到网页地址就打开。

        注意这里是每轮都检查 pending 表，而不是只在"未就绪→就绪"的边沿触发一次：
        动态端口服务是先起来、过几秒才把端口写进日志，边沿那一刻 URL 还是空的，
        只触发一次就会把待开标记白白丢掉（旧版踩过这个坑）。
        """
        if not self.pending_open:
            return
        if self.suppress_open:
            return
        for sid in list(self.pending_open.keys()):
            deadline = self.pending_open.get(sid)
            svc = by_id.get(sid)
            if not svc:
                self.pending_open.pop(sid, None)
                continue
            if deadline and time.time() > deadline:
                self.pending_open.pop(sid, None)
                self.logger.warn('%s 启动后 %d 秒内没等到网页地址，放弃自动打开'
                                 % (svc.get('name'), int(READY_TIMEOUT)))
                continue
            # modified: 2.6.3 统一以 _compute 产出的状态行 url 为唯一入口。
            # 以前这里直接 resolve_url(svc)，端口冲突时状态行已经把 url 掐成
            # None，这条路径却照旧拼静态地址，浏览器弹出的是占用者的页面。
            row = self.service_row(sid)
            if not row or not row.get('url'):
                continue
            self.pending_open.pop(sid, None)
            if not self.auto_open or not callable(self.on_open):
                continue
            try:
                self.on_open(sid)
            except Exception as exc:
                self.logger.error('自动打开网页失败：%r' % exc)

    # ================================================================ 对外数据
    def view(self):
        with self.lock:
            services = [dict(row) for row in self._view.get('services', [])]
            out = dict(self._view)
            out['services'] = services
            out['cost_ms'] = round(float(self._view.get('cost_ms', 0) or 0), 1)
            return out

    def service_row(self, sid):
        for row in self.view()['services']:
            if row['id'] == sid:
                return row
        return None

    def running_count(self):
        return sum(1 for row in self.view()['services'] if row.get('running'))

    # ================================================================ 动态端口
    def capture_dynamic_port(self, svc):
        """从子进程日志里抠动态端口（如"服务已启动 http://127.0.0.1:8771"）。"""
        pattern = svc.get('urlPattern')
        if not pattern:
            return False
        # modified: 2.6.0 端口已抓到、且本轮快照里它确实在监听，就不必再读一次日志。
        # 原来每个带 urlPattern 的服务每一轮（1 秒）都要打开文件读尾部 16KB，
        # 服务越多浪费越明显。进程重启后 actions.start 会清掉 dynamic_port、
        # managed 记录也会重建，所以这里沿用旧端口不会漏掉"新实例换了端口"。
        known = _safe_int(self.dynamic_port.get(svc['id']))
        if known and known in self._snapshot.listen:
            # modified: 2.6.3 旧端口被别的程序占了就别再当"已抓到"，
            # 否则服务换端口后永远读不到新日志，还会沿用一个错误地址。
            snap = self._snapshot
            rec = self.managed.get(svc['id'])
            if self._owns_port(svc, snap, snap.listen.get(known),
                               self._marker_pids(svc, snap), rec):
                return True
        rec = self.managed.get(svc['id'])
        logfile = rec.get('logfile') if rec else None
        if not logfile or not os.path.exists(logfile):
            return False
        try:
            with open(logfile, 'rb') as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 16384))
                tail = fh.read().decode('utf-8', errors='replace')
        except Exception:
            return False
        compiled = self._compile_pattern(pattern)  # modified
        if compiled is None:  # modified: 正则写错时只跳过这一条，不影响整轮采集
            return False  # modified
        match = compiled.search(tail)  # modified
        if not match:
            return False
        port = _safe_int(match.group(1))  # modified
        if port is None:  # modified
            return False
        if self.dynamic_port.get(svc['id']) != port:
            self.dynamic_port[svc['id']] = port
            self.logger.ok('%s 动态端口：%d' % (svc.get('name'), port))
        return True

    def _compile_pattern(self, pattern):
        # modified: urlPattern 每秒都要用，编译一次缓存；非法正则返回 None，不影响整轮采集。
        compiled = self._patterns.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                self.logger.warn('urlPattern 不是合法正则，已忽略：%r（%s）' % (pattern, exc))
                compiled = False
            self._patterns[pattern] = compiled
        return compiled or None
```

---

## `hub/registry.py`

```python
"""services.json —— 全服务台唯一的注册表来源。

字段含义（与旧 PowerShell 版完全一致，新增服务只改这个文件）：
  id        唯一标识，命令行 svc 用的也是它
  kind      proc=常驻进程 / tool=一次性脚本（旧版还支持 docker，已按用户要求移出）
  exe/args  可执行文件与参数；args 必须是数组（旧版踩过"单元素数组被退化成字符串"的坑）
  cwd       工作目录，不存在时降级到服务台目录
  marker    命令行特征串，用于无端口进程的探活与停止
  health    port=看端口 / cmdline=看命令行特征 / none=不探活
  port/url  固定端口与网页地址
  urlPattern  动态端口服务：正则从子进程日志里捕获端口号，第 1 个捕获组必须是端口
  autostart false 表示"全部启动"时跳过（桌面 GUI 工具，不该被批量拉起）
  version   版本号（可手写；不写则从 package.json / version.txt 推断，都没有就显示 —）
  updated   更新时间（可手写 YYYY-MM-DD；不写则取工作目录里最新的文件修改时间）
  confirm   有风险的动作（清理、结束 Word 等）写一句提示，网页点“运行”前会先弹确认

本机差异放 services.local.json（不入库，见 .gitignore）：默认按 id 合并覆盖，
写 "remove": [id...] 可隐藏、写 "mode": "replace" 可整份替换——这样换电脑
（笔记本 / 台式机）拉同一份仓库时，不必改动已提交的 services.json。
"""
import json
import os
import threading

from . import meta


def _norm_args(raw):
    """args 只认数组；历史数据里万一存成字符串，按空白切成数组。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [a for a in raw.split() if a]
    if not isinstance(raw, (list, tuple)):  # modified: 写成对象/数字时不再逐键拆成参数
        return []
    return [str(a) for a in raw]


class Registry:
    def __init__(self, path, logger):
        self.path = path
        # services.json -> services.local.json：本机差异层，不提交到仓库
        self.local_path = os.path.splitext(path)[0] + '.local.json'
        self.logger = logger
        self.lock = threading.RLock()
        self._mtime = (0, 0)
        self._cfg = {'version': 1, 'services': []}

    # ---------------------------------------------------------------- 加载
    @staticmethod
    def _read_json(path):
        """读一个 JSON；文件不存在返回 None（与"解析失败"区分开）。"""
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8-sig') as fh:
            return json.load(fh)

    def _apply_local(self, cfg, local):
        """把 services.local.json 叠到主注册表上（本机差异，不入库）。

        默认策略是按 id 合并：同 id 覆盖字段（例如只改 exe 路径），新 id 追加；
        local 里写 "remove": [id...] 隐藏某些条目；写 "mode": "replace" 则
        整份清单改用 local 的（笔记本上只想留自己装得上的工具时用这个）。
        """
        if not isinstance(local, dict):
            return cfg
        # modified: 2.6.3 原来在 load() 的类型校验之前就对数组元素 .get('id')：
        # services.json / local 里混进一个字符串就抛 AttributeError，整轮
        # 采集停帧、/api/status 直接 500。先在这里把两边都过滤干净。
        main = cfg.get('services')
        cfg['services'] = ([s for s in main if isinstance(s, dict)]
                           if isinstance(main, list) else [])
        raw_local = local.get('services')
        local_items = ([x for x in raw_local if isinstance(x, dict)]
                       if isinstance(raw_local, list) else [])
        if local.get('mode') == 'replace':
            cfg['services'] = list(local_items)
            if local.get('groups'):
                cfg['groups'] = local['groups']
            self.logger.ok('已按 services.local.json 全量替换清单')
            return cfg
        drop_raw = local.get('remove')
        drop = {str(x) for x in drop_raw} if isinstance(drop_raw, (list, tuple)) else set()
        if drop:
            before = len(cfg['services'])
            cfg['services'] = [s for s in cfg['services'] if s.get('id') not in drop]
            self.logger.ok('services.local.json 隐藏了 %d 条服务' % (before - len(cfg['services'])))
        by_id = {s.get('id'): s for s in cfg['services']}
        added = 0
        for item in local_items:
            sid = item.get('id')
            if not isinstance(sid, str) or not sid.strip():
                continue
            if sid in by_id:
                by_id[sid].update(item)
            else:
                cfg['services'].append(item)
                by_id[sid] = item
                added += 1
        if added:
            self.logger.ok('services.local.json 补充了 %d 条服务' % added)
        return cfg

    def load(self, force=False):
        # modified: 2.6.3 self.lock 定义了却从没用过：后台采集线程与 HTTP
        # 线程会在 mtime 变化时同时重读、互相覆盖 _cfg。整段串行化。
        with self.lock:
            return self._load(force)

    def _load(self, force=False):
        try:
            stamp = (os.path.getmtime(self.path),
                     os.path.getmtime(self.local_path) if os.path.exists(self.local_path) else 0)
        except Exception:
            self.logger.error('注册表不可读：%s' % self.path)
            return self._cfg
        if not force and stamp == self._mtime and self._cfg.get('services'):
            return self._cfg
        try:
            cfg = self._read_json(self.path)
        except Exception as exc:
            self.logger.error('注册表解析失败（沿用上一份）：%s' % exc)
            return self._cfg
        if not isinstance(cfg, dict):
            self.logger.error('注册表为空或格式不对（沿用上一份）：%s' % self.path)
            return self._cfg
        try:
            local = self._read_json(self.local_path)
        except Exception as exc:
            local = None
            self.logger.warn('services.local.json 解析失败，已忽略：%s' % exc)
        if local:
            cfg = self._apply_local(cfg, local)
        services = []
        raw_services = cfg.get('services', [])
        if not isinstance(raw_services, list):  # modified: 顶层写坏时不让 load() 抛异常
            self.logger.error('services 必须是数组，已按空清单处理：%s' % type(raw_services).__name__)
            raw_services = []
        for idx, item in enumerate(raw_services):
            if not isinstance(item, dict):  # modified: 数组里混进字符串/数字时跳过
                self.logger.warn('第 %d 条服务不是对象，已跳过：%s' % (idx + 1, type(item).__name__))
                continue
            sid = item.get('id')
            # modified: 2.6.3 数字 id 会渲染出卡片，但前端传回来的是字符串，
            # registry.get() 的 == 永远不成立，启停全部回"没有这个服务"。
            if not isinstance(sid, str) or not sid.strip():
                self.logger.warn('第 %d 条服务缺 id 或 id 不是字符串，已跳过' % (idx + 1))
                continue
            svc = dict(item)
            svc['id'] = sid.strip()
            svc['args'] = _norm_args(svc.get('args'))
            svc.setdefault('name', svc['id'])
            svc.setdefault('group', '未分组')
            svc.setdefault('desc', '')
            svc.setdefault('kind', 'proc')
            svc.setdefault('health', 'none')
            svc.setdefault('stopMode', 'pid')
            svc.setdefault('autostart', True)
            if svc.get('port') == '':
                svc['port'] = None
            # 版本号/更新时间：注册表写了就用写的，没写就从磁盘推断（见 hub/meta.py）
            svc['version'] = meta.version_of(svc)
            svc['updated'] = meta.updated_of(svc)
            services.append(svc)
        cfg['services'] = services
        self._cfg = cfg
        self._mtime = stamp
        return self._cfg

    def services(self):
        return self.load().get('services', [])

    def get(self, sid):
        for svc in self.services():
            if svc['id'] == sid:
                return svc
        return None

    def groups(self):
        return list(self.load().get('groups') or [])
```

---

## `hub/server.py`

```python
"""HTTP 层 —— 网页界面和 REST 接口，全部只听 127.0.0.1。

接口清单：
  GET  /                  网页界面
  GET  /api/status        全部服务状态（读快照，毫秒级返回）
  GET  /api/logs?id=      某个服务最近的输出（id=__hub__ 看服务台自己的日志）
  POST /api/action        {id, action: start|stop|run|open, force?}
  POST /api/batch         {action: start-all|stop-all}
  GET  /api/settings      当前设置 + 已装浏览器 + 关于信息
  POST /api/settings      改设置（只改传进来的字段）
  POST /api/browser/test  用指定浏览器打开服务台，验证设置对不对
  POST /api/reload        强制重读 services.json
  POST /api/exit          关掉服务台本身（不影响已启动的服务）

安全：所有 POST 必须带 X-ServiceHub: 1。浏览器跨站发 JSON 会先发预检，
预检请求我们不应答 CORS，加上这个自定义头，别的网站就没法偷偷启停本机服务。
"""
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import autostart  # modified: 开机自启（写 HKCU Run）
from . import browsers

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')

APP_VERSION = '2.6.3'  # modified: 2.6.3 第三轮审计：修 2.6.2 端口归属链路回归 + 纵深防御
APP_BUILD = '2026-11-04'  # modified

# modified: 2.6.3 只认本机的 Host / Origin。原来只校验自定义头
# X-ServiceHub，DNS rebinding 把外部域名重解析到 127.0.0.1 后，
# 页面与接口变同源、自定义头可以随便加，预检也不会发生，防护就被绕过。
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.woff2': 'font/woff2',
}


class HubContext:
    """把各模块串起来的小容器，也顺手记住运行信息给网页显示。"""

    def __init__(self, registry, monitor, actions, logger, appdir, logdir, settings):
        self.registry = registry
        self.monitor = monitor
        self.actions = actions
        self.logger = logger
        self.appdir = appdir
        self.logdir = logdir
        self.settings = settings
        self.port = 0
        self.started_at = time.time()
        self.tray = False
        self.version = APP_VERSION
        self.build = APP_BUILD

    # ---------------------------------------------------------------- 设置
    def load_settings(self):
        data = self.settings.load()
        self.monitor.auto_open = bool(data.get('auto_open', True))
        return data

    def apply_settings(self, patch):
        data = self.settings.update(patch)
        self.monitor.auto_open = bool(data.get('auto_open', True))
        self.logger.act('设置已更新：%s' % ', '.join('%s=%s' % (k, v) for k, v in patch.items()))
        return data

    def hub_url(self):
        return 'http://127.0.0.1:%d/' % self.port

    def open_hub(self, browser=None, browser_path=None):
        """用指定（或当前设置的）浏览器打开服务台自己。返回 (ok, message, info)。"""
        # modified: 2.6.3 /api/browser/test 传 1 / true / {"x":1} 时，
        # resolve_info() 会对非字符串 .strip() 直接抛 AttributeError → 500。
        # 非字符串一律当作"没传"，沿用当前设置。
        browser = browser if isinstance(browser, str) else None
        browser_path = browser_path if isinstance(browser_path, str) else None
        probe = {'browser': browser or self.settings.get('browser'),
                 'browser_path': (browser_path
                                  if browser_path is not None
                                  else self.settings.get('browser_path'))}
        # modified: 2.6.1 一并带回「实际用了哪个浏览器 / 是否退回」，设置页的测试按钮
        # 就能直接显示「已用 ego lite 打开」，而不是含糊的「已打开」。
        return browsers.open_url_ex(self.hub_url(), probe, self.logger)

    def settings_payload(self):
        data = self.settings.as_dict()
        self.monitor.auto_open = bool(data.get('auto_open', True))
        return {
            'ok': True,
            'settings': data,
            'browsers': browsers.discover(),
            # modified: 2.6.1 告诉界面「按现在的设置到底会用哪个 exe」，以及会不会退回
            # 系统默认。设置页据此显示「实际使用：ego lite」或「⚠ 找不到，将用 Edge」，
            # 不必再靠用户点一次按钮去猜。
            'browserResolved': browsers.resolve_info(self.settings),
            'about': self.hub_info(),
            'autostart': autostart.status(self.appdir),  # modified: 开机自启状态
        }

    def hub_info(self):
        view = self.monitor.view()
        rows = view.get('services', [])
        return {
            'version': self.version,
            'build': self.build,
            'python': '%d.%d.%d' % sys.version_info[:3],
            'port': self.port,
            'url': self.hub_url() if self.port else '',
            'uptime': int(time.time() - self.started_at),
            'started_at': datetime.fromtimestamp(self.started_at).strftime('%Y-%m-%d %H:%M:%S'),
            'tray': bool(self.tray),
            'psutil': self.monitor.psutil_error is None,
            'cost_ms': round(float(view.get('cost_ms', 0) or 0), 1),
            'generated': view.get('generated', 0),
            'total': len(rows),
            'running': sum(1 for row in rows if row.get('running')),
            'appdir': self.appdir,
            'logdir': self.logdir,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = 'ServiceHub/' + APP_VERSION
    ctx = None            # HubContext
    logger = None

    # ---------------------------------------------------------------- 底层
    def log_message(self, fmt, *args):
        """默认会打到 stderr，pythonw 下看不到也没意义，统一走自己的日志。"""
        try:
            self.logger.debug('%s %s' % (self.client_address[0], fmt % args))
        except Exception:
            pass

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        self._responded = True   # modified: 2.6.3 防止异常兜底在同一连接写第二个状态行
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if extra:
            for key, val in extra.items():
                self.send_header(key, val)
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def _ok(self, payload):
        self._json(200, payload)

    def _fail(self, message, code=400):
        # modified: 2.6.3 响应头已发出（例如 wfile.write 失败）时，do_GET/do_POST
        # 的兜底再写一次 500 会在同一连接里塞进第二个状态行。这里直接记日志。
        if getattr(self, '_responded', False):
            try:
                self.logger.warn('响应已发出，忽略重复错误响应：%s' % message)
            except Exception:
                pass
            return
        self._json(code, {'ok': False, 'message': message})

    MAX_BODY = 1024 * 1024   # modified: 本机接口不需要大 body，超过 1MB 直接拒绝

    def setup(self):
        # modified: 2.6.3 声明了合法 Content-Length 却一直不发完的连接，
        # 会把 ThreadingHTTPServer 的一个工作线程永久挂住。给读加个超时。
        super().setup()
        try:
            self.connection.settimeout(10)
        except Exception:
            pass

    def _read_body(self):
        # modified: Content-Length 可能被伪造/写错（非数字），以前 int() 会抛 500
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            self.close_connection = True  # modified: body 没读，直接断开，避免残留字节污染下一个请求
            self._fail('Content-Length 不合法', 400)
            return None
        if length < 0:
            self.close_connection = True  # modified
            self._fail('Content-Length 不合法', 400)
            return None
        if length > self.MAX_BODY:
            self.close_connection = True  # modified: 不读 body，直接断开
            self._fail('请求体过大（上限 1MB）', 413)
            return None
        if not length:
            return {}
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, OSError):
            self.close_connection = True
            self._fail('读取请求体超时或连接中断', 408)
            return None
        raw = raw.decode('utf-8', errors='replace')
        try:
            if raw.strip().startswith('{'):
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            return {k: v[0] if isinstance(v, list) and v else v
                    for k, v in parse_qs(raw).items()}
        except Exception:
            return {}

    def _local_request(self):
        """modified: 2.6.3 Host / Origin 必须是本机，挡住 DNS rebinding。"""
        host = (self.headers.get('Host') or '').strip().lower()
        if host:
            name = host.split(']', 1)[0] + ']' if host.startswith('[') else host.split(':', 1)[0]
            if name not in LOCAL_HOSTS:
                return False
        origin = (self.headers.get('Origin') or '').strip()
        if origin:
            try:
                name = (urlparse(origin).hostname or '').lower()
            except Exception:
                return False
            if name not in LOCAL_HOSTS:
                return False
        return True

    def _guard(self):
        """POST 必须带自定义头；顺带挡掉跨站表单（它没法设置自定义头）。"""
        # modified: 2.6.3 自定义头在同源化（DNS rebinding）后不再是一道墙，
        # 先校验证求来源确实指向本机地址。
        if not self._local_request():
            self._fail('请求来源不是本机地址，已拒绝', 403)
            return False
        if self.command == 'OPTIONS':
            # 不返回任何 CORS 头，让跨站预检直接失败
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        if self.command == 'POST' and self.headers.get('X-ServiceHub') != '1':
            self._fail('缺少 X-ServiceHub 请求头，跨站请求已拒绝', 403)
            return False
        return True

    # ---------------------------------------------------------------- 路由
    def do_GET(self):
        if not self._guard():
            return
        try:
            self._route_get()
        except Exception as exc:
            self.logger.error('GET %s 异常：%r' % (self.path, exc))
            try:
                self._fail('服务器内部错误：%s' % exc, 500)
            except Exception:
                pass

    def do_POST(self):
        if not self._guard():
            return
        try:
            self._route_post()
        except Exception as exc:
            self.logger.error('POST %s 异常：%r' % (self.path, exc))
            try:
                self._fail('服务器内部错误：%s' % exc, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self._guard()

    def _route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        query = parse_qs(parsed.query)

        if path in ('/', '/index.html', '/ui'):
            return self._static('index.html')
        if path == '/static' or path.startswith('/ui/') or path.startswith('/static/'):
            return self._static(os.path.basename(path))
        if path == '/api/ping':
            return self._ok({'ok': True, 'pong': True, 'port': self.ctx.port,
                             'version': self.ctx.version})
        if path == '/api/status':
            return self._ok({'ok': True, 'hub': self.ctx.hub_info(),
                             'services': self.ctx.monitor.view()['services']})
        if path == '/api/settings':
            return self._ok(self.ctx.settings_payload())
        if path == '/api/logs':
            sid = (query.get('id') or [''])[0]
            try:
                lines = int((query.get('lines') or ['200'])[0])
            except Exception:
                lines = 200
            lines = max(1, min(2000, lines))
            if not sid:
                return self._fail('缺少 id')
            if sid == '__hub__':
                return self._ok({'ok': True, 'logfile': 'hub.log', 'lines': self.logger.tail(lines)})
            return self._ok(dict(self.ctx.actions.read_log(sid, lines), id=sid))
        if path == '/favicon.ico':
            return self._send(204, b'')
        self._fail('没有这个地址：%s' % path, 404)

    def _route_post(self):
        path = (urlparse(self.path).path or '').rstrip('/')
        body = self._read_body()
        if body is None:  # modified: 解析失败/超限时已经回过 4xx，不再往下走
            return

        def item(key, default=None):
            val = body.get(key, default)
            if isinstance(val, list):
                return val[0] if val else default
            return val

        if path == '/api/action':
            sid = item('id')
            action = item('action')
            force = str(item('force', '')).lower() in ('1', 'true', 'yes')
            if not sid or not action:
                return self._fail('缺少 id 或 action')
            if action == 'start':
                result = self.ctx.actions.start(sid)
            elif action == 'stop':
                result = self.ctx.actions.stop(sid, force=force)
            elif action == 'restart':  # modified
                result = self.ctx.actions.restart(sid, force=force)  # modified: 外部实例先确认
            elif action == 'run':
                result = self.ctx.actions.run_tool(sid)
            elif action == 'open':
                result = self.ctx.actions.open_url(sid)
            else:
                return self._fail('不认识的动作：%s' % action)
            return self._ok(dict(result, id=sid))

        if path == '/api/batch':
            action = item('action')
            if action == 'start-all':
                return self._ok(self.ctx.actions.start_all())
            if action == 'stop-all':
                return self._ok(self.ctx.actions.stop_all())
            return self._fail('不认识的批量动作：%s' % action)

        if path == '/api/settings':
            patch = {}
            # modified: bg_motion / hidden_ids / group_order / density 加入可写字段。
            # 后三项是界面布局偏好（隐藏哪些卡片、分组怎么排、密度），落在 settings.json，
            # 不碰 services.json——注册表描述「服务是什么」，这里描述「我怎么看它」。
            for key in ('auto_open', 'browser', 'browser_path', 'theme', 'view',
                        'refresh_ms', 'bg_motion', 'hidden_ids', 'group_order', 'density',
                        'service_group', 'service_order'):
                if key in body:
                    # 注意不能用 item()：那个帮助函数会把数组/对象压成第一个元素（它是给
                    # /api/action 这类单值参数准备的），hidden_ids / group_order 是数组、
                    # service_group / service_order 是对象，走 item() 会被压成字符串
                    # 再被设置层判为非法而丢弃。
                    patch[key] = body.get(key)
            note = ''
            # modified: 开机自启落在注册表里，不进 settings.json，所以单独处理。
            # 注册表才是唯一真相——用户也可能从「任务管理器 → 启动」里自己关掉。
            if 'autostart' in body:
                # modified: 2.6.0 原来直接 bool(body.get('autostart'))——
                # "false"/"0"/"no" 这些字符串在 Python 里都是真值，会被误判成"开启"。
                # 网页传的是布尔不会踩到，但命令行/第三方调用传字符串就会误开自启。
                raw_flag = body.get('autostart')
                # modified: 2.6.3 2.6.0 只补了字符串分支；["false"] / {"x":1}
                # 这类容器仍是 truthy，会把"关自启"误判成写注册表开机自启。
                if isinstance(raw_flag, str):
                    want = raw_flag.strip().lower() in ('1', 'true', 'yes', 'on')
                elif isinstance(raw_flag, (bool, int, float)):
                    want = bool(raw_flag)
                else:
                    return self._fail('autostart 类型不合法（应为布尔或字符串）')
                ok, note = (autostart.enable(self.ctx.appdir) if want
                            else autostart.disable())
                if not ok:
                    return self._fail(note)
            if patch:
                self.ctx.apply_settings(patch)
                if self.ctx.settings.last_error:  # modified: 2.6.0 落盘失败不许报成功
                    return self._fail('设置保存失败：%s' % self.ctx.settings.last_error, 500)
            payload = self.ctx.settings_payload()
            if note:
                payload['message'] = note
            return self._ok(payload)

        if path == '/api/browser/test':
            browser = item('browser')
            browser_path = item('browser_path')
            ok, message, info = self.ctx.open_hub(browser, browser_path)
            return self._ok({'ok': ok, 'message': message,
                             'browser': info.get('name') or '',
                             'fallback': bool(info.get('fallback'))})

        if path == '/api/reload':
            cfg = self.ctx.registry.load(force=True)
            count = len(cfg.get('services', []))
            self.ctx.logger.act('已重读 services.json，共 %d 条' % count)
            return self._ok({'ok': True, 'message': '已重读注册表，共 %d 条服务' % count,
                             'count': count})

        if path == '/api/exit':
            self.logger.act('收到退出指令，正在关掉服务台网页服务')
            threading.Timer(0.4, self.server.shutdown).start()
            return self._ok({'ok': True, 'message': '服务台已退出'})

        self._fail('没有这个地址：%s' % path, 404)

    # ---------------------------------------------------------------- 静态
    def _static(self, name):
        # modified: 2.6.3 解码 %xx 后再取 basename，含空格/中文名的资源也能命中；
        # 用 commonpath 明确"必须落在 UI_DIR 内"替代恒假的 '..' in safe 检查。
        from urllib.parse import unquote
        safe = os.path.basename(unquote(name))
        full = os.path.abspath(os.path.join(UI_DIR, safe))
        if (not safe or os.path.commonpath([full, UI_DIR]) != UI_DIR
                or not os.path.isfile(full)):
            return self._send(404, b'not found', 'text/plain; charset=utf-8')
        ext = os.path.splitext(safe)[1].lower()
        try:
            with open(full, 'rb') as fh:
                body = fh.read()
        except Exception as exc:
            return self._send(500, ('读取失败：%s' % exc).encode('utf-8'), 'text/plain; charset=utf-8')
        self._send(200, body, MIME.get(ext, 'application/octet-stream'))


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    # modified: Windows 的 SO_REUSEADDR 允许绑定已被监听的端口（与 Linux 相反），
    # 会让端口探测误判"可用"，两个进程抢同一端口。Windows 上关掉。
    allow_reuse_address = (os.name != 'nt')

    def __init__(self, ctx, port_range):
        self.ctx = ctx
        self.start_port = port_range[0]
        self.end_port = port_range[1]
        handler = self._handler_class()
        # modified: 2.6.0 "探测端口空闲"和"真正 bind"之间有窗口期：两个实例同时启动时
        # 可能都探到同一端口可用，后 bind 的那个直接 OSError 退出，用户只看到启动失败。
        # 现在 bind 失败就顺延到下一个端口；socketserver 在 bind 失败时已自己 server_close，
        # 重新 __init__ 会新建 socket，不会泄漏句柄。
        port = self._bind()
        while True:
            try:
                super().__init__(('127.0.0.1', port), handler)
                break
            except (OSError, OverflowError) as exc:   # modified: 2.6.3
                if port >= min(self.end_port, 65535):
                    raise RuntimeError('端口 %d-%d 都不可用：%s'
                                       % (self.start_port, self.end_port, exc))
                port += 1
        ctx.port = port

    def _handler_class(self):
        ctx = self.ctx

        class BoundHandler(Handler):
            pass

        BoundHandler.ctx = ctx
        BoundHandler.logger = ctx.logger
        return BoundHandler

    def _bind(self):
        """从起始端口往上找第一个能绑的；端口最终写进 runtime\\port.txt 供批处理调用者读取。"""
        last_error = None
        # modified: 2.6.3 端口上界原来只校验 start，start+tries 越过 65535 时
        # bind() 抛的是 OverflowError 而非 OSError，会一路冒泡让服务台启动崩溃。
        for port in range(self.start_port, min(self.end_port, 65535) + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # modified: Windows 用 SO_EXCLUSIVEADDRUSE 才能真正探出"已被占用"；
            # 设 SO_REUSEADDR 反而会绑定成功，导致端口冲突被掩盖。
            if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except (OSError, OverflowError) as exc:   # modified: 2.6.3
                last_error = exc
                continue
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        raise RuntimeError('端口 %d-%d 都不可用：%s' % (self.start_port, self.end_port, last_error))

    def serve_forever(self, *args, **kwargs):
        self.ctx.logger.ok('服务台网页已就绪：http://127.0.0.1:%d/' % self.ctx.port)
        super().serve_forever(*args, **kwargs)
```

---

## `hub/settings.py`

```python
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
```

---

## `hub/tray.py`

```python
"""系统托盘（可选）。

托盘不是必需品：网页 + 桌面快捷方式已经够用。这里做成"能用就更好，用不了不影响"，
pystray 缺失或初始化失败时直接降级为无托盘模式，只在日志里留一句。
"""
import os
import threading


def _make_icon_image():
    """用 Pillow 现画一个图标：蓝色圆角底 + 白色电源符号，不依赖外部 ico 文件。"""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=(22, 119, 255, 255))
    # 电源符号：一段开口圆弧 + 一条竖线
    draw.arc([16, 13, 48, 45], start=300, end=240, fill=(255, 255, 255, 255), width=5)
    draw.line([32, 28, 32, 48], fill=(255, 255, 255, 255), width=5)
    return img


def start_tray(ctx, hub_url, on_open, on_start_all, on_stop_all, on_exit):
    """返回托盘对象；失败返回 None，调用方继续无托盘运行。

    为什么要返回对象：退出时要显式 stop()，否则托盘的消息循环线程可能挂着不让进程结束，
    留下一具"端口已关、进程还在"的僵尸实例（实测踩过）。
    """
    try:
        import pystray
    except Exception as exc:
        ctx.logger.warn('托盘不可用（缺少 pystray：%s），改用网页入口' % exc)
        return None

    def open_panel(icon=None, item=None):
        try:
            on_open()
        except Exception as exc:
            ctx.logger.error('打开面板失败：%r' % exc)

    def start_all(icon=None, item=None):
        try:
            result = on_start_all()
            ctx.logger.ok('托盘触发全部启动：%s' % result.get('message'))
        except Exception as exc:
            ctx.logger.error('全部启动异常：%r' % exc)

    def stop_all(icon=None, item=None):
        try:
            result = on_stop_all()
            ctx.logger.ok('托盘触发全部停止：%s' % result.get('message'))
        except Exception as exc:
            ctx.logger.error('全部停止异常：%r' % exc)

    def quit_hub(icon=None, item=None):
        try:
            icon.stop()
        except Exception:
            pass
        on_exit()

    menu = pystray.Menu(
        pystray.MenuItem('打开服务台面板', open_panel, default=True),
        pystray.MenuItem('全部启动', start_all),
        pystray.MenuItem('全部停止', stop_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出服务台', quit_hub),
    )
    try:
        icon = pystray.Icon('ServiceHub', _make_icon_image(), '服务台 · %s' % hub_url, menu)
    except Exception as exc:
        ctx.logger.warn('托盘图标创建失败：%r' % exc)
        return None

    try:
        icon.run_detached()
        ctx.logger.ok('托盘图标已就绪')
        return icon
    except Exception as exc:
        ctx.logger.warn('托盘启动失败：%r' % exc)
        try:
            threading.Thread(target=icon.run, name='tray', daemon=True).start()
            ctx.logger.ok('托盘图标已就绪（线程模式）')
            return icon
        except Exception as exc2:
            ctx.logger.warn('托盘线程也起不来：%r' % exc2)
            return None
```

---

## `hub/ui/index.html`

```html
<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>服务台 · 本地服务一键启停</title>
<script>
/* 首屏防闪：先把上次用的主题套上，等接口回来再以服务器设置为准 */
(function () {
  try {
    var forced = (location.search.match(/[?&]theme=(light|dark)/) || [])[1];
    var t = forced || localStorage.getItem('hub.theme') || 'system';
    if (t === 'system') {
      t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
</script>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="backdrop" aria-hidden="true">
  <span class="blob b1"></span>
  <span class="blob b2"></span>
  <span class="blob b3"></span>
  <span class="grain"></span>
</div>

<header class="topbar glass">
  <div class="brand">
    <span class="logo" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
           stroke-width="2.1" stroke-linecap="round">
        <path d="M12 3v8"></path>
        <path d="M6.6 6.6a8 8 0 1 0 10.8 0"></path>
      </svg>
    </span>
    <div class="brandText">
      <h1>服务台</h1>
      <p class="sub" id="hubMeta">正在连接…</p>
    </div>
  </div>

  <div class="topActions">
    <div class="seg" id="viewSeg" role="group" aria-label="视图切换">
      <button type="button" class="segBtn" data-view="card" title="卡片视图">卡片</button>
      <button type="button" class="segBtn" data-view="list" title="列表视图">列表</button>
    </div>
    <button type="button" class="iconBtn" id="btnTheme" title="切换明亮 / 暗色">
      <svg class="icoSun" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"></circle>
        <path d="M12 2.5v2M12 19.5v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2.5 12h2M19.5 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"></path>
      </svg>
      <svg class="icoMoon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round"><path d="M20 14.5A8.2 8.2 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"></path>
      </svg>
    </button>
    <button type="button" class="btn ghost btnLayout" id="btnLayout"
            title="进入布局编辑：可拖动分组、隐藏卡片">布局</button>
    <button type="button" class="iconBtn" id="btnSettings" title="设置">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"></circle>
        <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.3-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4.1a2 2 0 1 1 4 0A1.7 1.7 0 0 0 16.9 5l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 1 1 0 4z"></path>
      </svg>
    </button>
    <span class="divider" aria-hidden="true"></span>
    <button type="button" class="btn" id="btnRefresh" title="立刻刷新状态">刷新</button>
    <button type="button" class="btn primary" id="btnStartAll">全部启动</button>
    <button type="button" class="btn danger" id="btnStopAll">全部停止</button>
    <button type="button" class="btn ghost" id="btnExit" title="关掉服务台网页，已启动的服务不受影响">退出</button>
  </div>
</header>

<section class="toolbar">
  <div class="searchWrap">
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle>
      <path d="M16.5 16.5L21 21"></path></svg>
    <input type="search" id="searchBox" placeholder="搜索名称 / 说明 / 端口 / 版本…" autocomplete="off">
  </div>
  <div class="chips" id="groupChips"></div>
  <div class="stats" id="stats"></div>
</section>

<main id="content" class="content"></main>

<div id="toasts" class="toasts"></div>

<div class="modalMask" id="modalMask" hidden>
  <div class="modal glass" id="modal" role="dialog" aria-modal="true">
    <div class="modalHead">
      <h2 id="modalTitle">标题</h2>
      <button type="button" class="iconBtn" id="modalClose" aria-label="关闭">×</button>
    </div>
    <div class="modalBody" id="modalBody"></div>
    <div class="modalFoot" id="modalFoot" hidden></div>
  </div>
</div>

<script src="/static/app.js"></script>
</body>
</html>
```

---

## `hub/ui/style.css`

```css
/* 服务台网页界面 —— 护眼玻璃拟态（Glassmorphism）
   设计要点：
     · 背景是低饱和的柔和光斑 + 细噪点，玻璃面板才有东西可"透"，不是一层灰底糊白块
     · 明暗两套变量都放在 :root / html[data-theme=dark]，改主题只动这里
     · 正文用暖中性色、绿松石色作强调，长时间看不刺眼（护眼优先于高对比炫技）
   兼容：backdrop-filter 不可用时自动退回不透明面板（见文件末尾 @supports）
*/

:root {
  --bg: #e9efec;
  --bg-2: #dfe9e4;
  --text: #17211f;
  --muted: #5c6c69;
  --line: rgba(23, 33, 31, .10);

  --glass: rgba(255, 255, 255, .58);
  --glass-soft: rgba(255, 255, 255, .38);
  --glass-strong: rgba(255, 255, 255, .78);
  /* modified: 卡片专用底色，比 --glass 更不透明。
     卡片下垫的是已经 blur(90px) 的光斑，再做背板模糊观感增益几乎为零，
     却要让 17 张卡片每帧重算一次大区域模糊 → 老内核/集显上表现为持续闪烁。
     去掉模糊后靠这点不透明度补回层次，视觉差别很小（详见 .card 注释）。 */
  --card-bg: rgba(255, 255, 255, .74);
  --glass-brd: rgba(255, 255, 255, .72);
  --glass-brd-2: rgba(23, 33, 31, .06);
  /* 弹窗、提示条这类"压在内容上面"的层，必须接近不透明：
     backdrop-filter 在无 GPU / 老内核下不生效，靠模糊遮不住底下的字 */
  --float-bg: rgba(252, 253, 252, .97);

  --accent: #2b8a77;
  --accent-2: #3d9db4;
  --accent-ink: #ffffff;
  --accent-soft: rgba(43, 138, 119, .12);
  --ring: rgba(43, 138, 119, .26);

  --ok: #1d8a62;
  --ok-bg: rgba(45, 170, 125, .14);
  --ext: #2c68c4;
  --ext-bg: rgba(63, 133, 214, .14);
  --warn: #a9701a;
  --warn-bg: rgba(214, 156, 42, .16);
  --idle: #687875;
  --idle-bg: rgba(110, 130, 127, .14);
  --tool: #6a4fca;
  --tool-bg: rgba(124, 90, 214, .14);
  --danger: #c1454f;
  --danger-bg: rgba(193, 69, 79, .12);

  --shadow-1: 0 1px 2px rgba(23, 45, 40, .05), 0 10px 28px rgba(23, 45, 40, .08);
  --shadow-2: 0 2px 4px rgba(23, 45, 40, .06), 0 18px 46px rgba(23, 45, 40, .14);
  --radius: 18px;
  --radius-sm: 12px;
  --blob-1: #9fe3c9;
  --blob-2: #b6d8f7;
  --blob-3: #ffd9bd;
  --noise: .035;
}

html[data-theme="dark"] {
  --bg: #0c1214;
  --bg-2: #0f181b;
  --text: #e5efec;
  --muted: #93a5a2;
  --line: rgba(229, 239, 236, .10);

  --glass: rgba(26, 35, 37, .55);
  --glass-soft: rgba(26, 35, 37, .34);
  --glass-strong: rgba(30, 41, 43, .82);
  --card-bg: rgba(26, 35, 37, .70);  /* modified: 卡片专用底色，见上方 :root 注释 */
  --glass-brd: rgba(255, 255, 255, .09);
  --glass-brd-2: rgba(255, 255, 255, .05);
  --float-bg: rgba(23, 31, 33, .97);

  --accent: #5ecbaf;
  --accent-2: #6fc4dd;
  --accent-ink: #06231d;
  --accent-soft: rgba(94, 203, 175, .14);
  --ring: rgba(94, 203, 175, .28);

  --ok: #5fd3a6;
  --ok-bg: rgba(60, 200, 150, .14);
  --ext: #82b4ff;
  --ext-bg: rgba(90, 150, 240, .16);
  --warn: #f0c069;
  --warn-bg: rgba(224, 172, 60, .16);
  --idle: #9aa9a6;
  --idle-bg: rgba(150, 170, 166, .12);
  --tool: #bda9ff;
  --tool-bg: rgba(150, 120, 240, .18);
  --danger: #ff8f96;
  --danger-bg: rgba(255, 120, 130, .14);

  --shadow-1: 0 1px 2px rgba(0, 0, 0, .3), 0 12px 30px rgba(0, 0, 0, .34);
  --shadow-2: 0 2px 6px rgba(0, 0, 0, .34), 0 22px 54px rgba(0, 0, 0, .48);
  --blob-1: #10564c;
  --blob-2: #123a53;
  --blob-3: #35254f;
  --noise: .05;
}

* { box-sizing: border-box; }

html, body { min-height: 100%; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.62 "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}

/* ------------------------------------------------------------------ 背景 */
.backdrop {
  position: fixed;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  z-index: 0;
  background: linear-gradient(160deg, var(--bg) 0%, var(--bg-2) 100%);
}
/* modified: 这里刻意【不再用】filter: blur()。
   原来每个光斑是 `filter: blur(90px)` + 无限 transform 动画。带 filter 的元素在部分
   内核（实测 Edge，同为 Chromium 的 ego 却不复现）不会缓存模糊结果，动画期间会逐帧
   重新栅格化这三大块，表现为整屏持续闪烁。改成径向渐变后，柔边是"画"出来的，不必再
   算滤镜，动画只是图层位移，任何内核都是零成本合成，观感几乎一致。 */
.blob {
  position: absolute;
  display: block;
  border-radius: 50%;
  opacity: .55;
  will-change: transform;
  background: radial-gradient(closest-side,
              var(--blob-c) 0%,
              var(--blob-c) 34%,
              transparent 100%);
}
.b1 { width: 46vw; height: 46vw; left: -10vw; top: -12vw; --blob-c: var(--blob-1); }
.b2 { width: 40vw; height: 40vw; right: -8vw; top: 6vh; --blob-c: var(--blob-2); }
.b3 { width: 38vw; height: 38vw; left: 26vw; bottom: -16vw; --blob-c: var(--blob-3); }

/* modified: 光斑漂移默认【不播放】。
   默认静止 = 首屏不可能闪，也不会出现"设置还没加载完就先动起来"的跳变。
   想恢复"呼吸感"：设置面板 → 背景动效 → 开（会给 <html> 加 .bg-motion）。
   实测 Edge + Intel 集显 + 高 DPI 下，这三个大图层一旦持续运动，整屏会闪。 */
html.bg-motion .b1 { animation: drift1 26s ease-in-out infinite alternate; }
html.bg-motion .b2 { animation: drift2 32s ease-in-out infinite alternate; }
html.bg-motion .b3 { animation: drift3 38s ease-in-out infinite alternate; }
.grain {
  position: absolute; inset: 0;
  opacity: var(--noise);
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2'/></filter><rect width='140' height='140' filter='url(%23n)' opacity='.5'/></svg>");
}
@keyframes drift1 { to { transform: translate(6vw, 5vh) scale(1.08); } }
@keyframes drift2 { to { transform: translate(-5vw, 7vh) scale(1.12); } }
@keyframes drift3 { to { transform: translate(4vw, -6vh) scale(1.06); } }

/* ------------------------------------------------------------------ 玻璃基类 */
.glass {
  background: var(--glass);
  border: 1px solid var(--glass-brd);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  backdrop-filter: blur(22px) saturate(160%);
  box-shadow: var(--shadow-1);
}

/* ------------------------------------------------------------------ 顶栏 */
.topbar {
  position: sticky;
  top: 12px;
  z-index: 30;
  margin: 12px 18px 0;
  padding: 12px 18px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.logo {
  width: 40px; height: 40px; flex: none;
  display: grid; place-items: center;
  border-radius: 13px;
  color: #fff;
  background: linear-gradient(145deg, var(--accent), var(--accent-2));
  box-shadow: 0 6px 16px rgba(43, 138, 119, .32), inset 0 1px 0 rgba(255, 255, 255, .35);
}
html[data-theme="dark"] .logo { color: #06231d; }
.brandText { min-width: 0; }
.topbar h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: .06em;
  line-height: 1.25;
}
.sub {
  margin: 0;
  color: var(--muted);
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.topActions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.divider { width: 1px; height: 22px; background: var(--line); margin: 0 2px; }

/* ------------------------------------------------------------------ 控件 */
.btn {
  appearance: none;
  border: 1px solid var(--glass-brd);
  background: var(--glass-soft);
  color: var(--text);
  padding: 7px 14px;
  border-radius: 11px;
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  white-space: nowrap;
  transition: transform .14s, background .18s, border-color .18s, color .18s, box-shadow .18s;
}
.btn:hover { background: var(--glass-strong); border-color: var(--glass-brd); transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
/* modified: 分组标题栏里的小号按钮，不抢视觉重量 */
.btn.tiny { padding: 3px 10px; font-size: 11.5px; border-radius: 8px; }
.btn.tiny:disabled { opacity: .4; cursor: default; transform: none; }
/* modified: 顶栏「布局」按钮的激活态 */
.btnLayout.active {
  background: var(--accent-soft);
  border-color: var(--ring);
  color: var(--accent);
}
.btn.primary {
  background: linear-gradient(145deg, var(--accent), var(--accent-2));
  border-color: transparent;
  color: var(--accent-ink);
  box-shadow: 0 6px 16px rgba(43, 138, 119, .26);
}
.btn.primary:hover { filter: brightness(1.05); }
.btn.danger { color: var(--danger); }
.btn.danger:hover { background: var(--danger-bg); }
.btn.ghost { background: transparent; }
.btn:disabled { opacity: .42; cursor: not-allowed; transform: none; }

.iconBtn {
  appearance: none;
  width: 36px; height: 36px;
  display: grid; place-items: center;
  border-radius: 11px;
  border: 1px solid var(--glass-brd);
  background: var(--glass-soft);
  color: var(--text);
  cursor: pointer;
  transition: background .18s, transform .14s;
}
.iconBtn:hover { background: var(--glass-strong); transform: translateY(-1px); }
.iconBtn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
.icoMoon { display: none; }
html[data-theme="dark"] .icoSun { display: none; }
html[data-theme="dark"] .icoMoon { display: block; }

.seg {
  display: inline-flex;
  padding: 3px;
  gap: 2px;
  border-radius: 12px;
  background: var(--glass-soft);
  border: 1px solid var(--glass-brd);
}
.segBtn {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-family: inherit;
  font-size: 12.5px;
  padding: 5px 12px;
  border-radius: 9px;
  cursor: pointer;
  transition: background .18s, color .18s;
}
.segBtn.active {
  background: var(--glass-strong);
  color: var(--accent);
  font-weight: 600;
  box-shadow: 0 2px 8px rgba(23, 45, 40, .10);
}

/* ------------------------------------------------------------------ 工具栏 */
.toolbar {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 16px 24px 6px;
}
.searchWrap {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1 1 260px;
  max-width: 380px;
  padding: 0 12px;
  height: 38px;
  border-radius: 12px;
  background: var(--glass);
  border: 1px solid var(--glass-brd);
  -webkit-backdrop-filter: blur(16px);
  backdrop-filter: blur(16px);
  color: var(--muted);
  transition: box-shadow .18s, border-color .18s;
}
.searchWrap:focus-within { border-color: var(--accent); box-shadow: 0 0 0 4px var(--ring); }
#searchBox {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  outline: none;
  height: 100%;
}
#searchBox::placeholder { color: var(--muted); opacity: .8; }

.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip {
  appearance: none;
  padding: 5px 13px;
  border-radius: 999px;
  border: 1px solid var(--glass-brd);
  background: var(--glass-soft);
  color: var(--muted);
  font-family: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: .18s;
}
.chip:hover { color: var(--text); background: var(--glass-strong); }
.chip.active {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
.stats {
  margin-left: auto;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.stats b { color: var(--text); font-weight: 600; }

/* ------------------------------------------------------------------ 内容区 */
.content { position: relative; z-index: 1; padding: 8px 24px 64px; }
.group { margin-top: 20px; }
/* modified: 2.4.0 分组标题栏——原来的纯文字标题换成可点（折叠）、可拖（换序）的整条栏，
   并带上「N 项 · N 运行」和组内批量按钮。 */
.groupHead {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0 0 12px;
  padding: 4px 2px 6px;
  border-bottom: 1px solid var(--line);
  font-size: 12.5px;
  letter-spacing: .04em;
}
.groupName {
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  /* modified: 2.4.2 组名左侧强调色条——多组扫视时定位更快 */
  position: relative;
  padding-left: 10px;
}
.groupName::before {
  content: "";
  position: absolute;
  left: 0; top: 2px; bottom: 2px;
  width: 3px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--accent), var(--accent-2));
}
.groupCount {
  font-weight: 400;
  letter-spacing: 0;
  padding: 1px 9px;
  border-radius: 999px;
  background: var(--glass-soft);
  border: 1px solid var(--glass-brd);
  color: var(--muted);
  font-size: 11.5px;
  white-space: nowrap;
}
.groupCount b { color: var(--ok); font-weight: 600; }
.groupActs { display: flex; gap: 6px; margin-left: auto; }
.groupToggle {
  appearance: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  flex-shrink: 0;
  transition: transform .18s ease, background .15s, color .15s;
}
.groupToggle:hover { background: var(--glass-strong); color: var(--text); }
.group.collapsed .groupToggle { transform: rotate(-90deg); }
.group.collapsed .groupHead { margin-bottom: 0; border-bottom-color: transparent; }
.dragGrip {
  display: none;
  align-items: center;
  padding: 0 1px;
  color: var(--muted);
  opacity: .5;
}
html.edit-mode .dragGrip { display: inline-flex; }
html.edit-mode .groupHead { cursor: grab; }
html.edit-mode .groupHead:hover { background: var(--glass-soft); border-radius: 8px; }
html.edit-mode .group.dragging { opacity: .5; }
html.edit-mode .group.dragging .groupHead { cursor: grabbing; }

/* modified: 2.4.1 卡片拖拽。只在编辑模式下可拖，平时 cursor 保持默认，
   否则鼠标划过每张卡都是抓手，看着像随时能拖坏。
   落点提示用盒阴影画竖线，不插占位元素——占位会挤动 grid，光标底下的卡会跳走。 */
html.edit-mode .cardSortable { cursor: grab; }
html.edit-mode .cardSortable:active { cursor: grabbing; }
.card.cardDragging { opacity: .45; cursor: grabbing; }
/* modified: 2.4.2 修复——drop-before/drop-after 原来定义在这里（.card:hover 之前），
   特异性相同被后者的 box-shadow 顶掉，拖动落点竖线提示从来就看不见。
   两条目移到 .card:hover 之后并显式写 :hover 形态（见下方 modified 块）。 */
.group.drop-end .grid {
  outline: 2px dashed var(--accent);
  outline-offset: 4px;
  border-radius: 10px;
}
.group.drop-end.collapsed { outline: 2px dashed var(--accent); outline-offset: -2px; border-radius: 10px; }
html.edit-mode .cardSortable button { cursor: pointer; }   /* 卡内按钮不跟着变抓手 */

.grid {
  display: grid;
  gap: 12px;
  /* modified: 固定 4 列（原来是 auto-fill + minmax，1920 宽会自己涨到 6 列，
     与「一行 4 张」的要求不符）。窄屏按断点退 3 / 2 / 1 列，见文件末尾响应式段。 */
  grid-template-columns: repeat(4, minmax(0, 1fr));
  align-items: stretch;   /* modified: 原 start 是为 Bento 大小卡留层次；等宽后改成拉伸，同行卡片底边对齐 */
}

/* modified: 已停用 Bento 主卡（所有卡片等宽，一行 4 张）。
   规则保留在这里，将来要恢复只需在 renderContent 里给某张卡传 hero=true。 */
.card.is-hero { grid-column: span 2; }
.card.is-hero .cardName { font-size: 17px; }
.card.is-hero .cardDesc { -webkit-line-clamp: 3; }
@media (max-width: 700px) {
  .card.is-hero { grid-column: span 1; }
}

/* modified: 一次性脚本压成单行扁条——它们没有端口/运行时长，占一整张竖卡太浪费。
   等宽四列下扁条也只占 1 列（不再跨 2 列），否则一行排不满、右边留空。 */
.card.is-tool {
  grid-column: span 1;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: center;
  gap: 7px;
  padding: 10px 12px;
}
.card.is-tool .cardHead { flex: 1 1 auto; min-width: 0; align-items: center; gap: 7px; }
.card.is-tool .cardName {
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card.is-tool .pill { display: none; }   /* modified: 一次性脚本恒为「工具」，扁条里留这枚胶囊只是白占 50px 名称宽度 */
.card.is-tool .cardId { display: none; }
.card.is-tool .cardDesc { display: none; }
.card.is-tool .cardVersion { display: none; }   /* modified: 2.5.1 扁条只留「运行」，版本号不进操作行 */
.card.is-tool .cardMeta { display: none; }   /* modified: 只有「一次性脚本」一个信息，pill「工具」已表达 */
.card.is-tool .cardActions { margin-left: auto; margin-top: 0; flex: 0 0 auto; gap: 4px; flex-wrap: nowrap; }
.card.is-tool .cardActions .btn { padding: 4px 9px; font-size: 11.5px; }
.card.is-tool .cardActions .linkBtn { font-size: 11.5px; padding: 4px 1px; }
.card.is-tool .svcIcon { width: 24px; height: 24px; border-radius: 7px; font-size: 12px; }

/* modified: 服务图标色块——首字 + 由 id 哈希决定的色相（--icon-h 由 JS 内联给出），
   零图片资源。明暗两套各自取色，暗色下压暗底、提亮字，符合 WCAG 对比度。 */
.svcIcon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  border-radius: 9px;
  font-size: 14px;
  font-weight: 600;
  line-height: 1;
  background: hsl(var(--icon-h, 166) 46% 91%);
  color: hsl(var(--icon-h, 166) 52% 28%);
  border: 1px solid hsl(var(--icon-h, 166) 38% 82%);
}
html[data-theme="dark"] .svcIcon {
  background: hsl(var(--icon-h, 166) 26% 19%);
  color: hsl(var(--icon-h, 166) 62% 74%);
  border-color: hsl(var(--icon-h, 166) 24% 27%);
}
.card.is-hero .svcIcon { width: 38px; height: 38px; border-radius: 11px; font-size: 17px; }

/* modified: 编辑模式下的「隐藏」按钮。平时 display:none，只有进布局编辑才出现，
   避免日常点启动/停止时手滑把卡片弄没了。 */
.eyeBtn {
  display: none;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  flex: 0 0 auto;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--float-bg);
  color: var(--muted);
  cursor: pointer;
  transition: color .15s, border-color .15s;
}
.eyeBtn:hover { color: var(--danger); border-color: var(--danger); }
html.edit-mode .eyeBtn { display: inline-flex; }
/* modified: 2.4.2 虚线框只在悬停/拖动的卡上显示。
   原来 edit-mode 下 17 张卡全是虚线框，满屏噪音；
   眼睛按钮照常全量显示，"可隐藏"的暗示已经够了。 */
html.edit-mode .card:hover,
html.edit-mode .card.cardDragging { outline: 1px dashed var(--line); outline-offset: 2px; }

/* modified: 已隐藏条——只在编辑模式出现，点一下就恢复 */
.hiddenBar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 22px;
  padding: 12px 14px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  background: var(--glass-soft);
  font-size: 12px;
}
.hiddenLabel { color: var(--muted); }
.hiddenChip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--glass-brd);
  background: var(--card-bg);
  color: var(--muted);
  font-size: 12px;
  cursor: pointer;
  transition: color .15s, border-color .15s;
}
.hiddenChip span { color: var(--accent); }
.hiddenChip:hover { color: var(--text); border-color: var(--ring); }

/* modified: 2.4.2 编辑模式下组末尾的空位占位——提示"可以拖到这里"。
   只在编辑模式由 JS 生成（renderContent），平时不占任何空间。
   悬停它触发 .group.drop-end 虚线框（dragover 走 overCard=null 分支），两者叠加强化提示。 */
.dropSlot {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 64px;
  border: 1.5px dashed var(--line);
  border-radius: var(--radius-sm);
  color: var(--muted);
  font-size: 11.5px;
  opacity: .55;
  pointer-events: none;   /* 别抢 dragover，让它穿到 section 上走 drop-end 逻辑 */
}
.group.drop-end .dropSlot {
  opacity: 1;
  border-color: var(--accent);
  color: var(--accent);
}

/* modified: 2.4.2 顶栏地址可点击复制 */
.copyable {
  cursor: pointer;
  border-bottom: 1px dashed transparent;
  transition: color .15s, border-color .15s;
}
.copyable:hover { color: var(--accent); border-color: var(--accent); }

/* modified: 2.5.0 分组管理——新建分组入口 + 改名/新建用的输入框 */
.newGroupBox {
  margin: 24px 0 10px;
  display: flex;
}
.inputDlg {
  width: 100%;
  font: 14px/1.5 inherit;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: rgba(127, 127, 127, .07);
  color: var(--text);
  outline: none;
}
.inputDlg:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--ring); }

/* ------------------------------------------------------------------ 卡片 */
/* modified: 这里刻意【不用】backdrop-filter。
   原因：卡片正下方就是 .backdrop 里 3 个带 blur(90px)、且永久 drift 动画的光斑。
   一旦卡片自己再挂 backdrop-filter，光斑每移动一个像素，浏览器就要把每张卡片
   覆盖的那块区域重新采样、重新模糊一遍；17 张卡片 = 每秒 60 帧 × 17 次大区域
   高斯模糊。集显、老内核（如部分 360/QQ 浏览器的旧 Chromium）扛不住，表现就是
   卡片一直"闪来闪去"。而光斑本身已经是 90px 模糊的色团，再糊 22px 肉眼几乎无差，
   属于纯开销零收益。改用略高不透明度的 --card-bg 保住"通透但不刺眼"的观感。 */
.card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  /* modified: 四列布局下列宽只有 236px，内距从 16/18 收到 13/14，
     把省下的 9px 让给正文，说明文字不至于一行只剩六七个字。 */
  padding: 13px 14px 12px;
  border-radius: var(--radius);
  background: var(--card-bg);  /* modified: 原 var(--glass)，配合去掉背板模糊 */
  border: 1px solid var(--glass-brd);
  box-shadow: var(--shadow-1);
  transition: transform .18s ease, box-shadow .22s ease, background .22s;
  overflow: hidden;
}
.card::before {
  content: "";
  position: absolute;
  inset: 0 0 auto 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .9), transparent);
  opacity: .7;
}
html[data-theme="dark"] .card::before { opacity: .22; }
.card:hover { transform: translateY(-3px); box-shadow: var(--shadow-2); background: var(--glass-strong); }

/* modified: 2.4.2 拖动落点竖线——必须定义在 .card:hover 之后且带上 :hover 形态。
   拖动时鼠标必然悬停在目标卡上，两条同特异性规则里后定义的赢：
   若放在 :hover 之前（原位置），竖线会被 hover 阴影顶掉而不可见。
   hover 阴影用 --shadow-2 与日常悬停一致，只是左侧/右侧多一条强调色竖线。 */
.card.drop-before, .card.drop-before:hover { box-shadow: -3px 0 0 0 var(--accent), var(--shadow-2); }
.card.drop-after, .card.drop-after:hover { box-shadow: 3px 0 0 0 var(--accent), var(--shadow-2); }

/* modified: 加了图标色块后，标题用 flex:1 顶开，状态胶囊和眼睛按钮靠右 */
.cardHead { display: flex; align-items: flex-start; gap: 9px; }
.cardName {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 15.5px;
  font-weight: 700;
  line-height: 1.36;
  letter-spacing: .01em;
  word-break: break-word;
}
.cardHead .pill { flex: 0 0 auto; }
.cardId {
  display: inline-block;
  margin-left: 7px;
  padding: 1px 7px;
  border-radius: 6px;
  background: var(--glass-soft);
  border: 1px solid var(--glass-brd);
  color: var(--muted);
  font: 11px/1.5 Consolas, "Cascadia Mono", monospace;
  vertical-align: 2px;
}
.cardDesc {
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 39px;
}

.cardMeta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.cardMeta .dot { width: 3px; height: 3px; border-radius: 50%; background: currentColor; opacity: .45; }
.cardMeta b { color: var(--text); font-weight: 600; }

/* modified: 2.5.1 版本号与操作按钮并成一行——原来的 .cardVer 独占一行
   （9px 上距 + 虚线上边框 + 1 行文字 ≈ 30px），去掉后每张卡矮 30px，
   四列布局一屏能多显示近一排卡片。版本号缩小成右侧一枚淡色小标签，
   「更新于 xx」不再单独占位，改由 title 悬停提示（列表视图仍有更新时间列）。 */
.cardVersion {
  margin-left: auto;           /* 靠右，按钮从左起排 */
  flex: 0 0 auto;
  max-width: 46%;              /* 版本号过长时省略，不把按钮挤到第二行 */
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid var(--glass-brd);
  background: var(--glass-soft);
  color: var(--muted);
  font: 10.5px/1.55 Consolas, "Cascadia Mono", monospace;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* modified: 2.5.1 操作行现在装「停止 / 重启 / 打开网页 + 版本号」四项。
   四列下列宽 236px、内容区约 208px，所以按钮再收一档（字号 11.5、左右内距 8），
   腾出右侧约 45px 给版本号；flex-wrap 保留作兜底，只会换成两行、不会溢出卡片。
   margin-top:auto 让操作区始终贴卡片底部——同行等高时按钮才会对齐，
   留白落在描述与按钮之间，不会出现「按钮悬在中间、下面一大块空」。 */
.cardActions { display: flex; align-items: center; gap: 5px; margin-top: auto; padding-top: 2px; flex-wrap: wrap; }
.cardActions .btn { padding: 4px 8px; font-size: 11.5px; }
.linkBtn {
  appearance: none;
  border: 0;
  background: none;
  color: var(--muted);
  font-family: inherit;
  font-size: 12px;
  padding: 5px 2px;
  cursor: pointer;
  border-bottom: 1px dashed transparent;
  transition: color .16s, border-color .16s;
}
.linkBtn:hover { color: var(--accent); border-color: var(--accent); }

/* 状态胶囊（卡片与列表共用） */
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  background: var(--idle-bg);
  color: var(--idle);
}
.pill::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 16%, transparent);
}
.pill.run { background: var(--ok-bg); color: var(--ok); }
/* modified: 2.4.2 运行中圆点默认【静态】。它是全页唯一常驻动画，
   虽然只是 6px 圆点的 opacity，鉴于闪屏前科还是默认静止；
   悬停到卡片/表格行时才脉动一下，保留"活的"反馈。 */
.card:hover .pill.run::before,
.svcTable tbody tr:hover .pill.run::before { animation: pulse 2.4s ease-in-out infinite; }
.pill.external { background: var(--ext-bg); color: var(--ext); }
.pill.starting { background: var(--warn-bg); color: var(--warn); }
.pill.starting::before { animation: pulse 1s ease-in-out infinite; }
/* modified: 2.6.2 端口被别的程序占着 —— 红色，和"启动中"的黄色区分开 */
.pill.conflict { background: var(--danger-bg); color: var(--danger); }
.pill.tool { background: var(--tool-bg); color: var(--tool); }
/* 未安装：可执行文件不在本机（换电脑后旧路径失效），用灰调而不是红色警告 */
.pill.missing { background: rgba(127, 127, 127, .14); color: var(--muted); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

/* ------------------------------------------------------------------ 列表视图 */
.tableWrap {
  border-radius: var(--radius);
  padding: 6px 8px 8px;
  overflow: auto;
}
.svcTable {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0 8px;
  table-layout: auto;
}
.svcTable th {
  position: sticky;
  top: 0;
  z-index: 2;
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  text-align: left;
  white-space: nowrap;
  background: transparent;
  letter-spacing: .04em;
}
.svcTable th.sortable { cursor: pointer; user-select: none; }
.svcTable th:first-child { width: 38%; }
.svcTable th:last-child { width: 1%; }
.svcTable th.sortable:hover { color: var(--accent); }
.svcTable th .arrow { opacity: 0; margin-left: 4px; font-size: 10px; }
.svcTable th.sorted { color: var(--accent); }
.svcTable th.sorted .arrow { opacity: 1; }
.svcTable td {
  padding: 11px 12px;
  background: var(--glass);
  border-top: 1px solid var(--glass-brd);
  border-bottom: 1px solid var(--glass-brd);
  vertical-align: middle;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  transition: background .16s;
}
.svcTable td:first-child {
  border-left: 1px solid var(--glass-brd);
  border-radius: 12px 0 0 12px;
}
.svcTable td:last-child {
  border-right: 1px solid var(--glass-brd);
  border-radius: 0 12px 12px 0;
}
.svcTable tbody tr:hover td { background: var(--glass-strong); }
.svcTable .cName { font-size: 13.5px; font-weight: 600; }
.svcTable .cId { color: var(--muted); font: 11px/1.4 Consolas, monospace; margin-left: 6px; }
.svcTable .cDesc {
  color: var(--muted);
  font-size: 11.5px;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.svcTable .cVer code {
  font: 12px/1.4 Consolas, "Cascadia Mono", monospace;
  padding: 1px 7px;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
}
.svcTable .cUpd { color: var(--muted); white-space: nowrap; }
.svcTable .cPort { font-family: Consolas, monospace; }
.svcTable .cAct { white-space: nowrap; text-align: right; }
.svcTable .cAct .btn { padding: 4px 10px; font-size: 12px; margin-left: 6px; }
.svcTable .cAct .linkBtn { font-size: 12px; margin-left: 6px; }

/* ------------------------------------------------------------------ 空态 / 离线 */
.empty, .offline {
  margin: 28px auto;
  max-width: 620px;
  padding: 26px 22px;
  border-radius: var(--radius);
  text-align: center;
  color: var(--muted);
  background: var(--glass-soft);
  border: 1px dashed var(--glass-brd);
  font-size: 13px;
  line-height: 1.8;
}
.offline {
  border-style: solid;
  border-color: var(--warn);
  color: var(--warn);
  background: var(--warn-bg);
}

/* ------------------------------------------------------------------ 提示条 */
.toasts {
  position: fixed;
  right: 18px;
  bottom: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 80;
  max-width: min(380px, calc(100vw - 36px));
}
.toast {
  padding: 11px 15px;
  border-radius: 13px;
  background: var(--float-bg);
  border: 1px solid var(--glass-brd);
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  box-shadow: var(--shadow-2);
  font-size: 13px;
  line-height: 1.55;
  animation: toastIn .2s ease-out;
}
.toast.ok { border-left: 3px solid var(--ok); }
.toast.warn { border-left: 3px solid var(--warn); }
.toast.error { border-left: 3px solid var(--danger); }
@keyframes toastIn { from { transform: translateY(10px) scale(.98); } to { transform: none; } }

/* ------------------------------------------------------------------ 弹层 */
/* 入场动画只做位移，不做透明度：
   动画一旦被系统"减少动效"或渲染时间轴冻结，opacity:0 会让弹窗永远看不见（真踩过） */
.modalMask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(12, 20, 20, .38);
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
}
.modalMask[hidden] { display: none; }
.modal {
  width: min(760px, 100%);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  border-radius: 20px;
  background: var(--float-bg);
  border: 1px solid var(--glass-brd);
  -webkit-backdrop-filter: blur(24px) saturate(150%);
  backdrop-filter: blur(24px) saturate(150%);
  box-shadow: var(--shadow-2);
  animation: modalIn .2s cubic-bezier(.2, .9, .3, 1);
}
@keyframes modalIn { from { transform: translateY(12px) scale(.985); } to { transform: none; } }
.modalHead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
}
.modalHead h2 { margin: 0; font-size: 15.5px; letter-spacing: .03em; }
.modalBody { padding: 16px 18px; overflow: auto; font-size: 13px; }
.modalBody pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font: 12.5px/1.6 Consolas, "Cascadia Mono", monospace;
  color: var(--text);
}
.modalFoot {
  padding: 12px 18px;
  border-top: 1px solid var(--line);
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
/* modified: display:flex 会盖掉浏览器对 hidden 的默认 display:none，
   设置弹窗底部会残留上一次的"刷新日志"按钮。显式补一条。 */
.modalFoot[hidden] { display: none; }

/* 设置面板 */
.setGroup { margin-bottom: 6px; }
.setGroup + .setGroup { margin-top: 18px; }
.setGroupTitle {
  margin: 0 0 4px;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: .12em;
  color: var(--accent);
}
.setRow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 11px 0;
  border-bottom: 1px dashed var(--line);
}
.setRow:last-child { border-bottom: 0; }
.setMain { min-width: 0; }
.setLabel { font-size: 13px; font-weight: 600; }
.setHint { color: var(--muted); font-size: 11.5px; line-height: 1.55; margin-top: 2px; }
/* modified: 2.6.1 设置页显示「实际会用哪个浏览器」，退回系统默认时变红字提醒 */
.setResolved {
  color: var(--muted);
  font-size: 11.5px;
  line-height: 1.55;
  margin-top: 3px;
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}
.setResolved.warn { color: var(--warn); }
.setCtrl { flex: none; display: flex; align-items: center; gap: 8px; }
.setCtrl select, .setCtrl input[type="text"] {
  appearance: none;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text);
  background: rgba(127, 127, 127, .07);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 7px 10px;
  min-width: 150px;
  outline: none;
}
.setCtrl select {
  padding-right: 30px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1.5L6 6.5L11 1.5' fill='none' stroke='%235c6c69' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 10px center;
}
html[data-theme="dark"] .setCtrl select {
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1.5L6 6.5L11 1.5' fill='none' stroke='%2393a5a2' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");
}
.modal .btn:not(.primary), .modal .iconBtn { border-color: var(--line); background: rgba(127, 127, 127, .07); }
.modal .btn:not(.primary):hover, .modal .iconBtn:hover { background: rgba(127, 127, 127, .14); }
.setCtrl input[type="text"] { min-width: 300px; font-family: Consolas, monospace; font-size: 12px; }
.setCtrl select:focus, .setCtrl input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--ring); }

.switch { position: relative; display: inline-block; width: 44px; height: 25px; flex: none; }
.switch input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
.switch .track {
  position: absolute;
  inset: 0;
  border-radius: 999px;
  background: var(--idle-bg);
  border: 1px solid var(--glass-brd);
  transition: background .2s;
  pointer-events: none;
}
.switch .track::after {
  content: "";
  position: absolute;
  top: 2px; left: 2px;
  width: 19px; height: 19px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .25);
  transition: transform .2s, background .2s;
}
.switch input:checked + .track { background: var(--accent); border-color: transparent; }
.switch input:checked + .track::after { transform: translateX(19px); }
.switch input:focus-visible + .track { box-shadow: 0 0 0 3px var(--ring); }

.aboutGrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 8px 14px;
  padding: 12px 14px;
  border-radius: 14px;
  background: var(--glass-soft);
  border: 1px solid var(--glass-brd);
  font-size: 12px;
  color: var(--muted);
}
.aboutGrid b { display: block; color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; word-break: break-all; }
.pathLine { grid-column: 1 / -1; margin-top: 4px; color: var(--muted); font-size: 11px; word-break: break-all; font-family: Consolas, monospace; }

/* ------------------------------------------------------------------ 密度（2.4.0 新增）
   舒适 = 等宽四列、说明两行、显示版本行（默认）；
   紧凑 = 同样四列，只收紧间距、说明压一行、隐去版本行。
   两档都固定 4 列，切换密度时网格不会忽多忽少地跳。
   切换只给 <html> 加一个类，规则全集中在这里，业务逻辑零改动。 */
html.density-compact .grid {
  gap: 9px;
}
html.density-compact .card {
  gap: 7px;
  padding: 12px 13px 11px;
  border-radius: 14px;
}
html.density-compact .cardDesc { -webkit-line-clamp: 1; min-height: 0; }
html.density-compact .cardActions { gap: 5px; }
html.density-compact .cardActions .btn { padding: 3px 8px; font-size: 11.5px; }
html.density-compact .cardVersion { font-size: 10px; padding: 0 5px; }   /* modified: 2.5.1 版本号并入操作行，紧凑模式下再压一档 */
html.density-compact .svcIcon { width: 24px; height: 24px; border-radius: 7px; font-size: 12px; }
html.density-compact .cardName { font-size: 13.5px; }
html.density-compact .group { margin-top: 14px; }
html.density-compact .groupHead { margin-bottom: 8px; }

/* ------------------------------------------------------------------ 设置面板：已隐藏卡片 */
.hiddenManage { display: flex; flex-direction: column; gap: 7px; min-width: 210px; }
.hiddenManageRow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12.5px;
}
.hiddenNone { color: var(--muted); font-size: 12.5px; }

/* ------------------------------------------------------------------ 响应式 */
/* modified: 卡片网格固定 4 列，窄屏逐级退让。
   1180 以下是笔记本半屏 / 平板横屏，4 列每列不足 250px 会把标题压成三行。 */
@media (max-width: 1180px) {
  .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 860px) {
  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .topbar { margin: 8px 10px 0; padding: 10px 12px; gap: 10px; }
  .content, .toolbar { padding-left: 12px; padding-right: 12px; }
  .brandText h1 { font-size: 16px; }
  .sub { max-width: 52vw; }
  .divider { display: none; }
  .stats { margin-left: 0; }
  .setRow { flex-direction: column; align-items: flex-start; gap: 8px; }
  .setCtrl { width: 100%; }
  .setCtrl select, .setCtrl input[type="text"] { min-width: 0; width: 100%; }
}
@media (max-width: 560px) {
  .grid { grid-template-columns: 1fr; }
  .topActions { width: 100%; }
  .topActions .btn { flex: 1 1 auto; }
}

/* 系统要求减弱动效 / 不支持毛玻璃时的降级 */
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
/* ------------------------------------------------------------------ 渲染诊断模式
   用于定位"界面一直在闪"的来源。地址栏加参数即可，去掉参数完全不影响日常外观：
     ?fx=noanim  只停背景光斑动画
     ?fx=noblur  只关毛玻璃
     ?fx=off     两样都停（此时页面应当彻底静止）
   三个都试一遍，哪个不闪了，原因就是哪一个。 */
html.fx-noanim .blob,
html.fx-off .blob {
  animation: none !important;
}
html.fx-noblur .glass,
html.fx-noblur .searchWrap,
html.fx-noblur .modalMask,
html.fx-noblur .modal,
html.fx-noblur .toast,
html.fx-off .glass,
html.fx-off .searchWrap,
html.fx-off .modalMask,
html.fx-off .modal,
html.fx-off .toast {
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
}
/* 关掉毛玻璃后必须有底色兜底，否则文字直接糊在背景光斑上，比闪更难看清 */
html.fx-noblur .glass,
html.fx-off .glass,
html.fx-noblur .searchWrap,
html.fx-off .searchWrap {
  background: var(--bg-2);
}

.fxBadge {
  position: fixed;
  left: 14px;
  bottom: 14px;
  z-index: 999;
  padding: 7px 12px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.4;
  color: var(--accent-ink);
  background: var(--accent);
  box-shadow: var(--shadow-2);
  pointer-events: none;
}

@supports not ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px))) {
  /* modified: .card 已不再使用 backdrop-filter，从降级名单里移除，
     否则不支持毛玻璃的浏览器会把卡片压成完全不透明，白丢 --card-bg 的通透感 */
  .glass, .svcTable td, .modal, .toast, .searchWrap { background: var(--bg-2); }
  html[data-theme="dark"] .glass,
  html[data-theme="dark"] .svcTable td { background: #1a2325; }
}
```

---

## `hub/ui/app.js`

```javascript
/* 服务台网页端交互 —— 原生 JS，无框架无 CDN，断网离网都能跑。
   原则：界面只负责显示与发指令，所有判断都在后台 Python 里，两边状态永远一致。 */
(function () {
  'use strict';

  // modified: 2.6.3 补上 2.6.2 新增的「端口冲突」，列表按状态排序时
  // 它原来取兜底 rank 9，被埋在"未安装"后面。
  var STATE_RANK = { '运行中': 0, '外部运行': 1, '端口冲突': 2, '启动中': 3,
                     '工具': 4, '已停止': 5, '未安装': 6 };

  var state = {
    services: [],
    hub: {},
    keyword: '',
    group: '全部',
    view: 'card',
    sort: { key: 'group', dir: 'asc' },
    busy: {},            // id -> true，避免连点重复提交
    failures: 0,
    timer: null,
    autoRefresh: true,
    refreshMs: 2000,
    settings: {},
    browsers: [],
    browserResolved: null,   // modified: 2.6.1 后端算出的「实际会用哪个浏览器」
    theme: 'system',
    fetchedAt: Date.now(),
    // modified: 2.4.0 首页布局
    hiddenIds: [],       // 已隐藏的服务 id（来自 settings.hidden_ids）
    groupOrder: [],      // 分组顺序（来自 settings.group_order）
    density: 'comfortable', // comfortable | compact
    editMode: false,     // 布局编辑模式：只有开启时才能拖分组/拖卡片、才能点眼睛隐藏
    dragging: false,     // 拖动进行中 → 冻结重绘，别把正在拖的节点重建掉
    dragEl: null,
    // modified: 2.4.1 卡片拖拽的两个落点
    // modified: 2.6.3 组名是用户随便起的字符串，普通对象的原型链键
    // （constructor / toString / __proto__）会被当成"已有记录"读出来。
    // 实测新建名为 constructor 的空组时，orderedItems 拿到的是 Object 构造函数，
    // order.forEach 直接抛异常、整张页面渲染中断。这里统一用无原型对象。
    serviceGroup: Object.create(null),  // {id: 分组名}
    serviceOrder: Object.create(null),  // {分组名: [id, ...]}
    dragCardId: null,    // 正在拖的卡片 id
    dropTarget: null,    // 当前高亮的落点 {id, pos} 或 {group, pos:'end'}
    collapsed: Object.create(null),  // modified: 2.6.3 同上，组名做键必须无原型
    offline: '',         // modified: 2.6.3 后台连不上时的提示，交给 render 统一渲染
    settingsReady: false, // modified: 2.6.3 /api/settings 是否已返回（设置面板竞态）
    batchAll: false,      // modified: 2.6.3 顶栏批量操作的提交锁
    // modified: 2.6.3 组名做键一律用无原型对象，constructor/__proto__/toString
    // 这种用户起的组名才不会被 Object.prototype 上的同名属性冒充。
    batchBusy: Object.create(null)  // modified: 2.6.3 分组批量操作的提交锁
  };

  var el = {};
  ['hubMeta', 'content', 'searchBox', 'groupChips', 'stats', 'viewSeg',
   'btnStartAll', 'btnStopAll', 'btnRefresh', 'btnExit', 'btnTheme', 'btnSettings',
   'btnLayout',
   'toasts', 'modalMask', 'modalTitle', 'modalBody', 'modalFoot', 'modalClose'
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  var lastPrint = '';
  var chipsSig = '';   // modified: 分组按钮的签名，没变就不重建 DOM
  var offlinePrinted = '';   // modified: 2.6.3 离线提示只写一次 DOM
  var modalOnClose = null;   // modified: 2.6.3 确认框被取消时的清理钩子

  // ------------------------------------------------------------------ 工具
  // modified: 2.6.3 把服务端/备份里的普通对象转成无原型字典，
  // 组名做键时不会被 Object.prototype 上的同名属性污染。
  function noProto(obj) {
    var out = Object.create(null);
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      Object.keys(obj).forEach(function (k) { out[k] = obj[k]; });
    }
    return out;
  }

  function esc(text) {
    if (text === null || text === undefined) { return ''; }
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function stateClass(text) {
    if (text === '运行中') { return 'run'; }
    if (text === '外部运行') { return 'external'; }
    if (text === '启动中') { return 'starting'; }
    if (text === '端口冲突') { return 'conflict'; }   // modified: 2.6.2
    if (text === '工具') { return 'tool'; }
    if (text === '未安装') { return 'missing'; }
    return '';
  }

  // modified: 2.6.2 端口冲突时把"谁占着"挂到胶囊的悬停提示上，
  // 光看"端口冲突"四个字还得自己 netstat 才知道去找哪个程序。
  function stateTip(svc) {
    var pc = svc && svc.portConflict;
    if (!pc) { return ''; }
    return ' title="' + esc('端口 ' + pc.port + ' 被 ' + (pc.name || '未知程序') +
      '（PID ' + (pc.pid || '?') + '）占用') + '"';
  }

  // modified: 2.6.3 停着但端口被占的服务，启动按钮先给出原因，别等点了才报错
  function startTitle(svc) {
    var pc = svc && svc.portConflict;
    if (!pc) { return ''; }
    return ' title="' + esc('端口 ' + pc.port + ' 被 ' + (pc.name || '未知程序') +
      ' 占用，请先停掉占用它的程序') + '"';
  }

  function humanUptime(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    if (!seconds) { return ''; }
    if (seconds < 60) { return seconds + ' 秒'; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + ' 分'; }
    var hours = Math.floor(seconds / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    if (hours < 24) { return hours + ' 时 ' + mins + ' 分'; }
    return Math.floor(hours / 24) + ' 天 ' + (hours % 24) + ' 时';
  }

  // modified: 2.6.1 当前设置会用哪个浏览器，给按钮 title 和设置页提示共用。
  // 优先用后端算好的结果（它知道 exe 在不在、会不会退回系统默认）。
  function browserLabel() {
    var info = state.browserResolved;
    if (info && info.name) {
      if (info.reason) { return '系统默认浏览器（' + info.name + ' 不可用）'; }
      return info.name;
    }
    var bid = (state.settings || {}).browser || 'auto';
    if (bid === 'auto') { return '系统默认浏览器'; }
    if (bid === 'custom') { return '自定义浏览器'; }
    var list = state.browsers || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === bid) { return list[i].name; }
    }
    return bid;
  }

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast ' + (kind || '');
    box.textContent = message;
    el.toasts.appendChild(box);
    setTimeout(function () {
      box.style.opacity = '0';
      box.style.transition = 'opacity .25s';
      setTimeout(function () { if (box.parentNode) { box.parentNode.removeChild(box); } }, 260);
    }, 3600);
  }

  function request(url, options) {
    return fetch(url, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (payload) {
        if (!resp.ok) { throw new Error(payload.message || ('HTTP ' + resp.status)); }
        return payload;
      });
    });
  }

  function get(url) {
    return request(url, { cache: 'no-store' });
  }

  function post(url, body) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ServiceHub': '1' },
      cache: 'no-store',
      body: JSON.stringify(body || {})
    });
  }

  // ------------------------------------------------------------------ 主题 / 视图
  function resolveTheme(theme) {
    if (theme === 'system') {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return theme === 'dark' ? 'dark' : 'light';
  }

  function applyTheme(theme, persist) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', resolveTheme(theme));
    if (persist) {
      try { localStorage.setItem('hub.theme', theme); } catch (e) {}
    }
  }

  function setView(view, persist) {
    state.view = view === 'list' ? 'list' : 'card';
    Array.prototype.forEach.call(el.viewSeg.querySelectorAll('.segBtn'), function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-view') === state.view);
    });
    if (persist) {
      try { localStorage.setItem('hub.view', state.view); } catch (e) {}
    }
    lastPrint = '';
    render();
  }

  // modified: 渲染诊断模式。界面"闪"的时候，光靠猜分不清是毛玻璃（backdrop-filter）
  // 还是背景动效造成的，这里给出三个 URL 开关，用排除法一次定位：
  //   ?fx=noanim  只停背景光斑动画
  //   ?fx=noblur  只关毛玻璃（改用不透明底色）
  //   ?fx=off     两样都停（理论上页面将完全静止）
  // 不带 ?fx= 或带不认识的值 = 正常外观，对日常使用零影响。
  var FX_LABEL = {
    noanim: '已停背景动效',
    noblur: '已关毛玻璃',
    off: '已停动效 + 关毛玻璃'
  };
  function applyFxMode(fx) {
    var label = FX_LABEL[fx];
    if (!label) { return; }
    var root = document.documentElement;
    root.classList.add('fx-' + fx);
    var badge = document.createElement('div');
    badge.className = 'fxBadge';
    badge.textContent = '渲染诊断：' + label + '（去掉地址里的 ?fx= 即恢复正常）';
    document.body.appendChild(badge);
  }


  // modified: 背景光斑漂移。默认关——首屏就是静止的，不存在"加载完设置才开始动"的跳变。
  // Edge + Intel 集显 + 高 DPI 下这三个大图层持续运动会整屏闪烁，所以做成可选。
  function applyBgMotion(on) {
    var root = document.documentElement;
    if (on) { root.classList.add('bg-motion'); } else { root.classList.remove('bg-motion'); }
  }

  // ------------------------------------------------------------------ 布局
  // modified: 以下整块为 2.4.0 新增——卡片隐藏 / 分组排序 / 密度 / 编辑模式。

  // 服务图标色块：取名字首字（英文取大写首字母），色相由 id 哈希决定，
  // 同一个服务永远同一个颜色。不引外部图片、不增依赖，靠 --icon-h 让明暗两套各自取色。
  var ICON_HUES = [166, 200, 262, 32, 342, 96, 12, 220];
  function iconOf(svc) {
    var name = String(svc.name || svc.id || '?').trim();
    var ch = name.charAt(0) || '?';
    if (/[a-z]/i.test(ch)) { ch = ch.toUpperCase(); }
    var key = String(svc.id || name);
    var hash = 0;
    for (var i = 0; i < key.length; i++) { hash = (hash * 31 + key.charCodeAt(i)) % 9973; }
    return { ch: ch, hue: ICON_HUES[hash % ICON_HUES.length] };
  }

  function isHidden(id) { return state.hiddenIds.indexOf(id) >= 0; }

  // modified: 2.4.1 有效分组——settings 里的 service_group 优先于 services.json 的 group。
  // 卡片被拖到别的组后只在本机改归属，注册表不动，所以两边可能不一致；
  // 界面显示、筛选、搜索、分组统计一律以这里为准。
  function groupOf(svc) {
    var override = state.serviceGroup[svc.id];
    return (typeof override === 'string' && override) ? override : svc.group;
  }

  function serviceById(id) {
    for (var i = 0; i < state.services.length; i++) {
      if (state.services[i].id === id) { return state.services[i]; }
    }
    return null;
  }

  // 组内顺序：service_order[组名] 里登记过的按登记位置，没登记的接在后面，
  // 并保持它们在 services.json 里的原始相对顺序。
  // Array#sort 从 ES2019 起保证稳定，所以"都没登记"时顺序与原来完全一致。
  function orderedItems(group, items) {
    var order = state.serviceOrder[group];
    // modified: 2.6.3 必须是数组；原型链键/脏数据给个函数或字符串时，
    // 原来的 order.forEach 会把整个 render() 打断。
    if (!Array.isArray(order) || !order.length) { return items; }
    var pos = Object.create(null);  // modified: 2.6.3 服务 id 也按无原型字典存
    order.forEach(function (id, i) { pos[id] = i; });
    var big = order.length + items.length + 1;   // 未登记的排到最后
    return items.slice().sort(function (a, b) {
      var pa = pos[a.id] === undefined ? big : pos[a.id];
      var pb = pos[b.id] === undefined ? big : pos[b.id];
      return pa - pb;
    });
  }

  // 拖动落盘时用它：以「全部服务」（含被隐藏的）为底算一份完整布局，
  // 这样拖动结果不会因为某组折叠、某些卡隐藏而丢掉那一部分的顺序。
  function computeLayout() {
    // modified: 2.6.3 分组名做键必须用无原型对象，否则名为 constructor 的组
    // 会读到 Object 构造函数，groups[g].push 直接抛异常（拖卡片全崩）。
    var groups = Object.create(null);
    var names = [];
    state.services.forEach(function (svc) {
      var g = groupOf(svc);
      if (!groups[g]) { groups[g] = []; names.push(g); }
      groups[g].push(svc);
    });
    var out = Object.create(null);  // modified: 2.6.3 同上，落盘布局不能被原型链污染
    names.forEach(function (g) {
      out[g] = orderedItems(g, groups[g]).map(function (s) { return s.id; });
    });
    return out;
  }

  // 把一张卡移到 targetGroup 里 beforeId 之前（beforeId 为空 = 放到该组末尾）。
  // 只改 settings.service_group / service_order，绝不写 services.json。
  function moveCard(cardId, targetGroup, beforeId) {
    var svc = serviceById(cardId);
    var target = String(targetGroup || '').trim();
    if (!svc || !target) { return; }

    var layout = computeLayout();
    // 先从所有组里摘掉，再插目标位置——这样"拖到自己前面"也不会出现重复项
    Object.keys(layout).forEach(function (g) {
      layout[g] = layout[g].filter(function (id) { return id !== cardId; });
    });
    if (!layout[target]) { layout[target] = []; }
    var idx = beforeId ? layout[target].indexOf(beforeId) : -1;
    if (idx >= 0) { layout[target].splice(idx, 0, cardId); }
    else { layout[target].push(cardId); }

    // 组归属：拖回 services.json 原本的组时就把覆盖删掉，不留下无意义的记录
    var svcGroup = groupOf(svc);            // 注意要在改 state 之前取
    if (target === svc.group) { delete state.serviceGroup[cardId]; }
    else { state.serviceGroup[cardId] = target; }

    // 组内顺序：只保留非空的组
    var nextOrder = Object.create(null);  // modified: 2.6.3 同上
    Object.keys(layout).forEach(function (g) {
      if (layout[g].length) { nextOrder[g] = layout[g]; }
    });
    state.serviceOrder = nextOrder;

    lastPrint = '';
    chipsSig = '';
    render();
    // modified: 2.4.2 成功文案走 saveSettings 的 note 参数，失败只弹它内部那条
    // 「保存失败」。原来是 .then 里弹成功——saveSettings 失败时 catch 吞错照样 resolve，
    // 会先弹「保存失败」再弹「顺序已保存」，两条矛盾提示。
    var moved = target !== svcGroup;
    saveSettings({
      service_group: state.serviceGroup,
      service_order: nextOrder
    }, moved ? '已移到「' + target + '」（只改本机布局，services.json 未动）'
             : '顺序已保存');
  }


  // 注意：隐藏只影响“看得见看不见”，不影响后台采集，
  // 所以被隐藏的服务仍在 state.services 里，启动/停止状态照常刷新。
  function visibleServices() {
    if (!state.hiddenIds.length) { return state.services; }
    return state.services.filter(function (s) { return !isHidden(s.id); });
  }

  // 分组顺序：先按 group_order 里登记的顺序，未登记的新分组接在末尾。
  // 这样以后往 services.json 加一个新分组，不用重新拖一遍。
  function orderedGroups() {
    var seen = [];
    visibleServices().forEach(function (svc) {
      var g = groupOf(svc);   // modified: 拖动过的卡算在它当前所在的组
      if (seen.indexOf(g) < 0) { seen.push(g); }
    });
    var out = [];
    state.groupOrder.forEach(function (name) {
      if (seen.indexOf(name) >= 0 && out.indexOf(name) < 0) { out.push(name); }
    });
    seen.forEach(function (name) {
      if (out.indexOf(name) < 0) { out.push(name); }
    });
    return out;
  }

  function groupCollapsed(name) {
    // modified: 2.6.3 state.collapsed 原来只写不读。隐私模式/禁用站点数据时
    // localStorage 抛异常，折叠按钮点了毫无反应；内存态优先就能在本会话生效。
    if (Object.prototype.hasOwnProperty.call(state.collapsed, name)) {
      return !!state.collapsed[name];
    }
    try {
      var saved = localStorage.getItem('hub.collapse.' + name) === '1';
      state.collapsed[name] = saved;
      return saved;
    } catch (e) { return false; }
  }

  function setGroupCollapsed(name, on) {
    try {
      if (on) { localStorage.setItem('hub.collapse.' + name, '1'); }
      else { localStorage.removeItem('hub.collapse.' + name); }
    } catch (e) {}
    state.collapsed[name] = !!on;
  }

  function applyDensity(density) {
    state.density = density === 'compact' ? 'compact' : 'comfortable';
    var root = document.documentElement;
    root.classList.toggle('density-compact', state.density === 'compact');
    try { localStorage.setItem('hub.density', state.density); } catch (e) {}
  }

  function setEditMode(on) {
    state.editMode = !!on;
    // modified: 2.6.0 列表视图里没有卡片可拖、也没有眼睛按钮，
    // 在列表里点「布局」看起来就像"功能坏了"。进入编辑时自动切回卡片视图
    // （persist=false：只改当前这一次，不动用户的默认视图设置）。
    if (state.editMode && state.view !== 'card') { setView('card', false); }
    document.documentElement.classList.toggle('edit-mode', state.editMode);
    var btn = el.btnLayout;
    if (btn) {
      btn.classList.toggle('active', state.editMode);
      btn.textContent = state.editMode ? '完成' : '布局';
      btn.title = state.editMode ? '退出布局编辑，保存当前顺序' : '进入布局编辑：可拖动分组、隐藏卡片';
    }
    lastPrint = '';
    render();
    if (state.editMode) {
      toast('布局编辑：拖卡片可换位置/换组，拖分组标题可换分组顺序，点眼睛可隐藏', 'ok');
    }
  }

  function saveGroupOrder(order) {
    // modified: 2.6.3 后台掉线时 offline 提示会替换内容区，dragend 读到的
    // order 是空数组；旧代码会把它当成"用户清空了分组顺序"直接写盘。
    if (!order.length) { toast('这次没有读到分组，顺序未改动', 'warn'); return; }
    var same = order.length === state.groupOrder.length &&
      order.every(function (name, i) { return name === state.groupOrder[i]; });
    if (same) { return; }
    state.groupOrder = order;
    lastPrint = '';
    saveSettings({ group_order: order }, null);
  }

  function setHidden(id, hidden) {
    var next = state.hiddenIds.filter(function (x) { return x !== id; });
    if (hidden) { next.push(id); }
    state.hiddenIds = next;
    lastPrint = '';
    chipsSig = '';
    render();
    // modified: 2.4.2 同 moveCard：成功文案走 note，避免保存失败时误弹「已恢复」
    saveSettings({ hidden_ids: next },
      hidden ? '已从首页隐藏，可在设置里恢复' : '已恢复到首页');
  }

  // ------------------------------------------------------------------ 渲染
  function renderChips() {
    // modified: 分组顺序改由 orderedGroups() 决定（跟随拖动结果），
    // 并加签名比对——顺序和选中项都没变时直接返回，不再每秒清空重建一遍按钮。
    var groups = ['全部'].concat(orderedGroups());
    if (groups.indexOf(state.group) < 0) { state.group = '全部'; }
    var sig = groups.join('\u0001') + '#' + state.group;
    if (sig === chipsSig) { return; }
    chipsSig = sig;
    el.groupChips.innerHTML = '';
    groups.forEach(function (name) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (name === state.group ? ' active' : '');
      chip.textContent = name;
      chip.addEventListener('click', function () {
        state.group = name;
        lastPrint = '';
        render();
      });
      el.groupChips.appendChild(chip);
    });
  }

  function renderStats() {
    var visible = visibleServices();
    var total = visible.length;
    var hidden = state.services.length - total;
    var running = visible.filter(function (s) { return s.running; }).length;
    var tools = visible.filter(function (s) { return s.kind === 'tool'; }).length;
    var cost = Math.round((state.hub || {}).cost_ms || 0);
    var parts = ['共 <b>' + total + '</b> 项'];
    if (hidden) { parts.push('隐藏 <b>' + hidden + '</b>'); }
    parts.push('运行 <b>' + running + '</b>');
    if (tools) { parts.push('工具 <b>' + tools + '</b>'); }
    if (cost) { parts.push('扫描 <b>' + cost + '</b> ms'); }
    el.stats.innerHTML = parts.join(' · ');
  }

  function matches(svc) {
    var hitGroup = state.group === '全部' || groupOf(svc) === state.group;  // modified
    if (!hitGroup) { return false; }
    var key = state.keyword.trim().toLowerCase();
    if (!key) { return true; }
    var hay = [svc.name, svc.desc, groupOf(svc), svc.portText, svc.state, svc.id,  // modified
               svc.version, svc.updated].join(' ').toLowerCase();
    return hay.indexOf(key) >= 0;
  }

  function uptimeSpan(svc) {
    if (!svc.running || !svc.uptime) { return ''; }
    return '<span>已运行 <b data-uptime="' + svc.uptime + '" data-uptime-at="' +
      state.fetchedAt + '">' + esc(humanUptime(svc.uptime)) + '</b></span>';
  }

  function buildCard(svc, hero) {
    var card = document.createElement('div');
    // modified: is-hero = Bento 大卡（占两列），is-tool = 一次性脚本（横向扁条）
    card.className = 'card' + (hero ? ' is-hero' : '') + (svc.kind === 'tool' ? ' is-tool' : '');
    card.setAttribute('data-id', svc.id);

    var meta = '<span>端口 <b>' + esc(svc.portText) + '</b></span>';
    if (svc.kind === 'tool') {
      meta += '<span class="dot"></span><span>一次性脚本</span>';
    } else {
      var up = uptimeSpan(svc);
      if (up) { meta += '<span class="dot"></span>' + up; }
    }
    if (svc.pid) { meta += '<span class="dot"></span><span>PID <b>' + esc(svc.pid) + '</b></span>'; }
    if (svc.external) { meta += '<span class="dot"></span><span>非本工作台启动</span>'; }
    if (svc.autostart === false) { meta += '<span class="dot"></span><span>不参与全部启动</span>'; }
    if (svc.lastExit !== null && svc.lastExit !== undefined && svc.lastExit !== 0) {  // modified
      meta += '<span class="dot"></span><span>上次退出 <b>' + esc(svc.lastExit) + '</b></span>';  // modified
    }

    var actions = '';
    if (svc.kind === 'tool') {
      actions += '<button type="button" class="btn primary" data-act="run" data-id="' + esc(svc.id) + '">运行</button>';
    } else {
      actions += svc.running
        ? '<button type="button" class="btn danger" data-act="stop" data-id="' + esc(svc.id) + '">停止</button>'
        : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '"'
          + startTitle(svc) + '>启动</button>';   // modified: 2.6.3 冲突时先说明原因
      if (svc.running) {  // modified: 重启只对运行中的常驻服务显示
        actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
      }
    }
    // modified: 2.6.1 没有地址就不画这个按钮。
    // 以前是先画出来再 disabled：16 张卡里有 3 张永远是灰的，点了毫无反应，
    // 用户以为按钮坏了（真实原因是服务没运行，或它压根没有网页入口）。
    if (svc.kind !== 'tool' && svc.url) {
      actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
        + ' title="用 ' + esc(browserLabel()) + ' 打开 ' + esc(svc.url) + '">打开网页</button>';
    }
    // 【删除】卡片上的「查看日志」按钮（2.4.2）：四列布局下操作区太挤。
    // 被删逻辑去向：日志入口保留在列表视图的「日志」按钮（data-act="log" 委托未动），
    // 需要看日志时切到列表视图即可；后台日志文件仍在 logs\ 目录可直接翻。

    var icon = iconOf(svc);
    // modified: 编辑模式下每张卡右上角出现「隐藏」按钮。平时不出现，
    // 避免日常操作时误点；隐藏后从首页移除，但后台照样采集状态。
    var eye = '';
    if (state.editMode) {
      eye = '<button type="button" class="eyeBtn" data-hide="' + esc(svc.id) + '"' +
        ' title="在首页隐藏这个服务" aria-label="隐藏 ' + esc(svc.name) + '">' +
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"' +
        ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M3.5 3.5l17 17"></path>' +
        '<path d="M10.6 10.6a2 2 0 0 0 2.8 2.8"></path>' +
        '<path d="M6.7 6.9C4.6 8.2 3.1 10.2 2.5 12c1 3 4.5 7 9.5 7 1.4 0 2.7-.3 3.8-.9"></path>' +
        '<path d="M9.8 5.2A9.7 9.7 0 0 1 12 5c5 0 8.5 4 9.5 7-.3 1-1 2.2-2.1 3.4"></path>' +
        '</svg></button>';
    }

    card.innerHTML =
      '<div class="cardHead">' +
        '<span class="svcIcon" style="--icon-h:' + icon.hue + '" aria-hidden="true">' +
          esc(icon.ch) + '</span>' +
        '<div class="cardName" title="' + esc(svc.name) + '">' + esc(svc.name) +
          '<span class="cardId">' + esc(svc.id) + '</span>' +
        '</div>' +
        '<span class="pill ' + stateClass(svc.state) + '"' + stateTip(svc) + '>' + esc(svc.state) + '</span>' +
        eye +
      '</div>' +
      '<div class="cardDesc">' + esc(svc.desc) + '</div>' +
      '<div class="cardMeta">' + meta + '</div>' +
      // modified: 2.5.1 版本号并进操作按钮同一行——原来 cardVer 独占一行
      // （虚线分隔 + 9px 上距 ≈ 30px 高度），17 张卡合计少滚近一屏。
      // 【删除】卡片上的「更新于 xx」文本：去向 = 版本号的 title 提示（悬停可见）、
      // 列表视图的「更新时间」列；信息没丢，只是不占版面。
      '<div class="cardActions">' + actions +
        '<span class="cardVersion" title="更新于 ' + esc(svc.updated || '—') + '">' +
          esc(svc.version || '—') + '</span>' +
      '</div>';

    // modified: 编辑模式下卡片本身也能拖——组内换序、跨组移动。
    // 平时不给 draggable，避免看页面时手滑把元素拖出来（浏览器默认拖动会拖走文字）。
    if (state.editMode) {
      card.setAttribute('draggable', 'true');
      card.classList.add('cardSortable');
    }
    if (state.busy[svc.id]) {
      Array.prototype.forEach.call(card.querySelectorAll('button'), function (btn) { btn.disabled = true; });
    }
    return card;
  }

  // ------------------------------------------------------------------ 列表视图
  var COLUMNS = [
    { key: 'name', label: '服务' },
    { key: 'group', label: '分组' },
    { key: 'version', label: '版本号' },
    { key: 'updated', label: '更新时间' },
    { key: 'state', label: '状态' },
    { key: 'port', label: '端口' },
    { key: 'actions', label: '操作' }
  ];

  function versionParts(value) {
    return String(value || '').split(/[^0-9]+/).filter(function (x) { return x !== ''; })
      .map(function (x) { return parseInt(x, 10) || 0; });
  }

  function compare(a, b, key) {
    if (key === 'version') {
      var pa = versionParts(a.version), pb = versionParts(b.version);
      for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
        var va = pa[i] === undefined ? -1 : pa[i];
        var vb = pb[i] === undefined ? -1 : pb[i];
        if (va !== vb) { return va - vb; }
      }
      return 0;
    }
    if (key === 'state') {
      var ra = STATE_RANK[a.state] === undefined ? 9 : STATE_RANK[a.state];
      var rb = STATE_RANK[b.state] === undefined ? 9 : STATE_RANK[b.state];
      return ra - rb;
    }
    if (key === 'group') {
      // modified: 2.4.2 列表「分组」列显示的是 groupOf（拖动后的组），
      // 排序也必须用它——原来走通用分支拿 a.group 原值，显示与排序不一致。
      return String(groupOf(a) || '').localeCompare(String(groupOf(b) || ''), 'zh');
    }
    if (key === 'port') {
      var na = a.port === null || a.port === undefined ? 999999 : a.port;
      var nb = b.port === null || b.port === undefined ? 999999 : b.port;
      return na - nb;
    }
    if (key === 'updated') {
      return String(a.updated || '').localeCompare(String(b.updated || ''));
    }
    return String(a[key] || '').localeCompare(String(b[key] || ''), 'zh-Hans-CN', { numeric: true });
  }

  function sortedList(list) {
    var copy = list.slice();
    var key = state.sort.key;
    var dir = state.sort.dir === 'desc' ? -1 : 1;
    copy.sort(function (a, b) {
      var result = compare(a, b, key);
      if (result === 0) { result = compare(a, b, 'name'); }
      return result * dir;
    });
    return copy;
  }

  function renderList(list) {
    var rows = sortedList(list);
    var head = COLUMNS.map(function (col) {
      if (col.key === 'actions') {
        return '<th>' + esc(col.label) + '</th>';   // modified: 2.6.3 去掉恒空 class
      }
      var active = state.sort.key === col.key;
      var arrow = active ? (state.sort.dir === 'desc' ? '▼' : '▲') : '▲';
      return '<th class="sortable' + (active ? ' sorted' : '') + '" data-sort="' + col.key +
        '" title="点击排序">' + esc(col.label) + '<span class="arrow">' + arrow + '</span></th>';
    }).join('');

    var body = rows.map(function (svc) {
      var actions = '';
      if (svc.kind === 'tool') {
        actions += '<button type="button" class="btn primary" data-act="run" data-id="' + esc(svc.id) + '">运行</button>';
      } else {
        actions += svc.running
          ? '<button type="button" class="btn danger" data-act="stop" data-id="' + esc(svc.id) + '">停止</button>'
          : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '"'
            + startTitle(svc) + '>启动</button>';   // modified: 2.6.3
        if (svc.running) {  // modified
          actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
        }
      }
      // modified: 2.6.1 与卡片一致——没地址就不画按钮，避免一排灰按钮看着像坏了
      // modified: 2.6.3 补上 kind 判断：卡片排除了工具卡，列表漏了，两个视图不一致
      if (svc.kind !== 'tool' && svc.url) {
        actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
          + ' title="用 ' + esc(browserLabel()) + ' 打开 ' + esc(svc.url) + '">打开网页</button>';
      }
      actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">日志</button>';

      return '<tr>' +
        '<td class="cName">' + esc(svc.name) + '<span class="cId">' + esc(svc.id) + '</span>' +
          '<div class="cDesc">' + esc(svc.desc) + '</div></td>' +
        '<td>' + esc(groupOf(svc)) + '</td>' +   // modified: 显示拖动后的有效分组
        '<td class="cVer"><code>' + esc(svc.version || '—') + '</code></td>' +
        '<td class="cUpd">' + esc(svc.updated || '—') + '</td>' +
        '<td><span class="pill ' + stateClass(svc.state) + '"' + stateTip(svc) + '">' + esc(svc.state) + '</span></td>' +
        '<td class="cPort">' + esc(svc.portText) + '</td>' +
        '<td class="cAct">' + actions + '</td>' +
      '</tr>';
    }).join('');

    var wrap = document.createElement('div');
    wrap.className = 'tableWrap glass';
    wrap.innerHTML = '<table class="svcTable"><thead><tr>' + head + '</tr></thead><tbody>' +
      body + '</tbody></table>';

    if (state.busy && Object.keys(state.busy).length) {
      Array.prototype.forEach.call(wrap.querySelectorAll('button[data-id]'), function (btn) {
        if (state.busy[btn.getAttribute('data-id')]) { btn.disabled = true; }  // modified: 不再拼选择器，id 带引号也不会注入
      });
    }
    el.content.appendChild(wrap);
  }

  // modified: 原 Bento 主卡（每组挑一张占两列）已停用——用户要求所有卡片等宽，
  // 这样 1440 宽下一行能排 4 张，一屏看到更多服务。
  // buildCard 的 hero 形参与 CSS 的 .is-hero 规则都保留着，将来要恢复只需
  // 在 renderContent 里给某张卡传 true。

  function buildGroupHead(name, items) {
    var head = document.createElement('header');
    head.className = 'groupHead';

    var running = items.filter(function (s) { return s.running; }).length;
    var startable = items.filter(function (s) {
      // modified: 2.6.3 端口冲突的服务启动了也只会秒退，别算进"可启动 N 项"
      return s.kind !== 'tool' && !s.running && s.autostart !== false &&
        s.state !== '端口冲突';
    }).length;
    var collapsed = groupCollapsed(name);
    // modified: 2.5.0 编辑模式下追加「改名 / 删除」，平时不显示
    var editActs = '';
    if (state.editMode) {
      editActs =
        '<button type="button" class="btn tiny" data-groupedit="rename" title="重命名分组">改名</button>' +
        '<button type="button" class="btn tiny danger" data-groupedit="delete" title="删除分组">删除</button>';
    }

    head.innerHTML =
      '<button type="button" class="groupToggle" aria-expanded="' + (collapsed ? 'false' : 'true') + '"' +
        ' title="' + (collapsed ? '展开' : '折叠') + '">' +
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"' +
        ' stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M9 6l6 6-6 6"></path></svg></button>' +
      '<span class="groupName">' + esc(name) + '</span>' +
      '<span class="groupCount">' + items.length + ' 项' +
        (running ? ' · <b>' + running + ' 运行</b>' : '') + '</span>' +
      '<span class="groupActs">' +
        '<button type="button" class="btn tiny" data-groupact="start"' + (startable ? '' : ' disabled') +
          '>启动本组</button>' +
        '<button type="button" class="btn tiny" data-groupact="stop"' + (running ? '' : ' disabled') +
          '>停止本组</button>' +
        editActs +
      '</span>' +
      '<span class="dragGrip" title="拖动调整分组顺序" aria-hidden="true">' +
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">' +
        '<circle cx="9" cy="6" r="1.5"></circle><circle cx="15" cy="6" r="1.5"></circle>' +
        '<circle cx="9" cy="12" r="1.5"></circle><circle cx="15" cy="12" r="1.5"></circle>' +
        '<circle cx="9" cy="18" r="1.5"></circle><circle cx="15" cy="18" r="1.5"></circle>' +
        '</svg></span>';

    head.querySelector('.groupToggle').addEventListener('click', function () {
      setGroupCollapsed(name, !groupCollapsed(name));
      lastPrint = '';
      render();
    });

    Array.prototype.forEach.call(head.querySelectorAll('[data-groupact]'), function (btn) {
      btn.addEventListener('click', function () {
        batchGroup(name, btn.getAttribute('data-groupact'));
      });
    });

    // modified: 2.5.0 分组改名 / 删除
    Array.prototype.forEach.call(head.querySelectorAll('[data-groupedit]'), function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();   // 别让 groupHead 的拖动/折叠逻辑误触
        var act = btn.getAttribute('data-groupedit');
        if (act === 'rename') { renameGroup(name); }
        else if (act === 'delete') { deleteGroup(name); }
      });
    });
    return head;
  }

  // 组内批量启停：逐条串行发单条 action，而不是后端新开批量接口。
  // 串行是刻意的——几条 Python 进程同时冷启动会抢 CPU，串行反而更快看到结果。
  function batchGroup(name, action) {
    // modified: 2.6.3 连点「启动本组」会并行发起两轮，第二轮全撞"已在运行"
    var key = name + ':' + action;
    if (state.batchBusy[key]) { return; }
    state.batchBusy[key] = true;
    var label = action === 'start' ? '启动' : '停止';
    var items = visibleServices().filter(function (s) {
      if (groupOf(s) !== name) { return false; }   // modified
      if (action === 'start') {
        // modified: 2.6.3 端口被别人占着的服务启动了也只会秒退，别算进批量
        return s.kind !== 'tool' && !s.running && s.autostart !== false &&
          s.state !== '端口冲突';
      }
      return !!s.running;
    });
    if (!items.length) {
      delete state.batchBusy[key];
      toast('「' + name + '」没有需要' + label + '的服务', 'warn');
      return;
    }
    toast('正在' + label + '「' + name + '」' + items.length + ' 项…');
    var index = 0, done = 0, failed = 0;
    function step() {
      if (index >= items.length) {
        delete state.batchBusy[key];   // modified: 2.6.3
        toast(label + '完成：成功 ' + done + ' 项' + (failed ? '，失败 ' + failed + ' 项' : ''),
              failed ? 'warn' : 'ok');
        delayedRefresh();
        return;
      }
      var svc = items[index++];
      setBusy(svc.id, true);
      post('/api/action', { id: svc.id, action: action }).then(function (res) {
        if (res.ok) { done += 1; } else { failed += 1; }
      }).catch(function () {
        failed += 1;
      }).then(function () {
        setBusy(svc.id, false);
        step();
      });
    }
    step();
  }

  function makeDraggable(section) {
    var head = section.querySelector('.groupHead');
    if (!head) { return; }
    head.setAttribute('draggable', 'true');
    head.addEventListener('dragstart', function (ev) {
      state.dragging = true;
      state.dragEl = section;
      section.classList.add('dragging');
      try {
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', section.getAttribute('data-group') || '');
      } catch (e) {}
    });
    head.addEventListener('dragend', function () {
      section.classList.remove('dragging');
      state.dragging = false;
      state.dragEl = null;
      // modified: 2.4.2 搜索/分组筛选下 DOM 里只有部分分组，照 DOM 顺序落盘会把
      // 没显示的分组从 group_order 里挤掉（orderedGroups 会把它们甩到末尾）。
      // 此时放弃本次拖动，提示清筛选后再拖。
      if (state.keyword || state.group !== '全部') {
        toast('搜索/筛选中只显示了部分分组，本次顺序未保存。请切回「全部」并清空搜索后拖一次', 'warn');
        render();
        return;
      }
      // 松手时按当前 DOM 顺序落盘：拖到一半的中间态就是最终结果，
      // 不存在"预览与保存不一致"的问题。
      var order = Array.prototype.map.call(
        el.content.querySelectorAll('section.group'),
        function (node) { return node.getAttribute('data-group'); }
      ).filter(function (name) { return !!name; });
      saveGroupOrder(order);
      render();
    });
  }

  function renderHiddenBar() {
    var box = document.createElement('div');
    box.className = 'hiddenBar';
    var parts = ['<span class="hiddenLabel">已隐藏 ' + state.hiddenIds.length + ' 项</span>'];
    state.hiddenIds.forEach(function (id) {
      var svc = null;
      for (var i = 0; i < state.services.length; i++) {
        if (state.services[i].id === id) { svc = state.services[i]; break; }
      }
      parts.push('<button type="button" class="hiddenChip" data-show="' + esc(id) + '">' +
        esc(svc ? svc.name : id) + '<span>恢复</span></button>');
    });
    box.innerHTML = parts.join('');
    Array.prototype.forEach.call(box.querySelectorAll('[data-show]'), function (btn) {
      btn.addEventListener('click', function () { setHidden(btn.getAttribute('data-show'), false); });
    });
    el.content.appendChild(box);
  }

  // ================================================================ modified: 2.5.0 分组管理
  // 新建 / 改名 / 删除分组。全部只动 settings.json（group_order / service_order /
  // service_group），services.json 一个字节都不碰——它描述"服务是什么"，
  // 这里改的是"我怎么看它"。改名后原始分组名仍留在 services.json，想要可以改回来。

  function renderNewGroupBox() {
    var box = document.createElement('div');
    box.className = 'newGroupBox';
    box.innerHTML = '<button type="button" class="btn ghost" id="btnAddGroup">＋ 新建分组</button>';
    box.querySelector('button').addEventListener('click', addGroup);
    el.content.appendChild(box);
  }

  function inputDialog(title, hint, defaultValue, onOk) {
    modalOnClose = null;   // modified: 2.6.3 别把上一次确认框的取消钩子带进来
    el.modalTitle.textContent = title;
    el.modalBody.innerHTML =
      (hint ? '<p class="setHint" style="margin:0 0 10px">' + esc(hint) + '</p>' : '') +
      '<input type="text" class="inputDlg" id="inputDlgValue" maxlength="64"' +
      ' value="' + esc(defaultValue || '') + '">';
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.addEventListener('click', closeModal);
    var ok = document.createElement('button');
    ok.type = 'button';
    ok.className = 'btn primary';
    ok.textContent = '确定';
    ok.addEventListener('click', function () {
      var val = document.getElementById('inputDlgValue').value;
      closeModal();
      onOk(val);
    });
    el.modalFoot.appendChild(cancel);
    el.modalFoot.appendChild(ok);
    el.modalMask.hidden = false;
    var input = document.getElementById('inputDlgValue');
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); ok.click(); }
    });
    input.focus();
    input.select();
  }

  function allGroupNames() {
    // modified: 2.6.3 普通对象的原型链键（constructor / toString /
    // __proto__）会被误判成"已存在同名分组"，改用无原型对象。
    var names = Object.create(null);
    state.groupOrder.forEach(function (n) { names[n] = true; });
    state.services.forEach(function (s) { names[groupOf(s)] = true; });
    return names;
  }

  function addGroup() {
    inputDialog('新建分组', '输入分组名称（最多 64 字）', '', function (raw) {
      var name = String(raw || '').trim();
      if (!name) { toast('分组名不能为空', 'warn'); return; }
      if (name.length > 64) { toast('分组名太长（最多 64 字）', 'warn'); return; }
      if (Object.prototype.hasOwnProperty.call(allGroupNames(), name)) {
        toast('已经存在同名分组「' + name + '」', 'warn'); return;
      }
      // modified: 新组排到当前显示顺序的末尾。
      // 直接 push(groupOrder) 会让它挤在"已登记组"的最前面（未登记组排在登记组之后），
      // 与"拖到最后"的手感不一致；把完整顺序+新名固化下来，落盘语义与拖动完全等价。
      // modified: 2.6.3 原来直接用 orderedGroups() 覆盖 groupOrder。
      // 那个函数只从"有可见服务"的组里取名，连续新建两个空组时，
      // 第一个空组会被丢掉并写进 settings.json，刷新后找不回来。
      var full = state.groupOrder.slice();
      orderedGroups().forEach(function (n) {
        if (full.indexOf(n) < 0) { full.push(n); }
      });
      full.push(name);
      state.groupOrder = full;
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({ group_order: state.groupOrder },
        '已新建分组「' + name + '」，把卡片拖进来即可');
    });
  }

  function renameGroup(oldName) {
    inputDialog('重命名分组', '给「' + oldName + '」起个新名字', oldName, function (raw) {
      var next = String(raw || '').trim();
      if (!next) { toast('分组名不能为空', 'warn'); return; }
      if (next.length > 64) { toast('分组名太长（最多 64 字）', 'warn'); return; }
      if (next === oldName) { return; }
      var names = allGroupNames();
      delete names[oldName];
      if (Object.prototype.hasOwnProperty.call(names, next)) {
        toast('已经存在同名分组「' + next + '」', 'warn'); return;
      }
      // 三处联动：显示顺序 / 组内顺序 / 卡片归属覆盖
      state.groupOrder = state.groupOrder.map(function (n) { return n === oldName ? next : n; });
      // modified: 2.6.3 只搬自有记录，别把原型链上的函数当成"组内顺序"
      if (Object.prototype.hasOwnProperty.call(state.serviceOrder, oldName)) {
        state.serviceOrder[next] = state.serviceOrder[oldName];
        delete state.serviceOrder[oldName];
      }
      Object.keys(state.serviceGroup).forEach(function (id) {
        if (state.serviceGroup[id] === oldName) { state.serviceGroup[id] = next; }
      });
      // 出厂分组名就是 oldName 的服务也要写覆盖，否则显示会分裂成新旧两个组
      state.services.forEach(function (s) {
        if (s.group === oldName && !state.serviceGroup[s.id]) { state.serviceGroup[s.id] = next; }
      });
      // 折叠状态跟着迁移
      try {
        if (localStorage.getItem('hub.collapse.' + oldName) === '1') {
          localStorage.setItem('hub.collapse.' + next, '1');
        }
        localStorage.removeItem('hub.collapse.' + oldName);
      } catch (e) {}
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({
        group_order: state.groupOrder,
        service_order: state.serviceOrder,
        service_group: state.serviceGroup
      }, '分组已改名为「' + next + '」');
    });
  }

  function deleteGroup(name) {
    // 出厂分组（services.json 里有服务的原始 group 就是它）不能删：
    // 覆盖删掉后这些服务又会把这个组"长"出来，删了等于没删。
    var natives = state.services.filter(function (s) { return s.group === name; });
    if (natives.length) {
      toast('「' + name + '」是 ' + natives.length + ' 个服务的原始分组，不能删除。' +
        '可以点「改名」，或把这些服务拖到别的组', 'warn');
      return;
    }
    var members = state.services.filter(function (s) { return groupOf(s) === name; });
    confirmDialog(members.length
      ? '删除分组「' + name + '」？组内 ' + members.length + ' 项将回到原来分组。'
      : '删除空分组「' + name + '」？', function () {
      // 组内卡片删掉归属覆盖 → 回 services.json 原分组
      Object.keys(state.serviceGroup).forEach(function (id) {
        if (state.serviceGroup[id] === name) { delete state.serviceGroup[id]; }
      });
      state.groupOrder = state.groupOrder.filter(function (n) { return n !== name; });
      delete state.serviceOrder[name];
      setGroupCollapsed(name, false);
      lastPrint = '';
      chipsSig = '';
      render();
      saveSettings({
        group_order: state.groupOrder,
        service_order: state.serviceOrder,
        service_group: state.serviceGroup
      }, '已删除分组「' + name + '」');
    });
  }
  // ================================================================ 分组管理 end

  function renderContent(list) {
    el.content.innerHTML = '';
    if (!state.services.length) {
      el.content.innerHTML = '<div class="empty">还没有登记任何服务，去 services.json 里加一条就行。</div>';
      return;
    }
    if (!list.length) {
      el.content.innerHTML = '<div class="empty">' +
        (state.hiddenIds.length && !state.keyword
          ? '当前可见的服务都被隐藏了。点顶栏「布局」→ 底部可恢复。'
          : '没有匹配的服务。') + '</div>';
      // modified: 2.6.0 空态也要给恢复入口。原来在这里直接 return，
      // 全部卡片被隐藏时连「已隐藏 N 项」恢复栏都不渲染——首页没有任何办法把卡找回来，
      // 只能去设置里翻（用户以为数据丢了）。编辑模式下照常渲染恢复栏与新建分组入口。
      if (state.editMode && state.hiddenIds.length) { renderHiddenBar(); }
      if (state.editMode) { renderNewGroupBox(); }
      return;
    }
    if (state.view === 'list') {
      renderList(list);
      return;
    }

    // modified: 2.6.3 组名是任意字符串；普通对象里 byGroup['constructor']
    // 会拿到 Object 构造函数，items.filter 抛异常、整页白屏。改无原型字典。
    var byGroup = Object.create(null);
    var present = [];
    list.forEach(function (svc) {
      // modified: 用 groupOf() 而不是 svc.group——拖动过的卡可能已被移到别的组
      var g = groupOf(svc);
      if (!byGroup[g]) { byGroup[g] = []; present.push(g); }
      byGroup[g].push(svc);
    });

    // modified: 分组顺序由 group_order 决定；搜索/筛选后只剩部分分组时，
    // 依然按用户拖出来的顺序排，不会因为过滤而跳回默认顺序。
    var order = [];
    // modified: 2.5.0 无筛选时显示空组（新建分组先是个空壳，等卡片拖进来）；
    // 搜索/筛选下不显示空组，免得一堆空壳占屏。
    var showEmpty = !state.keyword && state.group === '全部';
    state.groupOrder.forEach(function (name) {
      if ((byGroup[name] || showEmpty) && order.indexOf(name) < 0) { order.push(name); }
    });
    present.forEach(function (name) {
      if (order.indexOf(name) < 0) { order.push(name); }
    });

    order.forEach(function (name) {
      var items = orderedItems(name, byGroup[name] || []);   // modified: 空组给空数组
      var section = document.createElement('section');
      section.className = 'group' + (groupCollapsed(name) ? ' collapsed' : '');
      section.setAttribute('data-group', name);
      section.appendChild(buildGroupHead(name, items));
      if (!groupCollapsed(name)) {
        var grid = document.createElement('div');
        grid.className = 'grid';
        // modified: 不再挑主卡，全部等宽（一行 4 张）
        items.forEach(function (svc) { grid.appendChild(buildCard(svc, false)); });
        // modified: 2.5.0 编辑模式下组末尾放"拖到这里"占位，提示空位可以拖入
        if (state.editMode) {
          var slot = document.createElement('div');
          slot.className = 'dropSlot';
          slot.textContent = '+ 拖到这里';
          grid.appendChild(slot);
        }
        section.appendChild(grid);
      }
      if (state.editMode) { makeDraggable(section); }
      el.content.appendChild(section);
    });

    if (state.editMode && state.hiddenIds.length) { renderHiddenBar(); }
    if (state.editMode) { renderNewGroupBox(); }  // modified: 2.5.0 新建分组入口
  }

  // modified: 卡片布局的指纹。必须排序后再拼，否则对象键顺序一变就误判成"布局变了"，
  // 白白触发一次整块重建（页面会闪一下）。
  function layoutSignature() {
    var g = Object.keys(state.serviceGroup).sort().map(function (k) {
      return k + '=' + state.serviceGroup[k];
    }).join(';');
    var o = Object.keys(state.serviceOrder).sort().map(function (k) {
      var seq = state.serviceOrder[k];   // modified: 2.6.3 脏值不再让签名计算抛异常
      return k + ':' + (Array.isArray(seq) ? seq.join(',') : '');
    }).join(';');
    return g + '|' + o;
  }

  function fingerprint(list) {
    // modified: 2.6.3 两处一起改：
    // ① 补 portConflict——占用者换人时胶囊 title 原来永不更新（2.6.2 回归）；
    // ② 用 JSON.stringify 取代裸分隔符 join，服务名/分组名里带 | ~ # 时
    //    不同数据可能拼出同一个签名，卡片会漏重绘。
    var rows = list.map(function (s) {
      return JSON.stringify([s.id, s.state, s.portText, s.url, s.pid, s.version,
              s.updated, s.name, s.desc, s.group, s.kind, s.autostart,
              s.running, s.lastExit, s.missing, s.portConflict || null]);
    });
    // modified: 编辑模式 / 密度 / 隐藏 / 分组顺序 也计入指纹，
    // 否则改了这几项不会触发卡片区重建（它们不在服务数据里）。
    return [state.view, state.keyword, state.group, state.sort.key, state.sort.dir,
            state.editMode, state.density,
            state.hiddenIds.join(','), state.groupOrder.join(','),
            layoutSignature(),
            Object.keys(state.busy).join(','), JSON.stringify(rows)].join('#');
  }

  function updateUptimes() {
    var now = Date.now();
    Array.prototype.forEach.call(el.content.querySelectorAll('[data-uptime]'), function (node) {
      var base = parseInt(node.getAttribute('data-uptime'), 10) || 0;
      var at = parseInt(node.getAttribute('data-uptime-at'), 10) || now;
      var seconds = base + Math.max(0, (now - at) / 1000);
      node.textContent = humanUptime(seconds);
    });
  }

  function render() {
    renderChips();
    renderStats();
    // modified: 拖动进行中冻结重绘——每 2 秒的轮询重建 DOM 会把正在拖的节点抽走
    if (state.dragging) { return; }
    // modified: 2.6.3 离线提示改由 render 统一产出。原来 refresh 的 catch
    // 直接 el.content.innerHTML = ...，绕过 dragging 冻结（拖分组时会把
    // 顺序读成空数组），且随便一次交互重绘就把提示抹掉。
    if (state.offline) {
      var html = '<div class="offline">服务台后台没有响应（' + esc(state.offline) + '）。' +
        '双击桌面「服务台」重新唤起；已启动的业务服务不受影响。</div>';
      if (offlinePrinted !== html) { el.content.innerHTML = html; offlinePrinted = html; }
      lastPrint = '';
      return;
    }
    offlinePrinted = '';
    var list = visibleServices().filter(matches);
    var fp = fingerprint(list);
    if (fp !== lastPrint) {
      lastPrint = fp;
      renderContent(list);
    } else {
      updateUptimes();
    }
  }

  function renderHub() {
    var hub = state.hub || {};
    if (!hub.port) { el.hubMeta.textContent = '正在连接…'; return; }
    var url = 'http://127.0.0.1:' + hub.port + '/';
    // modified: 2.5.0 地址做成可点击复制（data-copy），其余信息照旧拼
    var bits = [
      '<span class="copyable" data-copy="' + esc(url) + '" title="点击复制服务台地址">' +
        esc('127.0.0.1:' + hub.port) + '</span>',
      'v' + esc(hub.version || '?'),
      'Python ' + esc(hub.python),
      '已运行 ' + esc(humanUptime(hub.uptime) || '刚刚')
    ];
    if (hub.tray) { bits.push('托盘常驻'); }
    if (!hub.psutil) { bits.push('psutil 缺失'); }
    // modified: 2.6.3 前两次轮询失败也要有轻量提示，别等满 3 次才说
    if (state.failures) { bits.push('<span style="color:var(--warn)">后台连接中断，正在重试</span>'); }
    el.hubMeta.innerHTML = bits.join(' · ');
    el.hubMeta.title = '安装目录：' + (hub.appdir || '') + '\n日志目录：' + (hub.logdir || '');
  }

  // modified: 2.5.0 复制到剪贴板。优先 clipboard API（127.0.0.1 属安全上下文），
  // 旧内核/非安全上下文退回 textarea + execCommand。
  function fallbackCopy(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      return true;
    } catch (e) {
      return false;
    }
  }
  function copyText(text) {
    var done = function () { toast('已复制 ' + text, 'ok'); };
    var fail = function () {
      if (fallbackCopy(text)) { done(); }
      else { toast('复制失败，请手动选择复制', 'error'); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fail);
    } else {
      fail();
    }
  }
  el.hubMeta.addEventListener('click', function (event) {
    var node = event.target.closest ? event.target.closest('[data-copy]') : null;
    if (node) { copyText(node.getAttribute('data-copy')); }
  });

  // ------------------------------------------------------------------ 轮询
  function refresh(quiet) {
    return get('/api/status').then(function (payload) {
      state.failures = 0;
      // modified: 2.6.3 恢复连接时强制重绘一次，别留着离线横幅
      if (state.offline) { state.offline = ''; offlinePrinted = ''; lastPrint = ''; }
      state.fetchedAt = Date.now();
      state.services = payload.services || [];
      state.hub = payload.hub || {};
      renderHub();
      render();
    }).catch(function (err) {
      state.failures += 1;
      renderHub();   // modified: 2.6.3 马上在顶栏标出连接中断
      if (state.failures >= 3) {
        state.offline = err.message || '未知错误';
        lastPrint = '';
        render();
      }
      if (!quiet) { toast('读取状态失败：' + err.message, 'error'); }
    });
  }

  function schedule() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (!state.autoRefresh || !state.refreshMs) { return; }
    state.timer = setTimeout(function () {
      if (!document.hidden) {
        refresh(true).then(schedule);
      } else {
        schedule();
      }
    }, state.refreshMs);
  }

  // ------------------------------------------------------------------ 动作
  function setBusy(id, flag) {
    if (flag) { state.busy[id] = true; } else { delete state.busy[id]; }
    lastPrint = '';
  }

  function doAction(id, action, force) {
    var awaitingConfirm = false;   // modified: 2.6.3
    setBusy(id, true);
    render();
    return post('/api/action', { id: id, action: action, force: !!force }).then(function (result) {
      if (result.needConfirm) {
        // modified: 2.6.3 原来这里 return 之后末尾 .then 照样把 busy 解掉，
        // 确认框还开着时按钮已可重复点；两个确认交错还会串台。
        awaitingConfirm = true;
        confirmDialog(result.message, function () {
          setBusy(id, false);
          doAction(id, action, true);
        }, function () {
          setBusy(id, false);
          render();
        });
        return;
      }
      toast(result.message || '已执行', result.ok ? 'ok' : 'warn');
      return delayedRefresh();
    }).catch(function (err) {
      toast('操作失败：' + err.message, 'error');
    }).then(function () {
      if (awaitingConfirm) { return; }   // modified: 等用户确认，别提前解锁
      setBusy(id, false);
      render();
    });
  }

  function batched(action, title) {
    // modified: 2.6.3 顶栏「全部启动/停止」连点会并行发起两轮批量操作
    if (state.batchAll) { toast('还有批量操作在进行中', 'warn'); return; }
    state.batchAll = true;
    confirmDialog('确定要' + title + '吗？', function () {
      toast(title + '中…');
      post('/api/batch', { action: action }).then(function (result) {
        toast(result.message || '完成', result.ok ? 'ok' : 'warn');
        delayedRefresh();
      }).catch(function (err) { toast('批量操作失败：' + err.message, 'error');
      }).then(function () { state.batchAll = false; });
    }, function () { state.batchAll = false; });
  }

  function delayedRefresh() {
    return new Promise(function (resolve) {
      setTimeout(function () {
        refresh(true).then(function () {
          setTimeout(function () { refresh(true).then(resolve); }, 900);
        });
      }, 400);
    });
  }

  // ------------------------------------------------------------------ 弹窗
  function closeModal() {
    el.modalMask.hidden = true;
    // modified: 2.6.3 取消确认框（含 ESC、点遮罩、右上角 ×）时也要清理，
    // 否则 doAction 为等确认保留的 busy 会永远禁用按钮。
    var fn = modalOnClose;
    modalOnClose = null;
    if (fn) { fn(); }
  }

  function confirmDialog(message, onYes, onNo) {
    modalOnClose = onNo || null;
    el.modalTitle.textContent = '请确认';
    el.modalBody.textContent = message;
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'btn danger';
    yes.textContent = '确定';
    yes.addEventListener('click', function () {
      modalOnClose = null;   // modified: 点"确定"不算取消
      closeModal();
      onYes();
    });
    var no = document.createElement('button');
    no.type = 'button';
    no.className = 'btn';
    no.textContent = '取消';
    no.addEventListener('click', closeModal);
    el.modalFoot.appendChild(no);
    el.modalFoot.appendChild(yes);
    el.modalMask.hidden = false;
  }

  function logDialog(id) {
    modalOnClose = null;   // modified: 2.6.3
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var again = document.createElement('button');
    again.type = 'button';
    again.className = 'btn';
    again.textContent = '刷新日志';
    again.addEventListener('click', function () { loadLog(id); });
    el.modalFoot.appendChild(again);
    el.modalTitle.textContent = '日志 · ' + (id === '__hub__' ? '服务台' : id);
    el.modalMask.hidden = false;
    loadLog(id);
  }

  function loadLog(id) {
    el.modalBody.innerHTML = '<div>读取中…</div>';
    get('/api/logs?id=' + encodeURIComponent(id) + '&lines=300').then(function (payload) {
      if (!payload.ok) {
        el.modalBody.innerHTML = '<div>' + esc(payload.message) + '</div>';
        return;
      }
      var text = (payload.lines || []).join('\n') || '（空）';
      el.modalBody.innerHTML = '<pre>' + esc(text) + '</pre>';
      el.modalBody.scrollTop = el.modalBody.scrollHeight;
    }).catch(function (err) {
      el.modalBody.innerHTML = '<div>读取失败：' + esc(err.message) + '</div>';
    });
  }

  // ------------------------------------------------------------------ 设置面板
  function settingsGroup(title, rows) {
    var box = document.createElement('section');
    box.className = 'setGroup';
    var h = document.createElement('h3');
    h.className = 'setGroupTitle';
    h.textContent = title;
    box.appendChild(h);
    rows.forEach(function (row) { box.appendChild(row); });
    return box;
  }

  function settingsRow(label, hint, ctrl) {
    var row = document.createElement('div');
    row.className = 'setRow';
    var main = document.createElement('div');
    main.className = 'setMain';
    var name = document.createElement('div');
    name.className = 'setLabel';
    name.textContent = label;
    main.appendChild(name);
    if (hint) {
      var tip = document.createElement('div');
      tip.className = 'setHint';
      tip.textContent = hint;
      main.appendChild(tip);
    }
    var right = document.createElement('div');
    right.className = 'setCtrl';
    right.appendChild(ctrl);
    row.appendChild(main);
    row.appendChild(right);
    return row;
  }

  function selectCtrl(options, value, onChange) {
    var select = document.createElement('select');
    // modified: 服务端可能存着选项表里没有的值（如手动改成 3000ms），补一个临时项，避免显示错位
    var known = options.some(function (opt) { return String(opt[0]) === String(value); });
    if (!known && value !== undefined && value !== null && value !== '') {
      options = options.concat([[String(value), String(value) + ' ms']]);
    }
    options.forEach(function (opt) {
      var option = document.createElement('option');
      option.value = opt[0];
      option.textContent = opt[1];
      if (String(opt[0]) === String(value)) { option.selected = true; }
      select.appendChild(option);
    });
    select.addEventListener('change', function () { onChange(select.value); });
    return select;
  }

  function switchCtrl(checked, onChange) {
    var label = document.createElement('label');
    label.className = 'switch';
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!checked;
    input.addEventListener('change', function () { onChange(input.checked); });
    var track = document.createElement('span');
    track.className = 'track';
    label.appendChild(input);
    label.appendChild(track);
    return label;
  }

  // modified: 2.6.0 服务端设置 → 前端状态的统一同步口。
  // 原来这段逻辑写在 init 的 get('/api/settings') 里，新增「从备份恢复」时又需要一遍；
  // 抽出来共用，以后加设置项只改这一处，不会出现"恢复时漏同步某项"。
  function adoptSettings(cfg, options) {
    options = options || {};
    state.settings = cfg || {};
    if (typeof state.settings.refresh_ms === 'number') { state.refreshMs = state.settings.refresh_ms; }
    if (state.settings.theme && !options.keepTheme) { applyTheme(state.settings.theme, true); }
    if (state.settings.view && !options.keepView) { setView(state.settings.view, true); }
    applyBgMotion(state.settings.bg_motion);
    // 首页布局偏好：数组/对象都做类型兜底，手改坏的 settings.json 不至于让首页白屏
    state.hiddenIds = Array.isArray(state.settings.hidden_ids) ? state.settings.hidden_ids : [];
    state.groupOrder = Array.isArray(state.settings.group_order) ? state.settings.group_order : [];
    var sg = state.settings.service_group;
    state.serviceGroup = noProto(sg);   // modified: 2.6.3
    var so = state.settings.service_order;
    state.serviceOrder = noProto(so);   // modified: 2.6.3
    applyDensity(state.settings.density || 'comfortable');
    chipsSig = '';
    lastPrint = '';
    render();
  }

  // ================================================================ modified: 2.6.0 设置备份 / 恢复
  // 布局（分组顺序、卡片归属、隐藏名单）只存在 runtime\settings.json，
  // 一次误操作或自动化测试就可能被清掉（2.5.0 验收脚本就清过一次，靠手动备份救回）。
  // 参考 Homepage / Homarr 的"配置可导出备份"做法：导出 JSON 下载，导入先给摘要再确认。
  // 不新增后端接口：导入仍走 /api/settings，服务端 settings 层有白名单与类型校验。
  var IMPORT_KEYS = ['auto_open', 'browser', 'browser_path', 'theme', 'view', 'refresh_ms',
                     'bg_motion', 'hidden_ids', 'group_order', 'density',
                     'service_group', 'service_order'];

  function exportSettings() {
    var payload = {
      app: 'ServiceHub',
      version: (state.hub || {}).version || '',
      saved_at: new Date().toISOString(),
      settings: state.settings || {}
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    var now = new Date();
    a.href = URL.createObjectURL(blob);
    a.download = 'servicehub-settings-' + now.getFullYear() + pad(now.getMonth() + 1) +
      pad(now.getDate()) + '-' + pad(now.getHours()) + pad(now.getMinutes()) + '.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    toast('已导出备份（含首页布局），在浏览器下载目录里', 'ok');
  }

  function importSettings(file) {
    if (!file) { return; }
    // modified: 2.6.3 几百 MB 的文件会把主线程堵死，设置备份正常只有几 KB
    if (file.size > 1024 * 1024) {
      toast('备份文件超过 1MB，已拒绝读取', 'error');
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var parsed;
      try { parsed = JSON.parse(String(reader.result || '')); }
      catch (e) { toast('这个文件不是合法的 JSON，无法恢复', 'error'); return; }
      // 既接受本工具导出的 {app, settings:{...}}，也接受直接拖进来的 settings.json
      var src = (parsed && parsed.settings && typeof parsed.settings === 'object')
        ? parsed.settings : parsed;
      if (!src || typeof src !== 'object' || Array.isArray(src)) {
        toast('文件里没有可识别的设置内容', 'error'); return;
      }
      var patch = {};
      IMPORT_KEYS.forEach(function (key) { if (key in src) { patch[key] = src[key]; } });
      if (!Object.keys(patch).length) {
        toast('文件里没有服务台认识的设置项', 'error'); return;
      }
      // modified: 2.6.3 类型不对的字段会让摘要算出"undefined 组"，确认后
      // 还把坏值 POST 给后端。前端先挡一道，服务端 normalize 仍是最后防线。
      var badTypes = [];
      ['hidden_ids', 'group_order'].forEach(function (k) {
        if (k in patch && !Array.isArray(patch[k])) { badTypes.push(k); }
      });
      ['service_group', 'service_order'].forEach(function (k) {
        if (k in patch && (!patch[k] || typeof patch[k] !== 'object' ||
                           Array.isArray(patch[k]))) { badTypes.push(k); }
      });
      if (badTypes.length) {
        toast('备份里这些字段类型不对，已拒绝恢复：' + badTypes.join('、'), 'error');
        return;
      }
      var brief = '将从备份恢复：主题 ' + (patch.theme || '保持') +
        '、视图 ' + (patch.view || '保持') +
        '、密度 ' + (patch.density || '保持') +
        '；隐藏 ' + ((patch.hidden_ids || []).length || 0) + ' 项' +
        '、分组顺序 ' + ((patch.group_order || []).length || 0) + ' 组' +
        '、卡片归属 ' + Object.keys(patch.service_group || {}).length + ' 条。' +
        '当前布局会被覆盖，确定继续吗？';
      confirmDialog(brief, function () {
        saveSettings(patch, null).then(function (res) {
          // 成功：用服务端 normalize 后的值同步；失败：回滚界面到旧设置（saveSettings 已弹错误）
          adoptSettings(state.settings, {});
          if (!res) { return; }
          closeModal();
          toast('已从备份恢复设置与首页布局', 'ok');
        });
      });
    };
    reader.onerror = function () { toast('读取文件失败', 'error'); };
    reader.readAsText(file, 'utf-8');
  }

  // modified: 2.6.3 布局保存失败时从服务端重新拉回真实设置，只同步布局字段
  // （不动主题/视图，免得打断正在进行的编辑），让界面与磁盘保持一致。
  function syncLayoutFromServer() {
    return get('/api/settings').then(function (payload) {
      var cfg = payload.settings || {};
      state.settings = cfg;
      state.hiddenIds = Array.isArray(cfg.hidden_ids) ? cfg.hidden_ids : [];
      state.groupOrder = Array.isArray(cfg.group_order) ? cfg.group_order : [];
      var sg = cfg.service_group;
      state.serviceGroup = noProto(sg);   // modified: 2.6.3
      var so = cfg.service_order;
      state.serviceOrder = noProto(so);   // modified: 2.6.3
      chipsSig = '';
      lastPrint = '';
      render();
    }).catch(function () {});
  }

  function saveSettings(patch, note) {
    return post('/api/settings', patch).then(function (payload) {
      state.settings = payload.settings || state.settings;
      if (payload.browserResolved) { state.browserResolved = payload.browserResolved; }  // modified: 2.6.1
      if (typeof state.settings.refresh_ms === 'number') {
        state.refreshMs = state.settings.refresh_ms;
      }
      if (note) { toast(note, 'ok'); }
      schedule();
      return payload;
    }).catch(function (err) {
      toast('保存失败：' + err.message, 'error');
      syncLayoutFromServer();   // modified: 2.6.3 界面别和磁盘分叉
    });
  }

  function openSettings() {
    // modified: 2.6.3 首屏 /api/settings 还没回来时点开设置，面板里全是
    // 默认值，用户据此改动会误改配置。先等设置就绪再渲染面板。
    if (state.settingsReady) { openSettingsPanel(); return; }
    get('/api/settings').then(function (payload) {
      state.browsers = payload.browsers || [];
      state.browserResolved = payload.browserResolved || null;
      state.autostart = payload.autostart || {};
      // modified: 只备齐面板要读的数据，不调 adoptSettings——那会顺带改
      // 主题/视图，把 URL 里的 ?theme= / ?view= 覆盖掉。
      state.settings = payload.settings || {};
      state.settingsReady = true;
    }).catch(function () {}).then(function () { openSettingsPanel(); });
  }

  function openSettingsPanel() {
    modalOnClose = null;   // modified: 2.6.3
    el.modalTitle.textContent = '设置';
    el.modalBody.innerHTML = '';
    el.modalFoot.hidden = true;

    var cfg = state.settings || {};
    var footer = document.createElement('div');

    // ---- 外观
    var themeRow = settingsRow('主题', '跟随系统会随 Windows 明暗模式自动切换', selectCtrl([
      ['system', '跟随系统'], ['light', '明亮'], ['dark', '暗色']
    ], cfg.theme || 'system', function (value) {
      applyTheme(value, true);
      saveSettings({ theme: value }, '主题已切换为' + (value === 'system' ? '跟随系统' : value === 'dark' ? '暗色' : '明亮'));
    }));

    var viewRow = settingsRow('默认视图', '列表视图每行一项，点表头可按列排序', selectCtrl([
      ['card', '卡片'], ['list', '列表']
    ], cfg.view || state.view, function (value) {
      setView(value, true);
      saveSettings({ view: value }, '默认视图已改为' + (value === 'list' ? '列表' : '卡片'));
    }));

    var refreshRow = settingsRow('自动刷新', '状态由后台每秒采集，这里只管网页多久取一次', selectCtrl([
      ['1000', '每 1 秒'], ['2000', '每 2 秒'], ['5000', '每 5 秒'], ['10000', '每 10 秒'], ['off', '暂停']
    ], state.autoRefresh ? String(state.refreshMs) : 'off', function (value) {
      if (value === 'off') {
        state.autoRefresh = false;
        try { localStorage.setItem('hub.autoRefresh', '0'); } catch (e) {}
        schedule();
        toast('已暂停自动刷新', 'ok');
        return;
      }
      state.autoRefresh = true;
      state.refreshMs = parseInt(value, 10);
      try { localStorage.setItem('hub.autoRefresh', '1'); } catch (e) {}
      saveSettings({ refresh_ms: state.refreshMs }, '刷新间隔已更新');
    }));

    // modified: 背景动效开关（默认关，见 style.css 的 html.bg-motion）
    var bgMotionRow = settingsRow('背景动效',
      '三团光斑缓慢漂移。部分浏览器 + 集显组合下持续运动会整屏闪烁，默认关闭；关掉后页面彻底静止',
      switchCtrl(!!cfg.bg_motion, function (checked) {
        applyBgMotion(checked);
        saveSettings({ bg_motion: checked }, '背景动效已' + (checked ? '打开' : '关闭'));
      }));

    // ---- 行为
    var autoOpenRow = settingsRow('启动服务后自动打开网页',
      '只对带网页入口的服务生效；动态端口服务会等端口出来再打开',
      switchCtrl(cfg.auto_open, function (checked) {
        saveSettings({ auto_open: checked }, '已保存自动开网页 = ' + (checked ? '开' : '关'));
      }));

    // modified: 开机自动启动（写 HKCU Run，注册表才是真相，所以不进 settings.json）
    var au = state.autostart || {};
    var auHint = '写当前用户的注册表启动项（HKCU\\...\\Run），不需要管理员权限；'
      + '开机只让托盘出现，不会自动弹浏览器';
    if (au.supported === false) { auHint = '只有 Windows 才支持开机自启'; }
    if (au.stale) { auHint += '。⚠ 注册表里登记的还是旧路径（程序目录挪过？），重新打开一次即可更新'; }
    var auSwitch = null;
    auSwitch = switchCtrl(!!au.enabled, function (checked) {
      post('/api/settings', { autostart: checked }).then(function (res) {
        state.autostart = res.autostart || {};
        if (res.settings) { state.settings = res.settings; }
        toast(res.message || (checked ? '已设为开机自启' : '已取消开机自启'), 'ok');
      }).catch(function (err) {
        toast('设置失败：' + err.message, 'error');
        var box = auSwitch && auSwitch.querySelector('input');  // 失败要把开关拨回去
        if (box) { box.checked = !checked; }
      });
    });
    var autostartRow = settingsRow('开机自动启动', auHint, auSwitch);

    var browserOptions = [['auto', '系统默认浏览器']];
    (state.browsers || []).forEach(function (b) {
      browserOptions.push([b.id, b.name + '（' + b.id + '）']);
    });
    browserOptions.push(['custom', '自定义路径…']);

    // modified: 2.6.1 记住下拉框「当前选中的值」。原来测试按钮读的是 state.settings，
    // 而改设置是异步落盘的——刚选完 ego 就点测试，可能还在用旧的 auto，
    // 结果又开了 Edge，看起来就像设置没生效。
    var pickedBrowser = cfg.browser || 'auto';
    var pickedPath = cfg.browser_path || '';

    var resolvedNote = document.createElement('div');
    resolvedNote.className = 'setResolved';
    function paintResolved(info) {
      info = info || state.browserResolved || null;
      if (!info) {
        resolvedNote.className = 'setResolved';
        resolvedNote.textContent = '当前设置：' + browserLabel();
        return;
      }
      if (info.reason) {
        resolvedNote.className = 'setResolved warn';
        resolvedNote.textContent = '⚠ ' + info.reason + '——点「打开网页」会退回系统默认浏览器（本机是 Edge）。';
        return;
      }
      resolvedNote.className = 'setResolved';
      if (info.mode === 'auto') {
        resolvedNote.textContent = '实际使用：系统默认浏览器（本机是 Edge）';
      } else {
        resolvedNote.textContent = '实际使用：' + (info.name || '') +
          (info.path ? '　' + info.path : '');
        resolvedNote.title = info.path || '';
      }
    }

    var browserRow = settingsRow('打开网页用哪个浏览器',
      '服务台的「打开网页」和托盘入口都走这里选定的浏览器',
      selectCtrl(browserOptions, pickedBrowser, function (value) {
        pickedBrowser = value;
        pathRow.style.display = value === 'custom' ? '' : 'none';
        saveSettings({ browser: value }, '浏览器已设为 ' + value).then(function (payload) {
          paintResolved(payload && payload.browserResolved);   // modified: 用后端算出的实际结果回填
        });
      }));
    browserRow.querySelector('.setMain').appendChild(resolvedNote);
    paintResolved();

    var pathInput = document.createElement('input');
    pathInput.type = 'text';
    pathInput.placeholder = '例如 C:\\Program Files\\...\\chrome.exe';
    pathInput.value = cfg.browser_path || '';
    var pathRow = settingsRow('自定义浏览器路径', '仅当上面选「自定义路径」时使用；可直接粘贴 exe 完整路径', pathInput);
    pathRow.style.display = (cfg.browser || 'auto') === 'custom' ? '' : 'none';
    pathInput.addEventListener('change', function () {
      pickedPath = pathInput.value.trim();
      pickedBrowser = 'custom';
      saveSettings({ browser_path: pickedPath, browser: 'custom' }, '自定义浏览器路径已保存')
        .then(function (payload) { paintResolved(payload && payload.browserResolved); });
    });

    var testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'btn';
    testBtn.textContent = '用这个浏览器打开服务台';
    testBtn.addEventListener('click', function () {
      // modified: 2.6.1 用下拉框当前值，并显示实际用了哪个浏览器（含退回提示）
      post('/api/browser/test', {
        browser: pickedBrowser,
        browser_path: pickedPath
      }).then(function (res) {
        toast(res.message || '已尝试打开', res.ok && !res.fallback ? 'ok' : 'warn');
        if (res.fallback) {
          // 后端明确说了「退回系统默认」，就直接在设置里留一条红字，别让用户下次再猜
          resolvedNote.className = 'setResolved warn';
          resolvedNote.textContent = '⚠ 刚才那次没有用上指定浏览器，已退回系统默认（Edge）。';
        }
      }).catch(function (err) { toast('打开失败：' + err.message, 'error'); });
    });
    footer.appendChild(testBtn);

    // ---- 关于
    var hub = state.hub || {};
    var about = document.createElement('div');
    about.className = 'aboutGrid';
    about.innerHTML =
      '<div>服务台版本<b>v' + esc(hub.version || '—') + '</b></div>' +
      '<div>构建日期<b>' + esc(hub.build || '—') + '</b></div>' +
      '<div>监听端口<b>' + esc(hub.port || '—') + '</b></div>' +
      '<div>Python<b>' + esc(hub.python || '—') + '</b></div>' +
      '<div>启动于<b>' + esc(hub.started_at || '—') + '</b></div>' +
      '<div>服务 / 运行中<b>' + esc(hub.total || 0) + ' / ' + esc(hub.running || 0) + '</b></div>' +
      '<div>扫描耗时<b>' + esc(hub.cost_ms || 0) + ' ms</b></div>' +
      '<div>托盘<b>' + (hub.tray ? '常驻' : '未启用') + '</b></div>' +
      '<div class="pathLine">程序目录：' + esc(hub.appdir || '—') + '</div>' +
      '<div class="pathLine">日志目录：' + esc(hub.logdir || '—') + '</div>';

    var aboutBox = document.createElement('section');
    aboutBox.className = 'setGroup';
    var aboutTitle = document.createElement('h3');
    aboutTitle.className = 'setGroupTitle';
    aboutTitle.textContent = '关于';
    aboutBox.appendChild(aboutTitle);
    aboutBox.appendChild(about);

    var hubLogBtn = document.createElement('button');
    hubLogBtn.type = 'button';
    hubLogBtn.className = 'btn';
    hubLogBtn.textContent = '查看服务台日志';
    hubLogBtn.addEventListener('click', function () { logDialog('__hub__'); });
    var reloadBtn = document.createElement('button');
    reloadBtn.type = 'button';
    reloadBtn.className = 'btn';
    reloadBtn.textContent = '重读 services.json';
    reloadBtn.addEventListener('click', function () {
      post('/api/reload', {}).then(function (res) {
        toast(res.message || '已重读', 'ok');
        lastPrint = '';
        delayedRefresh();
      }).catch(function (err) { toast('重读失败：' + err.message, 'error'); });
    });
    var tools = document.createElement('div');
    tools.style.display = 'flex';
    tools.style.gap = '8px';
    tools.style.marginTop = '12px';
    tools.appendChild(hubLogBtn);
    tools.appendChild(reloadBtn);
    aboutBox.appendChild(tools);

    // ---- 首页布局（2.4.0 新增，2.4.1 扩到卡片拖拽）
    // modified: 密度——两档都是等宽四列，区别只在信息多少与间距。
    var densityRow = settingsRow('卡片密度',
      '舒适：说明两行、间距宽松；紧凑：说明一行、间距更小。两档都是等宽四列，版本号都在按钮行右端',
      selectCtrl([
        ['comfortable', '舒适'], ['compact', '紧凑']
      ], cfg.density || state.density, function (value) {
        applyDensity(value);
        lastPrint = '';
        render();
        saveSettings({ density: value }, '密度已改为' + (value === 'compact' ? '紧凑' : '舒适'));
      }));

    // modified: 已隐藏卡片的恢复入口 + 分组顺序复位。
    // 这两个是「布局编辑」的后悔药——隐藏了找不到、拖乱了想还原时都从这里走。
    var hiddenBox = document.createElement('div');
    hiddenBox.className = 'hiddenManage';
    function paintHidden() {
      hiddenBox.innerHTML = '';
      if (!state.hiddenIds.length) {
        hiddenBox.innerHTML = '<span class="hiddenNone">没有隐藏任何卡片</span>';
        return;
      }
      state.hiddenIds.forEach(function (id) {
        var svc = null;
        for (var i = 0; i < state.services.length; i++) {
          if (state.services[i].id === id) { svc = state.services[i]; break; }
        }
        var line = document.createElement('div');
        line.className = 'hiddenManageRow';
        var label = document.createElement('span');
        label.textContent = svc ? svc.name : id + '（已不在注册表）';
        var back = document.createElement('button');
        back.type = 'button';
        back.className = 'btn tiny';
        back.textContent = '恢复';
        back.addEventListener('click', function () { setHidden(id, false); paintHidden(); });
        line.appendChild(label);
        line.appendChild(back);
        hiddenBox.appendChild(line);
      });
    }
    paintHidden();
    var hiddenRow = settingsRow('首页隐藏的卡片',
      '在首页点「布局」后，卡片右上角会出现眼睛按钮；隐藏后到这里恢复', hiddenBox);

    var orderReset = document.createElement('button');
    orderReset.type = 'button';
    orderReset.className = 'btn tiny';
    orderReset.textContent = '恢复默认顺序';
    orderReset.addEventListener('click', function () {
      // modified: 2.4.1 一并清掉卡片的组内顺序和跨组归属，
      // 否则"恢复默认"只还原了分组，卡片还杵在拖过去的位置，看着像没生效。
      state.groupOrder = [];
      state.serviceOrder = Object.create(null);  // modified: 2.6.3 还原后仍保持无原型
      state.serviceGroup = Object.create(null);  // modified: 2.6.3
      chipsSig = '';
      lastPrint = '';
      render();
      saveSettings({
        group_order: [],
        service_order: {},
        service_group: {}
      }, '分组顺序、卡片顺序与分组归属都已还原为 services.json 里的样子');
      closeModal();
    });
    var orderRow = settingsRow('分组顺序与卡片位置',
      '在首页点「布局」后：拖卡片可换组内位置或拖到别的组；拖分组标题可换分组先后。'
      + '全部记在设置里，不动 services.json', orderReset);

    // modified: 2.6.0 备份 / 恢复入口（函数见上方 exportSettings / importSettings）
    var backupBox = document.createElement('div');
    backupBox.style.display = 'flex';
    backupBox.style.gap = '6px';
    var exportBtn = document.createElement('button');
    exportBtn.type = 'button';
    exportBtn.className = 'btn tiny';
    exportBtn.textContent = '导出备份';
    exportBtn.addEventListener('click', exportSettings);
    var importBtn = document.createElement('button');
    importBtn.type = 'button';
    importBtn.className = 'btn tiny';
    importBtn.textContent = '从备份恢复';
    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.json,application/json';
    fileInput.style.display = 'none';
    fileInput.addEventListener('change', function () {
      importSettings(fileInput.files && fileInput.files[0]);
      fileInput.value = '';   // modified: 清空才能连续选同一个文件再触发 change
    });
    importBtn.addEventListener('click', function () { fileInput.click(); });
    backupBox.appendChild(exportBtn);
    backupBox.appendChild(importBtn);
    backupBox.appendChild(fileInput);
    var backupRow = settingsRow('备份与恢复',
      '把当前设置与首页布局（分组顺序、卡片位置、隐藏名单）导出成 JSON；'
      + '布局被改乱、或换电脑时，从备份一键恢复（导入前会先给摘要再确认）', backupBox);

    el.modalBody.appendChild(settingsGroup('外观', [themeRow, viewRow, refreshRow, bgMotionRow]));
    el.modalBody.appendChild(settingsGroup('首页布局', [densityRow, orderRow, backupRow, hiddenRow]));
    el.modalBody.appendChild(settingsGroup('行为', [autostartRow, autoOpenRow, browserRow, pathRow]));
    el.modalBody.appendChild(aboutBox);

    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    el.modalFoot.appendChild(footer);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn primary';
    close.textContent = '完成';
    close.addEventListener('click', closeModal);
    el.modalFoot.appendChild(close);

    el.modalMask.hidden = false;
  }

  // ------------------------------------------------------------------ 事件
  el.content.addEventListener('click', function (event) {
    // modified: 编辑模式下的「眼睛」按钮——把卡片从首页隐藏（可恢复）
    var eyeBtn = event.target.closest('button[data-hide]');
    if (eyeBtn) {
      setHidden(eyeBtn.getAttribute('data-hide'), true);
      return;
    }
    var th = event.target.closest('th[data-sort]');
    if (th) {
      var key = th.getAttribute('data-sort');
      if (state.sort.key === key) {
        state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sort.key = key;
        state.sort.dir = 'asc';
      }
      try { localStorage.setItem('hub.sort', JSON.stringify(state.sort)); } catch (e) {}
      lastPrint = '';
      render();
      return;
    }
    var btn = event.target.closest('button[data-act]');
    if (!btn || btn.disabled) { return; }
    var id = btn.getAttribute('data-id');
    var act = btn.getAttribute('data-act');
    if (act === 'open') { doAction(id, 'open'); return; }
    if (act === 'log') { logDialog(id); return; }
    if (act === 'run') {
      // 有风险的一次性工具（清理磁盘、结束 Word 等）先弹确认再跑
      var target = null;
      for (var i = 0; i < state.services.length; i++) {
        if (state.services[i].id === id) { target = state.services[i]; break; }
      }
      if (target && target.confirm) {
        confirmDialog(target.confirm, function () { doAction(id, 'run'); });
        return;
      }
    }
    doAction(id, act);
  });

  // modified: 2.4.1 卡片拖拽（组内换序 + 跨组移动）。全部走事件委托，不给每张卡绑监听。
  // 拖动期间只更新「落点提示」，不改 DOM 顺序——松手才一次性改数据并重建，
  // 这样不会出现「拖到一半页面乱掉、松手又跳回去」。
  function clearDropHint() {
    Array.prototype.forEach.call(
      el.content.querySelectorAll('.drop-before, .drop-after, .drop-end'),
      function (node) { node.classList.remove('drop-before', 'drop-after', 'drop-end'); }
    );
  }

  function setDropHint(card, section, beforeId) {
    clearDropHint();
    if (card) { card.classList.add(beforeId ? 'drop-before' : 'drop-after'); }
    else if (section) { section.classList.add('drop-end'); }
  }

  // 同一组里某张卡的下一张卡（跨过 hidden/折叠时返回 null = 放到组末尾）
  function nextCardId(card) {
    var node = card.nextElementSibling;
    while (node) {
      if (node.classList && node.classList.contains('card') && node.getAttribute('data-id')) {
        return node.getAttribute('data-id');
      }
      node = node.nextElementSibling;
    }
    return null;
  }

  el.content.addEventListener('dragstart', function (event) {
    if (!state.editMode) { return; }
    var card = event.target.closest ? event.target.closest('.card[data-id]') : null;
    if (!card) { return; }              // 拖的是分组标题，交给 makeDraggable
    state.dragCardId = card.getAttribute('data-id');
    state.dragging = true;              // 冻结定时重绘，别把正在拖的节点换掉
    card.classList.add('cardDragging');
    try {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', state.dragCardId);
    } catch (e) {}
  });

  el.content.addEventListener('dragend', function (event) {
    var card = event.target.closest ? event.target.closest('.card[data-id]') : null;
    if (card) { card.classList.remove('cardDragging'); }
    if (state.dragCardId) {             // 没走到 drop（拖到窗口外等）= 取消，不改数据
      state.dragCardId = null;
      state.dropTarget = null;
      state.dragging = false;
      clearDropHint();
    }
  });

  // 分组拖动。只在编辑模式下有 draggable（见 makeDraggable），
  // 这里负责「跟着鼠标实时换位」，松手才落盘，所以预览和最终结果永远一致。
  el.content.addEventListener('dragover', function (event) {
    // modified: 卡片拖动优先——按左右半边判定插到目标卡的前面还是后面
    if (state.dragCardId) {
      var section = event.target.closest ? event.target.closest('section.group') : null;
      if (!section) {
        // modified: 2.6.3 移出有效落点（隐藏栏、新建分组按钮、空白间隙）时
        // 原来直接 return，竖线高亮和过期的 dropTarget 会一直留着。
        clearDropHint();
        state.dropTarget = null;
        return;
      }
      event.preventDefault();
      try { event.dataTransfer.dropEffect = 'move'; } catch (e) {}
      var overCard = event.target.closest('.card[data-id]');
      if (overCard && overCard.getAttribute('data-id') === state.dragCardId) {
        return;                          // 悬停在自己身上，什么都不做
      }
      var beforeId = null;
      if (overCard) {
        var box = overCard.getBoundingClientRect();
        var after = event.clientX > box.left + box.width / 2;
        beforeId = after ? nextCardId(overCard) : overCard.getAttribute('data-id');
      }
      setDropHint(overCard, section, beforeId);
      state.dropTarget = {
        group: section.getAttribute('data-group'),
        beforeId: beforeId
      };
      return;
    }

    if (!state.dragEl) { return; }
    event.preventDefault();
    try { event.dataTransfer.dropEffect = 'move'; } catch (e) {}
    var node = event.target;
    while (node && node !== el.content &&
           !(node.classList && node.classList.contains('group'))) {
      node = node.parentNode;
    }
    if (!node || node === el.content || node === state.dragEl) { return; }
    var rect = node.getBoundingClientRect();
    var after = event.clientY > rect.top + rect.height / 2;
    el.content.insertBefore(state.dragEl, after ? node.nextSibling : node);
  });

  el.content.addEventListener('drop', function (event) {
    if (state.dragCardId) {
      event.preventDefault();
      event.stopPropagation();
      var id = state.dragCardId;
      var target = state.dropTarget;
      state.dragCardId = null;
      state.dropTarget = null;
      state.dragging = false;
      clearDropHint();
      if (target && target.group) { moveCard(id, target.group, target.beforeId || null); }
      return;
    }
    if (state.dragEl) { event.preventDefault(); }   // 不 preventDefault 部分内核不会派发 dragend
  });

  var searchTimer = null;
  el.searchBox.addEventListener('input', function () {
    var value = el.searchBox.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {  // modified: 大列表逐字重绘会卡，防抖 120ms
      state.keyword = value;
      lastPrint = '';
      render();
    }, 120);
  });

  el.viewSeg.addEventListener('click', function (event) {
    var btn = event.target.closest('.segBtn');
    if (!btn) { return; }
    var view = btn.getAttribute('data-view');
    if (view === state.view) { return; }
    setView(view, true);
    saveSettings({ view: view }, '视图已切到' + (view === 'list' ? '列表' : '卡片'));
  });

  el.btnRefresh.addEventListener('click', function () { refresh().then(schedule); });
  el.btnStartAll.addEventListener('click', function () { batched('start-all', '启动全部常驻服务'); });
  el.btnStopAll.addEventListener('click', function () {
    // modified: 2.6.0 与后端新行为对齐——只停本工作台拉起的，外部运行的服务不动
    batched('stop-all', '停止本工作台启动的全部服务（外部运行的不动）');
  });
  el.btnSettings.addEventListener('click', openSettings);
  el.btnLayout.addEventListener('click', function () { setEditMode(!state.editMode); });  // modified
  el.btnTheme.addEventListener('click', function () {
    var current = document.documentElement.getAttribute('data-theme');
    var next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
    saveSettings({ theme: next }, '已切换到' + (next === 'dark' ? '暗色' : '明亮'));
  });
  el.btnExit.addEventListener('click', function () {
    confirmDialog('退出后只结束服务台的网页后端，已启动的业务服务继续运行。确定退出服务台？', function () {
      post('/api/exit', {}).then(function () {
        el.content.innerHTML = '<div class="empty">服务台已退出。双击桌面「服务台」可重新唤起。</div>';
        lastPrint = '';
        if (state.timer) { clearTimeout(state.timer); }
      }).catch(function () {
        el.content.innerHTML = '<div class="empty">服务台已退出。</div>';
        lastPrint = '';
      });
    });
  });

  el.modalClose.addEventListener('click', closeModal);
  el.modalMask.addEventListener('click', function (event) {
    if (event.target === el.modalMask) { closeModal(); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !el.modalMask.hidden) { closeModal(); }
  });

  // ------------------------------------------------------------------ 初始化
  function init() {
    // 网址参数可以临时指定主题/视图（截图、书签、演示用），不写回设置
    var forcedTheme = '';
    var forcedView = '';
    var forcedFx = '';  // modified: 渲染诊断开关
    try {
      var params = new URLSearchParams(location.search);
      forcedTheme = params.get('theme') || '';
      forcedView = params.get('view') || '';
      forcedFx = (params.get('fx') || '').toLowerCase();  // modified
    } catch (e) {}

    // modified: 2.6.3 原来所有首屏偏好共用一个 try：hub.sort 存了个坏 JSON，
    // 主题/视图/密度/自动刷新就一起失效。逐项隔离，并校验排序列名。
    try {
      var savedSort = localStorage.getItem('hub.sort');
      if (savedSort) {
        var parsed = JSON.parse(savedSort);
        var keys = COLUMNS.map(function (c) { return c.key; });
        if (parsed && keys.indexOf(parsed.key) >= 0) {
          state.sort = { key: parsed.key, dir: parsed.dir === 'desc' ? 'desc' : 'asc' };
        }
      }
    } catch (e) {}
    try {
      if (localStorage.getItem('hub.autoRefresh') === '0') { state.autoRefresh = false; }
    } catch (e) {}
    try {
      var cachedTheme = localStorage.getItem('hub.theme');
      if (cachedTheme && !forcedTheme) { applyTheme(cachedTheme, false); }
    } catch (e) {}
    try {
      var cachedView = localStorage.getItem('hub.view');
      if (cachedView && !forcedView) { state.view = cachedView === 'list' ? 'list' : 'card'; }
    } catch (e) {}
    try {
      var cachedDensity = localStorage.getItem('hub.density');
      if (cachedDensity) { applyDensity(cachedDensity); }
    } catch (e) {}

    if (forcedTheme) { applyTheme(forcedTheme, false); }
    if (forcedView) { state.view = forcedView === 'list' ? 'list' : 'card'; }
    setView(state.view, false);
    applyFxMode(forcedFx);  // modified: ?fx= 渲染诊断

    get('/api/settings').then(function (payload) {
      state.browsers = payload.browsers || [];
      state.browserResolved = payload.browserResolved || null;  // modified: 2.6.1
      state.autostart = payload.autostart || {};  // modified: 开机自启状态
      // modified: 2.6.0 同步逻辑抽到 adoptSettings（与「从备份恢复」共用）
      adoptSettings(payload.settings || {}, { keepTheme: !!forcedTheme, keepView: !!forcedView });
      state.settingsReady = true;   // modified: 2.6.3
    }).catch(function () {});

    var darkMq = window.matchMedia('(prefers-color-scheme: dark)');
    var onSchemeChange = function () {
      if (state.theme === 'system') {
        document.documentElement.setAttribute('data-theme', resolveTheme('system'));
      }
    };
    if (darkMq.addEventListener) {
      darkMq.addEventListener('change', onSchemeChange);  // modified: 旧内核没有 addEventListener
    } else if (darkMq.addListener) {
      darkMq.addListener(onSchemeChange);  // modified
    }

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { refresh(true); }
    });

    refresh().then(schedule);
  }

  init();
})();
```

---

## `services.json`

```json
{
  "version": 1,
  "updated": "2026-11-02",
  "note": "服务注册表。新增服务只需在此文件追加一条，工作台最多 1 秒后自动看到，无需改代码（ServiceHub.py / 2026-10-31 起为 Python 版）。kind: proc=工作台拉起并管理进程, tool=一次性脚本(只运行不常驻)。Docker 容器不再纳入本工作台（2026-10-31 按用户要求移除），需要时用 Docker Desktop 管理。args 必须是数组；不需要参与\"全部启动\"的桌面 GUI 工具请加 \"autostart\": false；有风险的动作加 \"confirm\": \"提示语\"，网页会先弹确认。version 可手写（不写则从 package.json/version.txt 推断），updated 不写则取目录里最新的文件修改时间。",
  "services": [
    {
      "id": "docscan",
      "name": "澄文扫描台",
      "group": "自建应用",
      "desc": "PDF 扫描件漂白/保色/成册：1bit 无损+印章掩膜、方向性断笔接续（公文·仿宋档）、导出沿用源名可覆盖、参数自动记忆、服务边界加固",
      "kind": "proc",
      "cwd": "D:\\Tools\\服务台\\apps\\docscan",
      "exe": "C:\\Program Files\\nodejs\\node.exe",
      "args": [
        "serve.js"
      ],
      "marker": "serve.js",
      "health": "port",
      "port": 8765,
      "url": "http://127.0.0.1:8765/",
      "urlPattern": null,
      "stopMode": "pid",
      "version": "2.4.7"
    },
    {
      "id": "asr-pipeline",
      "name": "网课转字幕",
      "group": "自建应用",
      "desc": "药品监管网课录音转字幕 + 校对，FastAPI 网页版",
      "kind": "proc",
      "cwd": "D:\\Tools\\服务台\\apps\\asr-pipeline",
      "exe": "C:\\Users\\guiker\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe",
      "args": [
        "server.py"
      ],
      "marker": "server.py",
      "health": "port",
      "port": 7870,
      "url": "http://127.0.0.1:7870/",
      "urlPattern": null,
      "stopMode": "pid",
      "version": "1.1",
      "updated": "2026-11-02"
    },
    {
      "id": "rename-cn",
      "name": "中文文件批量改名器",
      "group": "自建应用",
      "desc": "零依赖本地网页服务，端口在 8770-8800 之间自动选取",
      "kind": "proc",
      "cwd": "D:\\Tools\\服务台\\apps\\rename-cn",
      "exe": "C:\\Users\\guiker\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "app.py"
      ],
      "marker": "app.py",
      "health": "port",
      "port": null,
      "url": null,
      "urlPattern": "http://127\\.0\\.0\\.1:(\\d+)",
      "stopMode": "pid",
      "version": "1.2.0"
    },
    {
      "id": "ocr-local",
      "name": "WeKnora 本机 OCR",
      "group": "AI 与 OCR",
      "desc": "RapidOCR / PP-OCRv6 本机推理服务（备用版，端口避开 Docker 容器占用的 8110）",
      "kind": "proc",
      "cwd": "D:\\WorkBuddy\\weknora",
      "exe": "C:\\Users\\guiker\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\pythonw.exe",
      "args": [
        "local_ocr_service.py",
        "--port",
        "8111"
      ],
      "marker": null,
      "health": "port",
      "port": 8111,
      "web": true,
      "url": null,
      "urlPattern": null,
      "stopMode": "pid",
      "version": "2.0"
    },
    {
      "id": "scanapp",
      "name": "佳能扫描助手",
      "group": "桌面工具",
      "desc": "C3525 III 图形界面：选项一次勾选、扫描后 SMB 推送",
      "kind": "tool",
      "cwd": "D:\\Tools\\ScanInbox",
      "exe": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "args": [
        "-NoProfile",
        "-STA",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        "D:\\Tools\\ScanInbox\\ScanApp.ps1"
      ],
      "marker": "ScanApp.ps1",
      "health": "cmdline",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "pid",
      "autostart": false,
      "version": "1.0"
    },
    {
      "id": "naps2",
      "name": "NAPS2 扫描仪",
      "group": "桌面工具",
      "desc": "便携版扫描客户端（与佳能扫描助手二选一，配置在程序目录 Data 里）",
      "kind": "tool",
      "cwd": "D:\\Tools\\NAPS2app",
      "exe": "D:\\Tools\\NAPS2app\\NAPS2.Portable.exe",
      "args": [],
      "marker": "NAPS2",
      "health": "cmdline",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "pid",
      "autostart": false,
      "version": "portable"
    },
    {
      "id": "wxlive",
      "name": "视频号录制器",
      "group": "桌面工具",
      "desc": "视频号直播/回放 + m3u8 下载器，网页界面 8766，bridge 服务在 18002",
      "kind": "proc",
      "cwd": "D:\\Tools\\服务台\\apps\\wxlive-recorder",
      "exe": "C:\\Users\\guiker\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe",
      "args": [
        "wxlive_recorder.py",
        "--no-browser"
      ],
      "marker": "wxlive_recorder.py",
      "health": "port",
      "port": 8766,
      "url": "http://127.0.0.1:8766/",
      "urlPattern": null,
      "stopMode": "pid",
      "autostart": false,
      "version": "5.7.1"
    },
    {
      "id": "weknora-import",
      "name": "WeKnora 导入助手",
      "group": "桌面工具",
      "desc": "双击文件批量入库归档，走系统 Python 3.12 的 tkinter",
      "kind": "tool",
      "cwd": "D:\\WorkBuddy\\weknora",
      "exe": "C:\\Users\\guiker\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "WeKnora导入助手.py"
      ],
      "marker": "WeKnora导入助手.py",
      "direct": true,
      "health": "cmdline",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "pid",
      "autostart": false,
      "version": "1.0"
    },
    {
      "id": "cleandisk-check",
      "name": "C盘空间体检",
      "group": "维护工具",
      "desc": "扫描 C 盘空间占用并生成报告，不做任何删除",
      "kind": "tool",
      "cwd": "D:\\Tools\\C盘空间体检与清理",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\C盘空间体检与清理\\CheckDiskSpace.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "cleandisk-clean",
      "name": "C盘空间清理",
      "group": "维护工具",
      "desc": "执行清理流程，会删除临时文件，执行前请先看体检报告",
      "kind": "tool",
      "cwd": "D:\\Tools\\C盘空间体检与清理",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\C盘空间体检与清理\\CleanDiskSpace.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0",
      "confirm": "清理会删除 C 盘临时文件与部分缓存，建议先跑一次「C盘空间体检」看清再动手。确定执行清理？"
    },
    {
      "id": "office-guard-install",
      "name": "锁定 Office 默认打开",
      "group": "维护工具",
      "desc": "把 Word/Excel/PPT 共 27 个扩展名默认程序锁回 Office，装完需注销重登",
      "kind": "tool",
      "cwd": "D:\\Tools\\锁定Office默认打开程序",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\锁定Office默认打开程序\\Install.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "office-guard-status",
      "name": "Office 默认打开状态",
      "group": "维护工具",
      "desc": "只读检查 27 个扩展名当前由谁打开",
      "kind": "tool",
      "cwd": "D:\\Tools\\锁定Office默认打开程序",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\锁定Office默认打开程序\\Status.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "office-fixnow",
      "name": "Office 关联立即修复",
      "group": "维护工具",
      "desc": "马上把 27 个扩展名抢回 Office，不用注销重登（先关掉 WPS，否则可能被抢回去）",
      "kind": "tool",
      "cwd": "D:\\Tools\\锁定Office默认打开程序",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\锁定Office默认打开程序\\FixNow.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "office-unlock",
      "name": "撤销 Office 关联锁定",
      "group": "维护工具",
      "desc": "移除默认打开程序的锁定策略，把选择权交回系统（需要管理员权限）",
      "kind": "tool",
      "cwd": "D:\\Tools\\锁定Office默认打开程序",
      "exe": "C:\\Windows\\System32\\cmd.exe",
      "args": [
        "/c",
        "D:\\Tools\\锁定Office默认打开程序\\Uninstall.bat"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0",
      "confirm": "撤销后 WPS 等程序可能重新抢占 .doc/.docx 关联。确定撤销锁定？"
    },
    {
      "id": "backup-normal",
      "name": "备份 Normal.dotm 模板",
      "group": "维护工具",
      "desc": "把 Word 宏模板备份到 D:\\fnOS\\Temp\\office，只留最近 3 份，并校验含 vbaProject.bin",
      "kind": "tool",
      "cwd": "D:\\WorkBuddy\\工具",
      "exe": "C:\\Users\\guiker\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe",
      "args": [
        "D:\\WorkBuddy\\工具\\backup_normal.py"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "word-macro-check",
      "name": "Word 宏体检",
      "group": "维护工具",
      "desc": "只读：看 Normal.dotm 里有多少模块、Word 实际加载了几个、宏安全级别是多少",
      "kind": "tool",
      "cwd": "D:\\fnOS\\WorkBuddy经验仓\\06-Tools\\Word宏体检修复",
      "exe": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "D:\\fnOS\\WorkBuddy经验仓\\06-Tools\\Word宏体检修复\\Word宏一键修复.ps1"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0"
    },
    {
      "id": "word-macro-fix",
      "name": "Word 宏一键修复",
      "group": "维护工具",
      "desc": "备份注册表 → 调宏安全级别 → 清网络标记 → 结束并重启 Word → 验证模块是否加载",
      "kind": "tool",
      "cwd": "D:\\fnOS\\WorkBuddy经验仓\\06-Tools\\Word宏体检修复",
      "exe": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "D:\\fnOS\\WorkBuddy经验仓\\06-Tools\\Word宏体检修复\\Word宏一键修复.ps1",
        "-Auto",
        "-KillWord",
        "-RestartWord"
      ],
      "marker": null,
      "health": "none",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "none",
      "version": "1.0",
      "confirm": "一键修复会结束所有 Word 进程并重新启动 Word，未保存的文档会丢失。请先保存文档再继续。"
    }
  ]
}
```

---

## `services.local.example.json`

```json
{
  "note": "把本文件复制成 services.local.json 即可生效（该文件不入库，不会和仓库冲突）。默认按 id 合并：同 id 只覆盖写出来的字段，新 id 追加；remove 里的 id 会被隐藏；写 \"mode\": \"replace\" 则整份清单改用本文件的。",
  "remove": [
    "wxlive",
    "scanapp"
  ],
  "services": [
    {
      "id": "docscan",
      "exe": "D:\\Tools\\服务台\\apps\\docscan\\app.py",
      "cwd": "D:\\Tools\\服务台\\apps\\docscan",
      "desc": "在本机改了路径的条目，只写要覆盖的字段就行"
    }
  ]
}
```

---

## `启动服务台.bat`

```bat
@echo off
rem ==========================================================================
rem  ServiceHub - local services workbench (portable launcher)
rem
rem  Double click this file: the hub starts, stays in the tray, and opens the
rem  panel in the browser chosen in Settings (default: system browser).
rem  If an instance is already running it just re-opens the panel.
rem
rem  First run on a new machine: missing packages are installed automatically
rem  into runtime\site-packages (needs Python 3.10+). See setup-deps.bat.
rem
rem  Options:  --selftest   run diagnostics, print report, no browser window
rem ==========================================================================
setlocal DisableDelayedExpansion
cd /d "%~dp0"

rem ---- locate Python 3
rem  0) the interpreter that installed the packages (psutil ships binary
rem     extensions, so a different minor version cannot import them)
set "PY="
if exist "%~dp0runtime\python.txt" set /p PY=<"%~dp0runtime\python.txt"
if defined PY if not exist "%PY%" set "PY="
rem  1) the official py launcher: always points at a real system install
if not defined PY for /f "usebackq delims=" %%I in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"
rem  2) common per-user install dirs
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
rem  3) whatever "python" resolves to (last resort)
if not defined PY for /f "usebackq delims=" %%I in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"

if not defined PY (
  echo [ERROR] Python 3 not found on this machine.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
if not exist "%~dp0ServiceHub.py" (
  echo [ERROR] ServiceHub.py not found in %~dp0
  pause
  exit /b 1
)

rem ---- dependencies live in runtime\site-packages, not in git
"%PY%" -c "import sys;sys.path.insert(0,r'%~dp0runtime\site-packages');import psutil" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Installing psutil / pystray / Pillow into runtime\site-packages ...
  call "%~dp0setup-deps.bat" /quiet
  if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. Run setup-deps.bat manually to see why.
    pause
    exit /b 1
  )
)

if /i "%~1"=="--selftest" (
  chcp 65001 >nul
  "%PY%" -X utf8 "%~dp0ServiceHub.py" --selftest
  echo.
  echo Report written to logs\selftest-report.txt
  pause
  exit /b 0
)

rem Start the resident hub. The hub opens the panel itself with the configured
rem browser, so this script must not run "start http://..." (double tab).
rem modified: remember and clear stale port.txt first; a crashed previous run
rem otherwise makes this script report an old port immediately.
set "OLDPORT="
if exist "%~dp0runtime\port.txt" set /p OLDPORT=<"%~dp0runtime\port.txt"
if exist "%~dp0runtime\port.txt" del /q "%~dp0runtime\port.txt" >nul 2>nul
start "ServiceHub" "%PYW%" "%~dp0ServiceHub.py" --port 9500

rem Wait at most 15 seconds for runtime\port.txt as a liveness check
set HUBPORT=
for /l %%i in (1,1,15) do (
  if exist "%~dp0runtime\port.txt" (
    set /p HUBPORT=<"%~dp0runtime\port.txt"
    goto HAVE_PORT
  )
  timeout /t 1 /nobreak >nul
)
:HAVE_PORT
if defined HUBPORT (
  echo ServiceHub is running on port %HUBPORT%.
  echo If the panel did not open, visit http://127.0.0.1:%HUBPORT%/
) else (
  rem modified: an existing instance does not rewrite port.txt; restore it and say so.
  if defined OLDPORT (
    >"%~dp0runtime\port.txt" echo %OLDPORT%
    echo [WARN] No new port reported. ServiceHub may already be running on port %OLDPORT%.
    echo Open http://127.0.0.1:%OLDPORT%/ in your browser.
  ) else (
    echo [WARN] Hub did not report a port in 15 seconds.
    echo Check logs\hub-*.log, or run this file with:  --selftest
  )
)
exit /b 0
```

---

## `setup-deps.bat`

```bat
@echo off
rem ==========================================================================
rem  ServiceHub - install bundled python packages into runtime\site-packages
rem  Run once after cloning the repo on a new machine (the launcher calls it
rem  automatically when psutil is missing, so double clicking is optional).
rem  Usage:  setup-deps.bat          install / upgrade
rem          setup-deps.bat /quiet   no pause at the end (called by launcher)
rem ==========================================================================
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist "%~dp0runtime" mkdir "%~dp0runtime"

set "QUIET=0"
if /i "%~1"=="/quiet" set "QUIET=1"

rem ---- locate Python 3 (same order as the launcher)
set "PY="
if exist "%~dp0runtime\python.txt" set /p PY=<"%~dp0runtime\python.txt"
if defined PY if not exist "%PY%" set "PY="
if not defined PY for /f "usebackq delims=" %%I in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY for /f "usebackq delims=" %%I in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"

if not defined PY (
  echo [ERROR] Python 3 not found on this machine.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH", then run this file again.
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo [INFO] python : %PY%
"%PY%" -c "import sys; print('[INFO] version: ' + sys.version.split()[0])"
if errorlevel 1 (
  echo [ERROR] That interpreter does not run. Install a fresh Python 3.
  if "%QUIET%"=="0" pause
  exit /b 1
)

rem remember the interpreter: psutil / Pillow ship per-version binary wheels
>"%~dp0runtime\python.txt" echo %PY%

echo [INFO] installing psutil / pystray / Pillow into runtime\site-packages ...
"%PY%" -m pip install --disable-pip-version-check --no-warn-script-location --target "%~dp0runtime\site-packages" --upgrade --no-compile psutil pystray Pillow
if errorlevel 1 (
  echo.
  echo [ERROR] pip install failed. Things to try:
  echo         1^) "%PY%" -m pip install --upgrade pip
  echo         2^) if the network needs a proxy, set HTTPS_PROXY and retry
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo [INFO] verifying imports ...
"%PY%" -c "import sys; sys.path.insert(0, r'%~dp0runtime\site-packages'); import psutil, pystray, PIL; print('[OK] psutil ' + psutil.__version__ + ' | Pillow ' + PIL.__version__)"
if errorlevel 1 (
  echo [ERROR] Packages installed but import failed. Delete runtime\site-packages and retry.
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo.
echo [OK] Dependencies are ready. Double click the launcher bat to start the hub.
if "%QUIET%"=="0" pause
exit /b 0
```

---

## `svc.cmd`

```bat
@echo off
chcp 65001 >nul
setlocal
set "PS1=%~dp0svc.ps1"
if not exist "%PS1%" (
  echo [ERROR] svc.ps1 not found: %PS1%
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %ERRORLEVEL%
```

---

## `svc.ps1`

```powershell
param(
    [Parameter(Position = 0)][string]$Action = 'list',
    [Parameter(Position = 1)][string]$Target = ''
)

<#
================================================================================
 svc —— 服务台的命令行入口（与图形界面共用 services.json）
--------------------------------------------------------------------------------
 用法：
   svc list                      列出全部服务及状态
   svc status <id>               查看某个服务状态
   svc start <id>                启动服务（带端口的会等就绪）
   svc stop  <id>                停止服务
   svc open  <id>                打开服务网页
 供计划任务、脚本、其它工具调用；id 见 svc list 输出。
================================================================================
#>

$ErrorActionPreference = 'Continue'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ConfigFile = Join-Path $AppDir 'services.json'
$LogDir = Join-Path $AppDir 'logs'

function Get-SvcList {
    if (-not (Test-Path $ConfigFile)) { Write-Host "[ERROR] 找不到 $ConfigFile"; return @() }
    try {
        $cfg = (Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8) | ConvertFrom-Json
        return @($cfg.services)
    } catch {
        Write-Host "[ERROR] 配置解析失败：$($_.Exception.Message)"
        return @()
    }
}

function Test-PortOpen {
    param([int]$Port, [int]$TimeoutMs = 700)
    if ($Port -le 0) { return $false }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { $client.EndConnect($async); return $true }
        return $false
    } catch { return $false } finally { try { $client.Close() } catch { } }
}

function Get-State {
    param($Svc)
    if ($Svc.kind -eq 'tool') { return '工具' }
    if ($Svc.kind -eq 'docker') {
        $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
        if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
        try {
            $raw = & $dockerExe inspect -f '{{.State.Running}}' $Svc.container 2>$null
            if ("$raw".Trim() -eq 'true') { return '运行中' }
        } catch { }
        return '已停止'
    }
    if ($Svc.port) {
        if (Test-PortOpen -Port ([int]$Svc.port)) { return '运行中' }
        return '已停止'
    }
    if ($Svc.marker) {
        try {
            $procs = Get-CimInstance Win32_Process -ErrorAction Stop
            foreach ($procItem in $procs) {
                if ($procItem.CommandLine -and $procItem.CommandLine.ToLower().Contains($Svc.marker.ToLower())) { return '运行中' }
            }
        } catch { }
    }
    return '已停止'
}

function Get-Url {
    param($Svc)
    if ($Svc.url) { return $Svc.url }
    return $null
}

$list = Get-SvcList
if (-not $list) { exit 1 }

switch ($Action.ToLower()) {
    'list' {
        "{0,-22} {1,-12} {2,-8} {3}" -f 'ID', '分组', '状态', '说明'
        '-' * 88
        foreach ($svc in $list) {
            "{0,-22} {1,-12} {2,-8} {3}" -f $svc.id, $svc.group, (Get-State -Svc $svc), $svc.desc
        }
        exit 0
    }
    'status' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        "名称 : $($svc.name)"
        "状态 : $(Get-State -Svc $svc)"
        "端口 : $(if ($svc.port) { $svc.port } else { '无' })"
        "地址 : $(if (Get-Url -Svc $svc) { Get-Url -Svc $svc } else { '无' })"
        exit 0
    }
    'start' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        if ($svc.kind -eq 'tool') {
            & $svc.exe @($svc.args) | Out-Null
            Write-Host "已运行工具：$($svc.name)"
            exit 0
        }
        if ((Get-State -Svc $svc) -eq '运行中') { Write-Host "$($svc.name) 已在运行"; exit 0 }
        if ($svc.kind -eq 'docker') {
            $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
            if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
            & $dockerExe start $svc.container | Out-Null
            Write-Host "已启动容器：$($svc.container)"
            exit 0
        }
        if (-not (Test-Path $svc.exe)) { Write-Host "[ERROR] 找不到：$($svc.exe)"; exit 1 }
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        $logFile = Join-Path $LogDir ("{0}-{1}.log" -f $svc.id, (Get-Date).ToString('yyyyMMdd-HHmmss'))
        $argLine = ''
        $argList = @()
        if ($svc.args -is [string]) { $argList = @($svc.args -split '\s+' | Where-Object { $_ }) }
        elseif ($svc.args) { $argList = @($svc.args) }
        foreach ($a in $argList) { if ($a -match '[\s]') { $argLine += '"' + $a + '" ' } else { $argLine += $a + ' ' } }
        $inner = '"' + $svc.exe + '" ' + $argLine.Trim() + ' > "' + $logFile + '" 2>&1'
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'C:\Windows\System32\cmd.exe'
        $psi.Arguments = '/c "' + $inner + '"'
        $psi.WorkingDirectory = $svc.cwd
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        Write-Host "已启动 $($svc.name)  PID=$($proc.Id)"
        if ($svc.port) {
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 500
                if (Test-PortOpen -Port ([int]$svc.port)) { Write-Host "端口 $($svc.port) 已就绪"; break }
            }
        }
        exit 0
    }
    'stop' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        if ($svc.kind -eq 'docker') {
            $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
            if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
            & $dockerExe stop $svc.container | Out-Null
            Write-Host "已停止容器：$($svc.container)"
            exit 0
        }
        if ($svc.marker) {
            try {
                $procs = Get-CimInstance Win32_Process -ErrorAction Stop
                $killed = 0
                foreach ($procItem in $procs) {
                    if ($procItem.CommandLine -and $procItem.CommandLine.ToLower().Contains($svc.marker.ToLower())) {
                        Stop-Process -Id ([int]$procItem.ProcessId) -Force -ErrorAction SilentlyContinue
                        $killed++
                    }
                }
                Write-Host "已结束 $($killed) 个进程：$($svc.name)"
            } catch { Write-Host "[ERROR] $($_.Exception.Message)"; exit 1 }
        } else {
            Write-Host "[WARN] $($svc.name) 没有配置 marker，无法用命令行停止（请在服务台界面停止）"
        }
        exit 0
    }
    'open' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        $url = Get-Url -Svc $svc
        if (-not $url) { Write-Host "[WARN] $($svc.name) 没有网页入口"; exit 1 }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $url
        $psi.UseShellExecute = $true
        [void][System.Diagnostics.Process]::Start($psi)
        Write-Host "已打开：$url"
        exit 0
    }
    default {
        Write-Host '用法: svc list | svc status <id> | svc start <id> | svc stop <id> | svc open <id>'
        exit 1
    }
}
```

---

共 21 个文件，7515 行；其中带 modified 标注的行 353 行。
