'use client'

import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Loader2, Tags as TagsIcon } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { cn } from '@/lib/utils'
import { InlineError, ResponsiveDialog } from '@/components/ui'
import { AssetClassPicker } from './AssetClassPicker'
import { OptionPicker } from './OptionPicker'
import { TagPicker } from './TagPicker'
import {
  DIM_LABEL,
  INSTITUTION_KIND_LABEL,
  MAX_TAGS,
  MEMBER_ROLE_LABEL,
  RISK_LEVEL_OPTIONS,
  classificationErrorText,
  dimsText,
  invalidateClassificationViews,
  memberLabel,
} from './classification'
import type { ClassificationSnapshot, RequiredDim } from './classification'
import {
  EMPTY_CHOICES,
  buildPayload,
  buildTags,
  hasChange,
  missingAfter,
} from './batchChoices'
import type { BatchChoices, ScalarMode, TagMode } from './batchChoices'
import { useClassificationDictionaries } from './useClassificationDictionaries'


/**
 * 批量补分类：一次给一批资产项写入**同一条**分类值。
 *
 * 部分更新语义（关键设计）：
 * - 每个维度默认「保持不变」——只有显式改成具体值的维度才会进请求体，
 *   其余维度在每一项上保留它自己的原值；
 * - 可选维度多一档「清空」（显式传 null，与「不传」区分开）；
 * - 标签走整组替换接口，因此批量只提供「添加 / 移除」两种增量语义；
 *   追加后超过 5 个的项按失败如实报出，不静默截断。
 *
 * 请求逐项 PUT（每项一个幂等键），而不是一个批量端点：这样才能把
 * 「第 37 项因为类别还有子类被拒」这类原因落到具体资产名上。
 */

interface BatchFailure {
  itemId: number
  name: string
  message: string
}

