'use client'

import { useState, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatAmount, formatDate, formatFileSize } from '@/lib/utils'
import type { ImportPreviewResponse, ImportBatch, NewAttribute } from '@/types'
import { Upload, FileText, CheckCircle, AlertTriangle, X, Plus, EyeOff, Sparkles, Database, History } from 'lucide-react'

// --- Backup List Component ---
function BackupList() {
  const { data: backups, isLoading } = useQuery({
    queryKey: ['importBackups'],
    queryFn: api.getImportBackups,
  })
  
  const queryClient = useQueryClient()
  
  const deleteMutation = useMutation({
    mutationFn: (filename: string) => api.deleteImportBackup(filename),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['importBackups'] })
    },
  })
  
  if (isLoading) return <div className="p-4 text-center text-slate-500">加载中...</div>
  
  if (!backups || backups.length === 0) {
    return <div className="p-4 text-center text-slate-400">暂无备份文件</div>
  }
  
  return (
    <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
      <div className="px-4 py-3 bg-slate-50 border-b border-slate-200">
        <h4 className="text-sm font-medium text-slate-700">备份文件列表</h4>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-50/50">
            <th className="text-left px-4 py-2 text-slate-600 font-medium">文件名</th>
            <th className="text-right px-4 py-2 text-slate-600 font-medium">大小</th>
            <th className="text-left px-4 py-2 text-slate-600 font-medium">创建时间</th>
            <th className="text-center px-4 py-2 text-slate-600 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {backups.map((backup) => (
            <tr key={backup.filename} className="border-t border-slate-100">
              <td className="px-4 py-2 font-mono text-xs text-slate-600">{backup.filename}</td>
              <td className="px-4 py-2 text-right text-slate-500">{formatFileSize(backup.size)}</td>
              <td className="px-4 py-2 text-slate-500">{new Date(backup.created_at).toLocaleString('zh-CN')}</td>
              <td className="px-4 py-2 text-center">
                <button
                  onClick={() => {
                    if (confirm(`确定要删除备份 "${backup.filename}" 吗？`)) {
                      deleteMutation.mutate(backup.filename)
                    }
                  }}
                  className="text-xs text-red-600 hover:text-red-700 px-2 py-1 rounded hover:bg-red-50"
                  disabled={deleteMutation.isPending}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --- New Attributes Panel ---
function NewAttributesPanel({
  newAttributes,
  attributeActions,
  onActionChange
}: {
  newAttributes: NewAttribute[]
  attributeActions: Record<string, string>
  onActionChange: (name: string, action: string) => void
}) {
  const typeLabels: Record<string, string> = {
    liquidity_rating: '流动性评级',
    fund_type: '资产类型',
    account: '账户'
  }

  const typeColors: Record<string, string> = {
    liquidity_rating: 'bg-blue-50 text-blue-700 border-blue-200',
    fund_type: 'bg-purple-50 text-purple-700 border-purple-200',
    account: 'bg-orange-50 text-orange-700 border-orange-200'
  }

  return (
    <div className="px-6 py-4 bg-indigo-50 border-b border-indigo-200">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Sparkles size={16} className="text-indigo-600" />
          <span className="text-sm font-medium text-indigo-800">
            发现 {newAttributes.length} 个新属性
          </span>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => {
              newAttributes.forEach(attr => {
                onActionChange(attr.name, 'create')
              })
            }}
            className="px-3 py-1 text-xs text-white bg-indigo-600 rounded hover:bg-indigo-700"
          >
            全部创建
          </button>
          <button
            type="button"
            onClick={() => {
              newAttributes.forEach(attr => {
                onActionChange(attr.name, 'ignore')
              })
            }}
            className="px-3 py-1 text-xs text-indigo-600 border border-indigo-300 rounded hover:bg-indigo-100"
          >
            全部忽略
          </button>
        </div>
      </div>
      <div className="space-y-2">
        {newAttributes.map((attr) => {
          const action = attributeActions[attr.name] || 'create'
          return (
            <div
              key={`${attr.type}-${attr.name}`}
              className="flex items-center justify-between p-2 bg-white rounded border border-indigo-100"
            >
              <div className="flex items-center gap-3">
                <span className={`text-xs px-2 py-0.5 rounded border ${typeColors[attr.type]}`}>
                  {typeLabels[attr.type]}
                </span>
                <span className="text-sm font-medium text-slate-700">{attr.name}</span>
                <span className="text-xs text-slate-400">涉及 {attr.rows.length} 行</span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => onActionChange(attr.name, 'create')}
                  className={`flex items-center gap-1 px-2 py-1 text-xs rounded ${
                    action === 'create'
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'text-slate-500 hover:bg-slate-100'
                  }`}
                >
                  <Plus size={12} />
                  创建
                </button>
                <button
                  onClick={() => onActionChange(attr.name, 'ignore')}
                  className={`flex items-center gap-1 px-2 py-1 text-xs rounded ${
                    action === 'ignore'
                      ? 'bg-slate-200 text-slate-700'
                      : 'text-slate-500 hover:bg-slate-100'
                  }`}
                >
                  <EyeOff size={12} />
                  忽略
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function ImportPage() {
  const queryClient = useQueryClient()
  const [dragOver, setDragOver] = useState(false)
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [conflictRes, setConflictRes] = useState('skip')
  const [importResult, setImportResult] = useState<ImportBatch | null>(null)
  const [attributeActions, setAttributeActions] = useState<Record<string, string>>({})

  const { data: history } = useQuery({ queryKey: ['importHistory'], queryFn: api.getImportHistory })

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadCsv(file),
    onSuccess: (data) => {
      setPreview(data)
      // 初始化属性操作，默认全部创建
      const initialActions: Record<string, string> = {}
      data.new_attributes.forEach(attr => {
        initialActions[attr.name] = 'create'
      })
      setAttributeActions(initialActions)
    },
  })

  const [backupInfo, setBackupInfo] = useState<{ filename: string; showBackups: boolean } | null>(null)

  const confirmMutation = useMutation({
    mutationFn: () => api.confirmImport(selectedFile!, conflictRes, attributeActions),
    onSuccess: (data) => {
      setImportResult(data)
      // 显示备份信息
      if (data.backup_filename) {
        setBackupInfo({ filename: data.backup_filename, showBackups: false })
      }
      setPreview(null)
      setSelectedFile(null)
      setAttributeActions({})
      queryClient.invalidateQueries({ queryKey: ['importHistory'] })
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
      // 刷新维度数据
      queryClient.invalidateQueries({ queryKey: ['fundTypes'] })
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['liquidityRatings'] })
      queryClient.invalidateQueries({ queryKey: ['assetNames'] })
    },
  })

  const handleFile = useCallback((file: File) => {
    if (!file.name.endsWith('.csv')) return
    setSelectedFile(file)
    setImportResult(null)
    uploadMutation.mutate(file)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) handleFile(file)
  }, [handleFile])

  const handleAttributeActionChange = (name: string, action: string) => {
    setAttributeActions(prev => ({ ...prev, [name]: action }))
  }

  // 检查是否有被忽略的属性
  const hasIgnoredAttributes = preview?.new_attributes.some(
    attr => attributeActions[attr.name] === 'ignore'
  )

  // 获取包含新属性的行号集合
  const newAttributeRows = new Set<number>()
  preview?.new_attributes.forEach(attr => {
    attr.rows.forEach(row => newAttributeRows.add(row))
  })

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold text-brand-950">数据导入</h1>
        <p className="text-sm text-slate-500 mt-1">从 CSV 文件导入资产记录</p>
      </div>

      {/* Success Banner */}
      {importResult && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 p-4 bg-emerald-50 rounded-xl border border-emerald-200">
            <CheckCircle size={20} className="text-emerald-600 shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-medium text-emerald-800">
                导入成功: {importResult.filename} ({importResult.record_count} 条记录)
              </p>
            </div>
            <button onClick={() => { setImportResult(null); setBackupInfo(null) }} className="text-emerald-400 hover:text-emerald-600"><X size={16} /></button>
          </div>
          
          {/* Backup Info Banner */}
          {backupInfo && (
            <div className="flex items-center gap-3 p-4 bg-blue-50 rounded-xl border border-blue-200">
              <Database size={20} className="text-blue-600 shrink-0" />
              <div className="flex-1">
                <p className="text-sm font-medium text-blue-800">
                  已自动创建备份: {backupInfo.filename}
                </p>
                <p className="text-xs text-blue-600 mt-0.5">
                  导入前的数据已备份，可在需要时恢复
                </p>
              </div>
              <button 
                onClick={() => setBackupInfo({ ...backupInfo, showBackups: !backupInfo.showBackups })}
                className="flex items-center gap-1 px-3 py-1.5 text-xs text-blue-700 bg-blue-100 rounded hover:bg-blue-200"
              >
                <History size={14} />
                {backupInfo.showBackups ? '隐藏' : '查看备份'}
              </button>
            </div>
          )}
          
          {/* Backup List */}
          {backupInfo?.showBackups && <BackupList />}
        </div>
      )}

      {/* Upload Zone */}
      {!preview && (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors ${
            dragOver ? 'border-brand-400 bg-brand-50/50' : 'border-slate-200 hover:border-slate-300 bg-white'
          }`}
        >
          <Upload size={40} className="mx-auto text-slate-300 mb-4" />
          <p className="text-slate-600 font-medium mb-1">拖拽 CSV 文件到此处</p>
          <p className="text-sm text-slate-400 mb-4">或点击选择文件</p>
          <label className="inline-block px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 cursor-pointer">
            选择文件
            <input type="file" accept=".csv" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f) }} />
          </label>
          {uploadMutation.isPending && (
            <p className="mt-4 text-sm text-brand-600">解析中...</p>
          )}
        </div>
      )}

      {/* Preview */}
      {preview && (
        <div className="bg-white rounded-xl shadow-card overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <FileText size={20} className="text-brand-600" />
              <div>
                <p className="font-medium text-brand-950">{preview.filename}</p>
                <p className="text-sm text-slate-500">
                  {preview.valid_rows.length} 条有效 | {preview.invalid_rows.length} 条无效 | {preview.conflict_count} 条冲突
                  {preview.new_attributes.length > 0 && ` | ${preview.new_attributes.length} 个新属性`}
                </p>
              </div>
            </div>
            <button onClick={() => { setPreview(null); setSelectedFile(null); setAttributeActions({}) }}
              className="text-slate-400 hover:text-slate-600"><X size={20} /></button>
          </div>

          {/* New Attributes Panel */}
          {preview.new_attributes.length > 0 && (
            <NewAttributesPanel
              newAttributes={preview.new_attributes}
              attributeActions={attributeActions}
              onActionChange={handleAttributeActionChange}
            />
          )}

          {preview.conflict_count > 0 && (
            <div className="px-6 py-3 bg-amber-50 border-b border-amber-200 flex items-center gap-3">
              <AlertTriangle size={16} className="text-amber-600" />
              <span className="text-sm text-amber-800">发现 {preview.conflict_count} 条数据冲突</span>
              <select value={conflictRes} onChange={(e) => setConflictRes(e.target.value)}
                className="ml-auto px-2 py-1 border border-amber-300 rounded text-sm bg-white">
                <option value="skip">跳过冲突</option>
                <option value="overwrite">覆盖已有</option>
              </select>
            </div>
          )}

          <div className="max-h-[400px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 sticky top-0">
                <tr>
                  <th className="text-left px-4 py-2 text-slate-600 font-medium">行号</th>
                  <th className="text-left px-4 py-2 text-slate-600 font-medium">日期</th>
                  <th className="text-left px-4 py-2 text-slate-600 font-medium">资产名称</th>
                  <th className="text-left px-4 py-2 text-slate-600 font-medium">类型</th>
                  <th className="text-left px-4 py-2 text-slate-600 font-medium">账户</th>
                  <th className="text-right px-4 py-2 text-slate-600 font-medium">金额</th>
                  <th className="text-center px-4 py-2 text-slate-600 font-medium">状态</th>
                </tr>
              </thead>
              <tbody>
                {preview.valid_rows.map((row) => {
                  const hasNewAttribute = newAttributeRows.has(row.row_num)
                  return (
                    <tr key={row.row_num}
                      className={`border-t border-slate-50 ${row.has_conflict ? 'bg-amber-50/50' : ''} ${hasNewAttribute ? 'bg-indigo-50/30' : ''}`}>
                      <td className="px-4 py-2 text-slate-400">
                        {row.row_num}
                        {hasNewAttribute && <span className="ml-1 text-indigo-500">*</span>}
                      </td>
                      <td className="px-4 py-2">{row.asset_date}</td>
                      <td className="px-4 py-2 font-medium">{row.asset_name}</td>
                      <td className="px-4 py-2 text-slate-500">{row.fund_type}</td>
                      <td className="px-4 py-2 text-slate-500">{row.account}</td>
                      <td className={`px-4 py-2 text-right font-mono ${parseFloat(row.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
                        {formatAmount(row.amount)}
                      </td>
                      <td className="px-4 py-2 text-center">
                        {row.has_conflict ? (
                          <span className="inline-block px-2 py-0.5 rounded text-xs bg-amber-100 text-amber-700">冲突</span>
                        ) : hasNewAttribute ? (
                          <span className="inline-block px-2 py-0.5 rounded text-xs bg-indigo-100 text-indigo-700">新属性</span>
                        ) : (
                          <span className="inline-block px-2 py-0.5 rounded text-xs bg-emerald-100 text-emerald-700">正常</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
                {preview.invalid_rows.map((row) => (
                  <tr key={`err-${row.row_num}`} className="border-t border-slate-50 bg-red-50/50">
                    <td className="px-4 py-2 text-slate-400">{row.row_num}</td>
                    <td colSpan={5} className="px-4 py-2 text-danger text-sm">{row.error}</td>
                    <td className="px-4 py-2 text-center">
                      <span className="inline-block px-2 py-0.5 rounded text-xs bg-red-100 text-red-700">错误</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="px-6 py-4 border-t border-slate-100 flex justify-end gap-3">
            <button onClick={() => { setPreview(null); setSelectedFile(null); setAttributeActions({}) }}
              className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
            <button onClick={() => confirmMutation.mutate()} disabled={confirmMutation.isPending}
              className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50">
              {confirmMutation.isPending ? '导入中...' : `确认导入 (${preview.valid_rows.length} 条)`}
            </button>
          </div>
        </div>
      )}

      {/* Import History */}
      {history && history.length > 0 && (
        <div className="bg-white rounded-xl shadow-card overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-100">
            <h3 className="font-semibold text-brand-950">导入历史</h3>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50">
                <th className="text-left px-4 py-2 text-slate-600 font-medium">文件名</th>
                <th className="text-right px-4 py-2 text-slate-600 font-medium">记录数</th>
                <th className="text-left px-4 py-2 text-slate-600 font-medium">导入时间</th>
                <th className="text-left px-4 py-2 text-slate-600 font-medium">状态</th>
              </tr>
            </thead>
            <tbody>
              {history.map((batch) => (
                <tr key={batch.id} className="border-t border-slate-50">
                  <td className="px-4 py-2">{batch.filename}</td>
                  <td className="px-4 py-2 text-right">{batch.record_count}</td>
                  <td className="px-4 py-2 text-slate-500">{batch.imported_at}</td>
                  <td className="px-4 py-2">
                    <span className={`inline-block px-2 py-0.5 rounded text-xs ${
                      batch.status === 'success' ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'
                    }`}>{batch.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
