import { Fragment, useCallback, useMemo, useRef, useState } from 'react'
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Route,
  Trash2,
  Workflow,
} from 'lucide-react'
import {
  PageShell,
  PageHeader,
  PageToolbar,
  PageBody,
  ToolbarDivider,
} from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { TableEmpty } from '@/components/ui/table'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { cn } from '@/lib/utils'
import {
  CHAIN_CHECK_META,
  STATUS_META,
  channelMeta,
  type AutoChain,
  type AutoChainModel,
  type ChainCheckResult,
  type Channel,
  type ModelEntry,
} from '@/types/domain'

/**
 * Auto路由链 配置页（v3.2 多链版）
 *
 * 一条路由链 = 若干真实模型按顺序组成的故障转移序列。v3.2 起支持**多条**自定义
 * 路由链，链名可自定义；客户端请求 model="<链名>" 即调用该链。
 * （总名 "Auto路由链" 已按 master 要求不再暴露进 /v1/models，仅保留请求兼容。）
 *
 * 数据：
 *  - GET  /v1/admin/auto-chain         → 全量链 + 后端可选模型（兜底）
 *  - POST /v1/admin/auto-chain         → 整份保存（启用/关闭、删除、编辑共用）
 *  - POST /v1/admin/auto-chain/create  → 添加「无名N」新链
 *  - POST /v1/admin/auto-chain/check   → 向上游发极短探测请求（检查连通性）
 *  - GET  /v1/admin/models（listModels）→ 与「模型列表」页同源的统一模型目录：
 *        隐藏的模型不进「选择模型」下拉，置顶的模型排最前（需求 3）。
 * 保存后立即生效，无需重启网关（每次请求都会重读配置）。
 */

/** 通道 → 圆点颜色（CSS 变量，深浅主题各自的值定义在 globals.css）
 *  ★ Trae 用与「模型列表」等页一致的灰（--fg-subtle），不用 channel-trae 的黑色 */
const CHANNEL_DOT_COLOR: Partial<Record<Channel, string>> = {
  Loomy: 'hsl(var(--channel-loomy))',
  WorkBuddy_IE: 'hsl(var(--channel-wbie))',
  WorkBuddy: 'hsl(var(--channel-wb))',
  Trae: 'hsl(var(--fg-subtle))',
}

/**
 * 从对外模型名推断所属通道（与后端 route_provider 的前缀规则一致）。
 *
 * ⚠ Loomy 判定必须放在其它前缀之前：v3.1 起对外前缀是 `lm-`
 * （历史前缀 `loomy-` 仍兼容请求），后端规则为
 * `m.startswith("lm-") or "loomy" in m`（providers/__init__.py）。
 * 仅在统一模型列表里查不到该模型时兜底使用。
 */
function channelOfRouteModel(routeModelId: string): Channel | undefined {
  const low = routeModelId.toLowerCase()
  if (low.startsWith('lm-') || low.includes('loomy')) return 'Loomy'
  if (low.startsWith('wbie') || low.startsWith('wbai') || low.includes('intl'))
    return 'WorkBuddy_IE'
  if (low.startsWith('tr-') || low.includes('trae')) return 'Trae'
  if (low.startsWith('wb-') || low.includes('workbuddy')) return 'WorkBuddy'
  return undefined
}

const cloneChains = (chains: AutoChain[]): AutoChain[] =>
  chains.map((c) => ({ ...c, models: c.models.map((m) => ({ ...m })) }))

