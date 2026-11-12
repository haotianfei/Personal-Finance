'use client'

import { useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { message } from 'antd'
import { Bell, Download, Pencil, Plus, Search, Trash2, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import type { AlertRule, AlertRuleCreate } from '@/types'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, DataTable, InlineError, ResponsiveDialog } from '@/components/ui'
import type { DataTableColumn } from '@/components/ui'
import { AmountField, FormField, SelectField, fieldControlClass } from '@/components/form'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { cn } from '@/lib/utils'

const DIMENSION_OPTIONS = [
  { value: 'asset_name', label: '资产名称' },
  { value: 'asset_class', label: '资产类别' },
  { value: 'liquidity', label: '流动性档位' },
  { value: 'institution', label: '机构' },
  { value: 'channel', label: '渠道' },
  { value: 'owner', label: '归属成员' },
  { value: 'tag', label: '标签' },
]
const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' }, { value: 'week', label: '周' }, { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' }, { value: 'year', label: '年' },
]
const COMPARE_TYPE_OPTIONS = [{ value: 'previous', label: '上一期' }, { value: 'custom', label: '自定义' }]
const DIRECTION_OPTIONS = [{ value: 'both', label: '双向' }, { value: 'up', label: '仅增长' }, { value: 'down', label: '仅下降' }]

function dimensionLabel(value: string) {
  return DIMENSION_OPTIONS.find((item) => item.value === value)?.label ?? value
}
function periodLabel(value: string) {
  return PERIOD_TYPE_OPTIONS.find((item) => item.value === value)?.label ?? value
}
function directionLabel(value: string) {
  return DIRECTION_OPTIONS.find((item) => item.value === value)?.label ?? value
}

const EMPTY_RULE: AlertRuleCreate = {
  name: '', dimension: 'asset_name', target_id: null, period_type: 'month',
  compare_type: 'previous', compare_period: null, amount_threshold: null,
  percent_threshold: null, direction: 'both', is_active: true,
}

