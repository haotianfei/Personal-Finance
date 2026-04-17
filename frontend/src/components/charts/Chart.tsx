'use client'

import { useEffect, useRef, useState } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart, TreemapChart, SunburstChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, BrushComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart, BarChart, PieChart, TreemapChart, SunburstChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, BrushComponent, CanvasRenderer,
])

export { echarts }

interface ChartProps {
  option: Record<string, unknown>
  height?: string
  loading?: boolean
  onEvents?: Record<string, (params: unknown) => void>
  lazy?: boolean
}

export default function Chart({ option, height = '350px', loading = false, onEvents, lazy = true }: ChartProps) {
  const chartRef = useRef<HTMLDivElement>(null)
  const [isVisible, setIsVisible] = useState(!lazy)
  const [hasRendered, setHasRendered] = useState(!lazy)

  useEffect(() => {
    if (!lazy) return

    const element = chartRef.current
    if (!element) return

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            setIsVisible(true)
            setHasRendered(true)
          }
        })
      },
      {
        rootMargin: '100px',
        threshold: 0.1,
      }
    )

    observer.observe(element)

    return () => {
      observer.disconnect()
    }
  }, [lazy])

  // 如果已经渲染过，保持渲染状态（避免图表消失）
  const shouldRender = isVisible || hasRendered

  return (
    <div ref={chartRef} style={{ height, width: '100%' }}>
      {shouldRender ? (
        <ReactEChartsCore
          echarts={echarts}
          option={option}
          style={{ height: '100%', width: '100%' }}
          showLoading={loading}
          notMerge={true}
          opts={{ renderer: 'canvas' }}
          onEvents={onEvents}
        />
      ) : (
        <div
          style={{
            height: '100%',
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: '#f8fafc',
            borderRadius: '8px',
          }}
        >
          <div className="text-slate-400 text-sm">加载中...</div>
        </div>
      )}
    </div>
  )
}
