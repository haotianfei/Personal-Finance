/**
 * 可视化层公共出口（design §2.1/§2.2）——页面**只** import 这里。
 *
 * 当前（V6 / M4）：`CompositionChart` / `TrendChart` / `ComparisonChart` 已接入；
 * V1.1 `ProgressChart` / `WaterfallChart` 待三期-1。
 *
 * 明确不导出：`Chart`、`registry`、`presets/*`、`primitives/*`（design §2.2/§6.4）。
 * 页面也不得直接 import `echarts*`、`Chart`/`presets`/`primitives`；ESLint 已按 §6.4 设 error。
 */
export { CompositionChart } from './primitives/CompositionChart'
export type { CompositionChartProps } from './primitives/CompositionChart'
export { TrendChart } from './primitives/TrendChart'
export type { TrendChartProps } from './primitives/TrendChart'
export { ComparisonChart } from './primitives/ComparisonChart'
export type { ComparisonChartProps } from './primitives/ComparisonChart'
export type {
  ChartFrameHeight,
  ChartHeight,
  ChartRenderContext,
  ComparisonMode,
  ComparisonRow,
  ComparisonSort,
  CompositionItem,
  CompositionMode,
  FrameBadge,
  FrameBadgeTone,
  FrameLamp,
  LegendItem,
  Provenance,
  SeriesColor,
  TrendRefLine,
  TrendSeries,
  TrendWindowOption,
  TrendWindowValue,
  TooltipDetail,
  TooltipDetailRow,
  Unit,
} from './types'