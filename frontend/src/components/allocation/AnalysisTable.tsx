'use client'

import { useState, createContext, useContext, useCallback } from 'react'
import { ChevronDown, ChevronRight, Edit2, TrendingUp, TrendingDown, Minus, FolderOpen, FolderClosed, Bell } from 'lucide-react'
import { formatCurrency, formatPercent, formatPp } from '@/lib/format'
import { DirectionText, Money } from '@/components/ui'
import { actionsWidth } from '@/components/ui/DataTable'
import { tokens } from '@/styles/tokens'
import type { AllocationAnalysisItem } from '@/types'

/** 操作列（1 个图标按钮）宽度：原生表内边距 40，故与 DataTable 的 64 不同（spec §1.2）。 */
const ANALYSIS_ACTIONS_WIDTH = actionsWidth(['icon'], { cellPadX: tokens.table.actions['pad-x-native'] })

/**
 * 本表派生表宽（下限语义，spec D3/A5）：10 个档位之和 + 操作列。
 * `table-layout: fixed` 要生效必须有确定宽度来源 —— 缺了 `<colgroup>` + minWidth，
 * 档位就只是「建议值」（spec C2）。
 */
const ANALYSIS_TABLE_WIDTH = tokens.table.col['text-mid']
  + tokens.table.col.ratio * 4      // 目标% / 实际% / 偏离 / 阈值
  + tokens.table.col.amount * 3     // 目标金额 / 实际金额 / 偏离金额
  + tokens.table.col['text-short']  // 建议
  + ANALYSIS_ACTIONS_WIDTH

interface AnalysisTableProps {
  items: AllocationAnalysisItem[]
  onEdit: (item: AllocationAnalysisItem) => void
  editingId?: string | null
  totalAmount?: string
}

interface AnalysisItemRowProps {
  item: AllocationAnalysisItem
  level?: number
  onEdit: (item: AllocationAnalysisItem) => void
  editingId?: string | null
  totalAmount?: string
}

// Context for managing expanded state globally
interface ExpandedContextType {
  expandedIds: Set<string>
  toggleExpanded: (id: string) => void
  expandAll: () => void
  collapseAll: () => void
  isExpanded: (id: string) => boolean
}

const ExpandedContext = createContext<ExpandedContextType | null>(null)

function useExpandedContext() {
  const context = useContext(ExpandedContext)
  if (!context) {
    throw new Error('useExpandedContext must be used within ExpandedProvider')
  }
  return context
}

// Collect all item IDs recursively
const collectAllIds = (items: AllocationAnalysisItem[]): string[] => {
  const ids: string[] = []
  for (const item of items) {
    ids.push(item.id)
    if (item.children?.length) {
      ids.push(...collectAllIds(item.children))
    }
  }
  return ids
}

// Check if there are any items with children
const hasAnyChildren = (items: AllocationAnalysisItem[]): boolean => {
  for (const item of items) {
    if (item.children?.length) {
      return true
    }
    if (hasAnyChildren(item.children)) {
      return true
    }
  }
  return false
}

function thresholdText(value: number): string {
  return `±${formatPp(value).replace(/^\+/, '')}`
}

function bandValues(item: AllocationAnalysisItem) {
  const yellow = Number(item.band?.yellow ?? item.alert_threshold ?? 5)
  const red = Number(item.band?.red ?? (Number.isFinite(yellow) ? yellow * 2 : 10))
  const relApplicable = item.band?.components?.rel_applicable === true
  const relYellow = Number(item.band?.components?.rel_yellow ?? 25)
  return {
    yellow: Number.isFinite(yellow) ? yellow : 5,
    red: Number.isFinite(red) ? red : 10,
    relApplicable,
    relYellow: Number.isFinite(relYellow) ? relYellow : 25,
  }
}

