'use client'

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dynamic from 'next/dynamic'
import { Tabs, message } from 'antd'
import { Download, Edit3, Filter, History, Plus, Settings, Trash2 } from 'lucide-react'
import dayjs from 'dayjs'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { formatDate } from '@/lib/format'
import { formatDateTime, formatFileSize } from '@/lib/format'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { DataTable, actionsWidth } from '@/components/ui/DataTable'
import { InlineError } from '@/components/ui/States'
import { Money } from '@/components/ui/Money'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import type { DataTableColumn } from '@/components/ui'
import { DateField, SelectField } from '@/components/form'
import { isDownloadRestrictedWebView } from '@/lib/platform'
import type { AutoExportRule, AutoExportRuleCreate, ExportHistory, ExportPreviewRow } from '@/types'

const LazyConfirmDialog = dynamic(
  () => import('@/components/ui/ConfirmDialog').then((m) => m.ConfirmDialog),
  { ssr: false },
)
const LazyRuleDialog = dynamic(
  () => import('./RuleDialog').then((m) => m.RuleDialog),
  { ssr: false },
)

const LazyFormatGuide = dynamic(
  () => import('@/components/import-export/FormatGuide').then((m) => m.FormatGuide),
  {
    ssr: false,
    loading: () => <div className="h-36 animate-pulse rounded-card bg-subtle" aria-label="格式说明加载中" />,
  },
)

type ExportPeriod = 'all' | 'year' | 'quarter' | 'month' | 'day' | 'custom'

interface ExportFilters {
  period: ExportPeriod
  year: number | ''
  quarter: number | ''
  month: number | ''
  day: number | ''
  startDate: string
  endDate: string
}

const PERIOD_OPTIONS = [
  { value: 'all', label: '全部数据' },
  { value: 'year', label: '按年' },
  { value: 'quarter', label: '按季度' },
  { value: 'month', label: '按月' },
  { value: 'day', label: '按日' },
  { value: 'custom', label: '自定义时间' },
]

