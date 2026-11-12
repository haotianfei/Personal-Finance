/**
 * 多页截图录入：总表树序、归父校验、跨图核对（ADR-0029，票 03/04/05/09）。
 *
 * 为什么单独成模块：复核页 `app/screenshot/page.tsx` 已 6000+ 行，而下面三件事
 * 都有「换一处实现就会漂移」的风险，必须只有一份口径：
 *
 * 1. **树序**：跨图父子关系要摊平在一张总表里，父行与其子行必须相邻、子行只缩进
 *    名称列（金额列右对齐基线不许被缩进推动）。排序一旦写进渲染循环，筛选器
 *    每加一个视图就会各排一遍。
 * 2. **归父护栏**：不能挂到自己、不能挂到自己的后代——这两条在「归到…」选择器与
 *    拖拽加速器里各要判一次，两处必须同源。目标行不必先手勾汇总：归父即自动置为
 *    汇总行（`aggregateSource='parent'`，spec `.scratch/review-tree-visual` D11）。
 * 3. **核对口径**：差额 = 父项金额 − **直接**子项合计（叶子计量：父项不计入合计，
 *    子项照常计入；见 ADR-0029 D1）。零容差，只有「改子项 / 补子项 / 显式接受」
 *    三个出口，接受之后差额再变即作废。
 *
 * 金额一律 `decimal.js`，本模块不出现浮点加法。
 */
import Decimal from 'decimal.js'

// ---------------------------------------------------------------- 行键与父引用

/** 跨图行键：`(imgId, index)` 是本批内唯一的行身份，跨图引用只能靠它。 */
export type RowKey = string

/** 行键构造／解析（imgId 由前端生成，不含 `\u0000`，故可安全拼接）。 */
export function rowKeyOf(imgId: string, index: number): RowKey {
  return `${imgId}\u0000${index}`
}

export function splitRowKey(key: RowKey): { imgId: string; index: number } | null {
  const at = key.indexOf('\u0000')
  if (at <= 0) return null
  const index = Number(key.slice(at + 1))
  if (!Number.isFinite(index)) return null
  return { imgId: key.slice(0, at), index }
}

/**
 * 一行的父项引用。
 * - `row`：本批里勾了「汇总行」的某一行（可跨图），父项金额取该行金额；
 * - `item`：既有资产项（已落库、有现值），父项金额取该项当前值。
 *
 * 用结构化联合而不是 `parentId: string`：两种父项的后果、可核对性、最终
 * 载荷形状都不同，字符串 id 会把这些差异推迟到运行时。
 */
export type ParentRef =
  | { kind: 'row'; key: RowKey }
  | { kind: 'item'; itemId: number; name: string }

/** 树序与归父判定所需的最小行面（只有身份与从属，不含金额与文案）。 */
export interface TreeRowLike {
  key: RowKey
  parent?: ParentRef | null
}

/** 参与核对与候选列举所需的最小行面（树序 + 金额 + 汇总标记）。 */
export interface MeterRow extends TreeRowLike {
  name: string
  amount: string
  /** 这一行是否被勾为「汇总行」（只核对、不入账） */
  aggregate: boolean
}

// ---------------------------------------------------------------- 树序

// ---------------------------------------------------------------- 缩进与引导线

/**
 * 引导线车道宽（px）：每一级祖先占一条车道，本行自己的连接符再占最后一条。
 * 因此内容（名称文字）左缘 = `indentPad(level)`，它同时也是横折的右端点。
 *
 * 这是**唯一**的缩进常量出处（spec D3）：桌面名称列、身份黄框、窄屏卡片都调它，
 * 不再各写 `level * 16` / `level * 16 + 20` / `level * 12`（三处已漂移过）。
 */
export const LANE_PX = 16

/** 车道中线（线心）：`lane * LANE_PX + LANE_CENTER`，也是横折的起点。 */
export const LANE_CENTER = LANE_PX / 2

/** 内容左缘 = `(level + 1) * LANE_PX`（最后一条车道留给本行的 ├/└ 连接符）。 */
export function indentPad(level: number): number {
  return (Math.max(0, level) + 1) * LANE_PX
}

