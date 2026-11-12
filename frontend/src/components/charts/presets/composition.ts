/**
 * 结构图 option 构造（design §3.4 / §5.4）。
 *
 * 纯函数：数据 → EChartsOption；不取数、不做业务计算。
 * - `share` 只读调用方值（0–100）；缺省不显示占比，绝不使用 ECharts `{d}` 重算分母；
 * - 窄容器回退由 primitive 决定（传进来的 `mode` 已是 effectiveMode）；
 * - 颜色、字号、间距、动效只从 `theme.ts` / `chartTokens` 取。
 */
import { tooltipBaseOption, tooltipHtml, type TooltipRow } from '../tooltip'
import { CHART_COLORS, CHART_MOTION, chartTokens } from '../theme'
import { formatPercent, formatValue } from '../format'
import type { ChartRenderContext, CompositionItem, CompositionMode, Unit } from '../types'

export const COMPOSITION_MODE_LABELS: Record<CompositionMode, string> = {
  pie: '饼图',
  donut: '环形图',
  rose: '玫瑰图',
  treemap: '树图',
  sunburst: '旭日图',
}

export interface CompositionOptionProps {
  items: readonly CompositionItem[]
  /** 已由 primitive 处理过窄屏回退的最终模式。 */
  mode: CompositionMode
  /** 调用方给的合计；只用于 tooltip 脚注，不参与扇区/占比计算。 */
  total?: number | null
  unit?: Unit
}

