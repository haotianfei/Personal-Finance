'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { cn } from '@/lib/utils'
import { ChevronDown, ChevronLeft, ChevronRight, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  APP_BRAND_NAME,
  APP_BRAND_TILE,
  SETTINGS_GROUPS,
  SIDEBAR_COLLAPSED_KEY,
  WORKSPACE_GROUPS,
  isActivePath,
  isItemActive,
  itemsForGroup,
  itemHref,
  readPref,
  useIsomorphicLayoutEffect,
  writePref,
  type NavGroup,
  type NavItem,
} from './nav-config'
import { COMPACT_DESKTOP_QUERY, useMediaQuery } from '@/hooks/useMediaQuery'
import { useDialogA11y } from './useDialogA11y'

/**
 * 桌面侧栏（≥1024）。
 *
 * 菜单 100% 由 nav-config 派生，本文件不写死路由/分组；config 支持
 * 「叶子 / 分区（children）/ 入口+子项」三种节点，IA 定稿后只换 config。
 *
 * - ≥1280：224，可折叠为 64（偏好沿用旧 key）；
 * - 1024–1279：自动 64，点击组/分区图标或底部展开按钮打开 224 overlay；
 * - 收起态只有图标，44px 命中 + hover/focus 文字提示；
 * - 设置组独立于工作区，固定在底部；
 * - overlay：role=dialog / aria-modal / 焦点陷阱 / Esc / 焦点还原 / 背景 inert。
 */
