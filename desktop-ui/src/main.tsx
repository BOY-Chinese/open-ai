import React from 'react'
import ReactDOM from 'react-dom/client'
import { AppLayout } from '@/components/layout/AppLayout'
import { ToastProvider } from '@/components/feedback/Toast'
import { TooltipProvider } from '@/components/ui/tooltip'
import '@/styles/globals.css'

/**
 * 应用入口
 *
 * Provider 顺序：Tooltip（全局悬停提示）→ Toast（操作反馈）→ 布局
 */
function App() {
  return (
    <TooltipProvider delayDuration={400} skipDelayDuration={200}>
      <ToastProvider>
        <AppLayout />
      </ToastProvider>
    </TooltipProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
