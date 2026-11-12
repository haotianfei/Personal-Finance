'use client'

import Link from 'next/link'
import { Github } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card } from '@/components/ui'
import { APP_REPO_URL } from '@/components/layout/nav-config'
import { APP_VERSION } from '@/lib/version'

export default function AboutSettingsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="关于与诊断"
        description="版本、仓库与运行信息。"
      />

      <Card padding="md" className="space-y-4">
        <div>
          <p className="text-meta text-muted">版本</p>
          <p className="pf-num mt-1 text-body-strong text-ink">v{APP_VERSION}</p>
        </div>
        <div className="border-t border-line pt-4">
          <p className="text-meta text-muted">仓库</p>
          <a
            href={APP_REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="pf-subtle-link mt-1 inline-flex min-h-[44px] items-center gap-2 text-body text-link"
          >
            <Github size={16} aria-hidden />
            haotianfei/Personal-Finance
          </a>
        </div>
        <div className="border-t border-line pt-4">
          <p className="text-meta text-muted">数据与隐私</p>
          <p className="mt-1 text-body text-secondary">
            数据仅存储在本机数据库；本页不包含诊断上传。数据库路径与备份策略在「备份与恢复」。
          </p>
          <Link href="/settings/backup-restore" className="mt-2 inline-flex min-h-[44px] items-center text-body text-link">
            前往备份与恢复
          </Link>
        </div>
      </Card>
    </div>
  )
}