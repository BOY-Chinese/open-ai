import { useCallback, useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { Download, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useToast } from '@/components/feedback/Toast'
import { backend } from '@/lib/dataSource'
import { inTauri } from '@/lib/gateway'
import { cn, formatBytes } from '@/lib/utils'
import type { UpdateCheckResult, UpdateState } from '@/types/domain'

/** 下载进度轮询间隔：下载本身由后端后台线程做，这里只读内存状态，开销可忽略 */
const POLL_MS = 500

/** 阶段中文名（对话框标题下方的状态行） */
const PHASE_TEXT: Record<UpdateState['phase'], string> = {
  idle: '',
  downloading: '正在下载安装包',
  ready: '安装包已就绪',
  error: '下载失败',
}

export interface UpdateDialogProps {
  /** 已完成的检查结果（按钮文案与版本号都由它决定） */
  info: UpdateCheckResult
  /** 关闭对话框（安装启动成功后由内部调用，避免用户看到残留界面） */
  onClose: () => void
  /** 版本号发生变化（下载完成）时通知设置页刷新显示 */
  onVersionKnown?: (version: string) => void
}

/**
 * 一键更新对话框 —— 从「发现新版本」到「拉起安装包」的完整流程
 *
 * 为什么不做成「点一下全自动」：安装包要提权到管理员，中途会覆盖
 * `gateway.exe` / `.venv` / `desktop\open-ai-desktop.exe`，并让界面与网关
 * 全部退出。这种动作必须让用户先看清「装哪个版本、多大、会发生什么」，
 * 再显式点一次确认 —— 与「一键卸载」的二次确认是同一套理由。
 *
 * 相位（与后端 `/version/update-state` 一致，只覆盖下载）：
 *   idle → downloading → ready →（用户确认 → 提权安装 → 桌面端退出）
 * 安装阶段由本地 `installing` 状态表达：它必须经 Tauri 命令发起，
 * 网关状态机里没有这一相位（见 domain.ts 的 UpdatePhase 说明）。
 * 任一环节失败都停在原地并把原因写清楚，绝不静默继续。
 */
export function UpdateDialog({ info, onClose, onVersionKnown }: UpdateDialogProps) {
  const { toast } = useToast()
  const [state, setState] = useState<UpdateState | null>(null)
  const [starting, setStarting] = useState(false)
  const [installing, setInstalling] = useState(false)
  /** 下载是否已经由本次会话发起（决定要不要轮询，避免无谓请求） */
  const polling = useRef(false)

  const latestLabel = info.latest || '新版本'

  /**
   * 开始下载：交给后端后台线程，这里只启动轮询。
   *
   * ★ 为什么由后端下载而不是浏览器：安装包 34MB（portable 400MB），
   *   fetch 到内存再落盘既慢又吃内存；而 UAC 提权本来也只能由本机进程发起。
   */
  const startDownload = useCallback(async () => {
    setStarting(true)
    try {
      const r = await backend.startUpdateDownload()
      polling.current = true
      setState((prev) => ({
        phase: r.phase,
        percent: 0,
        received: 0,
        total: 0,
        version: r.version || latestLabel,
        path: '',
        error: '',
        ts: Date.now() / 1000,
        ...(prev ?? {}),
      }))
      toast(`开始下载 ${r.version || latestLabel}`, 'info')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setState((prev) => ({
        phase: 'error',
        percent: 0,
        received: 0,
        total: 0,
        version: latestLabel,
        path: '',
        error: msg,
        ts: Date.now() / 1000,
        ...(prev ?? {}),
      }))
    } finally {
      setStarting(false)
    }
  }, [latestLabel, toast])

  /** 下载完成 → 自动进入待安装（不再需要用户再点一次「检查」） */
  useEffect(() => {
    if (state?.phase === 'ready' && state.version) onVersionKnown?.(state.version)
  }, [state?.phase, state?.version, onVersionKnown])

  /* 轮询进度：仅在下载进行中才持续请求，其它相位一次即止 */
  useEffect(() => {
    if (!polling.current) return
    let alive = true
    const tick = async () => {
      try {
        const s = await backend.updateState()
        if (!alive) return
        setState(s)
        if (s.phase === 'downloading') {
          setTimeout(tick, POLL_MS)
        } else {
          polling.current = false
        }
      } catch {
        // 轮询失败不弹错：网关可能正好在重启，下一轮自然会失败并停下
        polling.current = false
      }
    }
    void tick()
    return () => {
      alive = false
    }
  }, [starting])

  /* Esc 关闭：安装已启动后不再响应（界面马上要退出，关闭会造成状态错乱） */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !installing) {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [installing, onClose])

  /**
   * 确认安装：以管理员身份启动安装包，随后界面与网关一起退出。
   *
   * 顺序（Rust 侧 `install_update` 实现，这里只负责触发与回报）：
   *   ① ShellExecuteEx 以 runas 启动 → **UAC 弹窗**；
   *   ② 用户点「是」后：停掉网关 / trae node / 定时任务，让出文件占用；
   *   ③ 桌面端退出 → 安装器覆盖安装 → 完成后用户重新打开界面。
   * 用户在 UAC 上点「否」时 invoke 会当场抛错（ERROR_CANCELLED），
   * 此时什么都不做，界面保持原样。
   */
  const confirmInstall = useCallback(async () => {
    if (!inTauri) {
      toast('一键更新只能在桌面端应用内使用（当前是浏览器预览）', 'warn', 4000)
      return
    }
    if (!state?.path) {
      toast('安装包路径未知，请重新下载', 'error')
      return
    }
    setInstalling(true)
    try {
      await invoke<string>('install_update', { path: state.path })
      toast('安装程序已启动，正在退出 open-ai 以完成安装…', 'warn', 6000)
      // 界面随后会被 Rust 侧退出，这里不重置 installing，
      // 避免退出前按钮闪回可点状态导致重复拉起安装器
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setInstalling(false)
      toast(`启动安装程序失败：${msg}`, 'error', 6000)
    }
  }, [state?.path, toast])

  const phase = state?.phase ?? 'idle'
  const percent = Math.max(0, Math.min(100, state?.percent ?? 0))
  const done = phase === 'ready'
  const failed = phase === 'error'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="update-title"
      onClick={() => !installing && onClose()}
    >
      <div
        className="w-[460px] rounded-lg border border-border bg-bg-card p-6 shadow-popup"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="update-title" className="text-md font-semibold text-fg">
          更新到 {latestLabel}
        </h2>

        {/* ── 版本与包信息 ── */}
        <dl className="mt-3 space-y-1 text-base">
          <div className="flex justify-between gap-4">
            <dt className="text-fg-muted">当前版本</dt>
            <dd className="font-mono text-fg">{info.current || '未知'}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-fg-muted">目标版本</dt>
            <dd className="font-mono text-fg">{latestLabel}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-fg-muted">安装包</dt>
            <dd className="truncate font-mono text-fg" title={info.assetName}>
              {info.assetName || '—'}
              {info.assetSize > 0 && (
                <span className="ml-2 text-fg-subtle">{formatBytes(info.assetSize)}</span>
              )}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-fg-muted">更新通道</dt>
            <dd className="font-mono text-fg">{info.channel || 'dev'}</dd>
          </div>
        </dl>

        {/* ── 下载进度（仅下载/完成相位展示，idle 时不占位）── */}
        {phase !== 'idle' && (
          <div className="mt-4">
            <div className="flex items-center justify-between text-sm">
              <span className={cn('text-fg-muted', failed && 'text-danger')}>
                {failed ? state?.error : PHASE_TEXT[phase]}
              </span>
              {phase === 'downloading' && (
                <span className="font-mono text-fg-subtle">{percent.toFixed(1)}%</span>
              )}
            </div>
            {!failed && (
              <div
                className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-control-off"
                role="progressbar"
                aria-valuenow={Math.round(percent)}
                aria-valuemin={0}
                aria-valuemax={100}
              >
                <div
                  className={cn(
                    'h-full rounded-full transition-[width] duration-slow',
                    done ? 'bg-success' : 'bg-primary'
                  )}
                  style={{ width: `${done ? 100 : percent}%` }}
                />
              </div>
            )}
            {state && state.total > 0 && !failed && (
              <div className="mt-1 text-xs text-fg-subtle">
                {formatBytes(state.received)} / {formatBytes(state.total)}
              </div>
            )}
          </div>
        )}

        {/* ── 安装时的行为说明：必须提前讲清会退出，避免用户以为程序崩了 ── */}
        {done && (
          <div className="mt-4 flex gap-2 rounded-md border border-warning/40 bg-warning-soft/40 p-3">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
            <p className="text-sm text-fg-muted">
              点击「立即安装」后会弹出<strong className="text-fg">管理员授权（UAC）</strong>窗口，
              授权后 open-ai 的<strong className="text-fg">网关与界面将全部退出</strong>，
              由安装包完成覆盖安装；装好后请重新打开 open-ai。
              <br />
              <span className="text-fg-subtle">
                已有账号与配置会被保留（安装器只备份不覆盖 config.json）。
              </span>
            </p>
          </div>
        )}

        {/* ── 操作区 ── */}
        <div className="mt-6 flex items-center justify-end gap-2">
          {phase === 'idle' && (
            <>
              <Button variant="outline" size="default" onClick={onClose}>
                稍后再说
              </Button>
              <Button
                variant="default"
                size="default"
                loading={starting}
                onClick={() => void startDownload()}
              >
                {!starting && <Download />}
                下载安装包
              </Button>
            </>
          )}

          {phase === 'downloading' && (
            <Button variant="outline" size="default" onClick={onClose}>
              后台下载（可关闭本窗口）
            </Button>
          )}

          {failed && (
            <>
              <Button variant="outline" size="default" onClick={onClose}>
                关闭
              </Button>
              <Button
                variant="default"
                size="default"
                loading={starting}
                onClick={() => void startDownload()}
              >
                {!starting && <RefreshCw />}
                重试
              </Button>
            </>
          )}

          {done && (
            <>
              <Button
                variant="outline"
                size="default"
                disabled={installing}
                onClick={onClose}
              >
                稍后安装
              </Button>
              <Button
                variant="default"
                size="default"
                loading={installing}
                onClick={() => void confirmInstall()}
              >
                {!installing && <ShieldCheck />}
                立即安装
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}