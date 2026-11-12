'use client'

import { useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { api } from '@/lib/api'
import { InlineError, LoadingState } from '@/components/ui'

/**
 * 抽屉按需加载：一条链是 `ItemHistoryDrawer → EvidenceDialog → EvidenceDetail →
 * 本组件`，静态引入会绕回来形成环；同时「受影响资产项」不点开时也不需要抽屉代码。
 */
const LazyItemHistoryDrawer = dynamic(
  () => import('./ItemHistoryDrawer').then((m) => m.ItemHistoryDrawer),
  { ssr: false },
)

/**
 * 凭证详情里的「受影响资产项」（票 02 J1 的第二个入口）。
 *
 * 数据来源是既有的按凭证反查（`GET /api/assets?source_evidence_id=`，spec D8），
 * 取全行口径（含被更正的 superseded 行）——取证问的是「这张凭证动过哪些项」，
 * 不只是当前还有效的行。
 *
 * 点一项：宿主给了 `onOpenItemHistory` 就交给宿主（操作历史抽屉里换主体，
 * 不再套第二个抽屉）；没给就在原地开同一个抽屉（凭证详情页/独立弹层）。
 */
/** 后端 `page_size` 上限（list_assets: le=1000）；超出时明说只列了前 N 行。 */
const PAGE_SIZE = 1000

interface AffectedItem {
  item_id: number
  display_name: string
  rows: number
}

export interface EvidenceAffectedItemsProps {
  evidenceId: number
  /** 宿主给了就接管点击（在操作历史抽屉内换主体）；否则本组件自己开抽屉。 */
  onOpenItemHistory?: (item: { item_id: number; display_name: string }) => void
}

export function EvidenceAffectedItems({ evidenceId, onOpenItemHistory }: EvidenceAffectedItemsProps) {
  const [openItem, setOpenItem] = useState<{ item_id: number; display_name: string } | null>(null)

  const query = useQuery({
    queryKey: ['evidenceAffectedItems', evidenceId],
    queryFn: () =>
      api.getAssets({ page_size: String(PAGE_SIZE), include_superseded: 'true' }, evidenceId),
    // 账本只追加：一分钟内重复打开凭证不必再拉
    staleTime: 60_000,
    retry: false,
  })

  const { items, unlinkedRows } = useMemo(() => {
    const grouped = new Map<number, AffectedItem>()
    let unlinked = 0
    for (const row of query.data?.items ?? []) {
      if (row.asset_item_id === null) {
        // v5 之前的历史行没有资产项身份：如实计数，不硬塞进某个项
        unlinked += 1
        continue
      }
      const existing = grouped.get(row.asset_item_id)
      if (existing) {
        existing.rows += 1
        continue
      }
      grouped.set(row.asset_item_id, {
        item_id: row.asset_item_id,
        // 显示名取行上回显的项名；旧数据没有则退回写入时的名称快照
        display_name: row.display_name || row.asset_name,
        rows: 1,
      })
    }
    const list = [...grouped.values()].sort(
      (a, b) => b.rows - a.rows || a.display_name.localeCompare(b.display_name, 'zh-CN'),
    )
    return { items: list, unlinkedRows: unlinked }
  }, [query.data])

  const handleOpen = (item: AffectedItem) => {
    if (onOpenItemHistory) {
      onOpenItemHistory({ item_id: item.item_id, display_name: item.display_name })
      return
    }
    setOpenItem({ item_id: item.item_id, display_name: item.display_name })
  }

  const fetched = query.data?.items.length ?? 0
  const overPageLimit = (query.data?.total ?? 0) > fetched

  return (
    <section className="rounded-control border border-line bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-body-strong font-medium text-ink">受影响资产项</h3>
        {items.length > 0 && (
          <span className="pf-num text-meta text-muted">{items.length} 项</span>
        )}
      </div>
      <p className="mt-0.5 text-micro text-muted">
        这张凭证写过的资产项（含被更正的历史行）。点一项看它的操作历史。
      </p>

      {query.isLoading && <LoadingState rows={2} compact className="mt-2" label="正在读取受影响资产项" />}

      {!query.isLoading && query.error && (
        <InlineError
          className="mt-2"
          action={
            <button
              type="button"
              onClick={() => { void query.refetch() }}
              className="inline-flex min-h-[32px] items-center rounded-control px-2 text-meta font-medium text-status-critical-text"
            >
              重试
            </button>
          }
        >
          受影响资产项加载失败
        </InlineError>
      )}

      {!query.isLoading && !query.error && items.length === 0 && (
        <p className="mt-1.5 text-meta text-secondary">
          {unlinkedRows > 0
            ? '这张凭证的行还没有关联资产项（v5 之前的旧数据），无法按项下钻。'
            : '没有可下钻的资产项。'}
        </p>
      )}

      {items.length > 0 && (
        <ul className="mt-1.5 divide-y divide-line">
          {items.map((item) => (
            <li key={item.item_id}>
              <button
                type="button"
                onClick={() => handleOpen(item)}
                aria-label={`查看「${item.display_name}」的操作历史`}
                className="flex min-h-[44px] w-full items-center gap-2 rounded-control px-1 text-left transition-colors hover:bg-subtle focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]"
              >
                <span className="min-w-0 flex-1 truncate text-body text-ink">{item.display_name}</span>
                <span className="pf-num shrink-0 text-micro text-muted">{item.rows} 行</span>
                <ChevronRight size={14} aria-hidden className="shrink-0 text-muted" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {overPageLimit && (
        <p className="mt-1.5 text-micro text-muted">
          这张凭证超过 {PAGE_SIZE} 行，只列出前 {PAGE_SIZE} 行涉及的资产项。
        </p>
      )}
      {items.length > 0 && unlinkedRows > 0 && (
        <p className="mt-1.5 text-micro text-muted">
          另有 {unlinkedRows} 行未关联资产项（旧数据），不在上面的清单里。
        </p>
      )}

      {openItem && (
        <LazyItemHistoryDrawer
          open
          itemId={openItem.item_id}
          displayName={openItem.display_name}
          onClose={() => setOpenItem(null)}
        />
      )}
    </section>
  )
}