# open-ai 本地聚合网关

一个跑在本地的 AI 网关，把多个上游供应商（**WorkBuddy / 腾讯混元**、**Trae / 字节**）聚合成统一的
OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models`）与 Anthropic 兼容接口（`/v1/messages`），
供 Claude Code、CC Switch、OpenAI 客户端等任意兼容工具统一调用。

> **当前版本: v3.0**（`version.py` 单源定义 `APP_VERSION` / `UPDATE_CHANNEL`）
> - **dev 版**（开源/开发者版）: `APP_VERSION='v3.0-dev'`、通道 `dev`，需自备 Python 3.10+ 运行环境；
> - **portable 版**（普通用户版）: `APP_VERSION='portable-v3.0'`、通道 `portable`，安装包内建运行时，
>   对电脑运行环境要求大大降低。
> 两个通道互不干扰，「一键更新」按 `UPDATE_CHANNEL` 在 GitHub Release Assets 中精确匹配对应安装包
> （dev → `open-ai-installer-dev.exe`；portable → `open-ai-installer-portable.exe`）。

## v3.0 更新内容

1. **全新桌面端（Tauri 2 + React 18）**：暗色主题、左侧导航、三通道统一表格；
   安装包内置于 `desktop/`，桌面快捷方式默认拉起新桌面端。
2. **前后端链路打通**：网关新增管理面 REST 接口 `/v1/admin/*`
   （账号 / API 密钥 / 模型 / 积分 / 日志），桌面端直接读取真实数据。
   `GET` 只读本地（毫秒级），需要联网的动作走 `POST`。
3. **托盘图标改由桌面端自建**：v3.0 起界面默认是桌面端，而托盘原先只在 Python
   GUI 里创建 → 表现为「后端在跑但托盘空白」。现桌面端自带托盘
   （左键恢复 / X 隐藏到托盘 / 菜单退出界面），并加单实例防止双托盘。
4. **桌面端免配置连网关**：打包态经 Tauri 命令读 `config.json` 拿地址与密钥
   （前端产物中不含密钥明文），并在后端未运行时**自动拉起 + 轮询就绪**；
   界面顶部提供「启动门」，失败时给出原因、安装目录与重试入口。
5. **修复开机自启托盘缺陷**：自启链路改为「后端 + 桌面端（`--minimized` 驻留托盘）」。
6. **国际版模型前缀 `wbai-` → `wbie-`**：与 Trae `tr-`、WorkBuddy `wb-` 形成统一
   三字母通道前缀；旧前缀仍作为请求别名兼容，老客户端无需改动。
7. **自启脚本根治硬编码路径**：`open-ai-autostart.bat` 改为 `%~dp0` 自适应，
   安装到任意目录都能工作。
8. **修复打包脚本的两处「静默失效」**：① PowerShell 5.1 以 ANSI 解码无 BOM 的
   UTF-8 脚本，中文注释会吞掉紧随的语句（曾吞掉 `TAURI_ENV_DEBUG=false`，
   导致 release 包重新嵌入 dev 地址、虚拟机白屏）；已转 UTF-8 with BOM 并加断言与
   批量门禁 `desktop-ui/tools/check-ps1.ps1`。② 构建脚本原先用「产物存在」代替
   「构建成功」，cargo 失败时会同步旧 exe 并打印成功；现按退出码判定并核对字节数。
9. **彻底删除旧 tkinter 界面**：`scripts/gui_account_manager.py`（2781 行）、
   `scripts/tray_icon.py`、`账号管理.bat`、`open-ai-manager.exe` shim 及其单元测试全部移除，
   并清掉启动链里所有「回落到 Python GUI」的分支（`launcher_main.py`、
   `installer/launcher.py`、`start_hidden.ps1`、`installer.py` 生成的启动器）。
   界面**只有一个入口**：`desktop/open-ai-desktop.exe`。
10. **修复模型列表「积分倍率」整列为 0**：三个根因 —— ① `admin_api` 里的 `import main`
   把网关**重复导入了一遍**，产生第二份 `PROVIDERS`，其 Trae 动态模型表为空
   （`/v1/models` 63 个模型 vs `/v1/admin/models` 只有 15 个配置别名）；
   ② WorkBuddy 倍率是字符串（`'x0.05 credits'`），`float()` 抛异常后整条记录被静默丢弃；
   ③ Trae 倍率元组的第 0 个字段（干净的配置名 `glm-5.2`）未被登记。
   修好后 105 个模型中 103 个可匹配、79 个非零（修前只有 2 个非零）。

## v2.4 更新内容

1. **查看积分消耗**：新增积分消耗页，可查看当天积分消耗与近 3 周逐日消耗（柱状图），
   **每 5 分钟自动更新**（与 Broker 采集节奏一致）。
2. **后端进程统一托管**：open-ai 全部后端进程（网关/Node/任务）全部被纳入
   `open-ai-daemon`（Broker）这个**父进程**统一托管（Windows Job Object 进程树），
   停止/退出即整树退出，不再产生孤儿进程。
3. **系统托盘管理**：open-ai 加入系统托盘，可在托盘**一键退出**全部运行中的进程
   （窗口 X 最小化到托盘，托盘右键退出）。
4. **发布 portable 版**：发布面向普通用户的 **portable 版**，对电脑运行环境要求大大降低
   （无需自装 Python/Node，安装包内建运行时）；同时依旧保留开源 **dev 版**。

---

## 0. 环境要求

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| **操作系统** | Windows 10/11 | 网关用 `pythonw.exe` 无窗口运行、依赖 Windows 计划任务与启动文件夹；WSL2 镜像网络下可在 WSL 侧访问 `127.0.0.1` |
| **Python** | **3.10+**（实测 3.13.14） | 用于 `main.py` 网关与 Broker（`app_runtime.py`）；`start.bat` 首次运行会自动建 `.venv` 并装依赖 |
| **Node.js** | 任意较新版本（实测 v24.15.0） | 仅 Trae 本地后端 `trae/server.js` 需要；**缺失时 WorkBuddy 网关仍可用**，仅 Trae 通道禁用 |
| **Python 包** | 见 `requirements.txt` | `fastapi>=0.110`、`uvicorn[standard]>=0.29`、`httpx>=0.27`、`playwright>=1.44` |
| **Playwright 浏览器** | 首次需安装 | `playwright` 用于 Trae 后端驱动，**装好依赖后还需** `playwright install`（代码用到其 driver） |
| **网络** | 可访问 `copilot.tencent.com` / `api.trae.cn` | 网关是代理，需出网到上游；本机回环 `127.0.0.1:8000`/`18787` 供客户端接入 |

### 首次安装步骤
```bat
# 1) 装 Python 3.10+ 与 Node.js (加到 PATH)
# 2) 双击 start.bat  (自动建 .venv + pip install -r requirements.txt)
# 3) 安装 Playwright 浏览器 (Trae 后端需要):
.venv\Scripts\python.exe -m playwright install
# 4) 填好 config.json (见 §3 约束):
#    - api_key: 替换 YOUR_API_KEY_HERE 为你的网关密钥
#    - trae.device_id / headers.x-device-id / ttnet_params.deviceId:
#      替换 YOUR_DEVICE_ID 为真实 machineid (见 §3)
#    - 账号请打开桌面端「账号管理」页添加 TRAE / WorkBuddy 账号
# 5) 运行 open-ai-autostart.bat 启动; 开机自启可在桌面端「系统设置」页勾选
```

> 注意：依赖安装使用清华镜像（`start.bat` 内已写 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

---

## 1. 架构（v2.4 进程管理重构）

> v2.4 重写了进程管理：所有子进程由唯一的 **Broker 主进程**通过 **Windows Job Object**
> 统一创建与托管，配合命名管道 IPC 心跳与优雅退出协议，从操作系统层面杜绝孤儿进程。
> 每个进程在任务管理器里都是独立的 open-ai 品牌 exe（名称/图标/文件描述），
> 且为**单进程**（不再有 python3.13.exe 子进程污染）。

```
                        bootstrap.py (open-ai.exe)   ← 统一 CLI: start/stop/status/doctor
                               │ spawn (DETACHED, 幂等)
                               ▼
              open-ai-daemon.exe  (Broker = 主进程, app_runtime.py)
              ├─ 单实例锁 (mutex + PID 文件双判)
              ├─ Job Object 树 (KILL_ON_JOB_CLOSE):
              │    root "open-ai.tree"  ← Broker 自指派; 崩溃/被杀 → 整树清零
              │      ├─ leaf "open-ai.gateway"  (内存限额)
              │      ├─ leaf "open-ai.trae"
              │      └─ leaf "open-ai.task"     (内存限额)
              ├─ IPC 服务端 \\.\pipe\open-ai.broker (心跳/指令/优雅退出)
              ├─ 监督循环 (5s): 心跳超时→判僵死→重启; 退出→指数退避重启
              │    (30s→1m→2m→4m→8m→15m 封顶, 稳定 5 分钟后重置)
              └─ 定时任务: 每日签到 / TRAE 9074 补试 / 5 分钟流水采集
                               │ Job 内创建 (零竞态自动入树)
              ┌────────────────┼─────────────────┐
              ▼                ▼                 ▼
   open-ai-gateway.exe  open-ai-trae.exe  open-ai-task.exe (短命)
   main.py :8000        trae/server.js     signin/usage 脚本
   (心跳 10s + 优雅退出)  :18787 (同协议)

   watchdog_boot.py (计划任务每 5 分钟) → bootstrap.py start (最后防线)
```

### 关键保证
| 场景 | 行为 |
|---|---|
| 子进程崩溃 | Broker 监督循环检测（退出码/心跳超时）→ 指数退避自动重启 |
| Broker 被杀/崩溃 | root Job 句柄随进程关闭 → `KILL_ON_JOB_CLOSE` 内核级终止全部子进程，**零孤儿** |
| 任务管理器一键关闭 | 结束 `open-ai-daemon.exe` 即全树退出；或 `bootstrap.py stop` 优雅关闭 |
| 优雅退出 | `bootstrap.py stop` → IPC 广播 shutdown → 子进程自行清理退出 → Job 兜底 |
| 开机自启后重复点击 | `bootstrap.py start` 幂等（单实例锁），已在跑直接返回 |

### 系统托盘与窗口生命周期（v3.0 桌面端）
界面是独立的桌面端进程（`desktop\open-ai-desktop.exe`，Tauri 2 + React）。
关闭窗口 X **不退出程序**，最小化到系统托盘；后端服务常驻：

| 操作 | 行为 |
|---|---|
| 启动界面 | 桌面端自建托盘图标（右下角，open-ai logo） |
| 点击窗口 **X** | 隐藏主窗口（**任务栏图标同步消失**），托盘图标保留，网关/签到/守护继续运行 |
| **左键点击托盘图标** | 恢复主窗口并置于前台（任务栏图标恢复），自动回填账号/积分/模型数据 |
| 托盘**右键 → 显示主界面** | 同左键单击 |
| 托盘**右键 → 打开日志目录** | 资源管理器打开 `logs\` |
| 托盘**右键 → 退出界面（后端继续运行）** | 只退出**界面**；网关/签到/守护**继续跑**。要停后端请用 `bootstrap.py stop` |
| 重复双击桌面快捷方式 | 单实例（`tauri-plugin-single-instance`）：不开第二个实例、不产生第二个托盘图标，直接把已运行实例的窗口带回前台 |
| 后端未运行 | 启动时先探活 `/v1/admin/health`，不通则**自动拉起后端**并轮询就绪（最长 45s），期间内容区显示「启动门」 |
| 运行中断开 | 每 45s 探活，连续 2 次失败才判定断开，给出原因 + 重试入口（**不会**偷偷重启后端） |

> **v3.0 语义变更**：旧版 Python 托盘的「退出」会**连后端一起关掉**；新版只退界面，
> 且菜单文案已写明。如需停掉全部后端：`bootstrap.py stop`。

托盘由桌面端用 Tauri 的 `tray-icon`（Rust）实现；旧的纯 Win32 Python 托盘
（`scripts/tray_icon.py`）已随旧 GUI 一并删除。验证托盘是否真的创建成功：

```powershell
powershell -ExecutionPolicy Bypass -File desktop-ui\tools\check-tray.ps1 -ProcessName open-ai-desktop
```

### 进程
| 进程（任务管理器映像名） | 端口 | 说明 |
|---|---|---|
| `open-ai-daemon.exe` | 无 | **Broker 主进程**：Job 托管 + IPC 服务端 + 监督重启 + 签到/流水调度 |
| `open-ai-gateway.exe` | **8000** | 聚合网关（main.py），OpenAI / Anthropic 兼容接口 |
| `open-ai-trae.exe` | **18787** | Trae Node 后端（server.js，积分/Work 通道） |
| `open-ai-task.exe` | 无 | 短命脚本宿主（签到/流水采集/登录脚本），跑完即退 |
| `open-ai-desktop.exe` | 无 | **图形界面（Tauri 桌面端）**，不在 Broker 进程树内：独立进程 + 自带托盘 |
| `open-ai.exe` | 无 | 引导/控制 CLI（bootstrap.py） |
| `watchdog_boot.py` | 无 | 计划任务兜底保活（委托 bootstrap） |

所有品牌化 exe 位于 `runtime\Scripts\`，由 `procname.py` 从真实解释器根复制并注入图标/版本信息生成（逐 shim 印章增量重建，只重建缺失/变更的，不触碰运行中被占用的文件）。解释器根按 `venv 反推 → Appx 查询 → WindowsApps/Program Files 目录探测 → py launcher` 多级定位，**Store 版与 python.org 版双兼容**：Store 版（WindowsApps，DLL 受 ACL 限制）自动复制扩展 DLLs 并注册 `.pth`；org 版（Program Files）直接加载系统 DLLs，跳过该步。仅核心 DLL（`python3xx.dll`/`vcruntime*.dll`，按版本动态发现）两种来源都复制到 shim 同目录。

**任务管理器视角**：open-ai 全部进程以品牌化名称+图标出现在**后台进程**分组
（`open-ai` / `open-ai gateway :8000` / `open-ai trae :18787`，一目了然）。
停止服务用 `bootstrap.py stop`（优雅），或结束 `open-ai-daemon.exe` 进程
（root Job `KILL_ON_JOB_CLOSE` 内核级级联清空全树，零孤儿）。

---

## 2. 目录结构

```
open-ai/
├── main.py                 # FastAPI 网关入口 (--from-broker 启用心跳/优雅退出)
├── daemon.py               # Broker 薄入口 (兼容旧调用; 实际逻辑在 app_runtime.py)
├── app_runtime.py          # ★ Broker: Job 树/IPC 服务端/监督循环/定时任务
├── bootstrap.py            # ★ 统一控制 CLI: start/stop/restart/status/doctor
├── procname.py             # ★ 进程品牌注册表 + runtime 构建工厂 (图标/版本注入)
├── ipc.py                  # ★ 命名管道 IPC 协议 (帧编解码/心跳客户端/会话)
├── jobmgmt.py              # ★ Windows Job Object 封装 (KILL_ON_CLOSE/配额/枚举)
├── watchdog_boot.py        # 计划任务兜底保活 (委托 bootstrap; 尊重退出抑制标记)
├── version.py              # ★ 版本号定义 (APP_VERSION, 设置页显示/一键更新比较用)
├── anthropic_api.py        # Anthropic 协议 ↔ OpenAI 协议转换
├── config.json             # ★ 核心配置 (含 runtime 节: 心跳/退避/内存限额)
├── MEMORY.md               # 关键事实记忆 (device_id 约束等, 打包必读)
├── desktop/                # ★ v3.0 桌面端 (唯一界面): open-ai-desktop.exe (Tauri 2 + React)
├── desktop-ui/             # ★ 桌面端源码 (React + TypeScript + Tailwind; src-tauri = Rust 侧)
├── start.bat               # 一键启动 (建 venv / 装依赖 / 构建 runtime / 起 Broker)
├── start_hidden.ps1        # 隐藏窗口启动 (供开机自启调用, 委托 bootstrap)
├── open-ai-autostart.bat   # 开机自启入口 (GUI 自启用隐藏 open-ai-autostart.vbs 调 start_hidden.ps1)
├── desktop-ui/             # ★ v3.0 桌面端 (Tauri 2 + React 18 + Tailwind)
├── admin_api.py            # ★ v3.0 管理面 REST 接口 (/v1/admin/*)
├── runtime/                # ★ v2.4 进程管理运行时 (procname.py 自动构建)
│   ├── pyvenv.cfg          #   home = Store Python 包目录
│   ├── Lib/site-packages   #   junction → .venv 的 site-packages
│   ├── DLLs/               #   Store 包 DLLs 副本 (WindowsApps ACL 所需)
│   └── Scripts/            #   open-ai-*.exe 品牌化进程 + python313.dll 等
├── providers/
│   ├── __init__.py         # Provider 注册表 + 模型路由
│   ├── workbuddy.py        # WorkBuddy (腾讯/混元) provider
│   ├── base.py             # Provider 基类
│   └── trae.py             # Trae provider (路由到本地 Node 后端)
├── trae/
│   └── server.js           # Trae 内嵌 Node 后端 (:18787, 含 Broker IPC 客户端)
├── scripts/
│   ├── api_store.py        # API 密钥存储管理 (create/rename/delete)
│   ├── signin_all.py       # 统一签到脚本 (TRAE + WorkBuddy + token 续期)
│   ├── account_manager.py  # 账号读写与积分查询 (管理接口 admin_api.py 复用其逻辑)
│   ├── usage_history.py    # ★ TRAE 逐笔积分消耗流水 (网页 dashboard 同款接口逆向)
│   ├── wb_usage_history.py # ★ WorkBuddy 逐笔消耗流水 (官网个人中心同款接口逆向)
│   ├── usage_collector.py  # ★ 逐笔流水自动采集 + 本地 SQLite 流水库 (Broker 调度)
│   ├── login_trae.py       # 登录/添加 Trae 账号
│   └── login_workbuddy.py  # 登录/添加 WorkBuddy 账号
├── tests/                  # 单元测试 (unittest, 无第三方依赖)
│   ├── test_api_store.py       # API 密钥管理逻辑测试
│   ├── test_account_parse.py   # 账号解析逻辑测试
│   └── test_procman.py         # ★ v2.4 进程管理测试 (Job/IPC/runtime)
├── logs/  (*.log)          # ★ 运行日志 (broker/gateway_*/trae_*/signin/daemon_boot)
├── data/  (状态文件)       # ★ 运行状态 (runtime_state.json/PID/签到状态)
└── .venv/                  # Python 虚拟环境
```

---

## 3. 配置 `config.json`

核心段是 `providers`：

```jsonc
{
  "providers": {
    "workbuddy": {
      "accessToken": "...",          // 或 accounts: [{userId, accessToken, refreshToken}]
      "domain": "www.workbuddy.cn",
      "product": "SaaS",
      "models": {                    // 自定义模型别名 (可选, 加到代码内置 MODEL_ALIASES)
        "hy3": "hy3",
        "wb-hy3": "hy3",
        "deepseek-v4-flash": "deepseek-v4-flash"
      }
    },
    "trae": {
      "enabled": true,
      "device_id": "<<真实 device_id>>",   // ★ 见下方「重要约束」
      "headers": {
        "x-device-id": "<<真实 device_id>>" // ★ 必须与上面一致
      },
      "accounts": [{ "uid": "...", "token": "...", "cookie": "..." }],
      "trae_dir": "C:/Users/Lenovo/AppData/Local/Programs/TRAE SOLO CN"
    }
  }
}
```

### ⚠️ 重要约束：device_id 必须由 Trae 客户端生成

> 详见 `MEMORY.md`。一句话：网关不会、也不能自己生成合法的 `device_id`。

- TRAE 的每日签到接口（`checkin_credits/claim`）用 HTTP 头 `x-device-id` 做风控维度。
- 合法值来自 **Trae 桌面客户端**首次启动时生成的随机 UUID，存于：
  `C:\Users\<用户>\AppData\Roaming\TRAE SOLO CN\machineid`
- **不要**填占位符 `CHANGE_ME_DEVICE_ID`、也不要自己 `randomUUID()` 伪造——服务端只认
  "客户端生成并上报登记过"的真值，其余一律返回 `code:9074`（伪装成"当前参与用户太多"）。
- 正确做法：从上面那个 `machineid` 文件复制真实值，填入 `config.json` 的
  `providers.trae.device_id` **和** `providers.trae.headers.x-device-id`（两处都要）。
- device_id 绑定「机器 + 当前 Trae 安装」：重装 Trae / 清空 AppData 会导致值变化，需重新提取。
- ⚠️ 项目里的 `config.json.bak-*` 旧备份是**填真实 device_id 之前**的版本（含占位符），
  **切勿用它覆盖当前的 `config.json`**，否则签到会失效。

---

## 4. 启动方式

### 图形界面（推荐，小白友好）
双击桌面 **`open-ai` 快捷方式**（或 `desktop\open-ai-desktop.exe`）打开管理界面。
界面**只有这一个入口** —— 旧的 tkinter 界面（`账号管理.bat` 等）已在 v3.0 删除。

六个页面：
- **账号管理**：Trae / WorkBuddy / WorkBuddy 国际三通道合一表格，含每日签到、当前积分、
  账号状态；底部可添加三类账号，右键行可复制账号名 / 重新连接 / 删除
- **API 管理**：顶部展示网关地址（OpenAI 兼容 `http://127.0.0.1:8000/v1`、
  Anthropic 兼容 `http://127.0.0.1:8000`，按 config.json 的 host/port 生成），
  右键卡片可复制地址；下方创建 / 命名 / 复制 / 删除 API 密钥（改后立即生效）
