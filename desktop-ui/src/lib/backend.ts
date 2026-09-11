/**
 * 演示数据源（Mock）— 与真实数据源 `httpBackend` 实现同一套函数签名。
 *
 * 用途：无网关环境下跑通界面、视觉回归、演示。
 * 默认数据源见 `@/lib/dataSource`（页面只 import `backend`，不直接依赖本文件）。
 */
import type {
  Account,
  ApiKey,
  Channel,
  ChannelFilter,
  DailyUsage,
  GatewayInfo,
  LogLine,
  LogLevel,
  ModelEntry,
  TodayBundle,
  UsageRow,
  WeekBundle,
} from '@/types/domain'
import { toDayKey } from '@/lib/utils'

/* ═══════════════ 内部工具 ═══════════════ */

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
let seq = 0
const uid = (p: string) => `${p}-${++seq}`

/** 模拟网络延迟：让 Loading/Skeleton 状态真实可见 */
const LATENCY = 320

/* ═══════════════ Mock 种子数据（取自真实界面截图） ═══════════════ */

let ACCOUNTS: Account[] = [
  {
    id: 'acc-trae-1',
    channel: 'Trae',
    name: '网页登录账号(3190130595077593)',
    enabled: true,
    status: 'enabled',
    credits: 4334.54,
    workCredits: 2800.0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-trae-2',
    channel: 'Trae',
    name: '网页登录账号(850371773232857)',
    enabled: true,
    status: 'enabled',
    credits: 3550.0,
    workCredits: 2805.86,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wb-1',
    channel: 'WorkBuddy',
    name: 'a95fdb1a-4ba5-4938-bf16-aa2233de144e',
    enabled: true,
    status: 'enabled',
    credits: 1301.23,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wb-2',
    channel: 'WorkBuddy',
    name: '93350cbf-2915-4a45-ab8f-8cc67168e383',
    enabled: true,
    status: 'enabled',
    credits: 2898.57,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wbie-1',
    channel: 'WorkBuddy_IE',
    name: '国际版账号(boy-chinese)',
    enabled: true,
    status: 'enabled',
    credits: 350.0,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
  },
]

let API_KEYS: ApiKey[] = [
  {
    id: 'api-1',
    name: 'Trae_1',
    key: 'df24c1a4b5620952c4e75b8d6e7b4bc3f997b7973ed696cb',
    createdAt: Date.now() / 1000 - 86400 * 3,
  },
  {
    id: 'api-2',
    name: 'WorkBuddy_1',
    key: 'sk-aMbhTe9SMRlykiE3zC7HgTbcvWWQFkVjvUZmJXvE7y4',
    createdAt: Date.now() / 1000 - 86400 * 2,
  },
  {
    id: 'api-3',
    name: '鲸专属',
    key: 'sk-TW7CWNt0fuVp2g8OZetsT2HIO1qVIdFewedP8GBwHE',
    createdAt: Date.now() / 1000 - 86400,
  },
]

/* 模型路由表：routeModelId = 通道前缀 + 上游模型名，与后端 provider 的
   对外命名保持一致（Trae→tr- / WorkBuddy→wb- / 国际版→wbie-）。

   数据来源：运行中网关 GET /v1/models 的真实模型名（国际版按新前缀 wbie- 书写）。
   真实环境模型总数约 100+，此处为便于演示取代表性样本（共 58 条，可触发虚拟滚动）。 */
