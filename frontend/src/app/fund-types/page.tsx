'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { FundType } from '@/types'
import { Plus, Pencil, Trash2, X, ChevronRight, ChevronDown } from 'lucide-react'

// --- Modal Component ---
function Modal({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: React.ReactNode
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[85vh] overflow-auto animate-slide-up"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <h3 className="text-lg font-semibold text-brand-950">{title}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X size={20} /></button>
        </div>
        <div className="p-6">{children}</div>
      </div>
    </div>
  )
}

// --- Form Component ---
function FundTypeForm({ fundTypes, initial, onSubmit, onCancel, isEdit = false }: {
  fundTypes: FundType[]; initial?: Partial<FundType>; onSubmit: (data: any) => void; onCancel: () => void; isEdit?: boolean
}) {
  const [name, setName] = useState(initial?.name || '')
  const [parentId, setParentId] = useState<number | null>(initial?.parent_id ?? null)
  const [level, setLevel] = useState(initial?.level ?? 0)

  // Filter out current node and its descendants from parent options when editing
  const getParentOptions = () => {
    if (!isEdit || !initial?.id) return fundTypes
    const excludeIds = new Set<number>()
    const collectDescendants = (node: FundType) => {
      excludeIds.add(node.id)
      node.children?.forEach(collectDescendants)
    }
    collectDescendants(initial as FundType)
    return fundTypes.filter((ft) => !excludeIds.has(ft.id))
  }

  const parentOptions = getParentOptions()

  const handleSubmit = () => {
    if (!name.trim()) {
      alert('请输入资产类型名称')
      return
    }
    
    let calculatedLevel = level
    if (parentId !== null && parentId !== undefined) {
      const parent = fundTypes.find((ft) => ft.id === parentId)
      calculatedLevel = parent ? parent.level + 1 : 0
    } else {
      calculatedLevel = 0
    }

    onSubmit({
      name: name.trim(),
      parent_id: parentId,
      level: calculatedLevel,
    })
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">名称</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="如：流动资产"
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
          autoFocus
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">父级类型（可选）</label>
        <select
          value={parentId ?? ''}
          onChange={(e) => setParentId(e.target.value ? parseInt(e.target.value) : null)}
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
        >
          <option value="">无（作为顶级类型）</option>
          {parentOptions.map((ft) => (
            <option key={ft.id} value={ft.id}>
              {'  '.repeat(ft.level)}{ft.name}
            </option>
          ))}
        </select>
      </div>

      <div className="flex justify-end gap-3 pt-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
        >
          取消
        </button>
        <button
          onClick={handleSubmit}
          className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700"
        >
          {isEdit ? '保存修改' : '创建'}
        </button>
      </div>
    </div>
  )
}

// --- Tree Node Component ---
function TreeNode({
  node,
  allNodes,
  onEdit,
  onDelete,
}: {
  node: FundType;
  allNodes: FundType[];
  onEdit: (node: FundType) => void;
  onDelete: (node: FundType) => void;
}) {
  const [expanded, setExpanded] = useState(true)
  const children = allNodes.filter((n) => n.parent_id === node.id)
  const hasChildren = children.length > 0

  return (
    <div className="select-none">
      <div className="flex items-center gap-2 py-2 px-3 hover:bg-slate-50 rounded-lg group transition-colors">
        {hasChildren ? (
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1 hover:bg-slate-200 rounded text-slate-500"
          >
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <div className="w-6" />
        )}
        
        <span className="flex-1 font-medium text-slate-700">{node.name}</span>
        
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={() => onEdit(node)}
            className="p-1.5 rounded hover:bg-brand-50 text-slate-400 hover:text-brand-600"
            title="编辑"
          >
            <Pencil size={14} />
          </button>
          <button
            onClick={() => onDelete(node)}
            className="p-1.5 rounded hover:bg-red-50 text-slate-400 hover:text-danger"
            title="删除"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      {expanded && hasChildren && (
        <div className="ml-6 border-l-2 border-slate-100 pl-2">
          {children.map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              allNodes={allNodes}
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// --- Main Page ---
export default function FundTypesPage() {
  const queryClient = useQueryClient()
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingNode, setEditingNode] = useState<FundType | null>(null)

  const { data: fundTypes = [] } = useQuery({
    queryKey: ['fundTypes'],
    queryFn: api.getFundTypes,
  })

  const createMutation = useMutation({
    mutationFn: api.createFundType,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['fundTypes'] })
      setShowAddModal(false)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: any }) => api.updateFundType(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['fundTypes'] })
      setEditingNode(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteFundType,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['fundTypes'] })
    },
  })

  const handleDelete = (node: FundType) => {
    const hasChildren = fundTypes.some((ft) => ft.parent_id === node.id)
    if (hasChildren) {
      alert('无法删除包含子级的资产类型')
      return
    }
    if (confirm(`确认删除资产类型 "${node.name}"？`)) {
      deleteMutation.mutate(node.id)
    }
  }

  // Get root nodes (no parent)
  const rootNodes = fundTypes.filter((ft) => !ft.parent_id)

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产类型管理</h1>
          <p className="text-sm text-slate-500 mt-1">管理资产类型的层级结构</p>
        </div>
        <button
          onClick={() => setShowAddModal(true)}
          className="flex items-center gap-1.5 px-3 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700"
        >
          <Plus size={16} /> 新增类型
        </button>
      </div>

      {/* Tree View */}
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100">
          <h3 className="font-semibold text-brand-950">类型列表（共 {fundTypes.length} 个）</h3>
        </div>
        <div className="p-6">
          {rootNodes.length > 0 ? (
            <div className="space-y-1">
              {rootNodes.map((node) => (
                <TreeNode
                  key={node.id}
                  node={node}
                  allNodes={fundTypes}
                  onEdit={setEditingNode}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          ) : (
            <div className="text-center py-12 text-slate-400">
              暂无资产类型，请点击右上角添加
            </div>
          )}
        </div>
      </div>

      {/* Add Modal */}
      <Modal
        open={showAddModal}
        onClose={() => setShowAddModal(false)}
        title="新增资产类型"
      >
        <FundTypeForm
          fundTypes={fundTypes}
          onSubmit={(data) => createMutation.mutate(data)}
          onCancel={() => setShowAddModal(false)}
        />
      </Modal>

      {/* Edit Modal */}
      <Modal
        open={editingNode !== null}
        onClose={() => setEditingNode(null)}
        title="编辑资产类型"
      >
        {editingNode && (
          <FundTypeForm
            fundTypes={fundTypes}
            initial={editingNode}
            isEdit
            onSubmit={(data) => updateMutation.mutate({ id: editingNode.id, data })}
            onCancel={() => setEditingNode(null)}
          />
        )}
      </Modal>
    </div>
  )
}
