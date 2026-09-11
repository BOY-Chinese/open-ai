/**
 * 领域类型契约 — 对应 Python 后端 config.json / usage_history.db 结构
 * 前端所有页面共用，禁止在组件内重新定义。
 */

/* ═══════════ 通道 ═══════════ */

/** 三通道：Trae / WorkBuddy / WorkBuddy 国际版 */
export type Channel = 'Trae' | 'WorkBuddy' | 'WorkBuddy_IE'

/** 筛选用值（含"全部"） */
export type ChannelFilter = Channel | 'all'

export const CHANNELS: Channel[] = ['Trae', 'WorkBuddy', 'WorkBuddy_IE']

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
  /** 展示名，如「网页登录账号(3190130595077593)」「国际版账号(boy-chinese)」 */
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

/** 图表用：单日三通道堆叠数据 */
export interface DailyUsage {
  day: string
  Trae: number
  WorkBuddy: number
  WorkBuddy_IE: number
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

export type LogLevel = 'info' | 'success' | 'warn' | 'error'

export interface LogLine {
  id: number
  level: LogLevel
  text: string
  ts: number
}

/* ═══════════ 通用异步状态 ═══════════ */

export type AsyncStatus = 'idle' | 'loading' | 'success' | 'error'

export interface AsyncState<T> {
  status: AsyncStatus
  data: T
  error?: string
}
