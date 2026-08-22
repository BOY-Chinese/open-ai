# open-ai 本地聚合网关

一个跑在本地的 AI 网关，把多个上游供应商（**WorkBuddy / 腾讯混元**、**Trae / 字节**）聚合成统一的
OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models`）与 Anthropic 兼容接口（`/v1/messages`），
供 Claude Code、CC Switch、OpenAI 客户端等任意兼容工具统一调用。

---

## 0. 环境要求

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| **操作系统** | Windows 10/11 | 网关用 `pythonw.exe` 无窗口运行、依赖 Windows 计划任务与启动文件夹；WSL2 镜像网络下可在 WSL 侧访问 `127.0.0.1` |
| **Python** | **3.10+**（实测 3.13.14） | 用于 `main.py` 网关与 `daemon.py` 守护；`start.bat` 首次运行会自动建 `.venv` 并装依赖 |
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
#    - 用 账号管理.bat 添加 TRAE / WorkBuddy 账号
# 5) 运行 open-ai-autostart.bat 启动; 开机自启可在 账号管理.bat 设置页勾选
```

> 注意：依赖安装使用清华镜像（`start.bat` 内已写 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

---

## 1. 架构

```
                  ┌─────────────────────────────────────────────┐
   客户端 ───────▶ │  main.py  (FastAPI 网关, 监听 :8000)         │
   (CC Switch /   │   - /v1/chat/completions  (OpenAI 协议)      │
    Claude Code / │   - /v1/messages          (Anthropic 协议)   │
    OpenAI 工具)   │   - /v1/models                              │
                  └───────┬───────────────────────┬─────────────┘
                          │ 路由                    │
                  ┌───────▼────────┐      ┌────────▼────────┐
                  │ WorkBuddy      │      │ Trae Node 后端   │
                  │ (腾讯网关)      │      │ server.js :18787 │
                  │ copilot.tencent│      │ (本地代理, 积分)  │
                  └────────────────┘      └─────────────────┘

   daemon.py  (pythonw 无窗口后台守护)
     - 每 60s 自愈: 网关/Node 掉线自动拉起
     - 每日签到 + TRAE 9074 繁忙时持续补试
   watchdog_boot.py (每 5 分钟被计划任务调用, 保 daemon 存活)
```

### 进程
| 进程 | 端口 | 说明 |
|---|---|---|
| `main.py` (FastAPI) | **8000** | 聚合网关，对外提供 OpenAI / Anthropic 兼容接口 |
| `trae/server.js` (Node) | **18787** | Trae 内嵌本地代理（积分/Work 通道） |
| `daemon.py` (pythonw) | 无 | 无窗口后台守护：自愈 + 每日签到 |
| `watchdog_boot.py` (pythonw) | 无 | 由计划任务周期性拉起 daemon（防 daemon 崩溃） |

---

## 2. 目录结构

