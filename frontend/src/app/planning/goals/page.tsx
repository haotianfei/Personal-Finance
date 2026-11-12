'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive, CheckCircle2, ChevronRight, Flag, Pencil, Plus, Star,
} from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { formatDate, formatRate, formatValue } from '@/lib/format'
import type {
  Goal, GoalCreateInput, GoalProgress, GoalVersion, GoalVersionInput, RiskProfile, LifeStage,
} from '@/types'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import {
  Card, ConfirmDialog, EmptyState, ErrorState, GradeDot, InlineError, LoadingState, Money,
  ResponsiveDialog,
} from '@/components/ui'
import { AmountField, DateField, FormField, SelectField, fieldControlClass } from '@/components/form'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { gradeLabel } from '@/lib/metric-language'
import { cn } from '@/lib/utils'

const RISK_OPTIONS: { value: RiskProfile; label: string }[] = [
  { value: 'conservative', label: '稳健' },
  { value: 'balanced', label: '平衡' },
  { value: 'growth', label: '成长' },
]
const LIFE_OPTIONS: { value: LifeStage; label: string }[] = [
  { value: 'accumulation', label: '积累期' },
  { value: 'transition', label: '过渡期' },
  { value: 'decumulation', label: '支出期' },
]
const STATUS_LABEL: Record<string, string> = {
  draft: '草案', active: '已生效', archived: '已归档',
}
const STATUS_CLASS: Record<string, string> = {
  draft: 'bg-status-warn-surface text-status-warn-text',
  active: 'bg-status-good-surface text-status-good-text',
  archived: 'bg-subtle text-muted',
}

function goalError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'GOAL_VERSION_CONFLICT') {
      return '版本已被其他操作更新。请刷新后重试，当前操作未覆盖任何版本。'
    }
    if (error.code === 'GOAL_VERSION_IMMUTABLE') {
      return '该版本已生效或归档，不能直接修改；请新建版本。'
    }
    if (error.code === 'VALIDATION_ERROR') {
      const fields = (error.details?.fields as Array<{ field?: string; message?: string }> | undefined) ?? []
      return fields[0]?.message || error.message || '输入有误，请检查后重试。'
    }
    if (error.code === 'ITEM_ARCHIVED') return '该资产项已归档，不能修改归属。'
    return error.message || '操作失败，请重试。'
  }
  return error instanceof Error ? error.message : '操作失败，请重试。'
}

function riskLabel(value: RiskProfile | null | undefined) {
  return RISK_OPTIONS.find((o) => o.value === value)?.label ?? '未设置'
}
function lifeLabel(value: LifeStage | null | undefined) {
  return LIFE_OPTIONS.find((o) => o.value === value)?.label ?? '未设置'
}

function ProgressSummary({ progress, compact = false }: { progress: GoalProgress; compact?: boolean }) {
  const pct = Number(progress.progress)
  const width = Math.max(0, Math.min(100, pct * 100))
  return (
    <div className="space-y-2">
      <div className="flex items-end justify-between gap-3">
        <div>
          <p className="text-meta text-muted">达成度</p>
          <p className="pf-num text-money-m font-semibold text-ink">{formatValue(progress.progress, 'ratio')}</p>
        </div>
        <div className="text-right">
          <p className="text-meta text-muted">当前净值</p>
          <Money value={progress.net_worth} unit="cny" size="s" />
        </div>
      </div>
      <div className="h-2 overflow-hidden rounded-pill bg-subtle">
        <div className={cn('h-full rounded-pill', pct >= 1 ? 'bg-status-good' : 'bg-brand-500')} style={{ width: `${width}%` }} />
      </div>
      <div className="flex flex-wrap justify-between gap-2 text-meta text-muted">
        <span>目标 <Money value={progress.target_amount} unit="cny" size="xs" /></span>
        <span>缺口 <Money value={progress.gap} unit="cny" size="xs" /></span>
        <span>期限 {formatDate(progress.target_date)}</span>
        <span>剩余 {progress.remaining_months} 个月</span>
      </div>
      {progress.required_annualized === null ? (
        <p className="text-meta text-status-warn-text">所需年化：率不可得（净值为 0/负或期限已过）</p>
      ) : (
        <p className="text-meta text-secondary">
          所需年化 <span className="pf-num font-medium text-ink">{formatRate(progress.required_annualized, 'ratio')}</span>
          <span className="text-muted"> · 假设不再投入</span>
        </p>
      )}
      {progress.grade && (
        <span className="inline-flex items-center gap-1 text-meta text-secondary">
          <GradeDot state={progress.grade.state} /> {gradeLabel(progress.grade.state)}
        </span>
      )}
      {!compact && progress.gates.length > 0 && (
        <p className="text-meta text-muted">{progress.gates.map((g) => g.note).join('；')}</p>
      )}
    </div>
  )
}

