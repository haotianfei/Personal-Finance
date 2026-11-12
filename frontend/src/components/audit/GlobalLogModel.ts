import type { AuditChange, AuditEntry, AuditIntegrity } from '@/lib/api'
import { EMPTY_VALUE } from '@/lib/format'

/**
 * 全局操作日志（设置 → 操作日志）的读侧规约：词表、行归一、链状态、时间与摘要。
 *
 * 为什么要「归一」而不是直接吃 `AuditEntry`：
 * 抽屉那份条目来自 `GET /asset-items/{id}/history`，后端已经排好序、给好中文动作名，
 * 字段 diff 在 `changes`、排障字段收在 `tech`；而全局列表 `GET /audit-log` 是
 * **同源不同形**——实测返回 `audit_log` 的原始列（`id` / `entity` / `action` /
 * `request_id` / `idempotency_key` / `prev_hash` 平铺在顶层，没有 `tech` 也没有
 * `changes`）。两份数据的语义相同、位置不同，所以这里按「`tech` 优先、顶层兜底」
 * 读一遍：后端将来把列表改成与抽屉同构，或维持现状，这个页面都出正确结果。
 *
 * 领域词表不在前端发明——`ENTITY_LABELS` / `ACTION_LABELS` 抄的是
 * `backend/services/audit_contract.py` 的 `ENTITY` / `ACTION`（2026-11-07 口径），
 * 只补一层显示名。**未知值原样显示**：不猜、不吞、不显示成「未知操作」。
 */

// --------------------------------------------------------------- 词表

/** 审计行 `entity` → 中文名（来源：audit_contract.py ENTITY）。 */
export const ENTITY_LABELS: Record<string, string> = {
  advisor_auto_trigger: '建议自动触发',
  advisor_decision: '建议决定',
  advisor_info_set: '建议信息集',
  advisor_settings: '建议设置',
  advisor_suggestion: '投资建议',
  allocation_target: '配置目标',
  alert: '预警规则',
  classification: '分类',
  expense: '支出',
  export: '导出',
  source_group: '来源组',
  asset_item: '资产项',
  asset_item_owner: '归属改判',
  asset: '资产记录',
  asset_snapshot: '资产快照',
  backup: '备份',
  batch: '批次',
  goal: '目标',
  goal_version: '目标版本',
  import: '导入',
  lineage_attach: '血缘挂接',
  lineage_link: '血缘关联',
  lineage_split: '血缘拆分',
  llm_provider: '模型供应商',
  parser_vocabulary: '解析词表',
  principal_change: '本金变更',
  review: '复盘',
  review_proposal: '复盘提案',
  schema: '库结构',
  screenshot_evidence: '截图凭证',
  screenshot_preview_row: '截图行',
  transfer_event: '转账',
  // 数据自述（痕迹表）的「实体」也走这里：抽屉里这些值同样出现在 tech.entity
  asset_item_names: '曾用名',
  asset_item_owner_changes: '归属变更',
  principal_changes: '本金变更',
}

/** 审计行 `action` → 中文名（来源：audit_contract.py ACTION）。 */
export const ACTION_LABELS: Record<string, string> = {
  accept: '采纳',
  activate: '启用',
  adopt: '采用',
  apply_row: '采纳行',
  archive: '归档',
  batch_create: '批量新建',
  batch_delete: '批量删除',
  change: '归属改判',
  classification_change: '分类改判',
  close: '结案',
  conclusion: '结论',
  correct: '更正',
  create: '新建',
  delete: '删除',
  dismiss: '驳回',
  execute: '批次执行',
  execution_note: '执行备注',
  expire: '过期',
  freeze: '冻结',
  generated: '生成',
  identity_decide: '身份裁决',
  import_confirm: '导入确认',
  import_seed: '导入种子',
  limit: '限流',
  lineage_link: '血缘关联',
  migration_run: '迁移执行',
  owner_backfill: '归属回填',
  owner_change: '归属改判',
  parser_vocabulary_reset: '词表重置',
  parser_vocabulary_update: '词表更新',
  principal_change: '本金变更',
  principal_set: '本金设定',
  rebuild_import: '重建导入',
  reject: '拒绝',
  rename: '改名',
  restore: '还原',
  rotate_key: '密钥轮换',
  set_primary: '设为主',
  transfer_register: '转账登记',
  unarchive: '取消归档',
  update: '更新',
  upload_db: '上传数据库',
}