let MODELS: ModelEntry[] = [
  // ── Trae（tr- 前缀，Work 积分通道）──
  { id: 'm-t1', channel: 'Trae', name: 'DeepSeek-V4-Flash 正式版', ratio: 0.08, ratioUnit: '×', routeModelId: 'tr-DeepSeek-V4-Flash-Official', hidden: false, pinned: true },
  { id: 'm-t2', channel: 'Trae', name: 'DeepSeek-V4-Pro 正式版', ratio: 0.36, ratioUnit: '×', routeModelId: 'tr-DeepSeek-V4-Pro-Official', hidden: false, pinned: false },
  { id: 'm-t3', channel: 'Trae', name: 'DeepSeek-V4-Flash', ratio: 0.08, ratioUnit: '×', routeModelId: 'tr-DeepSeek-V4-Flash', hidden: false, pinned: false },
  { id: 'm-t4', channel: 'Trae', name: 'DeepSeek-V4-Pro', ratio: 0.36, ratioUnit: '×', routeModelId: 'tr-DeepSeek-V4-Pro', hidden: false, pinned: false },
  { id: 'm-t5', channel: 'Trae', name: 'Kimi-K3', ratio: 1.83, ratioUnit: '×', routeModelId: 'tr-kimi-k3_dev', hidden: false, pinned: false },
  { id: 'm-t6', channel: 'Trae', name: 'Kimi-K2.8-Preview', ratio: 0.98, ratioUnit: '×', routeModelId: 'tr-kimi-k2.8-preview_dev', hidden: false, pinned: false },
  { id: 'm-t7', channel: 'Trae', name: 'GLM-5.3-Flash', ratio: 0.06, ratioUnit: '×', routeModelId: 'tr-glm-5.3-flash_dev', hidden: false, pinned: false },
  { id: 'm-t8', channel: 'Trae', name: 'Qwen3.8-Flash', ratio: 0.08, ratioUnit: '×', routeModelId: 'tr-qwen3.8-flash_dev', hidden: false, pinned: false },
  { id: 'm-t9', channel: 'Trae', name: 'Doubao-Seed-2.1-Pro', ratio: 0.42, ratioUnit: '×', routeModelId: 'tr-Doubao-Seed-2.1-Pro', hidden: false, pinned: false },
  { id: 'm-t10', channel: 'Trae', name: 'Doubao-Seed-2.1-Turbo', ratio: 0.12, ratioUnit: '×', routeModelId: 'tr-Doubao-Seed-2.1-Turbo', hidden: false, pinned: false },
  { id: 'm-t11', channel: 'Trae', name: 'Doubao-Seed-Code', ratio: 0.24, ratioUnit: '×', routeModelId: 'tr-Doubao-Seed-Code', hidden: false, pinned: false },
  { id: 'm-t12', channel: 'Trae', name: 'Doubao-Seed-Evolving', ratio: 0.3, ratioUnit: '×', routeModelId: 'tr-Doubao-Seed-Evolving', hidden: false, pinned: false },
  { id: 'm-t13', channel: 'Trae', name: 'deepseek-flash', ratio: 0.08, ratioUnit: '×', routeModelId: 'tr-deepseek-flash', hidden: false, pinned: false },
  { id: 'm-t14', channel: 'Trae', name: 'deepseek-v4', ratio: 0.36, ratioUnit: '×', routeModelId: 'tr-custom_model_deepseek_v4', hidden: false, pinned: false },
  { id: 'm-t15', channel: 'Trae', name: 'deepseek-chat', ratio: 0.1, ratioUnit: '×', routeModelId: 'tr-custom_model_deepseek_chat', hidden: false, pinned: false },
  { id: 'm-t16', channel: 'Trae', name: 'deepseek-reasoner', ratio: 0.4, ratioUnit: '×', routeModelId: 'tr-custom_model_deepseek_reasoner', hidden: false, pinned: false },
  { id: 'm-t17', channel: 'Trae', name: 'gemini', ratio: 0.25, ratioUnit: '×', routeModelId: 'tr-custom_model_gemini', hidden: false, pinned: false },
  { id: 'm-t18', channel: 'Trae', name: 'gpt-5', ratio: 1.2, ratioUnit: '×', routeModelId: 'tr-custom_model_gpt-5', hidden: false, pinned: false },
  { id: 'm-t19', channel: 'Trae', name: 'kimi', ratio: 1.5, ratioUnit: '×', routeModelId: 'tr-custom_model_kimi', hidden: false, pinned: false },
  { id: 'm-t20', channel: 'Trae', name: 'custom-1M', ratio: 1.0, ratioUnit: '×', routeModelId: 'tr-custom_model_1M', hidden: false, pinned: true },
  { id: 'm-t21', channel: 'Trae', name: 'custom-200k', ratio: 0.6, ratioUnit: '×', routeModelId: 'tr-custom_model_200k', hidden: false, pinned: false },
  { id: 'm-t22', channel: 'Trae', name: 'no-fc', ratio: 0.05, ratioUnit: '×', routeModelId: 'tr-custom_model_no-fc', hidden: false, pinned: false },
  { id: 'm-t23', channel: 'Trae', name: 'vercel', ratio: 0.3, ratioUnit: '×', routeModelId: 'tr-custom_model_vercel', hidden: false, pinned: false },
  { id: 'm-t24', channel: 'Trae', name: 'vercel-gemini', ratio: 0.32, ratioUnit: '×', routeModelId: 'tr-custom_model_vercel_gemini', hidden: false, pinned: false },
  // ── WorkBuddy（wb- 前缀，腾讯网关）──
  { id: 'm-w1', channel: 'WorkBuddy', name: 'Hy4 preview 正式版', ratio: 0.29, ratioUnit: 'credits', routeModelId: 'wb-hy4-preview', hidden: false, pinned: false },
  { id: 'm-w2', channel: 'WorkBuddy', name: 'Hy4 preview X', ratio: 0.35, ratioUnit: 'credits', routeModelId: 'wb-hy4-preview-x', hidden: false, pinned: false },
  { id: 'm-w3', channel: 'WorkBuddy', name: 'Hy3', ratio: 0.18, ratioUnit: 'credits', routeModelId: 'wb-hy3', hidden: false, pinned: false },
  { id: 'm-w4', channel: 'WorkBuddy', name: 'Hy3 X', ratio: 0.22, ratioUnit: 'credits', routeModelId: 'wb-hy3-x', hidden: false, pinned: false },
  { id: 'm-w5', channel: 'WorkBuddy', name: 'Deepseek-V4.1-Flash', ratio: 0.03, ratioUnit: 'credits', routeModelId: 'wb-deepseek-v4.1-flash', hidden: false, pinned: true },
  { id: 'm-w6', channel: 'WorkBuddy', name: 'DeepSeek-V4-Flash', ratio: 0.03, ratioUnit: 'credits', routeModelId: 'wb-deepseek-v4-flash', hidden: false, pinned: false },
  { id: 'm-w7', channel: 'WorkBuddy', name: 'DeepSeek-V4-Pro', ratio: 0.36, ratioUnit: 'credits', routeModelId: 'wb-deepseek-v4-pro', hidden: false, pinned: false },
  { id: 'm-w8', channel: 'WorkBuddy', name: 'GLM-5.3-Flash', ratio: 0.06, ratioUnit: 'credits', routeModelId: 'wb-glm-5.3-flash', hidden: false, pinned: false },
  { id: 'm-w9', channel: 'WorkBuddy', name: 'GLM-5.3', ratio: 0.12, ratioUnit: 'credits', routeModelId: 'wb-glm-5.3', hidden: false, pinned: false },
  { id: 'm-w10', channel: 'WorkBuddy', name: 'GLM-5v-Turbo', ratio: 0.2, ratioUnit: 'credits', routeModelId: 'wb-glm-5v-turbo', hidden: true, pinned: false },
  { id: 'm-w11', channel: 'WorkBuddy', name: 'Kimi-K3', ratio: 1.62, ratioUnit: 'credits', routeModelId: 'wb-kimi-k3-1', hidden: false, pinned: false },
  { id: 'm-w12', channel: 'WorkBuddy', name: 'Kimi-K2.7', ratio: 0.9, ratioUnit: 'credits', routeModelId: 'wb-kimi-k2.7', hidden: false, pinned: false },
  { id: 'm-w13', channel: 'WorkBuddy', name: 'Kimi-K2.6', ratio: 0.52, ratioUnit: 'credits', routeModelId: 'wb-kimi-k2.6', hidden: false, pinned: false },
  { id: 'm-w14', channel: 'WorkBuddy', name: 'MiniMax-M3', ratio: 0.44, ratioUnit: 'credits', routeModelId: 'wb-minimax-m3', hidden: false, pinned: false },
  // ── WorkBuddy 国际（wbie- 前缀，www.workbuddy.ai）──
  { id: 'm-i1', channel: 'WorkBuddy_IE', name: 'DeepSeek-V4.1-Flash', ratio: 0.04, ratioUnit: '×', routeModelId: 'wbie-deepseek-v4.1-flash', hidden: false, pinned: false },
  { id: 'm-i2', channel: 'WorkBuddy_IE', name: 'Kimi-K2.6', ratio: 0.52, ratioUnit: '×', routeModelId: 'wbie-kimi-k2.6', hidden: false, pinned: false },
  { id: 'm-i3', channel: 'WorkBuddy_IE', name: 'Kimi-K3', ratio: 1.62, ratioUnit: '×', routeModelId: 'wbie-kimi-k3', hidden: false, pinned: false },
  { id: 'm-i4', channel: 'WorkBuddy_IE', name: 'GLM-5.2', ratio: 0.1, ratioUnit: '×', routeModelId: 'wbie-glm-5.2', hidden: false, pinned: false },
  { id: 'm-i5', channel: 'WorkBuddy_IE', name: 'GLM-5.3', ratio: 0.12, ratioUnit: '×', routeModelId: 'wbie-glm-5.3', hidden: false, pinned: false },
  { id: 'm-i6', channel: 'WorkBuddy_IE', name: 'Hy3', ratio: 0.18, ratioUnit: '×', routeModelId: 'wbie-hy3', hidden: false, pinned: false },
  { id: 'm-i7', channel: 'WorkBuddy_IE', name: 'Hy4 preview', ratio: 0.29, ratioUnit: '×', routeModelId: 'wbie-hy4-preview', hidden: false, pinned: false },
  { id: 'm-i8', channel: 'WorkBuddy_IE', name: 'GPT-6-Astra', ratio: 2.4, ratioUnit: '×', routeModelId: 'wbie-gpt-6-astra', hidden: false, pinned: false },
  { id: 'm-i9', channel: 'WorkBuddy_IE', name: 'GPT-5.6-Sol', ratio: 2.1, ratioUnit: '×', routeModelId: 'wbie-gpt-5.6-sol', hidden: false, pinned: false },
  { id: 'm-i10', channel: 'WorkBuddy_IE', name: 'GPT-5.6-Luna', ratio: 1.8, ratioUnit: '×', routeModelId: 'wbie-gpt-5.6-luna', hidden: false, pinned: false },
  { id: 'm-i11', channel: 'WorkBuddy_IE', name: 'GPT-5.6-Terra', ratio: 1.95, ratioUnit: '×', routeModelId: 'wbie-gpt-5.6-terra', hidden: false, pinned: false },
  { id: 'm-i12', channel: 'WorkBuddy_IE', name: 'GPT-5.5', ratio: 1.5, ratioUnit: '×', routeModelId: 'wbie-gpt-5.5', hidden: false, pinned: false },
  { id: 'm-i13', channel: 'WorkBuddy_IE', name: 'GPT-5.4', ratio: 1.2, ratioUnit: '×', routeModelId: 'wbie-gpt-5.4', hidden: false, pinned: false },
  { id: 'm-i14', channel: 'WorkBuddy_IE', name: 'GPT-5.3-Codex', ratio: 1.6, ratioUnit: '×', routeModelId: 'wbie-gpt-5.3-codex', hidden: false, pinned: false },
  { id: 'm-i15', channel: 'WorkBuddy_IE', name: 'Gemini-3.5-Flash', ratio: 0.25, ratioUnit: '×', routeModelId: 'wbie-gemini-3.5-flash', hidden: false, pinned: false },
  { id: 'm-i16', channel: 'WorkBuddy_IE', name: 'balanced-model', ratio: 1.0, ratioUnit: '×', routeModelId: 'wbie-balanced-model', hidden: false, pinned: false },
  { id: 'm-i17', channel: 'WorkBuddy_IE', name: 'deep-model', ratio: 2.0, ratioUnit: '×', routeModelId: 'wbie-deep-model', hidden: false, pinned: false },
  { id: 'm-i18', channel: 'WorkBuddy_IE', name: 'fast-model', ratio: 0.5, ratioUnit: '×', routeModelId: 'wbie-fast-model', hidden: false, pinned: false },
  { id: 'm-i19', channel: 'WorkBuddy_IE', name: 'primary-model', ratio: 1.2, ratioUnit: '×', routeModelId: 'wbie-primary-model', hidden: false, pinned: false },
  { id: 'm-i20', channel: 'WorkBuddy_IE', name: 'default-model', ratio: 1.0, ratioUnit: '×', routeModelId: 'wbie-default-model', hidden: false, pinned: false },
]

