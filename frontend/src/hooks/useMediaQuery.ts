'use client'

import { useEffect, useLayoutEffect, useState } from 'react'

// SSR 下 useLayoutEffect 会告警；服务端模块使用 useEffect，客户端在绘制前同步纠正。
const useIsomorphicLayoutEffect =
  typeof window !== 'undefined' ? useLayoutEffect : useEffect

/**
 * matchMedia 的轻量封装：首帧用 `defaultValue` 保证 SSR 与水合一致，
 * 在浏览器绘制前用 layout effect 纠正。用于「宽度决定布局、能力决定交互」。
 */
export function useMediaQuery(query: string, defaultValue = false): boolean {
  const [matches, setMatches] = useState(defaultValue)

  useIsomorphicLayoutEffect(() => {
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [query])

  return matches
}

/** 1024–1279：侧栏强制 64，点击切换为 overlay；≥1280 才允许常驻展开。 */
export const COMPACT_DESKTOP_QUERY = '(min-width: 1024px) and (max-width: 1279.98px)'
export const HOVER_FINE_QUERY = '(hover: hover) and (pointer: fine)'