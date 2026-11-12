'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import dynamic from 'next/dynamic'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DatePicker, message } from 'antd'
import dayjs from 'dayjs'
import Decimal from 'decimal.js'
import { ArrowUpRight, Check, Fingerprint, History, Info, Pencil, RefreshCw, Search, StickyNote, Tags } from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { EMPTY_VALUE, formatDate, formatDays, todayDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { DataTable, actionsWidth, Money } from '@/components/ui'
import type { DataTableColumn, DataTableSort } from '@/components/ui'
import { ItemHistoryDrawer } from '@/components/audit/ItemHistoryDrawer'
import { dimsText, requiredMissing, snapshotFromCurrentValue } from '@/components/classification'
import type { ClassificationSnapshot, RequiredDim } from '@/components/classification'
import type {
  CurrentValueEditMode,
  CurrentValueItem,
  CurrentValueResponse,
  CurrentValueUpdateRequest,
} from '@/types'

const LazyItemIdentityDialog = dynamic(
  () => import('@/components/records/lineage/ItemIdentityDialog').then((m) => m.ItemIdentityDialog),
  { ssr: false },
)

/**
 * 当前值（spec §3/§4）：每资产项一行＝截至参考日的最新有效记录。
 *
 * - 默认「今天」：不传 as_of，由后端决定（避免客户端时区把今天算成昨天）；
 * - 就地编辑：点金额→输入→回车/失焦提交，无弹窗、无确认；
 * - 历史视图（is_latest=false）：更正「眼前这一行」自己日期的记录（PUT 的 as_of 用行
 *   的 asset_date，不用视图参考日），未录入的项只读；
 * - 保存后行内保留数秒「已保存 · 撤销」，撤销只复用现有 DELETE / correct 端点。
 */

/** 距今超过该天数＝陈旧（与后端 `valuation_stale` 同阈值：90 天）。 */
const STALE_DAYS = 90
/** 行内「撤销」保留时长。 */
const UNDO_WINDOW_MS = 6000

/** 陈旧三态由后端事实派生：无记录 / 陈旧 / 最新（不再有独立的 freshness 字段）。 */
type StalenessView = 'missing' | 'stale' | 'fresh'

const STALENESS_LABEL: Record<StalenessView, string> = {
  fresh: '最新',
  stale: '待更新',
  missing: '未录入',
}

const STALENESS_CLASS: Record<StalenessView, string> = {
  // 弱提示：陈旧用琥珀底，不用红色警报
  fresh: 'bg-subtle text-secondary',
  stale: 'bg-status-warn-surface text-status-warn-text',
  missing: 'bg-subtle text-muted',
}

const STALENESS_RANK: Record<StalenessView, number> = { fresh: 0, stale: 1, missing: 2 }

function stalenessOf(item: CurrentValueItem): StalenessView {
  if (item.record_id === null || item.valuation_age_days === null) return 'missing'
  return item.valuation_stale ? 'stale' : 'fresh'
}

/** 该项分类还缺哪几个必填维度（draft 的判定口径与后端一致）。 */
function requiredDimsOf(item: CurrentValueItem): RequiredDim[] {
  return requiredMissing(item.classification_missing)
}

/** 输入过程允许的半成品（空串、`12.`、`.5` 都先放行，提交时再校验）。 */
const AMOUNT_INPUT_PATTERN = /^\d*\.?\d{0,2}$/
/** 可提交的金额：非负、最多两位小数。 */
const AMOUNT_VALUE_PATTERN = /^\d+(\.\d{1,2})?$/

function compareNullableNumber(a: number | null, b: number | null, dir: number): number {
  // null（未录入）恒定排在最后，不随升降序翻转
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  return (a - b) * dir
}

function compareNullableText(a: string | null, b: string | null, dir: number): number {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  return a.localeCompare(b, 'zh-CN') * dir
}

function sortItems(items: CurrentValueItem[], sort: DataTableSort): CurrentValueItem[] {
  const dir = sort.direction === 'asc' ? 1 : -1
  return [...items].sort((a, b) => {
    let result = 0
    switch (sort.key) {
      case 'amount':
        result = compareNullableNumber(
          a.amount === null ? null : Number(a.amount),
          b.amount === null ? null : Number(b.amount),
          dir,
        )
        break
      case 'asset_date':
        result = compareNullableText(a.asset_date, b.asset_date, dir)
        break
      case 'valuation_age_days':
        result = compareNullableNumber(a.valuation_age_days, b.valuation_age_days, dir)
        break
      case 'status':
        result = (STALENESS_RANK[stalenessOf(a)] - STALENESS_RANK[stalenessOf(b)]) * dir
        break
      case 'institution_name':
        result = compareNullableText(a.institution_name, b.institution_name, dir)
        break
      case 'display_name':
      default:
        result = a.display_name.localeCompare(b.display_name, 'zh-CN') * dir
        break
    }
    if (result !== 0) return result
    // 同值稳定排序：按显示名升序
    return a.display_name.localeCompare(b.display_name, 'zh-CN')
  })
}

type EditNoticeTone = 'muted' | 'error'

interface EditNotice {
  text: string
  /** muted＝正常边界（如历史视图当日无记录）；error＝需要用户处理。 */
  tone: EditNoticeTone
}

/** 422 `details.fields` 里 amount 字段的说明（后端给的是「须 >= 0」这类短语）。 */
function amountFieldHint(error: ApiError): string | null {
  const fields = error.details?.fields
  if (!Array.isArray(fields)) return null
  for (const entry of fields) {
    if (typeof entry !== 'object' || entry === null) continue
    const field = (entry as { field?: unknown }).field
    const message = (entry as { message?: unknown }).message
    if (field === 'amount' && typeof message === 'string') return `金额${message}`
  }
  return null
}

function currentEditNotice(error: unknown): EditNotice {
  if (error instanceof ApiError) {
    const code = error.code.toUpperCase()
    // 历史视图里「当日无记录」是正常边界，不是故障：弱提示，不报警
    if (code === 'NO_RECORD_FOR_DATE') {
      return { tone: 'muted', text: '该日期无记录，历史视图只能更正已有值' }
    }
    if (code === 'ITEM_ARCHIVED') {
      return { tone: 'muted', text: '资产项已归档，不能修改金额' }
    }
    if (code === 'VALIDATION_ERROR') {
      return { tone: 'error', text: amountFieldHint(error) ?? (error.message || '金额不合法，请检查后重试') }
    }
    if (code === 'SUPERSEDED') return { tone: 'error', text: '该记录已被更正，请刷新后重试' }
    if (code === 'NOT_FOUND') return { tone: 'error', text: '资产项不存在，请刷新后重试' }
    return { tone: 'error', text: error.message || '保存失败，请重试' }
  }
  if (error instanceof Error && error.message) return { tone: 'error', text: error.message }
  return { tone: 'error', text: '保存失败，请重试' }
}

interface SaveNotice {
  itemId: number
  /** unchanged＝金额与现值相同，未写行也未写审计，没有可撤销的东西。 */
  kind: 'created' | 'corrected' | 'unchanged'
  /** 本次产生的有效记录（created＝新建行，corrected＝更正后的新行）；unchanged 为 null。 */
  recordId: number | null
  previousAmount: string | null
  /** 保存前的整行快照：撤销成功后立即回填，避免等刷新。 */
  before: CurrentValueItem
}

function AmountCell({
  item,
  editable,
  hint,
  onCommit,
}: {
  item: CurrentValueItem
  editable: boolean
  /** 不可编辑时的说明（原生 title，避免额外浮层）。 */
  hint: string
  onCommit: (item: CurrentValueItem, amount: string) => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [notice, setNotice] = useState<EditNotice | null>(null)
  // Esc / 空值取消后，紧随其后的 blur 不应再提交
  const cancelled = useRef(false)
  // Enter 与 blur 可能在同一拍触发：用 ref 兜底，状态来不及更新
  const submitting = useRef(false)

  const begin = () => {
    if (!editable || pending) return
    cancelled.current = false
    // 上次失败时保留输入，点回来即可在原值上修正
    setDraft(notice?.tone === 'error' && draft !== '' ? draft : (item.amount ?? ''))
    setNotice(null)
    setEditing(true)
  }

  const cancel = () => {
    cancelled.current = true
    setEditing(false)
    setNotice(null)
  }

  const commit = async () => {
    if (!editing || submitting.current) return
    if (cancelled.current) {
      cancelled.current = false
      setEditing(false)
      return
    }
    const raw = draft.trim().replace(/\.$/, '')
    setEditing(false)
    if (raw === '') {
      // 清空＝放弃这次编辑，静默还原
      setNotice(null)
      return
    }
    if (!AMOUNT_VALUE_PATTERN.test(raw)) {
      setNotice({ tone: 'error', text: '金额格式不对，已还原' })
      return
    }
    const normalized = new Decimal(raw).toFixed(2)
    if (item.amount !== null) {
      try {
        if (new Decimal(item.amount).eq(normalized)) return
      } catch {
        // 旧值不可解析时不拦截，交给后端校验
      }
    }
    submitting.current = true
    setPending(true)
    try {
      await onCommit(item, normalized)
      setNotice(null)
    } catch (err) {
      // 失败一律还原显示值（不写缓存）；原因贴着金额就地显示，不用 toast
      setNotice(currentEditNotice(err))
    } finally {
      submitting.current = false
      setPending(false)
    }
  }

  if (editing) {
    return (
      <input
        autoFocus
        type="text"
        inputMode="decimal"
        enterKeyHint="done"
        value={draft}
        onChange={(event) => {
          const next = event.target.value.replace(/[,，\s¥￥]/g, '')
          if (AMOUNT_INPUT_PATTERN.test(next)) setDraft(next)
        }}
        onFocus={(event) => event.currentTarget.select()}
        onBlur={() => { void commit() }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            void commit()
          } else if (event.key === 'Escape') {
            event.preventDefault()
            cancel()
          }
        }}
        aria-label={`「${item.display_name}」金额（元）`}
        placeholder="0.00"
        className="pf-num inline-block min-h-[44px] w-[118px] max-w-full rounded-control border border-line-strong bg-surface px-2 text-right text-money-s text-ink focus:border-action-primary focus:outline-none focus:ring-1 focus:ring-[var(--pf-color-focus)] md:min-h-[36px]"
      />
    )
  }

  return (
    <span className="inline-flex flex-col items-end gap-0.5">
      <button
        type="button"
        onClick={begin}
        disabled={!editable}
        title={hint}
        aria-label={editable ? `${item.display_name}：点击修改金额` : `${item.display_name}：${hint}`}
        className={cn(
          'group inline-flex min-h-[44px] items-center justify-end gap-1.5 rounded-control px-2 md:min-h-[36px]',
          editable
            ? 'cursor-text transition-colors hover:bg-subtle focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]'
            : 'cursor-default',
          pending && 'opacity-60',
        )}
      >
        <Money value={item.amount} unit="cny" size="s" />
        {editable && (
          <Pencil
            size={12}
            aria-hidden
            className="shrink-0 text-muted opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-visible:opacity-100"
          />
        )}
      </button>
      {notice && (
        <span
          role={notice.tone === 'error' ? 'alert' : 'status'}
          className={cn(
            'max-w-[200px] text-right text-micro leading-4',
            notice.tone === 'error' ? 'text-status-critical-text' : 'text-muted',
          )}
        >
          {notice.text}
        </span>
      )}
    </span>
  )
}

