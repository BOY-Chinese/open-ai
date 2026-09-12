/**
 * HTTP 数据源 — 对接 open-ai 网关的管理面 (/v1/admin/*)
 *
 * 与 `mock.ts` 实现**完全相同的函数签名**（契约见 `./contract.ts`），
 * 因此页面无需改动即可在「真实后端」与「演示数据」之间切换。
 *
 * 鉴权与跨域：
 *  - 开发态：Vite dev server 把 /v1 代理到 127.0.0.1:8000 并注入 Bearer 密钥
 *  - 生产态：经 Tauri 命令读同一份 config.json，拿到真实地址与密钥
 *  两种形态下前端代码一致、产物中都不含密钥（见 `./gateway.ts`）。
 */
import { authHeaders, gatewayRuntime } from './gateway'
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

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 统一请求：超时 + 错误归一化（把网关的 detail 转成可读文案） */
async function req<T>(
  path: string,
  init?: { method?: 'GET' | 'POST'; body?: unknown; timeoutMs?: number }
): Promise<T> {
  const { method = 'GET', body, timeoutMs = 20000 } = init ?? {}
  // 运行期解析网关地址与密钥：打包态由 Tauri 从 config.json 提供，
  // 开发态为空串 → 同源相对路径 → Vite 代理转发并注入密钥
  const rt = await gatewayRuntime()
  const headers: Record<string, string> = { ...(authHeaders(rt) ?? {}) }
  if (body) headers['Content-Type'] = 'application/json'

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)

  try {
    const res = await fetch(`${rt.baseUrl}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    })

    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const j = await res.json()
        const d = j?.detail
        detail = typeof d === 'string' ? d : d?.message ?? JSON.stringify(d ?? j)
      } catch {
        /* 保留 HTTP 状态文案 */
      }
      throw new ApiError(res.status, detail)
    }
    return (await res.json()) as T
  } catch (e) {
    if (e instanceof ApiError) throw e
    if ((e as Error).name === 'AbortError') {
      throw new ApiError(408, '请求超时（网关无响应）')
    }
    // 带上实际请求地址：虚拟机排障时一眼能看出「打到哪儿去了」
    throw new ApiError(
      0,
      `无法连接网关${rt.baseUrl ? `（${rt.baseUrl}）` : ''}：${(e as Error).message}`
    )
  } finally {
    clearTimeout(timer)
  }
}

const q = (params: Record<string, string | number | undefined>) => {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '' && v !== 'all') sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

/* ═══════════════ 本地视图偏好 ═══════════════ */

/** 模型隐藏/置顶偏好（后端模型目录无此字段，属前端视图状态） */
function readModelPrefs(): Record<string, Partial<ModelEntry>> {
  try {
    return JSON.parse(localStorage.getItem('open-ai.model-prefs') ?? '{}')
  } catch {
    return {}
  }
}

/* ═══════════════ 账号 ═══════════════ */

export const httpBackend = {
  async listAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    const [accRes, creditRes] = await Promise.all([
      req<{ accounts: Account[] }>(`/v1/admin/accounts${q({ channel: filter })}`),
      // 积分走缓存接口（不联网，毫秒级）；未刷新过时为空对象
      req<{ credits: Record<string, { credits: number; workCredits: number }> }>(
        '/v1/admin/accounts/credits' + q({ channel: filter })
      ).catch(() => ({ credits: {} as Record<string, { credits: number; workCredits: number }> })),
    ])
    return accRes.accounts.map((a) => {
      const c = creditRes.credits[a.id]
      return {
        ...a,
        credits: c?.credits ?? a.credits ?? 0,
        workCredits: c?.workCredits ?? a.workCredits ?? 0,
        refreshedAt: c ? Date.now() / 1000 : a.refreshedAt ?? 0,
      }
    })
  },

  /**
   * 刷新账号：先重新拉取列表，再触发一次联网积分刷新（真实余额）。
   * 联网较慢（逐账号请求上游），故给足超时并允许部分失败。
   */
  async refreshAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    await req('/v1/admin/accounts/credits/refresh', {
      method: 'POST',
      body: { channel: filter },
      timeoutMs: 120000,
    }).catch((e) => {
      // 积分刷新失败不应让整个操作失败：列表仍可用，余额保持旧值
      console.warn('[admin] 积分刷新失败：', (e as Error).message)
    })
    return this.listAccounts(filter)
  },

  async reconnectAccounts(filter: ChannelFilter = 'all'): Promise<Account[]> {
    await req('/v1/admin/accounts/reconnect', {
      method: 'POST',
      body: { channel: filter },
    })
    return this.listAccounts(filter)
  },

  async toggleAccount(id: string): Promise<void> {
    await req('/v1/admin/accounts/toggle', { method: 'POST', body: { id } })
  },

  async deleteAccount(id: string): Promise<void> {
    await req('/v1/admin/accounts/delete', { method: 'POST', body: { id } })
  },

  /**
   * 添加账号：需要交互式登录（扫码/OIDC），无法由 HTTP 接口静默完成。
   * 这里拉起网关侧的登录脚本，由用户在浏览器完成，随后列表会刷新出来。
   */
  async addAccount(channel: Channel): Promise<Account> {
    const script =
      channel === 'Trae' ? 'login_trae.py'
        : channel === 'WorkBuddy' ? 'login_workbuddy.py'
          : 'login_workbuddy_intl.py'
    await req('/v1/admin/accounts/login', {
      method: 'POST',
      body: { channel, script },
      timeoutMs: 30000,
    })
    // 登录是异步的：返回占位对象，界面会提示「已拉起登录窗口」
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

  /* ═══════════════ API 密钥 ═══════════════ */

  async listApiKeys(): Promise<ApiKey[]> {
    const r = await req<{ apiKeys: (ApiKey & { legacy?: boolean })[] }>(
      '/v1/admin/api-keys'
    )
    return r.apiKeys
  },

  async createApiKey(): Promise<ApiKey> {
    const r = await req<{ name: string; key: string }>('/v1/admin/api-keys/create', {
      method: 'POST',
      body: {},
    })
    return { id: `key-${r.key.slice(-6)}`, name: r.name, key: r.key, createdAt: Date.now() / 1000 }
  },

  /** 注意：用 key 作为标识（密钥不可变且唯一） */
  async renameApiKey(id: string, name: string, key?: string): Promise<void> {
    await req('/v1/admin/api-keys/rename', {
      method: 'POST',
      body: { key: key ?? id, name },
    })
  },

  async deleteApiKey(id: string, key?: string): Promise<void> {
    await req('/v1/admin/api-keys/delete', {
      method: 'POST',
      body: { key: key ?? id },
    })
  },

  async getGateway(): Promise<GatewayInfo> {
    return req<GatewayInfo>('/v1/admin/gateway')
  },

  /* ═══════════════ 模型 ═══════════════ */

  async listModels(opts?: { channel?: ChannelFilter; showHidden?: boolean }): Promise<ModelEntry[]> {
    const r = await req<{ models: ModelEntry[] }>(
      `/v1/admin/models${q({ channel: opts?.channel ?? 'all' })}`
    )

    // 隐藏/置顶是「视图偏好」——后端模型目录由上游动态生成，无此字段，
    // 故叠加本地偏好（见 updateModels），保证刷新后仍生效
    const prefs = readModelPrefs()

    // 积分倍率需要联网（与模型目录分开的端点），失败则保持 0 并保留列表可用
    const rates = await req<{ rates: Record<string, Record<string, number>> }>(
      '/v1/admin/models/rates'
    ).catch(() => ({ rates: {} as Record<string, Record<string, number>> }))

    const merged = r.models.map((m) => {
      // 路由名去掉通道前缀即上游模型名，用它去倍率表里查
      const bare = m.routeModelId.replace(/^(tr-|wb-|wbie-)/, '')
      const table = rates.rates[m.channel] ?? {}
      const rate = table[m.routeModelId] ?? table[bare] ?? table[m.name] ?? 0
      return { ...m, ratio: rate || m.ratio || 0, ...(prefs[m.id] ?? {}) }
    })
    const list = opts?.showHidden ? merged : merged.filter((m) => !m.hidden)
    return list.slice().sort((a, b) => Number(b.pinned) - Number(a.pinned))
  },

  async refreshModels(): Promise<void> {
    await req('/v1/admin/models/refresh', { method: 'POST', body: {}, timeoutMs: 60000 })
  },

  /** 隐藏/置顶：写入本地视图偏好（后端无对应持久化字段） */
  async updateModels(ids: string[], patch: Partial<ModelEntry>): Promise<void> {
    const prefs = readModelPrefs()
    for (const id of ids) prefs[id] = { ...(prefs[id] ?? {}), ...patch }
    localStorage.setItem('open-ai.model-prefs', JSON.stringify(prefs))
  },

  /* ═══════════════ 积分 ═══════════════ */

  async getToday(channel: ChannelFilter = 'all'): Promise<TodayBundle> {
    const r = await req<{
      stats: { gained: number; used: number }
      rows: UsageRow[]
      updatedAt: number
    }>(`/v1/admin/credits/today${q({ channel })}`)
    return { stats: r.stats, rows: r.rows, updatedAt: r.updatedAt }
  },

  async getWeek(offset = 0, channel: ChannelFilter = 'all'): Promise<WeekBundle> {
    const r = await req<{
      stats: { gained: number; used: number }
      daily: DailyUsage[]
      startDay: string
      endDay: string
      offset: number
      updatedAt: number
    }>(`/v1/admin/credits/week${q({ offset, channel })}`)
    return r
  },

  /* ═══════════════ 日志 ═══════════════ */

  async listLogs(): Promise<LogLine[]> {
    const r = await req<{ logs: LogLine[] }>('/v1/admin/logs?lines=300')
    return r.logs
  },

  /** 真实日志来自网关文件，前端不再本地追加 */
  appendLog(_level: LogLevel, _text: string): LogLine {
    return { id: Date.now(), level: _level, text: _text, ts: Date.now() / 1000 }
  },

  /* ═══════════════ 设置 ═══════════════ */

  async getAutostart(): Promise<boolean> {
    const r = await req<{ enabled: boolean }>('/v1/admin/settings/autostart')
    return r.enabled
  },

  async setAutostart(enabled: boolean): Promise<void> {
    await req('/v1/admin/settings/autostart', { method: 'POST', body: { enabled } })
  },

  async getVersion(): Promise<{ current: string; latest?: string }> {
    return req<{ current: string }>('/v1/admin/version')
  },

  async checkUpdate(): Promise<{ hasUpdate: boolean; latest: string }> {
    const r = await req<{ current: string; latest?: string; hasUpdate?: boolean }>(
      '/v1/admin/version/check',
      { method: 'POST', body: {}, timeoutMs: 30000 }
    )
    return { hasUpdate: !!r.hasUpdate, latest: r.latest ?? r.current }
  },
}

export type HttpBackend = typeof httpBackend
