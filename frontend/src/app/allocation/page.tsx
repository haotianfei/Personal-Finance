'use client'

import { useState, useEffect, useMemo } from 'react'
import { useRouter } from 'next/navigation'
import { PieChart, TrendingUp, Target, ArrowRight, Settings, BarChart3 } from 'lucide-react'
import { PeriodSelector } from '@/components/allocation'
import { useAllocationAnalysis } from '@/hooks/useAllocation'
import { formatAmount } from '@/lib/utils'
import type { PeriodConfig, AllocationAnalysisItem } from '@/types'

// Helper function to count configured items recursively
const countConfiguredItems = (items: AllocationAnalysisItem[]): number => {
  let count = 0
  for (const item of items) {
    if (item.target_percent !== null) {
      count++
    }
    if (item.children?.length) {
      count += countConfiguredItems(item.children)
    }
  }
  return count
}

const DIMENSIONS = [
  {
    key: 'fund_type',
    label: '资产类型',
    description: '按资产类型维度配置目标比例，支持层级结构',
    icon: PieChart,
    color: 'bg-blue-500',
    lightColor: 'bg-blue-50',
    textColor: 'text-blue-600',
  },
  {
    key: 'liquidity_rating',
    label: '流动性评级',
    description: '按流动性评级维度配置目标比例',
    icon: TrendingUp,
    color: 'bg-green-500',
    lightColor: 'bg-green-50',
    textColor: 'text-green-600',
  },
  {
    key: 'account',
    label: '账户',
    description: '按账户维度配置目标比例',
    icon: Target,
    color: 'bg-purple-500',
    lightColor: 'bg-purple-50',
    textColor: 'text-purple-600',
  },
]

const STORAGE_KEY = 'allocation_period_config'

export default function AllocationPage() {
  const router = useRouter()
  const [periodConfig, setPeriodConfig] = useState<PeriodConfig>({
    periodType: 'month',
    selectedPeriod: '',
  })

  // Load saved config from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      try {
        const parsed: PeriodConfig = JSON.parse(saved)
        setPeriodConfig(parsed)
      } catch {
        // Ignore parse errors
      }
    }
  }, [])

  // Save to localStorage when config changes
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(periodConfig))
  }, [periodConfig])

  // Get analysis data for summary
  const { analysis: fundTypeAnalysis } = useAllocationAnalysis(
    'fund_type',
    periodConfig.selectedPeriod || undefined
  )

  const handleDimensionClick = (dimensionKey: string) => {
    router.push(`/allocation/${dimensionKey}`)
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产配置</h1>
          <p className="text-sm text-slate-500 mt-1">
            多维度资产比例目标配置与偏离分析
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <BarChart3 size={16} />
          <span>选择维度进行配置</span>
        </div>
      </div>

      {/* Period Selector */}
      <PeriodSelector
        value={periodConfig}
        onChange={setPeriodConfig}
      />

      {/* Summary Card */}
      {fundTypeAnalysis && (
        <div className="bg-gradient-to-r from-brand-500 to-brand-600 rounded-xl p-6 text-white">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-brand-100 text-sm">当前账期总资产</p>
              <p className="text-3xl font-bold mt-1">
                {formatAmount(fundTypeAnalysis.total_amount)}
              </p>
            </div>
            <div className="text-right">
              <p className="text-brand-100 text-sm">已配置项目数</p>
              <p className="text-2xl font-bold mt-1">
                {countConfiguredItems(fundTypeAnalysis.items)}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Dimension Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {DIMENSIONS.map((dim) => {
          const Icon = dim.icon
          return (
            <button
              key={dim.key}
              onClick={() => handleDimensionClick(dim.key)}
              className="group bg-white rounded-xl shadow-card p-6 text-left hover:shadow-lg transition-all duration-300 hover:-translate-y-1"
            >
              <div className="flex items-start justify-between mb-4">
                <div className={`${dim.lightColor} ${dim.textColor} p-3 rounded-lg`}>
                  <Icon size={24} />
                </div>
                <ArrowRight
                  size={20}
                  className="text-slate-300 group-hover:text-brand-500 group-hover:translate-x-1 transition-all"
                />
              </div>
              <h3 className="text-lg font-semibold text-slate-900 group-hover:text-brand-700 transition-colors">
                {dim.label}
              </h3>
              <p className="text-sm text-slate-500 mt-2">
                {dim.description}
              </p>
              <div className="mt-4 flex items-center gap-2">
                <Settings size={14} className="text-slate-400" />
                <span className="text-xs text-slate-400">点击进行配置</span>
              </div>
            </button>
          )
        })}
      </div>

      {/* Quick Guide */}
      <div className="bg-slate-50 rounded-xl p-6">
        <h3 className="font-semibold text-slate-900 mb-4">快速指南</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-brand-100 text-brand-600 flex items-center justify-center text-sm font-medium shrink-0">
              1
            </div>
            <div>
              <p className="font-medium text-slate-900">选择账期</p>
              <p className="text-sm text-slate-500">使用上方账期选择器选择要分析的账期</p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-brand-100 text-brand-600 flex items-center justify-center text-sm font-medium shrink-0">
              2
            </div>
            <div>
              <p className="font-medium text-slate-900">选择维度</p>
              <p className="text-sm text-slate-500">点击上方卡片选择要配置的维度</p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-brand-100 text-brand-600 flex items-center justify-center text-sm font-medium shrink-0">
              3
            </div>
            <div>
              <p className="font-medium text-slate-900">设置目标</p>
              <p className="text-sm text-slate-500">为各项目设置目标比例，总和不超过100%</p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-brand-100 text-brand-600 flex items-center justify-center text-sm font-medium shrink-0">
              4
            </div>
            <div>
              <p className="font-medium text-slate-900">查看分析</p>
              <p className="text-sm text-slate-500">系统将自动计算偏离情况并给出调整建议</p>
            </div>
          </div>
        </div>
      </div>

      {/* Features */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white rounded-lg p-4 border border-slate-200">
          <h4 className="font-medium text-slate-900 mb-2">智能约束</h4>
          <p className="text-sm text-slate-500">
            自动验证比例约束，根级≤100%，子级≤父级
          </p>
        </div>
        <div className="bg-white rounded-lg p-4 border border-slate-200">
          <h4 className="font-medium text-slate-900 mb-2">偏离分析</h4>
          <p className="text-sm text-slate-500">
            实时计算偏离值、偏离率和偏离金额
          </p>
        </div>
        <div className="bg-white rounded-lg p-4 border border-slate-200">
          <h4 className="font-medium text-slate-900 mb-2">调整建议</h4>
          <p className="text-sm text-slate-500">
            智能生成调整建议，按优先级排序
          </p>
        </div>
      </div>
    </div>
  )
}
