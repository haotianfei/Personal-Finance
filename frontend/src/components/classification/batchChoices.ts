import type { ItemClassificationUpdate } from '@/types'
import { MAX_TAGS, REQUIRED_DIMS } from './classification'
import type { ClassificationSnapshot, RequiredDim } from './classification'

/**
 * 批量补分类的**纯逻辑**：选择 → 请求体 → 结果判定。
 *
 * 单独成文件的原因：这部分决定了「部分更新」的语义（哪些维度会被写、哪些保持原值），
 * 是这个功能的正确性核心，必须能脱离 React 被直接测到。
 */

export type ScalarMode = 'keep' | 'set' | 'clear'
export type TagMode = 'keep' | 'add' | 'remove'

export interface Choice<T> {
  mode: ScalarMode
  value: T
}

export const keep = <T,>(value: T): Choice<T> => ({ mode: 'keep', value })

/** 批量表单的全部选择（只有被主动改过的维度才生效）。 */
export interface BatchChoices {
  assetClass: Choice<number | null>
  liquidity: Choice<string>
  institution: Choice<string>
  channel: Choice<string>
  owner: Choice<string>
  risk: Choice<string>
  tagMode: TagMode
  tagIds: number[]
  reason: string
}

export const EMPTY_CHOICES: BatchChoices = {
  assetClass: keep(null),
  liquidity: keep(''),
  institution: keep(''),
  channel: keep(''),
  owner: keep(''),
  risk: keep(''),
  tagMode: 'keep',
  tagIds: [],
  reason: '',
}

/**
 * 本次要写的分类维度：**只包含用户真正选了的维度**。
 * 没选的维度不出现在请求体里，后端因此保持各项原值（部分更新）。
 */
export function buildPayload(
  snapshot: ClassificationSnapshot,
  choices: BatchChoices,
): ItemClassificationUpdate {
  const payload: ItemClassificationUpdate = {}
  if (choices.assetClass.mode === 'set' && choices.assetClass.value != null
      && choices.assetClass.value !== snapshot.assetClassId) {
    payload.asset_class_id = choices.assetClass.value
  }
  if (choices.liquidity.mode === 'set' && choices.liquidity.value) {
    const days = Number(choices.liquidity.value)
    if (days !== snapshot.liquidityDays) payload.liquidity_days = days
  }
  if (choices.institution.mode === 'set' && choices.institution.value) {
    const id = Number(choices.institution.value)
    if (id !== snapshot.institutionId) payload.institution_id = id
  }
  if (choices.owner.mode === 'set' && choices.owner.value) {
    const id = Number(choices.owner.value)
    if (id !== snapshot.ownerId) payload.owner_id = id
  }
  if (choices.channel.mode === 'clear') {
    if (snapshot.channelId != null) payload.channel_id = null
  } else if (choices.channel.mode === 'set' && choices.channel.value) {
    const id = Number(choices.channel.value)
    if (id !== snapshot.channelId) payload.channel_id = id
  }
  if (choices.risk.mode === 'clear') {
    if (snapshot.riskLevel != null) payload.risk_level = null
  } else if (choices.risk.mode === 'set' && choices.risk.value
      && choices.risk.value !== snapshot.riskLevel) {
    payload.risk_level = choices.risk.value
  }
  if (choices.reason.trim()) payload.reason = choices.reason.trim()
  return payload
}

/** 标签按项算增量；超上限返回 overflow（不静默截断）。 */
export function buildTags(
  snapshot: ClassificationSnapshot,
  choices: BatchChoices,
): { next: number[] | null; overflow: boolean } {
  if (choices.tagMode === 'keep' || choices.tagIds.length === 0) {
    return { next: null, overflow: false }
  }
  const current = snapshot.tagIds
  const next = choices.tagMode === 'add'
    ? Array.from(new Set([...current, ...choices.tagIds]))
    : current.filter((id) => !choices.tagIds.includes(id))
  if (next.length > MAX_TAGS) return { next: null, overflow: true }
  const changed = next.length !== current.length || next.some((id) => !current.includes(id))
  return { next: changed ? next : null, overflow: false }
}

/** 写入之后这项还缺哪几个必填维度。 */
export function missingAfter(
  snapshot: ClassificationSnapshot,
  payload: ItemClassificationUpdate,
): RequiredDim[] {
  const present = {
    asset_class: payload.asset_class_id !== undefined
      ? payload.asset_class_id != null
      : snapshot.assetClassId != null,
    liquidity: payload.liquidity_days !== undefined
      ? payload.liquidity_days != null
      : snapshot.liquidityDays != null,
    institution: payload.institution_id !== undefined
      ? payload.institution_id != null
      : snapshot.institutionId != null,
    owner: payload.owner_id !== undefined
      ? payload.owner_id != null
      : snapshot.ownerId != null,
  }
  return REQUIRED_DIMS.filter((dim) => !present[dim])
}

/** 本次是否真的会写点什么（用来拦「什么都没选就提交」）。 */
export function hasChange(choices: BatchChoices): boolean {
  const scalarTouched = (
    choices.assetClass.mode !== 'keep' || choices.liquidity.mode !== 'keep'
    || choices.institution.mode !== 'keep' || choices.owner.mode !== 'keep'
    || choices.channel.mode !== 'keep' || choices.risk.mode !== 'keep'
  )
  const tagTouched = choices.tagMode !== 'keep' && choices.tagIds.length > 0
  return scalarTouched || tagTouched
}