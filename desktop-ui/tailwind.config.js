/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        /* ── 语义化令牌（全部走 CSS 变量，组件内禁止硬编码 hex）── */
        bg: {
          app: 'hsl(var(--bg-app))',
          sidebar: 'hsl(var(--bg-sidebar))',
          content: 'hsl(var(--bg-content))',
          card: 'hsl(var(--bg-card))',
          'card-hover': 'hsl(var(--bg-card-hover))',
          input: 'hsl(var(--bg-input))',
          log: 'hsl(var(--bg-log))',
        },
        border: {
          DEFAULT: 'hsl(var(--border))',
          subtle: 'hsl(var(--border-subtle))',
          strong: 'hsl(var(--border-strong))',
        },
        fg: {
          DEFAULT: 'hsl(var(--fg))',
          muted: 'hsl(var(--fg-muted))',
          subtle: 'hsl(var(--fg-subtle))',
          faint: 'hsl(var(--fg-faint))',
        },
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          hover: 'hsl(var(--primary-hover))',
          fg: 'hsl(var(--primary-fg))',
          soft: 'hsl(var(--primary-soft))',
        },
        success: {
          DEFAULT: 'hsl(var(--success))',
          soft: 'hsl(var(--success-soft))',
        },
        warning: {
          DEFAULT: 'hsl(var(--warning))',
          soft: 'hsl(var(--warning-soft))',
        },
        danger: {
          DEFAULT: 'hsl(var(--danger))',
          hover: 'hsl(var(--danger-hover))',
          soft: 'hsl(var(--danger-soft))',
        },
        /* 三通道品牌色（图表/徽标共用，避免逐处硬编码） */
        channel: {
          trae: 'hsl(var(--channel-trae))',
          wb: 'hsl(var(--channel-wb))',
          wbie: 'hsl(var(--channel-wbie))',
        },
      },
      borderRadius: {
        /* 硬约束：最大 8px */
        sm: '4px',
        DEFAULT: '6px',
        md: '6px',
        lg: '8px',
      },
      spacing: {
        /* 4px 倍数节奏 */
        1: '4px',
        2: '8px',
        3: '12px',
        4: '16px',
        6: '24px',
        8: '32px',
      },
      fontSize: {
        /* 正文最小 13px */
        xs: ['11px', { lineHeight: '1.5' }],
        sm: ['12px', { lineHeight: '1.5' }],
        base: ['13px', { lineHeight: '1.5' }],
        md: ['14px', { lineHeight: '1.5' }],
        lg: ['16px', { lineHeight: '1.5' }],
        xl: ['20px', { lineHeight: '1.4' }],
        stat: ['32px', { lineHeight: '1.2' }],
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Microsoft YaHei', 'sans-serif'],
        mono: ['JetBrains Mono', 'Cascadia Code', 'Consolas', 'monospace'],
      },
      transitionDuration: {
        fast: '90ms',
        DEFAULT: '120ms',
        slow: '200ms',
      },
      boxShadow: {
        /* 轻阴影，不要重阴影 */
        card: '0 1px 2px 0 hsl(0 0% 0% / 0.24)',
        popup: '0 4px 12px -2px hsl(0 0% 0% / 0.5)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-in': {
          from: { opacity: '0', transform: 'translateY(2px) scale(0.98)' },
          to: { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 120ms ease-out',
        'slide-in': 'slide-in 120ms ease-out',
        shimmer: 'shimmer 1.4s infinite',
      },
    },
  },
  plugins: [require('tailwindcss-animate')],
}