export interface TreeRow<T> {
  row: T
  /** 缩进层级（0 = 根）；缩进只作用于名称列，金额列不参与 */
  level: number
  /** 本批内可见的直接子项数（>0 才给折叠按钮） */
  childCount: number
  /** 父项不在这张视图里（被筛选掉，或是既有资产项）：行首给「↳ 归到 X」小标记 */
  parentOutside: boolean
  /**
   * 同级里是否是最后一个（末位）：连接符画 `└` 且竖线截断在横折处；
   * 非末位画 `├` 且竖线贯穿整行高。
   */
  isLast: boolean
  /**
   * 每个祖先车道是否还要继续向下（该祖先后面还有兄弟）；长度 = `level`。
   * `ancestorHasNext[i] === false` 表示该车道是空白——**渲染时整格不出线元素**。
   */
  ancestorHasNext: boolean[]
}

/**
 * 摊平成「父行紧跟其子行」的树序。
 *
 * 三条例外都按「可见即根」处理，绝不隐藏行（隐藏=用户以为数据丢了）：
 * - 父项被筛选器挡在外面 → 该行按根渲染，`parentOutside=true`；
 * - 父项是既有资产项（不在本批行里）→ 同上；
 * - 成环脏数据 → 先走完可达根，再兜底补出没被访问到的行。
 *
 * 根的顺序＝传入顺序（＝图序 / 行序），因此「筛选到某一张图」时行序与总表一致。
 */
export function orderAsTree<T extends TreeRowLike>(rows: readonly T[]): TreeRow<T>[] {
  const byKey = new Map<RowKey, T>()
  for (const r of rows) byKey.set(r.key, r)

  const childrenOf = new Map<RowKey, T[]>()
  const roots: T[] = []
  for (const r of rows) {
    const p = r.parent
    if (p && p.kind === 'row' && p.key !== r.key && byKey.has(p.key)) {
      const list = childrenOf.get(p.key)
      if (list) list.push(r)
      else childrenOf.set(p.key, [r])
    } else {
      roots.push(r)
    }
  }

  const out: TreeRow<T>[] = []
  const seen = new Set<RowKey>()
  // `ancestorHasNext` 就是「每个祖先后面还有没有兄弟」，递归时随父链带下来：
  // 父行自己是否末位 → 子行的最后一条车道要不要继续。这样不必回头扫描后续行，
  // 末位判定天然看穿整棵子树（只看下一行会把所有父项误判成非末位）。
  const walk = (row: T, level: number, ancestorHasNext: boolean[], isLast: boolean) => {
    if (seen.has(row.key)) return
    seen.add(row.key)
    const kids = childrenOf.get(row.key) ?? []
    const p = row.parent
    out.push({
      row,
      level,
      childCount: kids.length,
      parentOutside: !!p && !(p.kind === 'row' && byKey.has(p.key) && p.key !== row.key),
      isLast,
      ancestorHasNext,
    })
    const childLanes = [...ancestorHasNext, !isLast]
    kids.forEach((k, i) => walk(k, level + 1, childLanes, i === kids.length - 1))
  }
  roots.forEach((r, i) => walk(r, 0, [], i === roots.length - 1))
  for (const r of rows) if (!seen.has(r.key)) walk(r, 0, [], true)
  return out
}

/** 该行的全部后代行键（不含自身）；用于「不能挂到自己的子项下」与选择器排除。 */
export function descendantKeys(rows: readonly TreeRowLike[], key: RowKey): RowKey[] {
  const childrenOf = new Map<RowKey, RowKey[]>()
  for (const r of rows) {
    const p = r.parent
    if (p && p.kind === 'row' && p.key !== r.key) {
      const list = childrenOf.get(p.key)
      if (list) list.push(r.key)
      else childrenOf.set(p.key, [r.key])
    }
  }
  const out: RowKey[] = []
  const seen = new Set<RowKey>([key])
  const stack = [...(childrenOf.get(key) ?? [])]
  while (stack.length) {
    const cur = stack.pop() as RowKey
    if (seen.has(cur)) continue
    seen.add(cur)
    out.push(cur)
    stack.push(...(childrenOf.get(cur) ?? []))
  }
  return out
}

// ---------------------------------------------------------------- 归父护栏

/**
 * 投放判定结果。文案留在组件里（同一份判定，选择器与拖拽用不同说法），
 * 这里只给**原因码**——文案散落是文案漂移的开始，判定散落是逻辑漂移的开始。
 */
export type DropVerdict =
  | { ok: true }
  | { ok: false; reason: 'self' | 'descendant' | 'unknown-target' }

