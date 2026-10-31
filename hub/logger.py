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
        self.path = os.path.join(logdir, '%s-%s.log' % (name, datetime.now().strftime('%Y%m%d')))

    # ---------------------------------------------------------------- 写入
    def write(self, level, message):
        line = '%s  %-5s  %s' % (datetime.now().strftime('%H:%M:%S'), level, message)
        with self.lock:
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
    def tail(self, lines=200):
        """返回最近 lines 行（给网页日志窗用）。"""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, 'r', encoding='utf-8', errors='replace') as fh:
                data = fh.read().splitlines()
            return data[-lines:]
        except Exception as exc:
            return ['读取日志失败：%s' % exc]

    def clean_old(self, keep_days=14):
        """清理超过保留天数的旧日志，只在启动时跑一次。"""
        now = time.time()
        limit = keep_days * 86400
        try:
            for fn in os.listdir(self.logdir):
                full = os.path.join(self.logdir, fn)
                if not os.path.isfile(full):
                    continue
                if not (fn.startswith('hub-') and fn.endswith('.log')):
                    continue
                if now - os.path.getmtime(full) > limit:
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass
