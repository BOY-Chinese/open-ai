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
import { UpdateDialog } from '@/components/settings/UpdateDialog'
import { useToast } from '@/components/feedback/Toast'
import { useAsync } from '@/hooks/useAsync'
import { useTheme } from '@/hooks/useTheme'
import { backend } from '@/lib/dataSource'
import { formatVersion } from '@/lib/utils'
import { inTauri } from '@/lib/gateway'
import type { UpdateCheckResult } from '@/types/domain'

/**
 * 版本信息卡片的数据源说明
 *
 * ★ 仓库名**不再**由前端硬编码。此前这里有一个 `const UPDATE_REPO =
 *   'owner/open-ai'` 占位常量，用于「检查更新」的提示文案 —— 结果是
 *   **点检查之前**界面显示的是 `github.com/owner/open-ai/releases`，
 *   一个并不存在的仓库。真实仓库只有后端知道（`version.py` 的 `UPDATE_REPO`，
 *   可被环境变量 `OPEN_AI_UPDATE_REPO` 覆盖），故改由 `GET /v1/admin/version`
 *   下发；拿不到时提示文案里干脆不写地址，而不是编一个出来。
 */

/**
 * 版本号格式化统一在 `lib/utils.ts` 的 {@link formatVersion}（侧边栏与设置页
 * 共用）。此处只负责「拿不到版本号时显示未知」这一层，不再自己拼前缀 ——
 * 曾经本文件里那份私有实现会把 `dev-v3.1` 显示成 `vdev-v3.1`。
 */

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

  /* ── 第 2 块：版本信息 + 一键更新 ── */
  const {
    data: version,
    loading: versionLoading,
    error: versionError,
  } = useAsync<{ current: string; latest?: string; repo?: string }>(
    () => backend.getVersion(),
    [],
    { current: '' }
  )
  const [updating, setUpdating] = useState(false)
  /** 检查结果（含本通道安装包信息）；null 表示尚未检查 */
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResult | null>(null)
  /** 更新对话框开关（下载 / 安装都在对话框内完成） */
  const [updateOpen, setUpdateOpen] = useState(false)

  /**
   * 展示文案：拿到版本号就原样显示（`dev-v3.1`），拿不到就明确写「读取失败」
   * 或留空 —— 不再回落到任何写死的常量（那正是「版本号显示不对」的来源）。
   */
  const versionLabel = versionLoading
    ? ''
    : version.current
      ? formatVersion(version.current)
      : versionError
        ? '读取失败'
        : '未知'

  /**
   * 一键更新第一步：检查是否有**更新的**版本。
   *
   * 判定与「挑哪个安装包」都在后端做（见 admin_api.check_update）：
   *   - 只有 release 版本号严格新于本机才算有更新 —— 同版本（`v3.1` vs
   *     `local-v3.1`）与更旧版本都不再误报，避免「点一次下载一次、装完没变化」；
   *   - 同时按 UPDATE_CHANNEL 匹配安装包，匹配不到就当场说清，
   *     而不是等下载完才发现通道不对。
   *
   * 有更新 → 打开更新对话框（下载进度与安装确认都在里面）；
   * 无更新 → 一条 toast 收尾，不弹窗打扰。
   */
  const handleCheckUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      const result = await backend.checkUpdate()
      setUpdateInfo(result)
      const latest = formatVersion(result.latest)

      if (result.assetMissing) {
        // 有新版本但这条通道没有包：这不是用户的错，必须给可执行的下一步
        toast(
          `发现 ${latest}，但该发布中没有 ${result.channel} 通道的安装包，请稍后再试`,
          'warn',
          6000
        )
        return
      }
      if (!result.hasUpdate) {
        toast(
          result.notes
            ? `检查更新失败：${result.notes}`
            : `当前已是最新版本 ${latest}`,
          result.notes ? 'error' : 'info'
        )
        return
      }
      setUpdateOpen(true)
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
                hint={
                  /* 仓库名取自后端（检查过则用检查结果里的，更权威）；
                     两者都没有时只描述动作，不编造地址 */
                  (() => {
                    const repo = updateInfo?.repo || version.repo
                    return repo
                      ? `检查并更新到 GitHub 最新发布版本 (github.com/${repo}/releases)。`
                      : '检查并更新到 GitHub 最新发布版本。'
                  })()
                }
              >
                <div className="flex items-center gap-2">
                  <Button
                    variant="default"
                    size="lg"
                    loading={updating}
                    onClick={() => void handleCheckUpdate()}
                  >
                    {!updating && <Download />}
                    {updating
                      ? '检查中...'
                      : updateInfo?.hasUpdate
                        ? `更新到 ${formatVersion(updateInfo.latest)}`
                        : '一键更新'}
                  </Button>
                  {/* 已有下载好的安装包时，不重新检查也能直接进安装流程 */}
                  {updateInfo?.hasUpdate && !updating && (
                    <Button
                      variant="outline"
                      size="lg"
                      onClick={() => setUpdateOpen(true)}
                    >
                      查看更新详情
                    </Button>
                  )}
                </div>
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

      {/* ═══════════ 一键更新对话框（下载 → 确认 → 提权安装）═══════════ */}
      {updateOpen && updateInfo && (
        <UpdateDialog
          info={updateInfo}
          onClose={() => setUpdateOpen(false)}
          onVersionKnown={(v) => setUpdateInfo((prev) => (prev ? { ...prev, latest: v } : prev))}
        />
      )}

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