/**
 * 「把 childKey 归到 targetKey 下」是否合法。
 *
 * 顺序即用户的犯错概率：先拦「拖到自己身上」（拖拽最常见的手滑），再拦成环
 * （罕见但一旦发生，派生计量会变成不动点计算）。
 *
 * 目标行**不必**先手勾汇总行：归父成功后调用方会把目标行自动置为汇总行
 * （`aggregateSource='parent'`）。旧护栏「只能挂到汇总行下」已按 spec D11 撤除。
 */
export function checkDrop(
  rows: readonly (TreeRowLike & { aggregate: boolean })[],
  childKey: RowKey,
  targetKey: RowKey,
): DropVerdict {
  if (childKey === targetKey) return { ok: false, reason: 'self' }
  const target = rows.find((r) => r.key === targetKey)
  if (!target) return { ok: false, reason: 'unknown-target' }
  if (descendantKeys(rows, childKey).includes(targetKey)) return { ok: false, reason: 'descendant' }
  return { ok: true }
}

/** 投放护栏的中文文案（唯一出处；两处入口共用，避免说两套话）。 */
export const DROP_HINT: Record<'self' | 'descendant' | 'unknown-target', string> = {
  self: '不能归到自己这一行下',
  descendant: '不能归到自己或自己的子项下',
  'unknown-target': '这一行已不在清单里，松开无效',
}

// ---------------------------------------------------------------- 父项候选

export interface ParentCandidate {
  /** `row:<行键>` 或 `item:<资产项 id>` */
  id: string
  kind: 'row' | 'item'
  name: string
  /** 右侧元信息：图 N · 已有 k 个子项 / 既有资产项 */
  meta: string
  /** 金额（两位小数字符串）；未知时为 null */
  amount: string | null
  key?: RowKey
  itemId?: number
}

/** 既有资产项现值（`item_id → 名称 + 金额`，由预览的 `existing_items` 汇总而来）。 */
export interface ExistingItemOption {
  itemId: number
  name: string
  amount: string
  /** 出现在哪张图（图序 1 基）；同一项在多张图里出现时取首张 */
  imageOrdinal?: number
}

/**
 * 「归到…」候选：本批任意行（排除自身与后代）＋ 可手选的既有资产项。
 *
 * 行候选不再要求目标已勾汇总：归父即自动置为汇总行（spec D11），候选里只剩
 * 「不能挂自己 / 不能挂后代」两条护栏。
 *
 * 既有资产项**只列本次预览真的读到现值的那些**：父项金额读不到就无法核对差额，
 * 列出来只会把「未知」塞进核对面板。读不到的情况（旧后端 / 该项不在本组截图里）
 * 由调用方在空态文案里说明。
 */
export function buildParentCandidates(
  rows: readonly MeterRow[],
  childKey: RowKey,
  existingItems: readonly ExistingItemOption[],
  imageOrdinalOf: (key: RowKey) => number,
): ParentCandidate[] {
  const banned = new Set<RowKey>([childKey, ...descendantKeys(rows, childKey)])
  const rowCandidates: ParentCandidate[] = rows
    .filter((r) => !banned.has(r.key))
    .map((r) => {
      const childCount = rows.filter(
        (x) => x.parent && x.parent.kind === 'row' && x.parent.key === r.key,
      ).length
      const ordinal = imageOrdinalOf(r.key)
      return {
        id: `row:${r.key}`,
        kind: 'row' as const,
        name: r.name,
        meta: `图 ${ordinal} · ${childCount ? `${childCount} 个子项` : '还没有子项'}`,
        amount: r.amount,
        key: r.key,
      }
    })
  const itemCandidates: ParentCandidate[] = existingItems.map((it) => ({
    id: `item:${it.itemId}`,
    kind: 'item' as const,
    name: it.name,
    meta: it.imageOrdinal ? `既有资产项 · 图 ${it.imageOrdinal}` : '既有资产项',
    amount: it.amount,
    itemId: it.itemId,
  }))
  return [...rowCandidates, ...itemCandidates]
}

// ---------------------------------------------------------------- 汇总来源与差额标记

/**
 * 这一行的「汇总行」勾选是谁置上的：
 * - `manual` 用户手勾（取消只能用户手点，不会被自动撤）；
 * - `rule`   后端 `aggregate_hint` 命中汇总词表预勾；
 * - `parent` 有子项归到它名下时自动置位。
 *
 * 只有 `parent` 会被自动撤销（最后一条子边解除时）——自动撤手勾的会把用户意图吃掉。
 */
