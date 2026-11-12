'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, History, Plus, Save, XCircle } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { formatDate, formatDateTime, formatPercent, formatRate, formatValue } from '@/lib/format'
import type {
  AssetClass, ReviewAllocationRow, ReviewAttribution, ReviewAttributionCategory, ReviewProposal,
  ReviewProposalInput, ReviewProposalKind, ReviewSnapshot,
} from '@/types'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, ConfirmDialog, EmptyState, ErrorState, GradeDot, InlineError, LoadingState, Money, ResponsiveDialog } from '@/components/ui'
import { DateField, FormField, SelectField, fieldControlClass } from '@/components/form'
import { DirectionText } from '@/components/ui/DirectionText'
import {
  ATTRIBUTION_LABEL, ATTRIBUTION_OPTIONS, PROPOSAL_KIND_LABEL, PROPOSAL_STATUS_CLASS,
  PROPOSAL_STATUS_LABEL, REVIEW_STATUS_CLASS, REVIEW_STATUS_LABEL, reviewError,
} from '@/components/reviews/shared'
import { cn } from '@/lib/utils'
import { gradeLabel } from '@/lib/metric-language'
import type { MetricGradeState } from '@/types'

const GRADE_STATES: MetricGradeState[] = ['green', 'yellow', 'red', 'gray']
const EVENT_STATUS_LABEL: Record<string, string> = {
  new: '新增', ongoing: '持续', resolved: '已解除', dismissed: '已忽略',
}
const TARGET_DIMENSION_LABEL: Record<string, string> = {
  asset_class: '资产类别', liquidity: '流动性', institution: '机构',
  channel: '渠道', risk_level: '风险等级',
}

function collectAssetClassNames(items: AssetClass[], out = new Map<number, string>()): Map<number, string> {
  for (const item of items) out.set(item.id, item.path || item.name)
  return out
}

function signalStateLabel(state: string | null | undefined): string | null {
  return state && GRADE_STATES.includes(state as MetricGradeState)
    ? gradeLabel(state as MetricGradeState)
    : null
}

function proposalTargetId(proposal: ReviewProposal): number | null {
  if (proposal.kind !== 'allocation_target' && proposal.kind !== 'threshold') return null
  const raw = proposal.payload?.allocation_target_id
  if (raw === null || raw === undefined || raw === '') return null
  const id = Number(raw)
  return Number.isInteger(id) && id > 0 ? id : null
}



function AnchorCard({ title, anchor, start = false }: {
  title: string
  anchor: ReviewSnapshot['anchors']['end'] | null
  start?: boolean
}) {
  if (!anchor) {
    return <Card padding="sm" className="space-y-1"><p className="text-meta text-muted">{title}</p><p className="text-body text-secondary">无起点锚（不可比）</p></Card>
  }
  return (
    <Card padding="sm" className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-meta text-muted">{title} · v{anchor.version_no} · {formatDate(anchor.as_of)}</p>
        {anchor.grade && <span className="inline-flex items-center gap-1 text-meta text-secondary"><GradeDot state={anchor.grade.state} />{anchor.grade.note ?? ''}</span>}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div><p className="text-meta text-muted">当前净值</p><Money value={anchor.net_worth} unit="cny" size="s" /></div>
        <div><p className="text-meta text-muted">达成度</p><p className="pf-num text-money-m font-semibold text-ink">{formatValue(anchor.progress, 'ratio')}</p></div>
        <div><p className="text-meta text-muted">缺口</p><Money value={anchor.gap} unit="cny" size="xs" /></div>
        <div><p className="text-meta text-muted">所需年化</p><p className="pf-num text-body text-secondary">{anchor.required_annualized ? formatRate(anchor.required_annualized, 'ratio') : '率不可得'}</p></div>
      </div>
      {start && <p className="text-meta text-muted">起点锚 {formatDate(anchor.anchor_date)}</p>}
    </Card>
  )
}

