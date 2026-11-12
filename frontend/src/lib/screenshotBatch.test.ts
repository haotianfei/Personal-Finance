/**
 * 多图批次「未识别项」回归测试（node:test，无第三方依赖）。
 *
 * 运行：cd frontend && node --test src/lib/screenshotBatch.test.ts
 *
 * 复现的线上症状（2026-10-30）：同一来源组上传 2 张截图，两张各命中 4 项，
 * 「未识别项」却列出 10 条——其中 8 条在本批的另一张截图里已经识别到了。
 * 根因：后端 preview 是单图视角（compute_preview 把「本组现有、本图未命中」
 * 全吐成 keep 行），前端按图聚合时未按「同来源组多图并集」反向过滤。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  batchRecognizedIndex,
  filterBatchUnrecognized,
  type BatchImage,
} from './screenshotBatch.ts'

const G = 'zhaoshang' // 来源组 slug
const P = '示例证券-'

type Row = { action: string; asset_name: string | null }

const asset = (name: string): string => P + name

// 系统内该来源组的 10 项资产
const EXISTING = [
  '宽基红利ETF甲',
  '行业半导体ETF乙',
  '红利低波ETF丙',
  '宽基标普ETF丁',
  '中证红利ETF戊',
  '红利ETF己',
  '示例电力',
  '跨境红利ETF庚',
  '示例股票',
  '示例现金',
].map(asset)

// 图 1 命中（update）这 4 项
const HIT_A = ['宽基红利ETF甲', '行业半导体ETF乙', '红利低波ETF丙', '宽基标普ETF丁'].map(asset)
// 图 2 命中（update）这 4 项
const HIT_B = ['中证红利ETF戊', '红利ETF己', '示例电力', '跨境红利ETF庚'].map(asset)

const previewOf = (hits: string[]): { rows: Row[] } => ({
  rows: [
    ...hits.map((n) => ({ action: 'update', asset_name: n })),
    ...EXISTING.filter((n) => !hits.includes(n)).map((n) => ({ action: 'keep', asset_name: n })),
  ],
})

const imgA: BatchImage = { sourceGroup: G, preview: previewOf(HIT_A) }
const imgB: BatchImage = { sourceGroup: G, preview: previewOf(HIT_B) }

/** 模拟前端聚合：每图 keep 行 → 候选未识别行（img + row 二元组） */
const keepCandidates = (imgs: BatchImage[]) =>
  imgs.flatMap((img) =>
    (img.preview?.rows ?? [])
      .filter((r) => r.action === 'keep')
      .map((row) => ({ img, row })),
  )

const names = (rows: { row: { asset_name: string | null } }[]) =>
  rows.map((r) => r.row.asset_name).sort()

test('多图并集：另一张截图已命中的资产不算未识别', () => {
  const imgs = [imgA, imgB]
  const kept = filterBatchUnrecognized(keepCandidates(imgs), batchRecognizedIndex(imgs))

  assert.deepEqual(names(kept), [asset('示例现金'), asset('示例股票')])
  for (const hit of [...HIT_A, ...HIT_B]) {
    assert.ok(!names(kept).includes(hit), `${hit} 已被本批识别，不应出现在未识别项`)
  }
})

test('单图批次行为不变：本图未命中的全部保留为未识别项', () => {
  const imgs = [imgA]
  const kept = filterBatchUnrecognized(keepCandidates(imgs), batchRecognizedIndex(imgs))

  assert.equal(kept.length, EXISTING.length - HIT_A.length)
})

test('不同来源组的命中互不影响', () => {
  const other: BatchImage = {
    sourceGroup: 'bantuou',
    preview: { rows: [{ action: 'update', asset_name: '示例银行-示例股票' }] },
  }
  // 注意：other 组的 asset_name 与 G 组同名不同组，不应命中 G 组
  const imgs = [imgA, imgB, other]
  const kept = filterBatchUnrecognized(keepCandidates(imgs), batchRecognizedIndex(imgs))

  assert.deepEqual(names(kept), [asset('示例现金'), asset('示例股票')])
})

test('新增（create）行同样算作「本批已覆盖」', () => {
  const imgs: BatchImage[] = [
    imgA,
    imgB,
    { sourceGroup: G, preview: { rows: [{ action: 'create', asset_name: asset('示例股票') }] } },
  ]
  const kept = filterBatchUnrecognized(keepCandidates(imgs), batchRecognizedIndex(imgs))

  assert.deepEqual(names(kept), [asset('示例现金')])
})

test('预览缺失（失败图）不参与判定，也不误剔除', () => {
  const imgs: BatchImage[] = [imgA, { sourceGroup: G, preview: null }]
  const kept = filterBatchUnrecognized(keepCandidates(imgs), batchRecognizedIndex(imgs))

  assert.equal(kept.length, EXISTING.length - HIT_A.length)
})