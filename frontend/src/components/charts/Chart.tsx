'use client'

import ReactEChartsCore from 'echarts-for-react/lib/core'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart, TreemapChart, SunburstChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart, BarChart, PieChart, TreemapChart, SunburstChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, CanvasRenderer,
])

export { echarts }

interface ChartProps {
  option: Record<string, unknown>
  height?: string
  loading?: boolean
}

export default function Chart({ option, height = '350px', loading = false }: ChartProps) {
  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      style={{ height, width: '100%' }}
      showLoading={loading}
      notMerge={true}
      opts={{ renderer: 'canvas' }}
    />
  )
}
