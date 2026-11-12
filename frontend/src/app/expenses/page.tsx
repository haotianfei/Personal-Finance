'use client'

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dynamic from 'next/dynamic'
import { Tabs, message } from 'antd'
import { CalendarPlus, Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { formatDate } from '@/lib/format'
import { FormField } from '@/components/form'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { DataTable, actionsWidth } from '@/components/ui/DataTable'
import { InlineError } from '@/components/ui/States'
import { KpiCard } from '@/components/ui/KpiCard'
import { Money } from '@/components/ui/Money'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import type { DataTableColumn, DataTableSort } from '@/components/ui'
import { cn } from '@/lib/utils'
import type {
  ExpenseRecord, ExpenseRecordCreate, ExpenseCategory, ExpenseType,
  FixedExpenseTemplate, FixedExpenseTemplateCreate, Institution,
} from '@/types'
import { CYCLE_LABELS, EXPENSE_TYPE_OPTIONS } from './constants'

const LazyFilterSheet = dynamic(
  () => import('@/components/ui/FilterSheet').then((m) => m.FilterSheet),
  { ssr: false },
)
const LazyConfirmDialog = dynamic(
  () => import('@/components/ui/ConfirmDialog').then((m) => m.ConfirmDialog),
  { ssr: false },
)
const LazyResponsiveDialog = dynamic(
  () => import('@/components/ui/ResponsiveDialog').then((m) => m.ResponsiveDialog),
  { ssr: false },
)

const LazyExpenseDialog = dynamic(
  () => import('./ExpenseDialogs').then((m) => m.ExpenseDialog),
  { ssr: false },
)
const LazyTemplateDialog = dynamic(
  () => import('./ExpenseDialogs').then((m) => m.TemplateDialog),
  { ssr: false },
)

const LazyGenerateFixedExpensePanel = dynamic(
  () => import('./GenerateFixedExpensePanel').then((m) => m.GenerateFixedExpensePanel),
  {
    ssr: false,
    loading: () => <div className="h-32 animate-pulse rounded-control bg-subtle" aria-label="生成面板加载中" />,
  },
)

const EXPENSE_TYPE_META: Record<ExpenseType, string> = {
  fixed: '固定支出',
  large_oneoff: '临时大额',
}

interface ExpenseFilters {
  name: string
  type: string
  categoryId: string
  accountId: string
  dateFrom: string
  dateTo: string
}

const EMPTY_EXPENSE_FILTERS: ExpenseFilters = {
  name: '', type: '', categoryId: '', accountId: '', dateFrom: '', dateTo: '',
}

const countFilters = (f: ExpenseFilters) =>
  (f.name ? 1 : 0) + (f.type ? 1 : 0) + (f.categoryId ? 1 : 0) +
  (f.accountId ? 1 : 0) + (f.dateFrom || f.dateTo ? 1 : 0)

const filterInputClass = 'min-h-[44px] w-full rounded-control border border-line-strong bg-surface px-3 text-body text-ink placeholder:text-muted'

function ExpenseFilterFields({
  draft,
  setDraft,
  categories,
  accounts,
}: {
  draft: ExpenseFilters
  setDraft: (next: ExpenseFilters) => void
  categories: ExpenseCategory[]
  accounts: Institution[]
}) {
  return (
    <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
      <FormField label="支出类型">
        <select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value })} className={filterInputClass}>
          <option value="">全部类型</option>
          {EXPENSE_TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </FormField>
      <FormField label="分类">
        <select value={draft.categoryId} onChange={(e) => setDraft({ ...draft, categoryId: e.target.value })} className={filterInputClass}>
          <option value="">全部分类</option>
          {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </FormField>
      <FormField label="账户">
        <select value={draft.accountId} onChange={(e) => setDraft({ ...draft, accountId: e.target.value })} className={filterInputClass}>
          <option value="">全部账户</option>
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </FormField>
      <div className="grid grid-cols-2 gap-2">
        <FormField label="开始日期">
          <input type="date" value={draft.dateFrom} onChange={(e) => setDraft({ ...draft, dateFrom: e.target.value })} className={filterInputClass} />
        </FormField>
        <FormField label="结束日期">
          <input type="date" value={draft.dateTo} onChange={(e) => setDraft({ ...draft, dateTo: e.target.value })} className={filterInputClass} />
        </FormField>
      </div>
    </div>
  )
}


