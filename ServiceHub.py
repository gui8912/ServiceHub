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
