/**
 * 领域类型契约 — 对应 Python 后端 config.json / usage_history.db 结构
 * 前端所有页面共用，禁止在组件内重新定义。
 */

/* ═══════════ 通道 ═══════════ */

/** 四通道：Trae / WorkBuddy / WorkBuddy 国际版 / Loomy（讯飞） */
export type Channel = 'Trae' | 'WorkBuddy' | 'WorkBuddy_IE' | 'Loomy'

/** 筛选用值（含"全部"） */
export type ChannelFilter = Channel | 'all'

export const CHANNELS: Channel[] = ['Trae', 'WorkBuddy', 'WorkBuddy_IE', 'Loomy']

/** 通道展示配置：色值走 CSS 变量，此处只做语义映射 */
export const CHANNEL_META: Record<
  Channel,
  { label: string; token: string; dot: string }
> = {
  Trae: { label: 'Trae', token: 'bg-channel-trae', dot: 'bg-fg-subtle' },
  WorkBuddy: { label: 'WorkBuddy', token: 'bg-channel-wb', dot: 'bg-success' },
  WorkBuddy_IE: {
    label: 'WorkBuddy 国际',
    token: 'bg-channel-wbie',
    dot: 'bg-warning',
  },
  Loomy: { label: 'Loomy', token: 'bg-channel-loomy', dot: 'bg-channel-loomy' },
}

/**
 * 安全取通道元信息。
 *
 * 后端可能返回未知/别名通道（例如 provider 注册键是 workbuddy-intl），
 * 直接 CHANNEL_META[x].dot 会抛 "Cannot read properties of undefined"
 * 并让整页白屏。这里统一兜底，未知通道降级为中性灰 + 原样标签。
 */
const FALLBACK_META = {
  label: '未知通道',
  token: 'bg-fg-subtle',
  dot: 'bg-fg-subtle',
}

export function channelMeta(channel: Channel | string | undefined) {
  if (!channel) return FALLBACK_META
  return CHANNEL_META[channel as Channel] ?? { ...FALLBACK_META, label: String(channel) }
}

/* ═══════════ 账号 ═══════════ */

export type AccountStatus = 'enabled' | 'disabled' | 'disconnected'

export interface Account {
  id: string
  channel: Channel
  /** 展示名，如「网页登录账号(<uid>)」「国际版账号(<用户名>)」 */
  name: string
  /** 是否已勾选启用 */
  enabled: boolean
  /** 派生状态（UI 直接消费，避免组件内重复推导） */
  status: AccountStatus
  /** 通用积分 */
  credits: number
  /** Work 积分（Trae 通道特有，其余为 0） */
  workCredits: number
  /** 最近一次刷新时间（epoch 秒） */
  refreshedAt: number
}

export const STATUS_META: Record<
  AccountStatus,
  { label: string; badge: string; dot: string }
> = {
  enabled: {
    label: '启用',
    badge: 'bg-success/12 text-success border-success/30',
    dot: 'bg-success',
  },
  disabled: {
    label: '关闭',
    badge: 'bg-warning/12 text-warning border-warning/30',
    dot: 'bg-warning',
  },
  disconnected: {
    label: '断连',
    badge: 'bg-danger/12 text-danger border-danger/30',
    dot: 'bg-danger',
  },
}

/* ═══════════ API 密钥 ═══════════ */

export interface ApiKey {
  id: string
  name: string
  key: string
  createdAt: number
}

export interface GatewayInfo {
  openaiBase: string
  chatEndpoint: string
  anthropicBase: string
}

/* ═══════════ 模型 ═══════════ */

export interface ModelEntry {
  id: string
  channel: Channel
  /** 对外展示名，如「DeepSeek-V4-Flash 正式版」 */
  name: string
  /** 积分倍率；international 通道单位为 credits，用 unit 区分 */
  ratio: number
  ratioUnit: string
  /**
   * 实际路由表模型名（客户端请求时填的 model 值）
   *
   * 规则：通道前缀 + 上游模型名，前缀由后端 provider 决定：
   *   Trae          → `tr-`   如 tr-DeepSeek-V4-Flash-Official_dev
   *   WorkBuddy     → `wb-`   如 wb-deepseek-v4.1-flash
   *   WorkBuddy_IE  → `wbie-` 如 wbie-deepseek-v4.1-flash
   */
  routeModelId: string
  hidden: boolean
  pinned: boolean
}

/* ═══════════ Auto 路由连 ═══════════ */

/**
 * Auto 路由连（虚拟模型）配置 — 对应 config.json 的 auto_chain 段
 *
 * 请求 model="Auto路由连" 时按 models 顺序故障转移：
 * 第一个成功产出内容的模型胜出，全部失败返回 502。
 */
export interface AutoChain {
  enabled: boolean
  /** 每个模型的最大等待秒数（0 = 用 provider 默认） */
  timeout: number
  /** 尝试顺序即数组顺序；元素为对外调用名（带 tr- / wb- / wbie- / lm- 前缀） */
  models: string[]
}

