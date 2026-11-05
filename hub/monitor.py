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