export default function ExportPage() {
  const queryClient = useQueryClient()
  const currentYear = new Date().getFullYear()
  const [activeTab, setActiveTab] = useState('export')
  const [filters, setFilters] = useState<ExportFilters>({
    period: 'all', year: currentYear, quarter: '', month: '', day: '', startDate: '', endDate: '',
  })
  const [isExporting, setIsExporting] = useState(false)
  const [downloadRestricted, setDownloadRestricted] = useState(false)

  const [historyPage, setHistoryPage] = useState(1)
  const [historyPageSize, setHistoryPageSize] = useState(10)
  const [historyType, setHistoryType] = useState('')

  const [ruleDialogOpen, setRuleDialogOpen] = useState(false)
  const [editingRule, setEditingRule] = useState<AutoExportRule | null>(null)
  const [deletingRule, setDeletingRule] = useState<AutoExportRule | null>(null)
  /** 删除框内的提示（缺理由时出现；弹窗不关）。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)

  // 理由草稿按规则保留：关窗不清、换一条规则才清
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deletingRule ? deletingRule.id : null)

  useEffect(() => {
    setDownloadRestricted(isDownloadRestrictedWebView())
  }, [])

  const buildQueryParams = useMemo(() => {
    const params: Record<string, string> = { period_type: filters.period }
    if (filters.year) params.year = String(filters.year)
    if (filters.quarter) params.quarter = String(filters.quarter)
    if (filters.month) params.month = String(filters.month)
    if (filters.day) params.day = String(filters.day)
    if (filters.period === 'custom') {
      if (filters.startDate) params.date_from = filters.startDate
      if (filters.endDate) params.date_to = filters.endDate
    }
    return params
  }, [filters])

  const { data: preview, isLoading: isPreviewLoading } = useQuery({
    queryKey: ['exportPreview', buildQueryParams],
    queryFn: () => api.previewExport(buildQueryParams),
    enabled: filters.period !== 'all' && (filters.period !== 'custom' || (!!filters.startDate && !!filters.endDate)),
  })

  const { data: exportHistory, isLoading: isHistoryLoading } = useQuery({
    queryKey: ['exportHistory', historyPage, historyPageSize, historyType],
    queryFn: () => api.getExportHistory({
      page: String(historyPage),
      page_size: String(historyPageSize),
      ...(historyType ? { export_type: historyType } : {}),
    }),
  })

  const { data: autoExportRules = [], isLoading: isRulesLoading } = useQuery({
    queryKey: ['autoExportRules'],
    queryFn: () => api.getAutoExportRules(),
  })

  const saveRuleMutation = useMutation({
    mutationFn: (values: AutoExportRuleCreate & { id?: number }) =>
      values.id ? api.updateAutoExportRule(values.id, values) : api.createAutoExportRule(values),
    onSuccess: () => {
      message.success(editingRule ? '规则更新成功' : '规则创建成功')
      setRuleDialogOpen(false)
      setEditingRule(null)
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    },
    onError: (error: Error) => message.error(error.message || '操作失败'),
  })

  const deleteRuleMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) => api.deleteAutoExportRule(id, audit),
    onSuccess: () => {
      message.success('规则删除成功')
      setDeletingRule(null)
      setDeleteError(null)
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    },
    onError: (error: Error) => {
      // 缺理由（422）不是删除失败：把弹窗和已选的理由都留着，补一句再点一次。
      if (isAuditReasonRequired(error)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(error.message || '删除失败')
    },
  })

  const toggleRuleMutation = useMutation({
    mutationFn: (rule: AutoExportRule) => api.updateAutoExportRule(rule.id, { is_active: !rule.is_active }),
    onSuccess: () => {
      message.success('规则状态已更新')
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    },
    onError: () => message.error('操作失败'),
  })

  const isExportDisabled = useMemo(() => {
    if (isExporting || downloadRestricted) return true
    if (filters.period === 'all') return false
    if (filters.period === 'custom') {
      return !filters.startDate || !filters.endDate || dayjs(filters.endDate).isBefore(dayjs(filters.startDate))
    }
    return !preview
  }, [isExporting, downloadRestricted, filters, preview])

  const getExportButtonText = () => {
    if (downloadRestricted) return 'WebView 暂不支持导出'
    if (filters.period === 'custom') {
      if (!filters.startDate || !filters.endDate) return '请选择日期范围'
      if (dayjs(filters.endDate).isBefore(dayjs(filters.startDate))) return '结束日期不能早于开始日期'
    }
    return isExporting ? '导出中…' : '导出 CSV'
  }

  const years = useMemo(() => Array.from({ length: 6 }, (_, i) => currentYear - i), [currentYear])
  const months = useMemo(() => Array.from({ length: 12 }, (_, i) => i + 1), [])
  const quarters = useMemo(() => [1, 2, 3, 4], [])
  const days = useMemo(() => {
    if (!filters.year || !filters.month) return []
    return Array.from({ length: new Date(filters.year, filters.month, 0).getDate() }, (_, i) => i + 1)
  }, [filters.year, filters.month])

  const handleExport = async () => {
    if (downloadRestricted) return
    setIsExporting(true)
    try {
      const operator = localStorage.getItem('username') || '当前用户'
      const blob = await api.downloadExport(buildQueryParams, operator)
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = preview?.filename || 'assets_export.csv'
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
      message.success('导出成功')
      queryClient.invalidateQueries({ queryKey: ['exportHistory'] })
    } catch {
      message.error('导出失败，请重试')
    } finally {
      setIsExporting(false)
    }
  }

  const getPeriodLabel = () => {
    switch (filters.period) {
      case 'all': return '全部数据'
      case 'year': return `${filters.year}年`
      case 'quarter': return `${filters.year}年 Q${filters.quarter}`
      case 'month': return `${filters.year}年 ${filters.month}月`
      case 'day': return `${filters.year}年 ${filters.month}月 ${filters.day}日`
      case 'custom': return `${filters.startDate} 至 ${filters.endDate}`
      default: return ''
    }
  }

  const openCreateRule = () => {
    setEditingRule(null)
    setRuleDialogOpen(true)
  }
  const openEditRule = (rule: AutoExportRule) => {
    setEditingRule(rule)
    setRuleDialogOpen(true)
  }

  const historyColumns: DataTableColumn<ExportHistory>[] = [
    {
      key: 'export_time', header: '导出时间', widthTier: 'datetime',
      cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{formatDateTime(row.export_time)}</span>,
      mobile: { role: 'meta', label: '导出时间' },
    },
    {
      key: 'export_type', header: '类型', widthTier: 'text-mid',
      cell: (row) => (
        <span className="rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">
          {row.export_type === 'manual' ? '手动' : '自动'}{row.rule_name ? ` · ${row.rule_name}` : ''}
        </span>
      ),
      mobile: { role: 'meta', label: '类型' },
    },
    {
      // 文件名是 note 档：单行 + 悬停全文（spec P4 / D5）。白名单按**每张表各自的
      // aria-label** 登记（`table_width_audit.py --emit-ellipsis-allowlist` 已覆盖同文件的
      // 「导出历史」，spec §6.3 R3.6）；合法省略双条件仍为 `ellipsis: true` + DOM `title`。
      key: 'filename', header: '文件名', widthTier: 'note', ellipsis: true,
      cell: (row) => <span className="block truncate font-medium text-ink" title={row.filename}>{row.filename}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'file_size', header: '大小', align: 'right', widthTier: 'count',
      cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{row.file_size ? formatFileSize(row.file_size) : '—'}</span>,
      mobile: { role: 'value', label: '大小' },
    },
    {
      key: 'operator', header: '操作人', widthTier: 'label',
      cell: (row) => <span className="text-secondary">{row.operator || '—'}</span>,
      mobile: { role: 'meta', label: '操作人' },
    },
  ]

  const previewColumns: DataTableColumn<ExportPreviewRow>[] = [
    { key: 'asset_date', header: '日期', widthTier: 'date', cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{formatDate(row.asset_date)}</span>, mobile: { role: 'meta', label: '日期' } },
    { key: 'asset_name', header: '资产名称', widthTier: 'text-mid', cell: (row) => <span className="font-medium text-ink">{row.asset_name}</span>, mobile: { role: 'title' } },
    { key: 'asset_class', header: '资产类别', widthTier: 'text-mid', cell: (row) => <span className="text-secondary" title={row.asset_class || undefined}>{row.asset_class || '—'}</span>, mobile: { role: 'meta', label: '资产类别' } },
    { key: 'liquidity_days', header: '流动性(天)', widthTier: 'text-short', cell: (row) => <span className="whitespace-nowrap rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">{row.liquidity_days === '' ? '—' : `${row.liquidity_days} 天`}</span>, mobile: { role: 'meta', label: '流动性' } },
    { key: 'institution', header: '机构', widthTier: 'label', cell: (row) => <span className="text-secondary">{row.institution || '—'}</span>, mobile: { role: 'meta', label: '机构' } },
    { key: 'channel', header: '渠道', widthTier: 'label', cell: (row) => <span className="text-secondary">{row.channel || '—'}</span>, mobile: { role: 'meta', label: '渠道' } },
    { key: 'owner', header: '归属', widthTier: 'label', cell: (row) => <span className="text-secondary">{row.owner || '—'}</span>, mobile: { role: 'meta', label: '归属' } },
    { key: 'amount', header: '金额', align: 'right', widthTier: 'amount', cell: (row) => <Money value={row.amount} unit="cny" size="s" />, mobile: { role: 'value', label: '金额' } },
  ]

  const ruleColumns: DataTableColumn<AutoExportRule>[] = [
    { key: 'name', header: '规则名称', widthTier: 'text-mid', cell: (row) => <span className="font-medium text-ink">{row.name}</span>, mobile: { role: 'title' } },
    { key: 'cron', header: 'Cron', widthTier: 'label', cell: (row) => <code className="rounded bg-subtle px-2 py-0.5 text-meta text-secondary">{row.cron_expression}</code>, mobile: { role: 'meta', label: 'Cron' } },
    { key: 'format', header: '格式', widthTier: 'text-short', cell: (row) => <span className="text-secondary">{row.export_format.toUpperCase()}</span>, mobile: { role: 'meta', label: '格式' } },
    { key: 'last_run_at', header: '上次执行', widthTier: 'datetime', cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{row.last_run_at ? formatDateTime(row.last_run_at) : '—'}</span>, mobile: { role: 'meta', label: '上次执行' } },
    { key: 'status', header: '状态', widthTier: 'text-short', cell: (row) => <span className={row.is_active ? 'whitespace-nowrap rounded-pill bg-status-good-surface px-2 py-0.5 text-meta text-status-good-text' : 'whitespace-nowrap rounded-pill bg-subtle px-2 py-0.5 text-meta text-muted'}>{row.is_active ? '启用' : '禁用'}</span>, mobile: { role: 'meta', label: '状态' } },
  ]

  const exportTab = (
    <div className="space-y-4">
      <div className="rounded-card border border-line bg-surface p-4 shadow-card tablet:p-6">
        <div className="flex items-center gap-2">
          <Filter size={18} className="text-secondary" aria-hidden />
          <h2 className="text-h3 font-semibold text-ink">导出筛选</h2>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 tablet:grid-cols-2 xl:grid-cols-4">
          <SelectField label="账期类型" value={filters.period} onChange={(value) => setFilters({ period: value as ExportPeriod, year: currentYear, quarter: '', month: '', day: '', startDate: '', endDate: '' })} options={PERIOD_OPTIONS} />
          {filters.period === 'custom' ? (
            <>
              <DateField label="开始日期" value={filters.startDate} onChange={(value) => setFilters({ ...filters, startDate: value })} required />
              <DateField label="结束日期" value={filters.endDate} onChange={(value) => setFilters({ ...filters, endDate: value })} required />
            </>
          ) : filters.period !== 'all' ? (
            <SelectField label="年份" value={String(filters.year)} onChange={(value) => setFilters({ ...filters, year: Number(value), quarter: '', month: '', day: '' })} options={years.map((y) => ({ value: String(y), label: `${y}年` }))} />
          ) : null}
          {filters.period === 'quarter' && (
            <SelectField label="季度" value={String(filters.quarter)} onChange={(value) => setFilters({ ...filters, quarter: value ? Number(value) : '' })} placeholder="选择季度" options={quarters.map((q) => ({ value: String(q), label: `第${q}季度` }))} />
          )}
          {(filters.period === 'month' || filters.period === 'day') && (
            <SelectField label="月份" value={String(filters.month)} onChange={(value) => setFilters({ ...filters, month: value ? Number(value) : '', day: '' })} placeholder="选择月份" options={months.map((m) => ({ value: String(m), label: `${m}月` }))} />
          )}
          {filters.period === 'day' && (
            <SelectField label="日期" value={String(filters.day)} onChange={(value) => setFilters({ ...filters, day: value ? Number(value) : '' })} placeholder="选择日期" options={days.map((d) => ({ value: String(d), label: `${d}日` }))} />
          )}
        </div>
        <div className="mt-6 flex flex-col gap-3 border-t border-line pt-4 tablet:flex-row tablet:items-center tablet:justify-between">
          <p className="text-body text-secondary">
            {filters.period === 'all' ? '将导出全部数据' : preview ? <>将导出 <strong className="text-ink">{getPeriodLabel()}</strong>，共 <strong className="pf-num text-brand-700">{preview.total_count}</strong> 条记录</> : isPreviewLoading ? '正在计算…' : '请选择完整的筛选条件'}
          </p>
          <button
            type="button"
            onClick={handleExport}
            disabled={isExportDisabled}
            className="inline-flex min-h-[44px] items-center justify-center gap-2 rounded-control bg-action-primary px-5 text-body-strong font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download size={17} aria-hidden />
            {getExportButtonText()}
          </button>
        </div>
        {downloadRestricted && (
          <div className="mt-3">
            <InlineError>
              当前在 WebView / 小程序内，文件下载可能被宿主拦截。已在当前环境禁用导出；请用系统浏览器打开后重试。
            </InlineError>
          </div>
        )}
      </div>

      {preview && preview.preview.length > 0 && (
        <DataTable
          label="导出数据预览"
          rows={preview.preview}
          columns={previewColumns}
          rowKey={(row) => `${row.asset_date}_${row.asset_name}_${row.amount}`}
          toolbar={<p className="text-meta text-muted">显示前 {preview.preview.length} 条记录</p>}
        />
      )}
    </div>
  )

  const rulesTab = (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-body text-secondary">按 Cron 表达式定时生成导出文件。</p>
        <button type="button" onClick={openCreateRule} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
          <Plus size={16} aria-hidden /> 创建规则
        </button>
      </div>
      <DataTable
        label="自动导出规则"
        rows={autoExportRules}
        columns={ruleColumns}
        rowKey={(row) => row.id}
        loading={isRulesLoading}
        empty={{
          title: '暂无自动导出规则',
          description: '创建规则后，系统将按 Cron 自动生成导出文件。',
          action: <button type="button" onClick={openCreateRule} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">创建规则</button>,
        }}
        toolbar={<p className="pf-num text-meta text-muted">共 {autoExportRules.length} 条</p>}
        rowActionsWidth={actionsWidth(['icon', 'icon', 'icon'])}
        rowActions={(row) => (
          <>
            <button
              type="button"
              role="switch"
              aria-checked={row.is_active}
              aria-label={row.is_active ? `停用${row.name}` : `启用${row.name}`}
              onClick={() => toggleRuleMutation.mutate(row)}
              className="inline-flex h-11 w-11 items-center justify-center rounded-control"
            >
              <span className={`relative inline-flex h-6 w-11 items-center rounded-pill ${row.is_active ? 'bg-action-primary' : 'bg-line-strong'}`}>
                <span className={`inline-block h-5 w-5 transform rounded-pill bg-surface shadow-card transition-transform ${row.is_active ? 'translate-x-[22px]' : 'translate-x-0.5'}`} />
              </span>
            </button>
            <button type="button" onClick={() => openEditRule(row)} aria-label={`编辑${row.name}`} title="编辑" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"><Edit3 size={16} aria-hidden /></button>
            <button type="button" onClick={() => setDeletingRule(row)} aria-label={`删除${row.name}`} title="删除" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"><Trash2 size={16} aria-hidden /></button>
          </>
        )}
      />
    </div>
  )

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="数据导出"
        description="导出资产记录到 CSV 文件；也可按 Cron 配置自动导出规则。"
      />

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'export', label: <span className="inline-flex items-center gap-1"><Download size={16} aria-hidden /> 数据导出</span>, children: exportTab },
          { key: 'rules', label: <span className="inline-flex items-center gap-1"><Settings size={16} aria-hidden /> 自动导出规则</span>, children: rulesTab },
        ]}
      />

      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <History size={18} className="text-secondary" aria-hidden />
            <h2 className="text-h3 font-semibold text-ink">导出历史记录</h2>
          </div>
          <select
            value={historyType}
            onChange={(e) => { setHistoryType(e.target.value); setHistoryPage(1) }}
            aria-label="按导出类型筛选"
            className="min-h-[44px] rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
          >
            <option value="">全部类型</option>
            <option value="manual">手动导出</option>
            <option value="auto">自动导出</option>
          </select>
        </div>
        <DataTable
          label="导出历史"
          rows={exportHistory?.items ?? []}
          columns={historyColumns}
          rowKey={(row) => row.id}
          loading={isHistoryLoading}
          pagination={{
            page: historyPage,
            pageSize: historyPageSize,
            total: exportHistory?.total ?? 0,
            onPageChange: setHistoryPage,
            pageSizeOptions: [10, 20, 50],
            onPageSizeChange: (size) => { setHistoryPageSize(size); setHistoryPage(1) },
          }}
          empty={{ title: '暂无导出记录', description: '完成一次导出后，记录会出现在这里。' }}
        />
      </div>

      <LazyFormatGuide
        showDownloadButton
        onDownload={() => {
          void import('@/components/import-export/FormatGuide').then(({ downloadTemplate }) => {
            downloadTemplate()
            message.success('模板下载成功')
          })
        }}
      />
      <p className="text-meta text-muted">导出的 CSV 会在现有字段外增加 <code className="rounded bg-subtle px-1">tags</code> 列（多个标签以 <code className="rounded bg-subtle px-1">; </code> 分隔）；下载的模板字段不变。</p>

      {ruleDialogOpen && <LazyRuleDialog
        open
        rule={editingRule}
        pending={saveRuleMutation.isPending}
        onClose={() => { setRuleDialogOpen(false); setEditingRule(null) }}
        onSave={(values) => {
          if (editingRule) saveRuleMutation.mutate({ ...values, id: editingRule.id })
          else saveRuleMutation.mutate(values)
        }}
      />}

      {deletingRule && <LazyConfirmDialog
        open
        onClose={() => { setDeletingRule(null); setDeleteError(null) }}
        onConfirm={() => deleteRuleMutation.mutate({
          id: deletingRule.id,
          audit: toAuditReasonInput(deleteReason),
        })}
        title={`删除规则「${deletingRule.name}」？`}
        description="删除后不再自动执行；历史导出文件不受影响。"
        confirmLabel="删除规则"
        danger
        loading={deleteRuleMutation.isPending}
      >
        <div className="space-y-3">
          {deleteError && <InlineError>{deleteError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteReason}
            onChange={setDeleteReason}
            hint="删除会留下谁在何时删了哪一条自动导出规则。"
          />
        </div>
      </LazyConfirmDialog>}
    </PageContainer>
  )
}
