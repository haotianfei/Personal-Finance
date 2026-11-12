import type { ReactNode } from 'react'
import Link from 'next/link'
import { ChevronLeft } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 页头：标题（H5 20 / 桌面 24）+ 范围/元信息 + 主操作（design §5.2/§6）。
 *
 * - 每页只有一个 H1；
 * - 移动端操作换行到标题下方，不挤标题；
 * - 返回链接 44px 命中，文案用动词/目标，不写「返回」以外的含糊词。
 */
export interface PageHeaderProps {
  title: ReactNode
  /** 副标题或范围（快照日/账期）。 */
  description?: ReactNode
  /** 右上动作区；主操作放最后一个（视觉右侧）。 */
  actions?: ReactNode
  /** 子页显示返回链接。 */
  back?: { href: string; label?: string }
  /** 标题上方的短标签（如「设置」「管理」）。 */
  eyebrow?: ReactNode
  className?: string
}

export function PageHeader({
  title,
  description,
  actions,
  back,
  eyebrow,
  className,
}: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-4 tablet:flex-row tablet:items-start tablet:justify-between', className)}>
      <div className="min-w-0">
        {back && (
          <Link
            href={back.href}
            className="pf-subtle-link -ml-2 mb-1 inline-flex min-h-[44px] items-center gap-1 rounded-control px-2 text-meta font-medium text-muted"
          >
            <ChevronLeft size={16} aria-hidden />
            {back.label ?? '返回'}
          </Link>
        )}
        {eyebrow && <p className="mb-1 text-meta font-medium text-muted">{eyebrow}</p>}
        <h1 className="text-pretty text-xl font-bold tracking-tight text-ink tablet-l:text-2xl">
          {title}
        </h1>
        {description && (
          <div className="mt-1 max-w-reading text-body text-secondary">{description}</div>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 tablet:justify-end">
          {actions}
        </div>
      )}
    </header>
  )
}