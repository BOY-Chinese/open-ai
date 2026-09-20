import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  RefreshCw,
  Boxes,
  Copy,
  Eye,
  EyeOff,
  Pin,
  PinOff,
  CheckSquare,
  Square,
  X,
  Layers,
  Database,
  TriangleAlert,
} from 'lucide-react'
import {
  PageShell,
  PageHeader,
  PageToolbar,
  PageBody,
  ToolbarDivider,
} from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { TableSkeleton } from '@/components/ui/skeleton'
import { TableEmpty } from '@/components/ui/table'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'
import { VirtualTable } from '@/components/data/VirtualTable'
import { useToast } from '@/components/feedback/Toast'
import { useAsync, useSelection } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { filterModelView, readModelCache, writeModelCache } from '@/lib/modelCache'
import { cn, fmtTime } from '@/lib/utils'
import { CHAIN_CHECK_META, CHANNELS, type ChannelFilter, type ModelEntry, channelMeta } from '@/types/domain'

/** 基础列（多选模式会额外插入勾选列，故宽度动态计算）
 *  宽度按 1180px 默认窗口核算：内容区可用 ≈908px（非多选）/ ≈868px（多选），
 *  使两种模式下均不产生横向滚动，同时保证末列 Badge 完整可见。 */
const BASE_COLUMNS = [
  { key: 'channel', label: '所属通道', width: 120 },
  { key: 'name', label: '模型名称', width: 230 },
  { key: 'ratio', label: '积分倍率', width: 110, align: 'right' as const },
  { key: 'upstream', label: '请求模型名称', width: 290 },
  { key: 'flags', label: '标记', width: 110 },
]

