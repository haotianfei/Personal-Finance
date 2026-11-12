'use client'

import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { MoreHorizontal } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { cn } from '@/lib/utils'

/**
 * 行操作菜单：行尾一个 44×44 图标触发器，全部动作收进浮层。
 *
 * 为什么 portal + fixed：`DataTable` 的滚动容器是 `overflow-auto max-h-[70vh]`，
 * 行内 absolute 浮层纵向被裁、右侧被裁（同因写在
 * `components/classification/FloatingPanel.tsx:8-19`）。层高取 55：表格 sticky 表头是
 * z-sticky=20，确认弹窗是 z-modal=50，菜单要盖住表格又留在弹窗之下。
 *
 * 为什么不复用 `useDialogA11y`：它给 `#app-shell` 加 `inert` 并锁 `body` 滚动 ——
 * 那是模态语义；行菜单是**非模态**的，套上去会让整页不可交互。
 *
 * 键盘契约（spec D6）：`role=menu`/`menuitem`、↑↓ 与 Home/End 导航、Esc 关闭、
 * **Tab 关闭菜单并把焦点交回触发器（不循环）**、任何关闭路径都把焦点还给触发器。
 */

const MENU_MAX_HEIGHT = 320
const MENU_GAP = 6
const MENU_WIDTH = 200
const VIEWPORT_PADDING = 8

export interface RowAction {
  /** 稳定标识，用于测试与日志；同一行内唯一。 */
  id: string
  label: string
  icon?: LucideIcon
  onSelect: () => void
  /** 常显为独立图标按钮（每个清单最多 1 个），见 spec D1。 */
  primary?: boolean
  /** 危险动作：置底、加分隔线、critical 色。 */
  danger?: boolean
  disabled?: boolean
  /** 禁用原因，作为菜单项副标题呈现（替代 tip/tooltip）。 */
  disabledReason?: string
  /** 该动作携带需用户注意的信号：触发器和菜单项都标警示色。 */
  badge?: boolean
}

export interface RowActionMenuProps {
  actions: RowAction[]
  /** 触发器的可访问名，默认「更多操作」。行级上下文由调用方拼入（如资产项名）。 */
  triggerLabel?: string
  className?: string
}

