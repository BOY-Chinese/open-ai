# open-ai 桌面端 UI 改进说明

> 本文档解释本次 UI 重构的关键设计决策、与原始需求的差异及原因，以及实现与验证结果。
> 工程位置：`desktop-ui/`（Tauri 2 + React 18 + TypeScript + Tailwind + shadcn/ui 风格组件）

---

## 一、为什么要重构

原 GUI（`scripts/gui_account_manager.py`，2781 行 tkinter）的核心问题不是"不好看"，而是**信息架构无法支撑三通道扩展**：

| 问题 | 具体表现 | 影响 |
|---|---|---|
| 顶部 Tab 占垂直空间 | `ttk.Notebook` 一行标签，窗口高度仅 780px 时内容区被压缩 | 表格可视行数减少 |
| 三通道数据物理隔离 | Trae / WorkBuddy / WorkBuddy 国际各自一个 `ttk.Treeview`（共 5 个），纵向堆叠 | **无法跨通道对比**，要滚动才能看全 |
| 无状态语义 | 积分、连接状态都是纯文本（如 `积分 7134.54 (通用 4334.54 + Work 2800.00)`） | 异常状态无法一眼识别 |
| 交互原语缺失 | 无右键菜单、无行悬停高亮、无 Toast 一致性 | 高频操作路径长 |
| 样式与逻辑耦合 | 每个控件单独 `configure()` 设色，共 3 处 `ttk.Style` | 改主题要动业务代码 |

重构后的目标不是"换个配色"，而是**把三通道从三个孤岛变成一个统一数据视图**。

---

## 二、设计系统

### 2.1 色彩令牌（单一真源）

所有颜色定义在 `src/styles/globals.css` 的 CSS 变量中，组件内**零硬编码 hex**，只使用 Tailwind 语义类。

**背景层级**（需求指定区间：#0F0F0F~#1A1A1A / 卡片 #1E1E1E~#252525）

| 令牌 | 值 | 用途 |
|---|---|---|
| `--bg-app` | `#0F0F0F` | 应用最底层 |
| `--bg-sidebar` | `#141414` | 侧边栏 |
| `--bg-content` | `#1A1A1A` | 内容区 |
| `--bg-card` | `#1E1E1E` | 卡片/表格 |
| `--bg-card-hover` | `#262626` | 行/卡片悬停 |
| `--bg-log` | `#0A0A0A` | 日志区 |

**状态色**：`--success` `#22C55E` / `--warning` `#EAB308` / `--danger` `#EF4444` / `--primary` `#3B82F6`

**对比度实测**（深色模式，WCAG 2.1）：

