'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { SelectField } from '@/components/form/SelectField'
import type { SelectFieldOption } from '@/components/form/SelectField'
import { FormField, fieldControlClass } from '@/components/form/FormField'
import { useConfirmGate, isReasonMissing } from '@/components/ui/confirmGate'
import {
  AUDIT_REASON_NOTE_MAX,
  type AuditReasonCode,
  type AuditReasonInput,
} from '@/lib/api'

/**
 * 「为什么改」输入（审计留痕，ADR-0027 D7）：枚举下拉 + 可选说明，**必选**。
 *
 * 五个入口共用同一个控件（值更正 / 删除单条 / 批量删除 / 归档 / 清理凭证）：
 * 复制几份的下场是几套文案和几套上限，改一次要改几个地方。
 *
 * 必选不是文案偏好：`actor='owner'` 的 correct / delete / archive 缺 reason，
 * 后端一律 422 `AUDIT_REASON_REQUIRED`。所以这里**不提供「不填」这项**——
 * 让它出现在下拉里，只会把一次注定失败的提交包装成一次选择。
 * 说明只在选了原因后才出现：没归类的一段文字进不了审计行。
 */
export type AuditReasonValue = {
  reason: AuditReasonCode | ''
  note: string
}

/** 未选状态（新开一个对话框时从这里开始）。 */
export const EMPTY_AUDIT_REASON: AuditReasonValue = { reason: '', note: '' }

/** 文案用中文短句：说得出口的理由，用户才会选。 */
const AUDIT_REASON_OPTIONS: SelectFieldOption[] = [
  { value: 'correction', label: '旧值写错了（录入错误）' },
  { value: 'evolution', label: '情况变了（金额随时间变化）' },
  { value: 'normalization', label: '口径调整（记法 / 归类统一）' },
  { value: 'import', label: '导入带进来的（文件 / 截图）' },
  { value: 'migration', label: '旧系统迁过来的' },
  { value: 'other', label: '其它（在说明里写清）' },
]

/** 未选就点确认时的一句话（与后端 422 的提示同义，不必等往返再说）。 */
const MISSING_REASON_TEXT = '还没选原因——这类操作必须说明原因才能继续。'

/** 控件的值 → 请求参数（没选时仍按「不带 reason」发，URL 与改动前逐字节相同）。 */
export function toAuditReasonInput(value?: AuditReasonValue): AuditReasonInput {
  return {
    reason: value?.reason ? value.reason : undefined,
    reason_note: value?.reason ? value.note : undefined,
  }
}

/**
 * 按对象保留理由草稿：**关窗不清、换对象才清**。
 *
 * Esc / 取消会关掉确认框（与既有对话框一致），但已经写下的半句理由不该跟着丢；
 * 换一条记录再打开时才是全新的操作，必须从「未选」开始。
 */
export function useAuditReasonDraft(
  key: string | number | null,
): [AuditReasonValue, (value: AuditReasonValue) => void] {
  const [value, setValue] = useState<AuditReasonValue>(EMPTY_AUDIT_REASON)
  const lastKey = useRef<string | number | null>(null)

  useEffect(() => {
    if (key === null || lastKey.current === key) return
    lastKey.current = key
    setValue(EMPTY_AUDIT_REASON)
  }, [key])

  return [value, setValue]
}

export interface AuditReasonFieldProps {
  value: AuditReasonValue
  onChange: (value: AuditReasonValue) => void
  /** 选择框标题，如「为什么更正」（必填标记由控件统一补）。 */
  label: string
  /** 额外一行说明（这次操作留下什么）；「必须说明原因」那句固定文案不用在这里重复。 */
  hint?: React.ReactNode
  disabled?: boolean
  className?: string
  /**
   * 外部提交入口拦下这次提交时把它 +1：控件显红字并回焦。
   *
   * 给**自己画保存按钮**的弹窗用（records 更正框走 `ResponsiveDialog`，没有确认按钮可门禁）：
   * 判定仍是 `confirmGate.isReasonMissing`，反馈仍是这里这一份，只是触发点不同。
   */
  nudgeToken?: number
}

export function AuditReasonField({
  value,
  onChange,
  label,
  hint,
  disabled = false,
  className,
  nudgeToken = 0,
}: AuditReasonFieldProps) {
  const note = value.note
  const nearLimit = note.length > AUDIT_REASON_NOTE_MAX - 20
  const selectRef = useRef<HTMLSelectElement>(null)
  /** 用户已经点过保存/确认但还没选原因——从这一刻起把「缺什么」摆在字段上，而不是只写在别处。 */
  const [nudged, setNudged] = useState(false)

  const missing = isReasonMissing(value.reason)
  /** 被拦下时唯一的反馈动作：说出缺什么 + 焦点回到下拉。三条触发路径共用它。 */
  const nudge = useCallback(() => {
    setNudged(true)
    selectRef.current?.focus()
  }, [])

  // 在 ConfirmDialog 里：缺原因时点确认不发出任何请求。
  useConfirmGate(missing, nudge)

  // 不在 ConfirmDialog 里（自画保存按钮）：由外层 `runConfirmGate` 拦下后把这个数 +1。
  const lastNudgeToken = useRef(nudgeToken)
  useEffect(() => {
    if (nudgeToken === lastNudgeToken.current) return
    lastNudgeToken.current = nudgeToken
    nudge()
  }, [nudgeToken, nudge])

  return (
    <div
      className={cn(
        'space-y-2 rounded-control border border-line bg-subtle p-3',
        className,
      )}
    >
      <SelectField
        label={label}
        required
        value={value.reason}
        onChange={(reason) => {
          setNudged(false)
          // 换原因时保留已写的说明：用户可能只是改主意，文字不该被抹掉
          onChange({ ...value, reason: reason as AuditReasonCode | '' })
        }}
        placeholder="请选择原因"
        options={AUDIT_REASON_OPTIONS}
        error={missing && nudged ? MISSING_REASON_TEXT : undefined}
        disabled={disabled}
        selectRef={selectRef}
      />

      {value.reason !== '' && (
        <FormField label="补充说明（可以不填）" className="animate-fade-in">
          <textarea
            value={note}
            onChange={(e) => onChange({ ...value, note: e.target.value })}
            maxLength={AUDIT_REASON_NOTE_MAX}
            rows={2}
            disabled={disabled}
            placeholder="如：把 120000 误录成 1200000，多打了一个 0"
            className={cn(fieldControlClass, 'min-h-[76px] resize-none py-2')}
          />
        </FormField>
      )}

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="text-micro text-secondary">
            必填：这类操作必须说明原因，理由会写进操作记录。
          </p>
          {hint && <p className="text-micro text-muted">{hint}</p>}
        </div>
        {value.reason !== '' && (
          <span
            className={cn(
              'pf-num shrink-0 text-micro',
              nearLimit ? 'text-status-warn-text' : 'text-muted',
            )}
          >
            {note.length}/{AUDIT_REASON_NOTE_MAX}
          </span>
        )}
      </div>
    </div>
  )
}