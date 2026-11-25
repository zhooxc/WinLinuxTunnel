# wlt — AI 代理操作手册

写给在 Linux 上驱动 Windows 桌面的自动化脚本与 AI 代理。核心原则：**桥接调用成功，
不代表程序自己的测试通过**；判定永远按 `transport_ok → state → error_code → exit_code`。

## 退出码语义（务必区分）

| 码 | 含义 | 你该怎么做 |
|---|---|---|
| 0 | 任务成功 | 继续 |
| 1 | 程序跑完但返回非零 | 真实的业务/测试失败，读输出定位 |
| 124 | 客户端等待超时 / 执行超时 | **状态未知，不是失败**；用 `job status`/`result` 查真实状态 |
| 125 | 桥接不可达 | Windows 侧没起或隧道断。打开 WinLinuxTunnel（窗口会启动代理），再 `wlt status` |
| 126 | 程序无法启动 / 任务 LOST | 检查 exe 路径；LOST 表示代理重启丢了进程 |
| 130 | 任务被取消 | — |

**绝不把 124 当成失败重试**——程序可能还在跑。先查状态。

## 幂等重试（烧板子不会烧两遍）

每条命令带 `--request-id`，相同 id 返回原任务而非新建：

```bash
wlt job start --request-id flash-20261125-001 --resource uart0 --timeout 300 \
  -- "C:\tools\flash.exe" --port COM3 --image fw.bin
```

网络抖断后重发同一 `--request-id`，拿回的是同一个 job，不会重复烧录。

## 三个超时严格分离

- **连接**（`WLT_CONNECT_TIMEOUT`，5s）：够不到代理 → 125。
- **执行**（`--timeout`，120s）：Windows 代理杀进程树 → `TIMEOUT` / `TIMEOUT_KILLED`。
- **客户端等待**（`--wait`）：等不到 → 124，任务仍在跑。

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
wlt wait-window "烧录完成" --timeout 120  # 轮询等待
wlt click 500 300 ; wlt type "hello" ; wlt key ctrl+s
wlt copy                                # 读剪贴板
wlt http http://127.0.0.1:9000/api      # 访问 Windows 本机服务
```

截图在锁屏时会是黑的；图形操作要求会话解锁。

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