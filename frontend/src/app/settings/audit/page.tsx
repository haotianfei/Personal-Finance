'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { getAuditIntegrity, getAuditLog } from '@/lib/api'
import { cn } from '@/lib/utils'
import { PageHeader } from '@/components/layout/PageHeader'
import { LoadingState } from '@/components/ui'
import { GlobalLogFilters } from '@/components/audit/GlobalLogFilters'
import {
  EMPTY_GLOBAL_LOG_FILTERS,
  buildSearchParams,
  countActiveFilters,
  filtersFromSearchParams,
  toAuditLogQuery,
  type GlobalLogFiltersValue,
} from '@/components/audit/GlobalLogFilters'
import { GlobalLogIntegrityBadge } from '@/components/audit/GlobalLogIntegrityBadge'
import { GlobalLogPagination } from '@/components/audit/GlobalLogPagination'
import { GlobalLogTable, type GlobalLogEmpty } from '@/components/audit/GlobalLogTable'
import { ItemHistoryDrawer } from '@/components/audit/ItemHistoryDrawer'
import { EvidenceDialog } from '@/components/audit/EvidenceDialog'
import { readIntegrity, toGlobalLogRow } from '@/components/audit/GlobalLogModel'

/**
 * 设置 → 操作日志（票 02 J6）。
 *
 * 这一页是**索引**，不是 diff 视图：回答「谁、什么时候、动了哪一行、依据哪张凭证」，
 * 逐字段旧→新回行级抽屉看（`ItemHistoryDrawer`）。页面只读两个端点：
 * `GET /audit-log`（服务端分页）与 `GET /audit-log/integrity`（链状态）。
 *
 * 状态与 URL 的关系：筛选/页码以 URL 为落点（`router.replace`，不污染历史栈），
 * 刷新不丢、链接可直接分享；输入框本身不直接驱动请求——统一延后 300ms 生效，
 * 免得一个字符一次查询。
 */

/** 打字停顿多久才发查询。 */
const FILTER_DEBOUNCE_MS = 300

const REFRESH_CLASS =
  'inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink'

const SECONDARY_LINK_CLASS =
  'inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-4 text-body-strong font-medium text-ink'

/** 空库时的「开始录入」：次要入口，不抢主操作的位置。 */
const TEXT_LINK_CLASS =
  'pf-subtle-link inline-flex min-h-[44px] items-center rounded-control px-2 text-body-strong font-medium text-link'

export default function AuditLogPage() {
  return (
    <Suspense fallback={<LoadingState rows={6} label="正在准备操作日志" />}>
      <AuditLogPageContent />
    </Suspense>
  )
}

