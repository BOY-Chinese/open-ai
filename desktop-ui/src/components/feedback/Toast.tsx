import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { CheckCircle2, AlertTriangle, XCircle, Info } from 'lucide-react'
import { cn } from '@/lib/utils'

/** 轻量 Toast — 替代 tkinter 版自绘 Toast，承载操作反馈 */

type ToastKind = 'success' | 'error' | 'warn' | 'info'

interface ToastItem {
  id: number
  kind: ToastKind
  text: string
  /** 0 表示不自动消失 */
  duration: number
}

interface ToastCtx {
  toast: (text: string, kind?: ToastKind, duration?: number) => void
}

const Ctx = createContext<ToastCtx>({ toast: () => {} })

export function useToast() {
  return useContext(Ctx)
}

const META: Record<ToastKind, { icon: typeof Info; cls: string }> = {
  success: { icon: CheckCircle2, cls: 'border-success/40 text-success' },
  error: { icon: XCircle, cls: 'border-danger/40 text-danger' },
  warn: { icon: AlertTriangle, cls: 'border-warning/40 text-warning' },
  info: { icon: Info, cls: 'border-border-strong text-fg-muted' },
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const seq = useRef(0)
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
  }, [])

  const toast = useCallback(
    (text: string, kind: ToastKind = 'info', duration = 2600) => {
      const id = ++seq.current
      setItems((prev) => [...prev.slice(-3), { id, kind, text, duration }])
      if (duration > 0) {
        timers.current.set(
          id,
          setTimeout(() => remove(id), duration)
        )
      }
    },
    [remove]
  )

  // 卸载时清空所有定时器
  useEffect(() => {
    const map = timers.current
    return () => {
      map.forEach((t) => clearTimeout(t))
      map.clear()
    }
  }, [])

  const value = useMemo(() => ({ toast }), [toast])

  return (
    <Ctx.Provider value={value}>
      {children}
      {/* 右下角堆叠，不阻塞交互 */}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col items-end gap-2"
        role="status"
        aria-live="polite"
      >
        {items.map((t) => {
          const { icon: Icon, cls } = META[t.kind]
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => remove(t.id)}
              className={cn(
                'pointer-events-auto flex max-w-md cursor-pointer items-center gap-2 rounded-md border bg-bg-card px-3 py-2',
                'text-base text-fg shadow-popup animate-slide-in',
                cls
              )}
            >
              <Icon className="size-4 shrink-0" />
              <span className="text-fg">{t.text}</span>
            </button>
          )
        })}
      </div>
    </Ctx.Provider>
  )
}
