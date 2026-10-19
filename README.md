# ChromeManager

Windows Chrome 多实例管理工具。它以独立的 Chrome Profile 为单位，提供中文管理页面，帮助需要同时维护多个项目、平台或账号的用户，清晰、稳定地启动和管理浏览器实例。

> 管理页面监听地址由 `.data/config/settings.toml` 中的 `cdp.bind_address` 控制。默认允许局域网访问，无登录或访问限制；局域网中能访问该端口的设备均可操作实例。请勿暴露到公网。

## 为什么开发它

在 Windows 上同时运行多个 Chrome 时，常见问题是账号、插件、Cookie、历史记录和代理配置彼此混杂；手动寻找并关闭某一个浏览器窗口也容易误操作。ChromeManager 为每个实例建立独立的数据目录与 CDP 端口，并把启动、停止、查看和资源占用集中到一个本地控制台中。

## 功能概览

- 🧩 独立 Profile：每个实例拥有独立的浏览器数据目录，重启后保留登录状态、插件、浏览记录和网站数据。
- 🪟 可见窗口运行：创建或启动实例后，以正常的 Windows Chrome 窗口显示，而非后台无头运行。
- 🔌 CDP 端口管理：自动分配并校验从 `9500` 起的调试端口。
- 🌐 启动网址：支持以英文逗号分隔多个网址；启动后先打开 Profile 信息页，再依次打开这些网址。
- 🔒 可选代理：可为单个实例配置代理；留空时 Chrome 会明确直连，不继承本机代理环境变量。
- 📋 本地控制台：创建、编辑、启动、停止、删除实例；运行中的实例禁止编辑，避免配置与运行状态不一致。
- 🎯 查看窗口：将已运行实例的 Chrome 窗口切换到前台；若窗口已在前台则置顶显示。
- 📈 资源监控：展示本机及各实例的 CPU、内存占用与最近 5 分钟趋势，每 10 秒刷新一次。
- 📈 资源采样保存在本机 SQLite 中，页面重启后仍可显示最近 5 分钟趋势；`/health` 可供独立监控检查。
- 🔌 每分钟检查运行实例的 CDP 连接和各标签页的命令响应；异常会显示具体卡住的页面，并使 `/health` 返回 503。检查不会自动关闭标签页或重启实例。
- 健康检查使用独立、限时的原生 CDP 命令，不再周期性附加 Playwright 到所有标签。浏览器启动时验证 WebSocket 命令及浏览器 PID；最小化运行时禁用后台计时器和渲染器限速。
- ⚠️ 本机内存连续约 3 分钟达到 90%，或 CPU 连续约 30 秒达到 95% 时显示告警。
- 💾 已停止实例可在页面手动备份与恢复浏览器数据；恢复前会自动保留当前数据的另一份备份。

## 环境要求

- Windows 10 或 Windows 11
- 已安装 Google Chrome（程序会尝试自动发现安装位置；也可手工指定路径）
- Python 3.12 或更高版本
- PowerShell 5.1+ 或 PowerShell 7+

## 手动部署

```powershell
git clone https://github.com/<你的账号或组织>/ChromeManager.git
Set-Location ChromeManager

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

python -m chrome_manager.cli web
```

首次启动后，在浏览器访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)，无需登录。管理服务保持在当前终端前台运行，按 `Ctrl+C` 即可停止服务。

也可双击 `start-chromemanager.bat`，在可见窗口中启动。脚本优先使用项目根目录已有的 `data/chrome_manager.db`，否则使用 `.data`；服务已运行时会直接退出。异常退出后每隔 10 秒重试，总计最多启动 3 次；正常退出或按 `Ctrl+C` 停止不会重试。启动时间和退出码记录在实际数据根目录的 `logs/startup.log` 中。

### 查看运行版本

