import { AlertTriangle, FolderOpen, Loader2, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { GatewayPhase } from '@/lib/gateway'

interface GatewayGateProps {
  /** 当前网关阶段：checking / starting 时显示连接中，offline 时显示错误面板 */
  phase: GatewayPhase
  /** 失败原因（offline 时展示，便于在虚拟机上直接定位） */
  reason?: string
  /** open-ai 安装根目录（诊断用，可能为空） */
  root?: string
  /** 是否具备「打开日志目录」的能力（仅桌面端有） */
  canOpenLogs?: boolean
  /** 点击重试 */
  onRetry: () => void
  /** 打开日志目录 */
  onOpenLogs?: () => void
}

/**
 * 网关启动门 —— 后端没就绪时占住内容区，避免 6 个页面各弹一次「无法连接网关」。
 *
 * 设计取舍：错误态必须给出**可执行的下一步**（重试 / 打开日志 / 安装路径），
 * 而不是只丢一句「连接失败」—— 虚拟机排障时用户只能看到界面，
 * 拿不到任何线索就等于把问题丢回给用户。
 */
export function GatewayGate({
  phase,
  reason,
  root,
  canOpenLogs,
  onRetry,
  onOpenLogs,
}: GatewayGateProps) {
  const busy = phase === 'checking' || phase === 'starting'

  const title = busy
    ? phase === 'starting'
      ? '后端未运行，正在自动启动…'
      : '正在连接后端网关…'
    : '无法连接后端网关'

  const hint = busy
    ? phase === 'starting'
      ? '首次启动需要几秒钟，请稍候。'
      : '正在探测 http://127.0.0.1:8000'
    : '界面已就绪，但拿不到后端数据。请按下方信息排查后重试。'

  return (
    <div className="flex h-full w-full items-center justify-center bg-bg-content p-6">
      <div className="w-full max-w-[520px] rounded-lg border border-border bg-bg-card p-6">
        <div className="flex items-start gap-3">
          <span
            className={
              'mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md ' +
              (busy ? 'bg-bg-card-hover text-fg-muted' : 'bg-danger/12 text-danger')
            }
          >
            {busy ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <AlertTriangle className="size-4" />
            )}
          </span>

          <div className="min-w-0 flex-1">
            <h2 className="text-md font-semibold text-fg">{title}</h2>
            <p className="mt-1 text-base text-fg-muted">{hint}</p>

            {!busy && reason && (
              <p className="mt-3 rounded-md border border-border bg-bg-app px-3 py-2 text-sm text-danger">
                {reason}
              </p>
            )}

            {!busy && root && (
              <p className="mt-2 break-all font-mono text-sm text-fg-faint">
                安装目录：{root}
              </p>
            )}

            {!busy && (
              <div className="mt-4 flex items-center gap-2">
                <Button variant="default" onClick={onRetry}>
                  <RefreshCw />
                  重试连接
                </Button>
                {canOpenLogs && (
                  <Button variant="outline" onClick={onOpenLogs}>
                    <FolderOpen />
                    打开日志目录
                  </Button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
