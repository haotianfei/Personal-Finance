'use client'

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { createPortal } from 'react-dom'
import {
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  ImageOff,
  Loader2,
  Minus,
  Plus,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useDialogA11y } from '@/components/layout/useDialogA11y'

/**
 * 一张可查看的原图。
 *
 * `evidenceId` 决定能不能用后端「下载原图」：文件名由后端 `Content-Disposition` 给出，
 * 前端不自己拼名（拼出来的只会是 image、download 这类占位名）。
 */
export interface ViewableImage {
  /** 稳定键（页内图 id）：调用方据此把卡片和查看器对应起来 */
  key: string
  /** 图片地址：优先 evidence 原图端点；没有 evidence_id 时回退本地 blob URL */
  src: string
  /** 原始文件名（卡片上显示的那个名字） */
  filename: string
  /** 截图凭证 id；缺失 = 只有本地 blob，不提供下载 */
  evidenceId?: number | null
}

const ZOOM_MIN = 0.5
const ZOOM_MAX = 4
/** 有级差的缩放台阶：0.5× → 4×，避免「连点十下才看得出变化」，也避免一步跳太狠 */
const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4]

const BTN_BASE =
  'inline-flex items-center justify-center rounded-control border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus'
// 桌面（sm+）带文字说明；移动端只剩图标，命中区仍是 44px
const BTN_LABELED = `${BTN_BASE} h-11 w-11 gap-1.5 px-0 sm:h-9 sm:w-auto sm:px-2.5`
const BTN_ICON = `${BTN_BASE} h-11 w-11 lg:h-9 lg:w-9`
const BTN_IDLE = 'border-white/15 bg-white/5 text-inverse-text/85 hover:bg-white/15 hover:text-inverse-text'
const BTN_OFF = 'cursor-not-allowed border-white/10 bg-transparent text-inverse-text/25'
const ZOOM_BTN = 'inline-flex h-11 w-11 items-center justify-center rounded-[6px] transition-colors lg:h-8 lg:w-8'

/**
 * 全屏原图查看器（截图录入 → 待录入清单）。
 *
 * 存在的理由：复核时要核对的是截图上的数字，缩略图和识别结果都不够看，
 * 得把原图铺满屏幕、放大、在几张图之间来回翻。
 *
 * 交互约定：
 * - 缩放 0.5–4×（按钮 / `+` `-` 键），双击复位到「适应窗口」（100% = 铺满可用区域）；
 * - 放大后可拖动平移（触摸同样可拖），平移范围按舞台尺寸夹住，拖不出视野；
 * - 多图左右切换（按钮 / `←` `→` 键，首尾循环），标题恒为「第 N/M 张 · 文件名」；
 * - 焦点陷阱、Esc 关闭、关闭后焦点回到触发元素、背景 inert、body 滚动锁 —— 全部交给
 *   `useDialogA11y`（与 `ResponsiveDialog` 同一套），这里只补它不管的翻页/缩放键；
 * - 加载失败就地报错 + 「新标签重试」，绝不把页面搞白屏。
 *
 * 挂到 `document.body`（而不是留在页面里）：这样背景 `#app-shell` 才能整体 inert，
 * 底下的清单既点不到也 Tab 不到，不用自己兜焦点了。
 *
 * 按「打开才挂载」使用（调用方 `{viewerIndex !== null && <ImageViewer …/>}`）：
 * 缩放、报错、焦点记忆都是挂载期状态，重新打开自动回到 100%。
 */