export type AggregateSource = 'manual' | 'rule' | 'parent'

export interface AggregateFlag {
  aggregate: boolean
  aggregateSource: AggregateSource | null
}

/** 行内微文案：勾选来源（与「汇总项 · 不计入合计」拼在一起显示）。 */
export const AGGREGATE_SOURCE_LABEL: Record<AggregateSource, string> = {
  manual: '手动勾选',
  rule: '词表预勾',
  parent: '归父自动',
}

/** 有子项归入：成为汇总行。已经是汇总行的保留原来源（不把手勾降级成 parent）。 */
export function withChildAdded(flag: AggregateFlag): AggregateFlag {
  if (flag.aggregate) return { aggregate: true, aggregateSource: flag.aggregateSource ?? 'manual' }
  return { aggregate: true, aggregateSource: 'parent' }
}

/** 最后一条子边解除：只撤自动置的那次勾选，手勾 / 词表预勾原样保留。 */
export function withLastChildRemoved(flag: AggregateFlag): AggregateFlag {
  if (flag.aggregate && flag.aggregateSource === 'parent') {
    return { aggregate: false, aggregateSource: null }
  }
  return flag
}

/**
 * 用户手点「汇总行」。
 *
 * `blocked=true` 表示这次点击应当被拒绝并回退：父行还有子项时取消汇总会让父子
 * 同计（ADR-0029 D1 缺省禁父子同计），必须先解除子项归父。
 */
export function withManualToggle(
  flag: AggregateFlag,
  childCount: number,
): { flag: AggregateFlag; blocked: boolean } {
  if (flag.aggregate) {
    if (childCount > 0) return { flag, blocked: true }
    return { flag: { aggregate: false, aggregateSource: null }, blocked: false }
  }
  return { flag: { aggregate: true, aggregateSource: 'manual' }, blocked: false }
}

/** 带符号金额（两位小数，千分位）：`+¥1,200.00` / `−¥12.00` / `¥0.00`。 */
export function formatSignedMoney(value: Decimal | null): string {
  if (value === null) return '金额未知'
  const fixed = value.abs().toFixed(2)
  const [int, frac] = fixed.split('.')
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const sign = value.isZero() ? '' : value.isNegative() ? '−' : '+'
  return `${sign}¥${grouped}.${frac}`
}

/** 汇总行金额下方那枚差额标记（四态；`stale` 并入 `mismatch` 并注明接受已作废）。 */
export interface AggregateDiffBadge {
  state: 'matched' | 'accepted' | 'mismatch' | 'unknown'
  /** 行内短文案：也是 `data-aggregate-diff` 旁边的可读文本 */
  label: string
  tone: 'ok' | 'accepted' | 'alert' | 'muted'
  /** 展开后的算式：`父 ¥A − 子 ¥S` */
  detail: string
}

/**
 * 差额标记的唯一口径（文案 / 色阶 / 算式都在这里，页面只负责排版）。
 *
 * - 四态直接对应 `ReconcileItem.state`，唯独 `stale`（接受过但差额又变了）
 *   归入 `mismatch`：它对用户的含义就是「现在对不上」，多给一个态只会让人再学一遍；
 * - 金额一律 Decimal，不经 float；`diff === null`（父项金额读不到）→ `unknown`。
 */
export function aggregateDiffBadge(item: ReconcileItem): AggregateDiffBadge {
  const detail = `父 ${formatSignedMoney(item.parentAmount)} − 子 ${formatSignedMoney(item.childSum)}`
  if (item.diff === null) {
    return { state: 'unknown', label: '子项待确认', tone: 'muted', detail: `${detail}（父项金额读不到，无法核对）` }
  }
  const signed = formatSignedMoney(item.diff)
  if (item.state === 'matched') {
    return { state: 'matched', label: '已对上', tone: 'ok', detail: `${detail} = ¥0.00` }
  }
  if (item.state === 'accepted') {
    const reason = item.accepted?.reason?.trim()
    return {
      state: 'accepted',
      label: `已接受 ${signed}`,
      tone: 'accepted',
      detail: `${detail} = ${signed}${reason ? ` · 理由：${reason}` : ''}`,
    }
  }
  if (item.state === 'stale') {
    return {
      state: 'mismatch',
      label: `差 ${signed}（接受已作废）`,
      tone: 'alert',
      detail: `${detail} = ${signed} · 差额又变了，之前的接受已作废，需要重新接受`,
    }
  }
  return { state: 'mismatch', label: `差 ${signed}`, tone: 'alert', detail: `${detail} = ${signed}` }
}

