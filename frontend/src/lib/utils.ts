import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatAmount(value: string | number, showSign = false): string {
  const num = typeof value === 'string' ? parseFloat(value) : value
  if (isNaN(num)) return '¥0.00'
  const prefix = showSign && num > 0 ? '+' : ''
  return `${prefix}¥${num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function formatPercent(value: number | null | undefined): string {
  if (value == null) return '--'
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

export function formatDate(dateStr: string): string {
  return dateStr
}

export const PERIOD_OPTIONS: { label: string; value: string }[] = [
  { label: '按日', value: 'day' },
  { label: '按月', value: 'month' },
  { label: '按季度', value: 'quarter' },
  { label: '按年', value: 'year' },
]

export const LIQUIDITY_OPTIONS = [
  'T+0', 'T+1', 'T+7', 'T+30', 'T+90', 'T+360',
]

export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

export function formatDateTime(dateStr: string): string {
  const date = new Date(dateStr)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
