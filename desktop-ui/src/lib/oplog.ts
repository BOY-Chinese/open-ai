/**
 * 操作日志（对应 v2.3 GUI 的「操作日志」页）
 *
 * 与「系统日志」的区别（这是本模块存在的理由）
 * ------------------------------------------
 * v2.x 的桌面端有一页叫「系统日志」，读的是 `logs/gateway_out.log` 的尾巴 ——
 * 那是**网关自己**在说什么（上游请求、路由、异常）。而 v2.3 的「操作日志」
 * 记的是**用户在界面上做了什么**：点了刷新、加了账号、改了密钥、重连了通道，
 * 以及每次操作的结果与耗时。
 *
 * 两者服务的问题完全不同：
 *   * 网关日志回答「网关为什么报错」—— 面向排障；
 *   * 操作日志回答「我刚才那一下到底成没成」—— 面向使用。
 * 后者才是用户在界面上真正会问的问题（「我点了添加账号，然后呢？」），
 * 所以这一页改回操作日志。
 *
 * 记账格式沿用 v2.3 的习惯（`gui_account_manager._run` / `_write_log`）：
 *   ────────────────────────────────────────────────────────────
 *   [账号] 重新连接 · 通道=Trae
 *   [登录] 已拉起 login_trae.py，请在浏览器完成登录
 *   [完成] 22:50:24（耗时 3.2s）
 * 操作之间用 60 个 `─` 分隔，失败时打 `[错误] 类型: 消息`。
 *
 * 持久化
 * ------
 * 写 localStorage（上限 {@link MAX_LINES} 行）。v2.3 只存在内存里，关掉 GUI
 * 就没了；但「我十分钟前那次添加账号报了错」恰恰是重启后才想起来要看的，
 * 所以这里落盘。代价是 localStorage 有配额，故超限时从**头部**丢弃旧行 ——
 * 丢旧的保留新的，符合日志的阅读直觉。
 */
import type { LogLevel } from '@/types/domain'

export interface OpLogLine {
  id: number
  level: LogLevel
  text: string
  ts: number
}

const STORAGE_KEY = 'open-ai.oplog.v1'

/** 行数上限：约 2000 行 × 100 字符 ≈ 200KB，远低于 localStorage 常见 5MB 配额 */
export const MAX_LINES = 2000

/** 操作之间的分隔线（与 v2.3 `result_q.put('─' * 60)` 一致） */
export const OP_SEPARATOR = '─'.repeat(60)

/* ═══════════════ 状态 ═══════════════ */

/** 单条记录的最小形状校验：localStorage 可能被旧版本/手改写脏 */
function isLine(v: unknown): v is OpLogLine {
  if (!v || typeof v !== 'object') return false
  const l = v as Record<string, unknown>
  return (
    typeof l.id === 'number' &&
    typeof l.text === 'string' &&
    typeof l.ts === 'number' &&
    (l.level === 'info' || l.level === 'success' || l.level === 'warn' || l.level === 'error')
  )
}

function load(): OpLogLine[] {
  if (typeof localStorage === 'undefined') return []
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isLine).slice(-MAX_LINES)
  } catch {
    return []
  }
}

/**
 * 当前全部日志行。
 *
 * ★ 引用稳定性是硬要求：`useSyncExternalStore` 靠 `Object.is` 判断是否重渲染，
 *   每次返回新数组会导致无限渲染。故只在真正变更时替换这个引用。
 */
let lines: OpLogLine[] = load()

let nextId = lines.reduce((max, l) => Math.max(max, l.id), 0) + 1

const listeners = new Set<() => void>()

function commit() {
  // 先裁剪再落盘：localStorage 的写入量由 MAX_LINES 封顶
  if (lines.length > MAX_LINES) lines = lines.slice(-MAX_LINES)
  lines = lines.slice() // 换引用 → 通知订阅者
  persist()
  listeners.forEach((fn) => fn())
}

function persist() {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(lines))
  } catch {
    /* 配额满 / 隐私模式：内存里的日志仍可用，只是不跨会话保留 */
  }
}

/* ═══════════════ 对外 API ═══════════════ */

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** 供 useSyncExternalStore 读取的快照（引用稳定） */
export function getSnapshot(): OpLogLine[] {
  return lines
}

/** 追加若干行（level 缺省按内容推断，与网关日志的着色习惯一致） */
export function append(level: LogLevel, text: string): void {
  const ts = Date.now() / 1000
  const added = text
    .split('\n')
    .map((t) => t.trimEnd())
    .filter((t) => t.length > 0)
    .map((t) => ({ id: nextId++, level, text: t, ts }))
  if (added.length === 0) return
  lines = lines.concat(added)
  commit()
}

/** 清空（同时清掉持久化，否则刷新页面又回来了） */
export function clear(): void {
  if (lines.length === 0) return
  lines = []
  commit()
}

/** `HH:MM:SS` —— 与 v2.3 `[完成] 22:50:24` 的时间格式一致 */
function hhmmss(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 耗时人类可读：<1s 用毫秒，否则秒（保留一位小数） */
function duration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`
}

/* ═══════════════ 操作包装（对应 v2.3 的 _run） ═══════════════ */

/**
 * 执行一次「操作」，按 v2.3 的格式记账。
 *
 * 用 try/finally 保证 `[完成]` 一定落笔：v2.3 的 `_thread_wrap` 也是这么做的
 * （无论成功失败都补一条完成时间），否则日志里会出现「一个操作开始了但没有下文」，
 * 读者无法判断是还在跑、还是崩了。
 */
export async function runOperation<T>(title: string, fn: () => Promise<T>): Promise<T> {
  const startedAt = Date.now()
  append('info', OP_SEPARATOR)
  append('info', title)
  try {
    const result = await fn()
    append('success', `[完成] ${hhmmss()}（耗时 ${duration(Date.now() - startedAt)}）`)
    return result
  } catch (e) {
    const name = e instanceof Error ? e.name : 'Error'
    const msg = e instanceof Error ? e.message : String(e)
    append('error', `[错误] ${name}: ${msg}`)
    append('info', `[完成] ${hhmmss()}（耗时 ${duration(Date.now() - startedAt)}）`)
    throw e
  }
}
