'use client'

import { useEffect, useRef, useState } from 'react'
import dayjs from 'dayjs'
import { ResponsiveDialog } from '@/components/ui'
import { AmountField, DateField, FormField, SelectField, fieldControlClass } from '@/components/form'
import { cn } from '@/lib/utils'
import type {
  Institution, ExpenseCategory, ExpenseCycle, ExpenseRecord, ExpenseRecordCreate,
  ExpenseType, FixedExpenseTemplate, FixedExpenseTemplateCreate,
} from '@/types'
import { CYCLE_OPTIONS, EXPENSE_TYPE_OPTIONS } from './constants'

export function ExpenseDialog({
  open, record, categories, accounts, pending, onClose, onSubmit,
}: {
  open: boolean
  record: ExpenseRecord | null
  categories: ExpenseCategory[]
  accounts: Institution[]
  pending: boolean
  onClose: () => void
  onSubmit: (data: ExpenseRecordCreate) => void
}) {
  const nameRef = useRef<HTMLInputElement>(null)
  const [form, setForm] = useState({
    expense_type: 'fixed' as ExpenseType,
    expense_date: '',
    expense_name: '',
    amount: '',
    category_id: '',
    account_id: '',
    note: '',
  })
  const [errors, setErrors] = useState<{ date?: string; name?: string; amount?: string }>({})

  useEffect(() => {
    if (!open) return
    setForm({
      expense_type: record?.expense_type ?? 'fixed',
      expense_date: record?.expense_date ?? dayjs().format('YYYY-MM-DD'),
      expense_name: record?.expense_name ?? '',
      amount: record?.amount ?? '',
      category_id: record?.category_id ? String(record.category_id) : '',
      account_id: record?.account_id ? String(record.account_id) : '',
      note: record?.note ?? '',
    })
    setErrors({})
  }, [open, record])

  const submit = () => {
    const next: { date?: string; name?: string; amount?: string } = {}
    if (!form.expense_date) next.date = '请选择支出日期'
    if (!form.expense_name.trim()) next.name = '请输入支出名称'
    const amount = Number(form.amount)
    if (!form.amount || !Number.isFinite(amount) || amount <= 0) next.amount = '金额必须大于 0'
    setErrors(next)
    if (Object.keys(next).length > 0) return
    onSubmit({
      expense_date: form.expense_date,
      expense_name: form.expense_name.trim(),
      amount: amount.toFixed(2),
      expense_type: form.expense_type,
      category_id: form.category_id ? Number(form.category_id) : null,
      account_id: form.account_id ? Number(form.account_id) : null,
      note: form.note.trim() || null,
    })
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={record ? '编辑支出' : '新增支出'}
      description="支出为流水，不参与净值计算。"
      size="md"
      initialFocusRef={nameRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {pending ? '保存中…' : record ? '保存修改' : '创建'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <SelectField label="支出类型" value={form.expense_type} onChange={(value) => setForm({ ...form, expense_type: value as ExpenseType })} options={EXPENSE_TYPE_OPTIONS} required />
          <DateField label="日期" value={form.expense_date} onChange={(value) => setForm({ ...form, expense_date: value })} required error={errors.date} />
        </div>
        <FormField label="支出名称" required error={errors.name}>
          <input
            ref={nameRef}
            type="text"
            value={form.expense_name}
            onChange={(e) => { setForm({ ...form, expense_name: e.target.value }); if (errors.name) setErrors({ ...errors, name: undefined }) }}
            placeholder="如：示例房贷 9 月还款"
            className={cn(fieldControlClass, errors.name && 'border-status-critical')}
          />
        </FormField>
        <AmountField label="金额" required value={form.amount} onChange={(value) => { setForm({ ...form, amount: value }); if (errors.amount) setErrors({ ...errors, amount: undefined }) }} error={errors.amount} />
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <SelectField label="分类（可选）" value={form.category_id} onChange={(value) => setForm({ ...form, category_id: value })} placeholder="未分类" options={categories.map((c) => ({ value: String(c.id), label: c.name }))} />
          <SelectField label="支出账户（可选）" value={form.account_id} onChange={(value) => setForm({ ...form, account_id: value })} placeholder="不指定" options={accounts.map((a) => ({ value: String(a.id), label: a.name }))} />
        </div>
        <FormField label="备注（可选）">
          <textarea value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} rows={2} placeholder="补充说明，不参与统计" className={cn(fieldControlClass, 'min-h-[80px] resize-none py-2')} />
        </FormField>
      </div>
    </ResponsiveDialog>
  )
}

export function TemplateDialog({
  open, record, categories, accounts, pending, onClose, onSubmit,
}: {
  open: boolean
  record: FixedExpenseTemplate | null
  categories: ExpenseCategory[]
  accounts: Institution[]
  pending: boolean
  onClose: () => void
  onSubmit: (data: FixedExpenseTemplateCreate) => void
}) {
  const nameRef = useRef<HTMLInputElement>(null)
  const [form, setForm] = useState({
    name: '', amount: '', cycle: 'monthly' as ExpenseCycle, start_date: '', end_date: '',
    category_id: '', account_id: '', note: '', active: true,
  })
  const [errors, setErrors] = useState<{ name?: string; amount?: string; start?: string; end?: string }>({})

  useEffect(() => {
    if (!open) return
    setForm({
      name: record?.name ?? '',
      amount: record?.amount ?? '',
      cycle: record?.cycle ?? 'monthly',
      start_date: record?.start_date ?? dayjs().format('YYYY-MM-DD'),
      end_date: record?.end_date ?? '',
      category_id: record?.category_id ? String(record.category_id) : '',
      account_id: record?.account_id ? String(record.account_id) : '',
      note: record?.note ?? '',
      active: record?.active ?? true,
    })
    setErrors({})
  }, [open, record])

  const submit = () => {
    const next: { name?: string; amount?: string; start?: string; end?: string } = {}
    if (!form.name.trim()) next.name = '请输入项目名称'
    const amount = Number(form.amount)
    if (!form.amount || !Number.isFinite(amount) || amount <= 0) next.amount = '金额必须大于 0'
    if (!form.start_date) next.start = '请选择开始日期'
    if (form.end_date && form.end_date < form.start_date) next.end = '结束日期不能早于开始日期'
    setErrors(next)
    if (Object.keys(next).length > 0) return
    onSubmit({
      name: form.name.trim(),
      amount: amount.toFixed(2),
      cycle: form.cycle,
      start_date: form.start_date,
      end_date: form.end_date || null,
      category_id: form.category_id ? Number(form.category_id) : null,
      account_id: form.account_id ? Number(form.account_id) : null,
      note: form.note.trim() || null,
      active: form.active,
    })
  }

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={record ? '编辑固定支出项目' : '新增固定支出项目'}
      description="维护周期性支出项目，可在支出口流水一键生成当月记录。"
      size="md"
      initialFocusRef={nameRef}
      footer={
        <div className="flex flex-col gap-2 tablet:flex-row tablet:justify-end">
          <button type="button" onClick={onClose} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control border border-line-strong bg-surface px-4 text-body-strong text-ink disabled:opacity-50">取消</button>
          <button type="button" onClick={submit} disabled={pending} className="inline-flex min-h-[44px] items-center justify-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white disabled:opacity-50">
            {pending ? '保存中…' : record ? '保存修改' : '创建'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
          <FormField label="项目名称" required error={errors.name}>
            <input ref={nameRef} type="text" value={form.name} onChange={(e) => { setForm({ ...form, name: e.target.value }); if (errors.name) setErrors({ ...errors, name: undefined }) }} placeholder="如：示例物业费" className={cn(fieldControlClass, errors.name && 'border-status-critical')} />
          </FormField>
          <AmountField label="预期金额" required value={form.amount} onChange={(value) => { setForm({ ...form, amount: value }); if (errors.amount) setErrors({ ...errors, amount: undefined }) }} error={errors.amount} />
          <SelectField label="周期" value={form.cycle} onChange={(value) => setForm({ ...form, cycle: value as ExpenseCycle })} options={CYCLE_OPTIONS} required />
          <DateField label="开始日期" value={form.start_date} onChange={(value) => setForm({ ...form, start_date: value })} required error={errors.start} />
          <DateField label="结束日期（可选）" value={form.end_date} onChange={(value) => setForm({ ...form, end_date: value })} error={errors.end} hint="留空表示长期有效" />
          <SelectField label="分类（可选）" value={form.category_id} onChange={(value) => setForm({ ...form, category_id: value })} placeholder="未分类" options={categories.map((c) => ({ value: String(c.id), label: c.name }))} />
          <SelectField label="支出账户（可选）" value={form.account_id} onChange={(value) => setForm({ ...form, account_id: value })} placeholder="不指定" options={accounts.map((a) => ({ value: String(a.id), label: a.name }))} />
        </div>
        <FormField label="备注（可选）">
          <textarea value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} rows={2} placeholder="补充说明，不参与统计" className={cn(fieldControlClass, 'min-h-[80px] resize-none py-2')} />
        </FormField>
        <label className="flex min-h-[44px] items-center gap-2 text-body text-secondary">
          <input type="checkbox" checked={form.active} onChange={(e) => setForm({ ...form, active: e.target.checked })} className="h-5 w-5 accent-[var(--pf-color-action-primary)]" />
          启用（参与一键生成）
        </label>
      </div>
    </ResponsiveDialog>
  )
}
