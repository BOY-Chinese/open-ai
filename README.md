# open-ai — 本地多通道 AI 聚合网关

把多个上游 AI 供应商聚合成本地统一的 **OpenAI 兼容接口**（`/v1/chat/completions`、`/v1/models`）与
**Anthropic 兼容接口**（`/v1/messages`），供 Claude Code、CC Switch、Cherry Studio 等任意兼容客户端
统一调用；并附带一个桌面管理端（Tauri 2 + React 18），负责账号、模型、路由链、密钥、积分与日志的可视化管理。

- 单端口网关：`http://127.0.0.1:8000`（可配置），Bearer 鉴权
- 四个通道：**WorkBuddy（腾讯）**、**WorkBuddy 国际版**、**Trae（字节）**、**Loomy（讯飞）**
- 多条自定义 **Auto 路由链**：把真实模型串成虚拟模型，按序故障转移
- 桌面端 + 管理面 REST：账号「真状态」、模型列表与倍率、API 密钥、积分看板、操作日志、一键更新

> 当前版本 **v3.3**（`version.py` 单源定义 `APP_VERSION` / `UPDATE_CHANNEL`）。
> dev（开源/开发者版）与 portable（普通用户版）两个通道**功能完全一致**，区别只在交付形态
> （前者装源码、需自备 Python 3.10+；后者装冻结 exe + 内嵌运行时）。

---

## v3.3 更新内容

本次为**六项稳定性修复**，不改界面、不新增功能，全部是「长期困扰但难排查」的问题被找到根因并治本。

### 1. Trae 通道模型列表消失、点刷新无效 —— 治本

部分模型（`tr-glm-5.3-flash`、kimi、qwen、doubao 全系约 20 个）不在列表里，点「刷新模型列表」也刷不出来，但**直接调用完全正常**。根因是首拉失败被「记忆化」永不重试、且与 token 续期赛跑必败，于是长期回落到只有 15 个模型的静态兜底表。

现已：失败不再记忆化（下次自动重拉）、指数退避防打挂上游（10s→120s）、首拉改到首次健康检查之后、新增强制刷新入口、**成功拉到的模型目录落盘快照**（重启后即使上游短暂失败也能先恢复完整列表）、拉取改为逐账号轮换尝试。修复后列表从 15 行恢复到 **47 行**。

### 2. 「本轮运行失败 / Event loop is closed」偶发 —— 根除

用 DSH 时**偶尔**整轮失败，重试又好了。根因是管理面把需要触碰 provider 长寿命连接池的协程丢进线程池用 `asyncio.run` 跑，线程里的事件循环返回时被关闭，连接池里残留的跨循环连接在后续请求中爆雷——**是否撞上是概率性的**，这正是「偶尔」的来源。

现已让这些端点全部改为主事件循环直接 `await` 或 `to_thread`（不再创建任何事件循环），并把易复发的入口改成 fail-fast。连续触发此前必现的两个高发场景，日志**零新增**。

### 3. 报错信息为空 `{"message":"","type":"upstream_error"}` —— 根治

API 偶发报错时 message 是**空字符串**，完全看不出是哪条通道、什么原因。根因是 httpx 在「传输中途读失败」时抛出的异常**不带任何消息文本**，而网关直接把异常文本透传，异常一空错误就空。

现已新增统一的异常描述助手，把网络层异常映射成**非空的中文语义描述**（读超时 / 连接中途被重置 / 拒连等）；同时对「TCP 建连失败」自动重试 2 次（请求尚未发出，幂等安全）。

> **设计取舍（有意为之）**：流式传输**中途**不做自动重试——部分内容已发给客户端，重发会造成重复输出或工具调用错乱，只能把原因说清楚。建连重试与中途不重试的边界严格分开。

### 4. 流式响应缺失用量 —— 修「上下文占用恒为 0」

在客户端里用本网关任何通道，**上下文占用恒显示 0**，换官方 API 就正常。根因是上游在流尾补发的用量事件没有 `choices` 字段，被「遍历 choices」的循环静默丢弃——**数据一直是有的，只是网关没往外发**。

