'use client'

import { Github } from 'lucide-react'

export default function Footer() {
  return (
    <footer className="h-12 bg-slate-50 border-t border-slate-200 flex items-center px-6">
      {/* 左侧：GitHub 链接 */}
      <div className="flex-1 flex justify-start">
        <a
          href="https://github.com/haotianfei/Personal-Finance"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 text-slate-500 hover:text-slate-700 transition-colors"
        >
          <Github size={18} />
          <span className="text-sm hidden sm:inline">GitHub：https://github.com/haotianfei/Personal-Finance</span>
        </a>
      </div>

      {/* 中间：版权信息 */}
      <div className="text-sm text-slate-500 text-center">
        © 2026 郝天飞 (HaoTianfei)
      </div>

      {/* 右侧：版本号 */}
      <div className="flex-1 flex justify-end">
        <div className="text-sm text-slate-400">
          v1.4.0
        </div>
      </div>
    </footer>
  )
}
