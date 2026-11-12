'use client'

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { UIEvent } from 'react'
import { createPortal } from 'react-dom'
import { useInfiniteQuery } from '@tanstack/react-query'
import { Check, Filter, History, X } from 'lucide-react'
import { getItemHistory } from '@/lib/api'
import type { AuditEntry } from '@/lib/api'
import { cn } from '@/lib/utils'
import { EmptyState, ErrorState, InlineError, LoadingState } from '@/components/ui'
import { AuditEntryRow } from './AuditEntryRow'
import { EvidenceDialog } from './EvidenceDialog'
import { entryKeyOf, evidenceIdOf } from './shared'
import { useDrawerA11y } from './useDrawerA11y'
import styles from './HistoryDrawer.module.css'

/** 首屏 50 条（与后端默认一致）；下一页由 `next_cursor` 决定。 */
const PAGE_LIMIT = 50
/** 兜底页数上限：游标万一不前进，最多翻 40 页（2000 条）就停，不打爆后端。 */
const MAX_PAGES = 40
/** 距底部多少像素时预取下一页（不是等到底才请求）。 */
const LOAD_MORE_THRESHOLD = 240

/**
 * 资产项「操作历史」抽屉（票 02 J1）。
 *
 * 一条时间线 = 操作记录（审计行）∪ 数据自述（数据内痕迹），合并与排序在后端做，
 * 这里只负责分页、筛选与呈现。入口有三处，共用这一个组件：
 * 记录页行操作菜单、凭证详情「受影响资产项」、以及抽屉内换主体。
 *
 * 取证动线：某条记录 →「查看凭证」→ 凭证详情叠在**本抽屉之上**；关掉凭证后
 * 抽屉不卸载、滚动位置与选中条目原地保留（组件一直挂着，状态都在本地）。
 */
export interface ItemHistoryDrawerProps {
  open: boolean
  itemId: number
  /** 首帧标题用的显示名；拿到响应后以后端 `display_name` 为准。 */
  displayName?: string
  onClose: () => void
}

