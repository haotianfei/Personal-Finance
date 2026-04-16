'use client'

import { useState, useCallback, useMemo } from 'react'
import { useParams } from 'next/navigation'
import { PieChart, TrendingUp, Target, ArrowLeft } from 'lucide-react'
import Link from 'next/link'
import {
  PeriodSelector,
  TargetConfigModal,
  ProgressBar,
  AnalysisTable,
  DeviationChart,
} from '@/components/allocation'
import {
  useAllocationTargets,
  useAllocationAnalysis,
  useAllocationStats,
  useAllocationEditing,
  findItemById,
} from '@/hooks/useAllocation'
import { formatAmount } from '@/lib/utils'
import type { AllocationAnalysisItem, PeriodConfig } from '@/types'

const DIMENSIONS = [
  { key: 'fund_type', label: '资产类型', icon: PieChart },
  { key: 'liquidity_rating', label: '流动性评级', icon: TrendingUp },
  { key: 'account', label: '账户', icon: Target },
]

const VALID_DIMENSIONS = ['fund_type', 'liquidity_rating', 'account']

export default function DimensionAllocationPage() {
  const params = useParams()
  const dimension = params.dimension as string

  const [periodConfig, setPeriodConfig] = useState<PeriodConfig>({
    periodType: 'month',
    selectedPeriod: '',
  })
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [selectedItem, setSelectedItem] = useState<AllocationAnalysisItem | null>(null)

  // Validate dimension
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

  const dimensionInfo = DIMENSIONS.find(d => d.key === dimension)!
  const DimensionIcon = dimensionInfo.icon

  // Data fetching
  const { targets, isLoading: isLoadingTargets, saveTarget, isSaving } = useAllocationTargets(dimension)
  const { analysis, isLoading: isLoadingAnalysis } = useAllocationAnalysis(
    dimension,
    periodConfig.selectedPeriod || undefined
  )

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

  // Handle modal close
  const handleCloseModal = useCallback(() => {
    setIsModalOpen(false)
    setSelectedItem(null)
    stopEditing()
  }, [stopEditing])

  // Handle save
  const handleSave = useCallback(async (value: string) => {
    if (!selectedItem) return

    const percent = parseFloat(value)
    
    // Find parent target ID from allocation_targets if parent exists
    let parentTargetId: number | undefined = undefined
    if (selectedItem.parent_id) {
      const parentAllocationTarget = targets?.find(t => t.target_id === selectedItem.parent_id)
      if (parentAllocationTarget) {
        parentTargetId = parentAllocationTarget.id
      }
    }

    try {
      await saveTarget(selectedItem.id, percent, parentTargetId)
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
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link
            href="/allocation"
            className="p-2 rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700 transition-colors"
          >
            <ArrowLeft size={20} />
          </Link>
          <div>
            <div className="flex items-center gap-2">
              <DimensionIcon size={24} className="text-brand-600" />
              <h1 className="text-2xl font-bold text-brand-950">{dimensionInfo.label}配置</h1>
            </div>
            <p className="text-sm text-slate-500 mt-1">
              设定{dimensionInfo.label}目标比例，监控配置偏离
            </p>
          </div>
        </div>
      </div>

      {/* Period Selector */}
      <PeriodSelector
        value={periodConfig}
        onChange={setPeriodConfig}
      />

      {/* Progress Bar */}
      <ProgressBar totalAllocated={totalAllocated} />

      {/* Deviation Chart */}
      {analysis?.items && analysis.items.length > 0 && (
        <div className="bg-white rounded-xl shadow-card p-6">
          <h3 className="font-semibold text-brand-950 mb-4">偏离分析图表</h3>
          <DeviationChart items={analysis.items} height="350px" />
        </div>
      )}

      {/* Analysis Table */}
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200">
          <h3 className="font-semibold text-brand-950">
            {dimensionInfo.label}分析
            {analysis && (
              <span className="ml-2 text-sm font-normal text-slate-500">
                (总资产: {formatAmount(analysis.total_amount)})
              </span>
            )}
          </h3>
          <p className="text-sm text-slate-500 mt-1">
            点击编辑按钮设置目标比例，系统将自动计算偏离情况和调整建议
          </p>
        </div>
        {isLoading ? (
          <div className="h-64 flex items-center justify-center text-slate-400">
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
          <div className="h-64 flex items-center justify-center text-slate-400">
            暂无数据
          </div>
        )}
      </div>

      {/* Configuration Note */}
      <div className="bg-blue-50 rounded-xl p-4 text-sm text-blue-700">
        <p className="font-medium mb-2">配置说明</p>
        <ul className="list-disc list-inside space-y-1 text-blue-600">
          <li>点击编辑按钮设置目标比例</li>
          <li>目标比例总和可以小于或等于100%</li>
          <li>子维度比例之和不能超过父维度</li>
          <li>偏离超过±5%时会给出调整建议</li>
          <li>红色表示超配（实际&gt;目标），绿色表示低配（实际&lt;目标）</li>
        </ul>
      </div>

      {/* Target Config Modal */}
      <TargetConfigModal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        onSave={handleSave}
        initialValue={selectedItem?.target_percent || ''}
        itemName={selectedItem?.name || ''}
        parentTarget={modalParentTarget}
        siblingsTotal={modalSiblingsTotal}
        isSaving={isSaving}
      />
    </div>
  )
}
