'use client'

import { Fragment, useState } from 'react'
import { ChevronDown, History, Image as ImageIcon, Milestone } from 'lucide-react'
import { EMPTY_VALUE } from '@/lib/format'
import { cn } from '@/lib/utils'
import { EmptyState, ErrorState, LoadingState } from '@/components/ui'
import { evidenceIdOf } from './shared'
import {
  actionLabelOf,
  actionToneOf,
  entityLabelOf,
  formatLogTime,
  hashPreview,
  historyDisabledReason,
  isAssetRelatedEntity,
  isGenerationStart,
  itemIdForHistory,
  rebuildDescriptionOf,
  rebuildSummaryOf,
  summaryOf,
  type GlobalLogRow,
} from './GlobalLogModel'

/**
 * 操作记录列表（设置 → 操作日志）。
 *
 * 与行级抽屉同构：操作＝`entity · action` 的中文名（后端没给中文就直接显示原值）、
 * 主体＝我/系统、排障字段收进「技术细节」。差别只有两点：
 * 1. 一屏看多行，所以不铺逐字段 diff——摘要只说「因为什么 / 改了哪些字段」，
 *    旧→新回抽屉看；
 * 2. 资产项相关的行能按对象 id 直接跳进同一条操作历史时间线，不另造抽屉。
 */

export interface GlobalLogEmpty {
  title: React.ReactNode
  description?: React.ReactNode
  action?: React.ReactNode
}

export interface GlobalLogTableProps {
  rows: GlobalLogRow[]
  loading: boolean
  /** 已有内容时重取失败：贴着列表提示，不顶掉已加载的行。 */
  staleError?: unknown
  error: unknown
  onRetry: () => void
  empty: GlobalLogEmpty
  onOpenItemHistory: (itemId: number) => void
  onOpenEvidence: (evidenceId: number, evidenceRef: string) => void
}

const HEADER_CELL = 'whitespace-nowrap px-3 text-left text-meta font-medium text-muted tablet:px-4'
const BODY_CELL = 'px-3 py-2 align-middle tablet:px-4'

function ActionPill({ row }: { row: GlobalLogRow }) {
  const entity = entityLabelOf(row.entity) ?? EMPTY_VALUE
  const action = actionLabelOf(row.action) ?? EMPTY_VALUE
  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1 rounded-pill px-1.5 py-0.5 text-micro leading-none',
        actionToneOf(row.action),
      )}
      title={`${row.entity ?? '—'}.${row.action ?? '—'}`}
    >
      <span className="truncate font-normal opacity-75">{entity}</span>
      <span aria-hidden className="opacity-40">·</span>
      <span className="truncate font-medium">{action}</span>
    </span>
  )
}

function SummaryText({ row, variant }: { row: GlobalLogRow; variant: 'row' | 'card' }) {
  const summary = summaryOf(row)
  return (
    <span
      className={cn(
        'block min-w-0 text-meta',
        variant === 'row' ? 'truncate' : 'line-clamp-2',
        summary.muted ? 'text-muted' : 'text-secondary',
      )}
      title={summary.title ?? (summary.text !== EMPTY_VALUE ? summary.text : undefined)}
    >
      {summary.text}
    </span>
  )
}

function ObjectCell({ row }: { row: GlobalLogRow }) {
  if (row.objectId === null) {
    return (
      <span className="text-meta text-muted" title="这条记录没有对象（如库结构迁移）">
        {EMPTY_VALUE}
      </span>
    )
  }
  return <span className="pf-num text-meta text-secondary">#{row.objectId}</span>
}

