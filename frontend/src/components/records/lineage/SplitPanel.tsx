'use client'

import { useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import Decimal from 'decimal.js'
import { Plus, Trash2 } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { cn } from '@/lib/utils'
import { DateField, FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import { InlineError } from '@/components/ui'
import type { CurrentValueItem } from '@/types'
import {
  AMOUNT_INPUT_PATTERN,
  ConsequenceNotice,
  PERCENT_INPUT_PATTERN,
  RATIO_TOLERANCE,
  cleanNumericInput,
  extractTargetErrors,
  lineageErrorText,
  toAmountString,
  toPercentDecimal,
  toRatioString,
} from './shared'

/**
 * 动作三 拆分（1→N）：父项 → 多子项（名称 / 比例 / 承接金额）→
 * `POST /api/lineage-links/split`（spec §1 冻结契约）。
 *
 * - 比例用百分数输入（最多两位小数），提交换算成 0–1 四位小数字符串；
 * - 合计实时显示，|Σ−1| > 1e-4 时按钮禁用（后端只给 ±1e-4 容差）；
 * - 承接金额默认按父项当前值 × 比例带入，手改后不再覆盖（人确认）；
 * - 后端 400 的逐行错误按 targets 序号标注到对应子项。
 */
export interface SplitPanelProps {
  item: CurrentValueItem
  /** 已被占用的名称（显示名 / 历史名 / legacy 名）。 */
  takenNames: Set<string>
  /** 后端「今天」或客户端今天。 */
  today: string
  onSubmitted: (childCount: number, archivedFrom: string) => void
}

interface SplitRow {
  key: string
  name: string
  /** 百分数文本（0–100），提交时 ÷100。 */
  percent: string
  principal: string
  principalTouched: boolean
}

interface RowIssues {
  name?: string
  percent?: string
  principal?: string
}

export function SplitPanel({ item, takenNames, today, onSubmitted }: SplitPanelProps) {
  const seqRef = useRef(0)
  const makeRow = (percent: string): SplitRow => ({
    key: `split-${seqRef.current++}`,
    name: '',
    percent,
    principal: '',
    principalTouched: false,
  })

  const [rows, setRows] = useState<SplitRow[]>(() => [makeRow('50'), makeRow('50')])
  const [effectiveDate, setEffectiveDate] = useState(today)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [serverRowErrors, setServerRowErrors] = useState<Map<number, string[]>>(() => new Map())

  const archived = item.item_status === 'archived'
  const parentAmount = item.amount

  const principalValue = (row: SplitRow): string => {
    if (row.principalTouched) return row.principal
    const ratio = toRatioString(row.percent)
    if (ratio === null || parentAmount === null) return ''
    return new Decimal(parentAmount).times(ratio).toFixed(2)
  }

  const percentSum = useMemo(
    () => rows.reduce((sum, row) => sum.plus(toPercentDecimal(row.percent)), new Decimal(0)),
    [rows],
  )
  const ratioSum = useMemo(
    () => rows.reduce((sum, row) => {
      const ratio = toRatioString(row.percent)
      return ratio === null ? sum : sum.plus(new Decimal(ratio))
    }, new Decimal(0)),
    [rows],
  )
  const ratioDelta = ratioSum.minus(1)
  const ratioOk = ratioDelta.abs().lte(RATIO_TOLERANCE)
  const percentSumText = percentSum.toFixed(2)
  const meterWidth = Math.min(100, Math.max(0, percentSum.toNumber()))

  const trimmedNames = rows.map((row) => row.name.trim())
  const duplicateNames = new Set<string>()
  {
    const seen = new Set<string>()
    for (const name of trimmedNames) {
      if (!name) continue
      if (seen.has(name)) duplicateNames.add(name)
      seen.add(name)
    }
  }

  const rowIssues: RowIssues[] = rows.map((row, index) => {
    const name = trimmedNames[index]
    const issues: RowIssues = {}
    if (name && duplicateNames.has(name)) issues.name = '子项名称重复'
    else if (name && takenNames.has(name)) issues.name = '该名称已被现有资产项或历史名占用'
    if (row.percent.trim() !== '' && toRatioString(row.percent) === null) {
      issues.percent = '比例需在 (0, 100] 之间'
    }
    const principal = principalValue(row)
    if (principal.trim() !== '' && toAmountString(principal) === null) {
      issues.principal = '金额需为两位小数且 ≥ 0'
    }
    return issues
  })

  const missingNames = trimmedNames.filter((name) => name === '').length
  const takenNameCount = rows.filter((_, index) => (
    trimmedNames[index] !== ''
    && !duplicateNames.has(trimmedNames[index])
    && takenNames.has(trimmedNames[index])
  )).length
  const missingRatios = rows.filter((row) => toRatioString(row.percent) === null).length
  const missingPrincipals = rows.filter((row) => {
    // 比例还没填时金额也无可带入，只报比例，避免同一行两条噪音
    if (!row.principalTouched && toRatioString(row.percent) === null) return false
    return toAmountString(principalValue(row)) === null
  }).length

  const blockers: string[] = []
  if (missingNames > 0) blockers.push(`${missingNames} 个子项缺名称`)
  if (duplicateNames.size > 0) blockers.push('子项名称不能重复')
  if (takenNameCount > 0) blockers.push(`${takenNameCount} 个子项名称已被占用`)
  if (missingRatios > 0) blockers.push(`${missingRatios} 个子项比例未填或不合法`)
  else if (!ratioOk) blockers.push(`比例合计需等于 100%（当前 ${percentSumText}%）`)
  if (missingPrincipals > 0) blockers.push(`${missingPrincipals} 个子项承接金额未填或不合法`)
  if (!effectiveDate) blockers.push('选择生效日期')
  if (!reason.trim()) blockers.push('填写登记理由')

  const blocked = blockers.length > 0 || archived

  const patchRow = (key: string, patch: Partial<SplitRow>) => {
    setRows((previous) => previous.map((row) => (row.key === key ? { ...row, ...patch } : row)))
  }

  const addRow = () => {
    const remaining = Decimal.max(0, new Decimal(100).minus(percentSum))
    setRows((previous) => [
      ...previous,
      makeRow(remaining.gt(0) ? remaining.toFixed(2) : ''),
    ])
  }

  const removeRow = (key: string) => {
    setRows((previous) => (previous.length <= 2 ? previous : previous.filter((row) => row.key !== key)))
  }

  const mutation = useMutation({
    mutationFn: () => {
      const targets = rows.map((row, index) => {
        const ratio = toRatioString(row.percent)
        const principal = toAmountString(principalValue(row))
        if (ratio === null || principal === null) {
          throw new Error(`子项 ${index + 1} 的比例或金额不合法`)
        }
        return { name: trimmedNames[index], ratio, principal, note: null }
      })
      return api.splitLineageLink({
        from_item_id: item.item_id,
        targets,
        effective_date: effectiveDate,
        reason: reason.trim(),
      }, newIdemKey())
    },
    onSuccess: (result) => onSubmitted(rows.length, result.archived_effective_from),
    onError: (mutationError) => {
      const rowErrors = extractTargetErrors(mutationError)
      // 后端 400 目前用 `details.field="targets"` + 文案点名具体子项（无序号）；
      // 文案里出现哪个子项名，就把错误挂到那一行，保证「逐行」可见。
      if (rowErrors.size === 0 && mutationError instanceof Error) {
        rows.forEach((row, index) => {
          const name = row.name.trim()
          if (name && mutationError.message.includes(name)) {
            rowErrors.set(index, [...(rowErrors.get(index) ?? []), mutationError.message])
          }
        })
      }
      setServerRowErrors(rowErrors)
      setError(rowErrors.size > 0
        ? '登记未通过：请修正标红的子项后重试。'
        : lineageErrorText(
          mutationError,
          '拆分登记失败，请重试',
          '拆分服务暂未就绪（404），请稍后再试；改名与合并不受影响。',
        ))
    },
  })

  const submit = () => {
    if (blocked || mutation.isPending) return
    setError(null)
    setServerRowErrors(new Map())
    mutation.mutate()
  }

  return (
    <div className="space-y-4">
      <p className="text-meta text-secondary">
        把一个旧项拆成 2 个以上子项。父项自生效日起归档，子项按比例承接金额；此前历史仍计入父项。
      </p>

      {archived && (
        <div className="rounded-control border border-status-warn/25 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text">
          该资产项已归档，不能再拆分为子项；血缘记录只读。
        </div>
      )}

      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 rounded-control border border-line bg-subtle/60 px-3 py-2">
        <span className="text-meta text-muted">父项当前值</span>
        <span className="pf-num text-body-strong text-ink">
          {parentAmount === null ? '未录入' : formatCurrency(parentAmount)}
        </span>
      </div>
      <p className="text-micro text-muted">
        承接金额默认按「父项当前值 × 比例」带入，可逐项改写；名称、比例、金额都由你确认后提交。
      </p>

      <section className="space-y-2" aria-label="子项">
        <div className="flex items-baseline justify-between gap-2">
          <h4 className="text-h3 font-semibold text-ink">子项</h4>
          <span className="text-meta text-muted">{rows.length} 项</span>
        </div>

        {rows.map((row, index) => {
          const issues = rowIssues[index]
          const rowServerErrors = serverRowErrors.get(index)
          return (
            <div key={row.key} className="rounded-control border border-line p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-meta font-medium text-secondary">子项 {index + 1}</span>
                <button
                  type="button"
                  onClick={() => removeRow(row.key)}
                  disabled={rows.length <= 2}
                  aria-label={`删除子项 ${index + 1}`}
                  title={rows.length <= 2 ? '至少保留两个子项' : '删除该子项'}
                  className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted transition-colors hover:bg-subtle hover:text-status-critical-text disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <Trash2 size={15} aria-hidden />
                </button>
              </div>

              <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-[minmax(0,1fr)_96px_150px]">
                <FormField
                  label="名称"
                  required
                  error={issues.name}
                  className="col-span-2 sm:col-span-1"
                >
                  <input
                    type="text"
                    value={row.name}
                    onChange={(event) => {
                      patchRow(row.key, { name: event.target.value })
                      setServerRowErrors(new Map())
                    }}
                    placeholder="子项名称"
                    autoComplete="off"
                    className={cn(fieldControlClass, 'min-w-0', issues.name && fieldErrorClass)}
                  />
                </FormField>

                <FormField label="比例（%）" required error={issues.percent}>
                  <div className="relative">
                    <input
                      type="text"
                      inputMode="decimal"
                      value={row.percent}
                      onChange={(event) => {
                        const next = cleanNumericInput(event.target.value).replace('%', '')
                        if (PERCENT_INPUT_PATTERN.test(next)) patchRow(row.key, { percent: next })
                        setServerRowErrors(new Map())
                      }}
                      placeholder="0.00"
                      aria-label={`子项 ${index + 1} 比例（%）`}
                      className={cn(fieldControlClass, 'pf-num min-w-0 pr-7 text-right', issues.percent && fieldErrorClass)}
                    />
                    <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-meta text-muted" aria-hidden>
                      %
                    </span>
                  </div>
                </FormField>

                <FormField label="承接金额（元）" required error={issues.principal}>
                  <div className="relative">
                    <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-meta text-muted" aria-hidden>
                      ¥
                    </span>
                    <input
                      type="text"
                      inputMode="decimal"
                      value={principalValue(row)}
                      onChange={(event) => {
                        const next = cleanNumericInput(event.target.value)
                        if (AMOUNT_INPUT_PATTERN.test(next)) {
                          patchRow(row.key, { principal: next, principalTouched: true })
                        }
                        setServerRowErrors(new Map())
                      }}
                      placeholder="0.00"
                      aria-label={`子项 ${index + 1} 承接金额（元）`}
                      className={cn(fieldControlClass, 'pf-num min-w-0 pl-6 text-right', issues.principal && fieldErrorClass)}
                    />
                  </div>
                </FormField>
              </div>

              {rowServerErrors && rowServerErrors.length > 0 && (
                <ul className="mt-2 space-y-0.5" role="alert">
                  {rowServerErrors.map((message, errorIndex) => (
                    <li key={`${row.key}-err-${errorIndex}`} className="text-meta text-status-critical-text">
                      {message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )
        })}

        <button
          type="button"
          onClick={addRow}
          className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-dashed border-line-strong px-3 text-meta font-medium text-secondary transition-colors hover:bg-subtle"
        >
          <Plus size={15} aria-hidden />
          添加子项
        </button>
      </section>

      <div
        className={cn(
          'space-y-1.5 rounded-control border px-3 py-2.5',
          ratioOk ? 'border-status-good/25 bg-subtle' : 'border-status-warn/30 bg-status-warn-surface',
        )}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <span className="text-meta text-secondary">比例合计</span>
          <span className={cn('pf-num text-h3 font-semibold', ratioOk ? 'text-status-good-text' : 'text-status-warn-text')}>
            {percentSumText}%
          </span>
        </div>
        <div className="h-1 overflow-hidden rounded-pill bg-line">
          <div
            className={cn('h-full rounded-pill transition-[width] duration-fast', ratioOk ? 'bg-status-good' : 'bg-status-warn')}
            style={{ width: `${meterWidth}%` }}
          />
        </div>
        <p className="text-micro text-muted">
          需等于 100%（允许 ±0.01% 误差）
          {!ratioOk && missingRatios === 0 && (
            <span className="ml-1 text-status-warn-text">
              · {ratioDelta.gt(0) ? '超出' : '还差'} {ratioDelta.abs().times(100).toFixed(2)}%
            </span>
          )}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
        <DateField
          label="生效日期"
          required
          value={effectiveDate}
          onChange={setEffectiveDate}
          hint="父项自该日起归档，此前历史仍计入父项"
        />
        <FormField
          label="登记理由"
          required
          hint="拆分是人确认的裁决，必须留下理由"
          className="tablet:col-span-2"
        >
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
            placeholder="如：按渠道拆出，原汇总项不再单独计量"
            className={cn(fieldControlClass, 'min-h-[76px] resize-none py-2')}
          />
        </FormField>
      </div>

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
          {mutation.isPending ? '登记中…' : '登记拆分'}
        </button>
      </div>
    </div>
  )
}