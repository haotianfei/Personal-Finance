'use client'

import { useState, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, newIdemKey } from '@/lib/api'
import { formatDate, formatDateTime } from '@/lib/format'
import { formatFileSize } from '@/lib/format'
import type { ImportPreviewResponse, ImportBatch } from '@/types'
import { Upload, FileText, CheckCircle, AlertTriangle, X, Database, History, Loader2 } from 'lucide-react'
import { FormatGuide, downloadTemplate } from '@/components/import-export/FormatGuide'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { Card, InlineError, Money, ResponsiveDialog } from '@/components/ui'
import { tokens } from '@/styles/tokens'

/**
 * 导入预览原生表的派生表宽（下限语义，spec D3/A5）：8 个档位之和。
 * 列多的表在 1440 视口横滚属合法（P3 完整性优先）；不写死像素，只加总 tokens。
 */
const PREVIEW_TABLE_WIDTH = tokens.table.col.count
  + tokens.table.col.date
  + tokens.table.col['text-mid'] * 3
  + tokens.table.col.label
  + tokens.table.col.amount
  + tokens.table.col['text-short']

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
    return <div className="p-4 text-center text-muted">暂无备份文件</div>
  }
  
  return (
    <div className="rounded-card border border-line bg-surface overflow-hidden">
      <div className="px-4 py-3 bg-subtle border-b border-line">
        <h4 className="text-sm font-medium text-ink">备份文件列表</h4>
      </div>
      <p className="px-4 pt-3 text-meta text-muted tablet:hidden">左右滑动查看完整表格。</p>
      <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-sm">
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
              <td className="px-4 py-2 text-slate-500">{formatDateTime(backup.created_at)}</td>
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
  const [uploadError, setUploadError] = useState<string | null>(null)

  const { data: history } = useQuery({ queryKey: ['importHistory'], queryFn: api.getImportHistory })

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadCsv(file),
    onSuccess: (data) => {
      setUploadError(null)
      setPreview(data)
    },
    onError: (error: Error & { response?: { data?: { detail?: string } } }) => {
      const message = error?.response?.data?.detail || error?.message || '上传文件失败，请检查文件格式是否正确'
      setUploadError(message)
      setSelectedFile(null)
    },
  })

  const [backupInfo, setBackupInfo] = useState<{ filename: string; showBackups: boolean } | null>(null)
  const [confirmModalVisible, setConfirmModalVisible] = useState(false)

  // v5 幂等：确认导入的每用户动作一个 uuid；react-query 自动重试复用同键，成功即解锁
  const confirmIdemRef = useRef<string | null>(null)

  const confirmMutation = useMutation({
    mutationFn: () => {
      if (!confirmIdemRef.current) confirmIdemRef.current = newIdemKey()
      return api.confirmImport(selectedFile!, conflictRes, confirmIdemRef.current)
    },
    onSuccess: (data) => {
      confirmIdemRef.current = null
      setUploadError(null)
      setImportResult(data)
      // 显示备份信息
      if (data.backup_filename) {
        setBackupInfo({ filename: data.backup_filename, showBackups: false })
      }
      setPreview(null)
      setSelectedFile(null)
      setConfirmModalVisible(false)
      queryClient.invalidateQueries({ queryKey: ['importHistory'] })
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
      // 刷新字典与资产名（导入会给资产项补分类）
      queryClient.invalidateQueries({ queryKey: ['assetClasses'] })
      queryClient.invalidateQueries({ queryKey: ['institutions'] })
      queryClient.invalidateQueries({ queryKey: ['channels'] })
      queryClient.invalidateQueries({ queryKey: ['members'] })
      queryClient.invalidateQueries({ queryKey: ['tags'] })
      queryClient.invalidateQueries({ queryKey: ['assetsCurrent'] })
      queryClient.invalidateQueries({ queryKey: ['assetNames'] })
    },
    onError: (error: Error & { response?: { data?: { detail?: string } } }) => {
      const message = error?.response?.data?.detail || error?.message || '导入失败，请重试'
      setUploadError(message)
      setConfirmModalVisible(false)
    },
  })

  const handleFile = useCallback((file: File) => {
    if (!file.name.endsWith('.csv')) {
      setUploadError('请选择 CSV 格式的文件')
      return
    }
    setSelectedFile(file)
    setImportResult(null)
    setUploadError(null)
    uploadMutation.mutate(file)
  }, [uploadMutation])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) handleFile(file)
  }, [handleFile])

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="数据导入"
        description="从 CSV 文件导入资产记录；先预览校验，再写入并自动备份。分类只给资产项补空，不覆盖已有值。"
      />

      {uploadError && (
        <InlineError action={<button type="button" onClick={() => setUploadError(null)} className="text-body-strong">关闭</button>}>
          {uploadError}
        </InlineError>
      )}

      {/* Success Banner */}
      {importResult && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 p-4 bg-emerald-50 rounded-xl border border-emerald-200">
            <CheckCircle size={20} className="text-emerald-600 shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-medium text-emerald-800">
                导入成功: {importResult.filename} ({importResult.record_count} 条记录)
                {/* v5 写语义：冲突显式清单（不静默覆盖） */}
                {(importResult.skipped?.length ?? 0) > 0 &&
                  ` · 跳过冲突 ${importResult.skipped!.length} 条（同日同项已有有效记录）`}
              </p>
              {(importResult.skipped?.length ?? 0) > 0 && (
                <ul className="mt-1 text-xs text-amber-700 list-disc pl-5">
                  {importResult.skipped!.slice(0, 5).map((sk, i) => (
                    <li key={i}>{sk.asset_name} · {sk.asset_date}：保留既有记录（如需修正请走「更正」）</li>
                  ))}
                  {(importResult.skipped!.length > 5) && <li>…共 {importResult.skipped!.length} 条</li>}
                </ul>
              )}
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
        <Card
          padding="none"
          className={`border-2 border-dashed p-8 text-center transition-colors tablet:p-12 ${
            dragOver ? 'border-brand-400 bg-brand-50/50' : 'border-line-strong'
          }`}
        >
          <div
            onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
          >
          <Upload size={40} className="mx-auto text-slate-300 mb-4" />
          <p className="text-slate-600 font-medium mb-1">拖拽 CSV 文件到此处</p>
          <p className="text-sm text-muted mb-4">或点击选择文件</p>
          <label className="inline-block px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 cursor-pointer">
            选择文件
            <input type="file" accept=".csv" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f) }} />
          </label>
          {uploadMutation.isPending && (
            <p role="status" className="mt-4 inline-flex items-center gap-2 text-body text-brand-700">
              <Loader2 size={16} className="animate-spin" aria-hidden /> 正在解析 CSV…
            </p>
          )}
          </div>
        </Card>
      )}

      {/* Preview */}
      {preview && (
        <Card padding="none" className="overflow-hidden">
          <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-4 tablet:px-6">
            <div className="flex min-w-0 items-center gap-3">
              <FileText size={20} className="shrink-0 text-brand-600" />
              <div className="min-w-0">
                <p className="truncate font-medium text-ink">{preview.filename}</p>
                <p className="mt-0.5 text-meta text-secondary">
                  {preview.valid_rows.length} 条有效 · {preview.invalid_rows.length} 条无效 · {preview.conflict_count} 条冲突
                </p>
              </div>
            </div>
            <button type="button" aria-label="关闭预览" onClick={() => { setPreview(null); setSelectedFile(null) }}
              className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted"><X size={20} aria-hidden /></button>
          </div>

          {preview.conflict_count > 0 && (
            <div className="px-6 py-3 bg-amber-50 border-b border-amber-200">
              <div className="flex items-center gap-3">
                <AlertTriangle size={16} className="text-amber-600" />
                <span className="text-sm text-amber-800">发现 {preview.conflict_count} 条数据冲突</span>
                <select value={conflictRes} onChange={(e) => setConflictRes(e.target.value)}
                  className="ml-auto px-2 py-1 border border-amber-300 rounded text-sm bg-white">
                  <option value="skip">跳过冲突</option>
                </select>
              </div>
              <p className="text-xs text-amber-600 mt-2">
                {'跳过冲突：保留数据库中已有记录，不导入冲突行（v5：金额为 write-once，批量覆盖入口已冻结；需修正已有金额请用记录页「更正」）'}
              </p>
            </div>
          )}

          <p className="px-6 pt-3 text-meta text-muted tablet:hidden">左右滑动查看完整表格。</p>
          <div className="max-h-[400px] overflow-auto">
            {/* 原生表（spec D5/D6/D7）：预览表显示的就是待写入记录的字段，截断会让
                「导入前核对」失效。就地套 .pf-table + colgroup 读 tokens 档位。 */}
            <table className="pf-table" style={{ minWidth: PREVIEW_TABLE_WIDTH }}>
              <colgroup>
                <col style={{ width: tokens.table.col.count }} />
                <col style={{ width: tokens.table.col.date }} />
                <col style={{ width: tokens.table.col['text-mid'] }} />
                <col style={{ width: tokens.table.col['text-mid'] }} />
                <col style={{ width: tokens.table.col['text-mid'] }} />
                <col style={{ width: tokens.table.col.label }} />
                <col style={{ width: tokens.table.col.amount }} />
                <col style={{ width: tokens.table.col['text-short'] }} />
              </colgroup>
              {/* thead 的 sticky top-0 已有，补 z-sticky(20) 满足 A10（A10 要求 thead z = 20）。 */}
              <thead className="sticky top-0 z-sticky bg-subtle">
                <tr>
                  <th className="pf-col-first py-2 text-left text-slate-600 font-medium">行号</th>
                  <th className="py-2 text-left text-slate-600 font-medium">日期</th>
                  <th className="py-2 text-left text-slate-600 font-medium">资产名称</th>
                  <th className="py-2 text-left text-slate-600 font-medium">资产类别</th>
                  <th className="py-2 text-left text-slate-600 font-medium">机构/渠道</th>
                  <th className="py-2 text-left text-slate-600 font-medium">归属</th>
                  <th className="py-2 text-right text-slate-600 font-medium">金额</th>
                  <th className="pf-col-last py-2 text-center text-slate-600 font-medium">状态</th>
                </tr>
              </thead>
              <tbody>
                {/* 冻结列：首列（行号）+ 末列（状态）。末列是**行级判读**（冲突/正常/错误）
                    而非数值，符合右冻结判据。行底色一栏：冲突行用不透明
                    status.warn-surface（原 bg-amber-50/50 半透，冻结格 inherit 后会透字）。 */}
                {preview.valid_rows.map((row) => (
                  <tr key={row.row_num}
                    className={`border-t border-slate-50 ${row.has_conflict ? 'bg-status-warn-surface' : 'bg-surface'}`}>
                    <td className="pf-col-first py-2 text-muted">{row.row_num}</td>
                    <td className="py-2">{formatDate(row.asset_date)}</td>
                    <td className="whitespace-normal py-2 font-medium">{row.asset_name}</td>
                    <td className="whitespace-normal py-2 text-slate-500">{row.asset_class || '—'}</td>
                    <td className="whitespace-normal py-2 text-slate-500">
                      {[row.institution, row.channel].filter(Boolean).join(' / ') || '—'}
                    </td>
                    <td className="py-2 text-slate-500">{row.owner || '—'}</td>
                    <td className="py-2 text-right">
                      <Money value={row.amount} unit="cny" size="xs" />
                    </td>
                    <td className="pf-col-last py-2 text-center">
                      {row.has_conflict ? (
                        <span className="inline-block whitespace-nowrap px-2 py-0.5 rounded text-xs bg-amber-100 text-amber-700">冲突</span>
                      ) : (
                        <span className="inline-block whitespace-nowrap px-2 py-0.5 rounded text-xs bg-emerald-100 text-emerald-700">正常</span>
                      )}
                    </td>
                  </tr>
                ))}
                {preview.invalid_rows.map((row) => (
                  // 错误行：colSpan={6} 的那格横跨中间 6 列，首/末两格照常冻结（不冲突）。
                  <tr key={`err-${row.row_num}`} className="border-t border-slate-50 bg-status-critical-surface">
                    <td className="pf-col-first py-2 text-muted">{row.row_num}</td>
                    <td colSpan={6} className="whitespace-normal py-2 text-danger text-sm">{row.error}</td>
                    <td className="pf-col-last py-2 text-center">
                      <span className="inline-block whitespace-nowrap px-2 py-0.5 rounded text-xs bg-red-100 text-red-700">错误</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="px-6 py-4 border-t border-slate-100 flex justify-end gap-3">
            <button onClick={() => { setPreview(null); setSelectedFile(null) }}
              className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
            <button onClick={() => setConfirmModalVisible(true)} disabled={confirmMutation.isPending}
              className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50">
              {confirmMutation.isPending ? '导入中...' : `确认导入 (${preview.valid_rows.length} 条)`}
            </button>
          </div>
        </Card>
      )}

      <ResponsiveDialog
        open={confirmModalVisible}
        onClose={() => setConfirmModalVisible(false)}
        title="确认导入"
        description="写入前将自动备份当前数据库。"
        size="sm"
        footer={
          <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
            <button type="button" onClick={() => setConfirmModalVisible(false)} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink">取消</button>
            <button
              type="button"
              onClick={() => {
                if (!selectedFile) {
                  setUploadError('文件已丢失，请重新上传')
                  setConfirmModalVisible(false)
                  return
                }
                setConfirmModalVisible(false)
                confirmMutation.mutate()
              }}
              disabled={confirmMutation.isPending || !selectedFile}
              className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
            >
              {confirmMutation.isPending ? '导入中…' : '确认导入'}
            </button>
          </div>
        }
      >
        {preview && (
          <div className="space-y-3">
            <div className="flex justify-between gap-4 text-body"><span className="text-secondary">文件名</span><span className="text-right font-medium text-ink break-all">{preview.filename}</span></div>
            <div className="flex justify-between text-body"><span className="text-secondary">有效记录数</span><span className="pf-num text-ink">{preview.valid_rows.length} 条</span></div>
            <div className="flex justify-between text-body"><span className="text-secondary">冲突数</span><span className={preview.conflict_count > 0 ? 'pf-num text-status-warn-text' : 'pf-num text-ink'}>{preview.conflict_count} 条</span></div>
            <div className="flex justify-between text-body"><span className="text-secondary">冲突处理策略</span><span className="text-ink">跳过冲突</span></div>
          </div>
        )}
      </ResponsiveDialog>

      {/* Import Format Guide */}
      <FormatGuide showDownloadButton={true} onDownload={downloadTemplate} />

      {/* Import History */}
      {history && history.length > 0 && (
        <div className="rounded-card border border-line bg-surface overflow-hidden">
          <div className="px-6 py-4 border-b border-line">
            <h3 className="font-semibold text-ink">导入历史</h3>
          </div>
          <p className="px-6 pt-3 text-meta text-muted tablet:hidden">左右滑动查看完整表格。</p>
          <div className="overflow-x-auto">
          <table className="w-full min-w-[620px] text-sm">
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
        </div>
      )}
    </PageContainer>
  )
}
