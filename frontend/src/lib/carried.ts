/**
 * 沿用事实（carried）的展示文案与图表片段。
 *
 * 口径（ADR-0019 D12 / `.scratch/db-rebuild/spec.md` D4）：账本是变更日志，
 * 「该日没有某项的记录」＝值没变，不判「部分录入」。后端只给两条可核对的事实：
 * `carried_items`（计入项中当前值来自更早估值日的项数）与 `carried_max_days`
 * （这些项里最久的已沿用天数）。无沿用 → 0/0，本文件函数一律返回空值。
 */

const MUTED = '#64748b'

/** 图表中沿用点的标记色（与净资产主色一致，空心圆）。 */
const SERIES_COLOR = '#3b82f6'

export interface CarriedPointFields {
  /** 计入项中当前值来自更早估值日的项数。 */
  carried_items?: number | null
  /** 这些项里最久的已沿用天数。 */
  carried_max_days?: number | null
  /** 该点代表日（TrendPoint.date），用于「截至 …」 */
  date?: string | null
}

/** 该点是否含沿用值（carried_items > 0）。 */
export function hasCarried(point: CarriedPointFields | null | undefined): boolean {
  const items = point?.carried_items
  return typeof items === 'number' && items > 0
}

/** 沿用摘要：`3 项沿用 · 最久 32 天`；无沿用返回 null。 */
export function carriedText(
  items?: number | null,
  maxDays?: number | null,
  date?: string | null,
): string | null {
  if (typeof items !== 'number' || items <= 0) return null
  const head = date ? `${items} 项沿用（截至 ${date}）` : `${items} 项沿用`
  if (typeof maxDays === 'number' && maxDays > 0) return `${head} · 最久 ${maxDays} 天`
  return head
}

/** 沿用明细说明；无沿用返回 null。 */
export function carriedDetailText(point: CarriedPointFields): string | null {
  return carriedText(point.carried_items, point.carried_max_days, point.date)
}

/** tooltip 副行（HTML，含换行）：`含沿用值 · …`；无沿用返回 ''。 */
export function carriedTooltipRow(point: CarriedPointFields): string {
  const detail = carriedDetailText(point)
  if (!detail) return ''
  return `<span style="color:${MUTED}">含沿用值 · ${detail}</span><br/>`
}

/** tooltip 里的「（含沿用值）」后缀；无沿用返回 ''。 */
export function carriedTooltipNote(carriedItems?: number | null): string {
  return typeof carriedItems === 'number' && carriedItems > 0
    ? `<span style="color:${MUTED}">（含沿用值）</span>`
    : ''
}

/** 沿用点的空心标记（line series data item 的 symbol 覆盖项）。 */
export function carriedPointMarker(size: number) {
  return {
    symbol: 'circle',
    symbolSize: size,
    itemStyle: { color: '#ffffff', borderColor: SERIES_COLOR, borderWidth: 2 },
  }
}