import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Copy, KeyRound, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { PageShell, PageHeader, PageBody, PageFooter } from '@/components/layout/PageShell'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton, TableSkeleton } from '@/components/ui/skeleton'
import { TableEmpty } from '@/components/ui/table'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { VirtualTable } from '@/components/data/VirtualTable'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'
import { fmtTime, maskKey } from '@/lib/utils'
import type { ApiKey, GatewayInfo } from '@/types/domain'

/* 列宽必须显式给 px：虚拟滚动模式下靠它计算总宽 */
const COLUMNS = [
  { key: 'name', label: '名称', width: 200 },
  { key: 'key', label: 'API密钥', width: 420 },
  { key: 'createdAt', label: '创建时间', width: 160 },
]

const EMPTY_GATEWAY: GatewayInfo = { openaiBase: '', chatEndpoint: '', anthropicBase: '' }

export function ApiPage() {
  const { toast } = useToast()

  /** 正在执行的操作（统一驱动按钮 loading，避免多处 loading 状态打架） */
  const [busy, setBusy] = useState<null | 'refresh' | 'create' | 'rename' | 'delete'>(null)
  /** 当前明文展示密钥的行 id（悬停时短暂解除掩码） */
  const [revealedId, setRevealedId] = useState<string | null>(null)
  /** 改名 / 删除对话框的目标行，null 表示未打开 */
  const [renameTarget, setRenameTarget] = useState<ApiKey | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ApiKey | null>(null)

  const { data: gateway, loading: gatewayLoading } = useAsync(
    () => backend.getGateway(),
    [],
    EMPTY_GATEWAY
  )

  const {
    data: keys,
    loading: keysLoading,
    reload: reloadKeys,
  } = useAsync(() => backend.listApiKeys(), [], [] as ApiKey[])

  /** 复制到剪贴板；失败（无权限/非安全上下文）时降级提示，不静默失败 */
  const copyText = useCallback(
    async (text: string, okText: string) => {
      if (!text) {
        toast('没有可复制的内容', 'warn')
        return
      }
      try {
        await navigator.clipboard.writeText(text)
        toast(okText, 'success')
      } catch {
        toast('复制失败，请手动选择文本', 'error')
      }
    },
    [toast]
  )

  /** 刷新列表：表格区照常可用，不阻塞交互 */
  const onRefresh = useCallback(async () => {
    setBusy('refresh')
    try {
      await reloadKeys()
      toast('API 列表已刷新', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '刷新失败', 'error')
    } finally {
      setBusy(null)
    }
  }, [reloadKeys, toast])

  const onCreate = useCallback(async () => {
    setBusy('create')
    try {
      const created = await backend.createApiKey()
      await reloadKeys()
      toast(`已创建 API 密钥：${created.name}`, 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '创建失败', 'error')
    } finally {
      setBusy(null)
    }
  }, [reloadKeys, toast])

  const onRename = useCallback(
    async (id: string, name: string, key: string) => {
      setBusy('rename')
      try {
        // 真实网关按 key 定位（name 可变，不能作标识）
        await backend.renameApiKey(id, name, key)
        await reloadKeys()
        setRenameTarget(null)
        toast('已重命名', 'success')
      } catch (e) {
        toast(e instanceof Error ? e.message : '重命名失败', 'error')
      } finally {
        setBusy(null)
      }
    },
    [reloadKeys, toast]
  )

  const onDelete = useCallback(
    async (id: string, key: string) => {
      setBusy('delete')
      try {
        await backend.deleteApiKey(id, key)
        await reloadKeys()
        setDeleteTarget(null)
        setRevealedId(null)
        toast('已删除 API 密钥', 'success')
      } catch (e) {
        toast(e instanceof Error ? e.message : '删除失败', 'error')
      } finally {
        setBusy(null)
      }
    },
    [reloadKeys, toast]
  )

  /** 地址行：无复制按钮，右键菜单承担复制；悬停给边框反馈 */
  const renderAddressRow = useCallback(
    (label: string, value: string) => (
      <div className="flex items-center gap-3">
        <span className="w-[160px] shrink-0 text-sm text-fg-muted">{label}</span>
        {gatewayLoading ? (
          <Skeleton className="h-4 w-64" />
        ) : (
          <ContextMenu>
            <ContextMenuTrigger asChild>
              <div
                className="min-w-0 flex-1 cursor-context-menu rounded-md border border-border bg-bg-input px-3 py-1.5 transition-colors duration-fast hover:border-border-strong"
                title="右键复制地址"
              >
                <span className="selectable block truncate font-mono text-base text-fg">
                  {value}
                </span>
              </div>
            </ContextMenuTrigger>
            <ContextMenuContent>
              <ContextMenuLabel>{label}</ContextMenuLabel>
              <ContextMenuSeparator />
              <ContextMenuItem onSelect={() => void copyText(value, '已复制地址')}>
                <Copy />
                复制地址
              </ContextMenuItem>
            </ContextMenuContent>
          </ContextMenu>
        )}
      </div>
    ),
    [copyText, gatewayLoading]
  )

  const totalWidth = useMemo(
    () => COLUMNS.reduce((sum, c) => sum + c.width, 0),
    []
  )

  return (
    <PageShell>
      <PageHeader
        title="API 管理"
        description="配置网关接入地址，管理调用凭证；右键表格行可改名、复制或删除"
      />

      {/* 上半部分：网关地址（仅展示 + 右键复制，保持界面干净） */}
      <Card className="mb-4 shrink-0">
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>网关地址</CardTitle>
            <CardDescription className="mt-1">接口接入信息</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {renderAddressRow('OpenAI 兼容地址', gateway.openaiBase)}
          {renderAddressRow('对话端点', gateway.chatEndpoint)}
          {renderAddressRow('Anthropic 兼容地址 (Claude)', gateway.anthropicBase)}
          <p className="pt-1 text-sm text-fg-subtle">
            Claude Code / CC Switch 把 ANTHROPIC_BASE_URL 指向 Anthropic
            兼容地址，OpenAI 客户端把 Base URL 指向 OpenAI 兼容地址即可。
          </p>
        </CardContent>
      </Card>

      {/* 下半部分：API 列表 */}
      <div className="mb-3 shrink-0 text-md font-semibold text-fg">API 列表</div>

      <PageBody className="overflow-auto">
        {keysLoading ? (
          <TableSkeleton rows={5} cols={COLUMNS.length} />
        ) : (
          <VirtualTable
            rows={keys}
            columns={COLUMNS}
            rowKey={(k) => k.id}
            threshold={50}
            empty={
              <TableEmpty
                colSpan={COLUMNS.length}
                icon={KeyRound}
                title="暂无 API 密钥"
                description="点击下方「+ 创建 API」生成第一个密钥"
              />
            }
            /* 行右键菜单：改名 / 复制密钥 / 删除 */
            wrapRow={(k, _i, content) => (
              <ContextMenu key={k.id}>
                <ContextMenuTrigger asChild>{content}</ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuLabel>{k.name}</ContextMenuLabel>
                  <ContextMenuSeparator />
                  <ContextMenuItem onSelect={() => setRenameTarget(k)}>
                    <Pencil />
                    命名/改名
                  </ContextMenuItem>
                  <ContextMenuItem onSelect={() => void copyText(k.key, '已复制密钥')}>
                    <Copy />
                    复制密钥
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem destructive onSelect={() => setDeleteTarget(k)}>
                    <Trash2 />
                    删除
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            )}
            /* 鼠标进入整行即解除掩码，离开恢复：与 Tooltip 双保险确保"悬停可见" */
            rowProps={(k) => ({
              onMouseEnter: () => setRevealedId(k.id),
              onMouseLeave: () => setRevealedId((prev) => (prev === k.id ? null : prev)),
              onContextMenu: () => setRevealedId(k.id),
            })}
            renderRow={(k) => {
              const revealed = revealedId === k.id
              const display = revealed ? k.key : maskKey(k.key)
              return (
                <>
                  <td className="px-3">
                    <span className="block truncate text-base text-fg" title={k.name}>
                      {k.name}
                    </span>
                  </td>
                  <td className="px-3">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span
                          className="selectable block cursor-context-menu truncate font-mono text-base text-fg-muted"
                          title={k.key}
                        >
                          {display}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top" align="start">
                        {k.key}
                      </TooltipContent>
                    </Tooltip>
                  </td>
                  <td className="px-3 tabular">
                    <span className="text-base text-fg-subtle">{fmtTime(k.createdAt)}</span>
                  </td>
                </>
              )
            }}
          />
        )}
      </PageBody>

      {/* 底部固定操作栏 */}
      <PageFooter>
        <Button
          variant="outline"
          onClick={() => void onRefresh()}
          loading={busy === 'refresh'}
        >
          <RefreshCw className={busy === 'refresh' ? 'animate-spin' : undefined} />
          刷新
        </Button>
        <Button variant="default" onClick={() => void onCreate()} loading={busy === 'create'}>
          <Plus />
          + 创建 API
        </Button>
      </PageFooter>

      {renameTarget && (
        <RenameDialog
          key={renameTarget.id}
          initialName={renameTarget.name}
          saving={busy === 'rename'}
          onCancel={() => setRenameTarget(null)}
          onConfirm={(name) => void onRename(renameTarget.id, name, renameTarget.key)}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="删除 API 密钥"
          message={`确认删除 API「${deleteTarget.name}」？此操作不可撤销`}
          confirmText="删除"
          danger
          busy={busy === 'delete'}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => void onDelete(deleteTarget.id, deleteTarget.key)}
        />
      )}

      {/* 隐藏占位：确保列宽总和被引用（虚拟模式依赖显式宽度） */}
      <span className="hidden" style={{ width: totalWidth }} aria-hidden />
    </PageShell>
  )
}

