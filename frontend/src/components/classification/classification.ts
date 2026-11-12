'use client'

import type { QueryClient } from '@tanstack/react-query'
import type {
  AssetClass,
  AssetRecord,
  ClassificationDim,
  CurrentValueItem,
  InstitutionKind,
  ItemClassification,
  MemberRole,
  TagRef,
} from '@/types'

/**
 * 资产项分类（七维）的共享语义层：标签、快照、树、错误翻译、缓存失效面。
 *
 * 为什么单独一层：分类的读写分散在记录页、当前值表、设置页三处，
 * 若各写一份「缺哪维」「错误码怎么念」，用户会在不同页面看到不同说法。
 * 这里只放**纯函数与常量**，不放组件。
 */

// ===== 维度 =====

/** 必填四维（与后端 `ck_asset_items_dims_required` 一致）；缺任一维即为 draft。 */
export const REQUIRED_DIMS = ['asset_class', 'liquidity', 'institution', 'owner'] as const
export type RequiredDim = (typeof REQUIRED_DIMS)[number]

export const DIM_LABEL: Record<RequiredDim, string> = {
  asset_class: '资产类别',
  liquidity: '流动性',
  institution: '机构',
  owner: '归属',
}

/**
 * 后端 `missing_dims` 返回的是**列名**（`asset_class_id`/`liquidity_days`/
 * `institution_id`/`owner_id`），而接口类型里又写着无后缀写法。两种都认，
 * 认不出的直接忽略——宁可少显示一个词，也不把 `owner_id` 这类字段名甩给用户。
 */
export function requiredMissing(
  dims: readonly (ClassificationDim | string)[] | null | undefined,
): RequiredDim[] {
  const seen = new Set<RequiredDim>()
  for (const dim of dims ?? []) {
    if (dim === 'asset_class' || dim === 'asset_class_id') seen.add('asset_class')
    else if (dim === 'liquidity' || dim === 'liquidity_days') seen.add('liquidity')
    else if (dim === 'institution' || dim === 'institution_id') seen.add('institution')
    else if (dim === 'owner' || dim === 'owner_id') seen.add('owner')
  }
  return REQUIRED_DIMS.filter((dim) => seen.has(dim))
}

/** 「资产类别、机构」这类人话枚举。 */
export function dimsText(dims: readonly RequiredDim[]): string {
  return dims.map((dim) => DIM_LABEL[dim]).join('、')
}

// ===== 其它维度字典的展示名 =====

export const MEMBER_ROLE_LABEL: Record<MemberRole, string> = {
  self: '本人',
  spouse: '配偶',
  joint: '共有',
  child: '子女',
  nominee: '代持',
}

export const INSTITUTION_KIND_LABEL: Record<InstitutionKind, string> = {
  bank: '银行',
  broker: '券商',
  fund: '基金公司',
  insurer: '保险',
  gov: '政府 / 公共',
  platform: '平台',
  other: '其他',
}

export const INSTITUTION_KIND_OPTIONS: Array<{ value: InstitutionKind; label: string }> = (
  Object.keys(INSTITUTION_KIND_LABEL) as InstitutionKind[]
).map((value) => ({ value, label: INSTITUTION_KIND_LABEL[value] }))

export const MEMBER_ROLE_OPTIONS: Array<{ value: MemberRole; label: string }> = (
  Object.keys(MEMBER_ROLE_LABEL) as MemberRole[]
).map((value) => ({ value, label: MEMBER_ROLE_LABEL[value] }))

/** 风险等级：R1..R5 是有序标量（不是标签），展示名只是给人看的近似档位。 */
export const RISK_LEVEL_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'R1', label: 'R1 低风险' },
  { value: 'R2', label: 'R2 中低风险' },
  { value: 'R3', label: 'R3 中风险' },
  { value: 'R4', label: 'R4 中高风险' },
  { value: 'R5', label: 'R5 高风险' },
]

export const MAX_TAGS = 5

export function memberLabel(member: { name: string; role: MemberRole }): string {
  return `${member.name}（${MEMBER_ROLE_LABEL[member.role] ?? member.role}）`
}

/** 类别节点的侧别归属（`side_scope='any'` 时两侧都适用）。 */
export function sideScopeText(scope: AssetClass['side_scope']): string {
  if (scope === 'liability') return '负债'
  if (scope === 'asset') return '资产'
  return '通用'
}

