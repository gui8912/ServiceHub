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
        }
        # 可执行文件不在本机：从 GitHub 拉到笔记本、或工具被挪走后旧路径失效时，
        # 界面要直说"未安装"，而不是显示"已停止"让人以为点一下就能起来。
        exe = str(svc.get('exe') or '')
        row['missing'] = bool(exe) and not os.path.exists(exe)
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
        port_ok = bool(port_val and int(port_val) in snap.listen)

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

        if port_val:
            row['port'] = int(port_val)
            row['portText'] = str(port_val)
        elif svc.get('urlPattern'):
            row['portText'] = '动态'

        if svc.get('url'):
            row['url'] = svc['url']
        elif svc.get('urlPattern'):
            # 动态端口服务只在真的跑着时才给地址，停了就别留一个点不开的链接
            row['url'] = ('http://127.0.0.1:%d/' % int(port_val)) if (row['running'] and port_val) else None
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
        port = self.dynamic_port.get(svc['id'])
        if port and svc.get('urlPattern'):
            return 'http://127.0.0.1:%d/' % int(port)
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
        match = re.search(pattern, tail)
        if not match:
            return False
        try:
            port = int(match.group(1))
        except Exception:
            return False
        if self.dynamic_port.get(svc['id']) != port:
            self.dynamic_port[svc['id']] = port
            self.logger.ok('%s 动态端口：%d' % (svc.get('name'), port))
        return True
