import { useCallback, useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { Sidebar } from '@/components/layout/Sidebar'
import { GatewayGate } from '@/components/feedback/GatewayGate'
import { DEFAULT_PAGE, getNavItem, NAV_ALL, type PageKey } from '@/config/navigation'
import { isMockMode } from '@/lib/dataSource'
import { ensureGateway, gatewayRuntime, inTauri, probeGateway, type GatewayPhase } from '@/lib/gateway'
import { AccountsPage } from '@/pages/AccountsPage'
import { ApiPage } from '@/pages/ApiPage'
import { ModelsPage } from '@/pages/ModelsPage'
import { CreditsPage } from '@/pages/CreditsPage'
import { AutoRouterPage } from '@/pages/AutoRouterPage'
import { LogsPage } from '@/pages/LogsPage'
import { SettingsPage } from '@/pages/SettingsPage'

/** 从 URL hash 读取初始页面（支持 #/accounts 直达，便于深链与自动化截图） */
function readHashPage(): PageKey {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return (NAV_ALL.find((n) => n.key === raw)?.key ?? DEFAULT_PAGE) as PageKey
}

/**
 * 全局布局 — 左侧固定侧边栏 + 右侧内容区
 *
 * 与原 tkinter 版（顶部 ttk.Notebook）的关键差异：
 *  - 横向 Tab 占 1 行垂直空间且标签一多就挤压；竖向侧边栏把导航成本压到零
 *  - 内容区顶部统一 20px 加粗标题，层级更清晰
 *
 * v3.0：内容区前增加「网关启动门」。打包后的桌面端没有 dev server，
 * 后端若未运行，6 个页面会同时报「无法连接网关」；改为先探活/自动拉起后端，
 * 就绪后再挂载页面 —— 用户只看到一次明确的进度或一次可操作的错误。
 */
export function AppLayout() {
  const [page, setPage] = useState<PageKey>(readHashPage)
  const nav = getNavItem(page)

  // 演示数据模式（?mock=1）不需要网关，直接放行，便于纯前端视觉回归
  const [phase, setPhase] = useState<GatewayPhase>(isMockMode ? 'online' : 'checking')
  const [reason, setReason] = useState<string>()
  const [root, setRoot] = useState<string>()
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (isMockMode) return
    let alive = true
    void (async () => {
      const rt = await gatewayRuntime()
      if (alive) setRoot(rt.root)
      const probe = await ensureGateway({
        onPhase: (p) => {
          if (alive) setPhase(p)
        },
      })
      if (alive && !probe.online) setReason(probe.reason)
    })()
    return () => {
      alive = false
    }
  }, [attempt])

  const retry = useCallback(() => {
    setReason(undefined)
    setAttempt((n) => n + 1)
  }, [])

  /**
   * 运行期存活探测（45 秒一次）。
   *
   * 为什么需要：启动时探活只覆盖「打开界面那一刻」。网关随后可能被停掉
   * （例如旧版 Python 托盘的「退出」会请求 Broker 优雅关闭全部后端），
   * 此时页面仍显示着上一次的数据与「网关运行中」—— 正是「看起来好的、
   * 其实已经断了」最容易诱发误报的状态。
   *
   * 取舍：连续 2 次失败才判定断开，避免瞬时抖动把整页卸载（丢本地状态）；
   * 判定断开后只提示 + 提供重试，**不自动重启后端** —— 用户若是主动停的
   * 服务，界面不该偷偷给他拉起来。
   */
  useEffect(() => {
    if (isMockMode || phase !== 'online') return
    let alive = true
    let strikes = 0
    const timer = window.setInterval(async () => {
      const probe = await probeGateway(3000)
      if (!alive) return
      if (probe.online) {
        strikes = 0
        return
      }
      strikes += 1
      if (strikes >= 2) {
        setReason(probe.reason)
        setPhase('offline')
      }
    }, 45000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [phase])

  const openLogs = useCallback(() => {
    void invoke('open_logs_dir').catch((e) => console.warn('[open-ai] 打开日志目录失败', e))
  }, [])

  const online = phase === 'online'

  /** 页面与 URL hash 双向同步：刷新/深链保持当前页，也便于自动化工具直达 */
  useEffect(() => {
    const next = `#/${page}`
    if (window.location.hash !== next) {
      window.history.replaceState(null, '', next)
    }
  }, [page])

  /** 支持浏览器前进/后退与外部改 hash */
  useEffect(() => {
    const onHash = () => setPage(readHashPage())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  return (
    <div className="flex h-full w-full overflow-hidden bg-bg-app">
      <Sidebar current={page} onSelect={setPage} gatewayOnline={online} />

      <main className="flex min-w-0 flex-1 flex-col bg-bg-content">
        {online ? (
          /* 内容区内边距：24px（4 的倍数） */
          <div className="flex min-h-0 flex-1 flex-col p-6">
            {page === 'accounts' && <AccountsPage />}
            {page === 'api' && <ApiPage />}
            {page === 'models' && <ModelsPage />}
            {page === 'credits' && <CreditsPage />}
            {page === 'auto_router' && <AutoRouterPage />}
            {page === 'logs' && <LogsPage />}
            {page === 'settings' && <SettingsPage />}
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col">
            <GatewayGate
              phase={phase}
              reason={reason}
              root={root}
              canOpenLogs={inTauri}
              onRetry={retry}
              onOpenLogs={openLogs}
            />
          </div>
        )}

        {/* 底部状态条，替代原窗口标题栏信息 */}
        <footer className="flex h-7 shrink-0 items-center gap-4 border-t border-border px-4 text-xs text-fg-faint">
          <span>{nav.title}</span>
          <span className="ml-auto font-mono">
            {isMockMode ? 'open-ai desk · 演示数据' : 'open-ai desk · 127.0.0.1:8000'}
          </span>
        </footer>
      </main>
    </div>
  )
}
