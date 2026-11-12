import Link from 'next/link'
import { ArrowRight } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card } from '@/components/ui'
import { DICTIONARY_ITEMS } from '@/components/settings/settings-config'

export default function DictionariesPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="字典与属性"
        description="资产类别、机构与渠道、流动性档位、成员、标签与支出分类；每个名词表只有一个编辑入口。"
      />

      <div className="grid grid-cols-1 gap-3 tablet:grid-cols-2">
        {DICTIONARY_ITEMS.map((item) => (
          <Link key={item.href} href={item.href} className="block rounded-card">
            <Card padding="md" interactive className="h-full">
              <div className="flex items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-control bg-subtle text-secondary">
                  <item.icon size={18} aria-hidden />
                </span>
                <div className="min-w-0 flex-1">
                  <h2 className="text-h3 font-semibold text-ink">{item.label}</h2>
                  <p className="mt-1 text-body text-secondary">{item.description}</p>
                </div>
                <ArrowRight size={16} className="mt-1 shrink-0 text-muted" aria-hidden />
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}