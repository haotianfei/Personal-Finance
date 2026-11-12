'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatCurrency, formatDateTime, EMPTY_VALUE } from '@/lib/format'
import { InlineError } from '@/components/ui/States'
import type { EvidenceRecognitionItem, EvidenceRecognitionSummary } from '@/types'
import { MetaRow } from './shared'

/**
 * 快照引擎：中文 + 括号原值，便于与接口字段对照（业主 2026-11-06 定）；
 * 未识别引擎（无值）→ 「—」，未知取值原样显示不猜测。
 */
function snapshotEngineLabel(engine: string | null | undefined): string {
  if (!engine) return EMPTY_VALUE
  if (engine === 'ai') return 'AI 识别（ai）'
  if (engine === 'ocr') return 'OCR 识别（ocr）'
  return engine
}

/** 识别来源映射（spec D1）：`server` → 服务端 OCR，`client` → 前端 AI；其余「—」。 */
function recognitionSourceLabel(source: EvidenceRecognitionSummary['source']): string {
  if (source === 'server') return '服务端 OCR'
  if (source === 'client') return '前端 AI'
  return EMPTY_VALUE
}

/** 条数展示：null（段缺失/读不出）→ 「—」；0 要照常显示，故不能靠真值判断。 */
function countLabel(count: number | null | undefined): string {
  return typeof count === 'number' ? String(count) : EMPTY_VALUE
}

/**
 * 原始块：**只报条数，不渲染内容**（spec D3/D5）——原文是 OCR 旁证，界面不需要。
 * 有数组 → 「原始块 N 条」；没有数组但被体积丢掉 → 说明原因（不是「0 条」）；
 * 本来就没有（AI 路径 `blocks=null` 且未丢）→ null（该行不显示）。
 */
function blocksLabel(
  blocks: unknown[] | null | undefined,
  dropped: boolean | undefined,
): string | null {
  if (Array.isArray(blocks)) return `原始块 ${blocks.length} 条`
  if (dropped) return '原始块已因体积丢弃'
  return null
}

/**
 * 快照的一段（识别行 / 采纳行）：有行逐条列出「序号 + 名称 + 金额」，
 * `items` 被体积降级摘掉时改报条数并明说「仅存条数」——不留空白（spec D3）。
 * 金额原样展示、不给编辑入口（spec D4：快照是旁证，改了就没了对照）。
 */
