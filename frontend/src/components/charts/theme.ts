/* ==========================================================================
 * ledger —— 账本主题（design §5）
 *
 * 视觉主张：账本仪表，不是 BI 大屏。白底、细线、左对齐、数字等宽、单一品牌蓝；
 * 颜色只做语义，不做装饰。
 *
 * 唯一性：
 * - 色板/字阶/间距/动效常量都只在本文件；`charts/**` 其余文件禁止出现 `#hex`；
 * - 本文件只导出数据，不调用 `echarts.use()` / `registerTheme()`（唯一出口在 registry.ts）；
 * - 页面 DOM 用 Tailwind，canvas 用这里的 TS token，两者只在此对齐。
 * ========================================================================== */

/** 色板 token（§5.1）。 */
export const CHART_COLORS = {
  ink: '#0f172a', // 主墨：标题、零线、总计
  inkSoft: '#334155', // 次墨：轴名、强调标签
  muted: '#64748b', // 功能文字：轴标签、图例值、脚注（≈4.7:1）
  faint: '#94a3b8', // 装饰/刻度：不承载必读文字
  hairline: '#e2e8f0', // 细线：轴线、卡片内分隔
  grid: '#f1f5f9', // 横向网格（不画竖向网格）
  surface: '#ffffff', // 图底
  canvas: '#f8fafc', // 空态/画布底
  inkInverse: '#ffffff', // 深底（tooltip/深色块）文字
  inkInverseMuted: 'rgba(255,255,255,.8)', // 深底次要文字（tooltip 行标签）
  inkInverseFaint: 'rgba(255,255,255,.6)', // 深底脚注
  inkInverseRule: 'rgba(255,255,255,.14)', // 深底分隔线（tooltip 明细块上缘）
  brand: '#3b82f6',
  brandDeep: '#2563eb',
  brandWash: 'rgba(59,130,246,.10)',
  up: '#ef4444', // 变化量·涨/增
  down: '#10b981', // 变化量·跌/减
  flat: '#94a3b8', // 0 值、持平
  status: {
    green: '#10b981',
    yellow: '#fbbf24',
    red: '#f43f5e',
    hollow: '#cbd5e1',
  },
  categorical: [
    '#3b82f6', '#8b5cf6', '#06b6d4', '#f59e0b',
    '#6366f1', '#14b8a6', '#ec4899', '#94a3b8',
  ],
  rampBrand: ['#dbeafe', '#bfdbfe', '#93c5fd', '#60a5fa', '#3b82f6', '#2563eb'],
  warn: '#f59e0b', // 阈值带/参考线（不用红绿）
  warnWash: 'rgba(245,158,11,.08)', // 阈值带 8% wash
  carryHatch: 'rgba(15,23,42,.14)', // 沿用值斜纹
  focus: '#60a5fa',
}

/** 字族 token（§5.2）。 */
export const CHART_FONTS = {
  body:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  numeric: "ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, Consolas, monospace",
}

/** 动效时长 token（§5.6）。 */
export const CHART_MOTION = {
  lineDraw: 420,
  barGrow: 300,
  barStagger: 16,
  maxAnimatedBars: 8,
  tooltipFade: 120,
  legendHighlight: 120,
  easing: 'cubicOut',
}

/** 静态主题：给 `echarts.registerTheme('ledger', …)`。 */
export const LEDGER_THEME = {
  color: CHART_COLORS.categorical,
  backgroundColor: CHART_COLORS.surface,
  textStyle: {
    fontFamily: CHART_FONTS.body,
    fontSize: 11,
    color: CHART_COLORS.muted,
  },
  stateAnimation: { duration: CHART_MOTION.legendHighlight, easing: CHART_MOTION.easing },
  animation: true,
  animationDuration: CHART_MOTION.lineDraw,
  animationDurationUpdate: 0, // 更新直接重绘，不叠加入场动画
  animationEasing: CHART_MOTION.easing,
  animationEasingUpdate: 'linear',

  line: {
    symbol: 'circle',
    symbolSize: 4,
    lineStyle: { width: 2 },
    smooth: false,
  },
  bar: {
    defaultBarGap: '20%',
    itemStyle: { borderColor: CHART_COLORS.surface, borderWidth: 0 },
  },
  pie: {
    itemStyle: { borderColor: CHART_COLORS.surface, borderWidth: 2 },
    label: { color: CHART_COLORS.inkSoft, fontSize: 10 },
    labelLine: { lineStyle: { color: CHART_COLORS.hairline } },
  },
  treemap: {
    breadcrumb: { show: false },
    itemStyle: {
      borderColor: CHART_COLORS.surface,
      borderWidth: 1,
      gapWidth: 1,
    },
    label: { fontSize: 11, color: CHART_COLORS.inkInverse },
  },
  sunburst: {
    itemStyle: { borderColor: CHART_COLORS.surface, borderWidth: 1 },
    label: { fontSize: 10, color: CHART_COLORS.inkInverse },
  },

  // 坐标轴：轴线 hairline、无刻度线、无竖网格（§5.3）
  categoryAxis: {
    axisLine: { show: true, lineStyle: { color: CHART_COLORS.hairline } },
    axisTick: { show: false },
    axisLabel: {
      color: CHART_COLORS.muted,
      fontSize: 11,
      fontFamily: CHART_FONTS.numeric,
      margin: 8,
      hideOverlap: true,
    },
    splitLine: { show: false },
    splitArea: { show: false },
    minorTick: { show: false },
  },
  valueAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: {
      color: CHART_COLORS.muted,
      fontSize: 11,
      fontFamily: CHART_FONTS.numeric,
      margin: 6,
    },
    splitLine: { show: true, lineStyle: { color: CHART_COLORS.grid, width: 1 } },
    splitArea: { show: false },
    minorSplitLine: { show: false },
  },
  grid: {
    left: 4,
    right: 12,
    top: 28,
    bottom: 22,
    containLabel: true,
    borderColor: CHART_COLORS.hairline,
    outerBoundsMode: 'same', // 6.1 自动布局：标签溢出时内缩，ledger 图显式启用
  },
  tooltip: {
    backgroundColor: CHART_COLORS.ink,
    borderWidth: 0,
    padding: [8, 10],
    textStyle: { color: CHART_COLORS.inkInverse, fontSize: 12, fontFamily: CHART_FONTS.body },
    extraCssText:
      'border-radius:8px;box-shadow:0 6px 20px rgba(15,23,42,.18);backdrop-filter:none;',
    axisPointer: {
      lineStyle: { color: CHART_COLORS.faint, width: 1 },
      crossStyle: { color: CHART_COLORS.faint, width: 1 },
      shadowStyle: { color: 'rgba(148,163,184,.12)' },
      label: { backgroundColor: CHART_COLORS.ink, color: CHART_COLORS.inkInverse },
    },
  },
}

