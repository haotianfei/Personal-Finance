'use client'

/**
 * 资产项聚焦面板（spec `.scratch/asset-record-edit/spec.md` D6 / 原型变体 C）。
 *
 * 结构承诺（不是样式偏好，别在重构里弄丢）：
 * 1. 抬头是**资产项**，不是记录；正文先摆「N 条历史 / 最新一期金额 / 分类」，
 *    再摆编辑器——用户先看到「这改的是一整项」，才看到输入框。
 * 2. 这里是记录页改元数据的**唯一**写入口，且只碰两样：项级备注
 *    （`PUT /api/asset-items/{id}/note`）与项级标签（`PUT /api/asset-items/{id}/tags`）。
 *    金额 / 侧别 / 日期仍走 write-once 更正（`POST /api/assets/{id}/correct`），本面板不提供。
 *    标签**名字**可在标签选择器里显式新建（spec `.scratch/tag-create-in-sheet/spec.md`）：
 *    只有用户点/回车那一行才 `POST /api/classification/tags`，新标签并进草稿、
 *    随本面板的「保存到资产项」一起落库——**绝不因输入而静默新建**（该 spec D8）。
 * 3. **不做多期金额相加**：同一资产项的多期记录是不同时点快照，把各期加总等于凭空造总额
 *    （spec D7）。面板里只有「最新一期」这一个金额，其余各期各自成行、各显示自己的值。
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, History, Layers, StickyNote, X } from 'lucide-react'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { formatDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { InlineError, LoadingState, Money } from '@/components/ui'
import { MAX_TAGS, MEMBER_ROLE_LABEL, TagPicker } from '@/components/classification'
import { useDialogA11y } from '@/components/layout/useDialogA11y'
import type { AssetRecord, TagItem } from '@/types'

/** 备注长度上限（Unicode 码点，**trim 后**计）：与后端 `schemas.py::NOTE_MAX` 同值（spec D14）。 */
export const NOTE_MAX = 500

/** 一次保存的结果：备注与标签是两个端点，任一半失败时另一半已落库，必须如实回传。 */
export interface AssetItemSheetSaveResult {
  itemId: number
  noteSaved: boolean
  /** 服务端回读值（trim 后；空即 null）。 */
  note: string | null
  tagsSaved: boolean
  tagIds: number[]
}

export interface AssetItemSheetProps {
  /** 打开哪一项；`null`＝关闭。项级身份全部取自这条记录（列表已回传 asset_item_id / display_name / 分类 / 标签）。 */
  record: AssetRecord | null
  /** 标签字典（记录页已有，面板不重复取数）。 */
  tags: readonly TagItem[]
  /**
   * 该项当前备注。`undefined`＝还没读到（面板会等它到再初始化输入框，
   * 避免「空输入框 + 已有备注」被当成「没有备注」直接覆盖）。
   */
  note: string | null | undefined
  /** 备注读回失败（审计日志查询挂了）：仍允许写，但要明说「保存会整条覆盖」。 */
  noteReadFailed?: boolean
  /** 资产项已归档：标签不可改（后端 409），备注仍可改（spec D3：备注不参与净值）。 */
  archived?: boolean
  onClose: () => void
  onSaved: (result: AssetItemSheetSaveResult) => void
}

// ---------------------------------------------------------------- 错误文案

/** 备注端点的错误码 → 人话（`NOTE_TOO_LONG` 带 details 的 max_length / length）。 */
function noteErrorText(error: unknown, length: number): string {
  if (error instanceof ApiError) {
    if (error.code === 'NOTE_TOO_LONG') {
      const max = Number(error.details?.max_length ?? NOTE_MAX)
      const actual = Number(error.details?.length ?? length)
      return actual > max
        ? `备注最多 ${max} 字，当前 ${actual} 字：请删掉 ${actual - max} 字再保存。`
        : `备注最多 ${max} 字。`
    }
    if (error.code === 'ASSET_ITEM_NOT_FOUND') {
      return '这个资产项不存在或已被删除，备注没有保存。'
    }
    return error.message || '备注保存失败，请重试。'
  }
  return error instanceof Error && error.message ? error.message : '备注保存失败，请重试。'
}

