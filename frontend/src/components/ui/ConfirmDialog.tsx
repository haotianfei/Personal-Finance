'use client'

import { useMemo, useRef } from 'react'
import { ResponsiveDialog } from './ResponsiveDialog'
import { ConfirmGateContext, runConfirmGate } from './confirmGate'
import type { ConfirmGate, ConfirmGateRegistry } from './confirmGate'

/** 破坏性操作二次确认：对象 + 后果 + 动词；默认焦点在取消。 */
export interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: React.ReactNode
  description?: React.ReactNode
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  loading?: boolean
  /** 禁止执行（如仍有子级/前置约束），按钮保留但禁用。 */
  confirmDisabled?: boolean
  /** 可选子内容（如「为什么改」的理由输入）：渲染在正文区、按钮上方。
   *  缺省保持原来的空正文；按钮区不随子内容变高而位移（正文自己滚动）。 */
  children?: React.ReactNode
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = false,
  loading = false,
  confirmDisabled = false,
  children,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  // 子内容（必填项）登记的门禁：缺项时这一下点击不产生任何请求。
  // registry 必须稳定（children 的登记 effect 依赖它），否则每次渲染都会重登记。
  const gateRef = useRef<ConfirmGate | null>(null)
  const gateRegistry = useMemo<ConfirmGateRegistry>(() => ({
    register: (gate) => {
      gateRef.current = gate
      return () => {
        if (gateRef.current === gate) gateRef.current = null
      }
    },
  }), [])

  const handleConfirm = () => {
    // 缺项时这一下点击不产生任何请求；红字与焦点由子内容自己给（与 records 更正框同一套判定）
    if (runConfirmGate(gateRef.current)) return
    onConfirm()
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="sm"
      initialFocusRef={cancelRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            disabled={loading}
            className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={loading || confirmDisabled}
            className={
              danger
                ? 'inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-danger px-4 text-body-strong font-medium text-white disabled:opacity-50'
                : 'inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50'
            }
          >
            {loading ? '处理中…' : confirmLabel}
          </button>
        </div>
      }
    >
      <ConfirmGateContext.Provider value={gateRegistry}>
        {children ?? <span />}
      </ConfirmGateContext.Provider>
    </ResponsiveDialog>
  )
}