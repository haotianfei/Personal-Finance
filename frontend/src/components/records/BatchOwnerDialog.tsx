'use client'

import { useEffect, useState } from 'react'
import dayjs from 'dayjs'
import type { BatchOwnerResponse, Member } from '@/types'
import type { AuditReasonInput } from '@/lib/api'
import { InlineError, ResponsiveDialog } from '@/components/ui'
import { isReasonMissing, runConfirmGate } from '@/components/ui/confirmGate'
import {
  AuditReasonField,
  EMPTY_AUDIT_REASON,
  toAuditReasonInput,
} from '@/components/records/AuditReasonField'
import type { AuditReasonValue } from '@/components/records/AuditReasonField'
import { DateField } from '@/components/form/DateField'
import { FormField, fieldControlClass } from '@/components/form/FormField'
import { SelectField } from '@/components/form/SelectField'
import { cn } from '@/lib/utils'

export interface BatchOwnerFormData {
  /** 归属只接受成员（family_pool 已随 ADR-0019 D6 退役）。 */
  owner_kind: 'member'
  owner_id: number | null
  reason: string
  effective_date: string | null
}

const FAILURE_LABEL: Record<string, string> = {
  ITEM_ARCHIVED: '资产项已归档',
  NOT_FOUND: '资产项不存在',
  VALIDATION_ERROR: '参数校验失败',
  // 审计理由没送到（不是归属冲突）：单独点名，别让人误以为选中的项都被占了
  AUDIT_REASON_REQUIRED: '审计理由没送到（缺 reason）',
}

function failureLabel(code: string): string {
  return FAILURE_LABEL[code] ?? code
}

