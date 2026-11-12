'use client'

import Link from 'next/link'
import { ArrowRight, Camera, Database, Download, Upload } from 'lucide-react'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card } from '@/components/ui'

const DATA_ACTIONS = [
  {
    href: '/screenshot',
    label: '截图录入',
    description: '从 App 截图识别资产与支出，适合日常快速补录。',
    icon: Camera,
  },
  {
    href: '/import',
    label: 'CSV 导入',
    description: '批量导入表格数据，保留导入历史与校验结果。',
    icon: Upload,
  },
  {
    href: '/export',
    label: '数据导出',
    description: '导出资产记录 CSV，查看导出历史与自动导出规则。',
    icon: Download,
  },
]

export default function DataPage() {
  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="数据"
        description="把截图、CSV 变成数据，或把账本导出；配置在设置，不在这里。"
      />

      <div className="grid grid-cols-1 gap-3 tablet:grid-cols-3">
        {DATA_ACTIONS.map((action) => (
          <Link key={action.href} href={action.href} className="block rounded-card">
            <Card padding="md" interactive className="h-full">
              <span className="flex h-9 w-9 items-center justify-center rounded-control bg-subtle text-secondary">
                <action.icon size={20} aria-hidden />
              </span>
              <div className="mt-4 flex items-start justify-between gap-3">
                <h2 className="text-h3 font-semibold text-ink">{action.label}</h2>
                <ArrowRight size={18} className="mt-0.5 shrink-0 text-muted" aria-hidden />
              </div>
              <p className="mt-1.5 text-body text-secondary">{action.description}</p>
            </Card>
          </Link>
        ))}
      </div>

      <p className="flex items-center gap-2 text-meta text-muted">
        <Database size={14} aria-hidden />
        数据任务与系统配置分开展示；导入/导出的历史留在各自页面。
      </p>
    </PageContainer>
  )
}