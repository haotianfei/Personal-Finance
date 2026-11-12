/**
 * 口径映射：gate / degradation → 图帧徽标（design §4.2、§4.5）。
 *
 * 排序：阻断 → 来源（沿用/本金未设/大额流近似）→ 判据（阈值/来源）→ 一般说明；
 * 同码去重（先到者胜，gates 在 degradations 之前）；最多 3 个常显，其余折叠为「＋N 项口径」。
 *
 * 不取数、不判灯：灯由调用方给，这里只翻译码值。
 */
import { degradationLabel } from '@/lib/metric-language'
import type { FrameBadge, FrameBadgeTone } from './types'

/** 结构兼容 `@/types` 的 Degradation，避免层内耦合全局契约。 */
export interface MetaDegradationLike {
  code: string
  message?: string | null
  scope?: string | null
}

/** 结构兼容 `@/types` 的 MetricGate。 */
export interface MetricGateLike {
  code: string
  note?: string | null
  blocking?: boolean
}

/** 图帧常显徽标上限（第 4 个起折叠）。 */
export const MAX_VISIBLE_BADGES = 3

/** 数据来源类（非阻断但也非纯说明）。 */
const SOURCE_CODES = new Set([
  'DATA_MISSING_CARRY_FORWARD',
  'PARTIAL',
  'PARTIAL_ENTRY',
  'DATA_PARTIAL',
  'PRINCIPAL_NOT_SET',
  'TWR_LARGE_FLOW_APPROX',
])

const BLOCKED_CODES = new Set(['RATE_UNAVAILABLE'])
const MUTED_CODES = new Set(['NO_DATA'])

/** 码值 → tone；未知码按一般说明处理（标签原样透出）。 */
export function degradationTone(code: string, blocking = false): FrameBadgeTone {
  if (blocking || BLOCKED_CODES.has(code)) return 'blocked'
  if (MUTED_CODES.has(code)) return 'muted'
  if (SOURCE_CODES.has(code)) return 'warn'
  return 'muted'
}

/** 排序权重：阻断 0 / 来源 1 / 判据 2 / 一般 3。 */
function badgeRank(badge: FrameBadge): number {
  if (badge.tone === 'blocked') return 0
  if (badge.tone === 'info') return 2
  if (SOURCE_CODES.has(badge.code)) return 1
  return 3
}

/** 排序稳定：同权重保持传入顺序。 */
export function sortFrameBadges(badges: readonly FrameBadge[]): FrameBadge[] {
  return badges
    .map((badge, index) => ({ badge, index }))
    .sort((a, b) => badgeRank(a.badge) - badgeRank(b.badge) || a.index - b.index)
    .map((entry) => entry.badge)
}

/** 同码去重（先到者胜），保留首个的 detail。 */
export function dedupeFrameBadges(badges: readonly FrameBadge[]): FrameBadge[] {
  const seen = new Set<string>()
  const out: FrameBadge[] = []
  for (const badge of badges) {
    if (seen.has(badge.code)) continue
    seen.add(badge.code)
    out.push(badge)
  }
  return out
}

/** 降级数组 → 徽标（未排序）。 */
export function degradationBadges(
  degradations?: readonly MetaDegradationLike[] | null,
): FrameBadge[] {
  if (!degradations?.length) return []
  return degradations.map((item) => ({
    code: item.code,
    label: degradationLabel(item.code),
    detail: item.message ?? null,
    tone: degradationTone(item.code),
  }))
}

/** 闸门数组 → 徽标（blocking → 阻断色；否则来源/一般）。 */
export function gateBadges(gates?: readonly MetricGateLike[] | null): FrameBadge[] {
  if (!gates?.length) return []
  return gates.map((gate) => ({
    code: gate.code,
    label: degradationLabel(gate.code),
    detail: gate.note ?? null,
    tone: degradationTone(gate.code, Boolean(gate.blocking)),
  }))
}

/**
 * 图帧徽标的唯一入口：合并 gates + degradations + 额外判据（阈值等），
 * 排序 → 去重。调用方只负责渲染 `collapseFrameBadges()` 的结果。
 */
export function frameBadgesFrom(
  meta?: { degradations?: readonly MetaDegradationLike[] | null } | null,
  gates?: readonly MetricGateLike[] | null,
  extras: readonly FrameBadge[] = [],
): FrameBadge[] {
  const merged = [...gateBadges(gates), ...degradationBadges(meta?.degradations), ...extras]
  return dedupeFrameBadges(sortFrameBadges(merged))
}

export interface CollapsedBadges {
  visible: FrameBadge[]
  hidden: FrameBadge[]
  /** 折叠提示文案（无隐藏项时为空串）。 */
  overflowLabel: string
}

/** 最多 `max` 个常显；第 `max + 1` 个起折叠为「＋N 项口径」。 */
export function collapseFrameBadges(
  badges: readonly FrameBadge[],
  max = MAX_VISIBLE_BADGES,
): CollapsedBadges {
  const visible = badges.slice(0, max)
  const hidden = badges.slice(max)
  return {
    visible,
    hidden,
    overflowLabel: hidden.length ? `＋${hidden.length} 项口径` : '',
  }
}