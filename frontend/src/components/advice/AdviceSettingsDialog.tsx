'use client'

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Settings2 } from 'lucide-react'
import { api, newIdemKey } from '@/lib/api'
import type { AdvisorHorizon } from '@/types'
import { FormField, SelectField, fieldControlClass } from '@/components/form'
import { InlineError, LoadingState, ResponsiveDialog } from '@/components/ui'
import { adviceError } from './shared'

const HORIZON_OPTIONS = [
  { value: 'short', label: '短期' },
  { value: 'mid', label: '中期' },
  { value: 'long', label: '长期' },
]

export function AdviceSettingsDialog({ open, onClose, onSaved }: {
  open: boolean
  onClose: () => void
  onSaved?: () => void
}) {
  const queryClient = useQueryClient()
  const [autoEnabled, setAutoEnabled] = useState(false)
  const [cooldown, setCooldown] = useState('7')
  const [horizon, setHorizon] = useState<AdvisorHorizon>('mid')
  const [error, setError] = useState<string | null>(null)

  const settingsQuery = useQuery({
    queryKey: ['adviceSettings'],
    queryFn: api.getAdviceSettings,
    enabled: open,
  })

  useEffect(() => {
    if (!settingsQuery.data) return
    setAutoEnabled(settingsQuery.data.auto_enabled)
    setCooldown(String(settingsQuery.data.cooldown_days))
    setHorizon(settingsQuery.data.default_horizon)
    setError(null)
  }, [settingsQuery.data])

  const mutation = useMutation({
    mutationFn: () => api.updateAdviceSettings({
      auto_enabled: autoEnabled,
      cooldown_days: Number(cooldown),
      default_horizon: horizon,
    }, newIdemKey()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['adviceSettings'] })
      onSaved?.()
      onClose()
    },
    onError: (e) => setError(adviceError(e)),
  })

  const save = () => {
    const n = Number(cooldown)
    if (!Number.isInteger(n) || n < 0 || n > 365) {
      setError('冷却天数须为 0–365 的整数')
      return
    }
    setError(null)
    mutation.mutate()
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="建议设置"
      description="自动触发默认关闭；手动生成始终可用。"
      size="sm"
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={mutation.isPending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={save} disabled={mutation.isPending || settingsQuery.isLoading} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {mutation.isPending ? '保存中…' : '保存设置'}
          </button>
        </div>
      }
    >
      {settingsQuery.isLoading ? <LoadingState rows={3} /> : settingsQuery.isError ? (
        <InlineError>建议设置加载失败：{adviceError(settingsQuery.error)}</InlineError>
      ) : (
        <div className="space-y-5">
          {error && <InlineError>{error}</InlineError>}
          <div className="flex items-center justify-between gap-3 rounded-control border border-line bg-subtle p-3">
            <div className="min-w-0">
              <p className="text-body-strong text-ink">自动触发</p>
              <p className="mt-1 text-meta text-muted">关闭时不会自动生成建议；手动生成不受影响。</p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={autoEnabled}
              aria-label="自动触发建议"
              onClick={() => setAutoEnabled((v) => !v)}
              className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-control"
            >
              <span className={`relative inline-flex h-6 w-11 items-center rounded-pill ${autoEnabled ? 'bg-action-primary' : 'bg-line-strong'}`}>
                <span className={`inline-block h-5 w-5 transform rounded-pill bg-surface shadow-card transition-transform ${autoEnabled ? 'translate-x-[22px]' : 'translate-x-0.5'}`} />
              </span>
            </button>
          </div>
          <FormField label="冷却天数" hint="两次自动触发之间至少间隔的天数（0–365）">
            <input type="number" inputMode="numeric" min={0} max={365} value={cooldown} onChange={(e) => setCooldown(e.target.value)} className={fieldControlClass} />
          </FormField>
          <SelectField label="默认期限" value={horizon} onChange={(v) => setHorizon(v as AdvisorHorizon)} options={HORIZON_OPTIONS} />
          <div className="rounded-control border border-line p-3 text-meta text-muted">
            <p className="flex items-center gap-1.5"><Settings2 size={14} aria-hidden /> 过期天数：30 天（系统固定）</p>
            <p className="mt-1">上次自动触发：{settingsQuery.data?.last_auto_at ? settingsQuery.data.last_auto_at.slice(0, 16).replace('T', ' ') : '—'}</p>
          </div>
        </div>
      )}
    </ResponsiveDialog>
  )
}