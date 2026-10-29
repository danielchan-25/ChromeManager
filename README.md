# ChromeManager

Ubuntu 上的独立 Chrome Profile 与 CDP 管理器。由原 Windows 版本移植，当前仓库统一提供 Ubuntu 版本，保留 MIT 版权。此仓库只包含程序、测试与部署配置，不包含原机器的浏览器数据、数据库、日志或凭据。

首版目标为 **Ubuntu 24.04 LTS amd64 / Python 3.12 / Chrome Stable**。提供中文管理页面、独立数据目录、代理、默认网址、备份/恢复、只读 CDP 与页面检查。详细范围、故障设计、阶段计划和主机验收见 [移植方案](docs/MIGRATION_PLAN.md)，验证结果见 [测试报告](docs/VALIDATION.md)。

## 设计

- 管理页面默认 `0.0.0.0:8765`、无密码。公开 CDP 端口也默认绑定 `0.0.0.0`，按实例独立配置。浏览器提交管理操作须与当前控制台同源；跨站写请求返回 403。没有浏览器来源头的本机脚本/API 客户端仍可调用。
- Chrome 只监听本机内部端口 20000–29999，独立 TCP 转发服务提供公开入口，避免 wildcard 监听与浏览器抢占同一端口。
- systemd 正式部署为 Web、转发和每个 Chrome 实例分别建立用户服务。重启 Web 不重启浏览器；重启转发本身会断开已有连接，调用方需要有限重连。
- 只有进程名称、绝对数据目录、端口、创建时间以及实际 CDP 身份相符，才重新接管或操作实例。检测超时不会自动重启业务浏览器。
- 本机健康检查保留，不发送飞书或其他第三方告警。UTF-8 结构化日志记录请求、步骤、PID、失败与恢复。

## 环境

三个轻量实例测试可用 4 vCPU / 8 GB RAM；建议长期运行 8 vCPU / 16 GB RAM / 100 GB SSD。视频、下载及其他项目需要另外预留资源。使用专用普通用户运行，保留 Chrome sandbox。