/** 变更原因（来源：audit_contract.py REASON）。 */
export const REASON_LABELS: Record<string, string> = {
  correction: '更正',
  evolution: '演进',
  normalization: '归一',
  import: '导入',
  migration: '迁移',
  other: '其它',
}

/** 纠正类动作：应当说明原因（ADR-0027 D3）。缺原因不算错，但值得在界面上露出来。 */
const REASON_EXPECTED_ACTIONS = new Set([
  'correct', 'delete', 'archive', 'restore', 'owner_change', 'classification_change', 'change',
])

export const ACTOR_LABELS: Record<string, string> = { owner: '我', system: '系统' }

/** 筛选下拉的「常用」分档——其余值仍然可选，只是排在后面。 */
export const COMMON_ENTITIES = new Set([
  'asset_item', 'asset_snapshot', 'asset', 'classification', 'asset_item_owner',
  'principal_change', 'screenshot_evidence', 'batch', 'import', 'backup', 'schema', 'expense',
])
export const COMMON_ACTIONS = new Set([
  'create', 'update', 'correct', 'delete', 'archive', 'restore', 'rename', 'execute',
  'import_confirm', 'owner_change', 'classification_change', 'migration_run', 'rotate_key',
])

export function entityLabelOf(entity: string | null): string | null {
  if (!entity) return null
  return ENTITY_LABELS[entity] ?? entity
}

export function actionLabelOf(action: string | null): string | null {
  if (!action) return null
  return ACTION_LABELS[action] ?? action
}

/**
 * 动作徽标配色：只给三个高频动作上色，其余中性——与行级抽屉同一套（`AuditEntryRow`），
 * 全局列表不该是调色盘。
 */
const ACTION_TONE: Record<string, string> = {
  create: 'bg-status-good/10 text-status-good-text',
  correct: 'bg-brand-50 text-brand-700',
  delete: 'bg-status-critical-surface text-status-critical-text',
}
const ACTION_TONE_FALLBACK = 'border border-line bg-surface text-secondary'

export function actionToneOf(action: string | null): string {
  return (action && ACTION_TONE[action]) ?? ACTION_TONE_FALLBACK
}

// --------------------------------------------------------------- 行归一

/** 至少包含抽屉条目 + 列表端点原始列的形状（两边的并集）。 */
export type GlobalLogEntry =
  Omit<AuditEntry, 'tech' | 'changes' | 'note' | 'actor_label'> & {
    tech?: Record<string, string | null> | null
    changes?: AuditChange[] | null
    note?: string | null
    actor_label?: string | null
    /** 列表端点的原始列（`audit_log` 的 JSON 快照；重建行靠它拿资产数与桥文件名）。 */
    before?: unknown
    after?: unknown
    /** 以下为列表端点实测存在的顶层列。 */
    id?: number | null
    entity?: string | null
    action?: string | null
    request_id?: string | null
    idempotency_key?: string | null
    prev_hash?: string | null
    reason?: string | null
    reason_note?: string | null
  }

export interface GlobalLogRow {
  /** React key + 展开态标识：同一条记录在同一次渲染里唯一。 */
  key: string
  occurredAt: string
  entity: string | null
  action: string | null
  actor: string
  /** 「我」/「系统」；后端给了就用后端的，没给就按 actor 折算。 */
  actorLabel: string
  /** 对象（`audit_log.row_id`）：被改的那一行的 id。 */
  objectId: number | null
  /** 审计行自身的 id（列表端点才有；抽屉那份条目里 `row_id` 是审计行 id）。 */
  auditId: number | null
  evidenceRef: string | null
  requestId: string | null
  idempotencyKey: string | null
  prevHash: string | null
  note: string | null
  changes: AuditChange[]
  /**
   * 整库重建的「本代起点」信息；非空即说明这一行把日志切成两代。
   * 只有 `entity='schema'` + `action='rebuild_import'` 才有。
   */
  rebuild: GlobalLogRebuildInfo | null
}