/** 这条记录的方向能否挂这个类别（`any` 不设限）。 */
export function classFitsSide(
  node: Pick<AssetClass, 'side_scope'>,
  side: 'asset' | 'liability' | null,
): boolean {
  if (!side) return true
  return node.side_scope === 'any' || node.side_scope === side
}

// ===== 快照：把「行数据」归一成弹窗需要的形状 =====
//
// 记录行与当前值行都已经带回项级分类；弹窗直接吃行数据，不再为打开一次弹窗
// 发一次请求（也避免列表与弹窗显示不一致）。

export interface ClassificationSnapshot {
  itemId: number
  displayName: string
  /** 已知侧别；无记录时为 null（后端对无记录的项默认回 'asset'，不能当真）。 */
  side: 'asset' | 'liability' | null
  status: 'active' | 'draft' | 'archived'
  assetClassId: number | null
  assetClassPath: string | null
  liquidityDays: number | null
  institutionId: number | null
  channelId: number | null
  ownerId: number | null
  riskLevel: string | null
  tagIds: number[]
  tagNames: string[]
  missing: RequiredDim[]
}

export function snapshotFromCurrentValue(item: CurrentValueItem): ClassificationSnapshot {
  return {
    itemId: item.item_id,
    displayName: item.display_name,
    // 无记录 ⇒ 后端固定回 side:'asset'（没有方向来源），这里如实记 null
    side: item.record_id === null ? null : item.side,
    status: item.item_status,
    assetClassId: item.asset_class_id,
    assetClassPath: item.asset_class_path,
    liquidityDays: item.liquidity_days,
    institutionId: item.institution_id,
    channelId: item.channel_id,
    ownerId: item.owner_id,
    riskLevel: item.risk_level,
    tagIds: (item.tags ?? []).map((tag) => tag.id),
    tagNames: (item.tags ?? []).map((tag) => tag.name),
    missing: requiredMissing(item.classification_missing),
  }
}

export function snapshotFromRecord(
  record: AssetRecord,
  opts: { archived?: boolean } = {},
): ClassificationSnapshot | null {
  if (record.asset_item_id == null) return null
  const missing = requiredMissing(record.classification_missing)
  return {
    itemId: record.asset_item_id,
    displayName: record.display_name || record.asset_name,
    side: record.side ?? 'asset',
    status: opts.archived ? 'archived' : missing.length > 0 ? 'draft' : 'active',
    assetClassId: record.asset_class_id,
    assetClassPath: record.asset_class_path,
    liquidityDays: record.liquidity_days,
    institutionId: record.institution_id,
    channelId: record.channel_id,
    ownerId: record.owner_id,
    riskLevel: record.risk_level,
    tagIds: (record.tags ?? []).map((tag) => tag.id),
    tagNames: (record.tags ?? []).map((tag) => tag.name),
    missing,
  }
}

export function snapshotFromItemClassification(item: ItemClassification): ClassificationSnapshot {
  const tags: TagRef[] = item.tags ?? []
  return {
    itemId: item.item_id,
    displayName: item.display_name,
    side: null,
    status: item.status,
    assetClassId: item.asset_class_id,
    assetClassPath: item.asset_class_code
      ? `${item.asset_class_code} ${item.asset_class_name ?? ''}`.trim()
      : item.asset_class_name,
    liquidityDays: item.liquidity_days,
    institutionId: item.institution_id,
    channelId: item.channel_id,
    ownerId: item.owner_id,
    riskLevel: item.risk_level,
    tagIds: tags.map((tag) => tag.id),
    tagNames: tags.map((tag) => tag.name),
    missing: requiredMissing(item.missing_dims ?? item.classification_missing),
  }
}

/**
 * 改完分类后要失效的缓存面。
 *
 * 分类变更影响四处：当前值表、明细列表、资产项列表、以及所有按维度聚合的分析。
 * react-query 只重取已挂载的查询，多余项没有代价——漏项才会让用户看到旧数字。
 */
export function invalidateClassificationViews(queryClient: QueryClient): void {
  for (const queryKey of [
    'assetsCurrent',
    'assetsList',
    'assets',
    'assetItems',
    'assetNames',
    'summary',
    'byItem',
    'byItemFamily',
    'byClass',
    'byLiquidity',
    'byInstitution',
    'mixedChart',
    'proportion',
    'allocation',
    'itemOwners',
    'itemClassification',
  ]) {
    void queryClient.invalidateQueries({ queryKey: [queryKey] })
  }
}