/** 生成近 N 天流水（含历史日，便于今日/本周两个视图复用） */
function seedUsage(): UsageRow[] {
  const rows: UsageRow[] = []
  const now = new Date()
  const models: Record<Channel, string[]> = {
    Trae: ['deepseek-v4-flash-official', 'kimi-k3', 'DeepSeek-V4-Pro-Official'],
    WorkBuddy: ['deepseek-v4.1-flash', 'glm-5.3-flash'],
    WorkBuddy_IE: ['kimi-k2.6', 'deepseek-v4.1-flash'],
  }
  const accounts: Record<Channel, string[]> = {
    Trae: ['trae_3937', 'trae_2857'],
    WorkBuddy: ['WB_fea0', 'WB_e383'],
    WorkBuddy_IE: ['WB_IE_boy'],
  }
  for (let d = 0; d < 9; d++) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - d)
    for (const ch of ['Trae', 'WorkBuddy', 'WorkBuddy_IE'] as Channel[]) {
      const count = ch === 'WorkBuddy_IE' ? 2 : 4 + (d % 3)
      for (let i = 0; i < count; i++) {
        const t = new Date(day)
        t.setHours(d === 0 ? Math.max(0, now.getHours() - i) : 9 + i * 3, (i * 13) % 60, 0, 0)
        const mlist = models[ch]
        const alist = accounts[ch]
        rows.push({
          id: uid('u'),
          channel: ch,
          account: alist[i % alist.length],
          model: mlist[i % mlist.length],
          ts: Math.floor(t.getTime() / 1000),
          amount: Number(([0, 0, 0, 0.42, 1.86, 0.03, 12.4][i % 7] * (1 + d * 0.1)).toFixed(2)),
        })
      }
    }
  }
  return rows.sort((a, b) => b.ts - a.ts)
}

