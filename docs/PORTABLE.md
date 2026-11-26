# 便携式运行时包（portable）

WinLinuxTunnel 的第二种发布形态，与 exe 安装版**同源同功能**，只是运行时不同：

| 形态 | 运行时 | 入口 | 适用 |
|---|---|---|---|
| exe 版（Setup.exe / onedir） | PyInstaller 打包 | `WinLinuxTunnel.exe` | 常规机器，推荐 |
| 便携式运行时包 | 随包 python.org embeddable CPython + Tk | `win\start-gui-portable.vbs` | 企业终端白名单拦截打包 exe 的机器；偏好绿色免安装 |

部分企业终端管理软件按白名单放行程序，PyInstaller 产物（内嵌 Python 的
打包 exe）可能被拦（表现为双击无反应 / 提示"根据管理规定禁止运行"）。
便携包用的是 **python.org 官方签名的解释器**，通常不受该策略影响。

## 用户使用

1. 从 [Releases](https://github.com/zhooxc/WinLinuxTunnel/releases/latest)
   下载 `WinLinuxTunnel-<版本>-portable.zip`，解压到任意目录。
2. 双击 `win\start-gui-portable.vbs`：打开中文 GUI + 系统托盘。
   代理由 GUI 自动拉起，无需手动启动；关闭到托盘、双击恢复与 exe 版一致。
3. 「设置」页填写 Linux 地址 / 账号 → 运行安装向导 → 弹出的 ssh 控制台里
   输入一次密码。其余操作与 exe 版完全相同（见 [USAGE.md](USAGE.md)）。

只跑代理（无窗口）：`win\start-agent-portable.cmd`（崩溃自动重启循环）。

目标机不需要安装 Python、pip 或 plink；配置与状态仍在 `%USERPROFILE%\.wlt\`。

## 开发者构建

在一台装有 python.org CPython（3.8+，带 Tcl/Tk）的 Windows 上：

```
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
python tools\package_portable.py
```

- `build-runtime.ps1`：下载与本机 CPython 同版本的 embeddable 包，嫁接
  Tcl/Tk 与 `_tkinter`，写 `._pth`，并跑真实 Tk 窗口自检。产物在 `win\runtime\`。
- `package_portable.py`：把源码（win/linux/tools/docs/tests）+ `win\runtime\`
  打成 `dist\WinLinuxTunnel-<版本>-portable.zip`。复用 `wlt_payload` 的
  排除规则：密钥、令牌、本地状态、`__pycache__` 一律不进包。

注意：embeddable 的 `._pth` 不会把脚本目录加入 `sys.path`，
`wlt_main.py` / `wlt_gui.py` 已自行处理；Tk 需要 `TCL_LIBRARY`/`TK_LIBRARY`，
GUI 启动时会按 `win\runtime` 布局自动设置。

## 许可

随包解释器与 Tcl/Tk 来自 python.org 官方发行（PSF 许可），
许可证文本在 `win\runtime\LICENSE-Python.txt`。详见 [NOTICE](../NOTICE)。