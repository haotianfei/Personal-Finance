/**
 * 值 / 单位 / 账期格式化 —— 全站唯一实现（design §2.4 纪律三、附录 A）。
 *
 * 调用方：
 * - 卡片与页面 → `@/lib/format`
 * - 图表（preset / tooltip / 轴刻度）→ `@/components/charts/format`（re-export 本文件）
 *
 * 口径（附录 A 真值表）：
 * - `cny`      `¥` + 千分位 + 2 位           → `¥1,234,567.00`
 * - `ratio`    0–1 → ×100 + 2 位 + `%`       → `40.00%`
 * - `percent`  已是 0–100 → 2 位 + `%`       → `40.00%`
 * - `pp`       带符号 + 2 位 + `pp`          → `+6.20pp`
 * - `count`    整数 + 千分位                 → `1,024`
 * - `days`     整数 + `天`                   → `32 天`
 * - 未知单位   原样透出，不猜                → `7.5 xyz`
 * - null/空    `—`（率类先走 `formatRate` 给「率不可得」）
 * - 负号       显示用 U+2212 `−`，数据用 ASCII `-`
 *
 * `ratio`（0–1）与 `percent`（0–100）分开实现：禁止一个 `formatPercent` 通吃，
 * 否则 ×100 口径只能靠调用点自觉。
 */
import { RATE_UNAVAILABLE_LABEL } from '@/lib/metric-language'

/** 展示用负号（U+2212）。数据与 API 仍用 ASCII `-`。 */
export const MINUS_SIGN = '\u2212'
/** 空值占位（附录 A）。 */
export const EMPTY_VALUE = '—'

export type Unit =
  | 'cny'
  | 'ratio'
  | 'percent'
  | 'pp'
  | 'count'
  | 'days'
  | (string & {})

type NumericInput = string | number | null | undefined

const CURRENCY_UNITS = new Set(['cny', 'rmb', 'yuan', 'currency', 'amount', 'money', '元', '金额'])
const COUNT_UNITS = new Set(['count', 'number', 'num', 'items', '项', '个'])
const DAYS_UNITS = new Set(['days', 'day', '天'])
const POINT_UNITS = new Set(['pp', 'pct_point', 'percentage_point', '百分点'])
/** 0–1 口径（MetricCard 旧口径：×100 展示）。 */
const RATIO_UNITS = new Set(['ratio', 'rate', '率', '占比', '比例', '收益率'])
/** 0–100 口径（比例端点直接返回的 percent）。 */
const PERCENT_UNITS = new Set(['pct', 'percent', 'percentage', '%'])

function normalizeUnit(unit?: string | null): string {
  return (unit ?? '').trim().toLowerCase()
}

/** 单位归类：格式分派的唯一判定点（未知单位原样透出）。 */
export function unitKind(unit?: string | null): 'cny' | 'ratio' | 'percent' | 'pp' | 'count' | 'days' | 'unknown' {
  const u = normalizeUnit(unit)
  if (CURRENCY_UNITS.has(u)) return 'cny'
  if (POINT_UNITS.has(u)) return 'pp'
  if (RATIO_UNITS.has(u)) return 'ratio'
  if (PERCENT_UNITS.has(u)) return 'percent'
  if (COUNT_UNITS.has(u)) return 'count'
  if (DAYS_UNITS.has(u)) return 'days'
  return 'unknown'
}

function toFiniteNumber(value: NumericInput): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