export default function Sidebar() {
  const pathname = usePathname()
  const isCompactDesktop = useMediaQuery(COMPACT_DESKTOP_QUERY)
  const isDesktop = useMediaQuery('(min-width: 1024px)')

  const [collapsed, setCollapsed] = useState(false)
  const [overlayOpen, setOverlayOpen] = useState(false)
  const [openBranches, setOpenBranches] = useState<Record<string, boolean>>({})
  const overlayRef = useRef<HTMLDivElement>(null)
  const overlayCloseRef = useRef<HTMLButtonElement>(null)
  const lastPathnameRef = useRef(pathname)

  useIsomorphicLayoutEffect(() => {
    setCollapsed(readPref(SIDEBAR_COLLAPSED_KEY) ?? false)
  }, [])

  const compact = collapsed || isCompactDesktop

  const closeOverlay = useCallback(() => setOverlayOpen(false), [])
  const { suppressRestoreRef } = useDialogA11y({
    open: overlayOpen,
    onClose: closeOverlay,
    containerRef: overlayRef,
    initialFocusRef: overlayCloseRef,
  })

  // 窗口回到 <1024 时侧栏整体隐藏，overlay 不应留在屏幕上
  useEffect(() => {
    if (!isDesktop) closeOverlay()
  }, [isDesktop, closeOverlay])

  // 路由跳转后关闭 overlay（Link onClick 已即时处理，这里兜底）。
  // 只在 pathname 真正变化时触发，避免「打开 overlay」本身被当成导航。
  useEffect(() => {
    if (lastPathnameRef.current === pathname) return
    lastPathnameRef.current = pathname
    if (!overlayOpen) return
    suppressRestoreRef.current = true
    closeOverlay()
  }, [pathname, overlayOpen, closeOverlay, suppressRestoreRef])

  const toggleCollapsed = () => {
    if (isCompactDesktop) {
      setOverlayOpen(true)
      return
    }
    const next = !collapsed
    setCollapsed(next)
    writePref(SIDEBAR_COLLAPSED_KEY, next)
  }

  const toggleBranch = (id: string, next: boolean) => {
    setOpenBranches((prev) => ({ ...prev, [id]: next }))
  }

  const handleNavigate = () => {
    suppressRestoreRef.current = true
    closeOverlay()
  }

  return (
    <aside
      aria-label="主导航"
      className={cn(
        'pf-on-dark hidden lg:flex sticky top-0 pf-h-dvh shrink-0 flex-col border-r border-white/10 bg-inverse text-inverse-text',
        'transition-[width] duration-base ease-standard',
        compact ? 'w-16' : 'w-16 xl:w-56',
      )}
    >
      <Link
        href="/"
        aria-label={APP_BRAND_NAME}
        className={cn(
          'flex h-14 shrink-0 items-center border-b border-white/10',
          compact ? 'justify-center px-2' : 'gap-2.5 px-4',
        )}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-brand-600 text-sm font-bold text-white">
          {APP_BRAND_TILE}
        </span>
        {!compact && (
          <span className="truncate text-base font-semibold tracking-tight">{APP_BRAND_NAME}</span>
        )}
      </Link>

      <nav className="nav-scroll min-h-0 flex-1 overflow-y-auto px-2 py-3" aria-label="工作区导航">
        {compact ? (
          <CollapsedRail pathname={pathname} onOpenBranch={() => setOverlayOpen(true)} />
        ) : (
          <div className="space-y-3">
            {WORKSPACE_GROUPS.map((group) => (
              <ExpandedGroup
                key={group.id}
                group={group}
                pathname={pathname}
                openBranches={openBranches}
                onToggleBranch={toggleBranch}
                onNavigate={handleNavigate}
              />
            ))}
          </div>
        )}
      </nav>

      {/* 设置：独立于工作区的固定区域 */}
      <div className="shrink-0 border-t border-white/10 px-2 py-2">
        {compact ? (
          <div className="flex flex-col items-center gap-1">
            {SETTINGS_GROUPS.flatMap((group) =>
              itemsForGroup(group.id).map((item) => (
                <CollapsedItem
                  key={item.id}
                  item={item}
                  pathname={pathname}
                  onOpenBranch={() => setOverlayOpen(true)}
                  onNavigate={handleNavigate}
                />
              )),
            )}
          </div>
        ) : (
          <SettingsGroup
            pathname={pathname}
            openBranches={openBranches}
            onToggleBranch={toggleBranch}
            onNavigate={handleNavigate}
          />
        )}
      </div>

      <button
        type="button"
        onClick={toggleCollapsed}
        aria-label={isCompactDesktop ? '展开完整导航' : collapsed ? '展开侧边栏' : '收起侧边栏'}
        aria-expanded={isCompactDesktop ? overlayOpen : !collapsed}
        className="flex h-12 shrink-0 items-center justify-center border-t border-white/10 text-nav-idle transition-colors duration-fast"
      >
        {compact ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
      </button>

      {overlayOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={overlayRef}
            className="pf-on-dark fixed inset-0 z-modal hidden lg:block"
            role="dialog"
            aria-modal="true"
            aria-label="导航面板"
            tabIndex={-1}
          >
            <div
              className="absolute inset-0 animate-fade-in bg-black/40"
              onClick={closeOverlay}
              aria-hidden="true"
            />
            <div className="animate-slide-in-left absolute inset-y-0 left-0 flex w-56 flex-col bg-inverse text-inverse-text shadow-overlay">
              <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-white/10 px-2.5">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-brand-600 text-sm font-bold text-white">
                  {APP_BRAND_TILE}
                </span>
                <span className="min-w-0 flex-1 truncate text-base font-semibold tracking-tight">
                  {APP_BRAND_NAME}
                </span>
                <button
                  ref={overlayCloseRef}
                  type="button"
                  onClick={closeOverlay}
                  aria-label="关闭导航"
                  className="pf-tool-btn"
                >
                  <X size={20} />
                </button>
              </div>

              <nav className="nav-scroll min-h-0 flex-1 overflow-y-auto px-2 py-3" aria-label="全部导航">
                <div className="space-y-3">
                  {WORKSPACE_GROUPS.map((group) => (
                    <ExpandedGroup
                      key={group.id}
                      group={group}
                      pathname={pathname}
                      openBranches={openBranches}
                      onToggleBranch={toggleBranch}
                      onNavigate={handleNavigate}
                    />
                  ))}
                </div>
                <div className="mt-3 border-t border-white/10 pt-2">
                  <SettingsGroup
                    pathname={pathname}
                    openBranches={openBranches}
                    onToggleBranch={toggleBranch}
                    onNavigate={handleNavigate}
                  />
                </div>
              </nav>
            </div>
          </div>,
          document.body,
        )}
    </aside>
  )
}