export function ModelsPage() {
  const { toast } = useToast()
  const [channel, setChannel] = useState<ChannelFilter>('all')
  const [showHidden, setShowHidden] = useState(false)
  const [selectionMode, setSelectionMode] = useState(false)
  const [busy, setBusy] = useState(false)

  /**
   * 本地缓存快照 —— **同步**读取，首次 render 就有内容可画。
   *
   * 惰性初始化保证整页只读一次 localStorage；页面随导航卸载/重挂载会再读一次，
   * 因此「离开模型列表页再回来」同样是秒开，而不是又一轮骨架屏。
   * 缓存的由来见 `lib/modelCache.ts`（倍率端点要联网，是首屏等待的根源）。
   */
  const [cache] = useState(readModelCache)

  const {
    status,
    data: allModels,
    reload,
    setData,
  } = useAsync(
    // ★ 只拉一次全量：通道 / 显隐筛选改在本地做（见下方 useMemo）。
    //   原先是把 channel 塞进依赖，切一次筛选就多一次秒级网络往返 + 一次骨架屏。
    () => backend.listModels({ channel: 'all', showHidden: true }),
    [],
    cache?.list ?? ([] as ModelEntry[])
  )

  /** 当前视图 = 全量 ∩ 通道 ∩ 显隐（与写入缓存使用同一个过滤函数） */
  const models = useMemo(
    () => filterModelView(allModels, channel, showHidden),
    [allModels, channel, showHidden]
  )

  /** 已经成功拿到过一次服务端数据（此后不再是「纯缓存视图」） */
  const [everLoaded, setEverLoaded] = useState(false)
  useEffect(() => {
    if (status === 'success') setEverLoaded(true)
  }, [status])

  /**
   * 缓存落盘 —— 全页唯一的写入点。
   *
   * 只在 `status === 'success'` 时写：否则「首帧用的是缓存」这件事会把缓存
   * 原样回写一遍并刷新 savedAt，等于用旧数据冒充新数据（陈旧提示就永远不会出现）。
   * 隐藏/置顶这类本地乐观修改会让 allModels 变化，故它们同样会顺带更新缓存。
   */
  useEffect(() => {
    if (status === 'success' && allModels.length > 0) writeModelCache(allModels)
  }, [status, allModels])

  /** 首帧来自缓存、且新数据尚未就绪 —— 用「本地缓存」提示替代骨架屏 */
  const showingCache = cache !== null && !everLoaded
  /** 最新一次拉取失败（此时界面上的内容是缓存，需要如实标注而非假装正常） */
  const loadFailed = status === 'error'
  /** 首屏仍在加载（无缓存可显示时才真正空屏） */
  const initialLoading = status === 'idle' || status === 'loading'
  const showSkeleton = initialLoading && models.length === 0

  /** 最新全量列表（回滚用）—— 渲染期同步到 ref，避免把它塞进 patch 的依赖 */
  const allRef = useRef(allModels)
  allRef.current = allModels

  const ids = useMemo(() => models.map((m) => m.id), [models])
  const sel = useSelection(ids)

  /** 关闭多选模式时清空勾选，避免隐藏操作作用于陈旧选择 */
  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false)
    sel.clear()
  }, [sel])

  /**
   * 统一的行数据变更入口：乐观更新 + 失败回滚。
   *
   * 回滚用的是**整份全量列表快照**，而不是当前视图 —— 视图是筛选后的子集，
   * 拿它 setData 会把未显示的通道整段抹掉（曾是个真实隐患）。
   *
   * 另外：隐藏后不再需要重新拉取。视图由 `models` 本地派生，`hidden` 一变，
   * 「显示隐藏模型」未勾选时该行会立即从列表消失，效果与刷新一致但无网络等待。
   */
  const patch = useCallback(
    async (targetIds: string[], next: Partial<ModelEntry>, label: string) => {
      const snapshot = allRef.current
      const set = new Set(targetIds)
      setData((list) => list.map((m) => (set.has(m.id) ? { ...m, ...next } : m)))
      try {
        await backend.updateModels(targetIds, next)
        toast(label, 'success')
      } catch {
        setData(snapshot)
        toast('操作失败，已回滚', 'error')
      }
    },
    [setData, toast]
  )

  const onToggleHidden = useCallback(
    (m: ModelEntry) => void patch([m.id], { hidden: !m.hidden }, m.hidden ? '模型已显示' : '模型已隐藏'),
    [patch]
  )

  const onTogglePinned = useCallback(
    (m: ModelEntry) => void patch([m.id], { pinned: !m.pinned }, m.pinned ? '已取消置顶' : '模型已置顶'),
    [patch]
  )

  /** 复制实际路由表模型名（右键菜单），便于直接粘进客户端 model 配置 */
  const onCopyRouteId = useCallback(
    async (m: ModelEntry) => {
      try {
        await navigator.clipboard.writeText(m.routeModelId)
        toast(`已复制 ${m.routeModelId}`, 'success')
      } catch {
        toast('复制失败：剪贴板不可用', 'error')
      }
    },
    [toast]
  )

  /** 正在检查的模型 id —— 一次只跑一个探活，避免连点右键向上游连环发请求 */
  const [checkingId, setCheckingId] = useState<string | null>(null)

  /**
   * 右键「检查该模型」：与 Auto 路由链的「检查」**同核** —— 后端同一个
   * auto_router.check_model，向上游发一条极短探测请求（"回复ok"）看有没有回复。
   *
   * 与路由链页的差异：模型列表不加「状态」列，结果只用右下角 Toast 报告
   * （正常/繁忙/断连 + 耗时），弹完即走，列表保持纯数据视图。
   */
  const onCheckModel = useCallback(
    async (m: ModelEntry) => {
      if (checkingId) return
      setCheckingId(m.id)
      // 探活最长 30s（后端 CHECK_TIMEOUT_CAP）：提示若用默认 2.6s 早没了，
      // 这里给 6s，让「我点过了」这件事在结果回来前可见
      toast(`正在检查 ${m.routeModelId}…（向上游发送极短探测请求）`, 'info', 6000)
      try {
        const { results } = await backend.checkModel(m.routeModelId)
        const r = results[0]
        if (r) {
          toast(
            `${m.routeModelId}：${CHAIN_CHECK_META[r.status].label}${r.latencyMs ? `（${r.latencyMs}ms）` : ''}`,
            r.status === 'ok' ? 'success' : r.status === 'busy' ? 'warn' : 'error'
          )
        } else {
          toast('检查失败：网关返回空结果', 'error')
        }
      } catch (e) {
        toast(e instanceof Error ? e.message : '检查失败', 'error')
      } finally {
        setCheckingId(null)
      }
    },
    [checkingId, toast]
  )

  const onRefresh = useCallback(async () => {
    setBusy(true)
    try {
      await backend.refreshModels()
      await reload()
      toast('已拉取最新模型列表及积分倍率', 'success')
    } finally {
      setBusy(false)
    }
  }, [reload, toast])

  /** 多选模式下的批量操作 */
  const selectedIds = useMemo(() => [...sel.selected], [sel.selected])
  const batchPatch = useCallback(
    async (next: Partial<ModelEntry>, label: string) => {
      if (selectedIds.length === 0) {
        toast('请先勾选模型', 'warn')
        return
      }
      await patch(selectedIds, next, `${label}（${selectedIds.length} 个）`)
      sel.clear()
    },
    [selectedIds, patch, sel, toast]
  )

  const columns = selectionMode
    ? [{ key: 'check', label: '', width: 40, align: 'center' as const }, ...BASE_COLUMNS]
    : BASE_COLUMNS

  return (
    <PageShell>
      <PageHeader
        title="模型列表"
        description="Trae / WorkBuddy / WorkBuddy 国际三通道模型与路由映射"
      />

      <PageToolbar className="mb-3">
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

        <ToolbarDivider />

        {/* 是否显示隐藏模型 */}
        <label className="flex cursor-pointer items-center gap-2 text-base text-fg-muted">
          <Switch checked={showHidden} onCheckedChange={setShowHidden} aria-label="显示隐藏模型" />
          显示隐藏模型
        </label>

        <ToolbarDivider />

        <Button
          variant={selectionMode ? 'default' : 'outline'}
          onClick={() => (selectionMode ? exitSelectionMode() : setSelectionMode(true))}
          title="开启后可勾选多行进行批量操作"
        >
          {selectionMode ? <CheckSquare /> : <Square />}
          多选模式
        </Button>

        <Button variant="outline" onClick={() => void onRefresh()} loading={busy}>
          <RefreshCw />
          刷新
        </Button>

        <span className="ml-auto flex items-center gap-2 text-sm text-fg-subtle tabular">
          {/* 缓存状态：让「为什么一进来就有数据 / 数据是不是旧的」有明确交代 */}
          {showingCache && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex">
                  <Badge variant={loadFailed ? 'warning' : 'outline'}>
                    {loadFailed ? (
                      <TriangleAlert className="size-3" />
                    ) : (
                      <Database className="size-3" />
                    )}
                    {loadFailed ? '离线 · 本地缓存' : '本地缓存'}
                  </Badge>
                </span>
              </TooltipTrigger>
              <TooltipContent>
                {loadFailed
                  ? '网关暂时不可达，当前显示的是上一次缓存的模型列表'
                  : `已先用本地缓存渲染${cache?.savedAt ? `（缓存于 ${fmtTime(cache.savedAt / 1000)}）` : ''}，正在后台拉取最新数据`}
              </TooltipContent>
            </Tooltip>
          )}
          {models.length} 个模型
          {sel.count > 0 && <span className="ml-2 text-primary">已选 {sel.count}</span>}
        </span>
      </PageToolbar>

      {/* 多选批量操作条：仅在有勾选时出现 */}
      {selectionMode && sel.count > 0 && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/8 px-3 py-2">
          <Layers className="size-4 shrink-0 text-primary" />
          <span className="text-base text-fg">已选中 {sel.count} 个模型</span>
          <span aria-hidden className="mx-1 h-4 w-px bg-border" />
          <Button size="sm" variant="outline" onClick={() => void batchPatch({ hidden: true }, '已批量隐藏')}>
            <EyeOff />
            批量隐藏
          </Button>
          <Button size="sm" variant="outline" onClick={() => void batchPatch({ hidden: false }, '已批量显示')}>
            <Eye />
            批量显示
          </Button>
          <Button size="sm" variant="outline" onClick={() => void batchPatch({ pinned: true }, '已批量置顶')}>
            <Pin />
            批量置顶
          </Button>
          <Button size="sm" variant="outline" onClick={() => void batchPatch({ pinned: false }, '已批量取消置顶')}>
            <PinOff />
            批量取消置顶
          </Button>
          <Button size="sm" variant="ghost" className="ml-auto" onClick={sel.clear}>
            <X />
            取消选择
          </Button>
        </div>
      )}

      <PageBody>
        {showSkeleton ? (
          <TableSkeleton rows={8} cols={columns.length} />
        ) : (
          <VirtualTable
            rows={models}
            columns={columns}
            rowKey={(m) => m.id}
            threshold={50}
            empty={
              /* 拉取失败 ≠ 没有模型。此前两者共用「没有匹配的模型」文案，
                 网关连不上时用户会读成「我模型怎么全没了」—— 一个看起来
                 完全正常的错误结论。失败时给出原因与重试入口。 */
              loadFailed && models.length === 0 ? (
                <TableEmpty
                  colSpan={columns.length}
                  icon={TriangleAlert}
                  title="模型列表加载失败"
                  description="网关暂时不可达，当前没有可用数据。可点「刷新」重试；若网关刚重启，稍候几秒再试。"
                />
              ) : (
                <TableEmpty
                  colSpan={columns.length}
                  icon={Boxes}
                  title="没有匹配的模型"
                  description={
                    showHidden ? '换个通道筛选试试' : '当前筛选下无可见模型，可开启「显示隐藏模型」'
                  }
                />
              )
            }
            rowProps={(m) => ({ 'data-selected': selectionMode && sel.has(m.id) ? true : undefined })}
            wrapRow={(m, _i, content) => (
              <ContextMenu key={m.id}>
                <ContextMenuTrigger asChild>{content}</ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuLabel>{m.name}</ContextMenuLabel>
                  <ContextMenuSeparator />
                  {/* 检查该模型：向上游发极短探测请求，结果只弹右下角 Toast（不加状态列） */}
                  <ContextMenuItem
                    onSelect={() => void onCheckModel(m)}
                    disabled={checkingId !== null}
                  >
                    <Activity />
                    {checkingId === m.id ? '检查中…' : '检查该模型'}
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  {/* 复制实际路由表模型名，便于直接粘进客户端配置 */}
                  <ContextMenuItem onSelect={() => void onCopyRouteId(m)}>
                    <Copy />
                    复制请求模型名称
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem onSelect={() => onToggleHidden(m)}>
                    {m.hidden ? <Eye /> : <EyeOff />}
                    {m.hidden ? '显示该模型' : '隐藏该模型'}
                  </ContextMenuItem>
                  <ContextMenuItem onSelect={() => onTogglePinned(m)}>
                    {m.pinned ? <PinOff /> : <Pin />}
                    {m.pinned ? '取消置顶' : '置顶该模型'}
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            )}
            renderRow={(m) => {
              const meta = channelMeta(m.channel)
              return (
                <>
                  {selectionMode && (
                    <td className="px-3 text-center">
                      <span className="inline-flex items-center justify-center">
                        <Checkbox
                          checked={sel.has(m.id)}
                          onCheckedChange={() => sel.toggle(m.id)}
                          aria-label={`选择 ${m.name}`}
                        />
                      </span>
                    </td>
                  )}

                  <td className="px-3">
                    <span className="flex items-center gap-2">
                      {m.pinned && <Pin className="size-3 shrink-0 text-primary" aria-label="已置顶" />}
                      <span
                        aria-hidden
                        className={cn('size-1.5 shrink-0 rounded-full', meta.dot)}
                      />
                      <span className="truncate text-base text-fg-muted">{meta.label}</span>
                    </span>
                  </td>

                  <td className="px-3">
                    <span className="block truncate text-base text-fg" title={m.name}>
                      {m.name}
                    </span>
                  </td>

                  <td className="px-3 text-right">
                    {/* 倍率三态：未知显示「--」，真·0 显示 0.00（如 hy3 确实免费）。
                        混在一起显示 0.00 会让人误以为 gpt-6-astra 免费 —— 它其实
                        收费，只是上游目录里没有、倍率从未拉到。
                        用 `!== false` 而非 `=== true`：旧缓存/演示数据没有该字段，
                        应按「已知」原样显示，不能一律变成「--」。 */}
                    {m.ratioKnown !== false ? (
                      <span className="tabular text-base text-fg-muted">
                        {m.ratio.toFixed(2)}
                        <span className="ml-1 text-sm text-fg-subtle">
                          {m.ratioUnit === 'credits' ? 'credits' : '×'}
                        </span>
                      </span>
                    ) : (
                      <span
                        className="tabular text-base text-fg-subtle"
                        title="上游未提供该模型的倍率（多为目录外模型），非 0 倍率"
                      >
                        --
                      </span>
                    )}
                  </td>

                  {/* 请求模型名称 = 实际路由表模型名（通道前缀 + 上游模型名） */}
                  <td className="px-3">
                    <span
                      className="block truncate font-mono text-sm text-fg-subtle"
                      title={m.routeModelId}
                    >
                      {m.routeModelId}
                    </span>
                  </td>

                  {/* 标记列：置顶/隐藏用紧凑 Badge，避免撑破列宽 */}
                  <td className="px-3">
                    <span className="flex items-center gap-1.5 overflow-hidden">
                      {m.pinned && <Badge variant="primary">置顶</Badge>}
                      {m.hidden && <Badge variant="outline">隐藏</Badge>}
                      {!m.pinned && !m.hidden && (
                        <span className="text-sm text-fg-faint">—</span>
                      )}
                    </span>
                  </td>
                </>
              )
            }}
          />
        )}
      </PageBody>

      {/* 表头全选（多选模式）：置于底部操作栏左侧，避免与 sticky 表头冲突 */}
      {selectionMode && !initialLoading && models.length > 0 && (
        <div className="mt-3 flex items-center gap-3">
          <label className="flex cursor-pointer items-center gap-2 text-base text-fg-muted">
            <Checkbox
              checked={sel.allChecked ? true : sel.someChecked ? 'indeterminate' : false}
              onCheckedChange={sel.toggleAll}
              aria-label="全选模型"
            />
            全选（{models.length}）
          </label>
          <Button size="sm" variant="ghost" onClick={exitSelectionMode}>
            退出多选
          </Button>
        </div>
      )}
    </PageShell>
  )
}
