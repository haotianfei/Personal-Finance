'use client'

import { useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarRange, ChevronRight, ClipboardList, Plus } from 'lucide-react'
import Link from 'next/link'
import { api, newIdemKey } from '@/lib/api'
import { formatDateTime, formatValue } from '@/lib/format'
import type { ReviewSummary } from '@/types'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, EmptyState, ErrorState, InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import { DateField, FormField, SelectField, fieldControlClass } from '@/components/form'
import { DirectionText } from '@/components/ui/DirectionText'
import { REVIEW_STATUS_CLASS, REVIEW_STATUS_LABEL, REVIEW_STATUS_OPTIONS, reviewError } from '@/components/reviews/shared'
import { cn } from '@/lib/utils'

function CreateReviewDialog({ open, onClose, onCreated }: {
  open: boolean
  onClose: () => void
  onCreated: (id: number) => void
}) {
  const [goalId, setGoalId] = useState('')
  const [asOf, setAsOf] = useState('')
  const [startAsOf, setStartAsOf] = useState('')
  const [label, setLabel] = useState('')
  const [note, setNote] = useState('')
  const [selected, setSelected] = useState<number[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const goalsQuery = useQuery({ queryKey: ['goals', true], queryFn: () => api.getGoals(true), enabled: open })
  const datesQuery = useQuery({ queryKey: ['assetDates'], queryFn: api.getDates, enabled: open })
  const selectedGoal = goalsQuery.data?.items.find((g) => String(g.id) === goalId)
  const goalQuery = useQuery({
    queryKey: ['goalDetail', selectedGoal?.id],
    queryFn: () => api.getGoal(selectedGoal!.id),
    enabled: open && !!selectedGoal,
  })
  const candidatesQuery = useQuery({
    queryKey: ['reviewCandidates', goalId, asOf, startAsOf],
    queryFn: () => api.getReviewCandidates({
      goal_id: Number(goalId),
      as_of: asOf || undefined,
      start_as_of: startAsOf || undefined,
    }),
    enabled: open && !!goalId && !!asOf,
  })

  useEffect(() => {
    if (!open) return
    setGoalId('')
    setAsOf('')
    setStartAsOf('')
    setLabel('')
    setNote('')
    setSelected([])
    setError(null)
  }, [open])

  useEffect(() => {
    setSelected([])
  }, [goalId, asOf, startAsOf])

  useEffect(() => {
    if (!open || asOf || !datesQuery.data?.length) return
    const latest = [...datesQuery.data].sort().at(-1)
    if (latest) setAsOf(latest)
  }, [open, asOf, datesQuery.data])

  const topVersions = useMemo(
    () => (goalQuery.data?.versions ?? []).filter((v) => v.status !== 'draft'),
    [goalQuery.data],
  )
  const goalOptions = (goalsQuery.data?.items ?? []).map((g) => ({
    value: String(g.id),
    label: `${g.name}${g.status === 'archived' ? '（已归档，不可创建）' : ''}`,
    disabled: g.status === 'archived',
  }))

  const submit = async () => {
    if (!goalId) { setError('请选择目标'); return }
    if (!asOf) { setError('请选择观察终点'); return }
    setSaving(true)
    setError(null)
    try {
      const result = await api.createReview({
        goal_id: Number(goalId),
        as_of: asOf,
        start_as_of: startAsOf || null,
        suggestion_ids: selected,
        label: label.trim() || null,
        note: note.trim() || null,
      }, newIdemKey())
      onCreated(result.review.id)
    } catch (e) {
      setError(reviewError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="新建复盘"
      description="创建即冻结快照；之后目标和阈值的历史变化不会改写这份复盘。"
      size="lg"
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={saving} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={saving || !goalId || !asOf} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {saving ? '冻结中…' : '创建并冻结'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        {error && <InlineError>{error}</InlineError>}
        {goalsQuery.isLoading ? <LoadingState rows={2} /> : goalsQuery.data?.items.length === 0 ? (
          <InlineError>还没有可复盘的目标。请先在「规划 → 目标与达成度」创建目标。</InlineError>
        ) : (
          <>
            <SelectField label="目标" required value={goalId} onChange={(v) => { setGoalId(v); setSelected([]); setError(null) }} placeholder="选择目标" options={goalOptions} />
            <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
              <DateField label="观察终点（as-of）" required value={asOf} onChange={setAsOf} hint="服务端按该日解析目标版本；草案版本不参与" />
              <DateField label="起点（可选）" value={startAsOf} onChange={setStartAsOf} hint="留空则由建议批次或上一期复盘推导" />
            </div>
            {topVersions.length > 0 && (
              <div className="rounded-control border border-line bg-subtle p-3">
                <p className="mb-2 flex items-center gap-1.5 text-meta text-secondary"><CalendarRange size={14} aria-hidden /> 该目标版本（不含草案）</p>
                <div className="flex flex-wrap gap-2">
                  {topVersions.map((v) => (
                    <button key={v.id} type="button" onClick={() => setAsOf(v.effective_from ?? asOf)} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-2 text-meta text-ink">
                      v{v.version_no} · {v.status === 'active' ? '已生效' : '已归档'} · {v.effective_from ?? '—'}
                    </button>
                  ))}
                </div>
                <p className="mt-1 text-meta text-muted">点击版本可把 as-of 设为生效日；最终版本由服务端按 as-of 重新解析。</p>
              </div>
            )}
            <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
              <FormField label="标签（可选）"><input type="text" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="如：2026 Q3 复盘" className={fieldControlClass} /></FormField>
              <FormField label="备注（可选）"><input type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="如：季度固定" className={fieldControlClass} /></FormField>
            </div>
            <div>
              <p className="mb-2 text-meta font-medium text-secondary">候选建议批次（可选）</p>
              {!goalId || !asOf ? <p className="text-meta text-muted">选择目标和终点后加载候选建议。</p> : candidatesQuery.isLoading ? <LoadingState rows={2} compact /> : candidatesQuery.isError ? <InlineError>{reviewError(candidatesQuery.error)}</InlineError> : (candidatesQuery.data?.items.length ?? 0) === 0 ? <p className="text-meta text-muted">该窗口没有已采纳且未被复用的建议。</p> : (
                <ul className="divide-y divide-line rounded-control border border-line">
                  {candidatesQuery.data!.items.map((candidate) => (
                    <li key={candidate.suggestion_id} className="flex items-start gap-3 px-3 py-2">
                      <input type="checkbox" checked={selected.includes(candidate.suggestion_id)} onChange={() => setSelected((prev) => prev.includes(candidate.suggestion_id) ? prev.filter((id) => id !== candidate.suggestion_id) : [...prev, candidate.suggestion_id])} className="mt-1 h-4 w-4 accent-[var(--pf-color-action-primary)]" aria-label={`纳入建议${candidate.title ?? candidate.suggestion_id}`} />
                      <div className="min-w-0">
                        <p className="text-body text-ink">{candidate.title ?? `建议 #${candidate.suggestion_id}`}</p>
                        <p className="text-meta text-muted">{candidate.horizon} · 信息集 as-of {candidate.info_set?.as_of ?? '—'}{candidate.execution?.status ? ` · 执行 ${candidate.execution.status}` : ''}</p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <p className="text-meta text-status-warn-text">若观察终点早于首条阈值史，详情会显示 REVIEW_BAND_HISTORY_MISSING；相关配置行不判灯。</p>
          </>
        )}
      </div>
    </ResponsiveDialog>
  )
}

function ReviewCard({ review }: { review: ReviewSummary }) {
  const batchLabel = (review.suggestion_batch_size ?? 0) > 0
    ? `${review.suggestion_batch_size} 条`
    : '未纳入建议'
  return (
    <Card as="article" padding="md" className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-h3 font-semibold text-ink">{review.label || review.goal_name || `复盘 #${review.id}`}</h2>
            <span className={cn('rounded-pill px-2 py-0.5 text-meta', REVIEW_STATUS_CLASS[review.status])}>{REVIEW_STATUS_LABEL[review.status]}</span>
            {review.band_history_missing && <span className="rounded-pill bg-status-warn-surface px-2 py-0.5 text-meta text-status-warn-text">无阈值史</span>}
          </div>
          <p className="mt-1 text-meta text-muted">{review.goal_name ?? `目标 #${review.goal_id}`} · v{review.version_no ?? '—'} · {review.as_of}{review.start_as_of ? ` ← ${review.start_as_of}` : ''}</p>
        </div>
        <Link href={`/planning/reviews/${review.id}`} className="inline-flex min-h-[44px] shrink-0 items-center gap-1 rounded-control px-2 text-meta text-link">详情 <ChevronRight size={14} aria-hidden /></Link>
      </div>
      <div className="grid grid-cols-2 gap-2 text-meta tablet:grid-cols-3">
        <div><p className="text-muted">终点达成度</p><p className="pf-num text-body-strong text-ink">{review.progress_end != null ? formatValue(review.progress_end, 'ratio') : '—'}</p></div>
        <div><p className="text-muted">进展 Δ</p>{review.comparable && review.progress_delta != null ? <DirectionText value={review.progress_delta} unit="ratio" size="xs" /> : <p className="text-muted">{review.comparable === false ? '不可比' : '—'}</p>}</div>
        <div><p className="text-muted">提案</p><p className="pf-num text-body-strong text-ink">{review.proposal_counts?.proposed ?? 0} 待裁决 · {review.proposal_counts?.adopted ?? 0} 已采纳</p></div>
      </div>
      <div className="rounded-control bg-subtle px-3 py-2 text-meta text-secondary">
        <p><span className="text-muted">建议批次：</span>{batchLabel}</p>
        {review.conclusion_excerpt && <p className="mt-1 line-clamp-2"><span className="text-muted">结论：</span>{review.conclusion_excerpt}</p>}
        {review.band_history_missing && <p className="mt-1 text-status-warn-text">信息集缺阈值史：该时点早于首条阈值史，配置行不判灯。</p>}
      </div>
      {review.status === 'closed' && <p className="text-meta text-muted">已关闭：归因/结论只读，提案仍可单独裁决。</p>}
      <p className="text-meta text-muted">{review.closed_at ? `关闭 ${formatDateTime(review.closed_at)}` : `创建 ${formatDateTime(review.created_at)}`}</p>
    </Card>
  )
}

export default function ReviewsPage() {
  const router = useRouter()
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)

  const listQuery = useQuery({
    queryKey: ['reviews', status, page],
    queryFn: () => api.getReviews({ status: status || undefined, page, page_size: 10 }),
  })
  const items = listQuery.data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((listQuery.data?.total ?? 0) / (listQuery.data?.page_size ?? 10)))

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader
        title="复盘"
        description="冻结某一时点的目标、阈值与建议材料，回看偏差并形成可审阅的提案；采纳只记决定，不改目标或账本。"
        actions={
          <>
            <Link href="/planning/advice" className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <ClipboardList size={15} aria-hidden /> 建议收件箱
            </Link>
            <button type="button" onClick={() => setCreateOpen(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
              <Plus size={16} aria-hidden /> 新建复盘
            </button>
          </>
        }
      />
      <Card padding="sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="review-status" className="text-meta text-secondary">状态</label>
            <select id="review-status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }} className="min-h-[44px] rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              {REVIEW_STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <span className="pf-num text-meta text-muted">共 {listQuery.data?.total ?? 0} 条</span>
          </div>
          <button type="button" onClick={() => listQuery.refetch()} className="inline-flex min-h-[44px] items-center rounded-control px-2 text-meta text-secondary">刷新</button>
        </div>
      </Card>

      {listQuery.isLoading ? <LoadingState rows={3} /> : listQuery.isError ? (
        <ErrorState title="复盘列表加载失败" description={reviewError(listQuery.error)} onRetry={() => listQuery.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={ClipboardList}
          title={status ? '当前筛选下没有复盘' : '还没有复盘'}
          description={status ? '切换状态，或新建一次复盘。' : '选择一个目标与观察终点，冻结当时的判据与建议材料，开始复盘。'}
          action={<button type="button" onClick={() => setCreateOpen(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong text-white"><Plus size={16} aria-hidden /> 新建复盘</button>}
        />
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">{items.map((review) => <ReviewCard key={review.id} review={review} />)}</div>
          {totalPages > 1 && <div className="flex items-center justify-between gap-2">
            <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-40">上一页</button>
            <span className="pf-num text-meta text-muted">{page} / {totalPages}</span>
            <button type="button" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-40">下一页</button>
          </div>}
        </div>
      )}

      <CreateReviewDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(id) => {
          setCreateOpen(false)
          queryClient.invalidateQueries({ queryKey: ['reviews'] })
          router.push(`/planning/reviews/${id}`)
        }}
      />
    </PageContainer>
  )
}