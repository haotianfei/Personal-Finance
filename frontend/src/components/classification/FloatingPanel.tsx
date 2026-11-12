'use client'

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { createPortal } from 'react-dom'

/**
 * 锚定浮层：贴住触发控件展开，下方放不下就翻到上方。
 *
 * 为什么用 portal + fixed 而不是绝对定位：分类弹窗的 body 是 `overflow-y-auto`，
 * 绝对定位的下拉会被裁掉；而弹窗面板带 `translate`（transform 会构成 containing
 * block），把 portal 挂到弹窗内部同样定位不准。所以统一挂到 body、用 fixed，
 * 层高取 55（弹窗 50 之上、toast 60 之下）。
 *
 * 键盘协同：浮层在弹窗 DOM 之外，若不干预，`useDialogA11y` 的 Tab 循环会把焦点
 * 从浮层里拽回弹窗、它的 Esc 也会先关掉整个弹窗。因此这里在 **window 捕获阶段**
 * 处理 Esc / Tab（window 捕获早于 document 捕获），把事件截在浮层内。
 */
const PANEL_MAX_HEIGHT = 320
const GAP = 6

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export interface FloatingPanelProps {
  open: boolean
  anchorRef: RefObject<HTMLElement | null>
  onClose: () => void
  children: React.ReactNode
  /** 与锚点同宽（缺省）；固定宽度时传数字。 */
  width?: 'anchor' | number
  label: string
}

export function FloatingPanel({ open, anchorRef, onClose, children, width = 'anchor', label }: FloatingPanelProps) {
  const [style, setStyle] = useState<React.CSSProperties>({ visibility: 'hidden' })
  const panelRef = useRef<HTMLDivElement>(null)

  const reposition = useCallback(() => {
    const anchor = anchorRef.current
    if (!anchor) return
    const rect = anchor.getBoundingClientRect()
    const spaceBelow = window.innerHeight - rect.bottom
    const spaceAbove = rect.top
    const showAbove = spaceBelow < PANEL_MAX_HEIGHT && spaceAbove > spaceBelow
    const maxHeight = Math.min(PANEL_MAX_HEIGHT, Math.max(160, (showAbove ? spaceAbove : spaceBelow) - GAP * 2))
    const panelWidth = width === 'anchor' ? rect.width : width
    const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - panelWidth - 8))

    setStyle({
      position: 'fixed',
      left,
      top: showAbove ? undefined : rect.bottom + GAP,
      bottom: showAbove ? window.innerHeight - rect.top + GAP : undefined,
      width: panelWidth,
      maxHeight,
      zIndex: 55,
    })
  }, [anchorRef, width])

  useLayoutEffect(() => {
    if (!open) return
    reposition()
  }, [open, reposition])

  useEffect(() => {
    if (!open) return
    const handle = () => reposition()
    window.addEventListener('resize', handle)
    window.addEventListener('scroll', handle, true)
    return () => {
      window.removeEventListener('resize', handle)
      window.removeEventListener('scroll', handle, true)
    }
  }, [open, reposition])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((element) => !element.hasAttribute('disabled') && element.getClientRects().length > 0)
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      const inside = active !== null && panel.contains(active)

      if (!event.shiftKey && (active === last || !inside)) {
        event.preventDefault()
        event.stopPropagation()
        first.focus()
      } else if (event.shiftKey && (active === first || !inside)) {
        event.preventDefault()
        event.stopPropagation()
        last.focus()
      } else {
        // 焦点在浮层内部且未到边界：让浏览器自然移动，但别让弹窗的 Tab 循环插手
        event.stopPropagation()
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [open, onClose])

  if (!open || typeof document === 'undefined') return null

  return createPortal(
    <>
      <div className="fixed inset-0 z-[54]" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="group"
        aria-label={label}
        style={style}
        className="flex flex-col overflow-hidden rounded-control border border-line bg-surface shadow-overlay"
      >
        {children}
      </div>
    </>,
    document.body,
  )
}