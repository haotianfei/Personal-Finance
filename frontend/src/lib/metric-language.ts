/**
 * 指标语言 —— 降级码 / 灯四态 / 方向文字的纯映射（design §2.1 前置小重构）。
 *
 * 唯一来源：
 * - `MetaBadge` 只 re-export `degradationLabel`；
 * - `MetricCard` / `Grade` / `charts/meta.ts` 共用本文件；
 * - 「率不可得」是口径文案而非数字格式，只由 `formatRate(null)` 与降级映射产出。
 *
 * 不做：取数、判灯、拼业务句子。
 */
import type { MetricGradeState } from '@/types'

/** 率的不可得文案（附录 A）。 */
export const RATE_UNAVAILABLE_LABEL = '率不可得'
/** grade=null 时的方向缺失文案（「无判据」不是第五态）。 */
export const NO_CRITERION_LABEL = '无判据'

/** 降级码 / 闸门码 → 短标签（码值沿用 meta 契约枚举，只翻译展示文案）。 */
const META_CODE_LABELS: Record<string, string> = {
  NO_DATA: '无数据',
  DATA_MISSING_CARRY_FORWARD: '含沿用值',
  RATE_UNAVAILABLE: RATE_UNAVAILABLE_LABEL,
  PRINCIPAL_NOT_SET: '本金未设',
  TWR_LARGE_FLOW_APPROX: '大额流近似',
  PARTIAL: '部分录入',
  PARTIAL_ENTRY: '部分录入',
  DATA_PARTIAL: '部分录入',
}

/** 未知码原样展示（宁可露出契约码，也不臆造文案）。 */
export function degradationLabel(code: string): string {
  return META_CODE_LABELS[code] ?? code
}

/** 灯四态的短文案；颜色永远配文字，不单独承载语义。 */
const GRADE_LABELS: Record<MetricGradeState, string> = {
  green: '在带内',
  yellow: '轻偏离',
  red: '超阈',
  gray: '不可判',
}

/** 图例顺序（绿 → 黄 → 红 → ⚪）。 */
export const GRADE_STATES: MetricGradeState[] = ['green', 'yellow', 'red', 'gray']

export function gradeLabel(state: MetricGradeState): string {
  return GRADE_LABELS[state]
}

/**
 * direction 现为中文字面量（backend 注册表：中性/越高越好/越低越好/双阈值警戒）；
 * 保留英文枚举映射作前向兼容，未知值原样透出。
 */
const DIRECTION_LABELS: Record<string, string> = {
  higher_better: '越高越好',
  higher_is_better: '越高越好',
  higher: '越高越好',
  up: '越高越好',
  lower_better: '越低越好',
  lower_is_better: '越低越好',
  lower: '越低越好',
  down: '越低越好',
  neutral: '中性',
  band: '适宜带',
  range: '适宜带',
  two_sided_band: '适宜带',
  dual_threshold: '双阈值警戒',
}

/** grade=null 时显示方向文字；空值返回 null（调用方决定是否显示「无判据」）。 */
export function directionLabel(direction?: string | null): string | null {
  if (!direction) return null
  return DIRECTION_LABELS[direction] ?? direction
}