import { Activity } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NAV_TOP, NAV_BOTTOM, type NavItem, type PageKey } from '@/config/navigation'

/**
 * 侧边栏 — 固定 200px，替代原顶部 Tab
 *
 * 设计要点：
 *  - 顶部组 / 底部组由 flex-1 spacer 撑开
 *  - 选中态：左侧 2px 主色条 + 半透明主色底 + 文字转白（三重编码，不单靠颜色）
 *  - hover 态明确；键盘可达（button 语义 + focus-visible）
 */

function NavButton({
  item,
  active,
  onSelect,
}: {
  item: NavItem
  active: boolean
  onSelect: (key: PageKey) => void
}) {
  const Icon = item.icon
  return (
    <button
      type="button"
      onClick={() => onSelect(item.key)}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'group relative flex h-9 w-full cursor-pointer items-center gap-2.5 rounded-md pl-3 pr-2 text-left text-base',
        'transition-colors duration-fast',
        active
          ? 'bg-primary/12 font-medium text-fg'
          : 'text-fg-muted hover:bg-bg-card-hover hover:text-fg'
      )}
    >
      {/* 选中指示条：主色左边框 */}
      <span
        aria-hidden
        className={cn(
          'absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-r-sm bg-primary transition-opacity duration-fast',
          active ? 'opacity-100' : 'opacity-0'
        )}
      />
      <Icon
        className={cn(
          'size-4 shrink-0 transition-colors duration-fast',
          active ? 'text-primary' : 'text-fg-subtle group-hover:text-fg-muted'
        )}
      />
      <span className="truncate">{item.label}</span>
    </button>
  )
}

export function Sidebar({
  current,
  onSelect,
  gatewayOnline = true,
  version,
}: {
  current: PageKey
  onSelect: (key: PageKey) => void
  /** 网关在线状态（对应「重新连接」的全局指示） */
  gatewayOnline?: boolean
  /**
   * 版本号文案（由 AppLayout 从后端读到后传入，已格式化）。
   *
   * ★ 这里**不再有默认值**。原来写死 `version = 'v3.0-dev'`，而 AppLayout
   *   调用时没传 —— 侧边栏于是永远显示这个硬编码常量：升级到 dev-v3.1 后
   *   界面上还是 v3.0-dev，看起来就是「版本号没正确显示」。
   *   版本号的唯一真源是后端 `version.py`（经 GET /v1/admin/version），
   *   拿不到时留空，宁可什么都不显示也不显示一个错的。
   */
  version?: string
}) {
  return (
    <aside className="flex w-[200px] shrink-0 flex-col border-r border-border bg-bg-sidebar">
      {/* 品牌区 */}
      <div className="flex h-12 items-center gap-2.5 border-b border-border px-4">
        <div className="flex size-6 shrink-0 items-center justify-center rounded-md bg-primary/15">
          <Activity className="size-3.5 text-primary" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-md font-semibold leading-tight text-fg">open-ai</div>
        </div>
      </div>

      {/* 顶部导航组 */}
      <nav className="flex flex-col gap-1 p-2" aria-label="主导航">
        {NAV_TOP.map((item) => (
          <NavButton
            key={item.key}
            item={item}
            active={current === item.key}
            onSelect={onSelect}
          />
        ))}
      </nav>

      {/* 撑开中间空白，把底部组压到最下方 */}
      <div className="flex-1" />

      {/* 底部导航组 */}
      <nav className="flex flex-col gap-1 border-t border-border p-2" aria-label="系统导航">
        {NAV_BOTTOM.map((item) => (
          <NavButton
            key={item.key}
            item={item}
            active={current === item.key}
            onSelect={onSelect}
          />
        ))}
      </nav>

      {/* 运行状态 + 版本 */}
      <div className="flex items-center gap-2 border-t border-border px-4 py-2.5">
        <span
          aria-hidden
          className={cn(
            'size-1.5 shrink-0 rounded-full',
            gatewayOnline ? 'bg-success' : 'bg-danger'
          )}
        />
        <span className="truncate text-xs text-fg-subtle">
          {gatewayOnline ? '网关运行中' : '网关已断开'}
        </span>
        <span className="ml-auto shrink-0 font-mono text-xs text-fg-faint">{version}</span>
      </div>
    </aside>
  )
}
