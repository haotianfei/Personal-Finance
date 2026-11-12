'use client'

import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 组合控件的字段外壳（按钮 / 多选这类不能挂 `<label for>` 的控件）。
 *
 * 不用 `FormField`：那套会把 `id / aria-invalid / aria-describedby` clone 到子元素上，
 * 而这里的子元素是容器 div，这些属性落上去只会产生无效 ARIA。标签与说明仍由
 * `aria-labelledby` 之外的 `aria-label` 承担（触发器按钮自己带名字）。
 */
export function FieldFrame({
  label,
  required = false,
  error,
  hint,
  children,
  className,
}: {
  label: ReactNode
  required?: boolean
  error?: string
  hint?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <span className="block text-meta font-medium text-secondary">
        {label}
        {required && <span className="ml-0.5 text-status-critical-text" aria-hidden="true">*</span>}
      </span>
      {children}
      {error ? (
        <p role="alert" className="text-meta text-status-critical-text">{error}</p>
      ) : hint ? (
        <p className="text-meta text-muted">{hint}</p>
      ) : null}
    </div>
  )
}

/** 触发器（按钮 / 输入框外壳）的统一观感：与 SelectField 的 44px 基线一致。 */
export const triggerClass =
  'min-h-[44px] w-full rounded-control border border-line-strong bg-surface px-3 text-body text-ink disabled:cursor-not-allowed disabled:bg-subtle disabled:text-disabled'

export const triggerErrorClass = 'border-status-critical'