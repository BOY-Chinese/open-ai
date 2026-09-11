import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Download, Info, Power, Trash2 } from 'lucide-react'
import { PageHeader, PageShell } from '@/components/layout/PageShell'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardHeader,
  CardSection,
  CardTitle,
} from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { backend } from '@/lib/dataSource'

/** 版本号统一展示格式：后端返回 "2.4.0" / "v2.4.0" 均归一为 v 前缀 */
function formatVersion(raw: string): string {
  const trimmed = raw.trim()
  return trimmed.startsWith('v') ? trimmed : `v${trimmed}`
}

/**
 * 系统设置页
 *
 * 结构（纵向单列，max-w-3xl 限制超宽）：
 *   1. 启动设置 —— 开机自启动开关
 *   2. 版本信息 —— 当前版本 + 一键更新
 *   3. 危险操作 —— 一键卸载（自带二次确认弹窗）
 */
export function SettingsPage() {
  const { toast } = useToast()

  /* ── 第 1 块：开机自启动 ── */
  const {
    data: autostart,
    loading: autostartLoading,
    setData: setAutostartState,
  } = useAsync<boolean>(() => backend.getAutostart(), [], false)
  const [autostartSaving, setAutostartSaving] = useState(false)

  /**
   * 切换自启动：先乐观改为目标值，写失败则回滚为原值并提示错误。
   * autostartSaving 期间禁用开关，避免连点产生竞态。
   */
  const handleAutostartChange = useCallback(
    async (next: boolean) => {
      const prev = autostart
      setAutostartState(next)
      setAutostartSaving(true)
      try {
        await backend.setAutostart(next)
        toast(next ? '已开启开机自启动' : '已关闭开机自启动', 'success')
      } catch (e) {
        // 回滚 UI 状态，保证界面与真实配置一致
        setAutostartState(prev)
        toast(
          `设置开机自启动失败：${e instanceof Error ? e.message : String(e)}`,
          'error'
        )
      } finally {
        setAutostartSaving(false)
      }
    },
    [autostart, setAutostartState, toast]
  )

  /* ── 第 2 块：版本信息 ── */
  const {
    data: version,
    loading: versionLoading,
  } = useAsync<{ current: string; latest?: string }>(
    () => backend.getVersion(),
    [],
    { current: '' }
  )
  const [updating, setUpdating] = useState(false)
  /** 检查到更新后按钮下次点击的目标版本，空串表示尚未发现新版本 */
  const [pendingVersion, setPendingVersion] = useState('')

  const versionLabel = versionLoading
    ? ''
    : formatVersion(version.current || '未知')

  /** 一键更新：检查 → 有更新则提示并切换按钮文案，全程不阻塞 UI */
  const handleCheckUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      const result = await backend.checkUpdate()
      const latest = formatVersion(result.latest)
      if (!result.hasUpdate) {
        setPendingVersion('')
        toast(`当前已是最新版本 ${latest}`, 'info')
      } else {
        setPendingVersion(latest)
        toast(`发现新版本 ${latest}，开始下载...`, 'success')
      }
    } catch (e) {
      toast(
        `检查更新失败：${e instanceof Error ? e.message : String(e)}`,
        'error'
      )
    } finally {
      setUpdating(false)
    }
  }, [toast])

  /* ── 第 3 块：卸载二次确认 ── */
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [uninstalling, setUninstalling] = useState(false)

  const closeConfirm = useCallback(() => {
    if (uninstalling) return
    setConfirmOpen(false)
  }, [uninstalling])

  /** Esc 关闭确认框；卸载进行中不响应，避免中途关闭造成状态错乱 */
  useEffect(() => {
    if (!confirmOpen) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeConfirm()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirmOpen, closeConfirm])

  /** 确认卸载：模拟后端调用，完成后提示并关闭对话框 */
  const handleConfirmUninstall = useCallback(async () => {
    setUninstalling(true)
    try {
      await new Promise((r) => setTimeout(r, 900))
      toast('卸载请求已提交，请在弹出的窗口中确认', 'warn', 4000)
      setConfirmOpen(false)
    } finally {
      setUninstalling(false)
    }
  }, [toast])

  return (
    <PageShell>
      <PageHeader
        title="系统设置"
        description="管理 open-ai 的启动方式、版本更新与卸载"
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pb-4">
        <div className="max-w-3xl space-y-4">
          {/* ═══════════ 1. 启动设置 ═══════════ */}
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <Power className="size-4 text-fg-muted" />
                <CardTitle>启动设置</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <CardSection
                label="开机自动运行"
                hint="勾选后，每次登录 Windows 自动启动网关和守护进程（无需人工打开）。"
              >
                <div className="flex flex-col items-end gap-2">
                  {autostartLoading ? (
                    <Skeleton className="h-5 w-9 rounded-full" />
                  ) : (
                    <Switch
                      checked={autostart}
                      disabled={autostartSaving}
                      onCheckedChange={(v) => void handleAutostartChange(v)}
                      aria-label="开机自动运行"
                    />
                  )}
                  {!autostartLoading &&
                    (autostart ? (
                      <Badge variant="success" dot>
                        已开启
                      </Badge>
                    ) : (
                      <Badge variant="outline" dot>
                        已关闭
                      </Badge>
                    ))}
                </div>
              </CardSection>
            </CardContent>
          </Card>

          {/* ═══════════ 2. 版本信息 ═══════════ */}
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <Info className="size-4 text-fg-muted" />
                <CardTitle>版本信息</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <CardSection label="当前版本" hint="open-ai 桌面端与网关内核版本">
                <div className="flex items-center gap-2">
                  {versionLoading ? (
                    <Skeleton className="h-5 w-24" />
                  ) : (
                    <>
                      <span className="text-base text-fg-muted">当前版本：</span>
                      <span className="font-mono text-base font-medium text-fg">
                        {versionLabel}
                      </span>
                    </>
                  )}
                </div>
              </CardSection>

              <CardSection
                label="检查更新"
                hint="检查并更新到 GitHub 最新发布版本 (github.com/BOY-Chinese/open-ai/releases)。"
              >
                <Button
                  variant="default"
                  size="lg"
                  loading={updating}
                  onClick={() => void handleCheckUpdate()}
                >
                  {!updating && <Download />}
                  {updating
                    ? '检查中...'
                    : pendingVersion
                      ? `更新到 ${pendingVersion}`
                      : '一键更新'}
                </Button>
              </CardSection>
            </CardContent>
          </Card>

          {/* ═══════════ 3. 危险操作 ═══════════ */}
          <Card className="border-danger/30 bg-bg-card">
            <CardHeader>
              <div className="flex items-center gap-2">
                <AlertTriangle className="size-4 text-danger" />
                <CardTitle className="text-danger">危险操作</CardTitle>
              </div>
            </CardHeader>
            <CardContent>
              <CardSection
                label="一键卸载"
                hint="一键卸载将彻底删除 open-ai（含插件文件、配置、账号与 API 密钥）。"
              >
                <Button
                  variant="danger"
                  size="default"
                  onClick={() => setConfirmOpen(true)}
                >
                  <Trash2 />
                  一键卸载
                </Button>
              </CardSection>
            </CardContent>
          </Card>
        </div>
      </div>

      {/* ═══════════ 卸载二次确认对话框 ═══════════ */}
      {confirmOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 animate-fade-in"
          role="dialog"
          aria-modal="true"
          aria-labelledby="uninstall-title"
          onClick={closeConfirm}
        >
          <div
            className="w-[420px] rounded-lg border border-border bg-bg-card p-6 shadow-popup"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="uninstall-title" className="text-md font-semibold text-fg">
              确认卸载 open-ai？
            </h2>
            <p className="mt-3 text-base text-fg-muted">
              此操作将停止所有进程并删除全部数据（账号、API 密钥、积分记录），且不可撤销。
            </p>
            <div className="mt-6 flex items-center justify-end gap-2">
              <Button
                variant="outline"
                size="default"
                disabled={uninstalling}
                onClick={closeConfirm}
              >
                取消
              </Button>
              <Button
                variant="danger"
                size="default"
                loading={uninstalling}
                onClick={() => void handleConfirmUninstall()}
              >
                {!uninstalling && <Trash2 />}
                确认卸载
              </Button>
            </div>
          </div>
        </div>
      )}
    </PageShell>
  )
}
