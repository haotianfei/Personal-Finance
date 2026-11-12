'use client'

import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Tooltip, message } from 'antd'
import { Eraser, Eye, Filter, Search, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { formatDate, formatFileSize } from '@/lib/format'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { DataTable, actionsWidth } from '@/components/ui/DataTable'
import { InlineError } from '@/components/ui/States'
import { DateField, SelectField } from '@/components/form'
import {
  AuditReasonField,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import { EvidenceDetail } from '@/components/evidence/EvidenceDetail'
import { EvidenceImage } from '@/components/evidence/EvidenceImage'
import { engineLabel } from '@/components/evidence/shared'
import type { DataTableColumn } from '@/components/ui'
import type { EvidenceListItem, EvidencePurgeResult } from '@/types'

const LazyConfirmDialog = dynamic(
  () => import('@/components/ui/ConfirmDialog').then((m) => m.ConfirmDialog),
  { ssr: false },
)
const LazyResponsiveDialog = dynamic(
  () => import('@/components/ui/ResponsiveDialog').then((m) => m.ResponsiveDialog),
  { ssr: false },
)

type UsageFilter = 'all' | 'used' | 'unused'

interface EvidenceFilters {
  sourceGroupId: string
  dateFrom: string
  dateTo: string
  usage: UsageFilter
  q: string
}

const EMPTY_FILTERS: EvidenceFilters = {
  sourceGroupId: '', dateFrom: '', dateTo: '', usage: 'all', q: '',
}

const USAGE_OPTIONS = [
  { value: 'used', label: '已使用' },
  { value: 'unused', label: '未使用' },
]

/**
 * `file_path`（`2026/11/20261105-164012_sg1_e9de4e24a00e.jpg`）→ basename。
 * 服务端新命名（ADR-0022 D1）后文件名本身就是可读标识，故 `original_filename`
 * 为 NULL 时（迁移 0032 清掉的 `shot_upload_*` 脏名）用它兜底，不留空白单元格。
 */
function fileNameOf(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

export default function EvidencePage() {
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState<EvidenceFilters>(EMPTY_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const [detail, setDetail] = useState<EvidenceListItem | null>(null)
  const [deleting, setDeleting] = useState<EvidenceListItem | null>(null)
  /** 删除框 / 批量清理框内的提示：缺理由时贴在这里，弹窗不关、已选理由不丢。 */
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [purgeError, setPurgeError] = useState<string | null>(null)

  // 批量清理：两段式（dry_run=true 出清单 → 用户确认后才 dry_run=false）
  const [purgeOpen, setPurgeOpen] = useState(false)
  // 点击时的范围快照：弹窗打开期间即使用户改筛选，也不改变这次清理的破坏范围
  const [purgeScope, setPurgeScope] = useState<number | undefined>(undefined)
  const [purgePreview, setPurgePreview] = useState<EvidencePurgeResult | null>(null)
  const [purgeResult, setPurgeResult] = useState<EvidencePurgeResult | null>(null)

  // 理由草稿：单条按凭证明细 id、批量按清理范围（'all' = 全部来源组）分槽保留，
  // 关窗不清、换对象才清。
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deleting ? deleting.id : null)
  const [purgeReason, setPurgeReason] = useAuditReasonDraft(purgeOpen ? purgeScope ?? 'all' : null)

  const filtersActive = useMemo(
    () => (Object.keys(EMPTY_FILTERS) as Array<keyof EvidenceFilters>).some(
      (key) => filters[key] !== EMPTY_FILTERS[key],
    ),
    [filters],
  )
  const dateRangeInvalid = !!filters.dateFrom && !!filters.dateTo && filters.dateTo < filters.dateFrom

  const { data: sourceGroups = [] } = useQuery({
    queryKey: ['source-groups'],
    queryFn: () => api.getSourceGroups(),
  })

  const evidenceQuery = useQuery({
    queryKey: ['evidence', filters, page, pageSize],
    queryFn: () => api.listEvidence({
      source_group_id: filters.sourceGroupId ? Number(filters.sourceGroupId) : undefined,
      date_from: filters.dateFrom || undefined,
      date_to: filters.dateTo || undefined,
      usage: filters.usage,
      q: filters.q.trim() || undefined,
      page,
      page_size: pageSize,
    }),
  })

  const rows = evidenceQuery.data?.items ?? []
  const total = evidenceQuery.data?.total ?? 0

  // 删掉某页最后一条后页码可能越界（后端返回空 items 但 total > 0）：回退一页，
  // 而不是让用户停在「暂无凭证」的空列表里自己猜。
  useEffect(() => {
    if (!evidenceQuery.data) return
    if (evidenceQuery.data.items.length === 0 && evidenceQuery.data.total > 0 && page > 1) {
      setPage(page - 1)
    }
  }, [evidenceQuery.data, page])

  const updateFilters = (patch: Partial<EvidenceFilters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setPage(1)
  }

  const sourceGroupOptions = useMemo(
    () => sourceGroups.map((group) => ({ value: String(group.id), label: group.label })),
    [sourceGroups],
  )

  const deleteMutation = useMutation({
    mutationFn: ({ id, audit }: { id: number; audit?: AuditReasonInput }) =>
      api.deleteEvidence(id, audit),
    onSuccess: () => {
      message.success('凭证已删除')
      setDeleting(null)
      setDeleteError(null)
      void queryClient.invalidateQueries({ queryKey: ['evidence'] })
    },
    onError: (error) => {
      // 缺理由（422）：弹窗留着、已选的理由留着，补一句再点一次即可。
      if (isAuditReasonRequired(error)) {
        setDeleteError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(error.message || '删除失败')
    },
  })

  const dryRunMutation = useMutation({
    mutationFn: (scope: number | undefined) =>
      api.purgeUnusedEvidence({ source_group_id: scope, dry_run: true }),
    onSuccess: (result) => setPurgePreview(result),
    onError: (error: Error) => {
      setPurgeOpen(false)
      message.error(error.message || '统计未使用凭证失败')
    },
  })

  const purgeMutation = useMutation({
    mutationFn: ({ scope, audit }: { scope: number | undefined; audit?: AuditReasonInput }) =>
      api.purgeUnusedEvidence({ source_group_id: scope, dry_run: false }, audit),
    onSuccess: (result) => {
      setPurgeResult(result)
      setPurgeError(null)
      void queryClient.invalidateQueries({ queryKey: ['evidence'] })
    },
    onError: (error) => {
      // 整批共用一条理由；缺理由时清单还在，用户补选后可直接再点「确认清理」。
      if (isAuditReasonRequired(error)) {
        setPurgeError(AUDIT_REASON_REQUIRED_TEXT)
        return
      }
      message.error(error.message || '批量清理失败')
    },
  })

  const openPurge = () => {
    const scope = filters.sourceGroupId ? Number(filters.sourceGroupId) : undefined
    setPurgeScope(scope)
    setPurgePreview(null)
    setPurgeResult(null)
    setPurgeError(null)
    setPurgeOpen(true)
    dryRunMutation.mutate(scope)
  }

  const closePurge = () => {
    if (purgeMutation.isPending) return
    setPurgeOpen(false)
  }

  /** 「确认清理」前先在本地拦一道：没选理由就不发请求（这个弹窗自己画按钮，走不了确认框的门禁）。 */
  const submitPurge = () => {
    if (!purgeReason.reason) {
      setPurgeError(AUDIT_REASON_REQUIRED_TEXT)
      return
    }
    purgeMutation.mutate({ scope: purgeScope, audit: toAuditReasonInput(purgeReason) })
  }

  const purgeScopeLabel = purgeScope
    ? sourceGroups.find((group) => group.id === purgeScope)?.label ?? `来源组 #${purgeScope}`
    : '全部来源组'

  const columns: DataTableColumn<EvidenceListItem>[] = [
    {
      key: 'thumbnail',
      header: '缩略图',
      widthTier: 'thumb',
      // 卡片视角下缩略图太占位；移动端用「查看」看大图（spec D3 只要求表格列）
      // 缩略图本身即入口：点图开详情（evidence-detail-entry D1/D2）。用 button 承载以便 Tab/Enter，
      // 且 EvidenceImage 的 404 占位态（内部 span）落在按钮内，原图缺失时同样可点开详情。
      cell: (row) => (
        <button
          type="button"
          onClick={() => setDetail(row)}
          aria-label={`查看凭证 ${row.id} 详情`}
          className="block max-w-full cursor-pointer rounded-control transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)] motion-reduce:transition-none"
        >
          <EvidenceImage
            id={row.id}
            alt={`凭证 #${row.id} 原图`}
            className="h-[100px] w-[100px]"
          />
        </button>
      ),
      mobile: { role: 'hidden' },
    },
    {
      key: 'original_filename',
      header: '原始文件名',
      // 文件名是 note 档：单行 + 悬停全文（spec P4）。`ellipsis` 与 title 同时成立
      // 才算合法省略（A4 白名单按 aria-label「截图凭证」登记，两者都在本表内）。
      widthTier: 'note',
      ellipsis: true,
      cell: (row) => {
        // 列值兜底（spec D3 / §3-12）：original_filename 为 NULL 时显示 file_path 的
        // basename（新命名本身可读），不再出现「未命名文件」这种无信息占位。
        const label = row.original_filename || fileNameOf(row.file_path)
        return (
          <div className="min-w-0">
            {/* 文件名同样直通详情（evidence-detail-entry D1/D2）；按钮内只有文本，
                「凭证 #id」留在按钮外，移动端卡片标题语义（role: 'title'）不变。 */}
            <button
              type="button"
              onClick={() => setDetail(row)}
              aria-label={`查看凭证 ${row.id} 详情`}
              title={label}
              className="block max-w-full cursor-pointer truncate text-left font-medium text-ink underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]"
            >
              {label}
            </button>
            <span className="pf-num block text-meta text-muted">凭证 #{row.id}</span>
          </div>
        )
      },
      mobile: { role: 'title' },
    },
    {
      key: 'source_group',
      header: '来源组',
      widthTier: 'label',
      cell: (row) => (
        <span className="text-secondary" title={row.source_group.slug}>
          {row.source_group.label}
        </span>
      ),
      mobile: { role: 'meta', label: '来源组' },
    },
    {
      key: 'data_date',
      header: '数据日期',
      widthTier: 'date',
      cell: (row) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{formatDate(row.data_date)}</span>,
      mobile: { role: 'meta', label: '数据日期' },
    },
    {
      key: 'engine',
      header: '引擎',
      widthTier: 'text-short',
      cell: (row) => <span className="text-meta text-secondary">{engineLabel(row.engine)}</span>,
      mobile: { role: 'meta', label: '引擎' },
    },
    {
      key: 'usage',
      header: '使用情况',
      widthTier: 'label',
      cell: (row) => (
        row.used_rows > 0 ? (
          <span className="pf-num whitespace-nowrap rounded-pill bg-status-good-surface px-2 py-0.5 text-meta text-status-good-text">
            已使用 · {row.used_rows} 行
          </span>
        ) : (
          <span className="whitespace-nowrap rounded-pill bg-subtle px-2 py-0.5 text-meta text-muted">
            未使用
          </span>
        )
      ),
      mobile: { role: 'meta', label: '使用情况' },
    },
  ]

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="截图凭证"
        description="上传即存证的原始图片。翻到任意一张、核对原图、顺着它找回写入的账本行；没有任何引用的凭证可以清理。"
        actions={
          <button
            type="button"
            onClick={openPurge}
            className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-4 text-body-strong font-medium text-ink"
          >
            <Eraser size={16} aria-hidden /> 清理未使用凭证
          </button>
        }
      />

      <div className="rounded-card border border-line bg-surface p-4 shadow-card tablet:p-6">
        <div className="flex items-center gap-2">
          <Filter size={18} className="text-secondary" aria-hidden />
          <h2 className="text-h3 font-semibold text-ink">筛选</h2>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-4 tablet:grid-cols-2 xl:grid-cols-4">
          <SelectField
            label="来源组"
            value={filters.sourceGroupId}
            onChange={(value) => updateFilters({ sourceGroupId: value })}
            placeholder="全部来源组"
            options={sourceGroupOptions}
          />
          <DateField
            label="起始日期"
            value={filters.dateFrom}
            onChange={(value) => updateFilters({ dateFrom: value })}
          />
          <DateField
            label="结束日期"
            value={filters.dateTo}
            onChange={(value) => updateFilters({ dateTo: value })}
          />
          <SelectField
            label="使用情况"
            value={filters.usage === 'all' ? '' : filters.usage}
            onChange={(value) => updateFilters({ usage: (value || 'all') as UsageFilter })}
            placeholder="全部凭证"
            options={USAGE_OPTIONS}
          />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[180px] flex-1 tablet:max-w-[320px]">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
            <input
              type="text"
              inputMode="search"
              value={filters.q}
              onChange={(e) => updateFilters({ q: e.target.value })}
              placeholder="搜索文件名或路径"
              aria-label="搜索文件名或路径"
              className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
            />
          </div>
          {filtersActive && (
            <button
              type="button"
              onClick={() => { setFilters(EMPTY_FILTERS); setPage(1) }}
              className="inline-flex min-h-[44px] items-center rounded-control px-3 text-meta text-link"
            >
              清除筛选
            </button>
          )}
          <span className="pf-num ml-auto text-meta text-muted">共 {total} 张</span>
        </div>
        {dateRangeInvalid && (
          <div className="mt-3">
            <InlineError>结束日期早于起始日期，这个区间不会有任何凭证。</InlineError>
          </div>
        )}
        <p className="mt-3 text-meta text-muted">
          按数据日期筛选时，未采纳富化的凭证（没有数据日期）不会命中任何区间，属预期。
        </p>
      </div>

      <DataTable
        label="截图凭证"
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        loading={evidenceQuery.isLoading}
        error={evidenceQuery.error}
        onRetry={() => { void evidenceQuery.refetch() }}
        pagination={{
          page,
          pageSize,
          total,
          onPageChange: setPage,
          pageSizeOptions: [20, 50, 100],
          onPageSizeChange: (size) => { setPageSize(size); setPage(1) },
        }}
        empty={{
          title: filtersActive ? '没有匹配的凭证' : '暂无凭证',
          description: filtersActive
            ? '换个筛选条件试试；清除筛选可看到全部凭证。'
            : '截图录入（含预览后放弃）都会自动存证，上传后就会出现在这里。',
          action: filtersActive
            ? <button type="button" onClick={() => { setFilters(EMPTY_FILTERS); setPage(1) }} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">清除筛选</button>
            : undefined,
        }}
        rowActionsWidth={actionsWidth(['icon', 'icon'])}
        rowActions={(row) => (
          <>
            <button
              type="button"
              onClick={() => setDetail(row)}
              aria-label={`查看凭证 ${row.id}`}
              title="查看"
              className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"
            >
              <Eye size={16} aria-hidden />
            </button>
            {row.used_rows > 0 ? (
              // 已使用 = 证据链底线：按钮保留但禁用，tooltip 说明引用数（spec D3/§3-11）。
              // antd Tooltip 对 disabled 元素不触发，故包一层 span 接事件。
              <Tooltip title={`已被 ${row.used_rows} 处引用，不能删除`}>
                <span className="inline-flex">
                  <button
                    type="button"
                    disabled
                    aria-label={`删除凭证 ${row.id}（已使用，不可删除）`}
                    className="inline-flex h-11 w-11 cursor-not-allowed items-center justify-center rounded-control text-status-critical-text opacity-40"
                  >
                    <Trash2 size={16} aria-hidden />
                  </button>
                </span>
              </Tooltip>
            ) : (
              <button
                type="button"
                onClick={() => setDeleting(row)}
                aria-label={`删除凭证 ${row.id}`}
                title="删除"
                className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"
              >
                <Trash2 size={16} aria-hidden />
              </button>
            )}
          </>
        )}
      />

      {detail && (
        <LazyResponsiveDialog
          open
          onClose={() => setDetail(null)}
          title={`凭证 #${detail.id}`}
          description={detail.original_filename || detail.file_path}
          size="lg"
          footer={
            <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
              <a
                href={`${api.evidenceImageUrl(detail.id)}?download=1`}
                className="inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink"
              >
                下载原图
              </a>
              <button
                type="button"
                onClick={() => setDetail(null)}
                className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white"
              >
                关闭
              </button>
            </div>
          }
        >
          <EvidenceDetail item={detail} />
        </LazyResponsiveDialog>
      )}

      {deleting && (
        <LazyConfirmDialog
          open
          onClose={() => {
            setDeleting(null)
            setDeleteError(null)
          }}
          onConfirm={() => deleteMutation.mutate({
            id: deleting.id,
            audit: toAuditReasonInput(deleteReason),
          })}
          title={`删除凭证 #${deleting.id}？`}
          description="将从数据库与磁盘永久删除这张原图，不可恢复；账本行不受影响（此凭证未被任何行引用）。"
          confirmLabel="删除凭证"
          danger
          loading={deleteMutation.isPending}
        >
          <div className="space-y-3">
            {deleteError && <InlineError>{deleteError}</InlineError>}
            <AuditReasonField
              label="为什么删"
              value={deleteReason}
              onChange={setDeleteReason}
              hint="删除会留下谁在何时删了哪张凭证。"
            />
          </div>
        </LazyConfirmDialog>
      )}

      <LazyResponsiveDialog
        open={purgeOpen}
        onClose={closePurge}
        title="清理未使用凭证"
        description="只清理没有被账本行、审计、本金变更或划转引用的凭证；已使用凭证永远不在清理范围内。"
        size="md"
        footer={
          purgeResult ? (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={closePurge}
                className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white"
              >
                完成
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
              <button
                type="button"
                onClick={closePurge}
                disabled={purgeMutation.isPending}
                className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submitPurge}
                disabled={
                  dryRunMutation.isPending
                  || purgeMutation.isPending
                  || !purgePreview
                  || purgePreview.items.length === 0
                }
                className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-danger px-4 text-body-strong font-medium text-white disabled:opacity-50"
              >
                {purgeMutation.isPending ? '清理中…' : '确认清理'}
              </button>
            </div>
          )
        }
      >
        <div className="space-y-3 text-body text-secondary">
          <p>
            清理范围：<strong className="text-ink">{purgeScopeLabel}</strong>
            {purgeScope ? '（当前筛选选中的来源组）' : ''}
          </p>

          {dryRunMutation.isPending && <p aria-busy="true">正在统计将删除的凭证…</p>}

          {dryRunMutation.isError && (
            <InlineError>{dryRunMutation.error instanceof Error ? dryRunMutation.error.message : '统计失败，请重试。'}</InlineError>
          )}

          {!purgeResult && purgePreview && (
            <div className="space-y-2 rounded-control border border-line bg-subtle p-3">
              <p>
                将永久删除{' '}
                {/* 计数必须取候选清单的条数，不能取 deleted：dry_run 阶段 deleted 恒为 0
                    （它的语义是「本次实际删掉了几条」），拿它当预览计数会得到
                    「0 条却释放 128 KB」这种自相矛盾的展示，并且把确认按钮永久禁用。 */}
                <strong className="pf-num text-ink">{purgePreview.items.length}</strong>{' '}
                条未使用凭证，释放{' '}
                <strong className="pf-num text-ink">{formatFileSize(purgePreview.freed_bytes)}</strong>。
              </p>
              {purgePreview.items.length === 0 ? (
                <p className="text-meta text-muted">范围内没有可清理的未使用凭证。</p>
              ) : (
                <p className="text-meta text-muted">
                  这一步只做了统计：库与磁盘都还没有任何变化。点「确认清理」才会真正删除文件，且不可恢复。
                  {purgePreview.truncated
                    ? `候选共 ${purgePreview.total} 条，本次只处理前 ${purgePreview.limit} 条，执行后可再次清理。`
                    : '单次最多清理 200 条，数量更多时再次执行即可。'}
                </p>
              )}
              {purgePreview.items.length > 0 && (
                <details className="rounded-control border border-line bg-surface">
                  <summary className="cursor-pointer select-none px-3 py-2 text-meta font-medium text-ink">
                    将删除的凭证清单（{purgePreview.items.length} 条）
                  </summary>
                  <ul className="max-h-48 space-y-1 overflow-auto px-3 pb-2">
                    {purgePreview.items.map((item) => (
                      <li key={item.id} className="flex items-center justify-between gap-3 text-meta text-secondary">
                        <span className="truncate" title={item.file_path}>凭证 #{item.id}：{item.file_path}</span>
                        <span className="pf-num shrink-0 text-muted">{formatFileSize(item.file_bytes)}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {purgePreview.failed.length > 0 && (
                <div>
                  <p className="text-meta font-medium text-ink">统计期内有 {purgePreview.failed.length} 条无法处理：</p>
                  <ul className="mt-1 space-y-1">
                    {purgePreview.failed.map((item) => (
                      <li key={item.id} className="text-meta text-secondary">凭证 #{item.id}：{item.reason}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {!purgeResult && (
            <div className="space-y-3">
              {purgeError && <InlineError>{purgeError}</InlineError>}
              <AuditReasonField
                label="为什么清理这一批"
                value={purgeReason}
                onChange={setPurgeReason}
                hint="整批共用这一条理由，每条被删的凭证都会留下谁在何时删了它。"
              />
            </div>
          )}

          {purgeResult && (
            <div className="space-y-2 rounded-control border border-line bg-subtle p-3">
              <p>
                已删除{' '}
                <strong className="pf-num text-ink">{purgeResult.deleted}</strong>{' '}
                条凭证，释放{' '}
                <strong className="pf-num text-ink">{formatFileSize(purgeResult.freed_bytes)}</strong>。
              </p>
              {purgeResult.failed.length > 0 ? (
                <div>
                  <p className="text-meta font-medium text-ink">以下 {purgeResult.failed.length} 条未能删除：</p>
                  <ul className="mt-1 space-y-1">
                    {purgeResult.failed.map((item) => (
                      <li key={item.id} className="text-meta text-secondary">凭证 #{item.id}：{item.reason}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-meta text-muted">没有失败的条目。</p>
              )}
            </div>
          )}
        </div>
      </LazyResponsiveDialog>
    </PageContainer>
  )
}