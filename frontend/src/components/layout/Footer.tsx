'use client'

import Link from 'next/link'
import { Info } from 'lucide-react'
import { APP_VERSION } from '@/lib/version'


/**
 * 桌面页脚（≥1024）。
 *
 * 内容列内部的 sticky 底栏：不横跨侧栏、不遮挡侧栏折叠按钮；
 * 占真实布局空间，滚到页末不遮住最后一条内容。移动端由底部 Tab 承担导航，
 * 仓库地址/版本号搬到抽屉页脚，信息不丢失。
 */
export default function Footer() {
  return (
    <footer className="sticky bottom-0 z-sticky hidden min-h-12 shrink-0 items-center gap-4 border-t border-line bg-subtle px-6 pb-[var(--pf-safe-bottom)] pt-1 lg:flex">
      <div className="flex min-w-0 flex-1 justify-start">
        <Link
          href="/settings/about"
          className="pf-subtle-link flex min-h-[44px] min-w-0 items-center gap-2 text-meta text-muted"
        >
          <Info size={16} className="shrink-0" aria-hidden />
          <span className="truncate">关于与诊断</span>
        </Link>
      </div>

      <div className="shrink-0 px-4 text-center text-meta text-muted">
        © 2026 郝天飞 (HaoTianfei)
      </div>

      <div className="flex flex-1 justify-end">
        <div className="pf-num shrink-0 text-meta text-muted">v{APP_VERSION}</div>
      </div>
    </footer>
  )
}