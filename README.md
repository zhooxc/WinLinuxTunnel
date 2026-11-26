# WinLinuxTunnel

**在 Linux 终端里操作 Windows 桌面 + 任意双向 SSH 隧道 —— 隧道由 Windows 自己发起。**

> 命令行工具名 **`wlt`**。Windows 侧是一个带系统托盘的中文桌面程序：用户双击 **`WinLinuxTunnel.exe`**（安装后在开始菜单 / 桌面）。同一个 exe 加 `--agent` 是常驻代理。
>
> **v0.3.0** · [Releases](https://github.com/zhooxc/WinLinuxTunnel/releases/latest) · 完整设计见 [DESIGN.md](DESIGN.md)。
> 仿照 [WinLinuxBridge](https://github.com/Fatmear/WinLinuxBridge)（MIT）重新实现的轻量级工具，
> 继承其经过验证的执行语义，新增**规则驱动的任意方向隧道**与**中文图形界面**。
> 借用代码的归属声明见 [NOTICE](NOTICE)。

---

## 两种使用方式

从 [Releases](https://github.com/zhooxc/WinLinuxTunnel/releases/latest) 下载：

| 方式 | 资产 | 用法 |
|---|---|---|
| **exe 安装版**（推荐） | `WinLinuxTunnel-Setup.exe` | 运行安装器 → 开始菜单 / 桌面打开 WinLinuxTunnel |
| **exe 绿色版** | `WinLinuxTunnel-<版本>-onedir.zip` | 解压后直接双击 `WinLinuxTunnel.exe` |
| **便携式运行时版** | `WinLinuxTunnel-<版本>-portable.zip` | 解压后双击 `win\start-gui-portable.vbs` |

前两者是同一个 PyInstaller 程序（安装版多快捷方式与开机自启）；便携版随包
python.org 官方解释器，**同源同功能**，用于企业终端白名单拦截打包 exe 的机器。
见 [docs/PORTABLE.md](docs/PORTABLE.md)。

## 两个平面

**控制面 —— Linux 操作 Windows 桌面**

用 Linux 上的脚本 / AI 代理驱动 Windows 的程序、图形界面、文件与本机服务：
持久化任务、幂等重试、三超时分离、有界输出。不开入站端口、不做端口映射、
两端都不要管理员权限、不依赖任何第三方中转。

**隧道面 —— 任意方向、规则化、热生效**

> 谁要发起访问，就在谁那边开监听口。

| 方向 | 监听在 | 目标在 | 典型用途 |
|---|---|---|---|
| `-L` | Windows | Linux | **Windows 访问 Linux 服务**（web、数据库、API…） |
| `-R` | Linux | Windows | Linux 访问任意 Windows 服务（不止控制代理） |

规则增删改即时生效，断线自动重连（指数退避），每条规则状态独立可见。

## 架构

```
 Linux 主机（sshd 即服务端，无守护进程）           Windows 桌面（用户态，无需管理员）
 ─────────────────────────────────────           ──────────────────────────────
 ~/.wlt/                                         %USERPROFILE%\.wlt\
 ├── tunnels.json  规则真相源                    ├── wlt.conf / tunnels.json 镜像
 ├── bridge.env    令牌(0600)                    ├── agent.token (ACL)
 └── authorized_keys 动态 permitopen/permitlisten├── keys\ 两把专用 ed25519
                                                 └── jobs\ 持久化任务
        ▲
        │ ① 控制隧道（固定 -R，独立 ssh 进程）
 wlt run / job / shot ──► 127.0.0.1:18081 ═════► 127.0.0.1:8788  WinLinuxTunnel.exe --agent
        │                                         ├ HTTP + Bearer 鉴权
        │ ② 用户隧道（规则驱动，独立 ssh 进程）    ├ 持久化 job 引擎
        │   -L 127.0.0.1:3080:127.0.0.1:3080      ├ 隧道管理（看门狗/退避/热应用）
        │   -R 127.0.0.1:19000:127.0.0.1:9000     └ 桌面/文件/剪贴板（ctypes）
        │
        │                          WinLinuxTunnel.exe（GUI）只通过上面的 HTTP 口访问代理。
        │                          窗口自己拉起 --agent；窗口崩溃不会带走代理。
        │
 Windows 应用 ──► 127.0.0.1:3080 ══②-L════════► Linux 127.0.0.1:3080
 Linux 应用  ──► 127.0.0.1:19000 ══②-R════════► Windows 127.0.0.1:9000
```

控制隧道与用户隧道是**两个独立的 ssh 进程**：一条坏用户规则不会把控制面打死，
你始终能连上去修它。

## 图形界面（Windows 侧，中文）

- 双击 `WinLinuxTunnel.exe`。代理没在跑时，窗口会自己启动它（无需再点别的脚本）。
- 「设置」页填写 Linux 地址 / 账号，运行安装向导。密码只在弹出的控制台里输入一次。
- 隧道面板：规则列表（方向 / 监听→目标 / 启用勾选 / 实时状态），增删改对话框
- 概览页：代理、控制隧道、用户隧道健康一览；日志页尾随本地日志

## 环境要求

| 侧 | 需要 | 不需要 |
|---|---|---|
| Windows 10/11（1809+） | 系统自带 OpenSSH 客户端（`ssh` / `scp` / `ssh-keygen`） | 管理员、自行安装 Python、plink |
| Linux | python3、sshd、一个普通账号 | root、装系统包、任何守护进程 |
| 网络 | Windows 能 SSH 到 Linux | Linux→Windows 的任何入站通路 |

安装包把程序放到 `%LOCALAPPDATA%\WinLinuxTunnel\`。配置和状态仍在 `%USERPROFILE%\.wlt\`。

## 快速开始

1. 在 Windows 上运行 `WinLinuxTunnel-Setup.exe`（见下方「在 Windows 上构建」，或从 [Releases](https://github.com/zhooxc/WinLinuxTunnel/releases/latest) 下载）。它把 onedir 程序复制到 `%LOCALAPPDATA%\WinLinuxTunnel\`，并创建开始菜单和桌面快捷方式。不需要管理员。便携版则解压后双击 `win\start-gui-portable.vbs`，后续步骤相同。
2. 打开 **WinLinuxTunnel**。窗口会在后台启动代理（`WinLinuxTunnel.exe --agent`）。
3. 「设置」页填写 Linux 地址和账号 → 「运行安装向导」→ 在弹出的控制台里 **输入一次 Linux 密码**。
4. Linux：`~/wlt/linux/wlt status` 开始用（建议 `alias wlt=~/wlt/linux/wlt`）。

开发机没有安装包时，用系统 Python（3.8+，带 Tk）即可，不必准备便携运行时：

```
python win/wlt_main.py              # 窗口；代理没起会自己拉起
python win/wlt_main.py --agent      # 只跑常驻代理（无窗口）
python win/wlt_main.py --minimized  # 启动即进托盘
```

```
wlt status                          # 隧道 / 代理 / 规则健康一览
wlt run "ipconfig"                  # 在 Windows 上执行命令
wlt tunnel add L 3080 127.0.0.1:3080   # Windows 访问 Linux 的 3080 服务
wlt tunnel list                     # 每条规则的实时状态
wlt shot                            # 截图
```

## 命令一览

```
wlt run [--cwd PATH] [--env K=V] [--exe PROGRAM -- 参数] [--timeout N] <命令>
wlt run --request-id ID [--no-wait | --wait N] ...   # 改走持久化 job（去重 / 不等待）
wlt windows [--json]                 # 窗口（参考项目里 wl ps 列的是这个）
wlt close --title T | --hwnd H       # 发送 WM_CLOSE
wlt proc [--name SUB] [--pid N]      # 进程表
wlt kill PID                         # 只结束该进程树，拒绝 agent 自身
wlt log [--source all|agent|control|user] [--lines N]
```

选项、三超时和 `wlt kill` 的拒绝规则见 [docs/USAGE.md](docs/USAGE.md)。没有 `wlt lanes`。

## 安全模型

- 代理只监听 `127.0.0.1`；每个请求带 Bearer 令牌。
- 两把专用 ed25519 密钥：隧道钥 `restrict,port-forwarding` + **按规则动态生成的
  `permitopen`/`permitlisten` 白名单**（泄露也只能打开已配置的回环端口，拿不到 shell）；
  管理钥可执行命令但禁止任何转发。
- Linux 密码只在安装期交互输入一次，不落盘、不进日志。
- 文件接口限定白名单目录，越界 `403`。

## 与 WinLinuxBridge 的关系

wlt 是独立重写，不是 fork。继承其最有价值的部分（持久化任务语义、三超时、
有界输出、双钥安全模型、隧道自愈），新增任意双向隧道与 GUI，砍掉三条同步车道、
plink 依赖与 PowerShell 常驻体系。`wl ps` 列的是窗口，对应这里的 `wlt windows`；
进程表是 `wlt proc`，结束进程树是 `wlt kill`（不伤 agent 自身）。`wl run` 始终走
job；`wlt run` 默认仍是同步执行，只有带上 `--request-id`、`--no-wait` 或 `--wait`
才进入同一个 job 引擎。默认端口刻意错开（8788/18081 vs 8787/18080），
**两者可在同一台机器上共存**。详见 [DESIGN.md](DESIGN.md) 与 [NOTICE](NOTICE)。

## 在 Windows 上构建

目标机拿到的是 **一个安装程序** `WinLinuxTunnel-Setup.exe`。它只在安装时解压一次。常驻的是目录版程序（`WinLinuxTunnel.exe` 加旁边的依赖），不是每次启动都解压到临时目录的 onefile。

在一台 Windows 10/11 上（需要 python.org 的 CPython 3.8+，自带 Tcl/Tk；不要用 embeddable 压缩包）：

```
powershell -ExecutionPolicy Bypass -File tools\build-windows.ps1
```

产物：

| 路径 | 是什么 |
|---|---|
| `dist\WinLinuxTunnel\` | onedir。里面的 `WinLinuxTunnel.exe` 才是用户平时运行的程序 |
| `dist\WinLinuxTunnel-Setup.exe` | 单文件安装器。复制上面的目录到 `%LOCALAPPDATA%\WinLinuxTunnel\`，并建立快捷方式 |

Linux 上的 CI 不产生这个 exe。脚本、spec 和 `win/wlt_main.py` 都在仓库里，换到 Windows 按上面的命令构建即可。绿色版资产就是把 `dist\WinLinuxTunnel\` 直接压成 zip。

**便携式运行时版**（第二种发布形态，供白名单拦截打包 exe 的机器）：

```
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
python tools\package_portable.py
```

产物 `dist\WinLinuxTunnel-<版本>-portable.zip`：源码 + embeddable CPython/Tk，双击 `win\start-gui-portable.vbs` 启动。`build-runtime.ps1` 下载与本机 CPython 同版本的 embeddable 包并嫁接 Tcl/Tk（带真实 Tk 自检）；`package_portable.py` 复用 `wlt_payload` 的密钥/状态排除规则。详见 [docs/PORTABLE.md](docs/PORTABLE.md)。

## 许可

[MIT](LICENSE)。用 PyInstaller 打出来的 Windows 安装包会带上构建机的 CPython、Tcl/Tk，以及 PyInstaller bootloader（带有允许随应用分发的例外条款）。这些不在本仓库源码里。详见 [NOTICE](NOTICE)。