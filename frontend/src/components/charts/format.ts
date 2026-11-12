/**
 * 图表侧格式出口（design §2.2）：
 * - re-export `lib/format`（值/单位/账期的唯一实现）；
 * - 轴刻度紧凑写法（万/亿、0–1 位小数 + %）。
 *
 * 只做展示格式，不做业务计算；preset / tooltip 都从这里取。
 */
import {
  EMPTY_VALUE,
  MINUS_SIGN,
  formatValue,
  unitKind,
  type Unit,
} from '@/lib/format'

export {
  EMPTY_VALUE,
  MINUS_SIGN,
  formatCount,
  formatCurrency,
  formatDays,
  formatPercent,
  formatPp,
  formatPeriod,
  formatRate,
  formatRatio,
  formatValue,
  unitKind,
} from '@/lib/format'
export type { Unit } from '@/lib/format'

/** `toPrecision` 后去掉多余的 0（12.5 / 1.23 / 120）。 */
function significant(value: number, digits: number): string {
  return Number(value.toPrecision(digits)).toLocaleString('zh-CN', {
    maximumFractionDigits: 2,
    useGrouping: false,
  })
}

/**
 * 金额轴：万/亿紧凑、不带 `¥`、3 位有效数字（§5.3/附录 A）。
 * `125000 → 12.5万`；`123456789 → 1.23亿`；`-12000 → −1.2万`。
 */
export function formatAxisAmount(value: number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const n = Number(value)
  if (!Number.isFinite(n)) return EMPTY_VALUE
  if (n === 0) return '0'
  const abs = Math.abs(n)
  const sign = n < 0 ? MINUS_SIGN : ''
  if (abs >= 1e8) return `${sign}${significant(abs / 1e8, 3)}亿`
  if (abs >= 1e4) return `${sign}${significant(abs / 1e4, 3)}万`
  return `${sign}${Math.round(abs).toLocaleString('zh-CN')}`
}

export interface AxisPercentOptions {
  /** 输入是 0–1 比率（×100 后展示）。默认 false（输入已是 0–100）。 */
  ratio?: boolean
  /** 小数位：0 或 1（附录 A）。默认 1，整数时省略 `.0`。 */
  decimals?: 0 | 1
}

/** 比率轴：0 或 1 位小数 + `%`；`−` 用 U+2212。`20 → 20%`，`20.5 → 20.5%`。 */
export function formatAxisPercent(
  value: number | null | undefined,
  { ratio = false, decimals = 1 }: AxisPercentOptions = {},
): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const n = Number(value)
  if (!Number.isFinite(n)) return EMPTY_VALUE
  const p = ratio ? n * 100 : n
  const body = `${Number(Math.abs(p).toFixed(decimals))}%`
  return p < 0 ? `${MINUS_SIGN}${body}` : body
}

/** 百分点轴：0 或 1 位小数 + `pp`，正数带 `+`（阈值/偏离图）。 */
export function formatAxisPp(value: number | null | undefined, decimals: 0 | 1 = 1): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const n = Number(value)
  if (!Number.isFinite(n)) return EMPTY_VALUE
  const sign = n > 0 ? '+' : n < 0 ? MINUS_SIGN : ''
  return `${sign}${Number(Math.abs(n).toFixed(decimals))}pp`
}

/** 单位感知的轴刻度：金额走万/亿，比率走 %，其余回落到值格式。 */
export function formatAxisValue(value: number | null | undefined, unit?: Unit | null): string {
  switch (unitKind(unit)) {
    case 'cny':
      return formatAxisAmount(value)
    case 'ratio':
      return formatAxisPercent(value, { ratio: true })
    case 'percent':
      return formatAxisPercent(value)
    case 'pp':
      return formatAxisPp(value)
    default:
      return formatValue(value, unit)
  }
}