// ---------------------------------------------------------------- 跨图核对

/** 核对面板里的父项（一条核对记录 = 一个父项 + 它的直接子项）。 */
export interface ReconcileParentInput {
  /** `row:<行键>` 或 `item:<资产项 id>` */
  id: string
  kind: 'row' | 'item'
  name: string
  /** 图序（1 基）；既有资产项为 null */
  imageOrdinal: number | null
  /** 父项金额；null = 读不到（无法核对，不阻断） */
  amount: string | null
  /** 后端建构成关系要的 item_id；null = 这一行会新建资产项（无法挂子项） */
  itemId: number | null
  /** 行锚（finalize 记录用）；既有资产项为 null */
  evidenceId: number | null
  rowIndex: number | null
}

export interface ReconcileChildInput {
  /** 父项 id（与 `ReconcileParentInput.id` 同域） */
  parentId: string
  key: RowKey
  name: string
  amount: string
  evidenceId: number | null
  rowIndex: number | null
  /** 这一行落库后的资产项（已命中 / 已关联才有）；本次才新建的行为 null */
  itemId?: number | null
}

/** 一次「接受差额」的留痕（差额快照 + 理由 + 时间）。 */
export interface AcceptedMismatch {
  /** 接受当时的差额（两位小数字符串）——与当前差额不等即作废 */
  diff: string
  reason: string
  at: number
}

export type ReconcileState = 'matched' | 'accepted' | 'stale' | 'mismatch' | 'unknown'

export interface ReconcileItem {
  parent: ReconcileParentInput
  children: ReconcileChildInput[]
  parentAmount: Decimal | null
  childSum: Decimal
  /** 父项金额 − 直接子项合计；父项金额未知时 null */
  diff: Decimal | null
  state: ReconcileState
  accepted: AcceptedMismatch | null
  /** 阻断写入的原因（null = 不阻断） */
  blockKind: 'mismatch' | 'stale' | 'parent-not-asset' | null
}

function toDec(v: string | number | null | undefined): Decimal {
  if (v === null || v === undefined || v === '') return new Decimal(0)
  try {
    return new Decimal(v)
  } catch {
    return new Decimal(0)
  }
}

/**
 * 逐父项算差额与状态。
 *
 * - 差额零容差：`diff.isZero()` 才算「一致」，0.01 的差也是差（D10）；
 * - 接受过的差额必须**与当前差额逐分相等**才继续有效，差额一变即回到
 *   「接受已作废」——静默沿用过期结论是这类核对面板最容易出的错；
 * - 父项金额读不到 → `unknown`，不阻断（没法核对≠对不上），由面板写清原因。
 */
export function computeReconciliations(
  parents: readonly ReconcileParentInput[],
  children: readonly ReconcileChildInput[],
  accepted: Readonly<Record<string, AcceptedMismatch>>,
): ReconcileItem[] {
  const byParent = new Map<string, ReconcileChildInput[]>()
  for (const c of children) {
    const list = byParent.get(c.parentId)
    if (list) list.push(c)
    else byParent.set(c.parentId, [c])
  }
  return parents.map((parent) => {
    const kids = byParent.get(parent.id) ?? []
    const childSum = kids.reduce((acc, c) => acc.plus(toDec(c.amount)), new Decimal(0))
    const parentAmount = parent.amount === null ? null : toDec(parent.amount)
    const diff = parentAmount === null ? null : parentAmount.minus(childSum)
    const acc = accepted[parent.id] ?? null

    let state: ReconcileState
    if (diff === null) state = 'unknown'
    else if (diff.isZero()) state = 'matched'
    else if (acc && acc.diff === diff.toFixed(2)) state = 'accepted'
    else if (acc) state = 'stale'
    else state = 'mismatch'

    let blockKind: ReconcileItem['blockKind'] = null
    if (parent.kind === 'row' && parent.itemId === null) blockKind = 'parent-not-asset'
    else if (state === 'mismatch') blockKind = 'mismatch'
    else if (state === 'stale') blockKind = 'stale'

    return {
      parent,
      children: kids,
      parentAmount,
      childSum,
      diff,
      state,
      accepted: acc,
      blockKind,
    }
  })
}

