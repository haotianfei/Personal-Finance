'use client'

import { Info } from 'lucide-react'
import Decimal from 'decimal.js'
import { ApiError } from '@/lib/api'

/**
 * 资产项身份 / 血缘动作的公共件：后果文案、数值解析、错误转人话。
 * 契约单源 `.scratch/lineage-split/spec.md` §1-§2。
 */

/** 合并 / 拆分提交前的后果说明（票 02 固定文案，勿改写）。 */
export const LINEAGE_CONSEQUENCE = '登记后旧项自生效日起终止计量；该日之前的历史仍计入旧项'

export function ConsequenceNotice() {
  return (
    <div className="flex gap-2 rounded-control border border-status-warn/25 bg-status-warn-surface p-3 text-meta text-status-warn-text">
      <Info size={15} className="mt-0.5 shrink-0" aria-hidden />
      <p>{LINEAGE_CONSEQUENCE}</p>
    </div>
  )
}

export const LINK_TYPE_LABEL: Record<string, string> = {
  merge: '合并',
  split: '拆分',
  revision: '版本',
}

/** 补登记（attach）关系的人话：两侧都是既有项时，revision 就是「改名 / 更正」。 */
export const ATTACH_LINK_TYPE_LABEL: Record<string, string> = {
  revision: '改名 / 更正',
  merge: '合并',
  split: '拆分',
}

/** 输入阶段放行的金额半成品（与当前值就地编辑同口径）。 */
export const AMOUNT_INPUT_PATTERN = /^\d*\.?\d{0,2}$/
/** 可提交金额：非负、最多两位小数。 */
export const AMOUNT_VALUE_PATTERN = /^\d+(\.\d{1,2})?$/
/** 比例输入（百分数）：0–100，最多两位小数。 */
export const PERCENT_INPUT_PATTERN = /^\d{0,3}(\.\d{0,2})?$/
/** 后端比例容差：Σratio = 1 ± 1e-4。 */
export const RATIO_TOLERANCE = new Decimal('0.0001')

/** 去掉用户常带的千分位 / ¥ / 空格。 */
export function cleanNumericInput(raw: string): string {
  return raw.replace(/[,，\s¥￥]/g, '')
}

/** 表单金额 → 两位小数字符串；非法返回 null（交给行内提示，不猜）。 */
export function toAmountString(raw: string): string | null {
  const clean = cleanNumericInput(raw).replace(/%$/, '')
  if (!AMOUNT_VALUE_PATTERN.test(clean)) return null
  return new Decimal(clean).toFixed(2)
}

/** 百分数（0–100）→ 0–1 比率字符串（四位小数，如 "33.33" → "0.3333"）。 */
export function toRatioString(percentRaw: string): string | null {
  const clean = cleanNumericInput(percentRaw).replace(/%$/, '')
  if (!PERCENT_INPUT_PATTERN.test(clean) || clean === '' || clean === '.') return null
  const percent = new Decimal(clean)
  if (percent.lte(0) || percent.gt(100)) return null
  return percent.div(100).toFixed(4)
}

/** 百分比文本 → 数值（非法按 0，仅用于合计展示）。 */
export function toPercentDecimal(percentRaw: string): Decimal {
  const clean = cleanNumericInput(percentRaw).replace(/%$/, '')
  if (clean === '' || !PERCENT_INPUT_PATTERN.test(clean)) return new Decimal(0)
  return new Decimal(clean)
}

/** 金额文本 → 数值（null / 空按 0，仅用于预填与合计）。 */
export function toAmountDecimal(amount: string | null | undefined): Decimal {
  if (amount === null || amount === undefined || amount === '') return new Decimal(0)
  try {
    return new Decimal(amount)
  } catch {
    return new Decimal(0)
  }
}

/** 金额展示（预填 / 合计用；正式展示走 Money 组件）。 */
export function formatAmountInput(value: Decimal | string): string {
  return new Decimal(value).toFixed(2)
}

/** 资产项名称占用判断（显示名 / 历史名 / legacy 名，精确匹配，与后端同口径）。 */
export function buildTakenNames(items: { display_name: string; aliases: string[]; legacy_name?: string | null }[]): Set<string> {
  const taken = new Set<string>()
  for (const item of items) {
    if (item.display_name) taken.add(item.display_name)
    if (item.legacy_name) taken.add(item.legacy_name)
    for (const alias of item.aliases) taken.add(alias)
  }
  return taken
}

/**
 * 后端错误 → 人话。拆分端点 404（FastAPI「Not Found」）单独给一句可执行的说明：
 * 并行车道尚未落地时，用户看到的是「暂时不能拆分」，而不是莫名报错。
 */
export function lineageErrorText(
  error: unknown,
  fallback: string,
  notFoundText?: string,
): string {
  if (error instanceof ApiError) {
    if (error.code === 'ITEM_ARCHIVED') {
      return '所选项里有已归档的资产项，不能再参与合并、拆分或关联；请刷新后重试。'
    }
    if (error.code === 'PRINCIPAL_REASON_REQUIRED') {
      return '请填写登记理由：承接金额是人确认的裁决，必须留下理由。'
    }
    if (error.code === 'LINEAGE_CYCLE') {
      return '该关联会形成闭环（甲 → 乙 → 甲）；请先核对已有前后关系，再重新选择。'
    }
    if (error.code === 'LINEAGE_EXISTS') {
      return '这对资产项之间的关联已登记过，无需重复登记。'
    }
    if (error.code === 'NOT_FOUND' && notFoundText) return notFoundText
    return error.message || fallback
  }
  if (error instanceof Error && error.message) {
    if (error.message === 'Not Found' && notFoundText) return notFoundText
    return error.message
  }
  return fallback
}

/**
 * 后端逐行校验（拆分 400）→ 子项序号 → 错误文本。
 * 并行车道未冻结 details 形状，这里兼容 fields/errors/targets 三种清单与
 * `targets[0].ratio` 形式的 field 名；匹配不到的行交给顶部 InlineError。
 */
export function extractTargetErrors(error: unknown): Map<number, string[]> {
  const out = new Map<number, string[]>()
  if (!(error instanceof ApiError) || !error.details) return out
  const details = error.details

  const push = (index: unknown, message: unknown) => {
    const parsed = typeof index === 'number' ? index : Number(index)
    if (!Number.isInteger(parsed) || parsed < 0 || typeof message !== 'string' || !message) return
    out.set(parsed, [...(out.get(parsed) ?? []), message])
  }

  const lists = [details.fields, details.errors, details.targets]
  for (const list of lists) {
    if (!Array.isArray(list)) continue
    list.forEach((entry, fallbackIndex) => {
      if (entry === null || typeof entry !== 'object') return
      const record = entry as Record<string, unknown>
      const message = record.message ?? record.msg ?? record.detail
      let index: unknown = record.index ?? record.target_index ?? record.targetIndex
      const field = record.field ?? record.path
      if (index === undefined && typeof field === 'string') {
        const match = field.match(/targets?[.[](\d+)/i)
        if (match) index = Number(match[1])
      }
      push(index ?? fallbackIndex, message)
    })
  }

  // 兜底：message 里点名了 targets[n]
  const pattern = /targets?\[(\d+)\][^；;。]*?[:：]?\s*([^；;。]+)/gi
  let match: RegExpExecArray | null
  while ((match = pattern.exec(error.message)) !== null) {
    push(Number(match[1]), match[2].trim())
  }
  return out
}