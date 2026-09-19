# open-ai 本地聚合网关

一个跑在本地的 AI 网关，把多个上游供应商（**WorkBuddy / 腾讯混元**、**Trae / 字节**）聚合成统一的
OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models`）与 Anthropic 兼容接口（`/v1/messages`），
供 Claude Code、CC Switch、OpenAI 客户端等任意兼容工具统一调用。

> **当前版本: v3.2**（`version.py` 单源定义 `APP_VERSION` / `UPDATE_CHANNEL`）
> - **dev 版**（开源/开发者版）: `APP_VERSION='dev-v3.2'`、通道 `dev`，需自备 Python 3.10+ 运行环境；
> - **portable 版**（普通用户版）: `APP_VERSION='portable-v3.2'`、通道 `portable`，安装包内建运行时，
>   对电脑运行环境要求大大降低。
> 两个通道互不干扰，「一键更新」按 `UPDATE_CHANNEL` 在 GitHub Release Assets 中精确匹配对应安装包
> （dev → `open-ai-installer-dev.exe`；portable → `open-ai-installer-portable.exe`）。
>
> **v3.2 起 dev 与 portable 两个通道功能完全一致**，区别只在交付形态（前者装源码、需自备 Python；
> 后者装冻结 exe + 内建运行时）。因此两个安装包的界面与功能一一对应，无需分别维护功能清单。

## v3.2 更新内容

1. **Auto 路由链多链化**：由原先单条链改为**多条自定义路由链**。请求时 `model` 填链名即可调用该链，
   链内按顺序故障转移、第一个成功的模型胜出；支持链名自定义、启用/关闭、逐模型超时、
   右键菜单（编辑 / 检查 / 启用关闭 / 删除），以及行尾展开的模型简报（顺序 · 模型 · 通道 · 三态状态）。
   「添加新路由链」自动命名「无名N」。`/v1/models` 不再暴露总名「Auto路由链」（请求侧仍兼容）。
   > v3.2 修订：Auto 路由链页空态文案原先整体贴左（不居中）—— 原因是 `TableEmpty` 渲染的是
   > `<tr><td>`，却被直接放进 `<div>`，脱离表格布局后匿名表格盒按内容宽度收缩。已补上
   > `<table><tbody>` 容器修复，实测图标/标题/描述中心与表格中心偏差 0.0px。

2. **一键更新全链路打通**（原实现只有「查 release」，按钮点了不下载也不安装，是半个空壳）：
   检查 → 下载（后台线程，`.part` 后原子改名并核对 `Content-Length`）→ UAC 提权安装（ShellExecuteExW
   `runas`）。安装器路径有白名单（只接受 `data\updates\` 下的 exe），防止「以管理员权限运行任意程序」。
   新增下载进度 / 确认安装对话框。
3. **版本判定改为数字段比较**：原先 `latest != APP_VERSION` 的字符串不等判断会导致
   `v3.1` vs `local-v3.1` 恒判有更新、上游更旧时被降级覆盖；现按数字段比较，
   解析不出数字一律**不提示**更新。
4. **仓库地址改由后端下发**：修掉「点检查前界面显示 `github.com/owner/open-ai`」这个占位地址；
   前端删除硬编码 `UPDATE_REPO`，拿不到就不写地址而不是编造。
5. **Trae「模型返回不全面」修复**：`trae/server.js` 四处修复（`watchdogTripped` TDZ 前移 /
   `endStreamError` 错误透传 / `traeChat` 转发 `r1.error` / `processLine` 抽取 + SSE 尾包 flush）。
6. **WorkBuddy 国际版「网页端拿积分」并入**：改走 Cloud Agent（`/console/as/`）活跃路径；
   前端文案改为「积分由后台自动入账，无法通过刷新主动获取」，刷新提示剔除 `WorkBuddy_IE`
   （避免延迟入账被每天误报一次失败）。
7. **Trae 屏蔽无效模型 + 倍率三态**：双层屏蔽 `custom_model_*` 与内部工具名；
   新增 `RATE_UNKNOWN(-1)` 哨兵，倍率「未知」与「真 0」分开显示（未知显示 `--`，不再显示 `0.00`）；
   倍率主源换 `/v3/config`（目录接口降为回退）；修 `X-User-Id` 空串缺陷
   （空串会让 `/v3/config` 静默返回 `models=null`）。
8. **四通道「真状态」全部打通**：Trae / WorkBuddy / WorkBuddy 国际 / Loomy 接入运行态真实状态
   （运行态失效或服务未响应标红断连）。`providers/base.py` 新增线程安全 `AccountHealth`；
   鉴权判死钩子会自动跳过失效账号并在 `observe_token` 时复活；网络异常**绝不判死**。
9. **Trae 排队透传 / Chromium 登录 / 便携版路径修复 / 刷新逻辑**（v3.1 起）：
   登录助手优先使用包内自带 Chromium（不再强拉系统 Edge），便携版路径与刷新补签逻辑修正。

## v3.0 更新内容

1. **全新桌面端（Tauri 2 + React 18）**：暗色主题、左侧导航、四通道统一表格
   （Trae / WorkBuddy / WorkBuddy 国际 / Loomy）；
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
   > 后续清理：`launcher_main.py` 与其打包产物（`open-ai-launcher.exe` +
   > `launcher_internal/`）**已一并删除** —— 桌面快捷方式早就直接指向
   > `desktop/open-ai-desktop.exe`（见 `desktop-ui/tools/install-local-shortcut.ps1`），
   > 开机自启走 `open-ai-autostart.vbs → start_hidden.ps1`，两条链路都不经过启动器。
   > 若日后重建安装包链路需要「一键启动」入口，重写一个即可（原实现只做三件事：
   > 建 runtime、`bootstrap.py start`、拉起桌面端）。
   > `uninstall.exe` / `uninstall_internal/` **保留** —— 设置页「一键卸载」仍在调用它。
11. **没有账号时模型列表为空**：Trae 的模型列表 = 动态上游模型 + 配置别名 + 默认模型，
    后两者是静态的，账号池为空时照样列出一堆调不通的模型。现按账号门控
    （判定口径与 `trae/server.js` 一致），无可用账号则不暴露任何模型。
12. **修复「一键卸载」**：桌面端那个按钮原本是**空壳**（只弹了个 toast，没调用任何
    卸载程序）。现由 Tauri 命令拉起 `<安装根>\uninstall.exe --silent`，并在 1.5 秒后
    退出桌面端本身（卸载器要删掉 desktop 目录里的 exe，界面必须先让出文件占用）。
13. **托盘「退出」= 退出全部 open-ai 进程**：先把 Broker 优雅停掉
    （`bootstrap.py stop`，同时写 watchdog 抑制标记），再退出界面 ——
    任务管理器里不再残留 open-ai 进程。
14. **「添加账号」不再弹终端**：登录脚本只在终端打印进度（真正的交互在它打开的
    浏览器里），原实现用 `CREATE_NEW_CONSOLE` 白弹一个黑框。现改为无窗口启动，
    输出重定向到 `logs/login_<通道>.log`，登录失败仍有据可查。
15. **取消顶层 `api_key` / `api_key_name`**：密钥统一只在 `api_keys` 里维护。
    安装包空壳配置里的 `YOUR_API_KEY_HERE` 曾被当成真密钥列在 API 列表里
    （虚拟机实测显示为「无名1 YOUR_API_KEY_HERE」）。现启动时自动迁移：
    真密钥搬进 `api_keys`，占位符换成新生成的强密钥，并保证至少有一条可用密钥。
16. **API 创建时间不再显示 1970**：此前后端把 `createdAt` 硬编码为 0。现在创建时
    写入真实时间戳并持久化；老配置里没有该字段的显示「—」（未知），不伪造时间。
17. **API 列表刷新立即生效**：桌面端会在 401 时**重读 config.json 的地址与密钥并
    重试一次**，所有管理面请求加 `no-store`。此前在界面开着时改 config.json，
    旧密钥失配 → 刷新失败 → 列表保持旧数据，看起来就像刷新按钮坏了。
18. **「创建 API」按钮文案**：去掉与 `+` 图标重复的加号。
19. **模型列表加本地缓存**：倍率接口要联网（秒级），此前每次进模型列表页都要空等一次。
    现在把「四通道全量模型 + 已叠加视图偏好」的结果落一份到 `localStorage`，
    首帧直接渲染、后台再拉最新数据；通道 / 显隐筛选同时改为**纯本地过滤**，
    切换筛选不再触发网络请求（详见 `desktop-ui/src/lib/modelCache.ts`）。
20. **「系统日志」改回 v2.3 的操作日志**：这一页原先读的是 `logs/gateway_out.log`
    （网关自己在说什么），现改回 v2.3 的语义 —— 记录**用户在界面上做了什么**
    （添加账号 / 刷新积分 / 重连 / 增删改密钥 / 改模型可见性 / 改自启 …）及结果与耗时，
    格式沿用 v2.3 的 `────` 分隔 + `[账号]/[API]/[模型]/[设置]/[更新]` 领域标签 +
    `[错误]` / `[完成]` 标记，并落盘保存。
    埋点做在数据源层（`lib/oplogBackend.ts` 的 Proxy），新增写操作不会漏记。
    **登录脚本输出实时跟随**（v2.3 `_subprocess_stream` 的等价物）：
    新增 `GET /v1/admin/accounts/login/log` 增量读取 `logs/login_<通道>.log`，
    前端每秒拉一段贴进日志，于是「添加账号」在日志里是**一个完整的操作块** ——
    提示 → 脚本输出 → `[完成]`；脚本结束后自动刷新账号列表。
    网关自身的运行输出仍可从本页「日志目录」按钮或托盘菜单查看。
21. **新增外观设置（白天 / 夜间 / 跟随系统）**：系统设置 → 启动设置 → 「外观设置」。
    此前界面是「暗色主题 First」且 `<html>` 写死 `class="dark"`；现在补了完整的
    浅色令牌（逐项核算对比度，正文 17.9:1、语义色在同色 12% 底上 ≥4.5:1），
    并把默认模式改为**白天**。跟随系统走 `prefers-color-scheme` 实时切换；
    首帧由 `index.html` 内联脚本兜底，不会闪。图表配色随主题换（两套色板，
    亮色系列在白底上会看不见，见 `src/config/chart.ts`）。
22. **「每日签到」列改为只读的今日签到状态**：原先是可勾选的 Checkbox 且初值取
    `a.enabled`，把「账号是否启用」当成了「今日是否签到」——勾选既不落库也不触发
    任何签到动作。现在读 `data/usage_history.db` 的 gain 表（当日入账即签到成功的
    客观凭证），显示「已签到 / 未签到」。新增只读接口 `GET /v1/admin/accounts/signin`；
    接口不可用时显示「—」而非「未签到」（避免把接口故障渲染成「今天都没签」）。

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
├── app_paths.py            # ★ 安装根唯一定义处 (源码态=仓库根 / 打包态=exe 所在目录)
├── main.py                 # FastAPI 网关入口 (--from-broker 心跳; --broker 兼当 Broker 宿主)
├── daemon.py               # Broker 薄入口 (兼容旧调用; 实际逻辑在 app_runtime.py)
├── app_runtime.py          # ★ Broker: Job 树/IPC 服务端/监督循环/定时任务
├── auto_router.py          # 虚拟模型「Auto路由连」: 按 auto_chain.models 顺序故障转移
├── bootstrap.py            # ★ 统一控制 CLI: start/stop/restart/status/doctor
├── procname.py             # ★ 进程品牌注册表 + runtime 构建工厂 (图标/版本注入)
├── ipc.py                  # ★ 命名管道 IPC 协议 (帧编解码/心跳客户端/会话)
├── jobmgmt.py              # ★ Windows Job Object 封装 (KILL_ON_CLOSE/配额/枚举)
├── watchdog_boot.py        # 计划任务兜底保活 (委托 bootstrap; 尊重退出抑制标记)
├── version.py              # ★ 版本号定义 (APP_VERSION, 设置页显示/一键更新比较用)
├── anthropic_api.py        # Anthropic 协议 ↔ OpenAI 协议转换
├── config.json             # ★ 核心配置 (含 runtime 节: 心跳/退避/内存限额)
├── MEMORY.md               # 关键事实记忆 (device_id 约束等, 打包必读)
├── sanitize_check.py       # ★ 发布前隐私/凭据泄漏检查 (推 GitHub 前必跑)
├── installer/              # ★ 一键安装包构建 (build_exe.bat [dev|portable] → open-ai-installer-<通道>.exe)
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
│   ├── loomy.py            # Loomy (讯飞) provider (静态 apiKey; lm-* 模型 + 动态模型目录)
│   ├── base.py             # Provider 基类
│   └── trae.py             # Trae provider (路由到本地 Node 后端)
├── trae/
│   └── server.js           # Trae 内嵌 Node 后端 (:18787, 含 Broker IPC 客户端)
├── scripts/
│   ├── api_store.py        # API 密钥存储管理 (create/rename/delete)
│   ├── signin_all.py       # 统一签到脚本 (TRAE + WorkBuddy + Loomy + token 续期)
│   ├── loomy_client.py     # Loomy 通道客户端 (对话/模型目录/积分台账)
│   ├── account_manager.py  # 账号读写与积分查询 (管理接口 admin_api.py 复用其逻辑)
│   ├── usage_history.py    # ★ TRAE 逐笔积分消耗流水 (网页 dashboard 同款接口逆向)
│   ├── wb_usage_history.py # ★ WorkBuddy 逐笔消耗流水 (官网个人中心同款接口逆向)
│   ├── usage_collector.py  # ★ 逐笔流水自动采集 + 本地 SQLite 流水库 (Broker 调度)
│   ├── login_trae.py       # 登录/添加 Trae 账号
│   ├── task_main.py        # ★ open-ai-task.exe 入口 (按脚本名路由到已打包模块)
│   └── login_workbuddy.py  # 登录/添加 WorkBuddy 账号
├── tests/                  # 单元测试 (unittest, 无第三方依赖)
│   ├── test_api_store.py       # API 密钥管理逻辑测试
│   ├── test_account_parse.py   # 账号解析逻辑测试
│   ├── test_procman.py         # ★ v2.4 进程管理测试 (Job/IPC/runtime)
│   └── test_task_tick.py       # ★ TaskScheduler.tick 调度状态机回归测试
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
      "trae_dir": "C:/Users/<用户>/AppData/Local/Programs/TRAE SOLO CN"
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

七个页面：
- **账号管理**：Trae / WorkBuddy / WorkBuddy 国际 / Loomy 四通道合一表格，含每日签到（**今日是否
  签到成功的只读状态**，以积分入账为凭证，不是可勾选的开关）、当前积分、账号状态；
  底部可添加四类账号（Loomy 为图形化登录向导，全程无命令行），
  右键行可复制账号名 / 重新连接 / 删除
- **API 管理**：顶部展示网关地址（OpenAI 兼容 `http://127.0.0.1:8000/v1`、
  Anthropic 兼容 `http://127.0.0.1:8000`，按 config.json 的 host/port 生成），
  右键卡片可复制地址；下方创建 / 命名 / 复制 / 删除 API 密钥（改后立即生效）
