# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-11-25

### 变更
- **Windows 只面对一个程序 `WinLinuxTunnel.exe`**。双击打开窗口和托盘；同一个文件加
  `--agent` 启动常驻代理（无控制台）。窗口发现代理没在跑时会自己把它拉起来
  （开发模式下用当前解释器执行 `wlt_main.py --agent`，不新开控制台）。
  代理仍是独立进程，窗口崩溃不会带走它。单实例锁行为不变。
- **安装方式改为 onedir + 单文件安装器**。`tools\build-windows.ps1` 用 PyInstaller
  打出 `dist\WinLinuxTunnel\`（常驻目录版，不是每次启动都解压的 onefile），再打出
  `dist\WinLinuxTunnel-Setup.exe`。安装器把目录复制到 `%LOCALAPPDATA%\WinLinuxTunnel\`，
  并创建开始菜单、桌面和「启动」文件夹快捷方式。目标机仍然不需要管理员、Python 或 plink。
- **自启不再注册 vbs/cmd**。`install-win.ps1` 写入 `WinLinuxTunnel.exe --agent` 的快捷方式
  （源码运行时则是 `pythonw wlt_main.py --agent`），并删掉旧的 `wlt.vbs`。
- **设置页承担首次配置**：填写 Linux 地址和账号后运行安装向导。密码仍在弹出的
  ssh 控制台里输入一次。向导结束后会让旧代理退出，再按新配置启动。
- `start-gui.vbs` / `start-win.vbs` / `start-win.cmd` 降为开发辅助，文档不再把它们当作用户入口。
- `tools/build-runtime.ps1`（embeddable CPython + 手工补 Tcl/Tk）不再是发布路径。
- 新增 `POST /shutdown`（仍要 Bearer，只监听 127.0.0.1），供安装向导干净地重启代理。

### 说明
- 本仓库在 Linux 上不产出 `.exe`。Windows 开发者按 README / USAGE 里的命令构建即可。

## [0.2.2] - 2026-11-25

### 优化
- **任务栏/托盘图标不再发糊**：ICO 从单张 256px 改为**多分辨率**
  （16/24/32/48/64/128/256），每个尺寸原生渲染 + 超采样抗锯齿，
  Windows 任务栏/标题栏直接取对应小尺寸，清晰锐利。

## [0.2.1] - 2026-11-25

### 修复（界面卡顿）
- **GUI 所有网络 I/O 移到后台线程**：结果经线程安全队列回主线程更新 UI
  （从工作线程直接调 `root.after` 在 Tk 中不可靠，改为队列 + 主线程轮询）。
  刷新调用现在 0.45ms 返回，界面不再因 SSH 往返 / 代理等待而冻结。
- **代理看门狗改为事件驱动**：规则 apply 立即唤醒看门狗，不再等待整个 tick
  周期。`/tunnels/apply` 延迟从最长 ~10s 降到 ~0.1–1.2s。
- 统一版本号（代理 / CLI / VERSION 文件）。

## [0.2.0] - 2026-11-25

### 变更
- **项目更名**：仓库 `wlx` → `WinLinuxTunnel`，命令行 `wlx` → `wlt`；
  全量重构所有标识符（`WLT_` 环境变量、`.wlt` 目录、`~/wlt`、模块文件名）。
  旧 GitHub 地址自动重定向。
- **GUI 重做为正规 Windows 桌面软件**：
  - 系统托盘（纯 ctypes Shell_NotifyIcon，零依赖）：关闭默认最小化到通知区域，
    双击恢复，右键菜单（显示/退出）；正常最小化到任务栏。
  - 应用图标（蓝底双向箭头）；仪表盘式布局：左侧导航（概览/隧道/桌面/日志/设置）
    + 状态卡片 + 连接状态指示。
  - 新增「桌面」页：截图预览/保存、窗口列表、剪贴板读取。
  - 隧道页状态着色（up 绿/down 红/off 灰），更友好的规则编辑对话框。

### 新增
- `win/wlt_tray.py`：零依赖系统托盘模块。
- `tools/make_icon.py`：纯标准库渲染应用图标（PNG-in-ICO）。

## [0.1.0] - 2026-11-25

首个版本。仿照 [WinLinuxBridge](https://github.com/Fatmear/WinLinuxBridge)（MIT）
重新实现的轻量级 Windows↔Linux 双向桥（归属声明见 NOTICE）。

### 新增
- **控制面**：Linux 终端操作 Windows 桌面。
  - 持久化 job 引擎：request_id 幂等去重、执行超时杀进程树、32MB 实时输出限额、
    字节游标日志、资源键互斥、代理重启标记 LOST、7 天保留。
  - 桌面全集：GDI 截图（+多显示器/区域/窗口）、SendInput 鼠标键盘（+拖拽/滚轮）、
    窗口列举（+进程名）/置前/最小化/最大化/关闭、wait-window、剪贴板、本机 HTTP 代理、
    白名单文件操作。
  - 单文件 Python3 CLI（urllib，无 curl 依赖）：run/job/batch/shot/input/windows/
    clipboard/http/fs/tunnel 全命令集。
- **隧道面（新能力）**：规则驱动的任意双向 SSH 隧道。
  - `-L`（Windows 访问 Linux）/`-R`（Linux 访问 Windows），热生效、自动重连（指数退避）、
    每条规则独立状态、probe 端到端探测、规则审计。
  - Linux 为规则真相源，动态生成 authorized_keys 的 permitopen/permitlisten 白名单。
  - 控制隧道与用户隧道分离为两个 ssh 进程，坏规则不伤控制面。
- **中文图形界面**（Tk）：状态 / 隧道面板（增删改查+勾选+探测）/ 日志 / 配置安装向导。
- **零依赖发布包**：随包便携 CPython 3.13 + Tcl/Tk（build-runtime.ps1 构建并自检），
  无 plink（系统 ssh 交互输一次密码代替）。
- 安装器（install-win.ps1 + install-linux.sh）、崩溃重启启动器、当前用户自启。
- 文档：README / DESIGN / USAGE / AI-AGENT。

### 修复（开发期验证抓到）
- cancel 竞态：runner 不再用 FAILED 覆盖已 CANCELLED 的任务。
- 剪贴板 x64 崩溃：GlobalLock/GlobalAlloc 指针 restype 截断导致进程硬崩溃。
- embeddable 运行时 sys.path：`._pth` 不含脚本目录，代理显式插入以导入兄弟模块。
- 保留控制端口校验：分离连接端口与控制端口身份。

### 安全
- 双专用 ed25519 密钥；restrict 在前、permitopen/permitlisten 成对生成（无 -L 规则时
  哑元封死该方向）；Bearer 令牌；文件白名单越界 403；密码不落盘。

### 验证
- 本地沙箱：控制面全命令、job 语义（去重/超时/取消/游标）、桌面（截图 PNG 有效/
  多显示器/窗口/剪贴板往返）、文件往返、白名单 403、隧道规则校验与 ak 生成、
  GUI 构造、零依赖运行时全栈（代理+CLI+GUI 仅用随包运行时）。
- 待办：真实 Linux 主机的 SSH 隧道端到端验证（需测试机）。

[0.1.0]: https://github.com/zhooxc/WinLinuxTunnel/releases/tag/v0.1.0