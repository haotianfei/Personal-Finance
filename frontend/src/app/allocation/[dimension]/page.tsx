'use client'

import { useState, useCallback, useMemo } from 'react'
import { useParams } from 'next/navigation'
import dynamic from 'next/dynamic'
import { PieChart, TrendingUp, Target, ArrowLeft } from 'lucide-react'
import Link from 'next/link'
const LazyComparisonChart = dynamic(
  () => import('@/components/charts').then((m) => m.ComparisonChart),
  { ssr: false, loading: () => <div className="h-[420px] animate-pulse rounded-card bg-subtle" aria-label="图表加载中" /> },
)

import {
  PeriodSelector,
  TargetConfigModal,
  ProgressBar,
  AnalysisTable,
} from '@/components/allocation'
import type { ComparisonRow } from '@/components/charts'
import {
  useAllocationTargets,
  useAllocationAnalysis,
  useAllocationStats,
  useAllocationEditing,
  findItemById,
  flattenAnalysisItems,
} from '@/hooks/useAllocation'
import { formatCurrency } from '@/lib/format'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card } from '@/components/ui'
import { formatPercent, formatPp } from '@/lib/format'
import type { AllocationAnalysisItem, PeriodConfig } from '@/types'

const DIMENSIONS = [
  { key: 'asset_class', label: '资产类别', icon: PieChart },
  { key: 'liquidity', label: '流动性档位', icon: TrendingUp },
  { key: 'institution', label: '机构', icon: Target },
  { key: 'channel', label: '渠道', icon: Target },
  { key: 'risk_level', label: '风险等级', icon: Target },
]

const VALID_DIMENSIONS = DIMENSIONS.map((d) => d.key)

export default function DimensionAllocationPage() {
  const params = useParams()
  const dimension = params.dimension as string

  if (!VALID_DIMENSIONS.includes(dimension)) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <h2 className="text-xl font-semibold text-slate-900">无效的维度</h2>
          <p className="text-slate-500 mt-2">请返回资产配置页面选择有效维度</p>
          <Link
            href="/allocation"
            className="inline-flex items-center gap-2 mt-4 text-brand-600 hover:text-brand-700"
          >
            <ArrowLeft size={16} />
            返回资产配置
          </Link>
        </div>
      </div>
    )
  }

  return <DimensionAllocationContent dimension={dimension} />
}

