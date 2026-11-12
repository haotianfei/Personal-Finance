'use client'

import { cn } from '@/lib/utils'
import { FormField, fieldControlClass, fieldErrorClass } from './FormField'

export interface SelectFieldOption {
  value: string
  label: string
  disabled?: boolean
}

export interface SelectFieldProps {
  label: React.ReactNode
  value: string
  onChange: (value: string) => void
  options: SelectFieldOption[]
  placeholder?: string
  error?: string
  hint?: React.ReactNode
  required?: boolean
  disabled?: boolean
  className?: string
  /** 原生 select 的 ref：被拦下时把焦点送回这里。 */
  selectRef?: React.Ref<HTMLSelectElement>
}

/** 选择字段：原生 select（移动端系统 picker；无需 hover），44px。 */
export function SelectField({
  label,
  value,
  onChange,
  options,
  placeholder = '请选择',
  error,
  hint,
  required,
  disabled,
  className,
  selectRef,
}: SelectFieldProps) {
  return (
    <FormField label={label} required={required} error={error} hint={hint} className={className}>
      <select
        ref={selectRef}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={cn(fieldControlClass, error && fieldErrorClass)}
      >
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
    </FormField>
  )
}