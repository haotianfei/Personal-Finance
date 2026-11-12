'use client'

import { useCallback, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { useDialogA11y } from '@/components/layout/useDialogA11y'

/**
 * 响应式弹层：≥768 居中 dialog，<768 全屏 sheet。
 *
 * 无障碍由 `useDialogA11y` 统一提供：焦点陷阱、Esc、关闭后焦点还原、
 * 背景 inert + aria-hidden、body 滚动锁。标题用 aria-labelledby 关联。
 */
export type DialogSize = 'sm' | 'md' | 'lg' | 'xl'

const DESKTOP_WIDTH: Record<DialogSize, string> = {
  sm: 'max-w-[480px]',
  md: 'max-w-[560px]',
  lg: 'max-w-[720px]',
  xl: 'max-w-[1100px]',
}

export interface ResponsiveDialogProps {
  open: boolean
  onClose: () => void
  title: React.ReactNode
  description?: React.ReactNode
  children: React.ReactNode
  footer?: React.ReactNode
  size?: DialogSize
  /** 打开后优先聚焦的控件；缺省聚焦关闭按钮。 */
  initialFocusRef?: React.RefObject<HTMLElement | null>
  className?: string
  bodyClassName?: string
}

export function ResponsiveDialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  initialFocusRef,
  className,
  bodyClassName,
}: ResponsiveDialogProps) {
  const isDesktop = useMediaQuery('(min-width: 768px)', true)
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  // 父级常用内联 onClose；固定成稳定函数，避免 inert/focus 随每次渲染重建。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const close = useCallback(() => onCloseRef.current(), [])
  useDialogA11y({
    open,
    onClose: close,
    containerRef: panelRef,
    initialFocusRef: initialFocusRef ?? closeRef,
  })

  if (!open || typeof document === 'undefined') return null

  const closeButton = (
    <button
      ref={closeRef}
      type="button"
      onClick={close}
      aria-label="关闭"
      className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:text-ink"
    >
      <X size={20} aria-hidden />
    </button>
  )

  return createPortal(
    <div className="fixed inset-0 z-modal" role="presentation">
      <div
        className="absolute inset-0 animate-fade-in bg-black/40"
        onClick={close}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cn(
          'absolute bg-surface text-ink shadow-overlay',
          isDesktop
            ? cn(
                'left-1/2 top-1/2 flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-overlay',
                DESKTOP_WIDTH[size],
                'animate-slide-up',
              )
            : 'inset-0 flex h-[var(--pf-viewport-h)] w-full flex-col pt-[var(--pf-safe-top)]',
          className,
        )}
      >
        <div className="flex shrink-0 items-start gap-3 border-b border-line px-4 py-3 tablet:px-6 tablet:py-4">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-h2 font-semibold text-ink">{title}</h2>
            {description && (
              <p id={descriptionId} className="mt-1 text-body text-secondary">{description}</p>
            )}
          </div>
          {closeButton}
        </div>

        <div className={cn('min-h-0 flex-1 overflow-y-auto p-4 tablet:p-6', bodyClassName)}>
          {children}
        </div>

        {footer && (
          <div className="shrink-0 border-t border-line bg-surface px-4 pb-[calc(1rem+var(--pf-safe-bottom))] pt-3 tablet:px-6 tablet:pb-4">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}