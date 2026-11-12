'use client'

import type { CSSProperties, ReactNode } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { tokens } from '@/styles/tokens'
import { EmptyState, ErrorState, LoadingState } from './States'

export type DataTableSortDirection = 'asc' | 'desc'

export interface DataTableSort {
  key: string
  direction: DataTableSortDirection
}

export type DataTableMobileRole = 'title' | 'value' | 'meta' | 'hidden'

/**
 * 列宽档位：`tokens.table.col` 的十三个键（拼错即编译错误）。
 * 宽度事实源只有 `tokens.json` 一处 —— 逐列写死像素的字段（`width`）已从类型上删除（spec D1/D2）。
 */
export type ColumnTier = keyof typeof tokens.table.col

export interface DataTableColumn<T> {
  key: string
  header: ReactNode
  cell: (row: T, index: number) => ReactNode
  sortable?: boolean
  align?: 'left' | 'right' | 'center'
  /** 列宽档位（必填）：选档看单元格渲染出来的内容类，不看数据字段名。 */
  widthTier: ColumnTier
  /**
   * 唯一合法的省略声明（仅 `note` 档，spec P4）：单元格文本单行 + 悬停全文。
   * 验收要求「配置声明 + DOM 给 title」双满足，title 由 cell 渲染的内容自带。
   */
  ellipsis?: true
  /** 移动卡片角色；缺省：首列 title，其余 meta。span=2 时 meta 占满整行。 */
  mobile?: { role?: DataTableMobileRole; label?: ReactNode; span?: 1 | 2 }
}

export interface DataTablePagination {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  pageSizeOptions?: number[]
  onPageSizeChange?: (size: number) => void
}

export interface DataTableSelection {
  selectedKeys: Array<string | number>
  onChange: (keys: Array<string | number>) => void
}

export interface DataTableProps<T> {
  rows: T[]
  columns: DataTableColumn<T>[]
  rowKey: (row: T) => string | number
  /** 表格的 sr-only 名称。 */
  label: string
  loading?: boolean
  error?: Error | string | null
  onRetry?: () => void
  empty?: { title: ReactNode; description?: ReactNode; action?: ReactNode }
  sort?: DataTableSort | null
  onSortChange?: (sort: DataTableSort) => void
  pagination?: DataTablePagination
  selection?: DataTableSelection
  /** 行是否可勾选；归档/不可操作行返回 false（复选框禁用且不参与全选）。 */
  isRowSelectable?: (row: T) => boolean
  rowActions?: (row: T) => ReactNode
  /** 操作列宽（px）。值应由 `actionsWidth()` 算出，不手写字面量（spec §4.3）。 */
  rowActionsWidth?: number
  rowClassName?: (row: T) => string | undefined
  toolbar?: ReactNode
  className?: string
  /** 桌面表格滚动区最大高度；默认 70vh。 */
  maxHeightClass?: string
  /** 左侧冻结（选择列 + 首列）；默认 true（spec D9/D10）。 */
  stickyFirstCol?: boolean
  /** 右侧冻结（操作列）；默认 true。 */
  stickyLastCol?: boolean
}

const TIER = tokens.table.col
const ACTIONS = tokens.table.actions

/** 字体容差：覆盖 macOS/Windows 字体栈 ±2~4% 的差异（.scratch/table-width/01-report.md §1.1）。 */
const ACTION_TEXT_TOLERANCE = 1.05

/** 操作列按钮：纯图标按钮（44px 命中区）或文字按钮（可带图标）。 */
export type DataTableActionButton = 'icon' | { label: string; icon?: boolean }

function ceil4(value: number): number {
  return Math.ceil(value / 4) * 4
}

function isWideGlyph(char: string): boolean {
  return /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/.test(char)
}