现已按 OpenAI 规范补发用量尾块（`choices: []` + `usage`），四通道全部生效，且严格按 `stream_options.include_usage` 门控：**老客户端行为完全不变**（不发尾块），新旧混跑安全。

### 5. Loomy 账号「手动关闭后再也开不回来」—— 修掉

桌面端把 Loomy 账号关掉后，再点开关**启用不回来**，账号永远停在「已停用」，反复点都没反应（接口实际返回 500）。根因是一处生成器表达式里**解包发生在过滤之前**，而 Loomy 的「桌面 + Web」两半边常只存在一边，对 `None` 解包当场抛异常。

异常发生在写盘**之前**，所以配置没被改坏，只表现为「点了没反应」。现已改为「先过滤缺失半边、再解包取反」，并补上回归套件。

### 6. Claude Code 报「response stream was malformed」+ 上游 11148 —— 根因根治

Claude Code 频繁出现流格式错误，且**越用越频繁**，上游提示「新建会话重试」看似有效但很快又犯。这是两个**互为因果**的网关缺陷形成的死循环：

- **缺陷一（报 malformed）**：流式转换时文本块硬编码 `index=0`，而工具块也从 0 开始，模型「先说一句再调工具」时两个块**共用索引 0**，违反协议。附带损害更隐蔽——工具参数丢失或**串到另一个工具上**。
- **缺陷二（报 11148）**：请求侧消息序列不做配对校验，而畸形消息**全部由缺陷一亲手制造**（客户端把错误文本作为 assistant 消息写回，恰好插在工具调用与结果之间），污染持续累积，旧会话因此**永久性失败**。

现已修复流式索引冲突（文本块占用 0 时工具块整体后移）并新增请求侧消息序列规范化。效果：工具参数完整归位、六种畸形消息由「5/7 触发 11148」变为 **7/7 通过**，且**此前被污染的旧会话无需新建即可直接恢复**。

---

## 一、现有功能

### 1. 通道聚合

| 通道 | 模型前缀 | 上游 | 说明 |
|---|---|---|---|
| WorkBuddy | `wb-` | copilot.tencent.com（内置 DeepSeek 等） | 国内主通道，积分制 |
| WorkBuddy 国际版 | `wbie-`（兼容旧 `wbai-`） | www.workbuddy.ai | 网页端 Cloud Agent 拿每日活跃积分 |
| Trae | `tr-` | TRAE 内嵌 Node 后端（TTNet 签名，:18787） | Work 积分通道，DeepSeek-V4 系列 |
| Loomy | `lm-` | loomyad.xunfei.cn | 讯飞办公助手，静态 apiKey |

### 2. 网关能力

- **OpenAI 兼容**：非流式 + 流式 + 工具调用（function calling）；`/v1/models` 聚合四通道模型列表
- **流式用量回传**：按 OpenAI 规范在流尾补发用量尾块（`choices: []` + `usage`），客户端可正确显示上下文占用；
  严格按 `stream_options.include_usage` 门控，老客户端行为不变
- **Anthropic 兼容**：`/v1/messages`（含流式），协议转换在网关内完成，客户端无需改造；
  流式 content block 索引唯一、请求侧消息序列自动规范化（修 Claude Code malformed / 11148）
- **错误信息保证非空**：网络层异常统一映射为中文语义描述（读超时 / 连接被重置 / 拒连等），
  不再出现 `{"message":""}` 这种无从排查的空错误
- **多 API 密钥**：`api_keys` 列表管理，创建/改名/删除即时生效（无需重启网关）
- **模型列表每日自动同步上游**：上游加模型无需改代码；历史裸名与旧前缀请求保持兼容
- **Trae 模型目录快照**：成功拉到的动态目录落盘，重启后上游短暂失败也能先恢复完整列表

### 3. Auto 路由链（虚拟模型）

- **任意多条自定义链**：链名即模型名——`model` 填链名即可调用该链；总名「Auto路由链」
  （历史别名「Auto路由连」/「Auto-mode」仍兼容）命中第一条启用链
