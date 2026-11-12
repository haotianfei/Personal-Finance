'use client'

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, DataTable } from '@/components/ui'
import type { DataTableColumn } from '@/components/ui'
import type { LiquidityLevel } from '@/types'

/**
 * 流动性档位（ADR-0019 D4）：**8 档只读常量**，`days` 是权威值、`label` 是展示派生。
 * 旧的流动性档位 CRUD 已 410——档位不随用户增长，编辑入口不存在。
 */
export default function LiquidityLevelsSettingsPage() {
  const levelsQuery = useQuery({
    queryKey: ['liquidityLevels'],
    queryFn: api.getLiquidityLevels,
    staleTime: Infinity,
  })
  const rows: LiquidityLevel[] = useMemo(() => levelsQuery.data?.items ?? [], [levelsQuery.data])

  const columns: DataTableColumn<LiquidityLevel>[] = [
    {
      key: 'label', header: '档位', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.label}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'days', header: '天数（权威值）', widthTier: 'count', align: 'right',
      cell: (row) => <span className="pf-num text-secondary">{row.days >= 9999 ? '9999+（长期）' : `${row.days} 天`}</span>,
      mobile: { role: 'value', label: '天数' },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="流动性档位"
        description="固定 8 档（只读常量）：资产项存天数，档位名只是展示派生，不随用户增长。"
      />

      <Card padding="none" className="overflow-hidden">
        <DataTable
          label="流动性档位列表"
          rows={rows}
          columns={columns}
          rowKey={(row) => row.days}
          loading={levelsQuery.isLoading}
          error={levelsQuery.error}
          onRetry={() => levelsQuery.refetch()}
          empty={{ title: '暂无档位', description: '档位由后端常量表提供（8 档）。' }}
        />
      </Card>
    </div>
  )
}