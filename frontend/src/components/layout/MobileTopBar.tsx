'use client'

import { usePathname } from 'next/navigation'
import { Menu } from 'lucide-react'
import { APP_BRAND_TILE, resolveNavTitle } from './nav-config'
import { useMobileNav } from './MobileNavContext'

/**
 * 移动端顶栏（<1024）：56px + 顶部安全区 + 左右安全区。
 *
 * 用 sticky 而非 fixed：占据真实布局空间，main 不需要额外预留 padding-top；
 * 桌面端整条不渲染（lg:hidden），桌面导航交给 Sidebar。
 */
export default function MobileTopBar() {
  const pathname = usePathname()
  const { openNav } = useMobileNav()

  return (
    <header
      className="pf-on-dark sticky top-0 z-nav shrink-0 bg-inverse pt-[var(--pf-safe-top)] text-inverse-text shadow-card lg:hidden"
    >
      <div
        className="flex h-14 items-center gap-1"
        style={{
          paddingLeft: 'calc(0.375rem + var(--pf-safe-left))',
          paddingRight: 'calc(0.375rem + var(--pf-safe-right))',
        }}
      >
        <button
          type="button"
          onClick={openNav}
          aria-label="打开导航菜单"
          aria-haspopup="dialog"
          className="pf-tool-btn shrink-0"
        >
          <Menu size={22} />
        </button>

        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-brand-600 text-sm font-bold text-white">
            {APP_BRAND_TILE}
          </span>
          <p className="min-w-0 truncate text-base font-semibold tracking-tight">
            {resolveNavTitle(pathname)}
          </p>
        </div>

        {/* 右侧占位，与左侧汉堡等宽，保证品牌块 + 标题在栏内视觉居中 */}
        <div className="h-11 w-11 shrink-0" aria-hidden="true" />
      </div>
    </header>
  )
}