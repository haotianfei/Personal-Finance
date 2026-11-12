'use client'

import { useId } from 'react'
import { cn } from '@/lib/utils'
import { fieldControlClass, fieldErrorClass } from './FormField'

export interface AmountFieldProps {
  label: React.ReactNode
  value: string
  onChange: (value: string) => void
  placeholder?: string
  error?: string
  hint?: React.ReactNode
  required?: boolean
  disabled?: boolean
  className?: string
}

/** 金额输入：¥ 前缀、右对齐、tabular、44px；值是字符串，格式化由调用方/服务端负责。 */
export function AmountField({
  label,
  value,
  onChange,
  placeholder = '0.00',
  error,
  hint,
  required,
  disabled,
  className,
}: AmountFieldProps) {
  const id = useId()
  const errorId = `${id}-error`
  const hintId = `${id}-hint`

  return (
    <div className={cn('space-y-1.5', className)}>
      <label htmlFor={id} className="block text-meta font-medium text-secondary">
        {label}
        {required && <span className="ml-0.5 text-status-critical-text" aria-hidden="true">*</span>}
      </label>
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-meta text-muted" aria-hidden>
          ¥
        </span>
        <input
          id={id}
          type="text"
          inputMode="decimal"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          aria-invalid={error ? true : undefined}
          aria-describedby={[error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined}
          aria-required={required || undefined}
          className={cn(fieldControlClass, 'pf-num pl-7 text-right', error && fieldErrorClass)}
        />
      </div>
      {error && <p id={errorId} role="alert" className="text-meta text-status-critical-text">{error}</p>}
      {!error && hint && <p id={hintId} className="text-meta text-muted">{hint}</p>}
    </div>
  )
}