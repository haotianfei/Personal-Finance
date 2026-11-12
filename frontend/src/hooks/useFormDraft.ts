import { useState, useEffect, useCallback, useRef, useMemo } from 'react'

interface DraftData<T> {
  data: T
  timestamp: number
  formId: string
}

interface UseFormDraftOptions<T> {
  formId: string
  initialData: T
  expireDays?: number
}

interface UseFormDraftReturn<T> {
  data: T
  setData: (data: T) => void
  hasDraft: boolean
  isRestored: boolean
  restoreDraft: () => void
  clearDraft: () => void
  lastSavedAt: Date | null
}

// Debounce 函数
function debounce<Args extends unknown[]>(func: (...args: Args) => void, wait: number) {
  let timeout: NodeJS.Timeout | null = null
  return (...args: Args) => {
    if (timeout) clearTimeout(timeout)
    timeout = setTimeout(() => func(...args), wait)
  }
}

// 获取 localStorage 中的草稿
function getDraftFromStorage<T>(formId: string, expireDays: number): DraftData<T> | null {
  if (typeof window === 'undefined') return null
  
  try {
    const key = `form_draft_${formId}`
    const stored = localStorage.getItem(key)
    if (!stored) return null

    const draft: DraftData<T> = JSON.parse(stored)
    
    // 检查是否过期
    const expireTime = expireDays * 24 * 60 * 60 * 1000
    if (Date.now() - draft.timestamp > expireTime) {
      localStorage.removeItem(key)
      return null
    }

    return draft
  } catch {
    return null
  }
}

// 保存草稿到 localStorage
function saveDraftToStorage<T>(formId: string, data: T) {
  if (typeof window === 'undefined') return
  
  try {
    const key = `form_draft_${formId}`
    const draft: DraftData<T> = {
      data,
      timestamp: Date.now(),
      formId
    }
    localStorage.setItem(key, JSON.stringify(draft))
  } catch (error) {
    console.error('Failed to save draft:', error)
  }
}

// 清除 localStorage 中的草稿
function clearDraftFromStorage(formId: string) {
  if (typeof window === 'undefined') return
  
  try {
    const key = `form_draft_${formId}`
    localStorage.removeItem(key)
  } catch (error) {
    console.error('Failed to clear draft:', error)
  }
}

export function useFormDraft<T>({
  formId,
  initialData,
  expireDays = 7
}: UseFormDraftOptions<T>): UseFormDraftReturn<T> {
  const [data, setDataState] = useState<T>(initialData)
  const [hasDraft, setHasDraft] = useState(false)
  const [isRestored, setIsRestored] = useState(false)
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null)
  const draftDataRef = useRef<T | null>(null)

  // 初始化时检查是否有草稿
  useEffect(() => {
    const draft = getDraftFromStorage<T>(formId, expireDays)
    if (draft) {
      setHasDraft(true)
      draftDataRef.current = draft.data
      setLastSavedAt(new Date(draft.timestamp))
    }
  }, [formId, expireDays])

  // 自动保存草稿（debounce 1秒）
  const saveDraft = useMemo(
    () => debounce((currentData: T) => {
      saveDraftToStorage(formId, currentData)
      setLastSavedAt(new Date())
    }, 1000),
    [formId]
  )

  // 设置数据并自动保存
  const setData = useCallback((newData: T) => {
    setDataState(newData)
    saveDraft(newData)
  }, [saveDraft])

  // 恢复草稿
  const restoreDraft = useCallback(() => {
    if (draftDataRef.current) {
      setDataState(draftDataRef.current)
      setIsRestored(true)
      setHasDraft(false)
    }
  }, [])

  // 清除草稿
  const clearDraft = useCallback(() => {
    clearDraftFromStorage(formId)
    setHasDraft(false)
    setIsRestored(false)
    draftDataRef.current = null
  }, [formId])

  return {
    data,
    setData,
    hasDraft,
    isRestored,
    restoreDraft,
    clearDraft,
    lastSavedAt
  }
}
