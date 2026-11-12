'use client'

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import Decimal from 'decimal.js'
import { Check, Info, Search } from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { formatCurrency, formatDate } from '@/lib/format'
import { deriveAttachLinkType, prefillSplitPercents } from '@/lib/lineage'
import { cn } from '@/lib/utils'
import { DateField, FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import { InlineError } from '@/components/ui'
import type {
  AssetItemInfo,
  CurrentValueItem,
  LineageAttachLinkType,
  LineageAttachResponse,
} from '@/types'
import {
  ATTACH_LINK_TYPE_LABEL,
  PERCENT_INPUT_PATTERN,
  RATIO_TOLERANCE,
  cleanNumericInput,
  lineageErrorText,
  toPercentDecimal,
  toRatioString,
} from './shared'

/**
 * 动作四 改关联（既有项 → 既有项）：`POST /api/lineage-links/attach`（spec §1.1）。
 *
 * 与合并 / 拆分不同：两侧都是已存在的资产项，不新建项、不写本金口径。
 * - 形状由数量推导：1:1 改名 / 更正、N:1 合并、1:N 拆分；N:M 一律拦住；
 * - 方向决定谁终止计量（本项是前身还是后继），默认本项终止；
 * - 生效日必须显式确认（预填最近快照日，**不用今天**）；
 * - 拆分比例按各目标项在生效日的金额占比预填，可改，合计须 100%；
 * - 理由必填（R-9）。
 */
export type AttachDirection = 'out' | 'in'

export interface AttachSubmitMeta {
  linkType: LineageAttachLinkType
  targetCount: number
  direction: AttachDirection
}

export interface AttachPanelProps {
  item: CurrentValueItem
  /** 全部资产项（含已归档）；候选只取「在用且非本项」。 */
  items: readonly AssetItemInfo[]
  /** 资产项列表是否仍在读取（空列表时给出状态文案）。 */
  itemsLoading?: boolean
  /** 当前是否停在「改关联」页：未打开时不去拉生效日读数。 */
  active?: boolean
  /** 当前值视图的行：生效日读模型未就绪时的金额兜底。 */
  candidates: readonly CurrentValueItem[]
  /** 生效日默认值＝最近快照日（不是今天）。 */
  defaultEffectiveDate: string
  onSubmitted: (result: LineageAttachResponse, meta: AttachSubmitMeta) => void
}

type FieldKey = 'selection' | 'ratios' | 'date' | 'reason'

interface CandidateRow {
  id: number
  name: string
  amount: string | null
  assetDate: string | null
}

const FIELD_LABELS: Record<FieldKey, string> = {
  selection: '目标项',
  ratios: '比例',
  date: '生效日期',
  reason: '登记理由',
}

function namesSummary(names: string[]): string {
  if (names.length === 0) return '所选资产项'
  if (names.length === 1) return `「${names[0]}」`
  if (names.length === 2) return `「${names[0]}」「${names[1]}」`
  return `「${names[0]}」等 ${names.length} 项`
}

function DirectionOption({
  value,
  current,
  disabled,
  title,
  hint,
  onSelect,
}: {
  value: AttachDirection
  current: AttachDirection
  disabled?: boolean
  title: string
  hint: string
  onSelect: (value: AttachDirection) => void
}) {
  const checked = current === value
  return (
    <label
      className={cn(
        'flex items-start gap-2.5 rounded-control border px-3 py-2.5 transition-colors',
        checked ? 'border-brand-600 bg-brand-50/60' : 'border-line bg-surface hover:bg-subtle',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      )}
    >
      <input
        type="radio"
        name="attach-direction"
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onSelect(value)}
        className="mt-1 h-4 w-4 shrink-0 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed"
      />
      <span className="min-w-0 space-y-0.5">
        <span className="block text-body font-medium text-ink">{title}</span>
        <span className="block text-micro leading-4 text-muted">{hint}</span>
      </span>
    </label>
  )
}

