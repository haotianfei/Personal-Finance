'use client'

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DatePicker, Tag, message } from 'antd'
import { Check, AlertTriangle } from 'lucide-react'
import dayjs from 'dayjs'
import type { Dayjs } from 'dayjs'
import { api } from '@/lib/api'
import { AmountInput } from '@/components/form/AmountInput'
import type { ExpenseGeneratePreview, ExpenseGenerateResult } from '@/types'
import { CYCLE_LABELS } from './constants'

// --- 生成固定支出向导（form → preview → result） ---
interface GenerateRow {
  template_id: number
  checked: boolean
  amount: string
}

export function GenerateFixedExpensePanel({ onClose, onGenerated }: {
  onClose: () => void
  onGenerated: () => void
}) {
  const queryClient = useQueryClient()
  const [step, setStep] = useState<'form' | 'preview' | 'result'>('form')
  const [month, setMonth] = useState<Dayjs | null>(dayjs())
  const [preview, setPreview] = useState<ExpenseGeneratePreview | null>(null)
  const [rows, setRows] = useState<GenerateRow[]>([])
  const [conflictResolution, setConflictResolution] = useState<'skip' | 'overwrite'>('skip')
  const [result, setResult] = useState<ExpenseGenerateResult | null>(null)

  const updateRow = (templateId: number, patch: Partial<GenerateRow>) =>
    setRows((prev) => prev.map((r) => (r.template_id === templateId ? { ...r, ...patch } : r)))

  const previewMutation = useMutation({
    mutationFn: (period: string) => api.getExpenseGeneratePreview(period),
    onSuccess: (data) => {
      setPreview(data)
      // status=new 默认勾选、exists 默认不勾选（契约 §6.3）
      setRows(data.items.map((it) => ({ template_id: it.template_id, checked: it.status === 'new', amount: it.amount })))
      setConflictResolution('skip')
      setStep('preview')
    },
    onError: (error: Error) => {
      message.error('获取预览失败: ' + (error.message || '未知错误'))
    },
  })

  const generateMutation = useMutation({
    mutationFn: () =>
      api.generateFixedExpenses({
        period: preview!.period,
        conflict_resolution: conflictResolution,
        items: rows
          .filter((r) => r.checked)
          .map((r) => ({ template_id: r.template_id, amount: Number(r.amount).toFixed(2) })),
      }),
    onSuccess: (data) => {
      setResult(data)
      setStep('result')
      queryClient.invalidateQueries({ queryKey: ['expenses'] })
      queryClient.invalidateQueries({ queryKey: ['expenseSummary'] })
      onGenerated()
    },
    onError: (error: Error) => {
      message.error('生成失败: ' + (error.message || '未知错误'))
    },
  })

  const handlePreview = () => {
    if (!month) { message.error('请选择月份'); return }
    previewMutation.mutate(month.format('YYYY-MM'))
  }

  const handleConfirm = () => {
    const selected = rows.filter((r) => r.checked)
    if (selected.length === 0) { message.error('请至少勾选一条待生成项目'); return }
    for (const r of selected) {
      const num = Number(r.amount)
      if (!r.amount || isNaN(num) || num <= 0) { message.error('勾选项目的金额必须大于0'); return }
    }
    generateMutation.mutate()
  }

  const hasExists = (preview?.items || []).some((it) => it.status === 'exists')

  // --- result 步骤 ---
  if (step === 'result' && result) {
    return (
      <div className="space-y-4">
        <div className={`p-4 rounded-lg ${result.success ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
          <div className="flex items-center gap-2 mb-2">
            {result.success ? (
              <Check size={20} className="text-green-600" />
            ) : (
              <AlertTriangle size={20} className="text-red-600" />
            )}
            <span className={`font-medium ${result.success ? 'text-green-800' : 'text-red-800'}`}>
              {result.success ? '生成成功' : '生成失败'}
            </span>
          </div>
          <p className="text-sm text-slate-600">{result.message}</p>
          {result.success && (
            <div className="mt-3 text-sm text-slate-600 space-y-1">
              <p>账期: {result.period}</p>
              <p>
                新增 <strong>{result.created_count}</strong> 条 ·
                覆盖 <strong>{result.overwritten_count}</strong> 条 ·
                跳过 <strong>{result.skipped_count}</strong> 条
              </p>
            </div>
          )}
        </div>
        <div className="flex justify-end">
          <button onClick={onClose} className="px-4 py-2 min-h-[44px] lg:min-h-0 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700">
            完成
          </button>
        </div>
      </div>
    )
  }

  // --- preview 步骤 ---
  if (step === 'preview' && preview) {
    const checkedCount = rows.filter((r) => r.checked).length
    return (
      <div className="space-y-4">
        <p className="lg:hidden text-xs font-medium text-muted">步骤 2/2 · 确认生成</p>
        <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <h4 className="font-medium text-blue-800 mb-2">生成预览</h4>
          <div className="text-sm text-slate-600 space-y-1">
            <p><strong>账期:</strong> {preview.period}（{preview.period_start} 至 {preview.period_end}）</p>
            <p><strong>符合条件项目:</strong> {preview.items.length} 个，已勾选 {checkedCount} 个</p>
          </div>
        </div>

        {preview.items.length === 0 ? (
          <div className="text-center py-8 text-muted text-sm">
            该月没有符合周期与有效期条件的启用项目，可返回更换月份，或先在「固定支出项目」中维护
          </div>
        ) : (
          <div className="space-y-2">
            {preview.items.map((item) => {
              const row = rows.find((r) => r.template_id === item.template_id)
              if (!row) return null
              return (
                <div key={item.template_id}
                  className={`flex flex-wrap items-center gap-x-3 gap-y-2 p-3 border rounded-lg transition-colors ${row.checked ? 'border-brand-200 bg-brand-50/40' : 'border-slate-200'}`}>
                  <label className="inline-flex min-h-[44px] min-w-[44px] items-center lg:min-h-0 lg:min-w-0">
                  <input type="checkbox" checked={row.checked}
                    onChange={(e) => updateRow(item.template_id, { checked: e.target.checked })}
                    className="w-5 h-5 lg:w-4 lg:h-4 shrink-0" />
                  </label>
                  <div className="flex-1 min-w-[180px]">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-brand-950">{item.name}</span>
                      {item.status === 'exists'
                        ? <Tag color="orange" className="m-0">已存在</Tag>
                        : <Tag color="blue" className="m-0">将新增</Tag>}
                    </div>
                    <div className="text-xs text-slate-500 mt-1">
                      {CYCLE_LABELS[item.cycle]} · 建议日期 {item.expense_date} · 分类 {item.category_name || '未分类'} · 账户 {item.account_name || '不指定'}
                    </div>
                  </div>
                  <div className="w-full sm:w-44 shrink-0">
                    <AmountInput
                      value={row.amount}
                      onChange={(value) => updateRow(item.template_id, { amount: value })}
                      disabled={!row.checked}
                      placeholder="0.00"
                      className="w-full"
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {hasExists && (
          <div className="p-4 bg-yellow-50 border border-yellow-200 rounded-lg space-y-3">
            <p className="text-sm text-yellow-700">
              <AlertTriangle size={14} className="inline mr-1" />
              部分项目在该账期已有生成记录（标「已存在」），勾选后按以下方式处理
            </p>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-2">如遇已存在记录</label>
              <div className="flex flex-wrap gap-4">
                <label className="flex min-h-[44px] lg:min-h-0 items-center gap-2 cursor-pointer">
                  <input type="radio" value="skip"
                    checked={conflictResolution === 'skip'}
                    onChange={(e) => setConflictResolution(e.target.value as 'skip' | 'overwrite')}
                    className="w-5 h-5 lg:w-4 lg:h-4 text-brand-600" />
                  <span className="text-sm text-slate-600">跳过（保留已有记录）</span>
                </label>
                <label className="flex min-h-[44px] lg:min-h-0 items-center gap-2 cursor-pointer">
                  <input type="radio" value="overwrite"
                    checked={conflictResolution === 'overwrite'}
                    onChange={(e) => setConflictResolution(e.target.value as 'skip' | 'overwrite')}
                    className="w-5 h-5 lg:w-4 lg:h-4 text-brand-600" />
                  <span className="text-sm text-slate-600">覆盖（按本次勾选与金额更新）</span>
                </label>
              </div>
            </div>
          </div>
        )}

        <div className="flex justify-end gap-3">
          <button onClick={() => setStep('form')} className="px-4 py-2 min-h-[44px] lg:min-h-0 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">
            返回修改
          </button>
          <button
            onClick={handleConfirm}
            disabled={generateMutation.isPending || preview.items.length === 0}
            className="px-4 py-2 min-h-[44px] lg:min-h-0 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50"
          >
            {generateMutation.isPending ? '生成中...' : `确认生成 (${checkedCount} 条)`}
          </button>
        </div>
      </div>
    )
  }

  // --- form 步骤 ---
  return (
    <div className="space-y-4">
      <p className="lg:hidden text-xs font-medium text-muted">步骤 1/2 · 选择账期</p>
      <div className="p-4 bg-slate-50 rounded-lg space-y-3">
        <h4 className="font-medium text-slate-700">选择账期</h4>
        <div className="w-full sm:max-w-xs">
          <label className="block text-sm font-medium text-slate-600 mb-1">生成月份</label>
          <DatePicker
            picker="month"
            value={month}
            onChange={(date) => setMonth(date)}
            placeholder="如: 2026-09"
            className="w-full"
            allowClear={false}
          />
          <p className="text-xs text-muted mt-1">按各项目的周期与有效期筛选该月应生成的固定支出</p>
        </div>
      </div>

      <div className="flex justify-end gap-3">
        <button onClick={onClose} className="px-4 py-2 min-h-[44px] lg:min-h-0 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">
          取消
        </button>
        <button
          onClick={handlePreview}
          disabled={previewMutation.isPending}
          className="px-4 py-2 min-h-[44px] lg:min-h-0 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50"
        >
          {previewMutation.isPending ? '加载中...' : '下一步'}
        </button>
      </div>
    </div>
  )
}
