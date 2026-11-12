'use client'

import { useState } from 'react'
import Link from 'next/link'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronUp, RefreshCw, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import type { AlertDetailItem } from '@/types'
import { PageHeader } from '@/components/layout/PageHeader'
import { PageContainer } from '@/components/layout/PageContainer'
import { Card, DirectionText, EmptyState, ErrorState, LoadingState, Money } from '@/components/ui'
import { formatDate } from '@/lib/format'
import PeriodPicker from './components/PeriodPicker'

function DetailRows({ details }: { details: AlertDetailItem[] }) {
  return (
    <>
      <ul className="mt-3 space-y-2 tablet:hidden">
        {details.map((item) => (
          <li key={item.id} className="rounded-control border border-line bg-surface px-3 py-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-body font-medium text-ink">{item.name}{item.triggered ? ' · 触发' : ''}</span>
              <DirectionText value={item.change_amount} unit="cny" size="xs" />
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-meta text-muted">
              <span>当前 <Money value={item.current_amount} unit="cny" size="xs" /></span>
              <span>对比 <Money value={item.compare_amount} unit="cny" size="xs" /></span>
              {item.change_percent !== null && <DirectionText value={item.change_percent} unit="percent" size="xs" />}
            </div>
          </li>
        ))}
      </ul>
      <div className="mt-3 hidden overflow-x-auto tablet:block">
        <table className="w-full min-w-[620px] text-body">
          <thead className="bg-subtle text-meta text-muted">
            <tr>
              <th scope="col" className="px-3 py-2 text-left">名称</th>
              <th scope="col" className="px-3 py-2 text-right">当前金额</th>
              <th scope="col" className="px-3 py-2 text-right">对比金额</th>
              <th scope="col" className="px-3 py-2 text-right">变化额</th>
              <th scope="col" className="px-3 py-2 text-right">变化%</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {details.map((item) => (
              <tr key={item.id}>
                <td className="px-3 py-2 text-secondary">{item.name}{item.triggered && <span className="ml-1 text-meta text-status-critical-text">触发</span>}</td>
                <td className="px-3 py-2 text-right"><Money value={item.current_amount} unit="cny" size="xs" /></td>
                <td className="px-3 py-2 text-right"><Money value={item.compare_amount} unit="cny" size="xs" /></td>
                <td className="px-3 py-2 text-right"><DirectionText value={item.change_amount} unit="cny" size="xs" /></td>
                <td className="px-3 py-2 text-right">
                  {item.change_percent !== null ? <DirectionText value={item.change_percent} unit="percent" size="xs" /> : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function AlertsPage() {
  const [selectedPeriod, setSelectedPeriod] = useState('')
  const [selectedPeriodType, setSelectedPeriodType] = useState<'day' | 'week' | 'month' | 'quarter' | 'year'>('month')
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const resultsQuery = useQuery({
    queryKey: ['alertResults', selectedPeriod],
    queryFn: () => api.getAlertResults(selectedPeriod || undefined),
  })
  const results = resultsQuery.data ?? []
  const triggered = results.filter((row) => row.triggered)
  const notTriggered = results.filter((row) => !row.triggered)

  const toggle = (id: number) => setExpanded((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="资产预警"
        description="预警状态与检查结果；阈值和规则在「设置 > 预警规则」维护。"
        actions={
          <Link href="/settings/automation/alerts" className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
            <Settings2 size={15} aria-hidden /> 管理规则
          </Link>
        }
      />

      <Card padding="sm">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0 flex-1">
            <PeriodPicker
              value={selectedPeriod}
              onChange={setSelectedPeriod}
              periodType={selectedPeriodType}
              onPeriodTypeChange={setSelectedPeriodType}
              hint="选择要检查的预警账期；默认最近一期"
              showQuickSelect={false}
            />
          </div>
          <button type="button" onClick={() => resultsQuery.refetch()} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
            <RefreshCw size={15} aria-hidden /> 刷新结果
          </button>
        </div>
      </Card>

      {resultsQuery.isLoading ? (
        <LoadingState rows={4} label="正在加载预警结果" />
      ) : resultsQuery.isError ? (
        <ErrorState title="预警结果加载失败" description="无法读取当前账期的检查结果，请重试。" onRetry={() => resultsQuery.refetch()} />
      ) : results.length === 0 ? (
        <EmptyState
          title="暂无预警结果"
          description="先创建并启用预警规则，再运行检查。"
          action={<Link href="/settings/automation/alerts" className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">创建预警规则</Link>}
        />
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-2">
            <Card padding="sm"><p className="text-meta text-muted">检查规则</p><p className="pf-num text-money-m font-semibold text-ink">{results.length}</p></Card>
            <Card padding="sm"><p className="text-meta text-muted">触发预警</p><p className="pf-num text-money-m font-semibold text-status-critical-text">{triggered.length}</p></Card>
            <Card padding="sm"><p className="text-meta text-muted">未触发</p><p className="pf-num text-money-m font-semibold text-secondary">{notTriggered.length}</p></Card>
          </div>

          {triggered.length > 0 && (
            <section aria-labelledby="triggered-title" className="space-y-2">
              <h2 id="triggered-title" className="flex items-center gap-2 text-h3 font-semibold text-ink"><AlertTriangle size={16} className="text-status-critical-text" aria-hidden /> 触发预警（{triggered.length}）</h2>
              {triggered.map((result) => (
                <Card key={result.rule_id} padding="sm" className="border-status-critical/30">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-body font-medium text-ink">{result.rule_name}</p>
                      <p className="mt-0.5 text-meta text-muted">{result.target_name} · {formatDate(result.current_period)}{result.compare_period ? ` vs ${formatDate(result.compare_period)}` : '（无对比数据）'}</p>
                      {result.message && <p className="mt-1 text-meta text-status-warn-text">{result.message}</p>}
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      <Money value={result.change_amount} unit="cny" size="s" />
                      {result.change_percent !== null && <Money value={result.change_percent} unit="percent" size="xs" />}
                    </div>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-meta text-muted">
                    <span>当前 <Money value={result.current_amount} unit="cny" size="xs" /> · 对比 <Money value={result.compare_amount} unit="cny" size="xs" /></span>
                    {result.details?.length > 0 && (
                      <button type="button" onClick={() => toggle(result.rule_id)} className="inline-flex min-h-[44px] items-center gap-1 text-link">
                        {expanded.has(result.rule_id) ? <><ChevronUp size={14} aria-hidden /> 收起详情</> : <><ChevronDown size={14} aria-hidden /> 查看详情（{result.details.filter((d) => d.triggered).length} 项触发）</>}
                      </button>
                    )}
                  </div>
                  {expanded.has(result.rule_id) && result.details?.length > 0 && <DetailRows details={result.details} />}
                </Card>
              ))}
            </section>
          )}

          {notTriggered.length > 0 && (
            <section aria-labelledby="not-triggered-title" className="space-y-2">
              <h2 id="not-triggered-title" className="text-h3 font-semibold text-secondary">未触发（{notTriggered.length}）</h2>
              {notTriggered.map((result) => (
                <Card key={result.rule_id} padding="sm">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-body font-medium text-secondary">{result.rule_name}</p>
                      <p className="mt-0.5 text-meta text-muted">{result.target_name} · {formatDate(result.current_period)}{result.compare_period ? ` vs ${formatDate(result.compare_period)}` : '（无对比数据）'}</p>
                    </div>
                    <DirectionText value={result.change_amount} unit="cny" size="s" />
                  </div>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-meta text-muted">
                    <span>当前 <Money value={result.current_amount} unit="cny" size="xs" /> · 对比 <Money value={result.compare_amount} unit="cny" size="xs" /></span>
                    {result.details?.length > 0 && (
                      <button type="button" onClick={() => toggle(result.rule_id)} className="inline-flex min-h-[44px] items-center gap-1 text-link">
                        {expanded.has(result.rule_id) ? <><ChevronUp size={14} aria-hidden /> 收起详情</> : <><ChevronDown size={14} aria-hidden /> 查看详情</>}
                      </button>
                    )}
                  </div>
                  {expanded.has(result.rule_id) && result.details?.length > 0 && <DetailRows details={result.details} />}
                </Card>
              ))}
            </section>
          )}
        </div>
      )}
    </PageContainer>
  )
}
