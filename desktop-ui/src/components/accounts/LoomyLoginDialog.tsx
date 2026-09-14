import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, MessageSquareText, Smartphone } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useToast } from '@/components/feedback/Toast'
import { backend } from '@/lib/dataSource'

/**
 * Loomy 图形化登录向导
 *
 * 面向只会图形界面的小白用户：输手机号 → 收短信 → 输验证码 → 完成。
 * 全程不出现命令行；本账号 session 由网关写入 config.json，账号列表自动刷新。
 *
 * 走**桌面通道**（讯飞账号服务）：只有它签发的 session 能鉴权对话，积分记在
 * 本账号自己头上。Web 通道的 cookie 只能查余额，所以不作为默认路径。
 *
 * 步骤状态机：phone → code → done（done 停留 1.5s 自动关闭）。
 * 验证码 60s 倒计时内禁止重发（与主流 App 的短信登录一致，降低困惑）。
 */
export function LoomyLoginDialog({ onClose }: { onClose: () => void }) {
  const { toast } = useToast()
  const [step, setStep] = useState<'phone' | 'code' | 'done'>('phone')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [messageId, setMessageId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [countdown, setCountdown] = useState(0)

  const phoneValid = /^1\d{10}$/.test(phone)
  const codeValid = /^\d{6}$/.test(code)

  /* 60s 倒计时：每秒一次 setTimeout 链，比 interval 少一次清理心智 */
  useEffect(() => {
    if (countdown <= 0) return
    const t = window.setTimeout(() => setCountdown((c) => c - 1), 1000)
    return () => window.clearTimeout(t)
  }, [countdown])

  /* Esc 关闭 */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const sendCode = useCallback(async () => {
    if (!phoneValid || busy || countdown > 0) return
    setBusy(true)
    setError('')
    try {
      const r = await backend.sendLoomyDesktopCode(phone)
      if (!r.ok) {
        setError(r.error || '验证码发送失败，请稍后再试')
        return
      }
      setMessageId(r.messageId ?? '')
      setCountdown(60)
      setStep('code')
      toast('验证码已发送，请查收短信', 'success')
    } catch (e) {
      setError(e instanceof Error ? e.message : '网络异常，请确认网关运行中')
    } finally {
      setBusy(false)
    }
  }, [phone, phoneValid, busy, countdown, toast])

  const doLogin = useCallback(async () => {
    if (!codeValid || busy) return
    setBusy(true)
    setError('')
    try {
      const r = await backend.loomyDesktopLogin(phone, code, messageId)
      if (!r.ok) {
        setError(r.error || '登录失败，请核对验证码')
        return
      }
      setStep('done')
      toast('Loomy 账号添加成功', 'success')
      window.setTimeout(onClose, 1500)
    } catch (e) {
      setError(e instanceof Error ? e.message : '网络异常，请确认网关运行中')
    } finally {
      setBusy(false)
    }
  }, [code, codeValid, busy, phone, messageId, toast, onClose])

  const backToPhone = useCallback(() => {
    setStep('phone')
    setCode('')
    setError('')
  }, [])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay/60 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="loomy-login-title"
      onClick={onClose}
    >
      <div
        className="w-[420px] rounded-lg border border-border bg-bg-card p-6 shadow-popup"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="loomy-login-title" className="text-md font-semibold text-fg">
          添加 Loomy 账号
        </h2>
        <p className="mt-2 text-sm text-fg-subtle">
          用手机号验证码登录你自己的 Loomy 账号，登录状态由网关自动保持，
          对话积分记在你自己的账号上。
        </p>

        {step === 'phone' && (
          <div className="mt-5 space-y-3">
            <label className="block">
              <span className="mb-1.5 block text-sm text-fg-muted">手机号</span>
              <div className="flex items-center gap-2">
                <Smartphone className="size-4 shrink-0 text-fg-subtle" aria-hidden />
                <Input
                  autoFocus
                  inputMode="numeric"
                  maxLength={11}
                  placeholder="请输入 11 位手机号"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value.replace(/\D/g, ''))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && phoneValid) void sendCode()
                  }}
                  className="tabular"
                />
              </div>
            </label>
            {error && (
              <p className="flex items-start gap-1.5 text-sm text-danger" role="alert">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                {error}
              </p>
            )}
            <Button
              variant="default"
              className="w-full"
              disabled={!phoneValid || busy || countdown > 0}
              loading={busy}
              onClick={() => void sendCode()}
            >
              {countdown > 0 ? `${countdown}s 后可重发` : '获取验证码'}
            </Button>
          </div>
        )}

        {step === 'code' && (
          <div className="mt-5 space-y-3">
            <p className="text-sm text-fg-muted">
              验证码已发送至 <span className="font-mono text-fg">{phone}</span>
              <button
                type="button"
                className="ml-2 cursor-pointer text-sm text-primary hover:underline"
                onClick={backToPhone}
              >
                换个号码
              </button>
            </p>
            <label className="block">
              <span className="mb-1.5 block text-sm text-fg-muted">短信验证码</span>
              <div className="flex items-center gap-2">
                <MessageSquareText className="size-4 shrink-0 text-fg-subtle" aria-hidden />
                <Input
                  autoFocus
                  inputMode="numeric"
                  maxLength={6}
                  placeholder="请输入 6 位验证码"
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && codeValid) void doLogin()
                  }}
                  className="tabular tracking-[0.4em]"
                />
              </div>
            </label>
            {error && (
              <p className="flex items-start gap-1.5 text-sm text-danger" role="alert">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                {error}
              </p>
            )}
            <Button
              variant="default"
              className="w-full"
              disabled={!codeValid || busy}
              loading={busy}
              onClick={() => void doLogin()}
            >
              登录
            </Button>
            <Button
              variant="ghost"
              className="w-full"
              disabled={busy || countdown > 0}
              onClick={() => void sendCode()}
            >
              {countdown > 0 ? `${countdown}s 后可重新发送` : '重新发送验证码'}
            </Button>
          </div>
        )}

        {step === 'done' && (
          <div className="mt-6 flex flex-col items-center gap-3 py-4" role="status">
            <CheckCircle2 className="size-10 text-success" aria-hidden />
            <p className="text-md font-medium text-fg">添加成功</p>
            <p className="text-sm text-fg-subtle">账号已加入 Loomy 通道，列表稍后自动刷新</p>          </div>
        )}
      </div>
    </div>
  )
}
