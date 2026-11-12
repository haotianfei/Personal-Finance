'use client'

import { useEffect, useState } from 'react'
import type { AssetTagBatchResponse } from '@/types'
import { ResponsiveDialog } from '@/components/ui'
import { cn } from '@/lib/utils'

const FAILURE_LABEL: Record<string, string> = {
  TAG_LIMIT_EXCEEDED: '超过 5 个标签',
  ITEM_ARCHIVED: '记录已归档',
  SUPERSEDED: '记录已被更正',
  NOT_FOUND: '记录不存在',
  VALIDATION_ERROR: '参数校验失败',
}

function failureLabel(code: string): string {
  return FAILURE_LABEL[code] ?? code
}

function TagToggle({ name, active, onClick }: { name: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'inline-flex min-h-[44px] items-center rounded-pill border px-3 text-meta transition-colors',
        active
          ? 'border-brand-300 bg-brand-50 text-brand-700'
          : 'border-line-strong bg-surface text-secondary hover:bg-subtle',
      )}
    >
      {name}
    </button>
  )
}

export function BatchTagDialog({
  open,
  selectedRecordCount,
  tags,
  pending,
  result,
  error,
  recordLabelById,
  onSubmit,
  onClose,
}: {
  open: boolean
  selectedRecordCount: number
  tags: Array<{ id: number; name: string }>
  pending: boolean
  result: AssetTagBatchResponse | null
  error: string | null
  recordLabelById: Map<number, string>
  onSubmit: (data: { add_tag_ids: number[]; remove_tag_ids: number[] }) => void
  onClose: () => void
}) {
  const [addIds, setAddIds] = useState<number[]>([])
  const [removeIds, setRemoveIds] = useState<number[]>([])
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setAddIds([])
    setRemoveIds([])
    setFormError(null)
  }, [open])

  const toggle = (id: number, group: 'add' | 'remove') => {
    setFormError(null)
    if (group === 'add') {
      setAddIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
      setRemoveIds((prev) => prev.filter((x) => x !== id))
    } else {
      setRemoveIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
      setAddIds((prev) => prev.filter((x) => x !== id))
    }
  }

  const submit = () => {
    if (addIds.length === 0 && removeIds.length === 0) {
      setFormError('请至少选择一个要加入或移除的标签')
      return
    }
    setFormError(null)
    onSubmit({ add_tag_ids: addIds, remove_tag_ids: removeIds })
  }

  const hasFailure = (result?.failed.length ?? 0) > 0
  const footer = result ? (
    <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
      <button type="button" onClick={onClose} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink">关闭</button>
      {hasFailure && <button type="button" onClick={submit} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{pending ? '重试中…' : `重试失败项（${result.failed.length}）`}</button>}
    </div>
  ) : (
    <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
      <button type="button" onClick={onClose} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
      <button type="button" onClick={submit} disabled={pending || tags.length === 0} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{pending ? '提交中…' : '提交标签变更'}</button>
    </div>
  )

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="批量打标签"
      description="对当前页选中的记录加入或移除标签；每条记录最多 5 个。"
      size="md"
      footer={footer}
    >
      <div className="space-y-4">
        {error && <p role="alert" className="rounded-control border border-status-critical/25 bg-status-critical-surface p-3 text-meta text-status-critical-text">{error}</p>}
        {result ? (
          <div className="space-y-3">
            <div className="rounded-control border border-line bg-subtle p-3 text-body text-secondary">
              <p className="font-medium text-ink">批量打标结果</p>
              <p className="mt-1 text-meta">已更新 <strong className="pf-num text-status-good-text">{result.updated}</strong> · 跳过 <strong className="pf-num text-status-warn-text">{result.skipped}</strong> · 失败 <strong className="pf-num text-status-critical-text">{result.failed.length}</strong></p>
            </div>
            {hasFailure && (
              <div className="space-y-1">
                <p className="text-meta font-medium text-secondary">失败项</p>
                <ul className="divide-y divide-line rounded-control border border-line">
                  {result.failed.map((item) => (
                    <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-meta">
                      <span className="text-secondary">{recordLabelById.get(item.id) ?? `记录 #${item.id}`}</span>
                      <span className="text-status-critical-text">{failureLabel(item.code)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {!hasFailure && <p className="text-meta text-status-good-text">本批全部处理完成。</p>}
            {hasFailure && <p className="text-meta text-muted">失败记录已保留在当前选择中，可直接重试。</p>}
          </div>
        ) : (
          <>
            <p className="rounded-control bg-subtle px-3 py-2 text-meta text-secondary">
              已选 <strong className="pf-num text-ink">{selectedRecordCount}</strong> 条记录；每条记录最多 5 个标签。
            </p>
            {formError && <p role="alert" className="text-meta text-status-critical-text">{formError}</p>}
            {tags.length === 0 ? (
              <p className="rounded-control border border-dashed border-line bg-subtle p-3 text-meta text-muted">
                还没有标签。请先在「设置 → 属性」创建标签。
              </p>
            ) : (
              <div className="space-y-4">
                <div role="group" aria-label="加入标签">
                  <p className="mb-2 text-meta font-medium text-secondary">加入标签</p>
                  <div className="flex flex-wrap gap-2">{tags.map((tag) => <TagToggle key={`add-${tag.id}`} name={tag.name} active={addIds.includes(tag.id)} onClick={() => toggle(tag.id, 'add')} />)}</div>
                </div>
                <div role="group" aria-label="移除标签">
                  <p className="mb-2 text-meta font-medium text-secondary">移除标签</p>
                  <div className="flex flex-wrap gap-2">{tags.map((tag) => <TagToggle key={`remove-${tag.id}`} name={tag.name} active={removeIds.includes(tag.id)} onClick={() => toggle(tag.id, 'remove')} />)}</div>
                </div>
                <p className="text-meta text-muted">同一标签不能同时加入和移除；超限、归档或缺失记录会进入失败清单。</p>
              </div>
            )}
          </>
        )}
      </div>
    </ResponsiveDialog>
  )
}