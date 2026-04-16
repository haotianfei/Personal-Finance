import type { Metadata } from 'next'
import './globals.css'
import Sidebar from '@/components/layout/Sidebar'
import Providers from '@/components/layout/Providers'

export const metadata: Metadata = {
  title: '个人资产管理系统',
  description: '个人资产管理与分析',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          <div className="flex min-h-screen">
            <Sidebar />
            <main className="flex-1 min-h-screen transition-all duration-200">
              <div className="p-6 max-w-[1400px] mx-auto">
                {children}
              </div>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  )
}
