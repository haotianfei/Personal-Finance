'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App, ConfigProvider, theme } from 'antd'
import { useState } from 'react'
import { antdTheme } from '@/styles/antd-theme'

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30 * 1000,
            retry: 1,
          },
        },
      })
  )

  return (
    <ConfigProvider theme={{ ...antdTheme, algorithm: theme.defaultAlgorithm }}>
      {/* App 提供 message/notification/modal 的上下文，避免静态调用失去主题与无障碍上下文 */}
      <App>
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      </App>
    </ConfigProvider>
  )
}