- **顺序故障转移**：链内按顺序逐个尝试，超时/失败自动切下一个，第一个成功者胜出
- **逐模型独立超时**（0 = 使用通道默认值）；链可启用/关闭、右键 编辑 / 检查 / 删除
- 「检查」对链上模型向上游发极短探测请求，三态报告：🟢 正常 / 🟡 繁忙 / 🔴 断连

### 4. 桌面管理端（7 个页面）

| 页面 | 能力 |
|---|---|
| 账号管理 | 四通道账号增删、启用开关、**真状态监控**（🟢 正常 / 🔴 断连 / 🟠 服务未响应）、Chromium 登录助手、刷新积分、补签、重连 |
| API 管理 | 密钥创建/重命名/删除，创建时间持久化 |
| 模型列表 | 四通道模型与积分倍率（三态：真 0 显示 0.00、未知显示 `--`）、显隐/置顶、**右键「检查该模型」**（向上游发极短探测请求，右下角 Toast 报告结果，不加状态列） |
| 积分看板 | 今日/本周积分（本地流水库聚合，毫秒级） |
| Auto路由链 | 多链管理、编辑器（增删/排序/逐模型超时）、链检查与单模型检查、模型简报 |
| 操作日志 | 记录用户在界面上的每个动作与结果，含登录脚本输出实时跟随 |
| 系统设置 | 开机自启、一键更新、一键卸载 |

### 5. 账号与积分

- **真状态监控**：网络异常**绝不判死**账号；鉴权失败才标红；重登后自动复活；失效账号自动跳过
- **每日签到/拿分**：国内通道走签到接口；国际版走网页端 Cloud Agent 活跃路径（服务端延迟自动入账）
- **积分流水采集**：后台每 5 分钟快照进本地 SQLite，看板按今日/周聚合展示

### 6. 一键更新

检查 → 下载（带进度、`.part` 临时文件原子改名）→ 确认 → UAC 提权 → 退出进程 → 安装器接管。
按 `UPDATE_CHANNEL` 在 GitHub Release Assets 中**精确匹配**对应安装包
（dev → `open-ai-installer-dev.exe`，portable → `open-ai-installer-portable.exe`），版本号按数字段比较。

### 7. 交付形态

| | **dev 版**（本仓库构建） | **portable 版** |
|---|---|---|
| 安装包 | `open-ai-installer-dev.exe`（约 90 MB） | `open-ai-installer-portable.exe`（约 422 MB） |
| 形态 | 源码分装 + 离线依赖轮子 | 冻结 exe + 内嵌 Python/Node 运行时 |
| 运行依赖 | 自备 Python 3.10+（**依赖已内置，离线安装**） | 机器零依赖 |
| 内置桌面端 | `desktop\open-ai-desktop.exe`（Tauri，前端已嵌入） | 同左 |

---

## 二、技术实现

### 1. 统一网关层（`main.py`，FastAPI）

- `POST /v1/chat/completions`：按模型名路由到 provider，支持非流式 / 流式（SSE）/ 工具调用；
  流式用 `StreamingResponse` 透传上游增量
- `POST /v1/messages`（含 `/v1/v1/messages` 别名）：`anthropic_api.py` 做 **OpenAI ↔ Anthropic 双向协议转换**
  （消息结构、系统提示、工具调用与流式事件的相互映射），网关内部只跑一种中间表示
  - 流式侧：每个 content block **独占一个 index**（文本块占 0 时工具块整体后移），
    并在流尾把上游真实用量写进 `message_delta.usage`
  - 请求侧：`sanitize_openai_messages()` 规范化消息序列——补悬空 tool_call 的占位结果、
    空 id 补合成 id（**不丢弃**，保住内容）、孤儿结果降级为文本、相邻 user 合并；
    工具结果先于同一 user 消息里的文本发出（OpenAI 要求 `role=tool` 紧跟带 tool_calls 的 assistant）
- 每次请求**实时重读 config.json 校验密钥**——管理页增删密钥立即生效，无需重启
- `GET /` 健康检查 + provider 概览；`POST /v1/admin/reload-providers` 热重建通道实例（如 Loomy 登录后）

### 2. 通道前缀路由（`providers/__init__.py`）

