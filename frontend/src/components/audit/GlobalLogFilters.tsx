'use client'

import { useId, useState } from 'react'
import { SlidersHorizontal, X } from 'lucide-react'
import type { AuditLogQuery } from '@/lib/api'
import { FilterSheet } from '@/components/ui'
import { FormField } from '@/components/form'
import { cn } from '@/lib/utils'
import {
  ACTION_LABELS,
  COMMON_ACTIONS,
  COMMON_ENTITIES,
  ENTITY_LABELS,
} from './GlobalLogModel'

/**
 * 全局操作日志的筛选：桌面端一条常驻筛选条，手机端收进 `FilterSheet`。
 *
 * 时间与请求号这类「边打边查」的字段不在本组件里防抖——页面统一把整个筛选对象
 * 延后 300ms 生效（见 `page.tsx`），否则每个字段各写一份计时器必然漂移。
 */

export interface GlobalLogFiltersValue {
  entity: string
  action: string
  actor: string
  dateFrom: string
  dateTo: string
  requestId: string
  /** 幂等键：服务端精确匹配（`idempotency_key`）；键本身 7 天后过期，见 `IDEM_NOTE`。 */
  idemKey: string
}

export const EMPTY_GLOBAL_LOG_FILTERS: GlobalLogFiltersValue = {
  entity: '',
  action: '',
  actor: '',
  dateFrom: '',
  dateTo: '',
  requestId: '',
  idemKey: '',
}

export const PAGE_SIZE_OPTIONS = [20, 50, 100]
export const DEFAULT_PAGE_SIZE = 50

/**
 * 幂等键的口径说明（常驻在输入框旁）。
 *
 * 键有 7 天 TTL：过期的是「可复算的键」，不是记录本身——旧操作记录按当时的键原样留在库里，
 * 用原值仍能查到。所以查不到时不能断定「没发生过」，界面上要留这句话。
 */
export const IDEM_NOTE = '幂等键 7 天后过期，旧记录仍按原值留存。'

/** 桌面筛选条里只放短句，完整解释走 title。 */
const IDEM_HINT = '幂等键 7 天后过期，旧记录仍按原值留存'

const ACTOR_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部主体' },
  { value: 'owner', label: '我' },
  { value: 'system', label: '系统' },
]

export function countActiveFilters(filters: GlobalLogFiltersValue): number {
  return [
    filters.entity,
    filters.action,
    filters.actor,
    filters.dateFrom,
    filters.dateTo,
    filters.requestId,
    filters.idemKey,
  ].filter((value) => value.trim() !== '').length
}

export function hasActiveFilters(filters: GlobalLogFiltersValue): boolean {
  return countActiveFilters(filters) > 0
}

function validDate(value: string | null): string {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : ''
}

function validPage(value: string | null): number {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 1
}

export interface ParsedGlobalLogLocation {
  filters: GlobalLogFiltersValue
  page: number
  pageSize: number
}

/** URL → 状态（刷新不丢、可分享；非法值按「没传」处理，不报错）。 */
export function filtersFromSearchParams(params: URLSearchParams): ParsedGlobalLogLocation {
  const pageSizeRaw = Number(params.get('page_size'))
  return {
    filters: {
      entity: params.get('entity')?.trim() ?? '',
      action: params.get('action')?.trim() ?? '',
      actor: params.get('actor')?.trim() ?? '',
      dateFrom: validDate(params.get('date_from')),
      dateTo: validDate(params.get('date_to')),
      requestId: params.get('request_id')?.trim() ?? '',
      idemKey: params.get('idem')?.trim() ?? '',
    },
    page: validPage(params.get('page')),
    pageSize: PAGE_SIZE_OPTIONS.includes(pageSizeRaw) ? pageSizeRaw : DEFAULT_PAGE_SIZE,
  }
}

/** 状态 → URL。空条件不进 query，免得链接里挂一串 `entity=&action=`。 */
export function buildSearchParams(
  filters: GlobalLogFiltersValue,
  page: number,
  pageSize: number,
): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.entity.trim()) params.set('entity', filters.entity.trim())
  if (filters.action.trim()) params.set('action', filters.action.trim())
  if (filters.actor.trim()) params.set('actor', filters.actor.trim())
  if (filters.dateFrom) params.set('date_from', filters.dateFrom)
  if (filters.dateTo) params.set('date_to', filters.dateTo)
  if (filters.requestId.trim()) params.set('request_id', filters.requestId.trim())
  if (filters.idemKey.trim()) params.set('idem', filters.idemKey.trim())
  if (page > 1) params.set('page', String(page))
  if (pageSize !== DEFAULT_PAGE_SIZE) params.set('page_size', String(pageSize))
  return params
}

