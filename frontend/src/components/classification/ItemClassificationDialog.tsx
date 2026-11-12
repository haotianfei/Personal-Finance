'use client'

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Info } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { ItemClassificationUpdate } from '@/types'
import { FormField, fieldControlClass } from '@/components/form'
import { InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import { AssetClassPicker } from './AssetClassPicker'
import { OptionPicker } from './OptionPicker'
import { TagPicker } from './TagPicker'
import { StatusStrip } from './StatusStrip'
import {
  INSTITUTION_KIND_LABEL,
  MAX_TAGS,
  MEMBER_ROLE_LABEL,
  REQUIRED_DIMS,
  RISK_LEVEL_OPTIONS,
  classificationErrorText,
  dimsText,
  invalidateClassificationViews,
  memberLabel,
  requiredMissing,
} from './classification'
import type { ClassificationSnapshot, RequiredDim } from './classification'
import { useClassificationDictionaries } from './useClassificationDictionaries'

/**
 * 单个资产项的分类编辑（七维）。
 *
 * 三条与后端契约对齐的行为：
 * 1. **只发改动过的维度**——PUT 是部分更新，不传的维度保持原值；可选维度
 *    （渠道 / 风险）清空时要显式传 `null`，否则后端把「没传」当成「不改」。
 * 2. 待补状态允许继续缺维保存（后端对 draft 放行），所以这里不拦「没填满」，
 *    只把「还差哪几维、这项暂不进统计」说清楚。
 * 3. 保存成功后用返回结果**更新本地基线**，再存一次仍按最新值算差异——
 *    否则「先把渠道设为 X 再清空」这类连续操作会被误判成「没改动」。
 */

export interface ItemClassificationDialogProps {
  /** null＝关闭；传入即打开。 */
  item: ClassificationSnapshot | null
  onClose: () => void
  onSaved?: () => void
}

interface FormState {
  classId: number | null
  liquidity: string
  institutionId: string
  channelId: string
  ownerId: string
  risk: string
  tagIds: number[]
}

function formFrom(item: ClassificationSnapshot): FormState {
  return {
    classId: item.assetClassId,
    liquidity: item.liquidityDays == null ? '' : String(item.liquidityDays),
    institutionId: item.institutionId == null ? '' : String(item.institutionId),
    channelId: item.channelId == null ? '' : String(item.channelId),
    ownerId: item.ownerId == null ? '' : String(item.ownerId),
    risk: item.riskLevel ?? '',
    tagIds: [...item.tagIds],
  }
}

/** 待补的必填维度（按当前表单值算，用于即时提示「保存后还剩几维」）。 */
function pendingDims(form: FormState): RequiredDim[] {
  const missing: RequiredDim[] = []
  if (form.classId == null) missing.push('asset_class')
  if (!form.liquidity) missing.push('liquidity')
  if (!form.institutionId) missing.push('institution')
  if (!form.ownerId) missing.push('owner')
  return REQUIRED_DIMS.filter((dim) => missing.includes(dim))
}

interface SaveResult {
  changed: boolean
  status: 'active' | 'draft' | 'archived'
  missing: RequiredDim[]
}

/** 停用的字典项不再作为新选择出现，但**当前值仍要看得见**（否则会被误读成「没设置」）。 */
function keepCurrent<T extends { id: number; active: boolean }>(
  rows: readonly T[],
  current: string,
): T[] {
  return rows.filter((row) => row.active || String(row.id) === current)
}

export function ItemClassificationDialog({ item, onClose, onSaved }: ItemClassificationDialogProps) {
  const queryClient = useQueryClient()
  const dict = useClassificationDictionaries(!!item)

  /** 服务端已知状态：打开时取 prop，保存成功后按返回值更新，供下一次差异计算用。 */
  const [current, setCurrent] = useState<ClassificationSnapshot | null>(item)
  const [form, setForm] = useState<FormState | null>(item ? formFrom(item) : null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<SaveResult | null>(null)

  // 换项时重置：不清上一次的提示，用户会以为新项已经保存过了
  useEffect(() => {
    setCurrent(item)
    setForm(item ? formFrom(item) : null)
    setReason('')
    setError(null)
    setSaved(null)
  }, [item])

  const classes = useMemo(() => dict.classesQuery.data?.items ?? [], [dict.classesQuery.data])
  const levels = useMemo(() => dict.levelsQuery.data?.items ?? [], [dict.levelsQuery.data])
  const institutions = useMemo(() => dict.institutionsQuery.data?.items ?? [], [dict.institutionsQuery.data])
  const channels = useMemo(() => dict.channelsQuery.data?.items ?? [], [dict.channelsQuery.data])
  const members = useMemo(() => dict.membersQuery.data?.items ?? [], [dict.membersQuery.data])
  const tags = useMemo(() => dict.tagsQuery.data?.items ?? [], [dict.tagsQuery.data])
  const dictLoading = dict.classesQuery.isLoading || dict.membersQuery.isLoading || dict.institutionsQuery.isLoading

  const classPathById = useMemo(
    () => new Map(classes.map((node) => [node.id, node.path])),
    [classes],
  )
  const tagNameById = useMemo(() => new Map(tags.map((tag) => [tag.id, tag.name])), [tags])

  const levelOptions = useMemo(
    () => levels.map((level) => ({
      value: String(level.days),
      label: level.label,
      hint: level.days >= 9999 ? '封闭期 / 一年以上' : `约 ${level.days} 天内可用`,
    })),
    [levels],
  )

  const institutionOptions = useMemo(
    () => keepCurrent(institutions, form?.institutionId ?? '')
      .map((entry) => ({
        value: String(entry.id),
        label: entry.name,
        badge: `${INSTITUTION_KIND_LABEL[entry.kind] ?? entry.kind}${entry.active ? '' : ' · 已停用'}`,
      })),
    [form?.institutionId, institutions],
  )

  const channelOptions = useMemo(
    () => keepCurrent(channels, form?.channelId ?? '')
      .map((entry) => ({ value: String(entry.id), label: entry.active ? entry.name : `${entry.name}（已停用）` })),
    [channels, form?.channelId],
  )

  const memberOptions = useMemo(
    () => keepCurrent(members, form?.ownerId ?? '')
      .map((entry) => ({
        value: String(entry.id),
        label: entry.name,
        badge: `${MEMBER_ROLE_LABEL[entry.role] ?? entry.role}${entry.active ? '' : ' · 已停用'}`,
        hint: memberLabel(entry),
      })),
    [form?.ownerId, members],
  )

  const saveMutation = useMutation({
    mutationFn: async (): Promise<SaveResult> => {
      if (!current || !form) throw new Error('没有可保存的资产项')
      const payload: ItemClassificationUpdate = {}
      if (form.classId != null && form.classId !== current.assetClassId) payload.asset_class_id = form.classId
      if (form.liquidity && Number(form.liquidity) !== current.liquidityDays) payload.liquidity_days = Number(form.liquidity)
      if (form.institutionId && Number(form.institutionId) !== current.institutionId) payload.institution_id = Number(form.institutionId)
      if (form.ownerId && Number(form.ownerId) !== current.ownerId) payload.owner_id = Number(form.ownerId)
      // 可选维度：清空要显式传 null，否则后端把「没传」理解成「不改」
      const nextChannel = form.channelId ? Number(form.channelId) : null
      if (nextChannel !== current.channelId) payload.channel_id = nextChannel
      const nextRisk = form.risk || null
      if (nextRisk !== current.riskLevel) payload.risk_level = nextRisk
      if (reason.trim()) payload.reason = reason.trim()

      const dimsChanged = Object.keys(payload).some((key) => key !== 'reason')
      const tagsChanged =
        form.tagIds.length !== current.tagIds.length ||
        form.tagIds.some((id) => !current.tagIds.includes(id))

      if (!dimsChanged && !tagsChanged) {
        return { changed: false, status: current.status, missing: pendingDims(form) }
      }

      let status = current.status
      let missing = pendingDims(form)
      if (dimsChanged) {
        const result = await api.setItemClassification(current.itemId, payload, newIdemKey())
        status = result.status
        missing = requiredMissing(result.missing_dims ?? result.classification_missing)
      }
      if (tagsChanged) {
        // 标签是整组替换：即使只多一个，也要把全量传上去
        await api.setItemTags(current.itemId, form.tagIds, newIdemKey())
      }
      return { changed: true, status, missing }
    },
    onSuccess: (result) => {
      if (!result.changed) {
        setError('没有改动，未提交。')
        return
      }
      setError(null)
      setSaved(result)
      setReason('')
      // 基线跟着表单走：保证「同一弹窗里连改两次」按最新值判差异
      setCurrent((previous) => previous && form ? {
        ...previous,
        status: result.status,
        missing: result.missing,
        assetClassId: form.classId ?? previous.assetClassId,
        assetClassPath: form.classId == null ? previous.assetClassPath : classPathById.get(form.classId) ?? previous.assetClassPath,
        liquidityDays: form.liquidity ? Number(form.liquidity) : previous.liquidityDays,
        institutionId: form.institutionId ? Number(form.institutionId) : previous.institutionId,
        channelId: form.channelId ? Number(form.channelId) : null,
        ownerId: form.ownerId ? Number(form.ownerId) : previous.ownerId,
        riskLevel: form.risk || null,
        tagIds: [...form.tagIds],
        tagNames: form.tagIds.map((id) => tagNameById.get(id) ?? '').filter(Boolean),
      } : previous)
      invalidateClassificationViews(queryClient)
      onSaved?.()
    },
    onError: (mutationError) => setError(classificationErrorText(mutationError)),
  })

  if (!item || !current) return null

  const archived = current.status === 'archived'
  const missingNow = form ? pendingDims(form) : []
  const willActivate = current.status === 'draft' && missingNow.length === 0

  const footer = (
    <div className="flex flex-col gap-2 tablet:flex-row tablet:items-center tablet:justify-between">
      <p className="text-meta text-muted">
        {archived
          ? '已归档的资产项不能改分类；需要修改请先取消归档。'
          : missingNow.length === 0
            ? '四个必填维度已齐，保存后这项立即生效。'
            : `还有${dimsText(missingNow)}没填，保存后这项会保持待补状态，暂不进统计。`}
      </p>
      <div className="flex flex-col gap-2 tablet:flex-row">
        <button
          type="button"
          onClick={onClose}
          disabled={saveMutation.isPending}
          className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50"
        >
          关闭
        </button>
        <button
          type="button"
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || dictLoading || archived}
          className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
        >
          {saveMutation.isPending ? '保存中…' : '保存分类'}
        </button>
      </div>
    </div>
  )

  return (
    <ResponsiveDialog
      open
      onClose={onClose}
      title="资产项分类"
      description={current.displayName}
      size="lg"
      footer={footer}
    >
      {dictLoading || !form ? (
        <LoadingState rows={5} label="正在加载分类选项" />
      ) : (
        <div className="space-y-4">
          <StatusStrip status={current.status} missing={current.missing} />

          {saved && (
            <div
              role="status"
              className={cn(
                'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-control border px-3 py-2 text-meta',
                saved.status === 'active'
                  ? 'border-status-good/25 bg-status-good-surface text-status-good-text'
                  : 'border-status-warn/25 bg-status-warn-surface text-status-warn-text',
              )}
            >
              {saved.status === 'active' ? <Check size={14} aria-hidden /> : <Info size={14} aria-hidden />}
              <span>
                {saved.status === 'active'
                  ? '已保存。这项分类已齐（已生效），按类别 / 机构 / 归属的统计与导出都已包含它。'
                  : `已保存。还差${dimsText(saved.missing)}，补齐后才会生效。`}
              </span>
            </div>
          )}

          {error && <InlineError>{error}</InlineError>}

          {willActivate && (
            <div className="flex gap-2 rounded-control border border-status-good/25 bg-status-good-surface p-3 text-meta text-status-good-text">
              <Check size={15} className="mt-0.5 shrink-0" aria-hidden />
              <p>四个必填维度已齐：保存后这项会从「待补分类」变为「已生效」，进入按类别的统计与导出。</p>
            </div>
          )}

          {!archived && !willActivate && missingNow.length > 0 && (
            <div className="flex gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden />
              <p>还差{dimsText(missingNow)}。这四项都填上，这项才会进入按类别的统计与导出。</p>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
            <AssetClassPicker
              classes={classes}
              value={form.classId}
              onChange={(next) => setForm({ ...form, classId: next })}
              side={current.side}
              required
              disabled={archived}
              hint="只能选最末一级；路径会一并列出"
            />
            <OptionPicker
              label="流动性"
              required
              options={levelOptions}
              value={form.liquidity}
              onChange={(next) => setForm({ ...form, liquidity: next })}
              placeholder="选择流动性档位"
              disabled={archived}
            />
            <OptionPicker
              label="机构"
              required
              options={institutionOptions}
              value={form.institutionId}
              onChange={(next) => setForm({ ...form, institutionId: next })}
              placeholder="选择机构"
              disabled={archived}
              emptyText="还没有机构，请先到「设置 → 字典 → 机构与渠道」新建"
            />
            <OptionPicker
              label="归属"
              required
              options={memberOptions}
              value={form.ownerId}
              onChange={(next) => setForm({ ...form, ownerId: next })}
              placeholder="选择归属成员"
              disabled={archived}
              emptyText="还没有成员，请先到「设置 → 字典 → 成员」新建"
            />
            <OptionPicker
              label="渠道"
              options={channelOptions}
              value={form.channelId}
              onChange={(next) => setForm({ ...form, channelId: next })}
              placeholder="不设置"
              clearable
              disabled={archived}
              hint="同一机构下有多个入口时才需要填"
              emptyText="还没有渠道，请先到「设置 → 字典 → 机构与渠道」新建"
            />
            <OptionPicker
              label="风险等级"
              options={RISK_LEVEL_OPTIONS}
              value={form.risk}
              onChange={(next) => setForm({ ...form, risk: next })}
              placeholder="不设置"
              clearable
              disabled={archived}
              hint="可选；用于加权风险统计"
            />
          </div>

          <TagPicker
            tags={tags}
            value={form.tagIds}
            onChange={(next) => setForm({ ...form, tagIds: next })}
            limit={MAX_TAGS}
            disabled={archived}
            hint="可选，最多 5 个；跨类别的标记用标签表达"
          />

          <FormField label="变更理由（可选）" hint="会随这次分类变更写进该项的变更记录">
            <input
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="如：确认这是货币基金"
              disabled={archived}
              className={fieldControlClass}
            />
          </FormField>
        </div>
      )}
    </ResponsiveDialog>
  )
}