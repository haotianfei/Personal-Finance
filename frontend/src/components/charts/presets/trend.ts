/**
 * 趋势图 option 构造（design §3.3 / §5.4 / §5.6）。
 *
 * 纯函数：数据 → EChartsOption；不取数、不做统计（极值/统计卡由页面算）。
 * - 窗口只切展示数据；categories/series/provenance/tooltip footers 同步切；
 * - carried → 空心点（线）/斜纹（柱）；missing → null（断线/空柱），不落 0；
 * - refLines 默认 muted 虚线，阈值/目标才 warn/accent；
 * - 双轴只由 series.axis 决定；轴刻度紧凑，不写 y 轴名（单位在帧副标题）。
 */
import { tooltipBaseOption, tooltipHtml, type TooltipRow } from '../tooltip'
import { CHART_COLORS, CHART_FONTS, CHART_MOTION, chartTokens } from '../theme'
import { formatAxisValue, formatRate, formatValue, unitKind } from '../format'
import type {
  ChartRenderContext,
  TrendRefLine,
  TrendSeries,
  TrendWindowValue,
  TooltipDetail,
  Unit,
} from '../types'

/** 趋势窗口切片起点；primitive 也用它把 dataIndex 映射回全量索引。 */
export function trendWindowStart(total: number, value?: TrendWindowValue): number {
  return value === undefined || value === 'all' ? 0 : Math.max(0, total - value)
}

/** 系列语义色（图例与图共用；direction 是双向柱，图例用中性色）。 */
export function trendSeriesColor(series: TrendSeries, index: number): string {
  switch (series.color) {
    case 'direction':
      return CHART_COLORS.flat
    case 'muted':
      return CHART_COLORS.faint
    case 'categorical':
      return CHART_COLORS.categorical[index % CHART_COLORS.categorical.length]
    case 'brand':
    default:
      return CHART_COLORS.brand
  }
}

export interface TrendOptionProps {
  categories: readonly string[]
  series: readonly TrendSeries[]
  /** 近 N 期或全部；preset 只切数据。 */
  window?: TrendWindowValue
  refLines?: readonly TrendRefLine[]
  /** tooltip 末行「点击查看本期明细」＋ pointer；事件由 primitive 绑。 */
  clickable?: boolean
  /** 每个账期的口径脚注（全量长度）；preset 随窗口切。 */
  tooltipFooters?: readonly (string | null)[] | null
  /** 每个账期的 tooltip 明细块（全量长度）；preset 随窗口切，窄屏只留标题与观察句。 */
  tooltipDetails?: readonly (TooltipDetail | null)[] | null
}

type LabelParams = { name?: string; dataIndex?: number; data?: { value?: number | null } }

function axisUnitOf(series: TrendSeries | undefined, fallback: Unit): Unit {
  return series?.unit ?? fallback
}

function directionColor(value: number | null): string {
  if (value === null) return CHART_COLORS.flat
  return value >= 0 ? CHART_COLORS.up : CHART_COLORS.down
}

function seriesPaint(series: TrendSeries, index: number, value: number | null): string {
  return series.color === 'direction'
    ? directionColor(value)
    : trendSeriesColor(series, index)
}

function formatSeriesValue(value: number | null, unit: Unit | undefined): string {
  if (value === null) {
    const kind = unitKind(unit)
    return kind === 'ratio' || kind === 'percent' ? formatRate(null, unit ?? null) : '—'
  }
  return formatValue(value, unit)
}

/** ECharts 斜纹：45° 细线，叠在柱色上（沿用值不是缺测）。 */
const CARRIED_DECAL = {
  symbol: 'rect',
  symbolSize: 1,
  dashArrayX: [1, 0],
  dashArrayY: [2, 4],
  rotation: Math.PI / 4,
  color: CHART_COLORS.carryHatch,
}

function refStyle(tone: TrendRefLine['tone'] = 'muted'): {
  line: string
  label: string
} {
  switch (tone) {
    case 'accent':
      return { line: CHART_COLORS.brand, label: CHART_COLORS.brandDeep }
    case 'warn':
      return { line: CHART_COLORS.warn, label: CHART_COLORS.warn }
    case 'muted':
    default:
      return { line: CHART_COLORS.faint, label: CHART_COLORS.muted }
  }
}

function extractValue(params: unknown): number | null {
  const point = params as LabelParams
  const raw = point.data && typeof point.data === 'object' && 'value' in point.data
    ? point.data.value
    : (params as { value?: number | string | null }).value
  if (raw === null || raw === undefined || raw === '') return null
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
}

/**
 * 趋势 option。
 *
 * 线：carried → 白心彩环空心点；missing → null（connectNulls:false）。
 * 柱：每个数据项显式 itemStyle.color（direction 按正负）；carried → decal 斜纹。
 * refLines 只挂到第一条 left 轴系列；若主系列被图例隐藏且没有其它 left 系列，则不画。
 */
