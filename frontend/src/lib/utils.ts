import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'
import tokens from '@/styles/tokens.json'

// tailwind-merge 默认只认识内置字阶；把 tokens.type 的语义字号注册进去，
// 否则 `text-money-l` 会被当成文字颜色，与 `text-ink` 合并时被误删。
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ text: Object.keys(tokens.type) }],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export const PERIOD_OPTIONS: { label: string; value: string }[] = [
  { label: '按日', value: 'day' },
  { label: '按周', value: 'week' },
  { label: '按月', value: 'month' },
  { label: '按季度', value: 'quarter' },
  { label: '按年', value: 'year' },
]

export const LIQUIDITY_OPTIONS = [
  'T+0', 'T+1', 'T+7', 'T+30', 'T+90', 'T+360',
]
