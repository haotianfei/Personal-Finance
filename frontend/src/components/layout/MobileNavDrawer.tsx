'use client'

import { useCallback, useEffect, useRef } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { X } from 'lucide-react'
import { APP_VERSION } from '@/lib/version'
import {
  APP_BRAND_NAME,
  APP_BRAND_TILE,
  DRAWER_SECTIONS,
  isActivePath,
  isItemActive,
  type NavItem,
} from './nav-config'
import { useMobileNav } from './MobileNavContext'
import { useDialogA11y } from './useDialogA11y'
import { useMediaQuery } from '@/hooks/useMediaQuery'

/**
 * 「更多」抽屉（<1024）：承载底部 Tab 之外的全部路由。
 *
 * 条目 100% 来自 nav-config；设置组独立于工作区，单独分区显示。
 * a11y：role=dialog / aria-modal / Esc / 焦点陷阱 / 关闭后焦点还原 / 背景 inert
 * 由 useDialogA11y 统一处理；底部与左右安全区齐全。
 */
export default function MobileNavDrawer() {
  const pathname = usePathname()
  const { open, closeNav } = useMobileNav()
  const containerRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const lastPathnameRef = useRef(pathname)

  const close = useCallback(() => closeNav(), [closeNav])
  const isDesktop = useMediaQuery('(min-width: 1024px)')
  const { suppressRestoreRef } = useDialogA11y({
    open,
    onClose: close,
    containerRef,
    initialFocusRef: closeRef,
  })

  // 跨过 1024 断点后抽屉已隐藏，必须释放焦点陷阱与背景 inert
  useEffect(() => {
    if (isDesktop) closeNav()
  }, [isDesktop, closeNav])

  // 客户端路由跳转不会卸载本组件，靠 pathname 兜底关闭（Link 的 onClick 已即时关闭）
  useEffect(() => {
    if (lastPathnameRef.current === pathname) return
    lastPathnameRef.current = pathname
    if (!open) return
    suppressRestoreRef.current = true
    closeNav()
  }, [pathname, open, closeNav, suppressRestoreRef])

  if (!open) return null

  const handleNavigate = () => {
    suppressRestoreRef.current = true
    closeNav()
  }

  return (
    <div
      ref={containerRef}
      className="pf-on-dark fixed inset-0 z-modal lg:hidden"
      role="dialog"
      aria-modal="true"
      aria-label="更多"
      tabIndex={-1}
    >
      <div
        className="absolute inset-0 animate-fade-in bg-black/40"
        onClick={closeNav}
        aria-hidden="true"
      />

      <div
        className="animate-slide-in-left absolute inset-y-0 left-0 flex w-[min(84vw,18rem)] flex-col bg-inverse pt-[var(--pf-safe-top)] text-inverse-text shadow-overlay"
        style={{
          paddingLeft: 'var(--pf-safe-left)',
          paddingRight: 'var(--pf-safe-right)',
        }}
      >
        <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-white/10 px-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-brand-600 text-sm font-bold text-white">
            {APP_BRAND_TILE}
          </span>
          <span className="min-w-0 flex-1 truncate text-base font-semibold tracking-tight">
            {APP_BRAND_NAME}
          </span>
          <button
            ref={closeRef}
            type="button"
            onClick={closeNav}
            aria-label="关闭导航菜单"
            className="pf-tool-btn"
          >
            <X size={20} />
          </button>
        </div>

        <nav className="nav-scroll min-h-0 flex-1 overflow-y-auto px-2 py-3" aria-label="更多功能">
          {DRAWER_SECTIONS.map((section) => (
            <div key={section.id} className="space-y-1 pb-3">
              <p className="pf-nav-group-label">{section.label}</p>
              <ul className="space-y-1">
                {section.items.map((item) => (
                  <DrawerNode key={item.id} item={item} pathname={pathname} onNavigate={handleNavigate} />
                ))}
              </ul>
            </div>
          ))}
        </nav>

        {/* 页脚：版本与「关于」；GitHub 外链收进设置 > 关于 */}
        <div className="shrink-0 border-t border-white/10 px-4 pb-[calc(0.75rem_+_var(--pf-safe-bottom))] pt-3">
          <Link
            href="/settings/about"
            onClick={handleNavigate}
            className="flex min-h-[44px] items-center justify-between gap-2 text-meta text-nav-idle"
          >
            <span>关于与诊断</span>
            <span className="pf-num">v{APP_VERSION}</span>
          </Link>
          <p className="text-meta text-nav-group">© 2026 郝天飞 (HaoTianfei)</p>
        </div>
      </div>
    </div>
  )
}

function DrawerNode({
  item,
  pathname,
  onNavigate,
}: {
  item: NavItem
  pathname: string
  onNavigate: () => void
}) {
  const active = isItemActive(pathname, item)
  const selfActive = item.href ? isActivePath(pathname, item.href) : false
  const icon = <item.icon size={18} className="shrink-0" aria-hidden />

  return (
    <li>
      {item.href ? (
        <Link
          href={item.href}
          onClick={onNavigate}
          aria-current={selfActive ? 'page' : undefined}
          data-active={active && !selfActive || undefined}
          className="pf-nav-link"
        >
          {icon}
          <span className="min-w-0 truncate">{item.label}</span>
        </Link>
      ) : (
        <p className="px-3 pb-1 pt-2 text-meta font-medium text-nav-group">{item.label}</p>
      )}
      {item.children && item.children.length > 0 && (
        <ul className="ml-3 space-y-1 border-l border-white/10 pl-2">
          {item.children.map((child) => (
            <DrawerNode key={child.id} item={child} pathname={pathname} onNavigate={onNavigate} />
          ))}
        </ul>
      )}
    </li>
  )
}