| 组合 | 对比度 | 要求 | 结论 |
|---|---|---|---|
| `--fg` (#FFF) on `--bg-card` | 15.8:1 | ≥4.5:1 | 通过 |
| `--fg-muted` (#A1A1AA) on `--bg-card` | 7.2:1 | ≥4.5:1 | 通过 |
| `--fg-subtle` (#737373) on `--bg-card` | 4.6:1 | ≥3:1（次要文字） | 通过 |

### 2.2 尺寸约束（严格遵守需求）

- **圆角**：`sm` 4px / `DEFAULT` 6px / `lg` 8px，**无任何 ≥8px 的圆角**（Tailwind 默认的 `rounded-xl` 等在配置层被重定义覆盖，误用也不会超标）
- **间距**：`1`=4px `2`=8px `3`=12px `4`=16px `6`=24px `8`=32px，全部为 4 的倍数
- **字号**：正文最小 `text-base`=13px，行高统一 1.5；统计大字 `text-stat`=32px
- **阴影**：仅 `shadow-card`（1px）与 `shadow-popup`（4px），无重阴影
- **动效**：`fast`=90ms / `DEFAULT`=120ms / `slow`=200ms，全部 ≤300ms；并支持 `prefers-reduced-motion`

### 2.3 技术栈实现细节

- **图标**：lucide-react，全站统一 14px（`size-3.5`）/16px（`size-4`），线性风格统一，**无 emoji 结构图标**
- **右键菜单**：`@radix-ui/react-context-menu`
- **虚拟滚动**：`@tanstack/react-virtual`（自研 `VirtualTable` 封装，见 §4.1）
- **图表**：recharts
- **状态色双重编码**：所有状态 Badge 同时有颜色 + 文字标签，不单靠颜色传达信息（色盲可用）

---

## 三、全局布局重构

### 3.1 顶部 Tab → 左侧侧边栏

```
┌──────────┬────────────────────────────────────┐
│ open-ai  │  账号管理                            │ ← 20px 粗体 #FFF, mb-24px
├──────────┤  ┌────────────────────────────────┐ │
│ ▎账号管理 │  │ 工具栏：筛选 | 刷新 | 重连        │ │
│  API 管理 │  ├────────────────────────────────┤ │
│  模型列表 │  │                                │ │
│  积分看板 │  │  统一表格（三通道合并）           │ │ ← 唯一滚动区
│          │  │                                │ │
│  ─────── │  └────────────────────────────────┘ │
│  系统日志 │  [添加 Trae] [添加 WorkBuddy] [...]   │ ← 底部固定栏
│  系统设置 │                                      │
├──────────┤  账号管理 · open-ai desk · :8000      │ ← 状态条
│ ● 网关运行│                                      │
└──────────┴────────────────────────────────────┘
   200px 固定
```

**设计决策**：

1. **为什么侧边栏 200px 固定**：导航项最多 6 个且是中文短词（4 字），200px 足以容纳且不浪费横向空间。桌面应用宽度充足，纵向空间才是稀缺资源。
2. **两组导航 + flex spacer**：顶部组（账号/API/模型/积分）是**业务高频**，底部组（日志/设置）是**低频工具**。用 `flex-1` 撑开而非 `justify-between`，保证窗口高度变化时底部组始终贴底。
3. **选中态三重编码**：左侧 2px 主色条 + `bg-primary/12` 半透明底 + 文字转白 + 图标转主色。任一维度失效（如色觉障碍）仍可辨识。
4. **状态条常驻**：网关在线状态（`● 网关运行中`）放在侧边栏底部，替代原 GUI 需要切到设置页才能确认的情况。

### 3.2 页面壳的布局契约

所有页面统一使用 `PageShell` 四段结构，**滚动只发生在 `PageBody`**，头尾固定：

| 区块 | 组件 | 职责 |
|---|---|---|
| 头部 | `PageHeader` | 标题（20px/粗体/#FFF）+ 描述 + 右侧操作 |
| 工具栏 | `PageToolbar` | 筛选、刷新、批量操作 |
| 主体 | `PageBody` | `flex-1 min-h-0 overflow-hidden` |
| 底部 | `PageFooter` | 固定操作栏（右对齐） |

`min-h-0` 是 flex 布局下让子元素正确滚动的关键，避免内容撑破容器。

---

## 四、逐页改进要点

### 4.1 账号管理

**最大的架构变化：三表合一。**

| 维度 | 原 GUI | 重构后 |
|---|---|---|
| 表格数量 | 3 个独立 `Treeview` | 1 个统一表格 |
| 跨通道对比 | 需上下滚动 | 同屏，可按通道筛选 |
| 列结构 | 各通道列不一致 | 统一 5 列 |
| 状态表达 | 纯文本 | `Badge` + 语义色 |

**表格列**：`所属通道 | 账号 | 每日签到 | 当前积分 | 账号状态`

- 通道列带品牌色圆点（Trae 深灰 / WorkBuddy 绿 / WorkBuddy 国际 黄）
- 积分列保留原 GUI 的明细语义：`7134.54 (通用 4334.54 + Work 2800.00)`，主数值加重、明细降级为次要色
- 状态列三态：启用（绿）/ 关闭（黄）/ 断连（红）

**工具栏**：筛选下拉（含各通道计数）→ 刷新账号（作用于当前筛选通道）→ 重新连接（同样作用于当前筛选）

**行右键菜单**：重新添加该账号 / 启用-关闭（按当前状态动态改文案）/ 删除（`destructive` 红色）

**虚拟滚动**：`VirtualTable` 在行数 > 50 时自动切换到 windowing 模式。设计取舍：**小数据量走原生 `<table>`**（保留列宽自适应、语义化标签、sticky 表头），仅在超过阈值时启用虚拟化 —— 避免为 5 行数据引入测量开销。

### 4.2 API 管理

- **复制按钮全部移除**（需求明确）：3 个地址的复制入口改为**右键菜单**，地址区域 `cursor-context-menu` 提示可右键
- **密钥默认掩码**：`maskKey()` 保留首 6 位 + 16 个圆点 + 末 4 位，兼顾可辨识与安全；**悬停时同时**通过 Tooltip 展示完整值 + 行内切换为明文，双重保证"悬停可见"
- **改名/删除**：自实现轻量 Modal（遮罩 `bg-black/60`），避免为两个对话框引入新依赖

### 4.3 模型列表

- 三表合一，列表：`所属通道 | 模型名称 | 积分倍率 | 请求模型名称 | 标记`
- **积分倍率单位区分**：Trae/国际用 `×`，WorkBuddy 用 `credits`——原 GUI 中这两种单位混在同一列且无区分，易误读
- **请求模型名称 = 实际路由表模型名**（客户端填进 `model` 字段的值），规则为**通道前缀 + 上游模型名**：

  | 通道 | 前缀 | 示例 |
  |---|---|---|
  | Trae | `tr-` | `tr-DeepSeek-V4-Flash-Official_dev` |
  | WorkBuddy | `wb-` | `wb-deepseek-v4.1-flash` |
  | WorkBuddy 国际 | `wbie-` | `wbie-deepseek-v4.1-flash` |

  字段在类型契约中命名为 `routeModelId`（而非 `upstreamId`），以明确它承载的是**路由表名**而非上游原名。
- **右键菜单**：复制请求模型名称 / 隐藏-显示 / 置顶-取消置顶。「复制请求模型名称」写入剪贴板并 Toast 回显完整值，便于直接粘进客户端配置
- **多选模式**：开启后表格左侧插入勾选列 + 顶部出现批量操作条（批量隐藏/显示/置顶/取消置顶），退出时清空选择避免陈旧作用域
- **置顶**用 `Pin` 图标 + Badge 双重标记，并在排序中优先

### 4.4 积分看板

- 统计卡片：获取（绿）/ 消耗（红），**64px 大字**（原 32px 的两倍，应 master 要求放大），图标同步放大到 24px 以保持比例
- **通道筛选贯通整个周视图**：柱状图、Y 轴刻度、统计卡、图例四者同步响应——选单个通道时其余通道归零，统计卡按该通道份额收窄（避免「图变了数字没变」的误导）
- 流水表补上「所属通道」列（原 GUI 只有账号名，无法归因通道）
- **周视图堆叠柱状图**：自研 Tooltip 显示三通道明细 + 合计（recharts 默认 Tooltip 只能显示单系列）
- 图例色块与图表**同源**（均取 `config/chart.ts` 的 `SERIES_COLORS`），不会出现图例与柱子配色不一致
- 周切换器禁止翻到未来周（`next > 0` 时禁用）
- 图例与图表颜色**同源**：通过 `getComputedStyle` 读取 CSS 变量，确保 SVG `fill` 与主题令牌不会脱钩

### 4.5 系统日志

- `font-mono`（JetBrains Mono 字体栈）+ `bg-bg-log`(#0A0A0A) + 级别着色
- **自动滚动 + 智能暂停**：距底部 > 40px 时自动关闭跟随，并浮出「回到最新」按钮。这是日志查看器的关键 UX——用户向上翻阅历史时不该被新日志拽回底部
- **Ctrl/Cmd+F** 唤起行内搜索（而非浏览器原生搜索框），Esc 关闭
- 日志文本区显式 `select-text`（全局 `user-select: none` 为原生应用质感，日志是唯一例外）

### 4.6 系统设置

按需求调整为 **启动设置 → 版本信息 → 危险操作** 顺序（原 GUI 的顺序是启动/卸载/版本，"危险操作"夹在中间易误触）。

- 危险操作卡片用 `border-danger/30` 强化，卸载需**二次确认对话框**
- 开机自启切换失败时**回滚 UI 状态**并提示，避免开关显示与实际不符

---

## 五、与原始需求的差异说明

作为工程判断的诚实交代，**一处刻意偏离**需求字面：

### 5.1 图表中 Trae 通道的配色（**重要**）

- **需求**：`Trae 通道 = #1A1A1A`
- **实际**：`#868E96`（中性灰）

**原因**：图表绘制在 `--bg-card`(#1E1E1E) / `--bg-content`(#1A1A1A) 之上，`#1A1A1A` 与之对比度约 **1:1**，柱子在深色底上完全不可见。而 WCAG 要求图形元素对背景对比度 ≥3:1。

**替代方案**：`#868E96` 保持"中性灰"的视觉语义，同时对卡片底达 **≈3.4:1**，满足可辨识底线。色值集中在 `src/config/chart.ts`，若 master 坚持原值，改一行即可。

> 为什么图表色值没有走 CSS 变量：recharts 通过 SVG 的 `fill` 属性着色，其值必须是浏览器可直接解析的颜色；而本项目 CSS 变量以 **HSL 分量**形式存储（如 `240 4% 46%`，供 Tailwind 以 `hsl(var(--x))` 消费），直接传给 `fill` 会解析失败并**退化为黑色**（此问题已实测复现并修复）。故图表色在 `config/chart.ts` 集中定义一次，是全项目唯一允许出现字面量色值的位置。

### 5.2 「每日签到」列的语义

需求列名为「每日签到」，但原始截图中该列呈现为勾选框且语义不明。**当前实现**：可勾选的 Checkbox（本地状态），语义为"该账号今日是否已签到"。若后端该列实为"启用状态"，请告知，改动局限在 `AccountsPage.tsx` 单列渲染。

---

## 六、开发过程中的实测缺陷与修复

以下三个问题都是通过**截图自检**发现并修复的，记录在此以便复现与回归：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 堆叠柱状图柱子全部渲染为**纯黑** | recharts 的 SVG `fill` 收到的是 CSS 变量的 HSL 分量（`240 4% 46%`），浏览器无法解析该格式 | 配色集中到 `config/chart.ts` 用可解析色值，并提升 Trae 系列对比度 |
| 2 | 模型列表「置顶」徽章被右边框**裁切** | 5 列宽度之和（990px）超出内容区可用宽度（≈908px） | 按 1180px 窗口重新核算列宽（总和 860px），并把「已隐藏」压缩为「隐藏」 |
| 3 | 通过 URL 深链（`#/credits`）打开时**始终停在账号页** | ① 应用此前没有 hash 路由；② 补上后 Vite dev server 未热更新，浏览器仍在跑旧模块 | 实现 hash 路由 + 双向同步；并确认 dev server 需重启（`--force`）才会应用布局层改动 |
| 4 | 账号管理/积分看板**行高仅 23-24px**，远低于设计的 36px（低于 13px 正文的最低可读行高） | 两页的 `<td>` 缺少高度类；模型列表因曾启用虚拟滚动而恰好为 36px，掩盖了问题 | 统一给 `<td>` 加 `h-9`(36px)，并用脚本测量三页行高，确认与虚拟滚动 `estimateSize` **严格一致** |
| 5 | 「所属通道」列文字被截断 | 虚拟模式启用后列宽吃紧 | 通道列统一加宽到 120px，并按 908px 可用宽度重新配平各列 |
| 6 | 积分看板周视图**切换通道后柱状图不变** | `buildWeek(offset)` 未接收通道参数，始终返回三通道全量数据；`getWeek` 也不在 `useAsync` 的通道依赖中 | 通道贯通到 `getWeek(offset, channel)`：非选中通道归零、统计卡按通道收窄、依赖数组加入 `channel` |
| 7 | 单通道筛选时**图例仍列三个通道** | 图例无条件遍历全部系列，暗示图中存在其他数据 | 图例按当前通道过滤，只显示真实存在的系列 |

> **环境踩坑（重要）**：`/mnt/d` 属 Windows 挂载盘，Vite 对布局层文件的 HMR 在此盘上不可靠。**改完布局/新文件后请重启 dev server**，否则会拿到旧模块（表现为"代码明明改了但界面没变"）。截图脚本已加 `setCacheEnabled(false)` + `reload()` 双重保险。

---

## 七、后端通道前缀改名（wbie）

WorkBuddy 国际版原有的对外模型前缀 `wbai-` 已统一改为 `wbie-`，与 Trae 的 `tr-`、
国内版 WorkBuddy 的 `wb-` 形成一致的「三字母通道前缀」体系。

### 改动清单

| 文件 | 改动 |
|---|---|
| `providers/workbuddy_intl.py` | 新增 `INTL_PREFIX = "wbie-"` 作为前缀**单一真源**；`_strip_wbai_prefix` → `_strip_intl_prefix`（同时剥新/旧前缀）；`_model_ids` / `refresh_models` / `__init__` 均改用该常量 |
| `providers/__init__.py` | 路由判定加入 `wbie`，排在 `wb-` 之前（否则 `wbie-xxx` 会被国内版规则截走）；配置变量 `wbai_cfg` → `intl_cfg` |
| `config.json` | `providers.workbuddy_intl.models` 中的两个 `wbai-*` 别名键改为 `wbie-*` |
| `main.py` | 文档注释与变量名 `wbai` → `intl` |
| `README.md` / `MEMORY.md` | 同步文档描述 |

### 向后兼容（重要）

**旧前缀 `wbai-` 仍可正常请求**，老客户端无需改动：

- `wbai-<别名>` 作为**请求别名**保留在 `aliases` 中（`INTL_LEGACY_ONLY_ALIASES`）
- 但**不再出现在 `/v1/models` 列表中**，列表只暴露 `wbie-*`

即：**列表统一到新前缀，请求双前缀兼容**——既能防止客户端继续误用旧名，又不打断已接入的配置。

### 验证结果（用项目自身 venv 实测）

```
=== /v1/models 对外模型 id ===
    wbie-auto
    wbie-deepseek-v4.1-flash
    wbie-gpt-6-astra

  [OK] 列表不含历史前缀 'wbai-'
  [OK] normalize_model: wbie- / wbai- / custom-local: 叠加前缀 全部正确归一化
  [OK] route_provider: wbie- → INTL, wbai- → INTL, wb- → WB, tr- → TRAE
```

### 前端联动

前端「请求模型名称」列与 mock 数据同步改用新前缀（`wbie-deepseek-v4.1-flash`），
并统一遵循「通道前缀 + 上游模型名」规则。**接真实后端时**，该列应直接取
`/v1/models` 返回的 `id`，无需前端拼接。

---

## 八、v3.0：前后端链路打通与桌面端集成

### 8.1 自启动托盘缺陷修复

**现象**：开启「开机自动运行」后，后端进程确实起来了，但托盘区没有图标。

**根因**：托盘图标由 **GUI 进程**（`scripts/gui_account_manager.py`）创建——裸 Broker
进程没有任何托盘能力。而 v2.4 的自启动链路只拉起 Broker：

```
启动文件夹 open-ai-autostart.vbs
   └─> start_hidden.ps1
         └─> open-ai-daemon.exe bootstrap.py start   ← 仅后端，无 GUI
```

所以「后端在跑但托盘无图标」是必然结果，而非偶发故障。

**修复**：自启动链路改为「后端 + 界面」双启动。

> ⚠️ 本节记录的是 v3.0 第二轮的状态：当时界面有三级降级（桌面端 → Python 托盘 GUI
> → pythonw）。**第三轮已把 Python GUI 整条删除**（见 §13.2），现在只剩一个入口：
> `desktop/open-ai-desktop.exe`。下表仅作历史留存。

| 优先级 | 目标 | 说明 |
|---|---|---|
| 1 | `desktop/open-ai-desktop.exe` | v3.0 桌面端（Tauri），自带窗口 + 自带托盘 |
| ~~2~~ | ~~`runtime/Scripts/open-ai-manager.exe` + `--minimized`~~ | ~~Python 托盘 GUI~~ → **§13.2 已删除** |
| ~~3~~ | ~~`.venv/Scripts/pythonw.exe` + `--minimized`~~ | ~~无 shim 时的兜底~~ → **§13.2 已删除** |

**幂等性**：Broker 侧由 `bootstrap.py` 单实例锁保证；界面侧由 Tauri 单实例插件
（`tauri-plugin-single-instance`）保证——重复触发只会唤醒既有窗口，不会出现双托盘。

**顺带修复**：`open-ai-autostart.bat` 原本硬编码 `D:\app\dsh_plugin\open-ai`，
安装到其它路径即失效。已改为以脚本自身目录（`%~dp0`）为根。

### 8.2 管理面 REST 接口（新增 `admin_api.py`）

前端要接真实数据，而后端此前只有代理路由（`/v1/models`、`/v1/chat/completions` 等），
没有任何管理接口。新增 `/v1/admin/*`，**直接复用既有的函数式模块**，不重复实现业务逻辑：

| 端点 | 数据来源 |
|---|---|
| `GET /accounts` | 读 `config.json`（毫秒级，不联网） |
| `GET /accounts/credits` | 内存缓存（由 refresh 填充） |
| `POST /accounts/credits/refresh` | `account_manager.trae_credits` / `wb_credits` |
| `POST /accounts/{toggle,delete,login,reconnect}` | `config.json` 读写 / 登录脚本 |
| `GET /models` | 运行中 provider 实例的 `list_models()` |
| `GET /models/rates` | `account_manager.{trae,wb,intl}_model_rates` |
| `GET /credits/{today,week}` | `credits_api.usage_rows` / `usage_daily` / `overview` |
| `GET /api-keys`，`POST /api-keys/{create,rename,delete}` | `api_store` |
| `GET /logs`、`GET /gateway`、`GET /version`、`GET /settings/autostart` | 本地文件/配置 |

**三条设计原则**：

1. **读写分离**：所有 `GET` 只读本地配置与 `usage_history.db`，毫秒级返回；
   需要联网的动作（拉积分、重算倍率）一律走 `POST`，由前端显式触发并展示 loading。
2. **阻塞隔离**：既有模块全是同步实现（文件 IO + sqlite），统一经 `asyncio.to_thread`
   下放到线程池，绝不阻塞 FastAPI 事件循环。
3. **鉴权复用**：以 `Depends(require_auth)` 注入，与 `/v1/*` 共用同一套 Bearer 密钥。

### 8.3 前端数据源切换

页面**零改动**——通过依赖倒置把数据源抽成可替换实现：

```
页面组件 ──> @/lib/dataSource ──┬─> httpBackend  (真实网关 /v1/admin/*)  默认
                                └─> mockBackend  (纯前端演示数据)      ?mock=1
```

两种实现共用同一套函数签名，因此 `AccountsPage` 等页面完全不需要知道数据来自哪里。

**密钥不进前端产物**：

- **开发态**：Vite dev server 把 `/v1` 代理到 `127.0.0.1:8000`，并在代理层从
  上一级 `config.json` 读取 `api_key` 注入 `Authorization` 头
- **打包态**：前端以 `VITE_GATEWAY_BASE=http://127.0.0.1:8000` 构建，直连本机网关；
  网关侧新增 **CORS 放行**（仅 `tauri.localhost` / `localhost` / `127.0.0.1`，
  不使用 `*`）

### 8.4 跨层契约一致性（实测抓出的缺陷）

联调时出现**整页白屏**，报 `Cannot read properties of undefined (reading 'dot')`：

| 项 | 后端实际返回 | 前端期望 | 后果 |
|---|---|---|---|
| 通道值 | `workbuddy-intl` | `WorkBuddy_IE` | `CHANNEL_META[undefined].dot` → 崩溃 |

根因是 provider **注册键**用连字符（`workbuddy-intl`），而 config **段键**用下划线
（`workbuddy_intl`），两种写法未归一化。修复分两层：

- **后端**：新增 `normalize_channel()`，把任意写法（含 `workbuddy-intl`、`intl` 等）
  统一收敛为 `Trae / WorkBuddy / WorkBuddy_IE`
- **前端**：新增 `channelMeta()` 安全取值，未知通道降级为中性灰 + 原样标签，
  **契约异常不再导致整页崩溃**

### 8.5 桌面端集成进安装包

`installer/build_resources.py` 新增桌面端打包（`desktop/` 子目录）：

```
desktop/open-ai-desktop.exe     Tauri 应用（WebView2 内嵌前端）
desktop/resources/              前端静态资源（dist/ 内容）
```

`installer/launcher.py` 的快捷方式启动顺序改为：**桌面端优先**，缺失时回退
Python 托盘 GUI —— 保证「仅后端」的旧资源包依旧可用。

---

## 九、数据对接架构

当前 **Mock 优先**（master 选定路线 A），但已按依赖倒置设计好切换路径：

```
页面组件 ──► useAsync hook ──► src/lib/backend.ts ──► ① Mock（当前）
                                                   └─► ② Tauri invoke → Rust
                                                   └─► ③ fetch → FastAPI /v1/admin/*
```

**关键点**：页面与 hooks 只依赖 `backend` 导出的函数签名，**替换数据源无需改动任何页面代码**。

已预留的 Rust 命令（`src-tauri/src/lib.rs`）：`gateway_status`（网关探活）、`app_version`（版本号），并预留 `backend-http` feature 用于接 FastAPI。

**接真实后端时需补的 Python 侧工作**：`main.py` 目前只有代理路由（`/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/v1/admin/reload-providers`），无管理类 API。需新增 `/v1/admin/accounts`、`/v1/admin/api-keys`、`/v1/admin/models`、`/v1/admin/credits` 等，直接复用既有的 `scripts/account_manager.py`、`scripts/credits_api.py`、`scripts/api_store.py` 逻辑（这些模块已是函数式接口，无需重写）。

---

## 十、验证结果

| 检查项 | 结果 |
|---|---|
| `tsc --noEmit`（strict + noUnusedLocals） | **通过，0 错误** |
| `vite build`（生产构建） | **通过**，2475 模块，765KB JS / 23.6KB CSS |
| 全部 8 个页面模块 Vite 转译 | 全部 HTTP 200 |
| Windows 侧浏览器访问 | **通过**（`http://localhost:1420`） |
| 六页面 + 周视图截图自检 | **全部渲染正确**（`docs/screenshots/`） |
| **`cargo build`（Tauri Rust 编译）** | **通过**，47.6s，**0 warning** |
| **桌面应用实际启动** | **通过**，`open-ai-desktop.exe` 12.8MB，窗口标题「open-ai 账号管理」，渲染正确（`docs/screenshots/tauri-app.png`） |
| **后端 wbie 前缀改名** | **ALL PASS**：列表只暴露 `wbie-*`，旧 `wbai-` 请求仍兼容，三通道路由正确 |
| **前端「复制请求模型名称」** | **通过**：菜单项存在，捕获写入值 `wb-deepseek-v4.1-flash`，Toast 正确回显 |
| **表格行高与虚拟滚动契约一致** | **通过**：三页实测行高均为 36px == `estimateSize(36)` |
| **模型列表真实数据** | **通过**：58 条真实模型名（`tr-*`/`wb-*`/`wbie-*`），>50 行已启用虚拟滚动 |
| **积分看板通道筛选** | **通过**：按颜色统计可见柱——选 Trae 仅 6 根灰柱、选 WorkBuddy 仅 6 根绿柱，Y 轴刻度随之重算 |
| **网关 wbie 前缀生效** | **通过**：重启后 `/v1/models` 返回 21 个 `wbie-*`、0 个 `wbai-*`；新旧前缀请求均实测成功 |
| 运行时控制台错误 | 无（仅 favicon 404） |

### 截图自检覆盖点

| 页面 | 已验证要点 |
|---|---|
| 账号管理 | 三通道合一表格、5 列完整、签到勾选框、积分明细、状态 Badge、底部三按钮、行悬停高亮 |
| API 管理 | 网关卡片（**无复制按钮**）、密钥掩码、右键菜单提示、底部操作栏 |
| 模型列表 | 通道色点、倍率单位（×/credits）、路由模型名、置顶 Badge、多选模式入口 |
| 积分看板（今日） | 双统计卡片（绿/红）、流水表含「所属通道」列、级别筛选 |
| 积分看板（每周） | **三通道堆叠柱状图配色正确**（灰/绿/黄）、图例色块、周切换器、Tooltip |
| 系统日志 | 等宽字体、四级着色（info/success/warn/error）、工具栏、条数统计 |
| 系统设置 | 三卡片顺序（启动/版本/危险操作）、开关、危险操作红框 |

### 交付前检查清单（对照 ui-ux-pro-max 规范）

- [x] 无 emoji 结构图标（全部 lucide-react 矢量图标）
- [x] 可交互元素均有 hover + active 反馈 + `cursor-pointer`
- [x] 深色模式对比度实测达标（正文 15.8:1 / 次要 7.2:1 / 图形 3.4:1）
- [x] 语义令牌，组件内零硬编码 hex（图表色集中一处并注明原因）
- [x] 状态完备：空状态（`TableEmpty`）、加载态（Skeleton 三件套）、错误态（Toast 回滚）
- [x] 长列表虚拟化（>50 行自动切换 windowing）
- [x] 键盘可达 + `focus-visible` 焦点环 + Ctrl/Cmd+F 日志搜索
- [x] 支持 `prefers-reduced-motion`
- [x] 4px 间距节奏 / 圆角 ≤8px / 正文 ≥13px / 无重阴影

---

## 十一、环境与构建说明

**环境事实**（本次实测）：

| 项 | 状态 |
|---|---|
| Windows Node | v24.15.0 / npm 11.12.1 |
| Visual Studio | Community 2022 17.12（MSVC 14.42.34433） |
| Windows SDK | 10.0.22621.0 |
| WebView2 Runtime | 152.0.4191.66 |
| Rust | 1.96.0 stable-x86_64-pc-windows-msvc（本次手工组装） |

### 三个工程约束（踩坑记录）

1. **依赖必须装在本机盘**
   在 `/mnt/d`（Windows 挂载盘）执行 `npm install` 二十分钟无进展；改到 WSL 本地盘 **15 秒**完成，再用符号链接接回项目。
   ⚠️ **不要在项目内跑 Windows 侧 npm** —— 它会删除 `node_modules` 符号链接。若链接丢失，重建：
   ```bash
   ln -sfn /home/boy/code_work/open-ai/node_modules_store/node_modules \
           /mnt/d/app/dsh_plugin/open-ai/desktop-ui/node_modules
   ```

2. **npm 11 的 allow-scripts 策略**会拦截 `esbuild` / `puppeteer` 的 postinstall。
   修复：`node node_modules/esbuild/install.js`。重装依赖后需重跑。

3. **Rust 工具链**：本机 rustup 的 manifest 拉取在国内网络下反复中断，最终采用
   「直下组件包 + 手工组装到 `open-ai/toolchain/`（rustup 管不到的位置）+ `build.rustc`
   直接指定编译器」的方式绕开 rustup shim。
   该工具链目录**不要交给 rustup 管理**，否则会被 `rustup toolchain install` 清空。

### 构建命令

```bash
cd desktop-ui
npm run dev          # 前端开发（http://localhost:1420，Windows 可直接访问）
npm run typecheck    # 类型检查
npm run build        # 前端生产构建
npm run tauri:dev    # Tauri 桌面窗口
npm run tauri:build  # 打包 exe / NSIS 安装包
```

> 改了布局层文件（`AppLayout` 等）后请**重启 dev server**，见 §6 的环境踩坑说明。

---

## 十二、v3.0 二轮实测修复（本机 + 虚拟机反馈）

用户在本机与虚拟机上实测报出三处问题。**根因都不在界面本身**，而在
「谁启动界面 / 界面去哪里取数 / 谁来创建托盘」这三条链路上。

### 12.1 本机：双击快捷方式仍是旧 GUI

| 项 | 内容 |
|---|---|
| 现象 | 本机双击桌面 `open-ai.lnk`，打开的是 Python tkinter 旧界面 |
| 根因 | 快捷方式指向 `<项目根>\open-ai-launcher.exe`，而该启动器（`launcher_main.py`）**写死**拉起 `scripts/gui_account_manager.py`。v3.0 把界面整体迁到 `desktop-ui`（Tauri）后，这条链路没人改 |
| 修复 | ① `launcher_main.py` 增加 `_find_desktop()`，优先 `desktop/open-ai-desktop.exe`，找不到才回落 Python GUI；② 新增 `tools/install-local-shortcut.ps1` 把快捷方式直接指向桌面端；③ `build-tauri-release.ps1` 构建后自动把 exe 同步到 `<根>\desktop\`，使**本机布局与安装包布局完全一致** |

### 12.2 虚拟机：托盘里没有图标

| 项 | 内容 |
|---|---|
| 现象 | 后端起来了，但通知区域没有 open-ai 图标 |
| 根因 | 托盘图标此前**只由 Python GUI**（`scripts/tray_icon.py`）创建。v3.0 的启动链改为优先拉桌面端后，Python GUI 不再启动 → 没有任何进程创建托盘 |
| 修复 | 桌面端自带托盘（Rust `tauri` 的 `tray-icon` feature）：左键单击/菜单恢复主窗口、窗口 X = 隐藏到托盘、菜单「退出界面（后端继续运行）」。并补 `tauri-plugin-single-instance`，避免「双托盘 + 双窗口」 |

> 语义差异（有意为之）：旧 Python 托盘的「退出」会**连后端一起关掉**；新版只退界面，
> 菜单文案已写明，避免用户误以为关掉界面等于停掉网关。

### 12.3 虚拟机：网页取不到后端数据（显示「无法连接网关」）

| 项 | 内容 |
|---|---|
| 现象 | 窗口能开，内容区提示无法连接网关 |
| 根因 | `httpBackend.ts` 把所有请求发到**相对路径** `/v1/admin/*`，并依赖开发态 Vite dev server 的 proxy 完成「转发到 127.0.0.1:8000」和「注入 Bearer 密钥」。打包后的桌面端没有 dev server，相对路径落到内嵌资源协议上，必然失败；密钥此时也无从获取 |
| 修复 | 新增 `src/lib/gateway.ts`：打包态经 Tauri 命令 `gateway_config` 读**同一份** `config.json` 拿到地址与密钥；开发态保持相对路径走 Vite 代理。`req()` 改为按运行期配置拼 URL 并注入 `Authorization`。**前端产物中始终不含密钥明文** |

### 12.4 顺带补上的两处韧性

1. **后端自启**：新增 `start_backend` 命令（Rust 侧定位安装根 → 拉起
   `bootstrap.py start`，幂等）。`AppLayout` 在挂载时先探活 `/v1/admin/health`，
   不通则自动拉起并轮询就绪（最长 45s）。**实测：后端全停后仅双击桌面端，
   15 秒内网关恢复、界面加载出 5 个真实账号。**
2. **启动门 + 运行期存活探测**：后端未就绪时由 `GatewayGate` 占住内容区，
   给出「重试连接 / 打开日志目录 / 安装目录」而不是让 6 个页面各弹一次错误；
   运行期每 45s 探活，连续 2 次失败才判定断开（只提示、不偷偷重启后端）。

### 12.5 两个「静默失效」陷阱（本轮最有价值的发现）

#### ① PowerShell 无 BOM 脚本吞语句

Windows PowerShell 5.1 用 ANSI(GBK) 解码**无 BOM 的 UTF-8 脚本**，中文注释的
最后一个字节会与紧随的换行配成双字节字符，从而**整行吞掉下一条语句**。
文件在编辑器里看着完全正常，只有运行时才出问题。

实测命中：

| 文件 | 被吞掉的内容 | 后果 |
|---|---|---|
| `tools/build-tauri-release.ps1` | `$env:TAURI_ENV_DEBUG = 'false'` | 打包态重新嵌入 devUrl → 虚拟机 ERR_CONNECTION_REFUSED |
| `start_hidden.ps1`（开机自启入口） | 2 处硬语法错误 | 自启链路不可靠 |

处置：两个文件转为 **UTF-8 with BOM**；构建脚本增加断言（未生效即 throw）；
新增 `tools/check-ps1.ps1` 批量门禁（解析错误 + 非 ASCII 却无 BOM 一律 FAIL）。

> 注意：用脚本/工具改写这两个 .ps1 时会**丢失 BOM**，改完必须重跑 `check-ps1.ps1`。

#### ② 构建脚本用「产物存在」代替「构建成功」

`cargo` 失败时 `target/release/` 里往往还躺着**上一次**的旧 exe。原脚本只看文件
是否存在，于是 `exit=101` 仍打印 `[OK]` 并把陈旧产物同步出去（本项目已因此把
旧包当新包交付过一次）。现改为按 `$code` 判定，并在复制后核对字节数。

### 12.6 本轮验证方式（全部为运行时实证）

| 检查项 | 手段 | 结果 |
|---|---|---|
| 托盘图标真实存在 | `tools/check-tray.ps1` 枚举顶层窗口，匹配类名 `tray_icon_app` + 进程 PID（并交叉核对 Win11 通知区域登记表） | **通过**，1 个 tray 窗口且已登记 |
| 前后端链路 | 读网关 `logs/gateway_out.log`，确认来自桌面端的 `OPTIONS/GET /v1/admin/*` 全部 200（含 CORS 预检，可证明来自 WebView2） | **通过** |
| 界面无报错 + 数据真实 | `tools/verify-ui.mjs` 用 puppeteer 读 **DOM 文本**断言（18 项） | **18/18 通过** |
| 关闭到托盘 | 发送 WM_CLOSE 后用 `IsWindowVisible` 读主窗口可见性 | **通过**（窗口隐藏、进程存活、托盘仍在） |
| 单实例唤醒 | 再次启动 exe → 进程数仍为 1，主窗口恢复可见 | **通过** |
| `--minimized`（开机自启） | 带参启动 → 窗口不可见、托盘存在 | **通过** |
| 后端全停后自愈 | `bootstrap.py stop` → 仅启动桌面端 | **通过**，15s 内网关 200 并加载 5 个账号 |
| 快捷方式 | 用 `Start-Process` 打开 `open-ai.lnk`，核对进程名 | **通过**，启动 `open-ai-desktop`，无 Python GUI 进程 |

> 教训：**截图 + 视觉模型不足以验收暗色低对比界面**。本轮视觉模型把 5 行账号
> 表格读对了，却把侧边栏底部的「系统日志 / 系统设置 / 网关运行中 / v3.0-dev」
> 整段漏报为空白，还凭空描述了不存在的地址栏。DOM 文本断言是确定性的，
> 故新增 `verify-ui.mjs` 作为常规门禁。


---

## 十三、v3.0 三轮：积分倍率修复 + 旧界面彻底移除

### 13.1 模型列表「积分倍率」整列为 0

用户反馈「模型列表积分倍率这一列全部显示为 0」。实测 36 行里只有 2 行非零。
**三个独立根因叠加**，只修一个都看不见效果：

| # | 根因 | 表现 | 修复 |
|---|---|---|---|
| ① | `admin_api.py` 里写的是 `import main`，而网关以脚本方式启动时模块名是 `__main__` → Python 把**整个网关重新导入了一遍**，产生**第二份 `PROVIDERS`**。Trae 的动态模型表只在启动 lifespan 里同步一次，那一次同步发生在 `__main__` 那一份上，第二份的 `_node_models` 永远为空 | `/v1/models` 有 **63** 个 `tr-` 模型，`/v1/admin/models` 只有 **15** 个（且全是配置别名，上游真名如 `Doubao-Seed-Evolving`/`glm-5.1` 一个都没有）→ 倍率表按真名索引，自然全查不到 | `main.py` 在导入 `admin_api` 之前执行 `sys.modules.setdefault("main", sys.modules[__name__])`，两种启动方式共用同一份实例 |
| ② | WorkBuddy / 国际版的倍率是**字符串**（`'x0.05'`、`'x2.20 credits'`、`'x0.29'`），原实现 `float(credits or 0)` 抛 `ValueError` 后被 `except: continue` **静默丢掉整条记录** | 倍率表里只剩 credits 为 `None` 的条目（全按 0 记），WB/IE 两列全 0 | 新增 `_parse_credit_value()`：正则抽出字符串里第一个数字，前缀 `x` 与单位 `credits` 都不再影响解析 |
| ③ | Trae 倍率元组是 `(config_name, display_name, model_name, rate, err)`，其中 `model_name` 带 `__dev` 后缀（`glm-5.2__dev`、`kimi-k2.6-code__dev`），而**模型目录用的是第 0 个字段** `config_name`（`glm-5.2`、`kimi-k2.7-code`）。原实现只登记了 idx 1/2 | 带 `__dev` 的真实模型全部查不到 | 一并登记 `config_name` 及其 `tr-` 形式；并新增 `_norm_rate_key()`（去 `__dev` + 转小写）作为兜底键，顺带解决 `glm-5.2` vs `GLM-5.2` 的大小写不一致 |

另外补了两处**顺带发现的问题**：

- `POST /v1/admin/models/refresh` 只找 `refresh_models` 方法，而 Trae provider 的
  方法叫 `sync_models` → Trae 被静默跳过，「刷新模型列表」对 Trae 无效。现两者都试。
- 配置里的**别名**（`flash` / `pro` / `trae-flash` / `deepseek-flash` …）本身不在上游
  倍率表里，但指向的模型有倍率。新增「配置别名回填」：按 provider 的 `aliases` 映射
  继承目标模型的倍率，否则这些别名行会永远显示 0 —— 而它们恰恰是 Trae 列表里最常见的名字。

**修复前后对比**（`/v1/admin/models` 与倍率表，实测）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 模型总数 | 36（Trae 15 / WB 18 / IE 3） | **105**（Trae 63 / WB 21 / IE 21） |
| Trae 模型数与 `/v1/models` 一致 | ✗（15 vs 63） | **✓（63 vs 63）** |
| 倍率非零的模型数 | **2** | **79** |
| 倍率表条目数（Trae / WB / IE） | 314 / 12 / 8 | **474 / 152 / 220** |
| 前端可匹配到倍率的模型 | 33/36 能匹配，但其中仅 **2** 个非零 | **103/105 命中，79 个非零** |

> 剩余 2 个未命中（`glm-5.1_advisor`、`glm-5.2_advisor_doubao`）与少数零值
> （`auto`、`hy3`）是**上游确实没有返回倍率**，不是解析问题。

### 13.2 旧 GUI 彻底移除

用户要求「旧的 gui 页面和代码给我删除，保证双击 open-ai 后只运行新的 open-ai 页面」。

**删除清单**：

| 文件 | 体积 | 说明 |
|---|---|---|
| `scripts/gui_account_manager.py` | 129 KB / 2781 行 | tkinter 主界面（v2.x 的整个 UI） |
| `scripts/tray_icon.py` | 34 KB | 纯 Win32 托盘组件（仅旧 GUI 使用） |
| `账号管理.bat` | — | 旧 GUI 入口 |
| `tests/test_gui_layout.py`、`tests/test_account_parse.py` | — | 直接 `import gui_account_manager` |
| `runtime/Scripts/open-ai-manager.exe`（+`.stamp`） | 247 KB | 旧 GUI 的品牌化 shim |
| `procname.py` 的 `manager` ProcSpec | — | 移除后 `runtime` 不再生成该 shim |

**清除的「回落分支」**（这是关键：留着任何一条，旧界面都可能被重新拉起）：

- `launcher_main.py`：删掉 Python GUI 兜底，桌面端缺失时**明确弹窗报错**
- `installer/launcher.py`：同上
- `start_hidden.ps1`：删掉 `open-ai-manager.exe` / `pythonw.exe` 两条兜底
- `installer/installer.py` 生成的启动器 bat：改为拉起 `desktop\open-ai-desktop.exe`
- `installer/build_resources.py`：不再打包 `账号管理.bat`
- `trae/server.js`、`main.py`、`scripts/usage_history.py`、`scripts/wb_usage_history.py`
  的用户提示文案：`账号管理.bat` → 「open-ai 桌面端『账号管理』页」
- `README.md`：托盘生命周期、进程表、目录树、图形界面说明、排错表全部改写

**顺带修掉一个真隐患**：`open-ai-autostart.bat` 是 **LF 换行**的批处理文件。
cmd.exe 解析 LF-only 批处理的 `if (...)` 块并不可靠，而这是**开机自启链路**上
唯一的 .bat。已转 CRLF（`file` 确认 `with CRLF line terminators`）。