function RecognitionItemBlock({
  title,
  items,
  itemCount,
  emptyText = '未存识别行',
}: {
  title: string
  items: EvidenceRecognitionItem[] | null | undefined
  itemCount?: number | null
  emptyText?: string
}) {
  const list = Array.isArray(items) ? items : null
  const count = list ? list.length : (typeof itemCount === 'number' ? itemCount : null)
  return (
    <div className="min-w-0">
      <p className="text-meta text-muted">
        {title}（{count === null ? EMPTY_VALUE : `${count} 条`}）
      </p>
      {list === null ? (
        <p className="mt-1 text-body text-secondary">
          {count === null ? emptyText : '仅存条数（快照因体积超限只保留了条数）'}
        </p>
      ) : list.length === 0 ? (
        <p className="mt-1 text-body text-secondary">无</p>
      ) : (
        <ol className="mt-1 space-y-1">
          {list.map((item, index) => (
            <li
              key={`${index}-${item.name}`}
              className="flex min-w-0 items-baseline justify-between gap-3 text-body"
            >
              <span className="min-w-0 break-words text-ink">
                <span className="pf-num mr-1.5 text-meta text-muted">{index + 1}.</span>
                {item.name}
              </span>
              <span className="pf-num shrink-0 text-ink">{formatCurrency(item.amount)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/**
 * 详情弹层的「识别快照」一节（evidence-recognition-view D1–D3）。
 *
 * - 概要字段**全部取自列表项已有的 `recognition_summary`**：打开抽屉零额外请求（D1）。
 *   `summary` 为 null 即未识别，整节只显示「未识别」且**不给展开入口**。
 * - 本体（识别行 / 采纳行 / 原始块条数）只在点「查看识别行」时才请求 D2 端点：
 *   `useQuery({ enabled: expanded })` 懒加载；`staleTime: Infinity` 让折叠后再展开
 *   直接吃缓存——同一抽屉重复开合不重复请求（D3/验收 3）。
 *
 * 单独成文件是为了让共用容器 `EvidenceDetail`（record-provenance D8）保持零副作用：
 * 「查看识别行」是本节自带的可选交互，它的请求与展开状态不外溢到容器。
 */
export function RecognitionSection({
  evidenceId,
  summary,
}: {
  evidenceId: number
  summary: EvidenceRecognitionSummary | null | undefined
}) {
  const [expanded, setExpanded] = useState(false)
  const snapshotQuery = useQuery({
    queryKey: ['evidence-recognition', evidenceId],
    queryFn: () => api.getEvidenceRecognition(evidenceId),
    enabled: expanded,
    // 快照写入后不会从本页回流（只读旁证）：缓存视为永久新鲜，避免重复请求
    staleTime: Infinity,
  })

  if (!summary) {
    return (
      <div className="rounded-control border border-line bg-surface p-3">
        <p className="text-body-strong text-ink">识别快照</p>
        <p className="mt-1 text-body text-secondary">
          未识别（该凭证没有识别快照）。
        </p>
      </div>
    )
  }

  const snapshot = snapshotQuery.data?.snapshot ?? null
  const blockLine = blocksLabel(snapshot?.recognized?.blocks, snapshot?.recognized?.blocks_dropped)

  return (
    <div className="rounded-control border border-line bg-surface p-3">
      <p className="text-body-strong text-ink">识别快照</p>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-3 tablet:grid-cols-2">
        <MetaRow label="识别引擎">
          <span className="pf-num">{snapshotEngineLabel(summary.engine)}</span>
        </MetaRow>
        <MetaRow label="来源">{recognitionSourceLabel(summary.source)}</MetaRow>
        <MetaRow label="识别时间">
          <span className="pf-num">{formatDateTime(summary.parsed_at)}</span>
        </MetaRow>
        <MetaRow label="解析器版本">
          <span className="pf-num">{summary.parser_version || EMPTY_VALUE}</span>
        </MetaRow>
        <MetaRow label="识别行数">
          <span className="pf-num">{countLabel(summary.recognized_items)}</span>
        </MetaRow>
        <MetaRow label="采纳行数">
          <span className="pf-num">{countLabel(summary.adopted_items)}</span>
        </MetaRow>
      </dl>
      {summary.blocks_dropped && (
        <p className="mt-2 text-meta text-secondary">原始块已因体积丢弃</p>
      )}

      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        className="mt-3 inline-flex min-h-[44px] items-center gap-1.5 rounded-control text-body-strong font-medium text-link"
      >
        {expanded ? '收起识别行' : '查看识别行'}
      </button>

      {expanded && (
        <div className="mt-2 space-y-3 border-t border-line pt-3">
          {snapshotQuery.isPending ? (
            <p className="text-body text-secondary">加载中…</p>
          ) : snapshotQuery.isError ? (
            <InlineError
              action={(
                <button
                  type="button"
                  onClick={() => void snapshotQuery.refetch()}
                  className="text-body-strong font-medium text-link"
                >
                  重试
                </button>
              )}
            >
              识别快照加载失败：{snapshotQuery.error?.message || '未知错误'}
            </InlineError>
          ) : snapshot === null ? (
            <p className="text-body text-secondary">快照为空（该凭证未存下识别行）。</p>
          ) : (
            <>
              {snapshot.note === 'size_limit' && (
                <p className="text-meta text-secondary">
                  快照体积超限已降级为标记，识别行与原始块均未保留。
                </p>
              )}
              <RecognitionItemBlock
                title="识别行"
                items={snapshot.recognized?.items}
                itemCount={snapshot.recognized?.item_count}
              />
              <RecognitionItemBlock
                title="已采纳"
                items={snapshot.adopted?.items}
                itemCount={snapshot.adopted?.item_count}
                emptyText="尚无采纳记录"
              />
              {blockLine && <p className="text-meta text-secondary">{blockLine}</p>}
            </>
          )}
        </div>
      )}
    </div>
  )
}