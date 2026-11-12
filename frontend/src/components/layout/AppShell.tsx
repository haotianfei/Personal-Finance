'use client'

import { useEffect } from 'react'
import Sidebar from './Sidebar'
import MobileTopBar from './MobileTopBar'
import MobileTabBar from './MobileTabBar'
import MobileNavDrawer from './MobileNavDrawer'
import Footer from './Footer'
import HomeScreenHint from './HomeScreenHint'

/**
 * 应用壳层：单一结构，平台差异只落在断点与安全区。
 *
 * - ≥1280：侧栏 224 / 折叠 64；1024–1279：自动 64 + overlay；<1024：顶栏 56 + 底部 Tab 56 + 抽屉。
 * - 跳过链接在壳层之外，键盘首个 Tab 可见；`main#main` 可被跳过链接聚焦。
 * - 四边安全区：顶栏/底栏/抽屉/内容列各自处理；粘性偏移统一读 `--pf-header-h`。
 * - `min-h-dvh` + `--pf-viewport-h`（visualViewport）处理移动浏览器动态工具栏与软键盘。
 * - 抽屉与 overlay 渲染在壳层之外，背景通过 `inert` 隔离。
 */
export default function AppShell({ children }: { children: React.ReactNode }) {
  // 软键盘/动态工具栏：写入 --pf-viewport-h，sheet/壳层可读取，避免 100vh 被键盘截断
  useEffect(() => {
    const viewport = window.visualViewport
    if (!viewport) return
    const update = () => {
      document.documentElement.style.setProperty('--pf-viewport-h', `${viewport.height}px`)
    }
    update()
    viewport.addEventListener('resize', update)
    viewport.addEventListener('scroll', update)
    return () => {
      viewport.removeEventListener('resize', update)
      viewport.removeEventListener('scroll', update)
    }
  }, [])

  return (
    <>
      <div id="app-shell" className="pf-min-h-dvh flex overflow-x-clip">
        {/* skip link 放在壳层内部：抽屉/overlay 打开时随背景一起 inert */}
        <a href="#main" className="pf-skip-link">
          跳到主要内容
        </a>

        <Sidebar />

        {/* min-w-0：flex item 默认 min-width:auto，会被内部宽表顶开；归零后溢出只留在内容列内部 */}
        <div className="pf-min-h-dvh flex min-w-0 flex-1 flex-col">
          <MobileTopBar />
          {/* 入流式安装引导：顶栏之下、main 之上，占真实布局空间 → 不拦截任何点按 */}
          <HomeScreenHint />
          <main id="main" tabIndex={-1} className="pf-main flex-1 outline-none">
            {children}
          </main>
          <Footer />
          <MobileTabBar />
        </div>
      </div>

      {/*
        抽屉放在横向裁剪容器之外：fixed 定位不受祖先 overflow 影响，
        层级也不与页面内弹窗纠缠。与顶栏汉堡、底部「更多」共用 context 状态。
      */}
      <MobileNavDrawer />
    </>
  )
}