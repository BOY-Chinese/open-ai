import * as React from 'react'
import { cn } from '@/lib/utils'

/** 卡片容器：微边框 + 轻阴影，圆角 8px 上限 */
const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        'rounded-lg border border-border bg-bg-card shadow-card',
        className
      )}
      {...props}
    />
  )
)
Card.displayName = 'Card'

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('flex items-center justify-between gap-3 px-4 py-3', className)}
      {...props}
    />
  )
)
CardHeader.displayName = 'CardHeader'

const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h3
      ref={ref}
      className={cn('text-md font-semibold text-fg', className)}
      {...props}
    />
  )
)
CardTitle.displayName = 'CardTitle'

const CardDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p ref={ref} className={cn('text-sm text-fg-subtle', className)} {...props} />
))
CardDescription.displayName = 'CardDescription'

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn('px-4 pb-4', className)} {...props} />
  )
)
CardContent.displayName = 'CardContent'

/** 卡片分区：标签 + 说明 + 右侧操作（系统设置页大量复用） */
const CardSection = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { label: string; hint?: string }
>(({ className, label, hint, children, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      'flex items-center justify-between gap-4 rounded-lg border border-border bg-bg-card px-4 py-3',
      className
    )}
    {...props}
  >
    <div className="min-w-0">
      <div className="text-md font-medium text-fg">{label}</div>
      {hint && <div className="mt-1 text-sm text-fg-subtle">{hint}</div>}
    </div>
    <div className="shrink-0">{children}</div>
  </div>
))
CardSection.displayName = 'CardSection'

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardSection }
