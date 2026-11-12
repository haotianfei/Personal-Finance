/**
 * 统一 tooltip（design §2.2/§5.1/§5.5）：深墨底、行式布局、统一转义。
 *
 * 安全：资产名、账户名是用户输入，**任何**进入 HTML 的字符串都必须先 `escapeHtml`；
 * 本文件是唯一允许拼 tooltip HTML 的地方，preset 不得手写 `<span>`。
 *
 * 视觉：背景 `CHART_COLORS.ink`，标题白 12/600，行标签 white/80、值 numeric 等宽。
 * 明细块（`detail`）：上缘细线分栏 + 短标题 + 观察句 + 至多 N 行；窄屏（dense）
 * 只留标题与观察句并限宽，明细改由页内列表承载（tooltip 不出画布）。
 * 触控：`confine`（密集）、`enterable:false`、`triggerOn:'mousemove|click'`。
 */
import { CHART_COLORS, CHART_FONTS } from './theme'
import type { TooltipDetail, TooltipDetailRow } from './types'

/** HTML 转义；null/undefined → 空串。 */
export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export interface TooltipRow {
  label: string
  /** 已格式化的值（走 `charts/format`，不要在这里算）。 */
  value: string
  /** 系列色点；缺省不画点。 */
  color?: string
  /** 弱化行（脚注、来源）。 */
  muted?: boolean
}

export interface TooltipContent {
  title?: string
  rows: readonly TooltipRow[]
  /** 逐点明细块（如「同日新增/消失」）；缺省时 tooltip 与改造前一致。 */
  detail?: TooltipDetail | null
  footer?: string
}

/** 行式布局：左标签 + 右值（等宽数字）。 */
export function tooltipRowHtml(row: TooltipRow): string {
  const label = escapeHtml(row.label)
  const value = escapeHtml(row.value)
  const dot = row.color
    ? `<span style="display:inline-block;width:8px;height:8px;border-radius:9999px;flex:0 0 auto;background:${escapeHtml(row.color)};"></span>`
    : ''
  const labelColor = row.muted ? CHART_COLORS.inkInverseFaint : CHART_COLORS.inkInverseMuted
  return (
    `<div style="display:flex;align-items:baseline;gap:12px;">` +
    `<span style="display:inline-flex;align-items:center;gap:6px;min-width:0;color:${labelColor};">${dot}${label}</span>` +
    `<span style="margin-left:auto;white-space:nowrap;font-family:${CHART_FONTS.numeric};font-variant-numeric:tabular-nums;color:${CHART_COLORS.inkInverse};">${value}</span>` +
    `</div>`
  )
}

/** 完整 tooltip 内容（不含 ECharts 外壳）。 */
export function tooltipHtml({ title, rows, detail, footer }: TooltipContent, dense = false): string {
  const head = title
    ? `<div style="font-weight:600;margin-bottom:4px;">${escapeHtml(title)}</div>`
    : ''
  const body = rows.map(tooltipRowHtml).join('')
  const block = detail ? tooltipDetailHtml(detail, dense) : ''
  const foot = footer
    ? `<div style="margin-top:4px;font-size:11px;line-height:16px;color:${CHART_COLORS.inkInverseFaint};">${escapeHtml(footer)}</div>`
    : ''
  return (
    `<div style="font-family:${CHART_FONTS.body};font-size:12px;line-height:18px;min-width:120px;">` +
    head +
    body +
    block +
    foot +
    `</div>`
  )
}

/** 明细行：短标签 + 名称（可换行，宽度收敛）+ 右对齐数值。 */
function detailRowHtml(row: TooltipDetailRow, labelWidth: number): string {
  const tag = row.tag
    ? `<span style="flex:0 0 auto;font-size:10px;line-height:16px;color:${CHART_COLORS.inkInverseFaint};">${escapeHtml(row.tag)}</span>`
    : ''
  const note = row.note
    ? `<span style="color:${CHART_COLORS.inkInverseFaint};"> · ${escapeHtml(row.note)}</span>`
    : ''
  return (
    `<div style="display:flex;align-items:baseline;gap:6px;margin-top:2px;">` +
    tag +
    `<span style="min-width:0;max-width:${labelWidth}px;white-space:normal;overflow-wrap:anywhere;color:${CHART_COLORS.inkInverseMuted};">${escapeHtml(row.label)}${note}</span>` +
    `<span style="margin-left:auto;white-space:nowrap;font-family:${CHART_FONTS.numeric};font-variant-numeric:tabular-nums;color:${CHART_COLORS.inkInverse};">${escapeHtml(row.value)}</span>` +
    `</div>`
  )
}

/**
 * 明细块：上缘细线分栏，标题行 + 观察口径句 + 至多 N 行明细。
 *
 * 名称可换行（`overflow-wrap:anywhere`），整块限宽——资产名很长时不撑破 tooltip，
 * 也不会被横向裁掉。窄屏（dense）只留计数标题：画布只有 ~300px 高，塞进观察句
 * 会把柱状整片盖住；观察句与明细改由页内「日期 chip → 明细」承载（tooltip 末行
 * 仍给「点击查看」）。
 */
function tooltipDetailHtml(detail: TooltipDetail, dense: boolean): string {
  const labelWidth = 190
  const boxStyle = dense ? 'max-width:200px;white-space:normal;' : 'max-width:300px;white-space:normal;'
  const heading =
    `<div style="display:flex;align-items:baseline;gap:6px;white-space:normal;">` +
    `<span style="font-size:11px;line-height:16px;font-weight:600;color:${CHART_COLORS.inkInverse};">${escapeHtml(detail.heading)}</span>` +
    `</div>`
  const note = detail.note
    ? `<div style="margin-top:2px;font-size:11px;line-height:15px;white-space:normal;overflow-wrap:anywhere;color:${CHART_COLORS.inkInverseFaint};">${escapeHtml(detail.note)}</div>`
    : ''
  const rows = dense ? '' : detail.rows.map((row) => detailRowHtml(row, labelWidth)).join('')
  return (
    `<div style="margin-top:6px;padding-top:6px;border-top:1px solid ${CHART_COLORS.inkInverseRule};${boxStyle}">` +
    heading +
    note +
    rows +
    `</div>`
  )
}

/** ECharts tooltip 外壳（深墨底 token），preset 直接展开到 `tooltip`。 */
export function tooltipBaseOption({ dense = false, trigger = 'item' }: { dense?: boolean; trigger?: 'item' | 'axis' } = {}) {
  return {
    trigger,
    backgroundColor: CHART_COLORS.ink,
    borderWidth: 0,
    padding: [8, 10],
    confine: dense,
    enterable: false,
    triggerOn: 'mousemove|click',
    textStyle: {
      color: CHART_COLORS.inkInverse,
      fontSize: 12,
      fontFamily: CHART_FONTS.body,
    },
    extraCssText: 'border-radius:8px;box-shadow:0 6px 20px rgba(15,23,42,.18);',
  }
}