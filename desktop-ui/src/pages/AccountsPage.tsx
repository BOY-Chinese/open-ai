import { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus, RefreshCw, Link2, Users, Check } from 'lucide-react'
import { PageShell, PageHeader, PageToolbar, PageBody, PageFooter, ToolbarDivider } from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { TableSkeleton, Skeleton } from '@/components/ui/skeleton'
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
import { LoomyLoginDialog } from '@/components/accounts/LoomyLoginDialog'
import { useAsync } from '@/hooks/useAsync'
import { onAccountsChanged } from '@/lib/accountEvents'
import { backend } from '@/lib/dataSource'
import { fmtCredit, cn, fmtTime } from '@/lib/utils'
import {
  CHANNELS,
  EMPTY_SIGNIN,
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
  { key: 'name', label: '账号', width: 310 },
  { key: 'signin', label: '每日签到', width: 112, align: 'center' as const },
  { key: 'credits', label: '当前积分', width: 248 },
  { key: 'status', label: '账号状态', width: 100 },
]

const FILTER_LABEL: Record<ChannelFilter, string> = {
  all: '全部通道',
  Trae: 'Trae',
  WorkBuddy: 'WorkBuddy',
  WorkBuddy_IE: 'WorkBuddy 国际',
  Loomy: 'Loomy',
}