- **模型列表**：三通道合一，含积分倍率与「请求模型名称」（即实际路由表 ai 名称）；
  右键行可复制请求模型名称 / 置顶 / 隐藏，支持多选批量操作
- **积分看板**：今日情况（获取/消耗双卡片 + 逐笔流水）与每周情况（三通道堆叠柱状图，
  可切通道与周次）
- **系统日志**：网关与守护进程实时输出，级别着色、Ctrl+F 搜索、自动滚动
- **系统设置**：启动设置（开机自动运行）、版本信息（一键更新，按 `UPDATE_CHANNEL`
  在 GitHub Release Assets 精确匹配：dev → `open-ai-installer-dev.exe`；
  portable → `open-ai-installer-portable.exe`；仓库 `BOY-Chinese/open-ai/releases`）、
  危险操作（一键卸载）

### 开发/手动启动
```bat
start.bat        # 首次建 .venv 装依赖 + 构建品牌化进程 (runtime/), 然后启动 Broker
```

### 控制命令（bootstrap.py / open-ai.exe）
所有进程生命周期统一走 `bootstrap.py`（品牌化入口 `runtime\Scripts\open-ai.exe`）：
```bat
.venv\Scripts\python.exe bootstrap.py start     # 启动 (幂等, 已在跑直接返回)
.venv\Scripts\python.exe bootstrap.py stop      # 优雅停止 (IPC 广播 → Job 兜底)
.venv\Scripts\python.exe bootstrap.py restart   # 重启
.venv\Scripts\python.exe bootstrap.py status    # 各角色 PID/心跳/存活
.venv\Scripts\python.exe bootstrap.py doctor    # 诊断: shim/Job/端口/管道/进程树
```