function EvidenceCell({
  row,
  onOpenEvidence,
}: {
  row: GlobalLogRow
  onOpenEvidence: (evidenceId: number, evidenceRef: string) => void
}) {
  const evidenceRef = row.evidenceRef
  const evidenceId = evidenceIdOf(evidenceRef)
  if (!evidenceRef) {
    return <span className="text-meta text-muted">{EMPTY_VALUE}</span>
  }
  if (evidenceId === null) {
    return (
      <span className="text-meta text-muted" title="凭证编号不是库内 id，当前无法直接打开">
        {evidenceRef}
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={() => onOpenEvidence(evidenceId, evidenceRef)}
      title={`查看来源凭证 #${evidenceId}`}
      className="inline-flex min-h-[44px] items-center gap-1 rounded-control px-1 text-meta font-medium text-link transition-colors hover:bg-subtle tablet:min-h-[32px]"
    >
      <ImageIcon size={13} aria-hidden />
      <span className="pf-num">#{evidenceId}</span>
    </button>
  )
}

/**
 * 「查看资产项历史」。
 *
 * `ItemHistoryDrawer` 要的是**资产项 id**，而审计行只有「对象 id」。只有 `asset_item` /
 * `classification` 的 `row_id` 恰好就是资产项 id（见 `GlobalLogModel.itemIdForHistory`）；
 * 其余相关实体（快照、归属改判、本金变更）在禁用态说明原因，绝不猜一个 id 去开错项。
 */
function HistoryJumpButton({
  row,
  onOpenItemHistory,
  size,
}: {
  row: GlobalLogRow
  onOpenItemHistory: (itemId: number) => void
  size: 'row' | 'card'
}) {
  if (!isAssetRelatedEntity(row.entity)) return null
  const itemId = itemIdForHistory(row)
  const disabled = itemId === null
  const compact = size === 'card'
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (itemId !== null) onOpenItemHistory(itemId)
      }}
      title={
        disabled
          ? `无法按对象 id 打开：${historyDisabledReason(row)}`
          : `查看资产项历史（按对象 id #${itemId} 打开同一条时间线）`
      }
      aria-label={
        disabled
          ? `查看资产项历史（不可用）：${historyDisabledReason(row)}`
          : '查看资产项历史'
      }
      className={cn(
        'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-control px-1.5 text-micro font-medium transition-colors',
        compact ? 'min-h-[44px]' : 'min-h-[32px]',
        disabled ? 'cursor-not-allowed text-disabled' : 'text-link hover:bg-subtle',
      )}
    >
      <History size={14} aria-hidden />
      查看资产项历史
    </button>
  )
}

function TechToggle({
  open,
  onToggle,
  size,
}: {
  open: boolean
  onToggle: () => void
  size: 'row' | 'card'
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      onClick={onToggle}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-control px-1.5 text-micro text-secondary transition-colors hover:bg-subtle hover:text-ink',
        size === 'card' ? 'min-h-[44px]' : 'min-h-[32px]',
      )}
    >
      <ChevronDown
        size={14}
        aria-hidden
        className={cn('transition-transform duration-fast', open && 'rotate-180')}
      />
      技术细节
    </button>
  )
}

