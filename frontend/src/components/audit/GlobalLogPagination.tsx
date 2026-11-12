'use client'

import { ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { PAGE_SIZE_OPTIONS } from './GlobalLogFilters'

/**
 * 服务端分页条：口径与列表页的 `DataTable` 一致（「第 a–b 条 · 共 N 条」+ 每页条数），
 * 只因操作日志需要可展开行而单独实现——两处文案与禁用规则必须同步改。
 */
export interface GlobalLogPaginationProps {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  onPageSizeChange: (size: number) => void
  /** 请求进行中时按钮仍可点（乐观翻页），但状态区会说明正在读取。 */
  busy?: boolean
  className?: string
}

const STEP_CLASS =
  'inline-flex h-11 w-11 items-center justify-center rounded-control text-secondary transition-colors hover:text-ink disabled:opacity-40 disabled:hover:text-secondary'

export function GlobalLogPagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  busy = false,
  className,
}: GlobalLogPaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1
  const to = Math.min(total, page * pageSize)

  return (
    <div
      className={cn(
        'flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-card border border-line bg-surface px-3 py-2 text-meta text-muted',
        className,
      )}
    >
      <p className="pf-num">
        第 {from}–{to} 条 · 共 {total} 条
        {busy && <span className="ml-2 text-micro">正在读取…</span>}
      </p>
      <div className="flex flex-wrap items-center gap-1">
        <select
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
          aria-label="每页条数"
          className="mr-1 min-h-[44px] rounded-control border border-line bg-surface px-2 text-meta text-ink"
        >
          {PAGE_SIZE_OPTIONS.map((size) => (
            <option key={size} value={size}>每页 {size}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={page <= 1}
          aria-label="上一页"
          className={STEP_CLASS}
        >
          <ChevronLeft size={18} aria-hidden />
        </button>
        <span className="pf-num min-w-[5.5rem] text-center text-secondary">
          {page} / {totalPages}
        </span>
        <button
          type="button"
          onClick={() => onPageChange(Math.min(totalPages, page + 1))}
          disabled={page >= totalPages}
          aria-label="下一页"
          className={STEP_CLASS}
        >
          <ChevronRight size={18} aria-hidden />
        </button>
      </div>
    </div>
  )
}