/** 重建行的 `after` 里对界面有用的三项（其余字段含脚本路径，一律不展示）。 */
export interface GlobalLogRebuildInfo {
  /** `after.assets_imported`：这次重建导入了多少行资产；取不到为 null。 */
  assetsImported: number | null
  /** `after.source_label`：旧链的来源标签（可能缺失）。 */
  sourceLabel: string | null
  /** `after.bridge_file`：跨代桥文件名（只留最后一段，绝不显示目录）。 */
  bridgeFile: string | null
}

function text(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/** 正整数；空/非数/0 一律 null（`row_id` 为空＝没有对象，不是 0 号对象）。 */
function positiveInt(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

/** 非负整数（行数、封存滞后量可以是 0）。 */
function nonNegativeInt(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null
}

/** `before`/`after` 是 JSON 快照：对象直接用，字符串再解一次（解析失败就当没有）。 */
function snapshot(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    try {
      return snapshot(JSON.parse(value))
    } catch {
      return null
    }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** 只留文件名：桥的路径形态随部署而变，目录部分一律不显示。 */
function fileNameOf(value: unknown): string | null {
  const raw = text(value)
  if (!raw) return null
  const parts = raw.split(/[\\/]/)
  return parts[parts.length - 1] || null
}

/**
 * 「本代起点」：整库重建在新库写的唯一一条 `schema/rebuild_import`。
 * 从这条往上是当前库代，往下是上一代（旧库）。取不到 `after` 也照样认这条记录，
 * 只是少显示「导入 N 行资产」——分代这件事本身不依赖快照是否可读。
 */
function rebuildInfoOf(
  entry: GlobalLogEntry,
  entity: string | null,
  action: string | null,
): GlobalLogRebuildInfo | null {
  if (entity !== 'schema' || action !== 'rebuild_import') return null
  const after = snapshot(entry.after)
  return {
    assetsImported: nonNegativeInt(after?.assets_imported),
    sourceLabel: text(after?.source_label),
    bridgeFile: fileNameOf(after?.bridge_file),
  }
}

export function toGlobalLogRow(entry: GlobalLogEntry, index: number): GlobalLogRow {
  const tech = entry.tech ?? {}
  const entity = text(tech.entity) ?? text(entry.entity)
  const action = text(tech.action) ?? text(entry.action)
  const actor = text(entry.actor) ?? ''
  const occurredAt = entry.occurred_at ?? ''
  const objectId = positiveInt(entry.row_id)
  const auditId = positiveInt(entry.id)
  return {
    key: [occurredAt, entity ?? '', action ?? '', objectId ?? '', auditId ?? '', index].join('|'),
    occurredAt,
    entity,
    action,
    actor,
    actorLabel: text(entry.actor_label) ?? ACTOR_LABELS[actor] ?? (actor || EMPTY_VALUE),
    objectId,
    auditId,
    evidenceRef: entry.evidence_ref === null || entry.evidence_ref === undefined
      ? null
      : String(entry.evidence_ref),
    requestId: text(tech.request_id) ?? text(entry.request_id),
    idempotencyKey: text(tech.idempotency_key) ?? text(entry.idempotency_key),
    prevHash: text(tech.prev_hash) ?? text(entry.prev_hash),
    note: text(entry.note) ?? text(entry.reason_note) ?? text(entry.reason),
    changes: Array.isArray(entry.changes) ? entry.changes : [],
    rebuild: rebuildInfoOf(entry, entity, action),
  }
}

// --------------------------------------------------------------- 分代分隔条

/** 这一行是否把日志切成两代（`schema/rebuild_import`）。 */
export function isGenerationStart(row: GlobalLogRow): boolean {
  return row.rebuild !== null
}

/** 右侧小字：「整库重建 · 导入 N 行资产」；资产数取不到时只留「整库重建」。 */
export function rebuildSummaryOf(row: GlobalLogRow): string {
  const count = row.rebuild?.assetsImported ?? null
  return count === null ? '整库重建' : `整库重建 · 导入 ${count} 行资产`
}

/**
 * 分隔条的完整说明（`title` / `aria-label`）：上方属于当前库代，下方属于上一代。
 * 只陈述位置与重建事实，不评价数据对错；来源优先用标签，没有就退回桥文件名。
 */
export function rebuildDescriptionOf(row: GlobalLogRow): string {
  const time = formatLogTime(row.occurredAt)
  const source = row.rebuild?.sourceLabel ?? row.rebuild?.bridgeFile
  const facts = source ? `重建于 ${time.label}，来源 ${source}` : `重建于 ${time.label}`
  return `此上方属于当前库代，下方属于上一代（${facts}）`
}

// --------------------------------------------------------------- 摘要

export interface GlobalLogSummary {
  text: string
  /** true＝这条没有可用摘要（渲染成更淡的次要字）。 */
  muted: boolean
  title?: string
}

/**
 * 摘要只用「不泄露内容」的素材：原因、变更字段名。
 *
 * 刻意**不**渲染 before/after 的值：`schema/migration_run` 的 `after.backup` 是绝对路径，
 * 而值里的金额/账号属于业务数据本身。要看逐字段旧→新，走行级抽屉或展开技术细节。
 */
export function summaryOf(row: GlobalLogRow): GlobalLogSummary {
  if (row.note) {
    const reason = REASON_LABELS[row.note]
    return reason
      ? { text: `原因：${reason}`, muted: false, title: row.note }
      : { text: `备注：${row.note}`, muted: false, title: row.note }
  }

  if (row.changes.length > 0) {
    // label 由后端给（`FIELD_LABELS`），前端不自造；去重后最多列两个
    const labels = Array.from(new Set(row.changes.map((change) => change.label || change.field)))
    const head = labels.slice(0, 2).join('、')
    return {
      text: labels.length > 2 ? `${head} 等 ${labels.length} 个字段` : head,
      muted: false,
    }
  }

  if (row.action && REASON_EXPECTED_ACTIONS.has(row.action)) {
    return {
      text: '原因未记录',
      muted: true,
      title: '纠正类动作建议说明原因；这条记录没有原因字段（存量补充在后续操作里）。',
    }
  }

  return { text: EMPTY_VALUE, muted: true }
}

// --------------------------------------------------------------- 资产项跳转

/** 与资产项相关的实体（票 02 口径）。 */
const ASSET_ITEM_ENTITIES = new Set([
  'asset_item', 'asset_snapshot', 'asset', 'classification', 'asset_item_owner', 'principal_change',
])

/**
 * 只有这两个实体的 `row_id` **就是**资产项 id（见 `asset_history_service` 的检索条件：
 * `asset_item`/`classification` 直接比 `row_id == item_id`；快照、归属改判、本金变更
 * 的 `row_id` 是被改那一行自己的 id）。所以在其余实体上「按对象 id 打开」会打开错的资产项，
 * 一律禁用按钮并说明原因。
 */
const ITEM_ID_ENTITIES = new Set(['asset_item', 'classification'])

export function isAssetRelatedEntity(entity: string | null): boolean {
  return entity !== null && ASSET_ITEM_ENTITIES.has(entity)
}

/** 能直接拿来打开 `ItemHistoryDrawer` 的资产项 id；拿不到就是 null。 */
export function itemIdForHistory(row: GlobalLogRow): number | null {
  if (!row.entity || !ITEM_ID_ENTITIES.has(row.entity)) return null
  return row.objectId
}

/** 按钮禁用时的原因（挂在 title 上，也读给读屏）。 */
export function historyDisabledReason(row: GlobalLogRow): string {
  if (row.objectId === null) {
    return '这条记录没有对象 id（如库结构迁移），无法定位到资产项。'
  }
  if (row.entity === 'asset_snapshot' || row.entity === 'asset') {
    return `对象 id #${row.objectId} 是资产快照 id，不是资产项 id；到记录页对应资产项上打开操作历史。`
  }
  return `对象 id #${row.objectId} 是改判行自己的 id，不是资产项 id；到记录页对应资产项上打开操作历史。`
}

// --------------------------------------------------------------- 链状态

export type IntegrityStatus = 'match' | 'db_newer' | 'db_older' | 'diverged' | 'unknown'

const INTEGRITY_STATUSES: readonly IntegrityStatus[] = [
  'match', 'db_newer', 'db_older', 'diverged', 'unknown',
]

export interface IntegrityMeta {
  /** 徽标文案：五值各一句，不加形容词。 */
  label: string
  /** 状态解释：说清「这个结论是怎么来的、要不要动作」。 */
  hint: string
  /** 外层容器配色。 */
  tone: string
  dot: string
}

export const INTEGRITY_META: Record<IntegrityStatus, IntegrityMeta> = {
  match: {
    label: '一致',
    hint: '与备份清单里的库外锚一致。对账只能发现不一致，不做拦截。',
    tone: 'border-status-good/30 bg-status-good/10 text-status-good-text',
    dot: 'bg-status-good',
  },
  db_newer: {
    label: '库比锚新',
    hint: '库里比备份里的锚多了记录，通常是锚还没跟上；下次备份或日封存后重新核对。',
    tone: 'border-action-primary/25 bg-brand-50 text-brand-700',
    dot: 'bg-action-primary',
  },
  db_older: {
    label: '库比锚旧',
    hint: '当前库比备份里的锚更旧：可能被换成了旧库，先核对恢复记录与备份文件。',
    tone: 'border-status-warn/40 bg-status-warn-surface text-status-warn-text',
    dot: 'bg-status-warn',
  },
  diverged: {
    label: '分叉',
    hint: '行数与锚一致，链头却不同：两段记录接不上同一条链，按失配对待。',
    tone: 'border-status-critical/30 bg-status-critical-surface text-status-critical-text',
    dot: 'bg-status-critical',
  },
  unknown: {
    label: '无锚可比',
    hint: '还没有可比的库外锚（没做过备份，或锚缺失）：既不等于一致，也不等于有问题。',
    tone: 'border-line bg-subtle text-secondary',
    dot: 'bg-status-unknown',
  },
}

interface IntegrityRawShape extends AuditIntegrity {
  worst_status?: string | null
  head?: { last_hash?: string | null; row_count?: number | null; max_id?: number | null } | null
}

export interface GlobalLogIntegrity {
  status: IntegrityStatus
  /** 链头哈希（完整值；界面只露前 8 位）。 */
  head: string | null
  maxId: number | null
  rows: number | null
  /** 尚未日封存的行数。 */
  sealLag: number | null
}

function normalizeStatus(value: unknown): IntegrityStatus {
  return INTEGRITY_STATUSES.find((status) => status === value) ?? 'unknown'
}

/**
 * `/audit-log/integrity` 的读侧兼容：契约里叫 `status`/`audit_head`/`audit_rows`，
 * 实测返回 `worst_status`/`head.last_hash`/`head.row_count`。两套都认。
 */
export function readIntegrity(raw: AuditIntegrity | undefined): GlobalLogIntegrity | null {
  if (!raw) return null
  const loose = raw as IntegrityRawShape
  const seals = loose.seals
  return {
    status: normalizeStatus(loose.status ?? loose.worst_status),
    head: text(loose.audit_head) ?? text(loose.head?.last_hash),
    maxId: nonNegativeInt(seals?.max_id) ?? nonNegativeInt(loose.head?.max_id),
    rows: nonNegativeInt(loose.audit_rows) ?? nonNegativeInt(loose.head?.row_count),
    sealLag: nonNegativeInt(seals?.lag),
  }
}

// --------------------------------------------------------------- 时间

function parseLocalStamp(value: string): Date | null {
  if (!value) return null
  // 后端时间串是本地时间不带时区（`2026-11-07T12:20:45.896204`）：裁到毫秒再解析
  const trimmed = value.replace(/(\.\d{3})\d+$/, '$1')
  const date = new Date(trimmed)
  return Number.isNaN(date.getTime()) ? null : date
}

const pad2 = (value: number) => String(value).padStart(2, '0')

/** 全局列表的时间口径：`YYYY-MM-DD HH:mm:ss`（本地时区），`title` 放后端原串。 */
export function formatLogTime(value: string): { label: string; title: string } {
  const date = parseLocalStamp(value)
  if (!date) return { label: value || EMPTY_VALUE, title: value }
  const label = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
    + ` ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
  return { label, title: value }
}

/** 哈希只露前 8 位（完整值挂在 title 里）。 */
export const HASH_PREVIEW = 8

export function hashPreview(value: string | null): string | null {
  if (!value) return null
  return value.length > HASH_PREVIEW ? value.slice(0, HASH_PREVIEW) : value
}