# wlt — AI 代理操作手册

写给在 Linux 上驱动 Windows 桌面的自动化脚本与 AI 代理。核心原则：**桥接调用成功，
不代表程序自己的测试通过**；判定永远按 `transport_ok → state → error_code → exit_code`。

## 退出码语义（务必区分）

| 码 | 含义 | 你该怎么做 |
|---|---|---|
| 0 | 任务成功 | 继续 |
| 1 | 程序跑完但返回非零 | 真实的业务/测试失败，读输出定位 |
| 124 | 客户端等待超时 / 执行超时 | **状态未知，不是失败**；用 `job status`/`result` 查真实状态 |
| 125 | 桥接不可达 | Windows 侧没起或隧道断；`wlt status` 排查 |
| 126 | 程序无法启动 / 任务 LOST | 检查 exe 路径；LOST 表示代理重启丢了进程 |
| 130 | 任务被取消 | — |

**绝不把 124 当成失败重试**——程序可能还在跑。先查状态。

## 幂等重试（烧板子不会烧两遍）

每条命令带 `--request-id`，相同 id 返回原任务而非新建：

```bash
wlt job start --request-id flash-20261125-001 --resource uart0 --timeout 300 \
  -- "C:\tools\flash.exe" --port COM3 --image fw.bin

# 等价入口：带 --request-id 的 wlt run 走同一个 job 引擎，不会另起语义
wlt run --request-id flash-20261125-001 --timeout 300 --cwd C:\fw \
  --env MODE=lab --no-wait -- C:\tools\flash.exe --port COM3
```

网络抖断后重发同一 `--request-id`，拿回的是同一个 job，不会重复烧录。
id 只能是 1–128 位字母、数字、`.`、`_`、`-`（Windows 上它是一个文件名）。

`wlt run` **不带** `--request-id` / `--no-wait` / `--wait` 时仍是同步 `POST /run`。
这条路径同样接受 `--cwd`、可重复的 `--env K=V`、以及 `--exe PROGRAM -- argv...`
（直接启动，不再拼进 cmd）。它不会按 request id 去重。这条路径上的 124 表示执行超时，
被启动的那个进程已经结束。

## 三个超时严格分离

- **连接**：连不上代理 → 125（连接失败立刻返回，不是执行超时）。
- **执行**（`--timeout`，默认 120s）：job 路径杀掉进程树 → `TIMEOUT` / `TIMEOUT_KILLED`。同步 `wlt run` 只结束它启动的那个进程。
- **客户端等待**（job 的 `--wait`；不写时是执行超时 + `WLT_WAIT_MARGIN`，默认 30s）：等不到 → 124，任务仍在跑。

## 持久化任务与有界输出

任务落盘在 Windows `%USERPROFILE%\.wlt\jobs\<id>\`，断连/重启后可查。
完整输出流式落盘（32MB 上限），API 只回**有界摘要 + 字节数 + SHA-256**，尊重上下文窗口：

```bash
wlt job result <id>                 # 摘要 + sha256
wlt job logs <id> --since 0         # 按字节游标增量读
wlt job logs <id> --since 4096      # 接着上次读
wlt job status <id> --wait 60       # 阻塞等终态
wlt job cancel <id>                 # 只杀该任务自己的进程树
```

厂商工具的多余字段原样保留——桥接绝不改写工具结果。

## 资源互斥与批处理

共享 `--resource <key>` 的任务自动串行（QUEUED 排队），避免抢占同一串口/烧录器：

```bash
wlt batch --file plan.json
```

`plan.json`：`{"steps": [{"cmd": "...", "resource": "uart0", "timeout": 120}, ...]}`，
顺序执行，遇失败即停。

## 桌面操作

```bash
wlt shot --monitor 0 --out screen.png   # 多显示器截图
wlt windows --json                      # 窗口含 PID + 进程名 + 矩形，便于定位
wlt focus --title "厂商上位机"
wlt close --title "厂商上位机"           # WM_CLOSE；窗口可以拒绝，这不是杀进程
wlt wait-window "烧录完成" --timeout 120  # 轮询等待
wlt proc --name flash --json            # 进程表（不是窗口列表）
wlt kill 4321                           # 只结束这个 pid 的进程树
wlt click 500 300 ; wlt type "hello" ; wlt key ctrl+s
wlt copy                                # 读剪贴板
wlt http http://127.0.0.1:9000/api      # 访问 Windows 本机服务
wlt log --source agent --lines 80      # 排障；尾部有界，令牌会打码
```

截图在锁屏时会是黑的；图形操作要求会话解锁。

`wlt proc` 是进程表。参考项目的 `wl ps` 列的是窗口，那一份能力在这里是 `wlt windows`。
`wlt kill` 拒绝 agent 自身、它的祖先、以及树里含有 agent 或系统关键进程的目标；
403 是政策拒绝，不要换一条更宽的命令再试。需要停掉自己启动的任务时，优先
`wlt job cancel <id>`（只杀该任务自己的进程树）。

## 隧道管理（让 Windows 访问 Linux）

```bash
wlt tunnel add L 3080 127.0.0.1:3080    # Windows 监听 3080 → Linux 3080
wlt tunnel add R 19000 127.0.0.1:9000   # Linux 监听 19000 → Windows 9000
wlt tunnel list                         # 每条规则状态
wlt tunnel probe <id>                   # 端到端验证
wlt tunnel off|on|rm <id>
```

规则即时生效、断线自动重连。`up` 表示监听口已建立；`-L` 目标拒绝发生在实际访问时，
用 `probe` 做端到端确认。

## 典型工作流（远程烧录）

```bash
wlt tunnel add L 3080 127.0.0.1:3080          # 如需访问 Linux 侧服务
wlt put fw.bin "C:\\fw\\fw.bin"                # 传固件
wlt job start --request-id flash-001 --resource uart0 --timeout 300 \
  -- "C:\\tools\\flash.exe" --port COM3 --image C:\\fw\\fw.bin
wlt job status <id> --wait 300                 # 等终态
wlt job result <id>                            # 读摘要判定
wlt shot --out after.png                       # 留证
```

## 文件传输

```bash
wlt put local.bin "C:\\path\\remote.bin"   # Linux → Windows（白名单内）
wlt get "C:\\path\\remote.bin" local.bin   # Windows → Linux
wlt ls "C:\\path" ; wlt rm "C:\\path\\tmp" --recursive
```

越出白名单（交付目录 + 用户目录）返回 403。