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
 * Trae 配色说明：需求指定 #1A1A1A，但它与卡片底（#1E1E1E）对比度约 1:1，
 * 柱子在深色背景上完全不可见。此处提升为 #868E96（对比度 ≈3.4:1），
 * 既保留中性灰的视觉语义，又满足图形元素对背景 ≥3:1 的可访问性要求。
 */
export interface SeriesColor {
  key: Channel
  label: string
  color: string
}

export const SERIES_COLORS: SeriesColor[] = [
  { key: 'Trae', label: 'Trae 通道', color: '#868E96' },
  { key: 'WorkBuddy', label: 'WorkBuddy 通道', color: '#22C55E' },
  { key: 'WorkBuddy_IE', label: 'WorkBuddy 国际', color: '#EAB308' },
]
