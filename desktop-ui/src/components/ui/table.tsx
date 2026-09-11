import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 数据表格基元 — 无边框 + 微行分隔 + 行悬停高亮
 * 设计要点：
 *  - 表头 sticky（虚拟滚动时保持可见）
 *  - 行 hover 用 bg-bg-card-hover，选中行用 primary/10
 *  - 单元格统一 12px 横向内边距，行高 36px（信息密度）
 */

export const Table = React.forwardRef<
  HTMLTableElement,
  React.TableHTMLAttributes<HTMLTableElement>
>(({ className, ...props }, ref) => (
  <table
    ref={ref}
    className={cn('w-full border-collapse text-base', className)}
    {...props}
  />
))
Table.displayName = 'Table'

export const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.TableHTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn(
      'sticky top-0 z-10 bg-bg-card text-sm font-medium text-fg-subtle',
      'after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-border',
      'relative',
      className
    )}
    {...props}
  />
))
TableHeader.displayName = 'TableHeader'

export const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.TableHTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn(className)} {...props} />
))
TableBody.displayName = 'TableBody'

export const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.TableHTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn(
      'border-b border-border-subtle transition-colors duration-fast',
      'hover:bg-bg-card-hover',
      'data-[selected=true]:bg-primary/10',
      className
    )}
    {...props}
  />
))
TableRow.displayName = 'TableRow'

export const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn('h-9 px-3 text-left font-medium whitespace-nowrap', className)}
    {...props}
  />
))
TableHead.displayName = 'TableHead'

export const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td
    ref={ref}
    className={cn('h-9 px-3 align-middle text-fg-muted', className)}
    {...props}
  />
))
TableCell.displayName = 'TableCell'

/** 空状态：所有表格必须提供，不允许白屏 */
export function TableEmpty({
  colSpan,
  icon: Icon,
  title,
  description,
  action,
}: {
  colSpan: number
  icon?: React.ComponentType<{ className?: string }>
  title: string
  description?: string
  action?: React.ReactNode
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-16">
        <div className="flex flex-col items-center justify-center text-center">
          {Icon && <Icon className="size-8 text-fg-faint" />}
          <p className="mt-3 text-md font-medium text-fg-muted">{title}</p>
          {description && <p className="mt-1 text-sm text-fg-subtle">{description}</p>}
          {action && <div className="mt-4">{action}</div>}
        </div>
      </td>
    </tr>
  )
}