### 生产/无窗口常驻（推荐）
`open-ai-autostart.bat` 以隐藏窗口拉起 Broker（不弹窗），由 Broker 统一托管网关/Node/任务：
- 手动：`open-ai-autostart.bat`
- 开机自启：GUI「设置」页勾选「开机自动运行」（向启动文件夹写入隐藏启动脚本
  `open-ai-autostart.vbs`，以无窗口方式调用 `start_hidden.ps1` 拉起 Broker —— **开机无任何
  控制台窗口/报错闪现**，卸载时自动清理 `.vbs`/`.bat`/计划任务）

### 卸载
GUI「设置」页点「一键卸载」，或运行安装目录下的 `uninstall.exe`：停止全部进程、
移除开机自启/计划任务/桌面快捷方式，并彻底删除插件目录（含配置与账号）。
GUI 内一键卸载以**普通进程直接启动** `uninstall.exe`（免 UAC 弹窗，卸载器已不带
管理员清单）；手动双击 `uninstall.exe` 同样直接运行。

### 守护与保活（v2.4 三层）
1. **Broker 监督循环**（`app_runtime.py`）：每 5s 巡检 gateway/trae —— 进程退出或心跳
   超时（45s）→ 指数退避自动重启（30s→1m→2m→4m→8m→15m 封顶，稳定运行 5 分钟后重置）。
   子进程崩溃自动恢复，无需人工干预。
2. **Job Object 兜底**（`jobmgmt.py`）：root Job `KILL_ON_JOB_CLOSE` —— Broker 无论因何
   退出（被任务管理器结束/崩溃/断电恢复失败），内核自动终止整棵进程树，**绝不产生孤儿**。
3. **计划任务兜底**（`watchdog_boot.py`）：`OpenAI-DaemonBoot`（每 5 分钟）检查 Broker
   存活，不在则委托 `bootstrap.py start` 拉起。全程无窗口。
- 旧方案（`watchdog.ps1` / `watchdog_check.ps1` / daemon 端口探测自愈）已移除，请勿恢复。

---

## 5. 模型路由

请求体里的 `model` 字段决定走哪个上游（见 `providers/__init__.py` 的 `route_provider`）：

| model 含 / 前缀 | 路由到 |
|---|---|
| `trae` / `tr-` | Trae 本地 Node 后端（Work 积分通道） |
| `workbuddy` / `wb-` | WorkBuddy 腾讯网关（混元等） |
| 其他 | 第一个可用 provider |

常用模型名（已内置别名，见 `workbuddy.py` 的 `MODEL_ALIASES` + config `models`）：
- `hy3` / `wb-hy3` / `custom-local:hy3` → 腾讯混元 hy3
- `deepseek-v4-flash` / `deepseek-v4-pro` → DeepSeek
- `workbuddy-deepseek-v4-flash` 等

