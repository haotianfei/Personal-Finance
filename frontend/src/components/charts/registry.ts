/**
 * charts registry —— 全仓唯一的 `echarts.use()` / `registerTheme()` 出口。
 *
 * V4（M2）起移除 brush / dataZoom 组件：分析页趋势已改窗口 chips，
 * 已确认无其它消费者；`LegendComponent` 仍供未迁移旧图（首页/分配页）使用。
 * 任何新图形/组件必须先在此登记，禁止在页面或其他组件里直接 `echarts.use()`。
 */
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart, TreemapChart, SunburstChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, MarkLineComponent, MarkAreaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { LEDGER_THEME } from './theme'

echarts.use([
  LineChart, BarChart, PieChart, TreemapChart, SunburstChart,
  GridComponent, TooltipComponent,
  MarkLineComponent, MarkAreaComponent, CanvasRenderer,
])

echarts.registerTheme('ledger', LEDGER_THEME)

export { echarts }