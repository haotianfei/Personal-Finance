'use client'

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { History, Info, UserRound } from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AssetRecord, ItemOwnerKind } from '@/types'
import { FormField, SelectField, fieldControlClass } from '@/components/form'
import { InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import { isReasonMissing, runConfirmGate } from '@/components/ui/confirmGate'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { cn } from '@/lib/utils'

function ownerError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'ITEM_ARCHIVED') return '该资产项已归档，不能修改归属。'
    if (error.code === 'VALIDATION_ERROR') {
      const fields = (error.details?.fields as Array<{ message?: string }> | undefined) ?? []
      return fields[0]?.message || error.message
    }
    return error.message
  }
  return error instanceof Error ? error.message : '归属保存失败'
}

export function ItemOwnerDialog({
  open, record, onClose, onSaved,
}: {
  open: boolean
  record: AssetRecord | null
  onClose: () => void
  onSaved?: () => void
}) {
  const queryClient = useQueryClient()
  const itemId = record?.asset_item_id ?? null
  const [ownerId, setOwnerId] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  /** 被拦下的次数：+1 让审计理由框显红字并把焦点拿回下拉（本弹窗自画保存按钮）。 */
  const [auditNudge, setAuditNudge] = useState(0)

  // 审计理由草稿按资产项保留：关窗不清、换一项才清（与其它理由入口同一约定）
  const [auditReason, setAuditReason] = useAuditReasonDraft(itemId)

  const ownerQuery = useQuery({
    queryKey: ['itemOwner', itemId],
    queryFn: () => api.getItemOwner(itemId!),
    enabled: open && !!itemId,
  })
  const membersQuery = useQuery({ queryKey: ['members'], queryFn: api.getMembers, enabled: open })
  const current = ownerQuery.data?.current ?? null
  const history = ownerQuery.data?.history ?? []

  useEffect(() => {
    if (!open) return
    setReason('')
    setError(null)
    setNotice(null)
    if (ownerQuery.data?.current?.owner_id) {
      setOwnerId(String(ownerQuery.data.current.owner_id))
    } else if (record?.owner_id) {
      setOwnerId(String(record.owner_id))
    } else {
      setOwnerId('')
    }
  }, [open, ownerQuery.data, record])

  const mutation = useMutation({
    mutationFn: () => api.setItemOwner(itemId!, {
      owner_kind: 'member' as ItemOwnerKind,
      owner_id: ownerId ? Number(ownerId) : null,
      reason: reason.trim() || null,
    }, toAuditReasonInput(auditReason), newIdemKey()),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['assetsList'] })
      queryClient.invalidateQueries({ queryKey: ['itemOwner', itemId] })
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      onSaved?.()
      if (!data.change) {
        setNotice('归属未变化，未追加历史。')
      } else {
        setNotice('归属已按资产项生效。')
      }
      setReason('')
    },
    // 缺审计理由（422）是「还没说为什么」，不是保存失败：弹窗不关，把话贴在行内
    onError: (e) => setError(isAuditReasonRequired(e) ? AUDIT_REASON_REQUIRED_TEXT : ownerError(e)),
  })

  const submit = () => {
    if (!itemId) return
    if (!ownerId) { setError('请选择成员'); return }
    // 改归属是纠正类写入（action='change'）：缺审计理由后端 422，这里先拦——请求不发、弹窗不关
    if (runConfirmGate({
      blocked: () => isReasonMissing(auditReason.reason),
      nudge: () => setAuditNudge((count) => count + 1),
    })) return
    setError(null)
    mutation.mutate()
  }

  // 记录侧的 owner_name 也是项级回显（ADR-0019：归属只在项上）；仅当项级史无记录时用它预填
  const recordFallback = !ownerQuery.isLoading && !current && !!record?.owner_name

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="资产项归属"
      description={record ? record.asset_name : undefined}
      size="md"
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={mutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={mutation.isPending || ownerQuery.isLoading} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {mutation.isPending ? '保存中…' : '保存归属'}
          </button>
        </div>
      }
    >
      {!itemId ? (
        <InlineError>这条记录没有关联资产项，无法设置项级归属；请先完成资产项迁移。</InlineError>
      ) : ownerQuery.isLoading ? <LoadingState rows={3} /> : (
        <div className="space-y-5">
          {error && <InlineError>{error}</InlineError>}
          {notice && <div className="rounded-control border border-status-good/20 bg-status-good-surface px-3 py-2 text-meta text-status-good-text">{notice}</div>}

          {recordFallback && (
            <div className="flex gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
              <Info size={15} className="mt-0.5 shrink-0" aria-hidden />
              <p>该资产项尚无归属变更史；当前分类里的归属为「{record?.owner_name}」。保存后会按资产项生效并追加一条历史。</p>
            </div>
          )}

          <div className="rounded-control border border-line bg-subtle p-3">
            <p className="text-meta text-muted">当前项级归属</p>
            <p className="mt-1 inline-flex items-center gap-1.5 text-body-strong text-ink">
              <UserRound size={16} className="text-secondary" aria-hidden />
              {current ? current.owner_name : recordFallback ? `${record?.owner_name}（项级分类）` : '未标注'}
            </p>
            {current?.effective_date && <p className="mt-1 text-meta text-muted">生效 {current.effective_date}{current.reason ? ` · ${current.reason}` : ''}</p>}
          </div>

          <SelectField
            label="归属成员"
            required
            value={ownerId}
            onChange={(value) => { setOwnerId(value); setError(null) }}
            placeholder={membersQuery.isLoading ? '加载成员…' : '选择成员'}
            options={(membersQuery.data?.items ?? []).map((member) => ({
              value: String(member.id),
              label: member.role === 'joint' ? `${member.name}（共有）` : member.name,
            }))}
          />
          <p className="text-meta text-muted">归属按资产项生效（一项多期共用）；「家庭池」已退役，共有归属请用 role=joint 的成员。</p>
          {/* 两栏「原因」用途不同，标签必须写清：这一栏进归属变更史（自由文本），
              下面那栏进审计行（封闭枚举，缺了后端 422 AUDIT_REASON_REQUIRED）。 */}
          <FormField label="变更说明（可选，留档到归属历史）" hint="写给人看的上下文，随这条归属历史一起保留。">
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="如：资产转入家庭共同管理" className={cn(fieldControlClass, 'min-h-[76px] resize-none py-2')} />
          </FormField>
          <AuditReasonField
            label="为什么现在改归属"
            value={auditReason}
            onChange={setAuditReason}
            nudgeToken={auditNudge}
            hint="审计理由：写进操作记录（谁在何时为什么改），与上面的变更说明是两回事。"
          />

          <div>
            <div className="mb-2 flex items-center gap-2">
              <History size={15} className="text-secondary" aria-hidden />
              <h3 className="text-h3 font-semibold text-ink">归属历史</h3>
              <span className="ml-auto text-meta text-muted">{history.length} 次</span>
            </div>
            {history.length === 0 ? (
              <p className="rounded-control border border-dashed border-line p-3 text-meta text-muted">尚无资产项级归属变更；保存后会追加一条不可变历史。</p>
            ) : (
              <ol className="divide-y divide-line rounded-control border border-line">
                {[...history].reverse().map((entry) => (
                  <li key={entry.id} className="px-3 py-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-body text-ink">{entry.owner_name ?? '未标注'}</span>
                      <span className="pf-num text-meta text-muted">{entry.effective_date}</span>
                    </div>
                    <p className="mt-0.5 text-meta text-muted">{entry.reason || '未填写理由'}{entry.origin === 'migration' ? ' · 历史迁移' : ''}</p>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </ResponsiveDialog>
  )
}