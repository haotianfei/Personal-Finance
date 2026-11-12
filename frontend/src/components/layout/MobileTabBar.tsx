'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { MoreHorizontal } from 'lucide-react'
import { cn } from '@/lib/utils'
import { MOBILE_TABS, isItemActive, itemHref } from './nav-config'
import { useMobileNav } from './MobileNavContext'

/**
 * 移动端底部 Tab（<1024）：4–5 个高频路由 + 「更多」。
 *
 * 条目来自 NAV_CONFIG 的 `mobile.mode === 'tab'`；空位始终留给「更多」。
 * sticky bottom-0 占据真实布局空间，最后一屏内容不会被永久遮住；
 * 高度 56 + 底部安全区，背景延伸进 iOS 手势条。
 */
export default function MobileTabBar() {
  const pathname = usePathname()
  const { open, openNav } = useMobileNav()

  // 当前路由不在 Tab 里（含设置页）→ 由「更多」承担高亮
  const moreActive = !MOBILE_TABS.some((item) => isItemActive(pathname, item))
  const columns = MOBILE_TABS.length + 1

  return (
    <nav
      aria-label="主导航"
      className="sticky bottom-0 z-nav shrink-0 border-t border-line bg-surface/95 pb-[var(--pf-safe-bottom)] backdrop-blur lg:hidden"
      style={{
        paddingLeft: 'var(--pf-safe-left)',
        paddingRight: 'var(--pf-safe-right)',
      }}
    >
      <ul className="grid" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {MOBILE_TABS.map((item) => {
          const href = itemHref(item)
          if (!href) return null
          const active = isItemActive(pathname, item)
          return (
            <li key={item.id}>
              <Link
                href={href}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'relative flex h-14 w-full flex-col items-center justify-center gap-1 text-meta leading-none transition-colors duration-fast',
                  active ? 'font-medium text-brand-600' : 'text-secondary',
                )}
              >
                {active && (
                  <span className="absolute top-0 h-0.5 w-8 rounded-full bg-brand-600" aria-hidden="true" />
                )}
                <item.icon size={21} strokeWidth={active ? 2.4 : 2} className="shrink-0" aria-hidden />
                <span className="w-full truncate px-0.5 text-center">
                  {item.mobile.shortLabel ?? item.label}
                </span>
              </Link>
            </li>
          )
        })}

        <li>
          <button
            type="button"
            onClick={openNav}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-current={moreActive ? 'page' : undefined}
            className={cn(
              'relative flex h-14 w-full flex-col items-center justify-center gap-1 text-meta leading-none transition-colors duration-fast',
              moreActive || open ? 'font-medium text-brand-600' : 'text-secondary',
            )}
          >
            {(moreActive || open) && (
              <span className="absolute top-0 h-0.5 w-8 rounded-full bg-brand-600" aria-hidden="true" />
            )}
            <MoreHorizontal size={21} className="shrink-0" aria-hidden />
            <span>更多</span>
          </button>
        </li>
      </ul>
    </nav>
  )
}