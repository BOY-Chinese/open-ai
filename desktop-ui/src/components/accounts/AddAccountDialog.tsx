import { useEffect, useRef } from 'react'
import { Plus, X, ShieldCheck, KeyRound, Phone } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { channelMeta, type Channel } from '@/types/domain'

/**
 * 「添加账号」通道选择弹窗 —— 一次点击，自选通道。
 *
 * 背景（2026-10，master 指定）：底部原先并排四个「添加 <通道> 账号」按钮，
 * 通道加到七个之后这排按钮会挤满整行、还把「哪个通道用哪种登录方式」这个
 * 关键差别藏进了按钮文案里。现在收敛成一个入口：
 *
 *   点「添加账号」→ 弹本弹窗 → 每个通道一行（图标 + 名称 + 登录方式说明）
 *   → 选中即关闭弹窗并拉起对应登录流程。
 *
 * 登录方式的三种形态直接写在行里，用户不用猜：
 *   - 浏览器授权（Qoder 设备授权 / CodeArts 华为云 OAuth2 / ###、WorkBuddy 扫码）
 *   - 手机号验证码（Loomy 讯飞账号服务 —— 全程无命令行，走图形化向导）
 */
export interface AddAccountDialogProps {
  /** 正在登录中的通道（按钮显示「登录中…」并禁用，避免连点拉起多个登录助手） */
  busy?: Partial<Record<Channel, boolean>>
  onClose: () => void
  onPick: (channel: Channel) => void
}

type LoginKind = 'browser' | 'phone'

interface Option {
  channel: Channel
  kind: LoginKind
  hint: string
}

/**
 * 通道清单（顺序即弹窗里的展示顺序）。
 *
 * ★ 与 types/domain.ts 的 CHANNELS 分开维护是有意的：这张表多了「登录方式」
 *   这一前端专属信息，而且顺序按「用户最常用 → 最少用」排，不必与筛选下拉
 *   的通道顺序一致。**通道名本身仍必须与后端 CHANNEL_TO_PROVIDER 逐字一致**。
 */
const OPTIONS: Option[] = [
  { channel: 'Trae', kind: 'browser', hint: '浏览器扫码 / OIDC 授权（Work 积分通道）' },
  { channel: 'WorkBuddy', kind: 'browser', hint: '浏览器扫码登录腾讯 WorkBuddy' },
  { channel: 'WorkBuddy_IE', kind: 'browser', hint: '浏览器登录 www.workbuddy.ai（国际版）' },
  { channel: 'QoderCN', kind: 'browser', hint: '浏览器设备授权 qoder.com.cn，登录后自动写回凭据' },
  { channel: 'QoderIntl', kind: 'browser', hint: '浏览器设备授权 qoder.com（国际版）' },
  { channel: 'CodeArts', kind: 'browser', hint: '浏览器登录华为云，OAuth2 授权换取 STS 凭证' },
  { channel: 'Loomy', kind: 'phone', hint: '手机号 + 短信验证码（无需命令行）' },
]

export function AddAccountDialog({ busy, onClose, onPick }: AddAccountDialogProps) {
  const ref = useRef<HTMLDivElement>(null)

  /* Esc 关闭 + 点击遮罩关闭：弹窗是「选一个就走」的轻交互，不该有额外确认步骤 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onMouseDown={(e) => {
        // 只认「按在遮罩上」的点击 —— 弹窗内部拖选文字后松开鼠标会冒泡到遮罩，
        // 不判 e.target 会把「选字」误当「点外面」直接关掉弹窗
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label="选择要添加账号的通道"
        className="w-full max-w-lg overflow-hidden rounded-lg border border-border bg-bg-card shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
          <div>
            <h2 className="text-base font-medium text-fg">添加账号</h2>
            <p className="mt-0.5 text-xs text-fg-subtle">
              选择要添加哪个通道的账号，登录助手会在浏览器里完成授权
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded p-1 text-fg-subtle transition-colors hover:bg-bg-card-hover hover:text-fg"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="max-h-[60vh] overflow-y-auto p-2">
          {OPTIONS.map((o) => {
            const meta = channelMeta(o.channel)
            const loading = busy?.[o.channel] === true
            return (
              <button
                key={o.channel}
                type="button"
                disabled={loading}
                onClick={() => onPick(o.channel)}
                className="flex w-full items-start gap-3 rounded-md px-3 py-2.5 text-left transition-colors hover:bg-bg-card-hover disabled:cursor-not-allowed disabled:opacity-60"
              >
                <span
                  aria-hidden
                  className={`mt-1.5 size-2 shrink-0 rounded-full ${meta.dot}`}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2">
                    <span className="text-sm font-medium text-fg">{meta.label}</span>
                    {o.kind === 'phone' ? (
                      <Phone className="size-3 text-fg-faint" />
                    ) : (
                      <KeyRound className="size-3 text-fg-faint" />
                    )}
                    {loading && (
                      <span className="text-xs text-fg-subtle">登录中…</span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-xs leading-relaxed text-fg-subtle">
                    {o.hint}
                  </span>
                </span>
                {!loading && <Plus className="mt-1 size-4 shrink-0 text-fg-faint" />}
              </button>
            )
          })}
        </div>

        <div className="flex items-center gap-2 border-t border-border-subtle bg-bg-app/40 px-4 py-2.5">
          <ShieldCheck className="size-3.5 shrink-0 text-fg-faint" />
          <span className="text-xs leading-relaxed text-fg-subtle">
            凭据只写入本机 config.json，不会上传到任何第三方服务器
          </span>
          <Button variant="outline" className="ml-auto" onClick={onClose}>
            取消
          </Button>
        </div>
      </div>
    </div>
  )
}
