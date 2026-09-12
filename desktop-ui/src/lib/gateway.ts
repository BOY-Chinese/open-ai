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
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/**
 * 单次探活。
 *
 * 用 `/v1/admin/health`：网关侧无需触达上游、毫秒级返回，
 * 是判断「网关进程是否就绪」最轻的端点。
 */
export async function probeGateway(timeoutMs = 2500): Promise<GatewayProbe> {
  const rt = await gatewayRuntime()
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(`${rt.baseUrl}/v1/admin/health`, {
      headers: authHeaders(rt),
      signal: ctrl.signal,
    })
    if (res.ok) return { online: true }
    if (res.status === 401 || res.status === 403) {
      return {
        online: false,
        reason: `网关拒绝鉴权（HTTP ${res.status}）：config.json 的 api_key 与网关当前使用的不一致`,
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
 */
export async function ensureGateway(opts: EnsureOptions = {}): Promise<GatewayProbe> {
  const { onPhase, waitMs = 45000 } = opts
  onPhase?.('checking')

  const first = await probeGateway(2500)
  if (first.online) {
    onPhase?.('online')
    return first
  }

  const rt = await gatewayRuntime()
  if (!rt.canStart) {
    // 浏览器开发态 / 找不到安装根：无法代劳，交由界面提示手工启动
    onPhase?.('offline')
    return first
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