/** 技术细节：抽屉里那三样（请求号／幂等键／上一条指纹）+ 行内可见的对象 id。 */
function TechPanel({ row }: { row: GlobalLogRow }) {
  const fields = [
    { key: 'request_id', label: '请求号', value: row.requestId, title: undefined as string | undefined },
    { key: 'idempotency_key', label: '幂等键', value: row.idempotencyKey, title: undefined as string | undefined },
    { key: 'prev_hash', label: '上一条指纹', value: hashPreview(row.prevHash), title: row.prevHash ?? undefined },
    {
      key: 'audit_id',
      label: '记录行号',
      value: row.auditId === null ? null : String(row.auditId),
      title: '审计行自身的 id（与对象 id 不是一回事）',
    },
  ].filter(
    (field): field is { key: string; label: string; value: string; title: string | undefined } =>
      field.value !== null && field.value !== '',
  )

  const itemId = itemIdForHistory(row)

  return (
    <div className="animate-fade-in rounded-control border border-line bg-surface px-2.5 py-2">
      {fields.length === 0 ? (
        <p className="text-micro text-muted">这条记录没有可展开的排障字段。</p>
      ) : (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
          {fields.map((field) => (
            <Fragment key={field.key}>
              <dt className="whitespace-nowrap text-micro text-muted">{field.label}</dt>
              <dd className="min-w-0 break-all font-code text-micro text-secondary" title={field.title}>
                {field.value}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
      <p className="mt-1.5 text-micro text-muted">
        对象{' '}
        <span className="pf-num text-secondary">
          {row.objectId === null ? EMPTY_VALUE : `#${row.objectId}`}
        </span>
        {isAssetRelatedEntity(row.entity) && (
          <span className="ml-2">
            {itemId === null
              ? historyDisabledReason(row)
              : `按对象 id #${itemId} 可打开该资产项的操作历史`}
          </span>
        )}
      </p>
    </div>
  )
}

/**
 * 分代分隔条（`schema/rebuild_import`）。
 *
 * 整库重建会在新库写下这一条，它把日志切成两段：**上方是当前库代，下方是上一代**。
 * 它不是一条可操作记录——不可点、不可展开，`tabIndex=0` 只为让键盘也能读到
 * 「上方属于哪一代、重建于何时、桥从哪来」这句说明（`title` 只能鼠标看）。
 */
function GenerationDividerBody({ row }: { row: GlobalLogRow }) {
  const time = formatLogTime(row.occurredAt)
  const description = rebuildDescriptionOf(row)
  return (
    <div
      role="note"
      tabIndex={0}
      title={description}
      aria-label={`本代起点。${description}`}
      className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-control px-1 py-0.5"
    >
      <span className="inline-flex shrink-0 items-center gap-1.5 text-micro font-semibold text-brand-700">
        <Milestone size={13} aria-hidden />
        本代起点
      </span>
      <span
        aria-hidden
        className="hidden min-w-6 flex-1 border-t border-dashed border-line-strong tablet:block"
      />
      <span className="pf-num shrink-0 text-micro text-muted">{time.label}</span>
      <span aria-hidden className="shrink-0 text-micro text-muted">·</span>
      <span className="shrink-0 text-micro text-secondary">{rebuildSummaryOf(row)}</span>
    </div>
  )
}

function GenerationDividerRow({ row }: { row: GlobalLogRow }) {
  return (
    <tr className="bg-subtle">
      <td colSpan={7} className="px-3 py-1.5 tablet:px-4">
        <GenerationDividerBody row={row} />
      </td>
    </tr>
  )
}

function GenerationDividerCard({ row }: { row: GlobalLogRow }) {
  return (
    <li className="rounded-card border border-dashed border-line-strong bg-subtle px-2 py-1.5">
      <GenerationDividerBody row={row} />
    </li>
  )
}

function RowDesktop({
  row,
  onOpenItemHistory,
  onOpenEvidence,
}: {
  row: GlobalLogRow
  onOpenItemHistory: (itemId: number) => void
  onOpenEvidence: (evidenceId: number, evidenceRef: string) => void
}) {
  const [open, setOpen] = useState(false)
  const time = formatLogTime(row.occurredAt)

  return (
    <>
      <tr
        className={cn(
          'text-body text-secondary transition-colors',
          open ? 'bg-subtle/60' : 'hover:bg-subtle/40',
        )}
      >
        <td className={BODY_CELL}>
          <time
            className="pf-num whitespace-nowrap text-meta text-ink"
            dateTime={row.occurredAt}
            title={time.title}
          >
            {time.label}
          </time>
        </td>
        <td className={BODY_CELL}>
          <ActionPill row={row} />
        </td>
        <td className={cn(BODY_CELL, 'whitespace-nowrap text-meta')}>{row.actorLabel}</td>
        <td className={BODY_CELL}>
          <ObjectCell row={row} />
        </td>
        <td className={BODY_CELL}>
          <EvidenceCell row={row} onOpenEvidence={onOpenEvidence} />
        </td>
        <td className={BODY_CELL}>
          <SummaryText row={row} variant="row" />
        </td>
        <td className="px-2 py-1">
          <div className="flex items-center justify-end gap-1">
            <HistoryJumpButton row={row} onOpenItemHistory={onOpenItemHistory} size="row" />
            <TechToggle open={open} onToggle={() => setOpen((previous) => !previous)} size="row" />
          </div>
        </td>
      </tr>
      {open && (
        <tr className="bg-subtle/40">
          <td colSpan={7} className="px-3 py-2 tablet:px-4">
            <TechPanel row={row} />
          </td>
        </tr>
      )}
    </>
  )
}

function CardMobile({
  row,
  onOpenItemHistory,
  onOpenEvidence,
}: {
  row: GlobalLogRow
  onOpenItemHistory: (itemId: number) => void
  onOpenEvidence: (evidenceId: number, evidenceRef: string) => void
}) {
  const [open, setOpen] = useState(false)
  const time = formatLogTime(row.occurredAt)

  return (
    <li className="rounded-card border border-line bg-surface p-3 shadow-card">
      {/* 首行：时间 + 操作 + 对象 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <time
          className="pf-num shrink-0 text-meta font-semibold text-ink"
          dateTime={row.occurredAt}
          title={time.title}
        >
          {time.label}
        </time>
        <ActionPill row={row} />
        <span className="pf-num shrink-0 text-micro text-muted">
          对象 {row.objectId === null ? EMPTY_VALUE : `#${row.objectId}`}
        </span>
      </div>

      {/* 次行：摘要 */}
      <SummaryText row={row} variant="card" />

      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-micro text-muted">
        <span>{row.actorLabel}</span>
        <EvidenceCell row={row} onOpenEvidence={onOpenEvidence} />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-line pt-1">
        <HistoryJumpButton row={row} onOpenItemHistory={onOpenItemHistory} size="card" />
        <TechToggle open={open} onToggle={() => setOpen((previous) => !previous)} size="card" />
      </div>

      {open && (
        <div className="mt-1.5">
          <TechPanel row={row} />
        </div>
      )}
    </li>
  )
}

export function GlobalLogTable({
  rows,
  loading,
  staleError,
  error,
  onRetry,
  empty,
  onOpenItemHistory,
  onOpenEvidence,
}: GlobalLogTableProps) {
  if (error && rows.length === 0) {
    return (
      <ErrorState
        title="操作记录加载失败"
        description={error instanceof Error ? error.message : '请稍后重试'}
        onRetry={onRetry}
      />
    )
  }

  if (loading) {
    return <LoadingState rows={6} label="正在加载操作记录" />
  }

  if (rows.length === 0) {
    return <EmptyState title={empty.title} description={empty.description} action={empty.action} />
  }

  return (
    <div className="space-y-2">
      {staleError !== undefined && staleError !== null && (
        <p
          role="status"
          className="flex flex-wrap items-center gap-x-2 rounded-control border border-status-warn/30 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text"
        >
          这页没刷新成功，显示的仍是上一次的结果。
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex min-h-[32px] items-center rounded-control px-1.5 text-meta font-medium underline underline-offset-2"
          >
            重试
          </button>
        </p>
      )}

      <p className="hidden text-meta text-muted tablet:block tablet-l:hidden">
        左右滑动查看完整表格。
      </p>

      {/* ≥768：表格（固定列宽，摘要列吃掉剩余空间） */}
      <div className="hidden overflow-hidden rounded-card border border-line bg-surface tablet:block">
        <div className="max-h-[70vh] overflow-auto">
          <table
            className="w-full"
            style={{ minWidth: 1000, tableLayout: 'fixed' }}
            aria-label="操作记录"
          >
            <caption className="sr-only">
              操作记录：时间、操作、主体、对象、凭证、摘要；每行可展开技术细节
            </caption>
            <thead className="sticky top-0 z-sticky bg-subtle">
              <tr className="border-b border-line">
                <th scope="col" className={HEADER_CELL} style={{ width: 168 }}>时间</th>
                <th scope="col" className={HEADER_CELL} style={{ width: 208 }}>操作</th>
                <th scope="col" className={HEADER_CELL} style={{ width: 64 }}>主体</th>
                <th scope="col" className={HEADER_CELL} style={{ width: 88 }}>对象</th>
                <th scope="col" className={HEADER_CELL} style={{ width: 92 }}>凭证</th>
                <th scope="col" className={HEADER_CELL}>摘要</th>
                <th
                  scope="col"
                  style={{ width: 196 }}
                  className="px-2 text-right text-meta font-medium text-muted"
                >
                  操作
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((row) =>
                isGenerationStart(row) ? (
                  <GenerationDividerRow key={row.key} row={row} />
                ) : (
                  <RowDesktop
                    key={row.key}
                    row={row}
                    onOpenItemHistory={onOpenItemHistory}
                    onOpenEvidence={onOpenEvidence}
                  />
                ),
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* <768：卡片 */}
      <ul className="space-y-2 tablet:hidden" aria-label="操作记录">
        {rows.map((row) =>
          isGenerationStart(row) ? (
            <GenerationDividerCard key={row.key} row={row} />
          ) : (
            <CardMobile
              key={row.key}
              row={row}
              onOpenItemHistory={onOpenItemHistory}
              onOpenEvidence={onOpenEvidence}
            />
          ),
        )}
      </ul>
    </div>
  )
}