import { useCallback, useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { AlertTriangle, Download, Info, Palette, Power, Trash2 } from 'lucide-react'
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
import { AppearanceDialog, THEME_MODE_LABEL } from '@/components/settings/AppearanceDialog'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { useTheme } from '@/hooks/useTheme'
import { backend } from '@/lib/dataSource'
import { inTauri } from '@/lib/gateway'

/**
 * 发布仓库（owner/repo）—— 仅用于「检查更新」的提示文案。
 *
 * 保持占位值是有意的：仓库归属由发布者决定，把某个人的 GitHub 账号写死进
 * 前端产物就等于把身份信息随安装包公开。发布时改这一处即可。
 * （后端查询最新的仓库名走 version.py 的 UPDATE_REPO，两者改一处要同步。）
 */
const UPDATE_REPO = 'owner/open-ai'

/** 版本号统一展示格式：后端返回 "2.4.0" / "v2.4.0" 均归一为 v 前缀 */
function formatVersion(raw: string): string {
  const trimmed = raw.trim()
  return trimmed.startsWith('v') ? trimmed : `v${trimmed}`
}

/**
 * 系统设置页
 *
 * 结构（纵向单列，max-w-3xl 限制超宽）：
 *   1. 启动设置 —— 开机自启动开关 + 外观设置（白天 / 夜间 / 跟随系统）
 *   2. 版本信息 —— 当前版本 + 一键更新
 *   3. 危险操作 —— 一键卸载（自带二次确认弹窗）
 */
export function SettingsPage() {
  const { toast } = useToast()
  const { mode } = useTheme()

  /* ── 外观设置弹窗开关 ── */
  const [appearanceOpen, setAppearanceOpen] = useState(false)

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

  /**
   * 确认卸载：真正拉起安装目录下的 uninstall.exe。
   *
   * 此前这里是**空壳**（`await new Promise(r => setTimeout(r, 900))` + 一条
   * toast）—— 点了「一键卸载」什么都没发生，用户实测反馈的正是这个。
   *
   * 交给 Tauri 命令而不是 HTTP 路由，是因为卸载器要删掉
   * `<安装根>\desktop\open-ai-desktop.exe` **自己**；界面必须先让出文件占用，
   * 所以命令在启动卸载器后会自行退出桌面端（见 Rust `uninstall_app`）。
   */
  const handleConfirmUninstall = useCallback(async () => {
    if (!inTauri) {
      toast('一键卸载只能在桌面端应用内使用（当前是浏览器预览）', 'warn', 4000)
      return
    }
    setUninstalling(true)
    try {
      await invoke<string>('uninstall_app')
      toast('卸载程序已启动，请按提示完成卸载', 'warn', 5000)
      setConfirmOpen(false)
      // 界面随后会被 Rust 侧退出，这里不重置 uninstalling，
      // 避免退出前按钮闪回可点状态导致重复触发
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), 'error', 6000)
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
            <CardContent className="space-y-4">
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

              {/* 外观设置：入口放在启动设置卡片内，与需求一致 */}
              <CardSection
                label="外观设置"
                hint="选择界面配色：白天（浅色）／夜间（深色）／跟随系统。默认白天模式。"
              >
                <div className="flex items-center gap-2">
                  {/* 回显当前模式，省得为一个只读信息点开弹窗 */}
                  <Badge variant="outline">{THEME_MODE_LABEL[mode]}</Badge>
                  <Button
                    variant="secondary"
                    size="default"
                    onClick={() => setAppearanceOpen(true)}
                  >
                    <Palette />
                    外观设置
                  </Button>
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
                hint={`检查并更新到 GitHub 最新发布版本 (github.com/${UPDATE_REPO}/releases)。`}
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

      {/* ═══════════ 外观设置对话框 ═══════════ */}
      {appearanceOpen && <AppearanceDialog onClose={() => setAppearanceOpen(false)} />}

      {/* ═══════════ 卸载二次确认对话框 ═══════════ */}
      {confirmOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 animate-fade-in"
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
