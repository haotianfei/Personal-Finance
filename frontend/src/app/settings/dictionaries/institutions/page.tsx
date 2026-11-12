'use client'

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/layout/PageHeader'
import type { DataTableColumn } from '@/components/ui'
import { DictionaryCrudCard } from '@/components/settings/DictionaryCrudCard'
import type { DictionaryFieldSpec } from '@/components/settings/DictionaryCrudCard'
import { INSTITUTION_KIND_LABEL, INSTITUTION_KIND_OPTIONS } from '@/components/classification'
import type { InstitutionKind } from '@/types'
import type { Channel, Institution } from '@/types'

/**
 * 机构与渠道（ADR-0019 D3）：两个独立维度，两个独立值域。
 *
 * 旧「账户」一维把「在某银行」和「从 App 进」混在一列，导致按机构合计要写字符串前缀；
 * 拆开后渠道只在「同一机构多入口」时才填。
 */

const institutionFields: DictionaryFieldSpec[] = [
  { key: 'name', label: '机构名称', type: 'text', required: true, placeholder: '如：某银行A', maxLength: 40 },
  { key: 'kind', label: '类型', type: 'select', required: true, options: INSTITUTION_KIND_OPTIONS },
  { key: 'active', label: '启用中', type: 'toggle', hideOnCreate: true, hint: '停用后不会出现在分类选择里，历史照旧' },
]

const channelFields: DictionaryFieldSpec[] = [
  { key: 'name', label: '渠道名称', type: 'text', required: true, placeholder: '如：手机银行 App', maxLength: 40 },
  { key: 'active', label: '启用中', type: 'toggle', hideOnCreate: true, hint: '停用后不会出现在分类选择里' },
]

export default function InstitutionsSettingsPage() {
  const institutionsQuery = useQuery({ queryKey: ['institutions'], queryFn: api.getInstitutions })
  const channelsQuery = useQuery({ queryKey: ['channels'], queryFn: api.getChannels })

  const institutions: Institution[] = useMemo(
    () => institutionsQuery.data?.items ?? [], [institutionsQuery.data],
  )
  const channels: Channel[] = useMemo(
    () => channelsQuery.data?.items ?? [], [channelsQuery.data],
  )

  const institutionColumns: DataTableColumn<Institution>[] = [
    {
      key: 'name', header: '机构名称', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'kind', header: '类型', widthTier: 'text-short',
      cell: (row) => (
        <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">
          {INSTITUTION_KIND_LABEL[row.kind] ?? row.kind}
        </span>
      ),
      mobile: { role: 'meta', label: '类型' },
    },
    {
      key: 'active', header: '状态', widthTier: 'text-short',
      cell: (row) => (row.active
        ? <span className="rounded-pill bg-status-good-surface px-2 py-0.5 text-meta text-status-good-text">启用</span>
        : <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-muted">停用</span>),
      mobile: { role: 'meta', label: '状态' },
    },
  ]

  const channelColumns: DataTableColumn<Channel>[] = [
    {
      key: 'name', header: '渠道名称', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'active', header: '状态', widthTier: 'text-short',
      cell: (row) => (row.active
        ? <span className="rounded-pill bg-status-good-surface px-2 py-0.5 text-meta text-status-good-text">启用</span>
        : <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-muted">停用</span>),
      mobile: { role: 'meta', label: '状态' },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="机构与渠道"
        description="分类的两个独立维度：机构是「钱托管在哪里」，渠道是「从哪个入口进」。机构必填，渠道可空。"
      />

      <DictionaryCrudCard<Institution>
        title="机构"
        description="银行、券商、基金公司、保险、政府/公共、平台。"
        noun="机构"
        addLabel="新建机构"
        rows={institutions}
        loading={institutionsQuery.isLoading}
        error={institutionsQuery.error}
        onRetry={() => { void institutionsQuery.refetch() }}
        columns={institutionColumns}
        rowKey={(row) => row.id}
        nameOf={(row) => row.name}
        extraSearchText={(row) => INSTITUTION_KIND_LABEL[row.kind] ?? row.kind}
        emptyTitle="暂无机构"
        emptyDescription="先建一家机构，补分类时才能选到它。"
        fields={institutionFields}
        valuesOf={(row) => ({ name: row.name, kind: row.kind, active: row.active })}
        blankValues={{ name: '', kind: 'bank', active: true }}
        submit={(values, editing) => {
          const name = String(values.name).trim()
          const kind = values.kind as InstitutionKind
          return editing
            ? api.updateInstitution(editing.id, { name, kind, active: Boolean(values.active) })
            : api.createInstitution({ name, kind })
        }}
        remove={({ row, audit }) => api.deleteInstitution(row.id, audit)}
        deleteTitle={(row) => `删除机构「${row.name}」？`}
        deleteDescription="只有在没有任何资产项用它时才能删除；仍在用的请改用「停用」，历史不受影响。"
        invalidateKeys={['institutions', 'assetsCurrent', 'assetsList', 'assetItems']}
        activeToggle={{
          isActive: (row) => row.active,
          setActive: (row, active) => api.updateInstitution(row.id, { active }),
        }}
        notice="社保、公积金这类账户的机构类型请选「政府 / 公共」，不要记成银行。"
      />

      <DictionaryCrudCard<Channel>
        title="渠道"
        description="同一机构下的多个入口（App、子钱包、代销口）。"
        noun="渠道"
        addLabel="新建渠道"
        rows={channels}
        loading={channelsQuery.isLoading}
        error={channelsQuery.error}
        onRetry={() => { void channelsQuery.refetch() }}
        columns={channelColumns}
        rowKey={(row) => row.id}
        nameOf={(row) => row.name}
        emptyTitle="暂无渠道"
        emptyDescription="多数情况下不需要渠道：只有在同一机构有多个入口时才建。"
        fields={channelFields}
        valuesOf={(row) => ({ name: row.name, active: row.active })}
        blankValues={{ name: '', active: true }}
        submit={(values, editing) => {
          const name = String(values.name).trim()
          return editing
            ? api.updateChannel(editing.id, { name, active: Boolean(values.active) })
            : api.createChannel({ name })
        }}
        remove={({ row, audit }) => api.deleteChannel(row.id, audit)}
        deleteTitle={(row) => `删除渠道「${row.name}」？`}
        deleteDescription="只有没被资产项使用时才能删除；仍在用的请改用「停用」。"
        invalidateKeys={['channels', 'assetsCurrent', 'assetsList', 'assetItems']}
        activeToggle={{
          isActive: (row) => row.active,
          setActive: (row, active) => api.updateChannel(row.id, { active }),
        }}
      />
    </div>
  )
}