function GoalFormDialog({
  open, goal, baseVersion, onClose, onSaved,
}: {
  open: boolean
  goal: Goal | null
  baseVersion?: GoalVersion | null
  onClose: () => void
  onSaved: () => void
}) {
  const isDraft = baseVersion?.status === 'draft'
  const [name, setName] = useState('')
  const [amount, setAmount] = useState('')
  const [date, setDate] = useState('')
  const [rate, setRate] = useState('')
  const [risk, setRisk] = useState('')
  const [life, setLife] = useState('')
  const [reason, setReason] = useState('')
  const [activate, setActivate] = useState(true)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    setName(goal?.name ?? '')
    setAmount(baseVersion?.target_amount ?? '')
    const defaultDate = new Date()
    defaultDate.setFullYear(defaultDate.getFullYear() + 5)
    setDate(baseVersion?.target_date ?? defaultDate.toISOString().slice(0, 10))
    setRate(baseVersion?.target_rate_annual ? String(Number(baseVersion.target_rate_annual) * 100) : '')
    setRisk(baseVersion?.risk_profile ?? '')
    setLife(baseVersion?.life_stage ?? '')
    setReason(baseVersion?.reason ?? '')
    setActivate(!isDraft)
    setErrors({})
    setSubmitError(null)
  }, [open, goal, baseVersion, isDraft])

  const submit = async () => {
    const next: Record<string, string> = {}
    if (!name.trim()) next.name = '请输入目标名称'
    const n = Number(amount)
    if (!amount || !Number.isFinite(n) || n <= 0) next.amount = '目标金额须大于 0'
    if (!date) next.date = '请选择目标期限'
    if (!reason.trim()) next.reason = '请填写设定/变更理由'
    if (rate && (!Number.isFinite(Number(rate)) || Number(rate) <= -100)) next.rate = '目标年化须大于 -100%'
    setErrors(next)
    if (Object.keys(next).length) return
    setSaving(true)
    setSubmitError(null)
    const versionData: GoalVersionInput = {
      target_amount: n.toFixed(2),
      target_date: date,
      target_rate_annual: rate === '' ? null : (Number(rate) / 100).toFixed(4),
      risk_profile: (risk || null) as RiskProfile | null,
      life_stage: (life || null) as LifeStage | null,
      reason: reason.trim(),
      activate: isDraft ? undefined : activate,
    }
    try {
      if (!goal) {
        const data: GoalCreateInput = { name: name.trim(), scope: { kind: 'family', owner_id: null }, ...versionData }
        await api.createGoal(data, newIdemKey())
      } else {
        if (name.trim() !== goal.name) await api.renameGoal(goal.id, name.trim(), newIdemKey())
        if (isDraft && baseVersion) {
          await api.updateGoalVersion(goal.id, baseVersion.version_no, versionData, newIdemKey())
          if (activate) await api.activateGoalVersion(goal.id, baseVersion.version_no, newIdemKey())
        } else {
          await api.createGoalVersion(goal.id, versionData, newIdemKey())
        }
      }
      onSaved()
    } catch (error) {
      setSubmitError(goalError(error))
    } finally {
      setSaving(false)
    }
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={goal ? (isDraft ? '编辑目标草案' : '新建目标版本') : '创建目标'}
      description={goal ? '修改金额或期限会留下新版本；生效版本不可直接改写。' : '目标按家庭净资产口径计算，家庭池、成员与未标注项都计入。'}
      size="lg"
      initialFocusRef={nameRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={saving} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={saving} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {saving ? '保存中…' : isDraft ? '保存草案' : goal ? '创建新版本' : '创建目标'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        {submitError && <InlineError>{submitError}</InlineError>}
        <FormField label="目标名称" required error={errors.name}>
          <input ref={nameRef} type="text" value={name} onChange={(e) => { setName(e.target.value); setErrors((p) => ({ ...p, name: '' })) }} placeholder="如：家庭净资产 2030" className={cn(fieldControlClass, errors.name && 'border-status-critical')} />
        </FormField>
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <AmountField label="目标金额（净资产）" required value={amount} onChange={(v) => { setAmount(v); setErrors((p) => ({ ...p, amount: '' })) }} error={errors.amount} hint="资产减负债，不是总资产" />
          <DateField label="目标期限" required value={date} onChange={(v) => { setDate(v); setErrors((p) => ({ ...p, date: '' })) }} error={errors.date} />
        </div>
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <FormField label="目标年化（可选）" error={errors.rate} hint="仅作对齐锚点，不参与达成度公式">
            <div className="relative">
              <input type="text" inputMode="decimal" aria-label="目标年化（可选）" value={rate} onChange={(e) => { setRate(e.target.value); setErrors((p) => ({ ...p, rate: '' })) }} placeholder="如 6.0" className={cn(fieldControlClass, 'pr-8 text-right', errors.rate && 'border-status-critical')} />
              <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-meta text-muted">%</span>
            </div>
          </FormField>
          <div className="grid grid-cols-2 gap-3">
            <SelectField label="风险档" value={risk} onChange={setRisk} placeholder="未设置" options={RISK_OPTIONS} />
            <SelectField label="生命周期" value={life} onChange={setLife} placeholder="未设置" options={LIFE_OPTIONS} />
          </div>
        </div>
        <FormField label="设定/变更理由" required error={errors.reason}>
          <textarea value={reason} onChange={(e) => { setReason(e.target.value); setErrors((p) => ({ ...p, reason: '' })) }} rows={3} placeholder="如：家庭共识，按当前收入与储蓄节奏设定" className={cn(fieldControlClass, 'min-h-[88px] resize-none py-2', errors.reason && 'border-status-critical')} />
        </FormField>
        {!isDraft && (
          <label className="flex min-h-[44px] items-center gap-2 text-body text-secondary">
            <input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} className="h-5 w-5 accent-[var(--pf-color-action-primary)]" />
            {goal ? '创建后立即激活（旧生效版本归档）' : '创建后立即激活'}
          </label>
        )}
        {isDraft && (
          <label className="flex min-h-[44px] items-center gap-2 text-body text-secondary">
            <input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} className="h-5 w-5 accent-[var(--pf-color-action-primary)]" />
            保存后立即激活
          </label>
        )}
      </div>
    </ResponsiveDialog>
  )
}

