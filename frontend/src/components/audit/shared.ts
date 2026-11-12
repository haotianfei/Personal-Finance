import type { AuditEntry } from '@/lib/api'

/**
 * 取证视图（票 02）的三个小工具：时间分级、凭证编号解析、条目稳定键。
 *
 * 放这里而不是 `lib/format.ts`：`formatDateTime` 是「年月日时分」的定长口径，
 * 时间线要的是「今天 / 昨天 / 具体日期」的分级，只在操作历史里用。
 */

/** 后端时间串是**本地时间不带时区**（`2026-11-07T12:20:45.896204`）。 */
function parseLocalStamp(value: string): Date | null {
  if (!value) return null
  // 部分引擎只认 3 位小数秒：先裁到毫秒再解析，与 formatRelativeTime 同口径
  const trimmed = value.replace(/(\.\d{3})\d+$/, '$1')
  const date = new Date(trimmed)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

export interface EntryTimeLabel {
  /** 「今天 12:20」/「昨天 09:05」/「11月5日 18:40」/「2025年11月5日 18:40」。 */
  label: string
  /** 完整时刻，挂在 `title` 上（鼠标悬停可核对）。 */
  title: string
}

/**
 * 时间分级：今天 / 昨天 / 同年具体日期 / 跨年具体日期。
 * 解析失败原样透出（不猜、不显示 Invalid Date）。
 */
export function formatEntryTime(value: string, now: Date = new Date()): EntryTimeLabel {
  const date = parseLocalStamp(value)
  if (!date) return { label: value || '—', title: value }

  const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  const title = `${date.getFullYear()}/${pad2(date.getMonth() + 1)}/${pad2(date.getDate())} ${clock}`
  const dayText = `${date.getMonth() + 1}月${date.getDate()}日`

  if (isSameDay(date, now)) return { label: `今天 ${clock}`, title }

  const yesterday = new Date(now.getTime())
  yesterday.setDate(yesterday.getDate() - 1)
  if (isSameDay(date, yesterday)) return { label: `昨天 ${clock}`, title }

  if (date.getFullYear() === now.getFullYear()) return { label: `${dayText} ${clock}`, title }
  return { label: `${date.getFullYear()}年${dayText} ${clock}`, title }
}

/**
 * 条目稳定键：同一事件（来源 + 行号 + 时刻 + 动作）在多页里只应出现一次。
 * 用途有二：跨页去重；React 列表 key。
 */
export function entryKeyOf(entry: AuditEntry): string {
  return [entry.source, entry.row_id, entry.occurred_at, entry.kind].join('|')
}

/**
 * `evidence_ref` → 凭证 id。
 *
 * 后端列是 `audit_log.evidence_ref`（INTEGER，FK → `screenshot_evidence.id`），
 * 契约类型写成 string（票里的示例形态是 `ev-20261030-001`）。这里**只认纯数字**
 * （含运行时实际是 JSON number 的情况）：`ev-…` 这类展示编号没有反解端点，
 * 猜一个 id 出去只会在取证时打开错的凭证——所以认不出就返回 null，调用方把
 * 编号原样显示成文本、不给「查看凭证」按钮。
 */
export function evidenceIdOf(ref: string | number | null | undefined): number | null {
  if (ref === null || ref === undefined) return null
  const raw = String(ref).trim()
  if (!/^\d+$/.test(raw)) return null
  const id = Number(raw)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}