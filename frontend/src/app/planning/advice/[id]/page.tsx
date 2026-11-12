'use client'

import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'next/navigation'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Clock3, History, Info, Loader2, XCircle } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { formatDate, formatDateTime, formatValue } from '@/lib/format'
import type { AdvisorExecutionStatus, AdvisorInfoEntry } from '@/types'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, EmptyState, ErrorState, InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import { FormField, SelectField, fieldControlClass } from '@/components/form'
import {
  ADVICE_STATUS_CLASS, ADVICE_STATUS_LABEL, adviceError, gateCheckText, horizonLabel,
} from '@/components/advice/shared'
import { cn } from '@/lib/utils'

const DIRECTION_LABEL: Record<string, string> = {
  increase: '增加', decrease: '减少', hold: '保持', rebalance: '再平衡',
}

const GROUPS: Array<{ key: string; label: string }> = [
  { key: 'meta', label: '元信息' },
  { key: 'nw', label: '净值' },
  { key: 'coverage', label: '覆盖度' },
  { key: 'appreciation', label: '升值' },
  { key: 'returns', label: '收益' },
  { key: 'goal', label: '目标' },
  { key: 'alloc', label: '配置' },
  { key: 'signal', label: '信号' },
]

function groupOf(key: string): string {
  return key.startsWith('alloc.') ? 'alloc' : key.split('.')[0]
}

function InfoEntryRow({ entry }: { entry: AdvisorInfoEntry }) {
  return (
    <li className="px-3 py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-body text-ink">{entry.label || entry.key}</span>
        <span className="pf-num text-body-strong text-ink">{formatValue(entry.value, entry.unit)}</span>
      </div>
      <p className="mt-0.5 break-all text-meta text-muted">{entry.key} · {entry.trust}{entry.source ? ` · ${entry.source}` : ''}{entry.as_of ? ` · ${formatDate(entry.as_of)}` : ''}</p>
    </li>
  )
}