/** 标签端点的错误码 → 人话。 */
function tagErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'TAG_LIMIT_EXCEEDED') {
      return `单个资产项最多 ${MAX_TAGS} 个标签，请先取消一个再保存。`
    }
    if (error.code === 'ITEM_ARCHIVED') {
      return '这个资产项已归档，不能改标签（备注已按原样保存）。'
    }
    if (error.code === 'NOT_FOUND') return '有标签不存在或已被删除，标签没有保存。'
    return error.message || '标签保存失败，请重试。'
  }
  return error instanceof Error && error.message ? error.message : '标签保存失败，请重试。'
}

/** 影响面的日期串：多期时只给首末，免得提示条被十几期撑成三行。 */
function datesSummary(rows: readonly AssetRecord[]): string {
  if (rows.length === 0) return ''
  if (rows.length <= 4) return rows.map((row) => formatDate(row.asset_date)).join(' · ')
  return `${formatDate(rows[0].asset_date)} … ${formatDate(rows[rows.length - 1].asset_date)}`
}

// ---------------------------------------------------------------- 只读小件

/** 项级提示：改标签/备注＝改整个资产项。三个原型变体里唯一强制的共识，保留原话。 */
function ScopeCaution({ itemName, count, dates }: { itemName: string; count: number | null; dates: string }) {
  return (
    <div className="relative overflow-hidden rounded-control border border-status-warn/30 bg-status-warn-surface/70 py-2.5 pl-3.5 pr-3">
      <span className="absolute inset-y-0 left-0 w-[3px] bg-status-warn" aria-hidden />
      <p className="flex items-center gap-1.5 text-meta font-medium text-status-warn-text">
        <Layers size={14} aria-hidden />
        项级 · 属于资产项「{itemName}」
      </p>
      <p className="mt-1 text-meta leading-relaxed text-status-warn-text">
        {count === null ? (
          <>在这里改标签或备注，会把<strong>这个资产项的全部历史记录</strong>一起改掉。</>
        ) : (
          <>
            在这里改标签或备注，会把 <strong className="pf-num">{count}</strong> 条历史记录一起改掉
            {dates ? `（${dates}）` : ''}。
          </>
        )}
        金额、侧别、日期仍然只更正这一行。
      </p>
    </div>
  )
}

/** 行内标签回显（只读）：项级标签，同一项的所有行显示同一组。 */
function ReadonlyTags({ names }: { names: readonly string[] }) {
  if (names.length === 0) {
    return (
      <span className="inline-flex items-center rounded-pill border border-dashed border-line-strong px-2 text-micro text-muted">
        未打标签
      </span>
    )
  }
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1">
      {names.map((name) => (
        <span key={name} className="inline-flex items-center rounded-pill bg-subtle px-2 py-0.5 text-micro text-secondary">
          {name}
        </span>
      ))}
    </span>
  )
}

// ---------------------------------------------------------------- 备注编辑器

function NoteField({ value, onChange, over, hint }: {
  value: string
  onChange: (next: string) => void
  over: boolean
  hint: React.ReactNode
}) {
  const id = useId()
  const length = value.trim().length
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="text-meta font-medium text-secondary">备注（项级）</label>
        <span className={cn('pf-num text-micro', over ? 'font-medium text-status-critical-text' : 'text-muted')}>
          {length} / {NOTE_MAX}
        </span>
      </div>
      <textarea
        id={id}
        rows={3}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="如：2027-10 到期前不动；与配偶共管。"
        aria-invalid={over || undefined}
        className={cn(
          'mt-2 w-full resize-y rounded-control border bg-surface px-3 py-2 text-body text-ink placeholder:text-muted focus:outline-none focus:ring-2',
          over
            ? 'border-status-critical focus:ring-status-critical/40'
            : 'border-line-strong focus:ring-brand-400',
        )}
      />
      {over && (
        <p role="alert" className="mt-1.5 text-micro font-medium text-status-critical-text">
          已超出 {length - NOTE_MAX} 字：备注最多 {NOTE_MAX} 字，请删到 {NOTE_MAX} 字以内再保存。
        </p>
      )}
      <p className="mt-1.5 text-micro leading-relaxed text-muted">{hint}</p>
    </div>
  )
}

