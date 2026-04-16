'use client'

import { useMemo } from 'react'
import Chart from '@/components/charts/Chart'
import type { AllocationAnalysisItem } from '@/types'

interface DeviationChartProps {
  items: AllocationAnalysisItem[]
  height?: string
}

// Filter items with targets and flatten hierarchy
const flattenItems = (items: AllocationAnalysisItem[]): AllocationAnalysisItem[] => {
  const result: AllocationAnalysisItem[] = []
  for (const item of items) {
    if (item.target_percent) {
      result.push(item)
    }
    if (item.children?.length) {
      result.push(...flattenItems(item.children))
    }
  }
  return result
}

export function DeviationChart({ items, height = '400px' }: DeviationChartProps) {
  const targetItems = useMemo(() => {
    return flattenItems(items)
      .filter(item => item.target_percent)
      .sort((a, b) => Math.abs(parseFloat(b.deviation)) - Math.abs(parseFloat(a.deviation)))
      .slice(0, 10) // Top 10 deviations
  }, [items])

  if (targetItems.length === 0) {
    return (
      <div className="h-[400px] flex items-center justify-center text-slate-400 bg-slate-50 rounded-xl">
        暂无配置目标数据
      </div>
    )
  }

  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: Array<{ name: string; value: number; seriesName: string }>) => {
        const item = targetItems.find(i => i.name === params[0].name)
        if (!item) return ''
        const target = parseFloat(item.target_percent!)
        const actual = parseFloat(item.actual_percent)
        const deviation = parseFloat(item.deviation)
        return `
          <div style="padding: 8px;">
            <div style="font-weight: bold; margin-bottom: 4px;">${item.name}</div>
            <div>目标: ${target.toFixed(2)}%</div>
            <div>实际: ${actual.toFixed(2)}%</div>
            <div style="color: ${deviation > 0 ? '#ef4444' : '#10b981'};">偏离: ${deviation > 0 ? '+' : ''}${deviation.toFixed(2)}%</div>
          </div>
        `
      },
    },
    legend: {
      data: ['目标比例', '实际比例', '偏离值'],
      top: 0,
      textStyle: { color: '#64748b' },
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: 50,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: targetItems.map(item => item.name),
      axisLabel: {
        interval: 0,
        rotate: 30,
        fontSize: 11,
        color: '#64748b',
      },
      axisLine: { lineStyle: { color: '#e2e8f0' } },
    },
    yAxis: [
      {
        type: 'value',
        name: '比例(%)',
        axisLabel: {
          formatter: '{value}%',
          color: '#64748b',
        },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      {
        type: 'value',
        name: '偏离(%)',
        axisLabel: {
          formatter: '{value}%',
          color: '#64748b',
        },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '目标比例',
        type: 'bar',
        data: targetItems.map(item => parseFloat(item.target_percent!)),
        itemStyle: { color: '#3b82f6' },
        barGap: '10%',
      },
      {
        name: '实际比例',
        type: 'bar',
        data: targetItems.map(item => parseFloat(item.actual_percent)),
        itemStyle: {
          color: (params: { dataIndex: number }) => {
            const deviation = parseFloat(targetItems[params.dataIndex].deviation)
            return deviation > 0 ? '#ef4444' : '#10b981'
          },
        },
      },
      {
        name: '偏离值',
        type: 'line',
        yAxisIndex: 1,
        data: targetItems.map(item => parseFloat(item.deviation)),
        itemStyle: { color: '#f59e0b' },
        lineStyle: { width: 2, type: 'dashed' },
        symbol: 'circle',
        symbolSize: 8,
      },
    ],
  }

  return <Chart option={option} height={height} />
}
