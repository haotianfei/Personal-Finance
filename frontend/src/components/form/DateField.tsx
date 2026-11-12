'use client'

import { DatePicker } from 'antd'
import dayjs from 'dayjs'
import type { Dayjs } from 'dayjs'
import { FormField } from './FormField'

export interface DateFieldProps {
  label: React.ReactNode
  value: string
  onChange: (value: string) => void
  error?: string
  hint?: React.ReactNode
  required?: boolean
  disabled?: boolean
  placeholder?: string
  className?: string
}

/** 日期字段：优先 antd DatePicker（桌面面板/移动 picker），值为 YYYY-MM-DD。 */
export function DateField({
  label,
  value,
  onChange,
  error,
  hint,
  required,
  disabled,
  placeholder = '选择日期',
  className,
}: DateFieldProps) {
  const current = value && dayjs(value, 'YYYY-MM-DD').isValid() ? dayjs(value, 'YYYY-MM-DD') : null

  return (
    <FormField label={label} required={required} error={error} hint={hint} className={className}>
      <DatePicker
        value={current}
        onChange={(date: Dayjs | null) => onChange(date ? date.format('YYYY-MM-DD') : '')}
        format="YYYY-MM-DD"
        placeholder={placeholder}
        disabled={disabled}
        size="large"
        status={error ? 'error' : undefined}
        className="w-full"
        allowClear
      />
    </FormField>
  )
}