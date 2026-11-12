'use client'

import { useMemo, useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import type { ECharts } from 'echarts/core'
import Chart from '../Chart'
import { ChartFrame, type ChartFrameBaseProps } from '../ChartFrame'
import { useChartEnvironment } from '../environment'
import { formatPercent, formatValue } from '../format'
import {
  buildCompositionOption,
  compositionColorMap,
  COMPOSITION_MODE_LABELS,
} from '../presets/composition'
import type { CompositionItem, CompositionMode, LegendItem, Unit } from '../types'

/**
 * 结构图 primitive（design §3.4）—— 首个真实消费者。
 *
 * 只做组合：数据 + 图帧 props → `buildCompositionOption` + `ChartFrame` + `Chart`。
 * - 不取数、不计算占比：`share` 由调用方给；缺省不显示占比标签；
 * - 窄容器回退（rose/treemap/sunburst → donut/pie）由这里按 `ctx.dense` 决定；
 * - 图例是静态 key（不可切换）：结构图必须对全体求和，隐藏扇区会改变分母；
 * - 数据表是 canvas 的等价物；键盘走表 + 可聚焦图例 key（hover/focus 高亮扇区）。
 */

export interface CompositionChartProps
  extends Omit<ChartFrameBaseProps, 'legend' | 'a11yTable' | 'bodyRef'> {
  items: readonly CompositionItem[]
  mode: CompositionMode
  /** 窄容器回退目标；默认 donut。`none` 表示不回退。 */
  narrowFallback?: 'donut' | 'pie' | 'none'
  /** 调用方给的合计；只用于 donut 中心与 tooltip 脚注，不参与占比计算。 */
  total?: number | null
  /** donut 中心标签（如「总资产」）；total 为空时不显示中心。 */
  totalLabel?: ReactNode
  unit?: Unit
  /** error 态重试（通常接 react-query refetch）。 */
  onRetry?: () => void
}

const FALLBACK_MODES: ReadonlySet<CompositionMode> = new Set(['rose', 'treemap', 'sunburst'])

export function CompositionChart({
  items,
  mode,
  narrowFallback = 'donut',
  total,
  totalLabel,
  unit,
  onRetry,
  state,
  empty,
  ...frame
}: CompositionChartProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)
  const env = useChartEnvironment(bodyRef)

  const shouldFallback =
    env.dense && narrowFallback !== 'none' && FALLBACK_MODES.has(mode)
  const effectiveMode: CompositionMode = shouldFallback
    ? narrowFallback === 'pie' ? 'pie' : 'donut'
    : mode

  const option = useMemo(
    () =>
      buildCompositionOption(
        { items, mode: effectiveMode, total, unit },
        { dense: env.dense, coarse: env.coarse, reducedMotion: env.reducedMotion },
      ),
    [items, effectiveMode, total, unit, env.dense, env.coarse, env.reducedMotion],
  )

  const colorMap = useMemo(
    () => compositionColorMap(items, effectiveMode),
    [items, effectiveMode],
  )

  const legendItems = useMemo<LegendItem[]>(
    () =>
      items.map((item) => ({
        key: item.key,
        label: item.label,
        color: colorMap[item.key],
        valueText:
          item.share === null || item.share === undefined
            ? formatValue(item.value, unit)
            : formatPercent(item.share),
        onHover: (hovered) => {
          chartRef.current?.dispatchAction({
            type: hovered ? 'highlight' : 'downplay',
            seriesIndex: 0,
            name: item.label,
          })
        },
      })),
    [items, colorMap, unit],
  )

  const titleText = typeof frame.title === 'string' ? frame.title : '结构图'
  const table = useMemo(
    () => ({
      caption: `${titleText}数据表`,
      columns: ['名称', '金额', '占比', '备注'],
      rows: items.map((item) => [
        item.label,
        formatValue(item.value, unit),
        item.share === null || item.share === undefined ? '—' : formatPercent(item.share),
        item.note ?? '—',
      ]),
    }),
    [items, titleText, unit],
  )

  const totalText = total === null || total === undefined ? null : formatValue(total, unit)
  const summary = `${titleText}，共 ${items.length} 项${totalText ? `，合计 ${totalText}` : ''}`
  const resolvedState = state ?? (items.length > 0 ? 'ready' : 'empty')
  const resolvedEmpty = empty ?? {
    title: '暂无数据',
    hint: '当前账期没有可展示的项；可切换账期',
  }

  const subtitle = shouldFallback && resolvedState === 'ready' ? (
    <>
      {frame.subtitle}
      {frame.subtitle ? ' · ' : ''}
      窄屏以{COMPOSITION_MODE_LABELS[effectiveMode]}呈现
    </>
  ) : (
    frame.subtitle
  )

  return (
    <ChartFrame
      {...frame}
      subtitle={subtitle}
      state={resolvedState}
      empty={resolvedEmpty}
      onRetry={onRetry}
      legend={legendItems}
      a11yTable={items.length > 0 ? table : undefined}
      a11ySummary={summary}
      bodyRef={bodyRef}
    >
      <Chart
        option={option}
        height="100%"
        lazy={false}
        onReady={(instance) => {
          chartRef.current = instance
        }}
      />
      {effectiveMode === 'donut' && totalText && (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-0.5 text-center">
          {totalLabel && (
            <span className="text-[11px] leading-4 text-slate-500">{totalLabel}</span>
          )}
          <span
            className={cn(
              'font-semibold tracking-tight text-brand-950 tabular-nums',
              env.dense ? 'text-[13px]' : 'text-lg',
            )}
          >
            {totalText}
          </span>
        </div>
      )}
    </ChartFrame>
  )
}