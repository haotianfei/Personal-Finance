'use client'

import { useState, useMemo, useCallback, useEffect, useRef, Fragment, Suspense, type ReactNode, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Modal, message, Tooltip } from 'antd'
import { api, fetchArchivedItemHints, newIdemKey, ApiError } from '@/lib/api'
import { formatCurrency, formatDate, formatDateTime, formatFileSize, formatSignedCurrency, formatValue } from '@/lib/format'
import { batchImagesInGroup, batchRecognizedIndex, filterBatchUnrecognized } from '@/lib/screenshotBatch'
import { buildItemAmounts, rowBaseline, rowDiffAmount, sumSystemBaseline } from '@/lib/screenshotTotals'
import {
  AGGREGATE_SOURCE_LABEL,
  aggregateDiffBadge,
  buildParentCandidates,
  checkDrop,
  computeReconciliations,
  describeBatchError,
  DROP_HINT,
  indentPad,
  LANE_CENTER,
  LANE_PX,
  orderAsTree,
  rowKeyOf,
  splitRowKey,
  toReconcilePayloads,
  withChildAdded,
  withLastChildRemoved,
  withManualToggle,
  type AcceptedMismatch,
  type AggregateSource,
  type ExistingItemOption,
  type MeterRow,
  type ParentCandidate,
  type ParentRef,
  type ReconcileChildInput,
  type ReconcileItem,
  type ReconcileParentInput,
  type RowKey,
} from '@/lib/screenshotAggregate'
import { HintToggle } from '@/components/ui/HintToggle'
import { ImageViewer, type ViewableImage } from '@/components/screenshot/ImageViewer'
import { ParentPicker } from '@/components/screenshot/ParentPicker'
import { ReconcilePanel } from '@/components/screenshot/ReconcilePanel'
import { Card } from '@/components/ui/Card'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import type {
  ScreenshotPreviewResponse,
  ScreenshotPreviewRow,
  ScreenshotApplyResult,
  ScreenshotBatchFinalizeBody,
  SourceGroup,
  LlmProvider,
  VerifyAiOnlyItem,
  VerifyRow,
  VerifyVerdict,
  PreviewJobStatus,
  PreviewJobLine,
  SupplementDraft,
  SupplementResolveResult,
  SupplementSide,
  IdentityDecisionPayload,
  IdentityGuessCandidate,
  AssetItemInfo,
} from '@/types'
import Decimal from 'decimal.js'
import {
  Camera, Upload, Loader2, AlertTriangle, CheckCircle2, X, Copy,
  FileImage, RefreshCw, ShieldCheck, Clock, Plus, Layers, Cpu,
  FileText, Save, ChevronLeft, Trash2, ListChecks, ListX, Info, Lightbulb, Bot,
  CalendarDays, Settings2, ChevronDown, GitMerge, Link2, Search,
  GripVertical, ChevronRight,
} from 'lucide-react'

// --- Helpers ---
function toDecimal(v: string | number | null | undefined): Decimal {
  if (v === null || v === undefined || v === '') return new Decimal(0)
  try {
    return new Decimal(v)
  } catch {
    return new Decimal(0)
  }
}

// 行级差异＝截图金额 − 基线（基线口径见 `lib/screenshotTotals.ts`：绑定行取目标项现值）
function diffColor(diffStr: string): string {
  const d = toDecimal(diffStr)
  if (d.isZero()) return 'text-muted'
  if (d.gt(0)) return 'text-emerald-600'
  return 'text-red-600'
}

// 汇总行行内差额标记（票 06）：金额格下方一枚小徽章，四态由 `aggregateDiffBadge` 决定。
// 只显示「差额」这一个数；展开才是「父 ¥A − 子 ¥S」算式与「去核对」——行内不复制接受/撤销
const AGGREGATE_DIFF_TONE: Record<'ok' | 'accepted' | 'alert' | 'muted', string> = {
  ok: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  accepted: 'border-amber-200 bg-amber-50 text-amber-700',
  alert: 'border-rose-300 bg-rose-50 text-rose-700',
  muted: 'border-slate-200 bg-slate-50 text-slate-500',
}

function AggregateDiffChip({ item, onGo }: { item: ReconcileItem; onGo: () => void }) {
  const [open, setOpen] = useState(false)
  const badge = aggregateDiffBadge(item)
  return (
    <div className="mt-1 flex flex-col items-end gap-0.5">
      <button
        type="button"
        data-aggregate-diff={badge.state}
        aria-expanded={open}
        title={`与下一级子项合计的差额：${badge.detail}`}
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center gap-0.5 whitespace-nowrap rounded-pill border px-1.5 text-meta ${AGGREGATE_DIFF_TONE[badge.tone]}`}
      >
        <GitMerge size={10} />
        {badge.label}
      </button>
      {open && (
        <div className="max-w-[13rem] text-right text-meta leading-relaxed text-slate-500">
          <p className="font-code">{badge.detail}</p>
          <button type="button" onClick={onGo} className="mt-0.5 text-brand-600 underline-offset-2 hover:underline">
            去核对
          </button>
        </div>
      )}
    </div>
  )
}

// 本地「今天」= YYYY-MM-DD（数据日期兜底 / 批量设置用；用本地时区，不用 toISOString 防跨日偏移）
function todayStr(): string {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

// --- 校正规则辅助（反哺：把人工修正固化为来源组规则） ---
// 改名规则条件：仅在用户真正改动了显示名时才可存（基准 = 行初始显示名 asset_name ?? source_name）
function renameRuleEligible(r: ReviewRow): boolean {
  const baseline = (r.row.asset_name ?? r.row.source_name ?? '').trim()
  return r.nameEditable && !!r.row.source_name && r.edit.name.trim() !== '' && r.edit.name.trim() !== baseline
}

// 忽略规则匹配子串：优先取识别名 source_name（未识别行取资产名单 asset_name）
function ignoreRuleMatch(r: ReviewRow): string {
  return r.row.source_name ?? r.row.asset_name ?? ''
}
function ignoreRuleEligible(r: ReviewRow): boolean {
  return ignoreRuleMatch(r).trim() !== ''
}

// --- 身份裁决（D4）辅助：疑似改名/拆分的行必须先由人三选一，才允许写入 ---
// 理由会进血缘审计（后端 R-9 要求非空）：UI 里留空时用一句纯事实的默认值，避免必填摩擦
const DEFAULT_IDENTITY_MERGE_REASON = '导入时人工确认沿用旧项'

function identityCandidates(r: ReviewRow): IdentityGuessCandidate[] {
  return r.row.identity_guess?.candidates ?? []
}

// 合并目标：用户选中的候选优先；只有一个候选时即该项（点「沿用旧项」就是明确选择）
function identityTargetOf(r: ReviewRow): number | null {
  const candidates = identityCandidates(r)
  const picked = r.edit.identityTargetId
  if (picked != null && candidates.some((c) => c.item_id === picked)) return picked
  return candidates.length === 1 ? candidates[0].item_id : null
}

// 未表态（撤销行＝skipped 不需要裁决，汇总行也不写库不需要裁决）
function identityNeedsDecision(r: ReviewRow): boolean {
  return !!r.row.identity_guess && !r.edit.skipped && !r.edit.aggregate && !r.edit.identityAction
}

// 已选「沿用旧项」但没选具体候选（多候选时）：同样阻断写入，不发无目标的血缘
function identityMergeNeedsTarget(r: ReviewRow): boolean {
  return (
    !!r.row.identity_guess &&
    !r.edit.skipped &&
    !r.edit.aggregate &&
    r.edit.identityAction === 'merge' &&
    identityTargetOf(r) == null
  )
}

function identityNeedsAttention(r: ReviewRow): boolean {
  // 后端 400 点名过的行（identityBlocked）也算未完成：保留其它行的选择，
  // 但在用户重新表态/补写理由前一直阻断写入，避免原地重复 400。
  return !!r.edit.identityBlocked || identityNeedsDecision(r) || identityMergeNeedsTarget(r)
}

// --- 关联到已有项（票 01 D1/D4）---
// 绑定目标：只有 identityAction === 'bind' 且选了项才算（取消关联 = 清空这三项，回到自动判定）
function bindTargetOf(r: ReviewRow): number | null {
  if (r.edit.identityAction !== 'bind') return null
  return r.edit.bindTargetId ?? null
}

function bindTargetNameOf(r: ReviewRow): string | null {
  if (r.edit.identityAction !== 'bind' || r.edit.bindTargetId == null) return null
  return r.edit.bindTargetName ?? ''
}

// 所有会新建资产项的行都必须先表态（D4 扩到「所有 create 行」）：
// 关联到已有项 / 确认作为新增 / 沿用旧项 / 撤销该行，任选其一。
// 未表态的行在提交前阻断写入（后端也会 400，前端不依赖后端兜底）。
function createNeedsDecision(r: ReviewRow): boolean {
  if (r.row.action !== 'create' || r.edit.skipped || r.edit.aggregate) return false
  if (bindTargetOf(r) != null) return false
  return !r.edit.identityAction
}

// 400 identity_decision_required 的 details.rows（旧后端不会产生该错误）
interface IdentityRequiredRow {
  key: number
  name?: string | null
  candidates: IdentityGuessCandidate[]
}

function parseIdentityRequiredRows(details: Record<string, unknown> | null): IdentityRequiredRow[] {
  const raw = details?.rows
  if (!Array.isArray(raw)) return []
  return raw
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
    .map((r) => ({
      key: Number(r.key),
      name: typeof r.name === 'string' ? r.name : null,
      candidates: Array.isArray(r.candidates)
        ? (r.candidates as IdentityGuessCandidate[]).filter((c) => c && typeof c.item_id === 'number')
        : [],
    }))
    .filter((r) => Number.isFinite(r.key))
}

// --- 异步 OCR job 进度呈现辅助 ---
function formatElapsed(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

// 整体百分比：各片均分；片内 det:rec 按实测耗时 ≈ 50:50 计（大 jpg 51:49 ~ png 61:39）
// chunk_index 为 0 基（后端冒烟实测）：已完成片数 = chunk_index，clamp 防越界/负值（含 done 快照归零态）
// line_total=0 表示当前片检测中，片内按 0 计；整体下限 1%，保证第 1 片检测中不显示 0%
// 运行态封顶 99%——done 瞬间直接切结果态，不闪 100%
function jobPercent(st: PreviewJobStatus): number {
  if (st.status === 'done') return 100
  if (st.stage === 'ocr' && st.ocr && st.ocr.chunk_total > 0) {
    const { chunk_index, chunk_total, line_done, line_total } = st.ocr
    const within =
      line_total > 0 ? 0.5 + 0.5 * Math.min(1, Math.max(0, line_done / line_total)) : 0
    const ci = Math.min(Math.max(0, chunk_index), chunk_total - 1) // 0 基 = 已完成片数
    return Math.min(99, Math.max(1, Math.round(((ci + within) / chunk_total) * 100)))
  }
  if (st.stage === 'cache' || st.stage === 'organize' || st.stage === 'match') return 99
  if (st.status === 'queued') return 2 // 排队（含提交后合成快照，ocr 为 null）：固定低占比
  return 5 // running 但 stage 未知/ocr 全 0（chunk_total=0）：兜底小进度，无除法不产生 NaN
}

// force=true（强制重新 OCR）：本次已要求后端忽略缓存，即便收到 cache 阶段快照也不能说「缓存命中」，
// 否则用户会以为白跑一趟；统一按「重新识别中」呈现
function jobStageText(st: PreviewJobStatus, force: boolean = false): string {
  const elapsed = st.elapsed > 0 ? ` · 已用 ${formatElapsed(st.elapsed)}` : ''
  if (st.status === 'queued') {
    return `排队中${st.queue_position != null ? `（第 ${st.queue_position} 位）` : ''}${elapsed}`
  }
  switch (st.stage) {
    case 'cache':
      return force ? `重新识别中${elapsed}` : `缓存命中${elapsed}`
    case 'ocr': {
      const o = st.ocr
      if (!o || o.chunk_total <= 0) return `识别中${elapsed}`
      // 0 基显示 +1；clamp 到 [1, chunk_total]，越界快照不显示「第 N+1/N 片」
      const cur = Math.min(Math.max(1, o.chunk_index + 1), o.chunk_total)
      const lineDone = Math.min(Math.max(0, o.line_done), o.line_total)
      const lines = o.line_total > 0 ? ` · 行 ${lineDone}/${o.line_total}` : ' · 检测中'
      return `识别中 · 第 ${cur}/${o.chunk_total} 片${lines}${elapsed}`
    }
    case 'organize':
    case 'match':
      return `解析匹配中${elapsed}` // 收尾阶段不再显示片数（done 快照 chunk_index 归零不误导）
    case 'done':
      return `完成${elapsed}`
    default:
      return `识别中${elapsed}` // stage=null 等异常态兜底，不显示片数
  }
}

const ACTION_META: Record<ScreenshotPreviewRow['action'], { label: string; cls: string }> = {
  update: { label: '更新', cls: 'bg-blue-100 text-blue-700' },
  create: { label: '新增', cls: 'bg-emerald-100 text-emerald-700' },
  zero: { label: '清零', cls: 'bg-amber-100 text-amber-700' },
  keep: { label: '保持原值', cls: 'bg-slate-100 text-slate-500' },
}

let idCounter = 0
function createId(): string {
  idCounter += 1
  return `img_${Date.now()}_${idCounter}`
}

// --- Types (local UI state) ---
type Phase = 'setup' | 'parsing' | 'review' | 'result'
// 未识别项策略：保持原值（默认） / 单独清0 / 全部清0
type UnrecognizedPolicy = 'keep' | 'zeroEach' | 'zeroAll'

// 逐图 AI 校验状态（User Story 11：让进度可见，独立于全局 spinner）
// idle=待校验 / verifying=校验中 / done=已校验 / failed=失败（降级，不影响写入）
type VerifyStatus = 'idle' | 'verifying' | 'done' | 'failed'

interface RowEdit {
  name: string
  amount: string
  skipped: boolean
  zeroChecked: boolean
  saveRename: boolean // 待存「改名规则」
  saveIgnore: boolean // 待存「忽略规则」
  // 规则备注（可选，只给人看不参与匹配）；设为可选以兼容热更新前创建的旧行状态
  renameNote?: string
  ignoreNote?: string
  // 「不录入」（仅 keep 行）：本次写入清 0 + 落 exclude 规则，以后不再出现在未识别列表；
  // 同样设为可选兼容旧行状态
  saveExclude?: boolean
  excludeNote?: string
  // 身份裁决（D4）：仅带 identity_guess 的行使用。undefined/null = 未表态（阻断写入）；
  // 'merge' = 沿用旧项（target 见 identityTargetId），'create' = 明确作为新资产新增，
  // 'bind' = 这一行其实是某个已有资产项（名字识别得不一样，target 见 bindTargetId）。
  // 「撤销该行」复用 skipped，不单独存值。可选以兼容旧行状态
  identityAction?: 'merge' | 'create' | 'bind' | null
  identityTargetId?: number | null
  identityReason?: string
  // 绑定目标（bind 专用）：显示名只用于行上标记「→ 目标名」，写入以 id 为准
  bindTargetId?: number | null
  bindTargetName?: string | null
  // 后端 400 identity_decision_required 回填：该行需要重新表态（红框提示）
  identityBlocked?: boolean
  // 汇总行（ADR-0029 D5/D7）：勾上＝只核对、不入账。不改动作语义（`action` 取值域不变），
  // 提交时走 apply 载荷的 `reconciles` 行号通道。aggregateSource 记「这一勾从哪来」（票 05 D2）：
  //   'rule'   = 后端 aggregate_hint 命中汇总词表的预勾，用户没动过；
  //   'manual' = 用户手点的；
  //   'parent' = 因为有子项归到它名下而自动置的（最后一条子边解除时自动撤，只撤这一种）。
  aggregate?: boolean
  aggregateSource?: AggregateSource | null
  // 归父（ADR-0029 D12）：父项引用。`row`＝本批勾了汇总行的某一行（可跨图），
  // `item`＝既有资产项。两者都表达「这一行由谁构成」，但核对锚与可挂性不同
  parent?: ParentRef | null
}

interface ImgState {
  id: string
  file: File
  previewUrl: string
  sourceGroup: string
  checked: boolean
  parsing: boolean
  parseError: string | null
  preview: ScreenshotPreviewResponse | null
  rows: Record<number, RowEdit>
  reparse: boolean
  // 本次解析是否为「强制重新 OCR」（忽略后端缓存）：只在该图在途解析期间为 true，
  // 用于进度文案（不显示「缓存命中」）与清单锁定；终局（成功/失败）复位。
  // 可选以兼容热更新前创建的旧图状态
  forceOcr?: boolean
  // 异步 OCR job 最近一次轮询快照（渲染进度条/排队位次；done/error 后置回 null）
  job: PreviewJobStatus | null
  // AI 校验结果（按行 index 存 verdict；重新解析后清空）
  verifyByIndex?: Record<number, VerifyRow> | null
  aiOnly?: VerifyAiOnlyItem[] | null
  // 图级校验状态与失败原因（逐图进度可见；重新解析后回到 idle）
  verifyStatus?: VerifyStatus
  verifyError?: string | null
  // 用户确认的数据日期（YYYY-MM-DD）：preview 就绪后初始化 = suspected_date ?? data_date ?? 今天；
  // apply 时作为顶层 data_date 携带。可选以兼容热更新前创建的旧图状态（缺失时按旧语义兜底）。
  selectedDate?: string
  // 补录草稿（AI 额外识别 → 用户手动加入本次清单）：独立于 rows，绝不进行号通道；
  // 重新识别（批量解析 / 强制重新 OCR）后清空。可选以兼容旧图状态
  supplements?: SupplementDraft[]
  // 识别引擎（peer-entry-design §4a：批级默认 + 逐图覆盖）：缺省 = 'ocr'（历史状态兼容，
  // OCR 路径行为与载荷逐字节不变）。'ai' = AI 主识别：preview 为空骨架（全 keep 行），
  // 识别项经 verify(rows=[]) → ai_only → 自动转采纳草稿（supplements 通道）写入
  engine?: 'ocr' | 'ai'
  // 本图正在「切换引擎」重跑 preview（仅在途为 true）：进度条文案不说「已忽略缓存」，
  // 失败/离开清单时按 engineBeforeSwitch 回滚。与 forceOcr 共用单图重跑轮询通道
  engineSwitch?: boolean
  // 切换引擎前的引擎（失败回滚用；成功/回滚后清空）
  engineBeforeSwitch?: 'ocr' | 'ai'
  // 页角色（票 05 D2）：主页 / 子页 / 未标 —— **纯注释**，不参与任何计算与校验，
  // 只在卡片上显示为备注（「改了也不影响任何数字」本身就该由文案说明，而不是靠控件暗示）
  pageRole?: 'none' | 'main' | 'sub'
}

// 该图当前生效引擎（engine 缺省 = ocr：旧状态 / OCR 默认路径）
const engineOf = (img: ImgState): 'ocr' | 'ai' => img.engine ?? 'ocr'

// 复核卡片的原图地址（review-image-viewer D3）：优先 evidence 原图端点（可缓存、可下载），
// 老状态没有 evidence_id 时回退本地 blob，保证「看原图」在两种状态下都成立
function reviewImageSrc(img: ImgState): string {
  const evidenceId = img.preview?.evidence_id
  return evidenceId != null ? api.evidenceImageUrl(evidenceId) : img.previewUrl
}

interface ReviewRow {
  imgId: string
  img: ImgState
  imageLabel: string
  groupLabel: string
  dataDate: string | null
  row: ScreenshotPreviewRow
  index: number
  edit: RowEdit
  displayAction: ScreenshotPreviewRow['action']
  isZeroed: boolean
  writable: boolean
  amount: string
  diff: string
  nameEditable: boolean
  /** 跨图行键 `(imgId, index)`：总表树序、归父、核对面板的唯一行身份 */
  key: RowKey
  /** 这一行被勾为汇总行（只核对、不入账） */
  aggregate: boolean
  /** 父项引用（null = 未归父） */
  parent: ParentRef | null
  /** 图序（1 基）：行内来源标记「图 N」与图卡跳转都用它 */
  imageOrdinal: number
}

interface ApplyOptions {
  source_group: string
  skipped: number[]
  amounts: Record<string, string>
  unrecognized: 'keep' | 'all'
  zero_names: string[]
  names: Record<string, string>
  // 补录项（独立于行号通道）；无补录时不带该键，载荷与今天逐字段相同
  supplements?: { name: string; amount: string; side: SupplementSide }[]
  // AI 主识别图专用（peer-entry-design §5）：跳过 OCR、anchor=null；OCR 图绝不带该键
  engine?: 'ai'
  // 身份裁决（D4）：键＝预览行 index；无裁决时不带该键（载荷与今天逐字段相同）
  identity_decisions?: Record<string, IdentityDecisionPayload>
  // 汇总行（ADR-0029 D5）：本次勾为「只核对、不入账」的行号（与 skipped 同形）；
  // 无汇总行时不带该键（载荷与今天逐字段相同）
  reconciles?: number[]
  // 批次成员（ADR-0029 D8）：先 POST /screenshot-batches 拿 id，逐图 apply 时带上，
  // 后端才写得进成员行；无批次（旧路径）时绝不带该键
  batch_id?: number
}

// 自动 AI 校验偏好（默认关）：开启后「生成清单时自动调 verify 一次」
const AUTO_VERIFY_KEY = 'screenshot_auto_verify'

// 拖拽落点停留判定（票 09 D3 / 调研建议）：悬停满这么久才认投放。
// 拖过一行就松手是这条交互最常见的误放，别用「即时生效」换手感
const DROP_DWELL_MS = 500

// --- Small UI atoms ---
function ActionBadge({ action, bound, aggregate }: { action: ScreenshotPreviewRow['action']; bound?: boolean; aggregate?: boolean }) {
  // 汇总行优先：动作徽章换成「核对」（ADR-0029 D6）——这一行不落账，动作是什么已经不重要，
  // 重要的是「它只参与核对」。这与「关联」互斥（汇总行不可作 bind 目标，D13）
  if (aggregate) {
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap rounded bg-slate-200 px-2 py-0.5 text-xs text-slate-700">
        <span aria-hidden="true" className="text-[9px]">◍</span> 核对
      </span>
    )
  }
  // 已关联到已有项：动作徽章换成「关联」（目标名在行内标记里，见 RowBindControl）
  if (bound) {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-brand-100 px-2 py-0.5 text-xs whitespace-nowrap text-brand-700">
        <Link2 size={11} aria-hidden="true" /> 关联
      </span>
    )
  }
  const meta = ACTION_META[action]
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs whitespace-nowrap ${meta.cls}`}>
      {meta.label}
    </span>
  )
}

// 页角色备注（票 05 D2）：主页 / 子页 / 未标。**纯注释**——不参与判定、核对与合计。
// 控件用虚线胶囊 + 一句「仅作注释」明说它什么都没变：把「标了也没区别」表达清楚，
// 比让用户以为它是个开关要好。
const PAGE_ROLE_LABEL: Record<'none' | 'main' | 'sub', string> = {
  none: '未标',
  main: '主页',
  sub: '子页',
}

function PageRoleNote({
  value,
  onChange,
  disabled,
  idSuffix,
}: {
  value: 'none' | 'main' | 'sub' | undefined
  onChange: (next: 'none' | 'main' | 'sub') => void
  disabled?: boolean
  idSuffix: string
}) {
  const role = value ?? 'none'
  return (
    <label className="flex items-center gap-1.5 text-meta text-muted" title="仅作注释：不改这一页任何一行的判定、核对差额与写入合计">
      <span className="whitespace-nowrap">页角色</span>
      <select
        id={`page-role-${idSuffix}`}
        value={role}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as 'none' | 'main' | 'sub')}
        className="min-h-[32px] cursor-pointer appearance-none rounded-pill border border-dashed border-line-strong bg-subtle px-2 text-center text-xs text-secondary focus:outline-none focus:ring-1 focus:ring-brand-400 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {(['none', 'main', 'sub'] as const).map((k) => (
          <option key={k} value={k}>
            {PAGE_ROLE_LABEL[k]}
          </option>
        ))}
      </select>
      <span className="hidden whitespace-nowrap sm:inline">仅作注释</span>
    </label>
  )
}

// AI 校验结论配色（ActionBadge 同款风格）
const VERIFY_META: Record<VerifyVerdict, { label: string; cls: string }> = {
  match: { label: '一致', cls: 'bg-emerald-100 text-emerald-700' },
  mismatch: { label: '分歧', cls: 'bg-amber-100 text-amber-700' },
  ocr_only: { label: '仅 OCR', cls: 'bg-blue-100 text-blue-700' },
  unverified: { label: '未校验', cls: 'bg-slate-100 text-slate-500' },
}

function VerifyBadge({ v, ocrName, ocrAmount, status, error }: {
  v?: VerifyRow
  ocrName: string | null
  ocrAmount: string
  status?: VerifyStatus
  error?: string | null
}) {
  // 图级「校验中 / 失败」优先展示：重跑时盖过上一轮的行级旧结论，保证进度真实可见
  if (status === 'verifying') {
    return (
      <Tooltip title="AI 校验中…">
        <span className="inline-flex items-center justify-center w-5 h-5 text-brand-500">
          <Loader2 size={14} className="animate-spin" />
        </span>
      </Tooltip>
    )
  }
  if (status === 'failed') {
    return (
      <Tooltip title={`AI 校验失败：${error || '请求异常'}（已降级跳过，不影响写入）`}>
        <span className="inline-flex items-center justify-center w-5 h-5 text-red-400">
          <AlertTriangle size={14} />
        </span>
      </Tooltip>
    )
  }
  const verdict: VerifyVerdict = v?.verdict ?? 'unverified'
  // 已获行级结论（已校验）：一致 / 分歧 / 仅 OCR
  if (v && verdict !== 'unverified') {
    const meta = VERIFY_META[verdict]
    const badge = (
      <span className={`inline-block px-2 py-0.5 rounded text-xs whitespace-nowrap ${meta.cls}`}>
        {verdict === 'match' && '✓ '}
        {verdict === 'mismatch' && '⚠ '}
        {meta.label}
      </span>
    )
    if (verdict === 'mismatch') {
      // 分歧时并排展示 OCR 值 vs AI 值（名称 + 金额）
      return (
        <Tooltip
          title={
            <div className="text-xs">
              <p>名称　OCR：{ocrName || '--'}</p>
              <p>　　　AI　：{v.ai_name || '--'}</p>
              <p className="mt-1">金额　OCR：{ocrAmount}</p>
              <p>　　　AI　：{v.ai_amount || '--'}</p>
            </div>
          }
        >
          {badge}
        </Tooltip>
      )
    }
    if (verdict === 'match') {
      return (
        <Tooltip title={<span className="text-xs">AI 识别一致：{v.ai_name || ocrName || '--'} {formatCurrency(v.ai_amount || ocrAmount)}</span>}>
          {badge}
        </Tooltip>
      )
    }
    // ocr_only：OCR 有、AI 没认出（可能是 AI 漏看；文案为双路模式措辞，peer-entry-design §1.2）
    return (
      <Tooltip title={<span className="text-xs">OCR 识别了该项，AI 未在截图中认出（比对供裁决，写入的是你确认的清单）</span>}>
        {badge}
      </Tooltip>
    )
  }
  // 无行级结论：done=AI 未给出该行结论；idle=待校验
  if (status === 'done') {
    // 调用成功但 AI 未给出该行结论（如供应商未配置 key）
    return (
      <Tooltip title="AI 未给出该行结论，可到「数据管理 → LLM 供应商」检查配置后重试">
        <span className={`inline-block px-2 py-0.5 rounded text-xs whitespace-nowrap ${VERIFY_META.unverified.cls}`}>
          {VERIFY_META.unverified.label}
        </span>
      </Tooltip>
    )
  }
  // idle：待校验
  return (
    <Tooltip title="待校验：尚未进行 AI 校验">
      <span className="inline-flex items-center justify-center w-5 h-5 text-slate-300">
        <Clock size={14} />
      </span>
    </Tooltip>
  )
}

// --- 校验汇总（清单顶部轻量条，仅校验完成后显示；疑点>0 用 amber 强调，全通过用 emerald） ---
interface VerifySummaryData {
  hasResult: boolean
  match: number
  mismatch: number
  ocrOnly: number // 仅 OCR（行级 verdict）
  aiOnly: number // 仅 AI（VerifyResponse.ai_only 列表，且尚未被用户补录）
  supplemented: number // 仅 AI 里已被用户补录进清单的项数（不再算「待处理的单边缺项」）
  unverified: number // 调用成功但 AI 未给出结论的行
  failed: number // 校验失败的图片数
}

// mode（peer-entry-design §1.2 文案条款）：'ocr'=纯 OCR/双路互验，'ai'=批内全部为 AI 主识别图，
// 'mixed'=混批。右侧结语句与「已补录/已采纳」措辞按模式映射（AI 模式术语 = 识别清单/采纳）
function VerifySummaryBar({ s, mode = 'ocr' }: { s: VerifySummaryData; mode?: 'ocr' | 'ai' | 'mixed' }) {
  const oneSided = s.ocrOnly + s.aiOnly
  const issues = s.mismatch + oneSided
  // 补录过的项算「已处理」，但也不能因此把整批说成「未发现疑点」
  const clean = issues === 0 && s.failed === 0 && s.unverified === 0 && s.supplemented === 0
  const parts: ReactNode[] = []
  if (s.mismatch > 0) parts.push(<span key="mm"><span className="font-semibold">{s.mismatch}</span> 处分歧</span>)
  if (oneSided > 0) {
    parts.push(
      <span key="os">
        <span className="font-semibold">{oneSided}</span> 处单边缺项（仅 OCR {s.ocrOnly} / 仅 AI {s.aiOnly}）
      </span>,
    )
  }
  if (s.supplemented > 0) {
    parts.push(
      <span key="sp" className="text-emerald-700">
        <span className="font-semibold">{s.supplemented}</span>{' '}
        {mode === 'ai' ? '项 AI 识别已采纳' : mode === 'mixed' ? '项 AI 识别已采纳/补录' : '项仅 AI 已补录'}
      </span>,
    )
  }
  if (s.failed > 0) parts.push(<span key="f"><span className="font-semibold">{s.failed}</span> 张{mode === 'ai' ? '识别失败' : '校验失败'}</span>)
  if (s.unverified > 0) parts.push(<span key="u"><span className="font-semibold">{s.unverified}</span> 行未获结论</span>)
  if ((issues > 0 || s.supplemented > 0) && s.match > 0) {
    parts.push(<span key="ok" className="text-amber-700/70">其余 {s.match} 行一致</span>)
  }
  const tailText =
    mode === 'ai'
      ? 'AI 识别项需你确认后写入'
      : mode === 'mixed'
        ? 'AI 项经采纳后写入 · 分歧行可一键采纳 AI 值 —— 写入的是你确认的清单'
        : '比对供裁决：分歧行可一键采纳 AI 值，仅 AI 项可补录；写入的是你确认的清单'
  return (
    <div
      className={`px-6 py-2 border-b text-xs flex items-center gap-2 flex-wrap ${
        clean ? 'bg-emerald-50/60 border-emerald-100 text-emerald-700' : 'bg-amber-50/70 border-amber-100 text-amber-800'
      }`}
    >
      <Bot size={13} className={`shrink-0 ${clean ? 'text-emerald-500' : 'text-amber-500'}`} />
      {clean ? (
        <span>
          {mode === 'ai' ? 'AI 识别：' : '本次校验：'}
          {s.match > 0 ? (
            <>
              <span className="font-semibold">{s.match}</span> 行与 AI 识别全部一致，未发现疑点
            </>
          ) : (
            // 全是未识别项的图（rows=[]）没有行级结论，别显示「0 行一致」
            '未发现疑点'
          )}
        </span>
      ) : (
        <span className="inline-flex items-center gap-2 flex-wrap">
          {mode === 'ai' ? 'AI 识别：' : '本次校验：'}
          {parts.map((p, i) => (
            <Fragment key={i}>
              {i > 0 && <span className="text-amber-300">·</span>}
              {p}
            </Fragment>
          ))}
        </span>
      )}
      <span className={`ml-auto whitespace-nowrap ${clean ? 'text-emerald-600/60' : 'text-amber-600/60'}`}>
        {tailText}
      </span>
    </div>
  )
}

function ErrorBanner({ message, onClose }: { message: string; onClose: () => void }) {
  return (
    <div className="flex items-center gap-3 p-4 bg-red-50 rounded-xl border border-red-200">
      <AlertTriangle size={20} className="text-red-600 shrink-0" />
      <div className="flex-1">
        <p className="text-sm font-medium text-red-800">录入失败</p>
        <p className="text-sm text-red-600 mt-1">{message}</p>
      </div>
      <button onClick={onClose} className="text-red-400 hover:text-red-600">
        <X size={16} />
      </button>
    </div>
  )
}

function HintBanner({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-3 p-3 bg-amber-50 rounded-xl border border-amber-200 text-sm text-amber-800">
      <AlertTriangle size={16} className="shrink-0 text-amber-500" />
      {message}
    </div>
  )
}

function StatCard({ label, value, hint, accent }: {
  label: string
  value: string
  hint?: string
  accent?: 'emerald' | 'amber' | 'red'
}) {
  const accentClasses = {
    emerald: 'text-emerald-600',
    amber: 'text-amber-600',
    red: 'text-red-600',
  }
  return (
    <div className="bg-white rounded-xl border border-slate-200 px-4 py-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1.5 text-lg font-semibold font-mono ${accent ? accentClasses[accent] : 'text-brand-950'}`}>
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  )
}

function AmountInput({ value, disabled, onChange, full }: {
  value: string
  disabled?: boolean
  onChange: (v: string) => void
  /** 移动卡片里金额输入占满整行（桌面表格仍用 w-24 紧凑宽） */
  full?: boolean
}) {
  return (
    <label className={`${full ? 'flex' : 'inline-flex'} items-center gap-1 ${disabled ? 'opacity-50' : ''}`}>
      <span className="text-xs text-muted">¥</span>
      <input
        type="number"
        step="0.01"
        inputMode="decimal"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={`${full ? 'w-full' : 'w-24'} px-2 py-1.5 text-right font-mono text-base lg:text-sm text-slate-700 border border-slate-200 rounded-md focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors ${
          disabled ? 'bg-slate-100 cursor-not-allowed' : 'bg-white'
        }`}
      />
    </label>
  )
}

function NameInput({ value, onChange, full }: { value: string; onChange: (v: string) => void; full?: boolean }) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`${full ? 'w-full' : 'w-full max-w-[180px]'} px-2 py-1.5 text-base lg:text-sm text-slate-700 border border-slate-200 rounded-md bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors`}
    />
  )
}

// 「存为规则」行内辅助开关：小号、不抢眼，与页内 native checkbox 视觉一致
function RuleToggle({ checked, onChange, label, title, hint, disabled }: {
  checked: boolean
  onChange: () => void
  label: string
  title: string
  /** 移动端常驻的一句话说明：title 是 hover 提示、触屏读不到，破坏性语义必须可见；桌面仍走 title */
  hint?: string
  disabled?: boolean
}) {
  return (
    <label
      title={title}
      className={`inline-flex flex-wrap items-center gap-1 min-h-[44px] lg:min-h-0 text-meta select-none transition-colors align-middle ${
        disabled ? 'text-slate-300 cursor-not-allowed' : 'text-slate-500 hover:text-brand-600 cursor-pointer'
      }`}
    >
      <Lightbulb size={11} className={`shrink-0 ${disabled ? 'text-slate-200' : 'text-slate-300'}`} />
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        className="w-5 h-5 lg:w-3.5 lg:h-3.5 accent-brand-600 cursor-pointer disabled:cursor-not-allowed"
      />
      <span className="whitespace-nowrap">{label}</span>
      {hint && (
        <span className="w-full whitespace-normal text-meta leading-snug text-muted lg:hidden">
          {hint}
        </span>
      )}
    </label>
  )
}

// 勾选「存为规则」后展开的备注小输入：说明原因用，可选不填；只给人看，不参与匹配
function RuleNoteInput({ value, onChange, placeholder, full, title }: {
  value: string; onChange: (v: string) => void; placeholder: string; full?: boolean; title?: string
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      title={title ?? '备注只给人看，不参与匹配；可不填'}
      className={`${full ? 'w-full' : 'w-36 shrink-0'} px-1.5 py-1 text-base lg:text-meta text-slate-600 border border-slate-200 rounded-md bg-white placeholder:text-slate-300 focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors`}
    />
  )
}

// --- 身份裁决（D4）：疑似改名/拆分的行内提示 + 三选一 ---
// 只陈述观测（「该行名称没有匹配到已有资产项，同账户下已有…」），不断言就是改名；
// 默认未表态（没有预选「新增」），未表态的行会在底部阻断写入。
// 视觉沿用页内既有 amber=需留意 / brand=选中 / slate=中性，不引入新体系。
function IdentityGuessNotice({ r, locked, onUpdate, surface }: {
  r: ReviewRow
  locked: boolean
  onUpdate: (patch: Partial<RowEdit>) => void
  /** 同一行在桌面表格与移动卡片各渲染一次：锚点 id 必须唯一（供底部摘要跳转） */
  surface: 'desktop' | 'mobile'
}) {
  // 候选 >3 时默认收起（票 02 D2）：黄框收进名称列后横向变窄，一次铺满会把整行撑高
  const [showAllCandidates, setShowAllCandidates] = useState(false)
  const guess = r.row.identity_guess
  if (!guess) return null
  const anchorId = `identity-${surface}-${r.imgId}-${r.index}`
  const candidates = guess.candidates ?? []
  const visibleCandidates = showAllCandidates ? candidates : candidates.slice(0, 3)
  const hiddenCandidates = candidates.length - visibleCandidates.length
  const action = r.edit.identityAction ?? null
  const canMerge = candidates.length > 0
  const needsTarget = action === 'merge' && candidates.length > 1 && identityTargetOf(r) == null
  const reason = r.edit.identityReason ?? ''
  const blocked = !!r.edit.identityBlocked
  // 已关联到已有项：这一行的身份由「关联」决定，三选一与候选列表都不再适用
  const bound = bindTargetOf(r) != null
  const radioName = `identity-${r.imgId}-${r.index}`
  const targetName = `identity-target-${r.imgId}-${r.index}`

  const choose = (next: 'merge' | 'create' | 'skip') => {
    if (next === 'skip') {
      onUpdate({ skipped: true, identityBlocked: false })
      return
    }
    if (next === 'create') {
      // 改选「作为新资产新增」= 放弃关联（否则 bind 会一直优先，选择不生效）
      onUpdate({
        skipped: false,
        identityAction: 'create',
        bindTargetId: null,
        bindTargetName: null,
        identityBlocked: false,
      })
      return
    }
    const picked = identityTargetOf(r)
    onUpdate({
      skipped: false,
      identityAction: 'merge',
      identityTargetId: picked,
      bindTargetId: null,
      bindTargetName: null,
      identityBlocked: false,
    })
  }

  const optionClass = (on: boolean, off: boolean = false) =>
    `flex min-h-[44px] items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors lg:min-h-[36px] ${
      off
        ? 'cursor-not-allowed border-slate-200 bg-white text-slate-300'
        : on
          ? 'border-brand-400 bg-white text-brand-800 shadow-sm'
          : 'border-slate-200 bg-white text-slate-600 hover:border-brand-300 hover:text-brand-700'
    }`

  return (
    <div
      id={anchorId}
      role="group"
      aria-label={`身份确认：${r.edit.name || r.row.source_name || '该行'}`}
      className={`rounded-lg border px-2.5 py-2 ${
        blocked ? 'border-red-300 bg-red-50/70' : 'border-amber-200 bg-amber-50/70'
      }`}
    >
      <p className={`flex items-start gap-1.5 text-xs ${blocked ? 'text-red-800' : 'text-amber-900'}`}>
        <AlertTriangle
          size={13}
          className={`mt-0.5 shrink-0 ${blocked ? 'text-red-500' : 'text-amber-500'}`}
          aria-hidden="true"
        />
        <span className="min-w-0">
          {bound
            ? '该行已关联到已有资产项'
            : blocked
              ? '后端仍未收到该行的身份确认，请重新选择：'
              : candidates.length > 0
                ? '该行名称没有匹配到已有资产项，同账户下已有：'
                : '该行名称没有匹配到已有资产项。'}
        </span>
      </p>

      {/* 已关联：沿用旧项 / 新增 / 撤销 这组选择不再适用（关联优先，见票 01 D2） */}
      {bound ? (
        <p className="mt-1 text-xs text-slate-600">
          这一行改为按已有项写入：沿用旧项 / 作为新增的选择不再适用；取消关联后可重新选择。
        </p>
      ) : (
        <>
          {candidates.length > 0 && (
            <ul
              className="mt-1 space-y-1"
              role={action === 'merge' && candidates.length > 1 ? 'radiogroup' : undefined}
              aria-label={action === 'merge' && candidates.length > 1 ? '选择要沿用的旧项' : undefined}
            >
              {visibleCandidates.map((c) => {
                const selectable = action === 'merge' && !r.edit.skipped && candidates.length > 1
                const selected = identityTargetOf(r) === c.item_id
                const info = (
                  <span className="min-w-0 flex-1">
                    <span className="block break-words text-xs font-medium text-slate-700">
                      {c.name}
                    </span>
                    <span className="block text-meta text-slate-500">
                      <span className="font-mono text-slate-700">{formatCurrency(c.amount)}</span>
                      {c.last_date && <span className="ml-1.5">最后记录 {formatDate(c.last_date)}</span>}
                    </span>
                  </span>
                )
                return (
                  <li key={c.item_id}>
                    {selectable ? (
                      <label
                        className={`flex cursor-pointer items-start gap-2 rounded-md border px-2 py-1 focus-within:ring-2 focus-within:ring-brand-400 focus-within:ring-offset-1 ${
                          selected ? 'border-brand-300 bg-white' : 'border-transparent'
                        } ${locked ? 'cursor-not-allowed opacity-60' : ''}`}
                      >
                        <input
                          type="radio"
                          name={targetName}
                          className="peer sr-only"
                          checked={selected}
                          disabled={locked}
                          onChange={() => onUpdate({ identityTargetId: c.item_id, identityBlocked: false })}
                        />
                        {info}
                      </label>
                    ) : (
                      <div className="flex items-start px-2 py-1">
                        {info}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          {hiddenCandidates > 0 && (
            <button
              type="button"
              data-identity-more="true"
              onClick={() => setShowAllCandidates(true)}
              className="mt-1 text-meta text-brand-700 underline-offset-2 hover:underline"
            >
              另有 {hiddenCandidates} 个候选，展开
            </button>
          )}
          {showAllCandidates && candidates.length > 3 && (
            <button
              type="button"
              onClick={() => setShowAllCandidates(false)}
              className="mt-1 text-meta text-slate-500 underline-offset-2 hover:underline"
            >
              收起候选
            </button>
          )}

          <div className="mt-1.5 flex flex-wrap gap-1.5" role="radiogroup" aria-label="身份处理方式">
            <label className={locked || !canMerge ? 'cursor-not-allowed' : 'cursor-pointer'}>
              <input
                type="radio"
                name={radioName}
                className="peer sr-only"
                checked={action === 'merge' && !r.edit.skipped}
                disabled={locked || !canMerge}
                data-identity-choice="merge"
                onChange={() => choose('merge')}
              />
              <span
                className={`${optionClass(action === 'merge' && !r.edit.skipped, locked || !canMerge)} peer-focus-visible:ring-2 peer-focus-visible:ring-brand-400 peer-focus-visible:ring-offset-1`}
              >
                <GitMerge size={13} className="shrink-0" aria-hidden="true" /> 沿用旧项（合并）
              </span>
            </label>
            <label className={locked ? 'cursor-not-allowed' : 'cursor-pointer'}>
              <input
                type="radio"
                name={radioName}
                className="peer sr-only"
                checked={action === 'create' && !r.edit.skipped}
                disabled={locked}
                data-identity-choice="create"
                onChange={() => choose('create')}
              />
              <span
                className={`${optionClass(action === 'create' && !r.edit.skipped, locked)} peer-focus-visible:ring-2 peer-focus-visible:ring-brand-400 peer-focus-visible:ring-offset-1`}
              >
                <Plus size={13} className="shrink-0" aria-hidden="true" /> 作为新资产新增
              </span>
            </label>
            <label className={locked ? 'cursor-not-allowed' : 'cursor-pointer'}>
              <input
                type="radio"
                name={radioName}
                className="peer sr-only"
                checked={r.edit.skipped}
                disabled={locked}
                data-identity-choice="skip"
                onChange={() => choose('skip')}
              />
              <span
                className={`${optionClass(r.edit.skipped, locked)} peer-focus-visible:ring-2 peer-focus-visible:ring-brand-400 peer-focus-visible:ring-offset-1`}
              >
                <X size={13} className="shrink-0" aria-hidden="true" /> 撤销该行
              </span>
            </label>
          </div>

          {action === 'merge' && !r.edit.skipped && (
            <div className="mt-1.5 space-y-1">
              {needsTarget && (
                <p className="text-meta text-amber-800">有多个候选，请点选上方要沿用的那一项。</p>
              )}
              <label className="block">
                <span className="mb-0.5 block text-meta text-slate-500">
                  理由（可选，写入血缘审计；留空记为「{DEFAULT_IDENTITY_MERGE_REASON}」）
                </span>
                <RuleNoteInput
                  full
                  value={reason}
                  onChange={(v) => onUpdate({ identityReason: v, identityBlocked: false })}
                  placeholder="例如：同一笔持仓改名"
                  title="理由会写入血缘审计，不参与匹配；留空按默认文案记录"
                />
              </label>
              <p className="text-meta text-slate-500">
                旧项会在本次写入日期起停止沿用；此前历史保留。选错时请改选；如已写入，请到记录页核对并更正。
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// --- 关联到已有项（票 01）：行内入口 / 已关联标记 + 候选选择器 ---
// 「+ 新增」行常说的情况是：这行其实就是库里某个已有资产项，只是名字识别得不一样。
// 关联不造新项、不改目标显示名、不动血缘，金额仍以本行截图为准。
const BIND_BTN =
  'flex min-h-[44px] items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors lg:min-h-[36px]'
const BIND_BTN_ON =
  'border-slate-200 bg-white text-slate-600 hover:border-brand-300 hover:text-brand-700 active:bg-slate-50'
const BIND_BTN_OFF = 'border-slate-200 bg-white text-slate-300 cursor-not-allowed'

function RowBindControl({ r, locked, onOpen, onClear }: {
  r: ReviewRow
  locked: boolean
  onOpen: (r: ReviewRow) => void
  onClear: (r: ReviewRow) => void
}) {
  const targetName = bindTargetNameOf(r)
  if (targetName != null) {
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-lg border border-brand-300 bg-white px-2.5 py-1.5 text-xs font-medium text-brand-800 lg:min-h-[36px]"
          title={`本次金额写入「${targetName}」`}
        >
          <Link2 size={13} className="shrink-0" aria-hidden="true" />
          <span className="truncate">→ {targetName}</span>
        </span>
        <button
          type="button"
          disabled={locked}
          onClick={() => onClear(r)}
          className={`${BIND_BTN} ${locked ? BIND_BTN_OFF : BIND_BTN_ON}`}
        >
          <X size={13} className="shrink-0" aria-hidden="true" /> 取消关联
        </button>
        <span className="text-meta text-slate-500">金额以本次截图为准</span>
      </div>
    )
  }
  return (
    <div>
      <button
        type="button"
        disabled={locked}
        onClick={() => onOpen(r)}
        title="这一行其实是某个已有资产项，只是名字识别得不一样：选它，本次金额写进该项，识别名记为它的别名"
        className={`${BIND_BTN} ${locked ? BIND_BTN_OFF : BIND_BTN_ON}`}
      >
        <Link2 size={13} className="shrink-0" aria-hidden="true" /> 关联到已有项
      </button>
    </div>
  )
}

/** 「历史上已并入 X」提示（spec `.scratch/archived-ingest-scope/spec.md` D3，票 02）。
 *
 *  触发：本行名称在血缘上属于一个已归档项，后端因此把该行生成为 create 行并附
 *  `archived_merged_into`。这里只陈述事实（历史上并入了谁），不替用户做决定——
 *  要落到同一个地方仍由「关联到已有项」选。
 *
 *  兼容口径：字段缺失（旧后端）或 null → 什么都不渲染、不报错；撤销行/汇总行也不显示。
 */
function ArchivedMergedIntoNotice({ r }: { r: ReviewRow }) {
  const merged = r.row.archived_merged_into
  if (!merged || r.edit.skipped || r.aggregate) return null
  return (
    <p className="flex items-start gap-1 text-meta leading-relaxed text-slate-500">
      <GitMerge size={12} className="mt-0.5 shrink-0 text-slate-400" aria-hidden="true" />
      <span className="min-w-0">
        历史上已并入「{merged.display_name}」
        {merged.archived_effective_from ? `（该名 ${formatDate(merged.archived_effective_from)} 起归档）` : ''}
        。本行直接按新增提交会被拒（该名已退出计量）：点「关联到已有项」选它并入的在用项，
        或把本名称改成一个真正的新名称。
      </span>
    </p>
  )
}

/** 关联目标选择器：列出**在用**的资产项（active + draft），按显示名 + 别名做本地过滤（大小写不敏感）。
 *
 *  规格 `.scratch/archived-ingest-scope/spec.md` D1/D5：
 *  - 可选中＝`status !== 'archived'`（后端 bind 只拒 archived，`draft` 是可计入净值的在册项）；
 *  - 搜索命中已归档项时，在列表尾部给一行**不可点选**的说明「<项名> 已归档（<生效日>），已并入 X」，
 *    直接消掉「未识别项面板说有、选择器搜不到」的死胡同；归档项绝不作为 bind 目标（后端 409 ITEM_ARCHIVED）。
 */
function BindItemPicker({ rowName, onPick, onClose, blockedItems }: {
  rowName: string
  onPick: (item: AssetItemInfo) => void
  onClose: () => void
  /** 不能作为关联目标的项（item_id → 原因）：本批勾为汇总行的项只核对、不入账（ADR-0029 D13） */
  blockedItems?: Map<number, string>
}) {
  const [keyword, setKeyword] = useState('')
  const panelRef = useRef<HTMLDivElement>(null)
  // include_archived=true（票 02 D2）：归档项要能被关键字搜到才谈得上「给一行说明」；
  // 能否选中由下面的 status 过滤决定，与后端一致。
  const itemsQuery = useQuery({
    queryKey: ['assetItems', true],
    queryFn: () => api.listAssetItems(true),
    staleTime: 60_000,
  })
  const allItems = useMemo(() => itemsQuery.data?.items ?? [], [itemsQuery.data])
  const kw = keyword.trim().toLowerCase()
  const matchesKeyword = useCallback((it: AssetItemInfo) => {
    if (!kw) return true
    if (it.display_name.toLowerCase().includes(kw)) return true
    return it.aliases.some((alias) => alias.toLowerCase().includes(kw))
  }, [kw])
  const candidates = useMemo(() => {
    const hit = allItems.filter((it) => it.status !== 'archived' && matchesKeyword(it))
    return [...hit].sort((a, b) => a.display_name.localeCompare(b.display_name, 'zh-CN'))
  }, [allItems, matchesKeyword])
  // 归档命中：只在有关键字时才说明（空关键字铺满整页归档项没有意义）
  const archivedHits = useMemo(() => {
    if (!kw) return []
    return allItems
      .filter((it) => it.status === 'archived' && matchesKeyword(it))
      .sort((a, b) => a.display_name.localeCompare(b.display_name, 'zh-CN'))
  }, [allItems, kw, matchesKeyword])
  const archivedIds = useMemo(() => archivedHits.map((it) => it.id), [archivedHits])
  const hintsQuery = useQuery({
    // 键用 id 串（数组每次渲染都是新引用）；queryFn 复用同一批 id，键变即重取
    queryKey: ['archivedItemHints', archivedIds.join(',')],
    queryFn: () => fetchArchivedItemHints(archivedIds),
    enabled: archivedIds.length > 0,
    staleTime: 60_000,
  })
  const hintOf = (itemId: number) => hintsQuery.data?.get(itemId)

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Tab 在弹层内循环（这里没有 portal，不能对背景设 inert，就自己兜住焦点）
  const trapTab = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return
    const container = panelRef.current
    if (!container) return
    const items = Array.from(
      container.querySelectorAll<HTMLElement>('button, input, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => !el.hasAttribute('disabled') && el.getClientRects().length > 0)
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    const active = document.activeElement
    if (!container.contains(active)) {
      event.preventDefault()
      first.focus()
    } else if (!event.shiftKey && active === last) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && active === first) {
      event.preventDefault()
      last.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center lg:items-center" role="dialog" aria-modal="true" aria-label="关联到已有项">
      <div className="absolute inset-0 bg-black/40 animate-fade-in" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        onKeyDown={trapTab}
        className="relative flex max-h-[85vh] w-full flex-col rounded-t-2xl bg-white shadow-2xl animate-slide-up lg:max-w-lg lg:rounded-xl"
      >
        <div className="flex items-start justify-between gap-2 border-b border-slate-100 px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-brand-950">关联到已有项</p>
            <p className="truncate text-xs text-muted">本行：{rowName || '未命名'}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-slate-100"
          >
            <X size={20} />
          </button>
        </div>

        <div className="border-b border-slate-100 px-4 py-2.5">
          <div className="flex items-center gap-2 rounded-lg border border-slate-200 px-2.5 focus-within:border-brand-400 focus-within:ring-1 focus-within:ring-brand-400">
            <Search size={14} className="shrink-0 text-slate-400" aria-hidden="true" />
            <input
              type="text"
              autoFocus
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索名称或别名"
              className="min-h-[44px] w-full bg-transparent text-base text-slate-700 placeholder:text-slate-300 focus:outline-none lg:min-h-[36px] lg:text-sm"
            />
            {keyword && (
              <button
                type="button"
                onClick={() => setKeyword('')}
                aria-label="清空搜索"
                className="shrink-0 rounded p-1 text-slate-400 hover:text-slate-600"
              >
                <X size={14} />
              </button>
            )}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {itemsQuery.isLoading && (
            <p className="flex items-center gap-2 px-4 py-6 text-sm text-muted">
              <Loader2 size={14} className="animate-spin" /> 正在读取资产项…
            </p>
          )}
          {itemsQuery.isError && (
            <p className="px-4 py-6 text-sm text-red-600">
              资产项读取失败：{itemsQuery.error instanceof Error ? itemsQuery.error.message : '请求异常'}
            </p>
          )}
          {!itemsQuery.isLoading && !itemsQuery.isError && candidates.length === 0 && (
            <p className="px-4 py-6 text-sm text-muted">
              {archivedHits.length > 0
                ? '没有匹配到在用的资产项；下面列出的是已归档的同名项，可改选它并入的那个在用项。'
                : keyword.trim()
                  ? '没有匹配到在用的资产项，换个关键词，或把它确认为新增。'
                  : '还没有在用的资产项可以关联；本次先确认为新增即可。'}
            </p>
          )}
          <ul className="divide-y divide-slate-100">
            {candidates.map((item) => {
              const blockedBy = blockedItems?.get(item.id)
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    onClick={() => onPick(item)}
                    disabled={!!blockedBy}
                    title={
                      blockedBy
                        ? `「${blockedBy}」是本批的汇总行：只核对、不入账，不能作为关联目标`
                        : undefined
                    }
                    className="flex min-h-[44px] w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-brand-50 focus-visible:bg-brand-50 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block break-words text-sm text-slate-700">{item.display_name}</span>
                      {blockedBy && (
                        <span className="mt-0.5 block text-meta leading-relaxed text-slate-500">
                          本批汇总行（只核对、不入账），不能作为关联目标
                        </span>
                      )}
                    </span>
                    {item.aliases.length > 0 && (
                      <span className="shrink-0 text-meta text-slate-500">含 {item.aliases.length} 个别名</span>
                    )}
                  </button>
                </li>
              )
            })}
          </ul>

          {/* 归档说明行（票 02 / spec D5）：**不可点选**，只负责解释「为什么搜得到却选不了、这笔该落到哪」。
              单项 hint 读不到（旧后端 / 链缺失）时只显示「已归档」，不报错、不阻塞选择 */}
          {archivedHits.length > 0 && (
            <div className="border-t border-slate-100 bg-slate-50/70">
              <p className="px-4 pt-2 text-meta font-medium text-slate-500">已归档 · 不能作为关联目标</p>
              <ul className="divide-y divide-slate-100">
                {archivedHits.map((item) => {
                  const hint = hintOf(item.id)
                  const archivedFrom = hint?.archived_effective_from
                  const mergedInto = hint?.merged_into ?? []
                  return (
                    <li key={`archived-${item.id}`} className="px-4 py-2">
                      <p className="break-words text-sm text-slate-500">
                        {item.display_name} 已归档
                        {archivedFrom ? `（${formatDate(archivedFrom)}）` : ''}
                        {mergedInto.length > 0 ? `，已并入 ${mergedInto.join('、')}` : ''}
                      </p>
                      <p className="mt-0.5 text-meta leading-relaxed text-slate-400">
                        归档项不再接收新金额；金额若仍归同一个地方，请选它并入的在用项。
                      </p>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
        </div>

        <p className="border-t border-slate-100 px-4 py-2.5 text-meta text-slate-500">
          关联后本次金额写进该项，识别名记成它的别名；目标项的名称与历史记录都不改。
        </p>
      </div>
    </div>
  )
}

// --- Step Stepper ---
const PHASE_STEPS = [
  { label: '上传预览', icon: Upload },
  { label: '选择来源组', icon: Layers },
  { label: '批量解析', icon: Cpu },
  { label: '待录入清单', icon: FileText },
  { label: '确认写入', icon: Save },
]

function StepStepper({ phase }: { phase: Phase }) {
  const reached: number = {
    setup: 2,
    parsing: 3,
    review: 4,
    result: 5,
  }[phase]
  const CurrentIcon = PHASE_STEPS[reached - 1]?.icon

  return (
    <>
      {/* 桌面：完整五段胶囊（原样保留） */}
      <div className="hidden lg:flex items-center gap-1.5 flex-wrap">
        {PHASE_STEPS.map((s, i) => {
          const done = i < reached
          const isCurrent = i === reached - 1
          return (
            <Fragment key={i}>
              <div
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                  done
                    ? 'bg-brand-50 text-brand-700 border-brand-200'
                    : 'bg-white text-muted border-slate-200'
                }`}
              >
                {done && !isCurrent ? <CheckCircle2 size={13} className="text-brand-500" /> : <s.icon size={13} />}
                {s.label}
              </div>
              {i < PHASE_STEPS.length - 1 && (
                <div className={`h-px w-3 ${i < reached - 1 ? 'bg-brand-200' : 'bg-slate-200'}`} />
              )}
            </Fragment>
          )
        })}
      </div>
      {/* 移动：五段胶囊在 390px 要占三四行、把首屏让给步骤条；压成「当前步 + 进度条」一行 */}
      <div className="lg:hidden">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            {CurrentIcon && <CurrentIcon size={13} className="text-brand-600" />}
            步骤 {reached}/{PHASE_STEPS.length} · {PHASE_STEPS[reached - 1]?.label}
          </span>
          <span className="text-meta text-muted">{PHASE_STEPS[PHASE_STEPS.length - 1].label}</span>
        </div>
        <div className="mt-1.5 h-1.5 bg-slate-200 rounded-full overflow-hidden">
          <div
            className="h-full bg-brand-600 rounded-full transition-all duration-300"
            style={{ width: `${(reached / PHASE_STEPS.length) * 100}%` }}
          />
        </div>
      </div>
    </>
  )
}

// --- Evidence Panel (从原实现保留) ---
function EvidencePanel({ evidenceId }: { evidenceId: number }) {
  const [copied, setCopied] = useState(false)
  const { data, isLoading, isError } = useQuery({
    queryKey: ['screenshotEvidence', evidenceId],
    queryFn: () => api.getEvidence(evidenceId),
    enabled: !!evidenceId,
  })

  if (isLoading) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 p-6 text-sm text-slate-500 flex items-center gap-2">
        <Loader2 size={16} className="animate-spin text-brand-500" />
        正在加载凭证信息...
      </div>
    )
  }

  if (isError || !data) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 p-6 text-sm text-slate-500">
        凭证信息加载失败
      </div>
    )
  }

  const sha = data.file_sha256
  const shaShort = sha && sha.length > 20 ? `${sha.slice(0, 12)}…${sha.slice(-8)}` : sha

  const handleCopy = async () => {
    if (!sha) return
    try {
      await navigator.clipboard.writeText(sha)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="bg-white rounded-xl shadow-card overflow-hidden">
      <div className="px-6 py-4 border-b border-slate-100 flex items-center gap-3">
        <ShieldCheck size={20} className="text-brand-600" />
        <h3 className="font-semibold text-brand-950">凭证信息</h3>
      </div>
      <div className="px-6 py-4 space-y-3 text-sm">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-slate-500 mb-1">原图相对路径</p>
            <p className="font-mono text-xs text-slate-700 break-all bg-slate-50 rounded px-2 py-1.5">
              {data.file_path}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-500 mb-1">数据来源</p>
            <p className="text-slate-700">{data.source_group.label}</p>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-slate-500 mb-1">SHA256</p>
            <div className="flex items-center gap-2">
              <span title={sha} className="font-mono text-xs text-slate-600">{shaShort}</span>
              <button
                onClick={handleCopy}
                className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700"
              >
                <Copy size={13} />
                {copied ? '已复制' : '复制'}
              </button>
            </div>
          </div>
          <div>
            <p className="text-xs text-slate-500 mb-1">录入时间</p>
            <p className="text-slate-700">{formatDateTime(data.created_at)}</p>
          </div>
        </div>
      </div>
    </div>
  )
}

// --- 强制重新 OCR（忽略后端缓存，对单张截图重跑识别）---
// 典型场景：清单里某张图的 OCR 与 AI 校验对不上（分歧 / 仅 OCR / 仅 AI），怀疑是旧缓存或识别质量，
// 重新识别一遍再自动重校验，让差异视图刷新。AI 侧仍复用后端校验缓存，不额外改契约。

// 该图是否存在「会被重新识别丢弃」的用户编辑：改名 / 改金额 / 跳过 / 清0 / 不录入 / 规则备注 / 改数据日期 / 补录草稿。
// 有 → 点击时先确认（避免白改）；无 → 直接提交，不打断
function hasDiscardableEdits(img: ImgState): boolean {
  if (!img.preview) return false
  // 补录草稿来自旧一轮 AI 校验，重新识别后一并清空（见 applyPreviewResult）
  if ((img.supplements?.length ?? 0) > 0) return true
  // 数据日期初值口径与 applyPreviewResult 一致（重新识别后同样按新 preview 复位）
  const initDate = img.preview.suspected_date ?? img.preview.data_date ?? todayStr()
  if (img.selectedDate && img.selectedDate !== initDate) return true
  for (const r of img.preview.rows) {
    const e = img.rows[r.index]
    if (!e) continue
    if (e.skipped || e.zeroChecked || e.saveRename || e.saveIgnore || e.saveExclude) return true
    if (e.identityAction || e.identityTargetId != null || (e.identityReason ?? '').trim() || e.identityBlocked) return true
    if ((e.renameNote ?? '').trim() || (e.ignoreNote ?? '').trim() || (e.excludeNote ?? '').trim()) return true
    if (e.name.trim() !== (r.asset_name ?? r.source_name ?? '').trim()) return true
    if (e.amount !== r.source_amount) return true
  }
  return false
}

// 该图是否正在「强制重新 OCR」在途：进度条 / 清单锁定 / 入口禁用 / 写入让路都看这个
function isForceBusy(img: ImgState): boolean {
  return !!img.parsing && !!img.forceOcr
}

// 该图是否存在校验疑点（分歧 / 仅 OCR / 仅 AI / 校验失败）：疑点图把入口调成 amber 提醒，
// 沿用页内「疑点=amber」的既有配色语义（VerifySummaryBar / ImageCardHeader 同款）
function imgHasVerifyIssues(img: ImgState): boolean {
  if (img.verifyStatus === 'failed') return true
  if ((img.aiOnly?.length ?? 0) > 0) return true
  return (
    !!img.verifyByIndex &&
    Object.values(img.verifyByIndex).some((v) => v.verdict === 'mismatch' || v.verdict === 'ocr_only')
  )
}

// 行级疑点（移动端「只看疑点」过滤器用）：与 imgHasVerifyIssues 同一套判定口径下沉到行——
// 分歧 / 仅 OCR / 该图校验失败。一致行与未校验行不算疑点（默认采纳，不打扰）。
// 身份待裁决的行同样是「必须先处理」的疑点：移动端默认过滤时绝不能把它藏起来。
function rowHasIssue(r: ReviewRow): boolean {
  if (!!r.row.identity_guess && !r.edit.skipped) return true
  // 还没表态的新增行同样必须先处理：别让「只看疑点」把它藏起来
  if (createNeedsDecision(r)) return true
  if (r.img.verifyStatus === 'failed') return true
  const v = r.img.verifyByIndex?.[r.index]
  return !!v && (v.verdict === 'mismatch' || v.verdict === 'ocr_only')
}

// 入口小按钮：清单卡头部与「AI 额外识别」条共用同一个 handler（就近可达，不引入新视觉体系）
function ForceOcrButton({ busy, disabled, hasIssue, onClick }: {
  busy: boolean
  disabled?: boolean
  hasIssue?: boolean
  onClick: () => void
}) {
  const off = busy || !!disabled
  const cls = off
    ? 'border-slate-200 bg-white text-slate-300 cursor-not-allowed'
    : hasIssue
      ? 'border-amber-300 bg-amber-50/70 text-amber-700 hover:bg-amber-100/70'
      : 'border-slate-300 bg-white text-slate-600 hover:border-brand-400 hover:text-brand-700'
  return (
    <span className="inline-flex items-center gap-0.5">
      <button
        type="button"
        onClick={onClick}
        disabled={off}
        title={
          busy
            ? '正在忽略缓存重新识别这张截图'
            : `忽略 OCR 缓存，重新识别这张截图${hasIssue ? '（本图存在校验疑点）' : ''}；完成后清单自动刷新，当前编辑会被重置`
        }
        className={`inline-flex items-center gap-1 px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border transition-colors whitespace-nowrap ${cls}`}
      >
        {busy ? <Loader2 size={11} className="animate-spin" /> : <Cpu size={11} />}
        {busy ? '重新识别中' : '强制重新 OCR'}
      </button>
      <HintToggle
        hint={busy ? '正在忽略缓存重新识别这张截图。' : '忽略缓存重跑 OCR，当前编辑会被重置。'}
      />
    </span>
  )
}

// 逐图引擎切换（peer-entry-design §4a：批级默认 + 逐图覆盖，与 ForceOcrButton 对称）。
// 切换 = 重新 preview + 清采纳/补录草稿并提示（沿用 force 重解析先例）；
// 无可用供应商时「转用 AI」禁用并指路配置页（不进入后再报错）
function EngineToggleButton({ engine, aiAvailable, busy, disabled, onClick }: {
  engine: 'ocr' | 'ai'
  aiAvailable: boolean
  busy: boolean
  disabled?: boolean
  onClick: () => void
}) {
  const toAi = engine !== 'ai'
  const off = busy || !!disabled || (toAi && !aiAvailable)
  const cls = off
    ? 'border-slate-200 bg-white text-slate-300 cursor-not-allowed'
    : 'border-slate-300 bg-white text-slate-600 hover:border-brand-400 hover:text-brand-700'
  const title = toAi
    ? aiAvailable
      ? '切换为 AI 识别：跳过 OCR，由 AI 直接看原图生成识别清单（按图计费，同图同供应商命中缓存不重复计费）；识别项自动转为采纳草稿，确认后才写入。当前编辑与草稿会清空'
      : '暂无可用 LLM 供应商：请先到「数据管理 → LLM 供应商」配置后再使用 AI 识别'
    : '切换为 OCR 识别：重新跑 OCR 并按新结果刷新清单（命中缓存则很快）；当前编辑与采纳草稿会清空'
  return (
    <span className="inline-flex items-center gap-0.5">
      <button type="button" onClick={onClick} disabled={off} title={title}
        className={`inline-flex items-center gap-1 px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border transition-colors whitespace-nowrap ${cls}`}>
        {toAi ? <Bot size={11} /> : <Cpu size={11} />}
        {toAi ? '转用 AI 识别' : '转用 OCR'}
      </button>
      <HintToggle
        hint={
          toAi
            ? aiAvailable
              ? '切换为 AI：清空当前编辑与草稿，按图计费。'
              : '需先配置 LLM 供应商。'
            : '切换为 OCR：清空当前编辑与采纳草稿。'
        }
      />
    </span>
  )
}

// --- AI 补录（supplements）：把「AI 额外识别」项手动补进本次待写入清单 ---
// 定稿 .scratch/llm-verification/supplement-design.md。三条铁律：
// ① 草稿独立于 img.rows —— 行号通道（skipped/amounts/names）是唯一真源，绝不被补录位移；
// ② 写入前判定必须可见（将更新谁 / 将新增什么 / 为什么冲突），补录不是盲写；
// ③ apply 只发 {name, amount, side}，匹配与冲突由后端权威重算（判定只是给用户看的预览）。

let supplementCounter = 0
function createSupplementUid(): string {
  supplementCounter += 1
  return `sup_${Date.now()}_${supplementCounter}`
}

// AI 给的金额可能带千分位 / 货币符；解析不出或为 0 时返回空串让用户自己填（不静默写 0）
function normalizeSupplementAmount(raw: string | null | undefined): string {
  const s = (raw ?? '').replace(/[,，¥\s]/g, '')
  if (!s) return ''
  try {
    const d = new Decimal(s)
    return d.isZero() ? '' : d.toFixed(2)
  } catch {
    return ''
  }
}

// 契约 §3：amount 必须 >0 且 <1e13（Numeric(15,2)），否则后端整单 400 —— 前端先拦下来
function supplementAmountValid(amount: string): boolean {
  const s = (amount ?? '').trim()
  if (!s) return false
  try {
    const d = new Decimal(s)
    return d.gt(0) && d.lt(new Decimal('1e13'))
  } catch {
    return false
  }
}

// 会进 apply 载荷的草稿：有名字 + 金额合法 + 未被判定为冲突。
// 「未判定 / 判定失败」仍然发送：后端权威重算，冲突会 drop 且在结果页可见 —— 判定服务不可用不该堵住写入
function isSendableSupplement(d: SupplementDraft): boolean {
  return d.name.trim() !== '' && supplementAmountValid(d.amount) && d.resolved?.action !== 'conflict'
}

// 后端丢弃补录项的原因（契约 §2 SupplementDrop.reason）→ 中文
const SUPPLEMENT_DROP_REASON: Record<string, string> = {
  conflict_update: '与本次要更新的识别行是同一资产，已跳过（不重复计入）',
  conflict_create: '与本次要新增的行同名，已跳过（不重复计入）',
  conflict_zero: '与本次要清零的行冲突，已跳过',
  duplicate_target: '与本批更早的补录项指向同一资产，已跳过（先到先得，金额不叠加）',
  invalid: '内容不合法（名称为空或金额无效）',
}
function supplementDropText(reason: string): string {
  return SUPPLEMENT_DROP_REASON[reason] ?? `已跳过（${reason}）`
}

// 记账方向二态切换：默认资产，负债项由用户逐项切（契约 §8：不按名字猜）
function SideToggle({ value, disabled, onChange }: {
  value: SupplementSide
  disabled?: boolean
  onChange: (v: SupplementSide) => void
}) {
  const base = 'px-1.5 py-0.5 text-meta font-medium transition-colors disabled:cursor-not-allowed'
  const on = 'bg-brand-600 text-white'
  const off = 'bg-white text-slate-500 hover:bg-slate-50 disabled:hover:bg-white disabled:text-slate-300'
  return (
    <span
      className="inline-flex rounded-md border border-slate-200 overflow-hidden shrink-0"
      title="补录项的记账方向：默认按资产记账，负债项请手动切换（不按名字猜）"
    >
      <button type="button" disabled={disabled} onClick={() => onChange('asset')} className={`${base} ${value === 'asset' ? on : off}`}>
        资产
      </button>
      <button type="button" disabled={disabled} onClick={() => onChange('liability')} className={`${base} ${value === 'liability' ? on : off}`}>
        负债
      </button>
    </span>
  )
}

// 单条补录草稿：名称 / 金额可改，方向可切，判定结论就地可见，可移除
// 视觉与「机器认出」的蓝色 chip 区分开：补录是「我加的」，用 emerald（与清单里 create 行同色系）
function SupplementDraftRow({ draft, resolving, skippedCross, locked, onChange, onRemove, onRetry }: {
  draft: SupplementDraft
  resolving: boolean
  skippedCross: boolean
  locked?: boolean
  onChange: (patch: Partial<Pick<SupplementDraft, 'name' | 'amount' | 'side'>>) => void
  onRemove: () => void
  onRetry: () => void
}) {
  const name = draft.name.trim()
  const amountOk = supplementAmountValid(draft.amount)
  const conflict = draft.resolved?.action === 'conflict'
  const invalid = name === '' || !amountOk
  const chipBase = 'inline-block px-2 py-0.5 rounded text-xs whitespace-nowrap'
  let verdict: ReactNode
  if (invalid) {
    verdict = (
      <span className={`${chipBase} bg-red-100 text-red-700`} title="补录项内容不完整，不会写入">
        {name === '' ? '名称为空，不会写入' : '金额需大于 0，不会写入'}
      </span>
    )
  } else if (resolving) {
    verdict = (
      <span className="inline-flex items-center gap-1 text-xs text-slate-500 whitespace-nowrap">
        <Loader2 size={12} className="animate-spin" />
        判定中…
      </span>
    )
  } else if (draft.resolveError) {
    verdict = (
      <span className="inline-flex items-center gap-1.5 text-xs text-amber-700 whitespace-nowrap">
        <AlertTriangle size={12} className="shrink-0" />
        <span title={draft.resolveError}>判定失败，写入时由后端判定</span>
        {!locked && (
          <button type="button" onClick={onRetry} className="text-brand-600 hover:text-brand-700 hover:underline underline-offset-2">
            重试
          </button>
        )}
      </span>
    )
  } else if (!draft.resolved) {
    verdict = <span className={`${chipBase} bg-slate-100 text-slate-500`}>待判定</span>
  } else if (conflict) {
    verdict = (
      <span
        className={`${chipBase} bg-red-100 text-red-700`}
        title={draft.resolved?.conflict_reason || '与本次已决定写入的行冲突'}
      >
        不写入：{draft.resolved?.conflict_reason || '与本次已决定写入的行冲突'}
      </span>
    )
  } else if (draft.resolved.action === 'update') {
    verdict = (
      <span className={`${chipBase} bg-blue-100 text-blue-700`}>
        将更新 {draft.resolved.asset_name || draft.resolved.final_name || name}
        {draft.resolved.current_amount != null && `（现值 ${formatCurrency(draft.resolved.current_amount)}）`}
      </span>
    )
  } else {
    verdict = (
      <span className={`${chipBase} bg-emerald-100 text-emerald-700`}>
        将新增 {draft.resolved.final_name || name}
      </span>
    )
  }

  return (
    <div
      className={`flex items-center gap-2 flex-wrap rounded-lg border bg-white px-2.5 py-2 ${
        conflict || invalid ? 'border-red-200' : 'border-emerald-200'
      } ${locked ? 'opacity-60' : ''}`}
    >
      <Plus size={13} className="shrink-0 text-emerald-600" />
      <input
        type="text"
        value={draft.name}
        disabled={locked}
        onChange={(e) => onChange({ name: e.target.value })}
        placeholder="资产名称"
        className="w-40 px-2 py-1 text-base lg:text-sm text-slate-700 border border-slate-200 rounded-md focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors disabled:bg-slate-100 disabled:cursor-not-allowed"
      />
      <AmountInput value={draft.amount} disabled={locked} onChange={(v) => onChange({ amount: v })} />
      <SideToggle value={draft.side} disabled={locked} onChange={(v) => onChange({ side: v })} />
      {verdict}
      {skippedCross && !conflict && (
        <span
          className="inline-flex items-center gap-1 text-meta text-amber-700 whitespace-nowrap"
          title="同名的识别行你已勾了跳过：跳过的行不写入，这条补录会照常写入（后端看不出两者是同一项）。若本来就不想写这个资产，请移除"
        >
          <AlertTriangle size={11} className="shrink-0" />
          同名识别行已跳过，仍会写入
        </span>
      )}
      <button
        type="button"
        onClick={onRemove}
        disabled={locked}
        title="移除这条补录"
        className="ml-auto p-1 rounded text-slate-300 hover:text-red-500 transition-colors disabled:cursor-not-allowed"
      >
        <X size={14} />
      </button>
    </div>
  )
}

/**
 * 卡片头的小缩略图（review-image-viewer D1）：点开该图原图。
 * 视觉 28px，移动端命中区 44px；文件不在盘上时退化成图标，不留一块破图。
 */
function ImagePeekButton({
  src, label, onClick,
}: {
  src: string | null
  label: string
  onClick: () => void
}) {
  // 记「哪张图加载失败」而不是布尔：重新识别后地址变了，缩略图要能自己恢复
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const broken = !!src && failedSrc === src
  return (
    <button
      type="button"
      onClick={onClick}
      title={`查看原图：${label}`}
      aria-label={`查看原图：${label}`}
      className="group inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus lg:h-7 lg:w-7"
    >
      <span className="flex h-7 w-7 items-center justify-center overflow-hidden rounded-[6px] border border-line bg-subtle text-muted transition-colors group-hover:border-brand-400 group-hover:text-brand-500">
        {src && !broken ? (
          // eslint-disable-next-line @next/next/no-img-element -- 原图直出当缩略图：本页统一原生 img，不走 next/image
          <img src={src} alt="" onError={() => setFailedSrc(src)} className="h-full w-full object-cover object-top" />
        ) : (
          <FileImage size={13} aria-hidden="true" />
        )}
      </span>
    </button>
  )
}

// --- 每图「数据日期」确认控件 ---
// 需求：截图识别到疑似日期必须显式提示，用户在 截图日期/今天/自选 间确认，不再静默采用。
// 卡片顶部：检测到 suspected_date 时显示提示条（含命中原文 + 三个快捷动作）；
//   选中 == suspected → slate 弱化（已采用截图日期）；选中 != suspected → amber 提醒 + 日期控件旁「≠截图识别」。
// 头部行：原生 date input（可编辑，轻样式沿用页内 slate/brand 灰阶）。无 suspected_date → 不显示提示条，控件仍可改。
// 改日期只更新该图 selectedDate，不触发重新 preview（映射不依赖日期）。
// 头部右侧另挂「强制重新 OCR」入口（忽略后端缓存重跑该图识别）；在途时头部下方显示与批量解析同款进度条，
// 日期控件与该图清单一并锁定（完成后整体按新识别结果刷新，改了也会被复位）。
function ImageCardHeader({ imgId, imageLabel, groupLabel, rowCount, selected, suspected, suspectedText, today, onChangeDate, job, busy, hasIssue, forceDisabled, parseError, onForceReocr, onOpenLines, engine, aiAvailable, aiCount, engineSwitch, engineDisabled, onSwitchEngine, onOpenSettings, thumbSrc, onOpenImage, pageRole, onChangePageRole }: {
  imgId: string
  imageLabel: string
  groupLabel: string
  rowCount: number
  selected: string
  suspected?: string | null
  suspectedText?: string | null
  today: string
  onChangeDate: (imgId: string, date: string) => void
  /** 页角色备注（票 05 D2）：纯注释，不参与任何计算；识别后仍可改 */
  pageRole?: 'none' | 'main' | 'sub'
  onChangePageRole: (imgId: string, role: 'none' | 'main' | 'sub') => void
  // 强制重新 OCR：在途 job 快照 / 是否在途 / 本图是否有校验疑点 / 入口是否禁用 / 上次失败原因
  job?: PreviewJobStatus | null
  busy?: boolean
  hasIssue?: boolean
  forceDisabled?: boolean
  parseError?: string | null
  onForceReocr: (imgId: string) => void
  onOpenLines: (imgId: string) => void
  // 引擎切换（peer-entry-design §4a）：当前引擎 / AI 是否可用（有 enabled 供应商）/
  // AI 识别项数（头部计数）/ 是否切换引擎在途 / 切换入口禁用 / 切换 handler
  engine: 'ocr' | 'ai'
  aiAvailable: boolean
  aiCount?: number
  engineSwitch?: boolean
  engineDisabled?: boolean
  onSwitchEngine: (imgId: string) => void
  /** 移动端：打开「本图设置」底部抽屉（日期/引擎/重识别/原始行都收进去） */
  onOpenSettings?: (imgId: string) => void
  /** 卡片头缩略图地址：调用方给本地 blob（28px 小图不必再拉一遍原图）；缺省时退化成图标 */
  thumbSrc?: string | null
  /** 点缩略图 / 文件名查看该图原图（review-image-viewer D1） */
  onOpenImage: (imgId: string) => void
}) {
  // showPicker 在部分浏览器/lib 类型中缺失，交叉类型兜底；调用失败仅聚焦即可
  const inputRef = useRef<(HTMLInputElement & { showPicker?: () => void }) | null>(null)
  const hasSuspected = !!suspected
  const matches = hasSuspected && selected === suspected
  const focusPicker = () => {
    const el = inputRef.current
    if (!el) return
    el.focus()
    try { el.showPicker?.() } catch { /* 需用户手势的浏览器：聚焦已足够 */ }
  }
  // locked = 该图正在强制重新识别：日期与清单都会被新结果复位，期间不接受改动
  const locked = !!busy
  const bannerCls = matches
    ? 'bg-slate-50 border-slate-200 text-slate-500'
    : 'bg-amber-50 border-amber-200 text-amber-800'
  const btnCls = locked
    ? 'border-slate-200 bg-white text-slate-300 cursor-not-allowed'
    : matches
      ? 'border-slate-300 text-slate-600 hover:bg-white'
      : 'border-amber-300 text-amber-700 hover:bg-amber-100/60'
  // 移动端把「用截图日期 / 用今天 / 自选」三个日期按钮的命中区抬到 44px（原先约 20px，
  // 误触 = 财务数据写错账期）；桌面 py-0.5 原样保留。
  const btnBase = 'px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border transition-colors whitespace-nowrap'
  return (
    <>
      {/* 移动：标题行 + 「本图设置」入口。日期三按钮 / 引擎切换 / 强制重识别 / 原始行
          全部收进底部抽屉（见 ImageSettingsSheet），单行不再塞 8 类信息 */}
      <div className="lg:hidden px-4 py-2 bg-slate-50/70 border-b border-slate-100 flex items-center gap-2">
        <ImagePeekButton src={thumbSrc ?? null} label={imageLabel} onClick={() => onOpenImage(imgId)} />
        <button
          type="button"
          onClick={() => onOpenImage(imgId)}
          title={`查看原图：${imageLabel}`}
          className="min-w-0 flex-1 truncate text-left text-sm font-medium text-slate-700 hover:text-brand-700"
        >
          {imageLabel}
        </button>
        {parseError && <span className="shrink-0 text-meta text-red-500">识别失败</span>}
        {hasIssue && <span className="h-2 w-2 shrink-0 rounded-full bg-amber-400" aria-label="本图有校验疑点" />}
        {busy && (
          <span className="inline-flex shrink-0 items-center gap-1 text-meta text-brand-600">
            <Loader2 size={11} className="animate-spin" /> 锁定
          </span>
        )}
        <span className="shrink-0 text-meta text-muted">
          {engine === 'ai' ? `AI ${aiCount ?? 0} 项` : `${rowCount} 行`}
        </span>
        <button
          type="button"
          onClick={() => onOpenSettings?.(imgId)}
          className="inline-flex min-h-[44px] shrink-0 items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 transition-colors active:bg-slate-50"
        >
          <Settings2 size={14} /> 本图设置
        </button>
      </div>
      {hasSuspected && (
        <div className={`hidden lg:flex px-6 py-2 border-b text-xs items-center gap-2 flex-wrap ${bannerCls}`}>
          <CalendarDays size={13} className="shrink-0" />
          <span>
            截图识别到疑似日期 <span className="font-mono font-semibold">{suspected}</span>
          </span>
          {suspectedText && (
            <span className="text-meta opacity-70 truncate max-w-[15rem]" title={suspectedText}>
              「{suspectedText}」
            </span>
          )}
          <span className="ml-auto flex items-center gap-1.5">
            <button type="button" disabled={locked} onClick={() => onChangeDate(imgId, suspected as string)} className={`${btnBase} ${btnCls}`}>
              用截图日期
            </button>
            <button type="button" disabled={locked} onClick={() => onChangeDate(imgId, today)} className={`${btnBase} ${btnCls}`}>
              用今天
            </button>
            <button type="button" disabled={locked} onClick={focusPicker} className={`${btnBase} ${btnCls}`}>
              自选
            </button>
          </span>
        </div>
      )}
      <div className={`hidden lg:flex px-6 py-2.5 bg-slate-50/70 border-b border-slate-100 ${hasSuspected ? '' : 'border-t'} items-center gap-2 text-xs text-slate-500`}>
        <ImagePeekButton src={thumbSrc ?? null} label={imageLabel} onClick={() => onOpenImage(imgId)} />
        <button
          type="button"
          onClick={() => onOpenImage(imgId)}
          title={`查看原图：${imageLabel}`}
          className="min-w-0 truncate rounded text-left underline-offset-2 hover:text-brand-700 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        >
          {imageLabel}
        </button>
        {engine === 'ai' && (
          <span
            className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-brand-100 text-brand-700 text-meta font-medium whitespace-nowrap"
            title="本图由 AI 主识别：清单来自「AI 识别 → 采纳」通道，不套来源组校正规则"
          >
            <Bot size={10} /> AI 识别
          </span>
        )}
        <span className="text-slate-300">·</span>
        <span>{groupLabel}</span>
        <span className="text-slate-300">·</span>
        <label className="flex items-center gap-1">
          <span className="text-muted">数据日期</span>
          <input
            ref={inputRef}
            type="date"
            value={selected}
            disabled={locked}
            onChange={(e) => { const v = e.target.value; if (v) onChangeDate(imgId, v) }}
            className="px-1.5 py-0.5 text-base lg:text-xs font-mono text-slate-700 border border-slate-200 rounded-md bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors disabled:bg-slate-100 disabled:text-disabled disabled:cursor-not-allowed"
          />
          {hasSuspected && !matches && (
            <span className="text-meta text-amber-600 whitespace-nowrap" title={`截图识别为 ${suspected}，当前已改为 ${selected}`}>
              ≠截图识别
            </span>
          )}
        </label>
        <span className="text-slate-300">·</span>
        <PageRoleNote
          idSuffix={imgId}
          value={pageRole}
          onChange={(next) => onChangePageRole(imgId, next)}
        />
        <span className="ml-auto flex items-center gap-2 whitespace-nowrap">
          <EngineToggleButton
            engine={engine}
            aiAvailable={aiAvailable}
            busy={locked}
            disabled={engineDisabled}
            onClick={() => onSwitchEngine(imgId)}
          />
          {/* 「强制重新 OCR」仅对 OCR 引擎图有意义；AI 图的重试/转 OCR 入口在识别状态条与引擎切换里 */}
          {engine !== 'ai' && (
            <ForceOcrButton busy={locked} disabled={forceDisabled} hasIssue={hasIssue} onClick={() => onForceReocr(imgId)} />
          )}
          <span>{engine === 'ai' ? `AI 识别 ${aiCount ?? 0} 项` : `${rowCount} 行`}</span>
        </span>
      </div>

      {/* 强制重新识别 / 切换引擎在途：与批量解析同款进度呈现（排队 amber 文案 / 识别中片数行号 / 收尾不显示片数），
          可点开实时原始行弹窗；此间该图清单锁定，完成后整体刷新 */}
      {locked && (
        <div className="hidden lg:block px-6 py-2 border-b border-brand-100 bg-brand-50/50">
          <div className="flex items-center gap-2 text-xs text-brand-700">
            <Loader2 size={12} className="animate-spin shrink-0" />
            <span className="font-medium whitespace-nowrap">
              {engine === 'ai'
                ? 'AI 识别清单生成中…（完成后自动采纳为草稿）'
                : engineSwitch
                  ? '已切换为 OCR，重新识别中'
                  : '已忽略缓存，重新识别中'}
            </span>
            {job && <span className="text-slate-500 truncate">{jobStageText(job, true)}</span>}
            <span className="ml-auto flex items-center gap-2 whitespace-nowrap">
              <span className="text-muted">本图清单已锁定，完成后自动刷新</span>
              <button
                type="button"
                onClick={() => onOpenLines(imgId)}
                className="text-brand-600 hover:text-brand-700 hover:underline underline-offset-2 transition-colors"
              >
                查看已识别内容
              </button>
            </span>
          </div>
          {job && (
            <div className="mt-1.5 h-1.5 bg-brand-100 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-300 ${job.status === 'queued' ? 'bg-amber-400' : 'bg-brand-600'}`}
                style={{ width: `${jobPercent(job)}%` }}
              />
            </div>
          )}
        </div>
      )}

      {/* 重新识别失败：保留原识别结果，就地说明原因（不静默、不误报「未进入清单」） */}
      {parseError && (
        <div className="hidden lg:flex px-6 py-2 border-b border-red-100 bg-red-50/60 text-xs text-red-600 items-center gap-2">
          <AlertTriangle size={12} className="shrink-0" />
          <span className="min-w-0">重新识别失败：{parseError}（已保留原识别结果，可再次尝试）</span>
        </div>
      )}
    </>
  )
}

// --- 移动复核形态（<lg）：逐行卡片 + 本图设置抽屉 ---
// 桌面（≥lg）仍走原 6 列表格工作台，以下组件只渲染在 lg:hidden 分支里。
// 只改渲染形态：所有状态仍走页内既有的 updateRow / toggleRowSkip / toggleExclude /
// adoptAiValue 等 handler，行号通道与写入载荷逐字段不变。

const MOBILE_SHEET_BTN =
  'inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-lg border px-3 text-sm font-medium transition-colors'

/** 待录入清单的单行卡片：名称全宽 → 来源标记 → 金额与差异 → 底部大号操作（含汇总/归到） */
function MobileEntryCard({
  r,
  locked,
  applyPending,
  childCount,
  folded,
  parentLabel,
  diffItem,
  onFold,
  onUpdate,
  onToggleSkip,
  onToggleAggregate,
  onAdoptAi,
  onOpenBind,
  onClearBind,
  onOpenParent,
  onClearParent,
  onJumpSource,
}: {
  r: ReviewRow
  locked: boolean
  applyPending: boolean
  childCount: number
  folded: boolean
  parentLabel: string
  diffItem: ReconcileItem | null
  onFold: () => void
  onUpdate: (imgId: string, index: number, patch: Partial<RowEdit>) => void
  onToggleSkip: (imgId: string, index: number) => void
  onToggleAggregate: (imgId: string, index: number) => void
  onAdoptAi: (r: ReviewRow) => void
  onOpenBind: (r: ReviewRow) => void
  onClearBind: (r: ReviewRow) => void
  onOpenParent: () => void
  onClearParent: () => void
  onJumpSource: () => void
}) {
  const verdict = r.img.verifyByIndex?.[r.index]?.verdict
  return (
    <div
      className={`space-y-3 p-4 ${r.edit.skipped ? 'bg-slate-50 opacity-70' : ''} ${
        !r.edit.skipped && r.aggregate ? 'bg-slate-50/70' : ''
      } ${!r.edit.skipped && !r.aggregate && r.displayAction === 'create' ? 'bg-emerald-50/40' : ''}`}
    >
      {/* 名称（全宽、16px）+ 动作徽章（折叠按钮只对父项出现） */}
      <div className="flex items-start gap-2">
        {childCount > 0 && (
          <button
            type="button"
            onClick={onFold}
            aria-expanded={!folded}
            aria-label={`${folded ? '展开' : '折叠'} ${r.edit.name || '这一行'} 的子项`}
            className="-ml-1 mt-0.5 flex h-8 w-7 shrink-0 items-center justify-center rounded text-slate-400 active:bg-slate-100"
          >
            {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </button>
        )}
        <div className="min-w-0 flex-1">
          {r.nameEditable ? (
            <NameInput full value={r.edit.name} onChange={(v) => onUpdate(r.imgId, r.index, { name: v })} />
          ) : (
            <p className="text-base font-medium text-slate-700">{r.edit.name || '--'}</p>
          )}
          {r.nameEditable && r.row.source_name && r.row.source_name !== r.edit.name && (
            <p className="mt-0.5 text-xs text-muted">识别: {r.row.source_name}</p>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={onJumpSource}
              className="rounded-pill border border-slate-200 px-1.5 text-meta text-muted active:bg-slate-50"
            >
              图 {r.imageOrdinal} · {r.imageLabel}
            </button>
            {r.aggregate && (
              <span className="text-meta text-slate-500">
                汇总项 · 不计入合计
                {r.edit.aggregateSource ? `（${AGGREGATE_SOURCE_LABEL[r.edit.aggregateSource]}）` : ''}
              </span>
            )}
          </div>
        </div>
        {/* 行尾开关（票 07 D2/D5）：与桌面同款图标 + sr-only 全称，触屏目标 44px */}
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            data-row-toggle="skip"
            data-active={r.edit.skipped ? 'true' : 'false'}
            aria-pressed={r.edit.skipped}
            title={r.edit.skipped ? '跳过本行（本次不录入）：点击恢复' : '跳过本行：本次不录入'}
            onClick={() => onToggleSkip(r.imgId, r.index)}
            className={`flex h-9 w-9 items-center justify-center rounded ${
              r.edit.skipped ? 'bg-slate-200 text-slate-700' : 'text-slate-300 active:bg-slate-100'
            }`}
          >
            <ListX size={16} />
            <span className="sr-only">跳过</span>
          </button>
          <button
            type="button"
            data-row-toggle="aggregate"
            data-active={r.aggregate ? 'true' : 'false'}
            aria-pressed={r.aggregate}
            title="汇总行：只核对，不入账"
            onClick={() => onToggleAggregate(r.imgId, r.index)}
            className={`flex h-9 w-9 items-center justify-center rounded ${
              r.aggregate ? 'bg-brand-100 text-brand-700' : 'text-slate-300 active:bg-slate-100'
            }`}
          >
            <Layers size={16} />
            <span className="sr-only">汇总行</span>
          </button>
        </div>
        <ActionBadge action={r.displayAction} bound={bindTargetOf(r) != null} aggregate={r.aggregate} />
      </div>

      {/* 来源金额 与 差异并排 */}
      <div className="flex items-center justify-between gap-2 text-sm">
        <span className="text-slate-500">
          来源 <span className="font-mono text-slate-700">{formatCurrency(r.row.source_amount ?? '0')}</span>
        </span>
        <span className={`font-mono ${diffColor(r.diff)}`}>差异 {formatSignedCurrency(r.diff)}</span>
      </div>

      {/* 金额输入（全宽，汇总行置灰）+ 校验结论 */}
      <div className="space-y-2">
        <div className={r.aggregate ? 'opacity-60' : ''}>
          <AmountInput
            full
            value={r.amount}
            disabled={locked || r.displayAction === 'zero' || r.displayAction === 'keep'}
            onChange={(v) => onUpdate(r.imgId, r.index, { amount: v })}
          />
        </div>
        {/* 汇总行差额（票 06 D6）：窄屏同款标记，位置在金额输入下方 */}
        {diffItem && (
          <div className="flex justify-end">
            <AggregateDiffChip
              item={diffItem}
              onGo={() => document.getElementById('reconcile-panel')?.scrollIntoView({ block: 'center' })}
            />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <VerifyBadge
            v={r.img.verifyByIndex?.[r.index]}
            ocrName={r.row.source_name}
            ocrAmount={r.row.source_amount}
            status={r.img.verifyStatus}
            error={r.img.verifyError}
          />
          {verdict === 'mismatch' && r.writable && (
            <button
              type="button"
              disabled={applyPending || locked}
              onClick={() => onAdoptAi(r)}
              className="inline-flex min-h-[44px] items-center gap-1 rounded-lg border border-brand-200 bg-brand-50 px-2.5 text-xs font-medium text-brand-700 active:bg-brand-100 disabled:opacity-50"
            >
              <Bot size={13} /> 采纳 AI 值
            </button>
          )}
        </div>
      </div>

      {/* 身份裁决（D4）：疑似改名/拆分 → 行内提示 + 三选一。汇总行不落账，故不提供裁决入口 */}
      {r.row.identity_guess && !r.aggregate && (
        <IdentityGuessNotice
          r={r}
          locked={locked}
          surface="mobile"
          onUpdate={(patch) => onUpdate(r.imgId, r.index, patch)}
        />
      )}

      {/* 关联到已有项（票 01）：新增行的另一条表态路径 —— 这行其实是某个已有项 */}
      {r.row.action === 'create' && !r.edit.skipped && !r.aggregate && (
        <>
          <RowBindControl r={r} locked={locked} onOpen={onOpenBind} onClear={onClearBind} />
          {/* 该名历史上有 merge 去向（spec D3）：后端已按 create 行下发，这里补一句「本来归谁」 */}
          <ArchivedMergedIntoNotice r={r} />
        </>
      )}

      {/* 底部操作：归到…（≥44px，触屏可点）。跳过/汇总行已搬到名称行尾（票 07 D5） */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 pt-2">
        {r.parent ? (
          <span className="inline-flex min-h-[44px] items-center gap-1">
            <span className="max-w-[10rem] truncate rounded-pill border border-brand-100 bg-brand-50 px-2 text-meta text-brand-700">
              ↳ {parentLabel}
            </span>
            <button
              type="button"
              onClick={onClearParent}
              aria-label={`取消「${r.edit.name || '这一行'}」的归属`}
              className="flex h-8 w-8 items-center justify-center rounded-full text-slate-400 active:bg-slate-100"
            >
              <X size={14} />
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={onOpenParent}
            disabled={applyPending || locked}
            className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-200 bg-white px-2.5 text-sm text-slate-600 active:bg-slate-50 disabled:opacity-50"
          >
            归到…
          </button>
        )}
        {r.edit.skipped && ignoreRuleEligible(r) && (
          <RuleToggle
            title={`将「${ignoreRuleMatch(r)}」保存为该来源组的忽略规则，下次导入自动丢弃`}
            hint="下次导入自动丢弃匹配行。"
            label="存为忽略"
            checked={r.edit.saveIgnore}
            onChange={() => onUpdate(r.imgId, r.index, { saveIgnore: !r.edit.saveIgnore })}
          />
        )}
        {renameRuleEligible(r) && (
          <RuleToggle
            title={`将「${r.row.source_name}」→「${r.edit.name}」保存为该来源组的改名规则，下次导入自动套用`}
            label="存为规则"
            checked={r.edit.saveRename}
            onChange={() => onUpdate(r.imgId, r.index, { saveRename: !r.edit.saveRename })}
          />
        )}
      </div>
      {r.edit.skipped && r.edit.saveIgnore && (
        <RuleNoteInput
          value={r.edit.ignoreNote ?? ''}
          onChange={(v) => onUpdate(r.imgId, r.index, { ignoreNote: v })}
          placeholder="备注为什么忽略，如：总金额项"
        />
      )}
      {r.edit.saveRename && (
        <RuleNoteInput
          value={r.edit.renameNote ?? ''}
          onChange={(v) => onUpdate(r.imgId, r.index, { renameNote: v })}
          placeholder="备注为什么改名（可选）"
        />
      )}
    </div>
  )
}

/** 未识别项的单行卡片：名称/当前金额/动作 + 不录入（+ 策略相关的 清0 / 跳过） */
function MobileKeepCard({
  r,
  policy,
  onUpdate,
  onToggleSkip,
  onToggleZero,
  onToggleExclude,
}: {
  r: ReviewRow
  policy: UnrecognizedPolicy
  onUpdate: (imgId: string, index: number, patch: Partial<RowEdit>) => void
  onToggleSkip: (imgId: string, index: number) => void
  onToggleZero: (imgId: string, index: number) => void
  onToggleExclude: (imgId: string, index: number) => void
}) {
  return (
    <div className={`space-y-3 p-4 ${r.edit.skipped ? 'bg-slate-50 opacity-70' : ''}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-base font-medium text-slate-700">{r.edit.name || '--'}</p>
          {r.isZeroed && <p className="mt-0.5 text-xs text-amber-600">将清零</p>}
        </div>
        <div className="shrink-0 text-right">
          <p className="font-mono text-sm text-slate-700">{formatCurrency(r.row.current_amount || '0')}</p>
          <ActionBadge action={r.displayAction} />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 pt-2">
        {policy === 'zeroEach' && (
          <label className="inline-flex min-h-[44px] items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={r.isZeroed || r.edit.zeroChecked}
              onChange={() => onToggleZero(r.imgId, r.index)}
              className="h-5 w-5 accent-amber-600 cursor-pointer"
            />
            清0
          </label>
        )}
        {policy !== 'keep' && (
          <label className="inline-flex min-h-[44px] items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={r.edit.skipped}
              onChange={() => onToggleSkip(r.imgId, r.index)}
              className="h-5 w-5 accent-brand-600 cursor-pointer"
            />
            跳过
          </label>
        )}
        <RuleToggle
          title={
            r.edit.skipped
              ? '已跳过的行本次不写入，无法设为不录入；如需排除请先取消跳过'
              : '本次写入时清 0，且以后不再出现在未识别列表；适合已改名 / 已清仓的项目（规则可在 设置 · 来源管理 删除恢复）'
          }
          label="不录入"
          hint={
            r.edit.skipped
              ? '已跳过的行不能设为不录入，先取消跳过。'
              : '本次清 0 且以后不再出现；可在来源管理恢复。'
          }
          checked={!!r.edit.saveExclude}
          disabled={r.edit.skipped}
          onChange={() => onToggleExclude(r.imgId, r.index)}
        />
      </div>
      {r.edit.skipped && r.edit.saveIgnore && ignoreRuleEligible(r) && (
        <RuleNoteInput
          value={r.edit.ignoreNote ?? ''}
          onChange={(v) => onUpdate(r.imgId, r.index, { ignoreNote: v })}
          placeholder="备注为什么忽略，如：总金额项"
        />
      )}
      {r.edit.saveExclude && !r.edit.skipped && (
        <RuleNoteInput
          value={r.edit.excludeNote ?? ''}
          onChange={(v) => onUpdate(r.imgId, r.index, { excludeNote: v })}
          placeholder="备注：已改名→xx / 已清仓"
        />
      )}
    </div>
  )
}

/** 「本图设置」底部抽屉：把图卡头单行塞不下的日期/引擎/重识别/原始行收进来 */
function ImageSettingsSheet({
  imgId,
  imageLabel,
  groupLabel,
  rowCount,
  selected,
  suspected,
  suspectedText,
  today,
  onChangeDate,
  busy,
  hasIssue,
  forceDisabled,
  parseError,
  onForceReocr,
  onOpenLines,
  engine,
  aiAvailable,
  engineDisabled,
  onSwitchEngine,
  onClose,
  pageRole,
  onChangePageRole,
}: {
  imgId: string
  imageLabel: string
  groupLabel: string
  rowCount: number
  selected: string
  suspected?: string | null
  suspectedText?: string | null
  today: string
  onChangeDate: (imgId: string, date: string) => void
  busy: boolean
  hasIssue: boolean
  forceDisabled: boolean
  parseError?: string | null
  onForceReocr: (imgId: string) => void
  onOpenLines: (imgId: string) => void
  engine: 'ocr' | 'ai'
  aiAvailable: boolean
  engineDisabled: boolean
  onSwitchEngine: (imgId: string) => void
  onClose: () => void
  pageRole?: 'none' | 'main' | 'sub'
  onChangePageRole: (imgId: string, role: 'none' | 'main' | 'sub') => void
}) {
  const inputRef = useRef<(HTMLInputElement & { showPicker?: () => void }) | null>(null)
  const focusPicker = () => {
    const el = inputRef.current
    if (!el) return
    el.focus()
    try { el.showPicker?.() } catch { /* 需用户手势的浏览器：聚焦已足够 */ }
  }
  const locked = !!busy
  const dateBtn = (active: boolean) =>
    `${MOBILE_SHEET_BTN} ${
      locked
        ? 'border-slate-200 bg-white text-slate-300 cursor-not-allowed'
        : active
          ? 'border-amber-300 bg-amber-50 text-amber-700'
          : 'border-slate-300 bg-white text-slate-600 active:bg-slate-50'
    }`
  return (
    <div className="lg:hidden fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="本图设置">
      <div className="absolute inset-0 bg-black/40 animate-fade-in" onClick={onClose} aria-hidden="true" />
      <div className="absolute inset-x-0 bottom-0 max-h-[85vh] overflow-y-auto rounded-t-2xl bg-white pb-[env(safe-area-inset-bottom)] shadow-2xl animate-slide-up">
        <div className="sticky top-0 flex items-center justify-between gap-2 border-b border-slate-100 bg-white px-4 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-brand-950">{imageLabel}</p>
            <p className="truncate text-xs text-muted">
              {groupLabel} · {engine === 'ai' ? `AI 识别` : `${rowCount} 行`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭本图设置"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-muted active:bg-slate-100"
          >
            <X size={20} />
          </button>
        </div>

        <div className="space-y-5 px-4 py-4">
          {/* 数据日期 */}
          <section className="space-y-2">
            <p className="text-xs font-medium text-slate-500">数据日期</p>
            {suspected && (
              <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                截图识别到疑似日期 <span className="font-mono font-semibold">{suspected}</span>
                {suspectedText && <span className="opacity-70"> 「{suspectedText}」</span>}
              </p>
            )}
            {suspected && (
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={locked} onClick={() => onChangeDate(imgId, suspected as string)} className={dateBtn(selected === suspected)}>
                  用截图日期
                </button>
                <button type="button" disabled={locked} onClick={() => onChangeDate(imgId, today)} className={dateBtn(selected === today)}>
                  用今天
                </button>
                <button type="button" disabled={locked} onClick={focusPicker} className={dateBtn(false)}>
                  自选
                </button>
              </div>
            )}
            <label className="flex min-h-[44px] items-center gap-2">
              <span className="shrink-0 text-sm text-slate-600">写入日期</span>
              <input
                ref={inputRef}
                type="date"
                value={selected}
                disabled={locked}
                onChange={(e) => { const v = e.target.value; if (v) onChangeDate(imgId, v) }}
                className="rounded-md border border-slate-200 bg-white px-2 py-1.5 font-mono text-base text-slate-700 focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 disabled:bg-slate-100 disabled:text-disabled"
              />
            </label>
          </section>

          {/* 识别引擎 */}
          <section className="space-y-2">
            <p className="text-xs font-medium text-slate-500">识别引擎</p>
            <EngineToggleButton
              engine={engine}
              aiAvailable={aiAvailable}
              busy={locked}
              disabled={engineDisabled}
              onClick={() => onSwitchEngine(imgId)}
            />
          </section>

          {/* 页角色（纯注释）：放在设置里而不是行上——它不参与任何计算，
              放在清单里只会被误当成一种「计入/不计入」开关 */}
          <section className="space-y-1">
            <p className="text-xs font-medium text-slate-500">页角色（仅作注释）</p>
            <PageRoleNote
              idSuffix={`sheet-${imgId}`}
              value={pageRole}
              onChange={(next) => onChangePageRole(imgId, next)}
            />
            <p className="text-meta leading-relaxed text-muted">
              标注不改变这一页任何一行的判定、核对差额与写入合计，只是方便日后按页排查。
            </p>
          </section>

          {/* 重新识别 / 原始行 */}
          <section className="space-y-2">
            <p className="text-xs font-medium text-slate-500">识别与核对</p>
            <div className="flex flex-wrap gap-2">
              {engine !== 'ai' && (
                <ForceOcrButton busy={locked} disabled={forceDisabled} hasIssue={hasIssue} onClick={() => onForceReocr(imgId)} />
              )}
              <button
                type="button"
                onClick={() => onOpenLines(imgId)}
                className={`${MOBILE_SHEET_BTN} border-slate-200 bg-white text-slate-600 active:bg-slate-50`}
              >
                查看已识别内容
              </button>
            </div>
          </section>

          {parseError && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">重新识别失败：{parseError}（已保留原识别结果）</p>
          )}
          {locked && (
            <p className="rounded-lg bg-brand-50 px-3 py-2 text-xs text-brand-700">本图正在重新识别，清单锁定，完成后自动刷新</p>
          )}
        </div>
      </div>
    </div>
  )
}

// --- Main Page ---
// useSearchParams 在静态预渲染时需 Suspense 包裹（App Router CSR bailout），页面逻辑放在内层组件
export default function ScreenshotPage() {
  return (
    <Suspense fallback={null}>
      <ScreenshotPageContent />
    </Suspense>
  )
}

function ScreenshotPageContent() {
  const queryClient = useQueryClient()

  const [phase, setPhase] = useState<Phase>('setup')
  const [images, setImages] = useState<ImgState[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [unrecognizedPolicy, setUnrecognizedPolicy] = useState<UnrecognizedPolicy>('keep')
  // 移动复核流程（<lg）：第一屏「合计对不对」→ 第二屏「逐行核对」。桌面不受影响，一次铺开。
  const [mobileReviewStep, setMobileReviewStep] = useState<'totals' | 'rows'>('totals')
  // 移动 review 工具条：批量日期/供应商/自动校验 收进「批量操作」折叠区（默认收起）
  const [mobileBatchOpen, setMobileBatchOpen] = useState(false)
  // 「只看疑点」：移动端默认全部采纳、只把校验判为疑点的行挑出来处理（注意力模型显式化）。
  // 默认开启；无疑点时清单显示「其余行默认采纳」，用户仍可一键看全部。桌面不受此状态影响。
  const [mobileOnlyIssues, setMobileOnlyIssues] = useState(true)
  // 「本图设置」底部抽屉当前打开的图（null = 关闭）
  const [settingsImgId, setSettingsImgId] = useState<string | null>(null)
  // 原图查看器当前下标（null = 关闭）；下标对应 viewerImages，与清单卡片同序
  const [viewerIndex, setViewerIndex] = useState<number | null>(null)
  const [applyResults, setApplyResults] = useState<ScreenshotApplyResult[]>([])
  // 400 identity_decision_required 拦下的图（部分成功时在结果页也要可见，不能让人以为全写完了）
  const [applyIdentityBlocked, setApplyIdentityBlocked] = useState<{ filename: string; count: number }[]>([])
  const [applyPending, setApplyPending] = useState(false)
  const [applyProgress, setApplyProgress] = useState(0)
  const [applyTotal, setApplyTotal] = useState(0)
  const [parseProgress, setParseProgress] = useState(0)
  const [parseTotal, setParseTotal] = useState(0)
  // --- 总表 / 归父 / 核对（ADR-0029，票 05/09） ---
  // 按图筛选（'all' = 全部）；分表退化成筛选结果，默认全部
  const [imageFilter, setImageFilter] = useState<'all' | string>('all')
  // 折叠的父行（键＝行键）；默认全部展开：折叠是用户的选择，不该由页面替他做
  const [foldedRows, setFoldedRows] = useState<Record<RowKey, boolean>>({})
  // 「归到…」选择器当前服务的行（null = 关闭）
  const [parentPickerKey, setParentPickerKey] = useState<RowKey | null>(null)
  // 核对面板展开态（桌面默认展开；窄屏默认收起，点底部一条打开抽屉）
  const [reconcileOpen, setReconcileOpen] = useState(true)
  // 接受差额的留痕：键＝父项 id（`row:<行键>` / `item:<id>`）。
  // 存页面状态而不是行状态：父项可能是既有资产项（没有对应行）。
  const [acceptedDiffs, setAcceptedDiffs] = useState<Record<string, AcceptedMismatch>>({})
  // 拖拽会话（加速器；指针事件，只由行首手柄发起）
  const dragSessionRef = useRef<{
    childKey: RowKey
    startX: number
    startY: number
    started: boolean
    targetKey: RowKey | null
    ok: boolean
  } | null>(null)
  const [dragView, setDragView] = useState<{ childKey: RowKey; targetKey: RowKey | null; ok: boolean } | null>(null)
  // 批次编排状态（ADR-0029 D8/D9）：建壳成功后逐图 apply 复用同一个 batch_id，
  // 重试也复用它——失败重试若换批次，成员行会散在两个批次里，finalize 永远等不齐
  const batchPlanRef = useRef<{ id: number; expected: number } | null>(null)
  // 本批已写入成功的图（跨重试累计）：决定「还差哪几张」，也避免重复 apply 已成功的图
  const appliedOkRef = useRef<Set<string>>(new Set())
  // 已写入的 apply 结果（跨重试累计；结果页一次展示，不因重试丢掉前一批的回执）
  const applyResultsRef = useRef<ScreenshotApplyResult[]>([])
  // 本次动作里写入失败的图（不 finalize；「重试」只重发这几张，沿用同一批次与幂等键）
  const [failedApply, setFailedApply] = useState<{ imgIds: string[]; labels: string[] } | null>(null)
  // 结算状态：写入成功但 finalize 失败时，结果页要能看到原因并只重试结算（绝不重发 apply）
  const [finalizeState, setFinalizeState] = useState<
    { status: 'ok' | 'failed'; message: string | null; batchId: number | null } | null
  >(null)

  // --- 异步 OCR 轮询生命周期 ---
  // 批次序号守卫：startParse 重启 / resetAll / 卸载时递增，旧批次在途轮询一律作废
  const batchSeqRef = useRef(0)
  const pollTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const pollFailsRef = useRef(0) // 连续网络失败计数（任一成功即清零）
  const batchRef = useRef({ total: 0, done: 0, success: 0 })
  // 连续 3 次轮询失败 → 降级提示（但不停止，退避重试）
  const [pollDegraded, setPollDegraded] = useState(false)

  const clearPollTimers = useCallback(() => {
    for (const t of pollTimersRef.current.values()) clearTimeout(t)
    pollTimersRef.current.clear()
  }, [])

  // --- 「强制重新 OCR」轮询通道（单图，独立于批次）---
  // 单图重跑发生在 review 阶段，不能走批次 seq/timers（会被 startParse 的批次计数与 phase 切换搅乱），
  // 这里用 jobId → timer 的独立表；离开 review（返回修改 / 重新开始）或卸载时整体作废
  const forceTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const clearForceTimers = useCallback(() => {
    for (const t of forceTimersRef.current.values()) clearTimeout(t)
    forceTimersRef.current.clear()
  }, [])
  // 作废在途强制重跑/引擎切换：清定时器 + 把仍处于单图重跑态的图复位（保留原 preview 与编辑，
  // 不误标失败）；切换引擎在途的图回滚到切换前引擎（旧 preview 是旧引擎的产物，守卫口径要一致）
  const cancelForceReocr = useCallback(() => {
    clearForceTimers()
    setImages((prev) =>
      prev.map((p) =>
        isForceBusy(p)
          ? {
              ...p,
              parsing: false,
              forceOcr: false,
              job: null,
              engineSwitch: false,
              ...(p.engineBeforeSwitch ? { engine: p.engineBeforeSwitch } : {}),
              engineBeforeSwitch: undefined,
            }
          : p,
      ),
    )
  }, [clearForceTimers])

  // 最新 images 镜像：强制重跑的轮询回调在数分钟后才落地，闭包里的 images 已过期，
  // 完成时要靠它取到该图当前状态（file / sourceGroup / 校验态）再决定是否自动重校验
  const imagesRef = useRef<ImgState[]>(images)
  useEffect(() => {
    imagesRef.current = images
  }, [images])

  // 补录判定（resolve）防抖定时器：改名称/金额/方向后 600ms 合并成一次批量判定
  const supDebounceRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const clearSupDebounce = useCallback(() => {
    for (const t of supDebounceRef.current.values()) clearTimeout(t)
    supDebounceRef.current.clear()
  }, [])
  // 每图判定序号：编辑/重新识别后回来的旧响应一律丢弃，绝不把过期结论盖到当前草稿上
  const supSeqRef = useRef<Record<string, number>>({})
  // 已就「与跳过行同名」提醒过的草稿 uid：提醒一次即可，不反复弹
  const supSkipWarnedRef = useRef<Set<string>>(new Set())
  // v5 契约层：apply 的每图幂等键。超时/断连后用户重点击＝同键重放（防双写，
  // 对应下方「勿连续重试」告警场景）；确认成功后清除，下次录入是全新动作。
  const applyIdemRef = useRef<Map<string, string>>(new Map())

  // 卸载时清定时器并作废批次（在途 fetch 返回后由 seq 守卫丢弃，不再 setState/续排）
  useEffect(
    () => () => {
      batchSeqRef.current += 1
      for (const t of pollTimersRef.current.values()) clearTimeout(t)
      pollTimersRef.current.clear()
      for (const t of forceTimersRef.current.values()) clearTimeout(t)
      forceTimersRef.current.clear()
      for (const t of supDebounceRef.current.values()) clearTimeout(t)
      supDebounceRef.current.clear()
    },
    [],
  )

  // AI 校验（第二意见，不参与写入）
  const [verifying, setVerifying] = useState(false)
  const [verifyProgress, setVerifyProgress] = useState<{ done: number; total: number }>({ done: 0, total: 0 })
  const [autoVerify, setAutoVerify] = useState(false)
  const autoVerifyRanRef = useRef(false)
  // 临时供应商选择（User Story 15）：默认选中 is_default 的 enabled 供应商，可临时切换；
  // 仅影响 verify 请求携带的 provider_id，不落库、不影响写入流
  const [providerId, setProviderId] = useState<number | null>(null)

  // 自动校验偏好：挂载时读 localStorage，变更时写回（默认关）
  useEffect(() => {
    setAutoVerify(localStorage.getItem(AUTO_VERIFY_KEY) === '1')
  }, [])
  useEffect(() => {
    localStorage.setItem(AUTO_VERIFY_KEY, autoVerify ? '1' : '0')
  }, [autoVerify])

  // 轻提示（规则保存反馈，自动消失）
  const [ruleNotice, setRuleNotice] = useState<{ kind: 'success' | 'info' | 'error'; text: string } | null>(null)
  const ruleNoticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pushRuleNotice = useCallback((kind: 'success' | 'info' | 'error', text: string) => {
    setRuleNotice({ kind, text })
    if (ruleNoticeTimer.current) clearTimeout(ruleNoticeTimer.current)
    ruleNoticeTimer.current = setTimeout(() => setRuleNotice(null), 3600)
  }, [])

  // --- 实时原始 OCR 行弹窗：记 imgId（多图各开各的）；数据来自既有轮询写入的 job 快照，不加新轮询 ---
  const [linesFor, setLinesFor] = useState<string | null>(null)
  // done 后 job 置 null：把最近一次非空 lines 定格在 ref，弹窗内容不随之消失
  const lastLinesRef = useRef<{ imgId: string; lines: PreviewJobLine[] } | null>(null)
  const linesBoxRef = useRef<HTMLDivElement | null>(null)
  // tail -f 式跟随滚动：用户上翻离开底部即暂停跟随，回到底部恢复
  const linesFollowRef = useRef(true)

  const openLinesModal = useCallback((imgId: string) => {
    linesFollowRef.current = true
    setLinesFor(imgId)
  }, [])

  const linesImg = linesFor ? (images.find((p) => p.id === linesFor) ?? null) : null
  const linesJob = linesImg?.job ?? null
  // 仍在识别中 = 该行 parsing 且 job 处于 queued/running（done/error/置空后为定格态）
  const linesLive = !!linesImg?.parsing && (linesJob?.status === 'queued' || linesJob?.status === 'running')
  const shownLines: PreviewJobLine[] =
    (linesJob?.lines?.length ? linesJob.lines : null) ??
    (lastLinesRef.current?.imgId === linesFor ? lastLinesRef.current.lines : null) ??
    []
  const shownLinesCount = shownLines.length

  // 弹窗打开期间持续把最新非空 lines 存入 ref（job 置空后作为定格数据）
  useEffect(() => {
    if (!linesFor) return
    const img = images.find((p) => p.id === linesFor)
    if (img?.job?.lines?.length) lastLinesRef.current = { imgId: img.id, lines: img.job.lines }
  }, [linesFor, images])

  // 新行到达时滚动到底部（仅在跟随态）
  useEffect(() => {
    const box = linesBoxRef.current
    if (linesFor && box && linesFollowRef.current) box.scrollTop = box.scrollHeight
  }, [linesFor, shownLinesCount])

  // 来源组列表（CRUD API，本页只读取用于下拉）
  const { data: sourceGroups = [], isPending: groupsLoading } = useQuery<SourceGroup[]>({
    queryKey: ['source-groups'],
    queryFn: api.getSourceGroups,
  })

  // LLM 供应商列表（只读，供校验前临时选择）：仅列 enabled；默认选中 is_default（否则第一个）
  const { data: llmProviders = [] } = useQuery<LlmProvider[]>({
    queryKey: ['llm-providers'],
    queryFn: api.getLlmProviders,
  })
  const enabledProviders = useMemo(() => llmProviders.filter((p) => p.enabled), [llmProviders])
  useEffect(() => {
    if (!enabledProviders.length) return
    setProviderId((prev) => {
      // 保留用户已选且仍可用的；否则回落到默认供应商
      if (prev != null && enabledProviders.some((p) => p.id === prev)) return prev
      return (enabledProviders.find((p) => p.is_default) ?? enabledProviders[0]).id
    })
  }, [enabledProviders])

  // --- 识别引擎（peer-entry-design §4a/§4b）：批级默认 + 逐图覆盖 ---
  // 默认 OCR（本地、免费、有缓存）；AI 每图真金白银 + 网络，只在用户显式选择时启用。
  // 无可用供应商：AI 选项直接禁用 + 指路配置页；已选 AI 时自动回落 OCR（不进入后再报错）
  const [defaultEngine, setDefaultEngine] = useState<'ocr' | 'ai'>('ocr')
  const defaultEngineRef = useRef(defaultEngine)
  useEffect(() => {
    defaultEngineRef.current = defaultEngine
  }, [defaultEngine])
  const aiAvailable = enabledProviders.length > 0
  useEffect(() => {
    if (!aiAvailable) setDefaultEngine('ocr')
  }, [aiAvailable])

  // AI 识别编排（runAiRecognition）定义在 verifyOne/addSupplements 之后；批量解析的轮询回调
  // 通过 ref 间接调用，避免声明顺序成环（同 imagesRef 先例）
  const runAiRecognitionRef = useRef<((imgId: string, preview: ScreenshotPreviewResponse) => Promise<void>) | null>(null)

  // 可选渠道 = 启用组（后端已按 sort_order 排序）；slug 有唯一约束，无需再去重
  const groupOptions = useMemo(
    () =>
      sourceGroups
        .filter((g) => g.enabled)
        .map((g) => ({ slug: g.slug, label: g.label })),
    [sourceGroups],
  )

  const labelFor = useCallback(
    (slug: string) => groupOptions.find((o) => o.slug === slug)?.label ?? slug,
    [groupOptions],
  )

  const groupSlugsRef = useRef<Set<string>>(new Set(groupOptions.map((o) => o.slug)))
  useEffect(() => {
    groupSlugsRef.current = new Set(groupOptions.map((o) => o.slug))
  }, [groupOptions])

  // 新图初始渠道：优先 URL ?group=（仪表盘「截图补录」携带，经启用组校验）
  // → 否则第一个启用组 → 无可用组时为空（addFiles 拦截，上传区显示引导空态）
  // 文件名包名推荐仍可覆盖单图选择
  const searchParams = useSearchParams()
  const urlGroup = searchParams.get('group')
  const defaultGroup = useMemo(() => {
    if (urlGroup && groupOptions.some((o) => o.slug === urlGroup)) return urlGroup
    return groupOptions[0]?.slug ?? ''
  }, [urlGroup, groupOptions])
  const defaultGroupRef = useRef(defaultGroup)
  useEffect(() => {
    defaultGroupRef.current = defaultGroup
  }, [defaultGroup])

  // --- 添加 / 移除图片（上传后立即预览，不解析） ---
  const addFiles = useCallback(
    (files: File[]) => {
      // 无可用渠道（列表未加载完 / 没有启用组）时拒收，避免图片挂上空 slug
      if (!defaultGroupRef.current) {
        setErrorMsg('暂无可用的截图渠道：请先在「设置 · 来源管理」创建并启用来源组')
        return
      }
      const ok: ImgState[] = []
      let rejected: string | null = null
      for (const f of files) {
        const okImage = /image\/(png|jpe?g|webp)/.test(f.type)
        if (!okImage) {
          rejected = rejected ?? `「${f.name}」不是 PNG / JPG / WEBP 图片，已跳过`
          continue
        }
        ok.push({
          id: createId(),
          file: f,
          previewUrl: URL.createObjectURL(f),
          sourceGroup: defaultGroupRef.current,
          checked: true,
          parsing: false,
          parseError: null,
          preview: null,
          rows: {},
          reparse: false,
          job: null,
        })
      }
      if (ok.length) {
        setImages((prev) => [...prev, ...ok])
        // 文件名包名自动推荐（命中且该组已存在才预填，避免未知组导致解析失败）
        ok.forEach((img) => {
          api.getRecommendSourceGroup(img.file.name)
            .then((res) => {
              if (res.source_group && groupSlugsRef.current.has(res.source_group)) {
                setImages((prev) =>
                  prev.map((p) =>
                    p.id === img.id ? { ...p, sourceGroup: res.source_group as string } : p,
                  ),
                )
              }
            })
            .catch(() => { /* 推荐失败忽略，保持默认 */ })
        })
      }
      setErrorMsg(rejected)
    },
    [],
  )

  const removeFile = useCallback((id: string) => {
    setImages((prev) => {
      const target = prev.find((p) => p.id === id)
      if (target) URL.revokeObjectURL(target.previewUrl)
      return prev.filter((p) => p.id !== id)
    })
  }, [])

  const setImageGroup = useCallback((id: string, slug: string) => {
    setImages((prev) =>
      prev.map((p) => (p.id === id ? { ...p, sourceGroup: slug, reparse: p.preview ? true : p.reparse } : p)),
    )
  }, [])

  const toggleImageChecked = useCallback((id: string) => {
    setImages((prev) => prev.map((p) => (p.id === id ? { ...p, checked: !p.checked } : p)))
  }, [])

  // --- 数据日期（每图可编辑 + 顶部批量设置）---
  // 会话内固定的「今天」（批量设置 / 快捷动作用；避免每次渲染重算）
  const today = useMemo(() => todayStr(), [])
  // 批量设置模式：'' 占位 / today 全部今天 / suspected 各图截图日期 / custom 自定义（展开 picker）
  const [batchMode, setBatchMode] = useState<'' | 'today' | 'suspected' | 'custom'>('')
  const [batchCustomDate, setBatchCustomDate] = useState<string>('')

  // 单图改日期：只更新 selectedDate，不触发重新 preview（映射不依赖日期）
  const setImageDate = useCallback((id: string, date: string) => {
    setImages((prev) => prev.map((p) => (p.id === id ? { ...p, selectedDate: date } : p)))
  }, [])

  // 批量覆盖所有已解析图的日期；单图仍可在各自卡片再改。
  // suspected 模式：各图有疑似日期则用之，无则保持当前选择（不强改）。
  const batchSetDate = useCallback((mode: 'today' | 'suspected' | 'custom', customDate?: string) => {
    setImages((prev) =>
      prev.map((p) => {
        if (!p.preview) return p
        if (mode === 'today') return { ...p, selectedDate: today }
        if (mode === 'custom') return { ...p, selectedDate: customDate || today }
        const suspected = p.preview.suspected_date
        return suspected ? { ...p, selectedDate: suspected } : p
      }),
    )
  }, [today])

  const handleBatchMode = useCallback((mode: string) => {
    if (mode === 'today') { batchSetDate('today'); setBatchMode('') }
    else if (mode === 'suspected') { batchSetDate('suspected'); setBatchMode('') }
    else if (mode === 'custom') { setBatchMode('custom'); setBatchCustomDate(today) }
    else { setBatchMode('') }
  }, [batchSetDate, today])

  const handleBatchCustom = useCallback((date: string) => {
    setBatchCustomDate(date)
    if (date) batchSetDate('custom', date)
  }, [batchSetDate])

  const handleFileInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files || [])
      if (files.length) addFiles(files)
      e.target.value = ''
    },
    [addFiles],
  )

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      setDragOver(false)
      const files = Array.from(e.dataTransfer.files || [])
      if (files.length) addFiles(files)
    },
    [addFiles],
  )

  // --- 解析结果回填行编辑态（与旧同步 /preview 返回时的行为逐字段一致） ---
  const applyPreviewResult = useCallback((imgId: string, preview: ScreenshotPreviewResponse) => {
    const rows: Record<number, RowEdit> = {}
    preview.rows.forEach((r) => {
      rows[r.index] = {
        name: r.asset_name ?? r.source_name ?? '',
        amount: r.source_amount,
        skipped: false,
        zeroChecked: false,
        saveRename: false,
        saveIgnore: false,
        renameNote: '',
        ignoreNote: '',
        saveExclude: false,
        excludeNote: '',
        // 汇总行预勾（ADR-0029 D7）：命中内置汇总词表/该来源组 aggregate 规则的行由后端
        // 标 aggregate_hint，前端**预勾但可取消**。默认不自动学习（一次误勾污染以后每批）
        aggregate: !!r.aggregate_hint,
        aggregateSource: r.aggregate_hint ? 'rule' : null,
        parent: null,
      }
    })
    // 重新识别 ⇒ aiOnly 与行数据都换新，补录草稿（源自旧 aiOnly）一并清空，别把过期项写进新清单
    const hadSupplements = (imagesRef.current.find((p) => p.id === imgId)?.supplements?.length ?? 0) > 0
    setImages((prev) =>
      prev.map((p) => {
        if (p.id === imgId) {
          return {
            ...p,
            preview,
            rows: { ...rows },
            parsing: false,
            job: null,
            reparse: false,
            forceOcr: false,
            engineSwitch: false,
            engineBeforeSwitch: undefined,
            parseError: null,
            verifyByIndex: null,
            aiOnly: null,
            verifyStatus: 'idle',
            verifyError: null,
            supplements: [],
            // 数据日期初值 = 疑似日期 ?? 兜底日期 ?? 今天；重新解析同样按新 preview 复位
            //（AI 引擎 preview 无 suspected/data_date → 自然落「今天」，用户在卡头可改）
            selectedDate: preview.suspected_date ?? preview.data_date ?? todayStr(),
          }
        }
        // 别张图里「归到这张图某行」的引用一并解除：行号换了，旧父项可能指向另一行
        // （悬空父项在界面上会表现为「父项不见了但子项还缩进」，比报错更难查）
        const prefix = `${imgId}\u0000`
        let changed = false
        const nextRows: Record<number, RowEdit> = { ...p.rows }
        for (const k of Object.keys(nextRows).map(Number)) {
          const e = nextRows[k]
          if (e?.parent?.kind === 'row' && e.parent.key.startsWith(prefix)) {
            nextRows[k] = { ...e, parent: null }
            changed = true
          }
        }
        return changed ? { ...p, rows: nextRows } : p
      }),
    )
    // 只在真的有草稿被清掉时提示（首次解析不该冒这句）；判定序号 +1，作废在途 resolve 响应
    if (hadSupplements) {
      supSeqRef.current[imgId] = (supSeqRef.current[imgId] ?? 0) + 1
      pushRuleNotice('info', '已重新识别，补录项已清空')
    }
  }, [pushRuleNotice])

  // 解析失败落错误态；若是「切换引擎」在途失败，回滚到切换前引擎（保留的旧 preview 是旧引擎产物）。
  // 批量解析的图不带 engineBeforeSwitch，回滚为 no-op（引擎戳保留，重试沿用同引擎）
  const markParseError = useCallback((imgId: string, msg: string) => {
    setImages((prev) =>
      prev.map((p) =>
        p.id === imgId
          ? {
              ...p,
              parsing: false,
              job: null,
              parseError: msg,
              reparse: false,
              forceOcr: false,
              engineSwitch: false,
              ...(p.engineBeforeSwitch ? { engine: p.engineBeforeSwitch } : {}),
              engineBeforeSwitch: undefined,
            }
          : p,
      ),
    )
  }, [])

  // --- 批量解析（异步 job）：并行提交 preview-jobs（秒回），后端单 worker 串行 OCR，
  //     前端逐 job 轮询（running 2s / queued 3s）；单图失败不影响其它图 ---
  const startParse = useCallback(async () => {
    const targets = images.filter((p) => p.checked && (!p.preview || p.reparse))
    if (!targets.length) {
      // 没有待解析项 ≠ 误操作：勾选的图都已解析过（典型是「返回修改」看了一眼又回来），
      // 直接进清单即可，不重跑 OCR、不报错；只有零勾选才是真的没东西可做，保留防御性提示。
      // 注意不动 autoVerifyRanRef：校验结果还在状态里，回清单不该把整批 AI 校验白跑一遍
      if (images.some((p) => p.checked)) {
        setErrorMsg(null)
        setPollDegraded(false)
        setPhase('review')
      } else {
        setErrorMsg('没有需要解析的图片，请至少勾选一张')
      }
      return
    }
    setErrorMsg(null)
    setPhase('parsing')
    setParseTotal(targets.length)
    setParseProgress(0)
    setPollDegraded(false)
    pollFailsRef.current = 0
    batchRef.current = { total: targets.length, done: 0, success: 0 }
    clearPollTimers()
    const seq = ++batchSeqRef.current
    autoVerifyRanRef.current = false // 本批清单尚未自动校验
    // 重新识别＝行号与命中项都换新：上一轮的汇总勾选、归父关系与接受留痕全部作废
    // （留着 = 让新清单继承旧关系，用户按旧位置勾的父项会静默指向另一行）
    setAcceptedDiffs({})
    setFoldedRows({})
    setParentPickerKey(null)
    batchPlanRef.current = null
    appliedOkRef.current = new Set()
    applyResultsRef.current = []

    setImages((prev) =>
      prev.map((p) =>
        targets.some((t) => t.id === p.id) ? { ...p, parsing: true, parseError: null, job: null } : p,
      ),
    )

    // 单图终局（成功/失败）计数；全部到齐后决定去向（与旧串行流程一致）
    const finish = (ok: boolean) => {
      const b = batchRef.current
      b.done += 1
      if (ok) b.success += 1
      setParseProgress(b.done)
      if (b.done >= b.total) {
        if (b.success === 0) {
          setPhase('setup')
          setErrorMsg('全部图片解析失败，请检查图片清晰度后重试')
        } else {
          setPhase('review')
        }
      }
    }

    const schedule = (imgId: string, jobId: string, delay: number) => {
      if (seq !== batchSeqRef.current) return
      pollTimersRef.current.set(jobId, setTimeout(() => { void tick(imgId, jobId) }, delay))
    }

    const tick = async (imgId: string, jobId: string) => {
      if (seq !== batchSeqRef.current) return
      try {
        const st = await api.getPreviewJob(jobId)
        if (seq !== batchSeqRef.current) return
        pollFailsRef.current = 0
        setPollDegraded((prev) => (prev ? false : prev))
        if (st.status === 'done' || st.status === 'error') {
          pollTimersRef.current.delete(jobId)
          if (st.status === 'done' && st.result) {
            applyPreviewResult(imgId, st.result)
            finish(true)
            // AI 引擎图：preview（空骨架）就绪 → 自动识别编排（verify rows=[] → ai_only →
            // 自动采纳草稿）。识别而非校验，不受 autoVerify 开关约束（peer-entry-design §3.1/§5）
            const cur = imagesRef.current.find((p) => p.id === imgId)
            if (cur && engineOf(cur) === 'ai') void runAiRecognitionRef.current?.(imgId, st.result)
          } else {
            markParseError(imgId, st.error || '解析失败，请重试')
            finish(false)
          }
        } else {
          setImages((prev) => prev.map((p) => (p.id === imgId ? { ...p, job: st } : p)))
          schedule(imgId, jobId, st.status === 'queued' ? 3000 : 2000)
        }
      } catch {
        if (seq !== batchSeqRef.current) return
        // 网络失败：退避续试不中断；连续 3 次给降级提示（任一成功自动撤销）
        pollFailsRef.current += 1
        if (pollFailsRef.current >= 3) setPollDegraded(true)
        schedule(imgId, jobId, 3000)
      }
    }

    // 并行提交（快接口秒回）；提交失败的图直接标错并计数，不阻塞其它图。
    // 每图引擎 = 逐图覆盖 ?? 批级默认；提交成功即把生效引擎戳进状态（后续识别编排、
    // apply 载荷、清零守卫都以该戳为准）。AI 图 preview 秒级完成（跳过 OCR）
    // 同时带上本图已有的机器识别行（buildRecognition，AI 图才非空）：本图此前跑过
    // AI 识别（重解析场景）时随 job 落快照，没跑过则 undefined（后端记 items:null）。
    // 注意：它声明在下方（:3180），此处只能在回调体内引用、不能进依赖数组（TDZ）。
    await Promise.all(
      targets.map(async (img) => {
        const eff = img.engine ?? defaultEngineRef.current
        try {
          const { job_id } = await api.createPreviewJob(img.file, img.sourceGroup, false, eff === 'ai' ? 'ai' : undefined, buildRecognition(img))
          if (seq !== batchSeqRef.current) return
          // 合成排队快照：首轮轮询返回前即可显示排队态
          setImages((prev) =>
            prev.map((p) =>
              p.id === img.id
                ? {
                    ...p,
                    engine: eff,
                    job: {
                      status: 'queued', stage: 'queued', ocr: null,
                      queue_position: null, elapsed: 0, result: null, error: null,
                    },
                  }
                : p,
            ),
          )
          schedule(img.id, job_id, 1500)
        } catch (e) {
          if (seq !== batchSeqRef.current) return
          markParseError(img.id, `提交解析失败：${e instanceof Error ? e.message : '请求失败'}`)
          finish(false)
        }
      }),
    )
  }, [images, applyPreviewResult, markParseError, clearPollTimers])

  // --- 行级编辑 ---
  const updateRow = useCallback((imgId: string, index: number, patch: Partial<RowEdit>) => {
    setImages((prev) =>
      prev.map((p) => {
        if (p.id !== imgId) return p
        const row = p.rows?.[index]
        if (!row) return p
        return { ...p, rows: { ...p.rows, [index]: { ...row, ...patch } } }
      }),
    )
  }, [])

  // 双路对称裁决（peer-entry-design §2/§5）：分歧行一键把 AI 值填入既有 amounts/names 编辑态，
  // 与手输完全同构 —— apply 仍走行号通道，后端零新面；写入的是用户看到并确认的值。
  // 金额解析不出（AI 给了非数字）只采纳名称；两者都不可用则提示手动修改
  const adoptAiValue = useCallback(
    (r: ReviewRow) => {
      const v = r.img.verifyByIndex?.[r.index]
      if (!v || v.verdict !== 'mismatch' || !r.writable) return
      const patch: Partial<RowEdit> = {}
      const rawAmt = (v.ai_amount ?? '').replace(/[,，¥\s]/g, '')
      if (rawAmt) {
        try {
          new Decimal(rawAmt) // 只验合法性；行金额允许 0（与手输同权）
          patch.amount = rawAmt
        } catch {
          /* 金额不可解析：只采纳名称 */
        }
      }
      const name = (v.ai_name ?? '').trim()
      if (name) patch.name = name
      if (!patch.amount && !patch.name) {
        message.info('AI 值无法解析，请手动修改该行')
        return
      }
      updateRow(r.imgId, r.index, patch)
      message.success(
        `已采纳 AI 值${patch.name && patch.amount ? '（名称+金额）' : patch.amount ? '（金额）' : '（名称）'}，仍可手动修改`,
      )
    },
    [updateRow],
  )

  // 行下的有效子项数（票 05 D5）：跳过的子项不算——它们不产生构成边，也不进差额
  const effectiveChildCount = useCallback((rowsByImage: typeof images, parentKey: RowKey, excludeKey?: RowKey) => {
    let n = 0
    for (const p of rowsByImage) {
      for (const r of p.preview?.rows ?? []) {
        const k = rowKeyOf(p.id, r.index)
        if (excludeKey && k === excludeKey) continue
        const e = p.rows[r.index]
        if (e?.parent?.kind === 'row' && e.parent.key === parentKey && !e.skipped) n += 1
      }
    }
    return n
  }, [])

  const toggleRowSkip = useCallback(
    (imgId: string, index: number) => {
      const cur = images.find((p) => p.id === imgId)?.rows?.[index]
      const selfKey = rowKeyOf(imgId, index)
      setImages((prev) => {
        // 跳过＝本次不写入：它不再算父项的子项。若因此一条子边都不剩，父项的「归父自动」
        // 标记一并撤掉（只撤 parent，手勾/词表预勾不动 —— 票 05 D3/D5）
        const revokeParents = new Set<RowKey>()
        if (cur && !cur.skipped && cur.parent?.kind === 'row') {
          if (effectiveChildCount(prev, cur.parent.key, selfKey) === 0) revokeParents.add(cur.parent.key)
        }
        return prev.map((p) => {
          let rows: Record<number, RowEdit> | null = null
          if (p.id === imgId) {
            const row = p.rows?.[index]
            if (row) {
              const next = !row.skipped
              // 互斥（ADR-0029 D13）：跳过＝这次不处理，汇总＝参与核对但不落账，语义不能并存。
              // 勾一个自动取消另一个，并留一句小字说明（不弹窗：这是局部动作，不是全批决策）
              rows = {
                ...p.rows,
                [index]: next
                  ? { ...row, skipped: true, aggregate: false, aggregateSource: null }
                  : { ...row, skipped: false },
              }
            }
          }
          for (const key of revokeParents) {
            const parsed = splitRowKey(key)
            if (!parsed || parsed.imgId !== p.id) continue
            const base = rows ?? { ...p.rows }
            const target = base[parsed.index]
            if (!target) continue
            const flag = withLastChildRemoved({
              aggregate: !!target.aggregate,
              aggregateSource: target.aggregateSource ?? null,
            })
            base[parsed.index] = { ...target, aggregate: flag.aggregate, aggregateSource: flag.aggregateSource }
            rows = base
          }
          return rows ? { ...p, rows } : p
        })
      })
      if (cur && !cur.skipped && cur.aggregate) {
        pushRuleNotice('info', `「${cur.name || '这一行'}」已改为跳过，汇总行勾选同时取消`)
      }
    },
    [images, pushRuleNotice, effectiveChildCount],
  )

  const toggleZeroChecked = useCallback(
    (imgId: string, index: number) => {
      setImages((prev) =>
        prev.map((p) => {
          if (p.id !== imgId) return p
          const row = p.rows?.[index]
          if (!row) return p
          return { ...p, rows: { ...p.rows, [index]: { ...row, zeroChecked: !row.zeroChecked } } }
        }),
      )
    },
    [],
  )

  // 「不录入」勾选：联动把该行纳入本次清0（走单独清0 同一 zeroChecked 通道，任何策略下生效）；
  // 取消勾选不回退清0——用户可能只想撤销排除、本次仍写 0
  const toggleExclude = useCallback(
    (imgId: string, index: number) => {
      setImages((prev) =>
        prev.map((p) => {
          if (p.id !== imgId) return p
          const row = p.rows?.[index]
          if (!row) return p
          const next = !row.saveExclude
          return {
            ...p,
            rows: {
              ...p.rows,
              [index]: { ...row, saveExclude: next, zeroChecked: next ? true : row.zeroChecked },
            },
          }
        }),
      )
    },
    [],
  )

  // --- 汇总行勾选（ADR-0029 D5/D7；票 05 D3/D5）---
  // 勾上：只核对、不入账（金额变灰、徽章「核对」、不计入写入合计、提交走 reconciles 行号通道）。
  // 三条互斥一次做全（D13）：
  //   ① 跳过自动取消（跳过＝这次不处理，与「参与核对」互斥）；
  //   ② 已有身份裁决自动撤销并提示（裁决是对「这行要不要落账」的表态，汇总行不落账）；
  //   ③ 汇总行恒不可作 bind 目标（徽章与行内入口一起收掉，见 ActionBadge / 行内控件）。
  const toggleRowAggregate = useCallback(
    (imgId: string, index: number) => {
      const cur = images.find((p) => p.id === imgId)?.rows?.[index]
      if (!cur) return
      const next = !cur.aggregate
      const key = rowKeyOf(imgId, index)
      // 有效子项 = 真挂在这行名下、且本次会写入的行（跳过的行不产生构成边，也不进差额）
      let childCount = 0
      for (const p of images) {
        for (const r of p.preview?.rows ?? []) {
          const e = p.rows[r.index]
          if (e?.parent?.kind === 'row' && e.parent.key === key && !e.skipped) childCount += 1
        }
      }
      // 票 05 D4：父行还有子项时不许手动取消——否则父子同计（ADR-0029 D1）。
      // 阻断而不是「顺手把子项解绑」：解绑是删数据，不能藏在一次勾选里
      const verdict = withManualToggle(
        { aggregate: !!cur.aggregate, aggregateSource: cur.aggregateSource ?? null },
        childCount,
      )
      if (verdict.blocked) {
        pushRuleNotice(
          'error',
          `「${cur.name || '这一行'}」下还挂着 ${childCount} 个子项：先解除子项归父，再取消汇总行`,
        )
        return
      }
      const hadDecision = !!cur.identityAction || !!cur.identityBlocked
      setImages((prev) =>
        prev.map((p) => {
          if (p.id !== imgId) return p
          const row = p.rows?.[index]
          if (!row) return p
          return {
            ...p,
            rows: {
              ...p.rows,
              [index]: next
                ? {
                    ...row,
                    aggregate: true,
                    aggregateSource: 'manual',
                    skipped: false,
                    identityAction: null,
                    identityTargetId: null,
                    identityReason: undefined,
                    bindTargetId: null,
                    bindTargetName: null,
                    identityBlocked: false,
                  }
                : { ...row, aggregate: false, aggregateSource: null },
            },
          }
        }),
      )
      if (!next) {
        setAcceptedDiffs((prev) => {
          if (!prev[key]) return prev
          const nextMap = { ...prev }
          delete nextMap[key]
          return nextMap
        })
      }
      const label = cur.name || '这一行'
      if (next) {
        pushRuleNotice(
          'info',
          hadDecision
            ? `「${label}」已标为汇总行：只核对、不入账，原有身份裁决已撤销`
            : `「${label}」已标为汇总行：只核对、不入账，不计入写入合计`,
        )
      } else {
        pushRuleNotice('info', `「${label}」已取消汇总行：恢复正常落账，金额重新计入写入合计`)
      }
    },
    [images, pushRuleNotice],
  )

  // --- 归父（ADR-0029 D12）：选择器与拖拽写的是同一个状态，两条路径结果完全一致 ---
  // 文案固定为叶子计量口径：父项只核对、不计入合计；本行（子项）照常计入。
  const assignParent = useCallback(
    (childKey: RowKey, parent: ParentRef, parentLabel: string) => {
      const parsed = splitRowKey(childKey)
      if (!parsed) return
      const { imgId, index } = parsed
      const childRow = images.find((p) => p.id === imgId)?.rows?.[index]
      const prevParent = childRow?.parent ?? null
      const parentParsed = parent.kind === 'row' ? splitRowKey(parent.key) : null
      setImages((prev) => {
        // 换父时，老父项可能失去最后一条子边 → 撤掉它的「归父自动」标记（票 05 D3）
        const revokeOld =
          prevParent?.kind === 'row' && prevParent.key !== (parent.kind === 'row' ? parent.key : null)
            ? effectiveChildCount(prev, prevParent.key, childKey) === 0
            : false
        return prev.map((p) => {
          let rows: Record<number, RowEdit> | null = null
          if (p.id === imgId) {
            const cur = p.rows?.[index]
            if (cur) rows = { ...p.rows, [index]: { ...cur, parent } }
          }
          // 票 05 D1：归父即自动成为汇总行——目标行从没勾过也要勾上（两入口同源，拖拽走这里）
          if (parentParsed && p.id === parentParsed.imgId) {
            const base = rows ?? { ...p.rows }
            const target = base[parentParsed.index]
            if (target) {
              const flag = withChildAdded({
                aggregate: !!target.aggregate,
                aggregateSource: target.aggregateSource ?? null,
              })
              base[parentParsed.index] = {
                ...target,
                aggregate: flag.aggregate,
                aggregateSource: flag.aggregateSource,
                // 汇总行与跳过互斥；父项也不再需要身份裁决（它只核对、不落账）
                skipped: false,
              }
              rows = base
            }
          }
          if (revokeOld && prevParent?.kind === 'row') {
            const oldParsed = splitRowKey(prevParent.key)
            if (oldParsed && oldParsed.imgId === p.id) {
              const base = rows ?? { ...p.rows }
              const target = base[oldParsed.index]
              if (target) {
                const flag = withLastChildRemoved({
                  aggregate: !!target.aggregate,
                  aggregateSource: target.aggregateSource ?? null,
                })
                base[oldParsed.index] = { ...target, aggregate: flag.aggregate, aggregateSource: flag.aggregateSource }
                rows = base
              }
            }
          }
          return rows ? { ...p, rows } : p
        })
      })
      // 归进去却看不见没有意义：父行一律展开
      if (parent.kind === 'row') setFoldedRows((prev) => (prev[parent.key] ? { ...prev, [parent.key]: false } : prev))
      pushRuleNotice(
        'success',
        parent.kind === 'row'
          ? `「${childRow?.name || '这一行'}」已归到「${parentLabel}」：目标行已自动置为汇总行（只核对、不计入合计），本行金额照常计入`
          : `「${childRow?.name || '这一行'}」已归到「${parentLabel}」：父项只核对、不计入合计，本行（子项）金额照常计入`,
      )
    },
    [images, pushRuleNotice, effectiveChildCount],
  )

  const clearParent = useCallback(
    (childKey: RowKey) => {
      const parsed = splitRowKey(childKey)
      if (!parsed) return
      const { imgId, index } = parsed
      const child = images.find((p) => p.id === imgId)?.rows?.[index]
      const parentRef = child?.parent ?? null
      const parentKey = parentRef?.kind === 'row' ? parentRef.key : null
      // 最后一条子边解除 ⇒ 父项的「归父自动」标记撤掉（只撤 parent，票 05 D3）
      const revoke = parentKey !== null && effectiveChildCount(images, parentKey, childKey) === 0
      const parentParsed = revoke && parentKey !== null ? splitRowKey(parentKey) : null
      setImages((prev) =>
        prev.map((p) => {
          let rows: Record<number, RowEdit> | null = null
          if (p.id === imgId) {
            const cur = p.rows?.[index]
            if (cur) rows = { ...p.rows, [index]: { ...cur, parent: null } }
          }
          if (parentParsed && p.id === parentParsed.imgId) {
            const base = rows ?? { ...p.rows }
            const target = base[parentParsed.index]
            if (target) {
              const flag = withLastChildRemoved({
                aggregate: !!target.aggregate,
                aggregateSource: target.aggregateSource ?? null,
              })
              base[parentParsed.index] = { ...target, aggregate: flag.aggregate, aggregateSource: flag.aggregateSource }
              rows = base
            }
          }
          return rows ? { ...p, rows } : p
        }),
      )
      if (revoke && parentKey !== null) {
        setAcceptedDiffs((prev) => {
          if (!prev[parentKey]) return prev
          const nextMap = { ...prev }
          delete nextMap[parentKey]
          return nextMap
        })
      }
      pushRuleNotice(
        'info',
        revoke
          ? `「${child?.name || '这一行'}」已取消归属：父项下没有子项了，它的汇总行勾选一并撤销`
          : `「${child?.name || '这一行'}」已取消归属：金额照常计入，父项也不再因它而有差额`,
      )
    },
    [images, pushRuleNotice, effectiveChildCount],
  )

  // 页角色（票 05 D2）：纯注释，写进图状态只为在卡片上显示；不参与任何计算与校验
  const setPageRole = useCallback((imgId: string, role: 'none' | 'main' | 'sub') => {
    setImages((prev) => prev.map((p) => (p.id === imgId ? { ...p, pageRole: role } : p)))
  }, [])

  // --- 派生：聚合待录入清单 ---
  // 已有项的 item 级现值（跨图合并）：绑定行的差额基线、顶部系统合计都靠它。
  // 旧后端不返回 existing_items → 空表，行基线自动回落到 current_amount（旧行为）。
  const itemAmounts = useMemo(
    () => buildItemAmounts(images.filter((p) => p.preview).map((p) => p.preview!)),
    [images],
  )
  const allReviewRows = useMemo<ReviewRow[]>(() => {
    const out: ReviewRow[] = []
    // 图序（1 基）：按上传顺序给每张已解析图编号，行内「图 N」标记、筛选器、
    // 核对面板与拖拽提示都用它——它是用户认图的方式，与内部 imgId 解耦
    let ordinal = 0
    for (const img of images) {
      if (!img.preview) continue
      ordinal += 1
      for (const row of img.preview.rows) {
        const edit =
          img.rows[row.index] ?? {
            name: row.asset_name ?? row.source_name ?? '',
            amount: row.source_amount,
            skipped: false,
            zeroChecked: false,
            saveRename: false,
            saveIgnore: false,
            renameNote: '',
            ignoreNote: '',
            saveExclude: false,
            excludeNote: '',
            aggregate: !!row.aggregate_hint,
            aggregateSource: row.aggregate_hint ? 'rule' : null,
            parent: null,
          }
        let displayAction: ScreenshotPreviewRow['action'] = row.action
        let isZeroed = false
        if (row.action === 'keep') {
          if (unrecognizedPolicy === 'zeroAll') {
            displayAction = 'zero'
            isZeroed = true
          } else if ((unrecognizedPolicy === 'zeroEach' && edit.zeroChecked) || edit.saveExclude) {
            // 勾「不录入」⇒ 任何策略下本次都清 0（行上显示「将清零」，不静默写 0）
            displayAction = 'zero'
            isZeroed = true
          } else {
            displayAction = 'keep'
          }
        }
        // 汇总行（ADR-0029 D5）：**不落账**，因此不算可写行——「本批写入合计」「系统合计」
        // 与「差异」三张卡自动不含它。它的金额仍参与核对（核对面板单独算）。
        const aggregate = !!edit.aggregate && displayAction !== 'keep'
        const writable = !edit.skipped && displayAction !== 'keep' && !aggregate
        const amount =
          displayAction === 'zero'
            ? '0.00'
            : displayAction === 'keep'
              ? (row.current_amount ?? edit.amount)
              : edit.amount
        const diff =
          displayAction === 'keep'
            ? '0.00'
            : rowDiffAmount(
                amount,
                // 绑定行以**目标项**现值为基线（缺陷修复：绑定后不再按全额虚增）
                rowBaseline(row, edit.bindTargetId ?? null, itemAmounts),
              )
        out.push({
          imgId: img.id,
          img,
          imageLabel: img.preview.image_filename,
          groupLabel: labelFor(img.sourceGroup),
          dataDate: img.preview.data_date,
          row,
          index: row.index,
          edit,
          displayAction,
          isZeroed,
          writable,
          amount,
          diff,
          nameEditable: displayAction !== 'keep',
          key: rowKeyOf(img.id, row.index),
          aggregate,
          parent: edit.parent ?? null,
          imageOrdinal: ordinal,
        })
      }
    }
    return out
  }, [images, unrecognizedPolicy, labelFor, itemAmounts])

  // 未识别项 = 批次级「系统有、本批同来源组所有截图都没出现」的资产：后端 preview
  // 是单图视角（组内未命中即 keep 行），多图叠加必须按并集反向剔除，否则别张图刚识别到
  // 的资产会在这里被要求「保持原值 / 清0」。同时按 (来源组, 资产) 去重：同一资产只留
  // 批次里第一张图（owner），「全部清0」按 owner 下发 zero_names，天然只清一次。
  const batchRecognized = useMemo(() => batchRecognizedIndex(images), [images])
  // 已被本批某行「关联」的已有项：它不算「本次未识别」（后端 apply 时同样剔除该目标，票 01 D10）。
  // keep 行只有名称没有 item_id，故按显示名剔除——关联标记里存的正是目标项现名。
  const boundTargetNames = useMemo(() => {
    const names = new Set<string>()
    for (const r of allReviewRows) {
      if (r.edit.skipped) continue
      const name = bindTargetNameOf(r)
      if (name) names.add(name)
    }
    return names
  }, [allReviewRows])
  const keepRows = useMemo(
    () => filterBatchUnrecognized(allReviewRows.filter((r) => r.row.action === 'keep'), batchRecognized)
      .filter((r) => !boundTargetNames.has(r.edit.name || r.row.asset_name || '')),
    [allReviewRows, batchRecognized, boundTargetNames],
  )
  // owner keep 行（批次内该资产的唯一一行）：zero_names 只从 owner 图下发
  const ownedKeepKeys = useMemo(() => new Set(keepRows.map((r) => `${r.imgId}\u0000${r.index}`)), [keepRows])
  // 批次视角下被别张图覆盖 / 重复的 keep 行不是「本次未识别项」，不进派生清单：
  // 否则它们会以「将清零」计入待写入条数与合计，而产生载荷时又不会下发（显示≠实际）。
  const reviewRows = useMemo(
    () => allReviewRows.filter((r) => r.row.action !== 'keep' || ownedKeepKeys.has(`${r.imgId}\u0000${r.index}`)),
    [allReviewRows, ownedKeepKeys],
  )
  const entryRows = useMemo(() => reviewRows.filter((r) => r.row.action !== 'keep'), [reviewRows])
  // 批量「全部不录入」按钮双态判定：存在任一 keep 行已勾「不录入」→ 切换为「取消全部不录入」
  const anyKeepExcluded = useMemo(() => keepRows.some((r) => !!r.edit.saveExclude), [keepRows])
  // 「全部不录入」只作用于批次视角的 owner keep 行（= 未识别项表里可见的那些）：
  // 别张图的重复 keep 行不属本次未识别项，打标只会留下幽灵勾选
  const excludeAllKeepRows = useCallback(() => {
    const byImg = new Map<string, number[]>()
    for (const r of keepRows) {
      const list = byImg.get(r.imgId)
      if (list) list.push(r.index)
      else byImg.set(r.imgId, [r.index])
    }
    setImages((prev) =>
      prev.map((p) => {
        const idxs = byImg.get(p.id)
        if (!idxs?.length) return p
        let changed = false
        const rows = { ...p.rows }
        for (const i of idxs) {
          const edit = rows[i]
          if (!edit || edit.skipped || edit.saveExclude) continue
          rows[i] = { ...edit, saveExclude: true, zeroChecked: true }
          changed = true
        }
        return changed ? { ...p, rows } : p
      }),
    )
  }, [keepRows])

  // 「取消全部不录入」：整体撤销「全部不录入」——对所有 saveExclude 行撤销排除 + 清空备注，
  // 并把随排除联动的清零勾选（zeroChecked）一并退回，行回到批量排除前的状态。
  // ⚠️ 与单行取消「不录入」语义不同：单行取消有意保留 zeroChecked（可能只想撤销排除、
  // 本次仍写 0）；批量取消 = 整体撤销，清零随之撤销。
  // 跳过行本就无法勾「不录入」（单行 toggle 禁用、批量排除亦跳过），天然不受影响。
  // 撤销后 buildApplyOptions 自然不再向 zero_names 推这些行（判定只看这两个 flag）。
  const clearAllKeepExcludes = useCallback(() => {
    setImages((prev) =>
      prev.map((p) => {
        let changed = false
        const rows = { ...p.rows }
        for (const k of Object.keys(rows).map(Number)) {
          const edit = rows[k]
          if (!edit.saveExclude) continue
          rows[k] = { ...edit, saveExclude: false, excludeNote: '', zeroChecked: false }
          changed = true
        }
        return changed ? { ...p, rows } : p
      }),
    )
  }, [])

  const writableRows = useMemo(() => reviewRows.filter((r) => r.writable), [reviewRows])
  const skippedCount = useMemo(() => reviewRows.filter((r) => r.edit.skipped).length, [reviewRows])
  // 汇总行条数（只核对、不入账）：确认栏里的「另有 N 行只核对」与 StatCard 用它
  const aggregateRowCount = useMemo(
    () => entryRows.filter((r) => r.aggregate && !r.edit.skipped).length,
    [entryRows],
  )
  // 身份裁决（D4）：带 identity_guess 的行 = 疑似改名/拆分，未表态则阻断写入。
  // 撤销行（skipped）不需要裁决；旧后端无 identity_guess 时这些集合天然为空。
  const identityGuessedRows = useMemo(
    () => reviewRows.filter((r) => !!r.row.identity_guess && !r.edit.skipped),
    [reviewRows],
  )
  const identityBlockers = useMemo(() => identityGuessedRows.filter(identityNeedsAttention), [identityGuessedRows])
  // 「+ 新增」行待表态（D4 扩面）：既没关联到已有项、也没说「就是新增」，写入前必须先选一个。
  // 撤销行（skipped）不算；带 identity_guess 的行仍由上面的身份裁决逐行处理（它们也有候选可选，
  // 不该被「全部确认新增」一把带过），这里只管没有候选的纯新增行。
  const createPendingRows = useMemo(
    () => reviewRows.filter((r) => createNeedsDecision(r) && !r.row.identity_guess),
    [reviewRows],
  )
  const confirmAllCreate = useCallback(() => {
    for (const r of createPendingRows) {
      updateRow(r.imgId, r.index, { identityAction: 'create', identityBlocked: false })
    }
  }, [createPendingRows, updateRow])

  // --- 关联到已有项：行内入口打开的选择器（一次只服务一行）---
  const [bindPickerRow, setBindPickerRow] = useState<{ imgId: string; index: number; name: string } | null>(null)
  const openBindPicker = useCallback((r: ReviewRow) => {
    setBindPickerRow({
      imgId: r.imgId,
      index: r.index,
      name: r.edit.name || r.row.source_name || r.row.asset_name || '',
    })
  }, [])
  const closeBindPicker = useCallback(() => setBindPickerRow(null), [])
  const pickBindTarget = useCallback((item: AssetItemInfo) => {
    const target = bindPickerRow
    if (!target) return
    const row = allReviewRows.find((r) => r.imgId === target.imgId && r.index === target.index)
    // 关联优先于自动判定（文本命中 / identity_guess）：清掉 merge 目标，避免取消关联后残留旧选择
    const apply = () => updateRow(target.imgId, target.index, {
      skipped: false,
      identityAction: 'bind',
      identityTargetId: null,
      bindTargetId: item.id,
      bindTargetName: item.display_name,
      identityBlocked: false,
    })
    setBindPickerRow(null)
    // 0 额关联＝把该目标项这次写成 0（金额以本行截图为准）：这类判定要二次确认（票 01 D9）
    if (row && toDecimal(row.amount).isZero()) {
      Modal.confirm({
        title: '这一行金额为 0，确认关联？',
        content: `关联后会把「${item.display_name}」在本次数据日期写成 0。若只是名称识别不同、金额不该为 0，请先改本行金额再关联。`,
        okText: '仍然关联',
        cancelText: '先不关联',
        onOk: apply,
      })
      return
    }
    apply()
  }, [allReviewRows, bindPickerRow, updateRow])
  const clearBind = useCallback((r: ReviewRow) => {
    // 取消关联 = 删掉该行的 bind 决策，回到自动判定（保留名称/金额等其它编辑）
    updateRow(r.imgId, r.index, {
      identityAction: null,
      bindTargetId: null,
      bindTargetName: null,
    })
  }, [updateRow])
  // 底部摘要 → 行内裁决控件的跳转：移动端清单很长（未识别项在下方），只给名字不给出路会让人来回滚
  const scrollToIdentityRow = useCallback((r: ReviewRow) => {
    const desktop = typeof window !== 'undefined' && window.matchMedia('(min-width: 1024px)').matches
    const el = document.getElementById(`identity-${desktop ? 'desktop' : 'mobile'}-${r.imgId}-${r.index}`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.querySelector<HTMLInputElement>('input[data-identity-choice]:not(:disabled)')?.focus({ preventScroll: true })
  }, [])

  const liveTotalScreenshot = useMemo(
    () => writableRows.reduce((acc, r) => acc.plus(toDecimal(r.amount)), new Decimal(0)),
    [writableRows],
  )
  const systemTotal = useMemo(
    () => sumSystemBaseline(
      writableRows.map((r) => ({ row: r.row, bindTargetId: r.edit.bindTargetId ?? null })),
      itemAmounts,
    ),
    [writableRows, itemAmounts],
  )
  const liveDiff = useMemo(
    () => liveTotalScreenshot.minus(systemTotal),
    [liveTotalScreenshot, systemTotal],
  )

  const checkedImages = useMemo(() => images.filter((p) => p.checked), [images])
  const parsedImages = useMemo(() => images.filter((p) => p.preview), [images])
  // AI 主识别图（已解析）：清零策略、汇总口径与文案按引擎分组（peer-entry-design §2/§4c）
  const aiParsedImages = useMemo(() => parsedImages.filter((p) => engineOf(p) === 'ai'), [parsedImages])
  const anyAiParsed = aiParsedImages.length > 0
  const pureAiBatch = anyAiParsed && aiParsedImages.length === parsedImages.length
  const failedImages = useMemo(() => images.filter((p) => p.parseError), [images])
  // 失败但仍有旧识别结果的图（如「强制重新 OCR」失败）：还在清单里，不能报「已跳过未进入清单」，
  // 改由各自卡片头部就地说明原因；这里只统计真正没进清单的
  const failedNoPreview = useMemo(() => failedImages.filter((p) => !p.preview), [failedImages])
  const pendingParseCount = useMemo(
    () => images.filter((p) => p.checked && (!p.preview || p.reparse)).length,
    [images],
  )
  const uncheckedCount = useMemo(() => images.filter((p) => !p.checked).length, [images])

  const parseRatio = parseTotal > 0 ? (parseProgress / parseTotal) * 100 : 0

  // --- 校验汇总（顶部轻量条数据源）：聚合各图行级 verdict + ai_only 单边项 + 图级失败 ---
  // 仅统计已产出校验结果的图（verifyByIndex 非空）；重新解析后结果清空，汇总自动消失
  const verifySummary = useMemo<VerifySummaryData>(() => {
    const s: VerifySummaryData = {
      hasResult: false,
      match: 0,
      mismatch: 0,
      ocrOnly: 0,
      aiOnly: 0,
      supplemented: 0,
      unverified: 0,
      failed: 0,
    }
    for (const img of images) {
      if (img.verifyStatus === 'failed') s.failed++
      if (!img.verifyByIndex) continue
      s.hasResult = true
      for (const v of Object.values(img.verifyByIndex)) {
        if (v.verdict === 'match') s.match++
        else if (v.verdict === 'mismatch') s.mismatch++
        else if (v.verdict === 'ocr_only') s.ocrOnly++
        else s.unverified++
      }
      // 仅 AI 项：已补录的单独计数（用户已处理），剩下的才算待处理单边缺项
      const drafted = new Set((img.supplements ?? []).map((d) => d.name.trim()))
      for (const a of img.aiOnly ?? []) {
        if (drafted.has((a.name ?? '').trim())) s.supplemented++
        else s.aiOnly++
      }
    }
    // 全部失败（无任何行级结果）时也算「有结果」，让汇总条呈现失败态
    if (!s.hasResult && s.failed > 0) s.hasResult = true
    return s
  }, [images])

  // --- 补录（AI 额外识别 → 手动加入本次清单）汇总 ---
  // 草稿跨图摊平，便于合计、写入前校验与结果页统计；sendable = 会真正进 apply 载荷的那些
  const supplementDrafts = useMemo(
    () => images.flatMap((p) => (p.supplements ?? []).map((draft) => ({ imgId: p.id, img: p, draft }))),
    [images],
  )
  const sendableSupplements = useMemo(
    () => supplementDrafts.filter((x) => isSendableSupplement(x.draft)),
    [supplementDrafts],
  )
  const sendableSupplementCount = sendableSupplements.length
  // 冲突项：标红、不发送（后端同样会 drop，这里提前不发，避免用户以为会写进去）
  const conflictSupplementCount = useMemo(
    () => supplementDrafts.filter((x) => x.draft.resolved?.action === 'conflict').length,
    [supplementDrafts],
  )
  // 还没判定成功的（可发送但无结论）：写入前确认一次，补录不盲写
  const unresolvedSupplementCount = useMemo(
    () => sendableSupplements.filter((x) => !x.draft.resolved).length,
    [sendableSupplements],
  )
  const supplementTotal = useMemo(
    () => sendableSupplements.reduce((acc, x) => acc.plus(toDecimal(x.draft.amount)), new Decimal(0)),
    [sendableSupplements],
  )
  // 本次写入合计 = 截图识别（OCR 可写行）＋ 补录。补录金额不混进「截图识别合计」
  const writeTotal = useMemo(
    () => liveTotalScreenshot.plus(supplementTotal),
    [liveTotalScreenshot, supplementTotal],
  )
  // AI 主识别汇总（§2 汇总行）：识别 Y 项（ai_only 源清单，含已采纳）/ 已采纳 Z 项 = 可发送草稿
  const aiRecognizedCount = useMemo(
    () => aiParsedImages.reduce((n, p) => n + (p.aiOnly?.length ?? 0), 0),
    [aiParsedImages],
  )
  const aiAdoptedSupplements = useMemo(
    () => sendableSupplements.filter((x) => engineOf(x.img) === 'ai'),
    [sendableSupplements],
  )
  const aiAdoptedCount = aiAdoptedSupplements.length
  const aiAdoptedTotal = useMemo(
    () => aiAdoptedSupplements.reduce((acc, x) => acc.plus(toDecimal(x.draft.amount)), new Decimal(0)),
    [aiAdoptedSupplements],
  )
  // 校验汇总条的模式口径：纯 AI 批 = 'ai'；混批 = 'mixed'；无 AI 图 = 'ocr'（文案与现状一致）
  const verifyBarMode: 'ocr' | 'ai' | 'mixed' = anyAiParsed ? (pureAiBatch ? 'ai' : 'mixed') : 'ocr'

  // 策略-引擎冲突兜底（§4c）：批内出现 AI 图而策略仍是「全部清0」（先选清零后切 AI）时自动退回
  // 「保持原值」—— AI 模式下 keep 行 = 「AI 没认出」，由模型召回率背书，整组清零破坏不对称
  useEffect(() => {
    if (anyAiParsed && unrecognizedPolicy === 'zeroAll') {
      setUnrecognizedPolicy('keep')
      pushRuleNotice(
        'info',
        '存在 AI 识别图，「全部清0」已退回「保持原值」（防模型漏认导致整组误清零）；可用「单独清0」或「不录入」',
      )
    }
  }, [anyAiParsed, unrecognizedPolicy, pushRuleNotice])
  // 补录项与「已被用户跳过的识别行」同名：后端看不到被跳过的行，会当全新补录写入 ⇒ 只有前端能提醒
  const skipCrossSupplements = useMemo(
    () =>
      supplementDrafts.filter(({ img, draft }) => {
        if (!img.preview || draft.resolved?.action === 'conflict') return false
        const target = draft.resolved?.asset_name || draft.resolved?.final_name
        if (!target) return false
        return img.preview.rows.some(
          (r) => r.action !== 'keep' && r.asset_name === target && img.rows[r.index]?.skipped,
        )
      }),
    [supplementDrafts],
  )

  // --- 补录判定（POST /screenshots/supplements/resolve，只读、不跑 OCR）---
  // 整批一次发：duplicate_target 是 first-wins，判定依赖整批顺序，拆开发会给出错误结论
  const [supResolving, setSupResolving] = useState<Record<string, boolean>>({})

  const resolveSupplements = useCallback(
    async (imgId: string, draftsOverride?: SupplementDraft[]) => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img) return
      // 只判定内容合法的草稿（空名/金额非法的发过去必然 400，白白拖垮整批判定）
      const drafts = (draftsOverride ?? img.supplements ?? []).filter(
        (d) => d.name.trim() !== '' && supplementAmountValid(d.amount),
      )
      if (!drafts.length) {
        setSupResolving((prev) => (prev[imgId] ? { ...prev, [imgId]: false } : prev))
        return
      }
      const seq = (supSeqRef.current[imgId] ?? 0) + 1
      supSeqRef.current[imgId] = seq
      setSupResolving((prev) => ({ ...prev, [imgId]: true }))
      const markError = (msg: string) => {
        const uids = new Set(drafts.map((d) => d.uid))
        setImages((prev) =>
          prev.map((p) =>
            p.id === imgId
              ? {
                  ...p,
                  supplements: (p.supplements ?? []).map((d) =>
                    uids.has(d.uid) ? { ...d, resolved: null, resolveError: msg } : d,
                  ),
                }
              : p,
          ),
        )
      }
      try {
        const res = await api.resolveSupplements(
          img.sourceGroup,
          drafts.map((d) => ({ name: d.name.trim(), amount: d.amount.trim(), side: d.side })),
        )
        if (supSeqRef.current[imgId] !== seq) return // 期间用户又改了 / 重新识别了：丢弃过期结论
        const list = Array.isArray(res) ? res : []
        // 契约 §7：响应逐项、与请求同序；按序回填到对应 uid
        const byUid = new Map<string, SupplementResolveResult>()
        list.forEach((r, i) => {
          const uid = drafts[i]?.uid
          if (uid && r) byUid.set(uid, r)
        })
        setImages((prev) =>
          prev.map((p) =>
            p.id === imgId
              ? {
                  ...p,
                  supplements: (p.supplements ?? []).map((d) => {
                    const r = byUid.get(d.uid)
                    if (!r) return d
                    return {
                      ...d,
                      resolved: {
                        action: r.action,
                        final_name: r.final_name ?? null,
                        asset_name: r.asset_name ?? null,
                        current_amount: r.current_amount ?? null,
                        conflict_reason: r.conflict_reason ?? null,
                      },
                      resolveError: null,
                    }
                  }),
                }
              : p,
          ),
        )
        // 与「已跳过的识别行」同名 ⇒ 提醒一次（每条草稿只提醒一次，不反复弹窗）
        const skippedNames = new Set(
          (img.preview?.rows ?? [])
            .filter((r) => r.action !== 'keep' && img.rows[r.index]?.skipped)
            .map((r) => r.asset_name)
            .filter((v): v is string => !!v),
        )
        const crossed = drafts.filter((d) => {
          if (supSkipWarnedRef.current.has(d.uid)) return false
          const r = byUid.get(d.uid)
          if (!r || r.action === 'conflict') return false
          const target = r.asset_name || r.final_name
          return !!target && skippedNames.has(target)
        })
        if (crossed.length) {
          crossed.forEach((d) => supSkipWarnedRef.current.add(d.uid))
          message.warning(
            `${crossed.length} 项补录与你已跳过的识别行同名：跳过的行不写入，补录项会照常写入，请确认是否保留`,
          )
        }
      } catch (e) {
        if (supSeqRef.current[imgId] !== seq) return
        // 判定失败不阻断写入（后端 apply 时权威重算），但必须可见 + 可重试
        markError(e instanceof Error ? e.message : '判定请求失败')
      } finally {
        if (supSeqRef.current[imgId] === seq) setSupResolving((prev) => ({ ...prev, [imgId]: false }))
      }
    },
    [],
  )

  // 编辑后合并判定：停顿 600ms 再整批重判，避免逐字符打请求
  const scheduleSupplementResolve = useCallback(
    (imgId: string) => {
      const prev = supDebounceRef.current.get(imgId)
      if (prev) clearTimeout(prev)
      supDebounceRef.current.set(
        imgId,
        setTimeout(() => {
          supDebounceRef.current.delete(imgId)
          void resolveSupplements(imgId)
        }, 600),
      )
    },
    [resolveSupplements],
  )

  // 加入补录/采纳（逐项 / 全部 / AI 模式自动）：按 name 幂等去重，默认 side=asset（不按名字猜负债），
  // 随后整批判定一次。origin='auto' = AI 主识别的自动采纳（peer-entry-design §5 编排）：
  // 提示语用「采纳」术语，且全部已在列表时静默（自动流程不弹「已经在列表里」）
  const addSupplements = useCallback(
    (imgId: string, items: VerifyAiOnlyItem[], origin: 'manual' | 'auto' = 'manual') => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img) return
      const existing = img.supplements ?? []
      const seen = new Set(existing.map((d) => d.name.trim()))
      const added: SupplementDraft[] = []
      for (const it of items) {
        const name = (it.name ?? '').trim()
        if (!name || seen.has(name)) continue
        seen.add(name)
        added.push({
          uid: createSupplementUid(),
          name,
          amount: normalizeSupplementAmount(it.amount),
          side: 'asset',
        })
      }
      if (!added.length) {
        if (origin === 'manual') message.info('这些项已经在补录列表里了')
        return
      }
      const next = [...existing, ...added]
      setImages((prev) => prev.map((p) => (p.id === imgId ? { ...p, supplements: next } : p)))
      pushRuleNotice(
        'info',
        origin === 'auto'
          ? `AI 识别 ${added.length} 项，已自动采纳为草稿（默认按资产记账）：请逐项核对名称/金额/方向，确认后才写入`
          : added.length > 1
            ? `已加入 ${added.length} 项补录，默认按资产记账；负债项请逐项切换`
            : `已加入「${added[0].name}」，默认按资产记账；负债项请切换`,
      )
      // 传 next 而不是回读 state：setState 还没落地，回读会漏掉刚加入的项
      void resolveSupplements(imgId, next)
    },
    [pushRuleNotice, resolveSupplements],
  )

  // 改名称 / 金额 / 方向：旧判定立刻作废（结论必须对得上当前内容），停顿后整批重判
  const updateSupplement = useCallback(
    (imgId: string, uid: string, patch: Partial<Pick<SupplementDraft, 'name' | 'amount' | 'side'>>) => {
      setImages((prev) =>
        prev.map((p) =>
          p.id === imgId
            ? {
                ...p,
                supplements: (p.supplements ?? []).map((d) =>
                  d.uid === uid ? { ...d, ...patch, resolved: null, resolveError: null } : d,
                ),
              }
            : p,
        ),
      )
      scheduleSupplementResolve(imgId)
    },
    [scheduleSupplementResolve],
  )

  const removeSupplement = useCallback((imgId: string, uid: string) => {
    supSkipWarnedRef.current.delete(uid)
    setImages((prev) =>
      prev.map((p) =>
        p.id === imgId ? { ...p, supplements: (p.supplements ?? []).filter((d) => d.uid !== uid) } : p,
      ),
    )
  }, [])

  // --- 按图分组用于渲染 ---
  // 以 images 顺序为准；没有非 keep 行的图，只要有 AI 额外识别项或补录草稿也要占一张卡，
  // 否则卡头 + ai_only 条不渲染 ⇒ 补录入口（和强制重新 OCR 入口）对这类图完全够不到。
  // AI 主识别图恒占卡：识别中 / 失败阻断态（重试 / 转用 OCR）必须在清单里可见，绝不静默
  const entryGroups = useMemo(() => {
    const byImg = new Map<string, ReviewRow[]>()
    for (const r of entryRows) {
      const arr = byImg.get(r.imgId) ?? []
      arr.push(r)
      byImg.set(r.imgId, arr)
    }
    const out: {
      imgId: string
      img: ImgState
      imageLabel: string
      groupLabel: string
      dataDate: string | null
      rows: ReviewRow[]
    }[] = []
    for (const img of images) {
      if (!img.preview) continue
      const rows = byImg.get(img.id)
      if (rows?.length) {
        out.push({
          imgId: img.id,
          img,
          imageLabel: rows[0].imageLabel,
          groupLabel: rows[0].groupLabel,
          dataDate: rows[0].dataDate,
          rows,
        })
        continue
      }
      if ((img.aiOnly?.length ?? 0) > 0 || (img.supplements?.length ?? 0) > 0 || engineOf(img) === 'ai') {
        out.push({
          imgId: img.id,
          img,
          imageLabel: img.preview.image_filename,
          groupLabel: labelFor(img.sourceGroup),
          dataDate: img.preview.data_date,
          rows: [],
        })
      }
    }
    return out
  }, [entryRows, images, labelFor])

  // 原图查看器的图序（review-image-viewer D3）：与「待录入清单」卡片同序，
  // 卡片 id ↔ 查看器下标一一对应；地址优先 evidence 原图端点，无 evidence_id 回退本地 blob
  const viewerImages = useMemo<ViewableImage[]>(
    () =>
      entryGroups.map((g) => ({
        key: g.imgId,
        filename: g.imageLabel,
        evidenceId: g.img.preview?.evidence_id ?? null,
        src: reviewImageSrc(g.img),
      })),
    [entryGroups],
  )

  const openImageViewer = useCallback(
    (imgId: string) => {
      const idx = viewerImages.findIndex((v) => v.key === imgId)
      if (idx >= 0) setViewerIndex(idx)
    },
    [viewerImages],
  )

  // 离开复核阶段就收起查看器：否则「返回修改 → 再进清单」会自己弹出来
  useEffect(() => {
    if (phase !== 'review') setViewerIndex(null)
  }, [phase])

  // ════════════════════════════════════════════════════════════════
  // 总表 / 归父 / 核对（ADR-0029；票 05 D1/D3/D4/D6、票 09）
  // ════════════════════════════════════════════════════════════════

  // 图筛选器选项：按图序（= 上传顺序），标签「图 N」，副标签是文件名（窄屏下拉里省掉）
  const imageFilterOptions = useMemo(
    () =>
      Array.from(new Map(entryRows.map((r) => [r.imgId, { imgId: r.imgId, ordinal: r.imageOrdinal, label: r.imageLabel }])).values()).sort(
        (a, b) => a.ordinal - b.ordinal,
      ),
    [entryRows],
  )
  // 筛选后仍可能在清单里的图（含只有 AI 项/补录的图）：用于「筛选器不显示空图」
  const filterStillValid = imageFilter === 'all' || imageFilterOptions.some((o) => o.imgId === imageFilter)
  useEffect(() => {
    if (!filterStillValid) setImageFilter('all')
  }, [filterStillValid])

  // 总表行（跨图）：筛选 → 树序。父行与其子行保持相邻，子行只缩进名称列。
  const tableRows = useMemo(() => {
    const scoped = imageFilter === 'all' ? entryRows : entryRows.filter((r) => r.imgId === imageFilter)
    return orderAsTree(scoped)
  }, [entryRows, imageFilter])

  // 折叠后的可见行：父行折叠时整枝收起（只藏显示，不动数据与判定）。
  // 树序已保证父在子前，一遍扫描即可判定「祖先是否被折叠」
  const visibleTableRows = useMemo(() => {
    const hiddenAncestor = new Set<RowKey>()
    const out: typeof tableRows = []
    for (const t of tableRows) {
      const p = t.row.parent
      const hiddenByParent = p?.kind === 'row' && (hiddenAncestor.has(p.key) || !!foldedRows[p.key])
      if (hiddenByParent) {
        hiddenAncestor.add(t.row.key)
        continue
      }
      out.push(t)
    }
    return out
  }, [tableRows, foldedRows])

  // 移动端「只看疑点」：同一份树序，只挑出疑点行（其余行默认采纳、不打扰）
  const mobileTableRows = useMemo(
    () => (mobileOnlyIssues ? visibleTableRows.filter((t) => rowHasIssue(t.row)) : visibleTableRows),
    [visibleTableRows, mobileOnlyIssues],
  )

  // 树序用到的行面（判定与文案共用一份，避免「显示的顺序」与「判定的依据」各算一遍）
  const meterRows = useMemo<MeterRow[]>(
    () => entryRows.map((r) => ({ key: r.key, name: r.edit.name || r.row.source_name || '', amount: r.amount, aggregate: r.aggregate, parent: r.parent })),
    [entryRows],
  )
  // 「归到…」候选：本批汇总行 + 本次预览读到现值的既有资产项（后者只用手选题材，
  // 拿不到金额的既有项不会进来——父项金额读不到就没法核对）
  const existingItemOptions = useMemo<ExistingItemOption[]>(() => {
    const out = new Map<number, ExistingItemOption>()
    let ordinal = 0
    for (const img of images) {
      if (!img.preview) continue
      ordinal += 1
      for (const e of img.preview.existing_items ?? []) {
        if (e?.item_id == null || out.has(e.item_id)) continue
        out.set(e.item_id, { itemId: e.item_id, name: e.name, amount: e.amount, imageOrdinal: ordinal })
      }
    }
    return Array.from(out.values())
  }, [images])
  const parentPickerRow = useMemo(
    () => (parentPickerKey ? entryRows.find((r) => r.key === parentPickerKey) ?? null : null),
    [parentPickerKey, entryRows],
  )
  const parentCandidates = useMemo<ParentCandidate[]>(() => {
    if (!parentPickerRow) return []
    return buildParentCandidates(meterRows, parentPickerRow.key, existingItemOptions, (key) => {
      const hit = entryRows.find((r) => r.key === key)
      return hit?.imageOrdinal ?? 0
    })
  }, [parentPickerRow, meterRows, existingItemOptions, entryRows])

  // 核对父项：① 本批勾为汇总行的行 ② 被手选为父项的既有资产项（有子项才算一条核对）
  const reconcileItems = useMemo<ReconcileItem[]>(() => {
    const parents: ReconcileParentInput[] = []
    const children: ReconcileChildInput[] = []

    for (const r of entryRows) {
      if (!r.aggregate) continue
      parents.push({
        id: `row:${r.key}`,
        kind: 'row',
        name: r.edit.name || r.row.source_name || '未命名',
        imageOrdinal: r.imageOrdinal,
        amount: r.amount,
        // create 行本次才新建资产项：汇总行必须**先落成资产项**才能建构成关系
        itemId: r.row.item_id ?? null,
        evidenceId: r.img.preview?.evidence_id ?? null,
        rowIndex: r.index,
      })
    }
    for (const r of entryRows) {
      const p = r.parent
      if (!p) continue
      // 跳过的行本次不写入 ⇒ 不产生构成边，也不进父项的差额（票 05 D5）
      if (r.edit.skipped) continue
      if (p.kind === 'item') {
        if (!parents.some((x) => x.id === `item:${p.itemId}`)) {
          const opt = existingItemOptions.find((o) => o.itemId === p.itemId)
          parents.push({
            id: `item:${p.itemId}`,
            kind: 'item',
            name: p.name || opt?.name || '既有资产项',
            imageOrdinal: null,
            amount: opt?.amount ?? null,
            itemId: p.itemId,
            evidenceId: null,
            rowIndex: null,
          })
        }
        children.push({
          parentId: `item:${p.itemId}`,
          key: r.key,
          name: r.edit.name || r.row.source_name || '未命名',
          amount: r.amount,
          evidenceId: r.img.preview?.evidence_id ?? null,
          rowIndex: r.index,
          itemId: r.edit.bindTargetId ?? r.row.item_id ?? null,
        })
        continue
      }
      children.push({
        parentId: `row:${p.key}`,
        key: r.key,
        name: r.edit.name || r.row.source_name || '未命名',
        amount: r.amount,
        evidenceId: r.img.preview?.evidence_id ?? null,
        rowIndex: r.index,
        // 子项落库后的资产项：已关联的以目标为准，其次看预览命中项；本次才新建的行认不出 id
        itemId: r.edit.bindTargetId ?? r.row.item_id ?? null,
      })
    }
    return computeReconciliations(parents, children, acceptedDiffs)
  }, [entryRows, existingItemOptions, acceptedDiffs])

  const reconcileBlocking = useMemo(() => reconcileItems.filter((it) => it.blockKind !== null), [reconcileItems])

  // 批次级接受理由（契约冻结：`accept_mismatch` 只在顶层一条，条目内不重复）。
  // 多条差额被接受时共用这一条：各写各的时按「；」拼接并截到后端上限 200 字——
  // 拼接是为了不丢内容，截断是为了不 422；面板的输入框会用它做预填，尽量让用户写同一条
  const acceptedReason = useMemo(() => {
    const reasons = Array.from(
      new Set(
        reconcileItems
          .filter((it) => it.state === 'accepted' && it.accepted)
          .map((it) => (it.accepted as AcceptedMismatch).reason.trim())
          .filter((x) => x !== ''),
      ),
    )
    return reasons.length ? reasons.join('；').slice(0, 200) : ''
  }, [reconcileItems])

  // 不可作为「关联到已有项」目标的项：本批勾为汇总行的行命中的资产项（ADR-0029 D13）
  const aggregateItemBlocker = useMemo(() => {
    const m = new Map<number, string>()
    for (const r of entryRows) {
      if (!r.aggregate) continue
      const itemId = r.row.item_id
      if (itemId == null) continue
      m.set(itemId, r.edit.name || r.row.source_name || '汇总行')
    }
    return m
  }, [entryRows])

  // 接受差额：留痕里存的是**接受当时**的差额，差额再变即自动作废（面板判 stale）
  const acceptReconcileDiff = useCallback(
    (parentId: string, reason: string) => {
      const item = reconcileItems.find((it) => it.parent.id === parentId)
      if (!item || item.diff === null || !reason.trim()) return
      const diff = item.diff.toFixed(2)
      setAcceptedDiffs((prev) => ({
        ...prev,
        [parentId]: { diff, reason: reason.trim().slice(0, 200), at: Date.now() },
      }))
      pushRuleNotice('success', `已接受「${item.parent.name}」的差额 ${formatSignedCurrency(diff)}，理由已留痕`)
    },
    [reconcileItems, pushRuleNotice],
  )
  const withdrawReconcileAccept = useCallback(
    (parentId: string) => {
      setAcceptedDiffs((prev) => {
        if (!prev[parentId]) return prev
        const nextMap = { ...prev }
        delete nextMap[parentId]
        return nextMap
      })
      pushRuleNotice('info', '已撤回对差额的接受，这一行重新阻断写入')
    },
    [pushRuleNotice],
  )

  // 行内定位：核对面板「去改子项 / 定位父项」与来源标记跳转共用。
  // 折起/筛选掉的行走位会静默失败，所以先把它「弄到能看见」：父链展开、筛选退回全部，
  // 下一帧再滚（同一帧滚动时 DOM 还没重新渲染，scrollIntoView 会落空）
  const focusRow = useCallback(
    (key: RowKey, target: 'amount' | 'row' = 'row') => {
      if (typeof document === 'undefined') return
      const chain: RowKey[] = []
      let cur = entryRows.find((r) => r.key === key)
      let p = cur?.parent
      while (p && p.kind === 'row') {
        chain.push(p.key)
        cur = entryRows.find((r) => r.key === (p as { key: RowKey }).key)
        p = cur?.parent
      }
      if (chain.length) {
        setFoldedRows((prev) => {
          if (!chain.some((k) => prev[k])) return prev
          const nextMap = { ...prev }
          for (const k of chain) nextMap[k] = false
          return nextMap
        })
      }
      if (imageFilter !== 'all' && !entryRows.some((r) => r.key === key && r.imgId === imageFilter)) {
        setImageFilter('all')
      }
      setTimeout(() => {
        const el =
          document.getElementById(`review-row-${key}`) ?? document.getElementById(`review-row-m-${key}`)
        if (!el) return
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        if (target === 'amount') {
          el.querySelector<HTMLInputElement>('[data-row-amount] input')?.focus({ preventScroll: true })
        }
      }, 0)
    },
    [entryRows, imageFilter],
  )

  // 父项的文字标记：图 N · 名称（本批汇总行）／既有项 · 名称。两处渲染共用一个说法
  const parentTextOf = useCallback(
    (r: ReviewRow): string => {
      const p = r.parent
      if (!p) return ''
      if (p.kind === 'item') return `既有项 · ${p.name}`
      const parentRow = entryRows.find((x) => x.key === p.key)
      return `图 ${parentRow?.imageOrdinal ?? '?'} · ${
        parentRow?.edit.name || parentRow?.row.source_name || '汇总行'
      }`
    },
    [entryRows],
  )

  // 核对项的父行（本批汇总行才有行锚）：面板的「定位父项 / 去归子项」都要先找到它
  const reconcileParentRow = useCallback(
    (it: ReconcileItem): ReviewRow | null => {
      if (it.parent.kind !== 'row' || it.parent.rowIndex === null) return null
      return (
        entryRows.find(
          (r) => r.img.preview?.evidence_id === it.parent.evidenceId && r.index === it.parent.rowIndex,
        ) ?? null
      )
    },
    [entryRows],
  )

  const jumpToImage = useCallback((imgId: string) => {
    if (typeof document === 'undefined') return
    document.getElementById(`shot-${imgId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // 拖拽加速器（票 09 D3）：指针事件全程只用行首手柄，不设整行 draggable
  // （整行 draggable 会毁掉单元格里的文本拖选）；拖动期间不重排、不重算金额。
  // 落点要**停留满 500ms** 才算数（扫过一行就放下是最常见的误放），离开即清零。
  const onGripPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>, childKey: RowKey) => {
      if (event.button !== 0) return
      event.preventDefault()
      dragSessionRef.current = {
        childKey,
        startX: event.clientX,
        startY: event.clientY,
        started: false,
        targetKey: null,
        ok: false,
      }
    },
    [],
  )

  useEffect(() => {
    const rowUnder = (x: number, y: number): { key: RowKey } | null => {
      const el = document.elementFromPoint(x, y) as HTMLElement | null
      const row = el?.closest('[data-row-key]') as HTMLElement | null
      return row?.dataset.rowKey ? { key: row.dataset.rowKey } : null
    }
    // 只在落点真的变了才 setState：pointermove 一秒几十次，每次都重渲染整张总表会卡
    let lastTarget: RowKey | null = null
    let lastOk = false
    const paint = (childKey: RowKey, targetKey: RowKey | null, ok: boolean) => {
      if (lastTarget === targetKey && lastOk === ok) return
      lastTarget = targetKey
      lastOk = ok
      setDragView({ childKey, targetKey, ok })
    }
    // 停留判定：换一行就重新计时；计时器没到之前不显示任何投放态、也不允许放下
    let hoverKey: RowKey | null = null
    let dwellTimer: ReturnType<typeof setTimeout> | null = null
    const clearDwell = () => {
      if (dwellTimer !== null) {
        clearTimeout(dwellTimer)
        dwellTimer = null
      }
    }
    const resetHover = (childKey: RowKey) => {
      clearDwell()
      hoverKey = null
      document.body.style.cursor = 'grabbing'
      paint(childKey, null, false)
    }
    const onMove = (event: PointerEvent) => {
      const s = dragSessionRef.current
      if (!s) return
      if (!s.started) {
        if (Math.abs(event.clientX - s.startX) + Math.abs(event.clientY - s.startY) < 4) return
        s.started = true
        // 拖起来了就给出「正在拖动」的回执（dragView 非 null 即拖动态）
        paint(s.childKey, null, false)
        document.body.style.cursor = 'grabbing'
      }
      const hit = rowUnder(event.clientX, event.clientY)
      if (!hit || hit.key === s.childKey) {
        // 目标是自己/空白：不是「非法」，只是还没落到可接收的行上
        s.targetKey = null
        s.ok = false
        resetHover(s.childKey)
        return
      }
      if (hoverKey === hit.key && dwellTimer !== null) return // 同一行停留中：不重启计时
      if (hoverKey !== hit.key) {
        clearDwell()
        hoverKey = hit.key
        s.targetKey = null
        s.ok = false
        document.body.style.cursor = 'grabbing'
        paint(s.childKey, null, false)
        const key = hit.key
        const verdict = checkDrop(meterRows, s.childKey, key)
        dwellTimer = setTimeout(() => {
          dwellTimer = null
          const cur = dragSessionRef.current
          if (!cur || hoverKey !== key) return
          cur.targetKey = key
          cur.ok = verdict.ok
          document.body.style.cursor = verdict.ok ? 'copy' : 'not-allowed'
          paint(cur.childKey, key, verdict.ok)
        }, DROP_DWELL_MS)
      }
    }
    const finish = (commit: boolean) => {
      clearDwell()
      const s = dragSessionRef.current
      dragSessionRef.current = null
      document.body.style.cursor = ''
      setDragView(null)
      if (!s || !s.started) return
      if (!commit) {
        message.info('已取消拖动，什么都没变')
        return
      }
      // 停留未满就松手：不落到任何地方（这正是 500ms 判定要挡的那一下）
      if (!s.ok || !s.targetKey) {
        message.info('没有停在可放入的行上，什么都没变')
        return
      }
      const target = entryRows.find((r) => r.key === s.targetKey)
      const child = entryRows.find((r) => r.key === s.childKey)
      if (!target || !child) return
      assignParent(s.childKey, { kind: 'row', key: target.key }, target.edit.name || target.row.source_name || '汇总行')
    }
    const onUp = () => finish(true)
    const onCancel = () => finish(false)
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && dragSessionRef.current) {
        event.preventDefault()
        finish(false)
      }
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
    window.addEventListener('keydown', onKey)
    return () => {
      clearDwell()
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
      window.removeEventListener('keydown', onKey)
      document.body.style.cursor = ''
    }
  }, [meterRows, entryRows, assignParent])

  // --- 构建 apply options per image ---
  const buildApplyOptions = useCallback(
    (img: ImgState): ApplyOptions => {
      // AI 主识别图：unrecognized 恒为 keep（§4c「全部清0」双端禁用的前端第二道防线；
      // 单独清0 / 「不录入」联动 = 逐项显式决策，保留）。OCR 图载荷逐字节不变
      const isAi = engineOf(img) === 'ai'
      const effPolicy: UnrecognizedPolicy = isAi && unrecognizedPolicy === 'zeroAll' ? 'keep' : unrecognizedPolicy
      // 「全部清0」在同组多图时必须按批次名单下发：后端 unrecognized='all' 清的是
      // **本图**没命中的组内全部资产，多张图各清一遍 ⇒ 先应用的图会清掉后一张图刚写入
      // 的值（结果取决于图序）。同组只有一张图时保持旧载荷逐字段不变。
      const batchZeroAll = effPolicy === 'zeroAll' && batchImagesInGroup(images, img.sourceGroup) > 1
      const opts: ApplyOptions = {
        source_group: img.sourceGroup,
        skipped: [],
        amounts: {},
        unrecognized: effPolicy === 'zeroAll' && !batchZeroAll ? 'all' : 'keep',
        zero_names: [],
        names: {},
      }
      if (isAi) opts.engine = 'ai'
      // 批次成员（ADR-0029 D8）：只有在建壳成功后才带；无批次时载荷与今天逐字段一致
      const batchId = batchPlanRef.current?.id
      if (batchId != null) opts.batch_id = batchId
      if (!img.preview) return opts
      const identityDecisions: Record<string, IdentityDecisionPayload> = {}
      const reconciles: number[] = []
      for (const row of img.preview.rows) {
        const edit = img.rows[row.index]
        if (!edit) continue
        if (edit.skipped) {
          opts.skipped.push(row.index)
          continue
        }
        // 汇总行（ADR-0029 D5/D6）：行号进 `reconciles`，**不写 amounts/names/身份裁决**，
        // 也不参与 zero_names（后端另会把该项从清零候选剔除，回执 zero_skipped_due_to_reconcile）
        if (edit.aggregate && row.action !== 'keep') {
          reconciles.push(row.index)
          continue
        }
        if (row.action === 'keep') {
          // 只有 keepRows 里的 owner 行才是「批次未识别」且可能被用户显式决策的行；
          // 已被别张图覆盖而隐藏的重复行，即使残留勾选也不写，避免静默清零。
          const owned = ownedKeepKeys.has(`${img.id}\u0000${row.index}`)
          if (effPolicy === 'zeroAll') {
            // 单图同组 → 由 unrecognized='all' 处理；多图同组 → 只清 owner 图上的行
            if (batchZeroAll && owned) opts.zero_names.push(row.asset_name || '')
            continue
          }
          // 单独清0 勾选，或「不录入」联动（任何策略下）→ zero_names（资产全名精确匹配）
          if (owned && ((effPolicy === 'zeroEach' && edit.zeroChecked) || edit.saveExclude)) {
            opts.zero_names.push(row.asset_name || '')
          }
          continue // keep：不写入
        }
        if (row.action === 'update' || row.action === 'create') {
          opts.amounts[String(row.index)] = edit.amount
        }
        // 身份裁决（D4 / 票 01 D2）：只发用户已表态的行；未表态由 confirmWrite 前置阻断（400 是安全网）。
        // 优先级：bind（关联到已有项）> create（确认为新增）> merge（沿用旧项）。一行只发一个决策。
        // merge → 登记血缘（link_type 默认 revision，principal/effective_date 交给后端默认：
        // 本行金额 / 本次写入日期）；reason 留空时发一句事实性默认值（后端审计要求非空）。
        const bindId = edit.identityAction === 'bind' ? edit.bindTargetId ?? null : null
        if (bindId != null) {
          // 目标选定即视为表态：金额仍走本行 amounts（本行截图金额为准，票 01 D8/D9）
          identityDecisions[String(row.index)] = { action: 'bind', target_item_id: bindId }
        } else if (edit.identityAction === 'create') {
          identityDecisions[String(row.index)] = { action: 'create' }
        } else if (row.identity_guess && edit.identityAction === 'merge') {
          const candidates = row.identity_guess.candidates ?? []
          const picked = edit.identityTargetId
          const target =
            picked != null && candidates.some((c) => c.item_id === picked)
              ? picked
              : candidates.length === 1
                ? candidates[0].item_id
                : null
          if (target != null) {
            const reason = (edit.identityReason ?? '').trim()
            identityDecisions[String(row.index)] = {
              action: 'merge',
              target_item_id: target,
              link_type: 'revision',
              reason: reason || DEFAULT_IDENTITY_MERGE_REASON,
            }
          }
        }
        // zero 行金额固定为 0，不写入 amounts
        // 名称覆盖：对最终名称做直接替换。基准为行初始显示名（create 即 prefix+source，
        // 恰为 row.asset_name）；用户未改（edit.name === 基准）则不写 names，走原加 prefix 逻辑。
        if (edit.name !== (row.asset_name ?? row.source_name ?? '')) {
          opts.names[String(row.index)] = edit.name
        }
      }
      // 补录项：走独立通道追加（上面这个按行号的循环一行不改，行号契约不受补录影响）。
      // 只发 {name, amount, side}，不发 uid / resolved；过滤空名、金额非法与已判定冲突的项。
      // 无补录时不带 supplements 键 ⇒ 载荷与今天逐字段相同（向后兼容硬要求）
      if (Object.keys(identityDecisions).length) opts.identity_decisions = identityDecisions
      // 汇总行行号：无汇总行时不带该键（旧后端零影响）
      if (reconciles.length) opts.reconciles = reconciles
      const sup = (img.supplements ?? [])
        .filter(isSendableSupplement)
        .map((d) => ({ name: d.name.trim(), amount: d.amount.trim(), side: d.side }))
      if (sup.length) opts.supplements = sup
      return opts
    },
    [unrecognizedPolicy, images, ownedKeepKeys],
  )

  // --- AI 路径识别快照回传（ADR-0023 D7 / 票 03 第 5 项）---
  // `recognized` 要的是**机器给的**那批行：直接用 `img.aiOnly`（verify 的 `ai_only` 原始
  // name/amount），而不是 `supplements` 里的采纳草稿 —— 后者混了用户改名/改金额/改方向，
  // 属 adopted 侧，混进来就失去了「识别率」的可比性。
  // 只有 AI 引擎图回传；OCR 图服务端自跑 OCR、自带 blocks 快照，且后端 engine!=ai 时本就
  // 走服务端分支，前端按「OCR 载荷逐字节不变」的硬要求恒不发。
  // `aiOnly` 为 null（识别未跑/失败）= 本次没有机器结果 → 不发：后端据此记
  // items:null + note:"client-side"（未知 ≠ 零项，别用空数组冒充）。
  const buildRecognition = useCallback(
    (img: ImgState): VerifyAiOnlyItem[] | undefined => {
      if (engineOf(img) !== 'ai') return undefined
      const items = img.aiOnly
      if (!items) return undefined
      return items.map((it) => ({ name: it.name, amount: it.amount }))
    },
    [],
  )

  // --- 把用户勾选的「存为规则」反哺成该来源组的校正规则（非阻断，不影响写入） ---
  const savePendingRules = useCallback(async () => {
    const pending: { slug: string; type: 'rename' | 'ignore'; match: string; replace?: string; note?: string }[] = []
    for (const r of reviewRows) {
      const slug = r.img.sourceGroup
      if (!slug) continue
      if (r.edit.saveRename && renameRuleEligible(r)) {
        // 备注可选：为空不携带 note，后端保持旧存储格式（?? '' 兼容无该字段的旧行状态）
        const note = (r.edit.renameNote ?? '').trim()
        pending.push({
          slug, type: 'rename', match: r.row.source_name as string, replace: r.edit.name,
          ...(note ? { note } : {}),
        })
      }
      // ignore 规则仅在「行仍被跳过」时才落（与开关可见性一致；取消跳过后不再保存）
      if (r.edit.saveIgnore && r.edit.skipped && ignoreRuleEligible(r)) {
        const note = (r.edit.ignoreNote ?? '').trim()
        pending.push({ slug, type: 'ignore', match: ignoreRuleMatch(r), ...(note ? { note } : {}) })
      }
    }
    if (!pending.length) return

    let saved = 0
    let existed = 0
    const failed: string[] = []
    for (const p of pending) {
      try {
        await api.saveCorrectionRule(p.slug, { type: p.type, match: p.match, replace: p.replace, note: p.note })
        saved++
      } catch (e) {
        const msg = e instanceof Error ? e.message : '请求失败'
        if (/已存在|exists|duplicate/i.test(msg)) existed++
        else failed.push(msg)
      }
    }

    // 已消费的行级开关与备注重置
    setImages((prev) =>
      prev.map((p) => {
        let changed = false
        const rows = { ...p.rows }
        const keys = Object.keys(rows).map(Number)
        for (const k of keys) {
          const row = rows[k]
          if (row.saveRename || row.saveIgnore) {
            rows[k] = { ...row, saveRename: false, saveIgnore: false, renameNote: '', ignoreNote: '' }
            changed = true
          }
        }
        return changed ? { ...p, rows } : p
      }),
    )

    let kind: 'success' | 'info' | 'error' = 'success'
    const parts: string[] = []
    if (saved) parts.push(`已保存 ${saved} 条规则`)
    if (existed) {
      kind = 'info'
      parts.push(`${existed} 条已存在`)
    }
    if (failed.length) {
      kind = 'error'
      parts.push(`${failed.length} 条失败：${failed[0]}`)
    }
    pushRuleNotice(kind, parts.join('；'))
  }, [reviewRows, pushRuleNotice])

  // --- 「不录入」落库（exclude 规则）---
  // ⚠️ 时序红线：必须在写 0 的 apply 成功之后调用——exclude 落库后该资产不再进
  // keep 列表，届时再翻 zero_names 是 no-op；因此只对本次写入成功的图落库
  // （失败图的 0 未落库，不能排除，否则旧余额会无声滞留）。跳过行不落（未写入）。
  const savePendingExcludes = useCallback(
    async (okImgIds: Set<string>) => {
      const seen = new Set<string>()
      const pending: { slug: string; match: string; note?: string }[] = []
      for (const r of keepRows) {
        if (!r.edit.saveExclude || r.edit.skipped) continue
        if (!okImgIds.has(r.imgId)) continue
        const slug = r.img.sourceGroup
        const match = (r.row.asset_name || r.edit.name).trim() // 契约：资产全名（含前缀）精确匹配
        if (!slug || !match) continue
        const key = `${slug}\u0000${match}`
        if (seen.has(key)) continue // 同资产出现在多张图只落一次（后端判重亦幂等）
        seen.add(key)
        const note = (r.edit.excludeNote ?? '').trim()
        pending.push({ slug, match, ...(note ? { note } : {}) })
      }
      if (!pending.length) return

      // 与 ignore/rename 同套计数与非阻断提示逻辑（已存在=静默幂等，不报错）
      let saved = 0
      let existed = 0
      const failed: string[] = []
      for (const p of pending) {
        try {
          await api.saveCorrectionRule(p.slug, { type: 'exclude', match: p.match, note: p.note })
          saved++
        } catch (e) {
          const msg = e instanceof Error ? e.message : '请求失败'
          if (/已存在|exists|duplicate/i.test(msg)) existed++
          else failed.push(msg)
        }
      }

      // 仅重置写入成功图的行级开关（失败图保留勾选，供用户回去处理）
      setImages((prev) =>
        prev.map((p) => {
          if (!okImgIds.has(p.id)) return p
          let changed = false
          const rows = { ...p.rows }
          for (const k of Object.keys(rows).map(Number)) {
            const row = rows[k]
            if (row.saveExclude) {
              rows[k] = { ...row, saveExclude: false, excludeNote: '' }
              changed = true
            }
          }
          return changed ? { ...p, rows } : p
        }),
      )

      let kind: 'success' | 'info' | 'error' = 'success'
      const parts: string[] = []
      if (saved) parts.push(`已设 ${saved} 项「不录入」，以后不再出现在未识别列表`)
      if (existed) {
        kind = 'info'
        parts.push(`${existed} 项此前已设置`)
      }
      if (failed.length) {
        kind = 'error'
        parts.push(`${failed.length} 项失败：${failed[0]}`)
      }
      pushRuleNotice(kind, parts.join('；'))
    },
    [keepRows, pushRuleNotice],
  )

  // --- 批次结算（ADR-0029 D9）：载荷构造与错误翻译，runApply / 重试结算共用 ---
  // 错误翻译的上下文：把后端给的 evidence_id / item_id 换成人能认的「第 N 张 · 文件名」「某行名称」
  const batchErrorContext = useMemo(
    () => ({
      imageLabelOf: (evidenceId: number) => {
        const hit = entryRows.find((r) => r.img.preview?.evidence_id === evidenceId)
        if (hit) return `第 ${hit.imageOrdinal} 张 · ${hit.imageLabel}`
        const img = images.find((p) => p.preview?.evidence_id === evidenceId)
        return img?.preview?.image_filename || `凭证 #${evidenceId}`
      },
      parentNameOfItem: (itemId: number) => {
        const hit = entryRows.find((r) => r.row.item_id === itemId)
        return hit ? hit.edit.name || hit.row.source_name || null : null
      },
    }),
    [entryRows, images],
  )

  const buildFinalizeBody = useCallback((): ScreenshotBatchFinalizeBody => {
    return {
      reconciliations: toReconcilePayloads(reconcileItems),
      ...(acceptedReason ? { accept_mismatch: { reason: acceptedReason } } : {}),
    }
  }, [reconcileItems, acceptedReason])

  // 写入已成功、只有结算失败时的重试：只重发 finalize（绝不重发 apply），
  // 幂等键每次重试都新生成——上一次可能是业务拒绝（差额/理由），用同一个键会回放旧结论
  const retryFinalize = useCallback(async () => {
    const batch = batchPlanRef.current
    if (!batch) {
      setErrorMsg('没有待结算的批次：请回到清单重新提交这一批')
      return
    }
    setApplyPending(true)
    try {
      await api.finalizeScreenshotBatch(batch.id, buildFinalizeBody(), newIdemKey())
      setFinalizeState({ status: 'ok', message: null, batchId: batch.id })
      setErrorMsg(null)
      batchPlanRef.current = null
      appliedOkRef.current = new Set()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'BATCH_ALREADY_FINALIZED') {
        setFinalizeState({ status: 'ok', message: null, batchId: batch.id })
        setErrorMsg(null)
        batchPlanRef.current = null
        appliedOkRef.current = new Set()
      } else {
        const text =
          e instanceof ApiError
            ? describeBatchError(e.code, e.details, batchErrorContext)
            : { title: '这一批没能结算', detail: e instanceof Error ? e.message : null }
        const msg = text.detail ? `${text.title}：${text.detail}` : text.title
        setFinalizeState({ status: 'failed', message: msg, batchId: batch.id })
        setErrorMsg(`写入已完成，但${msg}`)
      }
    } finally {
      setApplyPending(false)
    }
  }, [buildFinalizeBody, batchErrorContext])

  // 「重试第 N 张」在 runApply 之后定义（保持「先定义后使用」的顺序，不用 ref 绕）

  // --- 确认写入（三段编排：建批次 → 逐图 apply（带 batch_id）→ finalize）---
  // runApply：真正的写入循环（校验与确认都在 confirmWrite 里做完才进来）。
  // mode='auto' = 新的一次写入动作（重置批次/累计）；'retry' = 只重试失败的那些图，
  // **沿用同一个 batch_id 与该图的幂等键**（换批次会让成员散在两批里，finalize 永远等不齐）。
  const runApply = useCallback(
    async (targets: ImgState[], mode: 'auto' | 'retry' = 'auto') => {
      setErrorMsg(null)
      if (mode === 'auto') {
        // 新动作：批次与累计清零；上一轮的结算结果也不再适用
        batchPlanRef.current = null
        appliedOkRef.current = new Set()
        applyResultsRef.current = []
        setFinalizeState(null)
        setFailedApply(null)
        // 先落校正规则（独立于写入，失败/已存在均不阻断）
        await savePendingRules()
      }
      setApplyPending(true)
      setApplyProgress(0)
      setApplyTotal(targets.length)

      // ① 建批次（ADR-0029 D8）：expected_members = 本批**图片数**（含已写入成功的）。
      // 后端按成员是否齐来判 BATCH_NOT_COMPLETE，少报一张就永远结算不了
      const needsBatch = reconcileItems.length > 0
      if (batchPlanRef.current === null) {
        const expected = appliedOkRef.current.size + targets.length
        const groups = new Set(targets.map((t) => t.sourceGroup))
        const date = targets[0]?.selectedDate ?? null
        try {
          const created = await api.createScreenshotBatch(
            {
              data_date: date || null,
              source_group: groups.size === 1 ? targets[0].sourceGroup : null,
              expected_members: Math.max(1, expected),
            },
            newIdemKey(),
          )
          batchPlanRef.current = { id: created.batch_id, expected: Math.max(1, expected) }
        } catch (e) {
          const unsupported = e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 501)
          if (needsBatch) {
            // 有汇总行/核对项却拿不到批次：绝不能退回单图写入——那样构成关系一条也建不起来，
            // 用户会以为「归父成功、父项不计入合计」，而实际什么都没发生
            setApplyPending(false)
            setErrorMsg(
              unsupported
                ? '后端批次接口还没上线：本批有汇总行，需要批次才能核对。请先取消汇总行的勾选再写入'
                : `批次创建失败（${e instanceof Error ? e.message : '请求异常'}）：本次没有写入任何数据，请重试`,
            )
            return
          }
          // 无核对项：退回旧单图写入（载荷与今天逐字段一致），只是没有批次留痕
          message.info('批次接口暂不可用，本次按单图写入（写入结果不受影响）')
        }
      }

      const okImgIds = new Set<string>()
      const identityFailures: { filename: string; count: number }[] = []
      const failedThisRun: ImgState[] = []
      let done = 0
      for (const img of targets) {
        try {
          // 顶层 data_date 携带该图用户确认的日期；缺失（旧状态）时 api 不 append，后端走旧语义
          // 幂等键：首次生成分配给该图，重点击复用同键（超时后台可能已写）→ 命中回放不双写
          const key = applyIdemRef.current.get(img.id) ?? newIdemKey()
          applyIdemRef.current.set(img.id, key)
          const res = await api.screenshotApply(img.file, buildApplyOptions(img), img.selectedDate, key, buildRecognition(img))
          applyResultsRef.current.push(res)
          okImgIds.add(img.id)
          appliedOkRef.current.add(img.id)
          applyIdemRef.current.delete(img.id)  // 确认成功 → 解锁，下次录入是全新动作
        } catch (e) {
          failedThisRun.push(img)
          // 身份裁决缺口（安全网；正常路径由 confirmWrite 前置阻断）：
          // 只标出后端点名的行、保留其它行已作的选择，整图零写入。
          if (e instanceof ApiError && e.code === 'identity_decision_required') {
            const required = parseIdentityRequiredRows(e.details)
            const keys = new Set(required.map((x) => x.key))
            setImages((prev) =>
              prev.map((p) => {
                if (p.id !== img.id || !p.preview) return p
                const rows = { ...p.rows }
                // 后端揪出的行若不在本地预览里（旧预览/热更新前状态），把候选补进行数据，
                // 让裁决控件真的出现，用户才有办法处理；已有 identity_guess 的行不动。
                const previewRows = p.preview.rows.map((pr) => {
                  if (!keys.has(pr.index) || pr.identity_guess) return pr
                  const d = required.find((x) => x.key === pr.index)
                  return d
                    ? { ...pr, identity_guess: { reason: 'same_account_new_name', candidates: d.candidates } }
                    : pr
                })
                for (const key of keys) {
                  const pr = p.preview.rows.find((x) => x.index === key)
                  if (!pr) continue
                  const cur = rows[key]
                  rows[key] = cur
                    ? { ...cur, identityBlocked: true, identityAction: null }
                    : {
                        name: pr.asset_name ?? pr.source_name ?? '',
                        amount: pr.source_amount,
                        skipped: false,
                        zeroChecked: false,
                        saveRename: false,
                        saveIgnore: false,
                        renameNote: '',
                        ignoreNote: '',
                        saveExclude: false,
                        excludeNote: '',
                        identityBlocked: true,
                      }
                }
                return { ...p, preview: { ...p.preview, rows: previewRows }, rows }
              }),
            )
            identityFailures.push({
              filename: img.preview?.image_filename || '图片',
              count: keys.size,
            })
            setErrorMsg(
              `${img.preview?.image_filename || '图片'}：还有 ${keys.size} 行需要确认身份（疑似改名/拆分）。已在清单中标红，请选择「沿用旧项 / 作为新资产新增 / 撤销该行」后再写入${applyResultsRef.current.length ? '；其余图片已写入' : '；本图未写入任何数据'}`,
            )
          } else if (e instanceof ApiError && e.status < 500) {
            // 业务校验失败（如候选已归档、缺理由）：后端明确没写，别套用「可能仍在写入」的吓人提示
            setErrorMsg(
              `${img.preview?.image_filename || '图片'}：未写入（${e.message}）。请按提示调整后重试`,
            )
          } else {
            // 超时/断连不代表写入失败——apply 可能仍在后台完成，明确劝阻连续重试防重复写入
            setErrorMsg(
              `${img.preview?.image_filename || '图片'}：写入请求失败（${e instanceof Error ? e.message : '网络异常'}）。数据可能仍在后台完成写入，请稍后刷新记录页确认，勿连续重试`,
            )
          }
        }
        done++
        setApplyProgress(done)
      }
      setApplyResults(applyResultsRef.current.slice())
      setApplyIdentityBlocked(identityFailures)

      // ② 有图失败 → 不 finalize（D9：校验只在 finalize；批次允许 partial，永不跨图回滚）
      if (failedThisRun.length > 0) {
        setApplyPending(false)
        setFailedApply({
          imgIds: failedThisRun.map((i) => i.id),
          labels: failedThisRun.map((i) => i.preview?.image_filename || '图片'),
        })
        if (failedThisRun.length === 1) {
          const ord = images.findIndex((p) => p.id === failedThisRun[0].id) + 1
          setErrorMsg(`第 ${ord} 张（${failedThisRun[0].preview?.image_filename || '图片'}）写入失败，可重试；这一批还没有结算`)
        } else {
          setErrorMsg(`${failedThisRun.length} 张写入失败，可重试；这一批还没有结算`)
        }
        // 成功的那几张已经落库：列表侧的数据同样要刷（否则用户看到的是写入前的值）
        if (okImgIds.size > 0) {
          queryClient.invalidateQueries({ queryKey: ['assets'] })
          queryClient.invalidateQueries({ queryKey: ['dates'] })
          queryClient.invalidateQueries({ queryKey: ['summary'] })
          queryClient.invalidateQueries({ queryKey: ['assetNames'] })
          queryClient.invalidateQueries({ queryKey: ['assetItems'] })
          await savePendingExcludes(okImgIds)
        }
        return
      }

      // ③ 全部成功 → finalize（唯一校验点；差额未处理会被 409 拦回来）
      const batch = batchPlanRef.current
      if (batch) {
        const body = buildFinalizeBody()
        try {
          await api.finalizeScreenshotBatch(batch.id, body, newIdemKey())
          setFinalizeState({ status: 'ok', message: null, batchId: batch.id })
          // 结算完成：这一批到此为止，下次写入是全新批次
          batchPlanRef.current = null
          appliedOkRef.current = new Set()
        } catch (e) {
          if (e instanceof ApiError && e.code === 'BATCH_ALREADY_FINALIZED') {
            // 上一次结算其实成功了（响应丢了/重放）：按成功处理，不重复建构成边
            setFinalizeState({ status: 'ok', message: null, batchId: batch.id })
            batchPlanRef.current = null
            appliedOkRef.current = new Set()
          } else {
            const text =
              e instanceof ApiError
                ? describeBatchError(e.code, e.details, batchErrorContext)
                : { title: '这一批没能结算', detail: e instanceof Error ? e.message : null }
            const msg = text.detail ? `${text.title}：${text.detail}` : text.title
            setFinalizeState({ status: 'failed', message: msg, batchId: batch.id })
            setErrorMsg(`写入已完成，但${msg}`)
          }
        }
      }

      setApplyPending(false)
      if (applyResultsRef.current.length) {
        setPhase('result')
        queryClient.invalidateQueries({ queryKey: ['assets'] })
        queryClient.invalidateQueries({ queryKey: ['dates'] })
        queryClient.invalidateQueries({ queryKey: ['summary'] })
        queryClient.invalidateQueries({ queryKey: ['assetNames'] })
        // 本次可能新建/改写了资产项：关联选择器的候选列表要跟着刷新
        queryClient.invalidateQueries({ queryKey: ['assetItems'] })
        // ⚠️ 时序红线：exclude 必须在写 0 的 apply 成功之后落库（savePendingExcludes 注释）
        await savePendingExcludes(okImgIds)
      }
    },
    [buildApplyOptions, buildRecognition, queryClient, savePendingRules, savePendingExcludes, reconcileItems, buildFinalizeBody, batchErrorContext, images],
  )

  // 「重试第 N 张」：只重发失败的图，沿用同一批次（成员才能补齐）与各自的幂等键
  const retryFailedApply = useCallback(() => {
    if (!failedApply) return
    const again = parsedImages.filter((p) => failedApply.imgIds.includes(p.id))
    if (!again.length) {
      setErrorMsg('要重试的截图已不在清单里，请回到上一步重新解析')
      return
    }
    setFailedApply(null)
    void runApply(again, 'retry')
  }, [failedApply, parsedImages, runApply])

  const confirmWrite = useCallback(async () => {
    const writableImgIds = new Set(writableRows.map((r) => r.imgId))
    // 只有补录、没有可写 OCR 行的图也要进 apply，否则补录永远写不进去
    const supplementImgIds = new Set(sendableSupplements.map((x) => x.imgId))
    // AI 图守卫（§4a）：「零采纳且零清零决策」不进 apply —— 空骨架 + 空 supplements
    // 只会制造一份什么都没写的空凭证；清零决策口径与 buildApplyOptions 的 zero_names 一致
    const aiImgHasWriteIntent = (img: ImgState): boolean => {
      if (supplementImgIds.has(img.id)) return true
      if (!img.preview) return false
      return img.preview.rows.some((r) => {
        if (r.action !== 'keep') return false
        const e = img.rows[r.index]
        if (!e || e.skipped) return false
        return e.saveExclude || (unrecognizedPolicy === 'zeroEach' && e.zeroChecked)
      })
    }
    // 带汇总行的图即使一行都不落账也要进 apply：`reconciles` 行号是随该图的
    // apply 载荷下发的，跳过这张图就等于这一行从没被标成汇总行
    const aggregateImgIds = new Set(
      entryRows.filter((r) => r.aggregate && !r.edit.skipped).map((r) => r.imgId),
    )
    const targets = parsedImages.filter((img) => {
      if (engineOf(img) === 'ai') return aiImgHasWriteIntent(img) || aggregateImgIds.has(img.id)
      return writableImgIds.has(img.id) || supplementImgIds.has(img.id) || aggregateImgIds.has(img.id)
    })
    if (!targets.length) {
      setErrorMsg('没有可写入的解析结果')
      return
    }
    // 身份裁决前置阻断（400 只是安全网）：未表态 / 多候选未选目标时，绝不发 apply。
    if (identityBlockers.length > 0) {
      const names = identityBlockers
        .slice(0, 3)
        .map((r) => `「${r.edit.name || r.row.source_name || '未命名'}」`)
        .join('、')
      setErrorMsg(
        `还有 ${identityBlockers.length} 行需要确认身份（疑似改名/拆分）：${names}${identityBlockers.length > 3 ? ' 等' : ''}。请在对应行选择「沿用旧项 / 作为新资产新增 / 撤销该行」后再写入`,
      )
      return
    }
    // 新增行待表态（D4 扩面）：关联到已有项 / 确认新增，二选一后再写入
    if (createPendingRows.length > 0) {
      const names = createPendingRows
        .slice(0, 3)
        .map((r) => `「${r.edit.name || r.row.source_name || '未命名'}」`)
        .join('、')
      setErrorMsg(
        `还有 ${createPendingRows.length} 行新增待确认：${names}${createPendingRows.length > 3 ? ' 等' : ''}。请先在行内「关联到已有项」，或点「本批全部确认新增」后再写入`,
      )
      return
    }
    for (const r of writableRows) {
      if (r.displayAction === 'update' || r.displayAction === 'create') {
        const v = r.edit.amount
        if (v == null || v.trim() === '' || !isFinite(Number(v))) {
          setErrorMsg(`「${r.edit.name || '未知'}」的金额无效，请检查`)
          return
        }
      }
    }
    // 汇总行也要有合法金额：它的金额正是核对的父项金额，为空/非数字时核对面板会算不出来
    for (const r of entryRows) {
      if (!r.aggregate || r.edit.skipped) continue
      const v = r.edit.amount
      if (v == null || v.trim() === '' || !isFinite(Number(v))) {
        setErrorMsg(`汇总行「${r.edit.name || '未知'}」的金额无效，请检查`)
        return
      }
    }
    // 核对前置阻断（D9/D10：finalize 才是唯一校验点，但绝不把明知会被拒的一批发出去）：
    // 差额未处理 / 接受已过期 / 汇总行本次才新建资产项 —— 三种都指到具体行
    if (reconcileBlocking.length > 0) {
      const names = reconcileBlocking
        .slice(0, 3)
        .map((it) => `「${it.parent.name}」`)
        .join('、')
      const kinds = new Set(reconcileBlocking.map((it) => it.blockKind))
      const tail = kinds.has('parent-not-asset')
        ? '汇总行需先落成资产项：先取消这里的「汇总」勾选写入，再回来勾'
        : kinds.has('stale')
          ? '接受过的差额又变了，需要重新接受'
          : '差额还没对上：改子项金额，或在这一行接受差额并写明理由'
      setErrorMsg(`还有 ${reconcileBlocking.length} 条核对没处理：${names}${reconcileBlocking.length > 3 ? ' 等' : ''}。${tail}`)
      return
    }
    // 补录项内容校验：空名 / 金额非法会让后端整单 400（契约 §3），就地拦下而不是静默丢弃
    for (const { img, draft } of supplementDrafts) {
      if (draft.resolved?.action === 'conflict') continue // 冲突项本就不发送
      if (draft.name.trim() === '') {
        setErrorMsg(`「${img.preview?.image_filename || '图片'}」有补录项名称为空，请填写或移除后再写入`)
        return
      }
      if (!supplementAmountValid(draft.amount)) {
        setErrorMsg(`补录项「${draft.name.trim()}」的金额无效（需大于 0），请修改或移除后再写入`)
        return
      }
    }
    if (writableRows.length === 0 && sendableSupplementCount === 0 && aggregateImgIds.size === 0) {
      setErrorMsg('请至少保留一条待写入的记录')
      return
    }
    // 写入前把「判定不可见 / 可能与跳过行重复 / 冲突不写入」一次说清（补录不是盲写）
    const warns: string[] = []
    if (unresolvedSupplementCount > 0) {
      warns.push(
        `${unresolvedSupplementCount} 项补录还没判定成功（判定服务不可用或刚改过），写入时由后端判定，冲突项会被跳过并在结果页列出`,
      )
    }
    if (skipCrossSupplements.length > 0) {
      warns.push(
        `${skipCrossSupplements.length} 项补录与你已跳过的识别行同名：跳过的行不写入，补录项会照常写入`,
      )
    }
    if (conflictSupplementCount > 0) {
      warns.push(`${conflictSupplementCount} 项补录判定为冲突，本次不会写入`)
    }
    if (warns.length) {
      Modal.confirm({
        title: '确认写入',
        content: (
          <div className="text-sm text-slate-600 space-y-2">
            <p>写入前请留意补录项：</p>
            <ul className="text-xs text-slate-500 list-disc pl-4 space-y-1">
              {warns.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        ),
        okText: '仍然写入',
        cancelText: '返回检查',
        onOk: () => runApply(targets),
      })
      return
    }
    await runApply(targets)
  }, [
    parsedImages,
    writableRows,
    supplementDrafts,
    sendableSupplements,
    sendableSupplementCount,
    unresolvedSupplementCount,
    skipCrossSupplements,
    conflictSupplementCount,
    unrecognizedPolicy,
    identityBlockers,
    createPendingRows,
    entryRows,
    reconcileBlocking,
    runApply,
  ])

  // --- AI 校验（第二意见）：逐图复用已上传的 File 组 FormData 调 verify，结论存回该图行状态 ---
  // 单图核心：整批校验、「强制重新 OCR」后的自动重校验与 AI 主识别编排共用同一条路径，结论口径完全一致
  // 返回 outcome：judged=有行级结论或 ai_only / unjudged=调用成功但零产出 / failed=请求失败（已回写失败态）；
  // aiOnly 直接随返回值带出（setState 异步落地，AI 编排不能靠回读 imagesRef 拿新鲜值）
  const verifyOne = useCallback(
    async (img: ImgState): Promise<{ outcome: 'judged' | 'unjudged' | 'failed'; aiOnly: VerifyAiOnlyItem[] }> => {
      if (!img.preview) return { outcome: 'unjudged', aiOnly: [] }
      setImages((prev) =>
        prev.map((p) => (p.id === img.id ? { ...p, verifyStatus: 'verifying', verifyError: null } : p)),
      )
      try {
        const rows = img.preview.rows
          .filter((r) => r.action !== 'keep')
          .map((r) => ({ index: r.index, name: r.source_name, amount: r.source_amount }))
        const formData = new FormData()
        formData.append('file', img.file)
        formData.append('rows', JSON.stringify(rows))
        const resp = await api.verifyScreenshot(formData, providerId)
        const byIndex: Record<number, VerifyRow> = {}
        for (const v of resp.rows) byIndex[v.index] = v
        const aiOnly = resp.ai_only ?? []
        setImages((prev) =>
          prev.map((p) =>
            p.id === img.id
              ? { ...p, verifyByIndex: byIndex, aiOnly, verifyStatus: 'done', verifyError: null }
              : p,
          ),
        )
        // 全 keep 的图发过去 rows=[]，AI 只会回 ai_only（没有行级结论）——这也算「给出了结论」，
        // 否则会误报「AI 未给出校验结论，请检查配置」
        const judged = resp.rows.some((v) => v.verdict !== 'unverified') || aiOnly.length > 0
        return { outcome: judged ? 'judged' : 'unjudged', aiOnly }
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'AI 校验请求失败'
        // 失败即清掉该图上一轮旧结论，避免过期 verdict 与「失败」态混排误导
        setImages((prev) =>
          prev.map((p) =>
            p.id === img.id
              ? { ...p, verifyByIndex: null, aiOnly: null, verifyStatus: 'failed', verifyError: msg }
              : p,
          ),
        )
        return { outcome: 'failed', aiOnly: [] }
      }
    },
    [providerId],
  )

  // --- AI 主识别编排（peer-entry-design §5，单图生命周期）---
  // preview(engine=ai) 空骨架就绪 → 自动 verifyOne(rows=[])：这是「识别」而非「校验」，
  // 不受 autoVerify 开关约束 → ai_only 全量自动转采纳草稿（addSupplements 按名去重 + 整批 resolve）。
  // 失败 / 零产出 = 图级显式阻断态（§1.2：绝不静默给空清单——空清单 + 误清零是最坏事故），
  // 卡片渲染失败条给「重试 / 转用 OCR」两个显式动作；批内其余图与写入流不受影响
  const runAiRecognition = useCallback(
    async (imgId: string, preview: ScreenshotPreviewResponse) => {
      const base = imagesRef.current.find((p) => p.id === imgId)
      if (!base) return
      const { outcome, aiOnly } = await verifyOne({ ...base, preview, verifyStatus: 'idle' })
      if (outcome === 'judged') {
        if (aiOnly.length) addSupplements(imgId, aiOnly, 'auto')
        // 识别结果回写快照（evidence-recognition-writeback D6）：AI 识别是前端发起的，
        // 提交 preview job 时手里没有识别行，不补写则「只预览不采纳」的会话快照永远是
        // null（识别率无从对拍）。只写 recognized 段，adopted 段归 /apply。
        // **失败静默**：回写是旁证，绝不 toast、绝不阻断识别与写入主流程。
        // 只在 judged（AI 确实给出了识别行）时写：unjudged＝零产出/无供应商，属「未知」，
        // 写空数组会把「没结论」冒充成「识别出 0 项」（与 buildRecognition 的 null 语义一致）。
        const evidenceId = preview.evidence_id
        if (evidenceId != null) {
          api.writebackEvidenceRecognition(
            evidenceId,
            aiOnly.map((it) => ({ name: it.name, amount: it.amount })),
          ).catch(() => { /* 回写失败忽略：不影响识别结果与页面其它行为 */ })
        }
        return
      }
      if (outcome === 'unjudged') {
        // 调用成功但零产出：等同识别失败（供应商不可用 / 模型未认出），标阻断态给显式动作
        setImages((prev) =>
          prev.map((p) =>
            p.id === imgId
              ? {
                  ...p,
                  verifyStatus: 'failed',
                  verifyError: 'AI 未识别出任何条目：请检查 LLM 供应商配置，或重试 / 转用 OCR 识别',
                }
              : p,
          ),
        )
      }
      // outcome === 'failed'：verifyOne 已回写 verifyStatus='failed' + verifyError
    },
    [verifyOne, addSupplements],
  )

  // 批量解析的轮询回调（startParse，声明在前）经 ref 间接调用，避免声明顺序成环
  useEffect(() => {
    runAiRecognitionRef.current = runAiRecognition
  }, [runAiRecognition])

  // 单图「重试」AI 识别（失败条内入口）：按当前 preview 重跑识别编排
  const retryAiRecognition = useCallback(
    (imgId: string) => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img?.preview || img.parsing || img.verifyStatus === 'verifying') return
      void runAiRecognition(imgId, img.preview)
    },
    [runAiRecognition],
  )

  // 逐图串行：每张图独立流转 校验中→已校验/失败，单图失败降级跳过、不中断整批（不影响写入流）
  const runVerify = useCallback(async () => {
    // 排除正在校验中的图（可能是强制重新 OCR 后触发的单图重校验），避免同图并发两次 verify。
    // 全 keep 的图也要校验：rows=[] 是后端 /verify 的缺省入参，AI 仍会回 ai_only —— 那正是
    // 「OCR 什么都没认出」时唯一的补录来源，跳过它等于把补录入口对这类图彻底关掉。
    // AI 主识别图排除在外：其 verify 由识别编排（runAiRecognition/重试入口）承担，双路比对对它无意义
    const targets = images.filter((p) => p.preview && p.verifyStatus !== 'verifying' && engineOf(p) !== 'ai')
    if (!targets.length) {
      // 纯 AI 批不弹「没有可校验项」（autoVerify 偏好对其不适用，识别编排自动跑）
      if (images.some((p) => p.preview && engineOf(p) !== 'ai')) message.warning('没有可校验的清单项')
      return
    }
    setVerifying(true)
    setVerifyProgress({ done: 0, total: targets.length })
    // 开跑即把上一轮失败态清回待校验，避免旧「失败」图标与新一轮「校验中」并存
    const targetIds = new Set(targets.map((t) => t.id))
    setImages((prev) =>
      prev.map((p) =>
        targetIds.has(p.id) && p.verifyStatus === 'failed'
          ? { ...p, verifyStatus: 'idle', verifyError: null }
          : p,
      ),
    )
    let anyJudged = false
    let failedCount = 0
    for (const img of targets) {
      const { outcome } = await verifyOne(img)
      if (outcome === 'judged') anyJudged = true
      if (outcome === 'failed') failedCount++
      setVerifyProgress((prev) => ({ ...prev, done: prev.done + 1 }))
    }
    setVerifying(false)
    // 轻量提示（message，不弹窗打断）：失败降级 / 无供应商配置指路
    if (failedCount === targets.length) {
      message.error(`AI 校验失败（${failedCount}/${targets.length} 张），清单不受影响`)
    } else if (failedCount > 0) {
      message.warning(`${failedCount} 张图片 AI 校验失败，已跳过（不影响写入）`)
    } else if (!anyJudged) {
      // 后端无可用供应商时仍 200 返回 unverified，这里给一条轻提示指路配置页
      message.warning('AI 未给出校验结论，请到「数据管理 → LLM 供应商」检查配置')
    }
  }, [images, verifyOne])

  // 自动触发：偏好开启时，清单（preview 数据）就绪后自动调 verify 一次
  useEffect(() => {
    if (phase === 'review' && autoVerify && !autoVerifyRanRef.current) {
      autoVerifyRanRef.current = true
      void runVerify()
    }
  }, [phase, autoVerify, runVerify])

  // --- 单图重跑（强制重新 OCR / 切换引擎共用同一条轮询通道）---
  // opts.force：忽略后端 OCR 缓存（仅 OCR 引擎发送，AI 引擎无 OCR 可强制）；
  // opts.engine：目标引擎（缺省 = 沿用该图当前引擎）。提交即把 engine（切换时含
  // engineSwitch/engineBeforeSwitch）戳进状态；成功后走 applyPreviewResult 换行数据
  // （与「重新解析」同一口径：行编辑与数据日期复位、旧校验结果与补录/采纳草稿清空）→
  // OCR 引擎按需自动重校验刷新差异；AI 引擎自动进识别编排（runAiRecognition）。
  // 失败保留原识别结果、回滚引擎戳（markParseError）并就地报错，不静默、不影响其它图与写入流。
  const rerunPreview = useCallback(
    async (imgId: string, opts: { force?: boolean; engine?: 'ocr' | 'ai' }) => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img || img.parsing) return
      const label = img.preview?.image_filename || img.file.name
      const prevEngine = engineOf(img)
      const engine = opts.engine ?? prevEngine
      const switching = engine !== prevEngine
      const force = !!opts.force && engine === 'ocr'
      // 完成后是否自动重校验（仅 OCR 引擎）：此前已校验过（成功或失败）或开了自动校验；否则不打扰
      const willVerify = engine === 'ocr' && (autoVerify || img.verifyStatus === 'done' || img.verifyStatus === 'failed')

      setImages((prev) =>
        prev.map((p) =>
          p.id === imgId
            ? {
                ...p,
                parsing: true,
                forceOcr: true, // 单图重跑在途：清单锁定 / 写入让路与强制重新 OCR 共用同一标志
                engine,
                ...(switching ? { engineSwitch: true, engineBeforeSwitch: prevEngine } : {}),
                parseError: null,
                // 不动 reparse：中途返回修改而作废时，原本待重解析（如刚换过来源组）的标记要保留
                // 合成排队快照：首轮轮询返回前即可显示排队态（与批量解析一致）
                job: {
                  status: 'queued', stage: 'queued', ocr: null,
                  queue_position: null, elapsed: 0, result: null, error: null,
                },
              }
            : p,
        ),
      )
      if (switching) {
        pushRuleNotice(
          'info',
          engine === 'ai'
            ? `已把「${label}」切换为 AI 识别：正在生成识别清单，完成后全部自动采纳为草稿`
            : `已把「${label}」切换为 OCR 识别：重新解析中，补录/采纳草稿已清空`,
        )
      }

      let jobId = ''
      try {
        // 带本图已有的机器识别行（buildRecognition：AI 图才非空）——
        // 与 /apply 同一份回传结构，job 完成时落进凭证快照（ADR-0023 D7）。
        const res = await api.createPreviewJob(img.file, img.sourceGroup, force, engine === 'ai' ? 'ai' : undefined, buildRecognition(img))
        jobId = res.job_id
      } catch (e) {
        const msg = e instanceof Error ? e.message : '请求失败'
        markParseError(imgId, `提交重新识别失败：${msg}`)
        message.error(`「${label}」提交重新识别失败：${msg}`)
        return
      }

      const schedule = (delay: number) => {
        forceTimersRef.current.set(jobId, setTimeout(() => { void tick() }, delay))
      }
      const tick = async () => {
        // 已作废（返回修改 / 重新开始 / 卸载）：不再 setState、不再续排
        if (!forceTimersRef.current.has(jobId)) return
        try {
          const st = await api.getPreviewJob(jobId)
          if (!forceTimersRef.current.has(jobId)) return
          if (st.status === 'done' || st.status === 'error') {
            forceTimersRef.current.delete(jobId)
            if (st.status === 'done' && st.result) {
              const result = st.result
              // 换行数据 + 清旧校验（复用重新解析的现有处理；切换引擎标记一并复位）
              applyPreviewResult(imgId, result)
              if (engine === 'ai') {
                // AI 引擎：自动进识别编排（识别 → 自动采纳）；结果/失败由卡内状态条呈现，不弹 toast
                void runAiRecognition(imgId, result)
              } else {
                // 新结果里没有待录入项（全是未识别项）时明说，别让人以为图丢了；
                // 这类图照样重校验 —— AI 的 ai_only 正是它唯一的补录来源
                const hasEntry = result.rows.some((r) => r.action !== 'keep')
                if (switching) {
                  message.success(`「${label}」已切换为 OCR 识别，清单已刷新`)
                } else if (hasEntry) {
                  message.success(`「${label}」已忽略缓存重新识别，清单已刷新`)
                } else {
                  message.warning(
                    `「${label}」已重新识别，但没有识别出待录入项；若 AI 校验给出额外识别项，可在该图下方补录`,
                  )
                }
                if (willVerify) {
                  // 用新 preview 组一份该图状态喂给同一条校验路径（state 更新是异步的，不能回读）
                  const base = imagesRef.current.find((p) => p.id === imgId)
                  const { outcome } = await verifyOne({ ...(base ?? img), preview: result, verifyStatus: 'idle' })
                  if (outcome === 'failed') {
                    message.warning(`「${label}」AI 校验失败，清单不受影响`)
                  } else if (outcome === 'unjudged') {
                    message.warning('AI 未给出校验结论，请到「数据管理 → LLM 供应商」检查配置')
                  }
                }
              }
            } else {
              const msg = st.error || '请重试'
              markParseError(imgId, msg)
              message.error(`「${label}」重新识别失败：${msg}（已保留原识别结果）`)
            }
          } else {
            setImages((prev) => prev.map((p) => (p.id === imgId ? { ...p, job: st } : p)))
            schedule(st.status === 'queued' ? 3000 : 2000)
          }
        } catch {
          // 网络失败：退避续试不中断（与批量解析一致）；在途状态仍显示为重新识别中
          if (!forceTimersRef.current.has(jobId)) return
          schedule(3000)
        }
      }
      schedule(1500)
    },
    [applyPreviewResult, markParseError, verifyOne, autoVerify, runAiRecognition, pushRuleNotice],
  )

  // 入口：该图有会被丢弃的用户编辑时先确认（沿用页内 antd Modal.confirm 的确认口径），无编辑直接跑
  const handleForceReocr = useCallback(
    (imgId: string) => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img || img.parsing) return
      if (!hasDiscardableEdits(img)) {
        void rerunPreview(imgId, { force: true })
        return
      }
      const label = img.preview?.image_filename || img.file.name
      const willVerify = autoVerify || img.verifyStatus === 'done' || img.verifyStatus === 'failed'
      const supCount = img.supplements?.length ?? 0
      Modal.confirm({
        title: '强制重新 OCR',
        content: (
          <div className="text-sm text-slate-600 space-y-2">
            <p>
              「{label}」将忽略缓存重新识别，当前编辑会被刷新：名称、金额、跳过、不录入与数据日期都会按新的识别结果重置。
            </p>
            {supCount > 0 && (
              <p>已加入的 {supCount} 项补录也会清空（它们来自上一轮 AI 校验），重新识别后可再次补录。</p>
            )}
            <p>重新识别约需 1~5 分钟，期间该图清单锁定不可编辑，其它图不受影响。</p>
            <p>
              {willVerify
                ? '完成后会自动重跑该图的 AI 校验，差异视图随之刷新。'
                : '完成后不会自动 AI 校验，可在清单顶部点「AI 校验」再看差异。'}
            </p>
          </div>
        ),
        okText: '忽略缓存重新识别',
        cancelText: '取消',
        onOk: () => rerunPreview(imgId, { force: true }),
      })
    },
    [rerunPreview, autoVerify],
  )

  // 逐图引擎切换入口（§4a）：切换 = 重新 preview + 清采纳/补录草稿并提示（沿用 force 先例，
  // 有会被丢弃的编辑先确认）；转 AI 前校验供应商可用（禁用态兜底，正常到不了这里）
  const handleSwitchEngine = useCallback(
    (imgId: string) => {
      const img = imagesRef.current.find((p) => p.id === imgId)
      if (!img || img.parsing) return
      const next = engineOf(img) === 'ai' ? 'ocr' : 'ai'
      if (next === 'ai' && !aiAvailable) {
        message.warning('暂无可用 LLM 供应商：请先到「数据管理 → LLM 供应商」配置')
        return
      }
      if (!img.preview || !hasDiscardableEdits(img)) {
        void rerunPreview(imgId, { engine: next })
        return
      }
      const label = img.preview.image_filename || img.file.name
      const supCount = img.supplements?.length ?? 0
      Modal.confirm({
        title: next === 'ai' ? '切换为 AI 识别' : '切换为 OCR 识别',
        content: (
          <div className="text-sm text-slate-600 space-y-2">
            <p>
              「{label}」将{next === 'ai' ? '跳过 OCR，由 AI 直接看原图生成识别清单' : '重新跑 OCR 并按新结果刷新清单'}
              ；当前编辑会被刷新：名称、金额、跳过、不录入与数据日期都会按新结果重置。
            </p>
            {supCount > 0 && (
              <p>
                已加入的 {supCount} 项{engineOf(img) === 'ai' ? '采纳草稿' : '补录'}也会清空
                {next === 'ai' ? '（AI 识别完成后会重新自动采纳为草稿）' : ''}。
              </p>
            )}
            {next === 'ai' ? (
              <p>
                AI 识别按图计费（同图同供应商命中缓存不重复计费）；识别项自动采纳为草稿，逐项核对后才写入。
                AI 模式下「全部清0」不可用，防止模型漏认导致整组误清零。
              </p>
            ) : (
              <p>OCR 识别约需 1~5 分钟（命中缓存则秒级），期间该图清单锁定，其它图不受影响。</p>
            )}
          </div>
        ),
        okText: next === 'ai' ? '切换为 AI 识别' : '切换为 OCR 识别',
        cancelText: '取消',
        onOk: () => rerunPreview(imgId, { engine: next }),
      })
    },
    [rerunPreview, aiAvailable],
  )

  // 有图正在强制重新识别：整批 AI 校验按钮先让路（避免同图并发校验 / 结论互相覆盖）
  const anyForceRunning = useMemo(() => images.some(isForceBusy), [images])
  // 有图正在校验（含单图重校验）：汇总条先不显示，避免呈现「少了一张图」的部分结论
  const anyImgVerifying = useMemo(() => images.some((p) => p.verifyStatus === 'verifying'), [images])

  const resetAll = useCallback(() => {
    // 作废在途轮询（批次守卫 + 清定时器），避免重置后旧 job 回调改写新状态
    batchSeqRef.current += 1
    clearPollTimers()
    cancelForceReocr()
    // 补录判定：清防抖定时器 + 作废在途响应 + 清「已提醒」记录（下一批重新计）
    clearSupDebounce()
    supSeqRef.current = {}
    supSkipWarnedRef.current.clear()
    setSupResolving({})
    setPollDegraded(false)
    setImages((prev) => {
      prev.forEach((p) => URL.revokeObjectURL(p.previewUrl))
      return []
    })
    setPhase('setup')
    setErrorMsg(null)
    setUnrecognizedPolicy('keep')
    setApplyResults([])
    setApplyPending(false)
    setApplyProgress(0)
    setApplyTotal(0)
    setParseProgress(0)
    setParseTotal(0)
    setVerifyProgress({ done: 0, total: 0 })
    // 批次/核对相关的前端状态一处清干净：换一批截图后旧的批次 id、接受留痕、
    // 父项归属都不再有任何意义（留着只会让新一批继承旧关系）
    batchPlanRef.current = null
    appliedOkRef.current = new Set()
    applyResultsRef.current = []
    setFinalizeState(null)
    setFailedApply(null)
    setAcceptedDiffs({})
    setFoldedRows({})
    setParentPickerKey(null)
    setImageFilter('all')
    setDragView(null)
  }, [clearPollTimers, cancelForceReocr, clearSupDebounce])

  const goBackToSetup = useCallback(() => {
    batchSeqRef.current += 1
    clearPollTimers()
    // 离开清单即作废在途强制重跑（后端 job 继续跑完，前端不再回写，避免回到 setup 后状态错乱）
    cancelForceReocr()
    // 回到上传页意味着这一批要重来：批次壳作废（否则重提交会往旧批次里补成员，
    // 而旧批次可能已经有部分成员，finalize 的「成员齐」判定会指向错误的一批）
    batchPlanRef.current = null
    appliedOkRef.current = new Set()
    applyResultsRef.current = []
    setFailedApply(null)
    setPhase('setup')
    setErrorMsg(null)
  }, [clearPollTimers, cancelForceReocr])

  const totalRowsWritten = useMemo(
    () => applyResults.reduce((acc, r) => acc + r.rows_written, 0),
    [applyResults],
  )
  // 补录写入 / 被丢弃（后端字段缺省时按 0 / [] 兜底，旧响应照样能渲染）
  const totalSupplementsWritten = useMemo(
    () => applyResults.reduce((acc, r) => acc + (r.supplements_written ?? 0), 0),
    [applyResults],
  )
  const totalSupplementsDropped = useMemo(
    () => applyResults.flatMap((r) => r.supplements_dropped ?? []),
    [applyResults],
  )

  // 移动复核：疑点行数（「只看疑点」过滤器的 N，与 VerifySummaryBar / imgHasVerifyIssues 同口径）
  const mobileIssueRows = useMemo(() => entryRows.filter(rowHasIssue), [entryRows])
  // 「本图设置」抽屉对应的图分组（入口只在清单图卡头，故 review 阶段必然存在）
  const settingsGroup = entryGroups.find((g) => g.imgId === settingsImgId)

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader
        title="截图录入"
        description="上传理财平台截图 → 逐图选来源组 → 批量解析 → 预览待录入清单 → 确认数据日期并写入、留存凭证。"
        actions={(images.length > 0 || phase !== 'setup') ? (
          <button
            type="button"
            onClick={resetAll}
            className="inline-flex min-h-[44px] items-center gap-2 rounded-control border border-line-strong bg-surface px-3 text-body text-ink"
          >
            <RefreshCw size={16} aria-hidden />
            重新开始
          </button>
        ) : undefined}
      />

      {/* Step stepper */}
      <StepStepper phase={phase} />

      {/* Error / hint banners */}
      {errorMsg && <ErrorBanner message={errorMsg} onClose={() => setErrorMsg(null)} />}
      {phase === 'review' && failedNoPreview.length > 0 && (
        <HintBanner message={`${failedNoPreview.length} 张图片解析失败，已跳过未进入清单`} />
      )}
      {/* 部分失败（ADR-0029 D8/D9）：不结算，重试沿用同一 batch_id 与各图幂等键；
          已成功的图不再重发（重复 apply 会重复写入同一张截图的数据） */}
      {phase === 'review' && failedApply && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="flex items-start gap-1.5">
              <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-500" aria-hidden="true" />
              <span>
                写入失败：{failedApply.labels.join('、')}。本批还没有结算，重试会沿用同一个批次，已写入成功的图不会重发
              </span>
            </p>
            <button
              type="button"
              onClick={retryFailedApply}
              disabled={applyPending}
              className="inline-flex min-h-[44px] items-center gap-1 rounded-lg border border-amber-300 bg-white px-2.5 text-xs font-medium text-amber-800 transition-colors hover:border-amber-400 hover:text-amber-900 disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-[32px]"
            >
              <RefreshCw size={13} aria-hidden="true" /> 重试（{failedApply.imgIds.length} 张）
            </button>
          </div>
        </div>
      )}

      {/* ============ Phase: Setup (上传预览 + 选择来源组) ============ */}
      {phase === 'setup' && (
        <div className="space-y-4">
          {/* Upload zone：无启用渠道时（且列表已加载完）显示引导空态；加载中仍渲染正常上传区，由 addFiles 拦截兜底 */}
          {!groupsLoading && groupOptions.length === 0 ? (
            <div className="border-2 border-dashed border-slate-200 rounded-xl p-8 text-center bg-white">
              <Layers size={36} className="mx-auto text-slate-300 mb-3" />
              <p className="text-slate-600 font-medium mb-1">暂无可用的截图渠道</p>
              <p className="text-sm text-muted mb-4">
                来源组是截图录入的解析渠道；请先在「设置 · 来源管理」创建并启用，再回来上传截图
              </p>
              <Link
                href="/settings/source-groups"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 transition-colors"
              >
                去来源管理
              </Link>
            </div>
          ) : (
          <div
            onDragOver={(e) => {
              e.preventDefault()
              setDragOver(true)
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            className={`border-2 border-dashed rounded-xl p-8 text-center transition-colors ${
              dragOver ? 'border-brand-400 bg-brand-50/50' : 'border-slate-200 hover:border-slate-300 bg-white'
            }`}
          >
            <Camera size={36} className="mx-auto text-slate-300 mb-3" />
            <p className="text-slate-600 font-medium mb-1">拖拽理财平台截图到此处</p>
            <p className="text-sm text-muted mb-4">支持批量、PNG / JPG / WEBP。上传后可逐张预览并选择来源组</p>
            <label className="inline-flex items-center gap-2 px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 cursor-pointer">
              <Upload size={16} />
              选择截图
              <input type="file" accept="image/png,image/jpeg,image/webp" multiple className="hidden" onChange={handleFileInput} />
            </label>
            <p className="mt-4 inline-flex items-center gap-1.5 text-xs text-muted">
              <Clock size={13} className="text-slate-300" />
              {defaultEngine === 'ai'
                ? '仅在点击「下一步」后提交 AI 识别（约秒级到几十秒）；识别项自动采纳为草稿，确认后写入'
                : '仅在点击「下一步」后提交 OCR 解析，每张约需 1~5 分钟，期间可实时查看进度'}
            </p>
          </div>
          )}

          {/* Uploaded file cards */}
          {images.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {images.map((img, idx) => (
                <Card key={img.id} padding="none" className="flex flex-col overflow-hidden">
                  <div className="relative bg-slate-50">
                    {/* eslint-disable-next-line @next/next/no-img-element -- blob preview URLs do not benefit from next/image */}
                    <img
                      src={img.previewUrl}
                      alt={img.file.name}
                      className="h-40 w-full border-b border-line bg-subtle object-cover object-top"
                    />
                    <button
                      onClick={() => removeFile(img.id)}
                      className="absolute top-2 right-2 p-1.5 rounded-full bg-white/90 border border-slate-200 text-muted hover:text-red-500 shadow-sm"
                      title="移除"
                    >
                      <Trash2 size={14} />
                    </button>
                    <span className="absolute top-2 left-2 px-1.5 py-0.5 rounded text-meta font-medium bg-brand-600 text-white">
                      第 {idx + 1} 张
                    </span>
                  </div>
                  <div className="p-4 space-y-3 flex-1 flex flex-col">
                    <div>
                      <p className="font-medium text-brand-950 text-sm truncate" title={img.file.name}>
                        {img.file.name}
                      </p>
                      <p className="text-xs text-muted mt-0.5">{formatFileSize(img.file.size)}</p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Layers size={15} className="text-muted shrink-0" />
                      <select
                        value={img.sourceGroup}
                        onChange={(e) => setImageGroup(img.id, e.target.value)}
                        className="flex-1 px-2 py-1.5 text-base lg:text-sm text-slate-700 border border-slate-200 rounded-lg bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400"
                      >
                        {groupOptions.map((o) => (
                          <option key={o.slug} value={o.slug}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    {/* 页角色备注（票 05 D2）：上传时就能标，纯注释。
                        「标了也不改变任何数字」这件事必须明说，否则它会被当成计入/不计入开关 */}
                    <PageRoleNote
                      idSuffix={`setup-${img.id}`}
                      value={img.pageRole}
                      onChange={(next) => setPageRole(img.id, next)}
                    />
                    <label className="flex items-center gap-2 text-xs text-slate-600 mt-auto pt-1 min-h-[44px] lg:min-h-0">
                      <input
                        type="checkbox"
                        checked={img.checked}
                        onChange={() => toggleImageChecked(img.id)}
                        className="w-5 h-5 lg:w-4 lg:h-4 accent-brand-600 cursor-pointer"
                      />
                      参与批量解析
                    </label>
                  </div>
                </Card>
              ))}
            </div>
          )}

          {/* 批级默认识别引擎（§4a/§4b）：默认 OCR——本地、免费、有缓存、经校正规则打磨；
              AI 每图真金白银 + 网络，只在用户显式选择时启用。无可用供应商：AI 直接禁用 + 指路配置页。
              进清单后可在每张图卡头逐图切换（切换 = 重新 preview + 清采纳草稿并提示） */}
          {images.length > 0 && (
            <Card padding="md" className="flex flex-wrap items-center gap-3">
              <span className="text-sm font-medium text-slate-600">识别引擎</span>
              <div className="inline-flex overflow-hidden rounded-lg border border-slate-200">
                <button
                  type="button"
                  onClick={() => setDefaultEngine('ocr')}
                  title="本地 OCR：免费、有缓存，每张约 1~5 分钟；识别行套来源组校正规则"
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] lg:min-h-0 text-sm transition-colors ${
                    defaultEngine === 'ocr' ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  <Cpu size={14} /> OCR 识别（默认）
                </button>
                <button
                  type="button"
                  onClick={() => aiAvailable && setDefaultEngine('ai')}
                  disabled={!aiAvailable}
                  title={
                    aiAvailable
                      ? 'AI 主识别：视觉大模型直接看原图生成识别清单（按图计费）；识别项自动采纳为草稿，逐项确认后写入。不套来源组校正规则；「全部清0」不可用'
                      : '暂无可用 LLM 供应商：请先到「数据管理 → LLM 供应商」配置'
                  }
                  className={`inline-flex items-center gap-1.5 border-l border-slate-200 px-3 py-1.5 min-h-[44px] lg:min-h-0 text-sm transition-colors ${
                    defaultEngine === 'ai'
                      ? 'bg-brand-600 text-white'
                      : aiAvailable
                        ? 'bg-white text-slate-600 hover:bg-slate-50'
                        : 'cursor-not-allowed bg-white text-slate-300'
                  }`}
                >
                  <Bot size={14} /> AI 识别
                </button>
              </div>
              <HintToggle
                hint={
                  aiAvailable
                    ? 'AI 按图计费、识别项自动采纳为草稿；此模式下「全部清0」不可用。'
                    : '需先到「LLM 供应商」配置可用供应商。'
                }
              />
              {!aiAvailable ? (
                <span className="flex items-center gap-1.5 text-xs text-muted">
                  AI 识别需先配置 LLM 供应商
                  <Link href="/settings/llm-providers" className="text-brand-600 hover:underline">
                    去配置
                  </Link>
                </span>
              ) : defaultEngine === 'ai' ? (
                <span className="text-xs text-muted">
                  AI 直接看原图生成识别清单（按图计费、约秒级到几十秒，同图同供应商命中缓存不重复计费）；识别项自动采纳为草稿，逐项确认后写入 —— AI 产出不经你采纳绝不写入
                </span>
              ) : (
                <span className="text-xs text-muted">
                  本地 OCR 免费、每张约 1~5 分钟；个别图 OCR 效果差时，可在清单里逐图「转用 AI 识别」
                </span>
              )}
            </Card>
          )}

          {/* Next button */}
          {images.length > 0 && (
            <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 bg-white rounded-xl shadow-card px-6 py-4 border-t">
              <p className="text-sm text-slate-500">
                已选 <span className="font-semibold text-brand-950">{checkedImages.length}</span> 张参与解析
                {uncheckedCount > 0 && (
                  <span className="text-muted">，另有 {uncheckedCount} 张未勾选</span>
                )}
                {checkedImages.filter((p) => p.preview).length > 0 && (
                  <span className="text-muted">，已解析 {checkedImages.filter((p) => p.preview).length} 张</span>
                )}
              </p>
              {/* 可点条件 = 至少勾选一张（而非「存在待解析项」）：否则「返回修改」回来后按钮必然灰，
                  用户被卡在 setup（唯一天然解锁是改来源组触发 reparse）。
                  无待解析项时改为「查看清单」，明说不会重跑识别 */}
              <button
                onClick={startParse}
                disabled={checkedImages.length === 0}
                title={
                  pendingParseCount > 0
                    ? undefined
                    : '勾选的截图都已解析过，直接回到待录入清单，不会重新识别；需要重新识别请在清单里对该图点「强制重新 OCR」'
                }
                className="inline-flex items-center gap-2 px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {pendingParseCount > 0 ? <Cpu size={16} /> : <FileText size={16} />}
                {pendingParseCount > 0 ? '下一步：批量解析' : '下一步：查看清单'}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ============ Phase: Parsing ============ */}
      {phase === 'parsing' && (
        <div className="bg-white rounded-xl shadow-card p-6 space-y-5">
          <div className="flex items-center gap-3">
            <Cpu size={20} className="text-brand-600" />
            <div>
              <p className="font-semibold text-brand-950">正在批量解析截图</p>
              <p className="text-sm text-slate-500">
                {defaultEngine === 'ai'
                  ? 'AI 识别图秒级返回清单；OCR 图按队列逐张识别（每张约 1~5 分钟），请勿关闭页面'
                  : '后台按队列逐张 OCR，每张约需 1~5 分钟，请勿关闭页面'}
              </p>
            </div>
          </div>
          {pollDegraded && <HintBanner message="进度获取中断，稍后自动重试" />}
          <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-brand-600 transition-all duration-300"
              style={{ width: `${parseRatio}%` }}
            />
          </div>
          <ul className="space-y-3">
            {checkedImages.map((img) => (
              <li key={img.id} className="text-sm">
                <div className="flex items-center gap-3">
                  {/* eslint-disable-next-line @next/next/no-img-element -- 40px blob thumbnail; next/image adds no benefit */}
                  <img src={img.previewUrl} alt="" className="w-10 h-7 object-cover rounded border border-slate-100" />
                  <span className="flex-1 text-slate-600 truncate">{img.file.name}</span>
                  {img.parseError ? (
                    <span className="inline-flex items-center gap-1 text-xs text-red-500" title={img.parseError}>
                      <AlertTriangle size={13} /> 失败
                    </span>
                  ) : img.preview ? (
                    <span className="inline-flex items-center gap-1 text-xs text-emerald-600">
                      <CheckCircle2 size={13} /> 完成
                    </span>
                  ) : img.parsing && img.job?.status === 'queued' ? (
                    <span className="inline-flex items-center gap-1 text-xs text-amber-600">
                      <Clock size={13} />
                      排队中{img.job.queue_position != null ? ` · 第 ${img.job.queue_position} 位` : ''}
                    </span>
                  ) : img.parsing ? (
                    <span className="inline-flex items-center gap-1 text-xs text-brand-600">
                      <Loader2 size={13} className="animate-spin" /> 识别中
                    </span>
                  ) : (
                    <span className="text-xs text-muted">等待</span>
                  )}
                </div>
                {/* 单图真实进度：queued  amber / running brand；done 瞬间直接切「完成」，不闪 100% */}
                {img.parsing && img.job && (img.job.status === 'queued' || img.job.status === 'running') && (
                  <div className="mt-1.5 ml-[52px]">
                    <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${
                          img.job.status === 'queued' ? 'bg-amber-400' : 'bg-brand-600'
                        }`}
                        style={{ width: `${jobPercent(img.job)}%` }}
                      />
                    </div>
                    <p
                      className="mt-1 text-xs text-muted w-fit cursor-pointer hover:text-brand-600 hover:underline underline-offset-2 transition-colors"
                      title="点击查看已识别内容"
                      onClick={() => openLinesModal(img.id)}
                    >
                      {jobStageText(img.job)}
                    </p>
                  </div>
                )}
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between pt-1">
            <p className="text-sm text-slate-500">
              已完成 <span className="font-semibold text-brand-950">{parseProgress}</span> / {parseTotal}
            </p>
          </div>
        </div>
      )}

      {/* ============ Phase: Review（待录入清单预览 + 未识别项策略） ============ */}
      {phase === 'review' && (
        <div className={`space-y-4 ${entryRows.length > 0 ? 'pb-16 lg:pb-0' : ''}`}>
          {/* ===== 移动第一屏：合计对不对（序列化核对的第一步；桌面不渲染） ===== */}
          {mobileReviewStep === 'totals' && (
            <div className="lg:hidden space-y-3">
              <div className="bg-white rounded-xl shadow-card p-5 space-y-4">
                <div>
                  <p className="text-sm font-medium text-slate-700">先核对合计，再逐行核对</p>
                  <p className="mt-0.5 text-xs text-muted">移动端默认全部采纳；只有疑点行需要你处理</p>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-slate-500">截图识别合计</span>
                    <span className="font-mono text-lg font-semibold text-brand-950">{formatCurrency(liveTotalScreenshot.toString())}</span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-slate-500">系统合计</span>
                    <span className="font-mono text-lg font-semibold text-brand-950">{formatCurrency(systemTotal.toString())}</span>
                  </div>
                  <div className="flex items-center justify-between gap-3 border-t border-dashed border-slate-200 pt-2">
                    <span className="text-sm font-medium text-slate-600">差异</span>
                    <span className={`font-mono text-lg font-semibold ${liveDiff.isZero() ? 'text-emerald-600' : 'text-amber-600'}`}>
                      {formatSignedCurrency(liveDiff.toString())}
                    </span>
                  </div>
                </div>
                <p className={`rounded-lg px-3 py-2 text-sm ${liveDiff.isZero() ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-800'}`}>
                  {liveDiff.isZero()
                    ? '合计一致，可进入逐行核对'
                    : '合计存在差异：请确认是否有未勾选/跳过的行，或截图与系统口径不同'}
                </p>
                <button
                  type="button"
                  onClick={() => setMobileReviewStep('rows')}
                  className="flex min-h-[48px] w-full items-center justify-center gap-2 rounded-lg bg-brand-600 px-4 text-sm font-medium text-white transition-colors active:bg-brand-700"
                >
                  进入逐行核对（{entryRows.length} 条{mobileIssueRows.length > 0 ? ` · 疑点 ${mobileIssueRows.length} 条` : ''}）
                </button>
              </div>
            </div>
          )}

          {/* ===== 桌面整块 + 移动第二屏：同一份数据、两套渲染形态 =====
              移动仅在 step=rows 时显示；桌面（≥lg）始终显示，布局与改造前一致 */}
          <div className={`${mobileReviewStep === 'rows' ? 'block' : 'hidden'} lg:block space-y-4`}>
            {/* 移动第二屏顶栏：紧凑合计 + 「只看疑点」+ 返回合计 */}
            <div className="lg:hidden sticky top-[var(--pf-header-h)] z-20 space-y-2 rounded-xl bg-white/95 px-4 py-3 shadow-card backdrop-blur">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="text-slate-500">
                  差异{' '}
                  <span className={`font-mono font-semibold ${liveDiff.isZero() ? 'text-emerald-600' : 'text-amber-600'}`}>
                    {formatSignedCurrency(liveDiff.toString())}
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => setMobileReviewStep('totals')}
                  className="inline-flex min-h-[44px] items-center gap-1 rounded-lg border border-slate-200 px-2.5 text-xs text-slate-600 active:bg-slate-50"
                >
                  <ChevronLeft size={14} /> 合计
                </button>
              </div>
              <button
                type="button"
                onClick={() => setMobileOnlyIssues((v) => !v)}
                aria-pressed={mobileOnlyIssues}
                aria-label="切换只看疑点"
                className={`flex min-h-[44px] w-full items-center justify-center gap-1.5 rounded-lg border text-sm font-medium transition-colors ${
                  mobileOnlyIssues
                    ? 'border-amber-300 bg-amber-50 text-amber-800'
                    : 'border-slate-200 bg-white text-slate-600 active:bg-slate-50'
                }`}
              >
                {mobileOnlyIssues
                  ? mobileIssueRows.length > 0
                    ? `只看疑点中：${mobileIssueRows.length} 条 · 点击看全部`
                    : '无疑点 · 其余行默认采纳 · 点击看全部'
                  : `只看疑点 ${mobileIssueRows.length} 条`}
              </button>
            </div>

            {/* Summary（桌面四卡；移动第一屏已给合计，这里不重复） */}
            <div className="hidden lg:grid grid-cols-4 gap-3">
            <StatCard
              label="截图识别合计"
              value={formatCurrency(liveTotalScreenshot.toString())}
              hint={
                aggregateRowCount > 0
                  ? `已排除 ${aggregateRowCount} 行汇总行${skippedCount > 0 ? `、${skippedCount} 条跳过` : ''}`
                  : skippedCount > 0
                    ? `已排除 ${skippedCount} 条跳过`
                    : pureAiBatch
                      ? 'AI 识别项走「采纳」通道，不计入此卡'
                      : '仅 OCR 识别行，不含补录'
              }
            />
            <StatCard label="系统合计" value={formatCurrency(systemTotal.toString())} />
            <StatCard
              label="差异"
              value={formatSignedCurrency(liveDiff.toString())}
              accent={liveDiff.isZero() ? 'emerald' : 'amber'}
            />
            <StatCard label="录入图片数" value={`${parsedImages.length} 张`} />
          </div>

          {/* 本次写入合计：截图识别 ＋ 补录 ＝ 合计。
              补录金额只出现在这一行的「补录」项与合计里，不混进上面的识别合计 / 系统合计 / 差异。
              AI 模式（§2 汇总行）：纯 AI 批显示「AI 识别 Y 项（已采纳 Z）＝ 本次写入 W」；
              混批在 OCR 口径上把补录项改称「采纳/补录」 */}
          <div
            className="hidden lg:flex bg-white rounded-xl shadow-card px-6 py-2.5 items-center gap-2 flex-wrap text-xs text-slate-500"
            title={
              pureAiBatch
                ? 'AI 识别 = 本批 AI 主识别图产出的识别项总数；已采纳 = 经你确认、会真正写入的项（冲突/无效项不计）'
                : '截图识别 = 本次要写入的 OCR 行合计（不含跳过、不含补录）；补录 = 手动加入的「AI 额外识别」项'
            }
          >
            <span className="shrink-0">本次写入合计</span>
            {pureAiBatch ? (
              <span className="flex items-center gap-2 flex-wrap font-mono">
                <span>
                  AI 识别 <span className="text-slate-700">{aiRecognizedCount}</span> 项
                </span>
                <span className="text-slate-300">（</span>
                <span className="text-emerald-700">
                  已采纳 {aiAdoptedCount} 项 {formatCurrency(aiAdoptedTotal.toString())}
                </span>
                <span className="text-slate-300">）＝</span>
                <span className="text-sm font-semibold text-brand-950">{formatCurrency(writeTotal.toString())}</span>
              </span>
            ) : (
              <span className="flex items-center gap-2 flex-wrap font-mono">
                <span>
                  截图识别 <span className="text-slate-700">{formatCurrency(liveTotalScreenshot.toString())}</span>
                </span>
                {sendableSupplementCount > 0 && (
                  <>
                    <span className="text-slate-300">＋</span>
                    <span className="text-emerald-700">
                      {anyAiParsed ? '采纳/补录' : '补录'} {formatCurrency(supplementTotal.toString())}
                    </span>
                  </>
                )}
                <span className="text-slate-300">＝</span>
                <span className="text-sm font-semibold text-brand-950">{formatCurrency(writeTotal.toString())}</span>
              </span>
            )}
            {sendableSupplementCount > 0 && (
              <span className="ml-auto text-muted">
                {pureAiBatch
                  ? `已采纳 ${aiAdoptedCount} 项经确认后写入`
                  : `含补录 ${sendableSupplementCount} 项（不计入截图识别合计）`}
                {conflictSupplementCount > 0 && `，另有 ${conflictSupplementCount} 项冲突不写入`}
              </span>
            )}
          </div>

          {/* Main list（待录入清单：不含 keep 行；只有 AI 额外识别项/补录草稿的图也在这里占一张卡，
              否则全 keep 的图没有卡片 ⇒ 补录与强制重新 OCR 入口都够不到）。
              卡片本身常驻：工具条里有「AI 校验 / 返回修改」，整批没有待录入项时也不能把人困在清单页 */}
          <div className="bg-white rounded-xl shadow-card overflow-hidden">
              <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  <ListChecks size={20} className="text-brand-600" />
                  <div>
                    <p className="font-medium text-brand-950">待录入清单</p>
                    <p className="text-sm text-slate-500">
                      {entryRows.length > 0
                        ? `共 ${entryRows.length} 条（更新/新增/清零），可改名称与金额、勾选跳过或汇总；汇总行只核对、不入账`
                        : pureAiBatch
                          ? 'AI 主识别图没有 OCR 行骨架：请在各图下方核对「AI 识别清单」，采纳项确认后写入'
                          : '没有识别出待录入项，可在各图下方把「AI 额外识别」项补录进来'}
                      {sendableSupplementCount > 0 &&
                        `；另有${pureAiBatch ? '已采纳' : anyAiParsed ? '采纳/补录' : '补录'} ${sendableSupplementCount} 条`}
                      {identityBlockers.length > 0 && (
                        <span className="text-amber-700">
                          ；{identityBlockers.length} 行待确认身份（疑似改名/拆分）
                        </span>
                      )}
                      {createPendingRows.length > 0 && (
                        <span className="text-amber-700">
                          ；{createPendingRows.length} 行待确认新增
                        </span>
                      )}
                    </p>
                  </div>
                </div>
                {/* 桌面原布局（≥lg 不动）；移动端改走下方「批量操作」折叠区 */}
                <div className="hidden lg:flex items-center gap-4 flex-wrap">
                  {/* 批量设置日期（识别计数旁）：一次性覆盖所有已解析图，单图仍可在各自卡片再改 */}
                  <label
                    title="批量设置所有图片的写入日期；各图卡片仍可单独修改。改日期不会重新识别"
                    className="flex items-center gap-1 text-xs text-slate-500 whitespace-nowrap"
                  >
                    <CalendarDays size={12} className="text-muted shrink-0" />
                    批量日期
                    <select
                      value={batchMode}
                      onChange={(e) => handleBatchMode(e.target.value)}
                      className="px-1.5 py-1 text-base lg:text-xs text-slate-600 border border-slate-200 rounded-md bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors cursor-pointer"
                    >
                      <option value="">设置…</option>
                      <option value="today">全部用今天</option>
                      <option value="suspected">各图截图日期</option>
                      <option value="custom">自定义…</option>
                    </select>
                  </label>
                  {batchMode === 'custom' && (
                    <input
                      type="date"
                      value={batchCustomDate}
                      onChange={(e) => handleBatchCustom(e.target.value)}
                      className="px-1.5 py-1 text-base lg:text-xs font-mono text-slate-700 border border-slate-200 rounded-md bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors"
                    />
                  )}
                  {/* 临时供应商选择（低调小控件，仅本次校验生效，不改变供应商配置） */}
                  {enabledProviders.length > 0 && (
                    <label
                      title="本次 AI 校验使用的供应商：默认选中标记为默认的供应商，可临时切换（不改变配置）"
                      className="flex items-center gap-1 text-xs text-muted whitespace-nowrap"
                    >
                      <Bot size={12} className="text-slate-300 shrink-0" />
                      <select
                        value={providerId ?? ''}
                        onChange={(e) => setProviderId(Number(e.target.value))}
                        disabled={verifying}
                        className="max-w-[9rem] px-1.5 py-1 text-base lg:text-xs text-slate-500 border border-slate-200 rounded-md bg-white focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
                      >
                        {enabledProviders.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.label}
                            {p.is_default ? '（默认）' : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label
                    title="开启后，生成待录入清单时自动调用一次 AI 校验（第二意见，不影响写入）"
                    className="flex items-center gap-1.5 text-xs text-slate-500 cursor-pointer select-none whitespace-nowrap min-h-[44px] lg:min-h-0"
                  >
                    <input
                      type="checkbox"
                      checked={autoVerify}
                      onChange={() => setAutoVerify((v) => !v)}
                      className="w-5 h-5 lg:w-3.5 lg:h-3.5 accent-brand-600 cursor-pointer"
                    />
                    自动校验
                  </label>
                  <button
                    onClick={runVerify}
                    disabled={
                      verifying ||
                      parsedImages.length === 0 ||
                      anyForceRunning ||
                      !parsedImages.some((p) => engineOf(p) !== 'ai')
                    }
                    title={
                      anyForceRunning
                        ? '有截图正在强制重新识别，完成后再整批校验'
                        : parsedImages.length > 0 && !parsedImages.some((p) => engineOf(p) !== 'ai')
                          ? '本批均为 AI 主识别图：识别由编排自动执行（失败可在卡内重试），无需双路校验'
                          : undefined
                    }
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] lg:min-h-0 text-sm text-brand-700 border border-brand-200 bg-brand-50 rounded-lg hover:bg-brand-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
                  >
                    {verifying ? <Loader2 size={15} className="animate-spin" /> : <Bot size={15} />}
                    {verifying ? `AI 校验中 ${verifyProgress.done}/${verifyProgress.total}` : 'AI 校验'}
                  </button>
                  <button
                    onClick={goBackToSetup}
                    className="flex items-center gap-1 min-h-[44px] lg:min-h-0 text-sm text-muted hover:text-slate-600"
                  >
                    <ChevronLeft size={16} />
                    返回修改
                  </button>
                </div>

                {/* 移动（<lg）：主操作常驻一行，批量日期/供应商/自动校验 收进「批量操作」折叠区（默认收起）。
                    状态与桌面同一套（batchMode/providerId/autoVerify），不引入新契约 */}
                <div className="lg:hidden space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setMobileBatchOpen((v) => !v)}
                      aria-expanded={mobileBatchOpen}
                      className="inline-flex min-h-[44px] items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-600 active:bg-slate-50"
                    >
                      <Settings2 size={15} /> 批量操作
                      <ChevronDown size={14} className={`transition-transform ${mobileBatchOpen ? 'rotate-180' : ''}`} />
                    </button>
                    <button
                      onClick={runVerify}
                      disabled={
                        verifying ||
                        parsedImages.length === 0 ||
                        anyForceRunning ||
                        !parsedImages.some((p) => engineOf(p) !== 'ai')
                      }
                      className="inline-flex min-h-[44px] items-center gap-1.5 rounded-lg border border-brand-200 bg-brand-50 px-3 text-sm text-brand-700 active:bg-brand-100 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {verifying ? <Loader2 size={15} className="animate-spin" /> : <Bot size={15} />}
                      {verifying ? `AI 校验中 ${verifyProgress.done}/${verifyProgress.total}` : 'AI 校验'}
                    </button>
                    <button
                      onClick={goBackToSetup}
                      className="inline-flex min-h-[44px] items-center gap-1 text-sm text-muted active:text-slate-600"
                    >
                      <ChevronLeft size={16} />
                      返回修改
                    </button>
                  </div>
                  {mobileBatchOpen && (
                    <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50/60 p-3">
                      <label className="flex min-h-[44px] items-center justify-between gap-2 text-sm text-slate-600">
                        <span className="flex shrink-0 items-center gap-1.5">
                          <CalendarDays size={14} className="text-muted" /> 批量日期
                        </span>
                        <select
                          value={batchMode}
                          onChange={(e) => handleBatchMode(e.target.value)}
                          className="min-h-[44px] rounded-md border border-slate-200 bg-white px-2 text-base text-slate-600 focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400"
                        >
                          <option value="">设置…</option>
                          <option value="today">全部用今天</option>
                          <option value="suspected">各图截图日期</option>
                          <option value="custom">自定义…</option>
                        </select>
                      </label>
                      {batchMode === 'custom' && (
                        <input
                          type="date"
                          value={batchCustomDate}
                          onChange={(e) => handleBatchCustom(e.target.value)}
                          className="w-full min-h-[44px] rounded-md border border-slate-200 bg-white px-2 font-mono text-base text-slate-700 focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400"
                        />
                      )}
                      {enabledProviders.length > 0 && (
                        <label className="flex min-h-[44px] items-center justify-between gap-2 text-sm text-slate-600">
                          <span className="flex shrink-0 items-center gap-1.5">
                            <Bot size={14} className="text-muted" /> 校验供应商
                          </span>
                          <select
                            value={providerId ?? ''}
                            onChange={(e) => setProviderId(Number(e.target.value))}
                            disabled={verifying}
                            className="max-w-[12rem] min-h-[44px] rounded-md border border-slate-200 bg-white px-2 text-base text-slate-500 focus:outline-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 disabled:opacity-50"
                          >
                            {enabledProviders.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.label}
                                {p.is_default ? '（默认）' : ''}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                      <label className="flex min-h-[44px] cursor-pointer select-none items-center gap-2 text-sm text-slate-600">
                        <input
                          type="checkbox"
                          checked={autoVerify}
                          onChange={() => setAutoVerify((v) => !v)}
                          className="h-5 w-5 accent-brand-600"
                        />
                        自动校验
                        <HintToggle hint="生成清单时自动跑一次 AI 校验（第二意见，不影响写入）。" />
                      </label>
                    </div>
                  )}
                </div>
              </div>

              {/* 校验汇总：仅在本批校验出结果后显示（校验中由逐行「校验中」状态呈现进度；
                  单图重校验期间同样先收起，避免呈现少了一张图的部分结论）；文案按引擎模式映射 */}
              {verifySummary.hasResult && !verifying && !anyImgVerifying && (
                <VerifySummaryBar s={verifySummary} mode={verifyBarMode} />
              )}

{/* 按图筛选（ADR-0029 D11；票 05 D1）：按图分表退化为筛选器，
                  「全部」是默认视图。跨图构成要求子行缩进到父行下，分表结构做不到 */}
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-2.5 lg:px-6">
                <span className="shrink-0 text-xs text-slate-500">按图筛选</span>
                <div className="hidden flex-wrap items-center gap-1.5 lg:flex">
                  <button
                    type="button"
                    onClick={() => setImageFilter('all')}
                    aria-pressed={imageFilter === 'all'}
                    className={`inline-flex min-h-[30px] items-center rounded-pill border px-2.5 text-xs transition-colors ${
                      imageFilter === 'all'
                        ? 'border-brand-600 bg-brand-600 text-white'
                        : 'border-slate-200 bg-white text-slate-600 hover:border-brand-300'
                    }`}
                  >
                    全部（{entryRows.length}）
                  </button>
                  {imageFilterOptions.map((o) => (
                    <button
                      key={o.imgId}
                      type="button"
                      onClick={() => setImageFilter(o.imgId)}
                      aria-pressed={imageFilter === o.imgId}
                      title={o.label}
                      className={`inline-flex min-h-[30px] max-w-[13rem] items-center gap-1 rounded-pill border px-2.5 text-xs transition-colors ${
                        imageFilter === o.imgId
                          ? 'border-brand-600 bg-brand-600 text-white'
                          : 'border-slate-200 bg-white text-slate-600 hover:border-brand-300'
                      }`}
                    >
                      <span className="shrink-0">图 {o.ordinal}</span>
                      <span className="truncate opacity-70">{o.label}</span>
                    </button>
                  ))}
                </div>
                {/* 375px：筛选器变下拉（横向铺开会把第一屏挤满） */}
                <label className="lg:hidden min-w-0 flex-1">
                  <span className="sr-only">按图筛选</span>
                  <select
                    value={imageFilter}
                    onChange={(e) => setImageFilter(e.target.value)}
                    className="min-h-[44px] w-full rounded-control border border-slate-200 bg-white px-2 text-base text-slate-700 focus:border-brand-400 focus:outline-none focus:ring-1 focus:ring-brand-400"
                  >
                    <option value="all">全部图（{entryRows.length} 行）</option>
                    {imageFilterOptions.map((o) => (
                      <option key={o.imgId} value={o.imgId}>
                        图 {o.ordinal} · {o.label}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="ml-auto shrink-0 font-code text-meta text-muted">
                  {visibleTableRows.length} 行
                  {imageFilter !== 'all' && ` / 全部 ${entryRows.length} 行`}
                </span>
              </div>

              {/* 一张总表（票 05 D1）：跨图行摊平，父行与子行保持相邻；缩进只作用于名称列，
                  金额列右对齐基线不随缩进移动。行键始终是 (imgId, 行号)，提交时按图分组回各图 */}
              <div className="hidden lg:block overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="w-8 px-1 py-2 text-center font-medium text-slate-600" title="按住拖到目标行即可归入（目标行会自动成为汇总行）；等价操作是行尾「归到…」">
                        <span aria-hidden="true">⠿</span>
                        <span className="sr-only">拖拽手柄</span>
                      </th>
                      <th
                        className="min-w-[280px] px-3 py-2 text-left font-medium text-slate-600"
                        title="名称吃满剩余宽度（保底 280px）；行尾两个图标是「跳过」与「汇总行」"
                      >
                        名称 / 映射目标 · 来源图
                      </th>
                      <th className="w-[122px] px-3 py-2 text-right font-medium text-slate-600">金额</th>
                      <th className="w-[62px] px-2 py-2 text-center font-medium text-slate-600">动作</th>
                      <th className="w-[86px] px-2 py-2 text-center font-medium text-slate-600">校验</th>
                      <th className="w-[92px] px-2 py-2 text-right font-medium text-slate-600">金额差异</th>
                      <th className="w-[132px] px-2 py-2 text-right font-medium text-slate-600">归属</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleTableRows.map(({ row: r, level, childCount, parentOutside, isLast, ancestorHasNext }) => {
                      const folded = !!foldedRows[r.key]
                      // 汇总行的差额标记（票 06）：只有汇总行才有核对对象，普通行不挂
                      const diffItem = r.aggregate
                        ? (reconcileItems.find((it) => it.parent.id === `row:${r.key}`) ?? null)
                        : null
                      const draggingSelf = dragView?.childKey === r.key
                      const dropTarget = dragView?.targetKey === r.key ? dragView : null
                      const dropVerdict = dropTarget ? checkDrop(meterRows, dropTarget.childKey, r.key) : null
                      const parentText = parentTextOf(r)
                      // 背景只挑一种：拖拽预览要盖过行自身的状态色（否则出现两种底色打架）
                      const rowBg = dropTarget
                        ? dropTarget.ok
                          ? 'bg-brand-50 ring-1 ring-inset ring-brand-600'
                          : 'bg-rose-50 ring-1 ring-inset ring-rose-400'
                        : r.edit.skipped
                          ? 'bg-slate-50 opacity-60'
                          : r.aggregate
                            ? 'bg-slate-50/70'
                            : r.displayAction === 'create'
                              ? 'bg-emerald-50/40'
                              : ''
                      return (
                        <Fragment key={r.key}>
                          <tr
                            id={`review-row-${r.key}`}
                            data-row-key={r.key}
                            className={`border-t border-slate-50 transition-colors ${rowBg} ${draggingSelf ? 'opacity-40' : ''}`}
                          >
                            <td className="px-1 py-2 align-middle">
                              <button
                                type="button"
                                tabIndex={-1}
                                aria-hidden="true"
                                title="按住拖到汇总行（等价操作：行尾「归到…」）"
                                onPointerDown={(e) => onGripPointerDown(e, r.key)}
                                className="flex h-7 w-6 cursor-grab touch-none items-center justify-center rounded text-slate-300 hover:bg-slate-100 hover:text-slate-600"
                              >
                                <GripVertical size={14} />
                              </button>
                            </td>
                            <td className="px-3 py-2 align-top">
                              <div
                                className="relative"
                                data-tree-indent
                                data-level={level}
                                style={{ paddingLeft: indentPad(level) }}
                              >
                                {/* 层级引导线（票 01 D2/D3）：每级一条 16px 车道，绝对定位不占布局。
                                    空车道不渲染元素；第一级 2px brand、深层 1px #cbd5e1 */}
                                <span
                                  aria-hidden="true"
                                  data-tree-guides
                                  className="pointer-events-none absolute -top-2 -bottom-4 left-0 right-0"
                                >
                                  {ancestorHasNext.map((hasNext, i) =>
                                    hasNext ? (
                                      <i
                                        key={`lane-${i}`}
                                        data-guide="lane"
                                        data-lane={i}
                                        data-guide-width={i === 0 ? 2 : 1}
                                        data-guide-tone={i === 0 ? 'brand' : 'muted'}
                                        className={`absolute top-0 bottom-0 ${i === 0 ? 'w-[2px] bg-brand-600' : 'w-px bg-slate-300'}`}
                                        style={{ left: i * LANE_PX + LANE_CENTER - (i === 0 ? 1 : 0.5) }}
                                      />
                                    ) : null,
                                  )}
                                  <i
                                    data-guide="connector"
                                    data-guide-last={isLast ? 'true' : 'false'}
                                    data-guide-width={level === 0 ? 2 : 1}
                                    data-guide-tone={level === 0 ? 'brand' : 'muted'}
                                    className={`absolute top-0 ${level === 0 ? 'w-[2px] bg-brand-600' : 'w-px bg-slate-300'}`}
                                    style={{
                                      left: level * LANE_PX + LANE_CENTER - (level === 0 ? 1 : 0.5),
                                      height: isLast ? LANE_CENTER : '100%',
                                    }}
                                  />
                                  <i
                                    data-guide="elbow"
                                    data-guide-width={level === 0 ? 2 : 1}
                                    data-guide-tone={level === 0 ? 'brand' : 'muted'}
                                    className={`absolute ${level === 0 ? 'h-[2px] bg-brand-600' : 'h-px bg-slate-300'}`}
                                    style={{ left: level * LANE_PX + LANE_CENTER, top: LANE_CENTER, width: LANE_CENTER }}
                                  />
                                </span>
                                <div className="flex items-start gap-1">
                                  <div className="min-w-0 flex-1">
                                    {r.nameEditable ? (
                                      <div>
                                        <div className="flex items-center gap-2">
                                          {r.displayAction === 'create' && !r.aggregate && (
                                            <span className="inline-flex shrink-0 items-center gap-0.5 text-xs text-emerald-600">
                                              <Plus size={12} /> 新增
                                            </span>
                                          )}
                                          <NameInput
                                            value={r.edit.name}
                                            onChange={(v) => updateRow(r.imgId, r.index, { name: v })}
                                          />
                                          {renameRuleEligible(r) && (
                                            <>
                                              <RuleToggle
                                                title={`将「${r.row.source_name}」→「${r.edit.name}」保存为该来源组的改名规则，下次导入自动套用`}
                                                label="存为规则"
                                                checked={r.edit.saveRename}
                                                onChange={() => updateRow(r.imgId, r.index, { saveRename: !r.edit.saveRename })}
                                              />
                                              {r.edit.saveRename && (
                                                <RuleNoteInput
                                                  value={r.edit.renameNote ?? ''}
                                                  onChange={(v) => updateRow(r.imgId, r.index, { renameNote: v })}
                                                  placeholder="备注为什么改名（可选）"
                                                />
                                              )}
                                            </>
                                          )}
                                        </div>
                                        {r.row.source_name && r.row.source_name !== r.edit.name && (
                                          <p className="mt-0.5 text-xs text-muted">识别: {r.row.source_name}</p>
                                        )}
                                      </div>
                                    ) : (
                                      <span className="font-medium text-slate-700">{r.edit.name || '--'}</span>
                                    )}
                                  <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                                    <button
                                      type="button"
                                      onClick={() => jumpToImage(r.imgId)}
                                      title={`跳到图 ${r.imageOrdinal} 的截图卡：${r.imageLabel}`}
                                      className="shrink-0 rounded-pill border border-slate-200 px-1.5 text-meta text-muted underline-offset-2 hover:border-brand-300 hover:text-brand-700 hover:underline"
                                    >
                                      图 {r.imageOrdinal} · {r.imageLabel}
                                    </button>
                                    {parentOutside && r.parent && (
                                      <span className="shrink-0 rounded-pill border border-brand-100 bg-brand-50 px-1.5 text-meta text-brand-700">
                                        ↳ {parentText}
                                      </span>
                                    )}
                                    {r.aggregate && (
                                      <span className="shrink-0 text-meta text-slate-500">
                                        汇总项 · 不计入合计
                                        {r.edit.aggregateSource ? ` · ${AGGREGATE_SOURCE_LABEL[r.edit.aggregateSource]}` : ''}
                                      </span>
                                    )}
                                    {r.aggregate && childCount > 0 && (
                                      <span className="shrink-0 text-meta text-slate-500">· {childCount} 个子项</span>
                                    )}
                                  </div>
                                  {dropTarget && (
                                    <p className={`mt-1 text-meta leading-relaxed ${dropTarget.ok ? 'text-brand-700' : 'text-rose-700'}`}>
                                      {dropTarget.ok
                                        ? `↳ 「${entryRows.find((x) => x.key === dropTarget.childKey)?.edit.name || '这一行'}」归到这一行下`
                                        : `⚠ ${dropVerdict && !dropVerdict.ok ? DROP_HINT[dropVerdict.reason] : '不能放在这里'}`}
                                    </p>
                                  )}
                                  {/* 忽略规则（票 07 D3）：只在跳过时出现，落名称列第二行，不随缩进 */}
                                  {r.edit.skipped && ignoreRuleEligible(r) && (
                                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                      <RuleToggle
                                        title={`将「${ignoreRuleMatch(r)}」保存为该来源组的忽略规则，下次导入自动丢弃`}
                                        hint="下次导入自动丢弃匹配行。"
                                        label="存为忽略"
                                        checked={r.edit.saveIgnore}
                                        onChange={() => updateRow(r.imgId, r.index, { saveIgnore: !r.edit.saveIgnore })}
                                      />
                                      {r.edit.saveIgnore && (
                                        <RuleNoteInput
                                          value={r.edit.ignoreNote ?? ''}
                                          onChange={(v) => updateRow(r.imgId, r.index, { ignoreNote: v })}
                                          placeholder="备注为什么忽略，如：总金额项"
                                        />
                                      )}
                                    </div>
                                  )}
                                  </div>
                                  {/* 行尾开关（票 07 D2）：图标 + hover 说明，默认淡、勾上才加深。
                                      放在名称之后右对齐：不挡引导线，也不抢名称的可读宽度 */}
                                  <div className="flex shrink-0 items-center gap-0.5">
                                    {childCount > 0 && (
                                      <button
                                        type="button"
                                        onClick={() => setFoldedRows((prev) => ({ ...prev, [r.key]: !prev[r.key] }))}
                                        aria-expanded={!folded}
                                        aria-label={`${folded ? '展开' : '折叠'} ${r.edit.name || '这一行'} 的子项`}
                                        className="flex h-6 w-6 items-center justify-center rounded text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                                      >
                                        {folded ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                                      </button>
                                    )}
                                    <button
                                      type="button"
                                      data-row-toggle="skip"
                                      data-active={r.edit.skipped ? 'true' : 'false'}
                                      aria-pressed={r.edit.skipped}
                                      title={r.edit.skipped ? '跳过本行（本次不录入）：点击恢复' : '跳过本行：本次不录入'}
                                      onClick={() => toggleRowSkip(r.imgId, r.index)}
                                      className={`flex h-6 w-6 items-center justify-center rounded ${
                                        r.edit.skipped
                                          ? 'bg-slate-200 text-slate-700'
                                          : 'text-slate-300 hover:bg-slate-100 hover:text-slate-600'
                                      }`}
                                    >
                                      <ListX size={14} />
                                      <span className="sr-only">跳过</span>
                                    </button>
                                    <button
                                      type="button"
                                      data-row-toggle="aggregate"
                                      data-active={r.aggregate ? 'true' : 'false'}
                                      aria-pressed={r.aggregate}
                                      title="汇总行：只核对，不入账"
                                      onClick={() => toggleRowAggregate(r.imgId, r.index)}
                                      className={`flex h-6 w-6 items-center justify-center rounded ${
                                        r.aggregate
                                          ? 'bg-brand-100 text-brand-700'
                                          : 'text-slate-300 hover:bg-slate-100 hover:text-slate-600'
                                      }`}
                                    >
                                      <Layers size={14} />
                                      <span className="sr-only">汇总行</span>
                                    </button>
                                  </div>
                                </div>
                                {/* 身份裁决 / 关联到已有项（票 02 D7）：搬进名称列，占满名称列宽度；
                                    汇总行不落账，故不在这里提供裁决入口 */}
                                {!r.aggregate && (r.row.identity_guess || (r.row.action === 'create' && !r.edit.skipped)) && (
                                  <div className="mt-1.5 space-y-1.5">
                                    {r.row.identity_guess && (
                                      <IdentityGuessNotice
                                        r={r}
                                        locked={isForceBusy(r.img)}
                                        surface="desktop"
                                        onUpdate={(patch) => updateRow(r.imgId, r.index, patch)}
                                      />
                                    )}
                                    {r.row.action === 'create' && !r.edit.skipped && (
                                      <RowBindControl
                                        r={r}
                                        locked={isForceBusy(r.img)}
                                        onOpen={openBindPicker}
                                        onClear={clearBind}
                                      />
                                    )}
                                    {/* 该名历史上有 merge 去向（spec D3）：桌面同款提示，位置紧随关联按钮 */}
                                    <ArchivedMergedIntoNotice r={r} />
                                  </div>
                                )}
                              </div>
                            </td>
                            <td className="px-3 py-2 text-right align-top">
                              {/* 汇总行的金额变灰只灰数字本身：下面的差额标记要能看清 */}
                              <div data-row-amount className={r.aggregate ? 'opacity-60' : ''}>
                                <AmountInput
                                  value={r.amount}
                                  disabled={r.displayAction === 'zero' || r.displayAction === 'keep'}
                                  onChange={(v) => updateRow(r.imgId, r.index, { amount: v })}
                                />
                              </div>
                              {diffItem && (
                                <AggregateDiffChip
                                  item={diffItem}
                                  onGo={() => {
                                    const el = document.getElementById('reconcile-panel')
                                    el?.scrollIntoView({ block: 'center' })
                                  }}
                                />
                              )}
                            </td>
                            <td className="px-2 py-2 text-center align-middle">
                              <ActionBadge
                                action={r.displayAction}
                                bound={bindTargetOf(r) != null}
                                aggregate={r.aggregate}
                              />
                            </td>
                            <td className="px-2 py-2 text-center align-middle">
                              <div className="flex flex-col items-center gap-0.5">
                                <VerifyBadge
                                  v={r.img.verifyByIndex?.[r.index]}
                                  ocrName={r.row.source_name}
                                  ocrAmount={r.row.source_amount}
                                  status={r.img.verifyStatus}
                                  error={r.img.verifyError}
                                />
                                {/* 双路对称裁决：分歧行一键把 AI 值填入本行编辑态（与手输同构，走行号通道） */}
                                {r.img.verifyByIndex?.[r.index]?.verdict === 'mismatch' && r.writable && (
                                  <button
                                    type="button"
                                    disabled={applyPending || isForceBusy(r.img)}
                                    onClick={() => adoptAiValue(r)}
                                    title="把 AI 识别的名称/金额填入本行编辑框（与手动修改相同，写入的是你确认的值；仍可再改）"
                                    className="inline-flex items-center gap-0.5 whitespace-nowrap text-meta font-medium text-brand-600 underline-offset-2 hover:text-brand-700 hover:underline disabled:cursor-not-allowed disabled:text-slate-300 disabled:no-underline"
                                  >
                                    <Bot size={11} /> 采纳 AI 值
                                  </button>
                                )}
                              </div>
                            </td>
                            <td className={`px-2 py-2 text-right align-middle font-mono ${diffColor(r.diff)}`}>
                              {/* 汇总行这一列留空（票 06 D1）：「本行金额 − 本行基线」对汇总行没意义，
                                  真正要看的是与子项合计的差额——那枚标记在金额格下方 */}
                              {r.aggregate ? null : formatSignedCurrency(r.diff)}
                            </td>
                            <td className="px-2 py-2 text-right align-middle">
                              {r.parent ? (
                                <span className="inline-flex items-center gap-1">
                                  <button
                                    type="button"
                                    onClick={() => {
                                      if (r.parent?.kind === 'row') focusRow(r.parent.key)
                                      else setImageFilter('all')
                                    }}
                                    title={`已归到 ${parentText}`}
                                    className="max-w-[7.5rem] truncate rounded-pill border border-brand-100 bg-brand-50 px-1.5 text-meta text-brand-700 underline-offset-2 hover:underline"
                                  >
                                    ↳ {parentText}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => clearParent(r.key)}
                                    aria-label={`取消「${r.edit.name || '这一行'}」的归属`}
                                    className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                                  >
                                    <X size={12} />
                                  </button>
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => setParentPickerKey(r.key)}
                                  disabled={applyPending || isForceBusy(r.img)}
                                  aria-label={`把「${r.edit.name || '这一行'}」归到某个汇总行`}
                                  className="inline-flex min-h-[26px] items-center whitespace-nowrap rounded-control border border-slate-200 bg-white px-2 text-xs text-slate-600 transition-colors hover:border-brand-300 hover:text-brand-700 disabled:opacity-50"
                                >
                                  归到…
                                </button>
                              )}
                            </td>
                          </tr>
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {/* 拖拽回执（票 09 D5）：拖动期间常驻一条，说明「停一下才放得下」与 Esc 回滚 */}
              {dragView && (
                <div className="pointer-events-none fixed inset-x-0 bottom-6 z-40 hidden justify-center lg:flex" role="status">
                  <span className="rounded-pill bg-slate-900/90 px-3 py-1.5 text-xs text-white shadow-overlay">
                    正在拖动「{entryRows.find((r) => r.key === dragView.childKey)?.edit.name || '这一行'}」
                    · 在汇总行上停 0.5 秒才放得下 · Esc 取消
                  </span>
                </div>
              )}

              {/* 窄屏：同一份行的卡片形态。拖拽手柄整列不做（触屏与滚动冲突），
                  归父只留「归到…」（WCAG 2.5.7 要求的单指非拖拽路径） */}
              <div className="lg:hidden divide-y divide-slate-100">
                {mobileTableRows.map(({ row: r, level, childCount, ancestorHasNext }) => (
                  <div
                    key={r.key}
                    id={`review-row-m-${r.key}`}
                    className="relative"
                    data-tree-indent
                    data-level={level}
                  >
                    {/* 窄屏也画层级线（票 01 D2）：卡片没有表格行高，线只做「哪几级还有下文」的提示 */}
                    {level > 0 && (
                      <span
                        aria-hidden="true"
                        data-tree-guides
                        className="pointer-events-none absolute inset-y-0 left-0"
                      >
                        {ancestorHasNext.map((hasNext, i) =>
                          hasNext ? (
                            <i
                              key={`m-lane-${i}`}
                              data-guide="lane"
                              data-lane={i}
                              data-guide-width={i === 0 ? 2 : 1}
                              className={`absolute top-0 bottom-0 ${i === 0 ? 'w-[2px] bg-brand-600' : 'w-px bg-slate-300'}`}
                              style={{ left: i * LANE_PX + LANE_CENTER - (i === 0 ? 1 : 0.5) }}
                            />
                          ) : null,
                        )}
                      </span>
                    )}
                    <div style={{ paddingLeft: level > 0 ? indentPad(level) - LANE_PX : 0 }}>
                    <MobileEntryCard
                      r={r}
                      locked={isForceBusy(r.img)}
                      applyPending={applyPending}
                      childCount={childCount}
                      folded={!!foldedRows[r.key]}
                      parentLabel={parentTextOf(r)}
                      diffItem={r.aggregate ? (reconcileItems.find((it) => it.parent.id === `row:${r.key}`) ?? null) : null}
                      onFold={() => setFoldedRows((prev) => ({ ...prev, [r.key]: !prev[r.key] }))}
                      onUpdate={updateRow}
                      onToggleSkip={toggleRowSkip}
                      onToggleAggregate={toggleRowAggregate}
                      onAdoptAi={adoptAiValue}
                      onOpenBind={openBindPicker}
                      onClearBind={clearBind}
                      onOpenParent={() => setParentPickerKey(r.key)}
                      onClearParent={() => clearParent(r.key)}
                      onJumpSource={() => jumpToImage(r.imgId)}
                    />
                    </div>
                  </div>
                ))}
                {mobileTableRows.length === 0 && (
                  <p className="p-4 text-sm text-muted">
                    {mobileOnlyIssues ? '没有疑点行（其余行默认采纳）' : '没有待录入项'}
                  </p>
                )}
              </div>

              {/* 口径与键盘说明（原型页脚）：把「谁计入合计」写死在这里，别让人从控件猜 */}
              <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-2.5 text-meta leading-relaxed text-slate-500 lg:px-6">
                勾「汇总行」的行只核对、不入账：金额变灰、动作显示「核对」，不计入本批写入合计；取消勾选即恢复正常落账。
                <br className="hidden lg:block" />
                汇总行的来源分三种：手动勾选、词表预勾（命中汇总词表）、归父自动——把某行当父项时它自动成为汇总行，最后一条子项解除时只自动撤「归父自动」这一种。
                <br className="hidden lg:block" />
                归到某行后：<b className="font-semibold text-slate-600">父项只核对、不计入合计</b>，本行（子项）金额照常计入；父行金额与子项之和的差额显示在金额下方。
                <span className="hidden lg:inline">桌面可拖行首手柄，等价操作是行尾「归到…」，手机宽度只保留后者。</span>
                键盘：Tab 到「归到…」→ Enter → <kbd className="rounded border border-slate-300 bg-white px-1 font-code text-[10px]">/</kbd> 聚焦搜索 ·{' '}
                <kbd className="rounded border border-slate-300 bg-white px-1 font-code text-[10px]">↑</kbd>
                <kbd className="rounded border border-slate-300 bg-white px-1 font-code text-[10px]">↓</kbd> 选择 ·{' '}
                <kbd className="rounded border border-slate-300 bg-white px-1 font-code text-[10px]">Enter</kbd> 确认 ·{' '}
                <kbd className="rounded border border-slate-300 bg-white px-1 font-code text-[10px]">Esc</kbd> 关闭。
              </div>

              {entryGroups.length === 0 && (
                <p className="px-6 py-6 text-center text-sm text-muted">
                  没有识别出待录入项。可在下方处理未识别项，或点右上「AI 校验」看 AI 是否识别到额外项再补录
                </p>
              )}
            </div>

            {/* 截图卡：页角色（仅注释）、逐图重识别、AI 识别项与补录。
                行清单已上移到总表，这里只留「按图」的入口与产物 */}
            {entryGroups.length > 0 && (
            <div className="bg-white rounded-xl shadow-card overflow-hidden">
              <div className="flex items-center gap-3 border-b border-slate-100 px-6 py-3">
                <Camera size={18} className="text-brand-600" />
                <div className="min-w-0">
                  <p className="font-medium text-brand-950">截图 · 页角色与补录</p>
                  <p className="text-xs text-slate-500">
                    页角色仅作注释，不改任何判定与合计；AI 识别项与补录在这里处理
                  </p>
                </div>
              </div>
              {entryGroups.map((g) => (
                <div key={g.imgId} id={`shot-${g.imgId}`} className="scroll-mt-[var(--pf-header-h)]">
                  {/* 卡头缩略图走本地 blob：这一批图刚上传，blob 已在内存里，28px 的小图
                      不必再拉一遍几 MB 的原图；查看器里才用 evidence 原图端点（D3 主路径） */}
                  <ImageCardHeader
                    imgId={g.imgId}
                    imageLabel={g.imageLabel}
                    groupLabel={g.groupLabel}
                    rowCount={g.rows.length}
                    pageRole={g.img.pageRole}
                    onChangePageRole={setPageRole}
                    selected={g.img.selectedDate ?? g.img.preview?.data_date ?? today}
                    suspected={g.img.preview?.suspected_date}
                    suspectedText={g.img.preview?.suspected_date_text}
                    today={today}
                    onChangeDate={setImageDate}
                    job={g.img.job}
                    busy={isForceBusy(g.img)}
                    hasIssue={imgHasVerifyIssues(g.img)}
                    forceDisabled={verifying || applyPending || !!g.img.parsing}
                    parseError={g.img.parseError}
                    onForceReocr={handleForceReocr}
                    onOpenLines={openLinesModal}
                    engine={engineOf(g.img)}
                    aiAvailable={aiAvailable}
                    aiCount={g.img.aiOnly?.length ?? 0}
                    engineSwitch={!!g.img.engineSwitch}
                    engineDisabled={
                      verifying || applyPending || !!g.img.parsing || g.img.verifyStatus === 'verifying'
                    }
                    onSwitchEngine={handleSwitchEngine}
                    onOpenSettings={setSettingsImgId}
                    thumbSrc={g.img.previewUrl || reviewImageSrc(g.img)}
                    onOpenImage={openImageViewer}
                  />
                  {/* AI 主识别状态条（§4a：失败 = 显式阻断态，绝不静默空清单）：
                      识别中 = 进度可见；失败 = 原因 + 「重试 / 转用 OCR」两个显式动作，批内其余图不受影响 */}
                  {engineOf(g.img) === 'ai' && g.img.verifyStatus === 'verifying' && (
                    <div className="px-6 py-2.5 border-b border-brand-100 bg-brand-50/50 text-xs text-brand-700 flex items-center gap-2">
                      <Loader2 size={13} className="animate-spin shrink-0" />
                      <span>AI 识别中…（识别清单生成后将自动采纳为草稿，逐项核对后才会写入）</span>
                    </div>
                  )}
                  {engineOf(g.img) === 'ai' && g.img.verifyStatus === 'failed' && (
                    <div className="px-6 py-2.5 border-b border-red-100 bg-red-50/60 text-xs text-red-700">
                      <div className="flex items-center gap-2 flex-wrap">
                        <AlertTriangle size={13} className="shrink-0" />
                        <span className="font-medium whitespace-nowrap">AI 识别失败：</span>
                        <span className="min-w-0">{g.img.verifyError || '请求异常'}</span>
                        <span className="ml-auto flex items-center gap-2 whitespace-nowrap">
                          <button
                            type="button"
                            disabled={applyPending || !!g.img.parsing || verifying}
                            onClick={() => retryAiRecognition(g.imgId)}
                            className="inline-flex items-center gap-1 px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border border-red-300 bg-white text-red-600 hover:bg-red-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            <RefreshCw size={11} /> 重试
                          </button>
                          <button
                            type="button"
                            disabled={applyPending || !!g.img.parsing}
                            onClick={() => handleSwitchEngine(g.imgId)}
                            className="inline-flex items-center gap-1 px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border border-slate-300 bg-white text-slate-600 hover:border-brand-400 hover:text-brand-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            <Cpu size={11} /> 转用 OCR
                          </button>
                        </span>
                      </div>
                      <p className="mt-1 text-meta text-red-500/80">
                        AI 识别失败不会静默降级：本图不产生写入清单（也不会制造空凭证），批内其余图与写入流不受影响
                      </p>
                    </div>
                  )}
                  {/* 该图正在强制重新识别/切换引擎：清单即将整体换新，期间锁定编辑，避免白改后被复位 */}
                  {g.rows.length === 0 && (
                    <p className="px-6 py-4 text-sm text-muted">
                      {engineOf(g.img) === 'ai'
                        ? 'AI 主识别图没有 OCR 行骨架：识别项走下方「AI 识别清单 → 采纳」通道；组内现有资产见「未识别项」'
                        : '本图没有识别出待录入项（识别到的都是系统里已有、本张截图没有的项，见下方「未识别项」）'}
                    </p>
                  )}
                  {/* AI 项 → 补录/采纳：蓝色条是「机器认出但骨架里没有」的源，绿色块是「我确认要的」草稿。
                      两者视觉分开，判定结论（将更新/将新增/冲突）就地可见，写入前不做盲写。
                      术语按模式映射（§1.2 强制项）：双路=「AI 额外识别/补录」；AI 主识别=「AI 识别清单/采纳」 */}
                  {((g.img.aiOnly?.length ?? 0) > 0 || (g.img.supplements?.length ?? 0) > 0) && (
                    <div
                      className={
                        (g.img.aiOnly?.length ?? 0) > 0
                          ? 'border-t border-blue-100 bg-blue-50/60'
                          : 'border-t border-emerald-100'
                      }
                    >
                      {(g.img.aiOnly?.length ?? 0) > 0 && (
                        <div className="px-6 py-2.5 text-xs text-blue-700 flex items-center gap-2 flex-wrap">
                          <Bot size={13} className="shrink-0" />
                          <span>
                            {engineOf(g.img) === 'ai'
                              ? `AI 识别清单 ${g.img.aiOnly!.length} 项（已自动采纳为草稿；逐项核对后随本次写入，删除即不写）：`
                              : `AI 额外识别 ${g.img.aiOnly!.length} 项（OCR 未认出，可逐项补录进本次清单）：`}
                          </span>
                          {g.img.aiOnly!.map((a, i) => {
                            const drafted = (g.img.supplements ?? []).some(
                              (d) => d.name.trim() === (a.name ?? '').trim(),
                            )
                            return (
                              <span
                                key={i}
                                className="inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded bg-white border border-blue-200 whitespace-nowrap"
                              >
                                {a.name} {formatCurrency(a.amount)}
                                {drafted ? (
                                  <span className="inline-flex items-center gap-0.5 text-meta text-emerald-600">
                                    <CheckCircle2 size={11} /> {engineOf(g.img) === 'ai' ? '已采纳' : '已补录'}
                                  </span>
                                ) : (
                                  <button
                                    type="button"
                                    disabled={applyPending || isForceBusy(g.img)}
                                    onClick={() => addSupplements(g.imgId, [a])}
                                    title={
                                      engineOf(g.img) === 'ai'
                                        ? '重新采纳这项 AI 识别项；加入后会先判定是更新已有资产还是新增，判定结果可见后再写入'
                                        : '把这项加入本次待写入清单；加入后会先判定是更新已有资产还是新增，判定结果可见后再写入'
                                    }
                                    className="inline-flex items-center gap-0.5 text-meta font-medium text-brand-600 hover:text-brand-700 hover:underline underline-offset-2 disabled:text-slate-300 disabled:no-underline disabled:cursor-not-allowed"
                                  >
                                    <Plus size={11} /> {engineOf(g.img) === 'ai' ? '采纳' : '补录'}
                                  </button>
                                )}
                              </span>
                            )
                          })}
                          <span className="ml-auto flex items-center gap-2">
                            {g.img.aiOnly!.some(
                              (a) => !(g.img.supplements ?? []).some((d) => d.name.trim() === (a.name ?? '').trim()),
                            ) && (
                              <button
                                type="button"
                                disabled={applyPending || isForceBusy(g.img)}
                                onClick={() =>
                                  addSupplements(
                                    g.imgId,
                                    g.img.aiOnly!.filter(
                                      (a) =>
                                        !(g.img.supplements ?? []).some(
                                          (d) => d.name.trim() === (a.name ?? '').trim(),
                                        ),
                                    ),
                                  )
                                }
                                title={
                                  engineOf(g.img) === 'ai'
                                    ? '把本图尚未采纳的 AI 识别项一次全部采纳（按名称去重），默认按资产记账'
                                    : '把本图尚未补录的 AI 额外识别项一次全部加入（按名称去重），默认按资产记账'
                                }
                                className="inline-flex items-center gap-1 px-2 py-2 lg:py-0.5 min-h-[44px] lg:min-h-0 rounded text-meta font-medium border border-brand-300 bg-brand-50 text-brand-700 hover:bg-brand-100 transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                <Plus size={11} /> {engineOf(g.img) === 'ai' ? '全部采纳' : '全部补录'}
                              </button>
                            )}
                            {/* 单边缺项就近入口：怀疑 OCR 漏识别时，就地重新识别这张图（与卡头部同一动作）；
                                AI 主识别图不适用（无 OCR 骨架可重跑，重试/转引擎入口在识别状态条与卡头） */}
                            {engineOf(g.img) !== 'ai' && (
                              <ForceOcrButton
                                busy={isForceBusy(g.img)}
                                disabled={verifying || applyPending || !!g.img.parsing}
                                hasIssue
                                onClick={() => handleForceReocr(g.imgId)}
                              />
                            )}
                          </span>
                        </div>
                      )}
                      {(g.img.supplements?.length ?? 0) > 0 && (
                        <div className="px-6 py-3 border-t border-emerald-100 bg-emerald-50/50 space-y-2">
                          <div className="flex items-center gap-2 flex-wrap text-xs text-emerald-800">
                            <Plus size={13} className="shrink-0" />
                            <span className="font-medium">
                              {engineOf(g.img) === 'ai'
                                ? `已采纳 ${g.img.supplements!.length} 项（AI 识别）`
                                : `我补录的 ${g.img.supplements!.length} 项`}
                            </span>
                            <span className="text-emerald-700/70">
                              {engineOf(g.img) === 'ai'
                                ? '请逐项核对名称/金额/方向 —— AI 识别项需你确认后写入'
                                : '默认按资产记账；负债项请逐项切换'}
                            </span>
                            {supResolving[g.imgId] && (
                              <span className="inline-flex items-center gap-1 text-slate-500">
                                <Loader2 size={12} className="animate-spin" />
                                判定中…
                              </span>
                            )}
                            <span className="ml-auto text-emerald-700/60">
                              {engineOf(g.img) === 'ai'
                                ? '判定为预览，写入时由后端重新核对；未经你确认的项不写入'
                                : '随本次写入一起提交；比对供裁决，写入的是你确认的清单'}
                            </span>
                          </div>
                          {g.img.supplements!.map((d) => (
                            <SupplementDraftRow
                              key={d.uid}
                              draft={d}
                              resolving={!!supResolving[g.imgId]}
                              locked={applyPending || isForceBusy(g.img)}
                              skippedCross={skipCrossSupplements.some(
                                (x) => x.imgId === g.imgId && x.draft.uid === d.uid,
                              )}
                              onChange={(patch) => updateSupplement(g.imgId, d.uid, patch)}
                              onRemove={() => removeSupplement(g.imgId, d.uid)}
                              onRetry={() => void resolveSupplements(g.imgId)}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
            )}

          {/* 未识别项区（三态策略） */}
          <div className="bg-white rounded-xl shadow-card overflow-hidden">
            <div className="px-6 py-4 border-b border-slate-100">
              <div className="flex items-center gap-3 mb-2">
                <ShieldCheck size={20} className="text-brand-600" />
                <div>
                  <p className="font-medium text-brand-950">未识别项</p>
                  <p className="text-sm text-slate-500">
                    系统有、本批截图均未出现的资产项，共 {keepRows.length} 条，选择处理策略
                  </p>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {(
                  [
                    { key: 'keep' as const, label: '保持原值', desc: '默认，不改动' },
                    { key: 'zeroEach' as const, label: '单独清0', desc: '勾选哪些清0' },
                    { key: 'zeroAll' as const, label: '全部清0', desc: '全部清零' },
                  ] as const
                ).map((opt) => {
                  const active = unrecognizedPolicy === opt.key
                  // §4c：批内含 AI 主识别图时「全部清0」禁用（keep 行由模型召回率背书，未经验证；
                  // 误清零破坏不对称）。单独清0 / 「不录入」联动 = 逐项显式决策，保留。后端另有 400 兜底
                  const blocked = opt.key === 'zeroAll' && anyAiParsed
                  return (
                    <button
                      key={opt.key}
                      onClick={() => setUnrecognizedPolicy(opt.key)}
                      disabled={blocked}
                      title={
                        blocked
                          ? 'AI 识别模式下「全部清0」不可用：keep 行代表「AI 未在图中认出」，召回率未经验证，整组清零风险不对称；请用「单独清0」或「不录入」逐项决策'
                          : undefined
                      }
                      className={`flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] lg:min-h-0 text-sm rounded-lg border transition-colors ${
                        active
                          ? 'bg-brand-600 text-white border-brand-600'
                          : blocked
                            ? 'bg-slate-50 text-slate-300 border-slate-200 cursor-not-allowed'
                            : 'bg-white text-slate-600 border-slate-200 hover:border-brand-300'
                      }`}
                    >
                      {opt.label}
                      {!active && <span className={`text-xs ${blocked ? 'text-slate-300' : 'text-muted'}`}>· {blocked ? 'AI 图不可用' : opt.desc}</span>}
                    </button>
                  )
                })}
                {anyAiParsed && (
                  <HintToggle
                    tone="amber"
                    hint="AI 识别图不能整组清零，请逐项「单独清0」或「不录入」。"
                  />
                )}
                <span className="text-slate-200 mx-1 hidden sm:inline">|</span>
                <button
                  onClick={anyKeepExcluded ? clearAllKeepExcludes : excludeAllKeepRows}
                  disabled={keepRows.length === 0}
                  title={
                    anyKeepExcluded
                      ? '取消所有行的「不录入」：撤销排除与备注，并退回随排除联动的本次清 0 勾选（整体撤销；单行取消则保留清 0 勾选，本次仍写 0）'
                      : '当前全部未识别项：本次写入清 0，且以后不再出现在未识别列表（备注可逐行补；规则可在 设置 · 来源管理 删除恢复）'
                  }
                  className="flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] lg:min-h-0 text-sm rounded-lg border border-dashed border-slate-300 text-slate-600 hover:border-amber-400 hover:text-amber-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {anyKeepExcluded ? <ListChecks size={14} /> : <ListX size={14} />}
                  {anyKeepExcluded ? '取消全部不录入' : '全部不录入'}
                </button>
                <HintToggle
                  tone="amber"
                  hint={
                    anyKeepExcluded
                      ? '撤销全部排除与备注，并退回联动的清 0 勾选。'
                      : '本次全部清 0，且以后不再出现在未识别列表；可在来源管理恢复。'
                  }
                />
              </div>
            </div>

            {keepRows.length === 0 ? (
              <p className="px-6 py-6 text-sm text-muted text-center">本次没有未识别项</p>
            ) : (
              <>
              <div className="hidden lg:block overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50">
                    <tr>
                      {unrecognizedPolicy === 'zeroEach' && (
                        <th className="text-left px-4 py-2 w-14 text-slate-600 font-medium">清0</th>
                      )}
                      {unrecognizedPolicy !== 'keep' && (
                        <th className="text-left px-4 py-2 w-14 text-slate-600 font-medium">跳过</th>
                      )}
                      <th className="text-left px-4 py-2 w-28 text-slate-600 font-medium">不录入</th>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">资产名称</th>
                      <th className="text-right px-4 py-2 w-28 text-slate-600 font-medium">当前金额</th>
                      <th className="text-center px-4 py-2 w-24 text-slate-600 font-medium">动作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keepRows.map((r) => (
                      <tr key={`${r.imgId}_${r.index}`} className={`border-t border-slate-50 ${r.edit.skipped ? 'bg-slate-50 opacity-60' : ''}`}>
                        {unrecognizedPolicy === 'zeroEach' && (
                          <td className="px-4 py-2">
                            <label className="inline-flex min-h-[44px] min-w-[44px] items-center lg:min-h-0 lg:min-w-0">
                            <input
                              type="checkbox"
                              checked={r.isZeroed || r.edit.zeroChecked}
                              onChange={() => toggleZeroChecked(r.imgId, r.index)}
                              className="w-5 h-5 lg:w-4 lg:h-4 accent-amber-600 cursor-pointer"
                            />
                            </label>
                          </td>
                        )}
                        {unrecognizedPolicy !== 'keep' && (
                          <td className="px-4 py-2">
                            <div className="flex flex-col items-start gap-1">
                              <label className="inline-flex min-h-[44px] min-w-[44px] items-center lg:min-h-0 lg:min-w-0">
                              <input
                                type="checkbox"
                                checked={r.edit.skipped}
                                onChange={() => toggleRowSkip(r.imgId, r.index)}
                                className="w-5 h-5 lg:w-4 lg:h-4 accent-brand-600 cursor-pointer"
                              />
                              </label>
                              {r.edit.skipped && ignoreRuleEligible(r) && (
                                <>
                                  <RuleToggle
                                    title={`将「${ignoreRuleMatch(r)}」保存为该来源组的忽略规则，下次导入自动丢弃`}
                                    hint="下次导入自动丢弃匹配行。"
                                    label="存为忽略"
                                    checked={r.edit.saveIgnore}
                                    onChange={() => updateRow(r.imgId, r.index, { saveIgnore: !r.edit.saveIgnore })}
                                  />
                                  {r.edit.saveIgnore && (
                                    <RuleNoteInput
                                      value={r.edit.ignoreNote ?? ''}
                                      onChange={(v) => updateRow(r.imgId, r.index, { ignoreNote: v })}
                                      placeholder="备注为什么忽略，如：总金额项"
                                    />
                                  )}
                                </>
                              )}
                            </div>
                          </td>
                        )}
                        <td className="px-4 py-2">
                          <div className="flex flex-col items-start gap-1">
                            <RuleToggle
                              title={
                                r.edit.skipped
                                  ? '已跳过的行本次不写入，无法设为不录入；如需排除请先取消跳过'
                                  : '本次写入时清 0，且以后不再出现在未识别列表；适合已改名 / 已清仓的项目（规则可在 设置 · 来源管理 删除恢复）'
                              }
                              label="不录入"
                              hint={
                                r.edit.skipped
                                  ? '已跳过的行不能设为不录入，先取消跳过。'
                                  : '本次清 0 且以后不再出现；可在来源管理恢复。'
                              }
                              checked={!!r.edit.saveExclude}
                              disabled={r.edit.skipped}
                              onChange={() => toggleExclude(r.imgId, r.index)}
                            />
                            {r.edit.saveExclude && !r.edit.skipped && (
                              <RuleNoteInput
                                value={r.edit.excludeNote ?? ''}
                                onChange={(v) => updateRow(r.imgId, r.index, { excludeNote: v })}
                                placeholder="备注：已改名→xx / 已清仓"
                              />
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-2">
                          <span className="font-medium text-slate-700">{r.edit.name || '--'}</span>
                          {r.isZeroed && <p className="text-xs text-amber-600 mt-0.5">将清零</p>}
                        </td>
                        <td className="px-4 py-2 text-right font-mono text-slate-700">
                          {formatCurrency(r.row.current_amount || '0')}
                        </td>
                        <td className="px-4 py-2 text-center">
                          <ActionBadge action={r.displayAction} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {/* 移动：未识别项逐行卡片（桌面表格在上面 hidden lg:block） */}
              <div className="lg:hidden divide-y divide-slate-100">
                {keepRows.map((r) => (
                  <MobileKeepCard
                    key={`${r.imgId}_${r.index}`}
                    r={r}
                    policy={unrecognizedPolicy}
                    onUpdate={updateRow}
                    onToggleSkip={toggleRowSkip}
                    onToggleZero={toggleZeroChecked}
                    onToggleExclude={toggleExclude}
                  />
                ))}
              </div>
              </>
            )}
          </div>

          {/* 本批核对（票 04 D4 / 票 05 D6）：差额零容差，三个出口——改子项 / 补子项 / 显式接受。
              差额未处理时这里明说「不能写入」，写入按钮同时禁用（两处一致，不靠按钮灰猜原因） */}
          {entryRows.length > 0 && (
            <div id="reconcile-panel">
            <ReconcilePanel
              items={reconcileItems}
              open={reconcileOpen}
              onToggle={() => setReconcileOpen((v) => !v)}
              onAccept={acceptReconcileDiff}
              onWithdraw={withdrawReconcileAccept}
              onGoToChildren={(it) => {
                const first = it.children[0]
                if (first) {
                  focusRow(first.key, 'amount')
                  return
                }
                const parentRow = reconcileParentRow(it)
                if (parentRow) focusRow(parentRow.key)
                pushRuleNotice(
                  'info',
                  `「${it.parent.name}」还没有子项：在要归入的那一行点「归到…」选它，差额才会算出来`,
                )
              }}
              onLocateParent={(it) => {
                const parentRow = reconcileParentRow(it)
                if (parentRow) focusRow(parentRow.key)
              }}
              pending={applyPending}
              acceptSeed={acceptedReason}
            />
            </div>
          )}

          {/* Confirm write footer */}
          <div className="bg-white rounded-xl shadow-card px-4 py-4 border-t space-y-3 sm:px-6">
            {/* 身份裁决阻断摘要：按钮禁用是结果，原因必须写在这里（移动端 375px 也读得到） */}
            {identityBlockers.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <p className="flex items-start gap-1.5 font-medium">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-500" aria-hidden="true" />
                  <span>还有 {identityBlockers.length} 行需要确认身份（疑似改名/拆分）</span>
                  {/* 折叠的父行会把子行藏起来：给一个「把所有父行都展开」的直达按钮（票 02 D8） */}
                  <button
                    type="button"
                    onClick={() => {
                      const byKey = new Map(entryRows.map((x) => [x.key, x]))
                      const toUnfold = new Set<RowKey>()
                      for (const b of identityBlockers) {
                        let p = b.parent
                        let guard = 0
                        while (p && p.kind === 'row' && guard < 50) {
                          toUnfold.add(p.key)
                          p = byKey.get(p.key)?.parent ?? null
                          guard += 1
                        }
                      }
                      setFoldedRows((prev) => {
                        const next = { ...prev }
                        for (const k of toUnfold) next[k] = false
                        return next
                      })
                      pushRuleNotice(
                        'info',
                        toUnfold.size > 0 ? `已展开 ${toUnfold.size} 个父行` : '没有折叠的父行需要展开',
                      )
                    }}
                    className="ml-auto shrink-0 rounded border border-amber-300 bg-white/60 px-1.5 text-meta text-amber-900 underline-offset-2 hover:underline"
                  >
                    一键展开含待确认的父行
                  </button>
                </p>
                <ul className="mt-1 space-y-0.5 pl-5">
                  {identityBlockers.slice(0, 4).map((r) => (
                    <li key={`${r.imgId}_${r.index}`}>
                      <button
                        type="button"
                        onClick={() => scrollToIdentityRow(r)}
                        className="w-full break-words rounded text-left underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-1"
                        title="跳到该行的身份确认"
                      >
                        「{r.edit.name || r.row.source_name || '未命名'}」
                        {r.img.preview?.image_filename && (
                          <span className="text-amber-700/70"> · {r.img.preview.image_filename}</span>
                        )}
                        {identityMergeNeedsTarget(r) && <span className="text-amber-700">（请选择要沿用的旧项）</span>}
                      </button>
                    </li>
                  ))}
                  {identityBlockers.length > 4 && <li>等 {identityBlockers.length} 行</li>}
                </ul>
                <p className="mt-1 text-meta text-amber-700">
                  在对应行选择「沿用旧项（合并）/ 作为新资产新增 / 撤销该行」后即可写入；点上方名称可跳到该行。
                </p>
              </div>
            )}
            {/* 新增行待确认（D4 扩面）：关联到已有项 / 确认新增，二选一后才放行写入 */}
            {createPendingRows.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="flex items-start gap-1.5 font-medium">
                    <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-500" aria-hidden="true" />
                    <span>本批 {createPendingRows.length} 行待确认</span>
                  </p>
                  <button
                    type="button"
                    onClick={confirmAllCreate}
                    disabled={applyPending || anyForceRunning}
                    className="inline-flex min-h-[44px] items-center gap-1 rounded-lg border border-amber-300 bg-white px-2.5 py-1 text-xs font-medium text-amber-800 transition-colors hover:border-amber-400 hover:text-amber-900 disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-[32px]"
                  >
                    <CheckCircle2 size={13} aria-hidden="true" /> 本批全部确认新增
                  </button>
                </div>
                <p className="mt-1 text-meta text-amber-700">
                  识别成「新增」的行要先表态：其实已有这一项就点行内「关联到已有项」，确实是新的就确认新增。
                </p>
              </div>
            )}
            {/* 核对差额未处理（ADR-0029 D10）：按钮禁用是结果，原因写在这里，并指到具体行 */}
            {reconcileBlocking.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <p className="flex items-start gap-1.5 font-medium">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-500" aria-hidden="true" />
                  <span>{reconcileBlocking.length} 条核对还没对上，本批暂不能写入</span>
                </p>
                <ul className="mt-1 space-y-0.5 pl-5">
                  {reconcileBlocking.slice(0, 4).map((it) => (
                    <li key={it.parent.id}>
                      「{it.parent.name}」·{' '}
                      {it.blockKind === 'parent-not-asset'
                        ? '需先落成资产项'
                        : it.blockKind === 'stale'
                          ? '接受过的差额又变了，需要重新接受'
                          : `差额 ${it.diff === null ? '未知' : formatSignedCurrency(it.diff.toString())} 未处理`}
                    </li>
                  ))}
                  {reconcileBlocking.length > 4 && <li>等 {reconcileBlocking.length} 条</li>}
                </ul>
                <p className="mt-1 text-meta text-amber-700">
                  改子项金额，或在该汇总行「接受差额」并写明理由；两处都能从下方「本批核对」进入。
                </p>
              </div>
            )}
            <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
              <p className="flex items-center gap-2 text-xs text-amber-600">
                <AlertTriangle size={14} className="shrink-0" />
                财务数据将写入各图所选的数据日期，写入前请仔细核对金额、差异与日期
              </p>
              <div className="flex flex-wrap items-center gap-3">
                {anyForceRunning ? (
                  <span className="inline-flex items-center gap-1.5 text-sm text-amber-600">
                    <Loader2 size={14} className="animate-spin" />
                    有截图正在重新识别，完成后可写入
                  </span>
                ) : (
                  <span className="text-sm text-slate-500">
                    {writableRows.length + sendableSupplementCount} 条待写入
                    {aggregateRowCount > 0 && (
                      <span className="text-muted">，另有 {aggregateRowCount} 行只核对不入账</span>
                    )}
                    {sendableSupplementCount > 0 && (
                      <span className="text-muted">
                        （
                        {pureAiBatch
                          ? `AI 采纳 ${sendableSupplementCount}`
                          : `OCR ${writableRows.length} ＋ ${anyAiParsed ? '采纳/补录' : '补录'} ${sendableSupplementCount}`}
                        ）
                      </span>
                    )}
                  </span>
                )}
                <button
                  onClick={confirmWrite}
                  disabled={
                    applyPending ||
                    (writableRows.length === 0 && sendableSupplementCount === 0) ||
                    anyForceRunning ||
                    identityBlockers.length > 0 ||
                    createPendingRows.length > 0 ||
                    reconcileBlocking.length > 0
                  }
                  title={
                    identityBlockers.length > 0
                      ? '请先完成疑似改名/拆分行的身份确认'
                      : createPendingRows.length > 0
                        ? '请先处理待确认的新增行：关联到已有项，或确认新增'
                        : reconcileBlocking.length > 0
                          ? '还有核对差额没处理：改子项金额，或接受差额并写明理由'
                          : anyForceRunning
                            ? '有截图正在强制重新识别，完成后清单会刷新，再确认写入'
                            : undefined
                  }
                  className="inline-flex items-center gap-2 px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:bg-slate-300 disabled:hover:bg-slate-300 disabled:opacity-80 disabled:cursor-not-allowed"
                >
                  {applyPending ? (
                    <>
                      <Loader2 size={16} className="animate-spin" />
                      写入中（{applyProgress}/{applyTotal}）...
                    </>
                  ) : (
                    <>
                      <Save size={16} />
                      确认写入（{writableRows.length + sendableSupplementCount} 条）
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
          </div>

          {/* 「本图设置」底部抽屉（移动）：图卡头单行塞不下的日期/引擎/重识别/原始行收在这里 */}
          {settingsGroup && (
            <ImageSettingsSheet
              imgId={settingsGroup.imgId}
              imageLabel={settingsGroup.imageLabel}
              groupLabel={settingsGroup.groupLabel}
              rowCount={settingsGroup.rows.length}
              selected={settingsGroup.img.selectedDate ?? settingsGroup.img.preview?.data_date ?? today}
              suspected={settingsGroup.img.preview?.suspected_date}
              suspectedText={settingsGroup.img.preview?.suspected_date_text}
              today={today}
              onChangeDate={setImageDate}
              busy={isForceBusy(settingsGroup.img)}
              hasIssue={imgHasVerifyIssues(settingsGroup.img)}
              forceDisabled={verifying || applyPending || !!settingsGroup.img.parsing}
              parseError={settingsGroup.img.parseError}
              onForceReocr={handleForceReocr}
              onOpenLines={openLinesModal}
              engine={engineOf(settingsGroup.img)}
              aiAvailable={aiAvailable}
              engineDisabled={
                verifying || applyPending || !!settingsGroup.img.parsing || settingsGroup.img.verifyStatus === 'verifying'
              }
              onSwitchEngine={handleSwitchEngine}
              onClose={() => setSettingsImgId(null)}
              pageRole={settingsGroup.img.pageRole}
              onChangePageRole={setPageRole}
            />
          )}

          {/* 关联到已有项：候选选择器（一次只服务一行；选中即写 bind 决策）。
              本批的汇总行不可作为关联目标（ADR-0029 D13）：它们只核对、不入账，
              把一行关联过去会变成「同一个金额既核对又落账」 */}
          {bindPickerRow && (
            <BindItemPicker
              rowName={bindPickerRow.name}
              blockedItems={aggregateItemBlocker}
              onPick={pickBindTarget}
              onClose={closeBindPicker}
            />
          )}

          {/* 归到…（ADR-0029 D12 主路径）：可搜索父项、全键盘可达；拖拽只是它的加速器 */}
          <ParentPicker
            open={!!parentPickerRow}
            rowName={parentPickerRow ? parentPickerRow.edit.name || parentPickerRow.row.source_name || '' : ''}
            candidates={parentCandidates}
            writeTotalLabel={formatCurrency(writeTotal.toString())}
            onPick={(c) => {
              if (!parentPickerRow) return
              const parent: ParentRef =
                c.kind === 'row' && c.key
                  ? { kind: 'row', key: c.key }
                  : { kind: 'item', itemId: c.itemId as number, name: c.name }
              assignParent(parentPickerRow.key, parent, c.name)
            }}
            onClose={() => setParentPickerKey(null)}
          />

          {/* 原图查看器（review-image-viewer D1/D2）：卡片文件名 / 缩略图点开，
              与「待录入清单」同序，可左右翻页。关闭后焦点回到点它的那个元素 */}
          {viewerIndex !== null && (
            <ImageViewer
              images={viewerImages}
              index={viewerIndex}
              onIndexChange={setViewerIndex}
              onClose={() => setViewerIndex(null)}
            />
          )}
        </div>
      )}

      {/* ============ Phase: Result ============ */}
      {phase === 'result' && (
        <div className="space-y-4">
          <div className="bg-emerald-50 rounded-xl border border-emerald-200 p-6">
            <div className="flex items-start gap-3">
              <CheckCircle2 size={22} className="text-emerald-600 shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="text-lg font-semibold text-emerald-900">写入成功</p>
                <p className="text-sm text-emerald-700 mt-0.5">
                  共写入 {totalRowsWritten} 条记录，生成 {applyResults.length} 份截图凭证
                  {totalSupplementsWritten > 0 && `（含补录 ${totalSupplementsWritten} 条）`}
                </p>
              </div>
            </div>
            <dl className="mt-5 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <dt className="text-xs text-slate-500">写入行数</dt>
                <dd className="mt-0.5 font-semibold text-brand-950">
                  {totalRowsWritten} 条
                  {totalSupplementsWritten > 0 && (
                    <span className="ml-1 text-xs font-normal text-emerald-600">
                      补录 {totalSupplementsWritten} 条
                    </span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">凭证数量</dt>
                <dd className="mt-0.5 font-semibold text-brand-950">{applyResults.length} 份</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">录入图片</dt>
                <dd className="mt-0.5 font-semibold text-brand-950">{applyResults.length} 张</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">状态</dt>
                <dd className="mt-0.5 font-semibold text-emerald-600">已完成</dd>
              </div>
            </dl>
            <div className="mt-5 flex items-center gap-3">
              <button
                onClick={resetAll}
                className="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                <RefreshCw size={16} />
                重新开始（录入下一批）
              </button>
            </div>
          </div>

          {/* 批次结算结果（ADR-0029 D9）：写入成功 ≠ 这一批结算通过。
              差额未处理/构成建不起来时，这里给出可执行的那一步——只重试结算，绝不重发写入 */}
          {finalizeState && (
            <div
              className={`rounded-xl border p-4 ${
                finalizeState.status === 'ok'
                  ? 'border-emerald-200 bg-emerald-50'
                  : 'border-amber-200 bg-amber-50'
              }`}
            >
              <p
                className={`flex items-center gap-2 text-sm font-medium ${
                  finalizeState.status === 'ok' ? 'text-emerald-900' : 'text-amber-900'
                }`}
              >
                {finalizeState.status === 'ok' ? (
                  <CheckCircle2 size={16} className="shrink-0 text-emerald-600" aria-hidden="true" />
                ) : (
                  <AlertTriangle size={16} className="shrink-0 text-amber-500" aria-hidden="true" />
                )}
                {finalizeState.status === 'ok'
                  ? `批次已结算${finalizeState.batchId != null ? `（#${finalizeState.batchId}）` : ''}`
                  : '写入已完成，但这一批没有结算'}
              </p>
              {finalizeState.status === 'ok' ? (
                <p className="mt-1 text-xs text-emerald-700">
                  核对记录与构成关系已落库：汇总项从下一批起不计入合计，子项照常计入
                </p>
              ) : (
                <>
                  <p className="mt-1 text-xs leading-relaxed text-amber-800">{finalizeState.message}</p>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={retryFinalize}
                      disabled={applyPending}
                      className="inline-flex min-h-[44px] items-center gap-1 rounded-lg border border-amber-300 bg-white px-2.5 text-xs font-medium text-amber-800 transition-colors hover:border-amber-400 hover:text-amber-900 disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-[32px]"
                    >
                      {applyPending ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                      重试结算
                    </button>
                    <span className="text-meta text-amber-700">
                      只重发结算，不会重复写入截图数据
                    </span>
                  </div>
                </>
              )}
            </div>
          )}

          {/* 部分图片被身份裁决拦下：成功卡不能掩盖「这几张根本没写」 */}
          {applyIdentityBlocked.length > 0 && (
            <div className="bg-amber-50 rounded-xl border border-amber-200 p-4">
              <p className="flex items-center gap-2 text-sm font-medium text-amber-900">
                <AlertTriangle size={16} className="shrink-0 text-amber-500" />
                有 {applyIdentityBlocked.length} 张图片未写入：还有行需要确认身份
              </p>
              <ul className="mt-2 space-y-1 text-xs text-amber-800">
                {applyIdentityBlocked.map((x, i) => (
                  <li key={i} className="break-words">
                    「{x.filename}」还有 {x.count} 行未确认（疑似改名/拆分）
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-meta text-amber-700">
                已写入的图片不受影响，请勿重复写入；未写入的图片需重新录入并在清单里完成身份确认。
              </p>
            </div>
          )}

          {/* 补录被后端丢弃的项必须可见（契约 §6）：说明原因，别让人以为补录悄悄少了 */}
          {totalSupplementsDropped.length > 0 && (
            <div className="bg-amber-50 rounded-xl border border-amber-200 p-4">
              <p className="flex items-center gap-2 text-sm font-medium text-amber-900">
                <AlertTriangle size={16} className="shrink-0 text-amber-500" />
                补录未写入 {totalSupplementsDropped.length} 项（已跳过，OCR 行不受影响）
              </p>
              <ul className="mt-2 space-y-1 text-xs text-amber-800">
                {totalSupplementsDropped.map((d, i) => (
                  <li key={i} className="flex items-baseline gap-1.5">
                    <span className="font-medium whitespace-nowrap">「{d.name}」</span>
                    <span>{supplementDropText(d.reason)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-brand-950 flex items-center gap-2">
              <ShieldCheck size={16} className="text-brand-600" />
              凭证信息（按图）
            </h3>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {applyResults.map((r) => (
                <div key={r.evidence_id} className="space-y-2">
                  <p className="text-xs text-slate-500">
                    凭证 #{r.evidence_id} · {r.data_date} · 写入 {r.rows_written} 条
                    {(r.supplements_written ?? 0) > 0 && ` · 其中补录 ${r.supplements_written} 条`}
                    {(r.supplements_dropped?.length ?? 0) > 0 &&
                      ` · 补录跳过 ${r.supplements_dropped!.length} 项`}
                    {(r.conflicts?.length ?? 0) > 0 &&
                      ` · 冲突未写入 ${r.conflicts!.length} 项（同日已有有效记录，未静默覆盖）`}
                  </p>
                  {(r.conflicts?.length ?? 0) > 0 && (
                    <ul className="text-xs text-amber-700 list-disc pl-5">
                      {r.conflicts!.map((c) => (
                        <li key={`${c.asset_name}-${c.asset_date}`}>
                          {c.asset_name} · {c.asset_date}：如需改金额请对既有行走「更正」
                        </li>
                      ))}
                    </ul>
                  )}
                  <EvidencePanel evidenceId={r.evidence_id} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 实时原始 OCR 行弹窗：只服务识别中/原始行（整理结果在预览表，不在此复制）；
          页面级渲染——解析全部完成切到 review 后弹窗仍存活，内容定格可看完再关 */}
      <Modal
        open={linesFor !== null}
        onCancel={() => setLinesFor(null)}
        title={`实时识别结果 · ${linesImg?.file.name ?? ''}`}
        width={640}
        footer={<Button size="small" onClick={() => setLinesFor(null)}>关闭</Button>}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <p className="text-xs text-slate-500">
            已识别 <span className="font-semibold text-brand-950">{shownLinesCount}</span> 行
            {linesLive && shownLinesCount > 0 && <span className="text-muted"> · 随解析自动刷新</span>}
          </p>
          {!linesLive && linesImg && (
            <p className="text-xs text-amber-600">
              {linesImg.parseError
                ? `识别失败：${linesImg.parseError}`
                : '识别已完成，可在预览表查看整理结果'}
            </p>
          )}
        </div>
        <div
          ref={linesBoxRef}
          onScroll={(e) => {
            const box = e.currentTarget
            linesFollowRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48
          }}
          className="max-h-[60vh] overflow-y-auto rounded-lg border border-slate-100 bg-slate-50/40 px-3 py-2"
        >
          {shownLines.length === 0 ? (
            <p className="py-8 text-center text-xs text-muted">
              {linesLive ? '已识别 0 行，稍后再看' : '没有产生原始识别行'}
            </p>
          ) : (
            shownLines.map((ln, i) => (
              <div key={i}>
                {/* 片切换处细分隔线（c 为 0 基片号） */}
                {i > 0 && shownLines[i - 1].c !== ln.c && <div className="my-1.5 border-t border-slate-200/70" />}
                <div className="flex items-baseline gap-2 py-0.5">
                  <span className="shrink-0 w-8 text-meta font-mono text-slate-300">片{ln.c + 1}</span>
                  <span className="flex-1 min-w-0 text-meta font-mono text-slate-700 break-all">{ln.t}</span>
                  <span
                    className={`shrink-0 text-meta font-mono ${
                      ln.s < 0.6 ? 'text-red-500' : ln.s < 0.8 ? 'text-muted' : 'text-slate-500'
                    }`}
                    title="识别置信度"
                  >
                    {formatValue(ln.s, '')}
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </Modal>

      {/* 轻提示：规则保存反馈（成功 / 已存在 / 失败，自动消失，不阻断写入） */}
      {ruleNotice && (
        <div
          className={`fixed bottom-6 right-6 z-50 flex items-start gap-2 max-w-sm px-4 py-3 rounded-xl border shadow-card text-sm animate-slide-up ${
            ruleNotice.kind === 'success'
              ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
              : ruleNotice.kind === 'info'
                ? 'bg-brand-50 border-brand-200 text-brand-800'
                : 'bg-red-50 border-red-200 text-red-800'
          }`}
        >
          {ruleNotice.kind === 'success' ? (
            <CheckCircle2 size={16} className="shrink-0 text-emerald-500 mt-0.5" />
          ) : ruleNotice.kind === 'info' ? (
            <Info size={16} className="shrink-0 text-brand-500 mt-0.5" />
          ) : (
            <AlertTriangle size={16} className="shrink-0 text-red-500 mt-0.5" />
          )}
          <span>{ruleNotice.text}</span>
        </div>
      )}
    </PageContainer>
  )
}
