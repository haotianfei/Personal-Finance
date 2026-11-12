'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/**
 * 指标状态面（`GET /api/metrics/status`）的唯一取数入口。
 *
 * 指标条、域灯条、异动列表共用同一 query key：同页只发一次请求，切换页面走缓存；
 * `domains/events/summary` 与 `items` 同响应、同 meta（spec §5.1）。
 */
export const METRIC_STATUS_QUERY_KEY = ['metricStatus'] as const

export function useMetricStatus() {
  return useQuery({
    queryKey: METRIC_STATUS_QUERY_KEY,
    queryFn: () => api.getMetricStatus(),
    retry: 1,
  })
}