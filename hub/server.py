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