function actionButtonWidth(button: DataTableActionButton): number {
  if (button === 'icon') return ACTIONS.icon
  const em = tokens.type.body.size
  let text = 0
  // 实测基准：CJK = 1em、数字/拉丁 = 0.556em（01-report §1）
  for (const char of button.label) text += isWideGlyph(char) ? em : em * 0.556
  return ACTIONS['text-pad-x'] + text * ACTION_TEXT_TOLERANCE
    + (button.icon ? ACTIONS.gap + ACTIONS['text-icon'] : 0)
}

/**
 * 操作列宽度计算族（spec D1）：`ceil4(Σ按钮内容宽 + gap×(n−1) + 单元格内边距 + 2)`。
 * 图标按钮 1 个 = 64、2 个 = 112（`.scratch/row-action-menu/spec.md` D10 的冻结值）。
 * 原生表把 `cellPadX` 传 `table.actions.pad-x-native`（40）即可复用同一公式。
 */
export function actionsWidth(
  buttons: DataTableActionButton[],
  options?: { cellPadX?: number },
): number {
  if (buttons.length === 0) return 0
  const content = buttons.reduce((sum, button) => sum + actionButtonWidth(button), 0)
  const gaps = ACTIONS.gap * (buttons.length - 1)
  const pad = options?.cellPadX ?? ACTIONS['pad-x']
  return ceil4(content + gaps + pad + 2)
}

/** 表头冻结格取 20（表头优先），表体冻结格取 `z.stickyCol` = 10（spec D10/A10）。 */
const STICKY_HEAD_Z = 'var(--pf-z-sticky)'
const STICKY_BODY_Z = 'var(--pf-z-sticky-col)'

/**
 * 冻结格样式（spec D9/D10 路线甲）：`collapse` + inset 阴影做边界线。
 * 表体背景必须**跟随行色**（`inherit`），不能写死 surface —— 否则行级状态类
 * （settings/llm-providers 的 `bg-amber-50` 默认项行）穿不到首/末列，出现
 * 「首末列白底」割裂。前提：<tr> 一定有显式不透明行底色（下面数据行给了 `bg-surface`），
 * 且行背景只能是纯色（inherit 继承 background 简写，渐变/图片会按单元格框重绘错位）。
 * 表头冻结格不能 inherit：th 父 tr 无底色时会拿到 transparent，横滚照样透字。
 */
function stickyStyle(
  edge: 'left' | 'right',
  options: { header: boolean; line: boolean; offset?: number },
): CSSProperties {
  const { header, line, offset = 0 } = options
  return {
    position: 'sticky',
    ...(edge === 'left' ? { left: offset } : { right: offset }),
    zIndex: header ? STICKY_HEAD_Z : STICKY_BODY_Z,
    background: header ? 'var(--pf-color-bg-subtle)' : 'inherit',
    ...(line
      ? { boxShadow: edge === 'left' ? 'inset -1px 0 0 var(--pf-line)' : 'inset 1px 0 0 var(--pf-line)' }
      : null),
  }
}

const ALIGN: Record<NonNullable<DataTableColumn<unknown>['align']>, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
}

function SortIcon({ active, direction }: { active: boolean; direction?: DataTableSortDirection }) {
  if (!active) return <ArrowUpDown size={14} aria-hidden className="text-muted" />
  return direction === 'asc'
    ? <ArrowUp size={14} aria-hidden className="text-brand-600" />
    : <ArrowDown size={14} aria-hidden className="text-brand-600" />
}

