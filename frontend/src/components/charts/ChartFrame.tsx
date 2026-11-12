'use client'

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type Ref,
  type SyntheticEvent,
} from 'react'
import { ArrowUpRight, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { directionLabel, gradeLabel } from '@/lib/metric-language'
import { GradeDot } from '@/components/ui/Grade'
import { MetaBadge } from '@/components/ui/MetaBadge'
import { useChartEnvironment } from './environment'
import { collapseFrameBadges } from './meta'
import { CHART_COLORS, CHART_FONTS, chartTokens } from './theme'
import type { ChartFrameHeight, FrameBadge, FrameLamp, LegendItem } from './types'

/**
 * 图帧（design §4）—— 图表层唯一的「可读 chrome」。
 *
 * 分工（§4.5）：
 * - 灯是判断态，只在帧头；来源（含沿用值/缺测）才画进图形，不在 frame 里伪装；
 * - 数值格式不在本文件做，badge 文案由 `meta.ts` / `lib/metric-language` 给；
 * - 画布只负责数据墨迹，标题/口径/图例/状态/下钻都在 DOM 里。
 *
 * 状态（§4.4）：`ready | loading | empty | insufficient | error`。
 * 状态只覆盖画布，高度由 `chartTokens().heights` 预留，切换不跳。
 *
 * 无障碍（§4.6）：
 * - 本组件是 `<figure aria-labelledby>`；画布槽在 ready 时 `role="img"` + 摘要；
 * - primitive 不要再给 `Chart` 传 `ariaLabel`（会嵌套两层 img）；需要时把同一句摘要
 *   通过 `a11ySummary` 交给本组件；
 * - 数据表 `<details>` 是 canvas 的等价物；键盘可达。
 *
 * 本文件不 import presets/primitives、不取数、不计算业务值。
 */

export interface ChartFrameBaseProps {
  /** 帧标题；14/20、600、brand-950。string 时自动加 title 悬停全称。 */
  title: ReactNode
  /** 副标题（账期/单位/窗口等短句）；与 `asOf` 用 ` · ` 拼接。 */
  subtitle?: ReactNode
  /** as-of 日期；空则整段省略，不写「as-of —」。 */
  asOf?: string | null
  /** 单指标图的灯位；多系列趋势/结构图不传。 */
  lamp?: FrameLamp | null
  /** `lamp=null` 时的方向文字（越高越好/越低越好/中性…）。 */
  direction?: string | null
  /** 口径徽标；由 `frameBadgesFrom()` 生成，本组件负责折叠。 */
  badges?: FrameBadge[]
  /** 图例操作区；缺 `onToggle` 的项不可切换（结构图）。 */
  legend?: LegendItem[]
  /** 高度档或固定像素；默认 `md`（260 → 350）。 */
  height?: ChartFrameHeight
  /** `bare` 嵌在页面卡片里；`card` 自带白底/圆角/阴影。 */
  variant?: 'bare' | 'card'
  /** 容器测量 ref；primitive 可复用它调 `useChartEnvironment`。 */
  bodyRef?: Ref<HTMLDivElement>
  /** 动作区（窗口 chips 等）；最多两组，由 primitive 组织。 */
  actions?: ReactNode
  /** 下钻；只做显式按钮，不做整图点击。 */
  drill?: { label: string; onClick: () => void }
  /** 画布状态；默认 ready。 */
  state?: 'ready' | 'loading' | 'empty' | 'error' | 'insufficient'
  /** empty 文案/动作；action 由页面给（去记录 / 设定口径 / 切换账期）。 */
  empty?: { title: string; hint?: string; action?: ReactNode }
  /** 脚注（如「空心点＝含沿用值」）；11px、slate-500。 */
  footnote?: ReactNode
  /** 数据表（canvas 等价物）；列名与已格式化行由调用方给。 */
  a11yTable?: { caption: string; columns: string[]; rows: (string | number)[][] }
  className?: string
}

export interface ChartFrameProps extends ChartFrameBaseProps {
  /** 画布槽（primitive 的 `<Chart>` 或 DOM primitive）。 */
  children?: ReactNode
  /** 画布摘要；缺省由标题/副标题/徽标/图例派生。 */
  a11ySummary?: string
  /** error 态重试；缺省不显示重试按钮。 */
  onRetry?: () => void
}

const EMPTY_BADGES: FrameBadge[] = []
const EMPTY_LEGEND: LegendItem[] = []

/** 多个 ref 合并（bodyRef 与内部测量 ref 指向同一节点）。 */
function assignRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (!ref) return
  if (typeof ref === 'function') {
    ref(value)
    return
  }
  ;(ref as { current: T | null }).current = value
}

