# wlx 设计方案 —— 轻量级 Windows↔Linux 双向桥

> **状态：已确认，开发中。** 仓库：<https://github.com/zhooxc/wlx>（public，main）。
> 仿照 [Fatmear/WinLinuxBridge](https://github.com/Fatmear/WinLinuxBridge)（MIT，参考源码在
> 工作区 `refs/WinLinuxBridge`，v1.4.1）重新实现。借用代码归属见 NOTICE。
>
> 两个能力：
> 1. **控制面**：在 Linux 终端操作 Windows 桌面（原工程能力全集 + 补充优化）。
> 2. **隧道面（新增）**：任意方向、规则化的 SSH 隧道 —— 尤其是让 Windows 访问 Linux 服务
>    （`-L`，对应参考截图 `-L 127.0.0.1:3080 -> 127.0.0.1:3080` + 自动重连）。
>
> 已确认决策（2026-11-25）：名字 wlx；**要 GUI（中文、用户友好）**；桌面能力按原工程全集
> 并补充优化；**发布包目标机零依赖**（随包便携 CPython + Tcl/Tk）。

---

## 1. 与原工程的对比

| 维度 | WinLinuxBridge v1.4.1 | wlx |
|---|---|---|
| 隧道 | 硬编码单条 `-R 18080→8787` | 控制隧道 + **任意条 `-L`/`-R` 用户隧道**，规则化、热生效、自动重连 |
| Windows 常驻 | PS supervisor + Python agent + sync.ps1 | **单个 Python 进程**（HTTP + job 引擎 + 隧道看门狗线程） |
| Linux CLI | bash `wl` + `wl_job.py`，依赖 bash+curl+python3 | **单文件 Python3**（urllib 标准库），只依赖 python3 |
| GUI | Tk 配置界面（英文混排） | **Tk 中文界面**：安装向导 + 隧道面板（仿参考截图）+ 状态页 |
| 一次性密码登录 | 捆绑 plink 0.77 | **系统 ssh.exe 交互输密码**（或手动贴公钥），零额外二进制 |
| 文件同步 | 三条车道 30s 全量比对 + 程序自刷新 | 砍掉。按需 `get`/`put`（走控制隧道，即时） |
| 运行时 | 便携 CPython 3.13 + Tk + plink | 便携 CPython 3.13 + Tk（GUI 需要），**无 plink** |
| 默认端口 | agent 8787 / 隧道 18080 | **agent 8788 / 隧道 18081**（刻意错开，可与原工程共存） |
| 代码量 | ~6300 行 | 目标 ~4000 行（含 GUI） |
| 任务语义 | 持久化 job / 去重 / 三超时 / 有界输出 / LOST | **全部继承** |
| 自启 | 启动文件夹 + 计划任务 watchdog | 启动文件夹 vbs（进程自愈，v1 不建计划任务） |

**继承不动的设计**（原工程 81 项验证打磨过）：
- Windows 主动外连，两端零入站端口、零管理员、零系统安装。
- 任务落盘 `%USERPROFILE%\.wlx\jobs\<id>\`，断连/重启后可查；`request_id` 幂等重试。
- 三超时分离：连接 5s（exit 125）/ 执行 120s（TIMEOUT_KILLED）/ 客户端等待（exit 124 = 状态未知，**绝不谎报失败**）。
- 输出落盘 32MB 上限，对外只回有界摘要 + 字节数 + SHA-256，`--since` 游标增量读。
- 判定顺序 `transport_ok → state → error_code → exit_code`。
- 截图 GDI + zlib 纯标准库 PNG；文件白名单越界 403；Bearer 令牌。

## 2. 总体架构

```
 Linux 主机（sshd 即服务端，无守护进程）              Windows 桌面（用户态，无管理员）
 ─────────────────────────────────────              ──────────────────────────────────
 ~/.wlx/                                            %USERPROFILE%\.wlx\
 ├── tunnels.json   规则真相源(0600)                ├── wlx.conf / tunnels.json(镜像)
 ├── bridge.env     令牌+端口(0600)                 ├── agent.token (ACL)
 └── authorized_keys 动态选项                       ├── keys\  两把 ed25519 私钥
                                                    └── jobs\  持久化任务
        ▲
        │ ①控制隧道（固定，独立 ssh 进程）
        │   ssh -N -R 127.0.0.1:18081:127.0.0.1:8788
        │
 wlx run/job/shot/... ──► 127.0.0.1:18081 ═══════► 127.0.0.1:8788  wlx_agent.py
        │                                            │  ├ HTTP 线程（Bearer 鉴权）
        │ ②用户隧道（独立 ssh 进程，规则驱动）        │  ├ job 引擎（subprocess/Win32）
        │   ssh -N -L 127.0.0.1:3080:127.0.0.1:3080  │  ├ 隧道管理线程（spawn/看门狗/退避）
        │        -R 127.0.0.1:19000:127.0.0.1:9000   │  └ 桌面/文件/clipboard（ctypes）
        │                                            │
        │                          wlx_gui.pyw ──────┤  （GUI 是 agent 的本地 HTTP 客户端，
        │                           Tk 中文界面       │    独立进程，崩了不影响桥）
 Windows 应用 ──► 127.0.0.1:3080 ══②-L══════════► Linux 127.0.0.1:3080 服务
 Linux 应用  ──► 127.0.0.1:19000 ══②-R══════════► Windows 127.0.0.1:9000 服务
```

**为什么控制隧道和用户隧道分成两个 ssh 进程：**
`ExitOnForwardFailure=yes` 下任何一条 `-R` 绑不上（端口被死会话占着等）整个连接立即退出。
混在一条连接里，一条坏用户规则会把控制面一起打死，你就再也连不上去修它了。
分开后：用户隧道全挂也不影响 `wlx tunnel rm` 远程排障。代价只是多一个 ssh 进程。

**为什么规则变更 = 重启用户隧道进程，而不是 ssh 控制套接字热加：**
Win32-OpenSSH 的 ControlMaster 多路复用不可靠（无 fd 传递），`ssh -O forward` 不保证可用。
重启进程有 1–2 秒闪断，但 job 持久化、规则落盘，闪断不丢状态。

**为什么 GUI 是独立进程而不是 agent 内嵌：**
Tk 主循环和 HTTP/看门狗线程互相拖累；GUI 崩溃不能带崩桥。GUI 只通过
`127.0.0.1:8788` 的 HTTP 接口读写状态与规则（带令牌），天然解耦。

## 3. 隧道规则模型（新增核心）

### 3.1 语义（与参考截图一致）

> 谁要发起访问，就在谁那边开监听口。

| 方向 | 监听在 | 目标解析自 | 用途 | sshd 侧约束 |
|---|---|---|---|---|
| `L` | Windows | Linux | **Windows 访问 Linux 服务** | `permitopen="target"` |
| `R` | Linux | Windows | Linux 访问任意 Windows 服务 | `permitlisten="listen"` |

### 3.2 规则存储

Linux `~/.wlx/tunnels.json` 是**唯一真相源**（Linux 是 sshd 属主，authorized_keys 在它手里）；
Windows 持镜像，supervisor 启动时及周期性用管理密钥拉取核对（hash 不一致即自愈）。

```json
{
  "version": 1,
  "rules": [
    {"id": "web3080", "dir": "L", "listen": "127.0.0.1:3080",
     "target": "127.0.0.1:3080", "enabled": true, "note": "Linux 上的 web 服务"},
    {"id": "svc9000", "dir": "R", "listen": "127.0.0.1:19000",
     "target": "127.0.0.1:9000", "enabled": true, "note": "Windows 厂商上位机"}
  ]
}
```

约束：
- 默认只允许回环绑定。`-L` 绑 `0.0.0.0` 需显式 `--lan`（客户端行为）；
  `-R` 绑非回环受 sshd `GatewayPorts` 限制（默认 no 会静默改绑回环），doctor 明示。
- 控制端口 18081 保留；同方向监听端口冲突在 add 时校验拒绝。
- `enabled:false` = 规则保留、隧道摘除（截图勾选框的反面）。
- 每次规则变更追加审计记录 `~/.wlx/tunnels.audit.jsonl`（时间/动作/规则/来源 cli|gui）。

### 3.3 CLI

```
wlx tunnel add L 3080 127.0.0.1:3080 [--note ...] [--lan] [--id ...]
wlx tunnel add R 19000 127.0.0.1:9000
wlx tunnel list            # id 方向 监听→目标 状态(up/down/err) 最后错误
wlx tunnel on|off <id>     # 启用/停用（热生效）
wlx tunnel rm <id>
wlx tunnel probe <id>      # 主动打一次目标，端到端验证
wlx tunnel sync            # 强制推送真相源并核对 authorized_keys
wlx tunnel import          # 从 stdin 收完整规则集（Windows 侧经管理密钥调用）
```

### 3.4 热生效流程

**Linux 侧发起（CLI）：**
```
wlx tunnel add L 3080 ...
  1. 校验 → 写 ~/.wlx/tunnels.json + 审计
  2. 本地重写 authorized_keys 隧道钥选项（§4，零网络往返）
  3. POST /tunnels/apply {rules, hash} ──控制隧道──► agent
  4. agent 写镜像 → 重启用户隧道 ssh 进程 → 1.2s settle 验活 → 逐条回报状态
```

**Windows 侧发起（GUI 隧道面板）：**
```
GUI 编辑规则 → POST /tunnels/apply {rules}
  1. agent 经管理密钥 ssh 到 Linux：wlx tunnel import（stdin 传 JSON）
     → Linux 校验 + 写真相源 + 重写 authorized_keys（校验逻辑只此一份）
  2. agent 写镜像 → 重启用户隧道 ssh → settle 验活 → 回报 GUI
```

自动重连：隧道管理线程 10s tick 巡检 + 周期健康探测；失败 10s→20s→40s→…→120s
指数退避（继承原工程防重试风暴），恢复清零。每条规则状态独立可见。

> ssh `-L` 是懒连接：监听口 bind 成功即 up，目标拒绝发生在实际访问时。
> `tunnel list` 的 up = 监听口已建立；`tunnel probe` 做端到端验证。

## 4. 安全模型

### 4.1 两把专用 ed25519 密钥

| 密钥 | authorized_keys 选项 | 用途 |
|---|---|---|
| 隧道钥 | `restrict,port-forwarding,permitlisten=...,permitopen=...`（**动态**） | 两个 ssh 隧道进程；无 shell、无 pty |
| 管理钥 | `no-pty,no-port-forwarding,no-agent-forwarding,no-x11-forwarding` | 安装上传、规则 import/拉取；可执行命令、不可转发 |

### 4.2 动态 authorized_keys（关键机制）

规则变更时由 Linux 侧重写隧道钥那一行，**两个列表永远成对生成**：

```
restrict,port-forwarding,permitlisten="127.0.0.1:18081",permitlisten="127.0.0.1:19000",permitopen="127.0.0.1:3080",permitopen="127.0.0.1:9" ssh-ed25519 AAAA... wlx-tunnel
```

已查证的 OpenSSH 硬约束：
- `permitopen` 只约束 `-L` 目标；`permitlisten` 只约束 `-R` 监听。
- `restrict,port-forwarding` 同时放开两方向：**只写一个列表，另一方向完全不设防**。
  无 `-L` 规则时放哑元 `permitopen="127.0.0.1:9"`（discard）封死。
- `restrict` 必须写在最前（反序实测失效）。
- 两选项均支持多条目枚举；不依赖通配符（permitlisten 模式支持随版本变化）。
- 控制端口 18081 永远在 permitlisten 列表。

**泄露爆炸半径**：隧道钥泄露 = 只能打开已配置的回环端口，无 shell；
管理钥泄露 = 可跑命令（与原 sync 钥同级）但开不了转发。
令牌继承原工程：安装期生成，Windows 用户环境变量 + ACL 文件，Linux 0600。
Linux 密码只安装期交互输入一次，不落盘不进日志（v1 不做记住密码）。

### 4.3 其余继承项

- agent 只监听 127.0.0.1；文件白名单（交付目录 + %USERPROFILE%）越界 403。
- doctor 不含凭据。
- 可选扩展（默认关）：`-D` SOCKS 需 `permitopen="*"`，必须显式 `--unsafe-socks`，list 高亮。

## 5. 桌面能力：原工程全集 + 补充优化

**全集（继承）**：结构化执行 `exe+argv[]+cwd+env`、PowerShell/cmd、持久化 job/batch、
截图、鼠标点击/移动、键盘输入/组合键、剪贴板读写、窗口列举/置前/关闭、
`http` 代理访问 Windows 本机服务、文件 ls/get/put/rm（白名单）、`open` 打开路径/URL。

**补充优化（新增）**：
| 项 | 说明 |
|---|---|
| 多显示器截图 | `shot --monitor N`（按虚拟屏坐标枚举）、`--region x,y,w,h`、`--window <hwnd>` |
| 窗口信息增强 | `windows` 返回 PID + 进程名 + 矩形，AI 代理定位窗口更准 |
| 鼠标补全 | 滚轮 `scroll`、双击 `--double`、拖拽 `drag x1,y1→x2,y2` |
| 等待原语 | `wait-window --title <substr> --timeout N`（自动化轮询窗口出现） |
| 隧道 probe | 每条规则端到端验证（§3.3） |
| 规则审计 | tunnels.audit.jsonl |
| doctor 增强 | ssh 版本、端口冲突检测、GatewayPorts 提示、规则 hash 漂移检测 |
| 共存 | 默认端口错开原工程，互不干扰 |

## 6. 组件与代码预算

```
wlx/
├── win/
│   ├── wlx_agent.py      ~900 行  HTTP/鉴权 + 桌面(ctypes) + 文件 + http 代理 + 隧道管理线程
│   ├── wlx_jobs.py       ~700 行  持久化 job 引擎（状态机/去重/进程树取消/限额/游标/batch）
│   ├── wlx_gui.pyw       ~800 行  Tk 中文：安装向导 / 隧道面板 / 状态页 / 日志尾随
│   ├── install-win.ps1   ~250 行  键生成→交互 ssh 装公钥→上传→令牌→自启
│   ├── start-win.vbs / start-gui.vbs
│   └── wlx.conf.example
├── linux/
│   ├── wlx               ~1100 行 单文件 Python3：CLI + job 客户端 + 规则管理 + authorized_keys 重写
│   └── install-linux.sh  ~70 行
├── tools/
│   ├── build-runtime.ps1          官方 embeddable Python + 补 Tcl/Tk（借原工程思路，无 plink）
│   ├── package_release.py
│   └── verify.sh / verify.ps1     冒烟 + 安全负向测试
├── docs/  README / USAGE / AI-AGENT
└── DESIGN.md / NOTICE / LICENSE / VERSION / CHANGELOG.md
```

发布包：源码 + 便携 CPython 3.13（含 Tk）≈ 25–35MB，目标机零依赖。
构建脚本从官方源下载 embeddable 包并补 Tcl/Tk 与 `_tkinter.pyd`（embeddable 故意不含），
用真实 `Tk()` 窗口自检（继承原工程 build-runtime 的做法）。

## 7. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | 骨架 + 控制隧道 + ping/run + CLI 安装流 | 两端一次装通，`wlx run` 往返 |
| M1 | job 引擎 + 桌面全集 + 文件/http + 补充项 | 原工程核心语义逐条对齐 |
| M2 | **隧道面**：规则/动态 authorized_keys/热应用/自动重连/probe | 截图场景复现；杀 ssh 自愈；坏规则不伤控制面 |
| M3 | **GUI**：安装向导 + 隧道面板 + 状态页 | 非技术用户全程鼠标装通、加隧道 |
| M4 | 加固 + 验证套件 + 文档 + 零依赖发布包 | 负向测试全过；全新机器离线装通 |

## 8. 风险与已知限制

- **死会话占端口**：Linux 旧 sshd 会话未回收时 `-R` 被拒（原工程真实踩过）。继承错误翻译 + 退避；list 给 `ss -tlnp` 提示。
- **锁屏黑截图**、**重启需登录才恢复**：物理限制，文档明示。
- **Windows OpenSSH 版本**：目标 Win10 1809+；`ssh -V` 进 doctor。
- **规则双写一致性**：真相源 + 镜像 + hash 核对自愈；`tunnel sync` 兜底。
- **无本地 Linux 测试环境**：开发机无 WSL/docker，端到端验证需要一台 Linux 测试机（待用户提供）。

## 9. 已确认决策记录

1. 名字 **wlx**；仓库 github.com/zhooxc/wlx（public），边开发边推送，不携带原工程 git 历史。
2. **GUI 必须有**，中文、用户友好；Tk（随包 Tcl/Tk），M3 交付，v1 范围内。
3. 桌面能力**原工程全集 + §5 补充项**。
4. 发布包**目标机零依赖**：随包便携 CPython + Tk；无 plink（交互 ssh 代替）。
5. 许可 MIT + NOTICE 归属；默认端口 8788/18081 与原工程共存。
6. 开发纪律：不提交任何凭据/主机名/账号；shell 脚本 LF；每个可用增量即 commit + push。