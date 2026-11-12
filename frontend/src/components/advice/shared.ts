'use client'

import type { AdvisorStatus } from '@/types'

export const ADVICE_STATUS_LABEL: Record<AdvisorStatus, string> = {
  generating: '生成中',
  awaiting_review: '待审阅',
  gate_failed: '门失败',
  generation_failed: '生成失败',
  accepted: '已采纳',
  rejected: '已驳回',
  expired: '已过期',
}

export const ADVICE_STATUS_CLASS: Record<AdvisorStatus, string> = {
  generating: 'bg-brand-50 text-brand-700',
  awaiting_review: 'bg-status-warn-surface text-status-warn-text',
  gate_failed: 'bg-status-critical-surface text-status-critical-text',
  generation_failed: 'bg-status-critical-surface text-status-critical-text',
  accepted: 'bg-status-good-surface text-status-good-text',
  rejected: 'bg-subtle text-muted',
  expired: 'bg-subtle text-muted',
}

export const ADVICE_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'awaiting_review', label: '待审阅' },
  { value: 'accepted', label: '已采纳' },
  { value: 'rejected', label: '已驳回' },
  { value: 'expired', label: '已过期' },
  { value: 'gate_failed', label: '门失败' },
  { value: 'generation_failed', label: '生成失败' },
]

export const HORIZON_LABEL: Record<string, string> = {
  short: '短期',
  mid: '中期',
  long: '长期',
}

export function horizonLabel(value: string | null | undefined): string {
  return HORIZON_LABEL[value ?? ''] ?? value ?? '—'
}

export function adviceError(error: unknown): string {
  const err = error as { code?: string; message?: string; details?: { current_status?: string } } | null
  if (!err) return '操作失败，请重试。'
  switch (err.code) {
    case 'ADVISOR_PROVIDER_UNAVAILABLE':
      return '当前没有默认且启用的 LLM 供应商，暂时无法生成建议。请先在「设置 → LLM 供应商」配置并启用默认供应商。'
    case 'ADVISOR_DUPLICATE_TRIGGER':
      return '相同触发条件已有一单正在处理，请稍后刷新收件箱。'
    case 'ADVISOR_SUGGESTION_CLOSED':
      return `该建议已进入终态${err.details?.current_status ? `（${ADVICE_STATUS_LABEL[err.details.current_status as AdvisorStatus] ?? err.details.current_status}）` : ''}，不能再次裁决。`
    case 'ADVISOR_NOT_ACCEPTED':
      return '只有已采纳的建议可以填写执行备注。'
    case 'ADVISOR_INFO_SET_CORRUPT':
      return '信息集校验失败（疑似篡改或坏库），请联系维护者检查。'
    case 'NOT_FOUND':
      return '建议不存在或已被删除。'
    case 'VALIDATION_ERROR':
      return err.message || '输入有误，请检查后重试。'
    default:
      return err.message || '操作失败，请重试。'
  }
}
export const GATE_LABEL: Record<string, string> = {
  GATE_SCHEMA: '结构校验',
  GATE_INFO_SET_SEALED: '信息集封印',
  GATE_REF_EXISTS: '引用存在',
  GATE_NUMERIC_PROVENANCE: '数字来源',
  GATE_PLACEHOLDER_RESOLVES: '占位符解析',
  GATE_GOAL_ANCHOR: '目标锚点',
  GATE_FRESHNESS: '信息时效',
  GATE_NO_EXECUTION: '无执行意图',
  GATE_THRESHOLD_SOURCE: '阈值来源',
}

export function gateCheckText(check: { code: string; detail?: unknown }): string {
  const label = GATE_LABEL[check.code] ?? check.code
  const detail = typeof check.detail === 'string' ? check.detail : ''
  return detail ? `${label}：${detail}` : label
}