// --- 展开态 --------------------------------------------------------------

function ExpandedGroup({
  group,
  pathname,
  openBranches,
  onToggleBranch,
  onNavigate,
}: {
  group: NavGroup
  pathname: string
  openBranches: Record<string, boolean>
  onToggleBranch: (id: string, next: boolean) => void
  onNavigate: () => void
}) {
  return (
    <div>
      <p className="pf-nav-group-label">{group.label}</p>
      <NavNodeList
        items={itemsForGroup(group.id)}
        pathname={pathname}
        openBranches={openBranches}
        onToggleBranch={onToggleBranch}
        onNavigate={onNavigate}
      />
    </div>
  )
}

function SettingsGroup({
  pathname,
  openBranches,
  onToggleBranch,
  onNavigate,
}: {
  pathname: string
  openBranches: Record<string, boolean>
  onToggleBranch: (id: string, next: boolean) => void
  onNavigate: () => void
}) {
  return (
    <div className="space-y-2">
      {SETTINGS_GROUPS.map((group) => (
        <div key={group.id}>
          <p className="pf-nav-group-label">{group.label}</p>
          <NavNodeList
            items={itemsForGroup(group.id)}
            pathname={pathname}
            openBranches={openBranches}
            onToggleBranch={onToggleBranch}
            onNavigate={onNavigate}
          />
        </div>
      ))}
    </div>
  )
}

function NavNodeList({
  items,
  pathname,
  openBranches,
  onToggleBranch,
  onNavigate,
  depth = 0,
}: {
  items: NavItem[]
  pathname: string
  openBranches: Record<string, boolean>
  onToggleBranch: (id: string, next: boolean) => void
  onNavigate: () => void
  depth?: number
}) {
  return (
    <ul className={cn('space-y-1', depth > 0 && 'ml-3 border-l border-white/10 pl-2')}>
      {items.map((item) => (
        <li key={item.id}>
          <NavNode
            item={item}
            pathname={pathname}
            openBranches={openBranches}
            onToggleBranch={onToggleBranch}
            onNavigate={onNavigate}
            depth={depth}
          />
        </li>
      ))}
    </ul>
  )
}

function NavNode({
  item,
  pathname,
  openBranches,
  onToggleBranch,
  onNavigate,
  depth,
}: {
  item: NavItem
  pathname: string
  openBranches: Record<string, boolean>
  onToggleBranch: (id: string, next: boolean) => void
  onNavigate: () => void
  depth: number
}) {
  const children = item.children ?? []
  const hasChildren = children.length > 0
  const active = isItemActive(pathname, item)
  const open = hasChildren ? openBranches[item.id] ?? active : false
  const icon = <item.icon size={18} className="shrink-0" aria-hidden />

  if (!hasChildren) {
    if (!item.href) return null
    return (
      <Link
        href={item.href}
        onClick={onNavigate}
        aria-current={isActivePath(pathname, item.href) ? 'page' : undefined}
        className="pf-nav-link"
      >
        {icon}
        <span className="min-w-0 truncate">{item.label}</span>
      </Link>
    )
  }

  const chevron = (
    <ChevronDown
      size={16}
      aria-hidden
      className={cn('shrink-0 transition-transform duration-fast', open && 'rotate-180')}
    />
  )

  return (
    <>
      {item.href ? (
        <div className="flex items-center gap-1">
          <Link
            href={item.href}
            onClick={onNavigate}
            aria-current={item.branchIsPage && pathname === item.href ? 'page' : undefined}
            data-active={active && pathname !== item.href || undefined}
            className="pf-nav-link min-w-0 flex-1"
          >
            {icon}
            <span className="min-w-0 truncate">{item.label}</span>
          </Link>
          <button
            type="button"
            onClick={() => onToggleBranch(item.id, !open)}
            aria-expanded={open}
            aria-label={`${open ? '收起' : '展开'}${item.label}`}
            className="pf-nav-icon shrink-0"
          >
            {chevron}
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => onToggleBranch(item.id, !open)}
          aria-expanded={open}
          data-active={active || undefined}
          className="pf-nav-link w-full"
        >
          {icon}
          <span className="min-w-0 flex-1 truncate text-left">{item.label}</span>
          {chevron}
        </button>
      )}
      {open && (
        <NavNodeList
          items={children}
          pathname={pathname}
          openBranches={openBranches}
          onToggleBranch={onToggleBranch}
          onNavigate={onNavigate}
          depth={depth + 1}
        />
      )}
    </>
  )
}

