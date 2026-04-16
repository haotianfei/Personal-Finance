'use client'

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { AssetOwner } from '@/types'
import { Plus, Pencil, Trash2, X } from 'lucide-react'

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
function AssetOwnerForm({ initial, onSubmit, onCancel, isEdit = false }: {
  initial?: Partial<AssetOwner>; onSubmit: (data: any) => void; onCancel: () => void; isEdit?: boolean
}) {
  const [name, setName] = useState(initial?.name || '')
  const [description, setDescription] = useState(initial?.description || '')

  const handleSubmit = () => {
    if (!name.trim()) {
      alert('请输入拥有人名称')
      return
    }

    onSubmit({
      name: name.trim(),
      description: description.trim() || null,
    })
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">拥有人名称</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="如：张三"
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
          autoFocus
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-600 mb-1">描述（可选）</label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="如：家庭主要收入来源者"
          rows={3}
          className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 resize-none"
        />
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
export default function AssetOwnersPage() {
  const queryClient = useQueryClient()
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingOwner, setEditingOwner] = useState<AssetOwner | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const { data: assetOwners = [] } = useQuery({
    queryKey: ['assetOwners'],
    queryFn: api.getAssetOwners,
  })

  const createMutation = useMutation({
    mutationFn: api.createAssetOwner,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assetOwners'] })
      setShowAddModal(false)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: any }) => api.updateAssetOwner(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assetOwners'] })
      setEditingOwner(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteAssetOwner,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assetOwners'] })
      setDeleteError(null)
    },
    onError: (error: Error) => {
      setDeleteError(error.message || '删除失败，该拥有人可能已被资产记录引用')
    },
  })

  const handleDelete = (owner: AssetOwner) => {
    setDeleteError(null)
    if (confirm(`确认删除资产拥有人 "${owner.name}"？`)) {
      deleteMutation.mutate(owner.id)
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产拥有人管理</h1>
          <p className="text-sm text-slate-500 mt-1">管理资产拥有人信息</p>
        </div>
        <button
          onClick={() => setShowAddModal(true)}
          className="flex items-center gap-1.5 px-3 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700"
        >
          <Plus size={16} /> 新增拥有人
        </button>
      </div>

      {/* Error Alert */}
      {deleteError && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
          <div className="flex items-center gap-2">
            <span className="font-medium">删除失败：</span>
            <span>{deleteError}</span>
          </div>
          <p className="text-sm text-red-600 mt-1">该拥有人可能已被资产记录引用，无法删除。</p>
        </div>
      )}

      {/* List View */}
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100">
          <h3 className="font-semibold text-brand-950">拥有人列表（共 {assetOwners.length} 个）</h3>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-100">
              <th className="text-left px-4 py-3 text-slate-600 font-medium">拥有人名称</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">描述</th>
              <th className="text-right px-4 py-3 text-slate-600 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {assetOwners.map((owner) => (
              <tr key={owner.id} className="border-t border-slate-50 hover:bg-slate-50/50 transition-colors group">
                <td className="px-4 py-3 font-medium text-brand-950">{owner.name}</td>
                <td className="px-4 py-3 text-slate-600">{owner.description || '-'}</td>
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => setEditingOwner(owner)}
                      className="p-1.5 rounded hover:bg-brand-50 text-slate-400 hover:text-brand-600"
                      title="编辑"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      onClick={() => handleDelete(owner)}
                      className="p-1.5 rounded hover:bg-red-50 text-slate-400 hover:text-danger"
                      title="删除"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {assetOwners.length === 0 && (
              <tr>
                <td colSpan={3} className="px-4 py-12 text-center text-slate-400">
                  暂无资产拥有人，请点击右上角添加
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Add Modal */}
      <Modal
        open={showAddModal}
        onClose={() => setShowAddModal(false)}
        title="新增资产拥有人"
      >
        <AssetOwnerForm
          onSubmit={(data) => createMutation.mutate(data)}
          onCancel={() => setShowAddModal(false)}
        />
      </Modal>

      {/* Edit Modal */}
      <Modal
        open={editingOwner !== null}
        onClose={() => setEditingOwner(null)}
        title="编辑资产拥有人"
      >
        {editingOwner && (
          <AssetOwnerForm
            initial={editingOwner}
            isEdit
            onSubmit={(data) => updateMutation.mutate({ id: editingOwner.id, data })}
            onCancel={() => setEditingOwner(null)}
          />
        )}
      </Modal>
    </div>
  )
}
