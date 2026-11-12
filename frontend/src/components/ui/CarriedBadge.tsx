import { cn } from '@/lib/utils'

/**
 * 「含沿用值」小注记：载入点的 `carried_items > 0` 时由调用方渲染。
 * 事实口径见 `lib/carried.ts`（ADR-0019 D12：不再有 partial 三态）。
 */
export function CarriedBadge({ title, className }: {
  title?: string
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-amber-50 px-2 py-0.5 text-meta font-medium leading-4 text-amber-700',
        className,
      )}
      title={title}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-hidden />
      含沿用值
    </span>
  )
}