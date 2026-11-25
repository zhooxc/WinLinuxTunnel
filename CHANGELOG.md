# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[0.1.0]: https://github.com/zhooxc/wlx/releases/tag/v0.1.0