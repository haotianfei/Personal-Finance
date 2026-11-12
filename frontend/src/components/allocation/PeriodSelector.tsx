'use client'

import { useState, useEffect, useRef } from 'react'
import { Calendar } from 'lucide-react'
import PeriodPicker from '@/app/alerts/components/PeriodPicker'
import type { PeriodType } from '@/types'

const STORAGE_KEY = 'allocation_period_config'

export interface PeriodConfig {
  periodType: PeriodType
  selectedPeriod: string
}

interface PeriodSelectorProps {
  value: PeriodConfig
  onChange: (config: PeriodConfig) => void
  className?: string
}

export function PeriodSelector({ value, onChange, className = '' }: PeriodSelectorProps) {
  const [isTransitioning, setIsTransitioning] = useState(false)
  const onChangeRef = useRef(onChange)

  useEffect(() => {
    onChangeRef.current = onChange
  }, [onChange])

  // Load saved config from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      try {
        const parsed: PeriodConfig = JSON.parse(saved)
        onChangeRef.current(parsed)
      } catch {
        // Ignore parse errors
      }
    }
  }, [])

  // Save to localStorage when config changes
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  }, [value])

  const handlePeriodTypeChange = (newType: PeriodType) => {
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
    <div className={`rounded-card border border-line bg-subtle p-4 space-y-4 transition-opacity duration-300 ${isTransitioning ? 'opacity-50' : 'opacity-100'} ${className}`}>
      {/* Period Selection */}
      <div className="flex items-start gap-4">
        <Calendar className="mt-1 text-muted" size={18} aria-hidden />
        <div className="flex-1">
          <span className="mb-2 block text-meta font-medium text-secondary">选择账期</span>
          <PeriodPicker
            value={value.selectedPeriod}
            onChange={handlePeriodChange}
            periodType={value.periodType}
            onPeriodTypeChange={handlePeriodTypeChange}
            hint="选择用于配置分析的账期"
            showHint={false}
            showQuickSelect={false}
          />
        </div>
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
