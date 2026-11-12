import { formatDate, formatValue } from '@/lib/format'
import type {
  MetricDomain,
  MetricEvent,
  MetricRedSignal,
  MetricSummary,
} from '@/types'

/**
 * 域灯/异动的展示语言（不做取数、不做判灯）。
 *
 * 口径纪律：
 * - 域态只用后端 `state`；`worst` 只作诊断辅助，不另造第五态。
 * - 事件是「变化」不是「好坏」：默认黄；`new/ongoing` 是事件状态，不是灯。
 * - 所有文案避免把域灯读成总分：这里没有「综合评分/健康度」。
 */

export const DOMAIN_NAME_FALLBACK: Record<string, string> = {
  B1: '资产水位',
  B2: '收益',
  B3: '结构',
  C1: '目标',
  C2: '配置',
  底座: '底座',
}

export const DIMENSION_LABELS: Record<string, string> = {
  asset_class: '资产类别',
  liquidity: '流动性',
  institution: '机构',
  channel: '渠道',
  owner: '归属',
  risk_level: '风险等级',
  tag: '标签',
  asset_name: '资产名称',
}

export function domainName(code: string | null | undefined, domains?: MetricDomain[]): string {
  if (!code) return '未映射域'
  return domains?.find((item) => item.domain === code)?.name
    ?? DOMAIN_NAME_FALLBACK[code]
    ?? code
}

export function dimensionLabel(dimension: string | null | undefined): string | null {
  if (!dimension) return null
  return DIMENSION_LABELS[dimension] ?? dimension
}

/** 域态短文案 + 展开后的解释（颜色永远配文字）。 */
export function domainStateLabel(domain: MetricDomain): { label: string; detail: string } {
  switch (domain.state) {
    case 'red':
      return { label: '超阈', detail: '域内至少一项超阈，任一红即域红' }
    case 'yellow':
      return {
        label: '需注意',
        detail: (domain.active_events ?? 0) > 0
          ? '域内有异动事件；事件并入黄，不单独判红'
          : '域内至少一项轻偏离',
      }
    case 'gray':
      if (domain.judged === 0) {
        return { label: '无判据', detail: '域内指标均无判据，不点彩灯' }
      }
      if (domain.gray_blocked) {
        return { label: '降级阻断', detail: '域内存在判了但降级的项，⚪ 阻断绿' }
      }
      return { label: '不可判', detail: '域内存在不可判项，不判绿' }
    default:
      return { label: '在带内', detail: '域内可判项均在带内' }
  }
}

/** 域格第二行的计数片段；没有彩灯时只露「无判据 N」。 */
export function countBits(domain: MetricDomain): string[] {
  const counts = domain.counts
  const bits: string[] = []
  if (counts.red > 0) bits.push(`红 ${counts.red}`)
  if (counts.yellow > 0) bits.push(`黄 ${counts.yellow}`)
  if (counts.green > 0) bits.push(`绿 ${counts.green}`)
  if (counts.gray > 0) bits.push(`灰 ${counts.gray}`)
  if (bits.length === 0 && counts.unjudged > 0) bits.push(`无判据 ${counts.unjudged}`)
  return bits
}

/** 旧后端没有 summary 时的兜底：仅由 domains/events 相加，不造总分。 */
export function deriveSummary(domains: MetricDomain[], events: MetricEvent[]): MetricSummary {
  const summary: MetricSummary = {
    red: 0,
    yellow: 0,
    green: 0,
    gray: 0,
    unjudged: 0,
    judged: 0,
    total: 0,
    domains: domains.length,
    active_events: events.length,
    new_events: events.filter((event) => event.status === 'new').length,
  }
  for (const domain of domains) {
    summary.red += domain.counts.red
    summary.yellow += domain.counts.yellow
    summary.green += domain.counts.green
    summary.gray += domain.counts.gray
    summary.unjudged += domain.counts.unjudged
    summary.judged += domain.judged
    summary.total += domain.total
  }
  return summary
}

const SEVERITY_RANK: Record<string, number> = { red: 3, yellow: 2, green: 1 }

/** 先严重度、再「新」、再账期（倒序）；同账期按规则 id 稳定。 */
export function sortEvents(events: MetricEvent[]): MetricEvent[] {
  return [...events].sort((a, b) => {
    const severity = (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0)
    if (severity !== 0) return severity
    if (a.status !== b.status) return a.status === 'new' ? -1 : 1
    const period = b.period.localeCompare(a.period)
    if (period !== 0) return period
    return a.rule_id - b.rule_id
  })
}

/** 红信号：单灯在前，复合项行按 |偏离| 从大到小，先看最严重的切片。 */
export function sortRedSignals(reds: MetricRedSignal[]): MetricRedSignal[] {
  return [...reds].sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'item' ? -1 : 1
    const deviation = Math.abs(Number(b.deviation) || 0) - Math.abs(Number(a.deviation) || 0)
    if (deviation !== 0) return deviation
    return (a.name ?? '').localeCompare(b.name ?? '')
  })
}

/** 事件入口：规则 → 设置·预警规则；不把事件误送到「预警结果」页。 */
export function eventTarget(event: MetricEvent): string {
  return `/settings/automation/alerts?rule=${event.rule_id}`
}

export function eventPeriodLabel(period: string | null | undefined): string {
  if (!period) return '—'
  if (/^\d{4}-\d{2}-\d{2}$/.test(period)) return formatDate(period)
  if (/^\d{4}-\d{2}$/.test(period)) return period
  return period
}

export function eventThresholdText(event: MetricEvent): string | null {
  const parts: string[] = []
  if (event.thresholds?.amount != null) {
    parts.push(`金额 ${formatValue(event.thresholds.amount, 'cny')}`)
  }
  if (event.thresholds?.percent != null) {
    parts.push(`比例 ${formatValue(event.thresholds.percent, 'percent')}`)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

const EVENT_SEVERITY_LABELS: Record<string, string> = {
  red: '红',
  yellow: '黄',
  green: '绿',
  gray: '灰',
}

/** 事件严重度的读屏文案（点仍由 GradeDot 呈现，颜色不单独承载语义）。 */
export function eventSeverityLabel(severity: string): string {
  return EVENT_SEVERITY_LABELS[severity] ?? severity
}

const EVENT_DIRECTION_LABELS: Record<string, string> = {
  both: '双向',
  up: '仅增长',
  down: '仅下降',
}

export function eventDirectionLabel(direction: string | null | undefined): string | null {
  if (!direction) return null
  return EVENT_DIRECTION_LABELS[direction] ?? direction
}

/** 偏离方向文案：只解释「超配/低配」，不把方向色当好坏。 */
export function deviationSide(deviation: string | null | undefined): string | null {
  if (deviation == null || deviation === '') return null
  const value = Number(deviation)
  if (!Number.isFinite(value) || value === 0) return null
  return value > 0 ? '超配' : '低配'
}