- **模型列表**：四通道合一，含积分倍率与「请求模型名称」（即实际路由表 ai 名称）；
  右键行可复制请求模型名称 / 置顶 / 隐藏，支持多选批量操作。列表带**本地缓存**：
  再次进入页面直接渲染上次结果并在后台拉取最新数据，不再空等联网的倍率接口
- **Auto路由连**：虚拟模型「Auto路由连」的故障转移链配置页 —— 调整 `auto_chain.models`
  的顺序（上移/下移）、启用开关与单模型超时；保存写入 `config.json` 的 `auto_chain` 段，
  **立即生效无需重启**（网关每次请求都会重读配置）
- **积分看板**：今日情况（获取/消耗双卡片 + 逐笔流水）与每周情况（四通道堆叠柱状图，
  可切通道与周次）
- **操作日志**：记录用户**在界面上做过的操作**（添加账号 / 刷新积分 / 重连 / 增删改密钥 /
  改模型可见性 …）及其结果与耗时；等宽字体、级别着色、Ctrl+F 搜索、自动滚动，
  本地保存（重启后仍在）。网关自身的运行输出仍可从「日志目录」按钮或托盘菜单查看
- **系统设置**：启动设置（开机自动运行 + **外观设置**：白天 / 夜间 / 跟随系统，默认白天）、
  版本信息（一键更新，按 `UPDATE_CHANNEL`
  在 GitHub Release Assets 精确匹配：dev → `open-ai-installer-dev.exe`；
  portable → `open-ai-installer-portable.exe`；仓库 `<owner>/open-ai/releases`）、
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