export interface CurrentValueTableProps {
  className?: string
  /**
   * 打开该项的分类编辑（分类挂在资产项上，七维一次设置、全历史共用）。
   * 不传则不出现「分类」入口——表格本身不承担分类写入。
   */
  onEditClassification?: (snapshot: ClassificationSnapshot) => void
  /**
   * 进入资产项面板（标签/备注的唯一写入口，`.scratch/current-value-note/spec.md` D4）。
   *
   * 只透传被点的那一项，抽屉状态与保存回执都留在页面（与记录明细视图同一个面板、
   * 同一份状态）——本表不复制抽屉，也不做备注的行内编辑。
   */
  onOpenItemSheet?: (item: CurrentValueItem) => void
}

export function CurrentValueTable({ className, onEditClassification, onOpenItemSheet }: CurrentValueTableProps) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [hideZero, setHideZero] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  // null＝取后端「今天」；只有用户主动选过历史日期才有值
  const [asOf, setAsOf] = useState<string | null>(null)
  const [sort, setSort] = useState<DataTableSort>({ key: 'amount', direction: 'desc' })
  const [noticeByItem, setNoticeByItem] = useState<Map<number, SaveNotice>>(() => new Map())
  // 行详情（身份 / 改名 / 合并 / 拆分）：按 item_id 记，刷新后从最新列表取行
  const [identityItemId, setIdentityItemId] = useState<number | null>(null)
  // 操作历史抽屉（票 02）：行级取证入口。只在打开时记住这一刻的显示名，
