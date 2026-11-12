'use client'

import { useEffect, useId, useMemo, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { message } from 'antd'
import { History } from 'lucide-react'
import { ApiError, api, newIdemKey } from '@/lib/api'
import { formatDate, formatRatio } from '@/lib/format'
import { cn } from '@/lib/utils'
import { FormField, SelectField, fieldControlClass, fieldErrorClass } from '@/components/form'
import type { SelectFieldOption } from '@/components/form'
import { InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import type {
  CurrentValueItem,
  LineageAttachResponse,
  LineageLinkView,
  RenameEffect,
  RenamePreviewResponse,
  RenameReason,
} from '@/types'
import { AttachPanel, type AttachSubmitMeta } from './AttachPanel'
import { FamilyTree } from './FamilyTree'
import { MergePanel } from './MergePanel'
import { SplitPanel } from './SplitPanel'
import {
  ATTACH_LINK_TYPE_LABEL,
  LINK_TYPE_LABEL,
  buildTakenNames,
  lineageErrorText,
} from './shared'

/**
 * 资产项行详情：身份（现名 / 别名史 / 身份族）＋ 四个身份动作。
 *
 * 入口在「资产当前值」每行的「身份」按钮（票 02）。这里不做口径推算：
 * 合并输出金额与拆分比例都由人填，前端只做格式与合计校验。
 */
export interface ItemIdentityDialogProps {
  open: boolean
  item: CurrentValueItem | null
  /** 当前值视图的全部行：合并候选。 */
  candidates: CurrentValueItem[]
  /** 生效日默认值（当前值视图的参考日＝后端「今天」）。 */
  today: string
  /** 改关联的生效日预填＝最近快照日（补登记不静默用今天）。 */
  defaultEffectiveDate: string
  onClose: () => void
  /** 任一动作成功后：父级刷新当前值 + 趋势。 */
  onChanged: () => void
  /** 点击身份族成员：把弹层切到该资产项（父级按 item_id 换行）。 */
  onSelectItem?: (itemId: number) => void
}

type TabKey = 'rename' | 'merge' | 'split' | 'attach'

const TABS: { key: TabKey; label: string; hint: string }[] = [
  { key: 'rename', label: '改名', hint: '只改显示名' },
  { key: 'merge', label: '合并', hint: '多项合一' },
  { key: 'split', label: '拆分', hint: '一项拆多' },
  { key: 'attach', label: '改关联', hint: '关联已有项' },
]

function IdentityRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 px-3 py-2.5">
      <dt className="w-12 shrink-0 text-meta text-muted">{label}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  )
}

// --- 改名：原因 + 影响范围（票 03 D1–D3）---
const RENAME_REASON_OPTIONS: SelectFieldOption[] = [
  { value: 'correction', label: '旧名写错了（纠正）' },
  { value: 'evolution', label: '名称演进（产品 / 机构改名）' },
  { value: 'normalization', label: '书写归一（空格 / 分隔符 / 全半角）' },
]
const RENAME_EFFECT_OPTIONS: SelectFieldOption[] = [
  { value: 'current', label: '只改当前名（历史记录保留旧名）' },
  { value: 'history_backfill', label: '同时改写历史快照名（id 不变）' },
]
/** 默认强绑定：纠正类连历史快照一起改；演进 / 归一不动历史文本。 */
const RENAME_DEFAULT_EFFECT: Record<RenameReason, RenameEffect> = {
  correction: 'history_backfill',
  evolution: 'current',
  normalization: 'current',
}

