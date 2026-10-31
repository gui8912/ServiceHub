"""服务台 ServiceHub —— Python 版核心包。

设计要点（相对旧 PowerShell/WinForms 版）：
  1. 采集用 psutil（一次 process_iter + 一次 net_connections，实测 60ms 左右），
     结果放在后台线程每秒刷新一次的快照里；HTTP 接口只读快照，永远不会被拖动或卡顿。
  2. 界面改成浏览器网页，不再有 WinForms 的消息泵坑（首次点击丢事件、隐藏窗体不派发）。
  3. 注册表仍然只有 services.json 一个，格式与旧版完全兼容，命令行入口 svc 不受影响。
"""
