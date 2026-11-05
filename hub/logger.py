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
