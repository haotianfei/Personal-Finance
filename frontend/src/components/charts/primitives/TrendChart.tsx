'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import type { ECharts } from 'echarts/core'
import Chart from '../Chart'
import { ChartFrame, type ChartFrameBaseProps } from '../ChartFrame'
import { useChartEnvironment } from '../environment'
import { formatValue } from '../format'
import { buildTrendOption, trendSeriesColor, trendWindowStart } from '../presets/trend'
import type {
  LegendItem,
  TooltipDetail,
  TrendRefLine,
  TrendSeries,
  TrendWindowOption,
  TrendWindowValue,
} from '../types'

/**
 * 趋势 primitive（design §3.3）—— 第二个真实消费者。
 *
 * 只做组合：数据 + 图帧 props → `buildTrendOption` + `ChartFrame` + `Chart`。
 * - 不取数、不做统计：窗口只切展示数据；统计卡/极值由页面算；
 * - 多系列图例可切换；单系列才允许灯（多系列强制不点灯）；
 * - 数据表跟随窗口（与图同源），「全部」档即全量；图例/表/图读数同源
 *   （依据 `.scratch/chart-table-window-perf/spec.md` D1；caption 措辞见 D5）；
 * - window chips 只改展示窗口：图、数据表、图例读数一起切，统计卡仍由页面按窗口算。
 */

/**
 * 数据表追加列（spec `.scratch/analysis-detail-prune` D3）。
 *
 * `values` 按**全量账期**下标对齐（调用方给全量长度；本组件渲染时按窗口 `start` 切片，
 * 见文件头契约与 `table` 的 rows）；两侧长度不一致时缺位渲染 `—`。
 */
export type TrendTableColumn = { label: string; values: readonly (string | number)[] }

export interface TrendChartProps
  extends Omit<ChartFrameBaseProps, 'legend' | 'a11yTable' | 'bodyRef'> {
  categories: readonly string[]
  series: readonly TrendSeries[]
  window?: { value: TrendWindowValue; onChange: (value: TrendWindowValue) => void }
  windowOptions?: readonly TrendWindowOption[]
  refLines?: readonly TrendRefLine[]
  onDatumClick?: (datum: { seriesKey: string; index: number; category: string }) => void
  /** 每个账期的口径脚注（全量长度；preset 随窗口切）。 */
  tooltipFooters?: readonly (string | null)[] | null
  /** 每个账期的 tooltip 明细块（全量长度；preset 随窗口切，窄屏只留标题与观察句）。 */
  tooltipDetails?: readonly (TooltipDetail | null)[] | null
  /**
   * 追加到数据表的列：位置在系列列之后、口径列之前（全量账期下标对齐，调用方给全量长度；
   * 渲染时随表格按窗口切片）。
   * 不影响图例/折线/tooltip。
   */
  extraTableColumns?: readonly TrendTableColumn[]
  onRetry?: () => void
}

const DEFAULT_WINDOW_OPTIONS: readonly TrendWindowOption[] = [
  { value: 12, label: '12期' },
  { value: 24, label: '24期' },
  { value: 'all', label: '全部' },
]

