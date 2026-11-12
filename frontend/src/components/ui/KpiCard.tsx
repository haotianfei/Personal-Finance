import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card } from '@/components/ui/Card'
import { DirectionText } from '@/components/ui/DirectionText'
import { Money } from '@/components/ui/Money'

/**
 * KPI 卡：标签 → 金额 →（可选）变化量 → 辅助说明（design §5.2/§6.1）。
 *
 * - 金额是主角，默认不放彩色图标块；金额本体中性墨色；
 * - 只有变化量使用方向色（DirectionText）；
 * - 无数据用 `—`，加载态与最终高度一致，避免跳动；
 * - 320 下主 KPI 由调用方给 `col-span-2`，金额不截断。
 */
export interface KpiCardProps {
  label: ReactNode
  value: string | number | null | undefined
  unit?: string | null
  delta?: string | number | null
  /** 与 delta 同源的变化率；以同一方向色显示，避免把百分比塞进灰色标签。 */
  deltaPercent?: string | number | null
  deltaUnit?: string
  deltaLabel?: string
  meta?: ReactNode
  /** 空值/不可得口径（如「率不可得」）；默认 `—`。 */
  emptyLabel?: string
  loading?: boolean
  emphasis?: boolean
  className?: string
  /** 右上角次级动作（Link/Button），需自带 44px 命中。 */
  action?: ReactNode
}

export function KpiCard({
  label,
  value,
  unit = 'cny',
  delta,
  deltaPercent,
  deltaUnit = 'cny',
  deltaLabel,
  meta,
  emptyLabel,
  loading = false,
  emphasis = false,
  className,
  action,
}: KpiCardProps) {
  return (
    <Card
      as="article"
      padding="md"
      className={cn(
        'flex min-h-[96px] flex-col gap-2',
        emphasis && 'border-line-strong',
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="min-w-0 text-meta font-medium text-muted">{label}</span>
        {action && <div className="shrink-0">{action}</div>}
      </div>

      {loading ? (
        <div className="space-y-2" aria-hidden>
          <div className="h-7 w-32 max-w-full animate-pulse rounded-control bg-subtle" />
          <div className="h-3 w-20 animate-pulse rounded-control bg-subtle" />
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          <Money
            value={value}
            unit={unit}
            size={emphasis ? 'l' : 'm'}
            emptyLabel={emptyLabel}
            className={emphasis ? undefined : 'max-[359px]:text-money-xs max-[359px]:font-medium'}
          />
          {(delta !== undefined || deltaPercent !== undefined || meta) && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              {(delta !== undefined || deltaPercent !== undefined) && (
                <span className="inline-flex flex-nowrap items-baseline gap-x-2">
                  {delta !== undefined && (
                    <DirectionText value={delta} unit={deltaUnit} size="xs" />
                  )}
                  {deltaPercent !== undefined && deltaPercent !== null && (
                    <DirectionText value={deltaPercent} unit="percent" size="xs" />
                  )}
                  {deltaLabel && <span className="text-meta text-muted">{deltaLabel}</span>}
                </span>
              )}
              {meta && <span className="text-meta text-muted">{meta}</span>}
            </div>
          )}
        </div>
      )}
    </Card>
  )
}