`route_provider()` 按模型名前缀/关键字路由，**判断顺序有讲究**（如 `wbie-` 必须先于 `wb-` 判断，
否则国际版被误判给国内版；`lm-`/`loomy` 最先）：

```
lm-* / *loomy*        → loomy
tr-* / *trae*         → trae
wbie* / wbai* / *intl* → workbuddy-intl
wb-* / *workbuddy*    → workbuddy
其他                   → 第一个注册的 provider
```

### 3. Auto 路由链（`auto_router.py`）

- 链配置存 `config.json` 的 `auto_chain.chains[]`（id / 名称 / enabled / models[{model, timeout}]），
  读取时归一化并自动迁移旧单链格式；链名去重（重名自动补序号）
- `resolve_auto_chain()`：总名 → 第一条启用链；链名精确匹配（忽略大小写）；
  **链名与真实模型重名时让位给真实模型**（避免抢路由）
- `auto_chat` / `auto_stream`：顺序故障转移；流式版只有产出**真实内容**（content / reasoning / tool_calls）
  才算命中，之前缓冲的空 chunk 在切换模型时静默丢弃
- `check_model()`：向单个模型发极短探测请求（`"回复ok"`，上限 30s），按错误特征归因三态
  ——「正常 / 繁忙（限流、5xx、超时）/ 断连」，同时服务路由链「检查」与模型列表「检查该模型」

### 4. 四通道「真状态」（`providers/base.py`）

- `AccountHealth`：进程内线程安全的账号运行态，账号管理页「真状态」的数据源
- 三态区分：**鉴权失败（判死）≠ 网络异常（绝不判死）≠ 服务未响应（整通道标红，修法不同）**
- 鉴权判死钩子自动跳过失效账号（不再发出注定失败的请求）；`observe_token` 成功即复活
  （Loomy 重登无需重启网关）

### 5. 进程模型：Broker + Job Object + IPC（`app_runtime.py` / `ipc.py` / `jobmgmt.py`）

- **Broker**（`daemon.py` 常驻 / `bootstrap.py` 控制）是唯一的进程管理者，网关、Trae Node、
  任务进程都作为 leaf Job 由它 spawn 与监督——「都只与 Broker 通信，不亲手 spawn」，进程树永远清晰
- Windows **Job Object**（KILL_ON_JOB_CLOSE）保证父进程死亡时子进程不残留；
  稳定运行 5 分钟后崩溃退避重置
- **IPC 协议**（命名管道）：子进程启动发 `HELLO`、周期 `HEARTBEAT`、收到 `SHUTDOWN` 自行清理退出
  （优雅退出协议）；`bootstrap.py status/doctor` 可查各角色 pid / 心跳 / 管道健康
- 每日任务（签到、积分采集）由 TaskScheduler 经 **task shim**（`procname.py` 品牌化命名
  `open-ai-task.exe`）拉起，源码态与打包态同一套代码

### 6. 管理面 REST（`admin_api.py`，40 条路由 `/v1/admin/*`）

