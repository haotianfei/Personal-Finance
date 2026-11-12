'use client'

import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  INTEGRITY_META,
  hashPreview,
  type GlobalLogIntegrity,
} from './GlobalLogModel'

/**
 * 链状态徽标（票 02 J6 / spec I3）。
 *
 * 五值全部来自 `GET /audit-log/integrity`：前端**不推断**、不把「读不到」当成「没问题」。
 * 文案纪律：只说「与库外锚对得上 / 对不上」，不写「不可篡改」「绝对安全」——
 * 对账能发现不一致，但不阻止写入，这个区别必须留在界面上。
 */
export interface GlobalLogIntegrityBadgeProps {
  data: GlobalLogIntegrity | null
  loading: boolean
  error: unknown
  onRetry: () => void
  className?: string
}

const RETRY_CLASS =
  'inline-flex min-h-[32px] items-center gap-1 rounded-control px-1.5 text-micro font-medium underline underline-offset-2 transition-colors'

export function GlobalLogIntegrityBadge({
  data,
  loading,
  error,
  onRetry,
  className,
}: GlobalLogIntegrityBadgeProps) {
  if (loading) {
    return (
      <section
        aria-label="链状态"
        aria-busy="true"
        className={cn(
          'animate-pulse rounded-card border border-line bg-subtle/60 px-3 py-2',
          className,
        )}
      >
        <span className="sr-only">正在读取链状态</span>
        <div aria-hidden className="h-3 w-24 rounded-control bg-line" />
        <div aria-hidden className="mt-2 h-2.5 w-64 max-w-full rounded-control bg-line" />
      </section>
    )
  }

  if (error && !data) {
    return (
      <section
        aria-label="链状态"
        className={cn(
          'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-line bg-subtle px-3 py-2 text-muted',
          className,
        )}
      >
        <span className="inline-flex items-center gap-1.5 text-meta font-medium">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-status-unknown" />
          链状态：读取失败
        </span>
        <span className="text-micro">没有拿到对账结论，这不等于一致。</span>
        <button type="button" onClick={onRetry} className={cn(RETRY_CLASS, 'ml-auto text-link')}>
          <RefreshCw size={12} aria-hidden />
          重新读取
        </button>
      </section>
    )
  }

  if (!data) return null

  const meta = INTEGRITY_META[data.status]
  const head = hashPreview(data.head)

  return (
    <section
      aria-label="链状态"
      className={cn('rounded-card border px-3 py-2', meta.tone, className)}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1.5 text-meta font-semibold">
          <span aria-hidden className={cn('h-1.5 w-1.5 rounded-full', meta.dot)} />
          链状态：{meta.label}
        </span>

        <span
          className="pf-num text-micro opacity-80"
          title={data.head ?? '当前库还没有可展示的链头'}
        >
          链头 {head ?? '—'}
        </span>
        <span className="pf-num text-micro opacity-80">
          最大行号 {data.maxId ?? '—'}
        </span>
        {data.rows !== null && (
          <span className="pf-num text-micro opacity-80">
            已登记 {data.rows} 行
          </span>
        )}
        {data.sealLag !== null && data.sealLag > 0 && (
          <span
            className="pf-num text-micro opacity-80"
            title="日封存之后新增、还没进封存的行数；封存本身只记录进度，不改变判定"
          >
            未封存 {data.sealLag} 行
          </span>
        )}

        <button type="button" onClick={onRetry} className={cn(RETRY_CLASS, 'ml-auto')}>
          <RefreshCw size={12} aria-hidden />
          重新对账
        </button>
      </div>

      <p className="mt-1 text-micro leading-snug opacity-80">{meta.hint}</p>
    </section>
  )
}