let USAGE: UsageRow[] = seedUsage()

let LOGS: LogLine[] = [
  { id: 1, level: 'info', text: '正在启动 open-ai 网关 (127.0.0.1:8000) ...', ts: Date.now() / 1000 - 120 },
  { id: 2, level: 'success', text: '[OK] 网关已就绪，监听 0.0.0.0:8000', ts: Date.now() / 1000 - 119 },
  { id: 3, level: 'info', text: '正在拉取模型列表及积分倍率 ...', ts: Date.now() / 1000 - 90 },
  { id: 4, level: 'success', text: '[Done] Trae 模型 12 个已载入', ts: Date.now() / 1000 - 88 },
  { id: 5, level: 'warn', text: '[Warn] WorkBuddy 账号 a95fdb1a 心跳延迟 1.2s', ts: Date.now() / 1000 - 60 },
  { id: 6, level: 'error', text: '[Err] WorkBuddy_IE 登录态失效，请重新连接', ts: Date.now() / 1000 - 30 },
  { id: 7, level: 'info', text: '[Done] 22:50:24', ts: Date.now() / 1000 - 10 },
]

let gateway: GatewayInfo = {
  openaiBase: 'http://127.0.0.1:8000/v1',
  chatEndpoint: 'http://127.0.0.1:8000/v1/chat/completions',
  anthropicBase: 'http://127.0.0.1:8000',
}