export function buildTrendOption(
  { categories, series, window, refLines, clickable = false, tooltipFooters, tooltipDetails }: TrendOptionProps,
  ctx: ChartRenderContext,
): Record<string, unknown> {
  const dense = ctx.dense
  const tokens = chartTokens({ dense, reducedMotion: ctx.reducedMotion })
  const start = trendWindowStart(categories.length, window)
  const visibleCategories = categories.slice(start)
  const footers = tooltipFooters?.slice(start) ?? null
  const details = tooltipDetails?.slice(start) ?? null

  const leftSeries = series.find((item) => (item.axis ?? 'left') === 'left')
  const rightSeries = series.find((item) => item.axis === 'right')
  const leftUnit = axisUnitOf(leftSeries, series[0]?.unit ?? 'cny')
  const rightUnit = axisUnitOf(rightSeries, leftUnit)
  const hasRight = Boolean(rightSeries)
  const refHost = leftSeries
  const refUnit = axisUnitOf(refHost, leftUnit)

  const xTickInterval = dense
    ? Math.max(0, Math.ceil(visibleCategories.length / 6) - 1)
    : 'auto'

  const yAxis = [
    {
      type: 'value',
      splitNumber: tokens.axis.ySplitNumber,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: tokens.axis.labelColor,
        fontSize: tokens.fontSize.axisLabel,
        fontFamily: CHART_FONTS.numeric,
        margin: tokens.axis.labelMargin,
        formatter: (value: number) => formatAxisValue(value, leftUnit),
      },
      splitLine: { show: true, lineStyle: { color: tokens.axis.splitLineColor, width: 1 } },
      axisPointer: {
        label: { formatter: (params: { value: number }) => formatAxisValue(params.value, leftUnit) },
      },
    },
    ...(hasRight
      ? [{
          type: 'value',
          position: 'right',
          splitNumber: tokens.axis.ySplitNumber,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: tokens.axis.labelColor,
            fontSize: tokens.fontSize.axisLabel,
            fontFamily: CHART_FONTS.numeric,
            margin: tokens.axis.labelMargin,
            formatter: (value: number) => formatAxisValue(value, rightUnit),
          },
          splitLine: { show: false },
          axisPointer: {
            label: { formatter: (params: { value: number }) => formatAxisValue(params.value, rightUnit) },
          },
        }]
      : []),
  ]

  const tooltip = {
    ...tooltipBaseOption({ dense, trigger: 'axis' }),
    axisPointer: {
      type: ctx.dense || ctx.coarse ? 'line' : 'cross',
      lineStyle: { color: CHART_COLORS.faint, width: 1 },
      crossStyle: { color: CHART_COLORS.faint, width: 1 },
      label: { backgroundColor: CHART_COLORS.ink, color: CHART_COLORS.inkInverse },
    },
    formatter: (params: unknown) => {
      const list = Array.isArray(params) ? params : [params]
      if (list.length === 0) return ''
      const first = list[0] as { axisValue?: string; dataIndex?: number }
      const index = first.dataIndex ?? visibleCategories.indexOf(first.axisValue ?? '')
      const title = first.axisValue ?? visibleCategories[index] ?? ''
      const rows: TooltipRow[] = []

      for (const raw of list) {
        const point = raw as { seriesName?: string; value?: number; dataIndex?: number; data?: { value?: number | null; color?: string } }
        const item = series.find((candidate) => candidate.label === point.seriesName)
        if (!item) continue
        const value = extractValue(raw)
        const color = item.color === 'direction'
          ? directionColor(value)
          : point.data?.color ?? trendSeriesColor(item, series.indexOf(item))
        rows.push({
          label: item.label,
          value: formatSeriesValue(value, item.unit ?? (item.axis === 'right' ? rightUnit : leftUnit)),
          color,
        })
      }

      const carried = series.some((item) => item.provenance?.[start + index] === 'carried')
      const footerParts = [
        footers?.[index] ?? null,
        footers?.[index] ? null : carried ? '含沿用值' : null,
        clickable ? '点击查看本期明细' : null,
      ].filter(Boolean) as string[]

      return tooltipHtml({
        title,
        rows,
        detail: details?.[index] ?? null,
        footer: footerParts.length ? footerParts.join(' · ') : undefined,
      }, dense)
    },
  }

  const refData = (refLines ?? [])
    .filter((ref) => Number.isFinite(ref.value))
    .map((ref, index) => {
      const style = refStyle(ref.tone)
      return {
        yAxis: ref.value,
        name: ref.label,
        lineStyle: { color: style.line, type: 'dashed', width: 1 },
        label: {
          formatter: `${ref.label}: ${formatValue(ref.value, refUnit)}`,
          color: style.label,
          // 两条参考线数值可能很近（如窄幅资产）：一右一左，避免标签叠字
          position: index % 2 === 0 ? 'insideEndTop' : 'insideStartTop',
          distance: 4,
          fontSize: tokens.fontSize.footnote,
        },
      }
    })

  const seriesOptions = series.map((item, index) => {
    const values = item.values.slice(start)
    const provenance = item.provenance?.slice(start)
    const color = trendSeriesColor(item, index)
    const axisIndex = hasRight && item.axis === 'right' ? 1 : 0
    const cursor = clickable ? 'pointer' : 'default'

    if (item.kind === 'bar') {
      const data = values.map((value, valueIndex) => {
        const state = provenance?.[valueIndex]
        if (state === 'missing' || value === null) return null
        const paint = seriesPaint(item, index, value)
        return {
          value,
          itemStyle: {
            color: paint,
            ...(state === 'carried' ? { decal: CARRIED_DECAL } : {}),
          },
        }
      })
      return {
        name: item.label,
        type: 'bar',
        yAxisIndex: axisIndex,
        z: 3,
        stack: item.stack,
        cursor,
        barMaxWidth: dense ? 16 : 30,
        itemStyle: { borderRadius: [3, 3, 0, 0] },
        data,
      }
    }

    const data = values.map((value, valueIndex) => {
      const state = provenance?.[valueIndex]
      if (state === 'missing' || value === null) return null
      if (state !== 'carried') return value
      return {
        value,
        symbol: 'circle',
        symbolSize: dense ? 6 : 7,
        itemStyle: {
          color: CHART_COLORS.surface,
          borderColor: item.color === 'direction' ? directionColor(value) : color,
          borderWidth: 2,
        },
      }
    })

    // 逐点角标：空心菱形，抬离数据点；不新增系列、不占图例、不进 tooltip。
    const marks = item.marks?.slice(start)
    const markData = marks
      ? values.reduce<{ coord: [string, number] }[]>((acc, value, valueIndex) => {
          if (marks[valueIndex] && value !== null) {
            acc.push({ coord: [visibleCategories[valueIndex] ?? '', value] })
          }
          return acc
        }, [])
      : []

    const isRefHost = Boolean(refHost && item.key === refHost.key)
    return {
      name: item.label,
      type: 'line',
      yAxisIndex: axisIndex,
      cursor,
      smooth: true,
      connectNulls: false,
      showSymbol: values.length <= tokens.line.showSymbolMaxPoints,
      symbol: 'circle',
      symbolSize: dense ? 4 : 5,
      sampling: values.length > 100 ? 'lttb' : undefined,
      lineStyle: {
        color,
        width: item.emphasis ? 2.5 : 2,
        type: item.dashed ? 'dashed' : 'solid',
      },
      itemStyle: { color },
      areaStyle: item.area
        ? {
            opacity: 0.12,
            color: {
              type: 'linear',
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color },
                { offset: 1, color: 'transparent' },
              ],
            },
          }
        : undefined,
      emphasis: { lineStyle: { width: item.emphasis ? 3 : 2.5 } },
      markLine: isRefHost && refData.length > 0
        ? {
            silent: true,
            symbol: ['none', 'none'],
            lineStyle: { type: 'dashed', width: 1 },
            data: refData,
          }
        : undefined,
      markPoint: markData.length > 0
        ? {
            silent: true,
            symbol: 'diamond',
            symbolSize: dense ? 6 : 7,
            symbolOffset: [0, -8],
            itemStyle: {
              color: 'transparent',
              borderColor: CHART_COLORS.warn,
              borderWidth: 1.5,
            },
            label: { show: false },
            data: markData,
          }
        : undefined,
      data,
    }
  })

  return {
    animation: !ctx.reducedMotion,
    animationDuration: CHART_MOTION.lineDraw,
    animationDurationUpdate: 0,
    animationEasing: CHART_MOTION.easing,
    tooltip,
    grid: {
      left: tokens.grid.left,
      right: tokens.grid.right,
      top: tokens.grid.top,
      bottom: tokens.grid.bottom,
      containLabel: tokens.grid.containLabel,
    },
    xAxis: {
      type: 'category',
      data: visibleCategories,
      boundaryGap: true,
      axisLine: { lineStyle: { color: tokens.axis.lineColor } },
      axisTick: { show: false },
      axisLabel: {
        color: tokens.axis.labelColor,
        fontSize: tokens.fontSize.axisLabel,
        fontFamily: CHART_FONTS.numeric,
        margin: tokens.axis.labelMargin,
        rotate: tokens.axis.rotate,
        interval: xTickInterval,
        hideOverlap: true,
      },
      splitLine: { show: false },
    },
    yAxis,
    series: seriesOptions,
  }
}