function RuleDialog({ rule, onClose }: { rule: AlertRule | null; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<AlertRuleCreate>(rule ? {
    name: rule.name, dimension: rule.dimension, target_id: rule.target_id,
    period_type: rule.period_type, compare_type: rule.compare_type,
    compare_period: rule.compare_period, amount_threshold: rule.amount_threshold,
    percent_threshold: rule.percent_threshold, direction: rule.direction, is_active: rule.is_active,
  } : EMPTY_RULE)
  const [error, setError] = useState<string | undefined>()
  const nameRef = useRef<HTMLInputElement>(null)

  const { data: assetNames } = useQuery({ queryKey: ['assetNames'], queryFn: api.getAssetNames, enabled: form.dimension === 'asset_name' })
  const { data: assetClassesData } = useQuery({ queryKey: ['assetClasses'], queryFn: api.getAssetClasses, enabled: form.dimension === 'asset_class' })
  const { data: liquidityLevelsData } = useQuery({ queryKey: ['liquidityLevels'], queryFn: api.getLiquidityLevels, enabled: form.dimension === 'liquidity', staleTime: Infinity })
  const { data: institutionsData } = useQuery({ queryKey: ['institutions'], queryFn: api.getInstitutions, enabled: form.dimension === 'institution' })
  const { data: channelsData } = useQuery({ queryKey: ['channels'], queryFn: api.getChannels, enabled: form.dimension === 'channel' })
  const { data: membersData } = useQuery({ queryKey: ['members'], queryFn: api.getMembers, enabled: form.dimension === 'owner' })
  const { data: tagsData } = useQuery({ queryKey: ['tags'], queryFn: api.getTags, enabled: form.dimension === 'tag' })
  const { data: periods } = useQuery({
    queryKey: ['alertPeriods', form.period_type],
    queryFn: () => api.getAlertPeriods(form.period_type),
    enabled: form.compare_type === 'custom',
  })

  const targetOptions = useMemo(() => {
    switch (form.dimension) {
      case 'asset_name':
        return (assetNames ?? []).map((name) => ({ value: name, label: name }))
      case 'asset_class':
        return (assetClassesData?.items ?? []).map((item) => ({ value: String(item.id), label: item.path || item.name }))
      case 'liquidity':
        return (liquidityLevelsData?.items ?? []).map((item) => ({ value: String(item.days), label: item.label }))
      case 'institution':
        return (institutionsData?.items ?? []).map((item) => ({ value: String(item.id), label: item.name }))
      case 'channel':
        return (channelsData?.items ?? []).map((item) => ({ value: String(item.id), label: item.name }))
      case 'owner':
        return (membersData?.items ?? []).map((item) => ({ value: String(item.id), label: item.name }))
      case 'tag':
        return (tagsData?.items ?? []).map((item) => ({ value: String(item.id), label: item.name }))
      default:
        return []
    }
  }, [form.dimension, assetNames, assetClassesData, liquidityLevelsData, institutionsData, channelsData, membersData, tagsData])

  const mutation = useMutation({
    mutationFn: () => rule ? api.updateAlertRule(rule.id, form) : api.createAlertRule(form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
      message.success(rule ? '规则已保存' : '规则已创建')
      onClose()
    },
    onError: (e: Error) => setError(e.message || '保存失败，请重试。'),
  })

  const submit = () => {
    if (!form.name.trim()) { setError('请输入规则名称'); return }
    if (form.compare_type === 'custom' && !form.compare_period) { setError('请选择对比账期'); return }
    setError(undefined)
    mutation.mutate()
  }

  return (
    <ResponsiveDialog
      open
      onClose={onClose}
      title={rule ? '编辑预警规则' : '创建预警规则'}
      size="md"
      initialFocusRef={nameRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={mutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={mutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">{mutation.isPending ? '保存中…' : rule ? '保存修改' : '创建规则'}</button>
        </div>
      }
    >
      <div className="space-y-4">
        <FormField label="规则名称" required error={error}>
          <input ref={nameRef} type="text" value={form.name} onChange={(e) => { setForm({ ...form, name: e.target.value }); if (error) setError(undefined) }} placeholder="如：净资产月度波动" className={fieldControlClass} />
        </FormField>
        <SelectField label="预警维度" value={form.dimension} onChange={(value) => setForm({ ...form, dimension: value as AlertRuleCreate['dimension'], target_id: null })} options={DIMENSION_OPTIONS} />
        <SelectField label="预警对象" value={form.target_id ?? ''} onChange={(value) => setForm({ ...form, target_id: value || null })} placeholder="全部（不限定对象）" options={targetOptions} />
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <SelectField label="账期类型" value={form.period_type} onChange={(value) => setForm({ ...form, period_type: value as AlertRuleCreate['period_type'], compare_period: null })} options={PERIOD_TYPE_OPTIONS} />
          <SelectField label="对比方式" value={form.compare_type} onChange={(value) => setForm({ ...form, compare_type: value as AlertRuleCreate['compare_type'], compare_period: null })} options={COMPARE_TYPE_OPTIONS} />
        </div>
        {form.compare_type === 'custom' && (
          <SelectField label="对比账期" value={form.compare_period ?? ''} onChange={(value) => setForm({ ...form, compare_period: value || null })} options={(periods ?? []).map((p) => ({ value: p.label, label: p.label }))} />
        )}
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <AmountField label="金额阈值" value={form.amount_threshold ?? ''} onChange={(value) => setForm({ ...form, amount_threshold: value || null })} placeholder="可选" />
          <FormField label="百分比阈值（%）">
            <input type="number" step="0.01" value={form.percent_threshold ?? ''} onChange={(e) => setForm({ ...form, percent_threshold: e.target.value || null })} placeholder="可选" className={fieldControlClass} />
          </FormField>
        </div>
        <SelectField label="预警方向" value={form.direction ?? 'both'} onChange={(value) => setForm({ ...form, direction: value as AlertRuleCreate['direction'] })} options={DIRECTION_OPTIONS} />
        <label className="flex min-h-[44px] items-center gap-2 text-body text-secondary">
          <input type="checkbox" checked={form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} className="h-4 w-4 accent-[var(--pf-color-action-primary)]" />
          启用规则
        </label>
      </div>
    </ResponsiveDialog>
  )
}

export default function AlertRulesSettingsPage() {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<AlertRule | null>(null)
  const [deleting, setDeleting] = useState<AlertRule | null>(null)
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)

  // 理由草稿按规则保留：关窗不清、换一条规则才清
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? deleting.id : null)

  const rulesQuery = useQuery({ queryKey: ['alertRules'], queryFn: () => api.getAlertRules() })
  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data])

  const toggleMutation = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) => api.updateAlertRule(id, { is_active }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['alertRules'] }); queryClient.invalidateQueries({ queryKey: ['alertResults'] }) },
    onError: (e: Error) => message.error(e.message || '更新失败'),
  })
  const deleteMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) =>
      api.deleteAlertRule(id, audit),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
      message.success('规则已删除')
      setDeleting(null)
      setDeleteError(null)
    },
    onError: (error) => {
      // 缺理由（422）不是删除失败：把弹窗和已选的理由都留着，补一句再点一次。
      if (isAuditReasonRequired(error)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(error.message || '删除失败')
    },
  })

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? rules.filter((rule) => rule.name.toLowerCase().includes(q)) : rules
  }, [rules, query])

  const columns: DataTableColumn<AlertRule>[] = [
    {
      key: 'name', header: '规则名称', widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'dimension', header: '维度/对象', widthTier: 'text-mid',
      cell: (row) => <span>{dimensionLabel(row.dimension)}{row.target_id ? ` · ${row.target_id}` : ' · 全部'}</span>,
      mobile: { role: 'meta', label: '维度' },
    },
    {
      key: 'period', header: '账期/对比', widthTier: 'text-short',
      cell: (row) => <span>{periodLabel(row.period_type)} · {row.compare_type === 'previous' ? '上一期' : row.compare_period}</span>,
      mobile: { role: 'meta', label: '账期' },
    },
    {
      key: 'threshold', header: '阈值', widthTier: 'amount',
      cell: (row) => (
        <span className="text-meta text-secondary">
          {row.amount_threshold ? `${row.amount_threshold} 元` : row.percent_threshold ? `${row.percent_threshold}%` : '未设'} · {directionLabel(row.direction)}
        </span>
      ),
      mobile: { role: 'meta', label: '阈值' },
    },
    {
      key: 'active', header: '启用', widthTier: 'text-short',
      cell: (row) => (
        <button
          type="button"
          role="switch"
          aria-checked={row.is_active}
          aria-label={row.is_active ? `停用${row.name}` : `启用${row.name}`}
          onClick={() => toggleMutation.mutate({ id: row.id, is_active: !row.is_active })}
          className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center"
        >
          <span className={cn('relative inline-flex h-6 w-11 items-center rounded-pill', row.is_active ? 'bg-action-primary' : 'bg-line-strong')}>
            <span className={cn('inline-block h-5 w-5 transform rounded-pill bg-surface shadow-card transition-transform', row.is_active ? 'translate-x-[22px]' : 'translate-x-0.5')} />
          </span>
        </button>
      ),
      mobile: { role: 'value', label: '启用' },
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="预警规则"
        description="规则只定义阈值与比较方式；运行结果在「预警」工作页查看。"
        actions={
          <div className="flex flex-wrap gap-2">
            <Link href="/alerts" className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <Bell size={15} aria-hidden /> 查看预警结果
            </Link>
            <button
              type="button"
              onClick={() => {
                const a = document.createElement('a')
                a.href = '/api/alerts/export'
                a.download = 'alert-rules.json'
                document.body.appendChild(a)
                a.click()
                a.remove()
              }}
              className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
            >
              <Download size={15} aria-hidden /> 导出规则
            </button>
            <Link href="/settings/automation/alerts/import-export" className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <Upload size={15} aria-hidden /> 导入规则
            </Link>
            <button type="button" onClick={() => { setEditing(null); setFormOpen(true) }} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
              <Plus size={16} aria-hidden /> 创建规则
            </button>
          </div>
        }
      />

      <DataTable
        label="预警规则列表"
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        loading={rulesQuery.isLoading}
        error={rulesQuery.error}
        onRetry={() => rulesQuery.refetch()}
        empty={{
          title: query ? '没有匹配的规则' : '暂无预警规则',
          description: query ? '换个关键词试试。' : '创建规则后，可在预警页运行检查。',
          action: <button type="button" onClick={() => { setEditing(null); setFormOpen(true) }} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">创建规则</button>,
        }}
        toolbar={
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="relative w-full max-w-[320px]">
              <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
              <input type="text" inputMode="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索规则名称" aria-label="搜索规则名称" className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted" />
            </div>
            <p className="pf-num text-meta text-muted">共 {rules.length} 条规则</p>
          </div>
        }
        rowActions={(row) => (
          <>
            <button type="button" onClick={() => { setEditing(row); setFormOpen(true) }} aria-label={`编辑${row.name}`} title="编辑" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"><Pencil size={16} aria-hidden /></button>
            <button type="button" onClick={() => setDeleting(row)} aria-label={`删除${row.name}`} title="删除" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"><Trash2 size={16} aria-hidden /></button>
          </>
        )}
      />

      {formOpen && <RuleDialog rule={editing} onClose={() => setFormOpen(false)} />}

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
        title={deleting ? `删除预警规则「${deleting.name}」？` : '删除预警规则？'}
        description="删除后不可恢复；已产生的检查结果会保留。"
        confirmLabel="删除规则"
        danger
        loading={deleteMutation.isPending}
      >
        <div className="space-y-3">
          {deleteError && <InlineError>{deleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteReason}
            onChange={setDeleteReason}
            hint="删除会留下谁在何时删了哪条规则。"
          />
        </div>
      </ConfirmDialog>
    </div>
  )
}