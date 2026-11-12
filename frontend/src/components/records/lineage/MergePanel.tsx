'use client'

import { useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import Decimal from 'decimal.js'
import { Check, Search } from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { cn } from '@/lib/utils'
import { AmountField, DateField, FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import { InlineError } from '@/components/ui'
import type { CurrentValueItem } from '@/types'
import {
  ConsequenceNotice,
  formatAmountInput,
  lineageErrorText,
  toAmountDecimal,
  toAmountString,
} from './shared'

/**
 * 动作二 合并（N→1）：多选旧项 → 新项名 → `POST /api/lineage-links`（link_type=merge）。
 *
 * - 承接金额预填＝所选旧项当前值合计，人确认后可改（口径不是机器推算出来的）；
 * - 生效日默认今天（取当前值视图的参考日＝后端「今天」）；
 * - 理由必填（R-9）。
 */
export interface MergePanelProps {
  item: CurrentValueItem
  /** 当前值视图的全部行：合并候选＝其中的 active 项。 */
  candidates: CurrentValueItem[]
  /** 已被占用的名称（显示名 / 历史名 / legacy 名）。 */
  takenNames: Set<string>
  /** 后端「今天」或客户端今天。 */
  today: string
  onSubmitted: (name: string, count: number) => void
}

type FieldKey = 'name' | 'principal' | 'reason' | 'date' | 'selection'

export function MergePanel({ item, candidates, takenNames, today, onSubmitted }: MergePanelProps) {
  const activeCandidates = useMemo(
    () => candidates.filter((candidate) => candidate.item_status === 'active'),
    [candidates],
  )
  const initialSelection = useMemo(
    () => (item.item_status === 'active' && activeCandidates.some((c) => c.item_id === item.item_id)
      ? [item.item_id]
      : []),
    [item.item_id, item.item_status, activeCandidates],
  )

  const [selectedIds, setSelectedIds] = useState<number[]>(initialSelection)
  const [search, setSearch] = useState('')
  const [toName, setToName] = useState('')
  const [principal, setPrincipal] = useState('')
  const [principalTouched, setPrincipalTouched] = useState(false)
  const [effectiveDate, setEffectiveDate] = useState(today)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<FieldKey, string>>>({})

  const archived = item.item_status === 'archived'

  const selectedItems = useMemo(
    () => activeCandidates.filter((candidate) => selectedIds.includes(candidate.item_id)),
    [activeCandidates, selectedIds],
  )
  const selectedSum = useMemo(
    () => selectedItems.reduce((sum, candidate) => sum.plus(toAmountDecimal(candidate.amount)), new Decimal(0)),
    [selectedItems],
  )
  /** 未手改时金额跟随选择，避免「改了选择忘了改预填值」。 */
  const principalValue = principalTouched ? principal : formatAmountInput(selectedSum)

  const filteredCandidates = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return activeCandidates
    return activeCandidates.filter((candidate) =>
      candidate.display_name.toLowerCase().includes(keyword),
    )
  }, [activeCandidates, search])

  const toggle = (itemId: number) => {
    setSelectedIds((previous) => (
      previous.includes(itemId)
        ? previous.filter((id) => id !== itemId)
        : [...previous, itemId]
    ))
    setFieldErrors((previous) => ({ ...previous, selection: undefined }))
  }

  const trimmedName = toName.trim()
  const nameTaken = trimmedName !== '' && takenNames.has(trimmedName)
  const nameError = fieldErrors.name
    ?? (nameTaken ? '该名称已被现有资产项或其历史名占用；合并目标必须是新名称' : undefined)
  const principalError = fieldErrors.principal
    ?? (toAmountString(principalValue) === null ? '填写承接金额（两位小数，≥ 0）' : undefined)
  const reasonError = fieldErrors.reason
  const dateError = fieldErrors.date
  const selectionError = fieldErrors.selection

  const blockers: string[] = []
  if (selectionError) blockers.push(selectionError)
  if (selectedIds.length < 2) blockers.push(`还需再选 ${2 - selectedIds.length} 个旧项`)
  if (!trimmedName) blockers.push('填写新项名称')
  else if (nameTaken) blockers.push('新项名称已被占用')
  if (principalError) blockers.push('承接金额格式不对')
  if (!effectiveDate) blockers.push('选择生效日期')
  if (!reason.trim()) blockers.push('填写登记理由')

  const blocked = blockers.length > 0 || archived

  const mutation = useMutation({
    mutationFn: () => {
      const amount = toAmountString(principalValue)
      if (amount === null) throw new Error('承接金额格式不对')
      return api.createLineageLink({
        from_item_ids: selectedIds,
        to_item_name: trimmedName,
        link_type: 'merge',
        principal: amount,
        effective_date: effectiveDate,
        reason: reason.trim(),
      }, newIdemKey())
    },
    onSuccess: () => onSubmitted(trimmedName, selectedIds.length),
    onError: (mutationError) => {
      const text = lineageErrorText(mutationError, '合并登记失败，请重试')
      const field = mutationError instanceof ApiError && typeof mutationError.details?.field === 'string'
        ? mutationError.details.field
        : null
      if (field === 'to_item_name') setFieldErrors({ name: text })
      else if (field === 'principal') setFieldErrors({ principal: text })
      else if (field === 'reason') setFieldErrors({ reason: text })
      else if (field === 'effective_date') setFieldErrors({ date: text })
      else if (field === 'from_item_ids') setFieldErrors({ selection: text })
      else setError(text)
    },
  })

  const submit = () => {
    if (blocked || mutation.isPending) return
    setError(null)
    setFieldErrors({})
    mutation.mutate()
  }

  return (
    <div className="space-y-4">
      <p className="text-meta text-secondary">
        勾选 2 个以上旧项，合并成一个新项。新项从生效日起承接金额，勾选的旧项自该日起归档。
      </p>

      {archived && (
        <div className="rounded-control border border-status-warn/25 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text">
          该资产项已归档，不能再作为合并的旧项；血缘记录只读。
        </div>
      )}

      <section className="space-y-2" aria-label="选择旧项">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h4 className="text-h3 font-semibold text-ink">选择旧项</h4>
          <span className="pf-num text-meta text-secondary">
            已选 {selectedIds.length} 项 · 合计 {formatCurrency(selectedSum.toFixed(2))}
          </span>
        </div>

        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
            aria-hidden
          />
          <input
            type="text"
            inputMode="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索旧项"
            aria-label="搜索旧项"
            className={cn(fieldControlClass, 'pl-9')}
          />
        </div>

        <div className="max-h-[240px] divide-y divide-line overflow-y-auto rounded-control border border-line">
          {filteredCandidates.length === 0 ? (
            <p className="px-3 py-4 text-meta text-muted">
              {activeCandidates.length === 0 ? '当前没有可合并的在用资产项。' : '没有匹配的旧项。'}
            </p>
          ) : (
            filteredCandidates.map((candidate) => {
              const checked = selectedIds.includes(candidate.item_id)
              return (
                <label
                  key={candidate.item_id}
                  className={cn(
                    'flex min-h-[44px] cursor-pointer items-center gap-3 px-3 py-2 transition-colors',
                    checked ? 'bg-brand-50/60' : 'hover:bg-subtle',
                  )}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(candidate.item_id)}
                    disabled={archived}
                    className="h-4 w-4 shrink-0 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed disabled:opacity-40"
                  />
                  <span className="min-w-0 flex-1 truncate text-body text-ink" title={candidate.display_name}>
                    {candidate.display_name}
                  </span>
                  <span className={cn('pf-num shrink-0 text-meta', checked ? 'text-ink' : 'text-secondary')}>
                    {candidate.amount === null ? '未录入' : formatCurrency(candidate.amount)}
                  </span>
                  {checked && <Check size={14} className="shrink-0 text-brand-600" aria-hidden />}
                </label>
              )
            })
          )}
        </div>
        {selectionError && (
          <p role="alert" className="text-meta text-status-critical-text">{selectionError}</p>
        )}
      </section>

      <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
        <FormField
          label="新项名称"
          required
          error={nameError}
          hint="不能与现有资产项或其历史名重名"
          className="tablet:col-span-2"
        >
          <input
            type="text"
            value={toName}
            onChange={(event) => {
              setToName(event.target.value)
              setFieldErrors((previous) => ({ ...previous, name: undefined }))
            }}
            placeholder="如：示例支付-黄金（合并后）"
            autoComplete="off"
            className={cn(fieldControlClass, nameError && fieldErrorClass)}
          />
        </FormField>

        <AmountField
          label="承接金额（元）"
          required
          value={principalValue}
          onChange={(value) => {
            setPrincipal(value)
            setPrincipalTouched(true)
            setFieldErrors((previous) => ({ ...previous, principal: undefined }))
          }}
          error={principalError}
          hint={
            principalTouched
              ? `所选旧项当前值合计 ${formatCurrency(selectedSum.toFixed(2))}（已按实际修改）`
              : '预填＝所选旧项当前值合计，请按实际确认后提交'
          }
        />

        <DateField
          label="生效日期"
          required
          value={effectiveDate}
          onChange={(value) => {
            setEffectiveDate(value)
            setFieldErrors((previous) => ({ ...previous, date: undefined }))
          }}
          error={dateError}
          hint="旧项自该日起终止计量，此前历史仍计入旧项"
        />
      </div>

      <FormField
        label="登记理由"
        required
        error={reasonError}
        hint="口径承接是人确认的裁决，必须留下理由"
      >
        <textarea
          value={reason}
          onChange={(event) => {
            setReason(event.target.value)
            setFieldErrors((previous) => ({ ...previous, reason: undefined }))
          }}
          rows={2}
          placeholder="如：两个旧项是同一笔持仓，按账户合并登记"
          className={cn(fieldControlClass, 'min-h-[76px] resize-none py-2', reasonError && fieldErrorClass)}
        />
      </FormField>

      <ConsequenceNotice />

      {error && <InlineError>{error}</InlineError>}

      <div className="space-y-2">
        {blockers.length > 0 && (
          <p className="text-meta text-status-warn-text">{blockers.join('；')}</p>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={blocked || mutation.isPending}
          className="inline-flex min-h-[44px] w-full items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white transition-colors hover:bg-action-primary-hover disabled:cursor-not-allowed disabled:opacity-50 tablet:w-auto"
        >
          {mutation.isPending ? '登记中…' : '登记合并'}
        </button>
      </div>
    </div>
  )
}