function ModeSwitch({
  value,
  options,
  onChange,
  label,
}: {
  value: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string) => void
  label: string
}) {
  return (
    <div role="group" aria-label={label} className="flex shrink-0 rounded-control border border-line p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={cn(
            'min-h-[32px] rounded-[6px] px-2.5 text-micro font-medium transition-colors duration-fast',
            value === option.value ? 'bg-brand-600 text-white' : 'text-secondary hover:bg-subtle',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

const KEEP_SET = [{ value: 'keep', label: '保持不变' }, { value: 'set', label: '统一设为' }]
const KEEP_SET_CLEAR = [
  { value: 'keep', label: '保持不变' },
  { value: 'set', label: '统一设为' },
  { value: 'clear', label: '清空' },
]

function DimRow({
  title,
  required,
  mode,
  modes,
  onModeChange,
  children,
}: {
  title: string
  required?: boolean
  mode: string
  modes: Array<{ value: string; label: string }>
  onModeChange: (value: string) => void
  children?: React.ReactNode
}) {
  return (
    <div className="rounded-control border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-body font-medium text-ink">
          {title}
          {required && <span className="ml-0.5 text-status-critical-text" aria-hidden="true">*</span>}
        </span>
        <ModeSwitch label={`${title}处理方式`} value={mode} options={modes} onChange={onModeChange} />
      </div>
      {children && <div className="mt-3">{children}</div>}
    </div>
  )
}

export interface BatchClassificationDialogProps {
  open: boolean
  /** 待处理的资产项（打开时按此列表预选全部）。 */
  items: readonly ClassificationSnapshot[]
  onClose: () => void
  /** 有写入成功后回调（用于刷新列表）。 */
  onDone?: () => void
}

export function BatchClassificationDialog({
  open,
  items,
  onClose,
  onDone,
}: BatchClassificationDialogProps) {
  const queryClient = useQueryClient()
  const dict = useClassificationDictionaries(open)

  const [selected, setSelected] = useState<Set<number>>(() => new Set(items.map((item) => item.itemId)))
  const [choices, setChoices] = useState<BatchChoices>(EMPTY_CHOICES)
  const [phase, setPhase] = useState<'choose' | 'confirm' | 'running' | 'result'>('choose')
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [failures, setFailures] = useState<BatchFailure[]>([])
  const [succeeded, setSucceeded] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const itemById = useMemo(() => new Map(items.map((item) => [item.itemId, item])), [items])

  // 每次打开都回到「全选 + 只改想改的那一维」
  useEffect(() => {
    if (!open) return
    setSelected(new Set(items.map((item) => item.itemId)))
    setChoices(EMPTY_CHOICES)
    setPhase('choose')
    setProgress({ done: 0, total: 0 })
    setFailures([])
    setSucceeded(0)
    setError(null)
  }, [open, items])

  const classes = useMemo(() => dict.classesQuery.data?.items ?? [], [dict.classesQuery.data])
  const levels = useMemo(() => dict.levelsQuery.data?.items ?? [], [dict.levelsQuery.data])
  const institutions = useMemo(() => dict.institutionsQuery.data?.items ?? [], [dict.institutionsQuery.data])
  const channels = useMemo(() => dict.channelsQuery.data?.items ?? [], [dict.channelsQuery.data])
  const members = useMemo(() => dict.membersQuery.data?.items ?? [], [dict.membersQuery.data])
  const tags = useMemo(() => dict.tagsQuery.data?.items ?? [], [dict.tagsQuery.data])

  const classById = useMemo(() => new Map(classes.map((node) => [node.id, node])), [classes])

  const levelOptions = useMemo(
    () => levels.map((level) => ({
      value: String(level.days),
      label: level.label,
      hint: level.days >= 9999 ? '封闭期 / 一年以上' : `约 ${level.days} 天内可用`,
    })),
    [levels],
  )
  const institutionOptions = useMemo(
    () => institutions.filter((entry) => entry.active).map((entry) => ({
      value: String(entry.id),
      label: entry.name,
      badge: INSTITUTION_KIND_LABEL[entry.kind] ?? entry.kind,
    })),
    [institutions],
  )
  const channelOptions = useMemo(
    () => channels.filter((entry) => entry.active).map((entry) => ({ value: String(entry.id), label: entry.name })),
    [channels],
  )
  const memberOptions = useMemo(
    () => members.filter((entry) => entry.active).map((entry) => ({
      value: String(entry.id),
      label: entry.name,
      badge: MEMBER_ROLE_LABEL[entry.role] ?? entry.role,
      hint: memberLabel(entry),
    })),
    [members],
  )

  const update = (patch: Partial<BatchChoices>) => setChoices((previous) => ({ ...previous, ...patch }))

  const selectedItems = useMemo(
    () => items.filter((item) => selected.has(item.itemId)),
    [items, selected],
  )

  const plannedDims = useMemo(() => {
    const labels: string[] = []
    if (choices.assetClass.mode === 'set' && choices.assetClass.value != null) {
      const node = classById.get(choices.assetClass.value)
      labels.push(`${DIM_LABEL.asset_class} → ${node ? node.path : '所选类别'}`)
    }
    if (choices.liquidity.mode === 'set' && choices.liquidity.value) {
      const level = levels.find((entry) => String(entry.days) === choices.liquidity.value)
      labels.push(`${DIM_LABEL.liquidity} → ${level?.label ?? `${choices.liquidity.value} 天`}`)
    }
    if (choices.institution.mode === 'set' && choices.institution.value) {
      const entry = institutions.find((row) => String(row.id) === choices.institution.value)
      labels.push(`${DIM_LABEL.institution} → ${entry?.name ?? '所选机构'}`)
    }
    if (choices.owner.mode === 'set' && choices.owner.value) {
      const entry = members.find((row) => String(row.id) === choices.owner.value)
      labels.push(`${DIM_LABEL.owner} → ${entry ? memberLabel(entry) : '所选成员'}`)
    }
    if (choices.channel.mode === 'clear') {
      labels.push('渠道 → 清空')
    } else if (choices.channel.mode === 'set' && choices.channel.value) {
      const entry = channels.find((row) => String(row.id) === choices.channel.value)
      labels.push(`渠道 → ${entry?.name ?? '所选渠道'}`)
    }
    if (choices.risk.mode === 'clear') {
      labels.push('风险等级 → 清空')
    } else if (choices.risk.mode === 'set' && choices.risk.value) {
      const option = RISK_LEVEL_OPTIONS.find((entry) => entry.value === choices.risk.value)
      labels.push(`风险等级 → ${option?.label ?? choices.risk.value}`)
    }
    if (choices.tagMode !== 'keep' && choices.tagIds.length > 0) {
      const names = choices.tagIds.map((id) => tags.find((tag) => tag.id === id)?.name ?? '所选标签')
      labels.push(`标签 → ${choices.tagMode === 'add' ? '添加' : '移除'} ${names.join('、')}`)
    }
    return labels
  }, [choices, classById, channels, institutions, levels, members, tags])

  /** 补齐后仍不生效的项数（按维度聚合）。 */
  const stillIncomplete = useMemo(() => {
    const counters = new Map<RequiredDim, number>()
    for (const item of selectedItems) {
      for (const dim of missingAfter(item, buildPayload(item, choices))) {
        counters.set(dim, (counters.get(dim) ?? 0) + 1)
      }
    }
    return counters
  }, [choices, selectedItems])

  const toggleItem = (itemId: number) => {
    setSelected((previous) => {
      const next = new Set(previous)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((previous) => (previous.size === items.length
      ? new Set<number>()
      : new Set(items.map((item) => item.itemId))))
  }

  const run = async (targets: readonly ClassificationSnapshot[]) => {
    setPhase('running')
    setProgress({ done: 0, total: targets.length })
    setFailures([])
    setError(null)
    const nextFailures: BatchFailure[] = []
    let ok = 0

    for (const snapshot of targets) {
      try {
        const payload = buildPayload(snapshot, choices)
        const tagPlan = buildTags(snapshot, choices)
        if (tagPlan.overflow) {
          nextFailures.push({
            itemId: snapshot.itemId,
            name: snapshot.displayName,
            message: `加上后标签会超过 ${MAX_TAGS} 个，这项没有写入。请减少要添加的标签。`,
          })
        } else {
          const dimsChanged = Object.keys(payload).some((key) => key !== 'reason')
          if (dimsChanged) await api.setItemClassification(snapshot.itemId, payload, newIdemKey())
          if (tagPlan.next !== null) await api.setItemTags(snapshot.itemId, tagPlan.next, newIdemKey())
          ok += 1
        }
      } catch (mutationError) {
        nextFailures.push({
          itemId: snapshot.itemId,
          name: snapshot.displayName,
          message: classificationErrorText(mutationError),
        })
      } finally {
        setProgress((previous) => ({ ...previous, done: previous.done + 1 }))
      }
    }

    setSucceeded(ok)
    setFailures(nextFailures)
    setPhase('result')
    invalidateClassificationViews(queryClient)
    if (ok > 0) onDone?.()
  }

  const startConfirm = () => {
    if (selectedItems.length === 0) {
      setError('请先勾选要处理的资产项。')
      return
    }
    if (!hasChange(choices)) {
      setError('还没有选任何要修改的维度。至少把一项从「保持不变」改成具体值。')
      return
    }
    setError(null)
    setPhase('confirm')
  }

  const footer = (() => {
    if (phase === 'result') {
      const failedItems = failures
        .map((failure) => itemById.get(failure.itemId))
        .filter((item): item is ClassificationSnapshot => Boolean(item))
      return (
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink"
          >
            完成
          </button>
          {failedItems.length > 0 && (
            <button
              type="button"
              onClick={() => { void run(failedItems) }}
              className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white"
            >
              重试失败项（{failedItems.length}）
            </button>
          )}
        </div>
      )
    }
    if (phase === 'running') {
      return (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="inline-flex items-center gap-2 text-meta text-secondary">
            <Loader2 size={14} className="animate-spin" aria-hidden />
            正在保存 <span className="pf-num">{progress.done}</span> / {progress.total}
          </span>
          <span className="text-meta text-muted">中途关闭不会回滚已写入的项。</span>
        </div>
      )
    }
    if (phase === 'confirm') {
      return (
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-between">
          <button
            type="button"
            onClick={() => setPhase('choose')}
            className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink"
          >
            返回修改
          </button>
          <button
            type="button"
            onClick={() => { void run(selectedItems) }}
            className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white"
          >
            确认修改 {selectedItems.length} 项
          </button>
        </div>
      )
    }
    return (
      <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
        <button
          type="button"
          onClick={onClose}
          className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink"
        >
          取消
        </button>
        <button
          type="button"
          onClick={startConfirm}
          disabled={selectedItems.length === 0}
          className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
        >
          下一步（{selectedItems.length} 项）
        </button>
      </div>
    )
  })()

  return (
    <ResponsiveDialog
      open={open}
      onClose={phase === 'running' ? () => {} : onClose}
      title="批量补分类"
      description="分类写在资产项上：一次设置，这项的全部历史记录共用。"
      size="lg"
      footer={footer}
    >
      {phase === 'running' && (
        <div className="space-y-4">
          <div
            className="h-2 w-full overflow-hidden rounded-pill bg-subtle"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.done}
            aria-label="批量写入进度"
          >
            <div
              className="h-full rounded-pill bg-action-primary transition-[width] duration-200"
              style={{ width: `${progress.total === 0 ? 0 : (progress.done / progress.total) * 100}%` }}
            />
          </div>
          <p className="text-meta text-secondary">
            正在写入第 {Math.min(progress.done + 1, progress.total)} 项（共 {progress.total} 项）。请稍候，不要刷新页面。
          </p>
        </div>
      )}

      {phase === 'result' && (
        <div className="space-y-4">
          <div className="rounded-control border border-line bg-subtle p-3">
            <p className="text-body-strong text-ink">
              处理完成：成功 <span className="pf-num text-status-good-text">{succeeded}</span> 项
              {failures.length > 0 && <> · 失败 <span className="pf-num text-status-critical-text">{failures.length}</span> 项</>}
            </p>
            <p className="mt-1 text-meta text-secondary">列表、当前值与统计已按最新分类刷新。</p>
          </div>
          {failures.length > 0 ? (
            <div className="space-y-1">
              <p className="text-meta font-medium text-secondary">没写成功的项</p>
              <ul className="max-h-64 divide-y divide-line overflow-y-auto rounded-control border border-line">
                {failures.map((failure) => (
                  <li key={failure.itemId} className="px-3 py-2">
                    <p className="text-body text-ink">{failure.name}</p>
                    <p className="mt-0.5 text-meta text-status-critical-text">{failure.message}</p>
                  </li>
                ))}
              </ul>
              <p className="text-meta text-muted">这些项没有改动；点「重试失败项」可只重跑它们。</p>
            </div>
          ) : (
            <p className="inline-flex items-center gap-1.5 text-meta text-status-good-text">
              <Check size={14} aria-hidden />
              全部写入成功。
            </p>
          )}
        </div>
      )}

      {phase === 'confirm' && (
        <div className="space-y-4">
          <p className="text-body text-secondary">
            将对 <strong className="pf-num text-ink">{selectedItems.length}</strong> 个资产项写入以下分类：
          </p>
          <ul className="space-y-1 rounded-control border border-line bg-subtle p-3 text-body text-ink">
            {plannedDims.map((label) => <li key={label}>{label}</li>)}
          </ul>
          <p className="text-meta text-muted">没列出的维度保持各项原值不变。</p>
          {stillIncomplete.size > 0 && (
            <div className="flex gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden />
              <ul className="space-y-0.5">
                {Array.from(stillIncomplete.entries()).map(([dim, count]) => (
                  <li key={dim}>
                    有 <span className="pf-num">{count}</span> 项补完后仍缺{DIM_LABEL[dim]}，它们会继续留在「待补分类」。
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-meta text-muted">
            写入后可按单项再改；每次改动都会留下一条变更记录。
          </p>
        </div>
      )}

      {phase === 'choose' && (
        <div className="space-y-4">
          {error && <InlineError>{error}</InlineError>}

          <section>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-h3 font-semibold text-ink">
                要处理的资产项
                <span className="pf-num ml-1.5 text-meta font-normal text-muted">已选 {selectedItems.length} / {items.length}</span>
              </h3>
              <button
                type="button"
                onClick={toggleAll}
                className="min-h-[36px] rounded-control border border-line px-2.5 text-meta text-secondary"
              >
                {selected.size === items.length ? '全不选' : '全选'}
              </button>
            </div>
            <ul className="mt-2 max-h-52 divide-y divide-line overflow-y-auto rounded-control border border-line">
              {items.map((item) => (
                <li key={item.itemId}>
                  <label className="flex min-h-[44px] cursor-pointer items-center gap-2 px-3 py-1.5">
                    <input
                      type="checkbox"
                      checked={selected.has(item.itemId)}
                      onChange={() => toggleItem(item.itemId)}
                      className="h-4 w-4 shrink-0 accent-[var(--pf-color-action-primary)]"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-body text-ink" title={item.displayName}>{item.displayName}</span>
                      <span className="block truncate text-micro text-muted">
                        {item.missing.length > 0 ? `还差${dimsText(item.missing)}` : '分类已齐'}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </section>

          <section className="space-y-2">
            <h3 className="text-h3 font-semibold text-ink">只改需要的维度</h3>
            <p className="text-meta text-muted">
              默认「保持不变」：没选的维度在各资产项上保留原值，不会被清空。
            </p>

            <DimRow
              title={DIM_LABEL.asset_class}
              required
              mode={choices.assetClass.mode}
              modes={KEEP_SET}
              onModeChange={(mode) => update({ assetClass: { mode: mode as ScalarMode, value: choices.assetClass.value } })}
            >
              {choices.assetClass.mode === 'set' && (
                <AssetClassPicker
                  classes={classes}
                  value={choices.assetClass.value}
                  onChange={(next) => update({ assetClass: { mode: 'set', value: next } })}
                  label="统一设为"
                  placeholder="选择末级类别"
                  hint="只能选最末一级；方向不一致的项会在结果里单独报出"
                />
              )}
            </DimRow>

            <DimRow
              title={DIM_LABEL.liquidity}
              required
              mode={choices.liquidity.mode}
              modes={KEEP_SET}
              onModeChange={(mode) => update({ liquidity: { mode: mode as ScalarMode, value: choices.liquidity.value } })}
            >
              {choices.liquidity.mode === 'set' && (
                <OptionPicker
                  label="统一设为"
                  options={levelOptions}
                  value={choices.liquidity.value}
                  onChange={(next) => update({ liquidity: { mode: 'set', value: next } })}
                  placeholder="选择流动性档位"
                />
              )}
            </DimRow>

            <DimRow
              title={DIM_LABEL.institution}
              required
              mode={choices.institution.mode}
              modes={KEEP_SET}
              onModeChange={(mode) => update({ institution: { mode: mode as ScalarMode, value: choices.institution.value } })}
            >
              {choices.institution.mode === 'set' && (
                <OptionPicker
                  label="统一设为"
                  options={institutionOptions}
                  value={choices.institution.value}
                  onChange={(next) => update({ institution: { mode: 'set', value: next } })}
                  placeholder="选择机构"
                  emptyText="还没有机构，请先到「设置 → 字典 → 机构与渠道」新建"
                />
              )}
            </DimRow>

            <DimRow
              title={DIM_LABEL.owner}
              required
              mode={choices.owner.mode}
              modes={KEEP_SET}
              onModeChange={(mode) => update({ owner: { mode: mode as ScalarMode, value: choices.owner.value } })}
            >
              {choices.owner.mode === 'set' && (
                <OptionPicker
                  label="统一设为"
                  options={memberOptions}
                  value={choices.owner.value}
                  onChange={(next) => update({ owner: { mode: 'set', value: next } })}
                  placeholder="选择归属成员"
                  emptyText="还没有成员，请先到「设置 → 字典 → 成员」新建"
                />
              )}
            </DimRow>

            <DimRow
              title="渠道"
              mode={choices.channel.mode}
              modes={KEEP_SET_CLEAR}
              onModeChange={(mode) => update({ channel: { mode: mode as ScalarMode, value: choices.channel.value } })}
            >
              {choices.channel.mode === 'set' && (
                <OptionPicker
                  label="统一设为"
                  options={channelOptions}
                  value={choices.channel.value}
                  onChange={(next) => update({ channel: { mode: 'set', value: next } })}
                  placeholder="选择渠道"
                  emptyText="还没有渠道，请先到「设置 → 字典 → 机构与渠道」新建"
                />
              )}
              {choices.channel.mode === 'clear' && (
                <p className="text-meta text-secondary">会把所有勾选项的渠道清空（渠道本来就是可选项）。</p>
              )}
            </DimRow>

            <DimRow
              title="风险等级"
              mode={choices.risk.mode}
              modes={KEEP_SET_CLEAR}
              onModeChange={(mode) => update({ risk: { mode: mode as ScalarMode, value: choices.risk.value } })}
            >
              {choices.risk.mode === 'set' && (
                <OptionPicker
                  label="统一设为"
                  options={RISK_LEVEL_OPTIONS}
                  value={choices.risk.value}
                  onChange={(next) => update({ risk: { mode: 'set', value: next } })}
                  placeholder="选择风险等级"
                />
              )}
              {choices.risk.mode === 'clear' && (
                <p className="text-meta text-secondary">会把所有勾选项的风险等级清空。</p>
              )}
            </DimRow>

            <div className="rounded-control border border-line p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 text-body font-medium text-ink">
                  <TagsIcon size={15} className="text-secondary" aria-hidden />
                  标签
                </span>
                <ModeSwitch
                  label="标签处理方式"
                  value={choices.tagMode}
                  onChange={(value) => update({ tagMode: value as TagMode })}
                  options={[
                    { value: 'keep', label: '保持不变' },
                    { value: 'add', label: '添加' },
                    { value: 'remove', label: '移除' },
                  ]}
                />
              </div>
              {choices.tagMode !== 'keep' && (
                <div className="mt-3">
                  <TagPicker
                    label={choices.tagMode === 'add' ? '要添加的标签' : '要移除的标签'}
                    tags={tags}
                    value={choices.tagIds}
                    onChange={(next) => update({ tagIds: next })}
                    hint={choices.tagMode === 'add'
                      ? `在原标签上追加；加完超过 ${MAX_TAGS} 个的项会单独报出，不会静默丢弃`
                      : '从这些项的标签里去掉所选标签'}
                  />
                </div>
              )}
            </div>
          </section>

          <section>
            <label className="block text-meta font-medium text-secondary" htmlFor="batch-classification-reason">
              变更理由（可选）
            </label>
            <input
              id="batch-classification-reason"
              type="text"
              value={choices.reason}
              onChange={(event) => update({ reason: event.target.value })}
              placeholder="如：按产品说明书统一归类"
              className="mt-1 min-h-[44px] w-full rounded-control border border-line-strong bg-surface px-3 text-body text-ink placeholder:text-muted"
            />
            <p className="mt-1 text-meta text-muted">理由会随每一项的分类变更记录一起保存。</p>
          </section>
        </div>
      )}
    </ResponsiveDialog>
  )
}