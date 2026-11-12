/**
 * 截图复核页的「差额基线」纯函数（ADR-0026 补遗）。
 *
 * 为什么单独成模块：复核页 `app/screenshot/page.tsx` 已 5500+ 行，而基线这件事
 * 有两处必须**用同一套口径**——行内差额与顶部「系统合计」。分开写必然漂移
 * （真缺陷：绑定到已有项之后，行内仍按「新增」算全额，顶部差异被整额抬高）。
 *
 * 口径（一句话）：**一行的基线＝它最终会落到哪个资产项的当前值**。
 * - 未绑定（update/zero）：基线＝该行命中项的当前值（后端 `current_amount`）；
 * - 已绑定（bind）：基线＝**目标项**的当前值（后端 `existing_items` 的 item 级现值）；
 * - 新增（create，未绑定）：基线＝0。
 *
 * 顶部「系统合计」不是简单求和：同一资产项在本批里可能出现多行（多张截图命中
 * 同一项，或一行 update + 一行 bind 指向同一项），必须按**身份键**去重后累加，
 * 否则系统侧被重复计数，差异会凭空出现。
 */
import Decimal from 'decimal.js'

/** 参与基线计算所需的行最小面（只要这几个字段，便于单测与复用）。 */
export interface BaselineRow {
  asset_name?: string | null
  item_id?: number | null
  current_amount?: string | number | null
}

/** 后端 preview 随附的 item 级现值（`existing_items`）。 */
export interface ExistingItemAmountLike {
  item_id: number | null
  name: string
  amount: string
  as_of?: string | null
}

function dec(v: string | number | null | undefined): Decimal {
  if (v === null || v === undefined || v === '') return new Decimal(0)
  try {
    return new Decimal(v)
  } catch {
    return new Decimal(0)
  }
}

/**
 * 跨图合并 `item_id → 现值`。
 *
 * 多张截图各自带一份 `existing_items`，同一项在每张里的金额可能不同（不同批次
 * 的时点不同）。取**首个非空**是刻意选择：同一批识别共享一次「读取时刻」的语义，
 * 先到的即当时看到的现值；若后来的图覆盖，用户在页面上会看到差异自己跳动。
 * `item_id === null` 的历史遗留行不参与（无法安全地按身份归并）。
 */
export function buildItemAmounts(
  previews: ReadonlyArray<{ existing_items?: ExistingItemAmountLike[] | null }>,
): Map<number, Decimal> {
  const out = new Map<number, Decimal>()
  for (const p of previews) {
    for (const e of p?.existing_items ?? []) {
      if (e === null || e === undefined) continue
      if (e.item_id === null || e.item_id === undefined) continue
      if (out.has(e.item_id)) continue
      out.set(e.item_id, dec(e.amount))
    }
  }
  return out
}

/** 一行的差额基线（见文件头口径）。 */
export function rowBaseline(
  row: BaselineRow | null | undefined,
  bindTargetId: number | null | undefined,
  itemAmounts: ReadonlyMap<number, Decimal>,
): Decimal {
  if (bindTargetId !== null && bindTargetId !== undefined) {
    return itemAmounts.get(bindTargetId) ?? new Decimal(0)
  }
  return dec(row?.current_amount)
}

/** 行内差额（截图金额 − 基线），保留两位小数字符串。 */
export function rowDiffAmount(
  amount: string | number | null | undefined,
  baseline: Decimal,
): string {
  return dec(amount).minus(baseline).toFixed(2)
}

/**
 * 身份键：同一个键＝同一资产项，系统合计里只能计一次。
 *
 * - 已绑定 → `item:<目标 id>`（绑定是最强身份声明，压过行自身命中的项）；
 * - 命中已有项 → `item:<item_id>`；
 * - 兜底 → `name:<资产全名>`（新增行，或后端未给 item_id 的旧版本）。
 */
export function identityKeyOf(
  row: BaselineRow | null | undefined,
  bindTargetId: number | null | undefined,
): string {
  if (bindTargetId !== null && bindTargetId !== undefined) return `item:${bindTargetId}`
  const itemId = row?.item_id
  if (itemId !== null && itemId !== undefined) return `item:${itemId}`
  return `name:${row?.asset_name ?? ''}`
}

/** 系统合计：按身份键去重后累加各自基线。 */
export function sumSystemBaseline(
  entries: ReadonlyArray<{
    row: BaselineRow | null | undefined
    bindTargetId?: number | null
  }>,
  itemAmounts: ReadonlyMap<number, Decimal>,
): Decimal {
  const seen = new Set<string>()
  let total = new Decimal(0)
  for (const e of entries) {
    const key = identityKeyOf(e.row, e.bindTargetId ?? null)
    if (seen.has(key)) continue
    seen.add(key)
    total = total.plus(rowBaseline(e.row, e.bindTargetId ?? null, itemAmounts))
  }
  return total
}