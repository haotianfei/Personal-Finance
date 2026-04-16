import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAllocationStats, flattenAnalysisItems, findItemById } from '../useAllocation'
import type { AllocationAnalysisItem, AllocationTarget } from '@/types'

// Mock the API
vi.mock('@/lib/api', () => ({
  api: {
    getAllocationTargets: vi.fn(),
    getAllocationAnalysis: vi.fn(),
    validateAllocation: vi.fn(),
  },
}))

const createTestQueryClient = () => new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
})

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={createTestQueryClient()}>
    {children}
  </QueryClientProvider>
)

describe('useAllocationStats', () => {
  it('should calculate stats correctly with empty targets', () => {
    const { result } = renderHook(() => useAllocationStats(undefined), { wrapper })
    
    expect(result.current.totalAllocated).toBe(0)
    expect(result.current.remainingPercent).toBe(100)
    expect(result.current.isOverAllocated).toBe(false)
    expect(result.current.isFullyAllocated).toBe(false)
  })

  it('should calculate stats correctly with targets', () => {
    const targets: AllocationTarget[] = [
      { id: 1, dimension: 'fund_type', target_id: '1', parent_id: null, target_percent: '30.00', created_at: '', updated_at: '' },
      { id: 2, dimension: 'fund_type', target_id: '2', parent_id: null, target_percent: '40.00', created_at: '', updated_at: '' },
    ]
    
    const { result } = renderHook(() => useAllocationStats(targets), { wrapper })
    
    expect(result.current.totalAllocated).toBe(70)
    expect(result.current.remainingPercent).toBe(30)
    expect(result.current.isOverAllocated).toBe(false)
    expect(result.current.isFullyAllocated).toBe(false)
  })

  it('should detect over-allocation', () => {
    const targets: AllocationTarget[] = [
      { id: 1, dimension: 'fund_type', target_id: '1', parent_id: null, target_percent: '60.00', created_at: '', updated_at: '' },
      { id: 2, dimension: 'fund_type', target_id: '2', parent_id: null, target_percent: '50.00', created_at: '', updated_at: '' },
    ]
    
    const { result } = renderHook(() => useAllocationStats(targets), { wrapper })
    
    expect(result.current.totalAllocated).toBe(110)
    expect(result.current.remainingPercent).toBe(0)
    expect(result.current.isOverAllocated).toBe(true)
    expect(result.current.isFullyAllocated).toBe(false)
  })

  it('should detect fully allocated', () => {
    const targets: AllocationTarget[] = [
      { id: 1, dimension: 'fund_type', target_id: '1', parent_id: null, target_percent: '60.00', created_at: '', updated_at: '' },
      { id: 2, dimension: 'fund_type', target_id: '2', parent_id: null, target_percent: '40.00', created_at: '', updated_at: '' },
    ]
    
    const { result } = renderHook(() => useAllocationStats(targets), { wrapper })
    
    expect(result.current.totalAllocated).toBe(100)
    expect(result.current.remainingPercent).toBe(0)
    expect(result.current.isOverAllocated).toBe(false)
    expect(result.current.isFullyAllocated).toBe(true)
  })
})

describe('flattenAnalysisItems', () => {
  it('should flatten nested items', () => {
    const items: AllocationAnalysisItem[] = [
      {
        id: '1',
        name: 'Parent',
        parent_id: null,
        level: 0,
        target_percent: '50.00',
        actual_percent: '45.00',
        actual_amount: '45000',
        deviation: '-5.00',
        deviation_percent: '-10.00',
        deviation_amount: '5000',
        recommendation: '建议增持 5.0%',
        children: [
          {
            id: '2',
            name: 'Child',
            parent_id: '1',
            level: 1,
            target_percent: '20.00',
            actual_percent: '15.00',
            actual_amount: '15000',
            deviation: '-5.00',
            deviation_percent: '-25.00',
            deviation_amount: '5000',
            recommendation: '建议增持 5.0%',
            children: [],
          },
        ],
      },
    ]
    
    const flattened = flattenAnalysisItems(items)
    
    expect(flattened).toHaveLength(2)
    expect(flattened[0].id).toBe('1')
    expect(flattened[1].id).toBe('2')
  })

  it('should handle empty array', () => {
    const flattened = flattenAnalysisItems([])
    expect(flattened).toHaveLength(0)
  })
})

describe('findItemById', () => {
  const items: AllocationAnalysisItem[] = [
    {
      id: '1',
      name: 'Parent',
      parent_id: null,
      level: 0,
      target_percent: '50.00',
      actual_percent: '45.00',
      actual_amount: '45000',
      deviation: '-5.00',
      deviation_percent: '-10.00',
      deviation_amount: '5000',
      recommendation: '建议增持 5.0%',
      children: [
        {
          id: '2',
          name: 'Child',
          parent_id: '1',
          level: 1,
          target_percent: '20.00',
          actual_percent: '15.00',
          actual_amount: '15000',
          deviation: '-5.00',
          deviation_percent: '-25.00',
          deviation_amount: '5000',
          recommendation: '建议增持 5.0%',
          children: [],
        },
      ],
    },
  ]

  it('should find item at root level', () => {
    const found = findItemById(items, '1')
    expect(found).not.toBeNull()
    expect(found?.name).toBe('Parent')
  })

  it('should find nested item', () => {
    const found = findItemById(items, '2')
    expect(found).not.toBeNull()
    expect(found?.name).toBe('Child')
  })

  it('should return null for non-existent item', () => {
    const found = findItemById(items, '999')
    expect(found).toBeNull()
  })
})
