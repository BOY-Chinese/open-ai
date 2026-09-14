import {
  Users,
  KeyRound,
  Boxes,
  BarChart3,
  ScrollText,
  Settings,
  Workflow,
  type LucideIcon,
} from 'lucide-react'

/** 页面标识（路由键，唯一真源） */
export type PageKey =
  | 'accounts'
  | 'api'
  | 'models'
  | 'credits'
  | 'auto_router'
  | 'logs'
  | 'settings'

export interface NavItem {
  key: PageKey
  label: string
  /** 页面标题（内容区顶部统一渲染） */
  title: string
  description: string
  icon: LucideIcon
}

/**
 * 导航分两组，中间由 flex spacer 撑开：
 *   顶部组 — 业务高频（账号 / API / 模型 / 积分 / Auto 路由连）
 *   底部组 — 低频工具（日志 / 设置）
 */
export const NAV_TOP: NavItem[] = [
  {
    key: 'accounts',
    label: '账号管理',
    title: '账号管理',
    description: 'Trae / WorkBuddy / WorkBuddy 国际 / Loomy 四通道账号统一管理',
    icon: Users,
  },
  {
    key: 'api',
    label: 'API 管理',
    title: 'API 管理',
    description: '网关接入地址与 API 密钥',
    icon: KeyRound,
  },
  {
    key: 'models',
    label: '模型列表',
    title: '模型列表',
    description: '四通道模型与积分倍率、路由映射',
    icon: Boxes,
  },
  {
    key: 'credits',
    label: '积分看板',
    title: '积分看板',
    description: '积分获取与消耗趋势',
    icon: BarChart3,
  },
  {
    key: 'auto_router',
    label: 'Auto路由连',
    title: 'Auto路由连',
    description: '虚拟模型 Auto路由连：按顺序故障转移的模型路由链',
    icon: Workflow,
  },
]

export const NAV_BOTTOM: NavItem[] = [
  {
    key: 'logs',
    label: '操作日志',
    title: '操作日志',
    description: '账号 / 密钥 / 模型等操作的执行记录',
    icon: ScrollText,
  },
  {
    key: 'settings',
    label: '系统设置',
    title: '系统设置',
    description: '启动项、版本更新与危险操作',
    icon: Settings,
  },
]

export const NAV_ALL: NavItem[] = [...NAV_TOP, ...NAV_BOTTOM]

export const DEFAULT_PAGE: PageKey = 'accounts'

export function getNavItem(key: PageKey): NavItem {
  return NAV_ALL.find((n) => n.key === key) ?? NAV_ALL[0]
}