/* ═══════════ 积分 / 用量 ═══════════ */

export interface UsageRow {
  id: string
  channel: Channel
  /** 账号展示名（WB_fea0 这类短名在后端已映射） */
  account: string
  model: string
  /** epoch 秒 */
  ts: number
  /** 消耗积分 */
  amount: number
}

export interface CreditStats {
  gained: number
  used: number
}

/** 图表用：单日四通道堆叠数据 */
export interface DailyUsage {
  day: string
  Trae: number
  WorkBuddy: number
  WorkBuddy_IE: number
  Loomy: number
}

export interface WeekBundle {
  stats: CreditStats
  daily: DailyUsage[]
  /** 本周起始日 YYYY-MM-DD */
  startDay: string
  endDay: string
  offset: number
  updatedAt: number
}

export interface TodayBundle {
  stats: CreditStats
  rows: UsageRow[]
  updatedAt: number
}

/* ═══════════ 日志 ═══════════ */

/**
 * 记录级别。
 *
 * v3.0 起前端不再展示网关运行日志（那一页已改回 v2.3 的「操作日志」，
 * 记录的是用户在界面上的操作，见 `lib/oplog.ts`），故此类型只描述级别本身；
 * 网关日志行（`LogLine`）已随 /v1/admin/logs 的前端调用一并移除。
 * 网关自身的输出仍可通过「操作日志」页的「日志目录」按钮或托盘菜单查看。
 */
export type LogLevel = 'info' | 'success' | 'warn' | 'error'

/* ═══════════ 每日签到 ═══════════ */

/**
 * 单个账号今日的签到结果。
 *
 * 语义边界（重要）：这是**今日签到是否成功**的客观凭证，不是「是否要签到」的开关。
 * 证据来自本地流水库 gain 表 —— 有当日入账记录才算成功
 * （TRAE 记 `checkin`，WorkBuddy 系记当日入账的资源包）。
 * 因此该字段**只读**，前端不得提供勾选框让用户改它。
 */
export interface SigninStatus {
  /** 今日已签到成功 */
  checkedIn: boolean
  /** 今日签到入账积分 */
  amount: number
  /** 入账类型：TRAE 为 'checkin'，WB 系为资源包名 */
  kinds: string[]
  /** 入账时间（epoch 秒） */
  ts: number
}

/** 签到状态查询结果：未出现在 signin 里的账号即「今日未签到」 */
export interface SigninBundle {
  /** 统计日（YYYY-MM-DD，本地时区） */
  day: string
  /** accountId → 签到结果 */
  signin: Record<string, SigninStatus>
}

/** 空签到表（初值 / 接口不可用时使用） */
export const EMPTY_SIGNIN: SigninBundle = { day: '', signin: {} }

/* ═══════════ 外观 / 主题 ═══════════ */

/**
 * 外观模式（系统设置 → 启动设置 → 外观设置）
 *
 *   light  —— 白天：始终浅色
 *   dark   —— 夜间：始终深色
 *   system —— 跟随系统：按 `prefers-color-scheme` 实时切换
 *
 * 默认 `light`（白天）。定义放这里而不是 `lib/theme.ts`，
 * 是为了让组件只 import 类型，不牵出 localStorage 等副作用代码。
 */
export type ThemeMode = 'light' | 'dark' | 'system'

/* ═══════════ 一键更新 ═══════════ */

/** POST /v1/admin/version/check —— 检查更新返回体 */
export interface UpdateCheckResult {
  current: string
  latest: string
  channel: string
  repo: string
  /** 是否真的比本机新（同版本 / 更旧版本都是 false） */
  hasUpdate: boolean
  /** 有新版本，但 release 里没有本通道的安装包 */
  assetMissing: boolean
  assetName: string
  assetSize: number
  /** 查询失败时的错误文案（正常为 ''） */
  notes: string
}

/**
 * 更新包下载相位（GET /v1/admin/version/update-state 轮询）
 *
 * ★ 只覆盖**下载**：安装阶段由桌面端界面本地维护（见 UpdateDialog 的
 *   `installing` 状态）—— 安装必须经 Tauri 命令 `install_update` 发起，
 *   外部工具调不到，因此后端状态机里没有「正在安装」这一相位。
 */
export type UpdatePhase = 'idle' | 'downloading' | 'ready' | 'error'

export interface UpdateState {
  phase: UpdatePhase
  /** 下载进度 0~100（非下载相位为 0） */
  percent: number
  received: number
  total: number
  /** 目标安装包版本（release tag） */
  version: string
  /** 已下载安装包的绝对路径 */
  path: string
  /** error 相位时为失败文案 */
  error: string
  /** 上次状态变更时间（epoch 秒） */
  ts: number
}

/* ═══════════ 通用异步状态 ═══════════ */

export type AsyncStatus = 'idle' | 'loading' | 'success' | 'error'

export interface AsyncState<T> {
  status: AsyncStatus
  data: T
  error?: string
}
