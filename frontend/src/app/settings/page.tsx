'use client'

import { useMemo, useState } from 'react'
import Link from 'next/link'
import { ChevronRight, Search } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { EmptyState } from '@/components/ui'
import { SETTINGS_SECTIONS } from '@/components/settings/settings-config'

export default function SettingsPage() {
  const [query, setQuery] = useState('')

  const sections = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return SETTINGS_SECTIONS
    return SETTINGS_SECTIONS.map((section) => ({
      ...section,
      items: section.items.filter((item) =>
        `${item.label} ${item.description}`.toLowerCase().includes(q),
      ),
    })).filter((section) => section.items.length > 0)
  }, [query])

  return (
    <div className="space-y-6">
      <PageHeader
        title="设置"
        description="数据安全、字典、自动化、采集与关于；危险操作只出现在具体页面内。"
      />

      <div className="relative w-full max-w-[320px]">
        <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索设置"
          aria-label="搜索设置"
          className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
        />
      </div>

      {sections.length === 0 ? (
        <EmptyState compact title="没有匹配的设置" description="换个关键词试试。" />
      ) : (
        <div className="space-y-4">
          {sections.map((section) => (
            <section key={section.id} aria-labelledby={`settings-${section.id}`} className="overflow-hidden rounded-card border border-line bg-surface shadow-card">
              <div className="flex items-start gap-3 border-b border-line px-4 py-3 tablet:px-5">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-control bg-subtle text-secondary">
                  <section.icon size={18} aria-hidden />
                </span>
                <div className="min-w-0">
                  <h2 id={`settings-${section.id}`} className="text-h3 font-semibold text-ink">{section.label}</h2>
                  <p className="mt-0.5 text-meta text-muted">{section.description}</p>
                </div>
              </div>
              <ul className="divide-y divide-line">
                {section.items.map((item) => (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      className="pf-subtle-link flex min-h-[56px] items-center gap-3 px-4 py-2 tablet:px-5"
                    >
                      <item.icon size={16} className="shrink-0 text-muted" aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="block text-body font-medium text-ink">{item.label}</span>
                        <span className="block text-meta text-muted">{item.description}</span>
                      </span>
                      <ChevronRight size={16} className="shrink-0 text-muted" aria-hidden />
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}