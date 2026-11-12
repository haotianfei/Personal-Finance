'use client'

import { useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Loader2, Plus, Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TagItem } from '@/types'
import { MAX_TAGS } from './classification'
import { FieldFrame, triggerClass } from './FieldFrame'
import { FloatingPanel } from './FloatingPanel'

/**
 * 标签多选：上限 5 个（后端同一条限制）。
 *
 * 接近上限时把剩余名额说出来，而不是等保存时用 422 拒绝——
 * 让用户在点的当下就知道还能加几个。
 *
 * **新建标签**（spec `.scratch/tag-create-in-sheet/spec.md`）：
 * 传入 `onCreate` 后，搜索框有词、本地无精确同名、且没满额时，候选**首项**多一行
 * 「+ 新建标签「X」」。只有用户显式点它或回车才会调 `onCreate`——**输入本身永不落库**
 * （D8）：不因输入、不因失焦、不因搜索无结果自动创建，否则标签会退化成自由文本。
 * 精确同名（D3）与满额（D4）时不出该项；新建中禁用重复提交；失败只说一句人话（D7）。
 */

/** 标签名长度上限（Unicode 码点，**trim 后**计）。
 *  与设置页 `tags/page.tsx` 的 `maxLength: 20`、后端 `TagCreate.max_length=20` 同口径（D5）。 */
export const TAG_NAME_MAX = 20

export interface TagPickerProps {
  tags: readonly TagItem[]
  value: number[]
  onChange: (value: number[]) => void
  label?: React.ReactNode
  error?: string
  hint?: React.ReactNode
  disabled?: boolean
  limit?: number
  loading?: boolean
  /**
   * 显式新建的出口：返回创建（或复用）到的标签，由调用方决定是否落库。
   * 缺省＝不提供新建入口（如录入弹窗、已归档项）。
   */
  onCreate?: (name: string) => Promise<TagItem>
}