function PaginationBar({ pagination }: { pagination: DataTablePagination }) {
  const { page, pageSize, total, onPageChange, pageSizeOptions, onPageSizeChange } = pagination
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1
  const to = Math.min(total, page * pageSize)

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-3 py-2 text-meta text-muted">
      <span className="pf-num">
        第 {from}–{to} 条 · 共 {total} 条
      </span>
      <div className="flex flex-wrap items-center gap-1">
        {pageSizeOptions && pageSizeOptions.length > 1 && onPageSizeChange && (
          <select
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            aria-label="每页条数"
            className="mr-1 min-h-[44px] rounded-control border border-line bg-surface px-2 text-meta text-ink"
          >
            {pageSizeOptions.map((size) => (
              <option key={size} value={size}>每页 {size}</option>
            ))}
          </select>
        )}
        <button
          type="button"
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={page <= 1}
          aria-label="上一页"
          className="inline-flex h-11 w-11 items-center justify-center rounded-control text-secondary disabled:opacity-40"
        >
          <ChevronLeft size={18} aria-hidden />
        </button>
        <span className="pf-num min-w-[5.5rem] text-center text-secondary">
          {page} / {totalPages}
        </span>
        <button
          type="button"
          onClick={() => onPageChange(Math.min(totalPages, page + 1))}
          disabled={page >= totalPages}
          aria-label="下一页"
          className="inline-flex h-11 w-11 items-center justify-center rounded-control text-secondary disabled:opacity-40"
        >
          <ChevronRight size={18} aria-hidden />
        </button>
      </div>
    </div>
  )
}

