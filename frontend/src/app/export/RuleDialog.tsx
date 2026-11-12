'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { ResponsiveDialog } from '@/components/ui'
import { FormField, SelectField, fieldControlClass } from '@/components/form'
import type { AutoExportRule, AutoExportRuleCreate } from '@/types'
import { CRON_TEMPLATES } from './constants'

const inputClass = fieldControlClass

export function RuleDialog({
  open,
  rule,
  pending,
  onClose,
  onSave,
}: {
  open: boolean
  rule: AutoExportRule | null
  pending: boolean
  onClose: () => void
  onSave: (values: AutoExportRuleCreate) => void
}) {
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [format, setFormat] = useState('csv')
  const [filename, setFilename] = useState('')
  const [errors, setErrors] = useState<{ name?: string; cron?: string }>({})
  const [cronHint, setCronHint] = useState('')
  const [cronValid, setCronValid] = useState(true)

  useEffect(() => {
    if (!open) return
    setName(rule?.name ?? '')
    setCron(rule?.cron_expression ?? '')
    setFormat(rule?.export_format ?? 'csv')
    setFilename(rule?.filename_template ?? '')
    setErrors({})
    setCronHint('')
    setCronValid(true)
  }, [open, rule])

  useEffect(() => {
    if (!open || !cron) return
    let cancelled = false
    const timer = setTimeout(async () => {
      try {
        const result = await api.validateCronExpression(cron)
        if (cancelled) return
        setCronValid(result.valid)
        setCronHint(result.description)
      } catch {
        if (!cancelled) {
          setCronValid(false)
          setCronHint('Cron 表达式校验失败')
        }
      }
    }, 350)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [cron, open])

  const submit = () => {
    const next: { name?: string; cron?: string } = {}
    if (!name.trim()) next.name = '请输入规则名称'
    if (!cron.trim()) next.cron = '请输入 Cron 表达式'
    else if (!cronValid) next.cron = 'Cron 表达式无效'
    setErrors(next)
    if (Object.keys(next).length > 0) return
    onSave({
      name: name.trim(),
      cron_expression: cron.trim(),
      export_format: format,
      filename_template: filename.trim() || null,
    })
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={rule ? '编辑自动导出规则' : '创建自动导出规则'}
      description="按 Cron 表达式定时生成导出文件；规则只影响后续自动执行。"
      size="md"
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {pending ? '保存中…' : rule ? '保存修改' : '创建规则'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <FormField label="规则名称" required error={errors.name}>
          <input
            type="text"
            value={name}
            onChange={(e) => { setName(e.target.value); if (errors.name) setErrors({ ...errors, name: undefined }) }}
            placeholder="如：每日备份"
            className={inputClass}
          />
        </FormField>
        <FormField label="Cron 表达式" required error={errors.cron} hint={cron && cronHint ? (cronValid ? `✓ ${cronHint}` : `✗ ${cronHint}`) : '分钟 小时 日 月 星期'}>
          <input
            type="text"
            value={cron}
            onChange={(e) => { setCron(e.target.value); if (errors.cron) setErrors({ ...errors, cron: undefined }) }}
            placeholder="0 2 * * *"
            className={inputClass}
          />
        </FormField>
        <div className="space-y-1.5">
          <p className="text-meta font-medium text-secondary">常用模板</p>
          <div className="flex flex-wrap gap-2">
            {CRON_TEMPLATES.map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => { setCron(item.value); setErrors((prev) => ({ ...prev, cron: undefined })) }}
                className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-meta text-ink"
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        <SelectField label="导出格式" value={format} onChange={setFormat} options={[{ value: 'csv', label: 'CSV' }, { value: 'json', label: 'JSON' }]} />
        <FormField label="文件名模板" hint="支持变量：{date} 日期，{timestamp} 时间戳">
          <input type="text" value={filename} onChange={(e) => setFilename(e.target.value)} placeholder="backup_{date}.csv" className={inputClass} />
        </FormField>
      </div>
    </ResponsiveDialog>
  )
}
