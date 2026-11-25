# wlt 使用手册

部署、配置、命令与排障。设计背景见 [DESIGN.md](../DESIGN.md)。

## 环境要求

| 侧 | 需要 | 不需要 |
|---|---|---|
| Windows 10/11 | 系统自带 OpenSSH 客户端（`ssh`/`scp`/`ssh-keygen`，1809+ 内置） | 管理员权限、安装 Python（发布包自带便携运行时）、plink |
| Linux | `python3`（3.8+）、`sshd`、一个普通账号 | root、安装系统包、任何守护进程 |
| 网络 | Windows 能 SSH 到 Linux | Linux→Windows 的任何入站通路 |

Linux 的 sshd 需允许 TCP 转发（`AllowTcpForwarding yes`，绝大多数发行版默认开启）。

## 安装

### Windows

发布包完整解压后，二选一：

- **图形界面**：双击 `win\start-gui.vbs` → 「配置」页 → 「运行安装向导」。
- **命令行**：
  ```
  powershell -ExecutionPolicy Bypass -File win\install-win.ps1
  ```

按提示填 Linux 地址、账号，**输入一次 Linux 密码**（仅用于装公钥，不落盘、不进日志）。
安装器自动完成：生成两把专用密钥 → 装公钥 → 上传 `linux/tools/docs` 到 `~/wlt` →
写本地配置与令牌 → 注册当前用户自启 → 启动代理。

> 首次连接会显示服务器主机指纹让你确认（`StrictHostKeyChecking=accept-new`）。

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
wlt run [选项] [--] <命令>          在 Windows 上执行（见下方「执行」）
wlt job start|status|logs|result|cancel|list|stats         持久化任务
wlt batch --file plan.json          多步顺序执行（资源键互斥）
wlt shot [--monitor N|--region x,y,w,h|--window HWND] [--out FILE]   截图
wlt monitors | windows [--json]     显示器 / 窗口列举
wlt click X Y [--double|--right|--middle]   点击
wlt move X Y | drag X1 Y1 X2 Y2 | scroll DELTA   鼠标
wlt type TEXT | key COMBO [--repeat N]   键盘
wlt focus --title T | --hwnd H      置前窗口
wlt close --title T | --hwnd H      向窗口发送 WM_CLOSE
wlt wait-window TITLE [--timeout N] 等待窗口出现
wlt proc [--name SUB] [--pid N] [--limit N] [--json]   进程表
wlt kill PID [--json]               结束该进程树（拒绝 agent 自身）
wlt log [--source all|agent|control|user] [--lines N] [--json]  日志尾部
wlt copy | paste TEXT               剪贴板读 / 写
wlt http URL [--method --body --header K:V]   访问 Windows 本机 HTTP
wlt ls [PATH] | get REMOTE [LOCAL] | put LOCAL [REMOTE] | rm PATH [--recursive]
wlt open PATH|URL                   在 Windows 打开
wlt tunnel add L|R <监听> <目标> [--note --id --lan]
wlt tunnel list | on|off|rm|probe <id> | sync | import
```

## 执行

`wlt run` 默认走同步接口 `POST /run`。`--cwd`、`--env`、`--exe` 都是这个接口本来就有的字段：

```bash
wlt run [--shell cmd|powershell] [--timeout N] [--cwd PATH] \
        [--env K=V ...] [--exe PROGRAM -- 参数...] [--json] [--] <命令>

