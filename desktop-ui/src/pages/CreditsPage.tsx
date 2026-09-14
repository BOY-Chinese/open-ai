import { useCallback, useMemo, useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RTooltip,
  ResponsiveContainer,
} from 'recharts'
import {
  ChevronLeft,
  ChevronRight,
  TrendingUp,
  TrendingDown,
  Coins,
  RefreshCw,
  Receipt,
} from 'lucide-react'
import {
  PageShell,
  PageHeader,
  PageToolbar,
  PageBody,
  ToolbarDivider,
} from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Skeleton, StatSkeleton, TableSkeleton } from '@/components/ui/skeleton'
import { TableEmpty } from '@/components/ui/table'
import { VirtualTable } from '@/components/data/VirtualTable'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { useTheme } from '@/hooks/useTheme'
import { backend } from '@/lib/dataSource'
import { cn, fmtCredit, fmtTime, shortDay, toDayKey } from '@/lib/utils'
import {
  CHANNELS,
  type ChannelFilter,
  type UsageRow,
  channelMeta,
} from '@/types/domain'
import { seriesColors, type SeriesColor } from '@/config/chart'

type ViewMode = 'today' | 'week'

const USAGE_COLUMNS = [
  { key: 'channel', label: '所属通道', width: 120 },
  { key: 'account', label: '账号', width: 200 },
  { key: 'model', label: '模型', width: 290 },
  { key: 'time', label: '时间', width: 170 },
  { key: 'amount', label: '消耗量', width: 120, align: 'right' as const },
]

/* ── 图表配色：集中定义于 config/chart.ts（含为何用字面量的说明） ── */

/** 统计卡片：大字数值 + 语义色 */
function StatCard({
  label,
  value,
  tone,
  icon: Icon,
  loading,
  hint,
}: {
  label: string
  value: string
  tone: 'gain' | 'cost'
  icon: typeof Coins
  loading?: boolean
  hint?: string
}) {
  if (loading) return <StatSkeleton />
  return (
    <div className="flex flex-1 items-center gap-4 rounded-lg border border-border bg-bg-card px-6 py-4">
      {/* 图标随之放大，避免与 64px 数字比例失衡 */}
      <div
        className={cn(
          'flex size-12 shrink-0 items-center justify-center rounded-md',
          tone === 'gain' ? 'bg-success/12' : 'bg-danger/12'
        )}
      >
        <Icon className={cn('size-6', tone === 'gain' ? 'text-success' : 'text-danger')} />
      </div>
      <div className="min-w-0">
        <div className="text-sm text-fg-subtle">{label}</div>
        <div
          className={cn(
            'tabular font-semibold leading-none',
            tone === 'gain' ? 'text-success' : 'text-danger'
          )}
          /* 需求：数字放大一倍 —— 原 text-stat = 32px，此处 64px */
          style={{ fontSize: '64px' }}
        >
          {value}
        </div>
        {hint && <div className="mt-0.5 text-xs text-fg-faint">{hint}</div>}
      </div>
    </div>
  )
}

/** 图表 Tooltip：显示三通道明细与合计 */
function ChartTooltip({
  active,
  payload,
  label,
  colors,
}: {
  active?: boolean
  payload?: { dataKey?: string | number; value?: number }[]
  label?: string
  colors: SeriesColor[]
}) {
  if (!active || !payload?.length) return null
  const total = payload.reduce((s, p) => s + (Number(p.value) || 0), 0)
  return (
    <div className="rounded-md border border-border-strong bg-bg-card px-3 py-2 shadow-popup">
      <div className="mb-1.5 text-sm font-medium text-fg">{label}</div>
      <div className="space-y-1">
        {colors.map((c) => {
          const item = payload.find((p) => p.dataKey === c.key)
          return (
            <div key={c.key} className="flex items-center gap-2 text-sm">
              <span
                aria-hidden
                className="size-2 shrink-0 rounded-sm"
                style={{ background: c.color }}
              />
              <span className="text-fg-muted">{c.label}</span>
              <span className="ml-auto pl-4 tabular font-mono text-fg">
                {fmtCredit(Number(item?.value) || 0)}
              </span>
            </div>
          )
        })}
        <div className="mt-1 flex items-center gap-2 border-t border-border pt-1 text-sm">
          <span className="text-fg-subtle">合计</span>
          <span className="ml-auto pl-4 tabular font-mono font-medium text-fg">
            {fmtCredit(total)}
          </span>
        </div>
      </div>
    </div>
  )
}