export function ItemHistoryDrawer({
  open,
  itemId,
  displayName,
  onClose,
}: ItemHistoryDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const titleId = useId()

  /** 当前展示的是哪个资产项：抽屉内可从凭证的「受影响资产项」跳到别的项。 */
  const [subject, setSubject] = useState<{ id: number; name?: string }>({
    id: itemId,
    name: displayName,
  })
  const [auditOnly, setAuditOnly] = useState(false)
  /** 最近一次打开凭证的那条（关掉凭证后仍高亮，便于「刚才看的是这条」）。 */
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<{ id: number; ref: number | string } | null>(null)

  // 外部换行（记录页点了另一行）时重设主体
  useEffect(() => {
    setSubject({ id: itemId, name: displayName })
  }, [itemId, displayName])

  // 换主体或切筛选＝换一份列表：回到顶部、清掉选中（选中的那条可能已不在结果里）
  useEffect(() => {
    setSelectedKey(null)
    bodyRef.current?.scrollTo({ top: 0 })
  }, [subject.id, auditOnly])

  const handleClose = useCallback(() => {
    setEvidence(null)
    onClose()
  }, [onClose])

  useDrawerA11y({
    open,
    onClose: handleClose,
    containerRef: panelRef,
    initialFocusRef: closeRef,
    // 凭证详情在场时把键盘（Esc / Tab）让给上层，一次 Esc 只关一层
    suspended: evidence !== null,
  })

  const sources = auditOnly ? 'audit' : 'audit,trace'

  const historyQuery = useInfiniteQuery({
    queryKey: ['itemHistory', subject.id, sources],
    queryFn: ({ pageParam }) =>
      getItemHistory(subject.id, { limit: PAGE_LIMIT, cursor: pageParam, sources }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage, allPages) => {
      // 没有游标＝到底；空页＝到底；页数到上限＝兜底停（游标不前进时不死循环）
      if (!lastPage.next_cursor) return undefined
      if (lastPage.entries.length === 0) return undefined
      if (allPages.length >= MAX_PAGES) return undefined
      return lastPage.next_cursor
    },
    enabled: open,
    staleTime: 15_000,
    retry: false,
  })

  const rows = useMemo(() => {
    const seen = new Set<string>()
    const list: Array<{ entry: AuditEntry; key: string }> = []
    for (const page of historyQuery.data?.pages ?? []) {
      for (const entry of page.entries) {
        // 后端已按 sources 过滤；这里再拦一道，防「只看操作」漏出痕迹行
        if (auditOnly && entry.source === 'trace') continue
        const key = entryKeyOf(entry)
        if (seen.has(key)) continue
        seen.add(key)
        list.push({ entry, key })
      }
    }
    return list
  }, [historyQuery.data, auditOnly])

  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget
    const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    if (distanceToBottom > LOAD_MORE_THRESHOLD) return
    if (!historyQuery.hasNextPage || historyQuery.isFetchingNextPage) return
    // 上一次翻页失败后不再跟着滚动自动重试（否则每次滚动都打一次后端），交给「重试」按钮
    if (historyQuery.isFetchNextPageError) return
    void historyQuery.fetchNextPage()
  }

  const openEvidence = (ref: number | string, key: string) => {
    const id = evidenceIdOf(ref)
    if (id === null) return
    setSelectedKey(key)
    setEvidence({ id, ref })
  }

  // 凭证详情里点了别的资产项：换这条时间线（不再开第二个抽屉）
  const handleSelectAffectedItem = (next: { item_id: number; display_name: string }) => {
    setEvidence(null)
    setSubject({ id: next.item_id, name: next.display_name })
    // 上层弹层卸载时会把焦点还给「查看凭证」按钮，而它属于上一条时间线、这会儿已不在；
    // 把焦点收回抽屉本身（tabIndex=-1），读屏会跟着重新播报标题。
    window.requestAnimationFrame(() => panelRef.current?.focus())
  }

  if (!open || typeof document === 'undefined') return null

  const pages = historyQuery.data?.pages
  const firstPage = pages?.[0]
  const lastPage = pages && pages.length > 0 ? pages[pages.length - 1] : undefined
  const name = firstPage?.display_name || subject.name || `资产项 #${subject.id}`
  const error = historyQuery.error
  const showList = rows.length > 0
  const firstLoading = historyQuery.isLoading

  const statusText = historyQuery.isFetchingNextPage
    ? '正在加载更早的记录…'
    : historyQuery.hasNextPage
      ? '滚动到底部加载更早的记录'
      : lastPage?.truncated
        ? '更早的记录未全部载入（后端标记截断）'
        : showList
          ? '没有更早的记录了'
          : ''

  return createPortal(
    <>
      <div className="fixed inset-0 z-modal" role="presentation">
        <div
          className="absolute inset-0 animate-fade-in bg-black/40"
          onClick={handleClose}
          aria-hidden="true"
        />

        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          className={cn(
            'absolute flex flex-col bg-surface text-ink shadow-overlay',
            // 移动端：整屏（含安全区），与 ResponsiveDialog 的 sheet 同口径
            'inset-0 h-[var(--pf-viewport-h)] w-full pt-[var(--pf-safe-top)]',
            // ≥768：右侧抽屉，贴边、竖向铺满
            'tablet:inset-y-0 tablet:left-auto tablet:right-0 tablet:h-auto tablet:w-[min(580px,calc(100vw-2rem))] tablet:border-l tablet:border-line tablet:pt-0',
            styles.panel,
          )}
        >
          <header className="shrink-0 border-b border-line px-4 pb-3 pt-4 tablet:px-6">
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-1.5 text-micro font-medium text-muted">
                  <History size={13} aria-hidden />
                  操作历史
                </p>
                <h2
                  id={titleId}
                  className="mt-0.5 truncate text-h2 font-semibold text-ink"
                  title={name}
                >
                  {name}
                </h2>
                <p className="mt-0.5 text-meta text-muted">
                  资产项 <span className="pf-num">#{subject.id}</span>
                  {firstPage && (
                    <>
                      {' · 已加载 '}
                      <span className="pf-num">{rows.length}</span>
                      {' 条'}
                    </>
                  )}
                </p>
              </div>
              <button
                ref={closeRef}
                type="button"
                onClick={handleClose}
                aria-label="关闭操作历史"
                className="-mr-2 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:text-ink"
              >
                <X size={20} aria-hidden />
              </button>
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
              <button
                type="button"
                aria-pressed={auditOnly}
                onClick={() => setAuditOnly((previous) => !previous)}
                className={cn(
                  'inline-flex min-h-[44px] items-center gap-1.5 rounded-pill border px-3 text-meta font-medium transition-colors tablet:min-h-[32px]',
                  auditOnly
                    ? 'border-action-primary/30 bg-brand-50 text-brand-700'
                    : 'border-line-strong bg-surface text-secondary hover:text-ink',
                )}
              >
                {auditOnly ? <Check size={14} aria-hidden /> : <Filter size={14} aria-hidden />}
                只看操作
              </button>
              <p
                className="w-full text-micro text-muted tablet:w-auto tablet:min-w-0 tablet:flex-1 tablet:truncate"
                title="操作记录＝审计行；数据自述＝数据内痕迹（同一时刻同字段两者都有时，以操作记录为准）"
              >
                操作记录＝审计行 · 数据自述＝数据内痕迹 · 最新在上
              </p>
            </div>
          </header>

          <div
            ref={bodyRef}
            onScroll={handleScroll}
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 tablet:px-6"
          >
            {firstLoading && <LoadingState rows={5} compact label="正在加载操作历史" />}

            {!firstLoading && error && !showList && (
              <ErrorState
                compact
                title="操作历史加载失败"
                description={error instanceof Error ? error.message : '请稍后重试'}
                onRetry={() => { void historyQuery.refetch() }}
              />
            )}

            {!firstLoading && !error && !showList && (
              <EmptyState
                compact
                icon={History}
                title={auditOnly ? '只看操作时没有记录' : '这个资产项还没有操作记录'}
                description={
                  auditOnly
                    ? '当前只显示操作记录（审计行）。关掉「只看操作」可以看到数据自述（数据内痕迹）。'
                    : '改动金额、身份或分类之后，这里会按时间倒序列出每一次改动，以及依据的凭证。'
                }
              />
            )}

            {showList && (
              <ol className="relative border-l border-line">
                {rows.map(({ entry, key }) => (
                  <AuditEntryRow
                    key={key}
                    entry={entry}
                    selected={selectedKey === key}
                    onOpenEvidence={(ref) => openEvidence(ref, key)}
                  />
                ))}
              </ol>
            )}

            {/* 已经在看内容时下一页失败：贴着列表底部提示，不顶掉已加载的条目 */}
            {showList && error && (
              <InlineError
                className="mt-3"
                action={
                  <button
                    type="button"
                    onClick={() => { void historyQuery.fetchNextPage() }}
                    className="inline-flex min-h-[32px] items-center rounded-control px-2 text-meta font-medium text-status-critical-text"
                  >
                    重试
                  </button>
                }
              >
                更早的记录加载失败
              </InlineError>
            )}
          </div>

          {(showList || firstLoading) && (
            <div className="shrink-0 border-t border-line bg-surface px-4 py-2 pb-[calc(0.5rem+var(--pf-safe-bottom))] tablet:px-6">
              <p role="status" aria-live="polite" className="text-micro text-muted">
                {firstLoading ? '正在读取…' : statusText}
              </p>
            </div>
          )}
        </div>
      </div>

      {evidence && (
        <EvidenceDialog
          evidenceId={evidence.id}
          evidenceRef={evidence.ref}
          onClose={() => setEvidence(null)}
          onOpenItemHistory={handleSelectAffectedItem}
        />
      )}
    </>,
    document.body,
  )
}