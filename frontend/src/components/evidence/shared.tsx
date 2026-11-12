import type { ReactNode } from 'react'
import type { EvidenceListItem } from '@/types'

/**
 * 凭证详情里「标签 + 值」一行（原图体检表与识别快照共用）。
 * 值走 `<dd>` 的 `break-words`：文件名、设备名这类长串不能撑破两列栅格。
 */
export function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-meta text-muted">{label}</dt>
      <dd className="min-w-0 break-words text-body text-ink">{children}</dd>
    </div>
  )
}

/** 识别引擎：历史行 engine 为 NULL，后端语义即 ocr（同截图页 `engineOf`）。 */
export function engineLabel(engine: EvidenceListItem['engine']): string {
  return engine === 'ai' ? 'AI 识别' : 'OCR'
}