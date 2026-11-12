'use client'

import { useMemo, useState } from 'react'
import Link from 'next/link'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Lightbulb, RefreshCw, Settings2, Sparkles } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import { formatDate, formatDateTime } from '@/lib/format'
import type { AdvisorSuggestionSummary } from '@/types'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, EmptyState, ErrorState, InlineError, LoadingState } from '@/components/ui'
import { AdviceSettingsDialog } from '@/components/advice/AdviceSettingsDialog'
import {
  ADVICE_STATUS_CLASS, ADVICE_STATUS_LABEL, ADVICE_STATUS_OPTIONS, adviceError, horizonLabel,
} from '@/components/advice/shared'
import { cn } from '@/lib/utils'

function adviceAvailabilityNote(status: AdvisorSuggestionSummary['status']): string | null {
  switch (status) {
    case 'accepted':
      return '已采纳，不可再次裁决；执行备注请在详情中填写。'
    case 'rejected':
      return '已驳回，不可再裁决。'
    case 'expired':
      return '已过期，不可再裁决。'
    default:
      return null
  }
}

function AdviceCard({ item }: { item: AdvisorSuggestionSummary }) {
  const status = item.status
  const availability = adviceAvailabilityNote(status)
  const title = item.title || (status === 'generating' ? '正在生成建议…' : '未命名建议')
  const summary = item.summary || (status === 'generating' ? '正在汇总信息集并生成建议，完成后会出现在收件箱。' : null)
  return (
    <Card as="article" padding="md" className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-h3 font-semibold text-ink">{title}</h2>
            <span className={cn('rounded-pill px-2 py-0.5 text-meta', ADVICE_STATUS_CLASS[status])}>{ADVICE_STATUS_LABEL[status]}</span>
            <span className="text-meta text-muted">{horizonLabel(item.horizon)}</span>
          </div>
          <p className="mt-1 text-meta text-muted">生成 {formatDateTime(item.created_at)} · {item.trigger.kind === 'manual' ? '手动触发' : item.trigger.kind === 'signal' ? '信号触发' : '定时触发'}</p>
        </div>
        <Link href={`/planning/advice/${item.id}`} className="inline-flex min-h-[44px] shrink-0 items-center gap-1 rounded-control px-2 text-meta text-link">
          详情 <ChevronRight size={14} aria-hidden />
        </Link>
      </div>
      {summary && <p className="line-clamp-2 text-body text-secondary">{summary}</p>}
      {item.gate_failure && (
        <p className="rounded-control border border-status-critical/20 bg-status-critical-surface px-3 py-2 text-meta text-status-critical-text">
          门失败：{item.gate_failure.label}
        </p>
      )}
      {status === 'generation_failed' && (
        <p className="rounded-control border border-status-critical/20 bg-status-critical-surface px-3 py-2 text-meta text-status-critical-text">
          生成失败：请检查默认且启用的 LLM 供应商后重新生成。
        </p>
      )}
      {availability && <p className="text-meta text-muted">{availability}</p>}
      {status === 'awaiting_review' && item.expires_at && (
        <p className="text-meta text-muted">有效期至 {formatDate(item.expires_at)}</p>
      )}
    </Card>
  )
}

export default function AdviceInboxPage() {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)

  const listQuery = useQuery({
    queryKey: ['adviceList', status, page],
    queryFn: () => api.getAdviceList({ status: status || undefined, page, page_size: 10 }),
    refetchInterval: (query) => query.state.data?.items.some((x) => x.status === 'generating') ? 2500 : false,
  })
  const items = listQuery.data?.items ?? []
  const totalPages = listQuery.data?.total_pages ?? 1
  const hasFilter = !!status

  const createMutation = useMutation({
    mutationFn: () => api.createAdvice({}, newIdemKey()),
    onMutate: () => { setCreateError(null); setGenerating(true) },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['adviceList'] })
      window.setTimeout(() => queryClient.invalidateQueries({ queryKey: ['adviceList'] }), 1800)
    },
    onError: (error) => setCreateError(adviceError(error)),
    onSettled: () => setGenerating(false),
  })

  const statusOptions = useMemo(() => ADVICE_STATUS_OPTIONS, [])

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader
        title="建议收件箱"
        description="机器生成的建议只进入隔离区；是否采纳、是否执行由你决定，采纳不改变账本。"
        actions={
          <>
            <button type="button" onClick={() => setSettingsOpen(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <Settings2 size={15} aria-hidden /> 建议设置
            </button>
            <button type="button" onClick={() => createMutation.mutate()} disabled={createMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
              <Sparkles size={16} aria-hidden /> {createMutation.isPending || generating ? '生成中…' : '生成建议'}
            </button>
          </>
        }
      />

      {createError && (
        <InlineError action={<button type="button" onClick={() => setCreateError(null)} className="text-body-strong">关闭</button>}>
          {createError}
        </InlineError>
      )}

      <Card padding="sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="advice-status" className="text-meta text-secondary">状态</label>
            <select
              id="advice-status"
              value={status}
              onChange={(e) => { setStatus(e.target.value); setPage(1) }}
              className="min-h-[44px] rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
            >
              {statusOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <span className="pf-num text-meta text-muted">共 {listQuery.data?.total ?? 0} 条</span>
          </div>
          <button type="button" onClick={() => listQuery.refetch()} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control px-2 text-meta text-secondary">
            <RefreshCw size={14} aria-hidden /> 刷新
          </button>
        </div>
      </Card>

      {listQuery.isLoading ? <LoadingState rows={4} label="正在加载建议" /> : listQuery.isError ? (
        <ErrorState title="建议收件箱加载失败" description={adviceError(listQuery.error)} onRetry={() => listQuery.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Lightbulb}
          title={hasFilter ? '当前状态没有建议' : '收件箱还是空的'}
          description={hasFilter ? '换一个状态筛选看看。' : '点击「生成建议」让系统基于最新信息集给出一份可审阅建议；没有可用供应商时会给出提示。'}
          action={!hasFilter ? <button type="button" onClick={() => createMutation.mutate()} disabled={createMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong text-white"><Sparkles size={16} aria-hidden /> 生成建议</button> : undefined}
        />
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {items.map((item) => <AdviceCard key={item.id} item={item} />)}
          </div>
          {(page > 1 || totalPages > 1) && (
            <div className="flex items-center justify-between gap-2">
              <button type="button" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-40">上一页</button>
              <span className="pf-num text-meta text-muted">{page} / {Math.max(1, totalPages)}</span>
              <button type="button" onClick={() => setPage((p) => p + 1)} disabled={page >= totalPages} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-40">下一页</button>
            </div>
          )}
        </div>
      )}

      <AdviceSettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} onSaved={() => queryClient.invalidateQueries({ queryKey: ['adviceSettings'] })} />
    </PageContainer>
  )
}