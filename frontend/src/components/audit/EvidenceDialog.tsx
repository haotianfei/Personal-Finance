'use client'

import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { ErrorState, LoadingState, ResponsiveDialog } from '@/components/ui'
import { EvidenceDetail } from '@/components/evidence/EvidenceDetail'

/**
 * 凭证详情弹层（操作历史抽屉的**上层**）。
 *
 * 与「截图凭证」页的详情 Modal 同一份正文（`EvidenceDetail`），差别只有两点：
 * 1. 数据按 id 现取（审计行只带 `evidence_ref`，不带整条凭证）；
 * 2. 正文里的「受影响资产项」点击行为由宿主接管——在操作历史里点另一个资产项是
 *    「换这条时间线」，不是再开一个抽屉。
 *
 * 作为上层弹层，它自带焦点陷阱与 Esc；下层抽屉靠 `suspended` 让出键盘。
 * 关闭时下层抽屉的 DOM 与滚动位置原地不动（组件没有卸载）。
 */
export interface EvidenceDialogProps {
  evidenceId: number
  /** 审计行里的原始编号，仅用于标题（库内 INTEGER；异构形态可能是 `ev-…`）。 */
  evidenceRef?: number | string | null
  onClose: () => void
  onOpenItemHistory?: (item: { item_id: number; display_name: string }) => void
}

export function EvidenceDialog({
  evidenceId,
  evidenceRef,
  onClose,
  onOpenItemHistory,
}: EvidenceDialogProps) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['evidence', evidenceId],
    queryFn: () => api.getEvidence(evidenceId),
    // 凭证只增不改：短时间重复打开（同一个抽屉里来回看）不必再打后端
    staleTime: 60_000,
    retry: false,
  })

  // Esc 只关这一层：`ResponsiveDialog` 自己也在 document 捕获阶段监听 Esc，
  // 但它上面可能还叠着宿主的弹层（如凭证列表的详情 Modal）。挂到 window 捕获、
  // 提前收掉事件，才能保证「按一次 Esc 只关一层」。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      onCloseRef.current()
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [])

  const label = evidenceRef && !/^\d+$/.test(String(evidenceRef)) ? String(evidenceRef) : `#${evidenceId}`

  return (
    <ResponsiveDialog
      open
      onClose={onClose}
      title={`凭证 ${label}`}
      description={data ? data.original_filename || data.file_path : '正在读取凭证信息…'}
      size="lg"
    >
      {isLoading && <LoadingState rows={4} label="正在加载凭证" />}

      {!isLoading && error && (
        <ErrorState
          compact
          title="凭证加载失败"
          description={error instanceof Error ? error.message : '请稍后重试'}
          onRetry={() => { void refetch() }}
        />
      )}

      {data && <EvidenceDetail item={data} onOpenItemHistory={onOpenItemHistory} />}
    </ResponsiveDialog>
  )
}