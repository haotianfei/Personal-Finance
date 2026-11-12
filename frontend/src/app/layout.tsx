import type { Metadata, Viewport } from 'next'
import './globals.css'
import Providers from '@/components/layout/Providers'
import AppShell from '@/components/layout/AppShell'
import { MobileNavProvider } from '@/components/layout/MobileNavContext'
import { color } from '@/styles/tokens'

export const metadata: Metadata = {
  title: '个人资产管理系统',
  description: '个人资产管理与分析',
  // PWA（HTTP 可落地部分）：图标与 iOS 主屏元数据；manifest 见 app/manifest.ts
  icons: {
    icon: [
      { url: '/icon-192.png', type: 'image/png', sizes: '192x192' },
      { url: '/icon-512.png', type: 'image/png', sizes: '512x512' },
    ],
    apple: [{ url: '/apple-touch-icon.png', sizes: '180x180', type: 'image/png' }],
  },
  appleWebApp: {
    capable: true,
    title: '个人资产管理',
    // 与深色顶栏衔接：状态栏透明、由顶栏的 safe-area 内边距让出刘海
    statusBarStyle: 'black-translucent',
  },
}

/**
 * Next 14 起 viewport 相关字段必须从 metadata 拆到独立的 viewport export。
 * viewportFit:'cover' 是 env(safe-area-inset-*) 生效的前提——移动顶栏、底部 Tab、
 * 抽屉页脚都靠它避开刘海与 iOS 手势条；themeColor 让移动端浏览器 UI 与深色顶栏同色。
 */
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  themeColor: color.bg.inverse,
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          <MobileNavProvider>
            <AppShell>{children}</AppShell>
          </MobileNavProvider>
        </Providers>
      </body>
    </html>
  )
}