// --- 收起态 --------------------------------------------------------------

function CollapsedRail({
  pathname,
  onOpenBranch,
}: {
  pathname: string
  onOpenBranch: () => void
}) {
  return (
    <div className="flex flex-col items-center gap-1">
      {WORKSPACE_GROUPS.map((group, index) => (
        <div key={group.id} className="contents">
          {index > 0 && <div className="my-1 h-px w-8 bg-white/10" aria-hidden />}
          {itemsForGroup(group.id).map((item) => (
            <CollapsedItem
              key={item.id}
              item={item}
              pathname={pathname}
              onOpenBranch={onOpenBranch}
              onNavigate={() => {}}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

function CollapsedItem({
  item,
  pathname,
  onOpenBranch,
  onNavigate,
}: {
  item: NavItem
  pathname: string
  onOpenBranch: () => void
  onNavigate: () => void
}) {
  const active = isItemActive(pathname, item)
  const href = itemHref(item)
  const hasChildren = (item.children?.length ?? 0) > 0
  const itemRef = useRef<HTMLElement | null>(null)
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null)

  // 收起态文字提示用 portal 渲染：nav 是滚动容器，原生 absolute 提示会被裁掉。
  const showTip = () => {
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return
    const el = itemRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setTip({ x: rect.right + 8, y: rect.top + rect.height / 2 })
  }
  const hideTip = () => setTip(null)
  const triggerProps = {
    onMouseEnter: showTip,
    onMouseLeave: hideTip,
    onFocus: showTip,
    onBlur: hideTip,
  }

  const tooltip =
    tip &&
    typeof document !== 'undefined' &&
    createPortal(
      <span
        aria-hidden="true"
        className="pointer-events-none fixed z-toast -translate-y-1/2 rounded-control border border-white/10 bg-inverse px-2.5 py-1.5 text-meta text-inverse-text shadow-overlay"
        style={{ left: tip.x, top: tip.y }}
      >
        {item.label}
      </span>,
      document.body,
    )

  if (hasChildren || !href) {
    return (
      <>
        <button
          ref={(el) => {
            itemRef.current = el
          }}
          type="button"
          onClick={onOpenBranch}
          aria-label={item.label}
          aria-haspopup="dialog"
          data-active={active || undefined}
          className="pf-nav-icon"
          {...triggerProps}
        >
          <item.icon size={20} aria-hidden />
        </button>
        {tooltip}
      </>
    )
  }

  return (
    <>
      <Link
        ref={(el) => {
          itemRef.current = el
        }}
        href={href}
        onClick={onNavigate}
        aria-label={item.label}
        aria-current={isActivePath(pathname, href) ? 'page' : undefined}
        className="pf-nav-icon"
        {...triggerProps}
      >
        <item.icon size={20} aria-hidden />
      </Link>
      {tooltip}
    </>
  )
}