'use client'

import { useState, useMemo } from 'react'
import { DatePicker, Select, Space, Button, Tooltip } from 'antd'
import type { Dayjs } from 'dayjs'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
// `.week()` 由该插件提供：注册必须跟着用它的组件走。
// 原先靠记录页（frontend/src/app/records/page.tsx）在模块作用域 extend 顺带生效，
// 记录页退役分期入口后不再注册，故在此就地补齐（含类型增强）。
import weekOfYear from 'dayjs/plugin/weekOfYear'
import locale from 'antd/locale/zh_CN'
import { ArrowLeftOutlined, ArrowRightOutlined, InfoCircleOutlined } from '@ant-design/icons'

dayjs.extend(weekOfYear)

const { Option } = Select

type PeriodType = 'day' | 'week' | 'month' | 'quarter' | 'year'

interface PeriodPickerProps {
  value?: string
  onChange?: (value: string) => void
  periodType?: PeriodType
  onPeriodTypeChange?: (type: PeriodType) => void
  /** 使用场景提示；缺省保留预警页原文案，供其它页面覆写。 */
  hint?: string
  /** 配置/分析页可关闭提示条与快捷按钮，保持首屏紧凑。 */
  showHint?: boolean
  showQuickSelect?: boolean
}

const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' },
  { value: 'week', label: '周' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' },
  { value: 'year', label: '年' },
]