// 抽屉自己按 item_id 取数（列表因搜索/参考日变化时也不至于把抽屉关掉）。
  const [historyItem, setHistoryItem] = useState<{ item_id: number; display_name: string } | null>(null)

  const queryKey = useMemo(
    () => ['assetsCurrent', asOf ?? 'latest', includeArchived],
    [asOf, includeArchived],
  )

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey,
    queryFn: () => api.getCurrentValues({ as_of: asOf ?? undefined, include_archived: includeArchived }),
    staleTime: 30_000,
    // 端点未就绪时不要重试刷屏；错误交给列表的 ErrorState
    retry: false,
  })

  const isLatest = data?.is_latest ?? true
  /** 参考日：优先响应里的 as_of（后端「今天」以此为准）。 */
  const referenceDate = data?.as_of ?? asOf ?? ''

  /** 可编辑（不看 `side`：后端对从未录入的项恒返回 `side:"asset"`，无记录即无方向来源）。
   * - 今天视图：任意未归档项，含首次录入；
   * - 历史视图：只更正「眼前这一行」已有的记录；missing 没有行可更正，只读。 */
  const canEdit = useCallback(
    (item: CurrentValueItem) => {
      if (item.item_status === 'archived') return false
      if (isLatest) return true
      return item.record_id !== null && item.asset_date !== null
    },
    [isLatest],
  )

  const editHint = useCallback(
    (item: CurrentValueItem) => {
      if (item.item_status === 'archived') {
        return '已归档：不在此处修改，先在资产项管理中取消归档'
      }
      if (canEdit(item)) {
        return isLatest
          ? '点击修改金额，失焦或回车即保存'
          : `点击更正 ${item.asset_date ?? ''} 的金额（日期不变，不新增估值）`
      }
      if (item.record_id === null) {
        return `截至 ${referenceDate} 无记录，无法更正；回到今天可补录`
      }
      return '该行缺少估值日，无法更正；请回记录明细处理'
    },
    [canEdit, isLatest, referenceDate],
  )

  // --- 行内保存 ---------------------------------------------------------

  const patchItem = useCallback(
    (itemId: number, replace: (item: CurrentValueItem) => CurrentValueItem) => {
      queryClient.setQueryData<CurrentValueResponse>(queryKey, (previous) =>
        previous
          ? { ...previous, items: previous.items.map((row) => (row.item_id === itemId ? replace(row) : row)) }
          : previous,
      )
    },
    [queryClient, queryKey],
  )

  const refreshAll = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['assetsCurrent'] })
    // 当前值来自账本：明细列表（['assetsList']）与日期下拉随之失效
    void queryClient.invalidateQueries({ queryKey: ['assetsList'] })
    void queryClient.invalidateQueries({ queryKey: ['assets'] })
    void queryClient.invalidateQueries({ queryKey: ['dates'] })
  }, [queryClient])

  /**
   * 身份动作（改名 / 合并 / 拆分）后的失效面：本表行 + 明细 + 趋势。
   * 趋势只失效已挂载的查询（react-query 惰性重取），代价可忽略。
   */
  const refreshAfterLineage = useCallback(() => {
    refreshAll()
    void queryClient.invalidateQueries({ queryKey: ['assetItems'] })
    void queryClient.invalidateQueries({ queryKey: ['lineageByItem'] })
    void queryClient.invalidateQueries({ queryKey: ['lineageFamily'] })
    void queryClient.invalidateQueries({ queryKey: ['assetNames'] })
    void queryClient.invalidateQueries({ queryKey: ['summary'] })
    void queryClient.invalidateQueries({ queryKey: ['mixedChart'] })
    void queryClient.invalidateQueries({ queryKey: ['byItem'] })
    void queryClient.invalidateQueries({ queryKey: ['byItemFamily'] })
    void queryClient.invalidateQueries({ queryKey: ['byClass'] })
    void queryClient.invalidateQueries({ queryKey: ['byLiquidity'] })
    void queryClient.invalidateQueries({ queryKey: ['byInstitution'] })
  }, [queryClient, refreshAll])

  const noticeTimers = useRef(new Map<number, ReturnType<typeof setTimeout>>())

  const removeNotice = useCallback((itemId: number) => {
    const timer = noticeTimers.current.get(itemId)
    if (timer) {
      clearTimeout(timer)
      noticeTimers.current.delete(itemId)
    }
    setNoticeByItem((previous) => {
      if (!previous.has(itemId)) return previous
      const next = new Map(previous)
      next.delete(itemId)
      return next
    })
  }, [])

  const pushNotice = useCallback(
    (entry: SaveNotice) => {
      const previousTimer = noticeTimers.current.get(entry.itemId)
      if (previousTimer) clearTimeout(previousTimer)
      setNoticeByItem((previous) => new Map(previous).set(entry.itemId, entry))
      noticeTimers.current.set(
        entry.itemId,
        setTimeout(() => removeNotice(entry.itemId), UNDO_WINDOW_MS),
      )
    },
    [removeNotice],
  )

  useEffect(() => {
    const timers = noticeTimers.current
    return () => {
      timers.forEach((timer) => clearTimeout(timer))
      timers.clear()
    }
  }, [])

  // 换参考日/含归档后，旧的「撤销」不再对应眼前这张表
  useEffect(() => {
    setNoticeByItem((previous) => (previous.size > 0 ? new Map() : previous))
    noticeTimers.current.forEach((timer) => clearTimeout(timer))
    noticeTimers.current.clear()
  }, [asOf, includeArchived])

  const saveMutation = useMutation({
    mutationFn: (vars: {
      item: CurrentValueItem
      amount: string
      mode: CurrentValueEditMode
      /** 历史视图：这一行自己的估值日（后端按精确日期找行）；今天视图为 null。 */
      targetDate: string | null
      /** 视图参考日：只用于算「距今」，不写进请求。 */
      viewAsOf: string
      idemKey: string
    }) => {
      const body: CurrentValueUpdateRequest = {
        amount: vars.amount,
        mode: vars.mode,
      }
      if (vars.mode === 'correct_only') {
        // 必须用行自己的日期：PUT 按 asset_date == D 精确定位要更正的记录。
        // 传视图参考日会 409（行通常早于视图日，见 _active_row 语义）。
        if (!vars.targetDate) throw new Error('该行缺少估值日，无法更正')
        body.as_of = vars.targetDate
      }
      // 今天视图不传 as_of：交给后端取「今天」，避免客户端时区把今天算成昨天
      return api.updateCurrentValue(vars.item.item_id, body, vars.idemKey)
    },
    onSuccess: (result, vars) => {
      // unchanged：金额与现值相同，后端未写行、未写审计 —— 只提示「未变化」，不给撤销
      if (result.kind === 'unchanged') {
        pushNotice({
          itemId: vars.item.item_id,
          kind: 'unchanged',
          recordId: null,
          previousAmount: null,
          before: vars.item,
        })
        return
      }
      const ageDays = Math.max(0, dayjs(vars.viewAsOf).diff(dayjs(result.asset_date), 'day'))
      patchItem(vars.item.item_id, (row) => ({
        ...row,
        record_id: result.record_id,
        asset_date: result.asset_date,
        amount: result.amount,
        valuation_date: result.asset_date,
        valuation_age_days: ageDays,
        valuation_stale: ageDays > STALE_DAYS,
      }))
      pushNotice({
        itemId: vars.item.item_id,
        kind: result.kind,
        recordId: result.record_id,
        previousAmount: result.previous_amount,
        before: vars.item,
      })
      refreshAll()
    },
  })

  const commitAmount = useCallback(
    async (item: CurrentValueItem, amount: string) => {
      const mode: CurrentValueEditMode = isLatest ? 'auto' : 'correct_only'
      await saveMutation.mutateAsync({
        item,
        amount,
        mode,
        targetDate: mode === 'correct_only' ? item.asset_date : null,
        viewAsOf: referenceDate,
        idemKey: newIdemKey(),
      })
    },
    [saveMutation, isLatest, referenceDate],
  )

  // --- 撤销：只用现有端点（spec §3.13） --------------------------------

  const undoMutation = useMutation({
    mutationFn: async (entry: SaveNotice) => {
      if (entry.kind === 'unchanged' || entry.recordId === null) {
        throw new Error('这次保存没有产生记录，无需撤销')
      }
      if (entry.kind === 'created') {
        await api.deleteAsset(entry.recordId, newIdemKey())
        return
      }
      if (entry.previousAmount === null) throw new Error('没有可还原的金额')
      await api.correctAsset(entry.recordId, { amount: entry.previousAmount }, newIdemKey())
    },
  })

  const handleUndo = useCallback(
    (entry: SaveNotice) => {
      if (entry.kind === 'unchanged' || entry.recordId === null) {
        removeNotice(entry.itemId)
        return
      }
      undoMutation.mutate(entry, {
        onSuccess: () => {
          removeNotice(entry.itemId)
          patchItem(entry.itemId, () => entry.before)
          refreshAll()
        },
        onError: (undoError) => {
          // 撤销失败是真正的故障：这里用 toast，避免行内提示被下一次保存覆盖
          message.error(undoError instanceof Error ? undoError.message : '撤销失败，请重试')
        },
      })
    },
    [undoMutation, removeNotice, patchItem, refreshAll],
  )

  // --- 本地筛选 / 排序 --------------------------------------------------

  const items = useMemo(() => data?.items ?? [], [data])
  /** 合并候选沿用当前值表的排序：用户在表里看到的位置就是候选列表里的位置。 */
  const lineageCandidates = useMemo(() => sortItems(items, sort), [items, sort])
  const identityItem = useMemo(
    () => (identityItemId === null
      ? null
      : items.find((item) => item.item_id === identityItemId) ?? null),
    [items, identityItemId],
  )
  /** 生效日默认值：今天视图取后端 as_of（避免客户端时区差一天），历史视图取本地今天。 */
  const lineageToday = isLatest && referenceDate ? referenceDate : todayDate()
  /** 改关联的生效日预填：最近快照日（最长 asset_date）——补登记不静默用今天。 */
  const lastSnapshotDate = useMemo(() => {
    let latest: string | null = null
    for (const item of items) {
      if (item.asset_date && (!latest || item.asset_date > latest)) latest = item.asset_date
    }
    return latest ?? (referenceDate || todayDate())
  }, [items, referenceDate])
  const zeroCount = useMemo(
    () => items.filter((item) => item.amount !== null && Number(item.amount) === 0).length,
    [items],
  )

  const visibleItems = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    const filtered = items.filter((item) => {
      if (hideZero && item.amount !== null && Number(item.amount) === 0) return false
      if (!keyword) return true
      return (
        item.display_name.toLowerCase().includes(keyword) ||
        (item.institution_name ?? '').toLowerCase().includes(keyword) ||
        (item.channel_name ?? '').toLowerCase().includes(keyword)
      )
    })
    return sortItems(filtered, sort)
  }, [items, search, hideZero, sort])

  const filtered = search.trim() !== '' || hideZero

  // --- 列 ---------------------------------------------------------------

  const columns: DataTableColumn<CurrentValueItem>[] = [
    {
      key: 'display_name',
      header: '资产项',
      sortable: true,
      widthTier: 'text-mid',
      cell: (item) => (
        <div className="min-w-0">
          <span className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="min-w-0 font-medium text-ink" title={item.display_name}>
              {item.display_name}
            </span>
            {/* 项级备注只做「有备注」标记 + 悬停看全文（spec D3：不加表格列，也不做行内编辑）。
                呈现方式与记录明细视图逐字一致，两个视图的便签标记必须同形同义。 */}
            {item.note && (
              <span
                role="img"
                aria-label={`这个资产项有备注：${item.note}`}
                title={item.note}
                className="inline-flex shrink-0 items-center rounded-pill bg-status-warn-surface px-1 py-0.5 text-status-warn-text"
              >
                <StickyNote size={12} aria-hidden />
              </span>
            )}
            {item.item_status === 'archived' && (
              <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">已归档</span>
            )}
            {item.item_status === 'draft' && (
              <span
                className="shrink-0 rounded-pill bg-status-warn-surface px-1.5 py-0.5 text-micro font-medium text-status-warn-text"
                title={`分类未补齐：还差${dimsText(requiredDimsOf(item))}；补齐后才进分类统计与导出`}
              >
                待补分类
              </span>
            )}
          </span>
          {item.tags.length > 0 && (
            <span
              className="mt-1 flex flex-wrap gap-1"
              title={item.tags.map((tag) => tag.name).join('、')}
            >
              {item.tags.slice(0, 2).map((tag) => (
                <span key={tag.id} className="rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-secondary">
                  {tag.name}
                </span>
              ))}
              {item.tags.length > 2 && (
                <span className="rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">
                  +{item.tags.length - 2}
                </span>
              )}
            </span>
          )}
        </div>
      ),
      mobile: { role: 'title' },
    },
    {
      key: 'institution_name',
      header: '机构 / 渠道',
      widthTier: 'text-mid',
      cell: (item) => {
        const label = [item.institution_name, item.channel_name].filter(Boolean).join(' / ')
        return (
          <span className="text-secondary" title={label || undefined}>
            {label || EMPTY_VALUE}
          </span>
        )
      },
      mobile: { role: 'meta', label: '机构' },
    },
    {
      key: 'side',
      header: '资产·负债',
      widthTier: 'text-short',
      cell: (item) => {
        // 无记录 ⇒ 后端固定返回 side:"asset"（没有方向来源），如实显示「—」而不是编造「资产」
        if (item.record_id === null) {
          return (
            <span className="whitespace-nowrap text-meta text-muted" title="尚未录入，暂无方向；首次录入时确定">
              {EMPTY_VALUE}
            </span>
          )
        }
        return item.side === 'liability' ? (
          <span
            className="inline-flex items-center rounded-pill border border-line bg-subtle px-1.5 py-px text-micro font-medium leading-none text-muted"
            title="负债：计入负债合计，不计入资产合计"
          >
            负债
          </span>
        ) : (
          <span className="whitespace-nowrap text-meta text-muted">资产</span>
        )
      },
      mobile: { role: 'meta', label: '资产 / 负债' },
    },
    {
      key: 'amount',
      header: '金额（元）',
      sortable: true,
      align: 'right',
      widthTier: 'amount-extra',
      cell: (item) => (
        <AmountCell
          item={item}
          editable={canEdit(item)}
          hint={editHint(item)}
          onCommit={commitAmount}
        />
      ),
      mobile: { role: 'value', label: '金额（元）' },
    },
    {
      key: 'asset_date',
      header: '估值日',
      sortable: true,
      widthTier: 'date',
      cell: (item) => (
        <span className="pf-num whitespace-nowrap text-meta text-secondary">
          {formatDate(item.asset_date)}
        </span>
      ),
      mobile: { role: 'meta', label: '估值日' },
    },
    {
      key: 'valuation_age_days',
      header: '距今',
      sortable: true,
      align: 'right',
      widthTier: 'count',
      cell: (item) =>
        item.valuation_age_days === null ? (
          <span className="pf-num text-meta text-muted">{EMPTY_VALUE}</span>
        ) : (
          <span
            className={cn(
              'pf-num whitespace-nowrap text-meta',
              item.valuation_stale ? 'font-medium text-status-warn-text' : 'text-secondary',
            )}
            title={item.valuation_stale ? `已超过 ${STALE_DAYS} 天未更新` : undefined}
          >
            {formatDays(item.valuation_age_days)}
          </span>
        ),
      mobile: { role: 'meta', label: '距今' },
    },
    {
      key: 'status',
      header: '状态',
      widthTier: 'label',
      cell: (item) => {
        const notice = noticeByItem.get(item.item_id)
        if (notice?.kind === 'unchanged') {
          // 与现值相同：没有写行、没有审计，因此不提供撤销
          return <span role="status" className="whitespace-nowrap text-meta text-muted">未变化</span>
        }
        if (notice && notice.recordId !== null) {
          const undoing = undoMutation.isPending && undoMutation.variables?.itemId === item.item_id
          return (
            <span role="status" className="inline-flex items-center gap-1 whitespace-nowrap text-meta">
              <Check size={13} aria-hidden className="text-status-good-text" />
              <span className="text-status-good-text">已保存</span>
              <span aria-hidden className="text-muted">·</span>
              <button
                type="button"
                onClick={() => handleUndo(notice)}
                disabled={undoMutation.isPending}
                className="inline-flex min-h-[44px] items-center rounded-control px-1 text-meta font-medium text-link underline-offset-2 hover:underline disabled:opacity-60 md:min-h-[32px]"
              >
                {undoing ? '撤销中…' : '撤销'}
              </button>
            </span>
          )
        }
        const staleness = stalenessOf(item)
        return (
          <span className={cn('whitespace-nowrap rounded-pill px-2 py-0.5 text-meta', STALENESS_CLASS[staleness])}>
            {STALENESS_LABEL[staleness]}
          </span>
        )
      },
      mobile: { role: 'meta', label: '状态', span: 2 },
    },
  ]

  // --- 工具栏 -----------------------------------------------------------

  const referencePicker = (
    <label className="inline-flex min-h-[44px] items-center gap-2 text-meta text-secondary">
      <span className="shrink-0 text-muted">参考日</span>
      <DatePicker
        value={asOf ? dayjs(asOf) : referenceDate ? dayjs(referenceDate) : null}
        onChange={(date) => setAsOf(date ? date.format('YYYY-MM-DD') : null)}
        format="YYYY-MM-DD"
        allowClear={false}
        size="large"
        placeholder="今天"
        // 未来日期没有「截至」语义：只允许今天与历史
        disabledDate={(current) => current.isAfter(dayjs(), 'day')}
        className="w-[136px]"
      />
    </label>
  )

  const toolbar = (
    <div className="space-y-2">
      {data && (
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <p className="text-meta text-secondary">
            截至 <span className="pf-num font-medium text-ink">{referenceDate}</span>
            {!isLatest && (
              <span className="ml-1.5 rounded-pill bg-status-warn-surface px-1.5 py-0.5 text-micro text-status-warn-text">
                历史
              </span>
            )}
            <span className="ml-1.5 text-muted">· 每项最新有效记录</span>
          </p>
          <span className="pf-num text-meta text-muted">
            共 {visibleItems.length} 项
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-[280px]">
          <Search
            size={16}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
            aria-hidden
          />
          <input
            type="text"
            inputMode="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索资产项、机构或渠道"
            aria-label="搜索资产项、机构或渠道"
            className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
          />
        </div>
        {referencePicker}
        {asOf !== null && (
          <button
            type="button"
            onClick={() => setAsOf(null)}
            className="inline-flex min-h-[44px] items-center rounded-control px-3 text-meta text-secondary"
          >
            回到今天
          </button>
        )}
        <label className="inline-flex min-h-[44px] items-center gap-2 px-1 text-meta text-secondary">
          <input
            type="checkbox"
            checked={hideZero}
            onChange={(event) => setHideZero(event.target.checked)}
            className="h-4 w-4 accent-[var(--pf-color-action-primary)]"
          />
          隐藏零值
          {zeroCount > 0 && <span className="pf-num text-muted">（{zeroCount}）</span>}
        </label>
        <label className="inline-flex min-h-[44px] items-center gap-2 px-1 text-meta text-secondary">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => setIncludeArchived(event.target.checked)}
            className="h-4 w-4 accent-[var(--pf-color-action-primary)]"
          />
          含归档
        </label>
        <button
          type="button"
          onClick={() => { void refetch() }}
          className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control px-3 text-meta text-secondary"
        >
          <RefreshCw size={14} aria-hidden className={cn(isFetching && 'animate-spin')} />
          {isFetching ? '刷新中' : '刷新'}
        </button>
      </div>
    </div>
  )

  return (
    <div className={cn('space-y-3', className)}>
      {!isLatest && (
        <div className="flex flex-wrap items-start justify-between gap-2 rounded-control border border-line bg-subtle px-3 py-2">
          <p className="flex min-w-0 items-start gap-2 text-meta text-secondary">
            <Info size={14} className="mt-0.5 shrink-0 text-muted" aria-hidden />
            <span>
              历史视图：只能更正已有记录的金额（更正不改变日期），没有记录的项不能补录。
              要记录今天的值，请回到今天。
            </span>
          </p>
          <button
            type="button"
            onClick={() => setAsOf(null)}
            className="inline-flex min-h-[36px] shrink-0 items-center rounded-control border border-line-strong bg-surface px-3 text-meta font-medium text-ink"
          >
            回到今天
          </button>
        </div>
      )}

      <DataTable<CurrentValueItem>
        label="资产当前值"
        rows={visibleItems}
        columns={columns}
        rowKey={(item) => item.item_id}
        loading={isLoading}
        error={error ?? null}
        onRetry={() => { void refetch() }}
        sort={sort}
        onSortChange={setSort}
        rowActions={(item) => (
          <>
            {/* 项级元数据的唯一写入口（spec D4）：本表对备注只读，写走现成抽屉 */}
            {onOpenItemSheet && (
              <button
                type="button"
                onClick={() => onOpenItemSheet(item)}
                aria-label={`进入资产项${item.display_name}，改这一项的标签与备注`}
                title="进入资产项：改这一项的标签与备注（影响它的全部历史记录）"
                className="inline-flex min-h-[44px] shrink-0 items-center gap-1 whitespace-nowrap rounded-control px-2 text-meta font-medium text-brand-700 transition-colors hover:bg-subtle focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]"
              >
                进入资产项
                <ArrowUpRight size={14} aria-hidden />
              </button>
            )}
            {onEditClassification && item.item_status !== 'archived' && (
              <button
                type="button"
                onClick={() => onEditClassification(snapshotFromCurrentValue(item))}
                aria-label={`设置「${item.display_name}」的分类`}
                title={requiredDimsOf(item).length > 0
                  ? `补齐分类：还差${dimsText(requiredDimsOf(item))}`
                  : '查看或修改分类'}
                className={cn(
                  'inline-flex min-h-[44px] items-center gap-1 rounded-control px-2 text-meta transition-colors hover:bg-subtle focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]',
                  item.item_status === 'draft' ? 'font-medium text-status-warn-text' : 'text-secondary hover:text-ink',
                )}
              >
                <Tags size={14} aria-hidden />
                {item.item_status === 'draft' ? '补分类' : '分类'}
              </button>
            )}
            <button
              type="button"
              onClick={() => setIdentityItemId(item.item_id)}
              aria-label={`「${item.display_name}」的身份与血缘`}
              title="身份与血缘：改名 / 合并 / 拆分 / 改关联"
              className="inline-flex min-h-[44px] items-center gap-1 rounded-control px-2 text-meta text-secondary transition-colors hover:bg-subtle hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]"
            >
              <Fingerprint size={14} aria-hidden />
              身份
            </button>
            {/* 票 02 J1：取证第一现场是「我看到这一行数不对」，所以入口挂在行上 */}
            <button
              type="button"
              onClick={() => setHistoryItem({ item_id: item.item_id, display_name: item.display_name })}
              aria-label={`「${item.display_name}」的操作历史`}
              title="操作历史：谁在何时把哪个字段从多少改成多少、依据哪张凭证"
              className="inline-flex min-h-[44px] items-center gap-1 whitespace-nowrap rounded-control px-2 text-meta text-secondary transition-colors hover:bg-subtle hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--pf-color-focus)]"
            >
              <History size={14} aria-hidden />
              操作历史
            </button>
          </>
        )}
        rowActionsWidth={actionsWidth([
          ...(onOpenItemSheet ? [{ label: '进入资产项', icon: true }] : []),
          ...(onEditClassification ? [{ label: '补分类', icon: true }] : []),
          { label: '身份', icon: true },
          { label: '操作历史', icon: true },
        ])}
        empty={{
          title: filtered ? '当前筛选无结果' : '暂无资产项',
          description: filtered
            ? '调整搜索词，或关闭「隐藏零值」后重试。'
            : '先在「记录明细」新增一条记录，或在资产项管理中建立资产项。',
        }}
        toolbar={toolbar}
      />

      {historyItem && (
        <ItemHistoryDrawer
          open
          itemId={historyItem.item_id}
          displayName={historyItem.display_name}
          onClose={() => setHistoryItem(null)}
        />
      )}

      {identityItem && (
        <LazyItemIdentityDialog
          key={identityItem.item_id}
          open
          item={identityItem}
          candidates={lineageCandidates}
          today={lineageToday}
          defaultEffectiveDate={lastSnapshotDate}
          onClose={() => setIdentityItemId(null)}
          onChanged={refreshAfterLineage}
          onSelectItem={(nextItemId) => {
            // 族里可能点到当前列表未加载的已归档项：先纳入已归档，再切行
            if (!items.some((row) => row.item_id === nextItemId)) setIncludeArchived(true)
            setIdentityItemId(nextItemId)
          }}
        />
      )}
    </div>
  )
}