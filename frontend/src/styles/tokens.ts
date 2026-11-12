import raw from './tokens.json'

/**
 * Design token 的类型化入口（单一来源：`tokens.json`）。
 *
 * 使用规则：
 * - 页面/组件用 Tailwind 语义类（`bg-surface`、`text-muted`、`text-finance-up-text`），
 *   不写十六进制、rgb() 或色阶（`slate-400` 仅作为旧代码迁移期遗留）。
 * - 内联样式/运行时计算需要变量时用 `tokenVar()`，不要在 TS 里复制色值。
 * - antd 主题从本对象取；Tailwind 从同一 JSON 取；`charts/theme.ts` 只镜像，不回写。
 */
export const tokens = raw

export type Tokens = typeof raw
export type TokenColor = Tokens['color']

/** `tokenVar('color', 'text', 'primary')` → `var(--pf-color-text-primary)` */
export function tokenVar(...path: string[]): string {
  return `var(--pf-${path.map(kebab).join('-')})`
}

function kebab(key: string): string {
  return key.replace(/([a-z0-9])([A-Z])/g, '$1-$2').replace(/_/g, '-').toLowerCase()
}

export const color = raw.color
export const font = raw.font
export const focus = raw.focus
export const space = raw.space
export const radius = raw.radius
export const control = raw.control
export const type = raw.type
export const shadow = raw.shadow
export const z = raw.z
export const motion = raw.motion
export const shell = raw.shell
export const screen = raw.screen