/* ═══════════════ 周数据生成 ═══════════════ */

/**
 * 以周偏移量 + 通道筛选生成堆叠柱状图数据（负数=过去，0=本周）
 *
 * 通道语义：`all` 返回三通道堆叠；指定通道时，该通道数据保留、其余通道置 0
 * （等价于「只看该通道的柱子」），统计卡的消耗/获取同步按通道收窄。
 */
function buildWeek(offset: number, channel: ChannelFilter = 'all'): WeekBundle {
  const now = new Date()
  // 本周一为起点
  const dow = (now.getDay() + 6) % 7
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - dow + offset * 7)

  const daily: DailyUsage[] = []
  let used = 0
  let gained = 0
  for (let i = 0; i < 7; i++) {
    const d = new Date(monday)
    d.setDate(monday.getDate() + i)
    const key = toDayKey(d)
    const isFuture = d > now
    // 三通道量级刻意错开：Trae 为主力（量最大），WorkBuddy 次之，国际版最小
    // 数值由日期确定性派生，保证切换周/刷新时数据稳定可比
    const seed = (d.getFullYear() * 372 + (d.getMonth() + 1) * 31 + d.getDate()) % 97
    const raw = {
      Trae: isFuture ? 0 : Number((seed * 4.2 + (offset === 0 ? 220 : 90)).toFixed(1)),
      WorkBuddy: isFuture ? 0 : Number((seed * 3.1 + (offset === 0 ? 150 : 60)).toFixed(1)),
      WorkBuddy_IE: isFuture ? 0 : Number((seed * 1.6 + 30).toFixed(1)),
    }
    // 通道筛选：非选中通道归零，选中通道（或 all）保留
    const pick = (ch: Channel) => (channel === 'all' || channel === ch ? raw[ch] : 0)
    const trae = pick('Trae')
    const wb = pick('WorkBuddy')
    const wbie = pick('WorkBuddy_IE')
    used += trae + wb + wbie
    gained += isFuture ? 0 : seed * 2.2 + 120
    daily.push({ day: key, Trae: trae, WorkBuddy: wb, WorkBuddy_IE: wbie })
  }

  // 本周获取积分：按通道等比分摊（Trae 贡献最大），保证切通道时数字随之变化
  const weekGainBase = offset === 0 ? 3200 : 2800 + ((offset * 137) % 900)
  const share: Record<ChannelFilter, number> = {
    all: 1,
    Trae: 0.56,
    WorkBuddy: 0.33,
    WorkBuddy_IE: 0.11,
  }

  const last = new Date(monday)
  last.setDate(monday.getDate() + 6)

  return {
    stats: {
      gained: Number((weekGainBase * share[channel]).toFixed(2)),
      used: Number(used.toFixed(2)),
    },
    daily,
    startDay: toDayKey(monday),
    endDay: toDayKey(last),
    offset,
    updatedAt: Date.now() / 1000,
  }
}