在 CC Switch 等客户端里使用时，把 `ANTHROPIC_BASE_URL` 指向 `http://127.0.0.1:8000`、
`ANTHROPIC_MODEL` 设为 `workbuddy-hy3` 即可走混元。

---

## 6. 逐笔消耗流水（TRAE + WorkBuddy）

`scripts/usage_history.py` 复用 config 里已抓取的 TRAE token + device_id，直接调用
官网 dashboard（`www.trae.cn/dashboard` 的 Usage details 模块）同款接口，拉取**逐笔**
积分消耗记录（时间 / 模型 / 积分 / token 数 / 会话 / 输入预览）：

```bash
python scripts/usage_history.py                  # 最近 7 天, 所有账号
python scripts/usage_history.py --days 30        # 最近 30 天
python scripts/usage_history.py --uid 319013     # 只查指定账号 (uid 前缀)
python scripts/usage_history.py --pages 2        # 只拉前 2 页 (每页 50 条)
python scripts/usage_history.py --csv out.csv    # 导出 CSV (多账号自动加 uid 后缀)
python scripts/usage_history.py --json           # 输出原始 JSON
```

也可在 `scripts/usage_history.py` 直接调用（桌面端「积分看板」页同源）。

> 接口要点（前端 JS 逆向确认）：`POST api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session`，
> 鉴权 `Authorization: Cloud-IDE-JWT <token>` + `x-device-id`；
> `usage_type` 固定传 `[7]`（credits 计费，数组）；`page_size` ≤ 50，否则 400；
> token 过期返回 401，先用 `signin_all.py` 或「重新连接」续期。

