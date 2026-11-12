'use client'

import { useEffect, useState } from 'react'

/** 窄屏判定阈值：与 Tailwind md 断点一致（<768 视为窄屏） */
export const NARROW_CHART_BREAKPOINT = 768

/**
 * 图表视口自适应 hook。
 *
 * 与 app/page.tsx 既有的 windowWidth 探测逻辑同构（该文件只读参考、未改动），
 * 抽到共享位置供 analysis / allocation 等页复用，避免每页各写一份 resize 监听。
 *
 * SSR 首渲染回落桌面宽度，挂载后同步纠正——图表只在客户端渲染（Chart 组件懒加载），
 * 因此不存在水合错位问题。
 */
export function useChartResponsive(breakpoint: number = NARROW_CHART_BREAKPOINT) {
  const [windowWidth, setWindowWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1200
  )

  useEffect(() => {
    const handleResize = () => setWindowWidth(window.innerWidth)
    handleResize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  return { windowWidth, isNarrow: windowWidth < breakpoint }
}
