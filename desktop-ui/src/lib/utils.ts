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

/** 时间戳(秒) → '2026/09/12 00:56' */
export function fmtTime(sec: number): string {
  const d = new Date(sec * 1000)
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
