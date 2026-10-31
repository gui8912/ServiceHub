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
        base = cfg.setdefault('services', [])
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
        for idx, item in enumerate(cfg.get('services', [])):
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