安装系统依赖（管理员执行）：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv curl ca-certificates xvfb xauth openbox wmctrl x11vnc novnc xclip dbus-user-session dbus-x11 fonts-noto-cjk gnome-keyring libsecret-1-0
curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/google-chrome.deb
sudo apt-get install -y /tmp/google-chrome.deb
```

Chrome 的下载、网站 DNS/HTTPS 和业务网络访问需要可用；程序不会修改系统代理。Profile 未配置代理时 Chrome 使用直连；代理只作用于相应实例。

## 正式安装与开机运行

以最终运行服务的普通用户克隆项目，在该用户的 SSH/登录会话中执行：

```bash
git clone https://github.com/danielchan-25/ChromeManager.git
cd ChromeManager
export CHROME_MANAGER_DATA_ROOT="$HOME/.local/share/chromemanager/data-root"
bash scripts/install-user.sh
```

安装脚本建立项目自己的 `.venv`，将安装工具 pip 更新至 26.2 及以上的 26.x 版本，首次复制 `config/settings.example.toml`，生成用户服务及配置；既有 settings 和环境文件不会被覆盖。默认数据目录可改成已经授予该用户写权限的 `/srv/chromemanager`。安装位置记在 `~/.config/chromemanager/install.json`，以后 `scripts/cm.sh` 会定位该数据目录。

管理员为该用户开启无需登录的用户服务：

```bash
sudo loginctl enable-linger <运行用户名>
```

服务清单：

```bash
systemctl --user status chromemanager-web chromemanager-relay chromemanager-display chromemanager-desktop
journalctl --user -u chromemanager-web -u chromemanager-relay --since today
bash scripts/cm.sh doctor
```

创建三个空实例并立即启动：

```bash
bash scripts/cm.sh init --ports 9600,9601,9610
bash scripts/cm.sh profile-start profile-9600
bash scripts/cm.sh profile-start profile-9601
bash scripts/cm.sh profile-start profile-9610
```

为选定实例显式启用开机启动：

```bash
bash scripts/cm.sh profile-autostart profile-9600 --enable
bash scripts/cm.sh profile-autostart profile-9601 --enable
bash scripts/cm.sh profile-autostart profile-9610 --enable
```

删除实例会同时关闭该实例的开机启动；保留的浏览器数据目录不会删除。

`profile-autostart` 只改变下次开机启动策略，不会立即启动。页面“停止”只停止当前运行；若也不希望下次开机启动，请执行 `profile-autostart <名称> --disable`。systemd 对异常退出有限重试；正常关闭 Chrome 或手动停止服务不会被本程序周期性强行复活。不要另外用 cron、桌面自启动或第二份安装重复启动同一数据目录。

## 显示与远程操作

默认由普通用户的 Xvfb 提供固定 `DISPLAY=:99`，Openbox 提供窗口管理；X11 只用本机 socket，并使用用户私有 Xauthority。浏览器运行不依赖远程桌面客户端保持连接。现有桌面用户也可自行将环境文件的 DISPLAY/XAUTHORITY 改为实际 X11 会话。

管理页面选中运行中的实例，点击“打开浏览器”，会在新标签页显示当前浏览器桌面，可直接点击、输入、扫码和操作浏览器弹窗；关闭查看页不停止实例。连接中、断开及失败均有提示，可点击“重新连接”。中文或长文本可用“粘贴文字 / 中文”。多个实例共用显示桌面，打开时会激活选定实例，应避免同时操作不同实例。

正式安装默认启用 `[desktop] enabled=true, port=5900`；已有安装需补充配置并启用 `chromemanager-desktop` 用户服务。此服务运行 x11vnc，**仅监听本机回环地址**，浏览器通过管理页面同源 WebSocket 转发，沿用当前 LAN 无密码控制台的访问方式，无需对外开放 VNC 端口或安装客户端。noVNC 静态资源由系统包本地提供，无外部 CDN。禁用桌面或启用 headless 时，查看页会显示原因。

```toml
[desktop]
enabled = true
port = 5900
```

Xvfb 与网页桌面不负责解锁密钥环。凭据保存需要可用的 D-Bus 与 GNOME Keyring/兼容 secret service，并验证重启后的解锁方式。首版不自动设置无密码密钥环，也不以 `--password-store=basic` 绕过凭据保护。前台激活只在 X11 + wmctrl 下提供；Wayland 不承诺同样的窗口控制。可配置 `browser.headless=true` 供无人值守流程使用，但须另行验证网站登录与验证码。

## 从 Windows 导入

先制作 Windows 数据库的一致性快照并复制到 Ubuntu。导入仅包含未删除实例的元数据、公开端口、代理和默认网址；创建全新的 Linux 用户目录，不复制 Cookie/密码或旧 PID。Windows 原环境及 Profile 保留回退。

在创建默认三个实例之前，对空目标库导入；导入时需先停止 Web（转发和已有浏览器不应一起重启）：

```bash
systemctl --user stop chromemanager-web
bash scripts/cm.sh import-windows /path/to/windows-snapshot.sqlite3
systemctl --user start chromemanager-web
```

名称、端口或目录冲突时整批回滚本次导入。随后在页面启动各实例、重新登录，再测试业务调用。不要把 Windows 的 `settings.toml`、`.venv` 或 `cdp-lan-status.json` 复制到 Ubuntu。新增空实例和导入是两种初始化方式，避免先创建相同名称再导入。

## 调用与网络

- 管理页面：`http://<Ubuntu局域网IP>:8765`
- 浏览器：`http://<Ubuntu局域网IP>:9600`（其他实例用各自端口）
- 调用前检查：`GET /api/cdp/9600/health`，HTTP 200 且 `ready:true` 才表示最近一次本机检查及该公开转发均正常。
- `/api/status` 仅提供实例运行状态和浏览器连接状态，不采集 CPU/内存，也不保存历史指标。
- `/health` 汇总实例状态同步、浏览器/页面与转发检查；端口连通不代表网站业务请求一定成功。
- 调用方断开自己的 CDP 客户端，不调用 `Browser.close`、不关闭其他任务的页面；连接与业务步骤必须设置超时。提交结果不明时先核验，避免重复执行。

