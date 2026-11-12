'use client'

import { useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { FieldFrame, triggerClass, triggerErrorClass } from './FieldFrame'
import { FloatingPanel } from './FloatingPanel'

/**
 * 单值选择器（浮层列表）：机构 / 渠道 / 流动性 / 归属共用。
 *
 * 为什么不用原生 `<select>`：归属要显示角色徽标、机构要显示类型，
 * 原生 option 只能塞纯文本。这里用「触发器 + 浮层列表」保住信息密度，
 * 触发器和字段外壳仍走同一套 44px 与焦点规范。
 */
export interface OptionItem {
  value: string
  label: string
  /** 行尾小徽标（角色、类型…）。 */
  badge?: string
  /** 名称下方的一行说明。 */
  hint?: string
  disabled?: boolean
}

export interface OptionPickerProps {
  options: readonly OptionItem[]
  value: string
  onChange: (value: string) => void
  label?: React.ReactNode
  required?: boolean
  error?: string
  hint?: React.ReactNode
  disabled?: boolean
  placeholder?: string
  clearable?: boolean
  loading?: boolean
  emptyText?: string
  /** 选项多于此数才出现搜索框。 */
  searchThreshold?: number
}

export function OptionPicker({
  options,
  value,
  onChange,
  label = '请选择',
  required = false,
  error,
  hint,
  disabled = false,
  placeholder = '请选择',
  clearable = false,
  loading = false,
  emptyText = '暂无可选项',
  searchThreshold = 8,
}: OptionPickerProps) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const triggerRef = useRef<HTMLButtonElement>(null)

  const selected = useMemo(() => options.find((option) => option.value === value) ?? null, [options, value])
  const searchable = options.length > searchThreshold
  const keyword = search.trim().toLowerCase()
  const visible = useMemo(() => {
    if (!keyword) return options
    return options.filter((option) =>
      `${option.label} ${option.badge ?? ''} ${option.hint ?? ''}`.toLowerCase().includes(keyword))
  }, [keyword, options])

  const close = () => {
    setOpen(false)
    setSearch('')
  }

  const pick = (next: string) => {
    onChange(next)
    close()
  }

  return (
    <FieldFrame label={label} required={required} error={error} hint={hint}>
      <div className="flex items-stretch gap-1.5">
        <button
          ref={triggerRef}
          type="button"
          disabled={disabled}
          onClick={() => setOpen((previous) => !previous)}
          aria-label={typeof label === 'string' ? label : undefined}
          aria-haspopup="dialog"
          aria-expanded={open}
          className={cn(
            triggerClass,
            'flex items-center gap-2 text-left',
            error && triggerErrorClass,
            !selected && 'text-muted',
          )}
        >
          <span className="min-w-0 flex-1 truncate">
            {selected ? selected.label : placeholder}
          </span>
          {selected?.badge && (
            <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-micro text-secondary">
              {selected.badge}
            </span>
          )}
          <ChevronDown size={16} className={cn('shrink-0 text-muted transition-transform', open && 'rotate-180')} aria-hidden />
        </button>
        {clearable && selected && !disabled && (
          <button
            type="button"
            onClick={() => onChange('')}
            className="shrink-0 rounded-control border border-line-strong bg-surface px-3 text-meta text-secondary"
          >
            清除
          </button>
        )}
      </div>

      <FloatingPanel open={open} anchorRef={triggerRef} onClose={close} label={typeof label === 'string' ? label : '选项'}>
        {searchable && (
          <div className="shrink-0 border-b border-line p-2">
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
              <input
                autoFocus
                type="text"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索"
                aria-label="搜索选项"
                className="min-h-[40px] w-full rounded-control border border-line bg-surface pl-7 pr-2 text-body text-ink placeholder:text-muted"
              />
            </div>
          </div>
        )}
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-1.5">
          {loading && <li className="px-2 py-3 text-meta text-muted">加载中…</li>}
          {!loading && visible.length === 0 && <li className="px-2 py-3 text-meta text-muted">{emptyText}</li>}
          {!loading && visible.map((option) => {
            const active = option.value === value
            return (
              <li key={option.value}>
                <button
                  type="button"
                  disabled={option.disabled}
                  onClick={() => pick(option.value)}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-control px-2 py-2 text-left text-body',
                    option.disabled
                      ? 'cursor-not-allowed text-disabled'
                      : active ? 'bg-brand-50 text-brand-800' : 'text-ink hover:bg-subtle',
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    {option.hint && <span className="block truncate text-micro text-muted">{option.hint}</span>}
                  </span>
                  {option.badge && (
                    <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-micro text-secondary">
                      {option.badge}
                    </span>
                  )}
                  {active && <Check size={15} className="shrink-0 text-brand-600" aria-hidden />}
                </button>
              </li>
            )
          })}
        </ul>
      </FloatingPanel>
    </FieldFrame>
  )
}