/* ═══════════ 行内对话框（自实现，不引入新依赖） ═══════════ */

/** 遮罩 + 居中卡片；Esc 关闭、Enter 确认由调用方按语义决定 */
function ModalShell({
  title,
  children,
  footer,
  onClose,
  labelledBy,
}: {
  title: string
  children: ReactNode
  footer: ReactNode
  onClose: () => void
  labelledBy: string
}) {
  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 animate-fade-in"
      onMouseDown={onClose}
      role="presentation"
    >
      <div
        className="w-[380px] rounded-lg border border-border-strong bg-bg-card shadow-popup"
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
      >
        <div className="px-4 py-3">
          <h2 id={labelledBy} className="text-md font-semibold text-fg">
            {title}
          </h2>
        </div>
        <div className="px-4 pb-4">{children}</div>
        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {footer}
        </div>
      </div>
    </div>
  )
}

/** 重命名：Enter 确认 / Esc 取消，按钮与键盘行为一致 */
function RenameDialog({
  initialName,
  saving,
  onCancel,
  onConfirm,
}: {
  initialName: string
  saving: boolean
  onCancel: () => void
  onConfirm: (name: string) => void
}) {
  const [name, setName] = useState(initialName)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  const trimmed = name.trim()
  const canSave = trimmed.length > 0 && !saving

  const submit = () => {
    if (canSave) onConfirm(trimmed)
  }

  return (
    <ModalShell
      title="重命名 API 密钥"
      labelledBy="api-rename-title"
      onClose={onCancel}
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>
            取消
          </Button>
          <Button variant="default" loading={saving} disabled={!canSave} onClick={submit}>
            保存
          </Button>
        </>
      }
    >
      <label className="mb-2 block text-sm text-fg-muted" htmlFor="api-rename-input">
        名称
      </label>
      <Input
        id="api-rename-input"
        ref={inputRef}
        value={name}
        placeholder="请输入名称"
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            submit()
          } else if (e.key === 'Escape') {
            e.preventDefault()
            onCancel()
          }
        }}
      />
    </ModalShell>
  )
}

/** 删除确认：文案明确「不可撤销」，危险按钮用 danger 变体 */
function ConfirmDialog({
  title,
  message,
  confirmText,
  danger,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string
  message: string
  confirmText: string
  danger?: boolean
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  // Esc 取消；Enter 确认（busy 时忽略，防止重复提交）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
      if (e.key === 'Enter' && !busy) onConfirm()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onCancel, onConfirm])

  return (
    <ModalShell
      title={title}
      labelledBy="api-confirm-title"
      onClose={onCancel}
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>
            取消
          </Button>
          <Button
            variant={danger ? 'danger' : 'default'}
            loading={busy}
            onClick={onConfirm}
          >
            {confirmText}
          </Button>
        </>
      }
    >
      <p className="text-base text-fg-muted">{message}</p>
    </ModalShell>
  )
}
