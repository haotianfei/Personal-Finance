import { cn } from '@/lib/utils'
import { EMPTY_VALUE, formatValue } from '@/lib/format'

/**
 * 金额：唯一展示入口（design §3.3/§8.2）。
 *
 * - 金额本体永远中性墨色；涨跌色只属于 DirectionText 的「变化量」。
 * - 不截断、不省略；负数用 U+2212 且符号在货币符号前。
 * - 空值显示 `—`，绝不用 ¥0.00 冒充缺失。
 * - `tabular-nums` 由 `.pf-num` 统一提供。
 */
export type MoneySize = 'display' | 'l' | 'm' | 's' | 'xs'

const SIZE_CLASS: Record<MoneySize, string> = {
  display: 'text-display font-bold',
  l: 'text-money-l font-bold',
  m: 'text-money-m font-semibold',
  s: 'text-money-s font-medium',
  xs: 'text-money-xs font-medium',
}

export interface MoneyProps {
  value: string | number | null | undefined
  /** `cny`（默认）、`ratio`、`percent`、`pp`、`count`、`days` 或原样单位。 */
  unit?: string | null
  size?: MoneySize
  className?: string
  /** 用于覆盖「率不可得」等特殊口径；缺省按 formatValue 处理。 */
  emptyLabel?: string
}

export function Money({
  value,
  unit = 'cny',
  size = 'm',
  className,
  emptyLabel = EMPTY_VALUE,
}: MoneyProps) {
  const empty = value === null || value === undefined || value === ''
  const text = empty ? emptyLabel : formatValue(value, unit)

  return (
    <span
      className={cn(
        'pf-num whitespace-nowrap',
        SIZE_CLASS[size],
        empty ? 'text-muted' : 'text-ink',
        className,
      )}
      data-empty={empty || undefined}
    >
      {text}
    </span>
  )
}