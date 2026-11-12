import Decimal from 'decimal.js'
import type { AssetItemInfo, LineageAttachLinkType } from '@/types'

/**
 * 身份族（lineage family）的纯函数：
 * - 名称 → 资产项 id（分析页按 `asset_name` 选中的旧名反查身份）；
 * - 关联形状推导（1:1 / N:1 / 1:N；N:M 拒绝）；
 * - 拆分比例按「生效日各项目金额」预填，末位吸收舍入尾差，合计恰为 100%。
 * 契约单源 `.scratch/lineage-attach/spec.md` §1.1 / §1.3。
 */

/** 解析顺序与后端 `asset_service.resolve_item_id` 同序：现名 → legacy 锚名 → 显示名史；同分取 active 优先、id 最小。 */
export function resolveItemIdByName(items: readonly AssetItemInfo[], name: string): number | null {
  const target = name.trim()
  if (!target) return null

  const firstId = (matches: AssetItemInfo[]): number | null => {
    if (matches.length === 0) return null
    const sorted = [...matches].sort((left, right) => {
      const activeDelta = Number(right.status === 'active') - Number(left.status === 'active')
      return activeDelta !== 0 ? activeDelta : left.id - right.id
    })
    return sorted[0].id
  }

  return (
    firstId(items.filter((item) => item.display_name === target))
    ?? firstId(items.filter((item) => item.legacy_name === target))
    ?? firstId(items.filter((item) => item.aliases.includes(target)))
  )
}

/**
 * 关联形状由两侧数量唯一决定：
 * 1:1 改名 / 更正、N:1 合并、1:N 拆分；N:M 语义歧义，返回 null（界面必须拦住）。
 */
export function deriveAttachLinkType(
  fromCount: number,
  toCount: number,
): LineageAttachLinkType | null {
  if (fromCount === 1 && toCount === 1) return 'revision'
  if (fromCount > 1 && toCount === 1) return 'merge'
  if (fromCount === 1 && toCount > 1) return 'split'
  return null
}

const HUNDRED = new Decimal(100)

function equalPercents(count: number): string[] {
  const base = HUNDRED.div(count).toDecimalPlaces(2, Decimal.ROUND_DOWN)
  const head = Array.from({ length: count - 1 }, () => base.toFixed(2))
  const last = HUNDRED.minus(base.times(count - 1)).toFixed(2)
  return [...head, last]
}

/**
 * 拆分比例预填：percent_i ＝ amount_i / Σamount × 100（两位小数）。
 * - 末位吸收舍入尾差 → 合计恒为 100.00%（后端容差 ±0.01%）；
 * - 任一金额为 0 / 缺失（后端比例须在 (0,1]）或份额小到 0.00 时退化为等分，
 *   避免预填出一个提交不了的比例。
 */
export function prefillSplitPercents(
  amounts: readonly (string | null | undefined)[],
): string[] {
  const count = amounts.length
  if (count === 0) return []
  if (count === 1) return ['100.00']

  const values = amounts.map((amount) => {
    try {
      return Decimal.max(0, new Decimal(amount ?? 0))
    } catch {
      return new Decimal(0)
    }
  })
  const total = values.reduce((sum, value) => sum.plus(value), new Decimal(0))
  if (!total.gt(0) || values.some((value) => !value.gt(0))) return equalPercents(count)

  const head = values
    .slice(0, -1)
    .map((value) => value.div(total).times(HUNDRED).toDecimalPlaces(2, Decimal.ROUND_HALF_UP))
  const headSum = head.reduce((sum, value) => sum.plus(value), new Decimal(0))
  const last = HUNDRED.minus(headSum)
  // 尾差/极小份额把某行压成 0.00 或末行 ≤ 0 时，不让用户拿到一个不可提交的预填
  if (last.lte(0.01) || head.some((value) => value.lte(0))) return equalPercents(count)
  return [...head.map((value) => value.toFixed(2)), last.toFixed(2)]
}