'use client'

import { useEffect, useRef } from 'react'
import { X, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Money } from '@/components/ui/Money'

/**
 * 「N 个资产项分类未补齐」提示条。
 *
 * 文案的三条事实（全部可在代码里验证，别再把其中任何一条说反）：
 * ① **已计入净值**——`analysis_service._sum_as_of` 的口径只排除 archived 项，
 *    draft 项照常参与求和（`snapshot_identity.active_item_predicate`）；
 * ② 给出规模——金额取 `/analysis/summary` 的 `unclassified_*`（同一 as-of 口径，
 *    按资产/负债两侧给毛额），接口未回时不硬编数字；
 * ③ 后果——按「资产类别」维度做占比统计时这些项归不进去
 *    （`proportion_service.get_proportion_by_asset_class`：无类别的行不进任何桶），
 *    导出时也会被跳过（`export_service`：`status != 'draft'`）。
 */
export function DraftClassificationBanner({
  count,
  assetAmount,
  liabilityAmount,
  onOpenBatch,
  onDismiss,
  loading = false,
  highlight = false,
  className,
}: {
  count: number
  /** 未补齐项的资产侧合计（`summary.unclassified_asset_amount`）；缺省则不说金额。 */
  assetAmount?: string | number | null
  /** 未补齐项的负债侧合计（`summary.unclassified_liability_amount`）。 */
  liabilityAmount?: string | number | null
  onOpenBatch: () => void
  onDismiss?: () => void
  /** 打开批量弹窗前要先取各项目前分类，等待时给按钮一个进行中态。 */
  loading?: boolean
  /** 从首页「含 N 项待分类」深链（`?highlight=draft`）进来：高亮并滚进视野。 */
  highlight?: boolean
  className?: string
}) {
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!highlight) return
    // 深链进来时提示条可能不在首屏：滚到视野中央（定位不到就什么都不做）
    rootRef.current?.scrollIntoView({ block: 'center' })
  }, [highlight])

  if (count <= 0) return null

  const amountsKnown = assetAmount != null || liabilityAmount != null

  return (
    <div
      ref={rootRef}
      role="status"
      className={cn(
        'flex flex-wrap items-start gap-3 rounded-card border border-status-warn/30 bg-status-warn-surface px-4 py-3',
        highlight && 'ring-2 ring-status-warn/40 ring-offset-2 ring-offset-app',
        className,
      )}
    >
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-white/60 text-status-warn-text">
        <Layers size={16} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-body-strong text-ink">
          <span className="pf-num font-semibold">{count}</span> 个资产项分类未补齐
        </p>
        <p className="mt-0.5 text-meta text-secondary">
          这些项<b className="font-semibold text-ink">已经计入净值</b>
          {amountsKnown && (
            <>
              （截至最新快照：资产{' '}
              <Money value={assetAmount ?? '0'} size="xs" />
              <span aria-hidden> / </span>
              负债{' '}
              <Money value={liabilityAmount ?? '0'} size="xs" />
              ）
            </>
          )}
          。按「资产类别」做的占比里没有它们的份额，导出时也会被跳过。补齐后自动转为「已生效」。
        </p>
      </div>
      <div className="flex w-full shrink-0 items-center gap-2 tablet:w-auto">
        <button
          type="button"
          onClick={onOpenBatch}
          disabled={loading}
          className="inline-flex min-h-[40px] flex-1 items-center justify-center rounded-control bg-action-primary px-3 text-meta font-medium text-white disabled:opacity-60 tablet:flex-none"
        >
          {loading ? '加载中…' : '批量补齐'}
        </button>
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            aria-label="本次不再提示"
            title="本次不再提示"
            className="inline-flex h-10 w-10 items-center justify-center rounded-control text-muted transition-colors hover:text-ink"
          >
            <X size={16} aria-hidden />
          </button>
        )}
      </div>
    </div>
  )
}