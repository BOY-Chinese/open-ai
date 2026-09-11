import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import {
  ArrowDown,
  ArrowDownToLine,
  Eraser,
  RefreshCw,
  ScrollText,
  Search,
  X,
} from 'lucide-react'
import {
  PageShell,
  PageHeader,
  PageBody,
  PageToolbar,
  ToolbarDivider,
} from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { cn, fmtTime } from '@/lib/utils'
import type { LogLevel, LogLine } from '@/types/domain'

/* ═══════════════ 常量 ═══════════════ */

/** 筛选值：日志级别 + 全部 */
type LevelFilter = LogLevel | 'all'

const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
  { value: 'all', label: '全部级别' },
  { value: 'info', label: '信息' },
  { value: 'success', label: '成功' },
  { value: 'warn', label: '警告' },
  { value: 'error', label: '错误' },
]

/** 级别 → 文字色（语义令牌，禁止硬编码 hex） */
const LEVEL_CLASS: Record<LogLevel, string> = {
  info: 'text-fg-muted',
  success: 'text-success',
  warn: 'text-warning',
  error: 'text-danger',
}

/** 距底部多少像素内仍视为“贴底”，超过则判定用户手动上滑 */
const BOTTOM_THRESHOLD = 40

/** 加载骨架行的宽度（确定性，避免每帧重排时抖动） */
const SKELETON_WIDTHS = ['72%', '48%', '80%', '56%', '64%', '40%']

/* ═══════════════ 单行日志 ═══════════════ */

/**
 * LogLineRow — 日志单行渲染
 *
 * 左固定宽时间戳 + 右自适应正文；正文允许换行（长报文不丢内容）。
 * 时间戳用 tabular-nums 保持等宽，避免滚动时列抖动。
 */
function LogLineRow({ line }: { line: LogLine }) {
  return (
    <div className="flex items-start gap-3">
      <span className="shrink-0 select-none font-mono text-sm tabular text-fg-faint">
        {fmtTime(line.ts)}
      </span>
      <span
        className={cn(
          'min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-sm',
          LEVEL_CLASS[line.level]
        )}
      >
        {line.text}
      </span>
    </div>
  )
}

/** 加载态骨架：模拟 6 行日志的时间戳 + 正文结构 */
function LogSkeleton() {
  return (
    <div className="space-y-2 p-3" aria-busy="true" aria-live="polite">
      {SKELETON_WIDTHS.map((width, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-4 w-28 shrink-0" />
          <Skeleton className="h-4" style={{ width }} />
        </div>
      ))}
    </div>
  )
}

/** 空态：无数据 / 过滤无结果 / 已清空 */
function LogEmpty({
  icon: Icon,
  title,
  description,
}: {
  icon: typeof ScrollText
  title: string
  description: string
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 py-8 text-center">
      <Icon className="size-8 text-fg-faint" aria-hidden />
      <p className="text-md font-medium text-fg-muted">{title}</p>
      <p className="max-w-md text-base text-fg-subtle">{description}</p>
    </div>
  )
}

/* ═══════════════ 页面 ═══════════════ */

