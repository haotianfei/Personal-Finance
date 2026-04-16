'use client'

import { useState, createContext, useContext, useCallback } from 'react'
import { ChevronDown, ChevronRight, Edit2, TrendingUp, TrendingDown, Minus, FolderOpen, FolderClosed } from 'lucide-react'
import { formatAmount } from '@/lib/utils'
import type { AllocationAnalysisItem } from '@/types'

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

function AnalysisItemRow({ item, level = 0, onEdit, editingId, totalAmount }: AnalysisItemRowProps) {
  const hasChildren = item.children && item.children.length > 0
  const { isExpanded, toggleExpanded } = useExpandedContext()
  const expanded = isExpanded(item.id)
  const isEditing = editingId === item.id

  const deviation = parseFloat(item.deviation)
  const actualPercent = parseFloat(item.actual_percent)
  const targetPercent = item.target_percent ? parseFloat(item.target_percent) : null

  // Calculate target amount
  const targetAmount = targetPercent !== null && totalAmount
    ? (parseFloat(totalAmount) * targetPercent / 100).toFixed(2)
    : null

  // Calculate deviation amount if available
  const deviationAmount = item.deviation_amount ? parseFloat(item.deviation_amount) : null

  // Determine deviation color and icon
  const getDeviationStyle = () => {
    if (!targetPercent) return { color: 'text-slate-400', icon: Minus }
    if (deviation > 5) return { color: 'text-red-600', icon: TrendingUp }
    if (deviation < -5) return { color: 'text-green-600', icon: TrendingDown }
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
      <tr
        className={`hover:bg-slate-50 transition-colors ${
          level > 0 ? 'bg-slate-50/50' : ''
        } ${isEditing ? 'bg-brand-50' : ''} group`}
      >
        <td className="px-4 py-3">
          <div className="flex items-center" style={{ paddingLeft: `${level * 24}px` }}>
            {hasChildren && (
              <button
                onClick={() => toggleExpanded(item.id)}
                className="mr-2 p-0.5 rounded hover:bg-slate-200 transition-colors"
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
        <td className="px-4 py-3 text-right">
          {targetPercent !== null ? (
            <span className="font-medium text-slate-900">{targetPercent.toFixed(2)}%</span>
          ) : (
            <span className="text-slate-400">-</span>
          )}
        </td>
        <td className="px-4 py-3 text-right">
          {targetAmount !== null ? (
            <span className="font-medium text-slate-900">{formatAmount(targetAmount)}</span>
          ) : (
            <span className="text-slate-400">-</span>
          )}
        </td>
        <td className="px-4 py-3 text-right">
          <span className="font-medium text-slate-900">{actualPercent.toFixed(2)}%</span>
        </td>
        <td className="px-4 py-3 text-right">
          <span className="text-slate-700">{formatAmount(item.actual_amount)}</span>
        </td>
        <td className={`px-4 py-3 text-right font-medium ${deviationStyle.color}`}>
          <div className="flex items-center justify-end gap-1">
            <DeviationIcon size={14} />
            <span>{deviation > 0 ? '+' : ''}{deviation.toFixed(2)}%</span>
          </div>
        </td>
        <td className="px-4 py-3 text-right">
          {deviationAmount !== null ? (
            <span className={`font-medium ${deviation > 0 ? 'text-red-600' : 'text-green-600'}`}>
              {deviation > 0 ? '+' : ''}{formatAmount(item.deviation_amount!)}
            </span>
          ) : (
            <span className="text-slate-400">-</span>
          )}
        </td>
        <td className="px-4 py-3">
          <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getRecommendationStyle()}`}>
            {item.recommendation}
          </span>
        </td>
        <td className="px-4 py-3">
          <button
            onClick={() => onEdit(item)}
            className="p-1.5 rounded-lg hover:bg-slate-200 text-slate-400 hover:text-slate-600 opacity-0 group-hover:opacity-100 transition-all"
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
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        {/* Expand/Collapse Controls - only show if there are items with children */}
        {hasChildrenItems && (
          <div className="flex items-center justify-end gap-2 px-4 py-2 border-b border-slate-200 bg-slate-50">
            <button
              onClick={expandAll}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 hover:text-slate-900 hover:bg-slate-200 rounded-lg transition-colors"
              title="展开所有"
            >
              <FolderOpen size={16} />
              <span>展开全部</span>
            </button>
            <button
              onClick={collapseAll}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 hover:text-slate-900 hover:bg-slate-200 rounded-lg transition-colors"
              title="折叠所有"
            >
              <FolderClosed size={16} />
              <span>折叠全部</span>
            </button>
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-slate-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
                  项目
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  目标%
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  目标金额
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  实际%
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  实际金额
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  偏离
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-500 uppercase tracking-wider">
                  偏离金额
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
                  建议
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-slate-500 uppercase tracking-wider w-16">
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
      </div>
    </ExpandedContext.Provider>
  )
}