// ===== 类别树 =====

export interface AssetClassNode {
  node: AssetClass
  children: AssetClassNode[]
}

/** 平铺 → 树；孤儿节点（父级缺失）提到根，绝不静默丢节点。 */
export function buildClassTree(classes: readonly AssetClass[]): AssetClassNode[] {
  const byId = new Map<number, AssetClassNode>()
  for (const node of classes) byId.set(node.id, { node, children: [] })
  const roots: AssetClassNode[] = []
  for (const entry of byId.values()) {
    const parentId = entry.node.parent_id
    const parent = parentId == null ? null : byId.get(parentId)
    if (parent && parent !== entry) parent.children.push(entry)
    else roots.push(entry)
  }
  const sortNodes = (nodes: AssetClassNode[]) => {
    nodes.sort((a, b) => a.node.code.localeCompare(b.node.code, 'zh-CN', { numeric: true }))
    nodes.forEach((child) => sortNodes(child.children))
  }
  sortNodes(roots)
  return roots
}

/** 只保留能挂资产项的节点（末级）。 */
export function leafClasses(classes: readonly AssetClass[]): AssetClass[] {
  return classes.filter((node) => node.leaf)
}

/** 某节点及其全部后代的 id 集合（删除前提示用）。 */
export function descendantIds(classes: readonly AssetClass[], rootId: number): number[] {
  const childrenOf = new Map<number, number[]>()
  for (const node of classes) {
    if (node.parent_id == null) continue
    const list = childrenOf.get(node.parent_id) ?? []
    list.push(node.id)
    childrenOf.set(node.parent_id, list)
  }
  const out: number[] = []
  const stack = [rootId]
  const seen = new Set<number>()
  while (stack.length > 0) {
    const id = stack.pop() as number
    if (seen.has(id)) continue
    seen.add(id)
    out.push(id)
    stack.push(...(childrenOf.get(id) ?? []))
  }
  return out
}

// ===== 错误翻译：错误码 → 用户能懂的一句话 =====
//
// 纪律：不把 JSON、字段名、英文码放到界面上；每条都说清「发生了什么 + 下一步做什么」。
// 后端错误信封是 `{"error":{code,message,details}}`，由 ApiError 承载。

interface ErrorLike {
  code: string
  message: string
  details: Record<string, unknown> | null
}

function asApiError(error: unknown): ErrorLike | null {
  if (typeof error !== 'object' || error === null) return null
  const candidate = error as Partial<ErrorLike>
  if (typeof candidate.code !== 'string') return null
  return {
    code: candidate.code,
    message: typeof candidate.message === 'string' ? candidate.message : '',
    details: (candidate.details as Record<string, unknown> | null | undefined) ?? null,
  }
}

