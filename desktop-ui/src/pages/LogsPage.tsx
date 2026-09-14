import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import {
  ArrowDown,
  ArrowDownToLine,
  Eraser,
  FolderOpen,
  ScrollText,
  Search,
  X,
} from 'lucide-react'
import { invoke } from '@tauri-apps/api/core'
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
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { useToast } from '@/components/feedback/Toast'
import { inTauri } from '@/lib/gateway'
import { clear, getSnapshot, subscribe, type OpLogLine } from '@/lib/oplog'
import { cn, fmtTime } from '@/lib/utils'
import type { LogLevel } from '@/types/domain'

/* ═══════════════ 常量 ═══════════════ */

/**
 * 筛选值。
 *
 * 与 v2.2「系统日志」的四级筛选相比，这里收敛成「全部 / 成功 / 警告 / 失败」：
 * 操作日志的记录绝大多数是 info 的过程行与 success/error 的结果行，
 * 保留 info 单独可筛的意义不大，而「只看失败」是排障时最常用的动作。
 */
type LevelFilter = LogLevel | 'all'

const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'success', label: '成功' },
  { value: 'warn', label: '警告' },
  { value: 'error', label: '失败' },
  { value: 'info', label: '过程' },
]

/** 级别 → 文字色（语义令牌，禁止硬编码 hex） */
const LEVEL_CLASS: Record<LogLevel, string> = {
  info: 'text-fg-muted',
  success: 'text-success',
  warn: 'text-warning',
  error: 'text-danger',
}

/** 距底部多少像素内仍视为"贴底"，超过则判定用户手动上滑 */
const BOTTOM_THRESHOLD = 40

/* ═══════════════ 单行日志 ═══════════════ */

/**
 * LogLineRow — 操作日志单行渲染
 *
 * 左固定宽时间戳 + 右自适应正文；正文允许换行（长报文不丢内容）。
 * 时间戳用 tabular-nums 保持等宽，避免滚动时列抖动。
 */
function LogLineRow({ line }: { line: OpLogLine }) {
  const isSeparator = /^─+$/.test(line.text)
  return (
    <div className="flex items-start gap-3">
      <span className="shrink-0 select-none font-mono text-sm tabular text-fg-faint">
        {fmtTime(line.ts)}
      </span>
      <span
        className={cn(
          'min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-sm',
          isSeparator ? 'text-fg-faint' : LEVEL_CLASS[line.level]
        )}
      >
        {line.text}
      </span>
    </div>
  )
}

/** 空态：无操作记录 / 过滤无结果 */
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

/**
 * 操作日志页（v3.0 由「系统日志」改回 v2.3 的操作日志）
 *
 * 记录的是**用户在界面上做了什么**及其结果，不是网关的运行输出。
 * 数据来自 `lib/oplog` 的本地存储，由 `lib/oplogBackend` 在数据源层自动埋点，
 * 因此本页没有任何「拉取」动作 —— 刷新按钮在这里没有意义，已移除。
 *
 * 保留的交互（沿用 v2.2 已验证的日志查看器体验）：
 *   * 自动滚动 + 智能暂停（上滑即停，浮出「回到最新」）
 *   * Ctrl/Cmd+F 行内搜索、Esc 关闭
 *   * 文本区 select-text（全局 user-select: none 的唯一例外）
 */
export function LogsPage() {
  const { toast } = useToast()

  const [level, setLevel] = useState<LevelFilter>('all')
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  /** 自动滚动：默认开启，跟随最新日志 */
  const [autoScroll, setAutoScroll] = useState(true)
  /** 因用户上滑而暂停过自动滚动 —— 决定是否显示「回到最新」浮标 */
  const [pausedByUser, setPausedByUser] = useState(false)

  /** 订阅操作日志存储（外部状态，用 useSyncExternalStore 而非 useState 快照） */
  const logs = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

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
  /** 当前视图是否被筛选/搜索收窄（决定空态文案） */
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

  /** 清空：连持久化一起清（否则刷新页面又回来了） */
  const onClear = useCallback(() => {
    clear()
    setPausedByUser(false)
    setAutoScroll(true)
    toast('操作日志已清空', 'info')
  }, [toast])

  /** 打开网关日志目录：操作日志之外，排障仍需看网关自身的输出 */
  const onOpenLogsDir = useCallback(() => {
    void invoke('open_logs_dir').catch((e) =>
      toast(`打开日志目录失败：${e instanceof Error ? e.message : String(e)}`, 'error')
    )
  }, [toast])

  /** 是否显示「回到最新」浮标：因用户上滑而暂停时 */
  const showJumpButton = pausedByUser && !autoScroll && filtered.length > 0

  return (
    <PageShell>
      <PageHeader
        title="操作日志"
        description="账号 / 密钥 / 模型等操作的执行记录（本地保存，重启后仍在）"
      />

      {/* 工具栏：级别筛选 · 搜索 · 自动滚动 / 清空 / 日志目录 */}
      <PageToolbar className="mb-3">
        <Select value={level} onValueChange={(v) => setLevel(v as LevelFilter)}>
          <SelectTrigger className="w-[110px]" aria-label="按记录级别筛选">
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
              aria-label="搜索操作日志"
              aria-pressed={searchOpen}
            >
              <Search />
            </Button>
          </TooltipTrigger>
          <TooltipContent>搜索操作日志内容 (Ctrl+F)</TooltipContent>
        </Tooltip>

        {searchOpen && (
          <div className="relative">
            <Input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKeyDown}
              placeholder="搜索操作日志..."
              className="w-56 pr-8 font-mono text-sm"
              aria-label="搜索操作日志内容"
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
                ? '自动滚动已开启：新记录会跟随到底部'
                : '自动滚动已关闭：点击恢复跟随最新记录'}
            </TooltipContent>
          </Tooltip>

          {inTauri && (
            <Button
              variant="outline"
              onClick={onOpenLogsDir}
              title="操作日志之外，网关自身的运行输出仍在 logs/ 目录"
            >
              <FolderOpen />
              日志目录
            </Button>
          )}

          <Button variant="outline" onClick={onClear} disabled={logs.length === 0}>
            <Eraser />
            清空
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
          aria-label="操作日志输出"
        >
          {filtered.length === 0 ? (
            narrowed ? (
              <LogEmpty
                icon={Search}
                title="没有匹配的记录"
                description="试试其他关键词或切换级别筛选"
              />
            ) : (
              <LogEmpty
                icon={ScrollText}
                title="暂无操作记录"
                description="在账号管理 / API 管理 / 模型列表里做操作（添加账号、刷新积分、改密钥等），这里会按时间记录每一次操作及其结果"
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
            aria-label="回到最新记录并恢复自动滚动"
          >
            <ArrowDown className="size-3.5" />
            回到最新
          </button>
        )}
      </PageBody>
    </PageShell>
  )
}