function AllocationRow({ row }: { row: ReviewAllocationRow }) {
  const stateLabel = row.state && ['green', 'yellow', 'red', 'gray'].includes(row.state)
    ? gradeLabel(row.state as MetricGradeState)
    : null
  return (
    <div className="rounded-control border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-body-strong text-ink">{row.target_ref}</span>
        <span className="text-meta text-muted">{row.dimension}{row.revision_id ? ` · revision ${row.revision_id}` : ''}</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-meta">
        <div><dt className="text-muted">目标</dt><dd className="pf-num text-ink">{formatPercent(row.target_percent)}</dd></div>
        <div><dt className="text-muted">实际</dt><dd className="pf-num text-ink">{row.actual_percent_start ? `${formatPercent(row.actual_percent_start)} → ` : ''}{formatPercent(row.actual_percent_end)}</dd></div>
        <div><dt className="text-muted">移动</dt><dd>{row.delta_pp ? <DirectionText value={row.delta_pp} unit="pp" size="xs" /> : '—'}</dd></div>
        <div><dt className="text-muted">偏离</dt><dd>{row.deviation_pp ? <DirectionText value={row.deviation_pp} unit="pp" size="xs" /> : '—'}</dd></div>
        <div><dt className="text-muted">阈值</dt><dd className="pf-num text-secondary">{row.band && typeof row.band.threshold === 'string' ? formatValue(row.band.threshold, 'percent') : '—'}</dd></div>
        <div><dt className="text-muted">判定</dt><dd className="text-secondary">{stateLabel ?? (row.history === 'missing' ? '无史不判灯' : '—')}</dd></div>
      </div>
    </div>
  )
}