export function AttachPanel({
  item,
  items,
  itemsLoading = false,
  active = true,
  candidates,
  defaultEffectiveDate,
  onSubmitted,
}: AttachPanelProps) {
  const selfArchived = item.item_status === 'archived'

  const [direction, setDirection] = useState<AttachDirection>(selfArchived ? 'in' : 'out')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [search, setSearch] = useState('')
  const [effectiveDate, setEffectiveDate] = useState(defaultEffectiveDate)
  const [reason, setReason] = useState('')
  const [ratioEdits, setRatioEdits] = useState<Record<number, string>>({})
  const [ratioEdited, setRatioEdited] = useState<Set<number>>(() => new Set())
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<FieldKey, string>>>({})

  // 生效日各项目金额：/assets/current 支持 as-of，正好给出「生效日的最新记录」。
  const asOfQuery = useQuery({
    queryKey: ['currentValues', 'lineageAttach', effectiveDate],
    queryFn: () => api.getCurrentValues({ as_of: effectiveDate, include_archived: true }),
    enabled: active && Boolean(effectiveDate),
    staleTime: 30_000,
    retry: false,
  })

  const asOfMap = useMemo(
    () => new Map((asOfQuery.data?.items ?? []).map((row) => [row.item_id, row])),
    [asOfQuery.data],
  )
  const fallbackMap = useMemo(
    () => new Map(candidates.map((row) => [row.item_id, row])),
    [candidates],
  )

  const rows = useMemo<CandidateRow[]>(() => {
    return items
      .filter((candidate) => candidate.id !== item.item_id && candidate.status === 'active')
      .map((candidate) => {
        const value = asOfMap.get(candidate.id) ?? fallbackMap.get(candidate.id)
        return {
          id: candidate.id,
          name: candidate.display_name,
          amount: value?.amount ?? null,
          assetDate: value?.asset_date ?? null,
        }
      })
      .sort((left, right) => left.name.localeCompare(right.name, 'zh-Hans-CN'))
  }, [items, item.item_id, asOfMap, fallbackMap])

  const filteredRows = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return rows
    return rows.filter((row) => row.name.toLowerCase().includes(keyword))
  }, [rows, search])

  const rowById = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows])

  // 候选被归档 / 移出列表后，选中集随之收敛（不把不可见项偷偷提交）
  useEffect(() => {
    const available = new Set(rows.map((row) => row.id))
    setSelectedIds((previous) => {
      const next = previous.filter((id) => available.has(id))
      return next.length === previous.length ? previous : next
    })
  }, [rows])

  const selectedRows = useMemo(
    () => selectedIds.map((id) => rowById.get(id)).filter((row): row is CandidateRow => Boolean(row)),
    [selectedIds, rowById],
  )
  const selectedTotal = useMemo(
    () => selectedRows.reduce((sum, row) => {
      try {
        return sum.plus(new Decimal(row.amount ?? 0))
      } catch {
        return sum
      }
    }, new Decimal(0)),
    [selectedRows],
  )

  const fromIds = direction === 'out' ? [item.item_id] : selectedIds
  const toIds = direction === 'out' ? selectedIds : [item.item_id]
  const linkType = selectedIds.length === 0 ? null : deriveAttachLinkType(fromIds.length, toIds.length)

  // 比例预填只在「未手改」的行生效；改过哪一行，哪一行就保留人的输入。
  const prefillPercents = useMemo(() => {
    const percents = prefillSplitPercents(selectedIds.map((id) => (
      (asOfMap.get(id) ?? fallbackMap.get(id))?.amount ?? null
    )))
    return new Map(selectedIds.map((id, index) => [id, percents[index] ?? '']))
  }, [selectedIds, asOfMap, fallbackMap])
  const percentFor = (itemId: number): string => (
    ratioEdited.has(itemId) ? ratioEdits[itemId] ?? '' : prefillPercents.get(itemId) ?? ''
  )

  const ratioSum = selectedIds.reduce(
    (sum, id) => sum.plus(toPercentDecimal(percentFor(id))),
    new Decimal(0),
  )
  const ratioDelta = ratioSum.minus(100)
  const ratioSumText = ratioSum.toFixed(2)
  const ratioOk = ratioDelta.abs().lte(RATIO_TOLERANCE.times(100))
  const missingRatios = linkType === 'split'
    ? selectedIds.filter((id) => toRatioString(percentFor(id)) === null).length
    : 0
  const zeroTargetCount = linkType === 'split'
    ? selectedRows.filter((row) => row.amount === null || Number(row.amount) <= 0).length
    : 0

  const selectedNames = selectedRows.map((row) => row.name)
  const oldParty = direction === 'out'
    ? `本项「${item.display_name}」`
    : namesSummary(selectedNames)
  const newParty = direction === 'out' ? namesSummary(selectedNames) : `本项「${item.display_name}」`

  const selectionError = fieldErrors.selection
  const dateError = fieldErrors.date
  const reasonError = fieldErrors.reason
  const ratiosError = fieldErrors.ratios

  const blockers: string[] = []
  if (selectionError) blockers.push(selectionError)
  if (selectedIds.length === 0) blockers.push('至少选择一个已有资产项')
  else if (!linkType) blockers.push('不能登记多对多（N→M）：请先合并再拆分，分两次登记')
  if (linkType === 'split') {
    if (missingRatios > 0) blockers.push(`${missingRatios} 个目标项比例未填或不合法`)
    else if (!ratioOk) blockers.push(`比例合计需等于 100%（当前 ${ratioSumText}%）`)
  }
  if (!effectiveDate) blockers.push('选择生效日期')
  if (!reason.trim()) blockers.push('填写登记理由')
  if (selfArchived && direction === 'out') blockers.push('本项已归档，不能作为前身')

  const blocked = blockers.length > 0

  const toggle = (itemId: number) => {
    setSelectedIds((previous) => (
      previous.includes(itemId)
        ? previous.filter((id) => id !== itemId)
        : [...previous, itemId]
    ))
    setFieldErrors((previous) => ({ ...previous, selection: undefined, ratios: undefined }))
  }

  const editPercent = (itemId: number, raw: string) => {
    const next = cleanNumericInput(raw).replace('%', '')
    if (!PERCENT_INPUT_PATTERN.test(next)) return
    setRatioEdits((previous) => ({ ...previous, [itemId]: next }))
    setRatioEdited((previous) => {
      if (previous.has(itemId)) return previous
      const updated = new Set(previous)
      updated.add(itemId)
      return updated
    })
    setFieldErrors((previous) => ({ ...previous, ratios: undefined }))
  }

  const mutation = useMutation({
    mutationFn: () => {
      if (!linkType) throw new Error('关联形状无效')
      const ratios = linkType === 'split'
        ? selectedIds.map((id) => toRatioString(percentFor(id)))
        : null
      if (ratios && ratios.some((ratio) => ratio === null)) {
        throw new Error('拆分比例未填或不合法')
      }
      return api.attachLineageLinks({
        from_item_ids: fromIds,
        to_item_ids: toIds,
        link_type: linkType,
        ...(ratios ? { ratios: ratios as string[] } : {}),
        effective_date: effectiveDate,
        reason: reason.trim(),
      }, newIdemKey())
    },
    onSuccess: (result) => {
      if (!linkType) return
      onSubmitted(result, {
        linkType,
        targetCount: selectedIds.length,
        direction,
      })
    },
    onError: (mutationError) => {
      const text = lineageErrorText(
        mutationError,
        '关联登记失败，请重试',
        '关联登记服务暂未就绪（404），请稍后再试；改名、合并、拆分不受影响。',
      )
      const rawField = mutationError instanceof ApiError && typeof mutationError.details?.field === 'string'
        ? mutationError.details.field
        : ''
      const field = (Object.keys(FIELD_LABELS) as FieldKey[])
        .find((key) => rawField.startsWith(key))
      if (rawField.startsWith('to_item_ids') || rawField.startsWith('from_item_ids') || rawField === 'link_type') {
        setFieldErrors({ selection: text })
      } else if (rawField.startsWith('ratios')) {
        setFieldErrors({ ratios: text })
      } else if (rawField.startsWith('effective_date')) {
        setFieldErrors({ date: text })
      } else if (rawField.startsWith('reason')) {
        setFieldErrors({ reason: text })
      } else if (field) {
        setFieldErrors({ [field]: text })
      } else {
        setError(text)
      }
    },
  })

  const submit = () => {
    if (blocked || mutation.isPending) return
    setError(null)
    setFieldErrors({})
    mutation.mutate()
  }

  const activeRows = rows.length
  const emptyListText = itemsLoading
    ? '正在读取资产项…'
    : activeRows === 0
      ? '当前没有可关联的在用资产项（本项除外）。'
      : '没有匹配的资产项。'

  return (
    <div className="space-y-4">
      <p className="text-meta text-secondary">
        关联到已有资产项：把本项与已有资产项登记为前后关系。两侧都是现有资产项，不会新建项；只登记「从某日起归属」。
      </p>

      {selfArchived && (
        <div className="rounded-control border border-status-warn/25 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text">
          本项已归档，不能再作为前身终止计量；只能登记「所选项终止，本项继续」。
        </div>
      )}

      <fieldset className="space-y-2">
        <legend className="text-meta font-medium text-secondary">本项在这条关系里的位置</legend>
        <div className="grid grid-cols-1 gap-2 tablet:grid-cols-2">
          <DirectionOption
            value="out"
            current={direction}
            disabled={selfArchived}
            onSelect={setDirection}
            title="本项终止，所选项继续"
            hint="本项是前身：这项后来改由已有项承接（拆分或改名）"
          />
          <DirectionOption
            value="in"
            current={direction}
            onSelect={setDirection}
            title="所选项终止，本项继续"
            hint="本项是后继：已有项后来并入本项（合并或改名）"
          />
        </div>
      </fieldset>

      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 rounded-control border border-line bg-subtle/60 px-3 py-2">
        <span className="text-meta text-muted">将登记为</span>
        <span className="text-body-strong font-medium text-ink">
          {linkType
            ? `${ATTACH_LINK_TYPE_LABEL[linkType]}（${fromIds.length} → ${toIds.length}）`
            : selectedIds.length === 0
              ? '先选择目标项'
              : '多对多不登记'}
        </span>
      </div>

      <section className="space-y-2" aria-label="选择已有资产项">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h4 className="text-h3 font-semibold text-ink">
            {direction === 'out' ? '选择承接项' : '选择并入的前身'}
          </h4>
          <span className="pf-num text-meta text-secondary">
            已选 {selectedIds.length} 项 · 生效日合计 {formatCurrency(selectedTotal.toFixed(2))}
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
            placeholder="搜索资产项"
            aria-label="搜索资产项"
            className={cn(fieldControlClass, 'pl-9')}
          />
        </div>

        <div className="max-h-[260px] divide-y divide-line overflow-y-auto rounded-control border border-line">
          {filteredRows.length === 0 ? (
            <p className="px-3 py-4 text-meta text-muted">{emptyListText}</p>
          ) : (
            filteredRows.map((row) => {
              const checked = selectedIds.includes(row.id)
              return (
                <label
                  key={row.id}
                  className={cn(
                    'flex min-h-[44px] cursor-pointer items-center gap-3 px-3 py-2 transition-colors',
                    checked ? 'bg-brand-50/60' : 'hover:bg-subtle',
                  )}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(row.id)}
                    className="h-4 w-4 shrink-0 accent-[var(--pf-color-action-primary)]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body text-ink" title={row.name}>
                      {row.name}
                    </span>
                    <span className="pf-num block text-micro text-muted">
                      {row.assetDate ? `记录于 ${formatDate(row.assetDate)}` : '无记录'}
                    </span>
                  </span>
                  <span className={cn('pf-num shrink-0 text-right text-meta', checked ? 'text-ink' : 'text-secondary')}>
                    {row.amount === null ? '未录入' : formatCurrency(row.amount)}
                  </span>
                  {checked && <Check size={14} className="shrink-0 text-brand-600" aria-hidden />}
                </label>
              )
            })
          )}
        </div>
        <p className="text-micro text-muted">
          金额取各项目在生效日{effectiveDate ? `（${formatDate(effectiveDate)}）` : ''}的最新记录
          {asOfQuery.isFetching ? ' · 读数更新中…' : ''}
        </p>
        {selectionError && (
          <p role="alert" className="text-meta text-status-critical-text">{selectionError}</p>
        )}
      </section>

      {linkType === 'split' && (
        <section className="space-y-2" aria-label="拆分比例">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h4 className="text-h3 font-semibold text-ink">拆分比例</h4>
            <span className={cn('pf-num text-meta', ratioOk ? 'text-status-good-text' : 'text-status-warn-text')}>
              合计 {ratioSumText}%
            </span>
          </div>
          <p className="text-micro text-muted">
            已按各目标项在生效日的金额占比预填，可逐项修改；合计须为 100%（允许 ±0.01%）。
            {zeroTargetCount > 0 && (
              <span className="text-status-warn-text">
                {' '}有 {zeroTargetCount} 项在生效日无金额，比例已按等分预填，请按实际调整。
              </span>
            )}
          </p>

          <div className="space-y-1.5">
            {selectedRows.map((row) => (
              <div
                key={row.id}
                className="grid grid-cols-[minmax(0,1fr)_92px] items-center gap-x-2 gap-y-1 rounded-control border border-line px-2.5 py-2 tablet:grid-cols-[minmax(0,1fr)_112px_150px]"
              >
                <span className="min-w-0 truncate text-meta text-ink" title={row.name}>{row.name}</span>
                <div className="relative">
                  <input
                    type="text"
                    inputMode="decimal"
                    value={percentFor(row.id)}
                    onChange={(event) => editPercent(row.id, event.target.value)}
                    placeholder="0.00"
                    aria-label={`「${row.name}」比例（%）`}
                    className={cn(fieldControlClass, 'pf-num pr-7 text-right text-meta')}
                  />
                  <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-meta text-muted" aria-hidden>
                    %
                  </span>
                </div>
                <span className="pf-num col-span-2 text-micro text-muted tablet:col-span-1 tablet:text-right">
                  {row.amount === null ? '生效日无记录' : `生效日值 ${formatCurrency(row.amount)}`}
                </span>
              </div>
            ))}
          </div>

          <div className="h-1 overflow-hidden rounded-pill bg-line">
            <div
              className={cn('h-full rounded-pill transition-[width] duration-fast', ratioOk ? 'bg-status-good' : 'bg-status-warn')}
              style={{ width: `${Math.min(100, Math.max(0, ratioSum.toNumber()))}%` }}
            />
          </div>
          {!ratioOk && missingRatios === 0 && (
            <p className="text-micro text-status-warn-text">
              {ratioDelta.gt(0) ? '超出' : '还差'} {ratioDelta.abs().toFixed(2)}%
            </p>
          )}
          {ratiosError && (
            <p role="alert" className="text-meta text-status-critical-text">{ratiosError}</p>
          )}
        </section>
      )}

      <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
        <DateField
          label="生效日期"
          required
          value={effectiveDate}
          onChange={(value) => {
            setEffectiveDate(value)
            setFieldErrors((previous) => ({ ...previous, date: undefined }))
          }}
          error={dateError}
          hint="默认最近快照日，请按实际切换日确认"
        />
        <FormField
          label="登记理由"
          required
          hint="补登记是人确认的裁决，必须留下理由"
          error={reasonError}
          className="tablet:col-span-2"
        >
          <textarea
            value={reason}
            onChange={(event) => {
              setReason(event.target.value)
              setFieldErrors((previous) => ({ ...previous, reason: undefined }))
            }}
            rows={2}
            placeholder="如：09-16 起改由两个已有项分别记录，补登记归属"
            className={cn(fieldControlClass, 'min-h-[76px] resize-none py-2', reasonError && fieldErrorClass)}
          />
        </FormField>
      </div>

      <div className="flex gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
        <Info size={15} className="mt-0.5 shrink-0" aria-hidden />
        <div className="space-y-1">
          <p>{`本次：${oldParty} 自 ${effectiveDate ? formatDate(effectiveDate) : '生效日'} 起终止计量，${newParty} 自该日起继续。`}</p>
          <p>
            {direction === 'out'
              ? '登记后仅改归属口径：该日起旧项终止计量、目标项继续；'
              : '登记后仅改归属口径：该日起所选项终止计量、本项继续；'}
            <strong className="font-semibold">历史记录不迁移</strong>
            ，读数按身份族合并
          </p>
        </div>
      </div>

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
          {mutation.isPending ? '登记中…' : '登记关联'}
        </button>
      </div>
    </div>
  )
}