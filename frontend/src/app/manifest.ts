import type { MetadataRoute } from 'next'

/**
 * PWA manifest（HTTP 下可落地部分）。
 *
 * 背景（lib-1 调研）：iOS 26 起任意网站加主屏默认独立窗口打开、主屏 web app 豁免
 * 7 天存储清理；Android Chrome 的「安装促销」需 HTTPS，但合规 manifest 仍是前置条件。
 * Service Worker / 离线 / 推送依赖 HTTPS，本轮不做。
 *
 * theme_color 与移动顶栏/viewport themeColor 同色（brand-950），background_color 与
 * body 底色一致，启动过渡不闪白。
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: '个人资产管理',
    short_name: '资产管理',
    description: '个人资产管理与分析',
    lang: 'zh-CN',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    theme_color: '#0f172a',
    background_color: '#f1f5f9',
    icons: [
      { src: '/icon-192.png', sizes: '192x192', type: 'image/png' },
      { src: '/icon-512.png', sizes: '512x512', type: 'image/png' },
      {
        src: '/icon-maskable-512.png',
        sizes: '512x512',
        type: 'image/png',
        purpose: 'maskable',
      },
    ],
  }
}
