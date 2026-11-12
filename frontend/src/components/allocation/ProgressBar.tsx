'use client'

import { AlertCircle, CheckCircle2 } from 'lucide-react'
import { Card } from '@/components/ui'
import { formatPercent } from '@/lib/format'

interface ProgressBarProps {
  totalAllocated: number
  className?: string
}

export function ProgressBar({ totalAllocated, className = '' }: ProgressBarProps) {
  const remainingPercent = Math.max(100 - totalAllocated, 0)
  const isOverAllocated = totalAllocated > 100
  const isFullyAllocated = totalAllocated === 100

  return (
    <Card padding="md" className={className}>
      <div className="mb-3 flex flex-col gap-2 tablet:flex-row tablet:items-center tablet:justify-between">
        <h3 className="whitespace-nowrap font-semibold text-ink">目标配置进度</h3>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span className="flex items-center gap-1.5 whitespace-nowrap">
            <span className="text-secondary">已配置:</span>
            <span className={`font-medium ${isOverAllocated ? 'text-status-critical-text' : 'text-brand-700'}`}>
              {formatPercent(totalAllocated)}
            </span>
          </span>
          <span className="flex items-center gap-1.5 whitespace-nowrap">
            <span className="text-secondary">剩余:</span>
            <span className={`font-medium ${remainingPercent === 0 ? 'text-status-good-text' : 'text-ink'}`}>
              {formatPercent(remainingPercent)}
            </span>
          </span>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="h-4 bg-slate-100 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ease-out ${
            isOverAllocated
              ? 'bg-red-500'
              : isFullyAllocated
              ? 'bg-green-500'
              : 'bg-gradient-to-r from-brand-500 to-brand-400'
          }`}
          style={{ width: `${Math.min(totalAllocated, 100)}%` }}
        />
      </div>

      {/* Status Message */}
      <div className="mt-3">
        {isOverAllocated ? (
          <div className="flex items-center gap-2 text-sm text-red-600 animate-fade-in">
            <AlertCircle size={16} />
            <span>配置比例超出100%，请调整</span>
          </div>
        ) : isFullyAllocated ? (
          <div className="flex items-center gap-2 text-sm text-green-600 animate-fade-in">
            <CheckCircle2 size={16} />
            <span>已配满100%</span>
          </div>
        ) : (
          <div className="text-sm text-slate-500">
            还可配置 <span className="pf-num font-medium text-ink">{formatPercent(remainingPercent)}</span>
          </div>
        )}
      </div>
    </Card>
  )
}