export function TagPicker({
  tags,
  value,
  onChange,
  label = '标签',
  error,
  hint,
  disabled = false,
  limit = MAX_TAGS,
  loading = false,
  onCreate,
}: TagPickerProps) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // 本次会话内新建的标签：父级还没重取 `['tags']` 时，它也要能显示、能勾选
  const [createdTags, setCreatedTags] = useState<TagItem[]>([])
  const triggerRef = useRef<HTMLButtonElement>(null)

  /** 字典 ∪ 本地新建：按 id 去重（父级重取后同一标签只出现一次）。 */
  const allTags = useMemo(() => {
    if (createdTags.length === 0) return tags
    const known = new Set(tags.map((tag) => tag.id))
    return [...tags, ...createdTags.filter((tag) => !known.has(tag.id))]
  }, [createdTags, tags])

  const byId = useMemo(() => new Map(allTags.map((tag) => [tag.id, tag])), [allTags])
  const selectedTags = useMemo(
    () => value.map((id) => byId.get(id)).filter((tag): tag is TagItem => Boolean(tag)),
    [byId, value],
  )
  const full = value.length >= limit

  const keyword = search.trim().toLowerCase()
  const nameDraft = search.trim()
  // D3：与现有标签**精确同名**（trim 后逐字节比较，与后端 `Tag.name` 唯一约束同口径）。
  // 不做大小写归一——归一需要归一列 + 迁移，本 spec 明确不做（§4）。
  const exactMatch = useMemo(
    () => (nameDraft === '' ? undefined : allTags.find((tag) => tag.name.trim() === nameDraft)),
    [allTags, nameDraft],
  )
  const visibleTags = useMemo(() => {
    const list = keyword ? allTags.filter((tag) => tag.name.toLowerCase().includes(keyword)) : allTags
    // 受控词表在前（规范维护的常用词更容易被选中），自由词在后
    return [...list].sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'controlled' ? -1 : 1
      return a.name.localeCompare(b.name, 'zh-CN')
    })
  }, [allTags, keyword])

  /** D1 三个条件：搜索词非空 + 本地无精确同名 + 未满额；外加「能新建」（有出口、未禁用、字典已读完）。 */
  const canCreate = Boolean(onCreate) && !disabled && !loading
    && nameDraft !== '' && exactMatch === undefined && !full

  const toggle = (tagId: number) => {
    if (value.includes(tagId)) {
      onChange(value.filter((id) => id !== tagId))
      return
    }
    if (full) return
    onChange([...value, tagId])
  }

  const remove = (tagId: number) => onChange(value.filter((id) => id !== tagId))

  /** 选中一个标签（不 toggle：新建/复用路径是「加上」，不是「切换」）。 */
  const select = (tagId: number) => {
    if (value.includes(tagId) || full) return
    onChange([...value, tagId])
  }

  /**
   * 回车（或点「新建」那一行）：搜索框里的词就是候选首项的动作对象。
   *
   * 三条分支都不碰网络以外的隐式行为：
   * - 精确同名（D3）→ **不发 POST**，直接勾选 + 「已有同名标签」；
   * - 满额（D4）→ 什么都不做，不新建；
   * - 其余 → 显式调 `onCreate`（D1/D8），成功才勾选。
   */
  const submitSearch = () => {
    if (creating) return
    if (nameDraft === '') return
    if (nameDraft.length > TAG_NAME_MAX) {
      setNotice(null)
      setCreateError(`标签名最多 ${TAG_NAME_MAX} 字，请缩短后再新建。`)
      return
    }
    if (exactMatch) {
      select(exactMatch.id)
      setCreateError(null)
      setNotice(`已有同名标签「${exactMatch.name}」，不会新建。`)
      setSearch('')
      return
    }
    if (!onCreate || disabled || full) return

    setCreating(true)
    setCreateError(null)
    setNotice(null)
    onCreate(nameDraft)
      .then((created) => {
        setCreatedTags((previous) => (
          previous.some((tag) => tag.id === created.id) ? previous : [...previous, created]
        ))
        select(created.id)
        setNotice('已新建标签，保存后生效')
        setSearch('')
      })
      .catch(() => {
        // D7：行内一句话，保留用户输入、不改草稿
        setCreateError('新建标签失败，请重试')
      })
      .finally(() => setCreating(false))
  }

  const message = createError ?? notice ?? null

  return (
    <FieldFrame
      label={
        <span className="flex items-center gap-1.5">
          {label}
          <span className={cn('pf-num text-micro', full ? 'text-status-warn-text' : 'text-muted')}>
            {value.length}/{limit}
          </span>
        </span>
      }
      error={error}
      hint={
        full && !error
          ? `已选满 ${limit} 个，要换别的标签请先取消一个。`
          : hint
      }
    >
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((previous) => !previous)}
        aria-label="选择标签"
        aria-haspopup="dialog"
        aria-expanded={open}
        className={cn(
          triggerClass,
          'flex items-center gap-2 text-left',
          error && 'border-status-critical',
        )}
      >
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
          {selectedTags.length === 0 && <span className="text-muted">选择标签（可多选，最多 {limit} 个）</span>}
          {selectedTags.map((tag) => (
            <span key={tag.id} className="inline-flex items-center rounded-pill bg-subtle px-2 py-0.5 text-meta text-secondary">
              {tag.name}
            </span>
          ))}
        </span>
        <ChevronDown size={16} className={cn('shrink-0 text-muted transition-transform', open && 'rotate-180')} aria-hidden />
      </button>

      {/* 取消选择放在触发器外面：按钮里再嵌可点区域对键盘与读屏都不成立 */}
      {selectedTags.length > 0 && !disabled && (
        <ul className="flex flex-wrap gap-1 pt-1">
          {selectedTags.map((tag) => (
            <li key={tag.id}>
              <button
                type="button"
                onClick={() => remove(tag.id)}
                aria-label={`取消标签 ${tag.name}`}
                className="inline-flex items-center gap-1 rounded-pill border border-line px-2 py-0.5 text-micro text-secondary transition-colors hover:border-line-strong hover:text-ink"
              >
                {tag.name}
                <X size={12} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}

      <FloatingPanel open={open} anchorRef={triggerRef} onClose={() => { setOpen(false); setSearch(''); setCreateError(null); setNotice(null) }} label="选择标签">
        <div className="shrink-0 border-b border-line p-2">
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
            <input
              autoFocus
              type="text"
              value={search}
              maxLength={TAG_NAME_MAX}
              onChange={(event) => {
                setSearch(event.target.value)
                setCreateError(null)
                setNotice(null)
              }}
              onKeyDown={(event) => {
                if (event.key !== 'Enter') return
                // 中文输入法组字中的回车是「选字」，不是「新建标签」
                if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
                event.preventDefault()
                submitSearch()
              }}
              placeholder={onCreate ? '搜索标签，或输入新名字' : '搜索标签'}
              aria-label="搜索标签"
              className="min-h-[40px] w-full rounded-control border border-line bg-surface pl-7 pr-2 text-body text-ink placeholder:text-muted"
            />
          </div>
          {(message !== null || nameDraft !== '') && (
            <div className="mt-1 flex items-start justify-between gap-2 text-micro">
              <span className="min-w-0 flex-1 leading-relaxed">
                {createError ? (
                  <span role="alert" className="text-status-critical-text">{createError}</span>
                ) : notice ? (
                  <span role="status" className="text-status-good-text">{notice}</span>
                ) : exactMatch ? (
                  // D3 的可读性承诺：说清「为什么没有新建项」，并给下一步动作
                  <span className="text-muted">
                    已有同名标签「{exactMatch.name}」，回车直接勾选，不会新建。
                  </span>
                ) : null}
              </span>
              {nameDraft !== '' && (
                <span className={cn('pf-num shrink-0', nameDraft.length > TAG_NAME_MAX ? 'text-status-critical-text' : 'text-muted')}>
                  {nameDraft.length}/{TAG_NAME_MAX}
                </span>
              )}
            </div>
          )}
        </div>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-1.5">
          {loading && <li className="px-2 py-3 text-meta text-muted">正在加载标签…</li>}
          {/* D1：新建项固定排在候选**首项**，回车＝选它 */}
          {!loading && canCreate && (
            <li>
              <button
                type="button"
                onClick={submitSearch}
                disabled={creating}
                aria-label={`新建标签 ${nameDraft}`}
                className={cn(
                  'flex w-full items-center gap-2 rounded-control px-2 py-2 text-left text-body',
                  creating ? 'cursor-progress text-muted' : 'text-ink hover:bg-subtle',
                )}
              >
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-dashed border-line-strong text-brand-700">
                  {creating ? <Loader2 size={11} className="animate-spin" aria-hidden /> : <Plus size={11} aria-hidden />}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {creating
                    ? `正在新建「${nameDraft}」…`
                    : <>新建标签「<span className="font-medium">{nameDraft}</span>」</>}
                </span>
                <span className="shrink-0 text-micro text-muted">回车</span>
              </button>
            </li>
          )}
          {!loading && visibleTags.length === 0 && !canCreate && (
            <li className="px-2 py-3 text-meta text-muted">
              {allTags.length === 0
                ? (onCreate
                  ? '还没有标签：输入名字后回车就能新建。'
                  : '还没有标签。可以到「设置 → 字典 → 标签」新建。')
                : '没有匹配的标签，换个词试试。'}
            </li>
          )}
          {!loading && visibleTags.map((tag) => {
            const checked = value.includes(tag.id)
            const blocked = !checked && full
            return (
              <li key={tag.id}>
                <button
                  type="button"
                  onClick={() => toggle(tag.id)}
                  disabled={blocked}
                  aria-pressed={checked}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-control px-2 py-2 text-left text-body',
                    blocked ? 'cursor-not-allowed text-disabled' : 'text-ink hover:bg-subtle',
                  )}
                >
                  <span
                    className={cn(
                      'flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                      checked ? 'border-brand-600 bg-brand-600 text-white' : 'border-line-strong',
                    )}
                  >
                    {checked ? <Check size={12} aria-hidden /> : null}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{tag.name}</span>
                  <span className="shrink-0 text-micro text-muted">
                    {tag.kind === 'controlled' ? '常用' : '自定义'}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
        <div className="flex shrink-0 items-center justify-between border-t border-line px-2 py-1.5 text-micro text-muted">
          <span>已选 <span className="pf-num text-secondary">{value.length}</span> / {limit}</span>
          <button type="button" onClick={() => setOpen(false)} className="text-link">完成</button>
        </div>
      </FloatingPanel>
    </FieldFrame>
  )
}