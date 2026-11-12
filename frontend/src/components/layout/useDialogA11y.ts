'use client'

import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

const FOCUSABLE = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'object',
  'embed',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) =>
      !el.hasAttribute('disabled') &&
      el.getAttribute('aria-hidden') !== 'true' &&
      el.getClientRects().length > 0,
  )
}

export interface DialogA11yOptions {
  open: boolean
  onClose: () => void
  containerRef: RefObject<HTMLElement | null>
  /** 打开后优先聚焦的元素（通常是关闭按钮）；缺省聚焦第一个可聚焦项。 */
  initialFocusRef?: RefObject<HTMLElement | null>
  /** 需要 inert 的背景容器 id；默认 AppShell 的 `app-shell`。 */
  inertTargetId?: string
}

/**
 * 弹层/抽屉共用无障碍行为（design §5.3/§7.3）：
 * - 打开时记录触发点，关闭后还原焦点；
 * - Tab / Shift+Tab 在容器内循环，焦点不会逃到背景；
 * - Esc 关闭；背景 `inert` + `aria-hidden`，滚动锁定；
 * - 通过 `suppressRestoreRef` 支持「点链接导航」时不要抢新页面焦点。
 */
export function useDialogA11y({
  open,
  onClose,
  containerRef,
  initialFocusRef,
  inertTargetId = 'app-shell',
}: DialogA11yOptions) {
  const restoreRef = useRef<HTMLElement | null>(null)
  const suppressRestoreRef = useRef(false)

  useEffect(() => {
    if (!open) return
    const container = containerRef.current
    if (!container) return

    suppressRestoreRef.current = false
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null

    const inertTarget = document.getElementById(inertTargetId)
    const prevInert = inertTarget?.inert ?? false
    const prevAriaHidden = inertTarget?.getAttribute('aria-hidden') ?? null
    if (inertTarget) {
      inertTarget.inert = true
      inertTarget.setAttribute('aria-hidden', 'true')
    }

    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusFirst = () => {
      const preferred = initialFocusRef?.current
      const target =
        preferred && preferred.getClientRects().length > 0
          ? preferred
          : focusableIn(container)[0] ?? container
      target.focus()
    }
    const frame = window.requestAnimationFrame(focusFirst)

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return

      const items = focusableIn(container)
      if (items.length === 0) {
        event.preventDefault()
        container.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null

      if (!event.shiftKey && (active === last || !container.contains(active))) {
        event.preventDefault()
        first.focus()
      } else if (event.shiftKey && (active === first || !container.contains(active))) {
        event.preventDefault()
        last.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)

    return () => {
      window.cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = prevOverflow
      if (inertTarget) {
        inertTarget.inert = prevInert
        if (prevAriaHidden === null) inertTarget.removeAttribute('aria-hidden')
        else inertTarget.setAttribute('aria-hidden', prevAriaHidden)
      }
      if (!suppressRestoreRef.current) {
        const restore = restoreRef.current
        if (restore && restore.isConnected) restore.focus()
      }
    }
  }, [open, onClose, containerRef, initialFocusRef, inertTargetId])

  return { suppressRestoreRef }
}