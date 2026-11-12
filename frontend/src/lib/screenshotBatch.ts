/**
 * 多图批次内的「未识别项」判定（纯函数，零 React 依赖，node:test 可直跑）。
 *
 * 为什么需要这一层：后端 `compute_preview` 是**单图视角**——它把「本组现有、本图未命中」
 * 的资产全部吐成 `action="keep"` 行（screenshot_service.py:433-446）。一个账户的资产
 * 常分散在多张截图里（一个 App 只会截到其中一部分），所以单图的 keep 集合在批次内会
 * 互相误伤：图 A 没截到的资产，图 B 可能正好截到了；A 的 keep 行照样把它列进「未识别项」。
 *
 * 批次语义（产品规格 .scratch/screenshot-entry/spec.md §未识别项策略）：
 * **只有本批同来源组所有截图都没命中的资产才算未识别**——否则「保持原值/清0」这类
 * 整组决策都会踩到别张图刚覆盖的值。
 *
 * 判定键 = `${sourceGroup}\u0000${资产全名}`（资产全名含来源组前缀，与后端 apply 的
 * zero_names / exclude.match 精确匹配同一口径；组内同名不同组不会互相污染）。
 */

export interface BatchPreviewRow {
  /** 后端预览动作：update / create / zero / keep */
  action: string
  /** 映射后的资产全名（含来源组前缀）；keep 行即「本图没看到」的组内资产 */
  asset_name: string | null
}

export interface BatchImage {
  /** 来源组 slug（同组成员的候选集与现有资产完全一致） */
  sourceGroup: string
  /** 未解析成功/未解析的图为 null，不参与判定 */
  preview: { rows: readonly BatchPreviewRow[] } | null
}

export interface UnrecognizedCandidate {
  img: { sourceGroup: string }
  row: { asset_name: string | null }
}

const key = (group: string, name: string): string => `${group}\u0000${name}`

/**
 * 本批「已被覆盖」的资产索引：来源组 → 资产全名集合。
 *
 * 只认 update / create（本图真的看到了这项并有值）；keep 是「本图没看到」，
 * 不构成覆盖证据；zero 是清零动作，同样不是「截图里有」。
 */
export function batchRecognizedIndex(images: readonly BatchImage[]): Map<string, Set<string>> {
  const index = new Map<string, Set<string>>()
  for (const img of images) {
    if (!img.preview) continue
    for (const row of img.preview.rows) {
      if (row.action !== 'update' && row.action !== 'create') continue
      const name = (row.asset_name ?? '').trim()
      if (!name) continue
      let set = index.get(img.sourceGroup)
      if (!set) {
        set = new Set<string>()
        index.set(img.sourceGroup, set)
      }
      set.add(key(img.sourceGroup, name))
    }
  }
  return index
}

/**
 * 从候选 keep 行里收敛出**批次级**未识别项，两道闸：
 *
 * 1. 剔除「本批同组别张图已覆盖」的资产（单图视角的误报，本 bug 的主症）
 * 2. 同一 (来源组, 资产) 只留一行——同组每张图的预览都会把组内全部未命中资产吐成
 *    keep 行，多图叠加会把同一资产重复 N 次。归谁？留**批次里第一次**出现的那张图：
 *    「全部清0」时按 owner 图下发 zero_names，天然「一次且仅一次」。
 *
 * 传入的 rows 应已是单图预览里的 keep 行（含 img / row 引用）；返回顺序保持稳定。
 */
export function filterBatchUnrecognized<T extends UnrecognizedCandidate>(
  rows: readonly T[],
  index: Map<string, Set<string>>,
): T[] {
  const seen = new Set<string>()
  const out: T[] = []
  for (const r of rows) {
    const group = r.img.sourceGroup
    const name = (r.row.asset_name ?? '').trim()
    const covered = index.get(group)
    if (name && covered?.has(key(group, name))) continue
    if (name) {
      const k = key(group, name)
      if (seen.has(k)) continue
      seen.add(k)
    }
    out.push(r)
  }
  return out
}

/** 某来源组在本批的图片张数（含解析失败图）：>1 时「全部清0」必须按批次名单下发。 */
export function batchImagesInGroup(images: readonly BatchImage[], sourceGroup: string): number {
  let n = 0
  for (const img of images) {
    if (img.sourceGroup === sourceGroup) n += 1
  }
  return n
}