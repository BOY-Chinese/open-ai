import type { Channel } from '@/types/domain'

/**
 * 图表系列配色
 *
 * 为什么这里允许出现十六进制字面量（全项目唯一的例外）：
 * recharts 通过 SVG 的 fill 属性着色，其值必须是浏览器可直接解析的颜色，
 * 而 CSS 变量在本项目里以「HSL 分量」形式存储（如 `240 4% 46%`，供 Tailwind
 * 以 `hsl(var(--x))` 消费），直接传给 fill 会解析失败并退化为黑色。
 *
 * 因此图表色在此集中定义一次，作为「图表域」的颜色真源；
 * 其余所有 UI 颜色仍严格来自 globals.css 的语义令牌。
 *
 * ── 为什么要分深浅两套（v3.0 加入外观设置后）──
 * 原配色是为深色底选的：Trae 用中性灰 #868E96（对 #1E1E1E 约 3.4:1），
 * WorkBuddy 用亮绿 #22C55E、国际版用亮黄 #EAB308 —— 亮绿/亮黄在深色底上
 * 对比度很高（7.2:1 / 10.9:1），但**在白色底上分别只有 2.1:1 和 1.9:1**，
 * 柱子在浅色主题下几乎看不见。故浅色主题另给一套压暗的同色系值
 * （全部 ≥3:1，满足「图形元素对背景 ≥3:1」的无障碍底线）；色相保持不变，
 * 用户在不同主题下仍能凭颜色认出同一条通道。
 *
 * Trae 深色配色说明：需求指定 #1A1A1A，但它与卡片底（#1E1E1E）对比度约 1:1，
 * 柱子在深色背景上完全不可见。此处提升为 #868E96，既保留中性灰的视觉语义，
 * 又满足可访问性要求。
 */
export interface SeriesColor {
  key: Channel
  label: string
  color: string
}

/** 深色主题（沿用 v2.x 已验收的配色；v3.1 加入 Loomy 紫） */
const DARK_SERIES: SeriesColor[] = [
  { key: 'Trae', label: 'Trae 通道', color: '#868E96' },
  { key: 'WorkBuddy', label: 'WorkBuddy 通道', color: '#22C55E' },
  { key: 'WorkBuddy_IE', label: 'WorkBuddy 国际', color: '#EAB308' },
  { key: 'Loomy', label: 'Loomy 通道', color: '#A78BFA' },
]

/**
 * 浅色主题（vs 白底：4.8:1 / 3.5:1 / 4.1:1 / 3.9:1，色相与深色套一致）。
 * Loomy 紫：深色 #A78BFA 亮紫在白底仅 2.3:1，压暗为 #7C3AED（violet-600）。
 */
const LIGHT_SERIES: SeriesColor[] = [
  { key: 'Trae', label: 'Trae 通道', color: '#6B7280' },
  { key: 'WorkBuddy', label: 'WorkBuddy 通道', color: '#1A9E4A' },
  { key: 'WorkBuddy_IE', label: 'WorkBuddy 国际', color: '#BD6705' },
  { key: 'Loomy', label: 'Loomy 通道', color: '#7C3AED' },
]

/**
 * 按当前生效主题取配色。
 *
 * ★ 图例、Tooltip、柱子必须取自**同一次调用**的结果：分别取会让浅色主题下
 *   图例与柱子配色不一致（图例用一套、柱子用另一套），比配色难看更难排查。
 */
export function seriesColors(resolved: 'light' | 'dark'): SeriesColor[] {
  return resolved === 'dark' ? DARK_SERIES : LIGHT_SERIES
}
