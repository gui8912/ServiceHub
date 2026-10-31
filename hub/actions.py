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
import time
from datetime import datetime

from . import browsers

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

        row = self.monitor.service_row(sid)
        if row and row.get('running'):
            return {'ok': False, 'message': '%s 已在运行（%s），跳过启动' % (svc.get('name'), row.get('state'))}

        exe = svc.get('exe')
        if not exe or not os.path.exists(exe):
            return {'ok': False, 'message': '找不到可执行文件：%s' % exe}

        # 上一轮的动态端口和待开网页标记不能带过来，否则会打开旧端口
        self.monitor.dynamic_port.pop(sid, None)
        self.monitor.pending_open.pop(sid, None)

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
        exe = svc.get('exe')
        if not exe or not os.path.exists(exe):
            return {'ok': False, 'message': '找不到可执行文件：%s' % exe}
        logfile = self._logpath(svc)
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

        row = self.monitor.service_row(sid)
        if not row or not row.get('running'):
            return {'ok': False, 'message': '%s 当前未运行' % svc.get('name')}

        rec = self.monitor.managed.get(sid)
        if rec and self.monitor.managed_alive(sid):
            self._kill_tree(rec.get('pid'), svc.get('name'))
            self.monitor.managed.pop(sid, None)
            self.monitor.dynamic_port.pop(sid, None)
            self.monitor.pending_open.pop(sid, None)
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
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                data = fh.read().splitlines()
            return {'ok': True, 'logfile': os.path.basename(path), 'lines': data[-lines:]}
        except Exception as exc:
            return {'ok': False, 'message': '读取日志失败：%s' % exc}
