'use client'

import { useState, useEffect } from 'react'
import Link from 'next/link'
import { ArrowRight, BarChart3, PieChart, Target, TrendingUp } from 'lucide-react'
import { PeriodSelector } from '@/components/allocation'
import { useAllocationAnalysis } from '@/hooks/useAllocation'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, EmptyState, ErrorState, KpiCard } from '@/components/ui'
import { formatDate } from '@/lib/format'
import type { PeriodConfig, AllocationAnalysisItem } from '@/types'

const countConfiguredItems = (items: AllocationAnalysisItem[]): number => {
  let count = 0
  for (const item of items) {
    if (item.target_percent !== null) count++
    if (item.children?.length) count += countConfiguredItems(item.children)
  }
  return count
}

const DIMENSIONS = [
  {
    key: 'asset_class',
    label: '资产类别',
    description: '按资产类别维度配置目标比例，支持父子层级',
    icon: PieChart,
  },
  {
    key: 'liquidity',
    label: '流动性档位',
    description: '按流动性档位（天数）配置目标比例',
    icon: TrendingUp,
  },
  {
    key: 'institution',
    label: '机构',
    description: '按机构维度配置目标比例',
    icon: Target,
  },
]

const STORAGE_KEY = 'allocation_period_config'

/**
 * 资产配置索引页：只负责「选择账期 → 选一个维度」。
 * 页内不再放营销式渐变卡/四步指南/三张功能卡；目标设置与偏离在详情页完成。
 */
export default function AllocationPage() {
  const [periodConfig, setPeriodConfig] = useState<PeriodConfig>({
    periodType: 'month',
    selectedPeriod: '',
  })

  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (!saved) return
    try {
      setPeriodConfig(JSON.parse(saved) as PeriodConfig)
    } catch {
      // 忽略损坏的本地偏好
    }
  }, [])

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(periodConfig))
  }, [periodConfig])

  const { analysis, isLoading, error, refetch } = useAllocationAnalysis(
    'asset_class',
    undefined,
    periodConfig.selectedPeriod || undefined,
    periodConfig.periodType,
  )

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="资产配置"
        description="按维度设置目标比例，并查看实际结构与目标的偏离。"
      />

      <PeriodSelector value={periodConfig} onChange={setPeriodConfig} />

      {isLoading ? (
          <div className="grid grid-cols-2 gap-2 tablet:grid-cols-3">
            <KpiCard label=" " value={null} loading className="col-span-2 tablet:col-span-1" />
            <KpiCard label=" " value={null} loading />
          </div>
      ) : error ? (
        <ErrorState
          compact
          title="配置分析加载失败"
          description="当前账期的总资产与已配置项目未能读取，请重试。"
          onRetry={() => refetch()}
        />
      ) : analysis && analysis.items.length > 0 ? (
        <div className="grid grid-cols-2 gap-2 tablet:grid-cols-3">
          <KpiCard
            label="当前账期总资产"
            value={analysis.total_amount}
            meta={`快照 ${formatDate(analysis.snapshot_date)}`}
            className="col-span-2 tablet:col-span-1"
          />
          <KpiCard
            label="已配置项目数"
            value={countConfiguredItems(analysis.items)}
            unit="count"
            meta="含各层级目标"
          />
          <KpiCard
            label="未分配比例"
            value={analysis.unallocated_percent}
            unit="percent"
            meta="目标合计之外的部分"
          />
        </div>
      ) : (
        <EmptyState
          compact
          title="当前账期暂无配置数据"
          description="先录入资产记录，再为维度设置目标比例。"
          action={<Link href="/records" className="text-body-strong text-link">去录入</Link>}
        />
      )}

      <section aria-labelledby="allocation-dimensions" className="space-y-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 id="allocation-dimensions" className="text-h2 font-semibold text-ink">选择配置维度</h2>
          <span className="text-meta text-muted">3 个维度</span>
        </div>
        <div className="grid grid-cols-1 gap-3 tablet:grid-cols-3">
          {DIMENSIONS.map((dim) => (
            <Link key={dim.key} href={`/allocation/${dim.key}`} className="group block rounded-card">
              <Card padding="md" interactive className="h-full">
                <span className="flex h-9 w-9 items-center justify-center rounded-control bg-subtle text-secondary">
                  <dim.icon size={20} aria-hidden />
                </span>
                <div className="mt-4 flex items-start justify-between gap-3">
                  <h3 className="text-h3 font-semibold text-ink">{dim.label}</h3>
                  <ArrowRight size={18} className="mt-0.5 shrink-0 text-muted" aria-hidden />
                </div>
                <p className="mt-1.5 text-body text-secondary">{dim.description}</p>
                <p className="mt-4 flex items-center gap-1.5 text-meta text-muted">
                  <BarChart3 size={14} aria-hidden />
                  进入配置
                </p>
              </Card>
            </Link>
          ))}
        </div>
      </section>
    </PageContainer>
  )
}