### WorkBuddy 逐笔消耗流水

`scripts/wb_usage_history.py` 复用 config 里的 WorkBuddy accessToken，调用官网
个人中心（`www.codebuddy.cn/profile` →「套餐与用量」）同款接口，拉取**逐笔**消耗记录
（时间 / 模型 / 客户端 / 积分 / 输入预览），也支持按天汇总：

```bash
python scripts/wb_usage_history.py               # 最近 7 天逐笔, 所有账号
python scripts/wb_usage_history.py --days 30     # 最近 30 天
python scripts/wb_usage_history.py --daily       # 按天汇总视图
python scripts/wb_usage_history.py --uid a95fdb  # 只查指定账号 (userId 前缀)
python scripts/wb_usage_history.py --csv out.csv # 导出 CSV
```

也可在 `scripts/wb_usage_history.py` 直接调用（桌面端「积分看板」页同源）。

> 接口要点（前端 JS 逆向确认）：`POST copilot.tencent.com/billing/meter/get-user-request-usage`
>（逐笔）/ `get-user-daily-usage`（按天），鉴权 `Authorization: Bearer <accessToken>` +
> `X-User-Id`；**必须带 `X-Enterprise-Id` 头**（个人账号传自己的 userId，否则 400）；
> 逐笔接口支持 v1 页码式（`pageNum/pageSize`）与 v2 游标式（`version:2` + `pageToken`）。

