'use client'

import { useState } from 'react'
import { cn } from '@/lib/utils'

/**
 * 通用口径徽标（三期-0，spec §4）。
 *
 * 与 CarriedBadge 暂并存（合并留后续清理）：CarriedBadge 是
 * 「含沿用值」的单用途静态标签，本组件承接指标闸门 / meta 降级 / 判据来源等各类
 * 口径注记，并把原先只挂在 title= 上的说明变成触屏也能展开的正文（同 HintToggle 的取舍，
 * 但保留可见的胶囊外观）。
 *
 * 交互：label 常显；detail 点按展开（再次点按收起），桌面另有 title 悬停提示。
 * 命中区：胶囊视觉高度 24px，用 ::after 上下各外扩 10px → 44px 触控基线，不影响排版。
 */

export type MetaBadgeTone = 'warn' | 'info' | 'muted' | 'blocked'

const TONES: Record<MetaBadgeTone, { pill: string; dot: string }> = {
  // 非阻断注记（含沿用值 / 本金未设 / 大额流近似）
  warn: { pill: 'bg-amber-50 text-amber-700 hover:bg-amber-100', dot: 'bg-amber-400' },
  // 判据 / 参考信息
  info: { pill: 'bg-brand-50 text-brand-700 hover:bg-brand-100', dot: 'bg-brand-400' },
  // 一般口径说明
  muted: { pill: 'bg-slate-100 text-slate-500 hover:bg-slate-200/70', dot: 'bg-slate-400' },
  // 阻断性闸门（与 ⚪ 同一套「空心」语言）
  blocked: { pill: 'bg-slate-100 text-slate-600 hover:bg-slate-200/70', dot: 'border-2 border-slate-400 bg-white' },
}

/**
 * 降级码 → 短标签的唯一实现已收口到 `@/lib/metric-language`；
 * 这里 re-export，保持既有 `@/components/ui/MetaBadge` 消费方不破。
 */
export { degradationLabel } from '@/lib/metric-language'

export function MetaBadge({
  label,
  detail,
  tone = 'warn',
  className,
  title,
}: {
  label: string
  /** 完整说明（展开后正文）；缺省＝纯标签，不可点。 */
  detail?: string | null
  tone?: MetaBadgeTone
  className?: string
  /** 覆盖默认的悬停提示（缺省用 detail）。 */
  title?: string
}) {
  const [open, setOpen] = useState(false)
  const style = TONES[tone]

  const content = (
    <>
      <span className={cn('mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full', style.dot)} aria-hidden />
      <span className="min-w-0">
        <span className="whitespace-nowrap">{label}</span>
        {open && detail && (
          <span className="mt-0.5 block whitespace-normal pb-0.5 font-normal leading-4 opacity-80">
            {detail}
          </span>
        )}
      </span>
    </>
  )

  const base = cn(
    'relative inline-flex max-w-full items-start gap-1 rounded-full px-2 py-0.5 text-left text-meta font-medium leading-4 transition-colors',
    style.pill,
    open && 'rounded-lg',
    className,
  )

  if (!detail) {
    return (
      <span className={cn(base, 'cursor-default')} title={title}>
        {content}
      </span>
    )
  }

  return (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      aria-expanded={open}
      title={title ?? detail}
      className={cn(
        base,
        'after:absolute after:inset-x-0 after:-inset-y-2.5 after:content-[""]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1',
      )}
    >
      {content}
    </button>
  )
}