function AuditLogPageContent() {
  const router = useRouter()
  const searchParams = useSearchParams()

  // 首帧从 URL 取一次（非法值按「没传」处理）；之后由本页状态驱动 URL，不再回读
  const fromUrl = filtersFromSearchParams(new URLSearchParams(searchParams.toString()))
  const [filters, setFilters] = useState<GlobalLogFiltersValue>(fromUrl.filters)
  /** 防抖后的生效筛选：只有它变化才发请求、才回写 URL。 */
  const [settled, setSettled] = useState<GlobalLogFiltersValue>(fromUrl.filters)
  const [page, setPage] = useState(fromUrl.page)
  const [pageSize, setPageSize] = useState(fromUrl.pageSize)
  const [historyItemId, setHistoryItemId] = useState<number | null>(null)
  const [evidence, setEvidence] = useState<{ id: number; ref: string } | null>(null)

  // 输入 → 生效：300ms 后一起提交筛选与「回到第一页」，避免先按旧筛选多打一枪
  useEffect(() => {
    if (settled === filters) return
    const timer = window.setTimeout(() => {
      setSettled(filters)
      setPage(1)
    }, FILTER_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [filters, settled])

  const query = toAuditLogQuery(settled, page, pageSize)

  const logQuery = useQuery({
    queryKey: ['auditLog', query],
    queryFn: () => getAuditLog(query),
    // 翻页/改筛选时保留上一页内容，不闪空态；加载态由 isFetching 说明
    placeholderData: keepPreviousData,
    staleTime: 15_000,
    retry: false,
  })

  const integrityQuery = useQuery({
    queryKey: ['auditIntegrity'],
    queryFn: getAuditIntegrity,
    staleTime: 60_000,
    retry: false,
  })

  // 写回 URL：空条件不进 query；与当前地址一致时不动作（避免 replace 自激）
  useEffect(() => {
    const params = buildSearchParams(settled, page, pageSize)
    const next = params.toString()
    if (next === searchParams.toString()) return
    router.replace(next ? `/settings/audit?${next}` : '/settings/audit', { scroll: false })
  }, [settled, page, pageSize, router, searchParams])

  const rows = useMemo(
    () => (logQuery.data?.items ?? []).map(toGlobalLogRow),
    [logQuery.data],
  )
  const total = logQuery.data?.total ?? 0

  const activeCount = countActiveFilters(settled)
  const idemActive = settled.idemKey.trim() !== ''
  const integrity = readIntegrity(integrityQuery.data)

  const resetFilters = () => {
    setFilters(EMPTY_GLOBAL_LOG_FILTERS)
    setSettled(EMPTY_GLOBAL_LOG_FILTERS)
    setPage(1)
  }

  const clearIdemKey = () => {
    const next = { ...settled, idemKey: '' }
    setFilters(next)
    setSettled(next)
  }

  const refreshAll = () => {
    void logQuery.refetch()
    void integrityQuery.refetch()
  }

  const empty: GlobalLogEmpty = idemActive && rows.length === 0
    ? {
        title: '没有匹配这个幂等键的操作记录',
        description: '幂等键 7 天后过期：更早的记录可能已查不到，可改用请求号或对象 id 反查。',
        action: (
          <button type="button" onClick={clearIdemKey} className={SECONDARY_LINK_CLASS}>
            清除幂等键
          </button>
        ),
      }
    : rows.length === 0 && total > 0
      ? {
          title: '这一页没有记录',
          description: `共 ${total} 条，当前页码超出范围（可能来自一条旧链接）。`,
          action: (
            <button type="button" onClick={() => setPage(1)} className={SECONDARY_LINK_CLASS}>
              回到第一页
            </button>
          ),
        }
      : activeCount > 0
        ? {
            title: '没有匹配的操作记录',
            description: '换个条件试试，或清除筛选看全部。',
            action: (
              <button type="button" onClick={resetFilters} className={SECONDARY_LINK_CLASS}>
                清除筛选
              </button>
            ),
          }
        : {
            title: '还没有操作记录',
            description: '第一次保存资产、分类或凭证之后，这里会按时间倒序列出每一次写操作。',
            action: (
              <Link href="/records" className={TEXT_LINK_CLASS}>
                开始录入
              </Link>
            ),
          }

  return (
    <div className="space-y-4">
      <PageHeader
        title="操作日志"
        description="每一次写操作都在这里：谁、动了哪一行、依据哪张凭证。链状态只用来发现不一致。"
        actions={(
          <button type="button" onClick={refreshAll} className={REFRESH_CLASS}>
            <RefreshCw
              size={15}
              aria-hidden
              className={cn(logQuery.isFetching && 'animate-spin')}
            />
            刷新
          </button>
        )}
      />

      <GlobalLogIntegrityBadge
        data={integrity}
        loading={integrityQuery.isLoading}
        error={integrityQuery.error}
        onRetry={() => { void integrityQuery.refetch() }}
      />

      <GlobalLogFilters
        value={filters}
        onChange={setFilters}
        onReset={resetFilters}
        activeCount={activeCount}
      />

      <GlobalLogTable
        rows={rows}
        loading={logQuery.isLoading}
        staleError={rows.length > 0 ? logQuery.error : null}
        error={logQuery.error}
        onRetry={() => { void logQuery.refetch() }}
        empty={empty}
        onOpenItemHistory={setHistoryItemId}
        onOpenEvidence={(id, ref) => setEvidence({ id, ref })}
      />

      {(total > 0 || rows.length > 0) && (
        <GlobalLogPagination
          page={page}
          pageSize={pageSize}
          total={total}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size)
            setPage(1)
          }}
          busy={logQuery.isFetching && !logQuery.isLoading}
        />
      )}

      {historyItemId !== null && (
        <ItemHistoryDrawer
          open
          itemId={historyItemId}
          onClose={() => setHistoryItemId(null)}
        />
      )}

      {evidence && (
        <EvidenceDialog
          evidenceId={evidence.id}
          evidenceRef={evidence.ref}
          onClose={() => setEvidence(null)}
        />
      )}
    </div>
  )
}