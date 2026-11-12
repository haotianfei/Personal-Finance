'use client'

/**
 * 图表渲染环境（design §5.4）：容器宽 / 粗指针 / reduced-motion。
 *
 * 判定纪律：
 * - 图表密度看**容器宽**，不看窗口宽；`< 560px` 为 `dense`；
 * - 容器宽为 0（隐藏 tab / 折叠面板）时沿用上次宽度，再退回窗口宽，避免 0 宽 option；
 * - 页面布局断点仍用既有的 `useChartResponsive`（768 断点），两者不混。
 *
 * 不判定业务、不取数。
 */
import { useEffect, useRef, useState, type RefObject } from 'react'
import type { ChartRenderContext } from './types'

/** 密集布局阈值（px）。 */
export const DENSE_MAX_WIDTH = 560

export interface ChartEnvironment extends ChartRenderContext {
  /** 实测容器宽；0 表示尚未测得（SSR/隐藏）。 */
  width: number
}

function prefers(query: string): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(query).matches
}

/**
 * @param ref 画布容器（通常是 ChartFrame 的 body）。省略时只用窗口宽判密。
 */
export function useChartEnvironment(ref?: RefObject<HTMLElement | null>): ChartEnvironment {
  const [width, setWidth] = useState(0)
  const [coarse, setCoarse] = useState(false)
  const [reducedMotion, setReducedMotion] = useState(false)
  const lastWidth = useRef(0)

  useEffect(() => {
    const element = ref?.current
    if (!element) {
      // 无容器可测：退回窗口宽（仅影响密度判定，不影响布局）
      const onWindow = () => setWidth(window.innerWidth)
      onWindow()
      window.addEventListener('resize', onWindow)
      return () => window.removeEventListener('resize', onWindow)
    }

    const resolve = (measured: number) => {
      if (measured > 0) {
        lastWidth.current = measured
        return measured
      }
      return lastWidth.current || window.innerWidth
    }

    const apply = (measured: number) => {
      const next = resolve(measured)
      setWidth((prev) => (prev === next ? prev : next))
    }

    apply(element.getBoundingClientRect().width)

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry) apply(entry.contentRect.width)
    })
    observer.observe(element)

    return () => observer.disconnect()
  }, [ref])

  useEffect(() => {
    const coarseQuery = '(pointer: coarse)'
    const motionQuery = '(prefers-reduced-motion: reduce)'
    const sync = () => {
      setCoarse(prefers(coarseQuery))
      setReducedMotion(prefers(motionQuery))
    }
    sync()

    const coarseMq = window.matchMedia?.(coarseQuery)
    const motionMq = window.matchMedia?.(motionQuery)
    coarseMq?.addEventListener?.('change', sync)
    motionMq?.addEventListener?.('change', sync)
    return () => {
      coarseMq?.removeEventListener?.('change', sync)
      motionMq?.removeEventListener?.('change', sync)
    }
  }, [])

  return {
    width,
    dense: width > 0 && width < DENSE_MAX_WIDTH,
    coarse,
    reducedMotion,
  }
}