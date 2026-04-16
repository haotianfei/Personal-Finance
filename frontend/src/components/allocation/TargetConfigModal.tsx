'use client'

import { useState, useEffect, useCallback } from 'react'
import { X, AlertCircle, Percent, Calculator } from 'lucide-react'

interface TargetConfigModalProps {
  isOpen: boolean
  onClose: () => void
  onSave: (value: string) => void
  initialValue?: string
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
  itemName,
  parentTarget,
  siblingsTotal,
  isSaving = false,
}: TargetConfigModalProps) {
  const [value, setValue] = useState(initialValue)
  const [error, setError] = useState<string | null>(null)

  // Reset value when modal opens
  useEffect(() => {
    if (isOpen) {
      setValue(initialValue ? parseFloat(initialValue).toFixed(2) : '')
      setError(null)
    }
  }, [isOpen, initialValue])

  // Auto-focus input when modal opens
  useEffect(() => {
    if (isOpen) {
      const timer = setTimeout(() => {
        const input = document.getElementById('target-input') as HTMLInputElement | null
        input?.focus()
        input?.select()
      }, 100)
      return () => clearTimeout(timer)
    }
  }, [isOpen])

  const validateValue = useCallback((inputValue: string): string | null => {
    if (inputValue === '' || inputValue === '.') {
      return null
    }

    const numValue = parseFloat(inputValue)

    if (isNaN(numValue)) {
      return '请输入有效的数字'
    }

    if (numValue < 0) {
      return '比例不能小于0%'
    }

    if (numValue > 100) {
      return '比例不能超过100%'
    }

    if (parentTarget !== null && parentTarget !== undefined) {
      if (numValue > parentTarget) {
        return `不能超过父级目标比例 (${parentTarget.toFixed(2)}%)`
      }
    }

    const currentValue = initialValue ? parseFloat(initialValue) : 0
    const otherSiblings = siblingsTotal - currentValue
    if (otherSiblings + numValue > 100) {
      return `该层级总和将超过100% (当前已配置${otherSiblings.toFixed(2)}%)`
    }

    return null
  }, [parentTarget, siblingsTotal, initialValue])

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const inputValue = e.target.value

    // Allow only numbers and one decimal point, max 2 decimal places
    if (inputValue === '' || /^\d*\.?\d{0,2}$/.test(inputValue)) {
      setValue(inputValue)
      setError(validateValue(inputValue))
    }
  }

  const handleSave = () => {
    const validationError = validateValue(value)
    if (validationError) {
      setError(validationError)
      return
    }

    if (value === '' || value === '.') {
      setError('请输入比例值')
      return
    }

    onSave(value)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSave()
    } else if (e.key === 'Escape') {
      onClose()
    }
  }

  const calculateRemaining = () => {
    const numValue = parseFloat(value || '0')
    const currentValue = initialValue ? parseFloat(initialValue) : 0
    const otherSiblings = siblingsTotal - currentValue
    return Math.max(100 - otherSiblings - numValue, 0)
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm animate-fade-in">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 animate-slide-up">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200">
          <h3 className="text-lg font-semibold text-brand-950">配置目标比例</h3>
          <button
            onClick={onClose}
            className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-6 space-y-6">
          {/* Item Name */}
          <div className="bg-slate-50 rounded-lg p-3">
            <span className="text-sm text-slate-500">配置项目:</span>
            <p className="font-medium text-slate-900 mt-1">{itemName}</p>
          </div>

          {/* Parent Target Info */}
          {parentTarget !== null && parentTarget !== undefined && (
            <div className="flex items-center gap-2 text-sm text-slate-600">
              <span className="text-slate-500">父级目标比例:</span>
              <span className="font-medium text-brand-600">{parentTarget.toFixed(2)}%</span>
            </div>
          )}

          {/* Input Field */}
          <div className="space-y-2">
            <label className="text-sm font-medium text-slate-700">目标比例</label>
            <div className="relative">
              <input
                id="target-input"
                type="text"
                inputMode="decimal"
                value={value}
                onChange={handleInputChange}
                onKeyDown={handleKeyDown}
                placeholder="0.00"
                className={`w-full px-4 py-3 pr-12 text-lg border rounded-lg focus:outline-none focus:ring-2 transition-all ${
                  error
                    ? 'border-red-300 focus:ring-red-200 bg-red-50'
                    : 'border-slate-200 focus:ring-brand-200 focus:border-brand-400'
                }`}
              />
              <Percent className="absolute right-4 top-1/2 -translate-y-1/2 text-slate-400" size={20} />
            </div>

            {/* Error Message */}
            {error && (
              <div className="flex items-center gap-2 text-sm text-red-600 animate-fade-in">
                <AlertCircle size={16} />
                <span>{error}</span>
              </div>
            )}
          </div>

          {/* Real-time Calculation */}
          <div className="bg-brand-50 rounded-lg p-4 space-y-2">
            <div className="flex items-center gap-2 text-brand-700">
              <Calculator size={16} />
              <span className="text-sm font-medium">实时计算</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-slate-600">当前输入:</span>
              <span className="font-medium text-slate-900">{value || '0'}%</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-slate-600">该层级剩余可配置:</span>
              <span className="font-medium text-brand-600">{calculateRemaining().toFixed(2)}%</span>
            </div>
            {parentTarget !== null && parentTarget !== undefined && (
              <div className="flex justify-between text-sm">
                <span className="text-slate-600">占父级比例:</span>
                <span className="font-medium text-slate-900">
                  {value && parentTarget > 0
                    ? `${((parseFloat(value) / parentTarget) * 100).toFixed(1)}%`
                    : '-'}
                </span>
              </div>
            )}
          </div>

          {/* Shortcuts Hint */}
          <div className="text-xs text-slate-400 flex items-center gap-4">
            <span>快捷键:</span>
            <span className="flex items-center gap-1">
              <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-slate-600">Enter</kbd>
              保存
            </span>
            <span className="flex items-center gap-1">
              <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-slate-600">Esc</kbd>
              取消
            </span>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-slate-200 bg-slate-50 rounded-b-xl">
          <button
            onClick={onClose}
            disabled={isSaving}
            className="px-4 py-2 text-sm font-medium text-slate-600 hover:text-slate-800 hover:bg-slate-200 rounded-lg transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            disabled={isSaving || !!error || !value}
            className="px-4 py-2 text-sm font-medium text-white bg-brand-600 hover:bg-brand-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {isSaving ? (
              <>
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                保存中...
              </>
            ) : (
              '保存'
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