/** 状态 → 请求参数（全部服务端筛选；幂等键走 `idempotency_key` 精确匹配）。 */
export function toAuditLogQuery(
  filters: GlobalLogFiltersValue,
  page: number,
  pageSize: number,
): AuditLogQuery {
  return {
    ...(filters.entity.trim() ? { entity: filters.entity.trim() } : {}),
    ...(filters.action.trim() ? { action: filters.action.trim() } : {}),
    ...(filters.actor.trim() ? { actor: filters.actor.trim() } : {}),
    ...(filters.dateFrom ? { date_from: filters.dateFrom } : {}),
    ...(filters.dateTo ? { date_to: filters.dateTo } : {}),
    ...(filters.requestId.trim() ? { request_id: filters.requestId.trim() } : {}),
    ...(filters.idemKey.trim() ? { idempotency_key: filters.idemKey.trim() } : {}),
    page,
    page_size: pageSize,
    order: 'desc',
  }
}

const INPUT_CLASS =
  'min-h-[44px] w-full rounded-control border border-line-strong bg-surface px-2.5 text-meta text-ink placeholder:text-muted'

function byLabel([aKey, aLabel]: [string, string], [bKey, bLabel]: [string, string]) {
  const order = aLabel.localeCompare(bLabel, 'zh-CN')
  return order !== 0 ? order : aKey.localeCompare(bKey)
}

/** 常用值排前面（登记表里的高频写路径），其余按中文名排。 */
function orderedOptions(labels: Record<string, string>, common: Set<string>) {
  const entries = Object.entries(labels)
  return [
    ...entries.filter(([key]) => common.has(key)).sort(byLabel),
    ...entries.filter(([key]) => !common.has(key)).sort(byLabel),
  ]
}

const ENTITY_OPTIONS = orderedOptions(ENTITY_LABELS, COMMON_ENTITIES)
const ACTION_OPTIONS = orderedOptions(ACTION_LABELS, COMMON_ACTIONS)

/**
 * 实体 / 动作用 `input + datalist`：既能从常量表里选（下拉显示中文名，落库用英文值），
 * 也能手打后端新增、前端还不认识的值——后端没有枚举端点，写死成 `select` 会让新实体
 * 彻底筛不出来。
 */
