'use client'

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/layout/PageHeader'
import type { DataTableColumn } from '@/components/ui'
import { DictionaryCrudCard } from '@/components/settings/DictionaryCrudCard'
import type { DictionaryFieldSpec } from '@/components/settings/DictionaryCrudCard'
import type { TagItem } from '@/types'

/**
 * 标签（ADR-0019 §6）：横切属性，多值，单个资产项最多 5 个。
 *
 * 分两档：`controlled` 由规范维护（常用词，录入时随手可选）；
 * `free` 的**创建**有两个显式入口——本页，和资产项抽屉里的标签选择器
 * （「+ 新建标签「X」」，spec `.scratch/tag-create-in-sheet/spec.md`）。
 * 两个入口都必须由用户点/回车触发：录入时**不因输入静默新建**，
 * 否则标签会退化成自由文本（该 spec D8）。改名/删除仍只在本页。
 */

const TAG_KIND_LABEL: Record<TagItem['kind'], string> = {
  controlled: '常用',
  free: '自定义',
}

const fields: DictionaryFieldSpec[] = [
  { key: 'name', label: '标签名', type: 'text', required: true, placeholder: '如：境外 / 退休专用', maxLength: 20 },
  {
    key: 'kind',
    label: '类型',
    type: 'select',
    required: true,
    options: [
      { value: 'controlled', label: '常用（规范维护的固定词）' },
      { value: 'free', label: '自定义（自己起的词）' },
    ],
    hint: '两类都能挂资产项，区别只是来源与排序顺序',
  },
]

export default function TagsSettingsPage() {
  const tagsQuery = useQuery({ queryKey: ['tags'], queryFn: api.getTags })
  const rows: TagItem[] = useMemo(() => tagsQuery.data?.items ?? [], [tagsQuery.data])

  const columns: DataTableColumn<TagItem>[] = [
    {
      key: 'name', header: '标签', widthTier: 'text-mid',
      cell: (row) => (
        <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">{row.name}</span>
      ),
      mobile: { role: 'title' },
    },
    {
      key: 'kind', header: '类型', widthTier: 'text-short',
      cell: (row) => <span className="text-meta text-secondary">{TAG_KIND_LABEL[row.kind] ?? row.kind}</span>,
      mobile: { role: 'meta', label: '类型' },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="标签"
        description="跨类别的标记（境外、退休专用、已质押…）：一个资产项最多 5 个。"
      />

      <DictionaryCrudCard<TagItem>
        title="标签"
        description="改标签名会同时影响所有已挂该标签的资产项。"
        noun="标签"
        addLabel="新建标签"
        rows={rows}
        loading={tagsQuery.isLoading}
        error={tagsQuery.error}
        onRetry={() => { void tagsQuery.refetch() }}
        columns={columns}
        rowKey={(row) => row.id}
        nameOf={(row) => row.name}
        extraSearchText={(row) => TAG_KIND_LABEL[row.kind] ?? row.kind}
        emptyTitle="暂无标签"
        emptyDescription="标签是可选的；需要标记横切属性时再建。"
        fields={fields}
        valuesOf={(row) => ({ name: row.name, kind: row.kind })}
        blankValues={{ name: '', kind: 'free' }}
        submit={(values, editing) => {
          const name = String(values.name).trim()
          const kind = values.kind as TagItem['kind']
          return editing
            ? api.updateTag(editing.id, { name, kind })
            : api.createTag({ name, kind })
        }}
        remove={({ row, audit }) => api.deleteTag(row.id, audit)}
        deleteTitle={(row) => `删除标签「${row.name}」？`}
        deleteDescription="只有没被资产项使用时才能删除；删掉后该项的标签会一起消失。"
        invalidateKeys={['tags', 'assetsCurrent', 'assetsList', 'assetItems']}
        notice="区域、用途这类信息用标签表达，不要为此新建资产类别——否则类别树会按地区再复制一遍。"
      />
    </div>
  )
}