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
