import type { ExpenseCycle } from '@/types'

export const CYCLE_LABELS: Record<ExpenseCycle, string> = {
  monthly: '每月',
  quarterly: '每季',
  yearly: '每年',
}

export const CYCLE_OPTIONS = [
  { value: 'monthly', label: '每月' },
  { value: 'quarterly', label: '每季' },
  { value: 'yearly', label: '每年' },
]

export const EXPENSE_TYPE_OPTIONS = [
  { value: 'fixed', label: '固定支出' },
  { value: 'large_oneoff', label: '临时大额' },
]