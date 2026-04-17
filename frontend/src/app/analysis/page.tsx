'use client'

import { useState, useMemo, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { PERIOD_OPTIONS } from '@/lib/utils'
import Chart from '@/components/charts/Chart'
import { PieChart, LayoutGrid, CircleDot, BarChart3, Wallet } from 'lucide-react'

const TABS = [
  { key: 'trend', label: '净资产趋势' },
  { key: 'proportion', label: '占比分析' },
]

const TREND_DIMENSIONS = [
  { key: 'net_worth', label: '净资产', icon: Wallet },
  { key: 'liquidity_rating', label: '流动性评级', icon: BarChart3 },
  { key: 'fund_type', label: '资产类型', icon: PieChart },
  { key: 'asset_name', label: '资产名称', icon: LayoutGrid },
  { key: 'account', label: '账户', icon: Wallet },
]

const PROPORTION_DIMENSIONS = [
  { key: 'liquidity_rating', label: '流动性评级', icon: BarChart3 },
  { key: 'fund_type', label: '资产类型', icon: PieChart },
  { key: 'asset_name', label: '资产名称', icon: LayoutGrid },
  { key: 'account', label: '账户', icon: Wallet },
]

const CHART_TYPES = [
  { key: 'pie', label: '饼图' },
  { key: 'donut', label: '环形图' },
  { key: 'rose', label: '玫瑰图' },
  { key: 'treemap', label: '树图' },
  { key: 'sunburst', label: '旭日图' },
]

function TrendTab() {
  const [periodType, setPeriodType] = useState('month')
  const [dimension, setDimension] = useState('net_worth')
  const [selectedItem, setSelectedItem] = useState('')
  const [selectedType, setSelectedType] = useState<number | ''>('')
  const [selectedRating, setSelectedRating] = useState('')
  const [selectedAccount, setSelectedAccount] = useState<number | ''>('')
  const [brushRange, setBrushRange] = useState<{ startIndex: number; endIndex: number } | null>(null)

  // 根据维度选择获取数据
  const { data: mixedChartData, isLoading: isLoadingTrend } = useQuery({
    queryKey: ['mixedChart', periodType],
    queryFn: () => api.getMixedChart({ period_type: periodType }),
    enabled: dimension === 'net_worth',
  })

  const { data: itemData, isLoading: isLoadingItem } = useQuery({
    queryKey: ['byItem', selectedItem, periodType],
    queryFn: () => api.getByItem({ asset_name: selectedItem, period_type: periodType }),
    enabled: dimension === 'asset_name' && !!selectedItem,
  })

  const { data: typeData, isLoading: isLoadingType } = useQuery({
    queryKey: ['byType', selectedType, periodType],
    queryFn: () => api.getByType({ fund_type_id: String(selectedType), period_type: periodType }),
    enabled: dimension === 'fund_type' && selectedType !== '',
  })

  const { data: ratingData, isLoading: isLoadingRating } = useQuery({
    queryKey: ['byLiquidityRating', selectedRating, periodType],
    queryFn: () => api.getByLiquidityRating({ liquidity_rating: selectedRating, period_type: periodType }),
    enabled: dimension === 'liquidity_rating' && !!selectedRating,
  })

  const { data: accountData, isLoading: isLoadingAccount } = useQuery({
    queryKey: ['byAccount', selectedAccount, periodType],
    queryFn: () => api.getByAccount({ account_id: String(selectedAccount), period_type: periodType }),
    enabled: dimension === 'account' && selectedAccount !== '',
  })

  const { data: assetNames } = useQuery({
    queryKey: ['assetNames'],
    queryFn: api.getAssetNames,
    enabled: dimension === 'asset_name',
  })

  const { data: fundTypes } = useQuery({
    queryKey: ['fundTypes'],
    queryFn: api.getFundTypes,
    enabled: dimension === 'fund_type',
  })

  const { data: liquidityRatings } = useQuery({
    queryKey: ['liquidityRatings'],
    queryFn: api.getLiquidityRatings,
    enabled: dimension === 'liquidity_rating',
  })

  const { data: accounts } = useQuery({
    queryKey: ['accounts'],
    queryFn: api.getAccounts,
    enabled: dimension === 'account',
  })

  const isLoading = isLoadingTrend || isLoadingItem || isLoadingType || isLoadingRating || isLoadingAccount

  const getChartData = () => {
    if (dimension === 'net_worth' && mixedChartData) {
      return {
        trend: mixedChartData.trend,
        comparison: mixedChartData.comparison,
        name: '净资产',
      }
    }
    if (dimension === 'asset_name' && itemData) {
      return {
        trend: itemData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: itemData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: selectedItem,
      }
    }
    if (dimension === 'fund_type' && typeData) {
      return {
        trend: typeData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: typeData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: fundTypes?.find((ft: { id: number; name: string }) => ft.id === selectedType)?.name || '资产类型',
      }
    }
    if (dimension === 'liquidity_rating' && ratingData) {
      return {
        trend: ratingData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: ratingData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: selectedRating,
      }
    }
    if (dimension === 'account' && accountData) {
      return {
        trend: accountData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: accountData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: accounts?.find((a: { id: number; name: string }) => a.id === selectedAccount)?.name || '账户',
      }
    }
    return null
  }

  const chartData = getChartData()

  // 计算最近2年的默认选中区域
  const defaultBrushRange = useMemo(() => {
    if (!chartData?.trend?.length) return null
    const total = chartData.trend.length
    // 最近2年数据：假设月度数据，2年=24个月
    const periodsPerYear = periodType === 'month' ? 12 : periodType === 'quarter' ? 4 : periodType === 'day' ? 365 : 1
    const defaultPeriods = Math.min(periodsPerYear * 2, total)
    return {
      startIndex: Math.max(0, total - defaultPeriods),
      endIndex: total - 1,
    }
  }, [chartData?.trend, periodType])

  // 根据 brush 范围过滤数据
  const filteredData = useMemo(() => {
    if (!chartData) return null
    const range = brushRange || defaultBrushRange
    if (!range) return chartData

    return {
      ...chartData,
      trend: chartData.trend.slice(range.startIndex, range.endIndex + 1),
      comparison: chartData.comparison.slice(range.startIndex, range.endIndex + 1),
    }
  }, [chartData, brushRange, defaultBrushRange])

  // 处理 brush 事件
  const handleBrushChange = useCallback((params: { batch?: Array<{ startIndex: number; endIndex: number }> }) => {
    if (params.batch && params.batch.length > 0) {
      const { startIndex, endIndex } = params.batch[0]
      setBrushRange({ startIndex, endIndex })
    }
  }, [])

  const trendOption = useMemo(() => {
    if (!chartData) return {}

    const xAxisData = chartData.trend.map((t: { period: string }) => t.period)

    // 大数据量时使用降采样策略
    const dataLength = chartData.trend.length
    const sampling = dataLength > 100 ? 'lttb' : undefined

    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: Array<{ seriesName: string; value: number; axisValue: string }>) => {
          let html = `<strong>${params[0].axisValue}</strong><br/>`
          params.forEach((p) => {
            const val = Number(p.value).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
            html += `${p.seriesName}: ¥${val}<br/>`
          })
          return html
        },
      },
      legend: { data: [chartData.name, '变化额'], top: 0, textStyle: { color: '#64748b' } },
      grid: { top: 40, right: 60, bottom: 100, left: 80 },
      xAxis: {
        type: 'category',
        data: filteredData?.trend.map((t: { period: string }) => t.period) || [],
        axisLabel: { fontSize: 11, color: '#94a3b8' },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
      },
      yAxis: [
        {
          type: 'value', name: chartData.name,
          axisLabel: { fontSize: 11, color: '#94a3b8', formatter: (v: number) => `¥${(v / 10000).toFixed(0)}万` },
          splitLine: { lineStyle: { color: '#f1f5f9' } },
        },
        {
          type: 'value', name: '变化额',
          axisLabel: { fontSize: 11, color: '#94a3b8', formatter: (v: number) => `¥${(v / 10000).toFixed(1)}万` },
          splitLine: { show: false },
        },
      ],
      // 缩略轴(brush)组件配置
      brush: {
        toolbox: ['rect', 'clear'],
        brushMode: 'single',
        brushStyle: {
          borderWidth: 1,
          color: 'rgba(59,130,246,0.1)',
          borderColor: 'rgba(59,130,246,0.5)',
        },
        xAxisIndex: 0,
      },
      // 缩略图(dataZoom)配置
      dataZoom: [
        {
          type: 'slider',
          show: true,
          xAxisIndex: 0,
          startValue: defaultBrushRange?.startIndex ?? 0,
          endValue: defaultBrushRange?.endIndex ?? (xAxisData.length - 1),
          height: 30,
          bottom: 20,
          borderColor: '#e2e8f0',
          fillerColor: 'rgba(59,130,246,0.1)',
          handleStyle: {
            color: '#3b82f6',
          },
          textStyle: { color: '#64748b' },
        },
      ],
      series: [
        {
          name: chartData.name, type: 'line', smooth: true,
          data: filteredData?.trend.map((t: { total_amount: string }) => parseFloat(t.total_amount)) || [],
          lineStyle: { width: 2.5, color: '#3b82f6' },
          itemStyle: { color: '#3b82f6' },
          sampling,
          areaStyle: {
            color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(59,130,246,0.12)' },
                { offset: 1, color: 'rgba(59,130,246,0.01)' },
              ],
            },
          },
        },
        {
          name: '变化额', type: 'bar', yAxisIndex: 1,
          data: filteredData?.comparison.map((c: { change_amount: string }) => parseFloat(c.change_amount)) || [],
          itemStyle: {
            color: (params: { value: number }) =>
              params.value >= 0 ? '#ef4444' : '#10b981',
            borderRadius: [3, 3, 0, 0],
          },
          barMaxWidth: 30,
        },
      ],
    }
  }, [chartData, filteredData, defaultBrushRange])

  return (
    <div className="space-y-6">
      {/* Dimension Selection */}
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-slate-700">分析维度:</span>
        <div className="flex gap-2">
          {TREND_DIMENSIONS.map((d) => (
            <button
              key={d.key}
              onClick={() => {
                setDimension(d.key)
                setSelectedItem('')
                setSelectedType('')
                setSelectedRating('')
                setSelectedAccount('')
              }}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                dimension === d.key
                  ? 'bg-brand-600 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              <d.icon size={16} />
              {d.label}
            </button>
          ))}
        </div>
      </div>

      {/* Period Type Selection */}
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-slate-700">账期类型:</span>
        <div className="flex gap-1 bg-slate-100 rounded-lg p-1">
          {PERIOD_TYPE_OPTIONS.map((t) => (
            <button
              key={t.value}
              onClick={() => setPeriodType(t.value)}
              className={`px-3 py-1.5 text-sm rounded-md transition-all ${
                periodType === t.value
                  ? 'bg-white text-brand-700 font-medium shadow-sm'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Sub-dimension Selection */}
      {dimension === 'asset_name' && (
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">选择项目:</span>
          <select
            value={selectedItem}
            onChange={(e) => setSelectedItem(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
          >
            <option value="">请选择</option>
            {assetNames?.map((n: string) => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
      )}

      {dimension === 'fund_type' && (
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">选择类型:</span>
          <select
            value={selectedType}
            onChange={(e) => setSelectedType(e.target.value ? parseInt(e.target.value) : '')}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
          >
            <option value="">请选择</option>
            {fundTypes?.map((ft: { id: number; name: string; level: number }) => (
              <option key={ft.id} value={ft.id}>{'  '.repeat(ft.level)}{ft.name}</option>
            ))}
          </select>
        </div>
      )}

      {dimension === 'liquidity_rating' && (
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">选择流动性评级:</span>
          <select
            value={selectedRating}
            onChange={(e) => setSelectedRating(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
          >
            <option value="">请选择</option>
            {liquidityRatings?.map((rating: { id: number; name: string }) => (
              <option key={rating.id} value={rating.name}>{rating.name}</option>
            ))}
          </select>
        </div>
      )}

      {dimension === 'account' && (
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">选择账户:</span>
          <select
            value={selectedAccount}
            onChange={(e) => setSelectedAccount(e.target.value ? parseInt(e.target.value) : '')}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
          >
            <option value="">请选择</option>
            {accounts?.map((account: { id: number; name: string }) => (
              <option key={account.id} value={account.id}>{account.name}</option>
            ))}
          </select>
        </div>
      )}

      {chartData ? (
        <Chart
          option={trendOption}
          height="480px"
          loading={isLoading}
          onEvents={{
            dataZoom: handleBrushChange,
          }}
        />
      ) : (
        <div className="h-[480px] flex items-center justify-center text-slate-400 bg-slate-50 rounded-xl">
          {dimension === 'net_worth' ? '加载中...' : '请选择具体维度查看趋势'}
        </div>
      )}

      {/* Data Table */}
      {chartData && (
        <div className="bg-white rounded-xl shadow-card overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-200">
            <h3 className="font-semibold text-brand-950">数据明细</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-slate-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">账期</th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">{chartData.name}</th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">变化额</th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">变化%</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200">
                {chartData.trend.map((item: { period: string; total_amount: string }, index: number) => {
                  const changeAmount = chartData.comparison[index]?.change_amount || '0'
                  const currentAmount = parseFloat(item.total_amount)
                  const prevAmount = currentAmount - parseFloat(changeAmount)
                  const changePercent = prevAmount !== 0 ? (parseFloat(changeAmount) / Math.abs(prevAmount)) * 100 : 0

                  return (
                    <tr key={item.period} className="hover:bg-slate-50 transition-colors">
                      <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-slate-900">{item.period}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-right text-slate-700">
                        ¥{Number(item.total_amount).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </td>
                      <td className={`px-6 py-4 whitespace-nowrap text-sm text-right font-medium ${
                        parseFloat(changeAmount) >= 0 ? 'text-red-600' : 'text-green-600'
                      }`}>
                        {parseFloat(changeAmount) >= 0 ? '+' : ''}
                        ¥{Number(changeAmount).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                      </td>
                      <td className={`px-6 py-4 whitespace-nowrap text-sm text-right ${
                        changePercent >= 0 ? 'text-red-600' : 'text-green-600'
                      }`}>
                        {changePercent >= 0 ? '+' : ''}
                        {changePercent.toFixed(2)}%
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

export default function AnalysisPage() {
  const [tab, setTab] = useState('trend')

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">数据分析</h1>
          <p className="text-sm text-slate-500 mt-1">多维度资产变化趋势分析</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="bg-white rounded-xl p-6 shadow-card">
        {tab === 'trend' && <TrendTab />}
        {tab === 'proportion' && <ProportionTab />}
      </div>
    </div>
  )
}

const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' },
  { value: 'year', label: '年' },
]

function ProportionTab() {
  const [dimension, setDimension] = useState('liquidity_rating')
  const [chartType, setChartType] = useState('pie')
  const [periodType, setPeriodType] = useState('month')
  const [selectedPeriod, setSelectedPeriod] = useState('')
  const [fundTypeLevel, setFundTypeLevel] = useState<number | undefined>(undefined)

  const { data: periods } = useQuery({
    queryKey: ['proportionPeriods', periodType],
    queryFn: () => api.getProportionAvailablePeriods(periodType),
  })

  const { data: fundTypes } = useQuery({
    queryKey: ['fundTypes'],
    queryFn: api.getFundTypes,
  })

  // 计算资产类型的最大层级
  const maxLevel = fundTypes ? Math.max(...fundTypes.map(ft => ft.level), 0) : 0

  const { data: proportionData, isLoading } = useQuery({
    queryKey: ['proportion', dimension, selectedPeriod, fundTypeLevel],
    queryFn: () => api.getProportionByDimension(
      dimension, 
      selectedPeriod || undefined,
      dimension === 'fund_type' ? fundTypeLevel : undefined
    ),
    enabled: !!dimension,
  })

  const formatAmount = (amount: string) => {
    return `¥${Number(amount).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }

  // 使用 useMemo 缓存资产图表配置
  const assetChartOption = useMemo(() => {
    if (!proportionData?.asset_items?.length) return {}

    const items = proportionData.asset_items
    const totalAmount = Number(proportionData.total_assets)

    const baseOption = {
      tooltip: {
        trigger: 'item',
        formatter: (params: { name: string; value: number; percent: number; data: { count: number } }) => {
          return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${params.percent}%<br/>数量: ${params.data?.count || 0}项`
        },
      },
      legend: {
        type: 'scroll',
        orient: 'vertical',
        right: 10,
        top: 20,
        bottom: 20,
        textStyle: { fontSize: 12 },
      },
      color: [
        '#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6',
        '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1',
        '#14b8a6', '#f43f5e', '#8b5cf6', '#a855f7', '#d946ef',
      ],
    }

    switch (chartType) {
      case 'pie':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: '60%',
            center: ['40%', '50%'],
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            emphasis: {
              itemStyle: {
                shadowBlur: 10,
                shadowOffsetX: 0,
                shadowColor: 'rgba(0, 0, 0, 0.5)',
              },
            },
            label: {
              formatter: '{b}\n{d}%',
            },
          }],
        }

      case 'donut':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['40%', '50%'],
            avoidLabelOverlap: false,
            itemStyle: {
              borderRadius: 10,
              borderColor: '#fff',
              borderWidth: 2,
            },
            label: {
              show: false,
              position: 'center',
            },
            emphasis: {
              label: {
                show: true,
                fontSize: 18,
                fontWeight: 'bold',
                formatter: '{b}\n{d}%',
              },
            },
            labelLine: { show: false },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
          }],
        }

      case 'rose':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: [20, 100],
            center: ['40%', '50%'],
            roseType: 'area',
            itemStyle: { borderRadius: 5 },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            label: {
              formatter: '{b}\n{d}%',
            },
          }],
        }

      case 'treemap':
        return {
          tooltip: {
            formatter: (params: { name: string; value: number; data: { count: number } }) => {
              const percent = totalAmount > 0 ? ((params.value / totalAmount) * 100).toFixed(2) : '0.00'
              return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${percent}%<br/>数量: ${params.data?.count || 0}项`
            },
          },
          series: [{
            type: 'treemap',
            width: '100%',
            height: '100%',
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            label: {
              show: true,
              formatter: '{b}',
              fontSize: 14,
            },
            upperLabel: {
              show: false,
            },
            itemStyle: {
              borderColor: '#fff',
              borderWidth: 1,
            },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
          }],
        }

      case 'sunburst':
        return {
          tooltip: {
            trigger: 'item',
            formatter: (params: { name: string; value: number; percent: number; data: { count: number } }) => {
              return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${params.percent}%<br/>数量: ${params.data?.count || 0}项`
            },
          },
          series: [{
            type: 'sunburst',
            radius: ['15%', '90%'],
            center: ['50%', '50%'],
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            label: {
              rotate: 'radial',
              formatter: '{b}',
            },
            itemStyle: {
              borderRadius: 4,
              borderWidth: 2,
              borderColor: '#fff',
            },
            emphasis: {
              focus: 'ancestor',
            },
          }],
        }

      default:
        return baseOption
    }
  }, [proportionData, chartType])

  // 使用 useMemo 缓存负债图表配置
  const liabilityChartOption = useMemo(() => {
    if (!proportionData?.liability_items?.length) return {}

    const items = proportionData.liability_items
    const totalAmount = Number(proportionData.total_liabilities)

    const baseOption = {
      tooltip: {
        trigger: 'item',
        formatter: (params: { name: string; value: number; percent: number; data: { count: number } }) => {
          return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${params.percent}%<br/>数量: ${params.data?.count || 0}项`
        },
      },
      legend: {
        type: 'scroll',
        orient: 'vertical',
        right: 10,
        top: 20,
        bottom: 20,
        textStyle: { fontSize: 12 },
      },
      color: [
        '#10b981', '#06b6d4', '#8b5cf6', '#f59e0b', '#ef4444',
        '#ec4899', '#84cc16', '#f97316', '#6366f1', '#3b82f6',
        '#14b8a6', '#f43f5e', '#a855f7', '#d946ef', '#06b6d4',
      ],
    }

    switch (chartType) {
      case 'pie':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: '60%',
            center: ['40%', '50%'],
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            emphasis: {
              itemStyle: {
                shadowBlur: 10,
                shadowOffsetX: 0,
                shadowColor: 'rgba(0, 0, 0, 0.5)',
              },
            },
            label: {
              formatter: '{b}\n{d}%',
            },
          }],
        }

      case 'donut':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['40%', '50%'],
            avoidLabelOverlap: false,
            itemStyle: {
              borderRadius: 10,
              borderColor: '#fff',
              borderWidth: 2,
            },
            label: {
              show: false,
              position: 'center',
            },
            emphasis: {
              label: {
                show: true,
                fontSize: 18,
                fontWeight: 'bold',
                formatter: '{b}\n{d}%',
              },
            },
            labelLine: { show: false },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
          }],
        }

      case 'rose':
        return {
          ...baseOption,
          series: [{
            type: 'pie',
            radius: [20, 100],
            center: ['40%', '50%'],
            roseType: 'area',
            itemStyle: { borderRadius: 5 },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            label: {
              formatter: '{b}\n{d}%',
            },
          }],
        }

      case 'treemap':
        return {
          tooltip: {
            formatter: (params: { name: string; value: number; data: { count: number } }) => {
              const percent = totalAmount > 0 ? ((params.value / totalAmount) * 100).toFixed(2) : '0.00'
              return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${percent}%<br/>数量: ${params.data?.count || 0}项`
            },
          },
          series: [{
            type: 'treemap',
            width: '100%',
            height: '100%',
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            label: {
              show: true,
              formatter: '{b}',
              fontSize: 14,
            },
            upperLabel: {
              show: false,
            },
            itemStyle: {
              borderColor: '#fff',
              borderWidth: 1,
            },
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
          }],
        }

      case 'sunburst':
        return {
          tooltip: {
            trigger: 'item',
            formatter: (params: { name: string; value: number; percent: number; data: { count: number } }) => {
              return `${params.name}<br/>金额: ${formatAmount(String(params.value))}<br/>占比: ${params.percent}%<br/>数量: ${params.data?.count || 0}项`
            },
          },
          series: [{
            type: 'sunburst',
            radius: ['15%', '90%'],
            center: ['50%', '50%'],
            data: items.map(item => ({
              name: item.name,
              value: Number(item.amount),
              count: item.count,
            })),
            label: {
              rotate: 'radial',
              formatter: '{b}',
            },
            itemStyle: {
              borderRadius: 4,
              borderWidth: 2,
              borderColor: '#fff',
            },
            emphasis: {
              focus: 'ancestor',
            },
          }],
        }

      default:
        return baseOption
    }
  }, [proportionData, chartType])

  return (
    <div className="space-y-6">
      {/* Controls */}
      <div className="bg-slate-50 rounded-xl p-4 space-y-4">
        {/* Dimension Selection */}
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">分析维度:</span>
          <div className="flex gap-2">
            {PROPORTION_DIMENSIONS.map((d) => (
              <button
                key={d.key}
                onClick={() => setDimension(d.key)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                  dimension === d.key
                    ? 'bg-brand-600 text-white'
                    : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200'
                }`}
              >
                <d.icon size={16} />
                {d.label}
              </button>
            ))}
          </div>
        </div>

        {/* Chart Type Selection */}
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">图表类型:</span>
          <div className="flex gap-1 bg-white rounded-lg p-1 border border-slate-200">
            {CHART_TYPES.map((t) => (
              <button
                key={t.key}
                onClick={() => setChartType(t.key)}
                className={`px-3 py-1.5 text-sm rounded-md transition-all ${
                  chartType === t.key
                    ? 'bg-brand-100 text-brand-700 font-medium'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        {/* Period Selection */}
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">账期类型:</span>
          <div className="flex gap-1 bg-white rounded-lg p-1 border border-slate-200">
            {PERIOD_TYPE_OPTIONS.map((t) => (
              <button
                key={t.value}
                onClick={() => {
                  setPeriodType(t.value)
                  setSelectedPeriod('')
                }}
                className={`px-3 py-1.5 text-sm rounded-md transition-all ${
                  periodType === t.value
                    ? 'bg-brand-100 text-brand-700 font-medium'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        {/* Period Date Selection */}
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-slate-700">选择账期:</span>
          <select
            value={selectedPeriod}
            onChange={(e) => setSelectedPeriod(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
          >
            <option value="">最新账期</option>
            {periods?.map((p) => (
              <option key={p.date} value={p.date}>
                {p.label} ({p.count}条)
              </option>
            ))}
          </select>
        </div>

        {/* Fund Type Level Selection - Only show when dimension is fund_type */}
        {dimension === 'fund_type' && maxLevel > 0 && (
          <div className="flex items-center gap-4">
            <span className="text-sm font-medium text-slate-700">分析层级:</span>
            <select
              value={fundTypeLevel === undefined ? '' : fundTypeLevel}
              onChange={(e) => {
                const value = e.target.value
                setFundTypeLevel(value === '' ? undefined : parseInt(value))
              }}
              className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white min-w-[200px]"
            >
              <option value="">全部层级（叶子节点）</option>
              {Array.from({ length: maxLevel + 1 }, (_, i) => (
                <option key={i} value={i}>层级 {i}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Chart and Data */}
      <div className="grid grid-cols-3 gap-6">
        {/* Charts - Asset and Liability stacked vertically */}
        <div className="col-span-2 bg-white rounded-xl p-6 shadow-card space-y-6">
          {/* Asset Chart */}
          <div>
            <h3 className="font-semibold text-brand-950 mb-2 text-center">
              资产 - {CHART_TYPES.find(t => t.key === chartType)?.label}
            </h3>
            {proportionData?.asset_items?.length ? (
              <Chart option={assetChartOption} height="350px" loading={isLoading} />
            ) : (
              <div className="h-[350px] flex items-center justify-center text-slate-400">
                {isLoading ? '加载中...' : '暂无资产数据'}
              </div>
            )}
          </div>

          {/* Liability Chart */}
          <div>
            <h3 className="font-semibold text-brand-950 mb-2 text-center">
              负债 - {CHART_TYPES.find(t => t.key === chartType)?.label}
            </h3>
            {proportionData?.liability_items?.length ? (
              <Chart option={liabilityChartOption} height="350px" loading={isLoading} />
            ) : (
              <div className="h-[350px] flex items-center justify-center text-slate-400">
                {isLoading ? '加载中...' : '暂无负债数据'}
              </div>
            )}
          </div>
        </div>

        {/* Data Table */}
        <div className="bg-white rounded-xl p-6 shadow-card">
          <h3 className="font-semibold text-brand-950 mb-4">数据明细</h3>
          {proportionData?.asset_items?.length || proportionData?.liability_items?.length ? (
            <div className="space-y-3">
              {/* 总计信息 */}
              <div className="text-sm pb-2 border-b space-y-1">
                <div className="flex justify-between">
                  <span className="text-slate-500">总资产:</span>
                  <span className="font-medium text-success">{formatAmount(String(proportionData.total_assets || 0))}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">总负债:</span>
                  <span className="font-medium text-danger">{formatAmount(String(proportionData.total_liabilities || 0))}</span>
                </div>
                <div className="flex justify-between pt-1 border-t border-dashed">
                  <span className="text-slate-600 font-medium">净资产:</span>
                  <span className="font-semibold text-brand-700">{formatAmount(String(proportionData.net_worth || 0))}</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-slate-400">共 {proportionData.total_count} 项</span>
                </div>
              </div>

              {/* 资产列表 */}
              {proportionData.asset_items?.length > 0 && (
                <div>
                  <div className="text-xs font-medium text-slate-500 mb-2">资产 ({proportionData.asset_items.length}项)</div>
                  <div className="space-y-2">
                    {proportionData.asset_items.map((item: { name: string; amount: string | number; count: number; percent: number }, index: number) => (
                      <div
                        key={`asset-${item.name}`}
                        className="flex items-center justify-between p-3 rounded-lg bg-slate-50 hover:bg-slate-100 transition-colors"
                      >
                        <div className="flex items-center gap-3">
                          <span
                            className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold text-white"
                            style={{
                              backgroundColor: [
                                '#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6',
                                '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1',
                              ][index % 10],
                            }}
                          >
                            {index + 1}
                          </span>
                          <div>
                            <div className="font-medium text-slate-700">{item.name}</div>
                            <div className="text-xs text-slate-500">{item.count} 项资产</div>
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="font-semibold text-slate-700">
                            {formatAmount(String(item.amount))}
                          </div>
                          <div className="text-xs text-slate-500">{item.percent}%</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 负债列表 */}
              {proportionData.liability_items?.length > 0 && (
                <div>
                  <div className="text-xs font-medium text-danger mb-2">负债 ({proportionData.liability_items.length}项)</div>
                  <div className="space-y-2">
                    {proportionData.liability_items.map((item: { name: string; amount: string | number; count: number; percent: number }, index: number) => (
                      <div
                        key={`liability-${item.name}`}
                        className="flex items-center justify-between p-3 rounded-lg bg-red-50 hover:bg-red-100 transition-colors"
                      >
                        <div className="flex items-center gap-3">
                          <span
                            className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold text-white"
                            style={{ backgroundColor: '#10b981' }}
                          >
                            {index + 1}
                          </span>
                          <div>
                            <div className="font-medium text-slate-700">{item.name}</div>
                            <div className="text-xs text-slate-500">{item.count} 项负债</div>
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="font-semibold text-danger">
                            {formatAmount(String(item.amount))}
                          </div>
                          <div className="text-xs text-slate-500">{item.percent}%</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="h-64 flex items-center justify-center text-slate-400">
              {isLoading ? '加载中...' : '暂无数据'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
