import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { ThemeMode } from '@/types/domain'
import {
  applyThemeClass,
  readThemeMode,
  resolveTheme,
  systemPrefersDark,
  watchSystemTheme,
  writeThemeMode,
  type ResolvedTheme,
} from '@/lib/theme'

interface ThemeContextValue {
  /** 用户选择的模式（light / dark / system） */
  mode: ThemeMode
  /** 实际生效的主题（system 已被解析） */
  resolved: ResolvedTheme
  /** 切换模式：立即生效 + 持久化 */
  setMode: (mode: ThemeMode) => void
  /** 系统当前是否深色 —— 「跟随系统」选项的说明文案要用它 */
  systemDark: boolean
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

/**
 * 主题 Provider —— 挂在应用最外层（见 `main.tsx`）。
 *
 * 首帧的 `dark` class 已由 `index.html` 的内联脚本写好，这里的初始 state
 * 只是「把当前真实状态读进来」，不会再触发一次切换，因此不会闪。
 *
 * `system` 模式下的实时跟随：监听 matchMedia('change')。注意**只在 system
 * 模式下**订阅 —— 用户显式选了白天/夜间时，不该被系统主题变化打断
 * （那会表现为「我明明选了夜间，插上外接显示器自己变白了」）。
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readThemeMode)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)
  const resolved = resolveTheme(mode, systemDark)

  // 模式或系统偏好变化 → 落到 <html class>
  useEffect(() => {
    applyThemeClass(resolved)
  }, [resolved])

  // 仅在「跟随系统」时订阅系统变化
  useEffect(() => {
    if (mode !== 'system') return
    return watchSystemTheme(setSystemDark)
  }, [mode])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    writeThemeMode(next)
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ mode, resolved, setMode, systemDark }),
    [mode, resolved, setMode, systemDark]
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

/**
 * 读取当前主题。
 *
 * 刻意**不提供**「无 Provider 时的兜底单例」：主题缺失时静默返回一个假值，
 * 会让「忘了包 Provider」表现为「切换主题没反应」，排查成本远高于直接报错。
 */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme 必须在 <ThemeProvider> 内使用')
  return ctx
}