function DimensionAllocationContent({ dimension }: { dimension: string }) {
  const [periodConfig, setPeriodConfig] = useState<PeriodConfig>({
    periodType: 'month',
    selectedPeriod: '',
  })
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [selectedItem, setSelectedItem] = useState<AllocationAnalysisItem | null>(null)

  const dimensionInfo = DIMENSIONS.find(d => d.key === dimension)!

  // Data fetching
  const { targets, isLoading: isLoadingTargets, saveTarget, isSaving } = useAllocationTargets(dimension)
  const {
    analysis,
    isLoading: isLoadingAnalysis,
    error: analysisError,
    refetch: refetchAnalysis,
  } = useAllocationAnalysis(
    dimension,
    undefined, // 不再使用snapshot_date
    periodConfig.selectedPeriod || undefined,
    periodConfig.periodType
  )

  // 偏离图只读 analysis 的目标项；阈值取首个目标 alert_threshold（单值参考带，
  // 逐目标阈值仍由下方 AnalysisTable 展现）。
  const comparisonItems = useMemo(
    () => flattenAnalysisItems(analysis?.items ?? []).filter((item) => item.target_percent !== null),
    [analysis],
  )
  const comparisonRows: ComparisonRow[] = useMemo(
    () => comparisonItems.map((item) => ({
      key: item.id,
      label: item.name,
      delta: Number(item.deviation),
      target: Number(item.target_percent),
      actual: Number(item.actual_percent),
      note: `目标 ${formatPercent(item.target_percent)} · 实际 ${formatPercent(item.actual_percent)}`,
    })),
    [comparisonItems],
  )
  const chartThreshold = useMemo(() => {
    const first = comparisonItems.find((item) => item.band?.yellow)
    const value = Number(first?.band?.yellow ?? first?.alert_threshold ?? 5)
    return Number.isFinite(value) ? value : 5
  }, [comparisonItems])
  const thresholdBadge = [{
    code: 'THRESHOLD',
    label: `首个目标阈值 ±${formatPp(chartThreshold).replace(/^\+/, '')}`,
    detail: '图示阈值带取首个目标阈值；逐目标见明细表',
    tone: 'info' as const,
  }]
  const chartState: 'loading' | 'error' | 'ready' | 'empty' = analysisError
    ? 'error'
    : isLoadingAnalysis
      ? 'loading'
      : comparisonRows.length > 0
        ? 'ready'
        : 'empty'

  // Stats
  const { totalAllocated } = useAllocationStats(targets)

  // Editing state
  const { editingId, startEditing, stopEditing } = useAllocationEditing()

  // Handle edit click
  const handleEdit = useCallback((item: AllocationAnalysisItem) => {
    setSelectedItem(item)
    startEditing(item.id, item.target_percent)
    setIsModalOpen(true)
  }, [startEditing])

  // Get current alert threshold for selected item
  const currentThreshold = useMemo(() => {
    if (!selectedItem) return '5.00'
    const target = targets?.find(t => t.target_id === selectedItem.id)
    return target?.alert_threshold || '5.00'
  }, [selectedItem, targets])

  // Handle modal close
  const handleCloseModal = useCallback(() => {
    setIsModalOpen(false)
    setSelectedItem(null)
    stopEditing()
  }, [stopEditing])

  // Handle save
  const handleSave = useCallback(async (targetPercent: string, alertThreshold: string) => {
    if (!selectedItem) return

    const percent = parseFloat(targetPercent)
    const threshold = parseFloat(alertThreshold)

    // Find parent target ID from allocation_targets if parent exists
    let parentTargetId: number | undefined = undefined
    if (selectedItem.parent_id) {
      const parentAllocationTarget = targets?.find(t => t.target_id === selectedItem.parent_id)
      if (parentAllocationTarget) {
        parentTargetId = parentAllocationTarget.id
      }
    }

    try {
      await saveTarget(selectedItem.id, percent, parentTargetId, threshold)
      handleCloseModal()
    } catch (error) {
      console.error('Failed to save target:', error)
    }
  }, [selectedItem, targets, saveTarget, handleCloseModal])

  // Calculate parent target and siblings total for modal
  const modalParentTarget = useMemo(() => {
    if (!selectedItem?.parent_id) return null
    const parent = findItemById(analysis?.items || [], selectedItem.parent_id)
    return parent?.target_percent ? parseFloat(parent.target_percent) : null
  }, [selectedItem, analysis?.items])

  const modalSiblingsTotal = useMemo(() => {
    if (!selectedItem) return 0
    const parentId = selectedItem.parent_id
    const siblings = (analysis?.items || []).filter(item => {
      if (item.id === selectedItem.id) return false
      if (parentId === null) return item.parent_id === null
      return item.parent_id === parentId
    })
    return siblings.reduce((sum, item) => {
      return sum + (item.target_percent ? parseFloat(item.target_percent) : 0)
    }, 0)
  }, [selectedItem, analysis?.items])

  const isLoading = isLoadingTargets || isLoadingAnalysis

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        back={{ href: '/allocation', label: '返回资产配置' }}
        title={`${dimensionInfo.label}配置`}
        description={`设定${dimensionInfo.label}目标比例，监控配置偏离。`}
      />

      {/* Period Selector */}
      <PeriodSelector
        value={periodConfig}
        onChange={setPeriodConfig}
      />

      {/* Progress Bar */}
      <ProgressBar totalAllocated={totalAllocated} />

      {/* 配置偏离：ComparisonChart delta 模式（阈值来自目标 alert_threshold） */}
      <Card padding="md">
        <LazyComparisonChart
          title="配置偏离"
          subtitle="实际 − 目标 · 单位：pp"
          asOf={analysis?.snapshot_date}
          height="lg"
          mode="delta"
          unit="pp"
          threshold={chartThreshold}
          sort="absDelta"
          badges={thresholdBadge}
          rows={comparisonRows}
          state={chartState}
          empty={{ title: '暂无配置目标', hint: '为项目设置目标比例后查看偏离' }}
          onRetry={refetchAnalysis}
        />
        <p className="mt-2 text-meta text-muted">
          图示阈值带取首个目标阈值（±{formatPp(chartThreshold).replace(/^\+/, '')}）；逐目标阈值（黄/红/相对）见下方明细表。
        </p>
      </Card>

      {/* Analysis Table */}
      <Card padding="none" className="overflow-hidden">
        <div className="border-b border-line px-4 py-4 tablet:px-6">
          <h3 className="font-semibold text-ink">
            {dimensionInfo.label}分析
            {analysis && (
              <span className="ml-2 text-sm font-normal text-slate-500">
                (总资产: {formatCurrency(analysis.total_amount)})
              </span>
            )}
          </h3>
          <p className="mt-1 text-body text-secondary">
            点击编辑按钮设置目标比例，系统会自动计算偏离情况和调整建议。
          </p>
        </div>
        {isLoading ? (
          <div className="h-64 flex items-center justify-center text-muted">
            <div className="flex items-center gap-2">
              <div className="w-5 h-5 border-2 border-slate-300 border-t-brand-500 rounded-full animate-spin" />
              加载中...
            </div>
          </div>
        ) : analysis?.items && analysis.items.length > 0 ? (
          <AnalysisTable
            items={analysis.items}
            onEdit={handleEdit}
            editingId={editingId}
            totalAmount={analysis.total_amount}
          />
        ) : (
          <div className="h-64 flex items-center justify-center text-muted">
            暂无数据
          </div>
        )}
      </Card>

      {/* Configuration Note */}
      <div className="bg-blue-50 rounded-xl p-4 text-sm text-blue-700">
        <p className="font-medium mb-2">配置说明</p>
        <ul className="list-disc list-inside space-y-1 text-blue-600">
          <li>点击编辑按钮设置目标比例</li>
          <li>目标比例总和可以小于或等于100%</li>
          <li>子维度比例之和不能超过父维度</li>
          <li>偏离超过预警阈值时会给出调整建议（默认±5%，可自定义）</li>
          <li>红色表示超配（实际&gt;目标），绿色表示低配（实际&lt;目标）</li>
        </ul>
      </div>

      {/* Target Config Modal */}
      <TargetConfigModal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        onSave={handleSave}
        initialValue={selectedItem?.target_percent || ''}
        initialThreshold={currentThreshold}
        itemName={selectedItem?.name || ''}
        parentTarget={modalParentTarget}
        siblingsTotal={modalSiblingsTotal}
        isSaving={isSaving}
      />
    </PageContainer>
  )
}