function ProposalDialog({ open, reviewId, onClose, onSaved }: {
  open: boolean
  reviewId: number
  onClose: () => void
  onSaved: () => void
}) {
  const [kind, setKind] = useState<ReviewProposalKind>('goal_version')
  const [subjectRef, setSubjectRef] = useState('')
  const [amount, setAmount] = useState('')
  const [date, setDate] = useState('')
  const [rate, setRate] = useState('')
  const [reason, setReason] = useState('')
  const [targetId, setTargetId] = useState('')
  const [threshold, setThreshold] = useState('')
  const [red, setRed] = useState('')
  const [relative, setRelative] = useState('')
  const [percent, setPercent] = useState('')
  const [text, setText] = useState('')
  const [rationale, setRationale] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    setKind('goal_version'); setSubjectRef(''); setAmount(''); setDate(''); setRate(''); setReason('')
    setTargetId(''); setThreshold(''); setRed(''); setRelative(''); setPercent(''); setText(''); setRationale(''); setError(null)
  }, [open])

  const needsTarget = kind === 'threshold' || kind === 'allocation_target'
  const targetsQuery = useQuery({
    queryKey: ['allocationTargets'],
    queryFn: () => api.getAllocationTargets(),
    enabled: open && needsTarget,
    staleTime: 60_000,
  })
  const classesQuery = useQuery({
    queryKey: ['assetClasses'],
    queryFn: api.getAssetClasses,
    enabled: open && needsTarget,
    staleTime: 60_000,
  })
  const institutionsQuery = useQuery({
    queryKey: ['institutions'],
    queryFn: api.getInstitutions,
    enabled: open && needsTarget,
    staleTime: 60_000,
  })
  const channelsQuery = useQuery({
    queryKey: ['channels'],
    queryFn: api.getChannels,
    enabled: open && needsTarget,
    staleTime: 60_000,
  })
  const targetLoading = targetsQuery.isLoading || classesQuery.isLoading
    || institutionsQuery.isLoading || channelsQuery.isLoading
  const targetError = targetsQuery.error ?? classesQuery.error
    ?? institutionsQuery.error ?? channelsQuery.error
  const targetOptions = useMemo(() => {
    const classNameById = collectAssetClassNames(classesQuery.data?.items ?? [])
    const institutionNames = new Map((institutionsQuery.data?.items ?? []).map((row) => [row.id, row.name]))
    const channelNames = new Map((channelsQuery.data?.items ?? []).map((row) => [row.id, row.name]))
    const nameOf = (dimension: string, targetId: string): string | undefined => {
      if (dimension === 'liquidity') return `${targetId} 天`
      if (dimension === 'asset_class') return classNameById.get(Number(targetId))
      if (dimension === 'institution') return institutionNames.get(Number(targetId))
      if (dimension === 'channel') return channelNames.get(Number(targetId))
      return targetId
    }
    return (targetsQuery.data ?? []).map((target) => ({
      value: String(target.id),
      label: `${nameOf(target.dimension, target.target_id) ?? `目标已删除（#${target.target_id}）`} · ${TARGET_DIMENSION_LABEL[target.dimension] ?? target.dimension} · #${target.id} · 目标 ${formatPercent(target.target_percent)}`,
    }))
  }, [targetsQuery.data, classesQuery.data, institutionsQuery.data, channelsQuery.data])
  const noTargets = needsTarget && !targetLoading && !targetError && targetOptions.length === 0

  const payload = useMemo((): Record<string, unknown> => {
    if (kind === 'narrative') return { text: text.trim() }
    if (kind === 'goal_version') return {
      target_amount: amount ? Number(amount).toFixed(2) : '',
      target_date: date,
      target_rate_annual: rate ? (Number(rate) / 100).toFixed(4) : null,
      reason: reason.trim(),
    }
    if (kind === 'threshold') return {
      allocation_target_id: targetId ? Number(targetId) : null,
      alert_threshold: threshold ? Number(threshold).toFixed(2) : null,
      alert_threshold_red: red ? Number(red).toFixed(2) : null,
      relative_threshold: relative ? Number(relative).toFixed(2) : null,
    }
    return { allocation_target_id: targetId ? Number(targetId) : null, target_percent: percent ? Number(percent).toFixed(2) : null }
  }, [kind, amount, date, rate, reason, targetId, threshold, red, relative, percent, text])

  const submit = async () => {
    if (!rationale.trim()) { setError('请填写提案理由'); return }
    if (kind === 'narrative' && !text.trim()) { setError('请填写文字结论'); return }
    if (kind === 'goal_version' && (!amount || !date || !reason.trim())) { setError('目标版本提案需填写金额、期限与理由'); return }
    if ((kind === 'threshold' || kind === 'allocation_target') && !targetId) { setError('请选择配置目标'); return }
    if (kind === 'threshold' && !threshold.trim()) { setError('阈值提案需填写黄阈值'); return }
    if (kind === 'allocation_target' && !percent.trim()) { setError('配置提案需填写目标占比'); return }
    setSaving(true); setError(null)
    try {
      const data: ReviewProposalInput = { kind, subject_ref: subjectRef.trim() || null, payload, rationale: rationale.trim() }
      await api.createReviewProposal(reviewId, data, newIdemKey())
      onSaved()
    } catch (e) { setError(reviewError(e)) } finally { setSaving(false) }
  }

  return (
    <ResponsiveDialog open={open} onClose={onClose} title="新建反哺提案" description="提案只记录建议；采纳不会自动写目标、阈值或账本。" size="md"
      footer={<div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
        <button type="button" onClick={onClose} disabled={saving} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
        <button type="button" onClick={submit} disabled={saving || (needsTarget && (targetLoading || noTargets))} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{saving ? '保存中…' : '创建提案'}</button>
      </div>}>
      <div className="space-y-4">
        {error && <InlineError>{error}</InlineError>}
        <SelectField label="提案类型" value={kind} onChange={(v) => { setKind(v as ReviewProposalKind); setError(null) }} options={Object.entries(PROPOSAL_KIND_LABEL).map(([value, label]) => ({ value, label }))} />
        <FormField label="对象引用（可选）" hint="goal_version 用 goal:<id>；阈值/配置用 allocation_target:<id>"><input type="text" value={subjectRef} onChange={(e) => setSubjectRef(e.target.value)} placeholder="如 goal:1" className={fieldControlClass} /></FormField>
        {kind === 'goal_version' && <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <FormField label="目标金额" required><input type="text" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className={fieldControlClass} placeholder="1000000.00" /></FormField>
          <DateField label="目标期限" required value={date} onChange={setDate} />
          <FormField label="目标年化（%）" hint="可选，按年化百分比填写"><input type="text" inputMode="decimal" value={rate} onChange={(e) => setRate(e.target.value)} className={fieldControlClass} placeholder="6.0" /></FormField>
          <FormField label="理由" required><input type="text" value={reason} onChange={(e) => setReason(e.target.value)} className={fieldControlClass} placeholder="复盘：延长一年" /></FormField>
        </div>}
        {(kind === 'threshold' || kind === 'allocation_target') && <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          {targetError ? <InlineError>{reviewError(targetError)}</InlineError> : targetLoading ? <LoadingState rows={2} compact /> : noTargets ? (
            <p className="rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
              还没有配置目标。请先到 <Link href="/allocation" className="underline">规划 → 目标配置</Link> 新增配置目标后再提交。
            </p>
          ) : (
            <SelectField label="配置目标" required value={targetId} onChange={setTargetId} placeholder="选择配置目标" options={targetOptions} hint="显示目标名 · 维度 · 行 id；仅记录提案，不写入配置" />
          )}
          {kind === 'threshold' ? <>
            <FormField label="黄阈值" required><input type="text" inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} className={fieldControlClass} placeholder="5.00" /></FormField>
            <FormField label="红阈值（可选）"><input type="text" inputMode="decimal" value={red} onChange={(e) => setRed(e.target.value)} className={fieldControlClass} placeholder="10.00" /></FormField>
            <FormField label="相对阈值（可选）"><input type="text" inputMode="decimal" value={relative} onChange={(e) => setRelative(e.target.value)} className={fieldControlClass} placeholder="25.00" /></FormField>
          </> : <FormField label="目标占比（%）" required><input type="text" inputMode="decimal" value={percent} onChange={(e) => setPercent(e.target.value)} className={fieldControlClass} placeholder="20.00" /></FormField>}
        </div>}
        {kind === 'narrative' && <FormField label="文字结论" required><textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} className={cn(fieldControlClass, 'min-h-[100px] resize-none py-2')} /></FormField>}
        <FormField label="提案理由" required><textarea value={rationale} onChange={(e) => setRationale(e.target.value)} rows={3} className={cn(fieldControlClass, 'min-h-[80px] resize-none py-2')} placeholder="说明为什么提出这个调整" /></FormField>
        <p className="text-meta text-muted">相同内容的待裁决提案会被服务端以 REVIEW_PROPOSAL_DUPLICATE 拒绝，并提示重复。</p>
      </div>
    </ResponsiveDialog>
  )
}

