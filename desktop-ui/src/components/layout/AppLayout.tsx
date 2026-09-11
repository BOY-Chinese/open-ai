import { useEffect, useState } from 'react'
import { Sidebar } from '@/components/layout/Sidebar'
import { DEFAULT_PAGE, getNavItem, NAV_ALL, type PageKey } from '@/config/navigation'
import { AccountsPage } from '@/pages/AccountsPage'
import { ApiPage } from '@/pages/ApiPage'
import { ModelsPage } from '@/pages/ModelsPage'
import { CreditsPage } from '@/pages/CreditsPage'
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
 */
export function AppLayout() {
  const [page, setPage] = useState<PageKey>(readHashPage)
  const nav = getNavItem(page)

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
      <Sidebar current={page} onSelect={setPage} />

      <main className="flex min-w-0 flex-1 flex-col bg-bg-content">
        {/* 内容区内边距：24px（4 的倍数） */}
        <div className="flex min-h-0 flex-1 flex-col p-6">
          {page === 'accounts' && <AccountsPage />}
          {page === 'api' && <ApiPage />}
          {page === 'models' && <ModelsPage />}
          {page === 'credits' && <CreditsPage />}
          {page === 'logs' && <LogsPage />}
          {page === 'settings' && <SettingsPage />}
        </div>

        {/* 底部状态条，替代原窗口标题栏信息 */}
        <footer className="flex h-7 shrink-0 items-center gap-4 border-t border-border px-4 text-xs text-fg-faint">
          <span>{nav.title}</span>
          <span className="ml-auto font-mono">open-ai desk · 127.0.0.1:8000</span>
        </footer>
      </main>
    </div>
  )
}
