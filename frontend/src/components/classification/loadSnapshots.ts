'use client'

import { api } from '@/lib/api'
import type { ItemClassification } from '@/types'
import { snapshotFromItemClassification } from './classification'
import type { ClassificationSnapshot } from './classification'

/**
 * 按资产项 id 取分类快照（批量弹窗用）。
 *
 * 只为「从提示条进入、手上没有行数据」的路径准备：记录页横幅列出的是资产项，
 * 而记录列表只含「有记录」的项，补分类的入口不能依赖有没有记录。
 */

const CHUNK = 200

/** 后端返回 `{items: {id: {...}}}`（对象）或数组，两种都归一成数组。 */
export function normalizeItemClassifications(payload: unknown): ItemClassification[] {
  if (payload === null || typeof payload !== 'object') return []
  const items = (payload as { items?: unknown }).items
  if (Array.isArray(items)) return items as ItemClassification[]
  if (items !== null && typeof items === 'object') {
    return Object.values(items as Record<string, ItemClassification>)
  }
  return []
}

export async function loadClassificationSnapshots(
  itemIds: readonly number[],
): Promise<ClassificationSnapshot[]> {
  const unique = Array.from(new Set(itemIds))
  const snapshots: ClassificationSnapshot[] = []
  for (let index = 0; index < unique.length; index += CHUNK) {
    const chunk = unique.slice(index, index + CHUNK)
    const response = await api.getItemClassifications(chunk)
    for (const item of normalizeItemClassifications(response)) {
      snapshots.push(snapshotFromItemClassification(item))
    }
  }
  return snapshots
}