- 设计约定：**GET 只读本地（毫秒级），POST 才联网**（刷新积分、拉模型目录、检查更新等）
- 覆盖：账号（登录/真状态/签到/重连）、模型（列表/倍率/刷新/**检查**）、Auto 路由链（CRUD/检查）、
  API 密钥、积分（今日/周/采集）、日志、设置（自启）、版本与更新（check/download/update-state）
- 统一 Bearer 鉴权（`require_auth` 依赖），挂载在主应用上

### 7. 积分流水本地库（`scripts/usage_collector.py` + `scripts/credits_api.py`）

- 后台每 5 分钟把四通道用量快照写进本地 SQLite（`data/`），看板聚合全部走**本地库**（不联网）
- 「刷新积分」先催一次增量采集（幂等）再重读接口——否则读到的永远是上一份快照

### 8. 桌面端工程要点（`desktop-ui/`，React 18 + TypeScript + Tailwind + Tauri 2）

- **数据源抽象**：`lib/httpBackend.ts`（真实网关）与 `lib/backend.ts`（演示数据）同契约可切换，
  外层包一层 **oplog Proxy**（`lib/oplogBackend.ts`）——新增写操作自动进操作日志，不会漏记
- **首帧秒开**：模型列表 + 视图偏好落 `localStorage`，先渲染缓存再后台拉新；
  通道/显隐筛选纯本地过滤，不触发网络请求
- **配置热跟随**：管理面请求遇 401 自动重读 config.json 的地址与密钥并重试一次（改配置不用重启界面）
- Tauri 侧：托盘（左键恢复 / 关闭隐藏 / 菜单退出=优雅停 Broker）、**单实例**（防双托盘）、
  打包态经 Rust 命令读 config.json 拿网关地址与密钥（前端产物不含密钥明文）、
  后端未运行时自动拉起并轮询就绪（「启动门」）

### 9. 一键更新链路（`version.py` + `admin_api.py /version/*` + Tauri `install_update`）

- `version.py` 单源定义 `APP_VERSION` / `UPDATE_CHANNEL` / `UPDATE_REPO`，发布时同步修改并打同名 tag
- 版本比较按**数字段**（不误报、不降级）；下载写 `.part` 临时文件、核对 `Content-Length` 后原子改名；
  安装器路径**白名单**（只接受 `data\updates\` 下的 exe），UAC 提权为设计行为
- 按 `UPDATE_CHANNEL` 在 Release Assets 中精确匹配安装包（文件名须同时含 `installer` 与通道关键词），
  匹配不到时明确报「没有本通道的安装包」，**绝不回落到「随便下一个」**

### 10. 流式用量与错误语义（`providers/base.py` 公共助手）

- `normalize_usage` / `wants_usage_in_stream` / `make_usage_chunk`：把各上游形状不一的 usage 归一成三段式
  并构造规范尾块。**只补不删**——附加字段（如 `prompt_tokens_details.cached_tokens`）原样保留，
  客户端据此计算缓存命中
- `describe_net_exc`：httpx 网络层异常 → 非空中文语义描述；带消息的异常保留原文，未知异常兜底类名，
  **任何情况不返回空串**
- `AsyncHTTPTransport(retries=2)`：仅对「TCP 建连失败」重试（请求未发出，幂等安全）

### 11. 安装包与离线依赖（`installer/`）

- **dev 版 = 源码分装**：`resources.zip` 装全部后端源码 + 桌面端 + 卸载器，`config.shell.json`
  提供全占位空壳配置；**全量 Python 依赖轮子（56 个）按生产 venv 同版内置**，安装时
  `pip --no-index --find-links` 本地装——不访问 PyPI 镜像（根因：清华源对 pip 26.x 返回 403、
  大文件流被镜像限速掐断），仅 Python 版本无匹配轮子时回退网络源（阿里云主源，逐源可见日志）
- **portable 版 = 冻结 exe + 内嵌运行时**（PyInstaller onedir/onefile + Node + Chromium），机器零依赖
- `build_all.py` 一键打包，内置多道**构建门禁**：桌面端前端指纹校验（防嵌旧前端/演示密钥）、
  frozen-path 扫描（裸 `__file__`）、未定义名字静态扫描、脚本拉起冻结安全测试、安装包体积闸门

---

## 三、代码架构

```
open-ai/
├── main.py               # 网关入口：/v1/models、/v1/chat/completions、/v1/messages + 模型路由
├── admin_api.py          # 管理面 REST：/v1/admin/* 40 条路由（账号/模型/路由链/密钥/积分/日志/更新）
├── anthropic_api.py      # OpenAI ↔ Anthropic 协议转换（含流式事件映射）
├── auto_router.py        # Auto 路由链：多链归一化/故障转移/check_model 探活
├── providers/
│   ├── __init__.py       # provider 注册 + route_provider 前缀路由
│   ├── base.py           # Provider 抽象基类 + AccountHealth（真状态）
│   ├── trae.py           # Trae 通道（转发内嵌 Node 后端 :18787）
│   ├── workbuddy.py      # WorkBuddy 通道（腾讯）
│   ├── workbuddy_intl.py # WorkBuddy 国际版通道
│   └── loomy.py          # Loomy 通道（讯飞）
├── trae/                 # Trae DeepSeek 后端（Node，TTNet 签名，OpenAI 兼容 + function calling）
│   ├── server.js
│   └── lib/
├── app_runtime.py        # Broker：进程监督 / Job 托管 / IPC 服务端 / TaskScheduler
├── bootstrap.py          # 统一进程入口：start / stop / restart / status / doctor（open-ai.exe shim）
├── daemon.py             # Broker 常驻入口（兼容旧计划任务）
├── ipc.py                # Broker ↔ 子进程 IPC 协议（HELLO / HEARTBEAT / SHUTDOWN）
├── jobmgmt.py            # Windows Job Object 托管（KILL_ON_JOB_CLOSE）
├── procname.py           # 进程品牌化命名（open-ai-gateway / open-ai-task / open-ai-cli）
├── app_paths.py          # 安装根定位（源码态 / 冻结态统一，CONFIG_PATH / DATA_DIR）
├── version.py            # APP_VERSION / UPDATE_CHANNEL / UPDATE_REPO 单源
├── scripts/              # 任务脚本（task shim 拉起）：签到、积分采集、登录助手、积分 API
│   ├── signin_all.py         # 每日签到（逐账号去重 + 退避重试）
│   ├── usage_collector.py    # 积分流水采集（每 5 分钟快照 → data/ SQLite）
│   ├── wb_web_daily.py       # 国际版网页端拿分（Cloud Agent 活跃路径）
│   ├── login_*.py            # 各通道 Chromium 登录助手（输出实时跟随到界面）
│   ├── credits_api.py        # 积分聚合查询（本地库）
│   └── ...
├── desktop-ui/           # 桌面管理端（React 18 + TypeScript + Tailwind + Tauri 2）
│   ├── src/pages/            # Accounts / AutoRouter / Models / Api / Credits / Logs / Settings
│   ├── src/lib/              # 数据源（httpBackend + mock + oplog 埋点 + 模型缓存）
│   ├── src/components/       # 布局 / 通用组件 / 反馈（Toast）/ 数据表
│   ├── src-tauri/            # Rust 侧：托盘 / 单实例 / 配置读取 / 更新安装
│   └── tools/                # 构建 + 门禁脚本（build-tauri-release.ps1、check-ps1.ps1…）
├── installer/            # 安装器（PyInstaller）
│   ├── build_all.py          # dev 包一键打包（含全部门禁）
│   ├── installer.py          # 安装向导（venv 创建/离线依赖/资源解压/快捷方式）
│   ├── build_resources.py    # resources.zip 组包（关键条目强制校验）
│   ├── check_*.py            # 构建门禁：桌面端指纹 / frozen-path / 未定义名 / 登录浏览器
│   └── wheels/               # 离线依赖轮子（与生产 venv 同版，56 个）
├── tests/                # 23 个测试文件（pytest 或直接运行均可）
│   ├── test_stream_usage.py          # 流式用量尾块回归（归一化/门控/顺序/auto 链不吞）
│   ├── test_admin_account_toggle.py  # 账号开关回归（Loomy 半边组合 × 开关两方向）
│   └── ...
├── data/                 # 运行时数据（积分流水 SQLite 等，不入库）
└── logs/                 # 运行日志（网关 / 登录 / 更新，不入库）
```

---

## 四、配置说明（config.json）

单文件配置 `config.json`（安装根目录下；安装包首装会生成全占位空壳，敏感值不入库不入包）：

```jsonc
{
  "api_key": "...",              // 兼容旧字段；密钥统一维护在 api_keys
  "api_keys": [                  // 多密钥（API 管理页维护，增删即时生效）
    { "name": "...", "key": "sk-...", "createdAt": 1730000000 }
  ],
  "host": "0.0.0.0",
  "port": 8000,
  "max_req_per_min": 0,
  "providers": {                 // 四通道账号与开关（账号由界面登录后写入）
    "workbuddy":       { "accounts": [ /* accessToken 等 */ ] },
    "trae":            { "device_id": "...", "use_work_credits": true, "models": { ... } },
    "workbuddy-intl":  { "accounts": [ /* cookie 等 */ ] },
    "loomy":           { "accounts": [ /* 讯飞 session */ ] }
  },
  "auto_chain": {                // Auto 路由链（界面维护）
    "chains": [
      { "id": "c1726...", "name": "主力链", "enabled": true,
        "models": [ { "model": "tr-...", "timeout": 120 } ] }
    ]
  }
}
```

> 密钥、cookie、device_id 等敏感值只存在于本机 `config.json`；仓库与安装包只携带全占位空壳。

---

## 五、快速开始

### 方式 A：安装包（推荐）

从 [GitHub Releases](https://github.com/BOY-Chinese/open-ai/releases) 下载对应通道的安装包：

- **开发者版** `open-ai-installer-dev.exe`：装源码形态；依赖已内置（离线安装），仅需自备 Python 3.10+
- **便携版** `open-ai-installer-portable.exe`：内嵌 Python/Node 运行时，机器零依赖

安装后桌面快捷方式拉起 `desktop\open-ai-desktop.exe`，界面「启动门」会自动拉起网关并等待就绪。

### 方式 B：源码运行

```bat
git clone https://github.com/BOY-Chinese/open-ai.git
cd open-ai
start.bat          :: 首次运行自动创建 .venv 并装依赖, 随后拉起网关
```

### 客户端接入

任意 OpenAI 兼容客户端，指向 `http://127.0.0.1:8000/v1`，API Key 填 `config.json` 中 `api_keys` 里的密钥：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer <你的密钥>" \
  -H "Content-Type: application/json" \
  -d '{"model": "wb-deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]}'