/* ═══════════════ 对外 API（页面唯一数据入口） ═══════════════ */

export const mockBackend = {
  /* ── 账号 ── */
  async listAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    await sleep(LATENCY)
    const list = filter === 'all' ? ACCOUNTS : ACCOUNTS.filter((a) => a.channel === filter)
    return list.map((a) => ({ ...a }))
  },

  /** 刷新指定通道账号状态（重新拉取积分） */
  async refreshAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    await sleep(LATENCY * 2)
    const now = Date.now() / 1000
    ACCOUNTS = ACCOUNTS.map((a) => {
      const inScope = filter === 'all' || a.channel === filter
      if (!inScope || a.status === 'disconnected') return a
      const delta = Number((Math.random() * 40 - 8).toFixed(2))
      return {
        ...a,
        credits: Number(Math.max(0, a.credits + delta).toFixed(2)),
        refreshedAt: now,
      }
    })
    return mockBackend.listAccounts(filter)
  },

  /** 重连指定通道账号 */
  async reconnectAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    await sleep(LATENCY * 3)
    ACCOUNTS = ACCOUNTS.map((a) => {
      const inScope = filter === 'all' || a.channel === filter
      if (!inScope) return a
      return {
        ...a,
        status: a.enabled ? 'enabled' : 'disabled',
        refreshedAt: Date.now() / 1000,
      }
    })
    return mockBackend.listAccounts(filter)
  },

  async toggleAccount(id: string): Promise<void> {
    await sleep(LATENCY / 2)
    ACCOUNTS = ACCOUNTS.map((a) =>
      a.id === id
        ? { ...a, enabled: !a.enabled, status: !a.enabled ? 'enabled' : 'disabled' }
        : a
    )
  },

  async deleteAccount(id: string): Promise<void> {
    await sleep(LATENCY / 2)
    ACCOUNTS = ACCOUNTS.filter((a) => a.id !== id)
  },

  async addAccount(channel: Channel): Promise<Account> {
    await sleep(LATENCY * 2)
    const nameMap: Record<Channel, string> = {
      Trae: `网页登录账号(${Math.floor(Math.random() * 9e14 + 1e14)})`,
      WorkBuddy: crypto.randomUUID(),
      WorkBuddy_IE: `国际版账号(user-${Math.floor(Math.random() * 9000 + 1000)})`,
    }
    const acc: Account = {
      id: uid('acc'),
      channel,
      name: nameMap[channel],
      enabled: true,
      status: 'enabled',
      credits: 0,
      workCredits: 0,
      refreshedAt: Date.now() / 1000,
    }
    ACCOUNTS = [...ACCOUNTS, acc]
    return acc
  },

  /* ── API 密钥 ── */
  async listApiKeys(): Promise<ApiKey[]> {
    await sleep(LATENCY)
    return API_KEYS.map((k) => ({ ...k }))
  },

  async createApiKey(): Promise<ApiKey> {
    await sleep(LATENCY)
    const raw = Array.from(crypto.getRandomValues(new Uint8Array(24)))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
    const item: ApiKey = {
      id: uid('api'),
      name: `new-api-${API_KEYS.length + 1}`,
      key: `sk-${raw}`,
      createdAt: Date.now() / 1000,
    }
    API_KEYS = [...API_KEYS, item]
    return item
  },

  /** 按 key 定位（key 不可变且唯一；name 可变故不作标识） */
  async renameApiKey(id: string, name: string, key?: string): Promise<void> {
    await sleep(LATENCY / 2)
    const target = key ?? id
    API_KEYS = API_KEYS.map((k) => (k.key === target ? { ...k, name } : k))
  },

  async deleteApiKey(id: string, key?: string): Promise<void> {
    await sleep(LATENCY / 2)
    const target = key ?? id
    API_KEYS = API_KEYS.filter((k) => k.key !== target)
  },

  async getGateway(): Promise<GatewayInfo> {
    await sleep(LATENCY / 2)
    return { ...gateway }
  },

  /* ── 模型 ── */
  async listModels(opts?: {
    channel?: ChannelFilter
    showHidden?: boolean
  }): Promise<ModelEntry[]> {
    await sleep(LATENCY)
    const { channel = 'all', showHidden = false } = opts ?? {}
    return MODELS.filter(
      (m) => (channel === 'all' || m.channel === channel) && (showHidden || !m.hidden)
    )
      .slice()
      .sort((a, b) => Number(b.pinned) - Number(a.pinned))
      .map((m) => ({ ...m }))
  },

  async refreshModels(): Promise<void> {
    await sleep(LATENCY * 3)
  },

  async updateModels(ids: string[], patch: Partial<ModelEntry>): Promise<void> {
    await sleep(LATENCY / 2)
    const set = new Set(ids)
    MODELS = MODELS.map((m) => (set.has(m.id) ? { ...m, ...patch } : m))
  },

  /* ── 积分 ── */
  async getToday(channel: ChannelFilter = 'all'): Promise<TodayBundle> {
    await sleep(LATENCY)
    const todayKey = toDayKey(new Date())
    const rows = USAGE.filter((r) => {
      const d = new Date(r.ts * 1000)
      return toDayKey(d) === todayKey && (channel === 'all' || r.channel === channel)
    })
    const used = rows.reduce((s, r) => s + r.amount, 0)
    return {
      stats: { gained: 600, used: Number(used.toFixed(2)) },
      rows,
      updatedAt: Date.now() / 1000,
    }
  },

  /** 按周偏移 + 通道筛选取周数据（通道参与过滤，故必须随筛选变化重新拉取） */
  async getWeek(offset = 0, channel: ChannelFilter = 'all'): Promise<WeekBundle> {
    await sleep(LATENCY)
    return buildWeek(offset, channel)
  },

  /* ── 日志 ── */
  async listLogs(): Promise<LogLine[]> {
    await sleep(LATENCY / 2)
    return LOGS.map((l) => ({ ...l }))
  },

  /** 追加一条日志（用于演示自动滚动） */
  appendLog(level: LogLevel, text: string): LogLine {
    const line: LogLine = { id: ++seq, level, text, ts: Date.now() / 1000 }
    LOGS = [...LOGS, line]
    return line
  },

  /* ── 设置 ── */
  async getAutostart(): Promise<boolean> {
    await sleep(LATENCY / 2)
    return true
  },

  async setAutostart(_enabled: boolean): Promise<void> {
    await sleep(LATENCY)
  },

  async getVersion(): Promise<{ current: string; latest?: string }> {
    await sleep(LATENCY / 2)
    return { current: 'v3.0-dev' }
  },

  async checkUpdate(): Promise<{ hasUpdate: boolean; latest: string }> {
    await sleep(LATENCY * 3)
    return { hasUpdate: false, latest: 'v3.0-dev' }
  },
}

export type MockBackend = typeof mockBackend