function AnalysisItemRow({ item, level = 0, onEdit, editingId, totalAmount }: AnalysisItemRowProps) {
  const hasChildren = item.children && item.children.length > 0
  const { isExpanded, toggleExpanded } = useExpandedContext()
  const expanded = isExpanded(item.id)
  const isEditing = editingId === item.id

  const deviation = parseFloat(item.deviation)
  const actualPercent = parseFloat(item.actual_percent)
  const targetPercent = item.target_percent ? parseFloat(item.target_percent) : null
  const band = bandValues(item)

  // Calculate target amount
  const targetAmount = targetPercent !== null && totalAmount
    ? (parseFloat(totalAmount) * targetPercent / 100)
    : null

  // Calculate deviation amount if available
  const deviationAmount = item.deviation_amount ? parseFloat(item.deviation_amount) : null

  // Determine deviation color and icon based on custom threshold
  const getDeviationStyle = () => {
    if (!targetPercent) return { color: 'text-muted', icon: Minus }
    if (deviation > band.yellow) return { color: 'text-red-600', icon: TrendingUp }
    if (deviation < -band.yellow) return { color: 'text-green-600', icon: TrendingDown }
    return { color: 'text-blue-600', icon: Minus }
  }

  const deviationStyle = getDeviationStyle()
  const DeviationIcon = deviationStyle.icon

  // Parse recommendation for better display
  const getRecommendationStyle = () => {
    if (item.recommendation.includes('减持')) return 'text-red-600 bg-red-50'
    if (item.recommendation.includes('增持')) return 'text-green-600 bg-green-50'
    if (item.recommendation.includes('合理')) return 'text-blue-600 bg-blue-50'
    return 'text-slate-500 bg-slate-50'
  }

  return (
    <>
      {/* 行底色一律不透明纯色、互斥渲染：编辑态 / 树形子行 / 默认，三选一。
          类名必须是**完整字面量**（Tailwind JIT 按内容扫描，拼出来的类不会生成）。
          注意别让 hover 类盖掉编辑态：旧写法 `hover:bg-slate-50`(0,2,0) 压过
          `bg-brand-50`(0,1,0)，悬停编辑行时 brand 色会消失。 */}
      <tr
        className={`transition-colors group ${
          isEditing
            ? 'bg-brand-50'
            : level > 0
              ? 'bg-subtle hover:bg-app'
              : 'bg-surface hover:bg-app'
        }`}
      >
        <td className="pf-col-first whitespace-normal py-3">
          <div className="flex items-center" style={{ paddingLeft: `${level * 24}px` }}>
            {hasChildren && (
              <button
                onClick={() => toggleExpanded(item.id)}
                aria-expanded={expanded}
                aria-label={expanded ? `收起${item.name}` : `展开${item.name}`}
                className="mr-2 -m-2 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-secondary transition-colors hover:bg-subtle lg:m-0 lg:mr-2 lg:h-auto lg:w-auto lg:p-0.5"
              >
                {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </button>
            )}
            {!hasChildren && <span className="w-6" />}
            <span className={`${level === 0 ? 'font-medium text-slate-900' : 'text-sm text-slate-600'}`}>
              {item.name}
            </span>
          </div>
        </td>
        <td className="whitespace-nowrap py-3 text-right">
          {targetPercent !== null ? (
            <span className="font-medium text-slate-900">{formatPercent(targetPercent)}</span>
          ) : (
            <span className="text-muted">-</span>
          )}
        </td>
        <td className="whitespace-nowrap py-3 text-right">
          {targetAmount !== null ? (
            <span className="font-medium text-slate-900">{formatCurrency(targetAmount)}</span>
          ) : (
            <span className="text-muted">-</span>
          )}
        </td>
        <td className="whitespace-nowrap py-3 text-right">
          <span className="font-medium text-slate-900">{formatPercent(actualPercent)}</span>
        </td>
        <td className="whitespace-nowrap py-3 text-right">
          <span className="text-slate-700">{formatCurrency(item.actual_amount)}</span>
        </td>
        <td className={`whitespace-nowrap py-3 text-right font-medium ${deviationStyle.color}`}>
          <div className="flex items-center justify-end gap-1">
            <DeviationIcon size={14} />
            <span>{formatPp(deviation)}</span>
          </div>
        </td>
        <td className="whitespace-nowrap py-3 text-right">
          {deviationAmount !== null ? (
            <span className={`font-medium ${deviation > 0 ? 'text-red-600' : 'text-green-600'}`}>
              {deviation > 0 ? '+' : ''}{formatCurrency(item.deviation_amount!)}
            </span>
          ) : (
            <span className="text-muted">-</span>
          )}
        </td>
        <td className="py-3 text-right">
          {targetPercent !== null ? (
            <div className="space-y-0.5 text-meta">
              <div className="pf-num whitespace-nowrap text-secondary"><span className="text-status-warn-text">黄</span> {thresholdText(band.yellow)}</div>
              <div className="pf-num whitespace-nowrap text-secondary"><span className="text-status-critical-text">红</span> {thresholdText(band.red)}</div>
              <div className="whitespace-nowrap text-muted">{band.relApplicable ? `相对 ${formatPercent(band.relYellow)}` : '相对不适用'}</div>
            </div>
          ) : (
            <span className="text-muted">-</span>
          )}
        </td>
        <td className="py-3">
          <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getRecommendationStyle()}`}>
            {item.recommendation}
          </span>
        </td>
        <td className="pf-col-last py-3">
          <button
            onClick={() => onEdit(item)}
            aria-label={`编辑${item.name}目标`}
            className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted transition-colors hover:bg-subtle hover:text-secondary"
            title="编辑目标比例"
          >
            <Edit2 size={14} />
          </button>
        </td>
      </tr>
      {expanded &&
        hasChildren &&
        item.children.map((child) => (
          <AnalysisItemRow
            key={child.id}
            item={child}
            level={level + 1}
            onEdit={onEdit}
            editingId={editingId}
            totalAmount={totalAmount}
          />
        ))}
    </>
  )
}


function AnalysisItemCard({ item, level = 0, onEdit, totalAmount }: AnalysisItemRowProps) {
  const hasChildren = item.children && item.children.length > 0
  const { isExpanded, toggleExpanded } = useExpandedContext()
  const expanded = isExpanded(item.id)
  const deviation = parseFloat(item.deviation)
  const actualPercent = parseFloat(item.actual_percent)
  const targetPercent = item.target_percent ? parseFloat(item.target_percent) : null
  const band = bandValues(item)
  const targetAmount = targetPercent !== null && totalAmount
    ? (parseFloat(totalAmount) * targetPercent / 100)
    : null

  return (
    <li className={level > 0 ? 'ml-3 border-l-2 border-line pl-3' : undefined}>
      <div className="space-y-2 px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1">
            {hasChildren ? (
              <button
                type="button"
                onClick={() => toggleExpanded(item.id)}
                aria-expanded={expanded}
                aria-label={expanded ? `收起${item.name}` : `展开${item.name}`}
                className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted"
              >
                {expanded ? <ChevronDown size={16} aria-hidden /> : <ChevronRight size={16} aria-hidden />}
              </button>
            ) : <span className="w-2 shrink-0" />}
            <span className={level === 0 ? 'font-medium text-ink' : 'text-body text-secondary'}>{item.name}</span>
          </div>
          <button
            type="button"
            onClick={() => onEdit(item)}
            aria-label={`编辑${item.name}目标`}
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted"
          >
            <Edit2 size={16} aria-hidden />
          </button>
        </div>

        <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
          <div>
            <dt className="text-meta text-muted">目标</dt>
            <dd className="pf-num text-body text-ink">{targetPercent !== null ? formatPercent(targetPercent) : '—'}</dd>
          </div>
          <div>
            <dt className="text-meta text-muted">实际</dt>
            <dd className="pf-num text-body text-ink">{formatPercent(actualPercent)}</dd>
          </div>
          <div>
            <dt className="text-meta text-muted">目标金额</dt>
            <dd>{targetAmount !== null ? <Money value={targetAmount} unit="cny" size="xs" /> : <span className="text-muted">—</span>}</dd>
          </div>
          <div>
            <dt className="text-meta text-muted">实际金额</dt>
            <dd><Money value={item.actual_amount} unit="cny" size="xs" /></dd>
          </div>
          <div>
            <dt className="text-meta text-muted">偏离</dt>
            <dd>{targetPercent !== null ? <DirectionText value={deviation} unit="pp" size="xs" /> : <span className="text-muted">—</span>}</dd>
          </div>
          <div>
            <dt className="text-meta text-muted">偏离金额</dt>
            <dd>{item.deviation_amount ? <DirectionText value={item.deviation_amount} unit="cny" size="xs" /> : <span className="text-muted">—</span>}</dd>
          </div>
        </dl>

        <div className="flex flex-wrap items-center gap-2">
          <span className={`inline-flex items-center rounded-pill px-2 py-0.5 text-meta ${getRecommendationTone(item.recommendation)}`}>
            {item.recommendation}
          </span>
          {targetPercent !== null && (
            <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-muted">
              <span className="inline-flex items-center gap-1"><Bell size={12} aria-hidden /> 黄 {thresholdText(band.yellow)} · 红 {thresholdText(band.red)}</span>
              <span>{band.relApplicable ? `相对 ${formatPercent(band.relYellow)}` : '相对不适用'}</span>
            </span>
          )}
        </div>
      </div>
      {expanded && hasChildren && (
        <ul className="divide-y divide-line border-t border-line">
          {item.children.map((child) => (
            <AnalysisItemCard key={child.id} item={child} level={level + 1} onEdit={onEdit} totalAmount={totalAmount} />
          ))}
        </ul>
      )}
    </li>
  )
}

function getRecommendationTone(recommendation: string): string {
  if (recommendation.includes('减持')) return 'bg-status-critical-surface text-status-critical-text'
  if (recommendation.includes('增持')) return 'bg-status-good-surface text-status-good-text'
  if (recommendation.includes('合理')) return 'bg-brand-50 text-brand-700'
  return 'bg-subtle text-muted'
}

export function AnalysisTable({ items, onEdit, editingId, totalAmount }: AnalysisTableProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())

  const allIds = collectAllIds(items)

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const newSet = new Set(prev)
      if (newSet.has(id)) {
        newSet.delete(id)
      } else {
        newSet.add(id)
      }
      return newSet
    })
  }, [])

  const expandAll = useCallback(() => {
    setExpandedIds(new Set(allIds))
  }, [allIds])

  const collapseAll = useCallback(() => {
    setExpandedIds(new Set())
  }, [])

  const isExpanded = useCallback((id: string) => {
    return expandedIds.has(id)
  }, [expandedIds])

  const contextValue: ExpandedContextType = {
    expandedIds,
    toggleExpanded,
    expandAll,
    collapseAll,
    isExpanded,
  }

  const hasChildrenItems = hasAnyChildren(items)

  return (
    <ExpandedContext.Provider value={contextValue}>
      <div className="overflow-hidden">
        {/* Expand/Collapse Controls - only show if there are items with children */}
        {hasChildrenItems && (
          <div className="flex items-center justify-end gap-2 border-b border-line bg-subtle px-4 py-2">
            <button
              onClick={expandAll}
              className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control px-3 text-sm text-secondary transition-colors hover:bg-subtle"
              title="展开所有"
            >
              <FolderOpen size={16} />
              <span>展开全部</span>
            </button>
            <button
              onClick={collapseAll}
              className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control px-3 text-sm text-secondary transition-colors hover:bg-subtle"
              title="折叠所有"
            >
              <FolderClosed size={16} />
              <span>折叠全部</span>
            </button>
          </div>
        )}
        <div className="hidden xl:block overflow-x-auto">
          {/* 原生表（spec D6/D7）：树形子行 + 可编辑行撑不住 DataTable 的结构，就地套 .pf-table。
              字面类名（Tailwind JIT 按内容扫描）；列宽走 colgroup 读 tokens 档位；
              内边距由 .pf-table 统一给 40（原 px-4 会让每档丢 8px）。
              D8：本表保持 xl 才出表，断点不动。 */}
          <table className="pf-table" style={{ minWidth: ANALYSIS_TABLE_WIDTH }}>
            <colgroup>
              <col style={{ width: tokens.table.col['text-mid'] }} />
              <col style={{ width: tokens.table.col.ratio }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.ratio }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.ratio }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.ratio }} />
              <col style={{ width: tokens.table.col['text-short'] }} />
              <col style={{ width: ANALYSIS_ACTIONS_WIDTH }} />
            </colgroup>
            {/* thead 必须 sticky + z-sticky(20)：A10 要求表头 z 序 = 20，
                冻结表头格 z 取 20 才能盖住表体冻结格（10）。 */}
            <thead className="sticky top-0 z-sticky bg-subtle">
              <tr>
                <th className="pf-col-first py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
                  项目
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  目标%
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  目标金额
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  实际%
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  实际金额
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  偏离
                </th>
                <th className="py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  偏离金额
                </th>
                <th className="whitespace-normal py-3 text-center text-xs font-medium text-slate-500 uppercase tracking-wider">
                  阈值（黄 / 红）
                </th>
                <th className="py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
                  建议
                </th>
                <th className="pf-col-last py-3 text-center text-xs font-medium text-slate-500 uppercase tracking-wider">
                  操作
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {items.map((item) => (
                <AnalysisItemRow
                  key={item.id}
                  item={item}
                  onEdit={onEdit}
                  editingId={editingId}
                  totalAmount={totalAmount}
                />
              ))}
            </tbody>
          </table>
        </div>
        <ul className="divide-y divide-line xl:hidden">
          {items.map((item) => (
            <AnalysisItemCard key={item.id} item={item} onEdit={onEdit} totalAmount={totalAmount} />
          ))}
        </ul>
      </div>
    </ExpandedContext.Provider>
  )
}