延续 LAN 无密码需求。管理员应按实际网段在防火墙放行 8765 和登记的公开 CDP 端口；20000–29999 仅供本机 Chrome 使用。程序不自动启用或改写主机防火墙。部署验收必须从另一台 LAN 主机同时验证 HTTP `/json/version`、返回的 WebSocket 地址及实际 CDP 指令。

## 日志与排错

- `logs/diagnostics.jsonl`：Web 操作和健康检查的关联步骤。
- `logs/chrome_manager.log`、`logs/error.log`：管理服务摘要及错误。
- `logs/relay/diagnostics.jsonl`：转发监听与错误，不记录 CDP 消息正文。
- `logs/profiles/<ID>/chrome.log`、`chrome_debug.log`：浏览器输出及原生日志。
- `data/cdp-lan-status.json`：转发心跳和逐端口状态，过期不会当作健康。
- systemd journal：服务退出码、启动失败、恢复过程；Linux OOM 另查系统日志。

程序日志按配置轮转，浏览器原生文件仅在受管启动前轮转。运行很久的 Chrome 原生日志仍需观察磁盘增长，停止实例后归档，不对正在使用的 Profile 做在线文件复制。

## 测试与开发

自动分配端口遵循 `cdp.auto_port_start`–`auto_port_end`，到达上限后会搜索范围内空闲端口；显式指定的端口可使用 9500–65535。

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade 'pip>=26.2,<27'
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
CHROME_MANAGER_LIVE_TEST=1 xvfb-run -a .venv/bin/python -m pytest tests/test_live_chrome.py -q
```

显式运行独立 systemd 集成测试（以普通用户在可用的用户 systemd 会话内执行）：

```bash
CHROME_MANAGER_SYSTEMD_TEST=1 .venv/bin/python -m pytest tests/test_systemd_live.py -q
```

该测试使用临时数据目录、独立端口和随机服务名前缀；结束后清理对应测试服务。真实浏览器测试同时覆盖页面恢复确认、删除目标和只读信息页。测试默认保留 Chrome sandbox；一次性容器需显式设置测试用的 `CM_TEST_NO_SANDBOX=1`。

开发配置默认 `process.runtime="direct"`、`relay.enabled=false`。直接模式用于前台调试；它不提供 systemd 的实例隔离，正式部署请使用示例配置。需要公开端口时另外运行 `python -m chrome_manager.cli relay` 并启用 relay 配置。禁用 relay 时，就绪结果仅表示本机内部 CDP，不表示公开端口可用。

`Dockerfile.test` 仅用于隔离 Ubuntu 代码与真实 Chrome 验证，不代替主机 systemd/开机验收：

```bash
docker build -f Dockerfile.test -t chromemanager:test .
docker run --rm --init chromemanager:test
docker run --rm --init --shm-size=1g -e CHROME_MANAGER_LIVE_TEST=1 -e CM_TEST_NO_SANDBOX=1 chromemanager:test xvfb-run -a python -m pytest tests/test_live_chrome.py -q
```

`--init` 为容器提供独立的 PID 1，避免 Xvfb 启动握手停滞并回收测试子进程。

容器测试中的 `CM_TEST_NO_SANDBOX` 仅供测试 fixture 使用，生产默认配置保留 sandbox。Windows 上可运行不依赖 Linux 的单元测试；真实浏览器测试只在 Linux 显式开启。
