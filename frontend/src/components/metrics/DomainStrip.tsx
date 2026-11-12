'use client'

import { useId, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { GradeDot } from '@/components/ui/Grade'
import { Money } from '@/components/ui/Money'
import { EmptyState, InlineError } from '@/components/ui/States'
import { formatDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { MetricDomain, MetricEvent, MetricRedSignal, MetricSummary } from '@/types'
import { EventRow } from './EventList'
import {
  countBits,
  deriveSummary,
  deviationSide,
  dimensionLabel,
  domainStateLabel,
  sortEvents,
  sortRedSignals,
} from './domain-language'

/**
 * 域灯条（B4）：按指标注册表 domain 聚合单灯 → 域灯。
 *
 * 口径（spec §3、ADR-0012 D5）：
 * - 域＝B1/B2/B3/C1/C2/底座，不是 B4、不是 B/C 主域；B4 只做汇总；
 * - 红 > 黄（含异动）> ⚪ > 绿；`grade=null` 不阻断绿，但显示 judged/total；
 * - gray（判了但降级）阻断绿；不设全局灯/总分，只给计数；
 * - 点击域格展开该域的红信号与异动，域格本身不直接跳页（避免误触）。
 */

const STATE_TEXT_CLASS: Record<MetricDomain['state'], string> = {
  red: 'text-status-critical-text',
  yellow: 'text-status-warn-text',
  green: 'text-status-good-text',
  gray: 'text-muted',
}

/** 灰态本身中性；「降级阻断」是警示而非黄灯，用 warn 文字色提示，灯点仍为空心。 */
function stateTextClass(domain: MetricDomain): string {
  return domain.state === 'gray' && domain.gray_blocked
    ? 'text-status-warn-text'
    : STATE_TEXT_CLASS[domain.state]
}

const SUMMARY_STATES = [
  { state: 'red', label: '红' },
  { state: 'yellow', label: '黄' },
  { state: 'green', label: '绿' },
  { state: 'gray', label: '不可判' },
] as const

function SummaryLine({ summary, className }: { summary: MetricSummary; className?: string }) {
  return (
    <div
      className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 text-meta text-muted', className)}
      aria-label="指标项计数"
    >
      <span className="pf-num whitespace-nowrap">{summary.domains} 域</span>
      <span className="pf-num whitespace-nowrap">{summary.total} 项</span>
      <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
        {SUMMARY_STATES.map(({ state, label }) => (
          <span key={state} className="inline-flex items-center gap-1 whitespace-nowrap">
            <GradeDot state={state} />
            <span>{label}</span>
            <span className="pf-num text-secondary">{summary[state]}</span>
          </span>
        ))}
      </span>
      <span className="whitespace-nowrap">
        无判据 <span className="pf-num text-secondary">{summary.unjudged}</span>
      </span>
      <span className="pf-num whitespace-nowrap">可判 {summary.judged}/{summary.total}</span>
      {(summary.active_events ?? 0) > 0 && (
        <span className="pf-num whitespace-nowrap">
          异动 {summary.active_events}
          {summary.new_events > 0 ? `（新 ${summary.new_events}）` : ''}
        </span>
      )}
    </div>
  )
}

interface DomainCellProps {
  domain: MetricDomain
  expanded: boolean
  panelId: string
  onToggle: () => void
}

function DomainCell({ domain, expanded, panelId, onToggle }: DomainCellProps) {
  const state = domainStateLabel(domain)
  const bits = countBits(domain)
  const events = domain.active_events ?? 0
  const ariaLabel = [
    domain.name,
    state.label,
    `可判 ${domain.judged}/${domain.total}`,
    ...bits,
    events > 0 ? `异动 ${events}` : null,
    expanded ? '收起详情' : '展开红信号与异动',
  ].filter(Boolean).join('，')

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      aria-controls={expanded ? panelId : undefined}
      aria-label={ariaLabel}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && expanded) {
          event.preventDefault()
          onToggle()
          event.currentTarget.focus()
        }
      }}
      className={cn(
        'group relative flex min-h-[104px] flex-col gap-1.5 border-b border-l border-line px-3 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus',
        expanded ? 'bg-subtle/70' : 'bg-surface hover:bg-subtle/40',
      )}
    >
      <span className="flex w-full items-center gap-2">
        <GradeDot state={domain.state} />
        <span className="min-w-0 flex-1 truncate text-body-strong font-medium text-ink">
          {domain.name}
        </span>
        <ChevronDown
          size={14}
          aria-hidden
          className={cn(
            'shrink-0 text-muted transition-transform duration-base',
            expanded && 'rotate-180',
          )}
        />
      </span>

      <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-meta">
        <span className={cn('font-medium', stateTextClass(domain))}>{state.label}</span>
        <span className="pf-num text-muted">可判 {domain.judged}/{domain.total}</span>
      </span>

      <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-muted">
        {bits.map((bit) => (
          <span key={bit} className="pf-num">{bit}</span>
        ))}
        {events > 0 && (
          <span className="inline-flex items-center gap-1 text-status-warn-text">
            <span className="h-1.5 w-1.5 rounded-full bg-status-warn" aria-hidden />
            异动 <span className="pf-num">{events}</span>
          </span>
        )}
        {domain.gray_blocked && domain.state === 'gray' && (
          <span className="text-status-warn-text">⚪ 阻断绿</span>
        )}
      </span>
    </button>
  )
}