### 构建一键安装包（dev / portable 通道）

全部在 **Windows 侧**执行（PyInstaller 与 Tauri 都要产 Windows 二进制）。两步：

```bat
rem 1) 桌面端 (Tauri)。npm run build 产 dist, 再由 ps1 编译 Rust 并同步到 desktop\
cd desktop-ui && npm install && npm run build
powershell -ExecutionPolicy Bypass -File tools\build-tauri-release.ps1

rem 2) 安装包: 构建 5 个品牌 exe + 资源包 + 安装器, 产物自动复制到桌面
rem    通道决定产物名: 缺省 dev → open-ai-installer-dev.exe; 用户版 → portable
installer\build_exe.bat portable
```

产物 `open-ai-installer-<通道>.exe` 内含 `daemon / gateway / task / open-ai.exe(CLI) /
trae(node 副本) / desktop\open-ai-desktop.exe / uninstall.exe + trae\lib + pic`。
**用户机器零 Python 依赖**（解释器都内嵌在 exe 里）。

打包链路上有三道闸门，都是被真实事故逼出来的：

| 闸门 | 拦的是 |
|---|---|
| `check_desktop_bundle.py` | 桌面端躺着**上一次**构建的 exe，而那次的前端还带着真实密钥 —— 用前端产物的内容哈希当指纹，指纹不对直接中止 |
| `build_resources.py` 的 `REQUIRED` | 资源包缺 `trae/lib`（Trae 网络栈）也照样打包成功，装出来 Trae 通道静默不可用 |
| `build_exe.bat` 的体积校验 | 只看「PyInstaller 没报错」而漏掉 `--add-data` 没生效的空壳包 |

