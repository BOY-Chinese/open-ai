import { useCallback, useEffect, useRef } from 'react'
import { Check, Monitor, Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/useTheme'
import { cn } from '@/lib/utils'
import type { ThemeMode } from '@/types/domain'

/** 三个选项的展示元信息；顺序即界面顺序（白天在最前，因为它是默认值） */
const OPTIONS: {
  mode: ThemeMode
  label: string
  desc: string
  icon: typeof Sun
}[] = [
  {
    mode: 'light',
    label: '白天',
    desc: '始终使用浅色配色（默认）',
    icon: Sun,
  },
  {
    mode: 'dark',
    label: '夜间',
    desc: '始终使用深色配色',
    icon: Moon,
  },
  {
    mode: 'system',
    label: '跟随系统',
    desc: '随 Windows 的浅色 / 深色设置自动切换',
    icon: Monitor,
  },
]

export const THEME_MODE_LABEL: Record<ThemeMode, string> = {
  light: '白天',
  dark: '夜间',
  system: '跟随系统',
}

/**
 * 外观设置对话框
 *
 * 交互取舍：
 *  - **点击即生效，没有「保存」按钮**。主题是所见即所得的东西，让用户选完再确认
 *    纯属多余；保留的「完成」只是关闭入口。（对比：卸载那种不可逆操作才需要二次确认。）
 *  - 用原生 `<input type="radio">` 而不是自绘按钮组：方向键切换、Tab 焦点、
 *    屏幕阅读器播报全部白送，自绘要重新实现一遍还容易漏。
 *  - 「跟随系统」的说明里实时带上系统当前深浅色，用户能立刻确认它读到了什么，
 *    否则「选了跟随系统但界面没变」时无从判断是系统就是浅色还是功能坏了。
 */
export function AppearanceDialog({ onClose }: { onClose: () => void }) {
  const { mode, resolved, setMode, systemDark } = useTheme()
  const dialogRef = useRef<HTMLDivElement>(null)

  /** Esc 关闭（与卸载确认框保持一致） */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  /** 打开即聚焦对话框，键盘用户不必先 Tab 一圈 */
  useEffect(() => {
    dialogRef.current?.focus()
  }, [])

  const pick = useCallback((next: ThemeMode) => setMode(next), [setMode])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="appearance-title"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="w-[460px] rounded-lg border border-border bg-bg-card p-6 shadow-popup outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="appearance-title" className="text-md font-semibold text-fg">
          外观设置
        </h2>
        <p className="mt-2 text-sm text-fg-subtle">
          选择界面配色模式，点击立即生效并记住选择。
        </p>

        <div className="mt-4 space-y-2" role="radiogroup" aria-labelledby="appearance-title">
          {OPTIONS.map((opt) => {
            const Icon = opt.icon
            const active = mode === opt.mode
            const id = `theme-mode-${opt.mode}`
            return (
              <div key={opt.mode} className="relative">
                {/* 原生 radio 承担全部无障碍语义；peer 让焦点环画在可见的 label 上 */}
                <input
                  id={id}
                  type="radio"
                  name="open-ai-theme-mode"
                  className="peer sr-only"
                  checked={active}
                  onChange={() => pick(opt.mode)}
                />
                <label
                  htmlFor={id}
                  className={cn(
                    'flex cursor-pointer items-start gap-3 rounded-lg border p-3',
                    'transition-colors duration-fast',
                    'peer-focus-visible:outline peer-focus-visible:outline-2',
                    'peer-focus-visible:outline-offset-1 peer-focus-visible:outline-primary',
                    active
                      ? 'border-primary/40 bg-primary/10'
                      : 'border-border hover:border-border-strong hover:bg-bg-card-hover'
                  )}
                >
                  <Icon
                    className={cn(
                      'mt-0.5 size-4 shrink-0',
                      active ? 'text-primary' : 'text-fg-subtle'
                    )}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span
                      className={cn(
                        'block text-md',
                        active ? 'font-medium text-fg' : 'text-fg-muted'
                      )}
                    >
                      {opt.label}
                      {opt.mode === 'system' && (
                        <span className="ml-2 text-sm font-normal text-fg-subtle">
                          当前系统：{systemDark ? '深色' : '浅色'}
                        </span>
                      )}
                    </span>
                    <span className="mt-0.5 block text-sm text-fg-subtle">{opt.desc}</span>
                  </span>
                  {active && (
                    <Check className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
                  )}
                </label>
              </div>
            )
          })}
        </div>

        {/* 生效结果回显：单选组只表达「选了什么」，这里回答「现在是什么」 */}
        <p className="mt-4 text-sm text-fg-subtle">
          当前生效：
          <span className="ml-1 text-fg">{resolved === 'dark' ? '夜间（深色）' : '白天（浅色）'}</span>
        </p>

        <div className="mt-6 flex items-center justify-end">
          <Button variant="default" size="default" onClick={onClose}>
            完成
          </Button>
        </div>
      </div>
    </div>
  )
}
