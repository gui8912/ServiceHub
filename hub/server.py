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

APP_VERSION = '2.2.0'
APP_BUILD = '2026-10-31'

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

    def _read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
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
    allow_reuse_address = True

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