function relativeLuminance(color: string): number | null {
  const raw = color.replace('#', '')
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return null
  const linear = (value: number) => {
    const channel = value / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  }
  const r = linear(Number.parseInt(raw.slice(0, 2), 16))
  const g = linear(Number.parseInt(raw.slice(2, 4), 16))
  const b = linear(Number.parseInt(raw.slice(4, 6), 16))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** 在 ink / 白之间取对比度更高的字色；只用于标签，属呈现算术。 */
function labelColorFor(color: string): string {
  const background = relativeLuminance(color)
  const ink = relativeLuminance(CHART_COLORS.ink)
  if (background === null || ink === null) return CHART_COLORS.inkInverse
  const whiteContrast = 1.05 / (background + 0.05)
  const inkContrast = (Math.max(background, ink) + 0.05) / (Math.min(background, ink) + 0.05)
  return inkContrast >= whiteContrast ? CHART_COLORS.ink : CHART_COLORS.inkInverse
}

/** 品牌色阶按值排名分配；其余模式按调用方顺序取分类色。 */
export function compositionColorMap(
  items: readonly CompositionItem[],
  mode: CompositionMode,
): Record<string, string> {
  if (mode === 'treemap') {
    const ramp = CHART_COLORS.rampBrand
    const ranked = [...items].sort((a, b) => b.value - a.value)
    const map: Record<string, string> = {}
    ranked.forEach((item, index) => {
      const position = ranked.length > 1
        ? Math.round((index / (ranked.length - 1)) * (ramp.length - 1))
        : 0
      // 大值深、深值重：treemap 的第一眼锚点应是最深的一格
      map[item.key] = ramp[ramp.length - 1 - position]
    })
    return map
  }

  const palette = CHART_COLORS.categorical
  const map: Record<string, string> = {}
  items.forEach((item, index) => {
    map[item.key] = palette[index % palette.length]
  })
  return map
}

interface CompositionDatum {
  name: string
  value: number
  shareText: string | null
  note: string | null
  color: string
  itemStyle: { color: string; borderRadius?: number }
  label?: { color: string }
}

function shareOf(item: CompositionItem): string | null {
  return item.share === null || item.share === undefined ? null : formatPercent(item.share)
}

function buildData(
  items: readonly CompositionItem[],
  colors: Record<string, string>,
  inside: boolean,
): CompositionDatum[] {
  return items.map((item) => {
    const color = colors[item.key] ?? CHART_COLORS.categorical[0]
    const labelColor = labelColorFor(color)
    const datum: CompositionDatum = {
      name: item.label,
      value: item.value,
      shareText: shareOf(item),
      note: item.note ?? null,
      color,
      itemStyle: { color },
    }
    if (inside) datum.label = { color: labelColor }
    return datum
  })
}

/** 外置标签：名称 + 调用方给的占比；没有占比就只显示名称。 */
function outsideLabel(params: { name?: string; data?: { shareText?: string | null } }): string {
  const name = params.name ?? ''
  const share = params.data?.shareText
  return share ? `${name}\n${share}` : name
}

/** 内置标签：只显示调用方给的占比（缺省返回空串，不画 `{d}`）。 */
function insideLabel(params: { data?: { shareText?: string | null } }): string {
  return params.data?.shareText ?? ''
}

/**
 * 结构图 option。标签规则（§3.4/§5.4）：
 * - 桌面 pie/donut/rose：标签在外，名称 + share，细引导线；
 * - dense：标签在内只显示 share（<6% 由 ECharts 自行隐藏/避让），全名/金额交给图例与表；
 * - treemap：标签在内仅名称，面积 + 品牌色阶编码；
 * - sunburst：标签径向，分类色；v1 扁平数据等同 donut 的色板。
 */
export function buildCompositionOption(
  { items, mode, total, unit }: CompositionOptionProps,
  ctx: ChartRenderContext,
): Record<string, unknown> {
  const dense = ctx.dense
  const tokens = chartTokens({ dense, reducedMotion: ctx.reducedMotion })
  const colors = compositionColorMap(items, mode)
  const inside = dense || mode === 'treemap' || mode === 'sunburst'
  const data = buildData(items, colors, inside)
  const totalText = total === null || total === undefined ? null : formatValue(total, unit)

  const tooltip = {
    ...tooltipBaseOption({ dense, trigger: 'item' }),
    formatter: (params: unknown) => {
      const point = params as {
        name?: string
        value?: number
        data?: { shareText?: string | null; note?: string | null; color?: string }
      }
      const rows: TooltipRow[] = [
        {
          label: '金额',
          value: formatValue(point.value ?? 0, unit),
          color: point.data?.color,
        },
      ]
      if (point.data?.shareText) rows.push({ label: '占比', value: point.data.shareText })
      if (point.data?.note) rows.push({ label: '备注', value: point.data.note, muted: true })
      return tooltipHtml({
        title: point.name ?? '',
        rows,
        footer: totalText ? `合计 ${totalText}` : undefined,
      })
    },
  }

  const animation = {
    animation: !ctx.reducedMotion,
    animationDuration: CHART_MOTION.lineDraw,
    animationDurationUpdate: 0,
    animationEasing: CHART_MOTION.easing,
  }

  if (mode === 'treemap') {
    return {
      ...animation,
      tooltip,
      series: [{
        type: 'treemap',
        width: '100%',
        height: '100%',
        left: 0,
        top: 0,
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        sort: 'desc',
        itemStyle: {
          borderColor: CHART_COLORS.surface,
          borderWidth: 1,
          gapWidth: 1,
        },
        label: {
          show: true,
          formatter: '{b}',
          fontSize: tokens.fontSize.label,
          overflow: 'truncate',
          color: CHART_COLORS.inkInverse,
        },
        upperLabel: { show: false },
        emphasis: { focus: 'self' },
        data,
      }],
    }
  }

  if (mode === 'sunburst') {
    return {
      ...animation,
      tooltip,
      series: [{
        type: 'sunburst',
        radius: ['15%', '90%'],
        center: ['50%', '50%'],
        nodeClick: false,
        sort: 'desc',
        itemStyle: {
          borderColor: CHART_COLORS.surface,
          borderWidth: 1,
          borderRadius: 1,
        },
        label: {
          show: true,
          rotate: 'radial',
          formatter: '{b}',
          fontSize: tokens.fontSize.label,
          color: CHART_COLORS.inkInverse,
        },
        emphasis: { focus: 'ancestor' },
        data,
      }],
    }
  }

  const pieLike = {
    type: 'pie',
    center: ['50%', '50%'],
    avoidLabelOverlap: true,
    label: {
      show: true,
      position: dense ? 'inside' : 'outside',
      formatter: dense ? insideLabel : outsideLabel,
      fontSize: tokens.fontSize.label,
      lineHeight: dense ? 12 : 16,
      color: dense ? CHART_COLORS.inkInverse : CHART_COLORS.inkSoft,
    },
    labelLine: {
      show: !dense,
      length: 8,
      length2: 6,
      lineStyle: { color: CHART_COLORS.hairline },
    },
    emphasis: { scale: true, scaleSize: 3 },
    data,
  }

  if (mode === 'donut') {
    return {
      ...animation,
      tooltip,
      series: [{
        ...pieLike,
        radius: dense ? ['42%', '70%'] : ['40%', '62%'],
        itemStyle: { borderRadius: 3 },
      }],
    }
  }

  if (mode === 'rose') {
    return {
      ...animation,
      tooltip,
      series: [{
        ...pieLike,
        radius: dense ? ['20%', '80%'] : ['18%', '64%'],
        roseType: 'area',
        itemStyle: { borderRadius: 3 },
      }],
    }
  }

  return {
    ...animation,
    tooltip,
    series: [{
      ...pieLike,
      radius: dense ? '70%' : '58%',
    }],
  }
}