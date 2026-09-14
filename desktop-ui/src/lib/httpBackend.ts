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
import { authHeaders, gatewayRuntime, invalidateGatewayRuntime } from './gateway'
import { filterModelView } from './modelCache'
import { emitAccountsChanged } from './accountEvents'
import { followLoginLog, type LoginLogChunk } from './loginStream'
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

  // 首次尝试 + 401 后重读配置再试一次。
  // 为什么需要重试：密钥来自 config.json，用户可能一边开着界面一边改它
  // （实测：「我换了 config.json，点刷新没反应」——旧密钥失配 → 401 →
  //  刷新失败 → 列表保持旧数据，看起来就像刷新按钮坏了）。
  for (let attempt = 0; attempt < 2; attempt++) {
    const rt = attempt === 0 ? await gatewayRuntime() : await invalidateGatewayRuntime()
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
        // 管理面数据必须实时：WebView2 / 中间层不允许返回缓存副本，
        // 否则「点刷新拿到的还是上一次的列表」
        cache: 'no-store',
      })

      // 密钥失配：重读 config.json 后再试一次（配置改了但界面还开着）
      if ((res.status === 401 || res.status === 403) && attempt === 0) {
        continue
      }

      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const j = await res.json()
          const d = j?.detail
          detail = typeof d === 'string' ? d : d?.message ?? JSON.stringify(d ?? j)
        } catch {
          /* 保留 HTTP 状态文案 */
        }
        if (res.status === 401 || res.status === 403) {
          detail = headers.Authorization
            ? `${detail}（已按 config.json 重新读取密钥仍被拒绝，请确认网关已重启以加载新的 api_keys）`
            : `${detail}（当前从 config.json 读不到任何密钥；缺配置文件时重启网关会自动生成一条「默认密钥」）`
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
  throw new ApiError(401, '网关拒绝鉴权（已重试）')
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
  /**
   * 今日签到结果（只读本地流水库，毫秒级，不联网）。
   *
   * 返回体里**只包含签到成功的账号** —— 未出现即「今日未签到」，
   * 页面按缺席判定即可，不需要后端为每个账号补一条 false
   * （也让「真的没签到」和「接口没返回这个账号」走同一条渲染分支）。
   */
  async listSignin(filter: ChannelFilter = 'all'): Promise<SigninBundle> {
    const r = await req<{ signin: Record<string, SigninStatus>; day: string }>(
      `/v1/admin/accounts/signin${q({ channel: filter })}`
    )
    return { day: r.day ?? '', signin: r.signin ?? {} }
  },

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
   * 这里拉起网关侧的登录脚本，由用户在浏览器完成。
   *
   * ★ 本方法**要等到登录脚本结束才返回**，脚本输出全程实时写进「操作日志」。
   *   这正是 v2.3 GUI 的形状：一次「添加账号」在日志里是**一个**操作块 ——
   *   分隔线 → 提示 → 脚本输出 → `[完成]`。若在这里 fire-and-forget 立刻返回，
   *   代理写下的 `[完成]` 会插在脚本输出**前面**，读起来变成
   *   「操作已完成，然后（莫名其妙地）又有一堆输出」。
   *
   *   调用方（账号列表页）因此**不应 await 本方法** —— 用户可能在浏览器里
   *   填两分钟密码，界面没理由锁在那儿；结束后的刷新走 `accountEvents` 通知。
   *   （v2.3 把整个流程放在 GUI 线程里，界面就是被卡住的。）
   */
  async addAccount(channel: Channel): Promise<Account> {
    // Loomy 走讯飞手机号短信验证码登录（loomy.xunfei.cn Web 端），非浏览器交互。
    // 正常路径已被界面拦截（「添加 Loomy 账号」弹出图形化向导），这里只是
    // 防御性兜底 —— 万一有调用方绕过向导，给出明确指引而不是悄悄失败。
    if (channel === 'Loomy') {
      throw new ApiError(
        501,
        'Loomy 请使用「添加 Loomy 账号」弹窗登录（手机号验证码），无需命令行'
      )
    }
    const script =
      channel === 'Trae' ? 'login_trae.py'
        : channel === 'WorkBuddy' ? 'login_workbuddy.py'
          : 'login_workbuddy_intl.py'
    const r = await req<{ logOffset?: number }>('/v1/admin/accounts/login', {
      method: 'POST',
      body: { channel, script },
      timeoutMs: 30000,
    })

    await followLoginLog({
      fetchChunk: (offset) => this.loginLog(channel, offset),
      startOffset: r.logOffset ?? 0,
      label: `${channel} 登录助手（${script}）`,
      onFinished: () => emitAccountsChanged(channel),
    })

    // 登录是异步的：返回占位对象，界面据此提示「已拉起登录助手」
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

  /** 增量读取登录脚本输出（只读文件，不联网；见后端 /accounts/login/log） */
  async loginLog(channel: Channel, offset: number): Promise<LoginLogChunk> {
    return req<LoginLogChunk>(`/v1/admin/accounts/login/log${q({ channel, offset })}`, {
      timeoutMs: 10000,
    })
  },

  /* ═══════════════ API 密钥 ═══════════════ */

  async listApiKeys(): Promise<ApiKey[]> {
    const r = await req<{ apiKeys: ApiKey[] }>('/v1/admin/api-keys')
    return r.apiKeys
  },

  async createApiKey(): Promise<ApiKey> {
    const r = await req<{ name: string; key: string; createdAt: number }>(
      '/v1/admin/api-keys/create',
      { method: 'POST', body: {} }
    )
    // createdAt 由后端持久化到 config.json（此前是硬编码 0，
    // 前端一格式化就显示成 1970/01/01 —— 用户实测反馈的 bug）
    return {
      id: `key-${r.key.slice(-6)}`,
      name: r.name,
      key: r.key,
      createdAt: r.createdAt ?? 0,
    }
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

  /**
   * 拉取模型列表（全量）+ 积分倍率。
   *
   * 两点与「按 channel 请求」不同的取舍：
   *  1. **始终请求全量**（不带 channel）。后端这一步是内存读取（毫秒级），
   *     全量与单通道几乎同价；换来的是调用方手里永远有一份完整列表，
   *     切换通道/显隐筛选可以纯本地完成，不再各拉一次。
   *  2. **筛选交给 {@link filterModelView}**，与页面读取缓存走同一段代码，
   *     保证「缓存首帧」与「刷新后」结果一致。
   *
   * 注意：本函数**不写缓存** —— 缓存由模型列表页在拿到成功结果后统一落盘
   * （见 `lib/modelCache.ts`），这样「网络结果」与「本地乐观修改」两条路径
   * 只有一个写入点，不会互相覆盖。
   */
  async listModels(opts?: { channel?: ChannelFilter; showHidden?: boolean }): Promise<ModelEntry[]> {
    const r = await req<{ models: ModelEntry[] }>('/v1/admin/models')

    // 隐藏/置顶是「视图偏好」——后端模型目录由上游动态生成，无此字段，
    // 故叠加本地偏好（见 updateModels），保证刷新后仍生效
    const prefs = readModelPrefs()

    // 积分倍率需要联网（与模型目录分开的端点），失败则保持 0 并保留列表可用
    const rates = await req<{ rates: Record<string, Record<string, number>> }>(
      '/v1/admin/models/rates'
    ).catch(() => ({ rates: {} as Record<string, Record<string, number>> }))

    const merged = r.models.map((m) => {
      // 路由名去掉通道前缀即上游模型名，用它去倍率表里查
      const bare = m.routeModelId.replace(/^(tr-|wb-|wbie-|lm-|loomy-)/, '')
      const table = rates.rates[m.channel] ?? {}
      const rate = table[m.routeModelId] ?? table[bare] ?? table[m.name] ?? 0
      return { ...m, ratio: rate || m.ratio || 0, ...(prefs[m.id] ?? {}) }
    })
    const all = merged.slice().sort((a, b) => Number(b.pinned) - Number(a.pinned))
    return filterModelView(all, opts?.channel ?? 'all', opts?.showHidden ?? false)
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

  /* ═══════════════ Auto 路由连 ═══════════════ */

  async getAutoChain(): Promise<{ chain: AutoChain; availableModels: string[] }> {
    return req<{ chain: AutoChain; availableModels: string[] }>('/v1/admin/auto-chain')
  },

  async saveAutoChain(chain: AutoChain): Promise<void> {
    await req('/v1/admin/auto-chain', { method: 'POST', body: chain })
  },

  /* ═══════════════ Loomy 登录（图形化向导用） ═══════════════ */

  /**
   * 桌面通道（讯飞账号服务）发送短信验证码。
   *
   * 只有桌面通道的 session 能鉴权对话，积分记在本账号；Web 通道的 cookie
   * 只能查余额。因此界面默认走桌面通道。
   */
  async sendLoomyDesktopCode(
    phone: string
  ): Promise<{ ok: boolean; messageId?: string; error?: string }> {
    return req('/v1/admin/accounts/loomy/desktop-send-code', {
      method: 'POST',
      body: { phone },
    })
  },

  /** 桌面通道验证码登录：本账号 session 由网关写入 config.json */
  async loomyDesktopLogin(
    phone: string,
    code: string,
    messageId: string
  ): Promise<{ ok: boolean; userid?: string; name?: string; error?: string }> {
    return req('/v1/admin/accounts/loomy/desktop-login', {
      method: 'POST',
      body: { phone, code, messageId },
    })
  },

  /** 发送短信验证码（Web 通道，服务端校验手机号，{ok:false} 时带 error 文案） */
  async sendLoomyCode(
    phone: string
  ): Promise<{ ok: boolean; messageId?: string; error?: string }> {
    return req('/v1/admin/accounts/loomy/send-code', { method: 'POST', body: { phone } })
  },

  /** 验证码登录：cookie 会话由网关写入 config.json */
  async loomyWebLogin(
    phone: string,
    code: string,
    messageId: string
  ): Promise<{ ok: boolean; userid?: string; name?: string; error?: string }> {
    return req('/v1/admin/accounts/loomy/web-login', {
      method: 'POST',
      body: { phone, code, messageId },
    })
  },

  /** 校验某手机号的 Web 会话是否仍有效 */
  async checkLoomySession(phone: string): Promise<{ valid: boolean; nickname?: string }> {
    return req(`/v1/admin/accounts/loomy/session${q({ phone })}`)
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