function VersionTimeline({ versions }: { versions: GoalVersion[] }) {
  return (
    <ol className="space-y-3">
      {versions.map((version) => (
        <li key={version.id} className="rounded-control border border-line bg-surface p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-body-strong font-medium text-ink">v{version.version_no}</span>
              <span className={cn('rounded-pill px-2 py-0.5 text-meta', STATUS_CLASS[version.status])}>{STATUS_LABEL[version.status]}</span>
              {version.effective_from && <span className="pf-num text-meta text-muted">生效 {formatDate(version.effective_from)}</span>}
            </div>
            <span className="text-meta text-muted">{version.reason}</span>
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-meta">
            <div><dt className="text-muted">目标金额</dt><dd><Money value={version.target_amount} unit="cny" size="xs" /></dd></div>
            <div><dt className="text-muted">期限</dt><dd className="pf-num text-secondary">{formatDate(version.target_date)}</dd></div>
            <div><dt className="text-muted">目标年化</dt><dd className="pf-num text-secondary">{version.target_rate_annual ? formatRate(version.target_rate_annual, 'ratio') : '—'}</dd></div>
            <div><dt className="text-muted">分区</dt><dd className="text-secondary">{riskLabel(version.risk_profile)} · {lifeLabel(version.life_stage)}</dd></div>
          </dl>
          {version.criterion && (
            <details className="mt-2 text-meta text-muted">
              <summary className="cursor-pointer">判据快照（只读）</summary>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-subtle p-2 text-[12px] leading-5">{JSON.stringify(version.criterion, null, 2)}</pre>
            </details>
          )}
        </li>
      ))}
    </ol>
  )
}