function InfoSetPanel({ suggestionId }: { suggestionId: number }) {
  const infoQuery = useQuery({
    queryKey: ['adviceInfoSet', suggestionId],
    queryFn: () => api.getAdviceInfoSet(suggestionId),
    retry: false,
  })
  const entries = infoQuery.data?.entries ?? []
  const shown = entries.slice(0, 40)
  const grouped = useMemo(() => {
    const map = new Map<string, AdvisorInfoEntry[]>()
    shown.forEach((entry) => {
      const key = groupOf(entry.key)
      map.set(key, [...(map.get(key) ?? []), entry])
    })
    return map
  }, [shown])

  if (infoQuery.isLoading) return <LoadingState rows={4} compact />
  if (infoQuery.isError) return <InlineError>信息集读取失败：{adviceError(infoQuery.error)}</InlineError>
  if (!infoQuery.data) return null
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-meta text-muted">
        <span className="inline-flex items-center gap-1"><Clock3 size={14} aria-hidden /> as-of {infoQuery.data.as_of ? formatDate(infoQuery.data.as_of) : '—'}</span>
        <span className="pf-num">共 {infoQuery.data.entry_count} 条</span>
        <span>{infoQuery.data.builder_version}</span>
        {!infoQuery.data.payload_ok && <span className="text-status-critical-text">校验失败</span>}
      </div>
      {entries.length === 0 ? <EmptyState title="信息集为空" compact /> : (
        <>
          {shown.length < entries.length && <p className="text-meta text-status-warn-text">仅显示前 40 条（共 {entries.length} 条）。</p>}
          <div className="space-y-3">
            {GROUPS.filter((group) => grouped.has(group.key)).map((group) => (
              <details key={group.key} className="rounded-control border border-line" open={group.key === 'goal' || group.key === 'nw'}>
                <summary className="cursor-pointer px-3 py-2 text-body-strong text-ink">{group.label} <span className="text-meta font-normal text-muted">({grouped.get(group.key)!.length})</span></summary>
                <ul className="divide-y divide-line border-t border-line">
                  {grouped.get(group.key)!.map((entry) => <InfoEntryRow key={entry.key} entry={entry} />)}
                </ul>
              </details>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export default function AdviceDetailPage() {
  const params = useParams()
  const id = Number(params.id)
  const queryClient = useQueryClient()
  const [decision, setDecision] = useState<'accept' | 'reject' | null>(null)
  const [reason, setReason] = useState('')
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [executionStatus, setExecutionStatus] = useState<AdvisorExecutionStatus>('partial')
  const [executionNote, setExecutionNote] = useState('')
  const [executionError, setExecutionError] = useState<string | null>(null)

  const detailQuery = useQuery({
    queryKey: ['adviceDetail', id],
    queryFn: () => api.getAdvice(id),
    enabled: Number.isFinite(id),
    retry: false,
    refetchInterval: (query) => query.state.data?.status === 'generating' ? 2500 : false,
  })
  const detail = detailQuery.data

  useEffect(() => {
    const latest = detail?.decisions.filter((d) => d.action === 'execution_note').at(-1)
    if (latest?.execution_status) setExecutionStatus(latest.execution_status as AdvisorExecutionStatus)
    setExecutionNote(latest?.reason ?? '')
  }, [detail])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['adviceList'] })
    queryClient.invalidateQueries({ queryKey: ['adviceDetail', id] })
  }

  const decisionMutation = useMutation({
    mutationFn: () => decision === 'accept'
      ? api.acceptAdvice(id, reason.trim() || undefined, newIdemKey())
      : api.rejectAdvice(id, reason.trim(), newIdemKey()),
    onSuccess: () => { invalidate(); setDecision(null); setReason(''); setDecisionError(null) },
    onError: (error) => setDecisionError(adviceError(error)),
  })
  const executionMutation = useMutation({
    mutationFn: () => api.recordAdviceExecution(id, { execution_status: executionStatus, note: executionNote.trim() || null }, newIdemKey()),
    onSuccess: () => { invalidate(); setExecutionError(null) },
    onError: (error) => setExecutionError(adviceError(error)),
  })

  if (!Number.isFinite(id)) {
    return <PageContainer width="wide"><ErrorState title="建议地址无效" description="请从建议收件箱进入详情。" /></PageContainer>
  }

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader
        back={{ href: '/planning/advice', label: '返回建议收件箱' }}
        title={detail?.title || (detail ? ADVICE_STATUS_LABEL[detail.status] : '建议详情')}
        description={detail ? `${horizonLabel(detail.horizon)} · 生成 ${formatDateTime(detail.created_at)}` : undefined}
      />
      {detailQuery.isLoading ? <LoadingState rows={5} /> : detailQuery.isError ? (
        <ErrorState title="建议详情加载失败" description={adviceError(detailQuery.error)} onRetry={() => detailQuery.refetch()} />
      ) : detail ? (
        <div className="space-y-4">
          <Card padding="md" className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn('rounded-pill px-2 py-0.5 text-meta', ADVICE_STATUS_CLASS[detail.status])}>{ADVICE_STATUS_LABEL[detail.status]}</span>
              <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">{horizonLabel(detail.horizon)}</span>
              {detail.provider?.label && <span className="text-meta text-muted">供应商 {detail.provider.label} · {detail.provider.model}</span>}
              {detail.expires_at && <span className="ml-auto text-meta text-muted">过期 {formatDate(detail.expires_at)}</span>}
            </div>
            {detail.status === 'generating' ? (
              <div className="flex items-center gap-2 rounded-control border border-brand-200 bg-brand-50 p-3 text-body text-brand-800">
                <Loader2 size={17} className="animate-spin" aria-hidden /> 正在生成建议，稍后自动刷新…
              </div>
            ) : detail.status === 'generation_failed' ? (
              <InlineError>建议生成失败，未产生可审阅建议。请检查 LLM 供应商后重新生成。</InlineError>
            ) : detail.status === 'gate_failed' ? (
              <InlineError>建议未通过门检查，未进入待审阅；原因见下方门结果。</InlineError>
            ) : null}
            {detail.summary && <p className="whitespace-pre-wrap text-body text-secondary">{detail.summary}</p>}
            {detail.status === 'awaiting_review' && (
              <div className="flex flex-wrap gap-2 border-t border-line pt-3">
                <button type="button" onClick={() => { setDecision('accept'); setReason('') }} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-status-good/30 bg-status-good-surface px-3 text-meta font-medium text-status-good-text"><CheckCircle2 size={15} aria-hidden /> 采纳</button>
                <button type="button" onClick={() => { setDecision('reject'); setReason('') }} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-status-critical/30 bg-surface px-3 text-meta font-medium text-status-critical-text"><XCircle size={15} aria-hidden /> 驳回</button>
              </div>
            )}
            {detail.status === 'accepted' && (
              <div className="space-y-3 border-t border-line pt-3">
                <div className="flex items-center gap-2 text-body-strong text-ink"><CheckCircle2 size={16} className="text-status-good-text" aria-hidden /> 已采纳 · 可记录执行进展（不改建议状态）</div>
                {executionError && <InlineError>{executionError}</InlineError>}
                <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
                  <SelectField label="执行状态" value={executionStatus} onChange={(v) => setExecutionStatus(v as AdvisorExecutionStatus)} options={[{ value: 'done', label: '已完成' }, { value: 'partial', label: '部分执行' }, { value: 'not_done', label: '未执行' }]} />
                  <FormField label="执行备注（可选）">
                    <input type="text" value={executionNote} onChange={(e) => setExecutionNote(e.target.value)} placeholder="如：本月完成一次调整" className={fieldControlClass} />
                  </FormField>
                </div>
                <button type="button" onClick={() => executionMutation.mutate()} disabled={executionMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-50"><History size={14} aria-hidden /> {executionMutation.isPending ? '保存中…' : '记录执行进展'}</button>
              </div>
            )}
            {['rejected', 'expired', 'gate_failed', 'generation_failed'].includes(detail.status) && (
              <p className="flex items-center gap-2 border-t border-line pt-3 text-meta text-muted"><Info size={14} aria-hidden /> 终态只读，不可再次采纳或驳回。</p>
            )}
          </Card>

          {detail.gate && (
            <Card padding="md">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h2 className="flex items-center gap-2 text-h3 font-semibold text-ink"><AlertTriangle size={16} className={detail.gate.result === 'failed' ? 'text-status-critical-text' : 'text-status-good-text'} aria-hidden /> 门检查</h2>
                <span className={detail.gate.result === 'failed' ? 'text-meta text-status-critical-text' : 'text-meta text-status-good-text'}>{detail.gate.result === 'failed' ? '未通过' : '通过'} · {detail.gate.gate_version}</span>
              </div>
              <ul className="divide-y divide-line">
                {detail.gate.checks.map((check) => (
                  <li key={check.code} className="py-2">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className={check.passed ? 'text-body text-ink' : check.severity === 'warning' ? 'text-body text-status-warn-text' : 'text-body font-medium text-status-critical-text'}>{gateCheckText(check)}</span>
                      <span className="text-meta text-muted">{check.severity === 'blocking' ? '阻断' : '注记'} · {check.passed ? '通过' : check.severity === 'warning' ? '注记' : '未通过'}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card padding="md">
            <h2 className="text-h3 font-semibold text-ink">建议项</h2>
            {detail.items.length === 0 ? <EmptyState title="没有可审阅的建议项" description={detail.status === 'gate_failed' ? '门失败时不会生成建议项。' : '该建议没有产生条目。'} compact /> : (
              <ol className="mt-3 space-y-3">
                {detail.items.map((item) => (
                  <li key={item.seq} className="rounded-control border border-line p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-pill bg-brand-50 px-2 py-0.5 text-meta text-brand-700">{DIRECTION_LABEL[item.direction] ?? item.direction}</span>
                      <span className="text-body-strong text-ink">{item.subject_kind}{item.subject_ref ? ` · ${item.subject_ref}` : ''}</span>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap text-body text-secondary">{item.rationale}</p>
                    {item.info_refs.length > 0 && <p className="mt-1 break-all text-meta text-muted">引用 {item.info_refs.join('、')}</p>}
                  </li>
                ))}
              </ol>
            )}
          </Card>

          {detail.info_set && <Card padding="md"><h2 className="mb-3 text-h3 font-semibold text-ink">信息集（只读）</h2><InfoSetPanel suggestionId={id} /></Card>}

          {detail.decisions.length > 0 && (
            <Card padding="md">
              <h2 className="mb-3 flex items-center gap-2 text-h3 font-semibold text-ink"><History size={16} aria-hidden /> 决策记录</h2>
              <ol className="divide-y divide-line">
                {detail.decisions.map((d) => (
                  <li key={d.id} className="py-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-body text-ink">{d.action === 'accept' ? '已采纳' : d.action === 'reject' ? '已驳回' : d.action === 'execution_note' ? `执行备注 · ${d.execution_status === 'done' ? '已完成' : d.execution_status === 'partial' ? '部分执行' : '未执行'}` : '已过期'}</span>
                      <span className="pf-num text-meta text-muted">{formatDateTime(d.decided_at)}</span>
                    </div>
                    {d.reason && <p className="mt-0.5 text-meta text-secondary">{d.reason}</p>}
                  </li>
                ))}
              </ol>
            </Card>
          )}

          {detail.meta.degradations && detail.meta.degradations.length > 0 && (
            <p className="rounded-control border border-line bg-subtle p-3 text-meta text-muted">{detail.meta.degradations.map((d) => d.message).join('；')}</p>
          )}
        </div>
      ) : null}

      <ResponsiveDialog
        open={!!decision}
        onClose={() => setDecision(null)}
        title={decision === 'accept' ? '采纳这条建议' : '驳回这条建议'}
        description={decision === 'accept' ? '采纳只记录你的决定，不会改动账本。理由可选。' : '驳回需要填写理由；驳回后该建议进入终态。'}
        size="sm"
        footer={
          <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
            <button type="button" onClick={() => setDecision(null)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink">取消</button>
            <button type="button" onClick={() => { if (decision === 'reject' && !reason.trim()) { setDecisionError('驳回需要填写理由'); return } decisionMutation.mutate() }} disabled={decisionMutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
              {decisionMutation.isPending ? '提交中…' : decision === 'accept' ? '确认采纳' : '确认驳回'}
            </button>
          </div>
        }
      >
        <div className="space-y-3">
          {decisionError && <InlineError>{decisionError}</InlineError>}
          <FormField label={decision === 'reject' ? '驳回理由' : '采纳理由（可选）'} required={decision === 'reject'}>
            <textarea value={reason} onChange={(e) => { setReason(e.target.value); setDecisionError(null) }} rows={3} placeholder={decision === 'reject' ? '如：方向暂不适合当前阶段' : '如：同意方向，先小步执行'} className={cn(fieldControlClass, 'min-h-[90px] resize-none py-2')} />
          </FormField>
        </div>
      </ResponsiveDialog>
    </PageContainer>
  )
}
