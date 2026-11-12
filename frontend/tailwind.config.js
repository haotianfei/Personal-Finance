/** @type {import('tailwindcss').Config} */
const tokens = require('./src/styles/tokens.json')

const px = (value) => `${value}px`
const kebab = (key) =>
  key.replace(/([a-z0-9])([A-Z])/g, '$1-$2').replace(/_/g, '-').toLowerCase()

const typeScale = Object.fromEntries(
  Object.entries(tokens.type).map(([key, value]) => [
    kebab(key),
    [px(value.size), { lineHeight: px(value.line) }],
  ])
)

const brand = Object.fromEntries(
  Object.entries(tokens.color.brand).map(([step, value]) => [step, value])
)

/**
 * Tailwind 只从 tokens.json 取色/字阶/圆角/阴影/层级；
 * 页面使用语义名（bg-surface、text-ink、text-finance-up-text），不直接使用色阶。
 * 色阶中保留 brand 全阶与 slate 默认阶，仅为兼容 Wave 1b 前尚未迁移的页面。
 */
module.exports = {
  // 所有 hover: 工具类只在「支持 hover 的细指针设备」生成（design §2.2/§7.2）。
  future: { hoverOnlyWhenSupported: true },
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      colors: {
        app: tokens.color.bg.app,
        surface: tokens.color.bg.surface,
        subtle: tokens.color.bg.subtle,
        inverse: tokens.color.bg.inverse,
        ink: tokens.color.text.primary,
        secondary: tokens.color.text.secondary,
        muted: tokens.color.text.muted,
        'inverse-text': tokens.color.text.inverse,
        link: tokens.color.text.link,
        disabled: tokens.color.text.disabled,
        line: {
          DEFAULT: tokens.color.border.default,
          strong: tokens.color.border.strong,
        },
        brand,
        // DEPRECATED（仅 W1b 前旧页面）：success/danger 的命名与值相反，
        // 新代码一律用 finance.up/down 或 status.*；此处保留旧视觉避免本波改页面语义。
        success: tokens.color.finance.up,
        danger: tokens.color.finance.down,
        action: {
          primary: tokens.color.action.primary,
          'primary-hover': tokens.color.action['primary-hover'],
          danger: tokens.color.action.danger,
          'danger-surface': tokens.color.action['danger-surface'],
        },
        finance: {
          up: tokens.color.finance.up,
          'up-text': tokens.color.finance['up-text'],
          down: tokens.color.finance.down,
          'down-text': tokens.color.finance['down-text'],
          flat: tokens.color.finance.flat,
          'flat-text': tokens.color.finance['flat-text'],
          neutral: tokens.color.finance.neutral,
        },
        status: {
          good: tokens.color.status.good,
          'good-text': tokens.color.status['good-text'],
          warn: tokens.color.status.warn,
          'warn-text': tokens.color.status['warn-text'],
          critical: tokens.color.status.critical,
          'critical-text': tokens.color.status['critical-text'],
          unknown: tokens.color.status.unknown,
          'unknown-text': tokens.color.status['unknown-text'],
          'warn-surface': tokens.color.status['warn-surface'],
          'critical-surface': tokens.color.status['critical-surface'],
        },
        focus: tokens.color.focus,
        nav: {
          idle: tokens.color.nav.idle,
          group: tokens.color.nav.group,
          active: tokens.color.nav.active,
        },
      },
      fontFamily: {
        sans: [tokens.font.sans],
        code: [tokens.font.code],
      },
      fontSize: typeScale,
      borderRadius: {
        control: px(tokens.radius.control),
        card: px(tokens.radius.card),
        overlay: px(tokens.radius.overlay),
        pill: px(tokens.radius.pill),
      },
      boxShadow: {
        card: tokens.shadow.card,
        overlay: tokens.shadow.overlay,
      },
      maxWidth: {
        // 内容宽度上限的唯一消费方是 .pf-main（globals.css），页面容器不再叠加同一条 clamp。
        content: 'var(--pf-shell-content-max)',
        reading: 'var(--pf-shell-prose-max)',
      },
      zIndex: {
        sticky: String(tokens.z.sticky),
        nav: String(tokens.z.nav),
        overlay: String(tokens.z.overlay),
        modal: String(tokens.z.modal),
        toast: String(tokens.z.toast),
      },
      screens: {
        xs: tokens.screen.xs,
        phone: tokens.screen.phone,
        tablet: tokens.screen.tablet,
        'tablet-l': tokens.screen['tablet-l'],
        desktop: tokens.screen.desktop,
      },
      transitionDuration: {
        fast: `${tokens.motion.fast}ms`,
        base: `${tokens.motion.base}ms`,
        sheet: `${tokens.motion.sheet}ms`,
      },
      transitionTimingFunction: {
        standard: tokens.motion.easing,
      },
      animation: {
        'fade-in': 'fadeIn 0.18s ease-out',
        'slide-up': 'slideUp 0.18s ease-out',
        // 移动「更多」抽屉从左侧滑入
        'slide-in-left': 'slideInLeft 0.24s cubic-bezier(0.4, 0, 0.2, 1)',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideInLeft: {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(0)' },
        },
      },
    },
  },
  plugins: [],
}