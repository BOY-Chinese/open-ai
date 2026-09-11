# 开发与验证工具

本目录脚本用于构建、启动与视觉回归，均需在 **Windows 侧** 执行（Tauri 与 WebView2 目标为 Windows）。

| 脚本 | 用途 | 前置条件 |
|---|---|---|
| `screenshot.mjs` | 六页面 + 周视图截图自检 | 依赖装在 `tools/node_modules_tools/`（见下）；dev server 在跑 |
| `build-tauri.ps1` | 编译 Tauri Rust 后端 | 独立工具链 `open-ai/toolchain/`（见下） |
| `launch-app.ps1` | 启动已编译的桌面应用 | 需先 `build-tauri.ps1`；dev 模式还需 dev server |
| `shot-window.ps1` | 截取应用窗口（验收用） | 应用正在运行 |

## 用法

```powershell
# 1) 前端开发服务器（在 WSL 或 Windows 均可）
cd desktop-ui && npm run dev

# 2) 视觉回归截图 -> docs/screenshots/
node tools/screenshot.mjs

# 3) 编译并启动桌面应用
powershell -ExecutionPolicy Bypass -File tools/build-tauri.ps1
powershell -ExecutionPolicy Bypass -File tools/launch-app.ps1
powershell -ExecutionPolicy Bypass -File tools/shot-window.ps1
```

## 关于截图依赖（为何单独一个目录）

`screenshot.mjs` 用 **Windows 侧 Node** 运行 —— 因为 puppeteer 需要启动 Windows 的
`msedge.exe`，而 WSL 内的 Node 无法跨 interop 拉起 Windows GUI 进程（会报 `Code: 21`）。

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
