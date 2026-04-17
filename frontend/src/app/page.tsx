'use client'

import { useQuery } from '@tanstack/react-query'
import { useState, useEffect, useMemo } from 'react'
import { api } from '@/lib/api'
import { formatAmount, formatPercent } from '@/lib/utils'
import { TrendingUp, TrendingDown, Wallet, PiggyBank, CreditCard, Calendar } from 'lucide-react'
import Chart from '@/components/charts/Chart'

function StatCard({ title, value, icon: Icon, change, changePercent, color }: {
  title: string
  value: string
  icon: React.ElementType
  change?: string
  changePercent?: number | null
  color: string
}) {
  const isPositive = change ? parseFloat(change) >= 0 : true
  return (
    <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200">
      <div className="flex items-start justify-between">
        <div className="space-y-2">
          <p className="text-sm text-slate-500">{title}</p>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">{value}</p>
          {change && (
            <div className="flex items-center gap-1.5">
              {isPositive ? (
                <TrendingUp size={14} className="text-success" />
              ) : (
                <TrendingDown size={14} className="text-danger" />
              )}
              <span className={`text-xs font-medium ${isPositive ? 'text-success' : 'text-danger'}`}>
                {formatAmount(change, true)} ({formatPercent(changePercent)})
              </span>
            </div>
          )}
        </div>
        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${color}`}>
          <Icon size={20} className="text-white" />
        </div>
      </div>
    </div>
  )
}

export default function DashboardPage() {
  const { data: summary } = useQuery({ queryKey: ['summary'], queryFn: api.getSummary })
  const { data: rawMixedChartData } = useQuery({
    queryKey: ['mixedChart', 'year'],
    queryFn: () => api.getMixedChart({ period_type: 'year' }),
  })

  // 限制最多取最近50条年度数据 - 使用 useMemo 缓存处理后的数据
  const mixedChartData = useMemo(() => {
    if (!rawMixedChartData) return null
    return {
      trend: rawMixedChartData.trend.slice(-50),
      comparison: rawMixedChartData.comparison.slice(-50),
    }
  }, [rawMixedChartData])
  const { data: proportionData } = useQuery({
    queryKey: ['proportion', 'fund_type', summary?.latest_date],
    queryFn: () => api.getProportionByDimension('fund_type', summary!.latest_date!),
    enabled: !!summary?.latest_date,
  })

  // 获取窗口宽度用于响应式配置
  const [windowWidth, setWindowWidth] = useState(typeof window !== 'undefined' ? window.innerWidth : 1200)
  
  useEffect(() => {
    const handleResize = () => setWindowWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])
  
  const isSmallScreen = windowWidth < 768

  // 使用 useMemo 缓存趋势图配置，避免重复计算
  const trendOption = useMemo(() => {
    if (!mixedChartData) return {}

    // 大数据量时使用降采样策略
    const dataLength = mixedChartData.trend.length
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
    legend: { 
      data: ['净资产', '变化额'], 
      top: 0, 
      textStyle: { color: '#64748b', fontSize: isSmallScreen ? 11 : 12 },
      itemWidth: isSmallScreen ? 15 : 25,
      itemHeight: isSmallScreen ? 10 : 14,
    },
    grid: { 
      top: isSmallScreen ? 30 : 40, 
      right: isSmallScreen ? 10 : 20, 
      bottom: isSmallScreen ? 50 : 40, 
      left: isSmallScreen ? 50 : 60, 
      containLabel: true 
    },
    xAxis: {
      type: 'category',
      data: mixedChartData.trend.map((t: { period: string }) => t.period),
      axisLabel: {
        fontSize: isSmallScreen ? 9 : 10,
        color: '#94a3b8',
        rotate: isSmallScreen ? 30 : 0,
        interval: 'auto',
      },
      axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: [
      {
        type: 'value', 
        name: '净资产',
        nameTextStyle: { fontSize: isSmallScreen ? 9 : 10 },
        axisLabel: { 
          fontSize: isSmallScreen ? 9 : 10, 
          color: '#94a3b8', 
          formatter: (v: number) => `¥${(v / 10000).toFixed(0)}万` 
        },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      {
        type: 'value', 
        name: '变化额',
        nameTextStyle: { fontSize: isSmallScreen ? 9 : 10 },
        axisLabel: { 
          fontSize: isSmallScreen ? 9 : 10, 
          color: '#94a3b8', 
          formatter: (v: number) => `¥${(v / 10000).toFixed(1)}万` 
        },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '净资产', type: 'line', smooth: true,
        data: mixedChartData.trend.map((t: { total_amount: string }) => parseFloat(t.total_amount)),
        lineStyle: { width: isSmallScreen ? 2 : 2.5, color: '#3b82f6' },
        itemStyle: { color: '#3b82f6' },
        symbolSize: isSmallScreen ? 4 : 6,
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
        data: mixedChartData.comparison.map((c: { change_amount: string }) => parseFloat(c.change_amount)),
        itemStyle: {
          color: (params: { value: number }) =>
            params.value >= 0 ? '#ef4444' : '#10b981',
          borderRadius: [3, 3, 0, 0],
        },
        barMaxWidth: isSmallScreen ? 15 : 20,
      },
    ],
    }
  }, [mixedChartData, isSmallScreen])

  // 使用 useMemo 缓存资产分布图配置
  const assetTreemapOption = useMemo(() => {
    if (!proportionData?.asset_items?.length) return {}
    return {
      tooltip: {
        formatter: (p: { name: string; value: number; data: { percent: number } }) =>
          `${p.name}<br/>¥${Number(p.value).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}<br/>占比: ${p.data?.percent || 0}%`,
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
        itemStyle: {
          borderColor: '#fff',
          borderWidth: 2,
        },
        data: proportionData.asset_items.map((item) => ({
          name: item.name,
          value: parseFloat(item.amount),
          percent: item.percent,
        })),
      }],
    }
  }, [proportionData])

  // 使用 useMemo 缓存负债分布图配置
  const liabilityTreemapOption = useMemo(() => {
    if (!proportionData?.liability_items?.length) return {}
    return {
      tooltip: {
        formatter: (p: { name: string; value: number; data: { percent: number } }) =>
          `${p.name}<br/>¥${Number(p.value).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}<br/>占比: ${p.data?.percent || 0}%`,
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
        itemStyle: {
          borderColor: '#fff',
          borderWidth: 2,
        },
        data: proportionData.liability_items.map((item) => ({
          name: item.name,
          value: parseFloat(item.amount),
          percent: item.percent,
        })),
      }],
    }
  }, [proportionData])

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产概览</h1>
          <p className="text-sm text-slate-500 mt-1">
            最新快照: {summary?.latest_date || '--'} | 共 {summary?.snapshot_count || 0} 期记录
          </p>
        </div>
      </div>

      {/* Summary Cards - 响应式网格布局：手机1列、平板3列、小桌面4列、大桌面5列 */}
      <div className="grid grid-cols-1 sm:grid-cols-3 xl:grid-cols-5 gap-3">
        {/* 净资产 */}
        <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200 border-t-4 border-brand-600">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-500">净资产</span>
            <div className="w-10 h-10 rounded-lg bg-brand-600 flex items-center justify-center">
              <Wallet size={20} className="text-white" />
            </div>
          </div>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">
            {formatAmount(summary?.net_worth || '0')}
          </p>
          {summary?.change_amount && (
            <div className="flex items-center gap-1.5 mt-2">
              {parseFloat(summary.change_amount) >= 0 ? (
                <TrendingUp size={14} className="text-success" />
              ) : (
                <TrendingDown size={14} className="text-danger" />
              )}
              <span className={`text-xs font-medium ${parseFloat(summary.change_amount) >= 0 ? 'text-success' : 'text-danger'}`}>
                {formatAmount(summary.change_amount, true)} ({formatPercent(summary.change_percent)})
              </span>
            </div>
          )}
        </div>

        {/* 总资产 */}
        <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200 border-t-4 border-emerald-500">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-500">总资产</span>
            <div className="w-10 h-10 rounded-lg bg-emerald-500 flex items-center justify-center">
              <PiggyBank size={20} className="text-white" />
            </div>
          </div>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">
            {formatAmount(summary?.total_assets || '0')}
          </p>
        </div>

        {/* 总负债 */}
        <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200 border-t-4 border-red-500">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-500">总负债</span>
            <div className="w-10 h-10 rounded-lg bg-red-500 flex items-center justify-center">
              <CreditCard size={20} className="text-white" />
            </div>
          </div>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">
            {formatAmount(summary?.total_liabilities || '0')}
          </p>
        </div>

        {/* 资产项目数 */}
        <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200 border-t-4 border-amber-500">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-500">资产项目</span>
            <div className="w-10 h-10 rounded-lg bg-amber-500 flex items-center justify-center">
              <Calendar size={20} className="text-white" />
            </div>
          </div>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">
            {summary?.record_count || 0} <span className="text-base font-normal text-slate-500">项</span>
          </p>
        </div>

        {/* 记录期数 */}
        <div className="bg-white rounded-xl p-5 shadow-card hover:shadow-card-hover transition-shadow duration-200 border-t-4 border-purple-500">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm text-slate-500">记录期数</span>
            <div className="w-10 h-10 rounded-lg bg-purple-500 flex items-center justify-center">
              <Calendar size={20} className="text-white" />
            </div>
          </div>
          <p className="text-2xl font-bold text-brand-950 tracking-tight">
            {summary?.snapshot_count || 0} <span className="text-base font-normal text-slate-500">期</span>
          </p>
        </div>
      </div>

      {/* 净资产趋势 - 独占一行 */}
      <div className="bg-white rounded-xl p-5 shadow-card">
        <h3 className="text-sm font-semibold text-slate-700 mb-3">净资产趋势</h3>
        <Chart option={trendOption} height="350px" loading={!mixedChartData} />
      </div>

      {/* 资产和负债分布 - 响应式：小屏幕堆叠，大屏幕并排 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white rounded-xl p-5 shadow-card">
          <h3 className="text-sm font-semibold text-slate-700 mb-3">资产分布</h3>
          {proportionData?.asset_items?.length ? (
            <Chart option={assetTreemapOption} height="300px" loading={!proportionData} />
          ) : (
            <div className="h-[300px] flex items-center justify-center text-slate-400">
              暂无资产数据
            </div>
          )}
        </div>
        <div className="bg-white rounded-xl p-5 shadow-card">
          <h3 className="text-sm font-semibold text-slate-700 mb-3">负债分布</h3>
          {proportionData?.liability_items?.length ? (
            <Chart option={liabilityTreemapOption} height="300px" loading={!proportionData} />
          ) : (
            <div className="h-[300px] flex items-center justify-center text-slate-400">
              暂无负债数据
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
