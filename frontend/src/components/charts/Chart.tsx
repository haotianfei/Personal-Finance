'use client'

import { Component, useEffect, useRef, useState, type ReactNode } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import type { ECharts } from 'echarts/core'
import { echarts } from './registry'
import { CHART_COLORS } from './theme'

/**
 * 渲染原语：全仓唯一的 `echarts-for-react` 出口（design §2.6）。
 *
 * 职责：单实例生命周期（懒挂载 / resize / 主题 / 事件 / 错误兜底）。
 * 不做：解释 option 语义、格式化、标题/图例/口径（那些属于 ChartFrame / format.ts）。
 *
 * `lazy` 默认 true、`notMerge` true、`canvas`；V6 后唯一主题为 `ledger`。
 */
interface ChartProps {
  option: Record<string, unknown>
  height?: string
  loading?: boolean
  onEvents?: Record<string, (params: unknown) => void>
  lazy?: boolean
  /** 画布摘要（系列/范围/单位/口径）；提供时容器为 `role="img"`。 */
  ariaLabel?: string
  /** 懒挂载占位；默认与迁移前一致（淡底 +「加载中...」）。 */
  emptyPlaceholder?: ReactNode
  /** 实例就绪：只给图例高亮 / dispatchAction 这类展示动作。 */
  onReady?: (instance: ECharts) => void
  /** 错误边界回调（单个图失败时上报，不拖垮页面）。 */
  onError?: (error: Error) => void
}

interface ChartBoundaryProps {
  children: ReactNode
  /** 自定义帧内错误态；缺省为「图表暂不可用 + 重试」。 */
  fallback?: ReactNode
  onError?: (error: Error) => void
}

interface ChartBoundaryState {
  error: Error | null
}

/**
 * 单图错误边界：preset/实例抛错时在帧内显示 error，页面其余部分不受影响（§4.4）。
 * 放在 primitive 边界使用；`Chart` 自己也会包一层，保证直接使用时也不炸页面。
 */
export class ChartBoundary extends Component<ChartBoundaryProps, ChartBoundaryState> {
  state: ChartBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ChartBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error) {
    this.props.onError?.(error)
  }

  private retry = () => this.setState({ error: null })

  render() {
    if (!this.state.error) return this.props.children
    if (this.props.fallback) return this.props.fallback
    return (
      <div
        role="alert"
        style={{ height: '100%', width: '100%' }}
        className="flex flex-col items-center justify-center gap-1 text-center"
      >
        <span className="text-sm font-medium text-slate-600">图表暂不可用</span>
        <button
          type="button"
          onClick={this.retry}
          className="rounded text-[11px] text-brand-700 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1"
        >
          重试
        </button>
      </div>
    )
  }
}

/** 懒挂载默认占位：与迁移前逐字一致，避免零页面改动阶段出现视觉差。 */
function DefaultPlaceholder() {
  return (
    <div
      style={{
        height: '100%',
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: CHART_COLORS.canvas,
        borderRadius: '8px',
      }}
    >
      <div className="text-muted text-sm">加载中...</div>
    </div>
  )
}

export default function Chart({
  option,
  height = '350px',
  loading = false,
  onEvents,
  lazy = true,
  ariaLabel,
  emptyPlaceholder,
  onReady,
  onError,
}: ChartProps) {
  const chartRef = useRef<HTMLDivElement>(null)
  const instanceRef = useRef<InstanceType<typeof ReactEChartsCore> | null>(null)
  const [isVisible, setIsVisible] = useState(!lazy)
  const [hasRendered, setHasRendered] = useState(!lazy)

  useEffect(() => {
    if (!lazy) return

    const element = chartRef.current
    if (!element) return

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            setIsVisible(true)
            setHasRendered(true)
          }
        })
      },
      {
        rootMargin: '100px',
        threshold: 0.1,
      }
    )

    observer.observe(element)

    return () => {
      observer.disconnect()
    }
  }, [lazy])

  // 如果已经渲染过，保持渲染状态（避免图表消失）
  const shouldRender = isVisible || hasRendered

  // 容器宽变化（侧栏折叠 / 三栏布局）→ ECharts resize()；窗口 resize 不一定触发。
  useEffect(() => {
    if (!shouldRender) return
    const element = chartRef.current
    if (!element || typeof ResizeObserver === 'undefined') return

    let frame = 0
    let lastWidth = element.clientWidth
    let lastHeight = element.clientHeight

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height: h } = entry.contentRect
      if (width === lastWidth && h === lastHeight) return
      lastWidth = width
      lastHeight = h
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const instance = instanceRef.current?.getEchartsInstance()
        if (instance && !instance.isDisposed()) instance.resize()
      })
    })
    observer.observe(element)

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [shouldRender])

  return (
    <div
      ref={chartRef}
      style={{ height, width: '100%' }}
      role={ariaLabel ? 'img' : undefined}
      aria-label={ariaLabel}
    >
      {shouldRender ? (
        <ChartBoundary onError={onError}>
          <ReactEChartsCore
            ref={instanceRef}
            echarts={echarts}
            option={option}
            theme="ledger"
            style={{ height: '100%', width: '100%' }}
            showLoading={loading}
            notMerge={true}
            opts={{ renderer: 'canvas' }}
            onEvents={onEvents}
            onChartReady={onReady}
          />
        </ChartBoundary>
      ) : (
        emptyPlaceholder ?? <DefaultPlaceholder />
      )}
    </div>
  )
}