function summaryFrom(
  title: ReactNode,
  subtitle: ReactNode,
  badges: FrameBadge[],
  legend: LegendItem[],
  lamp: FrameLamp | null | undefined,
  direction: string | null,
): string {
  const parts: string[] = []
  if (typeof title === 'string' && title.trim()) parts.push(title.trim())
  if (typeof subtitle === 'string' && subtitle.trim()) parts.push(subtitle.trim())
  if (badges.length) parts.push(badges.map((b) => b.label).join('、'))
  if (legend.length) parts.push(`图例：${legend.map((item) => item.label).join('、')}`)
  if (lamp) parts.push(`${gradeLabel(lamp.state)}${lamp.note ? `：${lamp.note}` : ''}`)
  else if (direction) parts.push(direction)
  return parts.join('。') || '图表'
}

/** 图帧状态面板：空/数据不足/错误共用一套排版（§4.4）。 */
function FrameState({
  title,
  hint,
  action,
  alert = false,
}: {
  title: string
  hint?: string
  action?: ReactNode
  alert?: boolean
}) {
  return (
    <div
      role={alert ? 'alert' : undefined}
      className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 px-4 text-center"
    >
      <span
        className="h-2.5 w-2.5 shrink-0 rounded-full border-2 border-slate-300 bg-white"
        aria-hidden
      />
      <p className="text-[13px] font-medium leading-5 text-slate-600">{title}</p>
      {hint && <p className="max-w-sm text-[11px] leading-4 text-slate-500">{hint}</p>}
      {action && <div className="mt-1.5 flex items-center justify-center">{action}</div>}
    </div>
  )
}

/** 加载骨架：3 条 hairline ＋ 1 块淡色轮廓（§4.4）。 */
function FrameSkeleton({ reducedMotion }: { reducedMotion: boolean }) {
  const pulse = !reducedMotion && 'animate-pulse motion-reduce:animate-none'
  return (
    <div className="absolute inset-0 flex flex-col justify-center gap-2.5 px-3" aria-hidden data-frame-skeleton>
      <div className={cn('h-px w-full bg-slate-100', pulse)} />
      <div className={cn('h-px w-5/6 bg-slate-100', pulse)} />
      <div className={cn('h-px w-2/3 bg-slate-100', pulse)} />
      <div className={cn('h-16 w-full rounded-lg border border-slate-100 bg-slate-50/70', pulse)} />
    </div>
  )
}

/** 图例项：可切换用 button（aria-pressed）；结构图用 span（只做起名/高亮）。 */
function LegendChip({ item, hoverable }: { item: LegendItem; hoverable: boolean }) {
  const active = item.active !== false
  const handlers = {
    ...(hoverable
      ? {
          onMouseEnter: () => item.onHover?.(true),
          onMouseLeave: () => item.onHover?.(false),
        }
      : {}),
    onFocus: () => item.onHover?.(true),
    onBlur: () => item.onHover?.(false),
  }

  const content = (
    <>
      <span
        className={cn(
          'h-2 w-2 shrink-0 rounded-full',
          !active && 'border-2',
          active && 'ring-1 ring-inset ring-slate-300/70',
        )}
        style={
          active
            ? { backgroundColor: item.color }
            : { borderColor: item.color, backgroundColor: CHART_COLORS.surface }
        }
        aria-hidden
      />
      <span
        className={cn(
          'max-w-[14rem] truncate text-[11px] font-medium leading-4',
          active ? 'text-slate-700' : 'text-slate-500',
        )}
        title={item.label}
      >
        {item.label}
      </span>
      {item.valueText && (
        <span className="shrink-0 text-[11px] leading-4 tabular-nums text-slate-500">
          {item.valueText}
        </span>
      )}
    </>
  )

  const className = cn(
    'relative inline-flex min-h-[44px] shrink-0 items-center gap-1.5 rounded',
    item.onToggle ? 'cursor-pointer' : 'cursor-default',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1',
  )

  if (item.onToggle) {
    return (
      <button
        type="button"
        onClick={item.onToggle}
        aria-pressed={active}
        aria-label={`${item.label}${item.valueText ? `，${item.valueText}` : ''}${active ? '' : '（已隐藏）'}`}
        className={className}
        {...handlers}
      >
        {content}
      </button>
    )
  }

  return (
    <span
      className={className}
      tabIndex={item.onHover ? 0 : undefined}
      aria-label={item.onHover ? `${item.label}${item.valueText ? `，${item.valueText}` : ''}` : undefined}
      {...handlers}
    >
      {content}
    </span>
  )
}

