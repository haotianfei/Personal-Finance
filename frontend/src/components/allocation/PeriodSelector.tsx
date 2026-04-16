'use client'

import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Calendar, ChevronDown } from 'lucide-react'

const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' },
  { value: 'year', label: '年' },
]

const STORAGE_KEY = 'allocation_period_config'

export interface PeriodConfig {
  periodType: string
  selectedPeriod: string
}

interface PeriodSelectorProps {
  value: PeriodConfig
  onChange: (config: PeriodConfig) => void
  className?: string
}

export function PeriodSelector({ value, onChange, className = '' }: PeriodSelectorProps) {
  const [isTransitioning, setIsTransitioning] = useState(false)

  // Load saved config from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      try {
        const parsed: PeriodConfig = JSON.parse(saved)
        onChange(parsed)
      } catch {
        // Ignore parse errors
      }
    }
  }, [])

  // Save to localStorage when config changes
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  }, [value])

  const { data: periods, isLoading } = useQuery({
    queryKey: ['proportionPeriods', value.periodType],
    queryFn: () => api.getProportionAvailablePeriods(value.periodType),
  })

  const handlePeriodTypeChange = (newType: string) => {
    setIsTransitioning(true)
    setTimeout(() => {
      onChange({
        periodType: newType,
        selectedPeriod: '',
      })
      setIsTransitioning(false)
    }, 150)
  }

  const handlePeriodChange = (newPeriod: string) => {
    setIsTransitioning(true)
    setTimeout(() => {
      onChange({
        ...value,
        selectedPeriod: newPeriod,
      })
      setIsTransitioning(false)
    }, 150)
  }

  return (
    <div className={`bg-slate-50 rounded-xl p-4 space-y-4 transition-opacity duration-300 ${isTransitioning ? 'opacity-50' : 'opacity-100'} ${className}`}>
      {/* Period Type Selection */}
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-slate-700">账期类型:</span>
        <div className="flex gap-1 bg-white rounded-lg p-1 border border-slate-200">
          {PERIOD_TYPE_OPTIONS.map((t) => (
            <button
              key={t.value}
              onClick={() => handlePeriodTypeChange(t.value)}
              className={`px-3 py-1.5 text-sm rounded-md transition-all duration-200 ${
                value.periodType === t.value
                  ? 'bg-brand-100 text-brand-700 font-medium'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Period Date Selection */}
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-slate-700">选择账期:</span>
        <div className="relative">
          <Calendar className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
          <select
            value={value.selectedPeriod}
            onChange={(e) => handlePeriodChange(e.target.value)}
            disabled={isLoading}
            className="pl-10 pr-10 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px] appearance-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <option value="">最新账期</option>
            {periods?.map((p) => (
              <option key={p.date} value={p.date}>
                {p.label} ({p.count}条)
              </option>
            ))}
          </select>
          <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" size={16} />
        </div>
        {isLoading && (
          <span className="text-sm text-slate-500">加载中...</span>
        )}
      </div>
    </div>
  )
}

// Hook for using period selector with localStorage persistence
export function usePeriodSelector() {
  const [config, setConfig] = useState<PeriodConfig>({
    periodType: 'month',
    selectedPeriod: '',
  })

  return {
    config,
    setConfig,
  }
}
