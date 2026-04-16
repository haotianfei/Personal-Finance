'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider, theme } from 'antd'
import { useState } from 'react'

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
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#3b82f6',
          colorInfo: '#3b82f6',
          colorSuccess: '#10b981',
          colorWarning: '#f59e0b',
          colorError: '#ef4444',
          colorTextBase: '#1e293b',
          colorBgBase: '#ffffff',
          borderRadius: 6,
          wireframe: false,
        },
        components: {
          Table: {
            headerBg: '#f8fafc',
            headerColor: '#1e293b',
            rowHoverBg: '#f1f5f9',
            borderColor: '#e2e8f0',
          },
          Button: {
            borderRadius: 6,
          },
          Input: {
            borderRadius: 6,
          },
          Select: {
            borderRadius: 6,
          },
          Card: {
            borderRadius: 8,
          },
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    </ConfigProvider>
  )
}
