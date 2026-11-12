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

export interface DrawerA11yOptions {
  open: boolean
  onClose: () => void
  containerRef: RefObject<HTMLElement | null>
  /** 打开后优先聚焦的控件；缺省聚焦第一个可聚焦项。 */
  initialFocusRef?: RefObject<HTMLElement | null>
  /**
   * 上面还叠了一层弹层（凭证详情）时挂起：Esc / Tab 全部让给上层。
   *
   * 为什么不用 `components/layout/useDialogA11y`：那个 hook 的 `open=false` 会卸载
   * 整个行为（含焦点还原与滚动锁），而这里要的是「抽屉的 DOM 与滚动位置原地不动，
   * 只是暂时不处理键盘」，否则关闭凭证时抽屉会闪一下、焦点被拽回表格行。
   * 同时避免一次 Esc 关掉两层：`document` 上的两个捕获监听器都会收到同一个事件。
   */
  suspended?: boolean
  /** 需要 inert 的背景容器 id；默认 AppShell 的 `app-shell`。 */
  inertTargetId?: string
}

/**
 * 右侧抽屉的无障碍行为（`useDialogA11y` 的抽屉变体）：
 * 焦点陷阱、Esc、关闭后焦点还原、背景 inert + aria-hidden、body 滚动锁。
 */
export function useDrawerA11y({
  open,
  onClose,
  containerRef,
  initialFocusRef,
  suspended = false,
  inertTargetId = 'app-shell',
}: DrawerA11yOptions) {
  const restoreRef = useRef<HTMLElement | null>(null)
  // 事件回调里读最新值：避免每次 suspended / onClose 变化都重挂监听（会打断焦点）
  const suspendedRef = useRef(suspended)
  suspendedRef.current = suspended
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const container = containerRef.current
    if (!container) return

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
      // 上层弹层（凭证详情）在场时不做任何事：Esc 由它关，Tab 由它的焦点陷阱管
      if (suspendedRef.current) return
      if (event.key === 'Escape') {
        event.preventDefault()
        // 只关本层：抽屉也可能叠在别的弹层之上（凭证列表的详情 Modal），
        // 不挡住传播的话一次 Esc 会关掉两层。
        event.stopPropagation()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return

      // Tab 同理：下层弹层的焦点陷阱看到「焦点不在自己容器里」会把焦点抢回去，
      // 所以整个 Tab 都由本层接管（只有到边界才 preventDefault）。
      event.stopPropagation()

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

    // 监听挂在 window 捕获阶段：比 `document` 上的 `useDialogA11y` 更早拿到事件
    window.addEventListener('keydown', onKeyDown, true)

    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = prevOverflow
      if (inertTarget) {
        inertTarget.inert = prevInert
        if (prevAriaHidden === null) inertTarget.removeAttribute('aria-hidden')
        else inertTarget.setAttribute('aria-hidden', prevAriaHidden)
      }
      const restore = restoreRef.current
      if (restore && restore.isConnected) restore.focus()
    }
  }, [open, containerRef, initialFocusRef, inertTargetId])
}