export function ChartFrame({
  title,
  subtitle,
  asOf,
  lamp,
  direction,
  badges,
  legend,
  height = 'md',
  variant = 'bare',
  bodyRef,
  actions,
  drill,
  state = 'ready',
  empty,
  footnote,
  a11yTable,
  className,
  children,
  a11ySummary,
  onRetry,
}: ChartFrameProps) {
  const titleId = useId()
  const bodyNode = useRef<HTMLDivElement | null>(null)
  const legendScrollRef = useRef<HTMLDivElement | null>(null)
  const detailsRef = useRef<HTMLDetailsElement | null>(null)
  const [legendEdges, setLegendEdges] = useState({ left: false, right: false })
  // 数据表惰性挂载（spec D2）：`<details>` 首次打开前不渲染 tbody 行；打开过即保持挂载。
  // 只记「是否曾打开过」，不给 `<details>` 传 `open`（受控会与用户点击抢状态）。
  const [tableOpened, setTableOpened] = useState(false)

  // 高度/骨架动效走同一份 chartTokens（§5.4/§5.6）；容器宽由 ChartFrame 自己测，
  // primitive 仍可用同一 bodyRef 再测一次（幂等，不影响布局）。
  const { dense, coarse, reducedMotion } = useChartEnvironment(bodyNode)
  const tokens = chartTokens({ dense, reducedMotion })
  const bodyHeight = typeof height === 'number' ? height : tokens.heights[height]

  const badgeList = badges ?? EMPTY_BADGES
  const legendItems = legend ?? EMPTY_LEGEND
  const { visible, hidden, overflowLabel } = collapseFrameBadges(badgeList)
  const directionText = lamp ? null : directionLabel(direction)
  const hasMeta = Boolean(asOf || subtitle || visible.length || hidden.length)
  const hasActions = Boolean(actions || drill)
  const summary =
    a11ySummary?.trim() || summaryFrom(title, subtitle, badgeList, legendItems, lamp, directionText)
  const toggleableHidden = legendItems.filter((item) => item.active === false && item.onToggle)

  const attachBody = useCallback(
    (node: HTMLDivElement | null) => {
      bodyNode.current = node
      assignRef(bodyRef, node)
    },
    [bodyRef],
  )

  // 图例横向溢出时给两侧渐隐（§4.3）；不可切换的静态图例同样适用。
  const updateLegendEdges = useCallback(() => {
    const element = legendScrollRef.current
    if (!element) return
    const max = element.scrollWidth - element.clientWidth
    const left = element.scrollLeft > 1
    const right = max > 1 && element.scrollLeft < max - 1
    setLegendEdges((previous) =>
      previous.left === left && previous.right === right ? previous : { left, right },
    )
  }, [])

  useEffect(() => {
    updateLegendEdges()
    const element = legendScrollRef.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(updateLegendEdges)
    observer.observe(element)
    return () => observer.disconnect()
  }, [legendItems, updateLegendEdges])

  const showAll = () => {
    for (const item of legendItems) {
      if (item.active === false && item.onToggle) item.onToggle()
    }
  }

  const openTable = () => {
    const element = detailsRef.current
    if (!element) return
    element.open = true
    // 命令式入口不走 React 状态；行渲染挂在 tableOpened 上，这里必须同步置位，
    // 否则 header「数据表」按钮（insufficient 态）打开后仍是空表。
    setTableOpened(true)
    element.scrollIntoView({ block: 'nearest' })
  }

  // 原生 toggle 事件（用户点 summary / 键盘回车 / 上面那段命令式置位触发）。
  // 只在「开」时记一次；关闭不回落状态 —— 行保持挂载，避免反复展开重排与丢滚动位置。
  const handleTableToggle = useCallback((event: SyntheticEvent<HTMLDetailsElement>) => {
    if (event.currentTarget.open) setTableOpened(true)
  }, [])

  // 兜底同步（仅挂载后一次）：浏览器可能在 hydration 之前恢复 `<details open>`（重载/回退的表单状态
  // 恢复），那条路径不经过 toggle，行会缺。effect 只在客户端跑，不影响首屏 SSR 与客户端的一致性。
  useEffect(() => {
    if (detailsRef.current?.open) setTableOpened(true)
  }, [])

  const hiddenDetail = hidden
    .map((badge) => (badge.detail ? `${badge.label}：${badge.detail}` : badge.label))
    .join('；')

  let bodyContent: ReactNode = null
  if (state === 'ready') {
    bodyContent = children
  } else if (state === 'loading') {
    bodyContent = (
      <>
        <span className="sr-only">图表加载中</span>
        <FrameSkeleton reducedMotion={reducedMotion} />
      </>
    )
  } else if (state === 'empty') {
    bodyContent = (
      <FrameState
        title={empty?.title ?? '暂无数据'}
        hint={empty?.hint}
        action={empty?.action}
      />
    )
  } else if (state === 'insufficient') {
    bodyContent = (
      <FrameState
        title="数据不足，暂不成图"
        hint="至少需要 2 个可画数据点"
        action={
          a11yTable ? (
            <button
              type="button"
              onClick={openTable}
              className="inline-flex min-h-[44px] items-center rounded px-2 text-[11px] font-medium text-brand-700 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1"
            >
              查看数据表
            </button>
          ) : null
        }
      />
    )
  } else {
    bodyContent = (
      <FrameState
        title="图表暂不可用"
        hint="请稍后重试"
        alert
        action={
          onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex min-h-[44px] items-center rounded px-2 text-[11px] font-medium text-brand-700 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1"
            >
              重试
            </button>
          ) : null
        }
      />
    )
  }

  return (
    <figure
      aria-labelledby={titleId}
      data-chart-frame=""
      data-state={state}
      data-dense={dense ? 'true' : 'false'}
      data-reduced-motion={reducedMotion ? 'true' : 'false'}
      data-coarse={coarse ? 'true' : 'false'}
      data-height={height}
      className={cn(
        'm-0 min-w-0 text-left',
        variant === 'card' && 'rounded-xl bg-white p-4 shadow-card sm:p-5',
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <h3
              id={titleId}
              title={typeof title === 'string' ? title : undefined}
              className="min-w-0 truncate text-sm font-semibold leading-5 text-brand-950"
            >
              {title}
            </h3>
            {lamp ? (
              <span
                className="inline-flex shrink-0 items-center gap-1 text-[11px] leading-4 text-slate-500"
                title={lamp.note ?? undefined}
                aria-label={lamp.note ? `${gradeLabel(lamp.state)}：${lamp.note}` : undefined}
              >
                <GradeDot state={lamp.state} />
                <span>{gradeLabel(lamp.state)}</span>
              </span>
            ) : directionText ? (
              <span className="shrink-0 text-[11px] leading-4 text-slate-500">{directionText}</span>
            ) : null}
          </div>

          {hasMeta && (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-[11px] leading-4 text-slate-500">
              {(asOf || subtitle) && (
                <span
                  className={cn(
                    'flex min-w-0 items-center gap-x-2',
                    dense && 'w-full flex-col items-start gap-y-0.5',
                  )}
                >
                  {asOf && (
                    <span
                      className="whitespace-nowrap tabular-nums"
                      style={{ fontFamily: CHART_FONTS.numeric }}
                    >
                      as-of {asOf}
                    </span>
                  )}
                  {!dense && asOf && subtitle && (
                    <span aria-hidden className="text-slate-300">
                      ·
                    </span>
                  )}
                  {subtitle && <span className="min-w-0">{subtitle}</span>}
                </span>
              )}
              {visible.map((badge, index) => (
                <MetaBadge
                  key={`${badge.code}-${index}`}
                  tone={badge.tone}
                  label={badge.label}
                  detail={badge.detail}
                />
              ))}
              {hidden.length > 0 && (
                <MetaBadge tone="muted" label={overflowLabel} detail={hiddenDetail} />
              )}
            </div>
          )}
        </div>

        {hasActions && (
          <div className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-2">
            {actions}
            {drill && (
              <button
                type="button"
                onClick={drill.onClick}
                className="relative inline-flex items-center gap-0.5 rounded px-1 py-1 text-[11px] font-medium leading-4 text-brand-700 transition-colors hover:text-brand-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1 after:absolute after:inset-x-0 after:-inset-y-2.5 after:content-[''] motion-reduce:transition-none"
              >
                {drill.label?.trim() || '查看明细'}
                <ArrowUpRight size={14} aria-hidden />
              </button>
            )}
          </div>
        )}
      </header>

      {legendItems.length > 0 && (
        <div className="mt-3 flex items-center gap-2" role="group" aria-label="图例">
          <div className="relative min-w-0 flex-1">
            <div
              ref={legendScrollRef}
              onScroll={updateLegendEdges}
              className="overflow-x-auto py-1 [scrollbar-width:thin]"
            >
              <div className="flex min-h-[44px] flex-nowrap items-center gap-x-4">
                {legendItems.map((item) => (
                  <LegendChip key={item.key} item={item} hoverable={!coarse} />
                ))}
              </div>
            </div>
            {legendEdges.left && (
              <span
                aria-hidden
                className="pointer-events-none absolute inset-y-0 left-0 w-6 bg-gradient-to-r from-white to-transparent"
              />
            )}
            {legendEdges.right && (
              <span
                aria-hidden
                className="pointer-events-none absolute inset-y-0 right-0 w-6 bg-gradient-to-l from-white to-transparent"
              />
            )}
          </div>
          {toggleableHidden.length > 0 && (
            <button
              type="button"
              onClick={showAll}
              className="inline-flex min-h-[44px] shrink-0 items-center rounded px-1 text-[11px] font-medium leading-4 text-brand-700 transition-colors hover:text-brand-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1 motion-reduce:transition-none"
            >
              全部显示
            </button>
          )}
        </div>
      )}

      <div
        ref={attachBody}
        style={{ height: bodyHeight }}
        role={state === 'ready' ? 'img' : undefined}
        aria-label={state === 'ready' ? summary : undefined}
        aria-busy={state === 'loading' ? true : undefined}
        className="relative mt-2 w-full"
        data-chart-body=""
      >
        {bodyContent}
      </div>

      {(footnote || a11yTable) && (
        <footer className="mt-2">
          {footnote && <div className="text-[11px] leading-4 text-slate-500">{footnote}</div>}
          {a11yTable && (
            <details ref={detailsRef} onToggle={handleTableToggle} className="group mt-1">
              <summary className="ml-auto flex min-h-[44px] w-fit cursor-pointer list-none items-center gap-1 text-[11px] font-medium leading-4 text-slate-500 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1 [&::-webkit-details-marker]:hidden">
                数据表
                <ChevronDown
                  size={12}
                  aria-hidden
                  className="transition-transform group-open:rotate-180 motion-reduce:transition-none"
                />
              </summary>
              <div className="mt-1 max-h-64 overflow-auto rounded-lg border border-slate-100">
                <table className="w-full border-collapse text-xs">
                  <caption className="sr-only">{a11yTable.caption}</caption>
                  <thead className="sticky top-0 bg-slate-50 text-[11px] text-slate-500">
                    <tr>
                      {a11yTable.columns.map((column, index) => (
                        <th
                          key={`${column}-${index}`}
                          scope="col"
                          className={cn(
                            'whitespace-nowrap px-2 py-1.5 font-medium',
                            index === 0 ? 'text-left' : 'text-right',
                          )}
                        >
                          {column}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 text-slate-600">
                    {tableOpened &&
                      a11yTable.rows.map((row, rowIndex) => (
                        <tr key={rowIndex}>
                          {row.map((cell, cellIndex) => (
                            <td
                              key={cellIndex}
                              className={cn(
                                'whitespace-nowrap px-2 py-1.5',
                                cellIndex === 0 ? 'text-left' : 'text-right tabular-nums',
                              )}
                            >
                              {String(cell ?? '')}
                            </td>
                          ))}
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </footer>
      )}
    </figure>
  )
}