/**
 * 资产项身份漂移的展示文案与明细提取（spec §3，additive）。
 *
 * 后端在趋势点上追加 `added` / `vanished` / `drift_suspect` / `drift_amount`；
 * 旧后端不返回这些字段时，本文件的函数一律返回空值，页面表现与改造前完全一致。
 *
 * 纪律：文案只陈述观测（同日有新增/消失），不做归因断言（不说「就是改名」）；
 * 金额一律走 `lib/format`，本文件不做第二套数字格式。
 */
import type { TooltipDetail, TooltipDetailRow } from '@/components/charts'
import { formatDate, formatValue } from '@/lib/format'
import type { TrendDriftAddedItem, TrendDriftVanishedItem } from '@/types'

/** 短标签：与「含沿用值」同级的注记词，用于图帧徽标 / 统计卡角标。 */
export const DRIFT_LABEL = '同日新增/消失'

/** 观察口径句（写观测，不写结论）。 */
export const DRIFT_OBSERVATION = '该点存在同日新增/消失的资产项，可能是改名或拆分未登记'

/** 明细最多列几项（Top-N）。 */
export const DRIFT_TOP_N = 5

/** 趋势点上的漂移字段（全部可选：缺字段＝旧后端/非净值维度）。 */
export interface DriftPointFields {
  drift_suspect?: boolean | null
  drift_amount?: string | null
  added?: TrendDriftAddedItem[] | null
  vanished?: TrendDriftVanishedItem[] | null
}

export interface DriftEntry {
  key: string
  kind: 'added' | 'vanished'
  /** 行首短标签：新增 / 消失。 */
  tag: string
  name: string
  /** 十进制字符串，交给 Money / formatValue。 */
  amount: string
  /** 消失项的最后记录日（展示为「最后记录 2026-08-31」）。 */
  note?: string
}

export interface DriftDetail {
  /** 已按金额降序取前 N 项。 */
  entries: DriftEntry[]
  addedCount: number
  vanishedCount: number
  total: number
  /** 有明细未列出（total > entries.length）。 */
  truncated: boolean
}

function amountValue(value: string | null | undefined): number {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

/** 是否需要提示：只看后端判定位（缺字段 → false）。 */
export function isDriftSuspect(point: DriftPointFields | null | undefined): boolean {
  return point?.drift_suspect === true
}

/**
 * 提取该点的新增/消失明细（按金额降序、最多 `limit` 项）。
 * 非可疑点返回 null —— 调用方据此完全跳过渲染。
 */
export function driftDetail(
  point: DriftPointFields,
  limit: number = DRIFT_TOP_N,
): DriftDetail | null {
  if (!isDriftSuspect(point)) return null

  const added = Array.isArray(point.added) ? point.added : []
  const vanished = Array.isArray(point.vanished) ? point.vanished : []

  const sortable = [
    ...added.map((item, index) => ({
      key: `added-${item.item_id ?? index}`,
      kind: 'added' as const,
      tag: '新增',
      name: item.name ?? '',
      amount: item.amount ?? '0',
      note: undefined as string | undefined,
      weight: amountValue(item.amount),
    })),
    ...vanished.map((item, index) => ({
      key: `vanished-${item.item_id ?? index}`,
      kind: 'vanished' as const,
      tag: '消失',
      name: item.name ?? '',
      amount: item.amount ?? '0',
      note: item.last_date ? `最后记录 ${formatDate(item.last_date)}` : undefined,
      weight: amountValue(item.amount),
    })),
  ]

  // 稳定排序：金额相同保持后端顺序（新增在前、消失在后）。
  sortable.sort((a, b) => b.weight - a.weight)
  const entries: DriftEntry[] = sortable.slice(0, Math.max(0, limit)).map((entry) => ({
    key: entry.key,
    kind: entry.kind,
    tag: entry.tag,
    name: entry.name,
    amount: entry.amount,
    note: entry.note,
  }))

  return {
    entries,
    addedCount: added.length,
    vanishedCount: vanished.length,
    total: added.length + vanished.length,
    truncated: added.length + vanished.length > entries.length,
  }
}

/** 计数摘要：`新增 50 项 / 消失 11 项`；两侧都空返回空串。 */
export function driftCountsText(detail: DriftDetail): string {
  const parts: string[] = []
  if (detail.addedCount > 0) parts.push(`新增 ${detail.addedCount} 项`)
  if (detail.vanishedCount > 0) parts.push(`消失 ${detail.vanishedCount} 项`)
  return parts.join(' / ')
}

/** 明细块标题：`同日新增/消失 · 新增 50 项 / 消失 11 项`。 */
export function driftHeading(detail: DriftDetail): string {
  const counts = driftCountsText(detail)
  return counts ? `${DRIFT_LABEL} · ${counts}` : DRIFT_LABEL
}

/** 观察句（截断时补一句排序/条数说明，仍是观测口径）。 */
export function driftObservationText(detail: DriftDetail): string {
  return detail.truncated
    ? `${DRIFT_OBSERVATION}（按该期金额列前 ${detail.entries.length} 项）`
    : DRIFT_OBSERVATION
}

/** 交给图表 tooltip 的明细块（纯字符串；HTML 由 `charts/tooltip` 拼）。 */
export function driftTooltipBlock(detail: DriftDetail): TooltipDetail {
  const rows: TooltipDetailRow[] = detail.entries.map((entry) => ({
    tag: entry.tag,
    label: entry.name,
    value: formatValue(entry.amount, 'cny'),
    note: entry.note,
  }))
  return {
    heading: driftHeading(detail),
    note: driftObservationText(detail),
    rows,
  }
}

/** 徽标 / 角标的悬停标题：`新增 50 项 / 消失 11 项；该点存在同日新增/消失的资产项…`。 */
export function driftBadgeTitle(detail: DriftDetail | null): string | undefined {
  if (!detail) return undefined
  const counts = driftCountsText(detail)
  return counts ? `${counts}；${DRIFT_OBSERVATION}` : DRIFT_OBSERVATION
}