/** 阻断写入的核对项条数（按钮禁用与状态文案的唯一来源）。 */
export function blockingReconciliations(items: readonly ReconcileItem[]): ReconcileItem[] {
  return items.filter((it) => it.blockKind !== null)
}

// ---------------------------------------------------------------- finalize 载荷

/**
 * finalize 的 `reconciliations[]` 一项（**契约已冻结**，后端按
 * `(parent_evidence_id, parent_row_index)` 与子项行锚复核金额）。
 *
 * 父项金额、子项金额都随载荷带上：后端仍用自己的 Decimal 重算（D4 唯一校验点），
 * 前端带的是「用户看到的那个数」，两端不一致时正是要报出来的错。
 */
export interface ReconcilePayload {
  parent_item_id: number | null
  parent_evidence_id: number | null
  parent_row_index: number | null
  aggregate_amount: string
  children: {
    evidence_id: number
    row_index: number
    item_id: number | null
    amount: string
  }[]
}

/**
 * 核对项 → finalize 载荷。
 *
 * 两类父项的锚：
 * - 本批汇总行：`parent_item_id` + `(parent_evidence_id, parent_row_index)` 三者齐全；
 * - 本批之外的既有资产项（「归到…」里手选的那一支）：只有 `parent_item_id`，
 *   两个行锚为 null——本批没有它的行，行锚无处可取。
 *
 * 不发的情况（宁可少发一条，也不发一条自己都算不清的）：
 * - 父项金额未知（读不到现值，差额无从复核）；
 * - 本批汇总行缺 evidence/行锚（旧后端没有 `evidence_id`）；
 * - 本批汇总行还没落成资产项（`item_id=null`，后端会 409 RECONCILE_PARENT_NOT_FOUND，
 *   界面上已提前硬阻断）；
 * - 任一子项缺行锚（记录必须可追溯）。
 */
export function toReconcilePayloads(items: readonly ReconcileItem[]): ReconcilePayload[] {
  const out: ReconcilePayload[] = []
  for (const it of items) {
    if (it.parentAmount === null) continue
    const anchored = it.parent.evidenceId !== null && it.parent.rowIndex !== null
    if (it.parent.kind === 'row' && !anchored) continue
    if (it.parent.itemId === null) continue
    if (it.children.some((c) => c.evidenceId === null || c.rowIndex === null)) continue
    out.push({
      parent_item_id: it.parent.itemId,
      parent_evidence_id: it.parent.evidenceId,
      parent_row_index: it.parent.rowIndex,
      aggregate_amount: it.parentAmount.toFixed(2),
      children: it.children.map((c) => ({
        evidence_id: c.evidenceId as number,
        row_index: c.rowIndex as number,
        item_id: c.itemId ?? null,
        amount: c.amount,
      })),
    })
  }
  return out
}

// ---------------------------------------------------------------- 批次错误 → 中文

export interface BatchErrorContext {
  /** evidence_id → 「第 N 张 · 文件名」 */
  imageLabelOf: (evidenceId: number) => string
  /** 父项 item_id → 本批里对应的行名（用于点名核对项） */
  parentNameOfItem: (itemId: number) => string | null
}

export interface BatchErrorText {
  /** 一句话结论（横幅标题） */
  title: string
  /** 具体到行/项的说明；没有则为 null */
  detail: string | null
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() !== '' ? v : null
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}

function list(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v)
    ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
    : []
}

