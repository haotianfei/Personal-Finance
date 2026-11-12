'use client'

import { useState, useCallback, useRef } from 'react'
import { CalculatorPopover } from '@/components/calculator/CalculatorPopover'
import { Decimal } from 'decimal.js'

interface AmountInputProps {
  value: string | number
  onChange: (value: string) => void
  placeholder?: string
  disabled?: boolean
  className?: string
  style?: React.CSSProperties
}

// 配置 Decimal.js：保留2位小数，四舍五入
Decimal.set({ precision: 20, rounding: Decimal.ROUND_HALF_UP })

// 安全计算表达式
const evaluateExpression = (expr: string): string | null => {
  try {
    // 替换中文符号和特殊字符
    const sanitized = expr
      .replace(/×/g, '*')
      .replace(/÷/g, '/')
      .replace(/，/g, ',')
      .replace(/=/g, '')
      .trim()

    // 如果已经是纯数字，直接返回（保留2位小数）
    if (/^\d+\.?\d*$/.test(sanitized)) {
      return new Decimal(sanitized).toFixed(2)
    }

    // 使用 Decimal.js 计算
    // 将表达式中的数字转换为 Decimal
    const result = evaluateWithDecimal(sanitized)
    return result.toFixed(2)
  } catch {
    return null
  }
}

// 使用 Decimal.js 安全计算表达式
const evaluateWithDecimal = (expr: string): Decimal => {
  // 简单的表达式解析：支持 + - * / ( )
  // 使用栈来处理运算符优先级
  
  const tokens = tokenize(expr)
  const output: (Decimal | string)[] = []
  const operators: string[] = []
  
  const precedence: Record<string, number> = {
    '+': 1,
    '-': 1,
    '*': 2,
    '/': 2,
  }
  
  for (const token of tokens) {
    if (token instanceof Decimal) {
      output.push(token)
    } else if (token === '(') {
      operators.push(token)
    } else if (token === ')') {
      while (operators.length > 0 && operators[operators.length - 1] !== '(') {
        output.push(operators.pop()!)
      }
      operators.pop() // 移除 '('
    } else if (['+', '-', '*', '/'].includes(token)) {
      while (
        operators.length > 0 &&
        operators[operators.length - 1] !== '(' &&
        precedence[operators[operators.length - 1]] >= precedence[token]
      ) {
        output.push(operators.pop()!)
      }
      operators.push(token)
    }
  }
  
  while (operators.length > 0) {
    output.push(operators.pop()!)
  }
  
  // 计算后缀表达式
  const stack: Decimal[] = []
  for (const token of output) {
    if (token instanceof Decimal) {
      stack.push(token)
    } else {
      const b = stack.pop()
      const a = stack.pop()
      if (!a || !b) throw new Error('Invalid expression')
      
      switch (token) {
        case '+':
          stack.push(a.plus(b))
          break
        case '-':
          stack.push(a.minus(b))
          break
        case '*':
          stack.push(a.times(b))
          break
        case '/':
          stack.push(a.div(b))
          break
      }
    }
  }
  
  if (stack.length !== 1) throw new Error('Invalid expression')
  return stack[0]
}

// 词法分析：将表达式拆分为数字和运算符
const tokenize = (expr: string): (Decimal | string)[] => {
  const tokens: (Decimal | string)[] = []
  let i = 0
  
  while (i < expr.length) {
    const char = expr[i]
    
    // 跳过空格
    if (char === ' ') {
      i++
      continue
    }
    
    // 数字（包括小数）
    if (/\d/.test(char)) {
      let num = ''
      while (i < expr.length && (/\d/.test(expr[i]) || expr[i] === '.')) {
        num += expr[i]
        i++
      }
      tokens.push(new Decimal(num))
      continue
    }
    
    // 运算符
    if (['+', '-', '*', '/', '(', ')'].includes(char)) {
      tokens.push(char)
      i++
      continue
    }
    
    // 未知字符，跳过
    i++
  }
  
  return tokens
}

export function AmountInput({
  value,
  onChange,
  placeholder = '支持计算: 1000*1.05+200',
  disabled,
  className,
  style
}: AmountInputProps) {
  const [displayValue, setDisplayValue] = useState(String(value || ''))
  const inputRef = useRef<HTMLInputElement>(null)

  // 处理输入变化
  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = e.target.value
    setDisplayValue(newValue)
    // 实时传递值，如果表达式有效则传递计算结果，否则传递原值
    const result = evaluateExpression(newValue)
    if (result !== null) {
      onChange(result)
    } else {
      onChange(newValue)
    }
  }, [onChange])

  // 处理失去焦点
  const handleBlur = useCallback(() => {
    const result = evaluateExpression(displayValue)
    if (result !== null) {
      // 显示计算结果
      setDisplayValue(result)
      onChange(result)
    }
  }, [displayValue, onChange])

  // 处理键盘事件
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      const result = evaluateExpression(displayValue)
      if (result !== null) {
        setDisplayValue(result)
        onChange(result)
      }
      inputRef.current?.blur()
    }
  }, [displayValue, onChange])

  // 处理计算器结果
  const handleCalculatorChange = useCallback((newValue: string) => {
    // 确保计算器结果也保留2位小数
    const formatted = new Decimal(newValue).toFixed(2)
    setDisplayValue(formatted)
    onChange(formatted)
  }, [onChange])

  return (
    <div className={`flex items-center gap-1 min-w-0 ${className || ''}`} style={style}>
      <input
        ref={inputRef}
        type="text"
        value={displayValue}
        onChange={handleChange}
        onBlur={handleBlur}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        className="flex-1 min-w-0 px-2 py-1 text-right border border-slate-200 rounded focus:outline-none focus:ring-2 focus:ring-brand-400 disabled:bg-slate-50 disabled:text-disabled"
      />
      <div className="flex-shrink-0">
        <CalculatorPopover
          value={displayValue}
          onChange={handleCalculatorChange}
          disabled={disabled}
        />
      </div>
    </div>
  )
}
