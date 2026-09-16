/**
 * 网关运行期配置 + 探活/自启 —— 桌面端与开发态的唯一入口
 *
 * 为什么需要这个模块（v3.0 虚拟机实测 bug 的根因）：
 *
 *   `src/lib/httpBackend.ts` 原先把所有请求发到「相对路径」`/v1/admin/*`，
 *   并依赖**开发态 Vite dev server 的 proxy** 完成两件事：
 *     ① 转发到 127.0.0.1:8000   ② 注入 `Authorization: Bearer <config.json 的 api_key>`
 *   打包后的桌面端里根本没有 dev server，相对路径会落到内嵌资源协议
 *   （`http://tauri.localhost/v1/...`）上 → 必然失败，界面上就是
 *   「无法连接网关」。而密钥当时也无从获取。
 *
 *   本模块把这两件事搬到运行期：
 *     - 打包态：经 Tauri 命令 `gateway_config` 读**同一份** config.json，
 *       拿到真实地址与密钥（前端产物里始终不含密钥明文）
 *     - 开发态：保持原样（相对路径 + Vite 代理），两种形态同一套页面代码
 *
 * 另外提供 `ensureGateway()`：双击快捷方式时若后端没在跑，自动拉起后轮询探活，
 * 让「打开界面」这一个动作就能把整套服务带起来。
 *
 * ★ 2026-09-16 修复：**密钥轮换后界面永久卡在启动门**（用户报「拿不到后端数据」）
 *
 *   成因链（本机实测，日志与截图可复现）：
 *     ① config.json 的 api_keys 被清空（用户在 API 管理页删完 / 配置被覆盖成空壳）；
 *     ② 桌面端在此时启动，`gateway_config` 读到空密钥 → 本模块把「空密钥」
 *        **缓存在进程内**（见 `pending`）；
 *     ③ 网关随后重启，`api_store.ensure_api_keys()` 补了一条**新**密钥（另一把）；
 *     ④ 启动门只探活、不做任何重读 —— 缓存里的空/旧密钥一直 401，
 *        而页面在启动门后面**根本没挂载**，`httpBackend.req()` 那条
 *        「401 就重读配置」的自愈路径永远不会被执行 → 死锁到重启程序为止。
 *   更糟的是 `ensureGateway()` 把 401 当成「后端没起来」，去拉起后端并轮询
 *   45 秒，最后给出「已请求启动后端，但 45 秒内网关仍未就绪」—— 网关明明健康，
 *   结论却是错的，用户按提示怎么修都修不好。
 *
 *   修法：探活遇到 401/403 一律「重读 config.json 再试一次」，并把
 *   `authRejected` 明确回传给调用方 —— 鉴权被拒**绝不**触发自动拉起后端。
 */
import { invoke } from '@tauri-apps/api/core'

/** 当前是否运行在 Tauri 桌面端（而非浏览器 / Vite dev server） */
export const inTauri =
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

export interface GatewayRuntime {
  /** 网关基址；空串表示「同源相对路径」（开发态走 Vite 代理） */
  baseUrl: string
  /** 网关密钥；空串表示「由代理注入」（开发态） */
  apiKey: string
  /** open-ai 安装根目录（诊断用，出错面板展示） */
  root: string
  /** 是否具备一键拉起后端的能力 */
  canStart: boolean
  inTauri: boolean
}

/** Rust 命令 `gateway_config` 的回传形状 */
interface RawGatewayConfig {
  baseUrl?: string
  apiKey?: string
  root?: string
  canStart?: boolean
}

const stripSlash = (s: string | undefined) => (s ?? '').replace(/\/+$/, '')

let pending: Promise<GatewayRuntime> | null = null

/** 读取网关运行期配置（进程内缓存，只解析一次） */
export function gatewayRuntime(): Promise<GatewayRuntime> {
  if (!pending) pending = loadRuntime()
  return pending
}