function ValueCombobox({
  listId,
  value,
  onChange,
  placeholder,
  ariaLabel,
  options,
}: {
  listId: string
  value: string
  onChange: (next: string) => void
  placeholder: string
  ariaLabel: string
  options: Array<[string, string]>
}) {
  return (
    <>
      <input
        type="text"
        list={listId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        spellCheck={false}
        autoComplete="off"
        className={INPUT_CLASS}
      />
      <datalist id={listId}>
        {options.map(([key, label]) => (
          <option key={key} value={key} label={label} />
        ))}
      </datalist>
    </>
  )
}

interface FieldSetProps {
  value: GlobalLogFiltersValue
  onChange: (next: GlobalLogFiltersValue) => void
  idPrefix: string
  /** 抽屉里用 FormField 显式标签；筛选条里靠 placeholder + aria-label。 */
  labelled: boolean
}

function GlobalLogFilterFields({ value, onChange, idPrefix, labelled }: FieldSetProps) {
  const entityId = `${idPrefix}-entity`
  const actionId = `${idPrefix}-action`

  const setEntity = (entity: string) => onChange({ ...value, entity })
  const setAction = (action: string) => onChange({ ...value, action })
  const setActor = (actor: string) => onChange({ ...value, actor })
  const setFrom = (dateFrom: string) => onChange({ ...value, dateFrom })
  const setTo = (dateTo: string) => onChange({ ...value, dateTo })
  const setRequestId = (requestId: string) => onChange({ ...value, requestId })
  const setIdem = (idemKey: string) => onChange({ ...value, idemKey })

  const entityInput = (
    <ValueCombobox
      listId={entityId}
      value={value.entity}
      onChange={setEntity}
      placeholder="实体（如 asset_item）"
      ariaLabel="按实体筛选"
      options={ENTITY_OPTIONS}
    />
  )
  const actionInput = (
    <ValueCombobox
      listId={actionId}
      value={value.action}
      onChange={setAction}
      placeholder="操作（如 correct）"
      ariaLabel="按操作筛选"
      options={ACTION_OPTIONS}
    />
  )
  const actorInput = (
    <select
      value={value.actor}
      onChange={(event) => setActor(event.target.value)}
      aria-label="按主体筛选"
      className={INPUT_CLASS}
    >
      {ACTOR_OPTIONS.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
  )
  const fromInput = (
    <input
      type="date"
      value={value.dateFrom}
      onChange={(event) => setFrom(event.target.value)}
      aria-label="开始日期"
      className={INPUT_CLASS}
    />
  )
  const toInput = (
    <input
      type="date"
      value={value.dateTo}
      onChange={(event) => setTo(event.target.value)}
      aria-label="结束日期"
      className={INPUT_CLASS}
    />
  )
  const requestInput = (
    <input
      type="text"
      value={value.requestId}
      onChange={(event) => setRequestId(event.target.value)}
      placeholder="请求号（精确匹配）"
      aria-label="按请求号筛选"
      spellCheck={false}
      autoComplete="off"
      className={cn(INPUT_CLASS, 'font-code')}
    />
  )
  const idemInput = (
    <input
      type="text"
      value={value.idemKey}
      onChange={(event) => setIdem(event.target.value)}
      placeholder="幂等键（精确匹配）"
      aria-label="按幂等键筛选"
      title={IDEM_NOTE}
      spellCheck={false}
      autoComplete="off"
      className={cn(INPUT_CLASS, 'font-code')}
    />
  )

  if (labelled) {
    return (
      <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
        <FormField label="实体">{entityInput}</FormField>
        <FormField label="操作">{actionInput}</FormField>
        <FormField label="主体">{actorInput}</FormField>
        <div className="grid grid-cols-2 gap-2">
          <FormField label="开始日期">{fromInput}</FormField>
          <FormField label="结束日期">{toInput}</FormField>
        </div>
        <FormField label="请求号" hint="一次操作一个请求号，精确匹配。">{requestInput}</FormField>
        <FormField label="幂等键" hint={IDEM_NOTE}>{idemInput}</FormField>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-[150px] flex-1 tablet:max-w-[190px]">{entityInput}</div>
        <div className="min-w-[150px] flex-1 tablet:max-w-[190px]">{actionInput}</div>
        <div className="min-w-[104px]">{actorInput}</div>
        <div className="flex items-center gap-1.5">
          <div className="w-[152px]">{fromInput}</div>
          <span className="text-muted">–</span>
          <div className="w-[152px]">{toInput}</div>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-[180px] flex-1 tablet:max-w-[260px]">{requestInput}</div>
        <div className="min-w-[180px] flex-1 tablet:max-w-[260px]">{idemInput}</div>
        <p className="min-w-0 text-micro text-muted" title={IDEM_NOTE}>
          {IDEM_HINT}
        </p>
      </div>
    </div>
  )
}

export interface GlobalLogFiltersProps {
  value: GlobalLogFiltersValue
  onChange: (next: GlobalLogFiltersValue) => void
  onReset: () => void
  activeCount: number
  className?: string
}

export function GlobalLogFilters({
  value,
  onChange,
  onReset,
  activeCount,
  className,
}: GlobalLogFiltersProps) {
  const idPrefix = useId()
  const [sheetOpen, setSheetOpen] = useState(false)
  const [draft, setDraft] = useState<GlobalLogFiltersValue>(value)

  const openSheet = () => {
    setDraft(value)
    setSheetOpen(true)
  }
  const applySheet = () => {
    onChange(draft)
    setSheetOpen(false)
  }
  const resetSheet = () => {
    setDraft(EMPTY_GLOBAL_LOG_FILTERS)
    onReset()
    setSheetOpen(false)
  }

  return (
    <div className={className}>
      {/* ≥768：常驻筛选条 */}
      <div className="hidden rounded-card border border-line bg-surface p-3 tablet:block">
        <div className="mb-2 flex items-center justify-between gap-2">
          <p className="text-micro font-medium text-muted">筛选</p>
          {activeCount > 0 && (
            <button
              type="button"
              onClick={onReset}
              className="inline-flex min-h-[32px] items-center gap-1 rounded-control px-1.5 text-micro font-medium text-link transition-colors hover:bg-subtle"
            >
              <X size={12} aria-hidden />
              清除筛选（{activeCount}）
            </button>
          )}
        </div>
        <GlobalLogFilterFields
          value={value}
          onChange={onChange}
          idPrefix={`${idPrefix}-bar`}
          labelled={false}
        />
      </div>

      {/* <768：收进筛选抽屉 */}
      <div className="flex items-center gap-2 tablet:hidden">
        <button
          type="button"
          onClick={openSheet}
          className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
        >
          <SlidersHorizontal size={15} aria-hidden />
          筛选{activeCount > 0 ? `（${activeCount}）` : ''}
        </button>
        {activeCount > 0 && (
          <button
            type="button"
            onClick={onReset}
            className="inline-flex min-h-[44px] items-center rounded-control px-2 text-meta text-link"
          >
            清除
          </button>
        )}
      </div>

      {sheetOpen && (
        <FilterSheet
          open
          onClose={() => setSheetOpen(false)}
          onReset={resetSheet}
          onApply={applySheet}
          applyLabel={`应用筛选${countActiveFilters(draft) > 0 ? `（${countActiveFilters(draft)}）` : ''}`}
        >
          <GlobalLogFilterFields
            value={draft}
            onChange={setDraft}
            idPrefix={`${idPrefix}-sheet`}
            labelled
          />
        </FilterSheet>
      )}
    </div>
  )
}