export function AutoRouterPage() {
  const { toast } = useToast()

  const chainState = useAsync(
    () => backend.getAutoChains(),
    [],
    { chains: [] as AutoChain[], availableModels: [] as string[] }
  )
  /** 统一模型列表（与「模型列表」页同源）：隐藏模型被排除、置顶模型排最前 */
  const modelsState = useAsync(
    () => backend.listModels({ channel: 'all', showHidden: false }),
    [],
    [] as ModelEntry[]
  )

  const chains = chainState.data.chains

  /** 渲染期同步到 ref，供乐观更新的回滚快照用（与 ModelsPage.patch 同思路） */
  const chainsRef = useRef(chains)
  chainsRef.current = chains

  /** 展开模型简报的链 id（点行尾「<」切换） */
  const [expandedId, setExpandedId] = useState<string | null>(null)
  /** 检查结果：chainId → (模型名 → 结果) */
  const [checks, setChecks] = useState<Record<string, Record<string, ChainCheckResult>>>({})
  const [checkingChain, setCheckingChain] = useState<string | null>(null)
  const [checkingModel, setCheckingModel] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  /** 编辑态草稿（非 null 即处于编辑页）；与已保存值分离，保存成功才落库 */
  const [draft, setDraft] = useState<AutoChain | null>(null)
  const [saving, setSaving] = useState(false)
  const [pick, setPick] = useState('')

  const refreshing = chainState.loading

  /** 检查状态的三色徽标 + 耗时/摘要提示 */
  const renderCheckBadge = useCallback((r?: ChainCheckResult) => {
    const meta = CHAIN_CHECK_META[r?.status ?? 'unknown']
    const tip = r
      ? `${r.detail || meta.label}${r.latencyMs ? ` · ${r.latencyMs}ms` : ''}`
      : '尚未检查：右键路由链选「检查」'
    return (
      <span className="inline-flex items-center gap-1.5" title={tip}>
        <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', meta.dot)} />
        <span className={cn('rounded-md border px-1.5 py-0.5 text-sm', meta.badge)}>
          {meta.label}
        </span>
      </span>
    )
  }, [])

  /* ─────────── 链级操作 ─────────── */

  /**
   * 统一的链数组落库入口：乐观更新 + 失败回滚。
   * 返回是否成功，供编辑保存决定是否退出编辑态。
   */
  const persist = useCallback(
    async (next: AutoChain[], label: string): Promise<boolean> => {
      const snapshot = chainsRef.current
      chainState.setData({ ...chainState.data, chains: next })
      try {
        await backend.saveAutoChains(next)
        toast(label, 'success')
        return true
      } catch (e) {
        chainState.setData({ ...chainState.data, chains: snapshot })
        toast(e instanceof Error ? e.message : '操作失败，已回滚', 'error')
        return false
      }
    },
    [chainState, toast]
  )

  /** 「添加新路由链」：后端自动命名「无名N」；旧网关无此端点时本地兜底同名规则 */
  const addChain = useCallback(async () => {
    setCreating(true)
    try {
      const { chains: next } = await backend.createAutoChain()
      chainState.setData({ ...chainState.data, chains: next })
      toast(`已添加「${next[next.length - 1]?.name ?? '新路由链'}」，右键它进行编辑`, 'success')
    } catch {
      try {
        const used = new Set(chainsRef.current.map((c) => c.name))
        let n = 1
        while (used.has(`无名${n}`)) n += 1
        const next: AutoChain[] = [
          ...cloneChains(chainsRef.current),
          { id: `c-${Date.now()}`, name: `无名${n}`, enabled: true, models: [] },
        ]
        const ok = await persist(next, `已添加「无名${n}」，右键它进行编辑`)
        if (!ok) return
      } catch {
        toast('添加失败：网关不可达', 'error')
        return
      }
    } finally {
      setCreating(false)
    }
  }, [chainState, persist, toast])

  /** 「启用/关闭」：启用=绿色参与路由；关闭=黄色暂不参与 */
  const toggleChain = useCallback(
    (c: AutoChain) => {
      void persist(
        chainsRef.current.map((x) => (x.id === c.id ? { ...x, enabled: !x.enabled } : x)),
        c.enabled ? `已关闭「${c.name}」` : `已启用「${c.name}」`
      )
    },
    [persist]
  )

  /** 「删除」：整链移除（保存即生效） */
  const removeChain = useCallback(
    (c: AutoChain) => {
      setChecks((prev) => {
        if (!(c.id in prev)) return prev
        const next = { ...prev }
        delete next[c.id]
        return next
      })
      setExpandedId((cur) => (cur === c.id ? null : cur))
      void persist(
        chainsRef.current.filter((x) => x.id !== c.id),
        `已删除「${c.name}」`
      )
    },
    [persist]
  )

  /* ─────────── 检查 ─────────── */

  const mergeCheckResults = useCallback((chainId: string, results: ChainCheckResult[]) => {
    setChecks((prev) => {
      const map = { ...(prev[chainId] ?? {}) }
      for (const r of results) map[r.model] = r
      return { ...prev, [chainId]: map }
    })
  }, [])

  /** 「检查」整条链：向上游每个模型发一条极短探测请求（并发） */
  const checkChain = useCallback(
    async (c: AutoChain) => {
      if (!c.models.length) {
        toast('该路由链还没有模型，右键选「编辑」先添加', 'warn')
        return
      }
      setExpandedId(c.id)
      setCheckingChain(c.id)
      try {
        const { results } = await backend.checkAutoChain(c.id)
        mergeCheckResults(c.id, results)
        const ok = results.filter((r) => r.status === 'ok').length
        const busy = results.filter((r) => r.status === 'busy').length
        const down = results.filter((r) => r.status === 'down').length
        toast(`「${c.name}」检查完成：正常 ${ok} · 繁忙 ${busy} · 断连 ${down}`, ok > 0 ? 'success' : 'warn')
      } catch (e) {
        toast(e instanceof Error ? e.message : '检查失败', 'error')
      } finally {
        setCheckingChain(null)
      }
    },
    [mergeCheckResults, toast]
  )

  /** 右键单个模型「检查」：只探活这一个模型 */
  const checkModel = useCallback(
    async (c: AutoChain, model: string) => {
      const key = `${c.id}:${model}`
      setExpandedId(c.id)
      setCheckingModel(key)
      try {
        const { results } = await backend.checkAutoChain(c.id, model)
        mergeCheckResults(c.id, results)
        const r = results[0]
        if (r) {
          toast(
            `${model}：${CHAIN_CHECK_META[r.status].label}${r.latencyMs ? `（${r.latencyMs}ms）` : ''}`,
            r.status === 'ok' ? 'success' : r.status === 'busy' ? 'warn' : 'error'
          )
        }
      } catch (e) {
        toast(e instanceof Error ? e.message : '检查失败', 'error')
      } finally {
        setCheckingModel(null)
      }
    },
    [mergeCheckResults, toast]
  )

  /* ─────────── 编辑 ─────────── */

  const startEdit = useCallback((c: AutoChain) => {
    setDraft({ ...c, models: c.models.map((m) => ({ ...m })) })
    setPick('')
  }, [])

  const cancelEdit = useCallback(() => {
    setDraft(null)
    setPick('')
  }, [])

  /** 保存编辑：名称查重（忽略大小写）+ 超时归一化，成功才退出编辑页 */
  const saveDraft = useCallback(async () => {
    if (!draft) return
    const name = draft.name.trim()
    if (!name) {
      toast('路由链名称不能为空', 'warn')
      return
    }
    if (
      chainsRef.current.some(
        (x) => x.id !== draft.id && x.name.trim().toLowerCase() === name.toLowerCase()
      )
    ) {
      toast(`已有同名的路由链「${name}」，请换个名字`, 'warn')
      return
    }
    const models: AutoChainModel[] = draft.models.map((m) => ({
      model: m.model,
      timeout: Math.max(0, Math.floor(Number(m.timeout) || 0)),
    }))
    const next = chainsRef.current.map((x) =>
      x.id === draft.id ? { ...x, name, enabled: draft.enabled, models } : x
    )
    setSaving(true)
    try {
      const ok = await persist(next, `已保存「${name}」`)
      if (ok) {
        setDraft(null)
        setPick('')
      }
    } finally {
      setSaving(false)
    }
  }, [draft, persist, toast])

  const addModel = useCallback(() => {
    if (!draft) return
    const m = pick.trim()
    if (!m) return
    if (draft.models.some((x) => x.model === m)) {
      toast('该模型已在链中', 'info')
      return
    }
    setDraft({ ...draft, models: [...draft.models, { model: m, timeout: 120 }] })
    setPick('')
  }, [draft, pick, toast])

  const removeModel = useCallback((idx: number) => {
    setDraft((d) => (d ? { ...d, models: d.models.filter((_, i) => i !== idx) } : d))
  }, [])

  const moveModel = useCallback((idx: number, delta: -1 | 1) => {
    setDraft((d) => {
      if (!d) return d
      const next = idx + delta
      if (next < 0 || next >= d.models.length) return d
      const arr = [...d.models]
      ;[arr[idx], arr[next]] = [arr[next], arr[idx]]
      return { ...d, models: arr }
    })
  }, [])

  const setTimeoutOf = useCallback((idx: number, value: string) => {
    setDraft((d) => {
      if (!d) return d
      const models = d.models.map((m, i) =>
        i === idx ? { ...m, timeout: Math.max(0, Math.floor(Number(value) || 0)) } : m
      )
      return { ...d, models }
    })
  }, [])

  /* ─────────── 派生数据 ─────────── */

  /** 链上模型 → 所属通道：优先用统一模型列表，查不到再按前缀推断 */
  const channelOf = useCallback(
    (routeModelId: string): Channel | undefined =>
      modelsState.data.find((m) => m.routeModelId === routeModelId)?.channel ??
      channelOfRouteModel(routeModelId),
    [modelsState.data]
  )

  /**
   * 「选择模型」可选项 = 统一模型列表（隐藏已排除、置顶已置顶）- 已在链中的。
   * 统一列表拉取失败时退回后端 auto-chain 返回的 availableModels（仅名称，无置顶信息）。
   */
  const modelOptions = useMemo<(ModelEntry | { id: string; name: string; routeModelId: string; channel?: Channel; pinned: boolean })[]>(() => {
    const inChain = new Set(draft?.models.map((m) => m.model) ?? [])
    if (modelsState.data.length > 0) {
      return modelsState.data
        .filter((m) => !inChain.has(m.routeModelId))
    }
    return chainState.data.availableModels
      .filter((id) => !inChain.has(id))
      .map((id) => ({ id, name: id, routeModelId: id, channel: channelOfRouteModel(id), pinned: false }))
  }, [modelsState.data, chainState.data.availableModels, draft])

  const enabledCount = useMemo(() => chains.filter((c) => c.enabled).length, [chains])
  const modelsLoading = modelsState.status === 'idle' || modelsState.loading

  /* ─────────── 渲染 ─────────── */

  return (
    <PageShell>
      <PageHeader
        title="Auto路由链"
        description="多条自定义路由链：请求 model 填链名，按链内顺序故障转移，第一个成功的模型胜出"
      />

      <PageToolbar className="mb-3">
        {draft ? (
          <>
            <Button variant="default" onClick={() => void saveDraft()} loading={saving}>
              保存路由链
            </Button>
            <Button variant="outline" onClick={cancelEdit} disabled={saving}>
              取消
            </Button>
            <span className="ml-auto text-sm text-fg-faint">
              正在编辑「{draft.name}」· 链上 {draft.models.length} 个模型
            </span>
          </>
        ) : (
          <>
            <Button variant="outline" onClick={() => void chainState.reload()} loading={refreshing}>
              <RefreshCw />
              刷新
            </Button>
            <ToolbarDivider />
            <Button variant="default" onClick={() => void addChain()} loading={creating}>
              <Plus />
              添加新路由链
            </Button>
            <span className="ml-auto text-sm text-fg-faint">
              {chains.length > 0
                ? `共 ${chains.length} 条路由链 · 启用 ${enabledCount} / 关闭 ${chains.length - enabledCount}`
                : '尚无路由链'}
            </span>
          </>
        )}
      </PageToolbar>

      <PageBody className="p-0">
        {draft ? (
          /* ═══════ 编辑页：链名 + 模型顺序/超时 + 添加模型 ═══════ */
          <div className="p-4">
            <div className="mb-4 flex items-center gap-3">
              <span className="w-20 shrink-0 text-sm text-fg-subtle">路由链名称</span>
              <Input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                className="w-64"
                placeholder="自定义链名，例如：主力链"
                aria-label="路由链名称"
              />
              <span className="text-sm text-fg-faint">
                客户端请求 model 填这个名称即可调用本链
              </span>
            </div>

            {draft.models.length === 0 ? (
              <div className="mb-3 rounded-lg border border-dashed border-border px-4 py-6 text-center text-fg-faint">
                链上还没有模型 —— 在下方「选择模型」加入第一个模型
              </div>
            ) : (
              <table className="w-full text-base">
                <thead>
                  <tr className="border-b border-border text-left text-sm text-fg-subtle">
                    <th className="px-4 py-2 font-medium">顺序</th>
                    <th className="px-4 py-2 font-medium">请求模型名称</th>
                    <th className="px-4 py-2 font-medium">所属通道</th>
                    <th className="px-4 py-2 font-medium">超时（秒）</th>
                    <th className="px-4 py-2 text-right font-medium">调整</th>
                  </tr>
                </thead>
                <tbody>
                  {draft.models.map((m, i) => {
                    const ch = channelOf(m.model)
                    return (
                      <tr key={`${m.model}-${i}`} className="border-b border-border-subtle last:border-0">
                        <td className="px-4 py-2.5">
                          <span className="flex size-6 items-center justify-center rounded-md bg-primary/12 font-mono text-sm font-medium text-primary">
                            {i + 1}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="font-mono text-fg" title={m.model}>
                            {m.model}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="flex items-center gap-2 text-fg-muted">
                            <span
                              aria-hidden
                              className="size-1.5 shrink-0 rounded-full bg-fg-subtle"
                              style={{ background: ch ? CHANNEL_DOT_COLOR[ch] : undefined }}
                            />
                            {channelMeta(ch).label}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <Input
                            type="number"
                            min={0}
                            value={String(m.timeout)}
                            onChange={(e) => setTimeoutOf(i, e.target.value)}
                            className="w-20 tabular"
                            aria-label={`模型 ${m.model} 超时秒数`}
                          />
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="flex items-center justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={i === 0}
                              onClick={() => moveModel(i, -1)}
                              aria-label={`上移 ${m.model}`}
                            >
                              <ArrowUp />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={i === draft.models.length - 1}
                              onClick={() => moveModel(i, 1)}
                              aria-label={`下移 ${m.model}`}
                            >
                              <ArrowDown />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => removeModel(i)}
                              aria-label={`移除 ${m.model}`}
                            >
                              <Trash2 className="text-danger" />
                            </Button>
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}

            {/* 添加模型：与「模型列表」页统一 —— 隐藏的不出现、置顶的排最前 */}
            <div className="mt-4 flex items-center gap-2 border-t border-border pt-4">
              <Route className="size-4 shrink-0 text-fg-subtle" />
              <Select value={pick} onValueChange={setPick}>
                <SelectTrigger className="w-[360px]" aria-label="选择要加入链的模型">
                  <SelectValue
                    placeholder={
                      modelsLoading ? '正在加载模型列表…' : '选择模型（隐藏模型不在此列，置顶排最前）'
                    }
                  />
                </SelectTrigger>
                <SelectContent className="max-h-72">
                  {modelOptions.length === 0 ? (
                    <div className="px-3 py-2 text-sm text-fg-faint">没有更多可添加的模型</div>
                  ) : (
                    modelOptions.map((m) => (
                      <SelectItem key={m.routeModelId} value={m.routeModelId}>
                        {'pinned' in m && m.pinned && <span className="mr-1 text-primary">★</span>}
                        <span className="font-mono">{m.routeModelId}</span>
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
          </div>
        ) : chains.length === 0 ? (
          /* ═══════ 空态 ═══════ */
          <TableEmpty
            colSpan={4}
            icon={Workflow}
            title="还没有路由链"
            description="点上方「添加新路由链」创建一条链（自动命名「无名N」），再右键它选「编辑」添加模型"
          />
        ) : (
          /* ═══════ 路由链列表 ═══════ */
          <table className="w-full text-base">
            <thead>
              <tr className="border-b border-border text-left text-sm text-fg-subtle">
                <th className="px-4 py-2 font-medium">路由链名称</th>
                <th className="px-4 py-2 font-medium">模型数量</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="w-14 px-4 py-2 text-right font-medium" aria-label="展开模型简报" />
              </tr>
            </thead>
            <tbody>
              {chains.map((c) => {
                const st = STATUS_META[c.enabled ? 'enabled' : 'disabled']
                const expanded = expandedId === c.id
                const checkMap = checks[c.id] ?? {}
                const isChecking = checkingChain === c.id
                return (
                  <Fragment key={c.id}>
                    <ContextMenu>
                      <ContextMenuTrigger asChild>
                        <tr className="cursor-context-menu border-b border-border-subtle last:border-0 hover:bg-bg-card-hover">
                          <td className="px-4 py-3">
                            <span className="flex items-center gap-2">
                              <Route className="size-4 shrink-0 text-fg-subtle" />
                              <span className="text-fg" title={c.name}>
                                {c.name}
                              </span>
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <span className="tabular text-fg-muted">{c.models.length} 个</span>
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={cn('inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-sm', st.badge)}
                            >
                              <span aria-hidden className={cn('size-1.5 rounded-full', st.dot)} />
                              {st.label}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-right">
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-label={expanded ? `收起 ${c.name} 模型简报` : `展开 ${c.name} 模型简报`}
                              onClick={() => setExpandedId(expanded ? null : c.id)}
                            >
                              {/* 「<」：展开时旋转成向下，示意下拉区已打开 */}
                              <ChevronLeft className={cn('transition-transform', expanded && '-rotate-90')} />
                            </Button>
                          </td>
                        </tr>
                      </ContextMenuTrigger>
                      <ContextMenuContent>
                        <ContextMenuLabel>{c.name}</ContextMenuLabel>
                        <ContextMenuSeparator />
                        <ContextMenuItem onSelect={() => startEdit(c)}>
                          <Pencil />
                          编辑
                        </ContextMenuItem>
                        <ContextMenuItem onSelect={() => void checkChain(c)} disabled={isChecking}>
                          <Activity />
                          {isChecking ? '检查中…' : '检查'}
                        </ContextMenuItem>
                        <ContextMenuItem onSelect={() => toggleChain(c)}>
                          <Power />
                          {c.enabled ? '关闭' : '启用'}
                        </ContextMenuItem>
                        <ContextMenuSeparator />
                        <ContextMenuItem destructive onSelect={() => removeChain(c)}>
                          <Trash2 />
                          删除
                        </ContextMenuItem>
                      </ContextMenuContent>
                    </ContextMenu>
                    {/* 「<」下拉的模型简报：紧贴本链行下方 */}
                    {expanded && (
                      <tr className="border-b border-border-subtle">
                        <td colSpan={4} className="bg-bg-content px-4 py-3">
                          <div className="mb-2 flex items-center gap-2 text-sm text-fg-subtle">
                            <Activity className="size-3.5" />
                            {isChecking
                              ? `正在检查「${c.name}」…（向上游发送极短探测请求）`
                              : `「${c.name}」模型简报 · 右键单个模型可单独检查`}
                          </div>
                          {c.models.length === 0 ? (
                            <div className="py-2 text-sm text-fg-faint">
                              该路由链还没有模型 —— 右键路由链选「编辑」添加
                            </div>
                          ) : (
                            <table className="w-full text-base">
                              <thead>
                                <tr className="border-b border-border text-left text-sm text-fg-subtle">
                                  <th className="px-3 py-1.5 font-medium">顺序</th>
                                  <th className="px-3 py-1.5 font-medium">请求模型名称</th>
                                  <th className="px-3 py-1.5 font-medium">所属通道</th>
                                  <th className="px-3 py-1.5 font-medium">状态</th>
                                </tr>
                              </thead>
                              <tbody>
                                {c.models.map((m, i) => {
                                  const ch = channelOf(m.model)
                                  const key = `${c.id}:${m.model}`
                                  return (
                                    <ContextMenu key={`${m.model}-${i}`}>
                                      <ContextMenuTrigger asChild>
                                        <tr className="cursor-context-menu border-b border-border-subtle last:border-0 hover:bg-bg-card-hover">
                                          <td className="px-3 py-2">
                                            <span className="flex size-5 items-center justify-center rounded bg-primary/12 font-mono text-xs font-medium text-primary">
                                              {i + 1}
                                            </span>
                                          </td>
                                          <td className="px-3 py-2">
                                            <span className="font-mono text-fg" title={m.model}>
                                              {m.model}
                                            </span>
                                          </td>
                                          <td className="px-3 py-2">
                                            <span className="flex items-center gap-2 text-fg-muted">
                                              <span
                                                aria-hidden
                                                className="size-1.5 shrink-0 rounded-full bg-fg-subtle"
                                                style={{ background: ch ? CHANNEL_DOT_COLOR[ch] : undefined }}
                                              />
                                              {channelMeta(ch).label}
                                            </span>
                                          </td>
                                          <td className="px-3 py-2">
                                            {checkingModel === key || isChecking ? (
                                              <span className="text-sm text-fg-faint">检查中…</span>
                                            ) : (
                                              renderCheckBadge(checkMap[m.model])
                                            )}
                                          </td>
                                        </tr>
                                      </ContextMenuTrigger>
                                      <ContextMenuContent>
                                        <ContextMenuLabel>{m.model}</ContextMenuLabel>
                                        <ContextMenuSeparator />
                                        <ContextMenuItem
                                          onSelect={() => void checkModel(c, m.model)}
                                          disabled={checkingModel === key || isChecking}
                                        >
                                          <Activity />
                                          {checkingModel === key || isChecking ? '检查中…' : '检查'}
                                        </ContextMenuItem>
                                      </ContextMenuContent>
                                    </ContextMenu>
                                  )
                                })}
                              </tbody>
                            </table>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </PageBody>
    </PageShell>
  )
}
