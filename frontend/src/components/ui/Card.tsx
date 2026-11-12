import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 一种卡片：1px 细线 + 两级阴影中的卡片级 + 12px 圆角（design §3.4/§5.2）。
 * 嵌套卡片不再加阴影；hover 只增强边框，且只在细指针设备上生效。
 */
export type CardPadding = 'none' | 'sm' | 'md' | 'lg'

const PADDING_CLASS: Record<CardPadding, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4 tablet:p-5',
  lg: 'p-5',
}

export interface CardProps {
  children: ReactNode
  padding?: CardPadding
  interactive?: boolean
  className?: string
  as?: 'div' | 'section' | 'article' | 'li'
  'aria-label'?: string
}

export function Card({
  children,
  padding = 'md',
  interactive = false,
  className,
  as: Tag = 'div',
  'aria-label': ariaLabel,
}: CardProps) {
  return (
    <Tag
      aria-label={ariaLabel}
      className={cn(
        'rounded-card border border-line bg-surface shadow-card',
        PADDING_CLASS[padding],
        interactive && 'pf-card-interactive',
        className,
      )}
    >
      {children}
    </Tag>
  )
}

export interface SectionProps {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  /** 标题层级：页面内区块默认 h2；卡片内小节可降为 h3。 */
  headingLevel?: 2 | 3
  padding?: CardPadding
  className?: string
  contentClassName?: string
}

/** 区块：标题 + 可选说明/动作 + 内容；桌面 24/32 间距，移动 24。 */
export function Section({
  title,
  description,
  actions,
  children,
  headingLevel = 2,
  padding = 'md',
  className,
  contentClassName,
}: SectionProps) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3'
  return (
    <Card as="section" padding={padding} className={className} aria-label={typeof title === 'string' ? title : undefined}>
      <div className="flex flex-col gap-3 tablet:flex-row tablet:items-start tablet:justify-between">
        <div className="min-w-0">
          <Heading
            className={cn(
              'text-ink',
              headingLevel === 2 ? 'text-h2 font-semibold' : 'text-h3 font-semibold',
            )}
          >
            {title}
          </Heading>
          {description && (
            <p className="mt-1 max-w-reading text-body text-secondary">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className={cn('mt-4', contentClassName)}>{children}</div>
    </Card>
  )
}