访问 [http://127.0.0.1:8765/version](http://127.0.0.1:8765/version) 可查看 JSON 格式的程序版本、Git 提交号、未提交改动状态、启动时间（UTC）、实际程序目录及 Python 路径，用于排查“下载版本与运行页面不一致”。当前版本为 `0.2.0`。

信息在服务启动时记录，更新代码后须重启服务才能更新；路由禁止缓存。ZIP 下载或未安装 Git 时，`git_commit` 和 `git_dirty` 返回 `null`，其余信息仍可查看。`git_dirty: true` 表示启动时存在本地改动，运行内容不完全等同于该提交。路径仅用于诊断，不包含账号、代理凭据或浏览器数据。

若 PowerShell 阻止激活虚拟环境，可只对当前终端执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 部署前校验

不启动 HTTP 服务或 Chrome，只检查配置、数据目录、数据库和网页应用是否可用：

```powershell
python -m chrome_manager.cli web --dry-run
```

运行测试：

```powershell
python -m pytest
```

## 使用 AI 部署

将本仓库和下面的提示交给支持本地 Windows 终端的 AI 编程助手即可：

```text
请在 Windows 上部署 ChromeManager：检查 Python 版本和 Google Chrome，
创建 .venv 并安装项目依赖，执行 `python -m chrome_manager.cli web --dry-run`，
通过后以前台方式运行 `python -m chrome_manager.cli web`。
不要将项目 `.data` 目录中的本地 Profile、数据库、日志或配置提交到 Git。
```

AI 部署完成后，同样访问 `http://127.0.0.1:8765` 使用控制台。请只授予 AI 本项目目录及本机 Chrome 管理所需的权限，不要在配置或提示中提供不必要的账号密码、Cookie 或代理凭据。

## 启动命令

```powershell
# 启动页面（前台运行，Ctrl+C 停止）
python -m chrome_manager.cli web
```

## 本地数据与配置

默认数据根目录为项目内的 `.data`，其中保存 SQLite 数据库、Profile 数据、日志、备份和配置文件。这些内容均属于本机运行数据，并由 `.gitignore` 排除，不应提交到 Git。

如需将数据保存到其他位置，可在启动前设置环境变量：

```powershell
$env:CHROME_MANAGER_DATA_ROOT = "E:\ChromeManagerData"
python -m chrome_manager.cli web
```

首次初始化会创建 `<数据根目录>\config\settings.toml`。可在该文件中配置 Chrome 路径及本地 CDP 端口范围；默认端口范围为 `9500` 至 `9999`。

### 迁移到另一台 Windows 电脑

1. 在旧电脑停止管理服务和全部 Chrome 实例，再复制整个项目目录（包括被 Git 忽略的 `.data`）；不要复制 `.venv`，也不要只通过 Git 仓库迁移 Profile 数据。
2. 在新电脑任意目录放置项目，安装 Python 3.12+ 和 Chrome，运行 `python -m venv .venv`、`.\.venv\Scripts\python.exe -m pip install -e .`。
3. 双击项目根目录的 `start-chromemanager.bat`，或在项目目录运行 `.\.venv\Scripts\python.exe -m chrome_manager.cli web`。启动时旧版数据库中的受管 Profile 绝对路径会转换为相对于 `.data` 的路径；项目移动后仍能找到浏览器数据。

默认数据始终位于项目下的 `.data`，不依赖启动时的工作目录。旧版自动生成的 `.data/config/settings.toml` 即使记录了旧机器的绝对 `.data` 路径，也会按当前项目位置使用；新生成的配置不再写入绝对数据路径。如果曾自定义外部数据目录，请在新电脑重新设置 `CHROME_MANAGER_DATA_ROOT`。自定义 Chrome 路径、代理地址和占用中的 CDP 端口也需按新电脑环境核对。

## 项目结构

```text
chrome_manager/     核心业务、Web 控制台、数据访问与工具模块
tests/              自动化测试
pyproject.toml      Python 项目与依赖配置
```

## 安全边界

- 默认绑定 `0.0.0.0` 供局域网访问，且不要求密码。任何能连接该端口的设备都能查看信息并操作实例；请勿直接暴露到公网。
- Profile 目录可能包含登录状态和站点数据；复制、备份或共享前请先确认数据权限。
- 代理地址仅在明确填写时使用；若代理带有账号密码，请仅保存在本机配置中。
- `.gitignore` 已排除虚拟环境、数据库、日志、Profile、环境变量和本地测试数据。提交前仍建议执行 `git status` 复核。

## 开发与贡献

提交前建议执行：

```powershell
python -m pytest
python -m chrome_manager.cli web --dry-run
git status
```

本项目以 [MIT License](LICENSE) 发布。

## 异常处理与日志

管理服务每 10 秒采集资源并核对运行进程；手动关闭 Chrome 后，实例会同步为已停止。资源采集异常会保留上次数据、在页面显示告警，并在下个采样周期重试；页面无法连接管理服务时也会显示提示。CDP 异常和资源阈值告警仅在本地页面显示，不发送外部通知。

启动进程失败后恢复为已停止，可检查原因后重试。停止实例时优先请求 Chrome 正常关闭，超过配置的 `shutdown_timeout` 才强制结束并记入日志；强制结束仍可能丢失尚未写盘的数据。

日志位于数据根目录的 `logs` 文件夹（默认项目内 `.data/logs`）：

- `chrome_manager.log`：应用操作、启动/停止、状态同步与异常。
- `error.log`：应用异常及堆栈。
- `console.log`：Web 服务访问和运行日志。
- `diagnostics.jsonl`：逐步骤详细日志，每行一个 JSON 事件，包含 UTC 时间、管理器进程和线程、启动批次 `run_id`、请求 `request_id`、操作 `operation_id` / `parent_operation_id`、实例 ID、端口、浏览器 PID、步骤开始/完成/失败、耗时及完整异常链。HTTP 响应中的 `X-Request-ID` 可用于关联一次请求。
- `profiles/<实例ID>/chrome.log`：该实例 Chrome 标准输出和错误输出；每次启动追加时间和端口标记，浏览器启动前按配置大小轮转。
- `profiles/<实例ID>/chrome_debug.log`：Chrome 原生文件日志，通过 `CHROME_LOG_FILE` 指定路径，以补充 Windows 标准错误输出无法覆盖的沙盒渲染进程日志。每次启动先保留上一次文件为 `.1` 等副本，副本数遵循 `backup_count`。Chrome 持续运行时直接写入这两种文件，需重启该实例才能轮转原生日志。[Chrome 日志机制说明](https://www.chromium.org/for-testers/enable-logging/)。

详细日志覆盖管理器初始化、数据库初始化、资源采集、HTTP 请求及错误、实例创建/编辑/删除、备份/恢复、浏览器端口与进程身份校验、启动参数和进程创建、每次 CDP 就绪重试、页面逐条 CDP 响应、停止请求、正常退出等待、超时强制结束和运行状态写入。详细步骤进入 `diagnostics.jsonl`，日常控制台保留操作结果和告警。

应用日志不记录请求体、Cookie/Authorization 请求头、页面内容或 CDP 执行参数，并对常见凭据和 URL 查询参数脱敏。Chrome 原生日志由浏览器直接生成，可能包含站点地址或诊断内容，分享前需另外检查。外部项目执行的业务步骤仍应记录在调用项目自己的日志中，ChromeManager 记录浏览器生命周期、协议检测和检测到的异常。

例如在当前数据目录中查找某个端口的详细过程：

```powershell
Get-Content .\logs\diagnostics.jsonl | ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.port -eq 9600 } |
    Select-Object timestamp,event,operation_id,browser_pid,duration_ms,error
```

日志使用 UTF-8 编码并按 `settings.toml` 中的 `logging.max_size_mb` 与 `backup_count` 轮转。常见密码、令牌、Authorization 及 URL 中的凭据会脱敏；分享日志前仍应检查项目名、路径等业务信息。`GET /health` 返回 `ok`、`checking` 或 HTTP 503；资源采集失败、CDP 连接失败、CDP 检查自身失败或达到持续资源阈值时返回 503。首次 CDP 检查完成前暂显示 `checking`。

### 其他项目调用 CDP

调用前可查询 `GET /api/cdp/9600/health`（将端口替换为目标实例）。只有 HTTP 200 且 `ready: true` 表示最近一次检查正常；停止、检查中、检查过期、进程已更换或页面无响应时返回 503，未登记的端口返回 404。返回值包含 `checked_at`、`pid`、`failed_pages` 和异常原因，且禁止缓存。该接口按实例隔离，其他端口故障不会使健康的实例返回失败。

这是每分钟更新的健康快照，调用项目仍需设置连接超时，并对尚未开始业务操作的连接失败做有限重试。连接后只断开自己的 CDP 客户端，不调用 `Browser.close`，也不关闭其他任务的标签。提交操作结果不明时应先核验，避免重复执行。页面自身卡死、网站异常、电脑休眠等情况无法保证不发生。

浏览器级检测发生短暂超时时，会在同一轮健康检查的剩余时间内复查一次（首次最多 2 秒，复查最多 4 秒，总检查预算仍为 8 秒），并记录重试和恢复过程；持续超时、PID 不匹配或页面无响应仍会报告失败。这样可避免一次调度延迟使页面持续显示到下一分钟的“连接失败”。

管理服务通过进程锁防止同一数据目录被重复启动；进程异常退出后锁由系统释放。重启管理服务可应用新的监控逻辑，新增的 Chrome 启动参数需要在对应浏览器下次启动时生效。请在外部任务空闲时安排浏览器重启。

### 独立监测

Web 服务退出后无法给自己告警。可在另一个 PowerShell 终端运行独立监测脚本；它每 60 秒检查一次 `/health`，连续两次异常时写入 `.data/logs/watchdog.log`，恢复时记录恢复，不会重启服务或 Chrome：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\watchdog.ps1
```

单次检查可加 `-Once`。若需登录 Windows 后自动持续监测，可自行注册计划任务（以下命令会新增系统任务，需在确认后手动执行）：

```powershell
$watchdogScript = (Resolve-Path .\scripts\watchdog.ps1).Path
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdogScript`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'ChromeManager-Watchdog' -Action $action -Trigger $trigger -Description '检查 ChromeManager Web 与 CDP 健康状态'
```

该脚本仅记录本机日志，不发送邮件或第三方通知。

### Profile 备份与恢复

先停止实例，再在详情区域点击“备份数据”。备份保存在 `.data/backups/<实例ID>/`；选择备份并勾选确认后才能恢复。恢复前的当前数据会自动成为一份新的“恢复前”备份，便于回退。备份包含浏览器登录状态、Cookie 与插件数据，请像保护原 Profile 目录一样保护备份目录。此功能只备份浏览器数据；若需完整灾备，请停止管理服务后额外保存 `.data/data/chrome_manager.db`。备份目前没有自动清理或自动定时执行。

真实 Chrome 回归测试需在 Windows 上显式开启，测试使用临时目录和独立端口，验证启动、正常停止、再次启动、手动关闭后的状态同步及日志写入：

```powershell
$env:CHROME_MANAGER_LIVE_TEST = "1"
python -m pytest
Remove-Item Env:CHROME_MANAGER_LIVE_TEST
```

### 所有实例的 CDP 局域网访问

本机使用 Windows 持久端口转发，为每个运行中的受管实例提供 `0.0.0.0:<CDP端口>` 入口，转发到 Chrome 的 `127.0.0.1:<CDP端口>`。这与管理页面的监听地址是两项独立设置。

管理员运行 `scripts/enable-cdp-lan.ps1` 安装 `ChromeManager-CDP-LAN` 开机计划任务。后台同步程序读取本项目 `data/chrome_manager.db`，每轮间隔 5 秒，只开放已登记且运行中的实例；新建、停止、删除实例后自动更新端口转发和防火墙。系统配置操作也需要时间，通常数秒至十余秒生效。防火墙限制来源为本地子网。日志位于 `logs/cdp-lan.log`。

Chrome 停止后撤销转发，避免 Windows 通配监听阻塞下次启动；启动检查最多等待 20 秒完成撤销。同步任务异常时需查看上述日志。管理服务重启不影响现有转发或浏览器。外部项目使用本机局域网 IP 和实例端口访问，不使用 `0.0.0.0` 作为连接地址。
