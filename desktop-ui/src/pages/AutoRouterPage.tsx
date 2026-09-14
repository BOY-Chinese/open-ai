import { useCallback, useMemo, useState } from 'react'
import { ArrowDown, Plus, RefreshCw, Route, Trash2, Workflow } from 'lucide-react'
import {
  PageShell,
  PageHeader,
  PageToolbar,
  PageBody,
  ToolbarDivider,
} from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { TableEmpty } from '@/components/ui/table'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { cn } from '@/lib/utils'
import { channelMeta } from '@/types/domain'

/**
 * Auto路由连 配置页
 *
 * Auto路由连 是一个**虚拟模型**：客户端请求 model="Auto路由连" 时，网关按
 * 本页配置的顺序逐个尝试链上模型，某个模型超时/报错/无输出时自动切换下一个，
 * 首个成功产出内容的模型胜出；全部失败返回 502。
 *
 * 数据：
 *  - GET /v1/admin/auto-chain      → 当前链 + 全量可选模型（provider 已注册的）
 *  - POST /v1/admin/auto-chain     → 保存（写 config.json 的 auto_chain 段）
 * 保存后立即生效，无需重启网关（每次请求都会重读配置）。
 */

/** 从对外模型名推断所属通道（与后端 route_provider 的前缀规则一致） */
function channelOfRouteModel(routeModelId: string): string {
  const low = routeModelId.toLowerCase()
  if (low.startsWith('loomy')) return channelMeta('Loomy').label
  if (low.startsWith('wbie') || low.startsWith('wbai') || low.includes('intl'))
    return channelMeta('WorkBuddy_IE').label
  if (low.startsWith('tr-') || low.includes('trae')) return channelMeta('Trae').label
  if (low.startsWith('wb-') || low.includes('workbuddy'))
    return channelMeta('WorkBuddy').label
  return '未知通道'
}

