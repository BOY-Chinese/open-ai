/**
 * 主题（外观）设置 —— 纯逻辑层，不依赖 React
 *
 * 三种模式：
 *   light   白天（浅色）
 *   dark    夜间（深色）
 *   system  跟随系统（读 `prefers-color-scheme`，系统切换时实时跟随）
 *
 * **默认 mode 为 `light`（白天）** —— 需求明确指定的默认值。
 * 注意不要写成「跟随系统」：那会在深色系统的机器上首次打开就是深色，
 * 与「默认模式为白天模式」相悖。
 *
 * 生效方式：给 `<html>` 加/去 `dark` class（Tailwind `darkMode: ['class']`）。
 * 令牌定义见 `styles/globals.css`。
 *
 * 防白闪：真正的首帧生效发生在 `index.html` 的内联脚本里 —— 这段逻辑
 * 若等 React 挂载后才跑，用户会先看到一帧默认主题再跳到目标主题。
 * 内联脚本与本文件的规则必须保持一致（键名、class 名、system 判定），
 * 二者是同一份约定的两个副本，改一处务必改另一处。
 */
import type { ThemeMode } from '@/types/domain'

export const THEME_STORAGE_KEY = 'open-ai.theme'

/** 需求指定：默认白天模式 */
export const DEFAULT_THEME_MODE: ThemeMode = 'light'

/** 实际生效的两套主题（`system` 会被解析成其中之一） */
export type ResolvedTheme = 'light' | 'dark'

/** 系统当前是否偏好深色 */
export function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** 模式 → 实际生效主题（`system` 走系统偏好） */
export function resolveTheme(mode: ThemeMode, systemDark = systemPrefersDark()): ResolvedTheme {
  if (mode === 'light') return 'light'
  if (mode === 'dark') return 'dark'
  return systemDark ? 'dark' : 'light'
}

/** 读取用户选择；缺失/损坏一律回落到默认（白天） */
export function readThemeMode(): ThemeMode {
  if (typeof localStorage === 'undefined') return DEFAULT_THEME_MODE
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY)
    return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : DEFAULT_THEME_MODE
  } catch {
    return DEFAULT_THEME_MODE
  }
}

/** 持久化用户选择（写失败静默：主题是偏好，写不进去不该影响本次使用） */
export function writeThemeMode(mode: ThemeMode): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    /* 隐私模式 / 配额满：忽略 */
  }
}

/** 把解析后的主题写到 `<html class>` 上 */
export function applyThemeClass(resolved: ResolvedTheme): void {
  if (typeof document === 'undefined') return
  document.documentElement.classList.toggle('dark', resolved === 'dark')
}

/** 订阅系统深浅色变化；返回取消订阅函数 */
export function watchSystemTheme(onChange: (systemDark: boolean) => void): () => void {
  if (typeof window === 'undefined' || !window.matchMedia) return () => {}
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const handler = (e: MediaQueryListEvent) => onChange(e.matches)
  mq.addEventListener('change', handler)
  return () => mq.removeEventListener('change', handler)
}