export function CreditsPage() {
  const { toast } = useToast()
  const [view, setView] = useState<ViewMode>('today')
  const [channel, setChannel] = useState<ChannelFilter>('all')
  const [weekOffset, setWeekOffset] = useState(0)

  /**
   * 图表配色随主题切换：亮色系列在白底上不可见、暗色系列在白底上才够亮，
   * 一套值无法同时满足两套主题（详见 config/chart.ts）。
   * 图例 / Tooltip / 柱子共用这一个数组，保证三者配色同源。
   */
  const { resolved } = useTheme()
  const colors = useMemo(() => seriesColors(resolved), [resolved])

  /* 今日数据 */
  const today = useAsync(() => backend.getToday(channel), [channel], null)
  /* 本周数据 */
  /* 周数据同样依赖通道：切换通道后需重新拉取，否则柱状图会停留在全部通道 */
  const week = useAsync(() => backend.getWeek(weekOffset, channel), [weekOffset, channel], null)

  const todayKey = toDayKey(new Date())

  /** 周切换：不允许翻到未来周 */
  const shiftWeek = useCallback((delta: number) => {
    setWeekOffset((prev) => {
      const next = prev + delta
      return next > 0 ? 0 : next
    })
  }, [])

  const onRefresh = useCallback(async () => {
    if (view === 'today') {
      await today.reload()
      toast('已刷新今日积分数据', 'success')
    } else {
      await week.reload()
      toast('已刷新本周积分数据', 'success')
    }
  }, [view, today, week, toast])

  const refreshing = today.loading || week.loading

  return (
    <PageShell>
      <PageHeader
        title="积分看板"
        description="积分获取与消耗趋势 · 三通道合并统计"
      />

      {/* 视图切换 + 通道筛选 + 刷新 */}
      <PageToolbar className="mb-3">
        <div className="flex items-center gap-1 rounded-md border border-border bg-bg-input p-0.5">
          {(
            [
              { key: 'today', label: '今日情况' },
              { key: 'week', label: '每周情况' },
            ] as const
          ).map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setView(t.key)}
              aria-pressed={view === t.key}
              className={cn(
                'cursor-pointer rounded-sm px-3 py-1 text-base transition-colors duration-fast',
                view === t.key
                  ? 'bg-primary/15 font-medium text-fg'
                  : 'text-fg-muted hover:bg-bg-card-hover hover:text-fg'
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        <ToolbarDivider />

        <Select value={channel} onValueChange={(v) => setChannel(v as ChannelFilter)}>
          <SelectTrigger className="w-[160px]" aria-label="按通道筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部通道</SelectItem>
            {CHANNELS.map((ch) => (
              <SelectItem key={ch} value={ch}>
                {channelMeta(ch).label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Button variant="outline" onClick={() => void onRefresh()} loading={refreshing}>
          <RefreshCw />
          刷新
        </Button>

        <span className="ml-auto text-sm text-fg-faint tabular">
          更新于{' '}
          {fmtTime(
            (view === 'today' ? today.data?.updatedAt : week.data?.updatedAt) ?? Date.now() / 1000
          )}
        </span>
      </PageToolbar>

      {view === 'today' ? (
        <>
          {/* 今日：获取 / 消耗 双卡片 */}
          <div className="mb-4 flex gap-4">
            <StatCard
              label="今日获取积分"
              value={today.loading ? '0' : fmtCredit(today.data?.stats.gained ?? 0)}
              tone="gain"
              icon={TrendingUp}
              loading={today.loading}
            />
            <StatCard
              label="今日消耗积分"
              value={today.loading ? '0' : fmtCredit(today.data?.stats.used ?? 0)}
              tone="cost"
              icon={TrendingDown}
              loading={today.loading}
              hint={`统计范围：${channel === 'all' ? '全部通道' : channelMeta(channel).label}`}
            />
          </div>

          {/* 今日流水 */}
          <PageBody>
            {today.loading ? (
              <TableSkeleton rows={8} cols={5} />
            ) : (
              <VirtualTable
                rows={today.data?.rows ?? []}
                columns={USAGE_COLUMNS}
                rowKey={(r) => r.id}
                threshold={50}
                empty={
                  <TableEmpty
                    colSpan={USAGE_COLUMNS.length}
                    icon={Receipt}
                    title="今日暂无积分消耗"
                    description={`${todayKey} 还没有产生用量记录`}
                  />
                }
                renderRow={(r: UsageRow) => (
                  <>
                    <td className="h-9 px-3">
                      <span className="flex items-center gap-2">
                        <span
                          aria-hidden
                          className={cn('size-1.5 shrink-0 rounded-full', channelMeta(r.channel).dot)}
                        />
                        <span className="truncate text-base text-fg-muted">
                          {channelMeta(r.channel).label}
                        </span>
                      </span>
                    </td>
                    <td className="h-9 px-3">
                      <span className="block truncate font-mono text-base text-fg" title={r.account}>
                        {r.account}
                      </span>
                    </td>
                    <td className="h-9 px-3">
                      <span className="block truncate font-mono text-sm text-fg-muted" title={r.model}>
                        {r.model}
                      </span>
                    </td>
                    <td className="h-9 px-3">
                      <span className="tabular text-sm text-fg-subtle">{fmtTime(r.ts)}</span>
                    </td>
                    <td className="h-9 px-3 text-right">
                      <span
                        className={cn(
                          'tabular text-base font-mono',
                          r.amount > 0 ? 'text-fg' : 'text-fg-faint'
                        )}
                      >
                        {fmtCredit(r.amount)}
                      </span>
                    </td>
                  </>
                )}
              />
            )}
          </PageBody>
        </>
      ) : (
        <>
          {/* 本周：获取 / 消耗 双卡片 */}
          <div className="mb-4 flex gap-4">
            <StatCard
              label="本周获取积分"
              value={week.loading ? '0' : fmtCredit(week.data?.stats.gained ?? 0)}
              tone="gain"
              icon={TrendingUp}
              loading={week.loading}
            />
            <StatCard
              label="本周消耗积分"
              value={week.loading ? '0' : fmtCredit(week.data?.stats.used ?? 0)}
              tone="cost"
              icon={TrendingDown}
              loading={week.loading}
              hint={
                week.data
                  ? `${shortDay(week.data.startDay)} — ${shortDay(week.data.endDay)}`
                  : undefined
              }
            />
          </div>

          {/* 周切换器 */}
          <div className="mb-3 flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => shiftWeek(-1)}>
              <ChevronLeft />
              上一周
            </Button>
            <Button
              variant={weekOffset === 0 ? 'default' : 'outline'}
              size="sm"
              onClick={() => setWeekOffset(0)}
            >
              本周
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => shiftWeek(1)}
              disabled={weekOffset >= 0}
            >
              下一周
              <ChevronRight />
            </Button>
            <span className="ml-2 font-mono text-sm text-fg-subtle">
              {week.data ? `${week.data.startDay} ~ ${week.data.endDay}` : ''}
            </span>
            <span className="ml-auto flex items-center gap-3">
              {/* 单通道筛选时只显示该通道图例，避免暗示图中有其他系列 */}
              {colors.filter((c) => channel === 'all' || c.key === channel).map((c) => (
                <span key={c.key} className="flex items-center gap-1.5 text-sm text-fg-subtle">
                  <span
                    aria-hidden
                    className="size-2 rounded-sm"
                    style={{ background: c.color }}
                  />
                  {c.label}
                </span>
              ))}
            </span>
          </div>

          {/* 堆叠柱状图：每日消耗趋势 */}
          <PageBody className="min-h-[280px] p-4">
            {week.loading ? (
              <div className="flex h-full flex-col justify-end gap-2">
                <Skeleton className="h-full w-full" />
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={week.data?.daily ?? []}
                  margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
                  barCategoryGap="28%"
                >
                  <CartesianGrid stroke="hsl(var(--border))" vertical={false} />
                  <XAxis
                    dataKey="day"
                    tickFormatter={(v: string) => shortDay(v)}
                    tick={{ fill: 'hsl(var(--fg-subtle))', fontSize: 12 }}
                    axisLine={{ stroke: 'hsl(var(--border))' }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: 'hsl(var(--fg-subtle))', fontSize: 12 }}
                    axisLine={false}
                    tickLine={false}
                    width={48}
                    tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v))}
                  />
                  <RTooltip
                    cursor={{ fill: 'hsl(var(--bg-card-hover))' }}
                    content={<ChartTooltip colors={colors} />}
                  />
                  {colors.map((c) => (
                    <Bar
                      key={c.key}
                      dataKey={c.key}
                      stackId="usage"
                      fill={c.color}
                      /* 圆角仅作用于堆叠顶部视觉，最大值 2px 保持克制 */
                      radius={[2, 2, 0, 0]}
                      maxBarSize={48}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </PageBody>
        </>
      )}
    </PageShell>
  )
}
