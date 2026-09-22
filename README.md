# ChromeManager

Windows Chrome 多实例管理工具。它以独立的 Chrome Profile 为单位，提供中文本地管理控制台和命令行，帮助需要同时维护多个项目、平台或账号的用户，清晰、稳定地启动和管理浏览器实例。

> 管理控制台只监听本机 `127.0.0.1`，不提供公网远程控制能力。

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
- 🖥️ 命令行与网页共用同一 SQLite 数据库，可按习惯使用。

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

python -m chrome_manager.cli init
python -m chrome_manager.cli web
```

首次启动后，在浏览器访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。管理服务保持在当前终端前台运行，按 `Ctrl+C` 即可安全停止服务。

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
不要将 D:\ChromeManager 下的本地 Profile、数据库、日志或配置提交到 Git。
```

AI 部署完成后，同样访问 `http://127.0.0.1:8765` 使用控制台。请只授予 AI 本项目目录及本机 Chrome 管理所需的权限，不要在配置或提示中提供不必要的账号密码、Cookie 或代理凭据。

## 常用命令

```powershell
# 初始化本地数据目录、配置文件和数据库
python -m chrome_manager.cli init

# 查看实际生效的配置
python -m chrome_manager.cli config show

# 创建并启动实例（端口可省略，由系统自动分配）
python -m chrome_manager.cli create demo --project "示例项目" --platform "bilibili" --account "账号A" --port 9500

# 查看、停止、启动实例
python -m chrome_manager.cli list
python -m chrome_manager.cli stop demo
python -m chrome_manager.cli start demo

# 启动本地控制台（前台运行，Ctrl+C 停止）
python -m chrome_manager.cli web
```

## 本地数据与配置

默认数据根目录为 `D:\ChromeManager`，其中保存 SQLite 数据库、Profile 数据、日志、备份和配置文件。这些内容均属于本机运行数据，不应提交到 Git。

如需将数据保存到其他位置，可在启动前设置环境变量：

```powershell
$env:CHROME_MANAGER_DATA_ROOT = "E:\ChromeManagerData"
python -m chrome_manager.cli web
```

首次初始化会创建 `<数据根目录>\config\settings.toml`。可在该文件中配置 Chrome 路径及本地 CDP 端口范围；默认端口范围为 `9500` 至 `9999`。

## 项目结构

```text
chrome_manager/     核心业务、Web 控制台、数据访问与工具模块
tests/              自动化测试
pyproject.toml      Python 项目与依赖配置
```

## 安全边界

- 控制台仅绑定 `127.0.0.1`，请勿通过端口转发或反向代理将其暴露到公网。
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

管理服务每 10 秒采集资源并核对运行进程；手动关闭 Chrome 后，实例会同步为已停止。资源采集异常会保留上次数据、在页面显示告警，并在下个采样周期重试；页面无法连接管理服务时也会显示提示。此版本的告警为本机页面和控制台提示，不包含邮件或第三方消息通知。

启动进程失败后恢复为已停止，可检查原因后重试。停止实例时优先请求 Chrome 正常关闭，超过配置的 `shutdown_timeout` 才强制结束并记入日志；强制结束仍可能丢失尚未写盘的数据。

日志位于数据根目录的 `logs` 文件夹（默认 `D:/ChromeManager/logs`）：

- `chrome_manager.log`：应用操作、启动/停止、状态同步与异常。
- `error.log`：应用异常及堆栈。
- `console.log`：Web 服务访问和运行日志。

日志使用 UTF-8 编码并按 `settings.toml` 中的 `logging.max_size_mb` 与 `backup_count` 轮转。常见密码、令牌、Authorization 及 URL 中的凭据会脱敏；分享日志前仍应检查项目名、路径等业务信息。管理服务仅用于本机，请勿直接向公网暴露。

真实 Chrome 回归测试需在 Windows 上显式开启，测试使用临时目录和独立端口，验证启动、正常停止、再次启动、手动关闭后的状态同步及日志写入：

```powershell
$env:CHROME_MANAGER_LIVE_TEST = "1"
python -m pytest
Remove-Item Env:CHROME_MANAGER_LIVE_TEST
```
