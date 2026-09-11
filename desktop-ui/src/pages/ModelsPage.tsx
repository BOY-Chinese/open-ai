import { useCallback, useMemo, useState } from 'react'
import {
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
import { cn } from '@/lib/utils'
import { CHANNELS, type ChannelFilter, type ModelEntry, channelMeta } from '@/types/domain'

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

  const { data: models, loading, reload, setData } = useAsync(
    () => backend.listModels({ channel, showHidden }),
    [channel, showHidden],
    [] as ModelEntry[]
  )

  const ids = useMemo(() => models.map((m) => m.id), [models])
  const sel = useSelection(ids)

  /** 关闭多选模式时清空勾选，避免隐藏操作作用于陈旧选择 */
  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false)
    sel.clear()
  }, [sel])

  /** 统一的行数据变更入口：乐观更新 + 失败回滚 */
  const patch = useCallback(
    async (targetIds: string[], next: Partial<ModelEntry>, label: string) => {
      const prev = models
      const set = new Set(targetIds)
      setData((list) => list.map((m) => (set.has(m.id) ? { ...m, ...next } : m)))
      try {
        await backend.updateModels(targetIds, next)
        toast(label, 'success')
        // 隐藏操作会改变筛选结果，需要重新拉取
        if (next.hidden !== undefined) await reload()
      } catch {
        setData(prev)
        toast('操作失败，已回滚', 'error')
      }
    },
    [models, setData, reload, toast]
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

        <span className="ml-auto text-sm text-fg-subtle tabular">
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
        {loading ? (
          <TableSkeleton rows={8} cols={columns.length} />
        ) : (
          <VirtualTable
            rows={models}
            columns={columns}
            rowKey={(m) => m.id}
            threshold={50}
            empty={
              <TableEmpty
                colSpan={columns.length}
                icon={Boxes}
                title="没有匹配的模型"
                description={showHidden ? '换个通道筛选试试' : '当前筛选下无可见模型，可开启「显示隐藏模型」'}
              />
            }
            rowProps={(m) => ({ 'data-selected': selectionMode && sel.has(m.id) ? true : undefined })}
            wrapRow={(m, _i, content) => (
              <ContextMenu key={m.id}>
                <ContextMenuTrigger asChild>{content}</ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuLabel>{m.name}</ContextMenuLabel>
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
                    <span className="tabular text-base text-fg-muted">
                      {m.ratio.toFixed(2)}
                      <span className="ml-1 text-sm text-fg-subtle">
                        {m.ratioUnit === 'credits' ? 'credits' : '×'}
                      </span>
                    </span>
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
      {selectionMode && !loading && models.length > 0 && (
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
