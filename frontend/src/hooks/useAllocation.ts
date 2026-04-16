'use client'

import { useState, useCallback, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { message } from 'antd'
import type { AllocationTarget, AllocationAnalysis, AllocationAnalysisItem } from '@/types'

interface AutoAdjustedChild {
  child_id: string
  old_percent: string
  new_percent: string
}

// Hook for managing allocation targets
export function useAllocationTargets(dimension: string) {
  const queryClient = useQueryClient()

  const { data: targets, isLoading } = useQuery({
    queryKey: ['allocationTargets', dimension],
    queryFn: () => api.getAllocationTargets(dimension),
    enabled: !!dimension,
  })

  const createMutation = useMutation({
    mutationFn: api.createAllocationTarget,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['allocationTargets', dimension] })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<AllocationTarget> }) =>
      api.updateAllocationTarget(id, data),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['allocationTargets', dimension] })
      
      // Check if any children were auto-adjusted
      const autoAdjustedChildren = (data as unknown as { auto_adjusted_children?: AutoAdjustedChild[] })?.auto_adjusted_children
      if (autoAdjustedChildren && autoAdjustedChildren.length > 0) {
        const count = autoAdjustedChildren.length
        const details = autoAdjustedChildren.map(child => 
          `${child.child_id}: ${parseFloat(child.old_percent).toFixed(2)}% → ${parseFloat(child.new_percent).toFixed(2)}%`
        ).join('\n')
        message.success(`已自动调整 ${count} 个子级目标比例\n${details}`, 5)
      }
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteAllocationTarget,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['allocationTargets', dimension] })
    },
  })

  const saveTarget = useCallback(async (targetId: string, percent: number, parentId?: number | null) => {
    const existingTarget = targets?.find(t => t.target_id === targetId)

    if (existingTarget) {
      await updateMutation.mutateAsync({
        id: existingTarget.id,
        data: { target_percent: percent.toString() },
      })
    } else {
      await createMutation.mutateAsync({
        dimension: dimension as 'fund_type' | 'liquidity_rating' | 'account',
        target_id: targetId,
        target_percent: percent.toString(),
        parent_id: parentId,
      })
    }
    // Invalidate both targets and analysis queries after save
    await queryClient.invalidateQueries({ queryKey: ['allocationTargets', dimension] })
    await queryClient.invalidateQueries({ queryKey: ['allocationAnalysis', dimension] })
  }, [targets, dimension, createMutation, updateMutation, queryClient])

  return {
    targets,
    isLoading,
    saveTarget,
    deleteTarget: deleteMutation.mutate,
    isSaving: createMutation.isPending || updateMutation.isPending,
  }
}

// Hook for allocation analysis
export function useAllocationAnalysis(dimension: string, snapshotDate?: string) {
  const { data: analysis, isLoading, error } = useQuery<AllocationAnalysis>({
    queryKey: ['allocationAnalysis', dimension, snapshotDate],
    queryFn: () => api.getAllocationAnalysis(dimension, snapshotDate),
    enabled: !!dimension,
  })

  return {
    analysis,
    isLoading,
    error,
  }
}

// Hook for allocation validation
export function useAllocationValidation() {
  const validateTarget = useCallback(async (
    dimension: string,
    targetId: string,
    targetPercent: number,
    parentId?: number
  ): Promise<{ valid: boolean; message: string }> => {
    try {
      const result = await api.validateAllocation(dimension, targetId, targetPercent, parentId)
      return result
    } catch (error) {
      return { valid: false, message: '验证失败' }
    }
  }, [])

  return { validateTarget }
}

// Hook for calculating allocation statistics
// Only root level targets (parent_id is null) are counted in the total
export function useAllocationStats(targets: AllocationTarget[] | undefined) {
  return useMemo(() => {
    if (!targets) {
      return {
        totalAllocated: 0,
        remainingPercent: 100,
        isOverAllocated: false,
        isFullyAllocated: false,
      }
    }

    // Only count root level targets (parent_id is null)
    const rootTargets = targets.filter(t => t.parent_id === null)
    const totalAllocated = rootTargets.reduce((sum, t) => sum + parseFloat(t.target_percent), 0)
    const remainingPercent = Math.max(100 - totalAllocated, 0)

    return {
      totalAllocated,
      remainingPercent,
      isOverAllocated: totalAllocated > 100,
      isFullyAllocated: totalAllocated === 100,
    }
  }, [targets])
}

// Hook for managing editing state
export function useAllocationEditing() {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')

  const startEditing = useCallback((id: string, currentValue: string | null) => {
    setEditingId(id)
    setEditValue(currentValue ? parseFloat(currentValue).toFixed(2) : '')
  }, [])

  const stopEditing = useCallback(() => {
    setEditingId(null)
    setEditValue('')
  }, [])

  return {
    editingId,
    editValue,
    setEditValue,
    startEditing,
    stopEditing,
  }
}

// Helper function to flatten analysis items
export function flattenAnalysisItems(items: AllocationAnalysisItem[]): AllocationAnalysisItem[] {
  const result: AllocationAnalysisItem[] = []
  for (const item of items) {
    result.push(item)
    if (item.children?.length) {
      result.push(...flattenAnalysisItems(item.children))
    }
  }
  return result
}

// Helper function to calculate siblings total for an item
export function calculateSiblingsTotal(
  items: AllocationAnalysisItem[],
  targetId: string,
  parentId: string | null
): number {
  const siblings = items.filter(item =>
    item.id !== targetId && item.parent_id === parentId
  )
  return siblings.reduce((sum, item) => {
    return sum + (item.target_percent ? parseFloat(item.target_percent) : 0)
  }, 0)
}

// Helper function to find item by ID in tree
export function findItemById(
  items: AllocationAnalysisItem[],
  id: string
): AllocationAnalysisItem | null {
  for (const item of items) {
    if (item.id === id) return item
    if (item.children?.length) {
      const found = findItemById(item.children, id)
      if (found) return found
    }
  }
  return null
}
