'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { LiquidityRating } from '@/types'
import { Plus, Pencil, Trash2, X, ArrowUp, ArrowDown } from 'lucide-react'

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
function LiquidityRatingForm({ initial, onSubmit, onCancel, isEdit = false }: {
  initial?: Partial<LiquidityRating>; onSubmit: (data: any) => void; onCancel: () => void; isEdit?: boolean
}) {
  const [name, setName] = useState(initial?.name || '')
  const [sortOrder, setSortOrder] = useState(initial?.sort_order?.toString() || '0')

  const handleSubmit = () => {
    if (!name.trim()) {
      alert('请输入流动性评级名称')
      return
    }

    onSubmit({
      name: name.trim(),
      sort_order: parseInt(sortOrder) || 0,
    })
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">评级名称</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="如：T+0"
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
          autoFocus
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">排序顺序</label>
        <input
          type="number"
          value={sortOrder}
          onChange={(e) => setSortOrder(e.target.value)}
          placeholder="0"
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
        />
        <p className="text-xs text-slate-400 mt-1">数字越小排序越靠前</p>
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

// --- Main Page ---
export default function LiquidityRatingsPage() {
  const queryClient = useQueryClient()
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingRating, setEditingRating] = useState<LiquidityRating | null>(null)

  const { data: ratings = [] } = useQuery({
    queryKey: ['liquidityRatings'],
    queryFn: api.getLiquidityRatings,
  })

  const createMutation = useMutation({
    mutationFn: api.createLiquidityRating,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['liquidityRatings'] })
      setShowAddModal(false)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: any }) => api.updateLiquidityRating(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['liquidityRatings'] })
      setEditingRating(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteLiquidityRating,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['liquidityRatings'] })
    },
    onError: (error: any) => {
      alert(error?.message || '删除失败，该流动性评级可能正在被使用')
    },
  })

  const handleDelete = (rating: LiquidityRating) => {
    if (confirm(`确认删除流动性评级 "${rating.name}"？\n\n注意：如果该评级正在被资产记录使用，将无法删除。`)) {
      deleteMutation.mutate(rating.id)
    }
  }

  const moveUp = (index: number) => {
    if (index === 0) return
    const current = ratings[index]
    const prev = ratings[index - 1]
    updateMutation.mutate({ id: current.id, data: { sort_order: prev.sort_order } })
    updateMutation.mutate({ id: prev.id, data: { sort_order: current.sort_order } })
  }

  const moveDown = (index: number) => {
    if (index === ratings.length - 1) return
    const current = ratings[index]
    const next = ratings[index + 1]
    updateMutation.mutate({ id: current.id, data: { sort_order: next.sort_order } })
    updateMutation.mutate({ id: next.id, data: { sort_order: current.sort_order } })
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">流动性评级管理</h1>
          <p className="text-sm text-slate-500 mt-1">管理资产流动性评级信息</p>
        </div>
        <button
          onClick={() => setShowAddModal(true)}
          className="flex items-center gap-1.5 px-3 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700"
        >
          <Plus size={16} /> 新增评级
        </button>
      </div>

      {/* List View */}
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100">
          <h3 className="font-semibold text-brand-950">评级列表（共 {ratings.length} 个）</h3>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-100">
              <th className="text-left px-4 py-3 text-slate-600 font-medium">排序</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">评级名称</th>
              <th className="text-right px-4 py-3 text-slate-600 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {ratings.map((rating, index) => (
              <tr key={rating.id} className="border-t border-slate-50 hover:bg-slate-50/50 transition-colors group">
                <td className="px-4 py-3 text-slate-500">{rating.sort_order}</td>
                <td className="px-4 py-3 font-medium text-brand-950">{rating.name}</td>
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => moveUp(index)}
                      disabled={index === 0}
                      className="p-1.5 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600 disabled:opacity-30"
                      title="上移"
                    >
                      <ArrowUp size={14} />
                    </button>
                    <button
                      onClick={() => moveDown(index)}
                      disabled={index === ratings.length - 1}
                      className="p-1.5 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600 disabled:opacity-30"
                      title="下移"
                    >
                      <ArrowDown size={14} />
                    </button>
                    <button
                      onClick={() => setEditingRating(rating)}
                      className="p-1.5 rounded hover:bg-brand-50 text-slate-400 hover:text-brand-600"
                      title="编辑"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      onClick={() => handleDelete(rating)}
                      className="p-1.5 rounded hover:bg-red-50 text-slate-400 hover:text-danger"
                      title="删除"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {ratings.length === 0 && (
              <tr>
                <td colSpan={3} className="px-4 py-12 text-center text-slate-400">
                  暂无流动性评级，请点击右上角添加
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Add Modal */}
      <Modal open={showAddModal} onClose={() => setShowAddModal(false)} title="新增流动性评级">
        <LiquidityRatingForm
          onSubmit={(data) => createMutation.mutate(data)}
          onCancel={() => setShowAddModal(false)}
        />
      </Modal>

      {/* Edit Modal */}
      <Modal open={editingRating !== null} onClose={() => setEditingRating(null)} title="编辑流动性评级">
        {editingRating && (
          <LiquidityRatingForm
            initial={editingRating}
            isEdit
            onSubmit={(data) => updateMutation.mutate({ id: editingRating.id, data })}
            onCancel={() => setEditingRating(null)}
          />
        )}
      </Modal>
    </div>
  )
}
