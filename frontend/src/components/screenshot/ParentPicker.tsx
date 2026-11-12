'use client'

import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Search, X } from 'lucide-react'
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog'
import { formatCurrency } from '@/lib/format'
import type { ParentCandidate } from '@/lib/screenshotAggregate'

/**
 * 「归到…」父项选择器（ADR-0029 D12 主路径；票 05 D4 / 票 09 D2）。
 *
 * 为什么它是**主路径**而不是拖拽的补充：WCAG 2.2 SC 2.5.7 要求「单指、无需拖拽」
 * 也能完成同一操作（理解文档 F108 明确：键盘等价不算满足）。因此这个弹层必须
 * 全键盘可用——`/` 聚焦搜索、↑↓ 选择、Enter 确认、Esc 关闭并回到触发按钮。
 *
 * 候选两类：本批的行（排除自身与后代；选中后该行自动成为汇总行）、可手选的既有资产项
 * （只列本次预览读到现值的那些，见 `buildParentCandidates` 的注释）。
 *
 * 后果文案固定为**叶子计量口径**，不得写反：父项不计入合计，本行（子项）照常计入。
 */
export function ParentPicker({
  open,
  rowName,
  candidates,
  writeTotalLabel,
  onPick,
  onClose,
}: {
  open: boolean
  rowName: string
  candidates: ParentCandidate[]
  /** 当前「本批写入合计」的展示值（归父不改变它，写出来才不让人怀疑动过账） */
  writeTotalLabel: string
  onPick: (candidate: ParentCandidate) => void
  onClose: () => void
}) {
  const [keyword, setKeyword] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  // 每次打开都回到初始态：上一次的关键词留在框里，会让人以为候选变少了
  useEffect(() => {
    if (!open) return
    setKeyword('')
    setActive(0)
  }, [open])

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return candidates
    return candidates.filter((c) => c.name.toLowerCase().includes(kw))
  }, [candidates, keyword])

  // 候选变少（搜索）时把高亮夹回范围内，否则 Enter 会落到不存在的项上
  useEffect(() => {
    setActive((prev) => (filtered.length === 0 ? 0 : Math.min(prev, filtered.length - 1)))
  }, [filtered.length])

  // `/` 随时把焦点送回搜索框（原型行为；输入框已聚焦时不抢，否则打不出斜杠）
  useEffect(() => {
    if (!open) return
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== '/') return
      const el = inputRef.current
      if (!el || document.activeElement === el) return
      event.preventDefault()
      el.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // 高亮项滚进视野：列表比可视区高时，键盘选择不能选到看不见的地方
  useEffect(() => {
    if (!open) return
    const host = listRef.current
    const el = host?.querySelector<HTMLElement>(`[data-option-index="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  const target = filtered[active] ?? null
  const noCandidateAtAll = candidates.length === 0

  const pick = (c: ParentCandidate) => {
    onPick(c)
    onClose()
  }

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    const n = filtered.length
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (n === 0) return
      setActive((prev) => (prev + (event.key === 'ArrowDown' ? 1 : n - 1)) % n)
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      if (target) pick(target)
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
    }
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="归到父项"
      description={`本行：${rowName || '未命名'}`}
      size="md"
      initialFocusRef={inputRef}
      bodyClassName="!p-0"
      footer={
        <div className="space-y-1.5 text-meta leading-relaxed text-muted">
          {target && (
            <p className="text-xs leading-relaxed text-ink">
              归到「{target.name}」后：<b className="font-semibold">{target.name} 不计入合计</b>，
              本行（子项）照常计入；本批写入合计不变（{writeTotalLabel}）。
              {target.kind === 'item' && '（这是既有资产项：本次结算后它才成为汇总项）'}
            </p>
          )}
          <p>
            键盘：<kbd className="rounded border border-line bg-subtle px-1 font-code text-[10px]">/</kbd> 聚焦搜索 ·{' '}
            <kbd className="rounded border border-line bg-subtle px-1 font-code text-[10px]">↑</kbd>
            <kbd className="rounded border border-line bg-subtle px-1 font-code text-[10px]">↓</kbd> 选择 ·{' '}
            <kbd className="rounded border border-line bg-subtle px-1 font-code text-[10px]">Enter</kbd> 确认 ·{' '}
            <kbd className="rounded border border-line bg-subtle px-1 font-code text-[10px]">Esc</kbd> 关闭。
            拖行首手柄是同一件事的快捷方式。
          </p>
        </div>
      }
    >
      <div className="border-b border-line px-4 py-2.5">
        <div className="flex items-center gap-2 rounded-control border border-line px-2.5 focus-within:border-brand-400 focus-within:ring-1 focus-within:ring-brand-400">
          <Search size={14} className="shrink-0 text-muted" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value)
              setActive(0)
            }}
            onKeyDown={onInputKeyDown}
            placeholder="搜索本批行或既有资产项名称"
            autoComplete="off"
            role="combobox"
            aria-expanded="true"
            aria-controls="parent-picker-list"
            aria-autocomplete="list"
            aria-activedescendant={target ? `parent-opt-${target.id}` : undefined}
            className="min-h-[44px] w-full bg-transparent text-base text-ink placeholder:text-disabled focus:outline-none tablet:min-h-[36px] tablet:text-sm"
          />
          {keyword && (
            <button
              type="button"
              onClick={() => setKeyword('')}
              aria-label="清空搜索"
              className="shrink-0 rounded p-1 text-muted transition-colors hover:text-ink"
            >
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      <ul ref={listRef} id="parent-picker-list" role="listbox" aria-label="可归入的父项">
        {filtered.length === 0 && (
          <li className="px-4 py-6 text-sm text-muted" role="presentation">
            {noCandidateAtAll
              ? '本批没有可归入的行：自己和自己的后代不能作父项。'
              : '没有匹配的名称，换个关键词。'}
          </li>
        )}
        {filtered.map((c, i) => (
          // 选项本身不可聚焦（APG combobox：焦点始终在搜索框，用 aria-activedescendant 指向这里）
          <li
            key={c.id}
            id={`parent-opt-${c.id}`}
            role="option"
            aria-selected={i === active}
            data-option-index={i}
            onMouseEnter={() => setActive(i)}
            onClick={() => pick(c)}
            className={`flex min-h-[44px] cursor-pointer items-center gap-3 border-t border-line/60 px-4 py-2 text-left transition-colors first:border-t-0 ${
              i === active ? 'bg-brand-50' : ''
            }`}
          >
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm text-ink">{c.name}</span>
              <span className="block truncate text-meta text-muted">{c.meta}</span>
            </span>
            <span className="shrink-0 font-code text-xs text-secondary">
              {c.amount === null ? '金额未知' : formatCurrency(c.amount)}
            </span>
          </li>
        ))}
      </ul>
    </ResponsiveDialog>
  )
}