'use client'

import { useMemo, useRef } from 'react'
import Chart from '../Chart'
import { ChartFrame, type ChartFrameBaseProps } from '../ChartFrame'
import { useChartEnvironment } from '../environment'
import { chartTokens } from '../theme'
import { formatPercent, formatValue } from '../format'
import { buildComparisonOption, sortComparisonRows } from '../presets/comparison'
import type {
  ComparisonMode,
  ComparisonRow,
  ComparisonSort,
  Unit,
} from '../types'

/**
 * 对比图 primitive（design §3.5）—— 分配页配置偏离的首个消费者。
 *
 * v1 只实现 delta 横向棒棒糖；dumbbell 仅保留类型契约，不画假图。
 * 不取数、不计算偏差：rows 的 delta/target/actual 全由调用方给。
 */
export interface ComparisonChartProps
  extends Omit<ChartFrameBaseProps, 'legend' | 'a11yTable' | 'bodyRef'> {
  rows: readonly ComparisonRow[]
  mode?: ComparisonMode
  unit?: Unit
  /** ±阈值；只画 amber 参考带，不是灯。 */
  threshold?: number | null
  sort?: ComparisonSort
  /** error 态重试（通常接 react-query refetch）。 */
  onRetry?: () => void
}

export function ComparisonChart({
  rows,
  mode = 'delta',
  unit = 'pp',
  threshold,
  sort = 'given',
  onRetry,
  state,
  empty,
  height: requestedHeight,
  ...frame
}: ComparisonChartProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const env = useChartEnvironment(bodyRef)
  const tokens = chartTokens({ dense: env.dense, reducedMotion: env.reducedMotion })
  const sortedRows = useMemo(() => sortComparisonRows(rows, sort), [rows, sort])

  const baseHeight =
    typeof requestedHeight === 'number'
      ? requestedHeight
      : tokens.heights[requestedHeight ?? 'lg']
  const rowHeight = env.dense ? 28 : 32
  const chartHeight = Math.max(baseHeight, sortedRows.length * rowHeight + 36)

  const option = useMemo(
    () => buildComparisonOption({ rows: sortedRows, unit, threshold }, env),
    [sortedRows, unit, threshold, env],
  )

  const titleText = typeof frame.title === 'string' ? frame.title : '配置偏离'
  const thresholdText =
    threshold === null || threshold === undefined
      ? null
      : formatValue(Math.abs(threshold), unit).replace(/^\+/, '')
  const table = useMemo(
    () => ({
      caption: `${titleText}（名称/目标/实际/偏离）`,
      columns: ['名称', '目标', '实际', '偏离'],
      rows: sortedRows.map((row) => [
        row.label,
        formatPercent(row.target),
        formatPercent(row.actual),
        formatValue(row.delta, unit),
      ]),
    }),
    [sortedRows, titleText, unit],
  )
  const summary = `${titleText}，${sortedRows.length} 项${
    thresholdText ? `，阈值 ±${thresholdText}` : ''
  }`

  const resolvedState =
    mode === 'dumbbell'
      ? 'empty'
      : state ?? (sortedRows.length > 0 ? 'ready' : 'empty')
  const resolvedEmpty =
    mode === 'dumbbell'
      ? { title: '哑铃图尚未开放', hint: 'v1 先交付 delta 模式' }
      : empty ?? {
          title: '暂无配置目标',
          hint: '为项目设置目标比例后查看偏离',
        }

  return (
    <ChartFrame
      {...frame}
      height={chartHeight}
      state={resolvedState}
      empty={resolvedEmpty}
      onRetry={onRetry}
      a11yTable={mode !== 'dumbbell' && sortedRows.length > 0 ? table : undefined}
      a11ySummary={summary}
      bodyRef={bodyRef}
    >
      <Chart option={option} height="100%" lazy={false} />
    </ChartFrame>
  )
}
