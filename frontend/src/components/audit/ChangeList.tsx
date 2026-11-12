'use client'

import { useState } from 'react'
import { ArrowRight } from 'lucide-react'
import type { AuditChange } from '@/lib/api'
import { EMPTY_VALUE, formatCurrency } from '@/lib/format'
import { cn } from '@/lib/utils'

/**
 * 一条记录里的逐字段变化：只渲染后端给出的变化字段（分型快照已保证是变更集），
 * 形态是「标签 旧值 → 新值」。
 *
 * - 金额字段走 `formatCurrency`（千分位 + 两位），右对齐等宽，和记录页金额口径一致；
 * - 其它字段原样展示，超长截两行、可展开——名称/备注这类长串不能把抽屉撑破；
 * - null 落「—」：清空字段是真实变化，不能显示成空字符串。
 */

/** 超过这个长度的文本先截断（按字符数，中文一字一眼）。 */
const COLLAPSE_AT = 48

function isAmountField(field: string): boolean {
  return field === 'amount' || field.endsWith('_amount')
}

function DiffValue({
  value,
  numeric,
  emphasis = false,
}: {
  value: string | null
  numeric: boolean
  /** 新值加粗（旧值保持次要色）。 */
  emphasis?: boolean
}) {
  const [expanded, setExpanded] = useState(false)

  if (value === null || value === '') {
    return <span className="pf-num shrink-0 text-meta text-muted">{EMPTY_VALUE}</span>
  }

  if (numeric) {
    return (
      <span
        className={cn(
          'pf-num shrink-0 whitespace-nowrap text-money-xs',
          emphasis ? 'font-medium text-ink' : 'text-muted',
        )}
      >
        {formatCurrency(value)}
      </span>
    )
  }

  const long = value.length > COLLAPSE_AT
  return (
    <span className="min-w-0 text-meta">
      <span
        className={cn(
          'break-words',
          emphasis ? 'font-medium text-ink' : 'text-muted',
          long && !expanded && 'line-clamp-2',
        )}
      >
        {value}
      </span>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((previous) => !previous)}
          aria-expanded={expanded}
          className="ml-1 inline-flex min-h-[32px] items-center rounded-control px-0.5 text-micro font-medium text-link underline-offset-2 hover:underline"
        >
          {expanded ? '收起' : '展开'}
        </button>
      )}
    </span>
  )
}

export interface ChangeListProps {
  changes: AuditChange[]
  className?: string
}

export function ChangeList({ changes, className }: ChangeListProps) {
  if (changes.length === 0) return null

  return (
    <ul className={cn('mt-2 space-y-1.5', className)}>
      {changes.map((change, index) => {
        const numeric = isAmountField(change.field)
        return (
          <li
            key={`${change.field}-${index}`}
            className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-baseline gap-x-2"
          >
            <span className="truncate text-meta text-muted" title={change.label}>
              {change.label}
            </span>
            <span className="flex min-w-0 flex-wrap items-baseline justify-end gap-x-1.5 gap-y-0.5 text-right">
              <DiffValue value={change.from} numeric={numeric} />
              <ArrowRight size={12} aria-hidden className="shrink-0 self-center text-muted" />
              <span className="sr-only">改为</span>
              <DiffValue value={change.to} numeric={numeric} emphasis />
            </span>
          </li>
        )
      })}
    </ul>
  )
}