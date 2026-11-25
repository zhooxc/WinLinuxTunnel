# wlt 使用手册

部署、配置、命令与排障。设计背景见 [DESIGN.md](../DESIGN.md)。

## 环境要求

| 侧 | 需要 | 不需要 |
|---|---|---|
| Windows 10/11 | 系统自带 OpenSSH 客户端（`ssh`/`scp`/`ssh-keygen`，1809+ 内置） | 管理员权限、自行安装 Python、plink |
| Linux | `python3`（3.8+）、`sshd`、一个普通账号 | root、安装系统包、任何守护进程 |
| 网络 | Windows 能 SSH 到 Linux | Linux→Windows 的任何入站通路 |

Linux 的 sshd 需允许 TCP 转发（`AllowTcpForwarding yes`，绝大多数发行版默认开启）。

## 安装

### Windows（给使用者）

1. 运行 `WinLinuxTunnel-Setup.exe`。它把程序复制到 `%LOCALAPPDATA%\WinLinuxTunnel\`，
   并创建开始菜单和桌面快捷方式。不需要管理员。配置仍在 `%USERPROFILE%\.wlt\`。
2. 从开始菜单或桌面打开 **WinLinuxTunnel**。窗口发现代理没在运行时，会自己启动
   `WinLinuxTunnel.exe --agent`（无控制台）。不用再去点别的启动脚本。
3. 打开「设置」，填写 Linux 地址和账号，点「运行安装向导」。
   向导会弹出一个控制台，**输入一次 Linux 密码**（只用来装公钥，不落盘、不进日志）。

安装向导完成这些事：生成两把专用密钥 → 装公钥 → 上传 `linux` / 文档到 `~/wlt` →
写本地配置与令牌 → 在当前用户的「启动」文件夹里放上 `WinLinuxTunnel.exe --agent`
的快捷方式 → 重启代理使新配置生效。

从源码直接装（开发）时，同样的脚本是：

```
powershell -ExecutionPolicy Bypass -File win\install-win.ps1
```

没有 `WinLinuxTunnel.exe` 时，自启快捷方式改为 `pythonw.exe win\wlt_main.py --agent`。

开发时打开界面：

```
python win/wlt_main.py
python win/wlt_main.py --agent
python win/wlt_main.py --minimized
```

`win\start-gui.vbs`、`start-win.vbs`、`start-win.cmd` 只是开发时的薄封装，内部仍是 `wlt_main.py`。不要把它们登记成自启，也不要让最终用户去双击。

> 首次连接会显示服务器主机指纹让你确认（`StrictHostKeyChecking=accept-new`）。

### 在 Windows 上构建安装包

构建机需要 python.org 的 CPython 3.8+（安装器里勾选 tcl/tk，这是默认项）。
不要用 embeddable 压缩包，那个包没有 Tk。

```
powershell -ExecutionPolicy Bypass -File tools\build-windows.ps1
```

脚本会建一个 `.venv-build`，安装 PyInstaller，然后产出：

| 路径 | 作用 |
|---|---|
| `dist\WinLinuxTunnel\WinLinuxTunnel.exe` | 常驻程序（onedir，旁边有 `_internal`） |
| `dist\WinLinuxTunnel-Setup.exe` | 给用户的单文件安装器，只在安装时解压一次 |

把 `WinLinuxTunnel-Setup.exe` 拷到目标机器运行即可。Linux CI 不负责打出这个 exe。

`tools\build-runtime.ps1` 是旧的「embeddable CPython + 补 Tcl/Tk」路径，不再作为发布方式。

### Linux

无需手动安装。安装器已把 CLI 放到 `~/wlt/linux/wlt`。建议加别名：

```bash
echo 'alias wlt=~/wlt/linux/wlt' >> ~/.bashrc && source ~/.bashrc
wlt status
```

## 配置

- Windows：`%USERPROFILE%\.wlt\wlt.conf`（键值对），令牌在 `agent.token`（ACL 保护）
  与用户环境变量 `WLT_AGENT_TOKEN`。
- Linux：`~/.wlt/bridge.env`（0600，含令牌与端口），规则真相源 `~/.wlt/tunnels.json`。

| 键 | 默认 | 含义 |
|---|---|---|
| `WLT_LINUX_HOST` / `WLT_LINUX_USER` | — | Windows 外连的目标 |
| `WLT_SSH_PORT` | 22 | SSH 端口 |
| `WLT_AGENT_PORT` | 8788 | Windows 控制代理（回环） |
| `WLT_TUNNEL_PORT` | 18081 | 控制隧道在 Linux 的监听口（回环） |

默认端口刻意与原 WinLinuxBridge（8787/18080）错开，**两者可共存**。

## 命令一览

```
wlt status | ping | doctor          健康检查 / 代理身份 / 环境报告
wlt run [--shell cmd|powershell] [--timeout N] -- <命令>   同步执行
wlt job start|status|logs|result|cancel|list|stats         持久化任务
wlt batch --file plan.json          多步顺序执行（资源键互斥）
wlt shot [--monitor N|--region x,y,w,h|--window HWND] [--out FILE]   截图
wlt monitors | windows [--json]     显示器 / 窗口列举
wlt click X Y [--double|--right|--middle]   点击
wlt move X Y | drag X1 Y1 X2 Y2 | scroll DELTA   鼠标
wlt type TEXT | key COMBO [--repeat N]   键盘
wlt focus --title T | --hwnd H      置前窗口
wlt wait-window TITLE [--timeout N] 等待窗口出现
wlt copy | paste TEXT               剪贴板读 / 写
wlt http URL [--method --body --header K:V]   访问 Windows 本机 HTTP
wlt ls [PATH] | get REMOTE [LOCAL] | put LOCAL [REMOTE] | rm PATH [--recursive]
wlt open PATH|URL                   在 Windows 打开
wlt tunnel add L|R <监听> <目标> [--note --id --lan]
wlt tunnel list | on|off|rm|probe <id> | sync | import
```

## 隧道规则（核心新功能）

> 谁要发起访问，就在谁那边开监听口。

| 方向 | 监听在 | 目标解析自 | 用途 |
|---|---|---|---|
| `L` | Windows | Linux | **Windows 访问 Linux 服务** |
| `R` | Linux | Windows | Linux 访问任意 Windows 服务 |

例：让 Windows 访问 Linux 上的 3080 web 服务（对应参考截图）：

```bash
wlt tunnel add L 3080 127.0.0.1:3080 --note "linux web"
# Windows 上即可：curl http://127.0.0.1:3080
wlt tunnel list        # 看每条规则实时状态
wlt tunnel probe l3080 # 端到端探测
```

- 规则增删改**即时生效**（重启用户隧道进程，1–2 秒闪断，不丢任务）。
- 断线**自动重连**，指数退避 10s→120s。
- 默认只绑回环；`--lan` 显式允许非回环监听。
- 控制端口 18081 保留；同方向监听端口冲突会被拒绝。
- `wlt tunnel sync` 强制把 Linux 真相源推给 Windows 并核对 authorized_keys。

GUI「隧道」页提供同样的增删改查（勾选启用、双击编辑、探测按钮）。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| `wlt status` 控制隧道 DOWN | 打开 WinLinuxTunnel（它会自行启动代理）；看 `%USERPROFILE%\.wlt\logs\` |
| 隧道 `remote port forwarding failed for listen port N` | Linux 侧旧 sshd 会话占着端口：`ss -tlnp \| grep :N`，等回收或杀掉 |
| `authentication refused` | 隧道钥未授权：在「设置」里再跑一次安装向导 |
| `host key mismatch` | 删 `~/.ssh/known_hosts` 里过期条目再试 |
| 截图很小/全黑 | Windows 锁屏了；需解锁会话 |
| 重启后桥没恢复 | 自启在登录时触发，需有人登录 |
| 规则状态 DRIFT | `wlt tunnel sync` 重新对齐 |

## 安全模型

- 代理只监听 127.0.0.1；每请求带 Bearer 令牌。
- 两把专用 ed25519 密钥：隧道钥 `restrict,port-forwarding` + 按规则动态生成的
  `permitopen`/`permitlisten` 白名单（泄露也只能开已配置的回环端口，无 shell）；
  管理钥可执行命令但禁止转发。
- 文件接口白名单（交付目录 + 用户目录），越界 403。
- Linux 密码仅安装期交互输入一次，不落盘。

## 已知限制

- 锁屏黑截图；重启需登录才恢复。
- 一个桥接实例对应一台 Windows 桌面；多目标分别部署。
- 隧道只绑回环（设计意图），局域网其它主机无法借用。