/** 改名错误 → 人话（统一信封 code 优先，其余交回公共兜底）。 */
function renameErrorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.code === 'NAME_CONFLICT') {
      const other = error.details?.conflicting_item_id
      const otherName = error.details?.conflicting_display_name
      const where = typeof otherName === 'string' && otherName
        ? `「${otherName}」${typeof other === 'number' ? `（#${other}）` : ''}`
        : typeof other === 'number' ? `（#${other}）` : ''
      return `这个名称已被另一个资产项${where}占用；请先处理同名项，或换一个名称。`
    }
    if (error.code === 'RENAME_REASON_REQUIRED') {
      return '请选择改名原因与影响范围；与默认组合不同时还要在备注里写明理由。'
    }
    if (error.code === 'RENAME_NOTE_REQUIRED') {
      return '这一组合不是默认做法：请在备注里写明理由，再重新预览。'
    }
    if (error.code === 'RENAME_CONFIRM_INVALID') {
      return '预览已过期，或者这段时间里记录有变化；请重新点「预览影响」再确认。'
    }
    if (error.code === 'ITEM_ARCHIVED') {
      return '该资产项已归档，不能再改名；请先恢复后再改。'
    }
    if (error.code === 'IDEMPOTENCY_KEY_REUSED') {
      return '这次提交与上一次重复了，请重新预览后再试。'
    }
  }
  return lineageErrorText(error, fallback)
}

