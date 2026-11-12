'use client'

import { ArrowUpRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { MetaBadge } from '@/components/ui/MetaBadge'
import { GradeDot } from '@/components/ui/Grade'
import { degradationLabel, directionLabel, gradeLabel, NO_CRITERION_LABEL } from '@/lib/metric-language'
import { formatValue } from '@/lib/format'
import { Money } from '@/components/ui/Money'
import type { MetricDetails, MetricGrade, MetricGradeState, MetricStatusItem } from '@/types'

/**
 * 指标卡（三期-0，spec §4）：值 ＋ 灯 ＋ 口径注记 ＋ 下钻。
 *
 * 显示纪律：
 * - 灯只有四态（绿 / 黄 / 红 / ⚪），颜色永远和「口径」一起出现——注记不藏进图表；
 * - `grade=null`＝本切片不判灯（不是第五态）：不点灯，改显示方向文字（越低越好等）；
 * - ⚪（gray）额外缀「不可判」二字：空心点 + 文字双通道，不靠颜色单独承载语义；
 * - 闸门（gates）折叠为徽标，点按展开完整注记（触屏同样可达）；阻断性与非阻断性用
 *   空心 / 琥珀两种语言区分；
 * - 下钻：整卡可点（透明按钮铺满卡面），箭头常显给出可点线索；注记徽标浮在其上，
 *   点注记不会误触跳转。
 *
 * 布局：桌面铺在总览条的细分格网里（非独立浮卡）——细线分格，数值逐行对齐便于扫读；
 * 移动 2 列紧凑格，沿首页统计卡（p-3 量级）的既有移动规范。
 */

/** 灯与图例已抽到 `Grade.tsx`；此处 re-export 保持既有 import 路径可用。 */
export { GradeDot, GradeLegend } from '@/components/ui/Grade'

/** 沿用事实 → 一行副文；指标不带这组键就不显示。 */
function coverageLine(details?: MetricDetails | null): string | null {
  if (!details) return null
  const carried = details.carried_max_days
  if (typeof carried === 'number' && carried > 0) return `最久沿用 ${carried} 天`
  return null
}

function criterionDetail(grade: MetricGrade | null): string | undefined {
  if (!grade) return undefined
  const parts: string[] = []
  if (grade.band?.source) parts.push(`来源 ${grade.band.source}`)
  if (grade.note) parts.push(grade.note)
  return parts.length ? parts.join(' · ') : undefined
}

/** 配置偏离逐目标行摘要：`3 个目标 · 最差 +6.20pp（股票型）`。 */
function deviationRowsLine(details: MetricDetails | null | undefined, unit: string): string | null {
  const rows = details?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null
  const worst = rows.reduce((a, b) =>
    Math.abs(Number(b.deviation) || 0) > Math.abs(Number(a.deviation) || 0) ? b : a)
  const name = typeof worst.name === 'string' && worst.name ? `（${worst.name}）` : ''
  return `${rows.length} 个目标 · 最差 ${formatValue(worst.deviation, unit)}${name}`
}

export interface MetricCardProps {
  item: MetricStatusItem
  /** 显示名（来自 /api/metrics 字典；缺字典时由调用方兜底）。 */
  name: string
  /** 主指标（净资产）轻微着色，其余格保持同一节奏。 */
  emphasis?: boolean
  /** 下钻动作；仅在 item.drill 非空且本页有目的地时传入。 */
  onDrill?: () => void
  className?: string
}

export function MetricCard({ item, name, emphasis, onDrill, className }: MetricCardProps) {
  const grade = item.grade ?? null
  const gates = item.gates ?? []
  const blocked = gates.some((g) => g.blocking)
  // 阻断性闸门 → 恒为 ⚪（不出彩灯）；grade=null 且无阻断才走「无判据 → 方向文字」
  const state: MetricGradeState | undefined = grade?.state ?? (blocked ? 'gray' : undefined)
  const stateText = grade?.note && state === 'gray' ? grade.note : undefined
  const valueText = formatValue(item.value, item.unit)
  const coverage = coverageLine(item.details)
  const deviation = deviationRowsLine(item.details, item.unit)
  const threshold = grade?.band?.threshold ?? null
  const thresholdText = threshold == null
    ? null
    : item.unit === 'ratio' || item.unit === 'percent'
      ? formatValue(threshold, item.unit)
      : threshold
  const direction = state ? null : directionLabel(item.direction)
  const drillable = typeof onDrill === 'function' && item.drill != null
  const drillLabel = item.drill?.label?.trim() || null

  const ariaLabel = [
    name,
    valueText,
    state ? gradeLabel(state) : undefined,
    direction ?? undefined,
    drillable ? (drillLabel ? `打开${drillLabel}` : '查看对应图表') : undefined,
  ].filter(Boolean).join('，')

  return (
    <article
      className={cn(
        'group relative flex min-h-[96px] flex-col gap-1 border-b border-l border-line px-3 py-3 sm:px-4 sm:py-3.5',
        emphasis && 'bg-brand-50/40',
        className,
      )}
    >
      {/* 整卡下钻：透明按钮铺满，内容层在它之上、注记徽标再单独接管点按 */}
      {drillable && (
        <button
          type="button"
          onClick={onDrill}
          aria-label={ariaLabel}
          className="absolute inset-0 z-0 rounded-[inherit] transition-colors hover:bg-slate-900/[0.03] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-400"
        />
      )}

      <div className="pointer-events-none relative z-10 flex items-center gap-2">
        <span className="line-clamp-2 flex-1 text-xs font-medium text-slate-600 sm:line-clamp-1">
          {name}
        </span>
        {state && (
          <span className="flex shrink-0 items-center gap-1">
            <GradeDot state={state} />
            <span className="sr-only">{gradeLabel(state)}</span>
          </span>
        )}
      </div>

      <div className="pointer-events-none relative z-10 flex items-end justify-between gap-2">
        <Money
          value={item.value}
          unit={item.unit}
          size="s"
          className="min-w-0 sm:text-money-m"
        />
        {drillable && (
          <ArrowUpRight
            size={14}
            aria-hidden
            className="mb-0.5 shrink-0 text-slate-300 transition-colors group-hover:text-brand-500 group-focus-within:text-brand-500"
          />
        )}
      </div>

      {(gates.length > 0 || threshold || coverage || deviation || !state) && (
        <div className="pointer-events-none relative z-10 flex flex-wrap items-center gap-x-1.5 gap-y-2.5">
          {/* 无判据＝显示方向文字；⚪ 额外缀「不可判」（不靠颜色单独承载语义） */}
          {!state && (
            <span className="text-meta leading-[18px] text-muted">{direction ?? NO_CRITERION_LABEL}</span>
          )}
          {state === 'gray' && (
            <span className="text-meta leading-[18px] text-muted" title={stateText}>
              {gradeLabel('gray')}
            </span>
          )}
          {gates.map((gate, i) => (
            <MetaBadge
              key={`${gate.code}-${i}`}
              tone={gate.blocking ? 'blocked' : 'warn'}
              label={degradationLabel(gate.code)}
              detail={gate.note}
              className="pointer-events-auto"
            />
          ))}
          {threshold && (
            <MetaBadge
              tone="info"
              label={`阈值 ${thresholdText}`}
              detail={criterionDetail(grade)}
              className="pointer-events-auto"
            />
          )}
          {coverage && <span className="text-meta leading-[18px] text-muted">{coverage}</span>}
          {deviation && <span className="text-meta leading-[18px] text-muted">{deviation}</span>}
        </div>
      )}
    </article>
  )
}

/** 加载占位：与卡片同格的细线骨架，避免高度跳动。 */
export function MetricCardSkeleton({ count = 6 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="border-b border-l border-line px-3 py-3 sm:px-4 sm:py-3.5"
          aria-hidden
        >
          <div className="h-3 w-16 animate-pulse rounded bg-slate-100" />
          <div className="mt-3 h-4 w-24 animate-pulse rounded bg-slate-100" />
        </div>
      ))}
    </>
  )
}