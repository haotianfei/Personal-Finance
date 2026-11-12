/**
 * 树形呈现（spec `.scratch/review-tree-visual`）的纯函数回归。
 *
 * 运行：cd frontend && bun test src/lib/screenshotAggregate.test.ts
 *
 * 只测「换一处实现就会漂移」的部分：
 * 1. `orderAsTree` 的 `isLast` / `ancestorHasNext`（末位祖先的不产生线的车道）；
 * 2. 归父护栏放开后只剩「自己 / 后代」两条；
 * 3. 汇总来源状态机（自动置位只撤 parent、有子项时手取消被阻断）；
 * 4. 差额标记四态与 Decimal 差额文案。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import Decimal from 'decimal.js'

import {
  aggregateDiffBadge,
  buildParentCandidates,
  checkDrop,
  computeReconciliations,
  indentPad,
  LANE_PX,
  orderAsTree,
  withChildAdded,
  withLastChildRemoved,
  withManualToggle,
  type AggregateFlag,
  type MeterRow,
  type ParentRef,
  type ReconcileChildInput,
  type ReconcileParentInput,
  type TreeRowLike,
} from './screenshotAggregate.ts'

type Row = TreeRowLike & { name: string; amount: string; aggregate: boolean }

const row = (key: string, parent: ParentRef | null = null, aggregate = false): Row => ({
  key,
  parent,
  name: key,
  amount: '0.00',
  aggregate,
})

// ---------------------------------------------------------------- 缩进单点

test('indentPad：内容左缘 = (level+1)*16，最后一条车道留给连接符', () => {
  assert.equal(LANE_PX, 16)
  assert.equal(indentPad(0), 16)
  assert.equal(indentPad(1), 32)
  assert.equal(indentPad(3), 64)
})

// ---------------------------------------------------------------- 树序与末位

test('orderAsTree：末位根组下面的车道是空白（ancestorHasNext[0] === false）', () => {
  const rows = [
    row('A'),
    row('a1', { kind: 'row', key: 'A' }),
    row('a2', { kind: 'row', key: 'A' }),
    row('B'),
    row('b1', { kind: 'row', key: 'B' }),
  ]
  const tree = orderAsTree(rows)
  const byKey = new Map(tree.map((t) => [t.row.key, t]))

  assert.deepEqual(
    tree.map((t) => t.row.key),
    ['A', 'a1', 'a2', 'B', 'b1'],
  )
  // 根级：A 后面还有 B → 非末位；B 是最后一个根 → 末位
  assert.equal(byKey.get('A')?.isLast, false)
  assert.equal(byKey.get('B')?.isLast, true)
  // a2 是 A 的最后一个子项
  assert.equal(byKey.get('a1')?.isLast, false)
  assert.equal(byKey.get('a2')?.isLast, true)
  // 车道：a1/a2 的 lane0 要继续（A 后面还有兄弟）；b1 的 lane0 是空白
  assert.deepEqual(byKey.get('a1')?.ancestorHasNext, [true])
  assert.deepEqual(byKey.get('b1')?.ancestorHasNext, [false])
  // 根行没有祖先车道
  assert.deepEqual(byKey.get('A')?.ancestorHasNext, [])
})

test('orderAsTree：isLast 看穿子树（父行之后还有自己的子行才算非末位）', () => {
  const rows = [
    row('A'),
    row('a1', { kind: 'row', key: 'A' }),
    row('a3', { kind: 'row', key: 'a1' }),
    row('B'),
  ]
  const tree = orderAsTree(rows)
  const byKey = new Map(tree.map((t) => [t.row.key, t]))
  // A 的「下一个同级」是 B（跨过整棵子树），故 A 非末位、B 末位
  assert.equal(byKey.get('A')?.isLast, false)
  assert.equal(byKey.get('B')?.isLast, true)
  // 深度 2：两条祖先车道（A 后面有兄弟 = true；a1 是 A 的末位子项 = false）
  assert.equal(byKey.get('a3')?.level, 2)
  assert.deepEqual(byKey.get('a3')?.ancestorHasNext, [true, false])
})

test('orderAsTree：childCount 与 parentOutside 语义不变', () => {
  const rows = [
    row('A'),
    row('a1', { kind: 'row', key: 'A' }),
    row('ghost', { kind: 'row', key: 'NOT-IN-VIEW' }),
    row('itemChild', { kind: 'item', itemId: 7, name: '既有项' }),
  ]
  const byKey = new Map(orderAsTree(rows).map((t) => [t.row.key, t]))
  assert.equal(byKey.get('A')?.childCount, 1)
  assert.equal(byKey.get('ghost')?.parentOutside, true)
  assert.equal(byKey.get('itemChild')?.parentOutside, true)
  assert.equal(byKey.get('itemChild')?.level, 0)
})

// ---------------------------------------------------------------- 归父护栏

test('checkDrop：目标行没勾汇总也允许（归父即自动置汇总）', () => {
  const rows = [row('child'), row('target', null, false)]
  assert.deepEqual(checkDrop(rows, 'child', 'target'), { ok: true })
})

test('checkDrop：自己 / 后代 / 不存在的目标仍然拦住', () => {
  const rows = [row('p'), row('c', { kind: 'row', key: 'p' }), row('g', { kind: 'row', key: 'c' })]
  assert.deepEqual(checkDrop(rows, 'p', 'p'), { ok: false, reason: 'self' })
  assert.deepEqual(checkDrop(rows, 'p', 'g'), { ok: false, reason: 'descendant' })
  assert.deepEqual(checkDrop(rows, 'c', 'nope'), { ok: false, reason: 'unknown-target' })
})

test('buildParentCandidates：候选里出现未勾汇总的行，且排除自身与后代', () => {
  const rows: MeterRow[] = [
    { key: 'p', parent: null, name: '父候选', amount: '100.00', aggregate: false },
    { key: 'c', parent: { kind: 'row', key: 'p' }, name: '子', amount: '40.00', aggregate: false },
    { key: 'g', parent: { kind: 'row', key: 'c' }, name: '孙', amount: '10.00', aggregate: false },
  ]
  const ids = buildParentCandidates(rows, 'c', [], () => 1).map((c) => c.id)
  assert.deepEqual(ids, ['row:p'])
})

// ---------------------------------------------------------------- 汇总来源状态机

const flag = (aggregate: boolean, aggregateSource: AggregateFlag['aggregateSource']): AggregateFlag => ({
  aggregate,
  aggregateSource,
})

test('withChildAdded：未勾 → parent；已手勾 → 保持 manual 不降级', () => {
  assert.deepEqual(withChildAdded(flag(false, null)), { aggregate: true, aggregateSource: 'parent' })
  assert.deepEqual(withChildAdded(flag(true, 'manual')), { aggregate: true, aggregateSource: 'manual' })
  assert.deepEqual(withChildAdded(flag(true, 'rule')), { aggregate: true, aggregateSource: 'rule' })
})

test('withLastChildRemoved：只撤 parent，manual / rule 不动', () => {
  assert.deepEqual(withLastChildRemoved(flag(true, 'parent')), { aggregate: false, aggregateSource: null })
  assert.deepEqual(withLastChildRemoved(flag(true, 'manual')), { aggregate: true, aggregateSource: 'manual' })
  assert.deepEqual(withLastChildRemoved(flag(true, 'rule')), { aggregate: true, aggregateSource: 'rule' })
})

test('withManualToggle：有子项时取消被阻断，无子项才放行', () => {
  const blocked = withManualToggle(flag(true, 'parent'), 2)
  assert.equal(blocked.blocked, true)
  assert.deepEqual(blocked.flag, { aggregate: true, aggregateSource: 'parent' })

  const ok = withManualToggle(flag(true, 'parent'), 0)
  assert.equal(ok.blocked, false)
  assert.deepEqual(ok.flag, { aggregate: false, aggregateSource: null })

  const on = withManualToggle(flag(false, null), 0)
  assert.equal(on.blocked, false)
  assert.deepEqual(on.flag, { aggregate: true, aggregateSource: 'manual' })
})

// ---------------------------------------------------------------- 差额标记

const parent = (amount: string | null): ReconcileParentInput => ({
  id: 'row:p',
  kind: 'row',
  name: '父',
  imageOrdinal: 1,
  amount,
  itemId: 1,
  evidenceId: 1,
  rowIndex: 0,
})
const kids = (amounts: string[]): ReconcileChildInput[] =>
  amounts.map((amount, i) => ({
    parentId: 'row:p',
    key: `c${i}`,
    name: `子${i}`,
    amount,
    evidenceId: 1,
    rowIndex: i + 1,
  }))

test('aggregateDiffBadge：四态文案与 data 取值', () => {
  const matched = aggregateDiffBadge(computeReconciliations([parent('400.00')], kids(['400.00']), {})[0])
  assert.equal(matched.state, 'matched')
  assert.equal(matched.label, '已对上')

  const unknown = aggregateDiffBadge(computeReconciliations([parent(null)], kids(['400.00']), {})[0])
  assert.equal(unknown.state, 'unknown')
  assert.equal(unknown.label, '子项待确认')

  const mismatch = aggregateDiffBadge(computeReconciliations([parent('1000.00')], kids(['400.00']), {})[0])
  assert.equal(mismatch.state, 'mismatch')
  assert.equal(mismatch.label, '差 +¥600.00')
  assert.equal(mismatch.detail, '父 +¥1,000.00 − 子 +¥400.00 = +¥600.00')

  const accepted = aggregateDiffBadge(
    computeReconciliations([parent('1000.00')], kids(['400.00']), {
      'row:p': { diff: '600.00', reason: '口径不同', at: 1 },
    })[0],
  )
  assert.equal(accepted.state, 'accepted')
  assert.equal(accepted.label, '已接受 +¥600.00')
  assert.match(accepted.detail, /理由：口径不同/)
})

test('aggregateDiffBadge：stale（接受后差额又变）并入 mismatch 并注明作废', () => {
  const stale = aggregateDiffBadge(
    computeReconciliations([parent('1000.00')], kids(['400.00']), {
      'row:p': { diff: '1.00', reason: '旧结论', at: 1 },
    })[0],
  )
  assert.equal(stale.state, 'mismatch')
  assert.match(stale.label, /接受已作废/)
})

test('aggregateDiffBadge：Decimal 逐分相等（0.01 也是差）', () => {
  const cents = aggregateDiffBadge(computeReconciliations([parent('0.30')], kids(['0.10', '0.20']), {})[0])
  assert.equal(cents.state, 'matched')
  const off = aggregateDiffBadge(computeReconciliations([parent('0.30')], kids(['0.11', '0.20']), {})[0])
  assert.equal(off.state, 'mismatch')
  assert.equal(off.label, '差 −¥0.01')
  // 证明没有走 float：0.1+0.2 在 float 下是 0.30000000000000004
  assert.equal(new Decimal('0.10').plus('0.20').toFixed(2), '0.30')
})