/** 千分位 + 固定小数位的绝对值，负号另行拼接（保证字形可控）。 */
function groupedAbs(n: number, digits: number): string {
  return Math.abs(n).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 金额：`¥` + 千分位 + 2 位。 */
export function formatCurrency(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const body = groupedAbs(n, 2)
  if (n < 0) return `${MINUS_SIGN}¥${body}`
  return `¥${body}`
}

/** 固定小数位数值（输入框/计算器内部显示用，不带千分位）。 */
export function formatDecimal(value: NumericInput, digits = 2): string {
  const n = toFiniteNumber(value)
  if (n === null) return ''
  return n.toFixed(digits)
}

/** 金额变化量文本：+¥ / −¥ / ¥0.00（用于无法使用 DirectionText 的纯文本位置）。 */
export function formatSignedCurrency(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const body = groupedAbs(n, 2)
  if (n > 0) return `+¥${body}`
  if (n < 0) return `${MINUS_SIGN}¥${body}`
  return `¥${body}`
}

/** 日期时间：YYYY/MM/DD HH:mm；非法/空值原样或 —。 */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * 相对时间：`刚刚` / `N 分钟前` / `N 小时前` / `N 天前`（「上次测试」这类易变事实）。
 *
 * 边界：
 * - `null` / `undefined` / `''` → `—`（`EMPTY_VALUE`）。调用方**不要**拿「空」当
 *   「刚刚」用：从未发生的事实不该显示成刚发生过。
 * - 非法输入 → 原样透出字符串（与 `formatDateTime` 同口径，不猜）。
 * - 按**时间差**分桶，不按日历天：<1min 刚刚，<60min 分钟，<24h 小时，其余天。
 * - 未来时间（两边时钟偏差）归「刚刚」，不显示负数。
 * - `now` 可注入（单测用），默认 `Date.now()`。
 * - 后端 `isoformat()` 给的是不带时区的本地时间串（如 `2026-11-06T14:30:00.123456`），
 *   这里按浏览器本地时区解析；超过 3 位的小数秒先裁到毫秒重解析（部分引擎只认 3 位）。
 */
export function formatRelativeTime(
  value: string | number | Date | null | undefined,
  now: number = Date.now(),
): string {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  let date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime()) && typeof value === 'string') {
    const trimmed = value.replace(/\.(\d{3})\d+$/, '.$1')
    if (trimmed !== value) date = new Date(trimmed)
  }
  const ms = date.getTime()
  if (Number.isNaN(ms)) return String(value)

  const diffMs = Math.max(0, now - ms)
  const minutes = Math.floor(diffMs / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

/** 文件大小：B/KB/MB/GB，最多 2 位小数。 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return EMPTY_VALUE
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(Math.max(0, Math.floor(Math.log(bytes) / Math.log(1024))), units.length - 1)
  const value = bytes / Math.pow(1024, i)
  return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)} ${units[i]}`
}

/** 比率：0–1 → ×100 + 2 位 + `%`。 */
export function formatRatio(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const sign = n < 0 ? MINUS_SIGN : ''
  return `${sign}${groupedAbs(n * 100, 2)}%`
}

/** 百分比：已是 0–100 → 2 位 + `%`。 */
export function formatPercent(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const sign = n < 0 ? MINUS_SIGN : ''
  return `${sign}${groupedAbs(n, 2)}%`
}

/** 偏离百分点：带符号 + 2 位 + `pp`。 */
export function formatPp(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const sign = n > 0 ? '+' : n < 0 ? MINUS_SIGN : ''
  return `${sign}${groupedAbs(n, 2)}pp`
}

/** 计数：整数 + 千分位。 */
export function formatCount(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const sign = n < 0 ? MINUS_SIGN : ''
  return `${sign}${groupedAbs(n, 0)}`
}

/** 天数：整数 + `天`。 */
export function formatDays(value: NumericInput): string {
  const n = toFiniteNumber(value)
  if (n === null) return EMPTY_VALUE
  const sign = n < 0 ? MINUS_SIGN : ''
  return `${sign}${groupedAbs(Math.round(n), 0)} 天`
}

/** 单位感知的通用格式化；未知单位原样透出，不猜。 */
export function formatValue(value: NumericInput, unit?: string | null): string {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  const n = toFiniteNumber(value)
  if (n === null) return String(value)
  switch (unitKind(unit)) {
    case 'cny':
      return formatCurrency(n)
    case 'pp':
      return formatPp(n)
    case 'ratio':
      return formatRatio(n)
    case 'percent':
      return formatPercent(n)
    case 'count':
      return formatCount(n)
    case 'days':
      return formatDays(n)
    default: {
      // 未知单位原样透出，不猜；有单位就附在值后（附录 A：`7.5 xyz`）。
      const u = normalizeUnit(unit)
      return u ? `${String(value)} ${(unit ?? '').trim()}` : String(value)
    }
  }
}

/**
 * 率类格式化：不可得时返回口径文案「率不可得」，而不是 `—` 或 0。
 * 「率不可得」只在 `formatRate(null)` 与降级码映射里出现（附录 A）。
 */
export function formatRate(value: NumericInput, unit: string | null = 'ratio'): string {
  if (value === null || value === undefined || value === '') return RATE_UNAVAILABLE_LABEL
  return formatValue(value, unit)
}

/** 账期键 → 展示标签；后端已给可读账期时原样返回，仅裁 ISO 日期的粒度。 */
export function formatPeriod(period: string | null | undefined, periodType?: string | null): string {
  if (!period) return EMPTY_VALUE
  if (!/^\d{4}-\d{2}(-\d{2})?$/.test(period)) return period
  switch (periodType) {
    case 'day':
    case 'week':
      return period.slice(0, 10)
    case 'month':
      return period.slice(0, 7)
    case 'quarter':
    case 'year':
      return period.slice(0, 4)
    default:
      return period
  }
}
/**
 * 日期：账本日期统一 `YYYY-MM-DD`；带时间的 ISO 输入只取日期部分。
 * 非 ISO 的可读日期原样返回（后端已给展示值时不再二次加工）。
 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE
  const match = String(value).match(/^(\d{4}-\d{2}-\d{2})/)
  return match ? match[1] : String(value)
}

/** 今天（本地 ISO 日期，用于日期输入默认值；展示仍走 formatDate）。 */
export function todayDate(): string {
  return new Date().toISOString().slice(0, 10)
}