export function LogsPage() {
  const { toast } = useToast()

  const [level, setLevel] = useState<LevelFilter>('all')
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  /** 自动滚动：默认开启，跟随最新日志 */
  const [autoScroll, setAutoScroll] = useState(true)
  /** 因用户上滑而暂停过自动滚动 —— 决定是否显示「回到最新」浮标 */
  const [pausedByUser, setPausedByUser] = useState(false)

  const { data: logs, loading, reload, setData } = useAsync(
    () => backend.listLogs(),
    [],
    [] as LogLine[]
  )

  const scrollRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  /** 标记程序自身的滚动，避免 scroll 事件被误判为用户手动上滑 */
  const programmaticScroll = useRef(false)

  /* ── 过滤：级别 + 关键词（useMemo 缓存，输入即时生效） ── */
  const filtered = useMemo(() => {
    const kw = query.trim().toLowerCase()
    return logs.filter((l) => {
      if (level !== 'all' && l.level !== level) return false
      if (kw && !l.text.toLowerCase().includes(kw)) return false
      return true
    })
  }, [logs, level, query])

  const hasQuery = query.trim().length > 0
  /** 当前视图是否被筛选/搜索/清空收窄（决定空态文案） */
  const narrowed = hasQuery || level !== 'all'

  /* ── 滚动控制 ── */

  /** 立即滚到底部（标记为程序滚动，不触发智能暂停） */
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    programmaticScroll.current = true
    el.scrollTop = el.scrollHeight
    // 下一帧复位标记：scroll 事件在设置 scrollTop 后异步派发
    requestAnimationFrame(() => {
      programmaticScroll.current = false
    })
  }, [])

  /** 自动滚动：数据变化后贴底（首次加载 / 追加日志 / 切换筛选均生效） */
  useEffect(() => {
    if (autoScroll) scrollToBottom()
  }, [autoScroll, filtered, scrollToBottom])

  /**
   * 智能暂停：用户手动上滑离开底部（>40px）即关闭自动滚动。
   * 程序滚动与「回到最新」被标记后跳过判定，否则会被自己误停。
   */
  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    if (programmaticScroll.current) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    if (distance > BOTTOM_THRESHOLD && autoScroll) {
      setAutoScroll(false)
      setPausedByUser(true)
    }
  }, [autoScroll])

  /** 「回到最新」：回到底部 + 恢复自动滚动 + 收起浮标 */
  const resumeAutoScroll = useCallback(() => {
    setAutoScroll(true)
    setPausedByUser(false)
    scrollToBottom()
  }, [scrollToBottom])

  /* ── 搜索框开关 ── */

  const closeSearch = useCallback(() => {
    setSearchOpen(false)
    setQuery('')
  }, [])

  const openSearch = useCallback(() => {
    setSearchOpen(true)
    // 输入框挂载后再 focus
    requestAnimationFrame(() => searchRef.current?.focus())
  }, [])

  /* ── Ctrl+F / Cmd+F 唤起搜索，Escape 关闭 ── */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const isFindKey = (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f'
      if (isFindKey) {
        e.preventDefault()
        openSearch()
        return
      }
      if (e.key === 'Escape' && searchOpen) {
        e.preventDefault()
        closeSearch()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [openSearch, closeSearch, searchOpen])

  /* ── 搜索框内 Escape 关闭（阻止冒泡到 window，避免重复处理） ── */
  const onSearchKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        closeSearch()
      }
    },
    [closeSearch]
  )

  /* ── 清空：仅清本地显示，不动后端 ── */
  const onClear = useCallback(() => {
    setData([])
    setPausedByUser(false)
    setAutoScroll(true)
    toast('已清空当前日志显示（后端日志未受影响）', 'info')
  }, [setData, toast])

  /* ── 刷新：重新拉取 ── */
  const onReload = useCallback(async () => {
    await reload()
    setAutoScroll(true)
    setPausedByUser(false)
  }, [reload])

  /** 追加按钮的修饰键语义：普通点击用「信息」级 */
  const onAppendWith = useCallback(
    (lvl: LogLevel) => {
      const label: Record<LogLevel, string> = {
        info: '信息',
        success: '成功',
        warn: '警告',
        error: '错误',
      }
      const line = backend.appendLog(
        lvl,
        `[Demo] 模拟${label[lvl]}日志 · 自动滚动验证`
      )
      setData((prev) => [...prev, line])
      setAutoScroll(true)
      setPausedByUser(false)
    },
    [setData]
  )

  /** 是否显示「回到最新」浮标：因用户上滑而暂停时 */
  const showJumpButton = pausedByUser && !autoScroll && filtered.length > 0

  return (
    <PageShell>
      <PageHeader title="系统日志" description="网关与守护进程运行时输出" />

      {/* 工具栏：级别筛选 · 搜索 · 自动滚动 / 清空 / 刷新 */}
      <PageToolbar className="mb-3">
        <Select value={level} onValueChange={(v) => setLevel(v as LevelFilter)}>
          <SelectTrigger className="w-[110px]" aria-label="按日志级别筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LEVEL_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <ToolbarDivider />

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant={searchOpen ? 'secondary' : 'outline'}
              size="icon"
              onClick={() => (searchOpen ? closeSearch() : openSearch())}
              aria-label="搜索日志"
              aria-pressed={searchOpen}
            >
              <Search />
            </Button>
          </TooltipTrigger>
          <TooltipContent>搜索日志内容 (Ctrl+F)</TooltipContent>
        </Tooltip>

        {searchOpen && (
          <div className="relative">
            <Input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKeyDown}
              placeholder="搜索日志内容..."
              className="w-56 pr-8 font-mono text-sm"
              aria-label="搜索日志内容"
            />
            {hasQuery && (
              <button
                type="button"
                onClick={() => {
                  setQuery('')
                  searchRef.current?.focus()
                }}
                aria-label="清除搜索"
                className={cn(
                  'absolute right-1 top-1/2 flex size-6 -translate-y-1/2 cursor-pointer items-center justify-center',
                  'rounded-sm text-fg-faint transition-colors duration-fast',
                  'hover:bg-bg-card-hover hover:text-fg active:bg-bg-card'
                )}
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
        )}

        {/* 匹配计数：仅在有关键词时出现 */}
        {hasQuery && (
          <Badge variant={filtered.length > 0 ? 'primary' : 'default'}>
            {filtered.length} / {logs.length}
          </Badge>
        )}

        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" onClick={() => onAppendWith('info')} title="追加一条演示日志">
            <ScrollText />
            追加演示日志
          </Button>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant={autoScroll ? 'default' : 'outline'}
                size="icon"
                onClick={() => (autoScroll ? setAutoScroll(false) : resumeAutoScroll())}
                aria-label="自动滚动"
                aria-pressed={autoScroll}
              >
                <ArrowDownToLine />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {autoScroll
                ? '自动滚动已开启：新日志会跟随到底部'
                : '自动滚动已关闭：点击恢复跟随最新日志'}
            </TooltipContent>
          </Tooltip>

          <Button variant="outline" onClick={onClear} disabled={logs.length === 0}>
            <Eraser />
            清空
          </Button>

          <Button variant="outline" onClick={() => void onReload()} loading={loading}>
            <RefreshCw />
            刷新
          </Button>
        </div>
      </PageToolbar>

      {/* 日志区：占满剩余高度，内部滚动 */}
      <PageBody className="relative overflow-hidden">
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="h-full overflow-y-auto bg-bg-log px-3 py-2 select-text"
          role="log"
          aria-live="polite"
          aria-label="系统日志输出"
        >
          {loading ? (
            <LogSkeleton />
          ) : filtered.length === 0 ? (
            narrowed ? (
              <LogEmpty
                icon={Search}
                title="没有匹配的日志"
                description="试试其他关键词或切换级别筛选"
              />
            ) : (
              <LogEmpty
                icon={ScrollText}
                title="暂无日志"
                description="网关尚未输出内容，可点击「刷新」重新拉取，或追加一条演示日志"
              />
            )
          ) : (
            <div className="space-y-0.5">
              {filtered.map((line) => (
                <LogLineRow key={line.id} line={line} />
              ))}
            </div>
          )}
        </div>

        {/* 底部状态条：显示条数 / 总条数 */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-end bg-gradient-to-t from-bg-log px-3 py-2">
          <span className="font-mono text-xs tabular text-fg-faint">
            显示 {filtered.length} / {logs.length} 条
          </span>
        </div>

        {/* 智能暂停浮标：右下角回到最新 */}
        {showJumpButton && (
          <button
            type="button"
            onClick={resumeAutoScroll}
            className={cn(
              'absolute bottom-10 right-4 flex cursor-pointer items-center gap-2 rounded-lg',
              'border border-border-strong bg-bg-card px-3 py-2 text-sm text-fg-muted',
              'shadow-popup transition-colors duration-fast animate-fade-in',
              'hover:bg-bg-card-hover hover:text-fg active:bg-bg-card'
            )}
            aria-label="回到最新日志并恢复自动滚动"
          >
            <ArrowDown className="size-3.5" />
            回到最新
          </button>
        )}
      </PageBody>
    </PageShell>
  )
}
