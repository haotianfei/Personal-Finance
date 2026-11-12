'use client'

import { cloneElement, isValidElement, useId } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 字段外壳：label 与控件 id 关联、必填星号、帮助/错误文本。
 * 单子元素会被自动补 id / aria-invalid / aria-describedby。
 */
export interface FormFieldProps {
  label: ReactNode
  children: ReactElement<Record<string, unknown>>
  required?: boolean
  error?: ReactNode
  hint?: ReactNode
  className?: string
  htmlFor?: string
}

export function FormField({
  label,
  children,
  required = false,
  error,
  hint,
  className,
  htmlFor,
}: FormFieldProps) {
  const autoId = useId()
  const id = htmlFor ?? autoId
  const errorId = `${id}-error`
  const hintId = `${id}-hint`
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined

  const control = isValidElement(children)
    ? cloneElement(children as ReactElement<Record<string, unknown>>, {
        id,
        'aria-invalid': error ? true : undefined,
        'aria-describedby': describedBy,
        'aria-required': required || undefined,
      })
    : children

  return (
    <div className={cn('space-y-1.5', className)}>
      <label htmlFor={id} className="block text-meta font-medium text-secondary">
        {label}
        {required && <span className="ml-0.5 text-status-critical-text" aria-hidden="true">*</span>}
      </label>
      {control}
      {error && (
        <p id={errorId} role="alert" className="text-meta text-status-critical-text">
          {error}
        </p>
      )}
      {!error && hint && (
        <p id={hintId} className="text-meta text-muted">
          {hint}
        </p>
      )}
    </div>
  )
}

export const fieldControlClass =
  'min-h-[44px] w-full rounded-control border border-line-strong bg-surface px-3 text-body text-ink placeholder:text-muted disabled:cursor-not-allowed disabled:bg-subtle disabled:text-disabled'

export const fieldErrorClass = 'border-status-critical'