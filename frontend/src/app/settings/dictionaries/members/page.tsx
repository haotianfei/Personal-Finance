'use client'

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/layout/PageHeader'
import type { DataTableColumn } from '@/components/ui'
import { DictionaryCrudCard } from '@/components/settings/DictionaryCrudCard'
import type { DictionaryFieldSpec } from '@/components/settings/DictionaryCrudCard'
import { MEMBER_ROLE_LABEL, MEMBER_ROLE_OPTIONS } from '@/components/classification'
import type { Member, MemberRole } from '@/types'

/**
 * 家庭成员（ADR-0019 D6）：归属维度的唯一值域。
 *
 * 这里存的是**角色显示名**，不是真人姓名（开源仓库不落真实身份）。
 * 「家庭池」已退役——共同持有用 `role='joint'` 的成员表达。
 */

const fields: DictionaryFieldSpec[] = [
  { key: 'name', label: '显示名', type: 'text', required: true, placeholder: '如：本人 / 配偶 / 共同账户', maxLength: 20 },
  {
    key: 'role',
    label: '角色',
    type: 'select',
    required: true,
    options: MEMBER_ROLE_OPTIONS,
    hint: '角色决定徽标与统计口径：共有归属请用「共有」，不要新建多人',
  },
  { key: 'active', label: '启用中', type: 'toggle', hideOnCreate: true, hint: '停用后不再出现在归属选择里' },
]

export default function MembersSettingsPage() {
  const membersQuery = useQuery({ queryKey: ['members'], queryFn: api.getMembers })
  const rows: Member[] = useMemo(() => membersQuery.data?.items ?? [], [membersQuery.data])

  const columns: DataTableColumn<Member>[] = [
    {
      key: 'name', header: '成员', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'role', header: '角色', widthTier: 'text-short',
      cell: (row) => (
        <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">
          {MEMBER_ROLE_LABEL[row.role] ?? row.role}
        </span>
      ),
      mobile: { role: 'meta', label: '角色' },
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
        title="成员"
        description="归属维度的取值：每笔资产都要落到一个成员或一个共有主体上。"
      />

      <DictionaryCrudCard<Member>
        title="成员"
        description="显示名 + 角色；不存放真实姓名以外的身份信息。"
        noun="成员"
        addLabel="新建成员"
        rows={rows}
        loading={membersQuery.isLoading}
        error={membersQuery.error}
        onRetry={() => { void membersQuery.refetch() }}
        columns={columns}
        rowKey={(row) => row.id}
        nameOf={(row) => row.name}
        extraSearchText={(row) => MEMBER_ROLE_LABEL[row.role] ?? row.role}
        emptyTitle="暂无成员"
        emptyDescription="至少要有一个成员（通常是「本人」），否则资产项无法补齐归属。"
        fields={fields}
        valuesOf={(row) => ({ name: row.name, role: row.role, active: row.active })}
        blankValues={{ name: '', role: 'self', active: true }}
        submit={(values, editing) => {
          const name = String(values.name).trim()
          const role = values.role as MemberRole
          return editing
            ? api.updateMember(editing.id, { name, role, active: Boolean(values.active) })
            : api.createMember({ name, role })
        }}
        remove={({ row, audit }) => api.deleteMember(row.id, audit)}
        deleteTitle={(row) => `删除成员「${row.name}」？`}
        deleteDescription="只有没被任何资产项或归属历史引用时才能删除；仍在用的请改用「停用」。"
        invalidateKeys={['members', 'assetsCurrent', 'assetsList', 'assetItems', 'itemOwners']}
        activeToggle={{
          isActive: (row) => row.active,
          setActive: (row, active) => api.updateMember(row.id, { active }),
        }}
        notice="共同持有的资产请用一个 role=「共有」的成员表示，而不是把同一笔资产记给两个人。"
      />
    </div>
  )
}