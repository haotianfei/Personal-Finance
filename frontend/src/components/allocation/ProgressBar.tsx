'use client'

import { AlertCircle, CheckCircle2 } from 'lucide-react'

interface ProgressBarProps {
  totalAllocated: number
  className?: string
}

export function ProgressBar({ totalAllocated, className = '' }: ProgressBarProps) {
  const remainingPercent = Math.max(100 - totalAllocated, 0)
  const isOverAllocated = totalAllocated > 100
  const isFullyAllocated = totalAllocated === 100

  return (
    <div className={`bg-white rounded-xl shadow-card p-6 ${className}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-brand-950">目标配置进度</h3>
        <div className="text-sm flex items-center gap-4">
          <span className="flex items-center gap-1.5">
            <span className="text-slate-500">已配置:</span>
            <span className={`font-medium ${isOverAllocated ? 'text-red-600' : 'text-brand-600'}`}>
              {totalAllocated.toFixed(2)}%
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="text-slate-500">剩余:</span>
            <span className={`font-medium ${remainingPercent === 0 ? 'text-green-600' : 'text-slate-700'}`}>
              {remainingPercent.toFixed(2)}%
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
            还可配置 <span className="font-medium text-slate-700">{remainingPercent.toFixed(2)}%</span>
          </div>
        )}
      </div>
    </div>
  )
}
