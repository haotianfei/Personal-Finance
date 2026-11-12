'use client'

import Link from 'next/link'
import { Bell, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { DirectionText } from '@/components/ui/DirectionText'
import { GradeDot } from '@/components/ui/Grade'
import { EmptyState, InlineError, LoadingState } from '@/components/ui/States'
import { cn } from '@/lib/utils'
import type { MetricDomain, MetricEvent } from '@/types'
import {
  domainName,
  eventDirectionLabel,
  eventPeriodLabel,
  eventSeverityLabel,
  eventTarget,
  eventThresholdText,
  sortEvents,
} from './domain-language'

/**
 * 异动列表：把 `alert_rules` 的上升沿投影为事件，不做「常驻状态」。
 *
 * 显示纪律：
 * - `new` 优先于 `ongoing`；默认黄，只有带型判据才出红；
 * - 行内同时给规则名/域/账期/变动额/变动%/触发阈值，颜色不单独承载语义；
 * - 入口是规则设置（规则→设置·预警），不是预警结果页；
 * - 无事件时分析页显示空态，首页由调用方 `hideWhenEmpty` 隐藏（票：无规则/无事件隐藏）。
 */

export interface EventRowProps {
  event: MetricEvent
  domains?: MetricDomain[]
  /** 域灯展开面板里的紧凑行。 */
  dense?: boolean
}

export function EventRow({ event, domains, dense = false }: EventRowProps) {
  const isNew = event.status === 'new'
  const threshold = eventThresholdText(event)
  const direction = eventDirectionLabel(event.direction)

  return (
    <li className={cn('border-l-2', isNew ? 'border-status-warn' : 'border-transparent')}>
      <Link
        href={eventTarget(event)}
        className={cn(
          'group flex min-h-[44px] flex-col gap-2 py-3 pl-3 pr-3 transition-colors hover:bg-subtle/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus',
          dense
            ? 'gap-1.5 py-2'
            : 'sm:flex-row sm:items-start sm:justify-between sm:gap-4 sm:pl-4',
        )}
      >
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-1.5 shrink-0">
            <GradeDot state={event.severity} />
            <span className="sr-only">严重度{eventSeverityLabel(event.severity)}</span>
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-body-strong font-medium text-ink">{event.rule_name}</span>
              <span
                className={cn(
                  'rounded-pill px-1.5 py-0.5 text-micro font-medium',
                  isNew
                    ? 'bg-status-warn-surface text-status-warn-text'
                    : 'border border-line bg-subtle text-muted',
                )}
              >
                {isNew ? '新' : '持续'}
              </span>
              {event.domain && (
                <span className="text-meta text-muted">{domainName(event.domain, domains)}</span>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-meta leading-[18px] text-muted">
              <span>
                账期 <span className="pf-num text-secondary">{eventPeriodLabel(event.period)}</span>
              </span>
              {event.previous_period && (
                <span>
                  上一有数据 <span className="pf-num">{eventPeriodLabel(event.previous_period)}</span>
                </span>
              )}
              {direction && <span>{direction}</span>}
              {threshold && <span className="break-words">阈值 {threshold}</span>}
            </div>
          </div>
        </div>

        <div className={cn(
          'flex shrink-0 items-center justify-between gap-2 pl-5',
          !dense && 'sm:flex-col sm:items-end sm:pl-0',
        )}>
          <div className="flex flex-wrap items-baseline justify-end gap-x-2 gap-y-0.5">
            <DirectionText value={event.change_amount} unit="cny" size="s" />
            {event.change_percent != null && (
              <DirectionText value={event.change_percent} unit="percent" size="xs" />
            )}
          </div>
          <ChevronRight
            size={14}
            aria-hidden
            className="shrink-0 text-muted transition-colors group-hover:text-link"
          />
        </div>
      </Link>
    </li>
  )
}

export interface EventListProps {
  events: MetricEvent[]
  domains?: MetricDomain[]
  isLoading?: boolean
  isError?: boolean
  onRetry?: () => void
  /** 首页只露前 N 条，其余走「查看全部」；分析页不传。 */
  limit?: number
  /** 首页无事件时整块隐藏（票：无规则/无事件隐藏）；分析页保留空态。 */
  hideWhenEmpty?: boolean
  className?: string
}

export function EventList({
  events,
  domains,
  isLoading = false,
  isError = false,
  onRetry,
  limit,
  hideWhenEmpty = false,
  className,
}: EventListProps) {
  const ordered = sortEvents(events)
  const visible = typeof limit === 'number' ? ordered.slice(0, limit) : ordered
  const newCount = ordered.filter((event) => event.status === 'new').length
  const hiddenCount = ordered.length - visible.length

  if (hideWhenEmpty && !isLoading && !isError && ordered.length === 0) return null

  return (
    <Card as="section" padding="none" aria-label="异动" className={cn('overflow-hidden', className)}>
      <div className="flex items-start justify-between gap-3 px-4 pb-3 pt-4 sm:px-5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h2 className="text-h3 font-semibold text-ink">异动</h2>
            <span className="pf-num text-meta text-muted">{ordered.length} 条</span>
            {newCount > 0 && (
              <span className="rounded-pill bg-status-warn-surface px-2 py-0.5 text-micro font-medium text-status-warn-text">
                新 {newCount}
              </span>
            )}
          </div>
          <p className="mt-1 max-w-reading text-meta leading-[18px] text-muted">
            只看上升沿；未回落不重复提示。按严重度、账期排序。
          </p>
        </div>
        <Link
          href="/settings/automation/alerts"
          className="inline-flex min-h-[44px] shrink-0 items-center gap-1 whitespace-nowrap rounded-control px-2 text-meta font-medium text-link"
        >
          规则设置 <ChevronRight size={14} aria-hidden />
        </Link>
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
            异动加载失败，请重试。
          </InlineError>
        </div>
      ) : isLoading ? (
        <div className="border-t border-line px-4 py-3 sm:px-5">
          <LoadingState rows={3} compact label="正在加载异动" />
        </div>
      ) : ordered.length === 0 ? (
        <div className="border-t border-line p-4 sm:p-5">
          <EmptyState
            compact
            icon={Bell}
            title="暂无新异动"
            description="预警规则未触发；规则只在上升沿出现，回落再触发才会重新提示。"
            action={(
              <Link
                href="/settings/automation/alerts"
                className="pf-card-interactive inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-4 text-body-strong font-medium text-ink"
              >
                去设置预警规则
              </Link>
            )}
            className="border-0 bg-transparent py-4"
          />
        </div>
      ) : (
        <>
          <ul className="border-t border-line px-2 sm:px-3">
            {visible.map((event) => (
              <EventRow key={event.id} event={event} domains={domains} />
            ))}
          </ul>
          {hiddenCount > 0 && (
            <div className="border-t border-line px-4 py-2 sm:px-5">
              <Link
                href="/analysis#signals"
                className="inline-flex min-h-[44px] items-center gap-1 text-meta font-medium text-link"
              >
                查看全部 {ordered.length} 条 <ChevronRight size={14} aria-hidden />
              </Link>
            </div>
          )}
        </>
      )}
    </Card>
  )
}