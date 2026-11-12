'use client'

import { FileText, RotateCcw, X } from 'lucide-react'
import { formatDateTime } from '@/lib/format'

interface DraftRestorePromptProps {
  visible: boolean
  lastSavedAt: Date | null
  onRestore: () => void
  onDiscard: () => void
}

export function DraftRestorePrompt({
  visible,
  lastSavedAt,
  onRestore,
  onDiscard
}: DraftRestorePromptProps) {
  if (!visible) return null

  return (
    <div className="mb-4 p-4 bg-amber-50 rounded-xl border border-amber-200">
      <div className="flex items-start gap-3">
        <FileText size={20} className="text-amber-600 shrink-0 mt-0.5" />
        <div className="flex-1">
          <p className="font-medium text-amber-800">发现未提交的草稿</p>
          <p className="text-sm text-amber-600 mt-1">
            上次编辑时间：{lastSavedAt ? formatDateTime(lastSavedAt) : '未知时间'}
          </p>
          <div className="flex gap-2 mt-3">
            <button
              onClick={onRestore}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-white bg-amber-600 rounded-lg hover:bg-amber-700 transition-colors"
            >
              <RotateCcw size={14} />
              恢复草稿
            </button>
            <button
              onClick={onDiscard}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-amber-700 bg-amber-100 rounded-lg hover:bg-amber-200 transition-colors"
            >
              <X size={14} />
              丢弃
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
