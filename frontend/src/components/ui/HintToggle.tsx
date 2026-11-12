'use client'

import { useState } from 'react'
import { HelpCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 把原先只挂在 title= 上的说明变成「无需 hover 也能读到」。
 *
 * 背景：title= 是纯 hover 提示，触屏上永远不出现；而其中一部分承载的是破坏性/不可逆
 * 操作的唯一说明（清零、不录入、忽略规则、重置编辑……），手机上等于让人盲操作。
 *
 * 形态：一个 44px 命中区的 ? 按钮，点开后在控件旁内联展开一句话说明（再次点按收起）。
 * 桌面端同样可点（比纯 hover 更易发现），调用方原有的 title 悬停提示保留，行为只增不减。
 *
 * 用法约束：作为兄弟节点放在目标控件旁边，**不要嵌套进 button/label 内部**
 * （嵌套会导致点 ? 同时触发外层控件）。
 */
export function HintToggle({
  hint,
  tone = 'slate',
  className,
}: {
  hint: string
  tone?: 'slate' | 'amber' | 'red'
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const tones = {
    slate: 'text-muted',
    amber: 'text-status-warn-text',
    red: 'text-status-critical-text',
  } as const

  return (
    <span className={cn('inline-flex items-center gap-1 align-middle', className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label="查看说明"
        className={cn(
          'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full transition-colors',
          tones[tone],
          open && 'bg-subtle'
        )}
      >
        <HelpCircle size={13} />
      </button>
      {open && (
        <span className="inline-block max-w-[15rem] whitespace-normal text-left text-meta font-normal leading-relaxed text-secondary">
          {hint}
        </span>
      )}
    </span>
  )
}
