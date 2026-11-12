'use client'

import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown, ChevronRight, FolderTree, Pencil, Plus, Search, Trash2, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { cn } from '@/lib/utils'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, InlineError, LoadingState, ResponsiveDialog, ErrorState, EmptyState } from '@/components/ui'
import { FormField, fieldControlClass, fieldErrorClass } from '@/components/form'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import {
  assetClassErrorText,
  buildClassTree,
  descendantIds,
  sideScopeText,
} from '@/components/classification'
import type { AssetClass } from '@/types'
import type { AssetClassNode as ClassNode } from '@/components/classification'

/**
 * 资产类别树（ADR-0019 §4）：8 大类 + 叶子，`code` 由后端按层级自动编号。
 *
 * 这一页必须处理的三个硬约束：
 * 1. **只有叶子能挂资产项** —— 中间层只用于分组，所以删除/新建都要围绕「层级」说人话；
 * 2. **改动不能成环** —— 选上级时把自身与全部后代从候选里剔除，不靠后端 400 兜底；
 * 3. **被引用不能删** —— 被资产项用着时给「改名」这条出路，而不是只说不能删。
 */

interface ClassFormValues {
  name: string
  parentId: string
  sideScope: string
  /** 只在新建时可填（留空＝后端按层级自动编号）。 */
  code: string
}

const SIDE_SCOPE_OPTIONS_EDIT = [
  { value: 'asset', label: '资产' },
  { value: 'liability', label: '负债' },
  { value: 'any', label: '通用（不限方向）' },
]