```

`model` 可填：四通道任一模型名（带前缀）、某条 Auto 路由链的链名、或总名「Auto路由链」。
Claude Code 等 Anthropic 客户端把 base URL 指到 `http://127.0.0.1:8000` 即可（`/v1/messages` 已兼容）。

---

## 六、开发与构建

```bat
:: 后端测试（pytest 或无 pytest 环境直接运行均可）
.venv\Scripts\python.exe -m pytest tests\ -v
.venv\Scripts\python.exe tests\test_auto_chain_admin.py

:: 前端类型检查 / 开发 / 构建
cd desktop-ui
npm run typecheck
npm run dev                  :: Vite :1420, /v1 代理到网关并注入密钥
npm run build                :: tsc --noEmit + vite build（产物 dist/）

:: 桌面端 Tauri 构建（产物覆盖 desktop\open-ai-desktop.exe）
powershell -ExecutionPolicy Bypass -File desktop-ui\tools\build-tauri-release.ps1

:: dev 安装包一键打包（含全部门禁, 产物 installer\dist\open-ai-installer-dev.exe）
cd installer && python build_all.py
```

构建约束（踩过的坑，门禁已固化）：

- 打包脚本必须 UTF-8 with BOM（PowerShell 5.1 会以 ANSI 解码无 BOM 脚本，中文注释吞语句）；
  构建成败以**退出码**判定，不看产物是否存在
- 桌面端 exe 构建前必须先 `npm run build`；安装器按「前端 bundle 文件名指纹」校验 exe 内嵌的是
  当前 dist，新 bundle 哈希需登记进 `installer/check_desktop_bundle.py` 的白名单
- 新增依赖版本以**生产 venv 为准**同步进 `installer/wheels/`（离线安装的版本一致性来源）

---

## 七、版本与更新

- 版本号单源在 `version.py`（如 `dev-v3.3`），发布时同步打同名 GitHub tag
- 界面「一键更新」：`UPDATE_CHANNEL` 精确匹配 Release Assets 中的安装包（dev ↔ dev、portable ↔ portable），
  版本数字段比较，只在确实有更新时提示
- 更新包下载到 `data\updates\`（路径白名单），UAC 提权安装；配置文件保留并另存时间戳备份

---

## 许可与说明

- 本项目为本地个人工具，聚合的各通道账号需自行注册与登录；请遵守各上游服务的使用条款
- 仓库不含任何真实密钥 / cookie / device_id（安装包只携带全占位空壳配置，构建门禁会做真值扫描）