export default function ExpensesPage() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('flow')

  const [expenseDialog, setExpenseDialog] = useState<{ open: boolean; record: ExpenseRecord | null }>({ open: false, record: null })
  const [templateDialog, setTemplateDialog] = useState<{ open: boolean; record: FixedExpenseTemplate | null }>({ open: false, record: null })
  const [deletingRecord, setDeletingRecord] = useState<ExpenseRecord | null>(null)
  const [deletingTemplate, setDeletingTemplate] = useState<FixedExpenseTemplate | null>(null)
  /** 两个删除框各自的提示（缺理由时出现；弹窗不关）。 */
  const [deleteRecordError, setDeleteRecordError] = useState<string | null>(null)
  const [deleteTemplateError, setDeleteTemplateError] = useState<string | null>(null)
  const [showGenerateModal, setShowGenerateModal] = useState(false)

  // 理由草稿按对象保留：关窗不清、换一条记录 / 一个项目才清
  const [deleteRecordReason, setDeleteRecordReason] = useAuditReasonDraft(deletingRecord ? deletingRecord.id : null)
  const [deleteTemplateReason, setDeleteTemplateReason] = useAuditReasonDraft(deletingTemplate ? deletingTemplate.id : null)

  const [filters, setFilters] = useState<ExpenseFilters>(EMPTY_EXPENSE_FILTERS)
  const [draftFilters, setDraftFilters] = useState<ExpenseFilters>(EMPTY_EXPENSE_FILTERS)
  const [filterOpen, setFilterOpen] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [sort, setSort] = useState<DataTableSort | null>({ key: 'expense_date', direction: 'desc' })

  const { data: summary } = useQuery({ queryKey: ['expenseSummary'], queryFn: () => api.getExpenseSummary() })
  const { data: categories = [] } = useQuery({ queryKey: ['expenseCategories'], queryFn: api.getExpenseCategories })
  // 支出账户＝机构（0027 起 expenses.account_id FK 到 institutions）
  const { data: accountsData } = useQuery({ queryKey: ['institutions'], queryFn: api.getInstitutions })
  const accounts: Institution[] = accountsData?.items ?? []
  const { data: templates = [] } = useQuery({ queryKey: ['expenseTemplates'], queryFn: () => api.getExpenseTemplates() })

  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((prev) => (prev.name === searchInput ? prev : { ...prev, name: searchInput }))
      setPage(1)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchInput])

  const expenseQuery = useQuery({
    queryKey: ['expenses', filters, page, pageSize],
    queryFn: () => api.getExpenses({
      page,
      page_size: pageSize,
      ...(filters.name ? { name: filters.name } : {}),
      ...(filters.type ? { expense_type: filters.type as ExpenseType } : {}),
      ...(filters.categoryId ? { category_id: Number(filters.categoryId) } : {}),
      ...(filters.accountId ? { account_id: Number(filters.accountId) } : {}),
      ...(filters.dateFrom ? { date_from: filters.dateFrom } : {}),
      ...(filters.dateTo ? { date_to: filters.dateTo } : {}),
    }),
  })
  const expenses = useMemo(() => expenseQuery.data?.items ?? [], [expenseQuery.data])
  const total = expenseQuery.data?.total ?? 0

  const sortedExpenses = useMemo(() => {
    if (!sort) return expenses
    return [...expenses].sort((a, b) => {
      let result = 0
      if (sort.key === 'expense_name') result = a.expense_name.localeCompare(b.expense_name, 'zh-CN')
      else if (sort.key === 'amount') result = Number(a.amount) - Number(b.amount)
      else result = a.expense_date.localeCompare(b.expense_date) || a.id - b.id
      return sort.direction === 'asc' ? result : -result
    })
  }, [expenses, sort])

  const [templateSort, setTemplateSort] = useState<DataTableSort | null>({ key: 'name', direction: 'asc' })
  const sortedTemplates = useMemo(() => {
    if (!templateSort) return templates
    return [...templates].sort((a, b) => {
      const result = templateSort.key === 'amount'
        ? Number(a.amount) - Number(b.amount)
        : a.name.localeCompare(b.name, 'zh-CN')
      return templateSort.direction === 'asc' ? result : -result
    })
  }, [templates, templateSort])

  const refreshExpenses = () => {
    queryClient.invalidateQueries({ queryKey: ['expenses'] })
    queryClient.invalidateQueries({ queryKey: ['expenseSummary'] })
  }
  const refreshTemplates = () => {
    queryClient.invalidateQueries({ queryKey: ['expenseTemplates'] })
    queryClient.invalidateQueries({ queryKey: ['expenseSummary'] })
  }

  const createExpenseMutation = useMutation({
    mutationFn: api.createExpense,
    onSuccess: () => {
      refreshExpenses()
      setExpenseDialog({ open: false, record: null })
      message.success('创建成功')
    },
    onError: (error: Error) => message.error('创建失败: ' + (error.message || '未知错误')),
  })
  const updateExpenseMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: ExpenseRecordCreate }) => api.updateExpense(id, data),
    onSuccess: () => {
      refreshExpenses()
      setExpenseDialog({ open: false, record: null })
      message.success('更新成功')
    },
    onError: (error: Error) => message.error('更新失败: ' + (error.message || '未知错误')),
  })
  const deleteExpenseMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) => api.deleteExpense(id, audit),
    onSuccess: () => {
      refreshExpenses()
      setDeletingRecord(null)
      setDeleteRecordError(null)
      message.success('删除成功')
    },
    onError: (error: Error) => {
      // 缺理由（422）不是删除失败：把弹窗和已选的理由都留着，补一句再点一次。
      if (isAuditReasonRequired(error)) {
        setDeleteRecordError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error('删除失败: ' + (error.message || '未知错误'))
    },
  })
  const createTemplateMutation = useMutation({
    mutationFn: api.createExpenseTemplate,
    onSuccess: () => {
      refreshTemplates()
      setTemplateDialog({ open: false, record: null })
      message.success('创建成功')
    },
    onError: (error: Error) => message.error('创建失败: ' + (error.message || '未知错误')),
  })
  const updateTemplateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: FixedExpenseTemplateCreate }) => api.updateExpenseTemplate(id, data),
    onSuccess: () => {
      refreshTemplates()
      setTemplateDialog({ open: false, record: null })
      message.success('更新成功')
    },
    onError: (error: Error) => message.error('更新失败: ' + (error.message || '未知错误')),
  })
  const deleteTemplateMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) => api.deleteExpenseTemplate(id, audit),
    onSuccess: () => {
      refreshTemplates()
      setDeletingTemplate(null)
      setDeleteTemplateError(null)
      message.success('删除成功，已生成的支出记录保留')
    },
    onError: (error: Error) => {
      // 同上：缺理由是「还没说为什么」，弹窗留着等人补一句。
      if (isAuditReasonRequired(error)) {
        setDeleteTemplateError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error('删除失败: ' + (error.message || '未知错误'))
    },
  })

  const filtersActive = countFilters(filters)

  const applyFilter = () => {
    setFilters(draftFilters)
    setSearchInput(draftFilters.name)
    setPage(1)
    setFilterOpen(false)
  }
  const resetFilter = () => {
    setDraftFilters(EMPTY_EXPENSE_FILTERS)
    setFilters(EMPTY_EXPENSE_FILTERS)
    setSearchInput('')
    setPage(1)
  }

  const expenseColumns: DataTableColumn<ExpenseRecord>[] = [
    {
      key: 'expense_date', header: '日期', sortable: true, widthTier: 'date',
      cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{formatDate(row.expense_date)}</span>,
      mobile: { role: 'meta', label: '日期' },
    },
    {
      key: 'expense_name', header: '名称', sortable: true, widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.expense_name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'expense_type', header: '类型', widthTier: 'text-short',
      cell: (row) => <span className="whitespace-nowrap rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">{EXPENSE_TYPE_META[row.expense_type]}</span>,
      mobile: { role: 'meta', label: '类型' },
    },
    {
      key: 'amount', header: '金额', sortable: true, align: 'right', widthTier: 'amount',
      cell: (row) => <Money value={row.amount} unit="cny" size="s" />,
      mobile: { role: 'value', label: '金额' },
    },
    {
      key: 'category_name', header: '分类', widthTier: 'label',
      cell: (row) => <span className="text-secondary">{row.category_name || '—'}</span>,
      mobile: { role: 'meta', label: '分类' },
    },
    {
      key: 'account_name', header: '账户', widthTier: 'label',
      cell: (row) => <span className="text-secondary">{row.account_name || '—'}</span>,
      mobile: { role: 'meta', label: '账户' },
    },
    {
      // 备注是 note 档：唯一允许「单行 + 悬停全文」的档（spec P4）。ellipsis 与
      // 单元格里的 title 两者都满足才算合法省略（A4 判据），缺一不可。
      // 同文件两张表各自登记（`table_width_audit.py` 按每张表的 aria-label 抽取白名单，
      // spec §6.3 R3.6），所以「固定支出项目」这张表也能声明，不需要退化成换行。
      key: 'note', header: '备注', widthTier: 'note', ellipsis: true,
      cell: (row) => <span className="block truncate text-meta text-muted" title={row.note ?? undefined}>{row.note || '—'}</span>,
      mobile: { role: 'meta', label: '备注' },
    },
  ]

  const templateColumns: DataTableColumn<FixedExpenseTemplate>[] = [
    {
      key: 'name', header: '项目名称', sortable: true, widthTier: 'text-mid',
      cell: (row) => <span className="font-medium text-ink">{row.name}</span>,
      mobile: { role: 'title' },
    },
    {
      key: 'amount', header: '预期金额', sortable: true, align: 'right', widthTier: 'amount',
      cell: (row) => <Money value={row.amount} unit="cny" size="s" />,
      mobile: { role: 'value', label: '预期金额' },
    },
    {
      key: 'cycle', header: '周期', widthTier: 'text-short',
      cell: (row) => <span className="text-secondary">{CYCLE_LABELS[row.cycle]}</span>,
      mobile: { role: 'meta', label: '周期' },
    },
    {
      key: 'period', header: '起止', widthTier: 'datetime',
      cell: (row) => <span className="whitespace-nowrap text-meta text-secondary">{row.start_date} ~ {row.end_date || '至今'}</span>,
      mobile: { role: 'meta', label: '起止' },
    },
    {
      key: 'category_name', header: '分类', widthTier: 'label',
      cell: (row) => <span className="text-secondary">{row.category_name || '—'}</span>,
      mobile: { role: 'meta', label: '分类' },
    },
    {
      key: 'account_name', header: '账户', widthTier: 'label',
      cell: (row) => <span className="text-secondary">{row.account_name || '—'}</span>,
      mobile: { role: 'meta', label: '账户' },
    },
    {
      key: 'active', header: '状态', widthTier: 'text-short',
      cell: (row) => <span className={cn('whitespace-nowrap rounded-pill px-2 py-0.5 text-meta', row.active ? 'bg-status-good-surface text-status-good-text' : 'bg-subtle text-muted')}>{row.active ? '启用' : '停用'}</span>,
      mobile: { role: 'meta', label: '状态' },
    },
    {
      // 备注是 note 档：单行 + 悬停全文（spec P4 / D5）。白名单按**每张表各自的 aria-label**
      // 登记（`table_width_audit.py --emit-ellipsis-allowlist` 已覆盖同文件的第二张表，
      // spec §6.3 R3.6），所以这里与「支出流水」一样可以声明 ellipsis；
      // 合法省略的双条件仍是「配置 ellipsis: true + DOM 给 title」，缺一即 FAIL。
      key: 'note', header: '备注', widthTier: 'note', ellipsis: true,
      cell: (row) => <span className="block truncate text-meta text-muted" title={row.note ?? undefined}>{row.note || '—'}</span>,
      mobile: { role: 'hidden' },
    },
  ]

  const flowTab = (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <KpiCard label="本月支出合计" value={summary?.month.total} meta={summary ? `${summary.month.period} · ${summary.month.count} 笔` : undefined} />
        <KpiCard label="本月固定支出" value={summary?.month.fixed_total} />
        <KpiCard label="本月大额支出" value={summary?.month.large_total} />
        <KpiCard label="固定支出月负担" value={summary?.fixed_monthly_burden} meta="有效项目归一化到每月" />
      </div>

      <DataTable
        label="支出流水"
        rows={sortedExpenses}
        columns={expenseColumns}
        rowKey={(row) => row.id}
        loading={expenseQuery.isLoading}
        error={expenseQuery.error}
        onRetry={() => expenseQuery.refetch()}
        sort={sort}
        onSortChange={setSort}
        pagination={{
          page, pageSize, total,
          onPageChange: setPage,
          pageSizeOptions: [10, 20, 50, 100],
          onPageSizeChange: (size) => { setPageSize(size); setPage(1) },
        }}
        empty={{
          title: filtersActive ? '没有匹配的支出' : '暂无支出记录',
          description: filtersActive ? '换个筛选条件试试。' : '新增一笔支出，或先生成当月固定支出。',
          action: <button type="button" onClick={() => setExpenseDialog({ open: true, record: null })} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">新增支出</button>,
        }}
        toolbar={
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[180px] flex-1 tablet:max-w-[300px]">
                <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
                <input
                  type="text"
                  inputMode="search"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="搜索支出名称"
                  aria-label="搜索支出名称"
                  className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
                />
              </div>
              <button type="button" onClick={() => { setDraftFilters(filters); setFilterOpen(true) }} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink tablet:hidden">
                筛选{filtersActive > 0 ? `（${filtersActive}）` : ''}
              </button>
              <div className="hidden flex-wrap items-center gap-2 tablet:flex">
                <select value={filters.type} onChange={(e) => { setFilters({ ...filters, type: e.target.value }); setPage(1) }} aria-label="按类型筛选" className="min-h-[44px] rounded-control border border-line-strong bg-surface px-2 text-meta text-ink">
                  <option value="">全部类型</option>
                  {EXPENSE_TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                <select value={filters.categoryId} onChange={(e) => { setFilters({ ...filters, categoryId: e.target.value }); setPage(1) }} aria-label="按分类筛选" className="min-h-[44px] max-w-[140px] rounded-control border border-line-strong bg-surface px-2 text-meta text-ink">
                  <option value="">全部分类</option>
                  {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
                <select value={filters.accountId} onChange={(e) => { setFilters({ ...filters, accountId: e.target.value }); setPage(1) }} aria-label="按账户筛选" className="min-h-[44px] max-w-[140px] rounded-control border border-line-strong bg-surface px-2 text-meta text-ink">
                  <option value="">全部账户</option>
                  {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
                <input type="date" value={filters.dateFrom} onChange={(e) => { setFilters({ ...filters, dateFrom: e.target.value }); setPage(1) }} aria-label="开始日期" className="min-h-[44px] rounded-control border border-line-strong bg-surface px-2 text-meta text-ink" />
                <span className="text-muted">–</span>
                <input type="date" value={filters.dateTo} onChange={(e) => { setFilters({ ...filters, dateTo: e.target.value }); setPage(1) }} aria-label="结束日期" className="min-h-[44px] rounded-control border border-line-strong bg-surface px-2 text-meta text-ink" />
                {filtersActive > 0 && <button type="button" onClick={resetFilter} className="text-meta text-link">清除筛选</button>}
              </div>
              <span className="ml-auto pf-num text-meta text-muted">共 {total} 笔</span>
            </div>
            <p className="hidden text-meta text-muted md:block lg:hidden">左右滑动查看完整表格。</p>
          </div>
        }
        rowActionsWidth={actionsWidth(['icon', 'icon'])}
        rowActions={(row) => (
          <>
            <button type="button" onClick={() => setExpenseDialog({ open: true, record: row })} aria-label={`编辑${row.expense_name}`} title="编辑" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"><Pencil size={16} aria-hidden /></button>
            <button type="button" onClick={() => setDeletingRecord(row)} aria-label={`删除${row.expense_name}`} title="删除" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"><Trash2 size={16} aria-hidden /></button>
          </>
        )}
      />

      {deletingRecord && <LazyConfirmDialog
        open
        onClose={() => { setDeletingRecord(null); setDeleteRecordError(null) }}
        onConfirm={() => deleteExpenseMutation.mutate({
          id: deletingRecord.id,
          audit: toAuditReasonInput(deleteRecordReason),
        })}
        title={`删除支出「${deletingRecord.expense_name}」？`}
        description="删除后不可恢复。"
        confirmLabel="删除支出"
        danger
        loading={deleteExpenseMutation.isPending}
      >
        <div className="space-y-3">
          {deleteRecordError && <InlineError>{deleteRecordError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteRecordReason}
            onChange={setDeleteRecordReason}
            hint="删除会留下谁在何时删了哪一笔支出。"
          />
        </div>
      </LazyConfirmDialog>}
    </div>
  )

  const templatesTab = (
    <div className="space-y-4">
      <DataTable
        label="固定支出项目"
        rows={sortedTemplates}
        columns={templateColumns}
        rowKey={(row) => row.id}
        sort={templateSort}
        onSortChange={setTemplateSort}
        empty={{
          title: '暂无固定支出项目',
          description: '新增项目后，可在支出口流水一键生成当月记录。',
          action: <button type="button" onClick={() => setTemplateDialog({ open: true, record: null })} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">新增项目</button>,
        }}
        toolbar={<p className="pf-num text-meta text-muted">共 {templates.length} 个</p>}
        rowActionsWidth={actionsWidth(['icon', 'icon'])}
        rowActions={(row) => (
          <>
            <button type="button" onClick={() => setTemplateDialog({ open: true, record: row })} aria-label={`编辑${row.name}`} title="编辑" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"><Pencil size={16} aria-hidden /></button>
            <button type="button" onClick={() => setDeletingTemplate(row)} aria-label={`删除${row.name}`} title="删除" className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"><Trash2 size={16} aria-hidden /></button>
          </>
        )}
      />
      {deletingTemplate && <LazyConfirmDialog
        open
        onClose={() => { setDeletingTemplate(null); setDeleteTemplateError(null) }}
        onConfirm={() => deleteTemplateMutation.mutate({
          id: deletingTemplate.id,
          audit: toAuditReasonInput(deleteTemplateReason),
        })}
        title={`删除项目「${deletingTemplate.name}」？`}
        description="已生成的支出记录会保留（解绑模板）。此操作不可恢复。"
        confirmLabel="删除项目"
        danger
        loading={deleteTemplateMutation.isPending}
      >
        <div className="space-y-3">
          {deleteTemplateError && <InlineError>{deleteTemplateError}</InlineError>}
          <AuditReasonField
            label="为什么删"
            value={deleteTemplateReason}
            onChange={setDeleteTemplateReason}
            hint="删除会留下谁在何时删了哪一个固定支出项目。"
          />
        </div>
      </LazyConfirmDialog>}
    </div>
  )

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title={activeTab === 'flow' ? '支出记录' : '固定支出项目'}
        description={activeTab === 'flow'
          ? '记录固定支出与临时大额支出；支出为流水，不参与净值计算。'
          : '维护周期性支出项目，可在支出口流水一键生成当月记录。'}
        actions={activeTab === 'flow' ? (
          <>
            <button type="button" onClick={() => setShowGenerateModal(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <CalendarPlus size={16} aria-hidden /> 生成固定支出
            </button>
            <button type="button" onClick={() => setExpenseDialog({ open: true, record: null })} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
              <Plus size={16} aria-hidden /> 新增支出
            </button>
          </>
        ) : (
          <button type="button" onClick={() => setTemplateDialog({ open: true, record: null })} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
            <Plus size={16} aria-hidden /> 新增项目
          </button>
        )}
      />

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'flow', label: '支出口流水', children: flowTab },
          { key: 'templates', label: '固定支出项目', children: templatesTab },
        ]}
      />

      {filterOpen && <LazyFilterSheet open onClose={() => setFilterOpen(false)} onReset={resetFilter} onApply={applyFilter} applyLabel={`应用筛选${countFilters(draftFilters) ? `（${countFilters(draftFilters)}）` : ''}`}>
        <FormField label="名称">
          <input type="text" value={draftFilters.name} onChange={(e) => setDraftFilters({ ...draftFilters, name: e.target.value })} placeholder="支出名称关键词" className={filterInputClass} />
        </FormField>
        <ExpenseFilterFields draft={draftFilters} setDraft={setDraftFilters} categories={categories} accounts={accounts} />
      </LazyFilterSheet>}

      {expenseDialog.open && <LazyExpenseDialog
        open
        record={expenseDialog.record}
        categories={categories}
        accounts={accounts}
        pending={createExpenseMutation.isPending || updateExpenseMutation.isPending}
        onClose={() => setExpenseDialog({ open: false, record: null })}
        onSubmit={(data) => {
          if (expenseDialog.record) updateExpenseMutation.mutate({ id: expenseDialog.record.id, data })
          else createExpenseMutation.mutate(data)
        }}
      />}

      {templateDialog.open && <LazyTemplateDialog
        open
        record={templateDialog.record}
        categories={categories}
        accounts={accounts}
        pending={createTemplateMutation.isPending || updateTemplateMutation.isPending}
        onClose={() => setTemplateDialog({ open: false, record: null })}
        onSubmit={(data) => {
          if (templateDialog.record) updateTemplateMutation.mutate({ id: templateDialog.record.id, data })
          else createTemplateMutation.mutate(data)
        }}
      />}

      {showGenerateModal && <LazyResponsiveDialog
        open
        onClose={() => setShowGenerateModal(false)}
        title="生成固定支出"
        description="按账期汇总启用的固定支出项目，预览后确认生成。"
        size="lg"
      >
        <LazyGenerateFixedExpensePanel
          onClose={() => setShowGenerateModal(false)}
          onGenerated={() => queryClient.invalidateQueries({ queryKey: ['expenses'] })}
        />
      </LazyResponsiveDialog>}
    </PageContainer>
  )
}