function money(v: unknown): string {
  const n = num(v)
  if (n === null) return '金额未知'
  const d = new Decimal(n)
  const fixed = d.abs().toFixed(2)
  const [int, frac] = fixed.split('.')
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${d.isNegative() ? '−' : ''}¥${grouped}.${frac}`
}

function numList(v: unknown): number[] {
  if (!Array.isArray(v)) return []
  return v
    .map((x) => (typeof x === 'object' && x !== null ? num((x as Record<string, unknown>).evidence_id ?? (x as Record<string, unknown>).id) : num(x)))
    .filter((n): n is number => n !== null)
}

/**
 * 批次错误 → 用户读得懂的一句话＋指到具体行。
 *
 * `details` 的键名**已冻结**，按冻结键取值：
 * - `BATCH_NOT_COMPLETE` → `missing_evidence_ids: number[]`
 * - `RECONCILE_MISMATCH` → `reconciliations: [{parent_item_id, aggregate_amount, children_sum, diff}]`
 * - `RECONCILE_PARENT_NOT_FOUND` → `{parent_row_index, name}`
 * - `RECONCILE_REASON_REQUIRED` → `{}`
 *
 * 旧键名（`missing` / `mismatches` / `parent_name` …）只作兜底，且**永远不把 JSON 糊到界面上**：
 * 取不到明细时退回一句通用说法。
 */
export function describeBatchError(
  code: string,
  details: Record<string, unknown> | null,
  ctx: BatchErrorContext,
): BatchErrorText {
  const d = details ?? {}
  switch (code) {
    case 'BATCH_NOT_COMPLETE': {
      const missing = numList(
        d.missing_evidence_ids ?? d.missing ?? d.members ?? d.evidence_ids,
      )
      if (missing.length) {
        return {
          title: `还有 ${missing.length} 张截图没写入成功，本批不能结算`,
          detail: `未完成：${missing.map((id) => ctx.imageLabelOf(id)).join('、')}`,
        }
      }
      return { title: '这一批的截图还没全部写入成功，本批不能结算', detail: null }
    }
    case 'RECONCILE_MISMATCH': {
      const mismatches = list(d.reconciliations ?? d.mismatches ?? (d.parent_item_id != null ? [d] : []))
      if (mismatches.length) {
        const parts = mismatches.slice(0, 3).map((m) => {
          const itemId = num(m.parent_item_id)
          const name =
            str(m.parent_name) ??
            (itemId !== null ? ctx.parentNameOfItem(itemId) : null) ??
            '汇总行'
          const diff = num(m.diff)
          const parent = num(m.aggregate_amount ?? m.parent_amount)
          const sum = num(m.children_sum ?? m.child_amount_sum)
          const body =
            diff === null
              ? '差额未处理'
              : `差额 ${diff > 0 ? '+' : diff < 0 ? '−' : ''}¥${new Decimal(Math.abs(diff)).toFixed(2)}`
          const nums =
            parent !== null && sum !== null
              ? `（父项 ${money(parent)}，子项合计 ${money(sum)}）`
              : ''
          return `「${name}」${body}${nums}`
        })
        return {
          title: `有 ${mismatches.length} 条核对差额没处理，本批不能结算`,
          detail: `${parts.join('；')}${mismatches.length > 3 ? ' 等' : ''}。改子项金额，或在这一行接受差额并写明理由`,
        }
      }
      return {
        title: '有核对差额没处理，本批不能结算',
        detail: '改子项金额，或在该汇总行接受差额并写明理由',
      }
    }
    case 'RECONCILE_PARENT_NOT_FOUND': {
      const name = str(d.name) ?? str(d.parent_name)
      return {
        title: '汇总行需先落成资产项',
        detail: name
          ? `「${name}」本次会新建资产项：先取消它的「汇总」勾选写入，再回来勾。`
          : '先取消这里的「汇总」勾选写入，再回来勾。',
      }
    }
    case 'RECONCILE_REASON_REQUIRED':
      return {
        title: '接受差额必须写明理由',
        detail: '在核对面板点「接受差额」，写清为什么差着也能过，再结算',
      }
    case 'COMPOSITION_PARENT_CONFLICT': {
      const name = str(d.child_name)
      return {
        title: '这个子项已经挂在别的汇总项下',
        detail: name
          ? `「${name}」只能有一个父项：先在原父项下取消归属，再归到新的这一项`
          : '一个子项只能有一个父项：先在原父项下取消归属，再归到新的这一项',
      }
    }
    case 'COMPOSITION_CYCLE':
      return {
        title: '这样归会绕成环，不能建立',
        detail: '目标行已经在这条链的子项链里：换一个父项，或先把那一段归属拆开',
      }
    case 'BATCH_ALREADY_FINALIZED':
      return { title: '这一批已经结算过，不能重复结算', detail: null }
    case 'RECONCILE_CONFLICT':
      return {
        title: '同一行不能既跳过又核对',
        detail: '把该行的「跳过」或「汇总」取消一个，再提交',
      }
    default:
      return { title: '这一批没能结算', detail: null }
  }
}