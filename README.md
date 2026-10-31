# 服务台 · ServiceHub

本机所有自建服务 / 小工具的**一个入口**：托盘常驻，浏览器里一张面板，逐个启停、看状态、开网页。
从 PowerShell + WinForms 重写而来 —— 旧版一轮 WMI 扫描要 600–700ms，界面周期性假死；
现在换成 Python + psutil + 网页界面，一轮采集约 50–60ms，接口只读缓存，实测 20ms 级返回。

- 后端：Python 3.10+（标准库 `ThreadingHTTPServer`，零 Web 框架）
- 采集：`psutil`（进程命令行 + 监听端口）
- 前端：手写 HTML/CSS/JS，零 CDN、零构建，断网也能用
- 依赖不污染系统：全部装在 `runtime\site-packages`，由启动脚本自己 `sys.path` 加载

## 界面

| 卡片视图（明亮） | 列表视图 |
|---|---|
| ![卡片视图](docs/screenshot-light.jpg) | ![列表视图](docs/screenshot-list.jpg) |

暗色主题：

![暗色主题](docs/screenshot-dark.jpg)

护眼玻璃面板，明暗两套主题（可跟随系统）；卡片和列表两种视图；列表按任意列排序。

## 快速开始

### 本机（已装好）

双击 `启动服务台.bat` → 托盘出现图标，浏览器自动打开 `http://127.0.0.1:9500/`。

### 换一台电脑（笔记本等）

```bat
git clone <本仓库地址> "D:\Tools\服务台"
cd /d "D:\Tools\服务台"
启动服务台.bat
```

首次运行发现 `runtime\site-packages\psutil` 不存在，会自动调用 `setup-deps.bat`
把 `psutil / pystray / Pillow` 装进工具自己的目录（需要本机有 Python 3.10+）。

单独装依赖、看详细报错：

```bat
setup-deps.bat
```

诊断（不弹浏览器，只打印报告，同时写 `logs\selftest-report.txt`）：

```bat
启动服务台.bat --selftest
```

## 目录结构

```
服务台\
├─ ServiceHub.py            入口：解析参数、起 HTTP 服务、挂托盘
├─ hub\
│  ├─ registry.py           services.json 解析 + 本机差异层
│  ├─ monitor.py            后台采集线程（进程/端口 → 状态行）
│  ├─ actions.py            启动 / 停止 / 运行工具 / 打开网页
│  ├─ server.py             HTTP 路由 + JSON 接口 + 静态文件
│  ├─ settings.py           设置读写（含类型校验、原子落盘）
│  ├─ browsers.py           从注册表发现已装浏览器，按选择打开
│  ├─ meta.py               版本号 / 更新时间推断
│  ├─ tray.py               托盘图标与菜单
│  ├─ logger.py             日志（logs\hub-日期.log）
│  └─ ui\                   index.html / style.css / app.js
├─ services.json            服务注册表（唯一配置源，入库）
├─ services.local.example.json  本机差异层样例（复制成 .local.json 生效，不入库）
├─ 启动服务台.bat           启动器（纯 ASCII + CRLF + 无 BOM）
├─ setup-deps.bat           依赖安装（纯 ASCII + CRLF + 无 BOM）
├─ svc.cmd / svc.ps1        命令行入口：svc list / start / stop / open
└─ 服务台说明.txt           中文使用说明（UTF-8 BOM）
```

`runtime\`（端口号、pid、设置）、`logs\`、`apps\`（指向各工具目录的联接）都不入库。

## services.json 字段

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识，`svc` 命令行用的也是它 |
| `name` / `group` / `desc` | 显示名、分组、一句话说明 |
| `kind` | `proc` 常驻进程 / `tool` 一次性工具 |
| `exe` / `args` / `cwd` | 可执行文件、参数（**数组**）、工作目录 |
| `marker` | 命令行特征串，无端口进程靠它探活与停止 |
| `health` | `port` 看端口 / `cmdline` 看命令行特征 / `none` 不探活 |
| `port` / `url` | 固定端口与网页地址 |
| `urlPattern` | 动态端口：正则从子进程日志里抓端口，第 1 个捕获组必须是端口 |
| `autostart` | `false` 表示「全部启动」时跳过（桌面 GUI 工具别被批量拉起） |
| `confirm` | 有风险的动作写一句提示，网页点「运行」前先弹确认 |
| `version` / `updated` | 手写优先；不写则从 `package.json` / `version.txt` / 目录内最新文件推断 |

## 换机差异：services.local.json

`services.json` 入库共享，每台机器的工具路径却常常不同。把
`services.local.example.json` 复制成 `services.local.json`（已 gitignore）后：

- 默认**按 id 合并**：只写要覆盖的字段，其余沿用主表；新 id 直接追加
- `"remove": ["wslive"]` 隐藏本机没有的条目
- `"mode": "replace"` 整份清单改用本文件的（笔记本上只留自己装得上的工具时最省事）

条目路径失效时，界面会显示灰色「未安装」，不会假装「已停止」。

## 安全与边界

- 只监听 `127.0.0.1`，不对外网开放
- 所有写操作（启停、改设置）要求请求头 `X-ServiceHub: 1`，且预检不返回 CORS —— 别的网页无法偷偷调
- 服务以当前用户身份运行，没有提权逻辑
- 停止服务时先按 `marker` 找进程；外部已启动的实例按监听端口反查 PID

## 编码约定（改文件前务必遵守）

| 类型 | 要求 | 原因 |
|---|---|---|
| `.bat` / `.cmd` | 纯 ASCII、CRLF、**无 BOM** | cmd 按代码页解析：带 BOM 首行会变成 `锘緻echo off`；裸 LF 会把多行连成一行；中文会乱码 |
| `.ps1` / `.txt` | UTF-8 带 BOM、CRLF | PowerShell 5.1 与记事本才认 |
| `.py` / `.js` / `.css` / `.json` | UTF-8、LF | 跨平台、便于 diff |

`.gitattributes` 已固定这些换行规则，克隆下来不会跑偏。

## 常见问题

- **托盘图标不见了**：双击 `启动服务台.bat` 重新唤起；已启动的业务服务不受影响。
- **关掉浏览器标签页**：服务台仍在运行，退出请点面板右上角「退出服务台」或托盘右键。
- **端口被占**：默认 9500，被占用则自动 +1（9500–9519），实际端口见 `runtime\port.txt`。
- **改了浏览器 / 主题**：立即生效，不必重启。
- **GitHub 拉取慢**：给 git 配代理 `git config --global http.proxy http://127.0.0.1:端口`。
