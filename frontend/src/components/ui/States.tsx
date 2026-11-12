import type { ReactNode } from 'react'
import { AlertTriangle, Inbox, RotateCcw } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 空 / 载入 / 错误三态：与库存统一（design §5.5/§5.6）。
 *
 * 纪律：错误绝不伪装成空或 0；空态说明「为什么空 + 怎么开始」；
 * 载入骨架与最终内容同高，避免首屏跳动。
 */

// --- Empty ---------------------------------------------------------------

export interface EmptyStateProps {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  icon?: LucideIcon
  compact?: boolean
  className?: string
}

export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Inbox,
  compact = false,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center rounded-card border border-dashed border-line bg-subtle/50 px-6 text-center',
        compact ? 'py-8' : 'py-12',
        className,
      )}
    >
      <Icon size={compact ? 22 : 28} strokeWidth={1.75} className="text-muted" aria-hidden />
      <p className="mt-3 text-h3 font-semibold text-ink">{title}</p>
      {description && (
        <p className="mt-1 max-w-reading text-body text-secondary">{description}</p>
      )}
      {action && <div className="mt-4 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  )
}

// --- Loading -------------------------------------------------------------

export interface LoadingStateProps {
  /** 骨架行数；默认 3–5 行，保持目标内容高度。 */
  rows?: number
  label?: string
  compact?: boolean
  className?: string
}

const SKELETON_WIDTHS = ['w-2/5', 'w-3/5', 'w-1/3', 'w-1/2', 'w-2/3']

export function LoadingState({
  rows = 3,
  label = '加载中',
  compact = false,
  className,
}: LoadingStateProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn('w-full', compact ? 'space-y-2' : 'space-y-3', className)}
    >
      <span className="sr-only">{label}</span>
      {Array.from({ length: Math.max(1, rows) }).map((_, index) => (
        <div
          key={index}
          aria-hidden
          className={cn(
            'flex items-center gap-3 rounded-control bg-subtle/70 px-3',
            compact ? 'h-8' : 'h-11',
          )}
        >
          <div className="h-3 w-3 shrink-0 animate-pulse rounded-full bg-line" />
          <div
            className={cn(
              'h-3 animate-pulse rounded-control bg-line',
              SKELETON_WIDTHS[index % SKELETON_WIDTHS.length],
            )}
          />
        </div>
      ))}
    </div>
  )
}

// --- Error ---------------------------------------------------------------

export interface ErrorStateProps {
  title?: ReactNode
  description?: ReactNode
  onRetry?: () => void
  retryLabel?: string
  compact?: boolean
  className?: string
}

export function ErrorState({
  title = '加载失败',
  description,
  onRetry,
  retryLabel = '重试',
  compact = false,
  className,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center rounded-card border border-status-critical/25 bg-status-critical-surface px-6 text-center',
        compact ? 'py-6' : 'py-10',
        className,
      )}
    >
      <AlertTriangle
        size={compact ? 20 : 26}
        strokeWidth={1.75}
        className="text-status-critical-text"
        aria-hidden
      />
      <p className="mt-2 text-h3 font-semibold text-ink">{title}</p>
      {description && (
        <p className="mt-1 max-w-reading text-body text-secondary">{description}</p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="pf-card-interactive mt-4 inline-flex min-h-[44px] items-center gap-2 rounded-control border border-line-strong bg-surface px-4 text-body-strong font-medium text-ink"
        >
          <RotateCcw size={16} aria-hidden />
          {retryLabel}
        </button>
      )}
    </div>
  )
}

// --- Inline error --------------------------------------------------------

export interface InlineErrorProps {
  children: ReactNode
  action?: ReactNode
  className?: string
}

/** 字段/区块内的错误行；不占满整块内容，也不把错误藏进 toast。 */
export function InlineError({ children, action, className }: InlineErrorProps) {
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-wrap items-center gap-x-3 gap-y-1 rounded-control border border-status-critical/25 bg-status-critical-surface px-3 py-2 text-meta text-status-critical-text',
        className,
      )}
    >
      <AlertTriangle size={14} strokeWidth={2} aria-hidden className="shrink-0" />
      <span className="min-w-0 flex-1">{children}</span>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}