function LinkList({ links, side }: { links: LineageLinkView[]; side: 'predecessor' | 'successor' }) {
  if (links.length === 0) {
    return (
      <span className="text-meta text-muted">
        {side === 'predecessor' ? '无前身' : '无后继'}
      </span>
    )
  }
  return (
    <ul className="space-y-1.5">
      {links.map((link) => {
        const name = side === 'predecessor' ? link.from_name : link.to_name
        return (
          <li key={link.id} className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
            <span className="min-w-0 break-words text-body text-ink">{name}</span>
            <span className="rounded-pill border border-line bg-subtle px-1.5 py-px text-micro text-secondary">
              {LINK_TYPE_LABEL[link.link_type] ?? link.link_type}
            </span>
            {link.split_ratio && (
              <span className="pf-num text-micro text-muted">占 {formatRatio(link.split_ratio)}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

export function ItemIdentityDialog({
  open,
  item,
  candidates,
  today,
  defaultEffectiveDate,
  onClose,
  onChanged,
  onSelectItem,
}: ItemIdentityDialogProps) {
  const itemId = item?.item_id ?? null
  const identityHeadingId = useId()

  const [activeTab, setActiveTab] = useState<TabKey>('rename')
  const [renameValue, setRenameValue] = useState(item?.display_name ?? '')
  const [renameDirty, setRenameDirty] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)
  const [renameNotice, setRenameNotice] = useState<string | null>(null)
  // 改名原因 / 影响范围 / 备注：默认都不预选（改写历史是破例动作，不能让默认值替人做决定）
  const [renameReason, setRenameReason] = useState<RenameReason | ''>('')
  const [renameEffect, setRenameEffect] = useState<RenameEffect | ''>('')
  const [renameNote, setRenameNote] = useState('')
  // 预览回执 + 预览错误；任一参数变动都会作废令牌，必须重新预览
  const [renamePreview, setRenamePreview] = useState<RenamePreviewResponse | null>(null)
  const [renamePreviewError, setRenamePreviewError] = useState<string | null>(null)

  const itemsQuery = useQuery({
    queryKey: ['assetItems', true],
    queryFn: () => api.listAssetItems(true),
    enabled: open,
    staleTime: 30_000,
  })
  const lineageQuery = useQuery({
    queryKey: ['lineageByItem', itemId],
    queryFn: () => api.getLineageByItem(itemId!),
    enabled: open && itemId !== null,
    retry: false,
  })
  // 身份族（双向传递闭包，含自身）；端点未就绪时静默回落到前身/后继两行。
  const familyQuery = useQuery({
    queryKey: ['lineageFamily', itemId],
    queryFn: () => api.getLineageFamily(itemId!),
    enabled: open && itemId !== null,
    retry: false,
  })

  const itemInfo = useMemo(
    () => itemsQuery.data?.items.find((candidate) => candidate.id === itemId) ?? null,
    [itemsQuery.data, itemId],
  )
  const displayName = itemInfo?.display_name ?? item?.display_name ?? ''
  const archived = (itemInfo?.status ?? item?.item_status) === 'archived'
  const aliases = useMemo(
    () => (itemInfo?.aliases ?? []).filter((name) => name !== displayName),
    [itemInfo, displayName],
  )
  const takenNames = useMemo(
    () => buildTakenNames(itemsQuery.data?.items ?? []),
    [itemsQuery.data],
  )

  // 换行 / 改名后把输入对齐到现名；成功提示保留到下一次输入
  useEffect(() => {
    setRenameValue(displayName)
    setRenameError(null)
    setRenameDirty(false)
  }, [itemId, displayName])

  useEffect(() => {
    setRenameNotice(null)
    setActiveTab('rename')
  }, [itemId])

  // 换资产项：原因 / 影响范围 / 预览一律回到未选状态（上一条项的令牌不能带过来）
  useEffect(() => {
    setRenameReason('')
    setRenameEffect('')
    setRenameNote('')
    setRenamePreview(null)
    setRenamePreviewError(null)
  }, [itemId])

  // 关窗即作废预览令牌：下次打开要重新看影响面（令牌是一次性且有有效期）
  useEffect(() => {
    if (open) return
    setRenamePreview(null)
    setRenamePreviewError(null)
  }, [open])

  // 预览：把原因、影响范围与备注一起交给后端算影响面，并拿一次性令牌
  const previewMutation = useMutation({
    mutationFn: (payload: { name: string; reason: RenameReason; effect: RenameEffect; note: string }) =>
      api.previewRenameItem(itemId!, {
        name: payload.name,
        reason: payload.reason,
        effect: payload.effect,
        ...(payload.note ? { reason_note: payload.note } : {}),
      }),
    onSuccess: (result) => {
      setRenamePreview(result)
      setRenamePreviewError(null)
      setRenameError(null)
    },
    onError: (error) => {
      setRenamePreview(null)
      setRenamePreviewError(renameErrorText(error, '预览失败，请重试'))
    },
  })

  const renameMutation = useMutation({
    mutationFn: (payload: { name: string; reason: RenameReason; effect: RenameEffect; note: string; token: string }) =>
      api.renameAssetItem(itemId!, {
        name: payload.name,
        reason: payload.reason,
        effect: payload.effect,
        ...(payload.note ? { reason_note: payload.note } : {}),
        confirm_token: payload.token,
      }, newIdemKey()),
    onSuccess: (result) => {
      setRenameValue(result.item.display_name)
      setRenameNotice(
        result.effect === 'history_backfill'
          ? `已改名；同时把 ${result.backfill_rows} 行历史记录的名称改写为新名（资产项 id 不变）。`
          : '已改名；历史记录保留旧名，读数仍归到同一个资产项（id 不变）。',
      )
      setRenameError(null)
      setRenamePreview(null)
      setRenameDirty(false)
      // 原因 / 影响范围 / 备注一并复位：下一次改名重新从「未选」开始，避免沿用上次的纠正语义
      setRenameReason('')
      setRenameEffect('')
      setRenameNote('')
      // 旧名已进名史：就地刷新别名史与名称占用
      void itemsQuery.refetch()
      onChanged()
    },
    onError: (error) => {
      setRenamePreview(null)
      setRenameError(renameErrorText(error, '改名失败，请重试'))
    },
  })

  if (!open || !item) return null

  const trimmedRename = renameValue.trim()
  const renamed = trimmedRename !== displayName
  const renameBlocked = trimmedRename === '' || !renamed
  const renameNoteTrimmed = renameNote.trim()
  // 默认组合随原因联动；改成非默认组合必须在备注里写理由（后端同样 400）
  const defaultEffect: RenameEffect | '' = renameReason ? RENAME_DEFAULT_EFFECT[renameReason] : ''
  const effectIsDefault = renameEffect !== '' && renameEffect === defaultEffect
  const renameNoteRequired = renameEffect !== '' && defaultEffect !== '' && !effectIsDefault
  const renameNoteMissing = renameNoteRequired && renameNoteTrimmed === ''
  const canPreview =
    !renameBlocked && renameReason !== '' && renameEffect !== '' && !renameNoteMissing && !previewMutation.isPending

  const pickReason = (value: string) => {
    const reason = value as RenameReason | ''
    setRenameReason(reason)
    setRenameEffect(reason ? RENAME_DEFAULT_EFFECT[reason] : '')
    setRenamePreview(null)
    setRenamePreviewError(null)
    setRenameError(null)
    setRenameNotice(null)
  }

  const pickEffect = (value: string) => {
    setRenameEffect(value as RenameEffect | '')
    setRenamePreview(null)
    setRenamePreviewError(null)
    setRenameError(null)
  }

  const runPreview = () => {
    // 先校验取值（顺带收窄类型），再看是否满足预览条件
    if (renameReason === '' || renameEffect === '' || !canPreview) return
    setRenamePreviewError(null)
    setRenameError(null)
    setRenameNotice(null)
    previewMutation.mutate({
      name: trimmedRename,
      reason: renameReason,
      effect: renameEffect,
      note: renameNoteTrimmed,
    })
  }

  const submitRename = () => {
    if (!renamePreview || renameMutation.isPending) return
    setRenameError(null)
    setRenameNotice(null)
    renameMutation.mutate({
      name: renamePreview.new_name,
      reason: renamePreview.reason,
      effect: renamePreview.effect,
      note: renameNoteTrimmed,
      token: renamePreview.confirm_token,
    })
  }

  /** Tabs 键盘模型（APG）：左右方向键切换、Home/End 到首尾，焦点跟随选中项。 */
  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End']
    if (!keys.includes(event.key)) return
    event.preventDefault()
    let nextIndex = index
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + TABS.length) % TABS.length
    else if (event.key === 'ArrowRight') nextIndex = (index + 1) % TABS.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = TABS.length - 1
    const nextTab = TABS[nextIndex]
    setActiveTab(nextTab.key)
    document.getElementById(`lineage-tab-${nextTab.key}`)?.focus()
  }

  const handleMergeSubmitted = (name: string, count: number) => {
    message.success(`已合并 ${count} 个旧项为「${name}」；旧项自生效日起终止计量。新项还没有本期记录，可在当前值里直接补录。`)
    onChanged()
    onClose()
  }

  const handleSplitSubmitted = (count: number, archivedFrom: string) => {
    message.success(`已拆分为 ${count} 个子项；父项自 ${archivedFrom} 起终止计量。子项还没有本期记录，可在当前值里直接补录。`)
    onChanged()
    onClose()
  }

  const handleAttachSubmitted = (result: LineageAttachResponse, meta: AttachSubmitMeta) => {
    const relation = ATTACH_LINK_TYPE_LABEL[meta.linkType] ?? meta.linkType
    const scope = meta.direction === 'out'
      ? `本项自 ${formatDate(result.archived_effective_from)} 起终止计量，${meta.targetCount} 个目标项继续`
      : `${meta.targetCount} 个所选资产项自 ${formatDate(result.archived_effective_from)} 起终止计量，本项继续`
    message.success(`已登记${relation}：${scope}。历史记录不迁移，读数按身份族合并。`)
    onChanged()
    onClose()
  }

  const familyMembers = familyQuery.data?.members ?? []
  const hasFamilyView = familyMembers.length > 0

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={<span className="break-words">{displayName}</span>}
      description="资产项身份与血缘"
      size="lg"
    >
      <div className="space-y-5">
        <section aria-labelledby={identityHeadingId} className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h3 id={identityHeadingId} className="text-h3 font-semibold text-ink">身份</h3>
            <span className="pf-num text-meta text-muted">资产项 #{item.item_id}</span>
          </div>

          {itemsQuery.isLoading || lineageQuery.isLoading || familyQuery.isLoading ? (
            <div className="rounded-control border border-line">
              <LoadingState rows={3} compact label="正在读取资产项身份" />
            </div>
          ) : (
            <div className="overflow-hidden rounded-control border border-line bg-surface">
              <dl className="divide-y divide-line">
                <IdentityRow label="现名">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="break-words text-body-strong text-ink">{displayName}</span>
                    <span
                      className={cn(
                        'rounded-pill px-1.5 py-px text-micro',
                        archived
                          ? 'bg-status-warn-surface text-status-warn-text'
                          : 'bg-subtle text-secondary',
                      )}
                    >
                      {archived ? '已归档' : '在用'}
                    </span>
                  </span>
                </IdentityRow>

                <IdentityRow label="别名史">
                  {aliases.length === 0 ? (
                    <span className="text-meta text-muted">暂无旧名</span>
                  ) : (
                    <span className="flex flex-wrap items-center gap-1">
                      <History size={13} className="shrink-0 text-muted" aria-hidden />
                      {aliases.map((alias) => (
                        <span
                          key={alias}
                          className="rounded-pill border border-line bg-subtle px-1.5 py-px text-micro text-secondary"
                          title={`旧名：${alias}`}
                        >
                          {alias}
                        </span>
                      ))}
                    </span>
                  )}
                </IdentityRow>

                {/* 身份族端点未就绪时回落到单向的前身 / 后继两行（信息不丢） */}
                {!hasFamilyView && (
                  <>
                    <IdentityRow label="前身">
                      <LinkList links={lineageQuery.data?.predecessors ?? []} side="predecessor" />
                    </IdentityRow>
                    <IdentityRow label="后继">
                      <LinkList links={lineageQuery.data?.successors ?? []} side="successor" />
                    </IdentityRow>
                  </>
                )}
              </dl>

              {hasFamilyView && (
                <div className="space-y-2 border-t border-line px-3 py-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                    <h4 className="text-meta font-medium text-secondary">身份族</h4>
                    <span className="pf-num text-micro text-muted">共 {familyMembers.length} 项</span>
                  </div>
                  <FamilyTree
                    members={familyMembers}
                    selfId={item.item_id}
                    selfName={displayName}
                    selfStatus={archived ? 'archived' : 'active'}
                    onSelect={onSelectItem}
                  />
                </div>
              )}
            </div>
          )}

          {lineageQuery.error && (
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-status-warn-text">
              <span>
                血缘读取失败：
                {lineageQuery.error instanceof Error ? lineageQuery.error.message : '未知错误'}
              </span>
              <button
                type="button"
                onClick={() => { void lineageQuery.refetch() }}
                className="inline-flex min-h-[44px] items-center underline underline-offset-2"
              >
                重试
              </button>
            </p>
          )}

          <p className="text-micro text-muted">
            改名只动名称（必要时可把历史快照里的旧名一起改写）；合并 / 拆分 / 改关联登记后旧项自生效日起归档，历史仍留在旧项，读数按身份族合并。
          </p>
        </section>

        <div
          role="tablist"
          aria-label="资产项动作"
          className="grid grid-cols-2 gap-1 rounded-control border border-line bg-subtle p-1 tablet:grid-cols-4"
        >
          {TABS.map((tab, index) => {
            const selected = tab.key === activeTab
            return (
              <button
                key={tab.key}
                type="button"
                role="tab"
                id={`lineage-tab-${tab.key}`}
                aria-selected={selected}
                aria-controls={`lineage-panel-${tab.key}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => setActiveTab(tab.key)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
                className={cn(
                  'flex min-h-[44px] flex-col items-center justify-center rounded-control px-2 py-1.5 text-center transition-colors',
                  selected
                    ? 'bg-surface text-ink shadow-card'
                    : 'text-secondary hover:text-ink',
                )}
              >
                <span className="text-meta font-medium">{tab.label}</span>
                <span className="text-micro text-muted">{tab.hint}</span>
              </button>
            )
          })}
        </div>

        <div
          role="tabpanel"
          id="lineage-panel-rename"
          aria-labelledby="lineage-tab-rename"
          hidden={activeTab !== 'rename'}
          className="space-y-3"
        >
          <p className="text-meta text-secondary">
            改名只动名称：金额、日期与归属都不变。历史记录里的旧名是否跟着改，由下面的影响范围决定。
          </p>
          <FormField
            label="新的显示名"
            required
            error={renameError ?? undefined}
            hint="改名不会合并或拆分资产项。"
          >
            <input
              type="text"
              value={renameValue}
              onChange={(event) => {
                setRenameValue(event.target.value)
                setRenameDirty(true)
                setRenameError(null)
                setRenameNotice(null)
                // 名称变了，旧令牌作废：必须重新预览影响面
                setRenamePreview(null)
                setRenamePreviewError(null)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  if (renamePreview) submitRename()
                  else runPreview()
                }
              }}
              autoComplete="off"
              className={cn(fieldControlClass, renameError && fieldErrorClass)}
            />
          </FormField>

          <div className="grid gap-3 tablet:grid-cols-2">
            <SelectField
              label="改名原因"
              required
              value={renameReason}
              onChange={pickReason}
              options={RENAME_REASON_OPTIONS}
              placeholder="请选择原因"
            />
            <SelectField
              label="影响范围"
              required
              value={renameEffect}
              onChange={pickEffect}
              options={RENAME_EFFECT_OPTIONS}
              placeholder={renameReason ? '请选择影响范围' : '先选原因'}
              disabled={renameReason === ''}
              hint={
                renameReason === ''
                  ? '原因选定后这里会给出默认组合。'
                  : effectIsDefault
                    ? '上方是默认组合，可直接预览。'
                    : '非默认组合：会改写历史记录里的名称文本，需在备注里写明理由。'
              }
            />
          </div>

          <FormField
            label={renameNoteRequired ? '备注（必填：说明为什么不是默认组合）' : '备注（可选）'}
            error={renameNoteMissing ? '这一组合不是默认做法，必须写明理由' : undefined}
            hint={renameNoteRequired ? undefined : '会随改名一起记入审计，方便日后回溯。'}
          >
            <textarea
              value={renameNote}
              onChange={(event) => {
                setRenameNote(event.target.value)
                setRenamePreview(null)
                setRenamePreviewError(null)
              }}
              rows={2}
              maxLength={200}
              placeholder="例如：机构换名，历史记录保留当时的名称"
              className={cn(fieldControlClass, 'py-2', renameNoteMissing && fieldErrorClass)}
            />
          </FormField>

          {renamePreview && (
            <div className="space-y-1 rounded-control border border-line bg-subtle px-3 py-2 text-meta text-secondary">
              {renamePreview.impact.rows > 0 ? (
                <p>
                  {/* 影响面是该项名下的全部记录跨度（后端口径）；是否改写名称由 effect 决定 */}
                  {renamePreview.effect === 'history_backfill' ? '将影响 ' : '该项共有 '}
                  <span className="pf-num font-medium text-ink">{renamePreview.impact.rows}</span> 行资产记录
                  （
                  {renamePreview.impact.first_date
                    ? `${formatDate(renamePreview.impact.first_date)} ~ ${formatDate(renamePreview.impact.last_date ?? renamePreview.impact.first_date)}`
                    : '暂无日期'}
                  ，其中 <span className="pf-num">{renamePreview.impact.superseded_rows}</span> 行已归档替换）
                </p>
              ) : (
                <p>该项还没有任何资产记录。</p>
              )}
              <p>
                {renamePreview.effect === 'history_backfill'
                  ? '这些记录的名称会被改写成新名；资产项 id、金额与日期都不变。'
                  : '历史记录的名称不改写，只改当前显示名；读数仍归到同一个资产项。'}
              </p>
              {renamePreview.impact.archived_item && (
                <p className="text-status-warn-text">该资产项已归档，改名后仍保持归档。</p>
              )}
            </div>
          )}

          {renamePreviewError && (
            <p role="alert" className="rounded-control border border-status-critical/25 px-3 py-2 text-meta text-status-critical-text">
              {renamePreviewError}
            </p>
          )}

          {renameNotice && (
            <p role="status" className="rounded-control border border-status-good/25 bg-subtle px-3 py-2 text-meta text-status-good-text">
              {renameNotice}
            </p>
          )}

          <div className="space-y-2">
            {renameDirty && renameValue.trim() !== '' && !renamed && (
              <p className="text-meta text-muted">新名称与现名相同，无需保存。</p>
            )}
            <div className="flex flex-col gap-2 tablet:flex-row">
              <button
                type="button"
                onClick={runPreview}
                disabled={!canPreview}
                className="inline-flex min-h-[44px] w-full items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong font-medium text-ink transition-colors hover:bg-subtle disabled:cursor-not-allowed disabled:opacity-50 tablet:w-auto"
              >
                {previewMutation.isPending ? '预览中…' : '预览影响'}
              </button>
              <button
                type="button"
                onClick={submitRename}
                disabled={!renamePreview || renameMutation.isPending}
                className="inline-flex min-h-[44px] w-full items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white transition-colors hover:bg-action-primary-hover disabled:cursor-not-allowed disabled:opacity-50 tablet:w-auto"
              >
                {renameMutation.isPending ? '保存中…' : '确认改名'}
              </button>
            </div>
            {!renamePreview && !renameBlocked && (
              <p className="text-meta text-muted">
                {renameReason === '' || renameEffect === ''
                  ? '先选改名原因与影响范围，再点「预览影响」看会改到哪些记录。'
                  : renameNoteMissing
                    ? '这一组合不是默认做法：在备注里写清理由后才能预览。'
                    : '先点「预览影响」看会改到哪些历史记录，确认无误再改名。'}
              </p>
            )}
          </div>
        </div>

        <div
          role="tabpanel"
          id="lineage-panel-merge"
          aria-labelledby="lineage-tab-merge"
          hidden={activeTab !== 'merge'}
        >
          <MergePanel
            key={`merge-${item.item_id}`}
            item={item}
            candidates={candidates}
            takenNames={takenNames}
            today={today}
            onSubmitted={handleMergeSubmitted}
          />
        </div>

        <div
          role="tabpanel"
          id="lineage-panel-split"
          aria-labelledby="lineage-tab-split"
          hidden={activeTab !== 'split'}
        >
          <SplitPanel
            key={`split-${item.item_id}`}
            item={item}
            takenNames={takenNames}
            today={today}
            onSubmitted={handleSplitSubmitted}
          />
        </div>

        <div
          role="tabpanel"
          id="lineage-panel-attach"
          aria-labelledby="lineage-tab-attach"
          hidden={activeTab !== 'attach'}
        >
          <AttachPanel
            key={`attach-${item.item_id}`}
            item={item}
            items={itemsQuery.data?.items ?? []}
            itemsLoading={itemsQuery.isLoading}
            active={activeTab === 'attach'}
            candidates={candidates}
            defaultEffectiveDate={defaultEffectiveDate || today}
            onSubmitted={handleAttachSubmitted}
          />
        </div>

        {itemsQuery.error && (
          <InlineError>
            资产项列表读取失败，别名史与名称占用检查可能不完整：
            {itemsQuery.error instanceof Error ? itemsQuery.error.message : '未知错误'}
          </InlineError>
        )}
      </div>
    </ResponsiveDialog>
  )
}