### 自动采集（打包分发用，零操作）

逐笔流水**不需要**手动查询——Broker 每 **5 分钟**自动增量采集两平台流水（消耗 + 签到
获取），累积存入本地缓存库 `data/usage_history.db`（SQLite，按 平台+账号+流水号 去重，
**缓存保留 1 个月**）。用户装好软件后（`start.bat` 或开机自启）Broker 常驻后台，
自动积累历史，无感。

- 采集器：`scripts/usage_collector.py`（`--collect` 立即一轮 / `--collect-loop` 独立常驻 / 无参数查看本地库）
- Broker 挂载：`app_runtime.py` → `TaskScheduler.run_collect()`（与监督/签到同循环，5 分钟一轮）
- **界面查看**：桌面端「积分看板」页（今日情况 / 每周情况）：
  - **今日情况**：上半部分显示今日获取/消耗积分；下半部分为逐笔消耗流水
    （格式：账号 + 模型 + 时间 + 消耗量，如 `TRAE_7593  GLM-5.3-Flash  2026/08/31 19:53  0.87`）
  - **每周情况**：优先显示本周，可回看最近 3 周；上半部分为该周获取/消耗积分；
    下半部分柱状图显示每天消耗——横坐标为日期，纵坐标为积分消耗量，
    **柱子由红（TRAE 通道）蓝（WorkBuddy 通道）两段叠放组成**，
    鼠标悬停红/蓝段显示对应通道的具体消耗数值，柱顶显示当日合计
  - 页面数据每 5 分钟自动刷新（与采集节奏一致）

手动查看本地库示例：

```bash
python scripts/usage_collector.py                # 本地库最近 7 天 (默认)
python scripts/usage_collector.py --days 30      # 最近 30 天
python scripts/usage_collector.py --platform trae --uid 319013
python scripts/usage_collector.py --csv out.csv  # 导出 CSV
python scripts/usage_collector.py --collect      # 不等 Broker, 立即采集一轮
```

## 7. 签到

统一脚本 `scripts/signin_all.py`：
```bash
python scripts/signin_all.py            # 完整: token 续期 + WorkBuddy 签到 + TRAE 签到
python scripts/signin_all.py --no-renew # 只签到, 不续期 token
python scripts/signin_all.py --force    # 强制续期 (忽略剩余时间)
python scripts/signin_all.py --trae-only# 只补试 TRAE (供 Broker 白天反复调用)
```

- **WorkBuddy 签到**：调用 `daily-checkin`，返回 `code:0`（成功）/ `10001`（今日已签），领取每日积分。
- **TRAE 签到**：先查 `status`（看 `checked_in`/`enable`），再 `claim` 领积分。
  - 若 `device_id` 不合法 → `code:9074`（见 §3 约束）。
  - 若服务端瞬时繁忙 → `9074`，脚本单次重试 2 次；仍失败则写 `state=pending`，
    由 daemon 每 30 分钟用 `--trae-only` 补试，直到签上（`state=done`）或跨天。
- 日志见 `logs/signin.log`（每日追加）；签到状态见 `data/.trae_signin_state`（`done`/`pending`）。

---

## 8. 排错

