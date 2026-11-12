'use client'

import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { APP_BRAND_TILE } from './nav-config'

/**
 * 「添加到主屏幕」引导（移动端、低调、可关闭、关闭后记忆）。
 *
 * 形态（修正遮挡问题后）：**入流式 slim 横幅**，渲染在移动顶栏之下、main 之上（layout.tsx）。
 *   - 占据真实布局空间 → 物理上不可能拦截/遮挡任何页面点按（旧版 fixed 悬浮在底部 Tab 之上，
 *     首访会盖住 /screenshot「下一步」、列表「加载更多」等底部主 CTA 一次点按）；
 *   - 随页面滚动自然离开视口，不打断阅读；
 *   - 不再延迟出现（入流元素延迟挂载会造成布局跳动），首访即见、关闭后记忆；
 * - standalone 显示模式（已从主屏打开）自动隐藏；
 * - iOS：给「分享 → 添加到主屏幕」路径（iOS 26 起主屏 web app 独立窗口 + 豁免 7 天存储清理）；
 * - Android：HTTP 下不承诺「可安装为应用」，只给浏览器菜单的加主屏路径并注明 HTTPS 前提；
 * - 桌面（≥lg）不渲染。
 */
const DISMISS_KEY = 'pwa_homescreen_hint_dismissed'

type Platform = 'ios' | 'android' | 'other'

function detectPlatform(): Platform {
  if (typeof navigator === 'undefined') return 'other'
  const ua = navigator.userAgent
  if (/iphone|ipad|ipod/i.test(ua)) return 'ios'
  if (/android/i.test(ua)) return 'android'
  return 'other'
}

function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  const mm = typeof window.matchMedia === 'function' ? window.matchMedia('(display-mode: standalone)') : null
  return !!mm?.matches || (navigator as { standalone?: boolean }).standalone === true
}

export default function HomeScreenHint() {
  const [visible, setVisible] = useState(false)
  const [platform, setPlatform] = useState<Platform>('other')

  useEffect(() => {
    if (isStandalone()) return
    let dismissed = false
    try {
      dismissed = localStorage.getItem(DISMISS_KEY) === '1'
    } catch {
      // 隐私模式读失败 → 当作未关闭
    }
    if (dismissed) return
    setPlatform(detectPlatform())
    setVisible(true)
  }, [])

  const dismiss = () => {
    setVisible(false)
    try {
      localStorage.setItem(DISMISS_KEY, '1')
    } catch {
      // 写失败则本次会话内不再弹出即可
    }
  }

  if (!visible) return null

  return (
    <div className="mx-3 mt-2 shrink-0 lg:hidden">
      <div className="flex items-start gap-2.5 rounded-card border border-line bg-surface/95 p-3 shadow-card backdrop-blur">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
          {APP_BRAND_TILE}
        </div>
        <p className="min-w-0 flex-1 text-meta leading-relaxed text-secondary">
          {platform === 'ios' ? (
            <>
              添加到主屏幕：点 Safari 的<span className="font-medium text-ink">分享</span>按钮，选
              <span className="font-medium text-ink">「添加到主屏幕」</span>
              ；之后独立窗口打开，也不易被浏览器清理。
            </>
          ) : platform === 'android' ? (
            <>
              加入主屏幕：浏览器菜单 →<span className="font-medium text-ink">「添加到主屏幕」</span>
              。当前为 HTTP，安装为独立应用需站点启用 HTTPS。
            </>
          ) : (
            <>在手机上打开本站并添加到主屏幕，可获得独立窗口的使用体验。</>
          )}
        </p>
        <button
          type="button"
          onClick={dismiss}
          aria-label="不再提示添加到主屏幕"
          className="pf-subtle-link -m-1 flex h-11 w-11 shrink-0 items-center justify-center rounded-control text-muted"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  )
}
