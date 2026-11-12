import { ArrowDown, ArrowUp } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatCurrency, formatPp, formatValue } from '@/lib/format'

/**
 * 变化量：只在这里使用方向色（design §3.2.3）。
 *
 * 红/绿只表达「涨/跌」，且必须同时有符号与箭头，不让颜色单独承载语义；
 * 金额本体（Money）永远中性墨色。持平/零值不点灯。
 */
export type Direction = 'up' | 'down' | 'flat'
export type DirectionSize = 'm' | 's' | 'xs'

const SIZE_CLASS: Record<DirectionSize, string> = {
  m: 'text-money-m font-semibold',
  s: 'text-money-s font-medium',
  xs: 'text-money-xs font-medium',
}

const TONE_CLASS: Record<Direction, string> = {
  up: 'text-finance-up-text',
  down: 'text-finance-down-text',
  flat: 'text-finance-flat-text',
}

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

function directionOf(n: number | null): Direction {
  if (n === null || n === 0) return 'flat'
  return n > 0 ? 'up' : 'down'
}

export function formatDelta(n: number, unit: string): string {
  if (unit === 'cny' || unit === 'rmb' || unit === '元') {
    const body = formatCurrency(Math.abs(n))
    return `${n > 0 ? '+' : n < 0 ? '\u2212' : ''}${body}`
  }
  // formatPp 自带带符号输出（+6.20pp / −3.15pp / 0.00pp），不能再补一次符号
  if (unit === 'pp' || unit === '百分点' || unit === 'pct_point' || unit === 'percentage_point') {
    return formatPp(n)
  }
  const text = formatValue(Math.abs(n), unit)
  if (n > 0) return `+${text}`
  if (n < 0) return `\u2212${text}`
  return text
}

export interface DirectionTextProps {
  value: string | number | null | undefined
  /** 变化量单位：金额 `cny`、百分点 `pp`、比率 `ratio`/`percent`。 */
  unit?: string
  /** 例：「较上期」「较目标」。 */
  label?: string
  size?: DirectionSize
  showArrow?: boolean
  className?: string
}

export function DirectionText({
  value,
  unit = 'pp',
  label,
  size = 'xs',
  showArrow = true,
  className,
}: DirectionTextProps) {
  const n = toNumber(value)
  const direction = directionOf(n)
  const text = n === null ? '—' : formatDelta(n, unit)
  const Arrow = direction === 'up' ? ArrowUp : ArrowDown

  return (
    <span
      className={cn(
        'inline-flex max-w-full flex-wrap items-baseline gap-x-1 gap-y-0.5',
        SIZE_CLASS[size],
        TONE_CLASS[direction],
        className,
      )}
      data-direction={direction}
    >
      <span className="inline-flex items-center gap-1 whitespace-nowrap">
        {direction !== 'flat' && n !== null && showArrow && (
          <Arrow size={size === 'm' ? 16 : 12} strokeWidth={2.25} aria-hidden />
        )}
        <span className="pf-num">{text}</span>
      </span>
      {label && <span className="whitespace-nowrap text-muted">{label}</span>}
    </span>
  )
}