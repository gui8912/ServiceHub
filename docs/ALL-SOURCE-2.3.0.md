# 服务台 ServiceHub 2.3.0 完整源代码

> 与 `D:\Tools\服务台` 下文件逐字节一致；修改行带 `# modified` / `// modified` / `/* modified */` 标注。

## 文件目录

- `ServiceHub.py`
- `hub/__init__.py`
- `hub/actions.py`
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
- `README.md`


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

    began = time.perf_counter()
    ctx.monitor.refresh()
    cost = (time.perf_counter() - began) * 1000
    add('采集一轮耗时 < 500ms', cost < 500, '%.1f ms' % cost)

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

    ctx = build_context()

    if args.selftest:
        ctx.logger.info('===== 自检开始 v%s =====' % APP_VERSION)
        code = run_selftest(ctx)
        ctx.logger.info('===== 自检结束，失败项 %d =====' % code)
        return code

    port_range = (args.port, args.port + max(1, args.tries) - 1)

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
    def _workdir(self, svc):
        cwd = svc.get('cwd')
        if cwd and os.path.isdir(cwd):
            return cwd
        if cwd:
            self.logger.warn('%s 工作目录不存在：%s，改用 %s' % (svc.get('name'), cwd, self.appdir))
        return self.appdir

    def _logpath(self, svc):
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        return os.path.join(self.logdir, '%s-%s.log' % (svc['id'], stamp))

    def _env(self):
        env = os.environ.copy()
        env.setdefault('PYTHONUNBUFFERED', '1')
        env.setdefault('PYTHONIOENCODING', 'utf-8')
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
            if row and row.get('running'):
                return {'ok': False, 'message': '%s 已在运行（%s），跳过启动' % (svc.get('name'), row.get('state'))}

            exe = svc.get('exe')
            if not exe or not os.path.exists(exe):
                return {'ok': False, 'message': '找不到可执行文件：%s' % exe}
            cwd = svc.get('cwd')  # modified
            if cwd and not os.path.isdir(cwd):  # modified: 工作目录失效就别拉了，直接说原因
                return {'ok': False, 'message': '工作目录不存在：%s' % cwd}

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
                self.monitor.pending_open[sid] = time.time() + 60
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
        logfile = self._logpath(svc)
        with self.lock:  # modified
            try:
                popen = self._spawn(svc, logfile)
            except Exception as exc:
                self.logger.error('%s 运行失败：%r' % (svc.get('name'), exc))
                return {'ok': False, 'message': '运行失败：%s' % exc}
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
            if not row or not row.get('running'):
                return {'ok': False, 'message': '%s 当前未运行' % svc.get('name')}

            rec = self.monitor.managed.get(sid)
            if rec and self.monitor.managed_alive(sid):
                self._kill_tree(rec.get('pid'), svc.get('name'))
                self.monitor.managed.pop(sid, None)
                self.monitor.dynamic_port.pop(sid, None)
                self.monitor.pending_open.pop(sid, None)
                self.monitor.last_exit.pop(sid, None)  # modified: 主动停止不算异常退出
                self.logger.ok('%s 已停止' % svc.get('name'))
                return {'ok': True, 'message': '%s 已停止' % svc.get('name')}

            # 不是本工作台拉起的：先确认（网页会弹二次确认再带 force 回来）
            pids = row.get('markerPids') or []
            if not pids:
                return {'ok': False, 'message': '%s 未找到可结束的特征进程（可能是外部端口占用）' % svc.get('name')}
            if not force:
                return {
                    'ok': False,
                    'needConfirm': True,
                    'pids': pids,
                    'message': '%s 不是本工作台启动的（PID: %s），确认要结束这些进程吗？'
                               % (svc.get('name'), ', '.join(str(p) for p in pids)),
                }
            for pid in pids:
                self._kill_tree(int(pid), svc.get('name'))
            self.monitor.dynamic_port.pop(sid, None)
            self.monitor.pending_open.pop(sid, None)
            self.logger.ok('%s 已结束外部进程：%s' % (svc.get('name'), pids))
            return {'ok': True, 'message': '%s 已结束' % svc.get('name')}

    def restart(self, sid, force=False):
        # modified: 新增动作，等价于"先停再起"；PM2 / Supervisor 都有，日常改完代码不用点两次
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        if svc.get('kind') == 'tool':
            return {'ok': False, 'message': '%s 是一次性工具，没有重启动作' % svc.get('name')}

        row = self.monitor.service_row(sid)
        if row and row.get('running'):
            result = self.stop(sid, force=force)  # modified: 外部实例先弹确认，别直接杀
            if not result.get('ok'):
                if result.get('needConfirm'):
                    return result  # modified: 把确认请求原样交给前端
                return {'ok': False, 'message': '重启失败（停止阶段）：%s' % result.get('message')}
            deadline = time.time() + 6
            while time.time() < deadline:
                self.monitor.refresh()
                row = self.monitor.service_row(sid)
                if not row or not row.get('running'):
                    break
                time.sleep(0.3)
        time.sleep(0.2)
        result = self.start(sid)
        if result.get('ok'):
            return {'ok': True, 'message': '%s 已重启' % svc.get('name')}
        return {'ok': False, 'message': '重启失败（启动阶段）：%s' % result.get('message')}

    def _kill_tree(self, pid, name):
        if not pid:
            return
        pid = int(pid)
        if pid == os.getpid():
            self.logger.error('拒绝自杀：%s 的 PID 指向服务台本身' % name)
            return
        self.logger.act('结束进程树 PID=%d（%s）' % (pid, name))
        if psutil is None:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        try:
            proc = psutil.Process(pid)
            targets = proc.children(recursive=True) + [proc]
        except Exception as exc:
            self.logger.warn('PID=%d 已不存在：%r' % (pid, exc))
            return
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

    # ================================================================ 打开网页
    def open_url(self, sid):
        svc = self.registry.get(sid)
        if not svc:
            return {'ok': False, 'message': '没有这个服务：%s' % sid}
        url = self.monitor.resolve_url(svc)
        if not url:
            return {'ok': False, 'message': '%s 没有网页入口' % svc.get('name')}
        ok, message = browsers.open_url(url, self.settings, self.logger)
        if ok:
            self.logger.ok('已打开：%s（%s）' % (url, message))
        else:
            self.logger.error('打开网页失败：%s' % message)
        return {'ok': ok, 'message': ('已打开 %s' % url) if ok else message}

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
        stopped, failed = [], []
        for svc in self.registry.services():
            if svc.get('kind') == 'tool':
                continue
            row = self.monitor.service_row(svc['id'])
            if not row or not row.get('running'):
                continue
            result = self.stop(svc['id'], force=True)
            (stopped if result['ok'] else failed).append(svc.get('name'))
        parts = ['已停止：%s' % '、'.join(stopped)] if stopped else []
        if failed:
            parts.append('失败：%s' % '、'.join(failed))
        return {'ok': True, 'message': '；'.join(parts) or '没有在运行的服务'}

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
            if cand:
                return max(cand, key=os.path.getmtime)
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
    if settings is None:  # modified: 调用方漏传设置时退回系统默认，别抛 AttributeError
        return 'auto', ''
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
                fh.seek(max(0, size - max_bytes))
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
    """
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


class Snapshot:
    """一次采集的原始材料。"""

    def __init__(self):
        self.stamp = 0.0
        self.procs = []        # [(pid, name, cmdline)]
        self.by_pid = {}       # pid -> name
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

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                self.logger.error('采集异常：%r' % exc)
            self._stop.wait(REFRESH_INTERVAL)

    # ================================================================ 采集
    def _take_snapshot(self):
        snap = Snapshot()
        began = time.perf_counter()
        snap.stamp = time.time()
        if psutil is None:
            snap.cost_ms = 0.0
            return snap
        attrs = ['pid', 'name', 'cmdline']
        try:
            for proc in psutil.process_iter(attrs=attrs):
                try:
                    info = proc.info
                    pid = info.get('pid')
                    if pid is None:
                        continue
                    cmd = info.get('cmdline') or []
                    snap.procs.append((pid, info.get('name') or '', ' '.join(cmd)))
                    snap.by_pid[pid] = info.get('name') or ''
                except Exception:
                    continue
        except Exception as exc:
            self.logger.warn('进程快照失败：%r' % exc)
        try:
            for conn in psutil.net_connections(kind='tcp'):
                try:
                    if conn.status == 'LISTEN' and conn.laddr:
                        snap.listen.setdefault(conn.laddr.port, conn.pid)
                        if conn.pid:
                            snap.listen_by_pid.setdefault(conn.pid, []).append(conn.laddr.port)
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
                self.managed.pop(sid, None)
                self.dynamic_port.pop(sid, None)
                self.pending_open.pop(sid, None)
                self.last_exit[sid] = code  # modified: 留下退出码，界面可提示"上次异常退出"
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
        marker_hit = False
        marker_pids = []
        marker = str(svc.get('marker') or '').strip().lower()
        if marker:
            exe_leaf = os.path.basename(str(svc.get('exe') or '')).lower()
            for pid, name, cmd in snap.procs:
                if exe_leaf and (name or '').lower() != exe_leaf:
                    continue
                if marker in (cmd or '').lower():
                    marker_hit = True
                    marker_pids.append(pid)

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
        port_ok = bool(port_num and port_num in snap.listen)  # modified

        detected = bool(alive or port_ok or marker_hit)
        # 动态端口服务也算"要端口"：没抓到端口之前只能算启动中，不能报"运行中"
        # （旧 PowerShell 版在这一步会提前判定就绪，结果是对方 pages 都还没监听就提示可用）
        expects_port = bool(port_val or svc.get('urlPattern'))
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

        # 外部运行 + 端口型服务：把监听端口的进程找出来，停止时才有 PID 可杀
        if row['external'] and port_val and not marker_pids:
            listen_pid = snap.listen.get(int(port_val))
            if listen_pid:
                marker_pids = [listen_pid]
        if marker_pids:
            row['markerPids'] = marker_pids
        if row['missing'] and not row['running']:
            row['state'] = '未安装'
        return row

    def resolve_url(self, svc):
        if svc.get('url'):
            return svc['url']
        port = _safe_int(self.dynamic_port.get(svc['id']))  # modified
        if port and svc.get('urlPattern'):
            return 'http://127.0.0.1:%d/' % port  # modified
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
            url = self.resolve_url(svc)
            if not url:
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
        if local.get('mode') == 'replace':
            cfg['services'] = list(local.get('services') or [])
            if local.get('groups'):
                cfg['groups'] = local['groups']
            self.logger.ok('已按 services.local.json 全量替换清单')
            return cfg
        drop = {str(x) for x in (local.get('remove') or [])}
        if drop:
            before = len(cfg['services'])
            cfg['services'] = [s for s in cfg['services'] if s.get('id') not in drop]
            self.logger.ok('services.local.json 隐藏了 %d 条服务' % (before - len(cfg['services'])))
        by_id = {s.get('id'): s for s in cfg['services']}
        added = 0
        for item in local.get('services') or []:
            sid = item.get('id')
            if not sid:
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
            if not item.get('id'):
                self.logger.warn('第 %d 条服务缺 id，已跳过' % (idx + 1))
                continue
            svc = dict(item)
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

from . import browsers

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')

APP_VERSION = '2.3.0'  # modified: 本次审计修复 + restart / lastExit
APP_BUILD = '2026-11-01'  # modified

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
        """用指定（或当前设置的）浏览器打开服务台自己。"""
        probe = {'browser': browser or self.settings.get('browser'),
                 'browser_path': (browser_path
                                  if browser_path is not None
                                  else self.settings.get('browser_path'))}
        return browsers.open_url(self.hub_url(), probe, self.logger)

    def settings_payload(self):
        data = self.settings.as_dict()
        self.monitor.auto_open = bool(data.get('auto_open', True))
        return {
            'ok': True,
            'settings': data,
            'browsers': browsers.discover(),
            'about': self.hub_info(),
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
        self._json(code, {'ok': False, 'message': message})

    MAX_BODY = 1024 * 1024   # modified: 本机接口不需要大 body，超过 1MB 直接拒绝

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
        raw = self.rfile.read(length).decode('utf-8', errors='replace')
        try:
            if raw.strip().startswith('{'):
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            return {k: v[0] if isinstance(v, list) and v else v
                    for k, v in parse_qs(raw).items()}
        except Exception:
            return {}

    def _guard(self):
        """POST 必须带自定义头；顺带挡掉跨站表单（它没法设置自定义头）。"""
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
        if path.startswith('/ui/') or path.startswith('/static/'):
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
            for key in ('auto_open', 'browser', 'browser_path', 'theme', 'view', 'refresh_ms'):
                if key in body:
                    patch[key] = item(key)
            if not patch:
                return self._ok(self.ctx.settings_payload())
            self.ctx.apply_settings(patch)
            return self._ok(self.ctx.settings_payload())

        if path == '/api/browser/test':
            browser = item('browser')
            browser_path = item('browser_path')
            ok, message = self.ctx.open_hub(browser, browser_path)
            return self._ok({'ok': ok, 'message': message})

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
        safe = os.path.basename(name)
        full = os.path.join(UI_DIR, safe)
        if not safe or '..' in safe or not os.path.isfile(full):
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
        port = self._bind()
        super().__init__(('127.0.0.1', port), handler)
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
        for port in range(self.start_port, self.end_port + 1):
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
            except OSError as exc:
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
        folder = os.path.dirname(self.path)  # modified: 相对路径（没有目录部分）时别对空串 makedirs
        if folder:
            os.makedirs(folder, exist_ok=True)
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
.blob {
  position: absolute;
  display: block;
  border-radius: 50%;
  filter: blur(90px);
  opacity: .55;
  will-change: transform;
}
.b1 { width: 46vw; height: 46vw; left: -10vw; top: -12vw; background: var(--blob-1); animation: drift1 26s ease-in-out infinite alternate; }
.b2 { width: 40vw; height: 40vw; right: -8vw; top: 6vh; background: var(--blob-2); animation: drift2 32s ease-in-out infinite alternate; }
.b3 { width: 38vw; height: 38vw; left: 26vw; bottom: -16vw; background: var(--blob-3); animation: drift3 38s ease-in-out infinite alternate; }
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
.group { margin-top: 18px; }
.groupTitle {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 12px;
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: .08em;
  color: var(--muted);
  text-transform: none;
}
.groupTitle::after {
  content: "";
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--line), transparent);
}
.groupTitle .count {
  font-weight: 400;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--glass-soft);
  border: 1px solid var(--glass-brd);
  letter-spacing: 0;
}

.grid {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(318px, 1fr));
}

/* ------------------------------------------------------------------ 卡片 */
.card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 16px 18px 14px;
  border-radius: var(--radius);
  background: var(--glass);
  border: 1px solid var(--glass-brd);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  backdrop-filter: blur(22px) saturate(160%);
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

.cardHead { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.cardName {
  font-size: 15.5px;
  font-weight: 700;
  line-height: 1.36;
  letter-spacing: .01em;
  word-break: break-word;
}
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

.cardVer {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding-top: 9px;
  border-top: 1px dashed var(--line);
  color: var(--muted);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.cardVer span { display: inline-flex; align-items: center; gap: 4px; }
.cardVer code {
  font: 11.5px/1.4 Consolas, "Cascadia Mono", monospace;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--accent-soft);
  color: var(--accent);
}

.cardActions { display: flex; align-items: center; gap: 8px; margin-top: 2px; flex-wrap: wrap; }
.cardActions .btn { padding: 6px 12px; font-size: 12.5px; }
.linkBtn {
  appearance: none;
  border: 0;
  background: none;
  color: var(--muted);
  font-family: inherit;
  font-size: 12.5px;
  padding: 6px 2px;
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
.pill.run::before { animation: pulse 2.4s ease-in-out infinite; }
.pill.external { background: var(--ext-bg); color: var(--ext); }
.pill.starting { background: var(--warn-bg); color: var(--warn); }
.pill.starting::before { animation: pulse 1s ease-in-out infinite; }
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

/* ------------------------------------------------------------------ 响应式 */
@media (max-width: 860px) {
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
@supports not ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px))) {
  .glass, .card, .svcTable td, .modal, .toast, .searchWrap { background: var(--bg-2); }
  html[data-theme="dark"] .glass,
  html[data-theme="dark"] .card,
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

  var STATE_RANK = { '运行中': 0, '外部运行': 1, '启动中': 2, '工具': 3, '已停止': 4, '未安装': 5 };

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
    theme: 'system',
    fetchedAt: Date.now()
  };

  var el = {};
  ['hubMeta', 'content', 'searchBox', 'groupChips', 'stats', 'viewSeg',
   'btnStartAll', 'btnStopAll', 'btnRefresh', 'btnExit', 'btnTheme', 'btnSettings',
   'toasts', 'modalMask', 'modalTitle', 'modalBody', 'modalFoot', 'modalClose'
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  var lastPrint = '';

  // ------------------------------------------------------------------ 工具
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
    if (text === '工具') { return 'tool'; }
    if (text === '未安装') { return 'missing'; }
    return '';
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

  // ------------------------------------------------------------------ 渲染
  function renderChips() {
    var groups = ['全部'];
    state.services.forEach(function (svc) {
      if (groups.indexOf(svc.group) < 0) { groups.push(svc.group); }
    });
    if (groups.indexOf(state.group) < 0) { state.group = '全部'; }
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
    var total = state.services.length;
    var running = state.services.filter(function (s) { return s.running; }).length;
    var tools = state.services.filter(function (s) { return s.kind === 'tool'; }).length;
    var cost = Math.round((state.hub || {}).cost_ms || 0);
    var parts = ['共 <b>' + total + '</b> 项', '运行 <b>' + running + '</b>'];
    if (tools) { parts.push('工具 <b>' + tools + '</b>'); }
    if (cost) { parts.push('扫描 <b>' + cost + '</b> ms'); }
    el.stats.innerHTML = parts.join(' · ');
  }

  function matches(svc) {
    var hitGroup = state.group === '全部' || svc.group === state.group;
    if (!hitGroup) { return false; }
    var key = state.keyword.trim().toLowerCase();
    if (!key) { return true; }
    var hay = [svc.name, svc.desc, svc.group, svc.portText, svc.state, svc.id,
               svc.version, svc.updated].join(' ').toLowerCase();
    return hay.indexOf(key) >= 0;
  }

  function uptimeSpan(svc) {
    if (!svc.running || !svc.uptime) { return ''; }
    return '<span>已运行 <b data-uptime="' + svc.uptime + '" data-uptime-at="' +
      state.fetchedAt + '">' + esc(humanUptime(svc.uptime)) + '</b></span>';
  }

  function buildCard(svc) {
    var card = document.createElement('div');
    card.className = 'card';

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
        : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '">启动</button>';
      if (svc.running) {  // modified: 重启只对运行中的常驻服务显示
        actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
      }
    }
    actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
      + (svc.url ? '' : ' disabled') + '>打开网页</button>';
    actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">查看日志</button>';

    card.innerHTML =
      '<div class="cardHead">' +
        '<div class="cardName">' + esc(svc.name) +
          '<span class="cardId">' + esc(svc.id) + '</span>' +
        '</div>' +
        '<span class="pill ' + stateClass(svc.state) + '">' + esc(svc.state) + '</span>' +
      '</div>' +
      '<div class="cardDesc">' + esc(svc.desc) + '</div>' +
      '<div class="cardMeta">' + meta + '</div>' +
      '<div class="cardVer">' +
        '<span>版本 <code>' + esc(svc.version || '—') + '</code></span>' +
        '<span>更新于 ' + esc(svc.updated || '—') + '</span>' +
      '</div>' +
      '<div class="cardActions">' + actions + '</div>';

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
        return '<th class="' + '">' + esc(col.label) + '</th>';
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
          : '<button type="button" class="btn primary" data-act="start" data-id="' + esc(svc.id) + '">启动</button>';
        if (svc.running) {  // modified
          actions += '<button type="button" class="btn" data-act="restart" data-id="' + esc(svc.id) + '">重启</button>';
        }
      }
      actions += '<button type="button" class="btn" data-act="open" data-id="' + esc(svc.id) + '"'
        + (svc.url ? '' : ' disabled') + '>网页</button>';
      actions += '<button type="button" class="linkBtn" data-act="log" data-id="' + esc(svc.id) + '">日志</button>';

      return '<tr>' +
        '<td class="cName">' + esc(svc.name) + '<span class="cId">' + esc(svc.id) + '</span>' +
          '<div class="cDesc">' + esc(svc.desc) + '</div></td>' +
        '<td>' + esc(svc.group) + '</td>' +
        '<td class="cVer"><code>' + esc(svc.version || '—') + '</code></td>' +
        '<td class="cUpd">' + esc(svc.updated || '—') + '</td>' +
        '<td><span class="pill ' + stateClass(svc.state) + '">' + esc(svc.state) + '</span></td>' +
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

  function renderContent(list) {
    el.content.innerHTML = '';
    if (!state.services.length) {
      el.content.innerHTML = '<div class="empty">还没有登记任何服务，去 services.json 里加一条就行。</div>';
      return;
    }
    if (!list.length) {
      el.content.innerHTML = '<div class="empty">没有匹配的服务。</div>';
      return;
    }
    if (state.view === 'list') {
      renderList(list);
      return;
    }
    var order = [];
    var byGroup = {};
    list.forEach(function (svc) {
      if (!byGroup[svc.group]) { byGroup[svc.group] = []; order.push(svc.group); }
      byGroup[svc.group].push(svc);
    });
    order.forEach(function (name) {
      var section = document.createElement('section');
      section.className = 'group';
      section.innerHTML = '<h2 class="groupTitle">' + esc(name) +
        '<span class="count">' + byGroup[name].length + ' 项</span></h2>';
      var grid = document.createElement('div');
      grid.className = 'grid';
      byGroup[name].forEach(function (svc) { grid.appendChild(buildCard(svc)); });
      section.appendChild(grid);
      el.content.appendChild(section);
    });
  }

  function fingerprint(list) {
    var rows = list.map(function (s) {
      return [s.id, s.state, s.portText, s.url, s.pid, s.version, s.updated, s.name,
              s.desc, s.group, s.kind, s.autostart, s.running].join('|');
    });
    return [state.view, state.keyword, state.group, state.sort.key, state.sort.dir,
            Object.keys(state.busy).join(','), rows.join('~')].join('#');
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
    var list = state.services.filter(matches);
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
    var bits = [
      '127.0.0.1:' + hub.port,
      'v' + (hub.version || '?'),
      'Python ' + hub.python,
      '已运行 ' + (humanUptime(hub.uptime) || '刚刚')
    ];
    if (hub.tray) { bits.push('托盘常驻'); }
    if (!hub.psutil) { bits.push('psutil 缺失'); }
    el.hubMeta.textContent = bits.join(' · ');
    el.hubMeta.title = '安装目录：' + (hub.appdir || '') + '\n日志目录：' + (hub.logdir || '');
  }

  // ------------------------------------------------------------------ 轮询
  function refresh(quiet) {
    return get('/api/status').then(function (payload) {
      state.failures = 0;
      state.fetchedAt = Date.now();
      state.services = payload.services || [];
      state.hub = payload.hub || {};
      renderHub();
      render();
    }).catch(function (err) {
      state.failures += 1;
      if (state.failures >= 3) {
        el.content.innerHTML =
          '<div class="offline">服务台后台没有响应（' + esc(err.message) + '）。' +
          '双击桌面「服务台」重新唤起；已启动的业务服务不受影响。</div>';
        lastPrint = '';
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
    setBusy(id, true);
    render();
    return post('/api/action', { id: id, action: action, force: !!force }).then(function (result) {
      if (result.needConfirm) {
        confirmDialog(result.message, function () { doAction(id, action, true); });
        return;
      }
      toast(result.message || '已执行', result.ok ? 'ok' : 'warn');
      return delayedRefresh();
    }).catch(function (err) {
      toast('操作失败：' + err.message, 'error');
    }).then(function () {
      setBusy(id, false);
      render();
    });
  }

  function batched(action, title) {
    confirmDialog('确定要' + title + '吗？', function () {
      toast(title + '中…');
      post('/api/batch', { action: action }).then(function (result) {
        toast(result.message || '完成', result.ok ? 'ok' : 'warn');
        delayedRefresh();
      }).catch(function (err) { toast('批量操作失败：' + err.message, 'error'); });
    });
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
  function closeModal() { el.modalMask.hidden = true; }

  function confirmDialog(message, onYes) {
    el.modalTitle.textContent = '请确认';
    el.modalBody.textContent = message;
    el.modalFoot.hidden = false;
    el.modalFoot.innerHTML = '';
    var yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'btn danger';
    yes.textContent = '确定';
    yes.addEventListener('click', function () { closeModal(); onYes(); });
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

  function saveSettings(patch, note) {
    return post('/api/settings', patch).then(function (payload) {
      state.settings = payload.settings || state.settings;
      if (typeof state.settings.refresh_ms === 'number') {
        state.refreshMs = state.settings.refresh_ms;
      }
      if (note) { toast(note, 'ok'); }
      schedule();
      return payload;
    }).catch(function (err) {
      toast('保存失败：' + err.message, 'error');
    });
  }

  function openSettings() {
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

    // ---- 行为
    var autoOpenRow = settingsRow('启动服务后自动打开网页',
      '只对带网页入口的服务生效；动态端口服务会等端口出来再打开',
      switchCtrl(cfg.auto_open, function (checked) {
        saveSettings({ auto_open: checked }, '已保存自动开网页 = ' + (checked ? '开' : '关'));
      }));

    var browserOptions = [['auto', '系统默认浏览器']];
    (state.browsers || []).forEach(function (b) {
      browserOptions.push([b.id, b.name + '（' + b.id + '）']);
    });
    browserOptions.push(['custom', '自定义路径…']);

    var browserRow = settingsRow('打开网页用哪个浏览器',
      '服务台的「打开网页」和托盘入口都走这里选定的浏览器',
      selectCtrl(browserOptions, cfg.browser || 'auto', function (value) {
        saveSettings({ browser: value }, '浏览器已设为 ' + value);
        pathRow.style.display = value === 'custom' ? '' : 'none';
      }));

    var pathInput = document.createElement('input');
    pathInput.type = 'text';
    pathInput.placeholder = '例如 C:\\Program Files\\...\\chrome.exe';
    pathInput.value = cfg.browser_path || '';
    var pathRow = settingsRow('自定义浏览器路径', '仅当上面选「自定义路径」时使用；可直接粘贴 exe 完整路径', pathInput);
    pathRow.style.display = (cfg.browser || 'auto') === 'custom' ? '' : 'none';
    pathInput.addEventListener('change', function () {
      saveSettings({ browser_path: pathInput.value.trim(), browser: 'custom' }, '自定义浏览器路径已保存');
    });

    var testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'btn';
    testBtn.textContent = '用这个浏览器打开服务台';
    testBtn.addEventListener('click', function () {
      post('/api/browser/test', {
        browser: (state.settings || {}).browser || 'auto',
        browser_path: (state.settings || {}).browser_path || ''
      }).then(function (res) {
        toast(res.message || '已尝试打开', res.ok ? 'ok' : 'warn');
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

    el.modalBody.appendChild(settingsGroup('外观', [themeRow, viewRow, refreshRow]));
    el.modalBody.appendChild(settingsGroup('行为', [autoOpenRow, browserRow, pathRow]));
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
  el.btnStopAll.addEventListener('click', function () { batched('stop-all', '停止全部服务'); });
  el.btnSettings.addEventListener('click', openSettings);
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
    try {
      var params = new URLSearchParams(location.search);
      forcedTheme = params.get('theme') || '';
      forcedView = params.get('view') || '';
    } catch (e) {}

    try {
      var savedSort = localStorage.getItem('hub.sort');
      if (savedSort) {
        var parsed = JSON.parse(savedSort);
        if (parsed && parsed.key) { state.sort = parsed; }
      }
      if (localStorage.getItem('hub.autoRefresh') === '0') { state.autoRefresh = false; }
      var cachedTheme = localStorage.getItem('hub.theme');
      if (cachedTheme && !forcedTheme) { applyTheme(cachedTheme, false); }
      var cachedView = localStorage.getItem('hub.view');
      if (cachedView && !forcedView) { state.view = cachedView === 'list' ? 'list' : 'card'; }
    } catch (e) {}

    if (forcedTheme) { applyTheme(forcedTheme, false); }
    if (forcedView) { state.view = forcedView === 'list' ? 'list' : 'card'; }
    setView(state.view, false);

    get('/api/settings').then(function (payload) {
      state.settings = payload.settings || {};
      state.browsers = payload.browsers || [];
      if (typeof state.settings.refresh_ms === 'number') { state.refreshMs = state.settings.refresh_ms; }
      if (state.settings.theme && !forcedTheme) { applyTheme(state.settings.theme, true); }
      if (state.settings.view && !forcedView) { setView(state.settings.view, true); }
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
  "updated": "2026-11-01",
  "note": "服务注册表。新增服务只需在此文件追加一条，工作台最多 1 秒后自动看到，无需改代码（ServiceHub.py / 2026-10-31 起为 Python 版）。kind: proc=工作台拉起并管理进程, tool=一次性脚本(只运行不常驻)。Docker 容器不再纳入本工作台（2026-10-31 按用户要求移除），需要时用 Docker Desktop 管理。args 必须是数组；不需要参与\"全部启动\"的桌面 GUI 工具请加 \"autostart\": false；有风险的动作加 \"confirm\": \"提示语\"，网页会先弹确认。version 可手写（不写则从 package.json/version.txt 推断），updated 不写则取目录里最新的文件修改时间。",
  "services": [
    {
      "id": "docscan",
      "name": "澄文扫描台",
      "group": "自建应用",
      "desc": "PDF 扫描件去黑边/去折痕/漂白保色/成册导出；1-bit Flate 无损 + 印章掩膜；网页版（自带零依赖静态服务，含路径边界校验与 404 text/plain）",
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
      "version": "2.4.2"
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
      "version": "1.0",
      "updated": "2026-11-01"
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
      "kind": "proc",
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
      "kind": "proc",
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
      "desc": "视频号直播/回放下载器，bridge 服务在 18002",
      "kind": "proc",
      "cwd": "D:\\Tools\\服务台\\apps\\wxlive-recorder",
      "exe": "C:\\Users\\guiker\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe",
      "args": [
        "wxlive_recorder.py"
      ],
      "marker": "wxlive_recorder.py",
      "health": "cmdline",
      "port": null,
      "url": null,
      "urlPattern": null,
      "stopMode": "pid",
      "autostart": false,
      "version": "1.0"
    },
    {
      "id": "weknora-import",
      "name": "WeKnora 导入助手",
      "group": "桌面工具",
      "desc": "双击文件批量入库归档，走系统 Python 3.12 的 tkinter",
      "kind": "proc",
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

## `README.md`

```markdown
# 服务台 · ServiceHub

> 仓库：<https://github.com/gui8912/ServiceHub>（私有）
> 当前版本：**2.3.0**（2026-11-01，源码审计修复 + 重启动作）

本机所有自建服务 / 小工具的**一个入口**：托盘常驻，浏览器里一张面板，逐个启停、看状态、开网页。
从 PowerShell + WinForms 重写而来 —— 旧版一轮 WMI 扫描要 600–700ms，界面周期性假死；
现在换成 Python + psutil + 网页界面，一轮采集约 50–60ms，接口只读缓存，实测 20ms 级返回。

- 后端：Python 3.10+（标准库 `ThreadingHTTPServer`，零 Web 框架）
- 采集：`psutil`（进程命令行 + 监听端口）
- 前端：手写 HTML/CSS/JS，零 CDN、零构建，断网也能用
- 依赖不污染系统：全部装在 `runtime\site-packages`，由启动脚本自己 `sys.path` 加载

## 界面

| 卡片视图（明亮） | 列表视图 |
|---|---|
| ![卡片视图](docs/screenshot-light.jpg) | ![列表视图](docs/screenshot-list.jpg) |

暗色主题：

![暗色主题](docs/screenshot-dark.jpg)

护眼玻璃面板，明暗两套主题（可跟随系统）；卡片和列表两种视图；列表按任意列排序。

## 快速开始

### 本机（已装好）

双击 `启动服务台.bat` → 托盘出现图标，浏览器自动打开 `http://127.0.0.1:9500/`。

### 换一台电脑（笔记本等）

```bat
git clone https://github.com/gui8912/ServiceHub.git "D:\Tools\服务台"
cd /d "D:\Tools\服务台"
启动服务台.bat
```

> 仓库是私有的，第一次 clone 需要登录：用同一 GitHub 账号，密码处填 Personal Access Token
> （Settings → Developer settings → Personal access tokens → 勾 `repo`）。登录一次后
> Windows 凭据管理器会记住，后续 pull/push 不再询问。

首次运行发现 `runtime\site-packages\psutil` 不存在，会自动调用 `setup-deps.bat`
把 `psutil / pystray / Pillow` 装进工具自己的目录（需要本机有 Python 3.10+）。

单独装依赖、看详细报错：

```bat
setup-deps.bat
```

诊断（不弹浏览器，只打印报告，同时写 `logs\selftest-report.txt`）：

```bat
启动服务台.bat --selftest
```

## 目录结构

```
服务台\
├─ ServiceHub.py            入口：解析参数、起 HTTP 服务、挂托盘
├─ hub\
│  ├─ registry.py           services.json 解析 + 本机差异层
│  ├─ monitor.py            后台采集线程（进程/端口 → 状态行）
│  ├─ actions.py            启动 / 停止 / 运行工具 / 打开网页
│  ├─ server.py             HTTP 路由 + JSON 接口 + 静态文件
│  ├─ settings.py           设置读写（含类型校验、原子落盘）
│  ├─ browsers.py           从注册表发现已装浏览器，按选择打开
│  ├─ meta.py               版本号 / 更新时间推断
│  ├─ tray.py               托盘图标与菜单
│  ├─ logger.py             日志（logs\hub-日期.log）
│  └─ ui\                   index.html / style.css / app.js
├─ services.json            服务注册表（唯一配置源，入库）
├─ services.local.example.json  本机差异层样例（复制成 .local.json 生效，不入库）
├─ 启动服务台.bat           启动器（纯 ASCII + CRLF + 无 BOM）
├─ setup-deps.bat           依赖安装（纯 ASCII + CRLF + 无 BOM）
├─ svc.cmd / svc.ps1        命令行入口：svc list / start / stop / open
└─ 服务台说明.txt           中文使用说明（UTF-8 BOM）
```

`runtime\`（端口号、pid、设置）、`logs\`、`apps\`（指向各工具目录的联接）都不入库。

## services.json 字段

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识，`svc` 命令行用的也是它 |
| `name` / `group` / `desc` | 显示名、分组、一句话说明 |
| `kind` | `proc` 常驻进程 / `tool` 一次性工具 |
| `exe` / `args` / `cwd` | 可执行文件、参数（**数组**）、工作目录 |
| `marker` | 命令行特征串，无端口进程靠它探活与停止 |
| `health` | `port` 看端口 / `cmdline` 看命令行特征 / `none` 不探活 |
| `port` / `url` | 固定端口与网页地址 |
| `urlPattern` | 动态端口：正则从子进程日志里抓端口，第 1 个捕获组必须是端口 |
| `autostart` | `false` 表示「全部启动」时跳过（桌面 GUI 工具别被批量拉起） |
| `confirm` | 有风险的动作写一句提示，网页点「运行」前先弹确认 |
| `version` / `updated` | 手写优先；不写则从 `package.json` / `version.txt` / 目录内最新文件推断 |

### 运行时状态字段（`/api/status` 返回，不是配置项）

| 字段 | 说明 |
|---|---|
| `state` | 运行中 / 外部运行 / 启动中 / 工具 / 已停止 / 未安装 |
| `running` / `ready` | 进程活着 / 端口已就绪 |
| `managed` / `external` | 是否工作台拉起 / 是否外部启动 |
| `port` / `portText` / `url` | 端口与网页入口 |
| `uptime` / `pid` | 已运行秒数 / 进程号 |
| `lastExit` | 上次托管进程的退出码（主动停止不计；异常退出时给前端提示） |
| `missing` | 可执行文件或工作目录在本机不存在 |
| `markerPids` | 按命令行特征找到的进程（外部实例停止时用） |

## 换机差异：services.local.json

`services.json` 入库共享，每台机器的工具路径却常常不同。把
`services.local.example.json` 复制成 `services.local.json`（已 gitignore）后：

- 默认**按 id 合并**：只写要覆盖的字段，其余沿用主表；新 id 直接追加
- `"remove": ["wslive"]` 隐藏本机没有的条目
- `"mode": "replace"` 整份清单改用本文件的（笔记本上只留自己装得上的工具时最省事）

条目路径失效时，界面会显示灰色「未安装」，不会假装「已停止」。

## 安全与边界

- 只监听 `127.0.0.1`，不对外网开放
- 所有写操作（启停、改设置）要求请求头 `X-ServiceHub: 1`，且预检不返回 CORS —— 别的网页无法偷偷调
- 服务以当前用户身份运行，没有提权逻辑
- 停止服务时先按 `marker` 找进程；外部已启动的实例按监听端口反查 PID

## 编码约定（改文件前务必遵守）

| 类型 | 要求 | 原因 |
|---|---|---|
| `.bat` / `.cmd` | 纯 ASCII、CRLF、**无 BOM** | cmd 按代码页解析：带 BOM 首行会变成 `锘緻echo off`；裸 LF 会把多行连成一行；中文会乱码 |
| `.ps1` / `.txt` | UTF-8 带 BOM、CRLF | PowerShell 5.1 与记事本才认 |
| `.py` / `.js` / `.css` / `.json` | UTF-8、LF | 跨平台、便于 diff |

`.gitattributes` 已固定这些换行规则，克隆下来不会跑偏。

## 常见问题

- **想重启某个服务**：运行中的常驻服务卡片上多了「重启」（等价于先停再起）；也可 `POST /api/action {"id":"xxx","action":"restart"}`。
- **卡片显示"上次退出 1"**：说明上次不是从工作台正常停止的（崩溃/被杀）。点「查看日志」看最后输出；主动停止不会显示这个标记。
- **托盘图标不见了**：双击 `启动服务台.bat` 重新唤起；已启动的业务服务不受影响。
- **关掉浏览器标签页**：服务台仍在运行，退出请点面板右上角「退出服务台」或托盘右键。
- **端口被占**：默认 9500，被占用则自动 +1（9500–9519），实际端口见 `runtime\port.txt`。
- **改了浏览器 / 主题**：立即生效，不必重启。
- **GitHub 拉取慢**：给 git 配代理 `git config --global http.proxy http://127.0.0.1:端口`。
```