function WindowChips({
  value,
  options,
  onChange,
}: {
  value: TrendWindowValue
  options: readonly TrendWindowOption[]
  onChange: (value: TrendWindowValue) => void
}) {
  return (
    <div
      role="group"
      aria-label="时间窗口"
      className="flex items-center gap-0.5 rounded-lg bg-slate-100 p-0.5"
    >
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={`inline-flex min-h-[44px] items-center rounded-md px-2 text-[11px] font-medium leading-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1 motion-reduce:transition-none ${
            value === option.value
              ? 'bg-white text-brand-700 shadow-sm'
              : 'text-slate-500 hover:text-slate-700'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function TrendChart({
  categories,
  series,
  window,
  windowOptions = DEFAULT_WINDOW_OPTIONS,
  refLines,
  onDatumClick,
  tooltipFooters,
  tooltipDetails,
  extraTableColumns,
  onRetry,
  state,
  empty,
  ...frame
}: TrendChartProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)
  const [instance, setInstance] = useState<ECharts | null>(null)
  const env = useChartEnvironment(bodyRef)
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  // 数据源换系列（维度/选中项变化）时，把已不存在的 key 剪掉；页面以 key 重挂载可整体复位。
  const seriesSignature = series.map((item) => item.key).join('|')
  useEffect(() => {
    const keys = new Set(seriesSignature ? seriesSignature.split('|') : [])
    setHidden((previous) => {
      let changed = false
      const next = new Set<string>()
      for (const key of previous) {
        if (keys.has(key)) next.add(key)
        else changed = true
      }
      return changed ? next : previous
    })
  }, [seriesSignature])

  const visibleSeries = useMemo(
    () => series.filter((item) => !hidden.has(item.key)),
    [series, hidden],
  )
  /** 可见系列 key 串：effect 只依赖它，避免每次渲染重绑 zrender 监听。 */
  const visibleKeys = visibleSeries.map((item) => item.key).join('|')
  const visibleSeriesRef = useRef(visibleSeries)
  visibleSeriesRef.current = visibleSeries

  const option = useMemo(
    () =>
      buildTrendOption(
        {
          categories,
          series: visibleSeries,
          window: window?.value,
          refLines,
          clickable: Boolean(onDatumClick),
          tooltipFooters,
          tooltipDetails,
        },
        { dense: env.dense, coarse: env.coarse, reducedMotion: env.reducedMotion },
      ),
    [
      categories,
      visibleSeries,
      window?.value,
      refLines,
      onDatumClick,
      tooltipFooters,
      tooltipDetails,
      env.dense,
      env.coarse,
      env.reducedMotion,
    ],
  )

  const legendItems = useMemo<LegendItem[]>(
    () =>
      series.map((item, index) => ({
        key: item.key,
        label: item.label,
        color: trendSeriesColor(item, index),
        active: !hidden.has(item.key),
        valueText: formatValue(item.values[item.values.length - 1], item.unit),
        onToggle: () =>
          setHidden((previous) => {
            const next = new Set(previous)
            if (next.has(item.key)) next.delete(item.key)
            else next.add(item.key)
            return next
          }),
        onHover: (hovered) => {
          chartRef.current?.dispatchAction({
            type: hovered ? 'highlight' : 'downplay',
            seriesName: item.label,
          })
        },
      })),
    [series, hidden],
  )

  const titleText = typeof frame.title === 'string' ? frame.title : '趋势'
  const start = trendWindowStart(categories.length, window?.value)
  /** 窗口内可见账期数：系列 dataIndex 与 grid 点击的列序号都落在 [0, visibleCount)。 */
  const visibleCount = Math.max(0, categories.length - start)
  const carriedVisible = visibleSeries.some((item) =>
    item.provenance?.slice(start).some((state) => state === 'carried'),
  )
  const missingVisible = visibleSeries.some((item) =>
    item.provenance?.slice(start).some((state) => state === 'missing'),
  )

  const table = useMemo(() => {
    const extras = extraTableColumns ?? []
    // D5：caption 随窗口——窗口覆盖全部账期时写「全部账期」，否则写「近 N 期」。
    const coversAll = window?.value === 'all' || visibleCount >= categories.length
    const caption = coversAll
      ? `${titleText}（全部账期）`
      : `${titleText}（近 ${visibleCount} 期）`
    // D1：行只取窗口内账期；追加列按全量下标注入，这里同样从 start 起切。
    const rows = categories.slice(start).map((category, offset) => {
      const index = offset + start
      const carried = series.some((item) => item.provenance?.[index] === 'carried')
      const missing = series.some((item) => item.provenance?.[index] === 'missing')
      return [
        category,
        ...series.map((item) => formatValue(item.values[index], item.unit)),
        ...extras.map((item) => item.values[index] ?? '—'),
        carried ? '含沿用值' : missing ? '缺测' : '实测',
      ]
    })
    return {
      caption,
      columns: ['账期', ...series.map((item) => item.label), ...extras.map((item) => item.label), '口径'],
      rows,
    }
  }, [categories, series, titleText, extraTableColumns, start, visibleCount, window?.value])

  const summary = `${titleText}，${categories.length} 个账期，${series.length} 个系列${
    window ? `，窗口${window.value === 'all' ? '全部' : `近 ${window.value} 期`}` : ''
  }`
  const footnote = [
    frame.footnote,
    carriedVisible ? '空心点/斜纹＝含沿用值' : null,
    missingVisible ? '断点＝缺测' : null,
  ].filter(Boolean).join(' · ') || undefined

  const resolvedState = state ?? (categories.length > 0 ? 'ready' : 'empty')
  const resolvedEmpty = empty ?? {
    title: '暂无趋势数据',
    hint: '当前账期没有可用数据；可切换账期或维度',
  }

  /**
   * 命中面是**整个绘图区**（design §5.5「画布 pointer」的落实）：tooltip 跟随指针且
   * `enterable:false`，浮层点不到；系列元素本身只有 2–5px，业主实际上点不中
   * （.scratch/chart-click-affordance/spec.md D1/D2）。
   *
   * 单通道：只在 zrender 层绑一次 click，不再同时绑系列 click，否则同一击会触发两次
   * （展开后立刻收起）。`containPixel({ gridIndex: 0 })` 把标题/图例/轴标签/卡片留白挡在外面。
   */
  useEffect(() => {
    if (!instance || !onDatumClick) return
    const zr = instance.getZr()
    const seriesList = visibleSeriesRef.current

    const pointOf = (event: unknown): [number, number] => {
      const { offsetX, offsetY } = event as { offsetX: number; offsetY: number }
      return [offsetX, offsetY]
    }

    // 指针落在 grid 内才算命中；列按最近账期取（boundaryGap 下两端各有半档余量，故夹紧不丢弃）。
    // 注意 `{xAxisIndex}` finder 只吃单个数（Grid 走 axis.coordToData，传数组会得 NaN）。
    const localIndexOf = (point: [number, number]): number | null => {
      if (visibleCount <= 0) return null
      if (!instance.containPixel({ gridIndex: 0 }, point)) return null
      const raw = instance.convertFromPixel({ xAxisIndex: 0 }, point[0]) as unknown as number
      if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
      return Math.min(visibleCount - 1, Math.max(0, Math.round(raw)))
    }

    // 系列归属：命中了哪个可见系列就报哪个，否则回退首个可见系列（不谎报）。
    const seriesKeyOf = (point: [number, number]): string | null => {
      for (let index = 0; index < seriesList.length; index += 1) {
        if (instance.containPixel({ seriesIndex: index }, point)) return seriesList[index].key
      }
      return seriesList[0]?.key ?? null
    }

    const handleZrClick = (event: unknown) => {
      const point = pointOf(event)
      const local = localIndexOf(point)
      if (local === null) return
      const seriesKey = seriesKeyOf(point)
      if (!seriesKey) return
      const index = start + local
      onDatumClick({ seriesKey, index, category: categories[index] ?? '' })
    }

    let cursor = ''
    const moveCursor = (event: unknown | null) => {
      const next = event && instance.containPixel({ gridIndex: 0 }, pointOf(event)) ? 'pointer' : 'default'
      if (next === cursor) return
      cursor = next
      zr.setCursorStyle(next)
    }
    const handleZrMove = (event: unknown) => moveCursor(event)
    const handleZrOut = () => moveCursor(null)

    zr.on('click', handleZrClick)
    zr.on('mousemove', handleZrMove)
    zr.on('globalout', handleZrOut)
    return () => {
      zr.off('click', handleZrClick)
      zr.off('mousemove', handleZrMove)
      zr.off('globalout', handleZrOut)
      zr.setCursorStyle('default')
    }
  }, [instance, onDatumClick, start, visibleCount, visibleKeys, categories])

  const chips = window ? (
    <WindowChips value={window.value} options={windowOptions} onChange={window.onChange} />
  ) : null

  return (
    <ChartFrame
      {...frame}
      lamp={visibleSeries.length === 1 ? frame.lamp : null}
      direction={visibleSeries.length === 1 ? frame.direction : null}
      actions={
        <>
          {chips}
          {frame.actions}
        </>
      }
      legend={categories.length > 0 ? legendItems : undefined}
      a11yTable={categories.length > 0 ? table : undefined}
      a11ySummary={summary}
      footnote={footnote}
      state={resolvedState}
      empty={resolvedEmpty}
      onRetry={onRetry}
      bodyRef={bodyRef}
    >
      <Chart
        option={option}
        height="100%"
        lazy={false}
        onReady={(chart) => {
          chartRef.current = chart
          setInstance(chart)
        }}
      />
    </ChartFrame>
  )
}