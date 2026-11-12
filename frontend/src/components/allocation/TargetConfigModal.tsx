'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import { Percent, Calculator, Bell } from 'lucide-react'
import { ResponsiveDialog } from '@/components/ui'
import { FormField, fieldControlClass } from '@/components/form'
import { formatDecimal, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

interface TargetConfigModalProps {
  isOpen: boolean
  onClose: () => void
  onSave: (targetPercent: string, alertThreshold: string) => void
  initialValue?: string
  initialThreshold?: string
  itemName: string
  parentTarget?: number | null
  siblingsTotal: number
  isSaving?: boolean
}

export function TargetConfigModal({
  isOpen,
  onClose,
  onSave,
  initialValue = '',
  initialThreshold = '5.00',
  itemName,
  parentTarget,
  siblingsTotal,
  isSaving = false,
}: TargetConfigModalProps) {
  const [value, setValue] = useState(initialValue)
  const [threshold, setThreshold] = useState(initialThreshold)
  const [error, setError] = useState<string | null>(null)
  const [thresholdError, setThresholdError] = useState<string | null>(null)
  const targetRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (isOpen) {
      setValue(formatDecimal(initialValue ? parseFloat(initialValue) : null))
      setThreshold(formatDecimal(initialThreshold ? parseFloat(initialThreshold) : null) || '5.00')
      setError(null)
      setThresholdError(null)
    }
  }, [isOpen, initialValue, initialThreshold])

  const validateValue = useCallback((inputValue: string): string | null => {
    if (inputValue === '' || inputValue === '.') return null
    const numValue = parseFloat(inputValue)
    if (isNaN(numValue)) return '请输入有效的数字'
    if (numValue < 0) return '比例不能小于 0%'
    if (numValue > 100) return '比例不能超过 100%'
    if (parentTarget !== null && parentTarget !== undefined && numValue > parentTarget) {
      return `不能超过父级目标比例（${formatPercent(parentTarget)}）`
    }
    const currentValue = initialValue ? parseFloat(initialValue) : 0
    const otherSiblings = siblingsTotal - currentValue
    if (otherSiblings + numValue > 100) {
      return `该层级总和将超过 100%（当前已配置 ${formatPercent(otherSiblings)}）`
    }
    return null
  }, [parentTarget, siblingsTotal, initialValue])

  const validateThreshold = useCallback((inputValue: string): string | null => {
    if (inputValue === '' || inputValue === '.') return null
    const numValue = parseFloat(inputValue)
    if (isNaN(numValue)) return '请输入有效的数字'
    if (numValue < 0.1) return '阈值不能小于 0.1%'
    if (numValue > 50) return '阈值不能超过 50%'
    return null
  }, [])

  const handleInputChange = (inputValue: string) => {
    if (inputValue === '' || /^\d*\.?\d{0,2}$/.test(inputValue)) {
      setValue(inputValue)
      setError(validateValue(inputValue))
    }
  }

  const handleThresholdChange = (inputValue: string) => {
    if (inputValue === '' || /^\d*\.?\d{0,2}$/.test(inputValue)) {
      setThreshold(inputValue)
      setThresholdError(validateThreshold(inputValue))
    }
  }

  const handleSave = () => {
    const validationError = validateValue(value)
    const thresholdValidationError = validateThreshold(threshold)
    if (validationError) { setError(validationError); return }
    if (thresholdValidationError) { setThresholdError(thresholdValidationError); return }
    if (value === '' || value === '.') { setError('请输入比例值'); return }
    if (threshold === '' || threshold === '.') { setThresholdError('请输入阈值'); return }
    onSave(value, threshold)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleSave()
  }

  const calculateRemaining = () => {
    const numValue = parseFloat(value || '0')
    const currentValue = initialValue ? parseFloat(initialValue) : 0
    const otherSiblings = siblingsTotal - currentValue
    return Math.max(100 - otherSiblings - numValue, 0)
  }

  return (
    <ResponsiveDialog
      open={isOpen}
      onClose={onClose}
      title="配置目标比例"
      description={itemName ? `正在配置「${itemName}」` : undefined}
      size="sm"
      initialFocusRef={targetRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={isSaving} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button
            type="button"
            onClick={handleSave}
            disabled={isSaving || !!error || !!thresholdError || !value || !threshold}
            className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
          >
            {isSaving ? '保存中…' : '保存'}
          </button>
        </div>
      }
    >
      <div className="space-y-5">
        {parentTarget !== null && parentTarget !== undefined && (
          <p className="rounded-control bg-subtle p-3 text-body text-secondary">
            父级目标比例：<span className="pf-num font-medium text-ink">{formatPercent(parentTarget)}</span>
          </p>
        )}

        <FormField label="目标比例" required error={error ?? undefined}>
          <div className="relative">
            <input
              ref={targetRef}
              type="text"
              inputMode="decimal"
              value={value}
              onChange={(e) => handleInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="0.00"
              className={cn(fieldControlClass, 'pr-10 text-right text-money-m', error && 'border-status-critical')}
            />
            <Percent className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-muted" size={18} aria-hidden />
          </div>
        </FormField>

        <FormField label="偏离预警阈值" required error={thresholdError ?? undefined} hint="实际与目标偏离超过此阈值时给出调整建议（默认 5%）">
          <div className="relative">
            <input
              type="text"
              inputMode="decimal"
              value={threshold}
              onChange={(e) => handleThresholdChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="5.00"
              className={cn(fieldControlClass, 'pr-10 text-right text-money-m', thresholdError && 'border-status-critical')}
            />
            <Percent className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-muted" size={18} aria-hidden />
          </div>
        </FormField>

        <div className="rounded-control bg-brand-50 p-3 text-body">
          <div className="flex items-center gap-2 text-brand-800">
            <Calculator size={16} aria-hidden />
            <span className="font-medium">实时计算</span>
          </div>
          <dl className="mt-2 space-y-1">
            <div className="flex justify-between gap-2"><dt className="text-secondary">当前输入</dt><dd className="pf-num text-ink">{formatPercent(parseFloat(value || '0'))}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-secondary">该层级剩余可配置</dt><dd className="pf-num text-brand-700">{formatPercent(calculateRemaining())}</dd></div>
            {parentTarget !== null && parentTarget !== undefined && (
              <div className="flex justify-between gap-2"><dt className="text-secondary">占父级比例</dt><dd className="pf-num text-ink">{value && parentTarget > 0 ? formatPercent((parseFloat(value) / parentTarget) * 100) : '—'}</dd></div>
            )}
            <div className="flex justify-between gap-2 border-t border-brand-200 pt-1"><dt className="flex items-center gap-1 text-secondary"><Bell size={13} aria-hidden /> 预警阈值</dt><dd className="pf-num text-status-warn-text">±{formatPercent(parseFloat(threshold || '5'))}</dd></div>
          </dl>
        </div>

        <p className="flex items-center gap-3 text-meta text-muted">
          <span>快捷键：</span>
          <span><kbd className="rounded bg-subtle px-1.5 py-0.5 text-secondary">Enter</kbd> 保存</span>
          <span><kbd className="rounded bg-subtle px-1.5 py-0.5 text-secondary">Esc</kbd> 取消</span>
        </p>
      </div>
    </ResponsiveDialog>
  )
}
