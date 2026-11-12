import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 页面容器：统一响应式留白 + 横向安全区（design §5.2/§6）。
 *
 * - 宽度上限只由 `.pf-main` 一层承载（`--pf-shell-content-max`）；本容器不再设页面级上限，
 *   否则 clamp 里的百分比会按各自的包含块连乘，套两层就成 88%×；
 * - `reading` 例外：它是正文行宽（prose-max），与页面宽无关；
 * - 内边距 16（H5）/ 24（平板）/ 32（桌面），并叠加 env(safe-area-inset-left/right)；
 * - 页面级禁止横向滚动由 AppShell 的 overflow-x-clip 兜底，这里不裁内容。
 */
export type PageWidth = 'wide' | 'reading'

const WIDTH_CLASS: Record<PageWidth, string> = {
  wide: 'w-full',
  reading: 'max-w-reading',
}

export interface PageContainerProps {
  children: ReactNode
  width?: PageWidth
  /** 关闭左右留白（用于全宽图表/表格容器自行控制内边距）。 */
  padded?: boolean
  className?: string
  as?: 'div' | 'section' | 'article'
}

export function PageContainer({
  children,
  width = 'wide',
  padded = true,
  className,
  as: Tag = 'div',
}: PageContainerProps) {
  return (
    <Tag
      data-pf-page=""
      className={cn('mx-auto w-full', WIDTH_CLASS[width], padded && 'pf-page-x pf-page-y', className)}
    >
      {children}
    </Tag>
  )
}