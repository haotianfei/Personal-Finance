'use client'

import { ResponsiveDialog } from './ResponsiveDialog'

/** 筛选抽屉：桌面 dialog / 移动全屏 sheet；底部固定重置/应用。 */
export interface FilterSheetProps {
  open: boolean
  onClose: () => void
  title?: React.ReactNode
  children: React.ReactNode
  onReset?: () => void
  onApply: () => void
  applyLabel?: string
}

export function FilterSheet({
  open,
  onClose,
  title = '筛选',
  children,
  onReset,
  onApply,
  applyLabel = '应用筛选',
}: FilterSheetProps) {
  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <div className="flex gap-2">
          {onReset && (
            <button
              type="button"
              onClick={onReset}
              className="inline-flex min-h-[44px] flex-1 items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink tablet:flex-none"
            >
              重置
            </button>
          )}
          <button
            type="button"
            onClick={onApply}
            className="inline-flex min-h-[44px] flex-[2] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white tablet:flex-none tablet:px-6"
          >
            {applyLabel}
          </button>
        </div>
      }
    >
      <div className="space-y-4">{children}</div>
    </ResponsiveDialog>
  )
}