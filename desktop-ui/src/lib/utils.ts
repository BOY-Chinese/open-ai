import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Tailwind 类名合并（shadcn/ui 约定） */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** 千分位 + 指定小数位 */
export function fmtNum(n: number, digits = 2): string {
  if (!Number.isFinite(n)) return '0'
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 整数千分位 */
export function fmtInt(n: number): string {
  return fmtNum(n, 0)
}

/** 累积积分展示：整数则不补小数 */
export function fmtCredit(n: number): string {
  return Number.isInteger(n) ? fmtInt(n) : fmtNum(n, 2)
}

/** 掩码：保留首尾便于辨识，中间固定长度圆点 */
export function maskKey(key: string): string {
  if (!key) return ''
  if (key.length <= 12) return '•'.repeat(12)
  return `${key.slice(0, 6)}${'•'.repeat(16)}${key.slice(-4)}`
}

/**
 * 版本号统一展示格式 —— 唯一的版本号格式化入口。
 *
 * 后端 `version.py` 的 APP_VERSION 形如 `dev-v3.1`（通道前缀 + 版本），
 * 历史上还有 `3.1.0` / `v3.1` 两种写法。规则：
 *   - 空值 → 空串（由调用方决定显示「未知」还是骨架屏）
 *   - 已带 `v`，或形如 `<通道>-v<数字>`（dev-v3.1 / portable-v3.0）→ 原样
 *   - 其余（'3.1.0'）→ 补上 `v` 前缀
 *
 * ★ 不能简单地 `'v' + raw`：那会把 `dev-v3.1` 显示成 `vdev-v3.1`。
 *   版本号显示错误正是从这种「无脑补前缀」来的。
 */
export function formatVersion(raw: string): string {
  const s = raw.trim()
  if (!s) return ''
  if (/^v\d/i.test(s)) return s
  if (/^[a-z][a-z0-9]*[-_]v?\d/i.test(s)) return s
  return `v${s}`
}

/** 时间戳(秒) → '2026/09/12 00:56'
 *
 * 秒级时间戳为 0 或非法时返回「—」：表示**未知**，而不是伪造一个时间。
 * 曾经后端把 createdAt 硬编码成 0，界面于是整列显示 1970/01/01 08:00 ——
 * 那是缺数据，不是数据。老配置里没有 createdAt 的密钥就属于这种情况。
 */
export function fmtTime(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return '—'
  const d = new Date(sec * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}`
}

/** Date → 'YYYY-MM-DD'（本地时区，避免 toISOString 的 UTC 偏移） */
export function toDayKey(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 'MM/DD' 短标签 */
export function shortDay(dayKey: string): string {
  const [, m, d] = dayKey.split('-')
  return `${m}/${d}`
}

/** 递归清理对象中的 undefined（Tauri invoke 参数要求） */
export function pruneUndefined<T extends object>(obj: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(obj).filter(([, v]) => v !== undefined)
  ) as Partial<T>
}
