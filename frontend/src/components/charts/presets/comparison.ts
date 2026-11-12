/**
 * 对比图 option 构造（design §3.5 / §5.1 / §5.3）。
 *
 * v1 只实现 `delta` 横向棒棒糖：
 * - 条从 0 到 delta，dot 在末端，零线 ink；
 * - ±threshold 为 amber 8% 参考带 + 1px dashed 边界；
 * - 右侧读数用 `formatPp`，颜色只表方向；
 * - 行序 given / |delta| 由 primitive 先排好，preset 不再改序。
 *
 * `dumbbell` 仅冻结契约，不在本 preset 实现。
 */
import { tooltipBaseOption, tooltipHtml, type TooltipRow } from '../tooltip'
import { CHART_COLORS, CHART_FONTS, CHART_MOTION, chartTokens } from '../theme'
import { formatPercent, formatValue } from '../format'
import type { ChartRenderContext, ComparisonRow, ComparisonSort, Unit } from '../types'

/** 对比行排序；稳定排序，空 delta 按 0 处理。 */
export function sortComparisonRows(
  rows: readonly ComparisonRow[],
  sort: ComparisonSort = 'given',
): ComparisonRow[] {
  if (sort !== 'absDelta') return [...rows]
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const diff = Math.abs(b.row.delta ?? 0) - Math.abs(a.row.delta ?? 0)
      return diff !== 0 ? diff : a.index - b.index
    })
    .map(({ row }) => row)
}

export interface ComparisonOptionProps {
  rows: readonly ComparisonRow[]
  unit?: Unit
  threshold?: number | null
}

function directionColor(delta: number | null): string {
  if (delta === null) return CHART_COLORS.flat
  if (delta > 0) return CHART_COLORS.up
  if (delta < 0) return CHART_COLORS.down
  return CHART_COLORS.flat
}

function extractDelta(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (value && typeof value === 'object' && 'value' in value) {
    const raw = (value as { value?: unknown }).value
    return typeof raw === 'number' && Number.isFinite(raw) ? raw : null
  }
  return null
}

/**
 * 偏离 option。
 *
 * 绘制顺序：阈值参考带（bar 的 markArea）→ 零线/阈值边界（markLine）→
 * 0→delta 细条（bar）→ 末端 dot + 读数（line with symbols，无连线）。
 */
export function buildComparisonOption(
  { rows, unit = 'pp', threshold }: ComparisonOptionProps,
  ctx: ChartRenderContext,
): Record<string, unknown> {
  const dense = ctx.dense
  const tokens = chartTokens({ dense, reducedMotion: ctx.reducedMotion })
  const thresholdAbs = threshold === null || threshold === undefined ? null : Math.abs(threshold)
  const maxAbs = Math.max(
    1,
    ...rows.map((row) => Math.abs(row.delta ?? 0)),
    thresholdAbs ?? 0,
  )
  const bound = maxAbs * 1.15
  const labels = rows.map((row) => row.label)
  const deltas = rows.map((row) => row.delta)

  const markLineData = [
    ...(thresholdAbs !== null && thresholdAbs > 0
      ? [
          {
            xAxis: -thresholdAbs,
            lineStyle: { color: CHART_COLORS.warn, width: 1, type: 'dashed' },
            label: { show: false },
          },
          {
            xAxis: thresholdAbs,
            lineStyle: { color: CHART_COLORS.warn, width: 1, type: 'dashed' },
            label: { show: false },
          },
        ]
      : []),
    {
      // 零线最后画，阈值很窄时仍保持 ink 可见（§5.1 零线 ink）
      xAxis: 0,
      lineStyle: { color: CHART_COLORS.ink, width: 1.5, type: 'solid' },
      label: { show: false },
    },
  ]

  const tooltip = {
    ...tooltipBaseOption({ dense, trigger: 'axis' }),
    axisPointer: { type: 'shadow' },
    formatter: (params: unknown) => {
      const list = Array.isArray(params) ? params : [params]
      const first = list[0] as { dataIndex?: number } | undefined
      const index = first?.dataIndex ?? -1
      const row = rows[index]
      if (!row) return ''
      const rowsTip: TooltipRow[] = [
        { label: '目标', value: formatPercent(row.target), muted: true },
        { label: '实际', value: formatPercent(row.actual), muted: true },
        {
          label: '偏离',
          value: formatValue(row.delta, unit),
          color: directionColor(row.delta),
        },
      ]
      return tooltipHtml({ title: row.label, rows: rowsTip, footer: row.note })
    },
  }

  const xAxisLabelFormatter = (value: number) => formatValue(value, unit)

  return {
    animation: !ctx.reducedMotion,
    animationDuration: CHART_MOTION.barGrow,
    animationDurationUpdate: 0,
    animationEasing: CHART_MOTION.easing,
    tooltip,
    grid: {
      left: tokens.grid.left,
      right: dense ? 56 : 72,
      top: 8,
      bottom: tokens.grid.bottom,
      containLabel: true,
    },
    xAxis: {
      type: 'value',
      min: -bound,
      max: bound,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: tokens.axis.labelColor,
        fontSize: tokens.fontSize.axisLabel,
        fontFamily: CHART_FONTS.numeric,
        margin: tokens.axis.labelMargin,
        formatter: xAxisLabelFormatter,
        hideOverlap: true,
      },
      splitLine: { show: true, lineStyle: { color: tokens.axis.splitLineColor, width: 1 } },
      axisPointer: {
        label: { formatter: (params: { value: number }) => xAxisLabelFormatter(params.value) },
      },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: labels,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: tokens.axis.labelColor,
        fontSize: tokens.fontSize.axisLabel,
        fontFamily: CHART_FONTS.body,
        margin: 8,
        width: dense ? 88 : 118,
        overflow: 'truncate',
        align: 'right',
      },
      splitLine: { show: false },
    },
    series: [
      {
        name: '偏离条',
        type: 'bar',
        barWidth: 3,
        z: 2,
        data: deltas,
        itemStyle: {
          color: (params: { dataIndex: number }) =>
            directionColor(deltas[params.dataIndex] ?? null),
          borderRadius: 1,
        },
        markArea:
          thresholdAbs !== null && thresholdAbs > 0
            ? {
                silent: true,
                itemStyle: { color: CHART_COLORS.warnWash },
                data: [[{ xAxis: -thresholdAbs }, { xAxis: thresholdAbs }]],
              }
            : undefined,
        markLine: {
          silent: true,
          symbol: ['none', 'none'],
          data: markLineData,
        },
      },
      {
        name: '偏离值',
        type: 'line',
        z: 3,
        data: rows.map((row) => ({
          value: row.delta,
          label: {
            color: directionColor(row.delta),
            position: row.delta !== null && row.delta < 0 ? 'left' : 'right',
          },
        })),
        symbol: 'circle',
        symbolSize: 8,
        showSymbol: true,
        lineStyle: { width: 0, opacity: 0 },
        itemStyle: {
          color: (params: { dataIndex: number }) =>
            directionColor(deltas[params.dataIndex] ?? null),
        },
        label: {
          show: true,
          position: 'right',
          distance: 5,
          color: CHART_COLORS.inkSoft,
          fontSize: tokens.fontSize.axisLabel,
          fontFamily: CHART_FONTS.numeric,
          formatter: (params: { value: number }) => formatValue(extractDelta(params.value), unit),
        },
        emphasis: { scale: false },
      },
    ],
  }
}
