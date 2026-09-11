import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 页面壳 — 内容区统一结构
 *
 * 布局契约（三页共用）：
 *   ┌ PageHeader  标题(20px 粗体 #FFF / mb 24px) + 可选右侧操作
 *   ├ PageToolbar 工具栏（筛选/刷新等，可选）
 *   ├ PageBody    flex-1 min-h-0，表格/图表滚动区
 *   └ PageFooter  固定底部操作栏（右对齐，可选）
 *
 * 滚动只发生在 PageBody，头尾固定不遮挡内容。
 */

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div className="min-w-0">
        {/* 需求：加粗 20px，颜色 #FFFFFF，margin-bottom 24px */}
        <h1 className="text-xl font-bold text-fg">{title}</h1>
        {description && (
          <p className="mt-1 truncate text-sm text-fg-subtle">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function PageToolbar({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-lg border border-border bg-bg-card px-3 py-2',
        className
      )}
    >
      {children}
    </div>
  )
}

/** 工具栏内的分组分隔，避免按钮糊成一片 */
export function ToolbarDivider() {
  return <span aria-hidden className="mx-1 h-4 w-px bg-border" />
}

export function PageBody({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-bg-card',
        className
      )}
    >
      {children}
    </div>
  )
}

/** 底部固定操作栏（右对齐） */
export function PageFooter({ children }: { children: ReactNode }) {
  return (
    <div className="mt-4 flex shrink-0 items-center justify-end gap-2">{children}</div>
  )
}

/** 页面根容器：充满内容区，纵向排布，仅 body 滚动 */
export function PageShell({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex h-full min-h-0 flex-col', className)}>{children}</div>
  )
}