function ProposalDecisionDialog({ open, proposal, mode, reviewId, onClose, onSaved }: {
  open: boolean
  proposal: ReviewProposal | null
  mode: 'adopt' | 'dismiss'
  reviewId: number
  onClose: () => void
  onSaved: () => void
}) {
  const [reason, setReason] = useState('')
  const [appliedKind, setAppliedKind] = useState('')
  const [appliedRef, setAppliedRef] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => { if (open) { setReason(''); setAppliedKind(''); setAppliedRef(''); setError(null) } }, [open])
  const cannotAdopt = mode === 'adopt' && proposal?.kind === 'narrative'
  const submit = async () => {
    if (!proposal) return
    if (cannotAdopt) { setError('文字结论提案不可采纳，只能驳回或保留。'); return }
    if (mode === 'dismiss' && !reason.trim()) { setError('驳回需要填写理由'); return }
    setSaving(true); setError(null)
    try {
      if (mode === 'adopt') await api.adoptReviewProposal(reviewId, proposal.id, { reason: reason.trim() || null, applied_kind: appliedKind || null, applied_ref: appliedRef.trim() || null }, newIdemKey())
      else await api.dismissReviewProposal(reviewId, proposal.id, reason.trim(), newIdemKey())
      onSaved()
    } catch (e) { setError(reviewError(e)) } finally { setSaving(false) }
  }
  return <ResponsiveDialog open={open} onClose={onClose} title={mode === 'adopt' ? '采纳提案' : '驳回提案'} description={mode === 'adopt' ? '采纳只记录决定，不会写目标、阈值或账本。' : '驳回需要填写理由。'} size="sm"
    footer={<div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end"><button type="button" onClick={onClose} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink">取消</button><button type="button" onClick={submit} disabled={saving || cannotAdopt} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{saving ? '提交中…' : mode === 'adopt' ? '确认采纳' : '确认驳回'}</button></div>}>
    <div className="space-y-4">
      {error && <InlineError>{error}</InlineError>}
      {cannotAdopt && <InlineError>文字结论提案不可采纳，只能驳回或保留。</InlineError>}
      {mode === 'adopt' && <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
        <SelectField label="关联动作（可选）" value={appliedKind} onChange={setAppliedKind} placeholder="不关联" options={[{ value: 'goal_version', label: '目标版本' }, { value: 'allocation_target', label: '配置目标' }, { value: 'threshold', label: '阈值' }]} />
        <FormField label="关联引用（可选）"><input type="text" value={appliedRef} onChange={(e) => setAppliedRef(e.target.value)} className={fieldControlClass} placeholder="如版本号/行 id" /></FormField>
      </div>}
      <FormField label={mode === 'dismiss' ? '驳回理由' : '采纳理由（可选）'} required={mode === 'dismiss'}><textarea value={reason} onChange={(e) => { setReason(e.target.value); setError(null) }} rows={3} className={cn(fieldControlClass, 'min-h-[90px] resize-none py-2')} /></FormField>
    </div>
  </ResponsiveDialog>
}