export function BatchOwnerDialog({
  open,
  selectedRecordCount,
  selectedItemCount,
  members,
  pending,
  result,
  error,
  itemLabelById,
  onSubmit,
  onClose,
}: {
  open: boolean
  selectedRecordCount: number
  selectedItemCount: number
  members: Member[]
  pending: boolean
  result: BatchOwnerResponse | null
  error: string | null
  itemLabelById: Map<number, string>
  onSubmit: (data: BatchOwnerFormData, audit: AuditReasonInput) => void
  onClose: () => void
}) {
  const [ownerId, setOwnerId] = useState('')
  const [reason, setReason] = useState('')
  const [effectiveDate, setEffectiveDate] = useState(dayjs().format('YYYY-MM-DD'))
  const [formError, setFormError] = useState<string | null>(null)
  /** 审计理由（写操作记录，缺了后端逐项 422）：本弹窗自画提交按钮，靠 nudgeToken 反馈。 */
  const [auditReason, setAuditReason] = useState<AuditReasonValue>(EMPTY_AUDIT_REASON)
  const [auditNudge, setAuditNudge] = useState(0)

  useEffect(() => {
    if (!open) return
    setOwnerId('')
    setReason('')
    setEffectiveDate(dayjs().format('YYYY-MM-DD'))
    setFormError(null)
    // 理由只在新开一批时清空；失败重试沿用上一次的选择（重试按钮就在结果页）
    setAuditReason(EMPTY_AUDIT_REASON)
    setAuditNudge(0)
  }, [open])

  const submit = () => {
    if (!ownerId) { setFormError('请选择成员'); return }
    if (!reason.trim()) { setFormError('请填写变更说明'); return }
    // 改归属是纠正类写入（action='change'）：缺审计理由后端逐项 422，这里先拦——请求不发
    if (runConfirmGate({
      blocked: () => isReasonMissing(auditReason.reason),
      nudge: () => setAuditNudge((count) => count + 1),
    })) return
    setFormError(null)
    onSubmit({
      owner_kind: 'member',
      owner_id: Number(ownerId),
      reason: reason.trim(),
      effective_date: effectiveDate || null,
    }, toAuditReasonInput(auditReason))
  }

  const hasFailure = (result?.failed.length ?? 0) > 0
  /** 失败项里「审计理由没送到」的条数：>0 时结果页给出可执行的下一步（而不是只说失败）。 */
  const missingReasonCount = (result?.failed ?? []).filter(
    (item) => item.code === 'AUDIT_REASON_REQUIRED',
  ).length
  const footer = result ? (
    <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
      <button type="button" onClick={onClose} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink">关闭</button>
      {hasFailure && <button type="button" onClick={submit} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{pending ? '重试中…' : `重试失败项（${result.failed.length}）`}</button>}
    </div>
  ) : (
    <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
      <button type="button" onClick={onClose} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
      <button type="button" onClick={submit} disabled={pending || selectedItemCount === 0} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{pending ? '提交中…' : '提交归属变更'}</button>
    </div>
  )

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="批量修改归属"
      description="按资产项写入归属历史；同值项会跳过，已归档项不可修改。"
      size="md"
      footer={footer}
    >
      <div className="space-y-4">
        {error && <p role="alert" className="rounded-control border border-status-critical/25 bg-status-critical-surface p-3 text-meta text-status-critical-text">{error}</p>}
        {result ? (
          <div className="space-y-3">
            <div className="rounded-control border border-line bg-subtle p-3 text-body text-secondary">
              <p className="font-medium text-ink">批量归属结果</p>
              <p className="mt-1 text-meta">已更新 <strong className="pf-num text-status-good-text">{result.updated}</strong> · 跳过 <strong className="pf-num text-status-warn-text">{result.skipped}</strong> · 失败 <strong className="pf-num text-status-critical-text">{result.failed.length}</strong></p>
            </div>
            {hasFailure && (
              <div className="space-y-1">
                <p className="text-meta font-medium text-secondary">失败项</p>
                <ul className="divide-y divide-line rounded-control border border-line">
                  {result.failed.map((item) => (
                    <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-meta">
                      <span className="text-secondary">{itemLabelById.get(item.id) ?? `资产项 #${item.id}`}</span>
                      <span className="text-status-critical-text">{failureLabel(item.code)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {!hasFailure && <p className="text-meta text-status-good-text">本批全部处理完成。</p>}
            {hasFailure && <p className="text-meta text-muted">失败项已保留在选择中，可直接重试；关闭后也可重新勾选。</p>}
            {/* 缺审计理由不是「归属冲突」：把话说明白；重试同样是一次写操作，理由放在结果页可改 */}
            {hasFailure && (
              <div className="space-y-3">
                {missingReasonCount > 0 && (
                  <InlineError>
                    有 {missingReasonCount} 项因「审计理由没送到」失败：这次请求没带上审计理由，不是归属冲突或占用。补选理由后点「重试失败项」即可。
                  </InlineError>
                )}
                <AuditReasonField
                  label="为什么现在改归属"
                  value={auditReason}
                  onChange={setAuditReason}
                  nudgeToken={auditNudge}
                  hint="重试会带上这条审计理由；写进操作记录，与变更说明是两回事。"
                />
              </div>
            )}
          </div>
        ) : (
          <>
            <p className="rounded-control bg-subtle px-3 py-2 text-meta text-secondary">
              将更新 <strong className="pf-num text-ink">{selectedItemCount}</strong> 个资产项（来自 <strong className="pf-num text-ink">{selectedRecordCount}</strong> 条记录）。
            </p>
            {formError && <p role="alert" className="text-meta text-status-critical-text">{formError}</p>}
            <SelectField
              label="归属成员"
              value={ownerId}
              onChange={(value) => { setOwnerId(value); setFormError(null) }}
              placeholder={members.length ? '选择成员' : '暂无可选成员'}
              options={members.map((member) => ({ value: String(member.id), label: member.name }))}
              disabled={members.length === 0}
              required
            />
            {members.length === 0 && <p className="text-meta text-status-warn-text">还没有可选成员，请先在「设置 → 字典 → 成员」维护；共有归属用 role=joint 的成员表达。</p>}
            <FormField label="变更说明" required hint="写给人看的上下文，随这条归属历史一起保留（不是审计理由）。">
              <textarea value={reason} onChange={(e) => { setReason(e.target.value); setFormError(null) }} rows={3} placeholder="如：年中家庭归属调整" className={cn(fieldControlClass, 'min-h-[90px] resize-none py-2')} />
            </FormField>
            <AuditReasonField
              label="为什么现在改归属"
              value={auditReason}
              onChange={setAuditReason}
              nudgeToken={auditNudge}
              hint="审计理由：写进操作记录（谁在何时为什么改）；缺了后端会逐项拒绝。"
            />
            <DateField label="生效日期（可选）" value={effectiveDate} onChange={setEffectiveDate} hint="留空表示今天；同日同值会跳过" />
          </>
        )}
      </div>
    </ResponsiveDialog>
  )
}