/**
 * 丢弃缓存的网关配置并重新读取。
 *
 * 为什么需要：密钥/端口都来自 `config.json`，而用户可能在界面开着的时候
 * 直接改这个文件（换了 api_keys、换了端口）。此时界面缓存的旧密钥会立刻失配，
 * 表现为「点刷新没反应、列表还是旧的」。请求层遇到 401 时调用本函数重读配置，
 * 再重试一次，就能自动跟上配置变更，不需要重启桌面端。
 */
export function invalidateGatewayRuntime(): Promise<GatewayRuntime> {
  pending = null
  return gatewayRuntime()
}

async function loadRuntime(): Promise<GatewayRuntime> {
  const devBase = stripSlash(import.meta.env.VITE_GATEWAY_BASE as string | undefined)

  if (!inTauri) {
    // 开发态 / 浏览器预览：同源相对路径，由 Vite dev server 代理并注入密钥
    return { baseUrl: devBase, apiKey: '', root: '', canStart: false, inTauri: false }
  }

  try {
    const c = await invoke<RawGatewayConfig>('gateway_config')
    return {
      baseUrl: stripSlash(c.baseUrl) || 'http://127.0.0.1:8000',
      apiKey: c.apiKey ?? '',
      root: c.root ?? '',
      canStart: Boolean(c.canStart),
      inTauri: true,
    }
  } catch (e) {
    // 拿不到配置也不能让界面白屏：回落默认地址，错误交给请求层暴露
    console.warn('[open-ai] 读取网关配置失败，回落默认地址', e)
    return {
      baseUrl: 'http://127.0.0.1:8000',
      apiKey: '',
      root: '',
      canStart: false,
      inTauri: true,
    }
  }
}

/** 鉴权头：打包态带 Bearer；开发态返回 undefined 交给代理注入 */
export function authHeaders(rt: GatewayRuntime): Record<string, string> | undefined {
  return rt.apiKey ? { Authorization: `Bearer ${rt.apiKey}` } : undefined
}

/* ═══════════════ 探活 / 自动拉起 ═══════════════ */

export type GatewayPhase = 'checking' | 'starting' | 'online' | 'offline'

