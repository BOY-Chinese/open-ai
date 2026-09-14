# 开发与验证工具

本目录脚本用于构建、启动与视觉回归，均需在 **Windows 侧** 执行（Tauri 与 WebView2 目标为 Windows）。

| 脚本 | 用途 | 前置条件 |
|---|---|---|
| `screenshot.mjs` | 六页面 + 周视图截图自检 | 依赖装在 `tools/node_modules_tools/`（见下）；dev server 在跑 |
| `verify-ui.mjs` | **DOM 文本断言自检（18 项）**，比截图可靠 | 同上（puppeteer + dev server） |
| `verify-packaged.mjs` | **直连真实打包应用**（WebView2 CDP）做 DOM 断言与点击流程；`--uninstall` 额外验证一键卸载 | 应用需带 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222` 启动 |
| `build-tauri.ps1` | 编译 Tauri Rust 后端（debug） | 独立工具链 `open-ai/toolchain/`（见下） |
| `build-tauri-release.ps1` | **生产构建**：`TAURI_ENV_DEBUG=false` + `custom-protocol`，产物同步到 `<根>\desktop\` | 同上；已先 `npm run build` |
| `install-local-shortcut.ps1` | 把桌面 `open-ai.lnk` 指向本机构建的桌面端 | 已生成 `<根>\desktop\open-ai-desktop.exe` |
| `check-ps1.ps1` | **PowerShell 脚本门禁**：解析错误 / 非 ASCII 却无 UTF-8 BOM | 无 |
| `check-tray.ps1` | **托盘图标运行时验证**：枚举 `tray_icon_app` 顶层窗口 + 通知区域登记表 | 应用正在运行 |
| `launch-app.ps1` | 启动已编译的桌面应用 | 需先 `build-tauri.ps1`；dev 模式还需 dev server |
| `shot-window.ps1` | 截取应用窗口（验收用） | 应用正在运行 |

## 用法

```powershell
# 1) 前端开发服务器（在 WSL 或 Windows 均可）
cd desktop-ui && npm run dev

# 2) 视觉回归截图 -> docs/screenshots/
node tools/screenshot.mjs

# 2b) DOM 断言自检（推荐日常使用：确定性，不依赖人眼看图）
node tools/verify-ui.mjs

# 2c) 直连「真实打包应用」做断言（验证用户实际在用的那一份）
#     先带调试端口启动：set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
#                       desktop\open-ai-desktop.exe
node tools/verify-packaged.mjs
node tools/verify-packaged.mjs --uninstall    # 额外走一遍一键卸载点击流程

# 3) 编译并启动桌面应用
powershell -ExecutionPolicy Bypass -File tools/build-tauri.ps1
powershell -ExecutionPolicy Bypass -File tools/launch-app.ps1
powershell -ExecutionPolicy Bypass -File tools/shot-window.ps1

# 4) 生产构建 + 本机快捷方式 + 托盘验证
powershell -ExecutionPolicy Bypass -File tools/build-tauri-release.ps1
powershell -ExecutionPolicy Bypass -File tools/install-local-shortcut.ps1
powershell -ExecutionPolicy Bypass -File tools/check-tray.ps1 -ProcessName open-ai-desktop

# 5) 改完任何 .ps1 后过一遍门禁
powershell -ExecutionPolicy Bypass -File tools/check-ps1.ps1
```

## ⚠️ PowerShell 脚本编码（务必遵守）

`tools/*.ps1` 与根目录的 `start_hidden.ps1` **必须保存为 UTF-8 with BOM**。

原因：Windows PowerShell 5.1 对**无 BOM** 的脚本按 ANSI(GBK) 解码，中文注释的
最后一个字节会与紧随的换行配成双字节字符，从而**整行吞掉下一条语句** ——
文件在编辑器里完全正常，只有运行时才炸。本项目已被吞掉过
`$env:TAURI_ENV_DEBUG = 'false'`（导致 release 包重新嵌入 dev 地址、虚拟机白屏），
`start_hidden.ps1` 也出现过 2 处硬语法错误。

**注意**：用脚本/工具（`write`、`edit`、各种编辑器）改写这些文件会**丢失 BOM**。
改完必须执行：

```powershell
# 查看是否带 BOM（应为 efbbbf）
Format-Hex file.ps1 -Count 3
# 门禁：解析错误 + 非 ASCII 无 BOM 一律 FAIL
powershell -ExecutionPolicy Bypass -File tools/check-ps1.ps1
```

## 关于截图依赖（为何单独一个目录）

`screenshot.mjs` 与 `verify-ui.mjs` 用 **Windows 侧 Node** 运行 —— 因为 puppeteer
需要启动 Windows 的 `msedge.exe`，而 WSL 内的 Node 无法跨 interop 拉起 Windows
GUI 进程（会报 `Code: 21`）。

因此依赖装在 `tools/node_modules_tools/`，与前端 `node_modules` 完全隔离：

```powershell
cd tools/node_modules_tools && npm install
```

脚本按绝对路径加载 `puppeteer-core`，不依赖 Node 的向上查找，故位置固定即可。

> 无头模式下 Chrome 会拒绝 `navigator.clipboard.writeText`（`NotAllowedError`），
> 这是**测试环境限制而非功能缺陷** —— 真实 Tauri 窗口具备焦点，剪贴板写入正常。
> 若需自动化验证剪贴板链路，可在页面注入 `navigator.clipboard` 替身捕获写入值。

## 关于 Rust 工具链（重要）

本机 rustup 的 manifest 拉取在国内网络下反复中断，因此项目采用
**手工组装的独立工具链**，位于仓库根的 `open-ai/toolchain/`：

- `src-tauri/.cargo/config.toml` 中的 `[build] rustc` 直接指向该工具链，绕开 rustup shim
- ⚠️ **不要**对该目录执行 `rustup toolchain install` 等 rustup 管理命令，否则会被清空
- 若需重建：从 `https://mirrors.aliyun.com/rustup/dist/2026-05-28/` 下载
  `rustc` / `rust-std` / `cargo` 三个 `x86_64-pc-windows-msvc` 组件，解包后按
  `bin/` 与 `lib/rustlib/` 结构合并即可

## 排错提示

- **页面没变**：布局层（`AppLayout` 等）改动后 Vite HMR 在 `/mnt/d` 挂载盘上不可靠，
  请重启 dev server（`npx vite --force`）。
- **截图各页相同**：说明页面未真正切换，通常是上一条原因导致浏览器跑着旧模块。
- **改完 `.ps1` 一律跑一次 `check-ps1.ps1`**。PS 5.1 会按 ANSI(GBK) 解码无 BOM 的 UTF-8
  脚本，中文注释的末字节可能与紧随的换行配成双字节字符，从而**静默吞掉下一行代码**。
  本项目已因此中招三次：`build-tauri-release.ps1` 丢了 `$env:TAURI_ENV_DEBUG`（打过 dev
  地址的包）、`start_hidden.ps1` 2 处硬解析错误（开机自启失效）、
  `install-local-shortcut.ps1` 被编辑工具保存后丢了 BOM。
  ⚠️ **有些编辑工具保存时会顺手去掉 BOM** —— 改完必须复核，不能只看「文件内容对不对」。
- **截图被裁掉一部分**：`shot-window.ps1` 已 `SetProcessDPIAware()`。若自行写截图脚本，
  务必先开 DPI 感知 —— 否则 `GetWindowRect` 给逻辑像素、实际窗口是 150% 缩放，
  截出来的图右边和下边各少约三分之一（本项目实测踩到：5 列表格只截出 2 列）。