function RedSignalRow({
  signal,
  onDrill,
}: {
  signal: MetricRedSignal
  onDrill?: (target: string) => void
}) {
  const side = deviationSide(signal.deviation)
  const dimension = dimensionLabel(signal.dimension)
  const drill = signal.drill
  const label = signal.name ?? signal.id ?? '红信号'
  const hasMeta = side !== null || signal.deviation != null

  return (
    <li className="flex flex-col gap-1 rounded-control border border-line bg-surface px-2.5 py-2 sm:flex-row sm:flex-wrap sm:items-start sm:gap-x-2">
      <span className="flex min-w-0 flex-1 items-start gap-2">
        <span className="mt-1.5 shrink-0"><GradeDot state="red" /></span>
        <span className="min-w-0 flex-1 text-body text-secondary">
          {dimension && <span className="text-meta text-muted">{dimension} · </span>}
          <span className="text-meta text-muted">{signal.kind === 'item' ? '指标' : '目标行'} · </span>
          {label}
        </span>
        {!hasMeta && drill && onDrill && (
          <button
            type="button"
            onClick={() => onDrill(drill.target)}
            className="inline-flex min-h-[44px] shrink-0 items-center gap-0.5 px-1 text-meta font-medium text-link"
          >
            查看 <ChevronRight size={12} aria-hidden />
          </button>
        )}
      </span>
      {hasMeta && (
        <span className="flex shrink-0 flex-wrap items-center justify-end gap-x-2 gap-y-1 pl-5 sm:pl-0">
          {side && <span className="text-meta text-muted">{side}</span>}
          {signal.deviation != null && <Money value={signal.deviation} unit="pp" size="xs" />}
          {drill && onDrill && (
            <button
              type="button"
              onClick={() => onDrill(drill.target)}
              className="inline-flex min-h-[44px] items-center gap-0.5 px-1 text-meta font-medium text-link"
            >
              查看 <ChevronRight size={12} aria-hidden />
            </button>
          )}
        </span>
      )}
    </li>
  )
}

function DomainDetails({
  domain,
  events,
  domains,
  panelId,
  onDrill,
}: {
  domain: MetricDomain
  events: MetricEvent[]
  domains: MetricDomain[]
  panelId: string
  onDrill?: (target: string) => void
}) {
  const state = domainStateLabel(domain)
  const reds = sortRedSignals(domain.reds ?? [])
  const shownReds = reds.slice(0, 6)
  const domainEvents = sortEvents(events.filter((event) => event.domain === domain.domain))
  const hasReds = reds.length > 0
  const hasEvents = domainEvents.length > 0
  const twoColumn = hasReds && hasEvents
  const drill = domain.drill
  const unjudged = domain.counts.unjudged

  return (
    <div
      id={panelId}
      role="region"
      aria-label={`${domain.name}详情`}
      className="border-t border-line bg-subtle/30 px-4 py-4 sm:px-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <GradeDot state={domain.state} />
          <span className="text-body-strong font-medium text-ink">{domain.name}</span>
          <span className={cn('text-meta font-medium', stateTextClass(domain))}>
            {state.label}
          </span>
          <span className="pf-num text-meta text-muted">可判 {domain.judged}/{domain.total}</span>
          {domain.needs_diagnosis && (
            <span className="rounded-pill border border-status-critical/30 bg-status-critical-surface px-2 py-0.5 text-micro font-medium text-status-critical-text">
              需诊断
            </span>
          )}
        </div>
        {drill && onDrill && (
          <button
            type="button"
            onClick={() => onDrill(drill.target)}
            className="inline-flex min-h-[44px] items-center gap-1 rounded-control px-2 text-meta font-medium text-link"
          >
            {drill.label ?? '查看本域'} <ChevronRight size={14} aria-hidden />
          </button>
        )}
      </div>
      <p className="mt-1 text-meta text-muted">{state.detail}</p>

      {!hasReds && !hasEvents ? (
        <p className="mt-4 text-meta text-muted">本域无红信号，也无异动。</p>
      ) : (
        <div className={cn('mt-4 grid gap-4', twoColumn && 'tablet:grid-cols-2')}>
          {hasReds && (
            <section aria-label="红信号">
              <h3 className="text-meta font-medium text-muted">
                红信号 <span className="pf-num text-secondary">{reds.length}</span>
              </h3>
              <ul className="mt-2 space-y-1.5">
                {shownReds.map((signal, index) => (
                  <RedSignalRow
                    key={`${signal.kind}-${signal.id ?? signal.target_id ?? signal.name ?? index}`}
                    signal={signal}
                    onDrill={onDrill}
                  />
                ))}
              </ul>
              {reds.length > shownReds.length && drill && onDrill && (
                <button
                  type="button"
                  onClick={() => onDrill(drill.target)}
                  className="mt-1 inline-flex min-h-[44px] items-center gap-1 px-1 text-meta font-medium text-link"
                >
                  另 {reds.length - shownReds.length} 项红信号，查看分析
                  <ChevronRight size={12} aria-hidden />
                </button>
              )}
              {!hasEvents && <p className="mt-2 text-meta text-muted">本域无异动。</p>}
            </section>
          )}

          {hasEvents && (
            <section aria-label="域内异动">
              <h3 className="text-meta font-medium text-muted">
                域内异动 <span className="pf-num text-secondary">{domainEvents.length}</span>
              </h3>
              <ul className="mt-2 divide-y divide-line rounded-control border border-line bg-surface px-2">
                {domainEvents.map((event) => (
                  <EventRow key={event.id} event={event} domains={domains} dense={twoColumn} />
                ))}
              </ul>
              {!hasReds && <p className="mt-2 text-meta text-muted">本域无红信号。</p>}
            </section>
          )}
        </div>
      )}

      {domain.gray_blocked && domain.state === 'gray' && (
        <p className="mt-3 rounded-control border border-status-warn/25 bg-status-warn-surface/60 px-3 py-2 text-meta text-status-warn-text">
          本域有不可判项（⚪）；按口径降级阻断绿，即使其余项在带内也不判绿。
        </p>
      )}
      {unjudged > 0 && (
        <p className="mt-2 text-meta text-muted">
          另有 {unjudged} 项无判据，不计入域灯，也不计为绿。
        </p>
      )}
    </div>
  )
}

