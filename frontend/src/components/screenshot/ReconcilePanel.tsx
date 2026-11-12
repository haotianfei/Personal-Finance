'use client'

import { useState } from 'react'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, X } from 'lucide-react'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { formatCurrency, formatSignedCurrency } from '@/lib/format'
import type { ReconcileItem } from '@/lib/screenshotAggregate'

/**
 * 本批核对面板（ADR-0029 D10；票 04 D4 / 票 05 D6）。
 *
 * 口径写死在这里的理由：差额是**父项金额 − 直接子项合计**，零容差，三个出口
 * （改子项 / 补子项 / 显式接受）。面板只呈现与拦截，不替人做决定：
 * `diff ≠ 0` 就阻断写入，接受必须写理由（≤200 字，空白则确认按钮禁用），
 * 接受之后差额再变即自动作废——静默沿用过期结论是这类面板最容易犯的错。
 *
 * 形态：桌面常驻在清单末尾（吸底）；窄屏收成底部一条，点开是抽屉（票 05 D7）。
 */
export function ReconcilePanel({
  items,
  open,
  onToggle,
  onAccept,
  onWithdraw,
  onGoToChildren,
  onLocateParent,
  pending,
  acceptSeed,
}: {
  items: ReconcileItem[]
  open: boolean
  onToggle: () => void
  onAccept: (parentId: string, reason: string) => void
  onWithdraw: (parentId: string) => void
  onGoToChildren: (item: ReconcileItem) => void
  onLocateParent: (item: ReconcileItem) => void
  /** 写入在途：面板动作一律禁用，避免半途改核对口径 */
  pending: boolean
  /** 本批已写过的接受理由：新开一条时预填它（载荷只有顶层一条理由，鼓励写同一条） */
  acceptSeed?: string
}) {
  const isDesktop = useMediaQuery('(min-width: 1024px)', true)
  // 正在填理由的那一条（一次只开一条：两条理由框并排会让人分不清在改哪条）
  const [acceptOpen, setAcceptOpen] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  // 窄屏抽屉是**独立**的展开态：桌面常驻（默认展开），窄屏默认收成底部一条。
  // 共用一个状态会让手机一进清单就弹整屏抽屉，把清单本身挡住
  const [mobileOpen, setMobileOpen] = useState(false)

  const blocking = items.filter((it) => it.blockKind !== null)
  const matchedCount = items.filter((it) => it.state === 'matched' || it.state === 'accepted').length
  const mismatchCount = items.length - matchedCount

  const statusText = (() => {
    if (items.length === 0) return { cls: 'text-muted', text: '本批没有汇总行，不需要核对' }
    if (blocking.length > 0) {
      return { cls: 'text-amber-700', text: `${blocking.length} 行还没对上，本批暂不能写入` }
    }
    return { cls: 'text-emerald-700', text: '差额已全部对上或已接受，可以写入' }
  })()

  const startAccept = (parentId: string, reason: string) => {
    setAcceptOpen(parentId)
    // 本条已有的理由优先；没有就预填本批已写过的那条，避免两条理由各写各的
    setDraft(reason || acceptSeed || '')
  }
  const cancelAccept = () => {
    setAcceptOpen(null)
    setDraft('')
  }
  const confirmAccept = (parentId: string) => {
    const reason = draft.trim()
    if (!reason) return
    onAccept(parentId, reason.slice(0, 200))
    setAcceptOpen(null)
    setDraft('')
  }

  const header = (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="flex min-h-[48px] w-full items-center gap-2.5 px-4 py-3 text-left"
    >
      <span className="text-sm font-semibold text-ink">本批核对</span>
      <span className="font-code text-xs text-muted">
        汇总 {items.length} 项 · 一致 {matchedCount} · 差额 {mismatchCount}
      </span>
      <span className="ml-auto text-muted" aria-hidden="true">
        {open ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
      </span>
    </button>
  )

  const lines = (
    <div className="divide-y divide-line/60">
      {items.length === 0 && (
        <p className="px-4 py-4 text-xs leading-relaxed text-muted">
          本批还没有汇总行：在要作父项的那一行勾「汇总（只核对，不入账）」，它才会出现在这里。
        </p>
      )}
      {items.map((it) => {
        const ok = it.state === 'matched'
        const acceptedAlright = it.state === 'accepted'
        const stale = it.state === 'stale'
        const unknown = it.state === 'unknown'
        const parentLabel =
          it.parent.kind === 'row'
            ? `图 ${it.parent.imageOrdinal ?? '?'}`
            : '既有资产项'
        return (
          <div key={it.parent.id} className="px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <button
                type="button"
                onClick={() => onLocateParent(it)}
                title="定位到清单里的这一行"
                className="max-w-[14rem] truncate text-left text-sm font-medium text-ink underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                {it.parent.name || '未命名'}
              </button>
              <span className="text-meta text-muted">{parentLabel}</span>
              <span className="flex flex-wrap items-center gap-x-3 text-xs text-secondary">
                <span>
                  父项{' '}
                  <b className="font-code font-semibold text-ink">
                    {it.parentAmount === null ? '未知' : formatCurrency(it.parentAmount.toString())}
                  </b>
                </span>
                <span>
                  子项 {it.children.length} 项{' '}
                  <b className="font-code font-semibold text-ink">{formatCurrency(it.childSum.toString())}</b>
                </span>
              </span>
              <span
                className={`ml-auto flex items-center gap-1.5 text-xs ${
                  ok || acceptedAlright ? 'text-emerald-700' : stale || unknown ? 'text-secondary' : 'text-amber-700'
                }`}
              >
                {ok ? (
                  <>
                    差额 <b className="font-code text-sm font-semibold">0.00</b> · 一致
                  </>
                ) : unknown ? (
                  <>差额未知</>
                ) : (
                  <>
                    差额{' '}
                    <b className="font-code text-sm font-semibold">
                      {formatSignedCurrency((it.diff as NonNullable<typeof it.diff>).toString())}
                    </b>
                  </>
                )}
              </span>
            </div>

            <div className="mt-1 flex flex-wrap items-center gap-2">
              {!ok && !unknown && (
                <>
                  <button
                    type="button"
                    disabled={pending}
                    onClick={() => onGoToChildren(it)}
                    className="inline-flex min-h-[36px] items-center rounded-control border border-line bg-surface px-2.5 text-xs font-medium text-secondary transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
                  >
                    {it.children.length ? '去改子项' : '去归子项'}
                  </button>
                  <button
                    type="button"
                    disabled={pending || it.blockKind === 'parent-not-asset'}
                    title={
                      it.blockKind === 'parent-not-asset'
                        ? '汇总行需先落成资产项：先取消这里的「汇总」勾选写入，再回来勾'
                        : '接受这个差额；必须写清原因，审计与对账都看这条'
                    }
                    onClick={() => startAccept(it.parent.id, it.accepted?.reason ?? '')}
                    className="inline-flex min-h-[36px] items-center rounded-control border border-line bg-surface px-2.5 text-xs font-medium text-secondary transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50"
                  >
                    接受差额
                  </button>
                </>
              )}
              {it.blockKind === 'parent-not-asset' && (
                <span className="flex items-start gap-1 text-meta leading-relaxed text-amber-700">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
                  汇总行需先落成资产项：先取消这里的「汇总」勾选写入，再回来勾。
                </span>
              )}
              {unknown && (
                <span className="text-meta text-muted">
                  父项金额不在本次预览里，无法算差额；该项可用「归到…」改成汇总行再做核对
                </span>
              )}
            </div>

            {acceptedAlright && it.accepted && (
              <p className="mt-1.5 flex flex-wrap items-baseline gap-2 rounded-control bg-emerald-50 px-2 py-1 text-meta text-emerald-700">
                <CheckCircle2 size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span>已接受差额 {formatSignedCurrency(it.accepted.diff)}</span>
                <span className="min-w-0 flex-1 truncate text-secondary" title={it.accepted.reason}>
                  理由：{it.accepted.reason}
                </span>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => onWithdraw(it.parent.id)}
                  className="shrink-0 rounded border border-line bg-surface px-1.5 py-0.5 text-meta text-secondary transition-colors hover:text-ink disabled:opacity-50"
                >
                  撤回
                </button>
              </p>
            )}
            {stale && it.accepted && (
              <p className="mt-1.5 rounded-control bg-amber-50 px-2 py-1 text-meta leading-relaxed text-amber-700">
                差额已变化：接受时 {formatSignedCurrency(it.accepted.diff)}，现在{' '}
                {formatSignedCurrency((it.diff as NonNullable<typeof it.diff>).toString())}。需要重新接受。
              </p>
            )}

            {acceptOpen === it.parent.id && (
              <div className="mt-2 rounded-control border border-amber-200 bg-amber-50 p-2.5">
                <label
                  htmlFor={`accept-${it.parent.id}`}
                  className="block text-meta text-amber-700"
                >
                  接受差额要写清原因，之后对账与审计都看这条（最多 200 字）
                </label>
                <textarea
                  id={`accept-${it.parent.id}`}
                  value={draft}
                  maxLength={200}
                  rows={2}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      e.stopPropagation()
                      cancelAccept()
                    }
                  }}
                  placeholder="例：父项只覆盖其中一个子账户，其余子项不在本次截图范围内"
                  className="mt-1 w-full resize-y rounded-control border border-line-strong bg-surface px-2 py-1.5 text-xs leading-relaxed text-ink focus:border-brand-400 focus:outline-none focus:ring-1 focus:ring-brand-400"
                />
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={cancelAccept}
                    className="inline-flex min-h-[32px] items-center rounded-control border border-line bg-surface px-2.5 text-xs text-secondary transition-colors hover:text-ink"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    disabled={!draft.trim() || pending}
                    onClick={() => confirmAccept(it.parent.id)}
                    className="inline-flex min-h-[32px] items-center rounded-control bg-brand-600 px-2.5 text-xs font-medium text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                  >
                    确认接受差额{' '}
                    {it.diff === null ? '' : formatSignedCurrency(it.diff.toString())}
                  </button>
                  <span className="text-meta text-muted">
                    {draft.trim() ? `${draft.trim().length}/200` : '不填理由，确认按钮保持不可用'}
                  </span>
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )

  // ---- 桌面：清单末尾常驻（吸底），写入按钮在下面的确认栏（这一块只给状态与修法） ----
  if (isDesktop) {
    return (
      <section
        aria-label="本批核对"
        className="sticky bottom-0 z-10 overflow-hidden rounded-card border border-line bg-surface shadow-card"
      >
        <div className="border-b border-line bg-subtle/60">{header}</div>
        {open && lines}
        {open && (
          <p className={`border-t border-line px-4 py-2 text-xs ${statusText.cls}`} role="status">
            {statusText.text}
          </p>
        )}
      </section>
    )
  }

  // ---- 窄屏：底部一条 + 抽屉（点遮罩关闭；标题行同时是展开按钮） ----
  return (
    <>
      <button
        type="button"
        onClick={() => setMobileOpen((v) => !v)}
        aria-expanded={mobileOpen}
        className="fixed inset-x-0 bottom-0 z-sticky flex min-h-[52px] items-center gap-2 border-t border-line bg-surface px-4 pb-[var(--pf-safe-bottom)] text-left shadow-overlay"
      >
        <span className="text-sm font-semibold text-ink">本批核对</span>
        <span className="font-code text-xs text-muted">
          汇总 {items.length} · 差额 {mismatchCount}
        </span>
        <span className={`ml-auto text-xs ${statusText.cls}`}>
          {blocking.length > 0 ? `${blocking.length} 行待处理` : '可写入'}
        </span>
        <span className="text-muted" aria-hidden="true">
          {mobileOpen ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
        </span>
      </button>
      {mobileOpen && (
        <div className="fixed inset-0 z-modal flex items-end">
          <div
            className="absolute inset-0 animate-fade-in bg-black/40"
            onClick={() => setMobileOpen(false)}
            aria-hidden="true"
          />
          <section
            role="dialog"
            aria-modal="true"
            aria-label="本批核对"
            className="relative flex max-h-[70dvh] w-full flex-col rounded-t-overlay bg-surface shadow-overlay"
          >
            <div className="flex items-center gap-2 border-b border-line px-4 py-2">
              <span className="text-sm font-semibold text-ink">本批核对</span>
              <span className="font-code text-xs text-muted">
                汇总 {items.length} · 一致 {matchedCount} · 差额 {mismatchCount}
              </span>
              <button
                type="button"
                onClick={() => setMobileOpen(false)}
                aria-label="收起核对"
                className="ml-auto flex h-11 w-11 items-center justify-center rounded-control text-muted"
              >
                <X size={18} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">{lines}</div>
            <p className={`border-t border-line px-4 py-2 text-xs ${statusText.cls}`} role="status">
              {statusText.text}
            </p>
          </section>
        </div>
      )}
    </>
  )
}