'use client'

import type { ReviewAttributionCategory, ReviewProposalKind, ReviewStatus } from '@/types'

export const REVIEW_STATUS_LABEL: Record<ReviewStatus, string> = {
  open: '进行中',
  closed: '已关闭',
}
export const REVIEW_STATUS_CLASS: Record<ReviewStatus, string> = {
  open: 'bg-status-warn-surface text-status-warn-text',
  closed: 'bg-subtle text-muted',
}
export const REVIEW_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'open', label: '进行中' },
  { value: 'closed', label: '已关闭' },
]

export const ATTRIBUTION_LABEL: Record<ReviewAttributionCategory, string> = {
  market_moved: '市场变化',
  advice_wrong: '建议不当',
  not_executed: '未执行',
  execution_drift: '执行偏差',
}
export const ATTRIBUTION_OPTIONS = (Object.entries(ATTRIBUTION_LABEL) as Array<[ReviewAttributionCategory, string]>)
  .map(([value, label]) => ({ value, label }))

export const PROPOSAL_KIND_LABEL: Record<ReviewProposalKind, string> = {
  goal_version: '目标版本',
  threshold: '阈值',
  allocation_target: '配置目标',
  narrative: '文字结论',
}
export const PROPOSAL_STATUS_LABEL: Record<string, string> = {
  proposed: '待裁决', adopted: '已采纳', dismissed: '已驳回',
}
export const PROPOSAL_STATUS_CLASS: Record<string, string> = {
  proposed: 'bg-status-warn-surface text-status-warn-text',
  adopted: 'bg-status-good-surface text-status-good-text',
  dismissed: 'bg-subtle text-muted',
}

export function reviewError(error: unknown): string {
  const err = error as { code?: string; message?: string; details?: Record<string, unknown> } | null
  if (!err) return '操作失败，请重试。'
  switch (err.code) {
    case 'REVIEW_CLOSED':
      return '复盘已关闭，内容只读；如需继续请新建复盘。'
    case 'REVIEW_SNAPSHOT_CORRUPT':
      return '复盘快照校验失败（疑似篡改或坏库），请联系维护者。'
    case 'REVIEW_PROPOSAL_DECIDED':
      return '该提案已经裁决，不能再次操作。'
    case 'REVIEW_PROPOSAL_DUPLICATE':
      return '同内容提案已有一条待裁决，请先处理既有提案。'
    case 'VALIDATION_ERROR':
      return err.message || '输入有误，请检查后重试。'
    case 'NOT_FOUND':
      return '复盘不存在或已被删除。'
    default:
      return err.message || '操作失败，请重试。'
  }
}