export function AccountsPage() {
  const { toast } = useToast()
  const [filter, setFilter] = useState<ChannelFilter>('all')
  const [busy, setBusy] = useState<null | 'refresh' | 'reconnect'>(null)

  /**
   * 正在等待登录脚本结束的通道。
   *
   * 用途有二：① 防止连点「添加账号」拉起多个登录助手 —— 后拉起的那次会把网关侧
   * 的 Popen 句柄顶掉，前一次的跟随就再也判断不出「还在跑吗」；② 让按钮上有个
   * 明确的「登录中…」，否则点完除了 toast 没有任何进行中的迹象。
   */
  const [loginBusy, setLoginBusy] = useState<Partial<Record<Channel, boolean>>>({})

  /** Loomy 图形化登录向导开关（点「添加 Loomy 账号」弹出，全程无命令行） */
  const [loomyDialog, setLoomyDialog] = useState(false)

  const { data: accounts, loading, reload, setData } = useAsync(
    () => backend.listAccounts(filter),
    [filter],
    [] as Account[]
  )

  /**
   * 今日签到结果（只读，本地流水库，毫秒级）。
   *
   * 单独一个 useAsync 而不是塞进 listAccounts：
   *  - 两者的失败影响不同 —— 签到表拿不到时表格照常可用，只是这一列显示「—」；
   *  - 两者刷新时机不同 —— 「刷新账号」要联网拉余额，签到表不必跟着等。
   *
   * ★ `error` 必须接住。接口不可用时 `data` 会停在 EMPTY_SIGNIN，
   *   而「表里没有这个账号」在当前实现里就等于「未签到」—— 不区分的话，
   *   网关没重启（新接口还没上线）会被渲染成「今天所有账号都没签到」，
   *   一个看起来完全正常的错误结论。故此处把失败单独标出来显示为「—」。
   */
  const {
    data: signin,
    loading: signinLoading,
    error: signinError,
    reload: reloadSignin,
    // 补签接口直接回读签到表（省一次 GET），用它就地替换
    setData: setSignin,
  } = useAsync(() => backend.listSignin(filter), [filter], EMPTY_SIGNIN)

  /* 从全量数据推导筛选下拉的计数，让"全部"也能显示总数 */
  const counts = useMemo(() => {
    const map: Record<ChannelFilter, number> = {
      all: accounts.length,
      Trae: 0,
      WorkBuddy: 0,
      WorkBuddy_IE: 0,
      Loomy: 0,
    }
    accounts.forEach((a) => {
      map[a.channel] += 1
    })
    return map
  }, [accounts])

  /**
   * 刷新当前筛选通道：拉余额 + **给未签到账号补一次签到** + 重读签到表。
   *
   * ★ 补签（refreshSignin）是这里的关键一步，此前缺失 —— 原实现只有两个
   *   「读」动作：credits/refresh 拉余额、listSignin 重读签到表。于是
   *   「今天还没签到的账号」在用户点刷新后依然是未签到，只能干等后台
   *   每 30 分钟一轮的补签；用户看到的却是「我刚点过刷新了，怎么还没签到」。
   *   现在由后端判定「谁还没签」并立即补（脚本幂等，已签/活动未参与为终态）。
   *
   * 顺序固定为「先补签、后拉余额」：签到会真实入账，先补签拿到的余额才是
   * 入账后的值，否则余额里会缺掉刚签的那一笔。
   *
   * 失败处理刻意**不对称**：
   *   - 补签失败 → 只在文案里说明，列表与余额照常刷新（签到是尽力而为，
   *     上游繁忙 9074 是常态，不该让整个刷新报错、更不该回滚已刷新的余额）；
   *   - 拉余额失败 → 抛出，由外层 toast 出来（它才是这次点击的主目标）。
   */
  const onRefresh = useCallback(async () => {
    setBusy('refresh')
    let signinNote = ''
    try {
      try {
        const r = await backend.refreshSignin(filter)
        setSignin(r.signin)
        if (r.pending.length > 0) {
          // 补签后仍未出现在签到表里的账号 —— 上游没签上（繁忙/活动未参与）。
          // ★ 用 `in` 判存在而不是判真假：签到项的 value 可能是空对象（假值），
          //   用 `!obj[id]` 会把「已签到」误报成「未签上」，提示文案就说谎了。
          const stillPending = r.pending.filter(
            (id) => !(id in r.signin.signin)
          )
          signinNote =
            stillPending.length > 0
              ? `；${stillPending.length} 个账号本次未签上，稍后自动补试`
              : `；已补签 ${r.pending.length} 个账号`
        }
      } catch (e) {
        signinNote = `；签到未能执行（${e instanceof Error ? e.message : String(e)}）`
        console.warn('[accounts] 补签失败：', e)
      }
      const next = await backend.refreshAccounts(filter)
      setData(next)
      await reloadSignin()
      toast(`已刷新 ${FILTER_LABEL[filter]} 账号积分${signinNote}`, 'success', 5000)
    } catch (e) {
      toast(e instanceof Error ? e.message : '刷新失败', 'error')
    } finally {
      setBusy(null)
    }
  }, [filter, setData, reloadSignin, toast])

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

  /**
   * 拉起登录助手并立即返回。
   *
   * `addAccount` 要等登录脚本结束才 resolve（这样操作日志里才是一个完整的
   * 操作块，见 `httpBackend.addAccount`），所以这里**刻意不 await** ——
   * 用户可能在浏览器里填两分钟密码，界面不该锁在那儿。
   * 脚本结束后由 `onAccountsChanged` 自动刷新列表与签到状态。
   *
   * 也不调 `reload()`：此刻 config.json 还没变，刷新只会让表格闪一次骨架屏。
   */
  const launchLogin = useCallback(
    (channel: Channel, verb: string) => {
      setLoginBusy((s) => ({ ...s, [channel]: true }))
      void backend
        .addAccount(channel)
        .catch((e) => {
          toast(
            `${verb}失败：${e instanceof Error ? e.message : String(e)}`,
            'error',
            6000
          )
        })
        // 用 finally 而不是等 onAccountsChanged：follow 自身出错时不会发事件，
        // 只靠事件会让按钮永远停在「登录中…」
        .finally(() => setLoginBusy((s) => ({ ...s, [channel]: false })))
      toast(
        `已拉起 ${channelMeta(channel).label} 登录助手，请在浏览器完成登录`,
        'success',
        4000
      )
    },
    [toast]
  )

  const onReadd = useCallback(
    (acc: Account) => {
      // Loomy: 登录态过期时走图形化验证码向导重新登录（覆盖同手机号的
      // 桌面+Web 两条记录，界面合并为一行，见 admin_api._loomy_groups）
      if (acc.channel === 'Loomy') {
        setLoomyDialog(true)
        return
      }
      launchLogin(acc.channel, '重新添加账号')
    },
    [launchLogin]
  )

  const onAdd = useCallback(
    (channel: Channel) => {
      // Loomy 是手机号短信验证码登录（讯飞账号服务，桌面通道），不是浏览器
      // 交互 —— 弹出图形化向导，用户只填手机号和验证码，全程无命令行。
      // 走桌面通道是因为只有它的 session 能鉴权对话、积分记在本账号。
      if (channel === 'Loomy') {
        setLoomyDialog(true)
        return
      }
      launchLogin(channel, '添加账号')
    },
    [launchLogin]
  )

  /**
   * 登录脚本在后台结束时（见 `lib/loginStream`）自动刷新列表与签到状态。
   *
   * 没有这一步，用户完成登录后必须自己想起「点一下刷新账号」才会看到新账号 ——
   * v2.3 的 GUI 是脚本跑完直接刷新的，这里补上同样的闭环。
   */
  useEffect(() => {
    return onAccountsChanged((channel) => {
      // 只看当前筛选相关的变化，免得在「Trae」筛选下被 WorkBuddy 的登录打断刷新
      if (filter !== 'all' && channel !== filter) return
      void reload()
      void reloadSignin()
      toast('检测到账号池变化，列表已自动刷新', 'info')
    })
  }, [filter, reload, reloadSignin, toast])

  return (
    <PageShell>
      <PageHeader
        title="账号管理"
        description="Trae / WorkBuddy / WorkBuddy 国际 / Loomy 四通道统一视图"
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
        <span className="text-xs leading-tight text-fg-faint">
          Loomy 登录态过期时「重新连接」无效，请直接重新添加账号
        </span>

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
              const sg = signin.signin[a.id]
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

                  {/*
                    每日签到 —— 只读状态列，回答「该账号今天签到成功了吗」。

                    语义纠正：这里**不是**「是否要每日签到」的开关。
                    此前渲染成 Checkbox 且初值取 `a.enabled`，把「账号是否启用」
                    当成了「今日是否签到」—— 两件事毫无关系，勾选还会写进一个
                    纯本地的 useState，既不落库也不触发任何签到动作，纯属误导。
                    现在改为读取后端流水库的当日入账凭证（见 SigninStatus）。
                  */}
                  <td className="h-9 px-3 text-center">
                    {signinLoading ? (
                      <Skeleton className="mx-auto h-5 w-14" />
                    ) : signinError ? (
                      /* 读不到 ≠ 没签到：明确标成未知，避免把接口故障渲染成「今天都没签」 */
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="inline-flex justify-center text-base text-fg-faint">
                            —
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          签到状态读取失败：{signinError}
                        </TooltipContent>
                      </Tooltip>
                    ) : (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="inline-flex justify-center">
                            {sg ? (
                              <Badge variant="success" dot>
                                已签到
                              </Badge>
                            ) : a.channel === 'WorkBuddy_IE' ? (
                              /* 国际版无签到渠道 (2026-09-13 官方确认): gain 表恒无记录,
                                 显示「未签到」是误导 —— 按无渠道语义渲染「—」。
                                 (Loomy 已接入按天状态缓存 data/loomy_signin_state.json,
                                  有「今日已领」凭证, 走正常渲染, 不再按无渠道处理。) */
                              <span className="inline-flex justify-center text-base text-fg-faint">
                                —
                              </span>
                            ) : (
                              <Badge variant="outline">未签到</Badge>
                            )}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          {sg
                            ? `今日已签到成功：入账 ${fmtCredit(sg.amount)} 积分（${sg.kinds.join('、')}）· ${fmtTime(sg.ts)}`
                            : a.channel === 'WorkBuddy_IE'
                              ? 'WorkBuddy 国际版暂无签到渠道，无每日签到积分'
                              : a.channel === 'Loomy'
                                ? '今日尚未领取每日登录积分；后台每日自动领取，可稍后刷新查看'
                                : `今日尚未签到成功${signin.day ? `（统计日 ${signin.day}）` : ''}；后台每日自动签到，可稍后刷新查看`}
                        </TooltipContent>
                      </Tooltip>
                    )}
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

      {/* 底部固定操作栏：四通道添加入口 */}
      <PageFooter>
        {(
          [
            ['Trae', '添加 Trae 账号'],
            ['WorkBuddy', '添加 WorkBuddy 账号'],
            ['WorkBuddy_IE', '添加 WorkBuddy 国际账号'],
            ['Loomy', '添加 Loomy 账号'],
          ] as [Channel, string][]
        ).map(([ch, label]) => (
          <Button
            key={ch}
            variant="outline"
            onClick={() => onAdd(ch)}
            disabled={busy !== null || loginBusy[ch] === true}
            loading={loginBusy[ch] === true}
            title={
              ch === 'Loomy'
                ? '使用手机号验证码登录你自己的 Loomy 账号'
                : loginBusy[ch]
                  ? '登录助手正在运行，请在浏览器完成登录'
                  : `拉起 ${channelMeta(ch).label} 登录助手`
            }
          >
            {!loginBusy[ch] && <Plus />}
            {loginBusy[ch] ? '登录中…' : label}
          </Button>
        ))}
      </PageFooter>

      {/* Loomy 图形化登录向导（手机号 → 验证码 → 完成，全程无命令行） */}
      {loomyDialog && (
        <LoomyLoginDialog
          onClose={() => {
            setLoomyDialog(false)
            void reload()
          }}
        />
      )}
    </PageShell>
  )
}
