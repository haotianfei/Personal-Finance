/**
 * `formatRelativeTime` 的真值表回归（node:test，零第三方依赖）。
 *
 * 运行：cd frontend && node --test src/lib/format.test.ts
 *
 * 时间一律用**本地时间**构造，断言只看差值分桶，不依赖运行时所在时区：
 * 后端给的是不带时区的本地时间串（`isoformat()`），前端按本地解析，两边同基准。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { formatRelativeTime } from './format.ts'

const NOW = new Date(2026, 10, 6, 10, 0, 0).getTime() // 本地 2026-11-06 10:00:00
const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

const at = (offsetMs: number) => NOW - offsetMs

test('空值与非法输入：不冒充「刚刚」', () => {
  assert.equal(formatRelativeTime(null, NOW), '—')
  assert.equal(formatRelativeTime(undefined, NOW), '—')
  assert.equal(formatRelativeTime('', NOW), '—')
  // 非法串原样透出（与 formatDateTime 同口径），绝不渲染成「刚刚」
  assert.equal(formatRelativeTime('not-a-date', NOW), 'not-a-date')
})

test('分桶边界：刚刚 / 分钟 / 小时 / 天', () => {
  assert.equal(formatRelativeTime(at(0), NOW), '刚刚')
  assert.equal(formatRelativeTime(at(59_999), NOW), '刚刚')
  assert.equal(formatRelativeTime(at(MINUTE), NOW), '1 分钟前')
  assert.equal(formatRelativeTime(at(59 * MINUTE + 59_000), NOW), '59 分钟前')
  assert.equal(formatRelativeTime(at(HOUR), NOW), '1 小时前')
  assert.equal(formatRelativeTime(at(DAY - 1000), NOW), '23 小时前')
  assert.equal(formatRelativeTime(at(DAY), NOW), '1 天前')
  assert.equal(formatRelativeTime(at(3 * DAY + 5 * HOUR), NOW), '3 天前')
})

test('跨天按差值算，不按日历天', () => {
  // 昨晚 23:58 → 今天 10:00 是「10 小时前」，不是「昨天 1 天前」
  assert.equal(formatRelativeTime(new Date(2026, 10, 5, 23, 58, 0).getTime(), NOW), '10 小时前')
  // 昨天 09:00 → 恰好 25 小时 → 1 天前
  assert.equal(formatRelativeTime(new Date(2026, 10, 5, 9, 0, 0).getTime(), NOW), '1 天前')
})

test('未来时间（时钟偏差）归「刚刚」，不出现负数', () => {
  assert.equal(formatRelativeTime(at(-5 * MINUTE), NOW), '刚刚')
  assert.equal(formatRelativeTime(at(-3 * DAY), NOW), '刚刚')
})

test('ISO 字符串：带 6 位微秒也能解析', () => {
  // 后端 isoformat() 形如 2026-11-06T09:55:00.123456（无时区，本地时间）
  assert.equal(formatRelativeTime('2026-11-06T09:55:00.123456', NOW), '4 分钟前')
  assert.equal(formatRelativeTime('2026-11-06T09:00:00', NOW), '1 小时前')
})