★ 安装根**不含任何 `.py` 源码**（exe 方案刻意不分发源码）。因此运行期那些
「拉起某个 .py」的调用都改成「品牌 exe + 路径当路由参数」，由 exe 内打包好的
模块**按文件名**接手 —— 见 `scripts/task_main.py` 与 `main.py` 的 `--broker`。

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
| 任务管理器/`%TEMP%` 里堆积 `_MEIxxxxxx` 残留 | **不是**"onedir 已修复"（此句曾误记：品牌 exe 与 uninstall.exe 至今都是 onefile，见 `installer/build_exe.bat`）。真实成因是停止流程把 onefile 父进程连同子进程一起强杀，父进程没机会清理临时目录；已改为"先轮询 leaf job 清空、超 12s 才强杀"（`bootstrap._wait_jobs_gone`），今后不再增长。存量用 `python installer\cleanup_temp.py --run` 清 —— 它先试 `os.rename`，有进程占用的目录必然重命名失败，因此不可能误删正在运行的实例 |

---

## 9. 推到 GitHub 之前（隐私闸门）

本仓库的开发机上，`config.json` / `logs/` / `data/` 里是**真实可用的账号凭据**
（accessToken、refreshToken、网关 api_key、cookie、device_id）。一次
`git add -A` 就能把它们全部公开，而 GitHub 上改写历史极其麻烦。所以别靠记性：

