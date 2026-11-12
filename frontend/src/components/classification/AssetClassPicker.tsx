'use client'

import { useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, ChevronRight, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { AssetClass } from '@/types'
import { buildClassTree, classFitsSide, sideScopeText } from './classification'
import type { AssetClassNode } from './classification'
import { FieldFrame, triggerClass, triggerErrorClass } from './FieldFrame'
import { FloatingPanel } from './FloatingPanel'

/**
 * 资产类别选择器：树形展开 + 搜索，**只允许选末级**。
 *
 * 类别树的三条硬规则在界面上各有落点：
 * - 落点必须叶子 → 有子类的节点只能展开，不给出「选中」语义，并明说还有下层；
 * - 类别方向须与资产 / 负债一致 → 冲突的叶子仍然可见（用户得知道它存在），
 *   但当场标出方向并在选中后提示；真正的裁决仍由后端做。
 */

export interface AssetClassPickerProps {
  classes: readonly AssetClass[]
  value: number | null
  onChange: (value: number | null) => void
  /** 该项已知的资产 / 负债方向；null＝未知（不设提示）。 */
  side?: 'asset' | 'liability' | null
  label?: React.ReactNode
  required?: boolean
  error?: string
  hint?: React.ReactNode
  disabled?: boolean
  placeholder?: string
  clearable?: boolean
  loading?: boolean
}

function nodeLabel(node: AssetClass): string {
  return node.code ? `${node.code} ${node.name}` : node.name
}

export function AssetClassPicker({
  classes,
  value,
  onChange,
  side = null,
  label = '资产类别',
  required = false,
  error,
  hint,
  disabled = false,
  placeholder = '选择资产类别',
  clearable = false,
  loading = false,
}: AssetClassPickerProps) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())
  const triggerRef = useRef<HTMLButtonElement>(null)

  const tree = useMemo(() => buildClassTree(classes), [classes])
  const byId = useMemo(() => new Map(classes.map((node) => [node.id, node])), [classes])
  const selected = value == null ? null : byId.get(value) ?? null

  const keyword = search.trim().toLowerCase()
  const searchHits = useMemo(() => {
    if (!keyword) return []
    return classes
      .filter((node) => node.leaf)
      .filter((node) => `${node.path} ${node.code} ${node.name}`.toLowerCase().includes(keyword))
  }, [classes, keyword])

  const close = () => {
    setOpen(false)
    setSearch('')
  }

  const pick = (node: AssetClass) => {
    if (!node.leaf) return
    onChange(node.id)
    close()
  }

  const toggle = (node: AssetClass) => {
    setExpanded((previous) => {
      const next = new Set(previous)
      if (next.has(node.id)) next.delete(node.id)
      else next.add(node.id)
      return next
    })
  }

  const renderRows = (nodes: AssetClassNode[], depth: number): React.ReactNode =>
    nodes.map((entry) => {
      const node = entry.node
      const hasChildren = entry.children.length > 0
      const isOpen = expanded.has(node.id) || keyword !== ''
      const isSelected = value === node.id
      const conflicting = !classFitsSide(node, side)

      return (
        <li key={node.id}>
          <button
            type="button"
            aria-expanded={hasChildren ? isOpen : undefined}
            onClick={() => (hasChildren ? toggle(node) : pick(node))}
            className={cn(
              'flex w-full items-center gap-2 rounded-control py-2 pr-2 text-left transition-colors duration-fast',
              isSelected ? 'bg-brand-50 text-brand-800' : 'text-ink hover:bg-subtle',
            )}
            style={{ paddingLeft: `${8 + depth * 16}px` }}
          >
            <span className="flex h-4 w-4 shrink-0 items-center justify-center text-muted">
              {hasChildren
                ? (isOpen ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />)
                : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className={cn('block truncate text-body', hasChildren ? 'font-medium' : 'font-normal')}>
                {nodeLabel(node)}
              </span>
              <span className="block truncate text-micro text-muted">
                {hasChildren
                  ? `还有 ${entry.children.length} 个子类，展开继续选`
                  : conflicting
                    ? `${sideScopeText(node.side_scope)}类别，与该项方向不一致`
                    : sideScopeText(node.side_scope) === '通用' ? '' : `${sideScopeText(node.side_scope)}类别`}
              </span>
            </span>
            {!hasChildren && isSelected && <Check size={15} className="shrink-0 text-brand-600" aria-hidden />}
          </button>
          {hasChildren && isOpen && <ul className="space-y-0.5">{renderRows(entry.children, depth + 1)}</ul>}
        </li>
      )
    })

  return (
    <FieldFrame label={label} required={required} error={error} hint={hint}>
      <div className="flex items-stretch gap-1.5">
        <button
          ref={triggerRef}
          type="button"
          disabled={disabled}
          onClick={() => setOpen((previous) => !previous)}
          aria-label={typeof label === 'string' ? label : '资产类别'}
          aria-haspopup="dialog"
          aria-expanded={open}
          className={cn(
            triggerClass,
            'flex items-center gap-2 text-left',
            error && triggerErrorClass,
            !selected && 'text-muted',
          )}
        >
          <span className="min-w-0 flex-1 truncate" title={selected?.path}>
            {selected ? selected.path : placeholder}
          </span>
          <ChevronDown size={16} className={cn('shrink-0 text-muted transition-transform', open && 'rotate-180')} aria-hidden />
        </button>
        {clearable && selected && !disabled && (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="shrink-0 rounded-control border border-line-strong bg-surface px-3 text-meta text-secondary"
          >
            清除
          </button>
        )}
      </div>

      <FloatingPanel open={open} anchorRef={triggerRef} onClose={close} label="选择资产类别">
        <div className="shrink-0 border-b border-line p-2">
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
            <input
              autoFocus
              type="text"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索类别名称或编号"
              aria-label="搜索资产类别"
              className="min-h-[40px] w-full rounded-control border border-line bg-surface pl-7 pr-2 text-body text-ink placeholder:text-muted"
            />
          </div>
        </div>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-1.5">
          {loading && <li className="px-2 py-3 text-meta text-muted">正在加载类别…</li>}
          {!loading && classes.length === 0 && (
            <li className="px-2 py-3 text-meta text-muted">
              还没有资产类别。先到「设置 → 字典 → 资产类别」建一棵树。
            </li>
          )}
          {!loading && classes.length > 0 && keyword !== '' && searchHits.length === 0 && (
            <li className="px-2 py-3 text-meta text-muted">没有匹配的末级分类，换个词试试。</li>
          )}
          {!loading && keyword !== '' && searchHits.length > 0 && (
            <li>
              <ul className="space-y-0.5">
                {searchHits.map((node) => (
                  <li key={node.id}>
                    <button
                      type="button"
                      onClick={() => pick(node)}
                      className={cn(
                        'flex w-full items-start gap-2 rounded-control px-2 py-2 text-left',
                        value === node.id ? 'bg-brand-50' : 'hover:bg-subtle',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-body text-ink">{nodeLabel(node)}</span>
                        <span className="block truncate text-micro text-muted" title={node.path}>{node.path}</span>
                      </span>
                      {value === node.id && <Check size={15} className="mt-1 shrink-0 text-brand-600" aria-hidden />}
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          )}
          {!loading && keyword === '' && renderRows(tree, 0)}
        </ul>
      </FloatingPanel>
    </FieldFrame>
  )
}