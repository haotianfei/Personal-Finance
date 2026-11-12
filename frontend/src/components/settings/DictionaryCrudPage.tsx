'use client'

import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { message } from 'antd'
import { Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, DataTable, InlineError, ResponsiveDialog } from '@/components/ui'
import type { DataTableColumn, DataTableSort } from '@/components/ui'
import { FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'

interface DictionaryRow {
  id: number
  name: string
  description?: string | null
}

export interface DictionaryCrudPageProps<T extends DictionaryRow> {
  title: string
  description: string
  addLabel: string
  nameLabel: string
  namePlaceholder: string
  descriptionLabel?: string
  descriptionPlaceholder?: string
  queryKey: string
  queryFn: () => Promise<T[]>
  createFn: (data: { name: string; description?: string | null }) => Promise<unknown>
  updateFn: (id: number, data: { name?: string; description?: string | null }) => Promise<unknown>
  /** 删除一行。入参刻意收成**单一对象**（原因见 `DictionaryCrudCard.remove`）：
   *  写成 `(id, audit?)` 时页面少接一个参数照样过编译，理由会被静默吞掉、必 422。 */
  deleteFn: (args: { id: number; audit: AuditReasonInput }) => Promise<unknown>
  deleteTitle: (row: T) => string
  deleteDescription: string
  emptyDescription: string
  onChanged?: () => void
}

/** 名称（+可选描述）型字典页的统一实现：DataTable + ResponsiveDialog + FormField。 */
export function DictionaryCrudPage<T extends DictionaryRow>({
  title,
  description,
  addLabel,
  nameLabel,
  namePlaceholder,
  descriptionLabel,
  descriptionPlaceholder,
  queryKey,
  queryFn,
  createFn,
  updateFn,
  deleteFn,
  deleteTitle,
  deleteDescription,
  emptyDescription,
  onChanged,
}: DictionaryCrudPageProps<T>) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<DataTableSort | null>({ key: 'name', direction: 'asc' })
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<T | null>(null)
  const [deleting, setDeleting] = useState<T | null>(null)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [error, setError] = useState<string | undefined>()
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  // 理由草稿按行保留：关窗不清、换一行才清（与字典卡 / records 页同一约定）
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? deleting.id : null)

  const listQuery = useQuery({ queryKey: [queryKey], queryFn })
  const rows = useMemo(() => listQuery.data ?? [], [listQuery.data])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: [queryKey] })
    onChanged?.()
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      editing
        ? updateFn(editing.id, { name: name.trim(), ...(descriptionLabel ? { description: desc.trim() || null } : {}) })
        : createFn({ name: name.trim(), ...(descriptionLabel ? { description: desc.trim() || null } : {}) }),
    onSuccess: () => {
      refresh()
      message.success(editing ? '已保存' : '已创建')
      setFormOpen(false)
    },
    onError: (e: Error) => setError(e.message || '保存失败，请重试。'),
  })

  const deleteMutation = useMutation({
    mutationFn: (args: { id: number; audit: AuditReasonInput }) => deleteFn(args),
    onSuccess: () => {
      refresh()
      message.success('已删除')
      setDeleting(null)
      setDeleteError(null)
    },
    onError: (e: Error) => {
      // 缺理由是「还没说为什么」，不是「删不掉」：弹窗与已选理由都留着，补一句再点一次。
      if (isAuditReasonRequired(e)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(e.message || '删除失败，可能仍被使用。')
    },
  })

  const visibleRows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = q
      ? rows.filter((row) => `${row.name} ${row.description ?? ''}`.toLowerCase().includes(q))
      : rows
    if (!sort) return filtered
    return [...filtered].sort((a, b) => {
      const result = a.name.localeCompare(b.name, 'zh-CN')
      return sort.direction === 'asc' ? result : -result
    })
  }, [rows, query, sort])

  const openCreate = () => {
    setEditing(null)
    setName('')
    setDesc('')
    setError(undefined)
    setFormOpen(true)
  }

  const openEdit = (row: T) => {
    setEditing(row)
    setName(row.name)
    setDesc(row.description ?? '')
    setError(undefined)
    setFormOpen(true)
  }

  const submit = () => {
    if (!name.trim()) {
      setError(`请输入${nameLabel}`)
      return
    }
    setError(undefined)
    saveMutation.mutate()
  }

  const columns: DataTableColumn<T>[] = [
    {
      key: 'name', header: nameLabel, sortable: true, widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    ...(descriptionLabel ? [{
      key: 'description', header: descriptionLabel, widthTier: 'note' as const,
      cell: (row: T) => <span className="block max-w-[46ch] truncate" title={row.description ?? undefined}>{row.description || '—'}</span>,
      mobile: { role: 'meta' as const, label: descriptionLabel },
    }] : []),
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title={title}
        description={description}
        actions={
          <button type="button" onClick={openCreate} className="inline-flex min-h-[44px] items-center gap-2 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
            <Plus size={16} aria-hidden /> {addLabel}
          </button>
        }
      />

      <DataTable
        label={`${title}列表`}
        rows={visibleRows}
        columns={columns}
        rowKey={(row) => row.id}
        loading={listQuery.isLoading}
        error={listQuery.error}
        onRetry={() => listQuery.refetch()}
        sort={sort}
        onSortChange={setSort}
        empty={{
          title: query ? '没有匹配项' : `暂无${title}`,
          description: query ? '换个关键词试试。' : emptyDescription,
          action: (
            <button type="button" onClick={openCreate} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">
              {addLabel}
            </button>
          ),
        }}
        toolbar={
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="relative w-full max-w-[320px]">
              <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
              <input
                type="text"
                inputMode="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={`搜索${title}`}
                aria-label={`搜索${title}`}
                className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
              />
            </div>
            <p className="pf-num text-meta text-muted">共 {rows.length} 条</p>
          </div>
        }
        rowActions={(row) => (
          <>
            <button type="button" onClick={() => openEdit(row)} aria-label={`编辑${row.name}`} title="编辑" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted">
              <Pencil size={16} aria-hidden />
            </button>
            <button type="button" onClick={() => setDeleting(row)} aria-label={`删除${row.name}`} title="删除" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text">
              <Trash2 size={16} aria-hidden />
            </button>
          </>
        )}
      />

      {formOpen && (
        <ResponsiveDialog
          open
          onClose={() => setFormOpen(false)}
          title={editing ? `编辑${title}` : `新增${title}`}
          size="sm"
          initialFocusRef={nameRef}
          footer={
            <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
              <button type="button" onClick={() => setFormOpen(false)} disabled={saveMutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
              <button type="button" onClick={submit} disabled={saveMutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
                {saveMutation.isPending ? '保存中…' : editing ? '保存修改' : '创建'}
              </button>
            </div>
          }
        >
          <div className="space-y-4">
            <FormField label={nameLabel} required error={error}>
              <input
                ref={nameRef}
                type="text"
                value={name}
                onChange={(e) => { setName(e.target.value); if (error) setError(undefined) }}
                placeholder={namePlaceholder}
                className={cn(fieldControlClass, error && fieldErrorClass)}
              />
            </FormField>
            {descriptionLabel && (
              <FormField label={descriptionLabel}>
                <textarea
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  placeholder={descriptionPlaceholder}
                  rows={3}
                  className={cn(fieldControlClass, 'min-h-[96px] resize-none py-2')}
                />
              </FormField>
            )}
          </div>
        </ResponsiveDialog>
      )}

      <ConfirmDialog
        open={!!deleting}
        onClose={() => {
          setDeleting(null)
          setDeleteError(null)
        }}
        onConfirm={() => deleting && deleteMutation.mutate({
          id: deleting.id,
          audit: toAuditReasonInput(deleteReason),
        })}
        title={deleting ? deleteTitle(deleting) : `删除${title}？`}
        description={deleteDescription}
        confirmLabel={`删除${title}`}
        danger
        loading={deleteMutation.isPending}
      >
        <div className="space-y-3">
          {deleteError && <InlineError>{deleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteReason}
            onChange={setDeleteReason}
            hint="删除会留下谁在何时删了哪一个。"
          />
        </div>
      </ConfirmDialog>
    </div>
  )
}