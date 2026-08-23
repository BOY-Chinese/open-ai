# open-ai · 本地 AI 聚合网关

> 把多个上游 AI 供应商（**WorkBuddy / 腾讯混元**、**Trae / 字节**）聚合成一个统一的本地网关，
> 对外提供标准的 **OpenAI 兼容**与 **Anthropic 兼容**接口，供 Claude Code、CC Switch、
> OpenAI 客户端等任意兼容工具即插即用地调用。

**当前版本：`v2.0`**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Node](https://img.shields.io/badge/Node.js-18%2B-green)](https://nodejs.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey)]()

---

## ✨ 特性

- 🌐 **统一网关**：一个端口（`:8000`）同时提供 OpenAI 与 Anthropic 两套协议
- 🔀 **多供应商聚合**：WorkBuddy（腾讯混元 / DeepSeek）+ Trae（字节），按模型名自动路由
- 👥 **多账号池**：每个供应商可挂多个账号，自动轮换、失效自动标记、积分实时查询
- 🖥️ **图形化管理**：内置账号管理 GUI（账号 / API 密钥 / 设置 / 日志 一站式）
- 🔑 **多 API 密钥**：可创建 / 命名 / 复制 / 删除多个网关密钥，改后即时生效
- 📅 **自动签到**：每日自动为全部账号签到领积分，失败自动补试
- 🛡️ **后台守护**：无窗口守护进程自愈保活，掉线自动拉起
- 📦 **一键安装包**：提供 `open-ai-installer.exe`，傻瓜式部署（见 [Releases](../../releases)）

---

## 📥 快速开始

### 方式一：一键安装包（推荐，小白友好）

从 [Releases](../../releases) 下载 **`open-ai-installer.exe`**，双击运行：

1. 选择安装目录（默认 `C:\open-ai`）
2. 安装器自动检测 / 下载并安装 Python、Node.js、依赖、Playwright
3. 自动创建桌面快捷方式、询问是否开机自启
4. 安装完成后编辑 `config.json` 填入 `api_key` 与 `device_id`（见 [配置](#3-配置-configjson)）

> ⚠️ 安装程序需要**以管理员身份运行**。

### 方式二：手动部署

```bat
:: 1) 安装 Python 3.10+ 与 Node.js 18+ (加入 PATH)
:: 2) 双击 start.bat  (自动创建 .venv 并安装依赖)
:: 3) 安装 Playwright 浏览器 (Trae 后端需要)
.venv\Scripts\python.exe -m playwright install
:: 4) 填好 config.json (api_key / device_id, 见下文)
:: 5) 双击 账号管理.bat 添加账号, 然后运行 start.bat 启动
```

> 依赖安装使用清华镜像（`start.bat` 内已写 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

---

## 🏗️ 架构

```
                  ┌─────────────────────────────────────────────┐
   客户端 ───────▶ │  main.py  (FastAPI 网关, 监听 :8000)         │
   (CC Switch /   │   - /v1/chat/completions  (OpenAI 协议)      │
    Claude Code / │   - /v1/messages          (Anthropic 协议)   │
    OpenAI 工具)   │   - /v1/models                              │
                  └───────┬───────────────────────┬─────────────┘
                          │ 按 model 路由          │
                  ┌───────▼────────┐      ┌────────▼────────┐
                  │ WorkBuddy      │      │ Trae Node 后端   │
                  │ (腾讯网关)      │      │ server.js :18787 │
                  │ copilot.tencent│      │ (本地代理·积分)   │
                  └────────────────┘      └─────────────────┘

   daemon.py  (pythonw 无窗口后台守护)
     - 每 60s 自愈: 网关/Node 掉线自动拉起
     - 每日签到 + TRAE 9074 繁忙时持续补试
   watchdog_boot.py (每 5 分钟被计划任务调用, 保 daemon 存活)
```

### 进程一览

| 进程 | 端口 | 说明 |
|---|---|---|
| `main.py` (FastAPI) | **8000** | 聚合网关，对外提供 OpenAI / Anthropic 兼容接口 |
| `trae/server.js` (Node) | **18787** | Trae 内嵌本地代理（积分 / Work 通道） |
| `daemon.py` (pythonw) | 无 | 无窗口后台守护：自愈 + 每日签到 |
| `watchdog_boot.py` (pythonw) | 无 | 由计划任务周期性拉起 daemon（防 daemon 崩溃） |

---

## 📁 目录结构

```
open-ai/
├── main.py                 # FastAPI 网关入口
├── daemon.py               # 无窗口后台守护 (自愈 + 签到)
├── watchdog_boot.py        # daemon 存活保活 (被计划任务调用)
├── anthropic_api.py        # Anthropic 协议 ↔ OpenAI 协议转换
├── config.json             # ★ 核心配置 (见下文)
├── MEMORY.md               # 关键事实记忆 (device_id 约束等)
├── 账号管理.bat            # ★ 图形界面入口 (账号/API管理/设置/日志)
├── start.bat               # 一键启动 (建 venv / 装依赖 / 拉起网关)
├── start_hidden.ps1        # 隐藏窗口启动网关 + 签到 (供开机自启调用)
├── open-ai-autostart.bat   # 开机自启入口 (放启动文件夹)
├── providers/
│   ├── __init__.py         # Provider 注册表 + 模型路由
│   ├── base.py             # Provider 基类
│   ├── workbuddy.py        # WorkBuddy (腾讯/混元) provider
│   └── trae.py             # Trae provider (路由到本地 Node 后端)
├── trae/
│   ├── server.js           # Trae 内嵌 Node 后端 (:18787)
│   └── lib/                # Trae 网络栈依赖 (sscronet.dll + @aha-kit/net)
├── scripts/
│   ├── gui_account_manager.py  # ★ 图形界面主程序 (账号/API管理/设置/日志)
│   ├── api_store.py        # API 密钥存储管理 (创建/命名/删除)
│   ├── account_manager.py  # 控制台版账号管理 (积分查询等, GUI 复用其逻辑)
│   ├── signin_all.py       # 统一签到脚本 (TRAE + WorkBuddy + token 续期)
│   ├── login_trae.py       # 登录/添加 Trae 账号
│   └── login_workbuddy.py  # 登录/添加 WorkBuddy 账号
├── pic/                    # 软件图标资源
├── logs/                   # ★ 运行日志 (运行时生成, 不入库)
├── data/                   # ★ 运行状态 (运行时生成, 不入库)
└── .venv/                  # Python 虚拟环境 (运行时生成)
```

---

## ⚙️ 3. 配置 `config.json`

核心是 `providers` 段：

```jsonc
{
  "api_key": "YOUR_API_KEY_HERE",       // ★ 网关访问密钥 (客户端用)
  "providers": {
    "workbuddy": {
      "domain": "www.workbuddy.cn",
      "product": "SaaS",
      "accounts": [                     // 多账号池: [{userId, accessToken, refreshToken}]
        { "userId": "...", "accessToken": "...", "refreshToken": "..." }
      ]
    },
    "trae": {
      "enabled": true,
      "device_id": "<<真实 device_id>>",   // ★ 见下方「重要约束」
      "headers": {
        "x-device-id": "<<真实 device_id>>" // ★ 必须与上面一致
      },
      "accounts": [ { "uid": "...", "token": "...", "cookie": "..." } ]
    }
  }
}
```

### ⚠️ 重要约束：`device_id` 必须由 Trae 客户端生成

> 详见 [`MEMORY.md`](MEMORY.md)。一句话：网关**不会、也不能**自己生成合法的 `device_id`。

- TRAE 的签到接口用 HTTP 头 `x-device-id` 做风控维度。
- 合法值来自 **Trae 桌面客户端**首次启动时生成的随机 UUID，存于：
  `C:\Users\<用户>\AppData\Roaming\TRAE SOLO CN\machineid`
- **不要**填占位符、也不要自己伪造——服务端只认"客户端生成并上报过"的真值，
  其余一律返回 `code:9074`（伪装成"当前参与用户太多"）。
- 正确做法：从上面的 `machineid` 文件复制真实值，填入 `config.json` 的
  `providers.trae.device_id` **和** `providers.trae.headers.x-device-id`（两处都要）。
- `device_id` 绑定「机器 + 当前 Trae 安装」：重装 Trae / 清空 AppData 会变化，需重新提取。

---

## 🖥️ 4. 使用

### 图形界面（推荐）

双击 `账号管理.bat` 打开管理界面，含四个页签：

| 页签 | 功能 |
|---|---|
| **已添加账号** | 查看 TRAE / WorkBuddy 账号与积分，刷新积分、添加账号、重新连接 |
| **API 管理** | 创建 / 命名 / 复制 / 删除网关 API 密钥（改后即时生效） |
| **设置** | 开机自动运行开关、一键卸载 |
| **操作日志** | 查看后台操作输出 |

### 客户端接入示例

把任意 OpenAI / Anthropic 兼容客户端指向本网关：

```
Base URL : http://127.0.0.1:8000/v1
API Key  : <config.json 里的 api_key>
```

例如在 CC Switch / Claude Code 中：

```
ANTHROPIC_BASE_URL = http://127.0.0.1:8000
ANTHROPIC_MODEL    = workbuddy-hy3      # 走腾讯混元
```

### 启动 / 停止

```bat
start.bat                 :: 前台启动 (建 venv + 拉起网关/Node)
open-ai-autostart.bat     :: 隐藏窗口启动 (网关 + 守护)
```

开机自启：在「账号管理 → 设置」页勾选「开机自动运行」。

---

## 🔀 5. 模型路由

请求体里的 `model` 字段决定走哪个上游（见 `providers/__init__.py` 的 `route_provider`）：

| model 含 / 前缀 | 路由到 |
|---|---|
| `trae` / `tr-` | Trae 本地 Node 后端（Work 积分通道） |
| `workbuddy` / `wb-` | WorkBuddy 腾讯网关（混元等） |
| 其他 | 第一个可用 provider |

常用模型名（内置别名）：
- `hy3` / `wb-hy3` → 腾讯混元
- `deepseek-v4-flash` / `deepseek-v4-pro` → DeepSeek
- `trae-flash-official` → Trae DeepSeek-V4-Flash-Official

---

## 📅 6. 签到

统一脚本 `scripts/signin_all.py`：

```bash
python scripts/signin_all.py             # 完整: token 续期 + WorkBuddy + TRAE 签到
python scripts/signin_all.py --no-renew  # 只签到, 不续期
python scripts/signin_all.py --force     # 强制续期
python scripts/signin_all.py --trae-only # 只补试 TRAE (供 daemon 反复调用)
```

- **WorkBuddy**：调用 `daily-checkin`，`code:0` 成功 / `10001` 今日已签。
- **TRAE**：先查 `status` 再 `claim`；`device_id` 不合法会返回 `9074`；
  服务端繁忙时由 `daemon` 每 30 分钟自动补试。
- 日志见 `logs/signin.log`，状态见 `data/.trae_signin_state`（`done`/`pending`）。

---

## 🛠️ 7. 排错

| 现象 | 排查 |
|---|---|
| TRAE 签到一直 9074 | 检查 `device_id` / `x-device-id` 是否为真实 machineid（见 §3），占位符必 9074 |
| 网关起不来 | 看 `logs/open_api_err.log`；确认 `start.bat` 已建好 `.venv` 并装了依赖 |
| 端口被占 | 8000/18787 已运行则跳过；`netstat -ano \| findstr :8000` 查占用 |
| 开机没自启 | 确认「设置」页已勾选「开机自动运行」 |
| daemon 没在跑 | 计划任务每 5 分钟通过 `watchdog_boot.py` 拉起；也可手动 `pythonw daemon.py` |
| 添加账号不显示 | 确认登录脚本无报错；查看「操作日志」页；点「重新连接」刷新账号池 |

---

## 📦 8. 版本与发布

- **当前版本**：`v2.0`
- **安装包**：[Releases](../../releases) 页下载 `open-ai-installer.exe`（一键安装器）
- 安装器功能：选目录 → 自动部署环境 → 创建快捷方式 → 询问开机自启

---

## ⚖️ 免责声明

本项目仅供**个人学习与研究**使用。它通过逆向手段对接第三方服务，可能违反相关服务的
用户协议；由此产生的任何后果由使用者自行承担。请遵守各上游服务的服务条款，勿用于
商业用途或大规模分发。

---

## 📄 关键文件速查

| 我想… | 看 / 改 |
|---|---|
| 管理账号/积分/API/自启/卸载 | `账号管理.bat`（GUI）→ `scripts/gui_account_manager.py` |
| 管理 API 密钥 | GUI「API 管理」页 → `scripts/api_store.py` |
| 换模型/加别名 | `config.json` → `providers.workbuddy.models` |
| 修签到失败(9074) | `config.json` → `providers.trae.device_id` / `headers.x-device-id` |
| 懂 device_id 约束 | `MEMORY.md` |
| 调守护节奏 | `daemon.py`（`CHECK_INTERVAL` / `TRAE_RETRY_INTERVAL`） |
| 加 Trae 账号 | 账号管理 GUI 或 `scripts/login_trae.py` |
| 加 WorkBuddy 账号 | 账号管理 GUI 或 `scripts/login_workbuddy.py` |
