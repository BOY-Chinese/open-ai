import { useCallback, useMemo, useState } from 'react'
import { Plus, RefreshCw, Link2, Users, Check } from 'lucide-react'
import { PageShell, PageHeader, PageToolbar, PageBody, PageFooter, ToolbarDivider } from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
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
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { fmtCredit, cn } from '@/lib/utils'
import {
  CHANNELS,
  STATUS_META,
  type Account,
  type Channel,
  type ChannelFilter,
  channelMeta,
} from '@/types/domain'

/* 列宽显式定义（虚拟滚动模式下必需）
   按 1180px 默认窗口核算：内容区可用 ≈908px，总和 890px 不产生横向滚动 */
const COLUMNS = [
  { key: 'channel', label: '所属通道', width: 120 },
  { key: 'name', label: '账号', width: 320 },
  { key: 'signin', label: '每日签到', width: 100, align: 'center' as const },
  { key: 'credits', label: '当前积分', width: 250 },
  { key: 'status', label: '账号状态', width: 100 },
]

const FILTER_LABEL: Record<ChannelFilter, string> = {
  all: '全部通道',
  Trae: 'Trae',
  WorkBuddy: 'WorkBuddy',
  WorkBuddy_IE: 'WorkBuddy 国际',
}

export function AccountsPage() {
  const { toast } = useToast()
  const [filter, setFilter] = useState<ChannelFilter>('all')
  const [busy, setBusy] = useState<null | 'refresh' | 'reconnect'>(null)
  /** 本地签到状态（后端接入后由接口返回） */
  const [signin, setSignin] = useState<Record<string, boolean>>({})

  const { data: accounts, loading, reload, setData } = useAsync(
    () => backend.listAccounts(filter),
    [filter],
    [] as Account[]
  )

  /* 从全量数据推导筛选下拉的计数，让"全部"也能显示总数 */
  const counts = useMemo(() => {
    const map: Record<ChannelFilter, number> = {
      all: accounts.length,
      Trae: 0,
      WorkBuddy: 0,
      WorkBuddy_IE: 0,
    }
    accounts.forEach((a) => {
      map[a.channel] += 1
    })
    return map
  }, [accounts])

  /** 刷新当前筛选通道的账号状态 */
  const onRefresh = useCallback(async () => {
    setBusy('refresh')
    try {
      const next = await backend.refreshAccounts(filter)
      setData(next)
      toast(`已刷新 ${FILTER_LABEL[filter]} 账号积分`, 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '刷新失败', 'error')
    } finally {
      setBusy(null)
    }
  }, [filter, setData, toast])

  /** 重连当前筛选通道的所有账号 */
  const onReconnect = useCallback(async () => {
    setBusy('reconnect')
    try {
      const next = await backend.reconnectAccounts(filter)
      setData(next)
      toast(`已重新连接 ${FILTER_LABEL[filter]} 账号`, 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '重连失败', 'error')
    } finally {
      setBusy(null)
    }
  }, [filter, setData, toast])

  /** 启用/关闭切换：乐观更新，失败回滚 */
  const onToggle = useCallback(
    async (acc: Account) => {
      const prev = accounts
      setData((list) =>
        list.map((a) =>
          a.id === acc.id
            ? { ...a, enabled: !a.enabled, status: !a.enabled ? 'enabled' : 'disabled' }
            : a
        )
      )
      try {
        await backend.toggleAccount(acc.id)
        toast(!acc.enabled ? '账号已启用' : '账号已关闭', 'success')
      } catch {
        setData(prev)
        toast('切换失败，已回滚', 'error')
      }
    },
    [accounts, setData, toast]
  )

  const onDelete = useCallback(
    async (acc: Account) => {
      const prev = accounts
      setData((list) => list.filter((a) => a.id !== acc.id))
      try {
        await backend.deleteAccount(acc.id)
        toast('账号已删除', 'success')
      } catch {
        setData(prev)
        toast('删除失败，已回滚', 'error')
      }
    },
    [accounts, setData, toast]
  )

  const onReadd = useCallback(
    async (acc: Account) => {
      setBusy('refresh')
      try {
        await backend.addAccount(acc.channel)
        await reload()
        toast('已重新添加该账号', 'success')
      } finally {
        setBusy(null)
      }
    },
    [reload, toast]
  )

  const onAdd = useCallback(
    async (channel: Channel) => {
      setBusy('refresh')
      try {
        await backend.addAccount(channel)
        await reload()
        toast(`已添加 ${channelMeta(channel).label} 账号`, 'success')
      } finally {
        setBusy(null)
      }
    },
    [reload, toast]
  )

  return (
    <PageShell>
      <PageHeader
        title="账号管理"
        description="Trae / WorkBuddy / WorkBuddy 国际三通道统一视图"
      />

      {/* 工具栏：筛选 + 刷新当前通道 + 重连当前通道 */}
      <PageToolbar className="mb-3">
        <Select value={filter} onValueChange={(v) => setFilter(v as ChannelFilter)}>
          <SelectTrigger className="w-[168px]" aria-label="按通道筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部通道 ({counts.all})</SelectItem>
            {CHANNELS.map((ch) => (
              <SelectItem key={ch} value={ch}>
                {channelMeta(ch).label} ({counts[ch]})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <ToolbarDivider />

        <Button
          variant="outline"
          onClick={onRefresh}
          loading={busy === 'refresh'}
          title={`刷新 ${FILTER_LABEL[filter]} 账号状态`}
        >
          <RefreshCw />
          刷新账号
        </Button>
        <Button
          variant="outline"
          onClick={onReconnect}
          loading={busy === 'reconnect'}
          title={`重连 ${FILTER_LABEL[filter]} 账号`}
        >
          <Link2 />
          重新连接
        </Button>

        <span className="ml-auto text-sm text-fg-subtle tabular">
          共 {accounts.length} 个账号
        </span>
      </PageToolbar>

      {/* 表格区：虚拟滚动 + 行右键菜单 */}
      <PageBody>
        {loading ? (
          <TableSkeleton rows={6} cols={5} />
        ) : (
          <VirtualTable
            rows={accounts}
            columns={COLUMNS}
            rowKey={(a) => a.id}
            threshold={50}
            empty={
              <TableEmpty
                colSpan={COLUMNS.length}
                icon={Users}
                title="暂无账号"
                description={`${FILTER_LABEL[filter]} 通道下还没有添加账号，可从下方添加`}
              />
            }
            /* 右键菜单：重新添加 / 启用-关闭 / 删除 */
            wrapRow={(acc, _i, content) => (
              <ContextMenu key={acc.id}>
                <ContextMenuTrigger asChild>{content}</ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuLabel>{acc.name.slice(0, 28)}</ContextMenuLabel>
                  <ContextMenuSeparator />
                  <ContextMenuItem onSelect={() => void onReadd(acc)}>
                    <Plus />
                    重新添加该账号
                  </ContextMenuItem>
                  <ContextMenuItem onSelect={() => void onToggle(acc)}>
                    <Check />
                    {acc.enabled ? '关闭该账号' : '启用该账号'}
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem destructive onSelect={() => void onDelete(acc)}>
                    <span className="size-3.5" aria-hidden />
                    删除
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            )}
            renderRow={(a) => {
              const chMeta = channelMeta(a.channel)
              const stMeta = STATUS_META[a.status]
              const checked = signin[a.id] ?? a.enabled
              return (
                <>
                  {/* 所属通道 */}
                  <td className="h-9 px-3">
                    <span className="flex items-center gap-2">
                      <span
                        aria-hidden
                        className={cn('size-1.5 shrink-0 rounded-full', chMeta.dot)}
                      />
                      <span className="truncate text-base text-fg-muted">{chMeta.label}</span>
                    </span>
                  </td>

                  {/* 账号 */}
                  <td className="h-9 px-3">
                    <span className="block truncate font-mono text-base text-fg" title={a.name}>
                      {a.name}
                    </span>
                  </td>

                  {/* 每日签到 */}
                  <td className="h-9 px-3 text-center">
                    <span className="inline-flex items-center justify-center">
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(v: boolean | 'indeterminate') =>
                          setSignin((prev) => ({ ...prev, [a.id]: v === true }))
                        }
                        aria-label={`${a.name} 每日签到`}
                      />
                    </span>
                  </td>

                  {/* 当前积分：通用 + Work 明细 */}
                  <td className="h-9 px-3">
                    <span className="flex items-baseline gap-2 tabular">
                      <span className="text-base font-medium text-fg">
                        {fmtCredit(a.credits + a.workCredits)}
                      </span>
                      {a.workCredits > 0 && (
                        <span className="text-sm text-fg-subtle">
                          (通用 {fmtCredit(a.credits)} + Work {fmtCredit(a.workCredits)})
                        </span>
                      )}
                    </span>
                  </td>

                  {/* 账号状态：Badge（颜色 + 文字双重编码） */}
                  <td className="h-9 px-3">
                    <Badge variant={a.status === 'enabled' ? 'success' : a.status === 'disabled' ? 'warning' : 'danger'} dot>
                      {stMeta.label}
                    </Badge>
                  </td>
                </>
              )
            }}
          />
        )}
      </PageBody>

      {/* 底部固定操作栏：三通道添加入口 */}
      <PageFooter>
        <Button variant="outline" onClick={() => void onAdd('Trae')} disabled={busy !== null}>
          <Plus />
          添加 Trae 账号
        </Button>
        <Button variant="outline" onClick={() => void onAdd('WorkBuddy')} disabled={busy !== null}>
          <Plus />
          添加 WorkBuddy 账号
        </Button>
        <Button variant="outline" onClick={() => void onAdd('WorkBuddy_IE')} disabled={busy !== null}>
          <Plus />
          添加 WorkBuddy 国际账号
        </Button>
      </PageFooter>
    </PageShell>
  )
}
