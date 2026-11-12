'use client'

import { createContext, useCallback, useContext, useMemo, useState } from 'react'

/**
 * 「更多」抽屉的开合状态。
 *
 * 顶栏汉堡（MobileTopBar）与底部 Tab 的「更多」（MobileTabBar）是两个不同 DOM 位置的触发点，
 * 但打开的是同一个抽屉（MobileNavDrawer，渲染在 layout.tsx 里、位于横向裁剪容器之外），
 * 因此状态提到 context。桌面端这些组件都不渲染（lg:hidden），context 只是一层空转。
 */
interface MobileNavValue {
  open: boolean
  openNav: () => void
  closeNav: () => void
}

// 兜底值提到模块级，避免每次渲染新建对象导致消费方无谓重渲染
const FALLBACK: MobileNavValue = {
  open: false,
  openNav: () => {},
  closeNav: () => {},
}

const MobileNavContext = createContext<MobileNavValue>(FALLBACK)

export function MobileNavProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const openNav = useCallback(() => setOpen(true), [])
  const closeNav = useCallback(() => setOpen(false), [])
  const value = useMemo(() => ({ open, openNav, closeNav }), [open, openNav, closeNav])

  return <MobileNavContext.Provider value={value}>{children}</MobileNavContext.Provider>
}

export function useMobileNav(): MobileNavValue {
  return useContext(MobileNavContext)
}
