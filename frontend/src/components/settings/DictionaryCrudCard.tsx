'use client'

import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { message } from 'antd'
import { Pencil, Plus, Power, Search, Trash2, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { DataTable, ConfirmDialog, InlineError, ResponsiveDialog } from '@/components/ui'
import type { DataTableColumn } from '@/components/ui'
import { FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import { dictionaryErrorText } from '@/components/classification'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'

/**
 * 字典页的统一实现（机构 / 渠道 / 成员 / 标签共用）。
 *
 * 为什么合并成一个通用卡：四张表都是「名称 + 一两个枚举/开关」的形状，
 * 各写一遍会出现四种不同的空态、四种不同的删除失败说法。这里统一：
 * - 删除被拦（DICT_IN_USE）用**行内**说明代替 toast —— 提示要能停留、能读完；
 * - 停用/启用放在行操作里，因为删除被拦时「改为停用」正是下一步动作。
 */

export type DictionaryFieldSpec =
  | {
    key: string
    label: string
    type: 'text'
    required?: boolean
    placeholder?: string
    hint?: React.ReactNode
    maxLength?: number
  }
  | {
    key: string
    label: string
    type: 'select'
    required?: boolean
    options: Array<{ value: string; label: string }>
    hint?: React.ReactNode
  }
  | {
    key: string
    label: string
    type: 'toggle'
    hint?: React.ReactNode
    /** 新建时隐藏（后端创建端点不接受该字段时，别让用户以为设置了）。 */
    hideOnCreate?: boolean
  }

export interface DictionaryCrudCardProps<T> {
  title: string
  description?: string
  addLabel: string
  /** 单数名词，用于「编辑机构」「这个机构还…」这类句子。 */
  noun: string
  rows: T[] | undefined
  loading: boolean
  error: unknown
  onRetry: () => void
  columns: DataTableColumn<T>[]
  rowKey: (row: T) => string | number
  /** 排序与搜索用的显示名。 */
  nameOf: (row: T) => string
  extraSearchText?: (row: T) => string
  emptyTitle: string
  emptyDescription: string
  fields: DictionaryFieldSpec[]
  /** 某行的表单初值（编辑时）。 */
  valuesOf: (row: T) => Record<string, string | boolean>
  /** 新建时的表单初值。 */
  blankValues: Record<string, string | boolean>
  submit: (values: Record<string, string | boolean>, editing: T | null) => Promise<unknown>
  /** 删除一行。`audit` 是这次删除的理由（删掉的字典项算「改判」，缺理由后端 422）；
   *  页面把它原样透传给自己的 delete API 即可。
   *
   *  入参刻意收成**单一对象**：TS 允许「参数更少的函数」赋值给「参数更多的函数类型」，
   *  写成 `(row, audit?)` 时页面少接一个参数照样过编译，理由就会被静默吞掉、必 422。
   *  对象解构是精确匹配，少给一个键当场报错。 */
  remove: (args: { row: T; audit: AuditReasonInput }) => Promise<unknown>
  deleteTitle: (row: T) => string
  deleteDescription: string
  /** 删除后刷新哪些 queryKey（含 rel)，由页面给）。 */
  invalidateKeys: string[]
  /** 提供后行操作里出现「停用 / 启用」。 */
  activeToggle?: {
    isActive: (row: T) => boolean
    setActive: (row: T, active: boolean) => Promise<unknown>
  }
  /** 页面顶部的补充说明（如只读提示）。 */
  notice?: React.ReactNode
}

export function DictionaryCrudCard<T>({
  title,
  description,
  addLabel,
  noun,
  rows,
  loading,
  error,
  onRetry,
  columns,
  rowKey,
  nameOf,
  extraSearchText,
  emptyTitle,
  emptyDescription,
  fields,
  valuesOf,
  blankValues,
  submit,
  remove,
  deleteTitle,
  deleteDescription,
  invalidateKeys,
  activeToggle,
  notice,
}: DictionaryCrudCardProps<T>) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<T | null>(null)
  const [deleting, setDeleting] = useState<T | null>(null)
  const [values, setValues] = useState<Record<string, string | boolean>>(blankValues)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [actionError, setActionError] = useState<string | null>(null)
  /** 删除框内的提示（与卡片顶部 actionError 分开：这句要贴着理由输入，弹窗不关）。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const firstFieldRef = useRef<HTMLInputElement>(null)

  // 理由草稿按行保留：Esc / 取消关窗不清，换一行才清（与 records 页同一约定）
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? rowKey(deleting) : null)

  const refresh = () => {
    for (const key of invalidateKeys) void queryClient.invalidateQueries({ queryKey: [key] })
  }

  const visibleRows = (() => {
    const keyword = query.trim().toLowerCase()
    const list = rows ?? []
    if (!keyword) return list
    return list.filter((row) =>
      `${nameOf(row)} ${extraSearchText?.(row) ?? ''}`.toLowerCase().includes(keyword))
  })()

  const openCreate = () => {
    setEditing(null)
    setValues(blankValues)
    setFieldErrors({})
    setActionError(null)
    setFormOpen(true)
  }

  const openEdit = (row: T) => {
    setEditing(row)
    setValues(valuesOf(row))
    setFieldErrors({})
    setActionError(null)
    setFormOpen(true)
  }

  const saveMutation = useMutation({
    mutationFn: () => submit(values, editing),
    onSuccess: () => {
      refresh()
      setFormOpen(false)
      message.success(editing ? '已保存' : '已新建')
    },
    onError: (saveError) => setActionError(dictionaryErrorText(saveError, noun)),
  })

  const deleteMutation = useMutation({
    mutationFn: ({ row, audit }: { row: T; audit: AuditReasonInput }) => remove({ row, audit }),
    onSuccess: () => {
      refresh()
      setDeleting(null)
      setDeleteError(null)
      setActionError(null)
      message.success('已删除')
    },
    onError: (error) => {
      // 缺理由是「还没说为什么」，不是「删不掉」：弹窗与已选理由都留着，补一句再点一次。
      // 其它失败（如 DICT_IN_USE）沿用原样：关窗 + 卡片行内给出可执行的下一步。
      if (isAuditReasonRequired(error)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      setDeleting(null)
      setDeleteError(null)
      setActionError(dictionaryErrorText(error, noun))
    },
  })

  const toggleMutation = useMutation({
    mutationFn: (vars: { row: T; active: boolean }) => activeToggle!.setActive(vars.row, vars.active),
    onSuccess: () => {
      refresh()
      setActionError(null)
    },
    onError: (toggleError) => setActionError(dictionaryErrorText(toggleError, noun)),
  })

  const submitForm = () => {
    const nextErrors: Record<string, string> = {}
    for (const field of fields) {
      if (field.type === 'toggle') continue
      if (field.required && !String(values[field.key] ?? '').trim()) {
        nextErrors[field.key] = `请填写${field.label}`
      }
    }
    setFieldErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return
    setActionError(null)
    saveMutation.mutate()
  }

  return (
    <section className="overflow-hidden rounded-card border border-line bg-surface shadow-card" aria-label={title}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3 tablet:px-5">
        <div className="min-w-0">
          <h2 className="text-h3 font-semibold text-ink">{title}</h2>
          {description && <p className="mt-0.5 text-meta text-muted">{description}</p>}
        </div>
        <button
          type="button"
          onClick={openCreate}
          className="inline-flex min-h-[40px] shrink-0 items-center gap-1.5 rounded-control bg-action-primary px-3 text-meta font-medium text-white"
        >
          <Plus size={15} aria-hidden /> {addLabel}
        </button>
      </div>

      {notice && <div className="border-b border-line bg-subtle px-4 py-2 text-meta text-secondary tablet:px-5">{notice}</div>}

      {actionError && (
        <div className="border-b border-line px-4 py-2 tablet:px-5">
          <InlineError
            action={
              <button
                type="button"
                onClick={() => setActionError(null)}
                aria-label="关闭提示"
                className="inline-flex h-6 w-6 items-center justify-center rounded-control text-status-critical-text"
              >
                <X size={14} aria-hidden />
              </button>
            }
          >
            {actionError}
          </InlineError>
        </div>
      )}

      <div className="p-4 tablet:p-5">
        <DataTable
          label={`${title}列表`}
          rows={visibleRows}
          columns={columns}
          rowKey={rowKey}
          loading={loading}
          error={error instanceof Error ? error : null}
          onRetry={onRetry}
          empty={{
            title: query ? '没有匹配项' : emptyTitle,
            description: query ? '换个关键词试试。' : emptyDescription,
            action: (
              <button
                type="button"
                onClick={openCreate}
                className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white"
              >
                {addLabel}
              </button>
            ),
          }}
          toolbar={
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="relative w-full max-w-[280px]">
                <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
                <input
                  type="text"
                  inputMode="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={`搜索${title}`}
                  aria-label={`搜索${title}`}
                  className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
                />
              </div>
              <p className="pf-num text-meta text-muted">共 {(rows ?? []).length} 条</p>
            </div>
          }
          rowActions={(row) => (
            <>
              <button
                type="button"
                onClick={() => openEdit(row)}
                aria-label={`编辑${nameOf(row)}`}
                title="编辑"
                className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"
              >
                <Pencil size={16} aria-hidden />
              </button>
              {activeToggle && (
                <button
                  type="button"
                  onClick={() => toggleMutation.mutate({ row, active: !activeToggle.isActive(row) })}
                  aria-label={`${activeToggle.isActive(row) ? '停用' : '启用'}${nameOf(row)}`}
                  title={activeToggle.isActive(row) ? '停用（保留历史，新建时不再出现）' : '重新启用'}
                  className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"
                >
                  <Power size={16} aria-hidden />
                </button>
              )}
              <button
                type="button"
                onClick={() => setDeleting(row)}
                aria-label={`删除${nameOf(row)}`}
                title="删除"
                className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"
              >
                <Trash2 size={16} aria-hidden />
              </button>
            </>
          )}
          rowActionsWidth={activeToggle ? 132 : 96}
        />
      </div>

      {formOpen && (
        <ResponsiveDialog
          open
          onClose={() => setFormOpen(false)}
          title={editing ? `编辑${noun}` : addLabel}
          size="sm"
          initialFocusRef={firstFieldRef}
          footer={
            <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
              <button
                type="button"
                onClick={() => setFormOpen(false)}
                disabled={saveMutation.isPending}
                className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitForm}
                disabled={saveMutation.isPending}
                className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
              >
                {saveMutation.isPending ? '保存中…' : editing ? '保存修改' : '新建'}
              </button>
            </div>
          }
        >
          <div className="space-y-4">
            {actionError && <InlineError>{actionError}</InlineError>}
            {fields.map((field, index) => {
              if (field.type === 'toggle') {
                if (field.hideOnCreate && !editing) return null
                return (
                  <label key={field.key} className="flex min-h-[44px] items-center gap-2 text-body text-ink">
                    <input
                      type="checkbox"
                      checked={Boolean(values[field.key])}
                      onChange={(event) => setValues((previous) => ({ ...previous, [field.key]: event.target.checked }))}
                      className="h-4 w-4 accent-[var(--pf-color-action-primary)]"
                    />
                    {field.label}
                    {field.hint && <span className="text-meta text-muted">{field.hint}</span>}
                  </label>
                )
              }
              if (field.type === 'select') {
                return (
                  <FormField key={field.key} label={field.label} required={field.required} hint={field.hint}>
                    <select
                      value={String(values[field.key] ?? '')}
                      onChange={(event) => setValues((previous) => ({ ...previous, [field.key]: event.target.value }))}
                      className={fieldControlClass}
                    >
                      {field.options.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </FormField>
                )
              }
              return (
                <FormField
                  key={field.key}
                  label={field.label}
                  required={field.required}
                  error={fieldErrors[field.key]}
                  hint={field.hint}
                >
                  <input
                    ref={index === 0 ? firstFieldRef : undefined}
                    type="text"
                    value={String(values[field.key] ?? '')}
                    maxLength={field.maxLength}
                    placeholder={field.placeholder}
                    onChange={(event) => {
                      const next = event.target.value
                      setValues((previous) => ({ ...previous, [field.key]: next }))
                      if (fieldErrors[field.key]) {
                        setFieldErrors((previous) => {
                          const copy = { ...previous }
                          delete copy[field.key]
                          return copy
                        })
                      }
                    }}
                    className={cn(fieldControlClass, fieldErrors[field.key] && fieldErrorClass)}
                  />
                </FormField>
              )
            })}
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
          row: deleting,
          audit: toAuditReasonInput(deleteReason),
        })}
        title={deleting ? deleteTitle(deleting) : `删除${noun}？`}
        description={deleteDescription}
        confirmLabel={`删除${noun}`}
        danger
        loading={deleteMutation.isPending}
      >
        <div className="space-y-3">
          {deleteError && <InlineError>{deleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteReason}
            onChange={setDeleteReason}
            hint={`删除会留下谁在何时删了哪一个${noun}。`}
          />
        </div>
      </ConfirmDialog>
    </section>
  )
}