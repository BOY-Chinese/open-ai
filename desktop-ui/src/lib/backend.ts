/**
 * 演示数据源（Mock）— 与真实数据源 `httpBackend` 实现同一套函数签名。
 *
 * 用途：无网关环境下跑通界面、视觉回归、演示。
 * 默认数据源见 `@/lib/dataSource`（页面只 import `backend`，不直接依赖本文件）。
 */
import type {
  Account,
  ApiKey,
  AutoChain,
  Channel,
  ChannelFilter,
  DailyUsage,
  GatewayInfo,
  ModelEntry,
  SigninBundle,
  SigninStatus,
  TodayBundle,
  UsageRow,
  WeekBundle,
} from '@/types/domain'
import { toDayKey } from '@/lib/utils'
import { filterModelView } from '@/lib/modelCache'
import { emitAccountsChanged } from '@/lib/accountEvents'
import { followLoginLog, type LoginLogChunk } from '@/lib/loginStream'

/* ═══════════════ 内部工具 ═══════════════ */

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
let seq = 0
const uid = (p: string) => `${p}-${++seq}`

/** 模拟网络延迟：让 Loading/Skeleton 状态真实可见 */
const LATENCY = 320

/* ═══════════════ Mock 种子数据（取自真实界面截图） ═══════════════ */

/* ⚠ 本文件是**演示数据源**，会被 Vite 原样打进前端产物 (dist/assets/*.js)。
   因此这里的账号名与密钥**必须是编造的占位值**：
   真实密钥只存在于 `config.json`，由 Tauri 的 `gateway_config` 命令在运行时
   注入前端内存。曾经此处直接抄了真实截图里的密钥与账号 ID，等于把网关凭据
   随安装包一起公开发布 —— 改回真值前请先想清楚这一条。 */
