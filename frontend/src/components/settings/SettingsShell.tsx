'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SETTINGS_SECTIONS } from './settings-config'
import { resolveNavTitle } from '@/components/layout/nav-config'

function sectionFor(pathname: string) {
  for (const section of SETTINGS_SECTIONS) {
    const item = section.items.find((entry) => pathname === entry.href || pathname.startsWith(`${entry.href}/`))
    if (item) return { section, item }
  }
  return null
}

export function SettingsShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const match = sectionFor(pathname)
  const isIndex = pathname === '/settings'
  const activeHref = SETTINGS_SECTIONS
    .flatMap((section) => section.items)
    .filter((item) => pathname === item.href || pathname.startsWith(`${item.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0]?.href

  // 宽度与内边距一律交给外层 PageContainer：这里只负责「设置导航 + 内容」的网格比例
  return (
    <div className="grid gap-6 lg:grid-cols-[200px_minmax(0,1fr)]">
      <nav
        aria-label="设置分区"
        className="hidden self-start lg:sticky lg:top-6 lg:block"
      >
        <div className="space-y-3 rounded-card border border-line bg-surface p-3 shadow-card">
          {SETTINGS_SECTIONS.map((section) => {
            const active = match?.section.id === section.id
            return (
              <div key={section.id}>
                <p className={cn('px-2 pb-1 pt-1 text-meta font-medium', active ? 'text-ink' : 'text-muted')}>
                  {section.label}
                </p>
                <ul className="space-y-0.5">
                  {section.items.map((item) => {
                    const current = activeHref === item.href
                    return (
                      <li key={item.href}>
                        <Link
                          href={item.href}
                          aria-current={current ? 'page' : undefined}
                          className={cn(
                            'flex min-h-[44px] items-center gap-2 rounded-control px-2 text-meta',
                            current ? 'bg-brand-50 font-medium text-brand-700' : 'text-secondary',
                          )}
                        >
                          <item.icon size={14} className="shrink-0" aria-hidden />
                          <span className="min-w-0 truncate">{item.label}</span>
                        </Link>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )
          })}
        </div>
      </nav>

      <div className="min-w-0 space-y-4">
        {!isIndex && (
          <nav aria-label="面包屑" className="flex flex-wrap items-center gap-1 text-meta text-muted">
            <Link href="/settings" className="pf-subtle-link inline-flex min-h-[32px] items-center">设置</Link>
            {match && (
              <>
                <ChevronRight size={12} aria-hidden />
                <span>{match.section.label}</span>
                {resolveNavTitle(pathname) !== match.section.label && (
                  <>
                    <ChevronRight size={12} aria-hidden />
                    <span className="text-secondary">{resolveNavTitle(pathname)}</span>
                  </>
                )}
              </>
            )}
          </nav>
        )}
        {children}
      </div>
    </div>
  )
}