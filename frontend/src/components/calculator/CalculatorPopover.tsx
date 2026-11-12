'use client'

import { useState, useRef, useEffect } from 'react'
import { Calculator } from './Calculator'
import { Calculator as CalculatorIcon } from 'lucide-react'

interface CalculatorPopoverProps {
  value: string | number
  onChange: (value: string) => void
  disabled?: boolean
}

export function CalculatorPopover({ value, onChange, disabled }: CalculatorPopoverProps) {
  const [isOpen, setIsOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const [popoverStyle, setPopoverStyle] = useState<React.CSSProperties>({})

  // 计算弹窗位置
  useEffect(() => {
    if (isOpen && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect()
      setPopoverStyle({
        position: 'fixed',
        top: rect.bottom + 8,
        right: window.innerWidth - rect.right,
        zIndex: 9999,
      })
    }
  }, [isOpen])

  // 点击外部关闭弹窗
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node
      // 检查点击的是否是计算器弹窗内部元素
      const popoverEl = document.querySelector('[data-calculator-popover]')
      if (
        popoverEl &&
        !popoverEl.contains(target) &&
        buttonRef.current &&
        !buttonRef.current.contains(target)
      ) {
        setIsOpen(false)
      }
    }

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [isOpen])

  const handleConfirm = (result: string) => {
    onChange(result)
    setIsOpen(false)
  }

  const handleCancel = () => {
    setIsOpen(false)
  }

  // 解析当前值作为初始值
  const initialValue = parseFloat(String(value)) || 0

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        onClick={() => !disabled && setIsOpen(!isOpen)}
        disabled={disabled}
        className={`p-1.5 rounded-md transition-colors ${
          disabled
            ? 'text-slate-300 cursor-not-allowed'
            : 'text-muted hover:text-brand-600 hover:bg-brand-50'
        }`}
        title="打开计算器"
        type="button"
      >
        <CalculatorIcon size={18} />
      </button>

      {isOpen && (
        <div
          data-calculator-popover
          className="fixed bg-white rounded-xl shadow-xl border border-slate-200 p-4 animate-fade-in"
          style={popoverStyle}
        >
          <Calculator
            initialValue={initialValue}
            onConfirm={handleConfirm}
            onCancel={handleCancel}
          />
        </div>
      )}
    </div>
  )
}
