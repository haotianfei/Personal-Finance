'use client'

import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo } from 'react'
import Link from 'next/link'
import dynamic from 'next/dynamic'
import { useRouter } from 'next/navigation'
import { ChevronRight } from 'lucide-react'
import { api } from '@/lib/api'
import { formatCurrency, formatDate, formatPeriod, formatValue } from '@/lib/format'
import { carriedDetailText, hasCarried } from '@/lib/carried'
import type { CompositionItem, TrendSeries } from '@/components/charts'
import { useChartResponsive } from '@/components/charts/useChartResponsive'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { DomainStrip } from '@/components/metrics/DomainStrip'
import { EventList } from '@/components/metrics/EventList'
import { useMetricStatus } from '@/components/metrics/useMetricStatus'
const LazyCompositionChart = dynamic(
  () => import('@/components/charts').then((m) => m.CompositionChart),
  { ssr: false, loading: () => <div className="h-[320px] animate-pulse rounded-card bg-subtle" aria-label="图表加载中" /> },
)
const LazyTrendChart = dynamic(
  () => import('@/components/charts').then((m) => m.TrendChart),
  { ssr: false, loading: () => <div className="h-[320px] animate-pulse rounded-card bg-subtle" aria-label="图表加载中" /> },
)

import {
  Card,
  ErrorState,
  InlineError,
  KpiCard,
  LoadingState,
  MetaBadge,
  degradationLabel,
} from '@/components/ui'

/** 与 `analysis` 同一套下钻语义：同页滚动、跨页交给目标页。 */
function motionBehavior(): ScrollBehavior {
  if (typeof window === 'undefined') return 'auto'
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
}

function startSectionScroll(section: string, behavior: ScrollBehavior): () => void {
  const id = `section-${section}`
  let tries = 0
  let timer: number | undefined
  const tick = () => {
    const el = document.getElementById(id)
    if (el && el.getBoundingClientRect().top > 160) {
      el.scrollIntoView({ behavior, block: 'start' })
    }
    if (tries++ < 6) timer = window.setTimeout(tick, 220)
  }
  tick()
  return () => {
    if (timer) window.clearTimeout(timer)
  }
}

/**
 * 首页：资产概览。
 *
 * P0 纪律：
 * - 摘要/支出请求失败 → 显式错误态＋重试，绝不显示 0/--；
 * - 金额中性墨色，方向色只出现在变化量（DirectionText）；
 * - KPI 无彩色图标块/彩色顶边；320 主 KPI 全宽、次 KPI 两列，金额不截断；
 * - 图表 1024 两栏容器用 donut 回退，避免 treemap 标签裁切；
 * - 域灯条/异动与 /analysis 同源（/api/metrics/status），不另设全局灯。
 */