export function RowActionMenu({ actions, triggerLabel = '更多操作', className }: RowActionMenuProps) {
  const [open, setOpen] = useState(false)
  const [style, setStyle] = useState<React.CSSProperties>({ visibility: 'hidden' })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()

  // 危险动作恒在末尾；同组内保持调用方给出的顺序。
  const menuActions = useMemo(() => {
    const normal = actions.filter((action) => !action.danger)
    const dangerous = actions.filter((action) => action.danger)
    return [...normal, ...dangerous]
  }, [actions])

  const primaryAction = useMemo(() => actions.find((action) => action.primary), [actions])
  const hasBadge = useMemo(
    () => actions.some((action) => action.badge && !action.disabled),
    [actions],
  )

  const restoreFocus = useCallback(() => {
    triggerRef.current?.focus()
  }, [])

  const close = useCallback(() => {
    setOpen(false)
    restoreFocus()
  }, [restoreFocus])

  const reposition = useCallback(() => {
    const anchor = triggerRef.current
    if (!anchor) return
    const rect = anchor.getBoundingClientRect()
    const spaceBelow = window.innerHeight - rect.bottom
    const spaceAbove = rect.top
    const showAbove = spaceBelow < MENU_MAX_HEIGHT && spaceAbove > spaceBelow
    const maxHeight = Math.min(
      MENU_MAX_HEIGHT,
      Math.max(120, (showAbove ? spaceAbove : spaceBelow) - MENU_GAP * 2),
    )
    // 操作列贴在表格右端，菜单右对齐触发器，再夹回视口内。
    const left = Math.min(
      Math.max(VIEWPORT_PADDING, rect.right - MENU_WIDTH),
      Math.max(VIEWPORT_PADDING, window.innerWidth - MENU_WIDTH - VIEWPORT_PADDING),
    )

    setStyle({
      position: 'fixed',
      left,
      top: showAbove ? undefined : rect.bottom + MENU_GAP,
      bottom: showAbove ? window.innerHeight - rect.top + MENU_GAP : undefined,
      width: MENU_WIDTH,
      maxHeight,
      zIndex: 55,
    })
  }, [])

  useLayoutEffect(() => {
    if (!open) return
    reposition()
  }, [open, reposition])

  useLayoutEffect(() => {
    if (!open) return
    const first = menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')
    first?.focus()
  }, [open])

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

  const focusableItems = useCallback((): HTMLElement[] => {
    const menu = menuRef.current
    if (!menu) return []
    return Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')).filter(
      (element) => element.getClientRects().length > 0,
    )
  }, [])

  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' || event.key === 'Tab') {
      // Tab 不循环：把焦点还给触发器，让用户的 Tab 继续走页面正常的顺序。
      event.preventDefault()
      event.stopPropagation()
      close()
      return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Home' && event.key !== 'End') {
      return
    }
    const items = focusableItems()
    if (items.length === 0) return
    event.preventDefault()
    const activeIndex = items.indexOf(document.activeElement as HTMLElement)
    if (event.key === 'Home') {
      items[0].focus()
      return
    }
    if (event.key === 'End') {
      items[items.length - 1].focus()
      return
    }
    const step = event.key === 'ArrowDown' ? 1 : -1
    const next = activeIndex === -1 ? 0 : Math.min(items.length - 1, Math.max(0, activeIndex + step))
    items[next].focus()
  }

  const onTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    setOpen(true)
  }

  const title = hasBadge ? `${triggerLabel}（有需要注意的项）` : triggerLabel

  return (
    <>
      {primaryAction ? (
        <button
          type="button"
          onClick={primaryAction.onSelect}
          disabled={primaryAction.disabled}
          aria-label={primaryAction.label}
          title={primaryAction.disabled && primaryAction.disabledReason ? primaryAction.disabledReason : primaryAction.label}
          className={cn(
            'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control transition-colors',
            'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]',
            primaryAction.disabled
              ? 'cursor-not-allowed text-disabled'
              : 'text-secondary hover:bg-subtle hover:text-ink',
            className,
          )}
        >
          {primaryAction.icon ? <primaryAction.icon size={16} aria-hidden /> : <span className="text-meta">{primaryAction.label}</span>}
        </button>
      ) : null}

      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((previous) => !previous)}
        onKeyDown={onTriggerKeyDown}
        aria-label={title}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title={title}
        className={cn(
          'relative inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-secondary transition-colors',
          'hover:bg-subtle hover:text-ink',
          'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]',
          open && 'bg-subtle text-ink',
          className,
        )}
      >
        <MoreHorizontal size={18} aria-hidden />
        {hasBadge ? (
          <span
            className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-status-warn"
            aria-hidden="true"
          />
        ) : null}
      </button>

      {open && typeof document !== 'undefined'
        ? createPortal(
            <>
              <div className="fixed inset-0 z-[54]" onClick={close} aria-hidden="true" />
              <div
                ref={menuRef}
                id={menuId}
                role="menu"
                aria-label={triggerLabel}
                style={style}
                onKeyDown={onMenuKeyDown}
                className="flex flex-col overflow-y-auto rounded-control border border-line bg-surface py-1 shadow-overlay"
              >
                {menuActions.map((action, index) => {
                  const previous = index > 0 ? menuActions[index - 1] : null
                  const startsDangerGroup = action.danger && !previous?.danger
                  const Icon = action.icon
                  return (
                    <Fragment key={action.id}>
                      {startsDangerGroup ? (
                        <div role="separator" className="my-1 border-t border-line" />
                      ) : null}
                      <button
                        type="button"
                        role="menuitem"
                        tabIndex={-1}
                        aria-disabled={action.disabled ? 'true' : undefined}
                        onClick={() => {
                          if (action.disabled) return
                          close()
                          action.onSelect()
                        }}
                        className={cn(
                          'flex min-h-[44px] w-full items-center gap-2 px-3 py-1 text-left text-meta transition-colors',
                          'focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]',
                          action.disabled && 'cursor-not-allowed text-disabled',
                          !action.disabled && action.danger && 'text-status-critical-text hover:bg-status-critical-surface',
                          !action.disabled && !action.danger && 'text-secondary hover:bg-subtle hover:text-ink',
                          !action.disabled && !action.danger && action.badge && 'text-status-warn-text',
                        )}
                      >
                        {Icon ? <Icon size={14} aria-hidden className="shrink-0" /> : null}
                        <span className="flex min-w-0 flex-col">
                          <span className="whitespace-nowrap">{action.label}</span>
                          {action.disabled && action.disabledReason ? (
                            <span className="mt-0.5 whitespace-nowrap text-micro text-muted">
                              {action.disabledReason}
                            </span>
                          ) : null}
                        </span>
                      </button>
                    </Fragment>
                  )
                })}
              </div>
            </>,
            document.body,
          )
        : null}
    </>
  )
}