export function AutoRouterPage() {
  const { toast } = useToast()
  const chainState = useAsync(
    () => backend.getAutoChain(),
    [],
    { chain: { enabled: true, timeout: 120, models: [] }, availableModels: [] }
  )

  /** 本地草稿：进入编辑态后与「已保存值」分离，保存成功才落库 */
  const [draft, setDraft] = useState<{ enabled: boolean; timeout: string; models: string[] } | null>(
    null
  )
  const [saving, setSaving] = useState(false)
  const [pick, setPick] = useState('')

  const saved = chainState.data.chain
  const editing = draft !== null
  const models = editing ? draft.models : saved.models
  const enabled = editing ? draft.enabled : saved.enabled
  const timeout = editing ? draft.timeout : String(saved.timeout)

  const dirty = useMemo(
    () =>
      editing &&
      (draft.enabled !== saved.enabled ||
        draft.timeout !== String(saved.timeout) ||
        draft.models.join('\n') !== saved.models.join('\n')),
    [editing, draft, saved]
  )

  const startEdit = useCallback(() => {
    setDraft({ enabled: saved.enabled, timeout: String(saved.timeout), models: [...saved.models] })
  }, [saved])

  const discard = useCallback(() => {
    setDraft(null)
  }, [])

  const onSave = useCallback(async () => {
    if (!draft) return
    const t = Math.max(0, Math.floor(Number(draft.timeout) || 0))
    setSaving(true)
    try {
      await backend.saveAutoChain({ enabled: draft.enabled, timeout: t, models: draft.models })
      await chainState.reload()
      setDraft(null)
      toast('Auto路由连 配置已保存', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally {
      setSaving(false)
    }
  }, [draft, chainState, toast])

  const addModel = useCallback(() => {
    if (!draft) return
    const m = pick.trim()
    if (!m) return
    if (draft.models.includes(m)) {
      toast('该模型已在链中', 'info')
      return
    }
    setDraft({ ...draft, models: [...draft.models, m] })
    setPick('')
  }, [draft, pick, toast])

  const removeModel = useCallback(
    (idx: number) => {
      if (!draft) return
      setDraft({ ...draft, models: draft.models.filter((_, i) => i !== idx) })
    },
    [draft]
  )

  const moveModel = useCallback(
    (idx: number, delta: -1 | 1) => {
      if (!draft) return
      const next = idx + delta
      if (next < 0 || next >= draft.models.length) return
      const arr = [...draft.models]
      ;[arr[idx], arr[next]] = [arr[next], arr[idx]]
      setDraft({ ...draft, models: arr })
    },
    [draft]
  )

  const onRefresh = useCallback(async () => {
    await chainState.reload()
    toast('已刷新路由链配置', 'success')
  }, [chainState, toast])

  const remaining = useMemo(() => {
    const opts = chainState.data.availableModels
    if (!opts.length) return []
    const inChain = new Set(models)
    return opts.filter((m) => !inChain.has(m))
  }, [chainState.data.availableModels, models])

  const refreshing = chainState.loading

  return (
    <PageShell>
      <PageHeader
        title="Auto路由连"
        description="虚拟模型 Auto路由连：按下方顺序故障转移，第一个成功的模型胜出"
      />

      <PageToolbar className="mb-3">
        <Button variant="outline" onClick={() => void onRefresh()} loading={refreshing}>
          <RefreshCw />
          刷新
        </Button>
        <ToolbarDivider />
        {editing ? (
          <>
            <Button variant="default" onClick={() => void onSave()} loading={saving} disabled={!dirty}>
              保存修改
            </Button>
            <Button variant="outline" onClick={discard} disabled={saving}>
              放弃编辑
            </Button>
            {dirty && <span className="text-sm text-warning">有未保存的修改</span>}
          </>
        ) : (
          <Button variant="default" onClick={startEdit}>
            编辑路由链
          </Button>
        )}
        <span className="ml-auto text-sm text-fg-faint">
          {models.length > 0 ? `链上模型 ${models.length} 个` : '尚未配置模型'}
        </span>
      </PageToolbar>

      {/* 启用开关 + 超时 */}
      <div className="mb-3 flex items-center gap-6 rounded-lg border border-border bg-bg-card px-4 py-3">
        <label className="flex cursor-pointer items-center gap-2.5">
          <Switch
            checked={enabled}
            disabled={!editing}
            onCheckedChange={(v) => draft && setDraft({ ...draft, enabled: v })}
          />
          <span className="text-base text-fg">启用 Auto路由连</span>
        </label>
        <div className="flex items-center gap-2">
          <span className="text-sm text-fg-subtle">单模型超时</span>
          <Input
            type="number"
            min={0}
            value={timeout}
            disabled={!editing}
            onChange={(e) => draft && setDraft({ ...draft, timeout: e.target.value })}
            className="w-24 tabular"
          />
          <span className="text-sm text-fg-faint">秒（0 = 用 provider 默认；超时切换下一个）</span>
        </div>
      </div>

      {/* 链顺序表 */}
      <PageBody className="p-0">
        {models.length === 0 ? (
          <TableEmpty
            colSpan={4}
            icon={Workflow}
            title="路由链为空"
            description="Auto路由连 未配置任何模型时不可用；点「编辑路由链」添加模型"
          />
        ) : (
          <table className="w-full text-base">
            <thead>
              <tr className="border-b border-border text-left text-sm text-fg-subtle">
                <th className="px-4 py-2 font-medium">顺序</th>
                <th className="px-4 py-2 font-medium">请求模型名称</th>
                <th className="px-4 py-2 font-medium">所属通道</th>
                <th className="px-4 py-2 text-right font-medium">调整</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m, i) => (
                <tr key={`${m}-${i}`} className="border-b border-border-subtle last:border-0">
                  <td className="px-4 py-2.5">
                    <span className="flex size-6 items-center justify-center rounded-md bg-primary/12 font-mono text-sm font-medium text-primary">
                      {i + 1}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className="font-mono text-fg" title={m}>
                      {m}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className="flex items-center gap-2 text-fg-muted">
                      <span
                        aria-hidden
                        className="size-1.5 shrink-0 rounded-full bg-fg-subtle"
                        style={{
                          background: m.toLowerCase().startsWith('loomy')
                            ? 'hsl(var(--channel-loomy))'
                            : m.toLowerCase().startsWith('wbie')
                              ? 'hsl(var(--channel-wbie))'
                              : m.toLowerCase().startsWith('wb-')
                                ? 'hsl(var(--channel-wb))'
                                : m.toLowerCase().startsWith('tr-')
                                  ? 'hsl(var(--channel-trae))'
                                  : undefined,
                        }}
                      />
                      {channelOfRouteModel(m)}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className="flex items-center justify-end gap-1">
                      {editing ? (
                        <>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={i === 0}
                            onClick={() => moveModel(i, -1)}
                            aria-label={`上移 ${m}`}
                          >
                            <ArrowDown className="rotate-180" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={i === models.length - 1}
                            onClick={() => moveModel(i, 1)}
                            aria-label={`下移 ${m}`}
                          >
                            <ArrowDown />
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={saving}
                            onClick={() => removeModel(i)}
                            aria-label={`移除 ${m}`}
                          >
                            <Trash2 className="text-danger" />
                          </Button>
                        </>
                      ) : (
                        <span className="text-sm text-fg-faint">
                          {i === 0 ? '首选' : i === models.length - 1 ? '末位兜底' : `第 ${i + 1} 顺位`}
                        </span>
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* 编辑态：添加模型 */}
        {editing && (
          <div className="flex items-center gap-2 border-t border-border px-4 py-3">
            <Route className="size-4 shrink-0 text-fg-subtle" />
            <Select value={pick} onValueChange={setPick}>
              <SelectTrigger className="w-[340px]" aria-label="选择要加入链的模型">
                <SelectValue placeholder="选择模型（按通道前缀命名）" />
              </SelectTrigger>
              <SelectContent className="max-h-72">
                {remaining.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-fg-faint">没有更多可添加的模型</div>
                ) : (
                  remaining.map((m) => (
                    <SelectItem key={m} value={m}>
                      <span className="font-mono">{m}</span>
                      <span className={cn('ml-2 text-xs text-fg-faint')}>{channelOfRouteModel(m)}</span>
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
            <Button variant="outline" onClick={addModel} disabled={!pick}>
              <Plus />
              加入链中
            </Button>
            <span className="text-sm text-fg-faint">
              顺序即尝试顺序：排得越靠前越优先被调用
            </span>
          </div>
        )}
      </PageBody>
    </PageShell>
  )
}
