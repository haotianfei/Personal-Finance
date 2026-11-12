'use client'

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { ExpenseCategory } from '@/types'
import { PageHeader } from '@/components/layout/PageHeader'
import type { DataTableColumn } from '@/components/ui'
import { DictionaryCrudCard } from '@/components/settings/DictionaryCrudCard'
import type { DictionaryFieldSpec } from '@/components/settings/DictionaryCrudCard'

/**
 * 支出分类（字典页）：改用字典卡后删除带得上理由。
 *
 * 旧实现走 `DictionaryCrudPage`，那个组件没有理由槽——`actor='owner'` 的删除
 * 缺 reason 一律 422，等于这一页的删除按钮必然失败。
 */

const fields: DictionaryFieldSpec[] = [
  { key: 'name', label: '分类名称', type: 'text', required: true, placeholder: '如：房租房贷', maxLength: 40 },
  { key: 'description', label: '描述（可选）', type: 'text', placeholder: '如：与住房相关的周期性开支', maxLength: 255 },
]

export default function ExpenseCategoriesSettingsPage() {
  const categoriesQuery = useQuery({ queryKey: ['expenseCategories'], queryFn: api.getExpenseCategories })
  const rows: ExpenseCategory[] = useMemo(() => categoriesQuery.data ?? [], [categoriesQuery.data])

  const columns: DataTableColumn<ExpenseCategory>[] = [
    {
      key: 'name', header: '分类名称', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'description', header: '描述', widthTier: 'note', ellipsis: true,
      cell: (row) => (
        <span className="block max-w-[46ch] truncate text-meta text-secondary" title={row.description ?? undefined}>
          {row.description || '—'}
        </span>
      ),
      mobile: { role: 'meta', label: '描述' },
    },
  ]

  return (
    <div className="space-y-6">
      {/* 页面标题只有 h1 一处（PageHeader）。`DictionaryCrudCard` 自带 h2，
          再叫「支出分类」就会把这个名字连着印两遍、两段说明也说同一件事；
          这里按 institutions 页的分工：页头管这一维度本身，卡片管条目表。 */}
      <PageHeader
        title="支出分类"
        description="分类是支出记录与固定支出的共同维度。"
      />

      <DictionaryCrudCard<ExpenseCategory>
        title="分类条目"
        description="改名会同步影响历史记录里的显示。"
        noun="分类"
        addLabel="新增分类"
        rows={rows}
        loading={categoriesQuery.isLoading}
        error={categoriesQuery.error}
        onRetry={() => { void categoriesQuery.refetch() }}
        columns={columns}
        rowKey={(row) => row.id}
        nameOf={(row) => row.name}
        extraSearchText={(row) => row.description ?? ''}
        emptyTitle="暂无支出分类"
        emptyDescription="先新增分类，再录入支出。"
        fields={fields}
        valuesOf={(row) => ({ name: row.name, description: row.description ?? '' })}
        blankValues={{ name: '', description: '' }}
        submit={(values, editing) => {
          const name = String(values.name).trim()
          const description = String(values.description ?? '').trim() || null
          return editing
            ? api.updateExpenseCategory(editing.id, { name, description })
            : api.createExpenseCategory({ name, description })
        }}
        remove={({ row, audit }) => api.deleteExpenseCategory(row.id, audit)}
        deleteTitle={(row) => `删除支出分类「${row.name}」？`}
        deleteDescription="若该分类已被支出记录或固定支出引用，将无法删除。此操作不可恢复。"
        invalidateKeys={['expenseCategories']}
      />
    </div>
  )
}