/** 动态 token 入参：只含展示环境，不含数据（design §3.2）。 */
export interface ChartTokenInput {
  /** 容器宽 < 560px。 */
  dense: boolean
  /** `prefers-reduced-motion: reduce`。 */
  reducedMotion: boolean
}

/** 动态 token：字号/间距/动画/高度——preset 唯一可读的来源。 */
export interface ChartTokens {
  dense: boolean
  reducedMotion: boolean
  colors: typeof CHART_COLORS
  font: { body: string; numeric: string }
  fontSize: {
    axisLabel: number
    label: number
    legend: number
    tooltipTitle: number
    tooltipRow: number
    footnote: number
  }
  grid: { left: number; right: number; top: number; bottom: number; containLabel: boolean }
  axis: {
    labelColor: string
    lineColor: string
    splitLineColor: string
    /** x 轴刻度上限（密集 6，桌面由 ECharts auto）。 */
    maxXTicks: number | 'auto'
    ySplitNumber: number
    labelMargin: number
    rotate: number
  }
  line: { width: number; symbolSize: number; showSymbolMaxPoints: number }
  bar: { maxAnimated: number; stagger: number }
  tooltip: { maxWidth: number; confine: boolean; backgroundColor: string; textColor: string }
  legend: { itemGap: number; itemSize: number }
  /** 高度表（§5.4）：移动 → 桌面。 */
  heights: Record<'sm' | 'md' | 'lg' | 'xl', number>
  animation:
    | false
    | { duration: number; updateDuration: number; easing: string; barDuration: number; barStagger: number }
}

export function chartTokens({ dense, reducedMotion }: ChartTokenInput): ChartTokens {
  return {
    dense,
    reducedMotion,
    colors: CHART_COLORS,
    font: CHART_FONTS,
    fontSize: {
      axisLabel: dense ? 10 : 11,
      label: dense ? 10 : 11,
      legend: 11,
      tooltipTitle: 12,
      tooltipRow: 12,
      footnote: 11,
    },
    grid: {
      left: 4,
      right: dense ? 8 : 12,
      top: dense ? 24 : 28,
      bottom: dense ? 20 : 22,
      containLabel: true,
    },
    axis: {
      labelColor: CHART_COLORS.muted,
      lineColor: CHART_COLORS.hairline,
      splitLineColor: CHART_COLORS.grid,
      maxXTicks: dense ? 6 : 'auto',
      ySplitNumber: dense ? 3 : 4,
      labelMargin: dense ? 6 : 8,
      rotate: dense ? 30 : 0,
    },
    line: { width: 2, symbolSize: 4, showSymbolMaxPoints: 60 },
    bar: { maxAnimated: CHART_MOTION.maxAnimatedBars, stagger: CHART_MOTION.barStagger },
    tooltip: {
      maxWidth: dense ? 260 : 360,
      confine: dense,
      backgroundColor: CHART_COLORS.ink,
      textColor: CHART_COLORS.inkInverse,
    },
    legend: { itemGap: 12, itemSize: 8 },
    heights: dense
      ? { sm: 200, md: 260, lg: 280, xl: 300 }
      : { sm: 240, md: 350, lg: 400, xl: 480 },
    animation: reducedMotion
      ? false
      : {
          duration: CHART_MOTION.lineDraw,
          updateDuration: 0,
          easing: CHART_MOTION.easing,
          barDuration: CHART_MOTION.barGrow,
          barStagger: CHART_MOTION.barStagger,
        },
  }
}