function DomainSkeleton() {
  return (
    <div
      className="-mb-px -ml-px grid grid-cols-2 border-t border-line tablet:grid-cols-3 lg:grid-cols-6"
      aria-hidden
    >
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="min-h-[104px] border-b border-l border-line px-3 py-3">
          <div className="h-3 w-20 animate-pulse rounded-control bg-line" />
          <div className="mt-3 h-3 w-14 animate-pulse rounded-control bg-line" />
          <div className="mt-2 h-3 w-24 animate-pulse rounded-control bg-line" />
        </div>
      ))}
    </div>
  )
}

export interface DomainStripProps {
  domains: MetricDomain[]
  events?: MetricEvent[]
  summary?: MetricSummary | null
  asOf?: string | null
  isLoading?: boolean
  isError?: boolean
  onRetry?: () => void
  onDrill?: (target: string) => void
  className?: string
}

export function DomainStrip({
  domains,
  events = [],
  summary,
  asOf,
  isLoading = false,
  isError = false,
  onRetry,
  onDrill,
  className,
}: DomainStripProps) {
  const baseId = useId()
  const [openKey, setOpenKey] = useState<string | null>(null)
  const viewSummary = summary ?? deriveSummary(domains, events)
  const openIndex = domains.findIndex((domain) => domain.domain === openKey)
  const openDomain = openIndex >= 0 ? domains[openIndex] : null
  const openPanelId = `${baseId}-${openIndex}`

  return (
    <Card as="section" padding="none" aria-label="域级信号" className={cn('overflow-hidden', className)}>
      <div className="flex flex-col gap-3 px-4 pb-3 pt-4 sm:px-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h2 className="text-h3 font-semibold text-ink">域级信号</h2>
            {asOf && <span className="text-meta text-muted">as-of {formatDate(asOf)}</span>}
          </div>
          <p className="mt-1 max-w-reading text-meta leading-[18px] text-muted">
            按指标域聚合；异动并入黄；有不可判项（⚪）时不判绿，无判据不阻断；不设全局灯，也不做总分。
          </p>
        </div>
        {!isError && !isLoading && domains.length > 0 && (
          <SummaryLine summary={viewSummary} className="lg:justify-end" />
        )}
      </div>

      {isError ? (
        <div className="border-t border-line px-4 py-3 sm:px-5">
          <InlineError
            action={onRetry ? (
              <button
                type="button"
                onClick={onRetry}
                className="min-h-[44px] px-2 text-body-strong font-medium text-status-critical-text"
              >
                重试
              </button>
            ) : undefined}
          >
            域级信号加载失败，请重试。
          </InlineError>
        </div>
      ) : isLoading ? (
        <DomainSkeleton />
      ) : domains.length === 0 ? (
        <div className="border-t border-line p-4 sm:p-5">
          <EmptyState
            compact
            title="暂无域级信号"
            description="状态面没有可聚合的指标项；录入快照后查看。"
          />
        </div>
      ) : (
        <>
          <div className="-mb-px -ml-px grid grid-cols-2 border-t border-line tablet:grid-cols-3 lg:grid-cols-6">
            {domains.map((domain, index) => (
              <DomainCell
                key={domain.domain}
                domain={domain}
                expanded={openKey === domain.domain}
                panelId={`${baseId}-${index}`}
                onToggle={() => setOpenKey((current) => (
                  current === domain.domain ? null : domain.domain
                ))}
              />
            ))}
          </div>
          {openDomain && (
            <DomainDetails
              domain={openDomain}
              events={events}
              domains={domains}
              panelId={openPanelId}
              onDrill={onDrill}
            />
          )}
        </>
      )}
    </Card>
  )
}