| 现象 | 排查 |
|---|---|
| 安装器报「依赖安装失败: [WinError 2] 系统找不到指定的文件」 | venv 静默损坏（Python 3.13/3.14 已知问题：`venvlauncher.exe` 复制失败时 `python -m venv` 仍返回成功，但 `.venv\Scripts\python.exe` 未生成）。新版安装器会自动校验/重建/修复并兜底直装；旧版安装器可先删除安装目录下的 `.venv` 再重装，或把安装目录加入杀软信任区。若仍失败，看安装目录下 `pip-install-error.log` |
| 安装器报「No matching distribution found for playwright (from versions: none)」，但其他包正常 | ① Python 非 64 位（playwright 只发 win_amd64 wheel，ARM64/32 位 Python 全部被过滤）→ 新版安装器已自动检测并补装 64 位 Python 3.12；② 镜像源对该包解析异常/pip 本地缓存污染 → 新版安装器已自动切换多镜像源（清华→阿里云→腾讯云→官方）并禁用本地缓存。旧版安装器手动处理：`python -c "import platform,struct;print(platform.machine(),struct.calcsize('P')*8)"` 确认是 `AMD64 64`，不是则改装 64 位 Python；架构无误则在 pip 命令后加 `--no-cache-dir -i https://mirrors.aliyun.com/pypi/simple` |
| TRAE 签到一直 9074 | 检查 `config.json` 的 `device_id` / `x-device-id` 是否为**真实客户端 machineid**（见 §3），占位符/伪造值必 9074 |
| 网关起不来 | 看 `logs\gateway_err.log`；确认 `start.bat` 已建好 `.venv` 且装了依赖；`bootstrap.py doctor` 全量诊断 |
| 端口被占 | `bootstrap.py doctor` 显示端口占用；8000/18787 被**非 open-ai** 进程占用时 Broker 会反复重启该角色（看 `logs\broker.log`） |
| 开机没自启 | 确认启动文件夹里有 `open-ai-autostart.vbs`（在桌面端「系统设置」页勾选「开机自动运行」） |
| Broker/服务没在跑 | 先 `bootstrap.py status` / `doctor`；计划任务 `OpenAI-DaemonBoot` 每 5 分钟兜底拉起；手动 `bootstrap.py start` |
| 想彻底关掉所有进程 | 托盘右键「退出」（GUI 内一键）；或 `bootstrap.py stop`（优雅，二者均抑制 watchdog 复活 10 分钟）；或任务管理器结束 `open-ai-daemon.exe`（Job Object 连带终止全部子进程） |
| runtime 构建失败 | 删除 `runtime\` 目录后重新运行 `start.bat`（自动重建）；解释器需为 Store Python 3.13 或 python.org 3.10+（任选其一） |
| 开机自启后**托盘没有图标** | v3.0 已修复：v2.4 自启只拉起 Broker，而托盘图标由 GUI 创建。现在自启会同时拉起界面（桌面端优先，回落 Python 托盘 GUI）。若仍是旧版，用「一键卸载」清理后重装 |
| 开机自启弹控制台/报「daemon 未运行」 | v2.4 已修复：开机自启改为隐藏 VBS（无窗口）调用 `start_hidden.ps1`；若仍弹旧版残留的启动项/计划任务，用「一键卸载」清理后重装即可 |
| 启动/卸载时弹 "Failed to remove temporary directory: ...\_MEIxxxxxx" | 已修复（launcher/uninstaller 改 PyInstaller onedir 打包，不再解压 `%TEMP%` 临时目录；onefile 引导器在 VM/杀软锁定文件时无法清理才会弹此框） |

---

## 9. 关键文件速查

| 我想… | 看/改 |
|---|---|
| 管理账号/积分/API/自启/卸载 | 桌面端 `desktop\open-ai-desktop.exe`（源码 `desktop-ui/`） |
| 窗口 X 后找不到界面了 | 没退出，最小化到了**系统托盘**（右下角 open-ai 图标）→ 左键点击即恢复 |
| 托盘图标不见了 | 由桌面端（Rust `tray-icon`）创建。用 `desktop-ui\tools\check-tray.ps1 -ProcessName open-ai-desktop` 验证；图标不显示时先确认是否被 Win11 收进托盘溢出区（`^`） |
| 托盘「退出」没退出 | 看 `logs\gui_exit.log`（退出链路逐步诊断）与 `logs\broker.log`（应出现 `gui-shutdown`）；退出流程有多重兜底（IPC→Job 清理→抑制标记→5s 看门狗硬退出），正常必退 |
| 管理 API 密钥 | GUI「API管理」页 → `scripts/api_store.py` |
| 换模型/加别名 | `config.json` → `providers.workbuddy.models` |
| 修签到失败(9074) | `config.json` → `providers.trae.device_id` / `headers.x-device-id`（填真实 machineid） |
| 懂 device_id 约束 | `MEMORY.md` |
| 调守护节奏/内存限额 | `config.json` → `runtime` 节；`app_runtime.py`（`CHECK_INTERVAL` / `BACKOFF_STEPS`） |
| 加 Trae 账号 | 账号管理GUI 或 `scripts/login_trae.py` |
| 加 WorkBuddy 账号 | 账号管理GUI 或 `scripts/login_workbuddy.py` |