export default function AssetClassesSettingsPage() {
  const queryClient = useQueryClient()
  const classesQuery = useQuery({ queryKey: ['assetClasses'], queryFn: api.getAssetClasses })
  const rows: AssetClass[] = useMemo(() => classesQuery.data?.items ?? [], [classesQuery.data])

  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<number> | null>(null)
  const [editing, setEditing] = useState<AssetClass | null>(null)
  const [creatingParent, setCreatingParent] = useState<AssetClass | null | undefined>(undefined)
  const [deleting, setDeleting] = useState<AssetClass | null>(null)
  const [values, setValues] = useState<ClassFormValues>({ name: '', parentId: '', sideScope: '', code: '' })
  const [nameError, setNameError] = useState<string | undefined>()
  const [actionError, setActionError] = useState<string | null>(null)
  /** 删除框内的提示：缺理由时贴在这里，弹窗不关。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  // 理由草稿按类别保留：关窗不清、换一个类别才清
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? deleting.id : null)

  const formOpen = editing !== null || creatingParent !== undefined
  const leafCount = rows.filter((row) => row.leaf).length

  const byId = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows])

  const keyword = query.trim().toLowerCase()
  const filtered = useMemo(() => {
    if (!keyword) return rows
    return rows.filter((row) => `${row.path} ${row.code} ${row.name}`.toLowerCase().includes(keyword))
  }, [keyword, rows])

  /**
   * 搜索时不按原树折叠：命中项连同其祖先一起显示，
   * 否则搜到叶子却因为父级收起而看不到。
   */
  const tree = useMemo(() => {
    if (!keyword) return buildClassTree(rows)
    const keep = new Set<number>()
    for (const row of filtered) {
      keep.add(row.id)
      let cursor: AssetClass | undefined = row
      while (cursor?.parent_id != null) {
        keep.add(cursor.parent_id)
        cursor = byId.get(cursor.parent_id)
      }
    }
    return buildClassTree(rows.filter((row) => keep.has(row.id)))
  }, [byId, filtered, keyword, rows])

  const allOpen = expanded === null || expanded.size >= rows.length

  const isOpen = (node: AssetClass) => allOpen || (expanded?.has(node.id) ?? false)

  const toggle = (node: AssetClass) => {
    setExpanded((previous) => {
      const next = previous === null ? new Set(rows.map((row) => row.id)) : new Set(previous)
      if (next.has(node.id)) next.delete(node.id)
      else next.add(node.id)
      return next
    })
  }

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['assetClasses'] })
    void queryClient.invalidateQueries({ queryKey: ['assetsCurrent'] })
    void queryClient.invalidateQueries({ queryKey: ['assetsList'] })
    void queryClient.invalidateQueries({ queryKey: ['assetItems'] })
  }

  const openCreateRoot = () => {
    setEditing(null)
    setCreatingParent(null)
    setValues({ name: '', parentId: '', sideScope: '', code: '' })
    setNameError(undefined)
    setActionError(null)
  }

  const openCreateChild = (parent: AssetClass) => {
    setEditing(null)
    setCreatingParent(parent)
    setValues({ name: '', parentId: String(parent.id), sideScope: '', code: '' })
    setNameError(undefined)
    setActionError(null)
  }

  const openEdit = (node: AssetClass) => {
    setEditing(node)
    setCreatingParent(undefined)
    setValues({
      name: node.name,
      parentId: node.parent_id == null ? '' : String(node.parent_id),
      sideScope: node.side_scope,
      code: node.code,
    })
    setNameError(undefined)
    setActionError(null)
  }

  const closeForm = () => {
    setEditing(null)
    setCreatingParent(undefined)
    setActionError(null)
  }

  /** 编辑时不能把自己挂到自身或后代下（后端会 400，这里先不给选项）。 */
  const parentOptions = useMemo(() => {
    const blocked = editing ? new Set(descendantIds(rows, editing.id)) : new Set<number>()
    return [
      { value: '', label: '顶层（成为一个大类）' },
      ...rows
        .filter((row) => !blocked.has(row.id))
        .map((row) => ({ value: String(row.id), label: row.path })),
    ]
  }, [editing, rows])

  const saveMutation = useMutation({
    mutationFn: () => {
      const name = values.name.trim()
      const parentId = values.parentId ? Number(values.parentId) : null
      if (editing) {
        return api.updateAssetClass(editing.id, {
          name,
          parent_id: parentId,
          ...(values.sideScope ? { side_scope: values.sideScope as AssetClass['side_scope'] } : {}),
        })
      }
      return api.createAssetClass({
        name,
        parent_id: parentId,
        // 留空＝后端自动编号；只有「自动编号撞车」时才需要手填
        ...(values.code.trim() ? { code: values.code.trim() } : {}),
        ...(values.sideScope ? { side_scope: values.sideScope as AssetClass['side_scope'] } : {}),
      })
    },
    onSuccess: () => {
      refresh()
      closeForm()
    },
    onError: (saveError) => setActionError(assetClassErrorText(saveError, editing ? 'update' : 'create')),
  })

  const deleteMutation = useMutation({
    mutationFn: ({ node, audit }: { node: AssetClass; audit?: AuditReasonInput }) =>
      api.deleteAssetClass(node.id, audit),
    onSuccess: () => {
      refresh()
      setDeleting(null)
      setDeleteError(null)
      setActionError(null)
    },
    onError: (error) => {
      // 缺理由：把人留在弹窗里补一句，已选的理由不丢。
      if (isAuditReasonRequired(error)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      setDeleting(null)
      setDeleteError(null)
      setActionError(assetClassErrorText(error, 'delete'))
    },
  })

  const submitForm = () => {
    if (!values.name.trim()) {
      setNameError('请填写类别名称')
      return
    }
    setNameError(undefined)
    setActionError(null)
    saveMutation.mutate()
  }

  const renderNode = (entry: ClassNode, depth: number): React.ReactNode => {
    const node = entry.node
    const hasChildren = entry.children.length > 0
    const open = keyword !== '' || isOpen(node)
    return (
      <li key={node.id}>
        <div
          className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 last:border-b-0"
          style={{ paddingLeft: `${12 + depth * 18}px` }}
        >
          <button
            type="button"
            onClick={() => (hasChildren ? toggle(node) : undefined)}
            aria-expanded={hasChildren ? open : undefined}
            aria-label={hasChildren ? `${open ? '收起' : '展开'}「${node.name}」` : undefined}
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-control text-muted',
              hasChildren ? 'hover:bg-subtle' : 'cursor-default',
            )}
          >
            {hasChildren ? (open ? <ChevronDown size={15} aria-hidden /> : <ChevronRight size={15} aria-hidden />) : null}
          </button>

          <code className="shrink-0 rounded bg-subtle px-1.5 py-0.5 text-micro text-secondary">{node.code}</code>
          {hasChildren ? (
            <button
              type="button"
              onClick={() => toggle(node)}
              className="min-w-0 flex-1 text-left"
            >
              <span className="block truncate text-body font-medium text-ink" title={node.path}>{node.name}</span>
              <span className="block truncate text-micro text-muted" title={node.path}>{node.path}</span>
            </button>
          ) : (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body font-medium text-ink" title={node.path}>{node.name}</span>
              <span className="block truncate text-micro text-muted" title={node.path}>{node.path}</span>
            </span>
          )}

          <span
            className={cn(
              'shrink-0 rounded-pill px-2 py-0.5 text-micro',
              node.leaf
                ? 'bg-status-good-surface text-status-good-text'
                : 'bg-subtle text-muted',
            )}
          >
            {node.leaf ? '末级 · 可挂资产项' : `分组 · ${entry.children.length} 个子类`}
          </span>
          <span className="shrink-0 rounded-pill bg-subtle px-2 py-0.5 text-micro text-secondary">
            {sideScopeText(node.side_scope)}
          </span>

          <div className="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              onClick={() => openCreateChild(node)}
              aria-label={`在「${node.name}」下新增子类`}
              title="新增子类"
              className="inline-flex h-9 w-9 items-center justify-center rounded-control text-muted hover:text-ink"
            >
              <Plus size={15} aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => openEdit(node)}
              aria-label={`编辑「${node.name}」`}
              title="改名 / 调整层级"
              className="inline-flex h-9 w-9 items-center justify-center rounded-control text-muted hover:text-ink"
            >
              <Pencil size={15} aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => setDeleting(node)}
              aria-label={`删除「${node.name}」`}
              title="删除"
              className="inline-flex h-9 w-9 items-center justify-center rounded-control text-status-critical-text"
            >
              <Trash2 size={15} aria-hidden />
            </button>
          </div>
        </div>
        {hasChildren && open && (
          <ul>{entry.children.map((child) => renderNode(child, depth + 1))}</ul>
        )}
      </li>
    )
  }

  const body = (() => {
    if (classesQuery.isLoading) return <div className="p-4"><LoadingState rows={6} label="正在加载类别树" /></div>
    if (classesQuery.error) {
      return (
        <div className="p-4">
          <ErrorState
            title="类别树加载失败"
            description="请求没有成功，请重试。"
            onRetry={() => { void classesQuery.refetch() }}
          />
        </div>
      )
    }
    if (rows.length === 0) {
      return (
        <div className="p-4">
          <EmptyState
            title="还没有资产类别"
            description="类别树是分类的骨架：先建大类，再往下建末级分类。"
            action={(
              <button type="button" onClick={openCreateRoot} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">
                新建大类
              </button>
            )}
          />
        </div>
      )
    }
    if (tree.length === 0) {
      return <p className="px-4 py-6 text-meta text-muted">没有匹配的类别，换个关键词试试。</p>
    }
    return <ul>{tree.map((entry) => renderNode(entry, 0))}</ul>
  })()

  return (
    <div className="space-y-6">
      <PageHeader
        title="资产类别"
        description="一棵树 + 适用范围：只有末级类别能挂资产项；中间层只用于分组与筛选。"
        actions={
          <button
            type="button"
            onClick={openCreateRoot}
            className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white"
          >
            <Plus size={16} aria-hidden /> 新建大类
          </button>
        }
      />

      <div className="flex flex-wrap items-center justify-between gap-2 rounded-card border border-line bg-subtle px-4 py-2 text-meta text-secondary">
        <span className="inline-flex items-center gap-1.5">
          <FolderTree size={14} aria-hidden />
          共 <span className="pf-num">{rows.length}</span> 个类别，其中末级 <span className="pf-num">{leafCount}</span> 个
        </span>
        <span className="text-muted">编号（A、B1…）由系统自动生成，不用手填</span>
      </div>

      {actionError && (
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
      )}

      <section className="overflow-hidden rounded-card border border-line bg-surface shadow-card" aria-label="资产类别树">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3 tablet:px-5">
          <div className="relative w-full max-w-[280px]">
            <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
            <input
              type="text"
              inputMode="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索类别名称或编号"
              aria-label="搜索类别名称或编号"
              className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
            />
          </div>
          <button
            type="button"
            onClick={() => setExpanded(allOpen ? new Set<number>() : new Set(rows.map((row) => row.id)))}
            className="min-h-[44px] rounded-control border border-line px-3 text-meta text-secondary"
          >
            {allOpen ? '全部收起' : '全部展开'}
          </button>
        </div>
        {body}
      </section>

      {formOpen && (
        <ResponsiveDialog
          open
          onClose={closeForm}
          title={editing ? `编辑类别「${editing.name}」` : creatingParent ? `在「${creatingParent.name}」下新增子类` : '新增大类'}
          size="sm"
          initialFocusRef={nameRef}
          footer={
            <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
              <button
                type="button"
                onClick={closeForm}
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
            <FormField label="类别名称" required error={nameError} hint="只写这一个层级的名字，不要带上父级">
              <input
                ref={nameRef}
                type="text"
                value={values.name}
                maxLength={20}
                onChange={(event) => {
                  setValues((previous) => ({ ...previous, name: event.target.value }))
                  if (nameError) setNameError(undefined)
                }}
                placeholder="如：货币基金"
                className={cn(fieldControlClass, nameError && fieldErrorClass)}
              />
            </FormField>
            <FormField label="上级类别" hint="选「顶层」就是新的大类；挂到末级下会让原末级变成分组">
              <select
                value={values.parentId}
                onChange={(event) => setValues((previous) => ({ ...previous, parentId: event.target.value }))}
                className={fieldControlClass}
              >
                {parentOptions.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </FormField>
            {!editing && (
              <FormField
                label="编号（可不填）"
                hint="留空就按层级自动编号（如 A、B1）；只有自动编号撞上已删除类别的编号空档时，才需要在这里手填一个"
              >
                <input
                  type="text"
                  value={values.code}
                  maxLength={6}
                  onChange={(event) => setValues((previous) => ({ ...previous, code: event.target.value.toUpperCase() }))}
                  placeholder="如 A8"
                  className={fieldControlClass}
                />
              </FormField>
            )}
            <FormField
              label="适用范围"
              hint={editing ? '改成与已有资产项方向冲突的值会被拒绝' : '留空表示跟随上级；顶层默认「通用」'}
            >
              <select
                value={values.sideScope}
                onChange={(event) => setValues((previous) => ({ ...previous, sideScope: event.target.value }))}
                className={fieldControlClass}
              >
                {!editing && <option value="">跟随上级（默认）</option>}
                {SIDE_SCOPE_OPTIONS_EDIT.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </FormField>
            <p className="rounded-control bg-subtle px-3 py-2 text-meta text-muted">
              编号由系统按层级自动生成（如 A → A1）；改层级会重新编号，历史映射不受影响。
            </p>
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
          node: deleting,
          audit: toAuditReasonInput(deleteReason),
        })}
        title={deleting ? `删除类别「${deleting.name}」？` : '删除类别？'}
        description="只有在没有子类别、也没有资产项使用它时才能删除；被引用时请改名或先把资产项换到别的类别。"
        confirmLabel="删除类别"
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