export default function DashboardPage() {
  const { windowWidth } = useChartResponsive()
  const router = useRouter()
  const metricStatusQuery = useMetricStatus()
  const summaryQuery = useQuery({ queryKey: ['summary'], queryFn: api.getSummary })
  const expenseQuery = useQuery({ queryKey: ['expenseSummary'], queryFn: () => api.getExpenseSummary() })
  const {
    data: rawMixedChartData,
    isLoading: mixedLoading,
    isError: mixedError,
    refetch: refetchMixed,
  } = useQuery({
    queryKey: ['mixedChart', 'year'],
    queryFn: () => api.getMixedChart({ period_type: 'year' }),
  })
  const {
    data: proportionData,
    isLoading: proportionLoading,
    isError: proportionError,
    refetch: refetchProportion,
  } = useQuery({
    queryKey: ['proportion', 'asset_class', 'latest_month'],
    queryFn: () => api.getProportionByDimension('asset_class', undefined, undefined, undefined, 'month'),
  })

  const summary = summaryQuery.data
  const hasSnapshot = !!summary && summary.snapshot_count > 0

  // 指标条/域灯下钻（三期-0/三期-2）：/?section=… 定位到对应区块。
  useEffect(() => {
    const section = new URLSearchParams(window.location.search).get('section')
    if (!section) return
    return startSectionScroll(section, 'auto')
  }, [])

  const handleSignalDrill = useCallback((target: string) => {
    const [path, search = ''] = target.split('?')
    const section = new URLSearchParams(search).get('section')
    // C2 的契约 drill 是 /analysis?section=allocation，实际配置偏离页在 /allocation
    if (path === '/analysis' && section === 'allocation') {
      router.push('/allocation')
      return
    }
    if (path === window.location.pathname) {
      if (section) startSectionScroll(section, motionBehavior())
      return
    }
    // 跨页交给目标页按 ?section= 定位；定位不到就只跳页（不报错）
    router.push(target)
  }, [router])

  const mixedChartData = useMemo(() => {
    if (!rawMixedChartData) return null
    return {
      trend: rawMixedChartData.trend.slice(-50),
      comparison: rawMixedChartData.comparison.slice(-50),
      meta: rawMixedChartData.meta,
    }
  }, [rawMixedChartData])

  const trendCategories = mixedChartData?.trend.map((t) => t.period) ?? []
  const trendSeries: TrendSeries[] = mixedChartData
    ? [
        {
          key: 'net_worth',
          label: '净资产',
          kind: 'line',
          color: 'brand',
          area: true,
          emphasis: true,
          unit: 'cny',
          values: mixedChartData.trend.map((t) => Number(t.total_amount)),
          provenance: mixedChartData.trend.map((t) => (hasCarried(t) ? 'carried' : 'measured')),
        },
        {
          key: 'change',
          label: '变化额',
          kind: 'bar',
          axis: 'right',
          color: 'direction',
          unit: 'cny',
          values: mixedChartData.comparison.map((c) => Number(c.change_amount)),
          provenance: mixedChartData.comparison.map((c) => (hasCarried(c) ? 'carried' : 'measured')),
        },
      ]
    : []
  // 沿用事实（ADR-0019 D12）：账本是变更日志，只有「哪几项沿用了更早的值」是事实
  const carriedPoint = mixedChartData?.trend.find(hasCarried)
  const trendBadges = carriedPoint
    ? [{
        code: 'DATA_MISSING_CARRY_FORWARD',
        label: degradationLabel('DATA_MISSING_CARRY_FORWARD'),
        detail: carriedDetailText(carriedPoint) ?? '部分资产项沿用更早的估值',
        tone: 'warn' as const,
      }]
    : []
  const trendTooltipFooters = mixedChartData?.trend.map((t) => {
    if (!hasCarried(t)) return null
    const detail = carriedDetailText(t) ?? '含沿用值'
    return detail === '含沿用值' ? detail : `含沿用值 · ${detail}`
  }) ?? []
  const trendState: 'loading' | 'error' | 'ready' | 'empty' = mixedError
    ? 'error'
    : mixedLoading
      ? 'loading'
      : trendCategories.length > 0
        ? 'ready'
        : 'empty'
  const trendAsOf = summary?.latest_date
    ?? mixedChartData?.trend[mixedChartData.trend.length - 1]?.date
    ?? null

  const assetItems: CompositionItem[] = (proportionData?.asset_items ?? []).map((item) => ({
    key: item.name,
    label: item.name,
    value: parseFloat(item.amount),
    share: item.percent,
  }))
  const liabilityItems: CompositionItem[] = (proportionData?.liability_items ?? []).map((item) => ({
    key: item.name,
    label: item.name,
    value: parseFloat(item.amount),
    share: item.percent,
  }))
  const assetState: 'loading' | 'error' | 'ready' | 'empty' = proportionError
    ? 'error'
    : proportionLoading
      ? 'loading'
      : assetItems.length > 0
        ? 'ready'
        : 'empty'
  const liabilityState: 'loading' | 'error' | 'ready' | 'empty' = proportionError
    ? 'error'
    : proportionLoading
      ? 'loading'
      : liabilityItems.length > 0
        ? 'ready'
        : 'empty'
  const assetTotal = proportionData ? parseFloat(proportionData.total_assets) : null
  const liabilityTotal = proportionData ? parseFloat(proportionData.total_liabilities) : null
  const shareFallback = windowWidth < 1280 ? 'donut' : 'none'

  const headerMeta = summaryQuery.isLoading
    ? '正在读取最新快照…'
    : summaryQuery.isError
      ? '摘要暂不可用'
      : `最新快照 ${formatDate(summary?.latest_date)} · 共 ${formatValue(summary?.snapshot_count, 'count')} 期记录`

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader title="资产概览" description={headerMeta} />

      {/* Summary Cards：无图标块、无彩色顶边；主 KPI 跨列，次 KPI 2/3/5 列 */}
      {summaryQuery.isError ? (
        <ErrorState
          title="资产摘要加载失败"
          description="无法读取最新快照与统计，请重试。"
          onRetry={() => summaryQuery.refetch()}
        />
      ) : summaryQuery.isLoading ? (
        <div className="grid grid-cols-2 gap-2 tablet-l:grid-cols-3 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, index) => (
            <KpiCard key={index} label=" " value={null} loading className={index === 0 ? 'col-span-2 tablet-l:col-span-2 xl:col-span-1' : undefined} />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-2 tablet-l:grid-cols-3 xl:grid-cols-5">
          <div id="section-net-worth" className="col-span-2 scroll-mt-20 tablet-l:col-span-2 xl:col-span-1">
            <KpiCard
              label="净资产"
              value={hasSnapshot ? summary?.net_worth : null}
              delta={hasSnapshot ? summary?.change_amount : undefined}
              deltaUnit="cny"
              deltaPercent={hasSnapshot ? summary?.change_percent : undefined}
              deltaLabel="较上期"
              meta={
                <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1.5">
                  <span>{hasSnapshot ? `快照 ${formatDate(summary?.latest_date)}` : '暂无快照'}</span>
                  {/* 分类披露：draft（分类未补齐）项**已计入**合计，这里标出规模并给补办入口 */}
                  {hasSnapshot && (summary?.unclassified_count ?? 0) > 0 && (
                    <Link
                      href="/records?highlight=draft"
                      title={
                        `其中资产 ${formatCurrency(summary?.unclassified_asset_amount)}`
                        + ` / 负债 ${formatCurrency(summary?.unclassified_liability_amount)}`
                        + ' 尚未补齐分类：已计入上面的合计，但按「资产类别」做的占比里没有它们的份额、导出会跳过'
                      }
                      className="pf-subtle-link inline-flex min-h-[24px] items-center gap-0.5 rounded-control text-status-warn-text underline decoration-dotted underline-offset-2"
                    >
                      含 <span className="pf-num font-semibold">{formatValue(summary?.unclassified_count, 'count')}</span> 项待分类
                      <ChevronRight size={12} aria-hidden />
                    </Link>
                  )}
                  {summary?.meta?.degradations.map((d, i) => (
                    <MetaBadge
                      key={`${d.code}-${d.scope ?? i}`}
                      tone={d.code === 'NO_DATA' ? 'muted' : 'warn'}
                      label={degradationLabel(d.code)}
                      detail={d.message}
                    />
                  ))}
                </span>
              }
              emphasis
              className="h-full"
            />
          </div>
          <KpiCard label="总资产" value={hasSnapshot ? summary?.total_assets : null} meta={hasSnapshot ? '快照口径' : '暂无快照'} />
          <KpiCard label="总负债" value={hasSnapshot ? summary?.total_liabilities : null} meta={hasSnapshot ? '快照口径' : '暂无快照'} />
          <KpiCard
            label="资产项目"
            value={summary?.record_count ?? null}
            unit="count"
            meta="当前在册"
          />
          <KpiCard
            label="记录期数"
            value={summary?.snapshot_count ?? null}
            unit="count"
            meta={hasSnapshot ? '全部账期' : '暂无快照'}
          />
        </div>
      )}

      {/* 域级信号（B4）：域灯条＋异动；与 /analysis 指标区同源，不设全局灯 */}
      <div id="signals" className="scroll-mt-20 space-y-4">
        <DomainStrip
          domains={metricStatusQuery.data?.domains ?? []}
          events={metricStatusQuery.data?.events ?? []}
          summary={metricStatusQuery.data?.summary}
          asOf={metricStatusQuery.data?.meta?.snapshot_date}
          isLoading={metricStatusQuery.isLoading}
          isError={metricStatusQuery.isError}
          onRetry={() => metricStatusQuery.refetch()}
          onDrill={handleSignalDrill}
        />
        {metricStatusQuery.data && !metricStatusQuery.isError && (
          <EventList
            events={metricStatusQuery.data.events ?? []}
            domains={metricStatusQuery.data.domains ?? []}
            limit={3}
            hideWhenEmpty
          />
        )}
      </div>

      {/* 净资产趋势：图表层负责状态/空态/重试 */}
      <Card padding="md">
        <LazyTrendChart
          title="净资产趋势"
          asOf={formatDate(trendAsOf)}
          subtitle="年 · 单位：元"
          height="md"
          categories={trendCategories}
          series={trendSeries}
          badges={trendBadges}
          tooltipFooters={trendTooltipFooters}
          state={trendState}
          empty={{ title: '暂无趋势数据', hint: '当前账期没有可用快照；录入后查看趋势' }}
          onRetry={refetchMixed}
        />
      </Card>

      {/* 资产/负债分布：1024 两栏容器 fallback donut，避免 treemap 标签裁切 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card padding="md">
          <LazyCompositionChart
            title="资产分布"
            asOf={formatDate(proportionData?.snapshot_date)}
            height={300}
            mode="treemap"
            narrowFallback={shareFallback}
            items={assetItems}
            total={assetTotal}
            totalLabel="总资产"
            unit="cny"
            state={assetState}
            empty={{ title: '暂无资产数据', hint: '当前账期没有资产项' }}
            onRetry={refetchProportion}
          />
        </Card>
        <Card padding="md">
          <LazyCompositionChart
            title="负债分布"
            asOf={formatDate(proportionData?.snapshot_date)}
            height={300}
            mode="treemap"
            narrowFallback={shareFallback}
            items={liabilityItems}
            total={liabilityTotal}
            totalLabel="总负债"
            unit="cny"
            state={liabilityState}
            empty={{ title: '暂无负债数据', hint: '当前账期没有负债项' }}
            onRetry={refetchProportion}
          />
        </Card>
      </div>
      {/* 本月支出：独立查询；失败不显示 0 */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-h2 font-semibold text-ink">本月支出概览</h2>
            {expenseQuery.data?.month.period && (
              <p className="mt-0.5 text-meta text-muted">
                {formatPeriod(expenseQuery.data.month.period, 'month')}
              </p>
            )}
          </div>
          <Link
            href="/expenses"
            className="pf-subtle-link inline-flex min-h-[44px] items-center gap-0.5 rounded-control px-2 text-meta font-medium text-link"
          >
            查看支出记录 <ChevronRight size={14} aria-hidden />
          </Link>
        </div>
        {expenseQuery.isError ? (
          <InlineError
            action={
              <button
                type="button"
                onClick={() => expenseQuery.refetch()}
                className="min-h-[44px] px-2 text-body-strong font-medium text-status-critical-text"
              >
                重试
              </button>
            }
          >
            本月支出加载失败，请重试。
          </InlineError>
        ) : expenseQuery.isLoading ? (
          <div className="grid grid-cols-2 gap-2 tablet-l:grid-cols-3">
            <KpiCard label=" " value={null} loading className="col-span-2 tablet-l:col-span-1" />
            <KpiCard label=" " value={null} loading />
            <KpiCard label=" " value={null} loading />
          </div>
        ) : expenseQuery.data ? (
          <div className="grid grid-cols-2 gap-2 tablet-l:grid-cols-3">
            <KpiCard
              label="本月支出合计"
              value={expenseQuery.data.month.total}
              meta={`共 ${formatValue(expenseQuery.data.month.count, 'count')} 笔`}
              className="col-span-2 tablet-l:col-span-1"
            />
            <KpiCard
              label="本月固定支出"
              value={expenseQuery.data.month.fixed_total}
              meta="来自固定支出项目"
            />
            <KpiCard
              label="固定支出月负担"
              value={expenseQuery.data.fixed_monthly_burden}
              meta="有效项目归一化到每月"
            />
          </div>
        ) : (
          <LoadingState rows={2} compact label="正在加载本月支出" />
        )}
      </section>

    </PageContainer>
  )
}