let ACCOUNTS: Account[] = [
  {
    id: 'acc-trae-1',
    channel: 'Trae',
    name: '示例账号(Trae-1)',
    enabled: true,
    status: 'enabled',
    credits: 4334.54,
    workCredits: 2800.0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-trae-2',
    channel: 'Trae',
    name: '示例账号(Trae-2)',
    enabled: true,
    status: 'enabled',
    credits: 3550.0,
    workCredits: 2805.86,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wb-1',
    channel: 'WorkBuddy',
    name: '示例账号(WorkBuddy-1)',
    enabled: true,
    status: 'enabled',
    credits: 1301.23,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wb-2',
    channel: 'WorkBuddy',
    name: '示例账号(WorkBuddy-2)',
    enabled: true,
    status: 'enabled',
    credits: 2898.57,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
  },
  {
    id: 'acc-wbie-1',
    channel: 'WorkBuddy_IE',
    name: '示例账号(国际版-1)',
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
    key: 'DEMO-KEY-NOT-REAL-0000000000000000000000000000',
    createdAt: Date.now() / 1000 - 86400 * 3,
  },
  {
    id: 'api-2',
    name: 'WorkBuddy_1',
    key: 'sk-DEMO-KEY-NOT-REAL-000000000000000000000000',
    createdAt: Date.now() / 1000 - 86400 * 2,
  },
  {
    id: 'api-3',
    name: '演示密钥',
    key: 'sk-DEMO-KEY-NOT-REAL-111111111111111111111111',
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
  // Loomy 模型不进演示数据：演示包只随三通道种子，Loomy 列表一律由真实网关提供
]

/** 生成近 N 天流水（含历史日，便于今日/本周两个视图复用） */function seedUsage(): UsageRow[] {
  const rows: UsageRow[] = []
  const now = new Date()
  const models: Record<Channel, string[]> = {
    Trae: ['deepseek-v4-flash-official', 'kimi-k3', 'DeepSeek-V4-Pro-Official'],
    WorkBuddy: ['deepseek-v4.1-flash', 'glm-5.3-flash'],
    WorkBuddy_IE: ['kimi-k2.6', 'deepseek-v4.1-flash'],
    Loomy: [],
  }
  const accounts: Record<Channel, string[]> = {
    Trae: ['trae_3937', 'trae_2857'],
    WorkBuddy: ['WB_fea0', 'WB_e383'],
    WorkBuddy_IE: ['WB_IE_boy'],
    Loomy: [],
  }
  for (let d = 0; d < 9; d++) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - d)
    for (const ch of ['Trae', 'WorkBuddy', 'WorkBuddy_IE', 'Loomy'] as Channel[]) {
      // Loomy 不造演示流水（主机同款账号/模型数据不进演示包），行数恒 0
      const count = ch === 'WorkBuddy_IE' ? 2 : ch === 'Loomy' ? 0 : 4 + (d % 3)
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

/**
 * 演示用「今日签到结果」——可变状态。
 *
 * 刻意让两种状态都出现（国际版账号今日未签到），否则「每日签到」这一列在演示
 * 模式下永远是同一个样子，等于没验证过；而可变是因为「刷新账号」会补签
 * （见 refreshSignin），补完再看这一列必须真的变了。
 */
const DEMO_SIGNIN: Record<string, SigninStatus> = (() => {
  const midnight = new Date()
  midnight.setHours(0, 5, 0, 0)
  const ts = Math.floor(midnight.getTime() / 1000)
  return {
    'acc-trae-1': { checkedIn: true, amount: 200, kinds: ['checkin'], ts },
    'acc-trae-2': { checkedIn: true, amount: 200, kinds: ['checkin'], ts },
    'acc-wb-1': {
      checkedIn: true,
      amount: 100,
      kinds: ['pack:CodeBuddy个人版国内运营裂变包'],
      ts,
    },
    'acc-wb-2': {
      checkedIn: true,
      amount: 100,
      kinds: ['pack:CodeBuddy个人版国内运营裂变包'],
      ts,
    },
    // acc-wbie-1（国际版）今日未签到：用于验证未签到态与提示文案
  }
})()

/**
 * 演示签到凭证对应的统计日。
 *
 * 跨零点后 `DEMO_SIGNIN` 里的凭证就过期了（真实后端同样按当天流水判定），
 * 故读取与补签时都过一遍 {@link syncDemoSigninDay}：日期一变即清空重来，
 * 否则界面会一直显示「昨天已签到」。
 */
let DEMO_SIGNIN_DAY = toDayKey(new Date())

function syncDemoSigninDay(): string {
  const today = toDayKey(new Date())
  if (DEMO_SIGNIN_DAY !== today) {
    for (const id of Object.keys(DEMO_SIGNIN)) delete DEMO_SIGNIN[id]
    DEMO_SIGNIN_DAY = today
  }
  return today
}

let gateway: GatewayInfo = {
  openaiBase: 'http://127.0.0.1:8000/v1',
  chatEndpoint: 'http://127.0.0.1:8000/v1/chat/completions',
  anthropicBase: 'http://127.0.0.1:8000',
}

/* ═══════════════ 周数据生成 ═══════════════ */

/**
 * 以周偏移量 + 通道筛选生成堆叠柱状图数据（负数=过去，0=本周）
 *
 * 通道语义：`all` 返回四通道堆叠；指定通道时，该通道数据保留、其余通道置 0
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
    // 四通道量级刻意错开：Trae 为主力（量最大），WorkBuddy 次之，国际版再次；
    // Loomy 演示值恒 0（不造主机同款数据，真实值由网关提供）
    // 数值由日期确定性派生，保证切换周/刷新时数据稳定可比
    const seed = (d.getFullYear() * 372 + (d.getMonth() + 1) * 31 + d.getDate()) % 97
    const raw = {
      Trae: isFuture ? 0 : Number((seed * 4.2 + (offset === 0 ? 220 : 90)).toFixed(1)),
      WorkBuddy: isFuture ? 0 : Number((seed * 3.1 + (offset === 0 ? 150 : 60)).toFixed(1)),
      WorkBuddy_IE: isFuture ? 0 : Number((seed * 1.6 + 30).toFixed(1)),
      Loomy: 0,
    }
    // 通道筛选：非选中通道归零，选中通道（或 all）保留
    const pick = (ch: Channel) => (channel === 'all' || channel === ch ? raw[ch] : 0)
    const trae = pick('Trae')
    const wb = pick('WorkBuddy')
    const wbie = pick('WorkBuddy_IE')
    const loomy = pick('Loomy')
    used += trae + wb + wbie + loomy
    gained += isFuture ? 0 : seed * 2.2 + 120
    daily.push({ day: key, Trae: trae, WorkBuddy: wb, WorkBuddy_IE: wbie, Loomy: loomy })
  }

  // 本周获取积分：按通道等比分摊（Trae 贡献最大），保证切通道时数字随之变化
  const weekGainBase = offset === 0 ? 3200 : 2800 + ((offset * 137) % 900)
  const share: Record<ChannelFilter, number> = {
    all: 1,
    Trae: 0.52,
    WorkBuddy: 0.31,
    WorkBuddy_IE: 0.17,
    Loomy: 0,
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

/* ═══════════════ 演示用的「登录脚本输出」 ═══════════════ */

/**
 * 模拟登录脚本输出：每个元素是**一次轮询**返回的一片。
 *
 * 分片而不是一次性给完，是为了让「输出实时滚动」在演示模式下真的看得见 ——
 * 一次给完的话，跟随循环一轮就结束了，跟直接打印没区别。
 */
function mockScript(channel: Channel, host: string): string[] {
  return [
    `[演示 15:05:12] ============================================================\n`,
    `[演示 15:05:12]   ${channel} 登录助手（演示数据，不会真的打开浏览器）\n` +
      `[演示 15:05:12]   目标站点: ${host}\n`,
    `[演示 15:05:13] 正在打开登录页 ...\n` +
      `[演示 15:05:14] 等待用户在浏览器中完成登录 ...\n`,
    `[演示 15:05:16] 已捕获 accessToken（长度 1302）\n`,
    `[演示 15:05:16] 校验 token 可用性 ... 成功\n` +
      `[演示 15:05:16] 账号已写入 config.json\n`,
  ]
}

interface MockLoginState {
  chunks: string[]
  /** 已吐出的分片数 */
  served: number
  /** 已吐出的字符数（作为返回给前端的新偏移） */
  offset: number
}

const MOCK_LOGIN: Record<string, MockLoginState> = {}

/** 造一个演示账号（与原 addAccount 的账号形状一致） */
function makeMockAccount(channel: Channel): Account {
  const nameMap: Record<Channel, string> = {
    Trae: `网页登录账号(${Math.floor(Math.random() * 9e14 + 1e14)})`,
    WorkBuddy: crypto.randomUUID(),
    WorkBuddy_IE: `国际版账号(user-${Math.floor(Math.random() * 9000 + 1000)})`,
    Loomy: `Loomy(${Math.floor(Math.random() * 9e9 + 1.3e10)})`,
  }
  return {
    id: uid('acc'),
    channel,
    name: nameMap[channel],
    enabled: true,
    status: 'enabled',
    credits: 0,
    workCredits: 0,
    refreshedAt: Date.now() / 1000,
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

  /**
   * 今日签到结果（演示数据）。
   *
   * 刻意让「已签到/未签到」两种状态都出现（国际版账号今天未签到），
   * 否则这列在演示模式下永远是同一个样子，等于没验证过。
   */
  async listSignin(filter: ChannelFilter = 'all'): Promise<SigninBundle> {
    await sleep(LATENCY / 2)
    const day = syncDemoSigninDay()
    // 演示签到结果：`DEMO_SIGNIN` 是可变状态，刷新账号时的「补签」会真的改它
    // （见 refreshSignin），否则「点刷新 → 未签到变已签到」这条路径无法演示。
    const signin: Record<string, SigninStatus> = {}
    for (const a of ACCOUNTS) {
      if (filter !== 'all' && a.channel !== filter) continue
      if (DEMO_SIGNIN[a.id]) signin[a.id] = { ...DEMO_SIGNIN[a.id] }
    }
    return { day, signin }
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

  /**
   * 补一次签到（演示）。
   *
   * 与真实后端同契约：把当前筛选范围内「今日未签到」的账号补成已签到，
   * 并回读签到表。演示模式下这一步会真的改变 `DEMO_SIGNIN`，
   * 于是「点刷新 → 未签到变已签到」这条路径在前端也验证得到。
   */
  async refreshSignin(
    filter: ChannelFilter = 'all',
    force = false
  ): Promise<{ signin: SigninBundle; pending: string[]; ran: string[]; day: string }> {
    await sleep(LATENCY * 2)
    const day = syncDemoSigninDay()
    const midnight = new Date()
    midnight.setHours(0, 5, 0, 0)
    const ts = Math.floor(midnight.getTime() / 1000)
    const scoped = ACCOUNTS.filter((a) => filter === 'all' || a.channel === filter)
    // 国际版无签到渠道：既不算 pending，也不会被补成已签到
    const pending = scoped.filter(
      (a) => a.channel !== 'WorkBuddy_IE' && (!DEMO_SIGNIN[a.id] || force)
    )
    for (const a of pending) {
      DEMO_SIGNIN[a.id] = {
        checkedIn: true,
        amount: a.channel === 'Trae' ? 200 : 100,
        kinds: [a.channel === 'Trae' ? 'checkin' : 'pack:演示资源包'],
        ts,
      }
    }
    DEMO_SIGNIN_DAY = day
    return {
      signin: await mockBackend.listSignin(filter),
      pending: pending.map((a) => a.id),
      ran: pending.length > 0 ? ['--wb-only'] : [],
      day,
    }
  },

  /** 重连指定通道账号 */
  async reconnectAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {    await sleep(LATENCY * 3)
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

  /**
   * 添加账号（演示）：拉起一次**模拟的**登录脚本，并让跟随逻辑真实地跑一遍。
   *
   * 关键取舍：这里不直接 append 几行假日志了事，而是把脚本输出放进
   * {@link MOCK_LOGIN_SCRIPTS}，由 `loginLog()` 一片一片吐出来，再交给
   * 与真实后端**完全相同**的 `followLoginLog` 驱动。
   * 这样演示模式验证的就是真实那条代码路径（轮询、分行、级别判定、
   * 结束判定、结束回调），而不是一条平行实现的仿制品。
   */
  async addAccount(channel: Channel): Promise<Account> {
    const script =
      channel === 'Trae' ? 'login_trae.py'
        : channel === 'WorkBuddy' ? 'login_workbuddy.py'
          : channel === 'Loomy' ? 'loomy_client.py'
            : 'login_workbuddy_intl.py'
    const host =
      channel === 'Trae' ? 'www.trae.cn'
        : channel === 'WorkBuddy' ? 'copilot.tencent.com'
          : channel === 'Loomy' ? 'loomyad.xunfei.cn'
            : 'www.workbuddy.ai'

    await sleep(LATENCY * 2)
    // 重放：同一通道再次登录时从头开始
    MOCK_LOGIN[channel] = { chunks: mockScript(channel, host), served: 0, offset: 0 }

    // 与真实实现同形状：等到脚本结束才返回（见 httpBackend.addAccount 的说明）
    await followLoginLog({
      fetchChunk: (offset) => this.loginLog(channel, offset),
      startOffset: 0,
      label: `${channel} 登录助手（${script}）`,
      onFinished: () => {
        // 演示：脚本结束 = 登录成功，新账号**此刻**才进列表
        // （真实实现里账号是脚本自己写进 config.json 的，这里等价补上）
        ACCOUNTS = [...ACCOUNTS, makeMockAccount(channel)]
        emitAccountsChanged(channel)
      },
    })

    // 与真实实现一致：立即返回占位对象，不等登录完成
    return {
      id: `${channel}:pending`,
      channel,
      name: '等待登录完成…',
      enabled: false,
      status: 'disconnected',
      credits: 0,
      workCredits: 0,
      refreshedAt: 0,
    }
  },

  /** 演示用的脚本输出：每次调用吐一片，吐完即视为脚本结束 */
  async loginLog(channel: Channel, _offset: number): Promise<LoginLogChunk> {
    await sleep(LATENCY / 2)
    const st = MOCK_LOGIN[channel]
    if (!st) {
      return { text: '', offset: 0, running: false, exists: false, exitCode: null }
    }
    const chunk = st.chunks[st.served] ?? ''
    if (chunk) {
      st.served += 1
      st.offset += chunk.length
    }
    const running = st.served < st.chunks.length
    return {
      text: chunk,
      offset: st.offset,
      running,
      exists: true,
      // 演示脚本「登录成功」：退出码 0
      exitCode: running ? null : 0,
    }
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
    // 与 httpBackend 同契约：返回全量列表的浅拷贝，筛选走同一个函数。
    // 缓存由页面统一写入（见 lib/modelCache.ts），此处不落盘。
    const all = MODELS.slice().sort((a, b) => Number(b.pinned) - Number(a.pinned))
    return filterModelView(all, channel, showHidden).map((m) => ({ ...m }))
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

  /**
   * 催一次流水采集（演示）。
   *
   * 真实后端在这里拉起 usage_collector 把新流水采进本地库；演示数据源没有库，
   * 但**必须保留这个方法**：否则界面在演示模式下的刷新路径与真实路径不同，
   * 「刷新没反应」这类 bug 就演示不出来（契约见 dataSource.ts）。
   */
  async refreshCredits(): Promise<void> {
    await sleep(LATENCY * 2)
    // 演示：让今日多出一笔流水，便于肉眼确认「刷新后数字确实变了」
    const now = Date.now() / 1000
    USAGE = [
      {
        id: uid('u'),
        channel: 'Trae',
        account: '示例账号(Trae-1)',
        model: 'deepseek-v4-flash-official',
        ts: Math.floor(now),
        amount: Number((Math.random() * 3 + 0.1).toFixed(2)),
      },
      ...USAGE,
    ]
  },

  /* ── Auto 路由连（演示） ── */
  async getAutoChain(): Promise<{ chain: AutoChain; availableModels: string[] }> {
    await sleep(LATENCY)
    return {
      chain: {
        enabled: true,
        timeout: 120,
        models: [
          'tr-DeepSeek-V4-Flash-Official',
          'wb-deepseek-v4.1-flash',
        ],
      },
      availableModels: MODELS.map((m) => m.routeModelId).sort(),
    }
  },

  async saveAutoChain(_chain: AutoChain): Promise<void> {
    await sleep(LATENCY)
  },

  /* ── Loomy 登录（演示） ── */

  async sendLoomyDesktopCode(
    phone: string
  ): Promise<{ ok: boolean; messageId?: string; error?: string }> {
    await sleep(LATENCY)
    if (!/^1\d{10}$/.test(phone)) return { ok: false, error: '手机号格式不正确（演示校验）' }
    return { ok: true, messageId: `demo-desktop-msg-${Date.now()}` }
  },

  async loomyDesktopLogin(
    phone: string,
    code: string
  ): Promise<{ ok: boolean; userid?: string; name?: string; error?: string }> {
    await sleep(LATENCY)
    if (code === '000000') return { ok: false, error: '验证码错误（演示：任意 6 位数字均可）' }
    const uid = `260913${String(Math.floor(Math.random() * 1e10)).padStart(10, '0')}`
    ACCOUNTS = [
      ...ACCOUNTS.filter((a) => a.id !== `Loomy:${uid}`),
      {
        id: `Loomy:${uid}`,
        channel: 'Loomy',
        name: `Loomy(${phone})`,
        enabled: true,
        status: 'enabled',
        credits: 0,
        workCredits: 0,
        refreshedAt: Date.now() / 1000,
      },
    ]
    emitAccountsChanged('Loomy')
    return { ok: true, userid: uid, name: `Loomy(${phone})` }
  },

  async sendLoomyCode(
    phone: string
  ): Promise<{ ok: boolean; messageId?: string; error?: string }> {
    await sleep(LATENCY)
    if (!/^1\d{10}$/.test(phone)) return { ok: false, error: '手机号格式不正确（演示校验）' }
    return { ok: true, messageId: `demo-msg-${Date.now()}` }
  },

  async loomyWebLogin(
    phone: string,
    code: string
  ): Promise<{ ok: boolean; userid?: string; name?: string; error?: string }> {
    await sleep(LATENCY)
    if (code === '000000') return { ok: false, error: '验证码错误（演示：任意 6 位数字均可）' }
    // 演示：同手机号的 Web 半边并入既有合并行（桌面优先出 id/名字），
    // 与真实后端 _loomy_groups 的「一行」语义一致
    const desktop = ACCOUNTS.find(
      (a) => a.channel === 'Loomy' && a.name === `Loomy(${phone})`
    )
    if (desktop) {
      emitAccountsChanged('Loomy')
      return { ok: true, userid: `web:${phone}`, name: desktop.name }
    }
    ACCOUNTS = [
      ...ACCOUNTS.filter((a) => a.id !== `Loomy:web:${phone}`),
      {
        id: `Loomy:web:${phone}`,
        channel: 'Loomy',
        name: `Loomy Web(${phone})`,
        enabled: true,
        status: 'enabled',
        credits: 0,
        workCredits: 0,
        refreshedAt: Date.now() / 1000,
      },
    ]
    emitAccountsChanged('Loomy')
    return { ok: true, userid: `web:${phone}`, name: `Loomy Web(${phone})` }
  },

  async checkLoomySession(phone: string): Promise<{ valid: boolean; nickname?: string }> {
    await sleep(LATENCY / 2)
    const hit = ACCOUNTS.find((a) => a.id === `Loomy:web:${phone}`)
    return { valid: !!hit, nickname: hit ? '演示用户' : undefined }
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
    // 与 version.py 的 APP_VERSION 保持一致的形状（真实版 dev-v3.1）：
    // 演示模式若落后一个版本，视觉回归就会把「版本号显示错」当成正常。
    return { current: 'dev-v3.1' }
  },

  async checkUpdate(): Promise<{ hasUpdate: boolean; latest: string }> {
    await sleep(LATENCY * 3)
    return { hasUpdate: false, latest: 'dev-v3.1' }
  },
}

export type MockBackend = typeof mockBackend