export interface GatewayProbe {
  online: boolean
  reason?: string
  /**
   * 网关**在运行**，只是拒绝了这次鉴权（HTTP 401/403）。
   *
   * 必须与「连不上 / 没起来」分开：对前者去拉起后端毫无意义（实测白等 45 秒，
   * 还把用户引向「后端起不来」的错误结论），而后者才需要自动启动。
   */
  authRejected?: boolean
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/**
 * 单次探活（用**给定的**运行期配置，不读也不改缓存）。
 *
 * 用 `/v1/admin/health`：网关侧无需触达上游、毫秒级返回，
 * 是判断「网关进程是否就绪」最轻的端点。
 */
async function probeWith(rt: GatewayRuntime, timeoutMs: number): Promise<GatewayProbe> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(`${rt.baseUrl}/v1/admin/health`, {
      headers: authHeaders(rt),
      signal: ctrl.signal,
      // 与 httpBackend 一致：WebView2 不允许把「网关活着/死了」的结论缓存下来
      cache: 'no-store',
    })
    if (res.ok) return { online: true }
    if (res.status === 401 || res.status === 403) {
      return {
        online: false,
        authRejected: true,
        reason: rt.apiKey
          ? `网关拒绝鉴权（HTTP ${res.status}）：config.json 的 api_key 与网关当前使用的不一致`
          : `网关拒绝鉴权（HTTP ${res.status}）：当前从 config.json 读不到任何密钥`,
      }
    }
    return { online: false, reason: `网关返回 HTTP ${res.status}` }
  } catch (e) {
    const err = e as Error
    return {
      online: false,
      reason:
        err.name === 'AbortError'
          ? '网关无响应（探活超时）'
          : `无法连接网关：${err.message}`,
    }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * 探活，并在被拒鉴权时**重读 config.json** 再试一次。
 *
 * 为什么重读是必需的：密钥只在 config.json 里，而网关重启时会重建密钥
 * （api_keys 空 → `ensure_api_keys()` 补一条新的）。缓存在进程内的旧密钥
 * 会一直 401，而界面在启动门后面根本不挂载页面 —— 请求层那条 401 自愈路径
 * 永远跑不到。少了这一步，用户只能重启桌面端（实测就是这条 bug）。
 */
export async function probeGateway(timeoutMs = 2500): Promise<GatewayProbe> {
  const first = await probeWith(await gatewayRuntime(), timeoutMs)
  if (!first.authRejected) return first

  const fresh = await invalidateGatewayRuntime()
  const retry = await probeWith(fresh, timeoutMs)
  if (retry.online) return retry
  if (!retry.authRejected) return retry

  return {
    ...retry,
    reason: `${retry.reason}（已按 config.json 重新读取密钥仍被拒绝：请确认网关已重启以加载新的 api_keys）`,
  }
}

export interface EnsureOptions {
  /** 阶段回调，用于界面展示「正在连接 / 正在启动」 */
  onPhase?: (phase: GatewayPhase) => void
  /** 请求拉起后端后，等待网关就绪的最长时间（毫秒） */
  waitMs?: number
}

/**
 * 确保网关可用：探活 → 不通则拉起后端 → 轮询直到就绪。
 *
 * 幂等：后端 Broker 自带单实例锁，网关已在运行时首次探活即返回，
 * 不会重复拉起进程。
 *
 * ★ 鉴权被拒（401/403）是**例外分支**：网关是活的，缺的是密钥匹配。
 *   此处必须原样上报，绝不能走「拉起后端 + 轮询 45 秒」——
 *   实测那条路会把结论写成「已请求启动后端，但 45 秒内网关仍未就绪」，
 *   让用户去修一个根本没坏的东西（本机 2026-09-16 的「拿不到后端数据」）。
 */
export async function ensureGateway(opts: EnsureOptions = {}): Promise<GatewayProbe> {
  const { onPhase, waitMs = 45000 } = opts
  onPhase?.('checking')

  const first = await probeGateway(2500)
  if (first.online) {
    onPhase?.('online')
    return first
  }
  if (first.authRejected) {
    // 探活内部已重读过一次 config.json，这里就是对用户最诚实的结论
    onPhase?.('offline')
    return first
  }

  const rt = await gatewayRuntime()
  if (!rt.canStart) {
    // 无法代为拉起后端。这个分支有两种完全不同的成因，**必须分开报**：
    // 静默返回会让界面只剩一句「无法连接网关」，把「装不完整 / 找不到安装根」
    // 这种一手问题伪装成网络问题 —— 虚拟机上就是这么白排查了一轮。
    onPhase?.('offline')
    if (!rt.inTauri) {
      return { ...first, reason: '浏览器预览模式不代为启动后端，请手动运行网关。' }
    }
    if (!rt.root) {
      return {
        ...first,
        reason:
          '未找到 open-ai 安装目录（桌面端需与品牌 exe 同级，或位于其 desktop\\ 子目录下）。' +
          '请确认安装包已完整安装到目录，而不是单独拷贝了桌面端 exe。',
      }
    }
    return {
      ...first,
      reason: `已找到安装目录 ${rt.root}，但缺少可拉起的后端入口（open-ai.exe / open-ai-daemon.exe）。`,
    }
  }

  onPhase?.('starting')
  try {
    await invoke('start_backend')
  } catch (e) {
    onPhase?.('offline')
    return { online: false, reason: `自动启动后端失败：${e}` }
  }

  const deadline = Date.now() + waitMs
  while (Date.now() < deadline) {
    await sleep(1200)
    // 轮询期间也走带重读的 probeGateway：后端若因「配置缺密钥」而被重启，
    // 网关会**新生成**一条密钥（见 api_store.ensure_api_keys），
    // 只有重读 config.json 才能跟上这次轮换。
    if ((await probeGateway(2000)).online) {
      onPhase?.('online')
      return { online: true }
    }
  }

  onPhase?.('offline')
  return {
    online: false,
    reason: `已请求启动后端，但 ${Math.round(waitMs / 1000)} 秒内网关仍未就绪`,
  }
}
