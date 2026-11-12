'use client'

import { CircleAlert, CircleCheck, CircleDashed } from 'lucide-react'
import { cn } from '@/lib/utils'
import { dimsText } from './classification'
import type { RequiredDim } from './classification'

/**
 * 资产项分类状态条：把 draft / active / archived 翻译成用户能懂的一句话。
 *
 * 后端只有一个 `status` 字段，但用户关心的是「这项进不进统计」，
 * 所以这里一律补一句后果说明，而不是只放一个状态徽标。
 */
export function StatusStrip({
  status,
  missing,
  className,
}: {
  status: 'active' | 'draft' | 'archived'
  missing: readonly RequiredDim[]
  className?: string
}) {
  if (status === 'archived') {
    return (
      <div className={cn('flex items-start gap-2 rounded-control border border-line bg-subtle px-3 py-2 text-meta text-secondary', className)}>
        <CircleDashed size={15} className="mt-0.5 shrink-0 text-muted" aria-hidden />
        <p>这项已归档：不计入净值；取消归档后才能继续补分类。</p>
      </div>
    )
  }

  if (status === 'active' || missing.length === 0) {
    return (
      <div className={cn('flex items-start gap-2 rounded-control border border-status-good/20 bg-status-good-surface px-3 py-2 text-meta text-status-good-text', className)}>
        <CircleCheck size={15} className="mt-0.5 shrink-0" aria-hidden />
        <p>分类已齐（已生效）：金额计入净值与按类别 / 机构 / 归属的统计。</p>
      </div>
    )
  }

  return (
    <div className={cn('flex items-start gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text', className)}>
      <CircleAlert size={15} className="mt-0.5 shrink-0" aria-hidden />
      <p>
        <span className="font-medium">待补分类</span>
        {missing.length > 0 && <>：还差{dimsText(missing)}</>}
        。补齐后这项才转为「已生效」。金额<b className="font-semibold">已计入净值</b>，
        只是按类别的统计与导出要等补齐后才包含它。
      </p>
    </div>
  )
}