import React from 'react'
import ReactDOM from 'react-dom/client'
import { AppLayout } from '@/components/layout/AppLayout'
import { ToastProvider } from '@/components/feedback/Toast'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ThemeProvider } from '@/hooks/useTheme'
import '@/styles/globals.css'

/**
 * 应用入口
 *
 * Provider 顺序：Theme（外观，最先：其余组件都要按主题着色）
 *   → Tooltip（全局悬停提示）→ Toast（操作反馈）→ 布局
 *
 * 首帧的 `dark` class 由 index.html 的内联脚本写好，ThemeProvider
 * 只负责接管后续切换，因此不会出现主题闪烁。
 */
function App() {
  return (
    <ThemeProvider>
      <TooltipProvider delayDuration={400} skipDelayDuration={200}>
        <ToastProvider>
          <AppLayout />
        </ToastProvider>
      </TooltipProvider>
    </ThemeProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