function GoalDetailDialog({
  open, goal, latestSnapshot, onClose, onEdit, onToast,
}: {
  open: boolean
  goal: Goal | null
  latestSnapshot: string | null | undefined
  onClose: () => void
  onEdit: (goal: Goal, version: GoalVersion | null) => void
  onToast: (msg: string) => void
}) {
  const queryClient = useQueryClient()
  const [confirmArchive, setConfirmArchive] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  /** 归档框内的提示（缺理由时出现；弹窗不关，已选的理由不丢）。 */
  const [archiveError, setArchiveError] = useState<string | null>(null)
  // 理由草稿按目标保留：关窗不清、换一个目标才清
  const [archiveReason, setArchiveReason] = useAuditReasonDraft(goal ? goal.id : null)
  const detailQuery = useQuery({
    queryKey: ['goalDetail', goal?.id],
    queryFn: () => api.getGoal(goal!.id),
    enabled: open && !!goal,
  })
  const progressQuery = useQuery({
    queryKey: ['goalProgress', goal?.id],
    queryFn: () => api.getGoalProgress(goal!.id),
    enabled: open && !!goal && !!detailQuery.data?.versions.some((v) => v.status === 'active') && latestSnapshot !== undefined && latestSnapshot !== null && (!detailQuery.data?.versions.find((v) => v.status === 'active')?.effective_from || (detailQuery.data?.versions.find((v) => v.status === 'active')?.effective_from ?? '') <= latestSnapshot),
    retry: false,
  })
  const versions = detailQuery.data?.versions ?? []
  const active = versions.find((v) => v.status === 'active') ?? null
  const detailGoal = detailQuery.data?.goal ?? goal

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['goals'] })
    queryClient.invalidateQueries({ queryKey: ['goalDetail', goal?.id] })
    queryClient.invalidateQueries({ queryKey: ['goalProgress', goal?.id] })
    queryClient.invalidateQueries({ queryKey: ['metricStatus'] })
  }
  const actionMutation = useMutation({
    mutationFn: async ({ action, version, audit }: {
      action: 'archive' | 'primary' | 'activate'
      version?: GoalVersion
      audit?: AuditReasonInput
    }) => {
      if (!goal) return
      if (action === 'archive') return api.archiveGoal(goal.id, newIdemKey(), audit)
      if (action === 'primary') return api.setPrimaryGoal(goal.id, newIdemKey())
      if (action === 'activate' && version) return api.activateGoalVersion(goal.id, version.version_no, newIdemKey())
    },
    onSuccess: (_, vars) => {
      invalidate()
      setActionError(null)
      if (vars.action === 'archive') {
        setConfirmArchive(false)
        setArchiveError(null)
      }
      onToast(vars.action === 'archive' ? '目标已归档' : vars.action === 'primary' ? '已设为主目标' : '版本已激活')
    },
    onError: (error, vars) => {
      // 归档成功才关弹窗；缺理由或其它失败都留在弹窗里说，底层详情的提示不动
      const text = isAuditReasonRequired(error) ? AUDIT_REASON_REQUIRED_TEXT : goalError(error)
      if (vars.action === 'archive') {
        setArchiveError(text)
        return
      }
      setActionError(text)
    },
  })

  return (
    <>
      <ResponsiveDialog open={open} onClose={onClose} title={detailGoal?.name ?? '目标详情'} description="版本变更留史；生效版本不可直接改写。" size="lg">
        {detailQuery.isLoading ? <LoadingState rows={4} /> : detailQuery.isError ? (
          <ErrorState title="目标详情加载失败" description={goalError(detailQuery.error)} onRetry={() => detailQuery.refetch()} />
        ) : (
          <div className="space-y-5">
            {actionError && <InlineError>{actionError}</InlineError>}
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn('rounded-pill px-2 py-0.5 text-meta', STATUS_CLASS[detailGoal?.status ?? 'draft'])}>{STATUS_LABEL[detailGoal?.status ?? 'draft']}</span>
              {detailGoal?.is_primary && <span className="rounded-pill bg-status-warn-surface px-2 py-0.5 text-meta text-status-warn-text">主目标</span>}
              <div className="ml-auto flex flex-wrap gap-2">
                {detailGoal && detailGoal.status !== 'archived' && (
                  <>
                    <button type="button" onClick={() => onEdit(detailGoal, active)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink"><Pencil size={14} aria-hidden /> 新建版本</button>
                    {!detailGoal.is_primary && active && (
                      <button type="button" onClick={() => actionMutation.mutate({ action: 'primary' })} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink"><Star size={14} aria-hidden /> 设为主目标</button>
                    )}
                    <button type="button" onClick={() => setConfirmArchive(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-status-critical/30 bg-surface px-3 text-meta text-status-critical-text"><Archive size={14} aria-hidden /> 归档目标</button>
                  </>
                )}
              </div>
            </div>

            {active && latestSnapshot !== undefined && latestSnapshot !== null && (!active.effective_from || active.effective_from <= latestSnapshot) ? (
              progressQuery.isLoading ? <LoadingState rows={2} /> : progressQuery.isError ? (
                <InlineError>达成度暂不可用：{goalError(progressQuery.error)}。目标仍有生效版本，可稍后重试。</InlineError>
              ) : progressQuery.data ? (
                <Card padding="sm"><ProgressSummary progress={progressQuery.data} /></Card>
              ) : null
            ) : active ? (
              <p className="rounded-control border border-dashed border-line bg-subtle p-3 text-meta text-status-warn-text">等待首个 ≥ 生效日的快照。目标自 {active.effective_from ?? '生效日'} 生效；最新快照 {latestSnapshot ?? '暂无'}。</p>
            ) : (
              <InlineError>目标暂无生效版本，无法计算达成度。请激活一个草案或新建版本。</InlineError>
            )}

            <div>
              <div className="mb-2 flex items-center justify-between gap-2">
                <h3 className="text-h3 font-semibold text-ink">版本时间线</h3>
                <span className="text-meta text-muted">{versions.length} 个版本</span>
              </div>
              <VersionTimeline versions={versions} />
            </div>

            {versions.filter((v) => v.status === 'draft').length > 0 && (
              <div className="space-y-2">
                <h3 className="text-h3 font-semibold text-ink">草案操作</h3>
                {versions.filter((v) => v.status === 'draft').map((version) => (
                  <div key={version.id} className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-line bg-subtle p-3">
                    <span className="text-body text-secondary">v{version.version_no} · <Money value={version.target_amount} unit="cny" size="xs" /> · {formatDate(version.target_date)}</span>
                    <div className="flex gap-2">
                      <button type="button" onClick={() => onEdit(detailGoal!, version)} className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink">编辑草案</button>
                      <button type="button" onClick={() => actionMutation.mutate({ action: 'activate', version })} disabled={actionMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-3 text-meta font-medium text-white disabled:opacity-50"><CheckCircle2 size={14} aria-hidden /> 激活</button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="rounded-control border border-line bg-subtle p-3 text-meta text-muted">
              达成度假设「不再追加/撤出本金」，所需年化为达到目标所需的复合年化门槛；目标金额按家庭净资产口径计算。
            </div>
          </div>
        )}
      </ResponsiveDialog>
      <ConfirmDialog
        open={confirmArchive}
        onClose={() => {
          setConfirmArchive(false)
          setArchiveError(null)
        }}
        onConfirm={() => actionMutation.mutate({
          action: 'archive',
          audit: toAuditReasonInput(archiveReason),
        })}
        title={detailGoal ? `归档目标「${detailGoal.name}」？` : '归档目标？'}
        description="归档后所有版本终态、主目标标记清除；不提供撤销，需要继续则新建目标。"
        confirmLabel="归档目标"
        danger
        loading={actionMutation.isPending}
      >
        <div className="space-y-3">
          {archiveError && <InlineError>{archiveError}</InlineError>}
          <AuditReasonField
            label="为什么归档"
            value={archiveReason}
            onChange={setArchiveReason}
            hint="归档会留下谁在何时归档了哪个目标。"
          />
        </div>
      </ConfirmDialog>
    </>
  )
}

function GoalCard({ goal, latestSnapshot, onDetail, onEdit, onToast }: {
  goal: Goal
  latestSnapshot: string | null | undefined
  onDetail: (goal: Goal) => void
  onEdit: (goal: Goal, version: GoalVersion | null) => void
  onToast: (msg: string) => void
}) {
  const queryClient = useQueryClient()
  const progressQuery = useQuery({
    queryKey: ['goalProgress', goal.id],
    queryFn: () => api.getGoalProgress(goal.id),
    enabled: goal.status === 'active' && !!goal.active_version && latestSnapshot !== undefined && latestSnapshot !== null && (!goal.active_version.effective_from || goal.active_version.effective_from <= latestSnapshot),
    retry: false,
  })
  const [confirmArchive, setConfirmArchive] = useState(false)
  /** 归档框内的提示（缺理由时出现；弹窗不关，已选的理由不丢）。 */
  const [archiveError, setArchiveError] = useState<string | null>(null)
  // 理由草稿按目标保留：关窗不清、换一个目标才清
  const [archiveReason, setArchiveReason] = useAuditReasonDraft(goal.id)
  const actionMutation = useMutation({
    mutationFn: async ({ action, audit }: { action: 'primary' | 'archive'; audit?: AuditReasonInput }) =>
      action === 'primary'
        ? api.setPrimaryGoal(goal.id, newIdemKey())
        : api.archiveGoal(goal.id, newIdemKey(), audit),
    onSuccess: (_, { action }) => {
      queryClient.invalidateQueries({ queryKey: ['goals'] })
      queryClient.invalidateQueries({ queryKey: ['metricStatus'] })
      if (action === 'archive') {
        setConfirmArchive(false)
        setArchiveError(null)
      }
      onToast(action === 'primary' ? '已设为主目标' : '目标已归档')
    },
    onError: (error, { action }) => {
      const text = isAuditReasonRequired(error) ? AUDIT_REASON_REQUIRED_TEXT : goalError(error)
      if (action === 'archive') {
        setArchiveError(text)
        return
      }
      onToast(text)
    },
  })
  const progress = progressQuery.data
  return (
    <Card as="article" padding="md" className={cn(goal.is_primary && 'border-brand-200')}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-h3 font-semibold text-ink">{goal.name}</h2>
            {goal.is_primary && <span className="rounded-pill bg-status-warn-surface px-2 py-0.5 text-meta text-status-warn-text">主目标</span>}
            <span className={cn('rounded-pill px-2 py-0.5 text-meta', STATUS_CLASS[goal.status])}>{STATUS_LABEL[goal.status]}</span>
          </div>
          <p className="mt-1 text-meta text-muted">家庭净资产口径 · 创建 {formatDate(goal.created_at)}</p>
        </div>
        <button type="button" onClick={() => onDetail(goal)} className="inline-flex min-h-[44px] shrink-0 items-center gap-1 rounded-control px-2 text-meta text-link">
          详情 <ChevronRight size={14} aria-hidden />
        </button>
      </div>

      {goal.status === 'active' && goal.active_version && latestSnapshot !== undefined && latestSnapshot !== null && (!goal.active_version.effective_from || goal.active_version.effective_from <= latestSnapshot) ? (
        progressQuery.isLoading ? <div className="mt-4"><LoadingState rows={2} compact /></div> : progress ? (
          <div className="mt-4"><ProgressSummary progress={progress} compact /></div>
        ) : (
          <p className="mt-4 text-meta text-status-warn-text">达成度暂不可用：{goalError(progressQuery.error)}</p>
        )
      ) : (
        <div className="mt-4 space-y-2 rounded-control border border-dashed border-line bg-subtle p-3 text-meta text-muted">
          {goal.status === 'archived' ? '已归档，不再参与北极星与指标条。' : goal.status === 'active' && goal.active_version ? (
            <>
              <p className="flex flex-wrap gap-x-3 gap-y-1">
                <span>目标 <Money value={goal.active_version.target_amount} unit="cny" size="xs" /></span>
                <span>期限 {formatDate(goal.active_version.target_date)}</span>
              </p>
              <p className="text-status-warn-text">等待首个 ≥ 生效日的快照。目标自 {goal.active_version.effective_from ?? '生效日'} 生效；最新快照 {latestSnapshot ?? '暂无'}。</p>
            </>
          ) : '暂无生效版本；在详情中激活草案后将显示达成度。'}
        </div>
      )}

      {goal.status !== 'archived' && (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-3">
          <button type="button" onClick={() => onEdit(goal, goal.active_version ?? null)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink">
            <Pencil size={14} aria-hidden /> {goal.active_version ? '新建版本' : '添加版本'}
          </button>
          {!goal.is_primary && goal.status === 'active' && (
            <button type="button" onClick={() => actionMutation.mutate({ action: 'primary' })} disabled={actionMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-meta text-ink disabled:opacity-50">
              <Star size={14} aria-hidden /> 设为主目标
            </button>
          )}
          <button type="button" onClick={() => setConfirmArchive(true)} disabled={actionMutation.isPending} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-status-critical/30 bg-surface px-3 text-meta text-status-critical-text disabled:opacity-50">
            <Archive size={14} aria-hidden /> 归档
          </button>
        </div>
      )}
      <ConfirmDialog
        open={confirmArchive}
        onClose={() => {
          setConfirmArchive(false)
          setArchiveError(null)
        }}
        onConfirm={() => actionMutation.mutate({
          action: 'archive',
          audit: toAuditReasonInput(archiveReason),
        })}
        title={`归档目标「${goal.name}」？`}
        description="归档后所有版本终态、主目标标记清除；不提供撤销，需要继续则新建目标。"
        confirmLabel="归档目标"
        danger
        loading={actionMutation.isPending}
      >
        <div className="space-y-3">
          {archiveError && <InlineError>{archiveError}</InlineError>}
          <AuditReasonField
            label="为什么归档"
            value={archiveReason}
            onChange={setArchiveReason}
            hint="归档会留下谁在何时归档了哪个目标。"
          />
        </div>
      </ConfirmDialog>
    </Card>
  )
}

export default function GoalsPage() {
  const queryClient = useQueryClient()
  const [includeArchived, setIncludeArchived] = useState(false)
  const [formOpen, setFormOpen] = useState(false)
  const [formGoal, setFormGoal] = useState<Goal | null>(null)
  const [formVersion, setFormVersion] = useState<GoalVersion | null>(null)
  const [detailGoal, setDetailGoal] = useState<Goal | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const goalsQuery = useQuery({
    queryKey: ['goals', includeArchived],
    queryFn: () => api.getGoals(includeArchived),
  })
  const datesQuery = useQuery({ queryKey: ['assetDates'], queryFn: api.getDates })
  const latestSnapshot = useMemo(() => {
    const dates = [...(datesQuery.data ?? [])].sort()
    return dates.at(-1) ?? null
  }, [datesQuery.data])
  const goals = useMemo(() => {
    const items = goalsQuery.data?.items ?? []
    return [...items].sort((a, b) => {
      if (a.is_primary !== b.is_primary) return a.is_primary ? -1 : 1
      return (b.active_version?.created_at ?? b.created_at).localeCompare(
        a.active_version?.created_at ?? a.created_at,
      )
    })
  }, [goalsQuery.data])

  const openCreate = () => { setFormGoal(null); setFormVersion(null); setFormOpen(true) }
  const openEdit = (goal: Goal, version: GoalVersion | null) => {
    setDetailGoal(null)
    setFormGoal(goal)
    setFormVersion(version)
    setFormOpen(true)
  }
  const saved = () => {
    setFormOpen(false)
    setFormGoal(null)
    setFormVersion(null)
    queryClient.invalidateQueries({ queryKey: ['goals'] })
    queryClient.invalidateQueries({ queryKey: ['goalDetail'] })
    queryClient.invalidateQueries({ queryKey: ['goalProgress'] })
    queryClient.invalidateQueries({ queryKey: ['metricStatus'] })
    setNotice('目标已保存')
  }

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader
        title="目标与达成度"
        description="以家庭净资产金额与期限定义未来目标；每次金额/期限变更都会留下版本与理由。"
        actions={
          <>
            <button type="button" onClick={() => setIncludeArchived((v) => !v)} aria-pressed={includeArchived} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <Archive size={15} aria-hidden /> {includeArchived ? '隐藏已归档' : '显示已归档'}
            </button>
            <button type="button" onClick={openCreate} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
              <Plus size={16} aria-hidden /> 新建目标
            </button>
          </>
        }
      />

      {notice && (
        <div className="flex items-center justify-between gap-2 rounded-control border border-status-good/20 bg-status-good-surface px-3 py-2 text-meta text-status-good-text">
          <span className="inline-flex items-center gap-1.5"><CheckCircle2 size={15} aria-hidden /> {notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="min-h-[44px] px-2 text-meta">关闭</button>
        </div>
      )}

      {goalsQuery.isLoading ? <LoadingState rows={4} label="正在加载目标" /> : goalsQuery.isError ? (
        <ErrorState title="目标列表加载失败" description={goalError(goalsQuery.error)} onRetry={() => goalsQuery.refetch()} />
      ) : goals.length === 0 ? (
        <EmptyState
          icon={Flag}
          title={includeArchived ? '暂无已归档目标' : '还没有目标'}
          description="用一个金额与期限定义方向；创建后可查看达成度、缺口与所需年化。"
          action={<button type="button" onClick={openCreate} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong text-white"><Plus size={16} aria-hidden /> 新建目标</button>}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {goals.map((goal) => (
            <GoalCard key={goal.id} goal={goal} latestSnapshot={datesQuery.isLoading ? undefined : latestSnapshot} onDetail={setDetailGoal} onEdit={openEdit} onToast={setNotice} />
          ))}
        </div>
      )}

      <GoalFormDialog open={formOpen} goal={formGoal} baseVersion={formVersion} onClose={() => setFormOpen(false)} onSaved={saved} />
      <GoalDetailDialog open={!!detailGoal} goal={detailGoal} latestSnapshot={datesQuery.isLoading ? undefined : latestSnapshot} onClose={() => setDetailGoal(null)} onEdit={openEdit} onToast={setNotice} />
    </PageContainer>
  )
}