/**
 * 列表唯一实现：
 * - ≥768：表格，`table-layout: fixed`、表头 sticky、行 48、可选排序/选择/分页；
 *   表宽由 `Σ列档位 + 选择列 + 操作列` 派生并只作**下限**（容器更宽时等比放大）；
 *   首列（含选择列）与操作列默认冻结（spec D9/D10）；
 * - <768：卡片列表（标题+金额同行，元信息 12，操作 44）；
 * - loading/empty/error 占据内容区，工具栏位置不变。
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  label,
  loading = false,
  error,
  onRetry,
  empty,
  sort,
  onSortChange,
  pagination,
  selection,
  isRowSelectable,
  rowActions,
  rowActionsWidth = ACTIONS.default,
  rowClassName,
  toolbar,
  className,
  maxHeightClass = 'max-h-[70vh]',
  stickyFirstCol = true,
  stickyLastCol = true,
}: DataTableProps<T>) {
  const actionsColumnWidth = rowActions ? rowActionsWidth : 0
  // 表宽语义是**下限**（spec D3/D5）：Σ列档位 + 选择列 + 操作列。
  // 容器更宽时浏览器在 fixed 布局下按列宽比例放大，只变宽不变窄；外部不再传 minWidth。
  // `?? 0`：票 02 落地前页面列定义还没有 widthTier，派生值退化为 0（而不是 NaN）。
  const minTableWidth = columns.reduce((sum, column) => sum + (TIER[column.widthTier] ?? 0), 0)
    + (selection ? TIER.select : 0)
    + actionsColumnWidth
  const firstColLeft = selection ? TIER.select : 0
  const freezeLast = stickyLastCol && !!rowActions

  const titleColumn = columns.find((column) => column.mobile?.role === 'title') ?? columns[0]
  const valueColumn = columns.find((column) => column.mobile?.role === 'value')
  const metaColumns = columns.filter(
    (column) =>
      column !== titleColumn &&
      column !== valueColumn &&
      column.mobile?.role !== 'hidden',
  )
  const selectableKeys = rows.filter((row) => isRowSelectable?.(row) ?? true).map(rowKey)
  const selected = new Set(selection?.selectedKeys ?? [])
  const allSelected = selectableKeys.length > 0 && selectableKeys.every((key) => selected.has(key))

  const toggleAll = () => {
    if (!selection) return
    if (allSelected) {
      selection.onChange(selection.selectedKeys.filter((key) => !selectableKeys.includes(key)))
    } else {
      const next = new Set(selection.selectedKeys)
      selectableKeys.forEach((key) => next.add(key))
      selection.onChange(Array.from(next))
    }
  }

  const toggleOne = (key: string | number) => {
    if (!selection) return
    const next = new Set(selection.selectedKeys)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    selection.onChange(Array.from(next))
  }

  if (error) {
    return (
      <div className={cn('space-y-3', className)}>
        {toolbar}
        <ErrorState
          title="加载失败"
          description={typeof error === 'string' ? error : '列表请求未成功，请重试。'}
          onRetry={onRetry}
        />
      </div>
    )
  }

  if (loading) {
    return (
      <div className={cn('space-y-3', className)}>
        {toolbar}
        <div aria-busy="true">
          <LoadingState rows={5} label={`正在加载${label}`} />
        </div>
      </div>
    )
  }

  if (rows.length === 0) {
    return (
      <div className={cn('space-y-3', className)}>
        {toolbar}
        <EmptyState
          title={empty?.title ?? '暂无数据'}
          description={empty?.description}
          action={empty?.action}
        />
      </div>
    )
  }

  return (
    <div className={cn('space-y-3', className)}>
      {toolbar}

      {/* 桌面表格 */}
      <div className="hidden overflow-hidden rounded-card border border-line bg-surface md:block">
        <div className={cn('overflow-auto', maxHeightClass)}>
          <table
            className="w-full"
            style={{ minWidth: minTableWidth, tableLayout: 'fixed' }}
            aria-label={label}
          >
            <caption className="sr-only">{label}</caption>
            <thead className="sticky top-0 z-sticky bg-subtle">
              <tr className="border-b border-line">
                {selection && (
                  <th
                    scope="col"
                    style={{
                      width: TIER.select,
                      ...(stickyFirstCol ? stickyStyle('left', { header: true, line: false }) : null),
                    }}
                    className="px-3 py-0.5"
                  >
                    <label className="inline-flex h-11 w-11 items-center justify-center">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAll}
                        disabled={selectableKeys.length === 0}
                        aria-label="选择当前页全部可选项"
                        title={selectableKeys.length === 0 ? '当前页没有可选择的记录' : undefined}
                        className="h-4 w-4 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed disabled:opacity-40"
                      />
                    </label>
                  </th>
                )}
                {columns.map((column, columnIndex) => {
                  const active = sort?.key === column.key
                  const ariaSort = active
                    ? sort.direction === 'asc' ? 'ascending' : 'descending'
                    : 'none'
                  const frozen = stickyFirstCol && columnIndex === 0
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      aria-sort={column.sortable ? ariaSort : undefined}
                      style={{
                        width: TIER[column.widthTier],
                        ...(frozen
                          ? stickyStyle('left', { header: true, line: true, offset: firstColLeft })
                          : null),
                      }}
                      className={cn(
                        'whitespace-nowrap px-4 py-0.5 text-meta font-medium text-muted tablet:px-5',
                        ALIGN[column.align ?? 'left'],
                      )}
                    >
                      {column.sortable && onSortChange ? (
                        <button
                          type="button"
                          onClick={() => onSortChange({
                            key: column.key,
                            direction: active && sort?.direction === 'asc' ? 'desc' : 'asc',
                          })}
                          className="inline-flex min-h-[44px] items-center gap-1 rounded-control text-meta font-medium text-muted"
                        >
                          {column.header}
                          <SortIcon active={active} direction={sort?.direction} />
                        </button>
                      ) : column.header}
                    </th>
                  )
                })}
                {rowActions && (
                  <th
                    scope="col"
                    style={{
                      width: actionsColumnWidth,
                      ...(freezeLast ? stickyStyle('right', { header: true, line: true }) : null),
                    }}
                    className="px-2 py-0.5 text-right text-meta font-medium text-muted"
                  >
                    操作
                  </th>
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((row, index) => {
                const key = rowKey(row)
                const selectable = isRowSelectable?.(row) ?? true
                return (
                  <tr
                    key={key}
                    // 行底色基线：冻结格 `background: inherit` 靠它拿到不透明色；
                    // rowClassName 放在后面（cn 走 tailwind-merge），行级状态类才能覆盖它。
                    className={cn('h-12 bg-surface text-body text-secondary', rowClassName?.(row))}
                  >
                    {selection && (
                      <td
                        style={stickyFirstCol ? stickyStyle('left', { header: false, line: false }) : undefined}
                        className="px-3 py-0.5"
                      >
                        <label className="inline-flex h-11 w-11 items-center justify-center">
                          <input
                            type="checkbox"
                            checked={selectable && selected.has(key)}
                            onChange={() => toggleOne(key)}
                            disabled={!selectable}
                            aria-label={selectable ? `选择第 ${index + 1} 行` : `第 ${index + 1} 行已归档，不可选择`}
                            title={selectable ? undefined : '已归档，不可选择'}
                            className="h-4 w-4 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed disabled:opacity-40"
                          />
                        </label>
                      </td>
                    )}
                    {columns.map((column, columnIndex) => (
                      <td
                        key={column.key}
                        style={
                          stickyFirstCol && columnIndex === 0
                            ? stickyStyle('left', { header: false, line: true, offset: firstColLeft })
                            : undefined
                        }
                        className={cn(
                          'px-4 py-2.5 tablet:px-5',
                          ALIGN[column.align ?? 'left'],
                          column.align === 'right' && 'pf-num',
                          column.ellipsis && 'truncate',
                        )}
                      >
                        {column.cell(row, index)}
                      </td>
                    ))}
                    {rowActions && (
                      <td
                        style={freezeLast ? stickyStyle('right', { header: false, line: true }) : undefined}
                        className="px-2 py-0.5"
                      >
                        <div className="flex items-center justify-end gap-1">{rowActions(row)}</div>
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {pagination && <PaginationBar pagination={pagination} />}
      </div>

      {/* 移动卡片 */}
      {selection && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-line bg-surface px-3 py-2 text-meta text-secondary md:hidden">
          <label className="inline-flex min-h-[44px] items-center gap-2">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={toggleAll}
              disabled={selectableKeys.length === 0}
              aria-label="全选本页可选项"
              title={selectableKeys.length === 0 ? '当前页没有可选择的记录' : undefined}
              className="h-4 w-4 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed disabled:opacity-40"
            />
            全选本页可选项
          </label>
          <span className="text-muted">{selectableKeys.length} 项可选</span>
        </div>
      )}
      <ul className="space-y-2 md:hidden" aria-label={label}>
        {rows.map((row, index) => {
          const key = rowKey(row)
          const selectable = isRowSelectable?.(row) ?? true
          return (
            <li key={key} className="rounded-card border border-line bg-surface p-4 shadow-card">
              <div className="flex items-start gap-3">
                {selection && (
                  <label className="mt-1 inline-flex h-11 w-11 shrink-0 items-center justify-center">
                    <input
                      type="checkbox"
                      checked={selectable && selected.has(key)}
                      onChange={() => toggleOne(key)}
                      disabled={!selectable}
                      aria-label={selectable ? `选择第 ${index + 1} 项` : `第 ${index + 1} 项已归档，不可选择`}
                      title={selectable ? undefined : '已归档，不可选择'}
                      className="h-4 w-4 accent-[var(--pf-color-action-primary)] disabled:cursor-not-allowed disabled:opacity-40"
                    />
                  </label>
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-3">
                    <div className="min-w-0 text-body-strong text-ink">{titleColumn?.cell(row, index)}</div>
                    {valueColumn && (
                      <div className="shrink-0 text-right">
                        {valueColumn.mobile?.label && (
                          <div className="text-meta text-muted">{valueColumn.mobile.label}</div>
                        )}
                        {valueColumn.cell(row, index)}
                      </div>
                    )}
                  </div>
                  {metaColumns.length > 0 && (
                    <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5">
                      {metaColumns.map((column) => (
                        <div key={column.key} className={cn('min-w-0', column.mobile?.span === 2 && 'col-span-2')}>
                          <dt className="text-meta text-muted">{column.mobile?.label ?? column.header}</dt>
                          <dd className="min-w-0 text-meta text-secondary">{column.cell(row, index)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </div>
              </div>
              {rowActions && (
                <div className="mt-3 flex flex-wrap items-center justify-end gap-1 border-t border-line pt-2">
                  {rowActions(row)}
                </div>
              )}
            </li>
          )
        })}
      </ul>
      {pagination && (
        <div className="rounded-card border border-line bg-surface md:hidden">
          <PaginationBar pagination={pagination} />
        </div>
      )}
    </div>
  )
}