// ---------------------------------------------------------------- 面板

export function AssetItemSheet({
  record,
  tags,
  note,
  noteReadFailed = false,
  archived = false,
  onClose,
  onSaved,
}: AssetItemSheetProps) {
  const open = record !== null && record.asset_item_id != null
  const itemId = record?.asset_item_id ?? null

  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const close = useCallback(() => onCloseRef.current(), [])

  useDialogA11y({ open, onClose: close, containerRef: panelRef })

  const titleId = useId()
  const queryClient = useQueryClient()

  const [noteDraft, setNoteDraft] = useState('')
  const [tagDraft, setTagDraft] = useState<number[]>([])
  // 初始化的目标项：换项要重置，同项内不因父级重渲染把用户正在写的内容冲掉
  const [initializedFor, setInitializedFor] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savedNotice, setSavedNotice] = useState<string | null>(null)

  // ---- 历史记录（唯一派生来源：现成 GET /api/assets?item_id=X，含被更正行；spec D7）
  const historyQuery = useQuery({
    queryKey: ['assetItemHistory', itemId],
    enabled: open && itemId !== null,
    staleTime: 30_000,
    queryFn: () => api.getAssets({
      item_id: String(itemId),
      include_superseded: 'true',
      page_size: '500',
      sort_field: 'asset_date',
      sort_order: 'asc',
    }),
  })

  const historyRows = useMemo(() => {
    const rows = historyQuery.data?.items ?? []
    return [...rows].sort((a, b) => a.asset_date.localeCompare(b.asset_date) || a.id - b.id)
  }, [historyQuery.data])
  const historyCount = historyQuery.data ? historyQuery.data.total : null

  /** 最新一期＝**当前有效**行里 asset_date 最大者（被更正的行不参与，D7：只展示最新一期，绝不加总）。 */
  const latestActive = useMemo(() => {
    let latest: AssetRecord | null = null
    for (const row of historyRows) {
      if (row.superseded_by != null) continue
      if (!latest || row.asset_date >= latest.asset_date) latest = row
    }
    return latest
  }, [historyRows])

  // ---- 草稿初始化（备注可能要等审计读回，未读到就等，不能先当成空）
  useEffect(() => {
    if (itemId === null) {
      setInitializedFor(null)
      return
    }
    if (initializedFor === itemId) return
    if (note === undefined) return
    setNoteDraft(note ?? '')
    setTagDraft((record?.tags ?? []).map((tag) => tag.id))
    setError(null)
    setSavedNotice(null)
    setInitializedFor(itemId)
  }, [initializedFor, itemId, note, record])

  const noteLength = noteDraft.trim().length
  const noteOverLimit = noteLength > NOTE_MAX
  const initialNote = (note ?? '').trim()
  const noteDirty = noteDraft.trim() !== initialNote
  const initialTagIds = useMemo(() => (record?.tags ?? []).map((tag) => tag.id), [record])
  const tagsDirty = tagDraft.length !== initialTagIds.length
    || tagDraft.some((id) => !initialTagIds.includes(id))

  /**
   * 显式新建标签（spec `.scratch/tag-create-in-sheet/spec.md` D1/D2/D3/D6）。
   *
   * 只由 `TagPicker` 里用户**点/回车**那一行触发（D8：输入本身永不落库）。
   * - 只填名字，`kind` 固定 `'free'`（D2）；
   * - 本地字典已有精确同名 → 不发请求，直接复用（D3）；
   * - 后端 409（并发/陈旧缓存下别人先建了）→ 重取 `['tags']`、按精确名匹配复用，
   *   **不向用户抛错**（D3）；
   * - 新建**只并入草稿**：`tagDraft` 变了、`tagsDirty` 为真，仍等「保存到资产项」
   *   那一下才 `PUT /api/asset-items/{id}/tags`（D6：不造第二条保存路径）。
   *
   * 副作用（spec D6 已接受）：建完不保存 → 字典里留一个 0 引用的 `free` 标签，
   * 可在「设置 → 字典 → 标签」删掉。
   */
  const createTag = useCallback(async (name: string): Promise<TagItem> => {
    const wanted = name.trim()
    const local = tags.find((tag) => tag.name.trim() === wanted)
    if (local) return local
    try {
      const created = await api.createTag({ name: wanted, kind: 'free' })
      // 让候选列表立刻认得这个新标签（不重新拉也不影响：TagPicker 本地并集兜着）
      void queryClient.invalidateQueries({ queryKey: ['tags'] })
      return created
    } catch (createError) {
      if (createError instanceof ApiError && createError.status === 409) {
        await queryClient.invalidateQueries({ queryKey: ['tags'] })
        const fresh = await queryClient.fetchQuery({ queryKey: ['tags'], queryFn: api.getTags })
        const reused = fresh.items.find((tag) => tag.name.trim() === wanted)
        if (reused) return reused
      }
      throw createError
    }
  }, [queryClient, tags])

  const saveMutation = useMutation({
    mutationFn: async (): Promise<AssetItemSheetSaveResult> => {
      if (itemId === null) throw new Error('没有可保存的资产项')
      const result: AssetItemSheetSaveResult = {
        itemId,
        noteSaved: false,
        note: initialNote === '' ? null : (note ?? null),
        tagsSaved: false,
        tagIds: [...tagDraft],
      }
      // 备注先写：标签失败时（如 TAG_LIMIT_EXCEEDED）不能把已保存的备注回滚（spec §3）
      if (noteDirty) {
        const response = await api.updateAssetItemNote(
          itemId,
          { note: noteDraft.trim() === '' ? null : noteDraft },
          newIdemKey(),
        )
        result.noteSaved = true
        result.note = response.note
      }
      if (tagsDirty) {
        try {
          await api.setItemTags(itemId, tagDraft, newIdemKey())
          result.tagsSaved = true
        } catch (tagError) {
          // 半成品的真实状态要带回调用方：备注已落库，列表上的便签标记必须立刻出现
          throw Object.assign(new Error(tagErrorText(tagError)), { partial: result })
        }
      }
      return result
    },
    onSuccess: (result) => {
      setError(null)
      onSaved(result)
      onCloseRef.current()
    },
    onError: (mutationError) => {
      const partial = (mutationError as { partial?: AssetItemSheetSaveResult }).partial
      if (partial) {
        // 备注成了、标签没成：把已保存的部分交出去同步列表，面板留着让用户改标签
        onSaved(partial)
        setError(mutationError.message)
        return
      }
      setError(noteErrorText(mutationError, noteLength))
    },
  })

  if (!open) return null
  if (typeof document === 'undefined') return null

  const itemName = record.display_name || record.asset_name
  const tagNames = (record.tags ?? []).map((tag) => tag.name)
  const dirtyParts: string[] = []
  if (noteDirty) dirtyParts.push('备注')
  if (tagsDirty) dirtyParts.push('标签')
  const dirty = dirtyParts.length > 0
  const saving = saveMutation.isPending

  return createPortal(
    <div className="fixed inset-0 z-modal" role="presentation">
      <div className="absolute inset-0 animate-fade-in bg-black/40" onClick={close} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 flex w-full max-w-[520px] animate-slide-in-right flex-col border-l border-line bg-surface shadow-overlay focus:outline-none"
      >
        <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-micro font-medium uppercase tracking-[0.14em] text-brand-700">
              <Layers size={12} aria-hidden /> 资产项
            </p>
            <h2 id={titleId} className="mt-0.5 truncate text-h3 font-semibold text-ink" title={itemName}>
              {itemName}
            </h2>
            <p className="mt-1 text-micro text-muted">
              {historyCount === null ? (
                <>正在读取这一项的历史记录…</>
              ) : (
                <><span className="pf-num">{historyCount}</span> 条历史记录 · 这里改的是它们共用的元数据</>
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label="关闭资产项面板"
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-subtle hover:text-ink"
          >
            <X size={18} aria-hidden />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-4 py-4">
          <ScopeCaution itemName={itemName} count={historyCount} dates={datesSummary(historyRows)} />

          {/* 先摆事实，再摆编辑器：历史条数 / 最新一期（只此一个金额，不相加） / 分类 */}
          <section className="grid grid-cols-2 gap-2">
            <div className="rounded-control border border-line bg-subtle/60 px-3 py-2">
              <p className="text-micro text-muted">历史记录</p>
              <p className="pf-num mt-0.5 text-body-strong font-semibold text-ink">
                {historyCount === null ? '读取中…' : `${historyCount} 条`}
              </p>
            </div>
            <div className="rounded-control border border-line bg-subtle/60 px-3 py-2">
              <p className="text-micro text-muted">
                最新一期{latestActive ? `（${formatDate(latestActive.asset_date)}）` : ''}
              </p>
              <p className="mt-0.5">
                <Money value={latestActive?.amount ?? null} size="s" />
              </p>
            </div>
          </section>

          <section>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="flex items-center gap-1.5 text-meta font-medium text-secondary">
                <History size={13} aria-hidden /> 历史记录（同一资产项）
              </h3>
              {historyCount !== null && (
                <span className="inline-flex items-center gap-1 rounded-pill bg-brand-50 px-2 py-0.5 text-micro font-medium text-brand-700">
                  <History size={12} aria-hidden />
                  影响 <span className="pf-num">{historyCount}</span> 条历史
                </span>
              )}
            </div>

            {historyQuery.isLoading ? (
              <div className="mt-2">
                <LoadingState rows={3} label="正在读取这一项的历史记录" />
              </div>
            ) : historyQuery.isError ? (
              <div className="mt-2 space-y-2">
                <InlineError>
                  {historyQuery.error instanceof Error ? historyQuery.error.message : '历史记录读取失败'}
                </InlineError>
                <button
                  type="button"
                  onClick={() => { void historyQuery.refetch() }}
                  className="inline-flex min-h-[36px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink"
                >
                  重试
                </button>
              </div>
            ) : (
              <ol className="mt-2 divide-y divide-line rounded-control border border-line">
                {historyRows.map((row) => {
                  const isCurrent = latestActive?.id === row.id
                  const superseded = row.superseded_by != null
                  return (
                    <li key={row.id} className="flex items-center justify-between gap-2 px-3 py-2 text-meta">
                      <span className="flex min-w-0 items-center gap-2">
                        <span className="pf-num text-secondary">{formatDate(row.asset_date)}</span>
                        {isCurrent && (
                          <span className="rounded-pill bg-brand-50 px-1.5 text-micro font-medium text-brand-700">当前</span>
                        )}
                        {superseded && (
                          <span className="rounded-pill bg-subtle px-1.5 text-micro text-muted">已更正</span>
                        )}
                      </span>
                      <Money value={row.amount} size="xs" />
                    </li>
                  )
                })}
                {historyRows.length === 0 && (
                  <li className="px-3 py-3 text-meta text-muted">这一项还没有历史记录。</li>
                )}
              </ol>
            )}
            <p className="mt-1.5 text-micro leading-relaxed text-muted">
              每一期记录都指向同一个资产项；改下面的标签或备注，上面这些行一起变。各期金额是不同时点的快照，不相加。
            </p>
          </section>

          <section>
            <h3 className="text-meta font-medium text-secondary">分类（项级，只读）</h3>
            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2 rounded-control border border-line bg-surface px-3 py-2.5 text-meta">
              <div>
                <dt className="text-micro text-muted">机构</dt>
                <dd className="mt-0.5 truncate text-ink" title={record.institution_name ?? undefined}>
                  {record.institution_name ?? '—'}
                </dd>
              </div>
              <div>
                <dt className="text-micro text-muted">资产类别</dt>
                <dd className="mt-0.5 truncate text-ink" title={record.asset_class_path ?? undefined}>
                  {record.asset_class_name ?? '—'}
                </dd>
              </div>
              <div>
                <dt className="text-micro text-muted">流动性</dt>
                <dd className="mt-0.5 truncate text-ink">{record.liquidity_label ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-micro text-muted">归属</dt>
                <dd className="mt-0.5 truncate text-ink" title={record.owner_kind ? MEMBER_ROLE_LABEL[record.owner_kind] : undefined}>
                  {record.owner_name ?? '未标注'}
                </dd>
              </div>
            </dl>
          </section>

          <div className="flex items-center gap-2 text-micro text-muted">
            <span className="h-px flex-1 bg-line" aria-hidden />
            <span>以下按资产项保存</span>
            <span className="h-px flex-1 bg-line" aria-hidden />
          </div>

          <section className="relative rounded-card border border-brand-200 bg-brand-50/40 p-3 pl-4">
            <span className="absolute inset-y-0 left-0 w-[3px] rounded-l-card bg-brand-500" aria-hidden />
            <div className="space-y-3">
              <TagPicker
                tags={tags}
                value={tagDraft}
                onChange={setTagDraft}
                disabled={archived}
                limit={MAX_TAGS}
                onCreate={archived ? undefined : createTag}
                hint={archived
                  ? '已归档的资产项不能改标签。'
                  : '标签也用于金额聚合，多标签会重叠计数。输入新名字可新建，保存后生效。'}
              />
              <div>
                <p className="mb-1.5 text-meta font-medium text-secondary">当前标签（保存后同项全部历史一起变）</p>
                <ReadonlyTags names={tagNames} />
              </div>
              <NoteField
                value={noteDraft}
                onChange={setNoteDraft}
                over={noteOverLimit}
                hint={archived
                  ? '备注不参与净值，归档项也可以改；这一条备注由这个资产项的全部历史记录共享。'
                  : '项级备注：这个资产项的全部历史记录共享这一条（最多 500 字，含换行）。'}
              />
              {noteReadFailed && (
                <p className="flex items-start gap-1.5 text-micro text-status-warn-text">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden />
                  备注没能从服务端读回（操作日志查询失败）：这里保存会用输入框内容整条覆盖原备注。
                </p>
              )}
              {savedNotice && (
                <p role="status" className="text-micro text-status-good-text">{savedNotice}</p>
              )}
              {error && <InlineError>{error}</InlineError>}
            </div>
          </section>
        </div>

        <footer className="border-t border-line bg-surface px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <span className={cn('min-w-0 text-micro', dirty ? 'text-status-warn-text' : 'text-muted')}>
              {dirty ? `待保存：${dirtyParts.join(' · ')}` : '未改动'}
            </span>
            <div className="flex shrink-0 gap-2">
              <button
                type="button"
                onClick={close}
                disabled={saving}
                className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-4 text-body text-ink disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => { setError(null); setSavedNotice(null); saveMutation.mutate() }}
                disabled={!dirty || noteOverLimit || saving || initializedFor !== itemId}
                className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50"
              >
                {saving ? '保存中…' : '保存到资产项'}
              </button>
            </div>
          </div>
          <p className="mt-2 flex items-center gap-1.5 text-micro text-muted">
            <StickyNote size={12} className="shrink-0" aria-hidden />
            金额、侧别、日期不在本面板修改：它们仍走「编辑记录」的单行更正。
          </p>
        </footer>
      </div>
    </div>,
    document.body,
  )
}