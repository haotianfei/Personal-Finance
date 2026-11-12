'use client'

import { useState, useCallback } from 'react'
import { Decimal } from 'decimal.js'
import { formatDecimal } from '@/lib/format'

interface CalculatorProps {
  initialValue?: number
  onConfirm: (result: string) => void
  onCancel: () => void
}

// 配置 Decimal.js
Decimal.set({ precision: 20, rounding: Decimal.ROUND_HALF_UP })

export function Calculator({ initialValue = 0, onConfirm, onCancel }: CalculatorProps) {
  const [display, setDisplay] = useState(initialValue ? String(initialValue) : '0')
  const [previousValue, setPreviousValue] = useState<Decimal | null>(null)
  const [operation, setOperation] = useState<string | null>(null)
  const [waitingForOperand, setWaitingForOperand] = useState(false)

  const inputDigit = useCallback((digit: string) => {
    if (waitingForOperand) {
      setDisplay(digit)
      setWaitingForOperand(false)
    } else {
      setDisplay(display === '0' ? digit : display + digit)
    }
  }, [display, waitingForOperand])

  const inputDot = useCallback(() => {
    if (waitingForOperand) {
      setDisplay('0.')
      setWaitingForOperand(false)
    } else if (display.indexOf('.') === -1) {
      setDisplay(display + '.')
    }
  }, [display, waitingForOperand])

  const clear = useCallback(() => {
    setDisplay('0')
    setPreviousValue(null)
    setOperation(null)
    setWaitingForOperand(false)
  }, [])

  const performOperation = useCallback((nextOperation: string) => {
    const inputValue = new Decimal(display)

    if (previousValue === null) {
      setPreviousValue(inputValue)
    } else if (operation) {
      const newValue = calculate(previousValue, inputValue, operation)

      setPreviousValue(newValue)
      setDisplay(formatDecimal(newValue.toString()))
    }

    setWaitingForOperand(true)
    setOperation(nextOperation)
  }, [display, operation, previousValue])

  const calculate = (firstValue: Decimal, secondValue: Decimal, operation: string): Decimal => {
    switch (operation) {
      case '+':
        return firstValue.plus(secondValue)
      case '-':
        return firstValue.minus(secondValue)
      case '×':
        return firstValue.times(secondValue)
      case '÷':
        return firstValue.div(secondValue)
      default:
        return secondValue
    }
  }

  const performCalculation = useCallback(() => {
    const inputValue = new Decimal(display)

    if (previousValue !== null && operation) {
      const newValue = calculate(previousValue, inputValue, operation)
      setDisplay(formatDecimal(newValue.toString()))
      setPreviousValue(null)
      setOperation(null)
      setWaitingForOperand(true)
    }
  }, [display, operation, previousValue])

  const handleConfirm = useCallback(() => {
    // 如果有未完成的运算，先计算
    if (previousValue !== null && operation && !waitingForOperand) {
      performCalculation()
    }
    // 返回保留2位小数的结果
    const result = new Decimal(display)
    onConfirm(formatDecimal(result.toString()))
  }, [display, onConfirm, operation, performCalculation, previousValue, waitingForOperand])

  const buttons = [
    ['7', '8', '9', '÷'],
    ['4', '5', '6', '×'],
    ['1', '2', '3', '-'],
    ['0', '.', '=', '+'],
  ]

  const getButtonClass = (btn: string) => {
    const baseClass = 'h-12 text-lg font-medium rounded-lg transition-colors '
    if (['÷', '×', '-', '+'].includes(btn)) {
      return baseClass + 'bg-brand-100 text-brand-700 hover:bg-brand-200'
    } else if (btn === '=') {
      return baseClass + 'bg-brand-600 text-white hover:bg-brand-700'
    } else {
      return baseClass + 'bg-slate-100 text-slate-700 hover:bg-slate-200'
    }
  }

  return (
    <div className="w-64">
      {/* 显示区域 */}
      <div className="mb-4 p-3 bg-slate-900 rounded-lg">
        <div className="text-right text-2xl font-mono text-white truncate">
          {display}
        </div>
        {operation && previousValue && (
          <div className="text-right text-sm text-muted mt-1">
            {formatDecimal(previousValue.toString())} {operation}
          </div>
        )}
      </div>

      {/* 按钮区域 */}
      <div className="space-y-2">
        {buttons.map((row, rowIndex) => (
          <div key={rowIndex} className="grid grid-cols-4 gap-2">
            {row.map((btn) => (
              <button
                key={btn}
                onClick={() => {
                  if (btn >= '0' && btn <= '9') {
                    inputDigit(btn)
                  } else if (btn === '.') {
                    inputDot()
                  } else if (btn === '=') {
                    performCalculation()
                  } else {
                    performOperation(btn)
                  }
                }}
                className={getButtonClass(btn)}
              >
                {btn}
              </button>
            ))}
          </div>
        ))}
      </div>

      {/* 底部按钮 */}
      <div className="flex gap-2 mt-4">
        <button
          onClick={clear}
          className="flex-1 h-10 text-sm font-medium text-slate-600 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
        >
          清除
        </button>
        <button
          onClick={onCancel}
          className="flex-1 h-10 text-sm font-medium text-slate-600 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
        >
          取消
        </button>
        <button
          onClick={handleConfirm}
          className="flex-1 h-10 text-sm font-medium text-white bg-brand-600 rounded-lg hover:bg-brand-700 transition-colors"
        >
          确认
        </button>
      </div>
    </div>
  )
}