```bat
python sanitize_check.py            "退出码 0 才算干净"
git check-ignore -v config.json     "确认它真的被忽略"
```

检查器管三件事：① 敏感文件是否还躺在工作区；② 敏感**值**是否被写进任何会被
提交的文件（JWT、`sk-` 密钥、已知账号 uid、个人标识、开发机私有绝对路径）；
③ `.gitignore` 是否**确实**覆盖了这些路径（问 `git check-ignore`，而不是读文本
—— 正则漏一条就等于没漏）。`--fix` 会清掉可再生的 `logs/ data/ runtime/`
与配置备份，但 `config.json` 只提示不删（那是真实凭据，误删不可恢复）。

**要改的东西一律用占位符**：`YOUR_DEVICE_ID` / `YOUR_API_KEY_HERE` /
`DEMO-KEY-NOT-REAL-…`。真实值只留在本地 `config.json`，安装器另行生成的是
`installer/config.shell.json` 那份空壳。特别注意
`desktop-ui/src/lib/backend.ts` —— 它是**演示数据源**，会被 Vite 原样打进前端
产物随安装包公开，那里的密钥与账号名必须是编造的。

---

## 10. 关键文件速查

| 我想… | 看/改 |
|---|---|
| 管理账号/积分/API/自启/卸载 | 桌面端 `desktop\open-ai-desktop.exe`（源码 `desktop-ui/`） |
| 窗口 X 后找不到界面了 | 没退出，最小化到了**系统托盘**（右下角 open-ai 图标）→ 左键点击即恢复 |
| 托盘图标不见了 | 由桌面端（Rust `tray-icon`）创建。用 `desktop-ui\tools\check-tray.ps1 -ProcessName open-ai-desktop` 验证；图标不显示时先确认是否被 Win11 收进托盘溢出区（`^`） |
| 托盘「退出」没退出 | 看 `logs\broker.log`（应出现 `gui-shutdown`）；退出流程有多重兜底（IPC→Job 清理→抑制标记→5s 看门狗硬退出），正常必退。<br>（原先还让看 `logs\gui_exit.log` —— 那份日志由**已删除的 Python GUI** 写入，v3.0 起不会再产生，排查请只看 `broker.log`） |
| 管理 API 密钥 | GUI「API管理」页 → `scripts/api_store.py` |
| 换模型/加别名 | `config.json` → `providers.workbuddy.models` |
| 修签到失败(9074) | `config.json` → `providers.trae.device_id` / `headers.x-device-id`（填真实 machineid） |
| 懂 device_id 约束 | `MEMORY.md` |
| 调守护节奏/内存限额 | `config.json` → `runtime` 节；`app_runtime.py`（`CHECK_INTERVAL` / `BACKOFF_STEPS`） |
| 加 Trae 账号 | 账号管理GUI 或 `scripts/login_trae.py` |
| 加 WorkBuddy 账号 | 账号管理GUI 或 `scripts/login_workbuddy.py` |