export function ImageViewer({
  images,
  index,
  onIndexChange,
  onClose,
}: {
  images: ViewableImage[]
  index: number
  onIndexChange: (index: number) => void
  onClose: () => void
}) {
  const total = images.length
  const safeIndex = total > 0 ? Math.min(Math.max(index, 0), total - 1) : 0
  const current = total > 0 ? images[safeIndex] : undefined

  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number } | null>(
    null,
  )

  const [zoom, setZoom] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)

  // 父级传的是内联 onClose：固定成稳定函数，避免无障碍副作用随每次渲染重建
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const close = useCallback(() => onCloseRef.current(), [])
  useDialogA11y({ open: true, onClose: close, containerRef: panelRef, initialFocusRef: closeRef })

  // --- 缩放 / 平移 ---
  // 平移上限按舞台盒子与缩放比推导：放大到 z 时每侧可溢出 (z-1)×舞台/2，
  // 比「量图片实际渲染尺寸」简单，且永远不会把图拖丢
  const clampOffset = useCallback((nextZoom: number, next: { x: number; y: number }) => {
    if (nextZoom <= 1) return { x: 0, y: 0 }
    const rect = stageRef.current?.getBoundingClientRect()
    if (!rect) return next
    const maxX = ((nextZoom - 1) * rect.width) / 2
    const maxY = ((nextZoom - 1) * rect.height) / 2
    return {
      x: Math.max(-maxX, Math.min(maxX, next.x)),
      y: Math.max(-maxY, Math.min(maxY, next.y)),
    }
  }, [])

  const applyZoom = useCallback(
    (nextZoom: number) => {
      const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(nextZoom * 100) / 100))
      setZoom(clamped)
      setOffset((prev) => clampOffset(clamped, prev))
    },
    [clampOffset],
  )

  const zoomIn = useCallback(() => {
    applyZoom(ZOOM_STEPS.find((s) => s > zoom + 0.001) ?? ZOOM_MAX)
  }, [applyZoom, zoom])

  const zoomOut = useCallback(() => {
    applyZoom([...ZOOM_STEPS].reverse().find((s) => s < zoom - 0.001) ?? ZOOM_MIN)
  }, [applyZoom, zoom])

  const resetView = useCallback(() => {
    setZoom(1)
    setOffset({ x: 0, y: 0 })
  }, [])

  // 窗口尺寸变了：夹一次平移量，免得图被挤到视野外
  useEffect(() => {
    const onResize = () => setOffset((prev) => clampOffset(zoom, prev))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [clampOffset, zoom])

  // --- 多图切换（首尾循环；换图回到 100% 并清掉上一张的报错状态）---
  const go = useCallback(
    (delta: number) => {
      if (total < 2) return
      setFailed(false)
      setLoaded(false)
      setZoom(1)
      setOffset({ x: 0, y: 0 })
      onIndexChange((safeIndex + delta + total) % total)
    },
    [onIndexChange, safeIndex, total],
  )

  // --- 键盘：只补 useDialogA11y 不管的翻页与缩放（Esc / Tab 由它负责）---
  const handlersRef = useRef({ go, zoomIn, zoomOut })
  handlersRef.current = { go, zoomIn, zoomOut }
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const handlers = handlersRef.current
      switch (event.key) {
        case '+':
        case '=':
          event.preventDefault()
          handlers.zoomIn()
          return
        case '-':
        case '_':
          event.preventDefault()
          handlers.zoomOut()
          return
        case 'ArrowLeft':
          event.preventDefault()
          handlers.go(-1)
          return
        case 'ArrowRight':
          event.preventDefault()
          handlers.go(1)
          return
        default:
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [])

  // --- 拖动平移（仅在放大后可拖；指针捕获让拖出舞台也不断线）---
  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (zoom <= 1) return
    if (event.pointerType === 'mouse' && event.button !== 0) return
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: offset.x,
      originY: offset.y,
    }
    setDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    event.preventDefault()
    setOffset(
      clampOffset(zoom, {
        x: drag.originX + (event.clientX - drag.startX),
        y: drag.originY + (event.clientY - drag.startY),
      }),
    )
  }

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
  }

  if (!current || typeof document === 'undefined') return null

  const percent = Math.round(zoom * 100)
  const canPan = zoom > 1
  const downloadHref = current.evidenceId != null ? `${api.evidenceImageUrl(current.evidenceId)}?download=1` : null

  return createPortal(
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-label="查看原图"
      tabIndex={-1}
      className="fixed inset-0 z-modal flex animate-fade-in flex-col bg-inverse/95 backdrop-blur-sm focus:outline-none"
    >
      {/* 顶栏：第 N/M 张 · 文件名 ＋ 兜底动作（新标签打开 / 下载原图 / 关闭） */}
      <div className="flex shrink-0 items-center gap-2 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-4 lg:px-6">
        <p aria-live="polite" className="flex min-w-0 flex-1 items-center gap-2 text-sm text-inverse-text">
          <span className="shrink-0 rounded-pill bg-white/10 px-2 py-0.5 text-meta tabular-nums text-inverse-text/80">
            第 {safeIndex + 1}/{total} 张
          </span>
          <span className="shrink-0 text-inverse-text/30">·</span>
          <span className="min-w-0 truncate text-inverse-text/90" title={current.filename}>
            {current.filename}
          </span>
        </p>
        <a
          href={current.src}
          target="_blank"
          rel="noreferrer"
          title="在新标签页打开原图"
          className={`${BTN_LABELED} ${BTN_IDLE}`}
        >
          <ExternalLink size={15} aria-hidden="true" />
          <span className="sr-only text-xs sm:not-sr-only">新标签打开</span>
        </a>
        {downloadHref ? (
          <a href={downloadHref} title="下载原图（保留原始文件名）" className={`${BTN_LABELED} ${BTN_IDLE}`}>
            <Download size={15} aria-hidden="true" />
            <span className="sr-only text-xs sm:not-sr-only">下载原图</span>
          </a>
        ) : (
          // 无 evidence_id = 只有本地 blob，服务端拿不到原始文件名，就不给一个假名字的下载
          <span className={`${BTN_LABELED} ${BTN_OFF}`} title="原图尚未保存到服务端，暂不可下载">
            <Download size={15} aria-hidden="true" />
            <span className="sr-only text-xs sm:not-sr-only">下载原图</span>
          </span>
        )}
        <button
          ref={closeRef}
          type="button"
          onClick={close}
          aria-label="关闭原图"
          title="关闭（Esc）"
          className={`${BTN_ICON} ${BTN_IDLE}`}
        >
          <X size={18} aria-hidden="true" />
        </button>
      </div>

      {/* 舞台：contain 铺满可用区域，放大后可拖 */}
      <div
        ref={stageRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={resetView}
        className={`relative min-h-0 flex-1 overflow-hidden ${
          canPan ? (dragging ? 'cursor-grabbing touch-none' : 'cursor-grab touch-none') : ''
        }`}
      >
        {/* 极轻的中心提亮：图是主体，这里只给它一点体积感 */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,rgba(255,255,255,0.07),transparent_70%)]"
        />
        <div className="flex h-full w-full items-center justify-center">
          {failed ? (
            <div role="alert" className="flex max-w-sm flex-col items-center gap-2 px-6 text-center">
              <ImageOff size={28} className="text-inverse-text/50" aria-hidden="true" />
              <p className="text-sm font-medium text-inverse-text">原图加载失败</p>
              <p className="text-xs leading-relaxed text-inverse-text/60">
                文件可能已从磁盘移除，或本次会话的临时预览已失效。可在新标签页重试。
              </p>
              <a
                href={current.src}
                target="_blank"
                rel="noreferrer"
                className="mt-1 inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-white/20 bg-white/10 px-3 text-xs font-medium text-inverse-text transition-colors hover:bg-white/20"
              >
                <ExternalLink size={14} aria-hidden="true" /> 新标签重试
              </a>
            </div>
          ) : (
            // eslint-disable-next-line @next/next/no-img-element -- 原图直出：本页统一用原生 img，不走 next/image
            <img
              src={current.src}
              alt={current.filename}
              draggable={false}
              onLoad={() => setLoaded(true)}
              onError={() => {
                setLoaded(false)
                setFailed(true)
              }}
              style={{ transform: `translate3d(${offset.x}px, ${offset.y}px, 0) scale(${zoom})` }}
              className={`max-h-full max-w-full select-none object-contain ${
                dragging ? 'transition-none' : 'transition-[transform,opacity] duration-fast ease-standard'
              } ${loaded ? 'opacity-100' : 'opacity-0'}`}
            />
          )}
        </div>
        {!loaded && !failed && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <Loader2 size={22} className="animate-spin text-inverse-text/60" aria-hidden="true" />
            <span className="sr-only">正在加载原图</span>
          </div>
        )}
      </div>

      {/* 底栏：翻页 ＋ 缩放（移动端收成一行图标；桌面再补一行操作提示） */}
      <div className="flex shrink-0 flex-col items-center gap-1 px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-2 sm:px-4 lg:px-6">
        <div className="flex flex-wrap items-center justify-center gap-1.5 sm:gap-2">
          {total > 1 && (
            <button type="button" onClick={() => go(-1)} title="上一张（←）" className={`${BTN_ICON} ${BTN_IDLE}`}>
              <ChevronLeft size={18} aria-hidden="true" />
              <span className="sr-only">上一张</span>
            </button>
          )}
          <div className="flex items-center gap-0.5 rounded-control border border-white/15 bg-white/5 p-0.5">
            <button
              type="button"
              onClick={zoomOut}
              disabled={zoom <= ZOOM_MIN}
              aria-label="缩小"
              title="缩小（-）"
              className={`${ZOOM_BTN} ${
                zoom <= ZOOM_MIN ? 'cursor-not-allowed text-inverse-text/25' : 'text-inverse-text/85 hover:bg-white/10'
              }`}
            >
              <Minus size={16} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={resetView}
              title="适应窗口（双击图片也可复位）"
              aria-label={`当前缩放 ${percent}%，点击适应窗口`}
              className="min-h-[44px] min-w-[3.75rem] rounded-[6px] px-1 text-xs tabular-nums text-inverse-text/85 transition-colors hover:bg-white/10 lg:min-h-[32px]"
            >
              {percent}%
            </button>
            <button
              type="button"
              onClick={zoomIn}
              disabled={zoom >= ZOOM_MAX}
              aria-label="放大"
              title="放大（+）"
              className={`${ZOOM_BTN} ${
                zoom >= ZOOM_MAX ? 'cursor-not-allowed text-inverse-text/25' : 'text-inverse-text/85 hover:bg-white/10'
              }`}
            >
              <Plus size={16} aria-hidden="true" />
            </button>
          </div>
          {total > 1 && (
            <button type="button" onClick={() => go(1)} title="下一张（→）" className={`${BTN_ICON} ${BTN_IDLE}`}>
              <ChevronRight size={18} aria-hidden="true" />
              <span className="sr-only">下一张</span>
            </button>
          )}
        </div>
        <p className="hidden text-meta text-inverse-text/45 lg:block">
          {total > 1 ? '方向键切换 · 放大后可拖动 · 双击复位' : '放大后可拖动 · 双击复位'}
        </p>
      </div>
    </div>,
    document.body,
  )
}