wlt run --cwd C:\fw --env MODE=lab -- ipconfig
wlt run --exe C:\tools\flash.exe -- --port COM3
```

- `--cwd`：工作目录。
- `--env K=V`：可重复。合并进 Windows 进程环境，不替换整个环境。值里可以再含 `=`。
- `--exe`：直接启动该程序，后面的参数是 argv，不再拼成 cmd 命令行。参数以 `-` 开头时用 `--` 隔开。
- `--timeout`：Windows 上的**执行超时**（秒）。同步 `/run` 到点结束它启动的那个进程再返回（这一条不扫整棵进程树；要树级超时，用下面的 job 路径）。同步调用的 HTTP 等待是「执行超时 + 15 秒」，好让这次返回先到，而不是客户端先断开。连不上代理仍是退出码 125。

`--request-id`、`--no-wait` 和显式的 `--wait` 只存在于持久化 job 引擎。同步 `/run` 不会按 request id 去重，也不会在进程结束前返回，所以带上其中任意一个时，`wlt run` 改为 `POST /job/start`：

```bash
wlt run --request-id flash-001 --timeout 300 --cwd C:\fw --env MODE=lab -- flash.exe
wlt run --request-id flash-001 --no-wait -- C:\tools\flash.exe --port COM3
wlt run --wait 60 --timeout 300 -- long-task
```

- `--request-id`：相同 id 返回原来的 job，不会再启动一次。只允许 1–128 位字母、数字、`.`、`_`、`-`（它会变成 Windows 上的文件名，不能带 `:` 或路径分隔符）。
- `--no-wait`：启动后立刻打印 job id，退出码 0。之后用 `wlt job status|logs|result` 看。
- `--wait`：这个客户端愿意等的秒数。不写时，job 路径的客户端等待 = 执行超时 + `WLT_WAIT_MARGIN`（默认 30 秒）。等到了还在跑 → 退出码 124，**状态未知，不是失败**。
- 三个超时仍然分开：连不上 → 125；job 路径上的执行超时杀掉进程树 → 状态 `TIMEOUT`、退出码 124；客户端等不及 → 124，任务还在跑。同步 `/run` 的 124 是执行超时，被启动的那个进程已经结束。

`wlt job start` 同样可以带 `--cwd`、`--env`、`--exe`、`--request-id`，另外还有原来的 `--resource` 和 `--wait`。

## 窗口关闭

```bash
wlt close --title "记事本"
wlt close --hwnd 123456
```

与 `wlt focus` 一样，按标题子串或十进制 hwnd 找窗口；两个都给时 hwnd 优先。代理 `PostMessage(WM_CLOSE)`，不等进程退出，窗口也可以拒绝关闭。要确认窗口没了，再跑 `wlt windows`。这不是 `wlt kill`。

## 进程

`wlt windows` 列的是可见窗口（hwnd、pid、进程名、标题）。参考项目里的 `wl ps` 列的也是窗口，所以这边没有把进程命令叫成 `ps`，避免和 `wlt windows` 撞在一起。

```bash
wlt proc                         # 进程表，* 标出 wlt agent
wlt proc --name notepad
wlt proc --pid 4321 --json
wlt kill 4321                    # 只杀这个 pid 的进程树
```

`wlt kill` 调用 `taskkill /F /T /PID`，只传这一个 pid，不用镜像名。下面这些会直接拒绝（HTTP 403），不会真正去杀：

- agent 自己的 pid，以及它的祖先（否则整棵树会把 agent 一起带走）
- 进程树里包含 agent，或包含系统关键进程（System、csrss、lsass、services、winlogon 等）
- pid 0–4，或快照里看不见的 pid（无法证明这棵树是安全的）

不提权。杀不掉别的用户的进程时返回 `KILL_FAILED`。杀掉隧道的 `ssh.exe` 会让控制隧道闪断，看门狗会再拉起来；agent 进程本身不受影响。

## 日志

```bash
wlt log                                # agent + 两条隧道日志，各最近 120 行
wlt log --source agent --lines 50
wlt log --source control --json
```

每份日志最多 400 行、32 KiB，并且只读文件尾部（读窗口 64 KiB），不会把整份日志拉回来。输出里的 Bearer 令牌、`token` / `password` / `secret` 赋值、私钥块和 SSH 公钥材料会换成 `***`。令牌文件本身不会被读取。缺的隧道日志会标成 missing（隧道还没拉起来时是正常的）。

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
| `wlt status` 控制隧道 DOWN | Windows 代理没起：双击 `win\start-win.vbs`；看 `%USERPROFILE%\.wlt\logs\` |
| 隧道 `remote port forwarding failed for listen port N` | Linux 侧旧 sshd 会话占着端口：`ss -tlnp \| grep :N`，等回收或杀掉 |
| `authentication refused` | 隧道钥未授权：重跑 `install-win.ps1` |
| `host key mismatch` | 删 `~/.ssh/known_hosts` 里过期条目再试 |
| 截图很小/全黑 | Windows 锁屏了；需解锁会话 |
| 重启后桥没恢复 | 自启在登录时触发，需有人登录 |
| 规则状态 DRIFT | `wlt tunnel sync` 重新对齐 |
| 想看 Windows 侧最近发生了什么 | `wlt log`（只拉尾部，令牌和密钥会打码） |
| `wlt kill` 返回 403 | 目标是 agent、它的祖先，或系统关键进程；换一个具体的子进程 pid |

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