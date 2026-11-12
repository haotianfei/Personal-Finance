'use client'

import { Fragment, useState } from 'react'
import { ChevronDown, Image as ImageIcon } from 'lucide-react'
import type { AuditEntry } from '@/lib/api'
import { EMPTY_VALUE } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ChangeList } from './ChangeList'
import { evidenceIdOf, formatEntryTime } from './shared'

/**
 * 时间线上的一条记录。
 *
 * 术语：审计行＝**操作记录**（无额外徽标，蓝点），痕迹行＝**数据自述**（虚线徽标，灰点）。
 * 动作名与字段名一律用后端给的 `title` / `label`，前端不做词表映射。
 */

/** 动作徽标配色：只给三个高频动作上色，其余中性——时间线不该是调色盘。 */
const TITLE_TONE: Record<string, string> = {
  create: 'bg-status-good/10 text-status-good-text',
  correct: 'bg-brand-50 text-brand-700',
  delete: 'bg-status-critical-surface text-status-critical-text',
}
const TITLE_TONE_FALLBACK = 'border border-line bg-surface text-secondary'

/** 技术细节：固定顺序 + 中文名；值为 null 的行不渲染（没有就是没有，不铺一排「—」）。 */
const TECH_FIELDS: Array<[key: string, label: string]> = [
  ['entity', '实体'],
  ['action', '动作'],
  ['request_id', '请求号'],
  ['idempotency_key', '幂等键'],
  ['prev_hash', '上一条指纹'],
  ['row_hash', '本行指纹'],
]

/** 指纹只露前 8 位（完整值在 title 里）。 */
const HASH_PREVIEW = 8

export interface AuditEntryRowProps {
  entry: AuditEntry
  /** 这条是否正被查看（打开过它的凭证），用于回到抽屉时仍能认出「刚才那条」。 */
  selected: boolean
  /** 库内是 INTEGER 外键；历史/异构形态可能是 `ev-…` 字符串，故两者都收。 */
  onOpenEvidence: (evidenceRef: number | string) => void
}

export function AuditEntryRow({ entry, selected, onOpenEvidence }: AuditEntryRowProps) {
  const [techOpen, setTechOpen] = useState(false)
  const time = formatEntryTime(entry.occurred_at)
  const isTrace = entry.source === 'trace'
  const evidenceRef = entry.evidence_ref
  const evidenceId = evidenceIdOf(evidenceRef)

  const techRows = TECH_FIELDS
    .map(([key, label]) => ({ key, label, value: entry.tech?.[key] ?? null }))
    .filter((row): row is { key: string; label: string; value: string } => row.value !== null && row.value !== '')
    // 行号是取证的第一落点，恒显示
    .concat([{ key: 'row_id', label: '记录行号', value: String(entry.row_id) }])

  return (
    <li className="relative pb-4 pl-5 last:pb-0">
      {/* 时间轴上的点：审计行实心蓝，痕迹行灰（视觉上就是「旁证」） */}
      <span
        aria-hidden
        className={cn(
          'absolute -left-[6px] top-2.5 h-2.5 w-2.5 rounded-full ring-4 ring-surface',
          isTrace ? 'bg-status-unknown' : 'bg-action-primary',
        )}
      />

      <div
        className={cn(
          'rounded-card border px-3 py-2.5 transition-colors duration-fast',
          selected
            ? 'border-action-primary/40 bg-brand-50/60'
            : 'border-line bg-subtle/70',
        )}
      >
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <time className="pf-num text-meta font-semibold text-ink" dateTime={entry.occurred_at} title={time.title}>
            {time.label}
          </time>
          <span
            className={cn(
              'rounded-pill px-1.5 py-0.5 text-micro font-medium leading-none',
              TITLE_TONE[entry.kind] ?? TITLE_TONE_FALLBACK,
            )}
          >
            {entry.title}
          </span>
          {isTrace && (
            <span
              className="inline-flex items-center gap-1 rounded-pill border border-dashed border-line-strong px-1.5 py-0.5 text-micro leading-none text-muted"
              title="数据自述：来自数据内痕迹，不是审计行；同一时刻同字段若两者都有，只看操作记录也够"
            >
              <span aria-hidden className="h-1 w-1 rounded-full bg-status-unknown" />
              数据自述
            </span>
          )}
          <span
            className="ml-auto whitespace-nowrap text-meta text-secondary"
            title={entry.actor !== entry.actor_label ? entry.actor : undefined}
          >
            {entry.actor_label}
          </span>
        </div>

        {entry.changes.length > 0 ? (
          <ChangeList changes={entry.changes} />
        ) : (
          <p className="mt-2 text-meta text-muted">没有字段级明细</p>
        )}

        {entry.note && (
          <p className="mt-2 break-words text-meta text-secondary">备注：{entry.note}</p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
          {evidenceRef &&
            (evidenceId !== null ? (
              <button
                type="button"
                onClick={() => onOpenEvidence(evidenceRef)}
                title={`查看来源凭证 #${evidenceId}`}
                className="inline-flex min-h-[44px] items-center gap-1 rounded-control px-1.5 text-meta font-medium text-link transition-colors hover:bg-surface tablet:min-h-[32px]"
              >
                <ImageIcon size={14} aria-hidden />
                查看凭证
              </button>
            ) : (
              <span className="text-micro text-muted" title="凭证编号不是库内 id（如 ev-…），当前无法直接打开">
                凭证 {evidenceRef}
              </span>
            ))}

          <button
            type="button"
            aria-expanded={techOpen}
            onClick={() => setTechOpen((previous) => !previous)}
            className="inline-flex min-h-[44px] items-center gap-1 rounded-control px-1.5 text-meta text-secondary transition-colors hover:bg-surface hover:text-ink tablet:min-h-[32px]"
          >
            <ChevronDown
              size={14}
              aria-hidden
              className={cn('transition-transform duration-fast', techOpen && 'rotate-180')}
            />
            技术细节
          </button>
        </div>

        {techOpen && (
          <dl className="mt-1.5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-control border border-line bg-surface px-2.5 py-2">
            {techRows.map((row) => {
              const display =
                row.key.endsWith('_hash') && row.value.length > HASH_PREVIEW
                  ? row.value.slice(0, HASH_PREVIEW)
                  : row.value
              return (
                <Fragment key={row.key}>
                  <dt className="whitespace-nowrap text-micro text-muted">{row.label}</dt>
                  <dd
                    className="min-w-0 break-all font-code text-micro text-secondary"
                    title={row.value !== display ? row.value : undefined}
                  >
                    {display || EMPTY_VALUE}
                  </dd>
                </Fragment>
              )
            })}
          </dl>
        )}
      </div>
    </li>
  )
}