export default function ReviewDetailPage() {
  const params = useParams()
  const id = Number(params.id)
  const queryClient = useQueryClient()
  const [attribution, setAttribution] = useState<Record<string, { checked: boolean; note: string }>>({})
  const [conclusion, setConclusion] = useState('')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [proposalOpen, setProposalOpen] = useState(false)
  const [decideProposal, setDecideProposal] = useState<{ proposal: ReviewProposal; mode: 'adopt' | 'dismiss' } | null>(null)
  const [closeConfirm, setCloseConfirm] = useState(false)
  const [closeError, setCloseError] = useState<string | null>(null)

  const detailQuery = useQuery({ queryKey: ['reviewDetail', id], queryFn: () => api.getReview(id), enabled: Number.isFinite(id), retry: false })
  const detail = detailQuery.data
  const review = detail?.review
  const isOpen = review?.status === 'open'

  const proposalTargetIds = useMemo(
    () => (detail?.proposals ?? []).map(proposalTargetId).filter((value): value is number => value !== null),
    [detail],
  )
  const proposalTargetsQuery = useQuery({
    queryKey: ['allocationTargets'],
    queryFn: () => api.getAllocationTargets(),
    enabled: proposalTargetIds.length > 0,
    staleTime: 60_000,
  })
  const existingTargetIds = useMemo(
    () => new Set((proposalTargetsQuery.data ?? []).map((target) => target.id)),
    [proposalTargetsQuery.data],
  )

  useEffect(() => {
    if (!review) return
    const next: Record<string, { checked: boolean; note: string }> = {}
    ATTRIBUTION_OPTIONS.forEach((o) => { next[o.value] = { checked: false, note: '' } })
    review.attribution.forEach((a) => { next[a.category] = { checked: true, note: a.note ?? '' } })
    setAttribution(next)
    setConclusion(review.conclusion ?? '')
  }, [review])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['reviews'] })
    queryClient.invalidateQueries({ queryKey: ['reviewDetail', id] })
  }
  const saveMutation = useMutation({
    mutationFn: () => {
      const attributionPayload: ReviewAttribution[] = ATTRIBUTION_OPTIONS.filter((o) => attribution[o.value]?.checked).map((o) => ({
        category: o.value as ReviewAttributionCategory,
        subject_kind: 'review',
        subject_ref: null,
        note: attribution[o.value]?.note.trim() || null,
      }))
      return api.updateReviewConclusion(id, { attribution: attributionPayload, conclusion: conclusion.trim() || null }, newIdemKey())
    },
    onSuccess: () => { invalidate(); setSaveError(null) },
    onError: (e) => setSaveError(reviewError(e)),
  })
  const closeMutation = useMutation({
    mutationFn: () => api.closeReview(id, conclusion.trim(), newIdemKey()),
    onSuccess: () => { invalidate(); setCloseConfirm(false); setCloseError(null) },
    onError: (e) => { setCloseConfirm(false); setCloseError(reviewError(e)) },
  })

  if (!Number.isFinite(id)) return <PageContainer width="wide"><ErrorState title="复盘地址无效" description="请从复盘列表进入。" /></PageContainer>

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader back={{ href: '/planning/reviews', label: '返回复盘列表' }} title={review?.label || review?.goal_name || '复盘详情'} description={review ? `${review.goal_name ?? `目标 #${review.goal_id}`} · ${review.as_of}` : undefined} />
      {detailQuery.isLoading ? <LoadingState rows={5} /> : detailQuery.isError ? <ErrorState title="复盘加载失败" description={reviewError(detailQuery.error)} onRetry={() => detailQuery.refetch()} /> : detail && review ? (
        <div className="space-y-4">
          <Card padding="md" className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn('rounded-pill px-2 py-0.5 text-meta', REVIEW_STATUS_CLASS[review.status])}>{REVIEW_STATUS_LABEL[review.status]}</span>
              <span className="text-meta text-muted">目标版本 v{review.version_no ?? '—'} · 冻结 {formatDateTime(review.created_at)}</span>
              {review.closed_at && <span className="text-meta text-muted">关闭 {formatDateTime(review.closed_at)}</span>}
              <span className="ml-auto text-meta text-muted">快照 {review.snapshot_sha256.slice(0, 12)}…</span>
            </div>
            {review.note && <p className="rounded-control bg-subtle px-3 py-2 text-meta text-secondary">{review.note}</p>}
            <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
              <AnchorCard title="起点锚" anchor={detail.snapshot.anchors.start} start />
              <AnchorCard title="终点锚" anchor={detail.snapshot.anchors.end} />
            </div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-control bg-subtle p-3 text-meta">
              <span className="text-secondary">净值变化 {detail.snapshot.delta.net_worth_change ? <DirectionText value={detail.snapshot.delta.net_worth_change} unit="cny" size="xs" /> : '不可比'}</span>
              <span className="text-secondary">达成度 Δ {detail.snapshot.delta.progress_delta ? <DirectionText value={detail.snapshot.delta.progress_delta} unit="ratio" size="xs" /> : '不可比'}</span>
              <span className="text-muted">{detail.snapshot.delta.comparable ? '两锚同目标版本，可比' : `不可比：${detail.snapshot.delta.not_comparable_reason ?? '版本变化'}`}</span>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-2 text-meta text-muted">
              <span className="inline-flex items-center gap-1 font-medium text-secondary"><History size={13} aria-hidden /> 时间线</span>
              <span>起点 {review.start_as_of ?? '—'}</span>
              {detail.suggestions.filter((s) => s.accepted_at).map((s) => <span key={`t-${s.suggestion_id}`}>建议采纳 {formatDate(s.accepted_at)}</span>)}
              <span>终点 {review.as_of}</span>
              <span>{review.closed_at ? `关闭 ${formatDate(review.closed_at)}` : `创建 ${formatDate(review.created_at)}`}</span>
            </div>
            {detail.snapshot.goal.version_changed && <p className="rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">起点与终点之间目标版本发生变化；达成度变化按不可比展示。</p>}
            {detail.snapshot.degradations.some((d) => d.code === 'REVIEW_BAND_HISTORY_MISSING') && <p className="rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">REVIEW_BAND_HISTORY_MISSING：该时点早于首条阈值史，相关配置行不判灯（不回退当期尺）。</p>}
            <div className="flex flex-wrap gap-2 text-meta text-muted"><span>阈值基准 {detail.snapshot.allocation.band_basis}</span><span>·</span><span>历史自 {detail.snapshot.allocation.history_from ?? '—'}</span><span>·</span><span>{detail.snapshot.allocation.rows.length} 个配置行</span></div>
          </Card>

          <Card padding="md">
            <h2 className="text-h3 font-semibold text-ink">配置移动</h2>
            {detail.snapshot.allocation.rows.length === 0 ? <EmptyState title="无配置行" compact /> : <div className="mt-3 grid grid-cols-1 gap-2 tablet-l:grid-cols-2">{detail.snapshot.allocation.rows.map((row) => <AllocationRow key={`${row.dimension}-${row.target_ref}`} row={row} />)}</div>}
          </Card>

          <Card padding="md">
            <h2 className="text-h3 font-semibold text-ink">建议批次</h2>
            {detail.suggestions.length === 0 ? <p className="mt-2 text-meta text-muted">本次未纳入已采纳建议。</p> : <ul className="mt-3 divide-y divide-line">{detail.suggestions.map((s) => <li key={s.suggestion_id} className="flex flex-wrap items-center justify-between gap-2 py-2"><div><Link href={`/planning/advice/${s.suggestion_id}`} className="text-body font-medium text-link">{s.title ?? `建议 #${s.suggestion_id}`}</Link><p className="text-meta text-muted">{s.horizon} · 信息集 {s.info_set?.id ?? '—'} · as-of {s.info_set?.as_of ?? '—'}{s.info_set?.payload_sha256 ? ` · ${s.info_set.payload_sha256.slice(0, 12)}…` : ''}</p></div><span className="text-meta text-muted">{s.execution?.status ? `执行 ${s.execution.status}` : '未记录执行'}</span></li>)}</ul>}
          </Card>

          <Card padding="md">
            <h2 className="flex items-center gap-2 text-h3 font-semibold text-ink"><History size={16} aria-hidden /> 归因与结论</h2>
            {isOpen ? <div className="mt-3 space-y-4">
              {saveError && <InlineError>{saveError}</InlineError>}
              <div className="grid grid-cols-1 gap-2 tablet:grid-cols-2">
                {ATTRIBUTION_OPTIONS.map((o) => <div key={o.value} className="rounded-control border border-line p-2">
                  <label className="flex min-h-[44px] items-center gap-2 text-body text-secondary"><input type="checkbox" checked={attribution[o.value]?.checked ?? false} onChange={(e) => { saveMutation.reset(); setAttribution((prev) => ({ ...prev, [o.value]: { ...prev[o.value], checked: e.target.checked } })) }} className="h-4 w-4 accent-[var(--pf-color-action-primary)]" />{o.label}</label>
                  {attribution[o.value]?.checked && <input type="text" value={attribution[o.value]?.note ?? ''} onChange={(e) => { saveMutation.reset(); setAttribution((prev) => ({ ...prev, [o.value]: { ...prev[o.value], note: e.target.value } })) }} placeholder="补充说明（可选）" className={cn(fieldControlClass, 'mt-1 min-h-[40px] text-meta')} />}
                </div>)}
              </div>
              <FormField label="结论（关闭必填）"><textarea value={conclusion} onChange={(e) => { saveMutation.reset(); setConclusion(e.target.value) }} rows={4} placeholder="如：目标未变，执行滞后；配置仅移动 3pp。" className={cn(fieldControlClass, 'min-h-[100px] resize-none py-2')} /></FormField>
              <div className="flex flex-wrap gap-2">
                <button type="button" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-50"><Save size={14} aria-hidden /> {saveMutation.isPending ? '保存中…' : '保存归因与结论'}</button>
                {saveMutation.isSuccess && <span className="inline-flex min-h-[44px] items-center text-meta text-status-good-text">已保存</span>}
                <button type="button" onClick={() => { if (!conclusion.trim()) { setCloseError('关闭复盘必须填写结论'); return } setCloseError(null); setCloseConfirm(true) }} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-3 text-meta font-medium text-white"><CheckCircle2 size={14} aria-hidden /> 关闭复盘</button>
              </div>
              {closeError && <InlineError>{closeError}</InlineError>}
            </div> : <div className="mt-3 space-y-2 text-body text-secondary">
              {review.attribution.length > 0 ? <div className="flex flex-wrap gap-2">{review.attribution.map((a) => <span key={a.category} className="rounded-pill bg-subtle px-2 py-1 text-meta text-secondary">{ATTRIBUTION_LABEL[a.category]}{a.note ? ` · ${a.note}` : ''}</span>)}</div> : <p className="text-meta text-muted">未填写归因。</p>}
              <p className="whitespace-pre-wrap">{review.conclusion || '未填写结论。'}</p>
              <p className="text-meta text-muted">复盘已关闭，归因与结论只读。</p>
            </div>}
          </Card>

          <Card padding="md">
            <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-h3 font-semibold text-ink">反哺提案</h2>{isOpen && <button type="button" onClick={() => setProposalOpen(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink"><Plus size={14} aria-hidden /> 新建提案</button>}</div>
            {detail.proposals.length === 0 ? <p className="mt-3 text-meta text-muted">还没有提案。提案只记录建议，采纳不会写目标/阈值。</p> : <ul className="mt-3 space-y-2">{detail.proposals.map((p) => {
              const targetId = proposalTargetId(p)
              const targetChecking = targetId !== null && proposalTargetsQuery.isLoading
              const targetMissing = targetId !== null && proposalTargetsQuery.isSuccess && !existingTargetIds.has(targetId)
              const targetCheckFailed = targetId !== null && proposalTargetsQuery.isError
              const adoptBlocked = targetChecking || targetMissing || targetCheckFailed
              return <li key={p.id} className="rounded-control border border-line p-3">
                <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-2"><span className="text-body-strong text-ink">{PROPOSAL_KIND_LABEL[p.kind]}</span><span className={cn('rounded-pill px-2 py-0.5 text-meta', PROPOSAL_STATUS_CLASS[p.status])}>{PROPOSAL_STATUS_LABEL[p.status]}</span></div><span className="text-meta text-muted">{p.payload_sha256.slice(0, 12)}…</span></div>
                <p className="mt-1 text-meta text-secondary">{p.rationale}</p>
                <pre className="mt-2 max-h-36 overflow-auto rounded bg-subtle p-2 text-[12px] leading-5 text-secondary">{JSON.stringify(p.payload, null, 2)}</pre>
                {targetMissing && <p className="mt-1 rounded-control bg-status-critical-surface px-2 py-1 text-meta text-status-critical-text">目标已删除（#{targetId}）</p>}
                {targetCheckFailed && <p className="mt-1 text-meta text-status-critical-text">配置目标校验失败，暂不能采纳。</p>}
                {p.decision_note && <p className="mt-1 text-meta text-muted">裁决备注：{p.decision_note}{p.applied_kind ? ` · 关联 ${p.applied_kind} ${p.applied_ref ?? ''}` : ''}</p>}
                {p.status === 'proposed' && <div className="mt-2 flex flex-wrap gap-2">{p.kind !== 'narrative' && <button type="button" onClick={() => setDecideProposal({ proposal: p, mode: 'adopt' })} disabled={adoptBlocked} title={targetMissing ? `目标已删除（#${targetId}）` : targetChecking ? '正在校验配置目标' : targetCheckFailed ? '配置目标校验失败' : undefined} className="inline-flex min-h-[44px] items-center gap-1 rounded-control border border-status-good/30 bg-status-good-surface px-3 text-meta text-status-good-text disabled:cursor-not-allowed disabled:opacity-40"><CheckCircle2 size={14} aria-hidden /> 采纳</button>}<button type="button" onClick={() => setDecideProposal({ proposal: p, mode: 'dismiss' })} className="inline-flex min-h-[44px] items-center gap-1 rounded-control border border-status-critical/30 bg-surface px-3 text-meta text-status-critical-text"><XCircle size={14} aria-hidden /> 驳回</button></div>}
              </li>
            })}</ul>}
          </Card>

          {(detail.snapshot.signals?.events?.length > 0 || detail.snapshot.signals?.domains?.length > 0) && <Card padding="md">
            <h2 className="text-h3 font-semibold text-ink">信号快照</h2>
            <p className="mt-1 text-meta text-muted">信号为复盘时点重算（规则未版本化）· {detail.snapshot.signals.events.length} 个事件</p>
            {detail.snapshot.signals.domains.length > 0 && <div className="mt-3 flex flex-wrap gap-2">{detail.snapshot.signals.domains.map((d) => <span key={d.domain} className="inline-flex items-center gap-1.5 rounded-pill bg-subtle px-2 py-1 text-meta text-secondary"><GradeDot state={d.state as MetricGradeState} />{d.domain} · {signalStateLabel(d.state) ?? d.state}</span>)}</div>}
            {detail.snapshot.signals.events.length > 0 && <ul className="mt-3 divide-y divide-line">{detail.snapshot.signals.events.map((e, index) => <li key={`${e.rule_id ?? e.id ?? 'event'}-${e.period ?? ''}-${index}`} className="flex flex-wrap items-center justify-between gap-2 py-2 text-meta text-secondary">
              <span className="inline-flex items-center gap-1.5">{signalStateLabel(e.severity) && <GradeDot state={e.severity as MetricGradeState} />}{e.rule_name ?? (e.rule_id ? `规则 #${e.rule_id}` : '信号事件')}{e.domain ? ` · ${e.domain}` : ''}</span>
              <span className="text-muted">{e.period ?? '—'} · {EVENT_STATUS_LABEL[e.status ?? ''] ?? e.status ?? '—'}</span>
            </li>)}</ul>}
          </Card>}
          {detail.snapshot.degradations.length > 0 && <div className="space-y-1">{detail.snapshot.degradations.map((d) => <p key={d.code} className="text-meta text-muted">{d.code}：{d.message}</p>)}</div>}
        </div>
      ) : null}
      <ProposalDialog open={proposalOpen} reviewId={id} onClose={() => setProposalOpen(false)} onSaved={() => { setProposalOpen(false); invalidate() }} />
      <ProposalDecisionDialog open={!!decideProposal} proposal={decideProposal?.proposal ?? null} mode={decideProposal?.mode ?? 'adopt'} reviewId={id} onClose={() => setDecideProposal(null)} onSaved={() => { setDecideProposal(null); invalidate() }} />
      <ConfirmDialog open={closeConfirm} onClose={() => setCloseConfirm(false)} onConfirm={() => closeMutation.mutate()} title="关闭这次复盘？" description="关闭后归因、结论与快照只读；提案仍可单独裁决。此操作不可撤销。" confirmLabel="关闭复盘" danger loading={closeMutation.isPending} />
    </PageContainer>
  )
}