function detailNumber(details: Record<string, unknown> | null, key: string): number | null {
  const value = details?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function detailStrings(details: Record<string, unknown> | null, key: string): string[] {
  const value = details?.[key]
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : []
}

/** 422 `details.fields[].message` 的通用兜底（字段级校验）。 */
function fieldMessage(details: Record<string, unknown> | null): string | null {
  const fields = details?.fields
  if (!Array.isArray(fields)) return null
  for (const entry of fields) {
    if (typeof entry !== 'object' || entry === null) continue
    const message = (entry as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return null
}

/** 保存分类时的错误。 */
export function classificationErrorText(error: unknown): string {
  const apiError = asApiError(error)
  if (!apiError) {
    return error instanceof Error && error.message ? error.message : '保存失败，请重试。'
  }
  switch (apiError.code) {
    case 'CLASS_NOT_LEAF': {
      const children = detailNumber(apiError.details, 'children')
      return children
        ? `这个类别下面还有 ${children} 个子类，只能选最末一级的分类。展开后再选。`
        : '这个类别下面还有子类，只能选最末一级的分类。'
    }
    case 'CLASS_SIDE_CONFLICT': {
      // details.side_scope ＝该类别自身的方向；details.conflicting_sides ＝与之冲突的
      // 该项方向（后端 `_check_side` 取的是记录侧的 side 集合，不是类别侧）。
      const sides = detailStrings(apiError.details, 'conflicting_sides')
      const scope = apiError.details?.side_scope
      const itemSide = sides.includes('liability') ? '负债' : sides.includes('asset') ? '资产' : ''
      const classSide = scope === 'liability' ? '负债' : scope === 'asset' ? '资产' : ''
      if (classSide && itemSide) {
        return `「${classSide}类」的类别只能挂在${classSide}项上，而这项是${itemSide}。请换一个${itemSide}类的类别。`
      }
      return '这个类别和这项的资产 / 负债方向不一致，请换一个方向相同的类别。'
    }
    case 'DIMS_INCOMPLETE': {
      const missing = requiredMissing(detailStrings(apiError.details, 'missing'))
      return missing.length > 0
        ? `还差${dimsText(missing)}没填。这四个必填维度都填上，这项才能正式生效。`
        : '四个必填维度（资产类别、流动性、机构、归属）必须全部填写。'
    }
    case 'TAG_LIMIT_EXCEEDED':
      return `一个资产项最多 ${MAX_TAGS} 个标签，先取消一个再选。`
    case 'ITEM_ARCHIVED':
      return '这个资产项已归档，不能再改分类。需要的话先取消归档。'
    case 'NOT_FOUND':
      return '找不到这个资产项，可能已被删除；请刷新后重试。'
    case 'VALIDATION_ERROR':
      return fieldMessage(apiError.details) ?? apiError.message ?? '填写的内容不合法，请检查后重试。'
    default:
      return apiError.message || '保存失败，请重试。'
  }
}

/** 维护类别树时的错误（改名 / 调整层级 / 删除）。 */
export function assetClassErrorText(
  error: unknown,
  action: 'create' | 'update' | 'delete' = 'update',
): string {
  const apiError = asApiError(error)
  if (!apiError) {
    return error instanceof Error && error.message ? error.message : '操作失败，请重试。'
  }
  switch (apiError.code) {
    case 'CLASS_CYCLE':
      return '不能把类别挂到它自己的下级里去，层级会绕成环。'
    case 'CLASS_HAS_CHILDREN': {
      const children = detailNumber(apiError.details, 'children')
      return children
        ? `这个类别下面还有 ${children} 个子类别。先删掉子类别，或把它们挪到别处。`
        : '这个类别下面还有子类别，先把子类别处理掉再删。'
    }
    case 'CLASS_IN_USE':
      return '已经有资产项用着这个类别，所以不能删。可以改名，或先把这些资产项换到别的类别。'
    case 'CLASS_SIDE_CONFLICT':
      return '这个类别下已有相反方向（资产 / 负债）的资产项，不能改成现在这个适用范围。'
    case 'VALIDATION_ERROR':
      return fieldMessage(apiError.details) ?? apiError.message ?? '填写的内容不合法，请检查后重试。'
    case 'INTERNAL':
      // 后端已知缺陷：`_auto_code` 用「兄弟数 + 1」生成子类编号，删过中间子类后
      // 会重复占用已有编号（UNIQUE 冲突 → 500）。给出可执行的绕法而不是「重试」。
      return action === 'create'
        ? '新建没有成功：自动编号撞上了已存在的编号（删过子类别会留空档）。请在「编号」里手填一个没用过的值再试。'
        : '保存没有成功（服务端内部错误）。请刷新后重试；仍失败请检查这个类别的编号是否重复。'
    default:
      return apiError.message || '操作失败，请重试。'
  }
}

/** 维护机构 / 渠道 / 成员 / 标签时的错误。`noun`＝「机构」这类名词。 */
export function dictionaryErrorText(error: unknown, noun: string): string {
  const apiError = asApiError(error)
  if (!apiError) {
    return error instanceof Error && error.message ? error.message : '操作失败，请重试。'
  }
  switch (apiError.code) {
    case 'DICT_IN_USE':
      return `还有资产项在用这个${noun}，所以不能删。可以改名，或先把相关资产项换成别的${noun}。`
    case 'NOT_FOUND':
      return `找不到这个${noun}，可能已被删除；请刷新后重试。`
    case 'VALIDATION_ERROR':
      // 重名等业务校验：后端 message 已是「「X」已存在」这类人话
      return fieldMessage(apiError.details) ?? apiError.message ?? '填写的内容不合法，请检查后重试。'
    default:
      return apiError.message || '操作失败，请重试。'
  }
}