```
open-ai/
├── main.py                 # FastAPI 网关入口
├── daemon.py               # 无窗口后台守护 (自愈 + 签到)
├── watchdog_boot.py        # daemon 存活保活 (被计划任务调用)
├── anthropic_api.py        # Anthropic 协议 ↔ OpenAI 协议转换
├── config.json             # ★ 核心配置 (见 §3)
├── config.json.bak-*       # 配置备份 (勿用含占位符 device_id 的旧版覆盖!)
├── MEMORY.md               # 关键事实记忆 (device_id 约束等, 打包必读)
├── 账号管理.bat            # ★ 图形界面入口 (账号/API管理/设置/日志)
├── start.bat               # 一键启动 (建 venv / 装依赖 / 拉起网关)
├── start_hidden.ps1        # 隐藏窗口启动网关 + 签到 (供开机自启调用)
├── open-ai-autostart.bat   # 开机自启入口 (放启动文件夹)
├── uninstall.ps1           # 一键彻底卸载 (GUI 设置页调用)
├── providers/
│   ├── __init__.py         # Provider 注册表 + 模型路由
│   ├── workbuddy.py        # WorkBuddy (腾讯/混元) provider
│   ├── base.py             # Provider 基类
│   └── trae.py             # Trae provider (路由到本地 Node 后端)
├── trae/
│   └── server.js           # Trae 内嵌 Node 后端 (:18787)
├── scripts/
│   ├── gui_account_manager.py  # ★ 图形界面主程序 (账号/API管理/设置/日志)
│   ├── api_store.py        # API 密钥存储管理 (create/rename/delete)
│   ├── signin_all.py       # 统一签到脚本 (TRAE + WorkBuddy + token 续期)
│   ├── account_manager.py  # 控制台版账号管理 (积分查询等, GUI 复用其逻辑)
│   ├── login_trae.py       # 登录/添加 Trae 账号
│   └── login_workbuddy.py  # 登录/添加 WorkBuddy 账号
├── tests/                  # 单元测试 (unittest, 无第三方依赖)
│   ├── test_api_store.py       # API 密钥管理逻辑测试
│   └── test_account_parse.py   # 账号解析逻辑测试
├── logs/  (*.log)          # ★ 全部运行日志集中于此 (daemon/signin/open_api/server)
├── data/  (状态文件)       # ★ 运行状态 (PID/签到状态, 不入库)
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
双击 `账号管理.bat` 打开管理界面，内含：
- **已添加账号**：查看 TRAE / WorkBuddy 账号与积分
- **API 管理**：创建 / 命名 / 复制 / 删除 API 密钥（改后立即生效）
- **设置**：开机自动运行开关、一键卸载
- **操作日志**：查看后台操作输出

### 开发/手动启动
```bat
start.bat        # 首次会建 .venv 并装依赖, 然后拉起 Node 后端(18787) + 网关(8000)
```

### 生产/无窗口常驻（推荐）
网关与守护都由 `open-ai-autostart.bat` 以隐藏窗口拉起（不弹窗）：
- 手动：`open-ai-autostart.bat`
- 开机自启：GUI「设置」页勾选「开机自动运行」（把 `open-ai-autostart.bat` 复制到启动文件夹）

`open-ai-autostart.bat` 会启动：
1. `start_hidden.ps1` → 拉起网关(8000) + Node 后端(18787)
2. `daemon.py`（pythonw 无窗口）→ 自愈 + 每日签到

### 卸载
GUI「设置」页点「一键卸载」，或命令行运行 `uninstall.ps1`：停止全部进程、
移除开机自启与计划任务，并彻底删除插件目录（含配置与账号）。

### 守护与保活
- **`daemon.py`**：`pythonw.exe` 运行（无控制台窗口）。每 60s 检查 8000/18787，掉线则用
  `start_hidden.ps1` 隐藏拉起；每天执行签到，TRAE 遇 9074 繁忙时每 30 分钟自动补试。
- **`watchdog_boot.py`**：由 Windows 计划任务 `OpenAI-DaemonBoot`（每 5 分钟）用 `pythonw`
  调用，检查 daemon 是否存活，死了就拉起。全程无窗口。
- 旧方案（`watchdog.ps1` / `watchdog_check.ps1`）已移除，请勿恢复。

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

## 6. 签到

统一脚本 `scripts/signin_all.py`：

```bash
python scripts/signin_all.py            # 完整: token 续期 + WorkBuddy 签到 + TRAE 签到
python scripts/signin_all.py --no-renew # 只签到, 不续期 token
python scripts/signin_all.py --force    # 强制续期 (忽略剩余时间)
python scripts/signin_all.py --trae-only# 只补试 TRAE (供 daemon 白天反复调用)
```

- **WorkBuddy 签到**：调用 `daily-checkin`，返回 `code:0`（成功）/ `10001`（今日已签），领取每日积分。
- **TRAE 签到**：先查 `status`（看 `checked_in`/`enable`），再 `claim` 领积分。
  - 若 `device_id` 不合法 → `code:9074`（见 §3 约束）。
  - 若服务端瞬时繁忙 → `9074`，脚本单次重试 2 次；仍失败则写 `state=pending`，
    由 daemon 每 30 分钟用 `--trae-only` 补试，直到签上（`state=done`）或跨天。
- 日志见 `logs/signin.log`（每日追加）；签到状态见 `data/.trae_signin_state`（`done`/`pending`）。

---

## 7. 排错

| 现象 | 排查 |
|---|---|
| TRAE 签到一直 9074 | 检查 `config.json` 的 `device_id` / `x-device-id` 是否为**真实客户端 machineid**（见 §3），占位符/伪造值必 9074 |
| 网关起不来 | 看 `open_api_err.log`；确认 `start.bat` 已建好 `.venv` 且装了依赖 |
| 端口被占 | 8000/18787 已在运行则脚本会跳过；用 `netstat -ano \| findstr :8000` 查占用 |
| 开机没自启 | 确认启动文件夹里有 `open-ai-autostart.bat`（在 账号管理.bat 设置页勾选「开机自动运行」） |
| daemon 没在跑 | 计划任务 `OpenAI-DaemonBoot` 每 5 分钟会通过 `watchdog_boot.py` 拉起；也可手动 `pythonw daemon.py` |

---

## 8. 关键文件速查

| 我想… | 看/改 |
|---|---|
| 管理账号/积分/API/自启/卸载 | `账号管理.bat`（GUI）→ `scripts/gui_account_manager.py` |
| 管理 API 密钥 | GUI「API管理」页 → `scripts/api_store.py` |
| 换模型/加别名 | `config.json` → `providers.workbuddy.models` |
| 修签到失败(9074) | `config.json` → `providers.trae.device_id` / `headers.x-device-id`（填真实 machineid） |
| 懂 device_id 约束 | `MEMORY.md` |
| 调守护节奏 | `daemon.py`（`CHECK_INTERVAL` / `TRAE_RETRY_INTERVAL`） |
| 加 Trae 账号 | 账号管理GUI 或 `scripts/login_trae.py` |
| 加 WorkBuddy 账号 | 账号管理GUI 或 `scripts/login_workbuddy.py` |
