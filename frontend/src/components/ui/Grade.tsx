'use client'

import { cn } from '@/lib/utils'
import { GRADE_STATES, gradeLabel } from '@/lib/metric-language'
import type { MetricGradeState } from '@/types'

/**
 * 灯（GradeDot）与四态图例（GradeLegend）—— 从 `MetricCard` 抽出，逐字等价（design §2.1）。
 *
 * 复用方：`MetricCard`、`ChartFrame`（帧头灯位）。规则不变：
 * - 8px 点；⚪ 用空心圈（与 meta 徽标同一套「空心＝不可判」语言）；
 * - 颜色不单独承载语义，点旁/图例必须带文字。
 */

// 灯色；⚪ 用空心圈（与图例、meta 徽标同一套「空心＝不可判」语言）
const DOT_STYLES: Record<MetricGradeState, string> = {
  green: 'bg-emerald-500 ring-2 ring-emerald-100',
  yellow: 'bg-amber-400 ring-2 ring-amber-100',
  red: 'bg-rose-500 ring-2 ring-rose-100',
  gray: 'border-2 border-slate-300 bg-white',
}

export function GradeDot({ state, className }: { state: MetricGradeState; className?: string }) {
  return (
    <span
      className={cn('inline-block h-2 w-2 shrink-0 rounded-full', DOT_STYLES[state], className)}
      aria-hidden
    />
  )
}

/** 四态图例：灯色不单独承载语义，条头常显一行读法。 */
export function GradeLegend({ className }: { className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 text-meta text-muted', className)}>
      {GRADE_STATES.map((state) => (
        <span key={state} className="inline-flex items-center gap-1">
          <GradeDot state={state} />
          {gradeLabel(state)}
        </span>
      ))}
    </div>
  )
}