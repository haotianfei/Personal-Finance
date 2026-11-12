/**
 * 图表层共享类型（design §3.2 / §4 / §5）。
 *
 * 只放「跨 preset / primitive / frame 共用」的形状：
 * 单位与来源态、图帧徽标、图例项、渲染环境、三族数据形状。
 * 不放业务实体、不放 React 组件 props 的页面字段（那些由 primitive 在 V3+ 声明）。
 */
import type { Unit } from '@/lib/format'
import type { MetricGradeState } from '@/types'

export type { Unit }

/**
 * 数据来源态（画进图形的那一层）：
 * - `measured` 实测；
 * - `carried` 含沿用值（白心彩环空心点/斜纹，不是缺测）；
 * - `missing` 缺测（断线，不落 0）。
 */
export type Provenance = 'measured' | 'carried' | 'missing'

/** 图帧高度档（§5.4；具体像素由 `chartTokens().heights` 给）。 */
export type ChartHeight = 'sm' | 'md' | 'lg' | 'xl'

/** 图帧高度：档位，或为保留既有页面的固定像素（如首页 300px treemap）。 */
export type ChartFrameHeight = ChartHeight | number

/** 徽标语义色，与 `MetaBadge` tone 同一套。 */
export type FrameBadgeTone = 'warn' | 'info' | 'muted' | 'blocked'

/** 图帧口径徽标（由 `meta.ts` 生成，`ChartFrame` 渲染）。 */
export interface FrameBadge {
  code: string
  label: string
  detail?: string | null
  tone: FrameBadgeTone
}

/** 图例操作区的一项；缺 `onToggle` ＝不可切换（结构图）。 */
export interface LegendItem {
  key: string
  label: string
  color: string
  active?: boolean
  valueText?: string
  onToggle?: () => void
  /** 细指针悬停/键盘聚焦时高亮对应系列；粗指针不触发。 */
  onHover?: (hovered: boolean) => void
}

/** 渲染环境（preset 的第二参数；不含数据）。 */
export interface ChartRenderContext {
  /** 容器宽 < 560px。 */
  dense: boolean
  /** 粗指针/触屏：禁 dataZoom/brush/roam。 */
  coarse: boolean
  /** `prefers-reduced-motion: reduce`：canvas 关闭动画。 */
  reducedMotion: boolean
}

/** 单指标图的灯位；多系列趋势/结构图不判灯。 */
export interface FrameLamp {
  state: MetricGradeState
  note?: string | null
}

/** 系列配色语义：品牌、方向（仅变化量）、弱化、分类。 */
export type SeriesColor = 'brand' | 'direction' | 'muted' | 'categorical'

/** 趋势系列（design §3.3）。`values` 与 `categories` 等长。 */
export interface TrendSeries {
  key: string
  label: string
  kind: 'line' | 'bar'
  axis?: 'left' | 'right'
  values: (number | null)[]
  /** `missing` 优先于 `carried`。 */
  provenance?: (Provenance | null)[]
  /**
   * 逐点角标：true 时在该点上方画一个空心小菱形（安静提示）。
   * 不新增系列、不改线型、不占图例；与 `provenance` 可同时存在。
   */
  marks?: readonly (boolean | null)[]
  color?: SeriesColor
  emphasis?: boolean
  area?: boolean
  dashed?: boolean
  stack?: string
  /** 双轴时逐系列声明。 */
  unit?: Unit
}

/** tooltip 明细行：短标签（新增/消失）＋名称＋已格式化值＋可选注（如最后记录日）。 */
export interface TooltipDetailRow {
  tag?: string
  label: string
  value: string
  note?: string
}

/**
 * 逐点 tooltip 明细块（如「同日新增/消失」）。
 * 只承载已格式化的字符串——HTML 一律由 `tooltip.ts` 拼，preset 不手写标签。
 */
export interface TooltipDetail {
  heading: string
  note?: string
  rows: readonly TooltipDetailRow[]
}

/** 趋势窗口值：近 N 期或全部。 */
export type TrendWindowValue = number | 'all'

/** 趋势窗口 chip（primitive 的 actions 槽）。 */
export interface TrendWindowOption {
  value: TrendWindowValue
  label: string
}

/** 趋势参考线；默认 muted 虚线，阈值/目标才 warn/accent（design §3.3/§6.5）。 */
export interface TrendRefLine {
  value: number
  label: string
  tone?: 'muted' | 'accent' | 'warn'
}

/** 结构图模式（design §3.4）；primitive 与 preset 共用。 */
export type CompositionMode = 'pie' | 'donut' | 'rose' | 'treemap' | 'sunburst'

/** 结构项（design §3.4）。`share` 由调用方给（0–100），层内不现算。 */
export interface CompositionItem {
  key: string
  label: string
  value: number
  share?: number
  note?: string
  provenance?: Provenance
  children?: CompositionItem[]
}

/** 对比行（design §3.5）。`delta` 带符号。 */
export interface ComparisonRow {
  key: string
  label: string
  delta: number | null
  target?: number | null
  actual?: number | null
  note?: string
}

/** 对比图模式：v1 实现 delta；dumbbell 仅冻结契约。 */
export type ComparisonMode = 'delta' | 'dumbbell'

/** 对比行排序：给定顺序或按 |delta| 降序。 */
export type ComparisonSort = 'given' | 'absDelta'