export default function PeriodPicker({
  value,
  onChange,
  periodType: controlledPeriodType,
  onPeriodTypeChange,
  hint = '请选择需要预警的账期',
  showHint = true,
  showQuickSelect = true,
}: PeriodPickerProps) {
  const [internalPeriodType, setInternalPeriodType] = useState<PeriodType>('month')
  const periodType = controlledPeriodType || internalPeriodType

  const handlePeriodTypeChange = (type: PeriodType) => {
    if (!controlledPeriodType) {
      setInternalPeriodType(type)
    }
    onPeriodTypeChange?.(type)
    // Clear value when type changes
    onChange?.('')
  }

  const handleDateChange = (date: Dayjs | null) => {
    if (!date) {
      onChange?.('')
      return
    }

    let periodValue = ''
    switch (periodType) {
      case 'day':
        periodValue = date.format('YYYY-MM-DD')
        break
      case 'week':
        // ISO week format: YYYY-WNN
        const weekYear = date.year()
        const weekNum = date.week()
        periodValue = `${weekYear}-W${weekNum.toString().padStart(2, '0')}`
        break
      case 'month':
        periodValue = date.format('YYYY-MM')
        break
      case 'quarter':
        const q = Math.ceil((date.month() + 1) / 3)
        periodValue = `${date.year()}-Q${q}`
        break
      case 'year':
        periodValue = date.format('YYYY')
        break
    }
    onChange?.(periodValue)
  }

  const getPickerProps = () => {
    switch (periodType) {
      case 'day':
        return {}
      case 'week':
        return { picker: 'week' as const }
      case 'month':
        return { picker: 'month' as const }
      case 'quarter':
        return { picker: 'quarter' as const }
      case 'year':
        return { picker: 'year' as const }
      default:
        return { picker: 'month' as const }
    }
  }

  const getDateValue = (): Dayjs | null => {
    if (!value) return null
    try {
      switch (periodType) {
        case 'day':
          return dayjs(value, 'YYYY-MM-DD')
        case 'week':
          // Parse YYYY-WNN format
          const match = value.match(/(\d{4})-W(\d{2})/)
          if (match) {
            const [, year, week] = match
            return dayjs().year(parseInt(year)).week(parseInt(week))
          }
          return null
        case 'month':
          return dayjs(value, 'YYYY-MM')
        case 'quarter':
          const qMatch = value.match(/(\d{4})-Q(\d)/)
          if (qMatch) {
            const [, year, q] = qMatch
            const month = (parseInt(q) - 1) * 3
            return dayjs().year(parseInt(year)).month(month)
          }
          return null
        case 'year':
          return dayjs(value, 'YYYY')
        default:
          return null
      }
    } catch {
      return null
    }
  }

  const handleQuickSelect = (type: 'this' | 'last', unit: PeriodType) => {
    const now = dayjs()
    let date: Dayjs | null = null

    if (type === 'this') {
      date = now
    } else {
      switch (unit) {
        case 'day':
          date = now.subtract(1, 'day')
          break
        case 'week':
          date = now.subtract(1, 'week')
          break
        case 'month':
          date = now.subtract(1, 'month')
          break
        case 'quarter':
          date = now.subtract(3, 'month')
          break
        case 'year':
          date = now.subtract(1, 'year')
          break
      }
    }

    if (date) {
      // Update period type first
      handlePeriodTypeChange(unit)
      // Then set the date value
      setTimeout(() => {
        handleDateChange(date)
      }, 0)
    }
  }

  const quickSelectButtons = useMemo(() => {
    const buttons = [
      { key: 'this-week', label: '本周', type: 'this' as const, unit: 'week' as const },
      { key: 'last-week', label: '上周', type: 'last' as const, unit: 'week' as const },
      { key: 'this-month', label: '本月', type: 'this' as const, unit: 'month' as const },
      { key: 'last-month', label: '上月', type: 'last' as const, unit: 'month' as const },
      { key: 'this-quarter', label: '本季', type: 'this' as const, unit: 'quarter' as const },
      { key: 'last-quarter', label: '上季', type: 'last' as const, unit: 'quarter' as const },
      { key: 'this-year', label: '本年', type: 'this' as const, unit: 'year' as const },
      { key: 'last-year', label: '去年', type: 'last' as const, unit: 'year' as const },
    ]
    return buttons
  }, [])

  return (
    <div className="pf-period-picker flex flex-col gap-2">
      {showHint && (
        <p className="mb-1 flex items-start gap-1.5 text-meta text-muted">
          <InfoCircleOutlined className="mt-0.5 shrink-0" aria-hidden />
          <span>{hint}</span>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={periodType}
          onChange={handlePeriodTypeChange}
          style={{ width: 72 }}
          size="small"
        >
          {PERIOD_TYPE_OPTIONS.map((opt) => (
            <Option key={opt.value} value={opt.value}>
              {opt.label}
            </Option>
          ))}
        </Select>

        <div className="min-w-[120px] flex-1 tablet:max-w-[180px]">
          <DatePicker
            {...getPickerProps()}
            value={getDateValue()}
            onChange={handleDateChange}
            placeholder="选择账期"
            style={{ width: '100%' }}
            size="small"
            locale={locale.DatePicker}
          />
        </div>

        <span className="hidden min-[360px]:inline-flex">
        <Tooltip title="上一期">
          <Button
            icon={<ArrowLeftOutlined />}
            size="small"
            onClick={() => {
              const current = getDateValue()
              if (current) {
                let prev: Dayjs | null = null
                switch (periodType) {
                  case 'day':
                    prev = current.subtract(1, 'day')
                    break
                  case 'week':
                    prev = current.subtract(1, 'week')
                    break
                  case 'month':
                    prev = current.subtract(1, 'month')
                    break
                  case 'quarter':
                    prev = current.subtract(3, 'month')
                    break
                  case 'year':
                    prev = current.subtract(1, 'year')
                    break
                }
                if (prev) handleDateChange(prev)
              }
            }}
            disabled={!value}
          />
        </Tooltip>
        </span>

        <span className="hidden min-[360px]:inline-flex">
        <Tooltip title="下一期">
          <Button
            icon={<ArrowRightOutlined />}
            size="small"
            onClick={() => {
              const current = getDateValue()
              if (current) {
                let next: Dayjs | null = null
                switch (periodType) {
                  case 'day':
                    next = current.add(1, 'day')
                    break
                  case 'week':
                    next = current.add(1, 'week')
                    break
                  case 'month':
                    next = current.add(1, 'month')
                    break
                  case 'quarter':
                    next = current.add(3, 'month')
                    break
                  case 'year':
                    next = current.add(1, 'year')
                    break
                }
                if (next) handleDateChange(next)
              }
            }}
            disabled={!value}
          />
        </Tooltip>
        </span>
      </div>

      {showQuickSelect && (
        <Space wrap size="small">
          {quickSelectButtons.map((btn) => (
            <Button
              key={btn.key}
              size="small"
              type="text"
              onClick={() => handleQuickSelect(btn.type, btn.unit)}
            >
              {btn.label}
            </Button>
          ))}
        </Space>
      )}
    </div>
  )
}
