import * as React from 'react'
import { cn } from '@/lib/utils'

/** 骨架屏：所有异步区域 loading 态统一使用 */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('skeleton rounded-md', className)} {...props} />
}

/** 表格骨架：rows 行 × cols 列 */
export function TableSkeleton({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-2 p-3" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className="h-4"
              style={{ width: c === 1 ? '38%' : `${14 + ((r + c) % 3) * 6}%` }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

/** 统计卡片骨架 */
export function StatSkeleton() {
  return (
    <div className="rounded-lg border border-border bg-bg-card px-4 py-3" aria-busy="true">
      <Skeleton className="h-3 w-20" />
      <Skeleton className="mt-3 h-8 w-28" />
    </div>
  )
}
