'use client'

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatAmount } from '@/lib/utils'
import { Bell, Plus, Edit2, Trash2, AlertTriangle, TrendingUp, TrendingDown, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react'
import type { AlertRule, AlertRuleCreate, AlertResult } from '@/types'

const DIMENSION_OPTIONS = [
  { value: 'asset_name', label: '资产名称' },
  { value: 'fund_type', label: '资产类型' },
  { value: 'liquidity_rating', label: '流动性评级' },
  { value: 'account', label: '账户' },
]

const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' },
  { value: 'year', label: '年' },
]

const COMPARE_TYPE_OPTIONS = [
  { value: 'previous', label: '上一期' },
  { value: 'custom', label: '自定义' },
]

const DIRECTION_OPTIONS = [
  { value: 'both', label: '双向' },
  { value: 'up', label: '仅增长' },
  { value: 'down', label: '仅下降' },
]

function CreateRuleModal({
  isOpen,
  onClose,
  rule,
}: {
  isOpen: boolean
  onClose: () => void
  rule?: AlertRule | null
}) {
  const queryClient = useQueryClient()
  const [formData, setFormData] = useState<AlertRuleCreate>({
    name: '',
    dimension: 'asset_name',
    target_id: null,
    period_type: 'month',
    compare_type: 'previous',
    compare_period: null,
    amount_threshold: null,
    percent_threshold: null,
    direction: 'both',
    is_active: true,
  })

  // Reset form data when rule changes or modal opens
  useEffect(() => {
    if (isOpen) {
      if (rule) {
        setFormData({
          name: rule.name,
          dimension: rule.dimension,
          target_id: rule.target_id,
          period_type: rule.period_type,
          compare_type: rule.compare_type,
          compare_period: rule.compare_period,
          amount_threshold: rule.amount_threshold,
          percent_threshold: rule.percent_threshold,
          direction: rule.direction,
          is_active: rule.is_active,
        })
      } else {
        setFormData({
          name: '',
          dimension: 'asset_name',
          target_id: null,
          period_type: 'month',
          compare_type: 'previous',
          compare_period: null,
          amount_threshold: null,
          percent_threshold: null,
          direction: 'both',
          is_active: true,
        })
      }
    }
  }, [isOpen, rule])

  const { data: assetNames } = useQuery({
    queryKey: ['assetNames'],
    queryFn: api.getAssetNames,
    enabled: formData.dimension === 'asset_name',
  })

  const { data: fundTypes } = useQuery({
    queryKey: ['fundTypes'],
    queryFn: api.getFundTypes,
    enabled: formData.dimension === 'fund_type',
  })

  const { data: liquidityRatings } = useQuery({
    queryKey: ['liquidityRatings'],
    queryFn: api.getLiquidityRatings,
    enabled: formData.dimension === 'liquidity_rating',
  })

  const { data: accounts } = useQuery({
    queryKey: ['accounts'],
    queryFn: api.getAccounts,
    enabled: formData.dimension === 'account',
  })

  const { data: periods } = useQuery({
    queryKey: ['alertPeriods', formData.period_type],
    queryFn: () => api.getAlertPeriods(formData.period_type),
    enabled: formData.compare_type === 'custom',
  })

  const createMutation = useMutation({
    mutationFn: (data: AlertRuleCreate) => api.createAlertRule(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
      onClose()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<AlertRuleCreate> }) =>
      api.updateAlertRule(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
      onClose()
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (rule) {
      updateMutation.mutate({ id: rule.id, data: formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-slate-200">
          <h2 className="text-xl font-bold text-brand-950">
            {rule ? '编辑预警规则' : '创建预警规则'}
          </h2>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">规则名称</label>
            <input
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
              placeholder="输入规则名称"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">预警维度</label>
            <select
              value={formData.dimension}
              onChange={(e) => setFormData({ ...formData, dimension: e.target.value as AlertRuleCreate['dimension'], target_id: null })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              {DIMENSION_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          {formData.dimension === 'asset_name' && (
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">资产名称</label>
              <select
                value={formData.target_id || ''}
                onChange={(e) => setFormData({ ...formData, target_id: e.target.value || null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
              >
                <option value="">全部资产</option>
                {assetNames?.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </div>
          )}

          {formData.dimension === 'fund_type' && (
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">资产类型</label>
              <select
                value={formData.target_id || ''}
                onChange={(e) => setFormData({ ...formData, target_id: e.target.value || null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
              >
                <option value="">全部类型</option>
                {fundTypes?.map((ft) => (
                  <option key={ft.id} value={ft.id}>{'  '.repeat(ft.level)}{ft.name}</option>
                ))}
              </select>
            </div>
          )}

          {formData.dimension === 'liquidity_rating' && (
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">流动性评级</label>
              <select
                value={formData.target_id || ''}
                onChange={(e) => setFormData({ ...formData, target_id: e.target.value || null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
              >
                <option value="">全部评级</option>
                {liquidityRatings?.map((lr) => (
                  <option key={lr.id} value={lr.name}>{lr.name}</option>
                ))}
              </select>
            </div>
          )}

          {formData.dimension === 'account' && (
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">账户</label>
              <select
                value={formData.target_id || ''}
                onChange={(e) => setFormData({ ...formData, target_id: e.target.value || null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
              >
                <option value="">全部账户</option>
                {accounts?.map((acc) => (
                  <option key={acc.id} value={acc.id}>{acc.name}</option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">账期类型</label>
            <select
              value={formData.period_type}
              onChange={(e) => setFormData({ ...formData, period_type: e.target.value as AlertRuleCreate['period_type'], compare_period: null })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              {PERIOD_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">对比方式</label>
            <select
              value={formData.compare_type}
              onChange={(e) => setFormData({ ...formData, compare_type: e.target.value as AlertRuleCreate['compare_type'], compare_period: null })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              {COMPARE_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          {formData.compare_type === 'custom' && (
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">对比账期</label>
              <select
                value={formData.compare_period || ''}
                onChange={(e) => setFormData({ ...formData, compare_period: e.target.value || null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
              >
                <option value="">选择账期</option>
                {periods?.map((p) => (
                  <option key={p.date} value={p.label}>{p.label}</option>
                ))}
              </select>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">金额阈值</label>
              <input
                type="number"
                step="0.01"
                value={formData.amount_threshold || ''}
                onChange={(e) => setFormData({ ...formData, amount_threshold: e.target.value ? e.target.value : null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                placeholder="可选"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">百分比阈值(%)</label>
              <input
                type="number"
                step="0.01"
                value={formData.percent_threshold || ''}
                onChange={(e) => setFormData({ ...formData, percent_threshold: e.target.value ? e.target.value : null })}
                className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                placeholder="可选"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">预警方向</label>
            <select
              value={formData.direction}
              onChange={(e) => setFormData({ ...formData, direction: e.target.value as AlertRuleCreate['direction'] })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              {DIRECTION_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="is_active"
              checked={formData.is_active}
              onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
              className="w-4 h-4 text-brand-600 rounded border-slate-300 focus:ring-brand-500"
            />
            <label htmlFor="is_active" className="text-sm text-slate-700">启用规则</label>
          </div>

          <div className="flex justify-end gap-3 pt-4 border-t border-slate-200">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 transition-colors"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50 transition-colors"
            >
              {rule ? '保存' : '创建'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function AlertsPage() {
  const queryClient = useQueryClient()
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingRule, setEditingRule] = useState<AlertRule | null>(null)
  const [selectedPeriod, setSelectedPeriod] = useState('')
  const [expandedResults, setExpandedResults] = useState<Set<number>>(new Set())

  const { data: rules, isLoading: isLoadingRules } = useQuery({
    queryKey: ['alertRules'],
    queryFn: () => api.getAlertRules(),
  })

  const { data: periods } = useQuery({
    queryKey: ['alertPeriods', 'month'],
    queryFn: () => api.getAlertPeriods('month'),
  })

  const { data: results, isLoading: isLoadingResults, refetch: refetchResults } = useQuery({
    queryKey: ['alertResults', selectedPeriod],
    queryFn: () => api.getAlertResults(selectedPeriod || undefined),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteAlertRule(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) =>
      api.updateAlertRule(id, { is_active }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alertRules'] })
      queryClient.invalidateQueries({ queryKey: ['alertResults'] })
    },
  })

  const handleEdit = (rule: AlertRule) => {
    setEditingRule(rule)
    setIsModalOpen(true)
  }

  const handleDelete = (id: number) => {
    if (confirm('确定要删除这个预警规则吗？')) {
      deleteMutation.mutate(id)
    }
  }

  const handleToggle = (rule: AlertRule) => {
    toggleMutation.mutate({ id: rule.id, is_active: !rule.is_active })
  }

  const handleCloseModal = () => {
    setIsModalOpen(false)
    setEditingRule(null)
  }

  const toggleExpand = (ruleId: number) => {
    setExpandedResults((prev) => {
      const newSet = new Set(prev)
      if (newSet.has(ruleId)) {
        newSet.delete(ruleId)
      } else {
        newSet.add(ruleId)
      }
      return newSet
    })
  }

  const triggeredResults = results?.filter((r) => r.triggered) || []
  const notTriggeredResults = results?.filter((r) => !r.triggered) || []

  // Component to render detail table
  const DetailTable = ({ details }: { details: import('@/types').AlertDetailItem[] }) => {
    if (!details || details.length === 0) return null

    return (
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200">
              <th className="text-left py-2 px-2 text-slate-600 font-medium">名称</th>
              <th className="text-right py-2 px-2 text-slate-600 font-medium">当前金额</th>
              <th className="text-right py-2 px-2 text-slate-600 font-medium">对比金额</th>
              <th className="text-right py-2 px-2 text-slate-600 font-medium">变化金额</th>
              <th className="text-right py-2 px-2 text-slate-600 font-medium">变化%</th>
            </tr>
          </thead>
          <tbody>
            {details.map((item) => (
              <tr
                key={item.id}
                className={`border-b border-slate-100 ${item.triggered ? 'bg-red-50' : ''}`}
              >
                <td className="py-2 px-2">
                  <span className={item.triggered ? 'font-medium text-red-700' : 'text-slate-700'}>
                    {item.name}
                  </span>
                  {item.triggered && (
                    <span className="ml-2 text-xs text-red-500">(触发)</span>
                  )}
                </td>
                <td className="text-right py-2 px-2 text-slate-600">{formatAmount(item.current_amount)}</td>
                <td className="text-right py-2 px-2 text-slate-600">{formatAmount(item.compare_amount)}</td>
                <td className={`text-right py-2 px-2 font-medium ${
                  parseFloat(item.change_amount) >= 0 ? 'text-red-600' : 'text-green-600'
                }`}>
                  {parseFloat(item.change_amount) >= 0 ? '+' : ''}
                  {formatAmount(item.change_amount)}
                </td>
                <td className={`text-right py-2 px-2 ${
                  item.change_percent !== null && item.change_percent >= 0 ? 'text-red-600' : 'text-green-600'
                }`}>
                  {item.change_percent !== null ? `${item.change_percent >= 0 ? '+' : ''}${item.change_percent.toFixed(2)}%` : '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产预警</h1>
          <p className="text-sm text-slate-500 mt-1">监控资产变化，及时预警</p>
        </div>
        <button
          onClick={() => setIsModalOpen(true)}
          className="flex items-center gap-2 px-4 py-2 text-white bg-brand-600 rounded-lg hover:bg-brand-700 transition-colors"
        >
          <Plus size={18} />
          创建规则
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <div className="bg-white rounded-xl shadow-card p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-semibold text-brand-950">预警规则</h2>
              <span className="text-sm text-slate-500">{rules?.length || 0} 条规则</span>
            </div>

            {isLoadingRules ? (
              <div className="text-center py-8 text-slate-400">加载中...</div>
            ) : rules && rules.length > 0 ? (
              <div className="space-y-3">
                {rules.map((rule) => (
                  <div
                    key={rule.id}
                    className={`p-4 rounded-lg border ${
                      rule.is_active ? 'border-brand-200 bg-brand-50' : 'border-slate-200 bg-slate-50'
                    }`}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className={`w-2 h-2 rounded-full ${rule.is_active ? 'bg-green-500' : 'bg-slate-300'}`} />
                          <span className="font-medium text-slate-800">{rule.name}</span>
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          {DIMENSION_OPTIONS.find((d) => d.value === rule.dimension)?.label}
                          {rule.target_id && ` · ${rule.target_id}`}
                        </div>
                        <div className="mt-1 text-xs text-slate-400">
                          {PERIOD_TYPE_OPTIONS.find((p) => p.value === rule.period_type)?.label}
                          {rule.compare_type === 'previous' ? ' · 上一期' : ` · ${rule.compare_period}`}
                        </div>
                      </div>
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => handleToggle(rule)}
                          className={`p-1.5 rounded hover:bg-slate-200 transition-colors ${
                            rule.is_active ? 'text-green-600' : 'text-slate-400'
                          }`}
                          title={rule.is_active ? '禁用' : '启用'}
                        >
                          <Bell size={16} />
                        </button>
                        <button
                          onClick={() => handleEdit(rule)}
                          className="p-1.5 rounded hover:bg-slate-200 text-slate-400 hover:text-slate-600 transition-colors"
                        >
                          <Edit2 size={16} />
                        </button>
                        <button
                          onClick={() => handleDelete(rule.id)}
                          className="p-1.5 rounded hover:bg-red-100 text-slate-400 hover:text-red-600 transition-colors"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-8 text-slate-400">
                <Bell size={32} className="mx-auto mb-2 opacity-50" />
                <p>暂无预警规则</p>
                <p className="text-xs mt-1">点击右上角按钮创建</p>
              </div>
            )}
          </div>
        </div>

        <div className="lg:col-span-2">
          <div className="bg-white rounded-xl shadow-card p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-semibold text-brand-950">预警结果</h2>
              <div className="flex items-center gap-3">
                <select
                  value={selectedPeriod}
                  onChange={(e) => setSelectedPeriod(e.target.value)}
                  className="px-3 py-1.5 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                >
                  <option value="">最新账期</option>
                  {periods?.map((p) => (
                    <option key={p.date} value={p.label}>{p.label}</option>
                  ))}
                </select>
                <button
                  onClick={() => refetchResults()}
                  className="p-1.5 rounded hover:bg-slate-100 text-slate-500 hover:text-slate-700 transition-colors"
                  title="刷新"
                >
                  <RefreshCw size={16} />
                </button>
              </div>
            </div>

            {isLoadingResults ? (
              <div className="text-center py-8 text-slate-400">加载中...</div>
            ) : results && results.length > 0 ? (
              <div className="space-y-4">
                {triggeredResults.length > 0 && (
                  <div>
                    <div className="flex items-center gap-2 mb-3">
                      <AlertTriangle size={16} className="text-red-500" />
                      <span className="text-sm font-medium text-red-600">触发预警 ({triggeredResults.length})</span>
                    </div>
                    <div className="space-y-2">
                      {triggeredResults.map((result) => (
                        <div
                          key={result.rule_id}
                          className="p-4 rounded-lg border border-red-200 bg-red-50"
                        >
                          <div className="flex items-center justify-between">
                            <div>
                              <div className="font-medium text-slate-800">{result.rule_name}</div>
                              <div className="text-sm text-slate-500 mt-1">
                                {result.target_name} · {result.current_period}
                                {result.compare_period ? ` vs ${result.compare_period}` : ' (无对比数据)'}
                              </div>
                              {result.message && (
                                <div className="text-xs text-amber-600 mt-1">{result.message}</div>
                              )}
                            </div>
                            <div className="text-right">
                              <div className={`flex items-center gap-1 ${
                                parseFloat(result.change_amount) >= 0 ? 'text-red-600' : 'text-green-600'
                              }`}>
                                {parseFloat(result.change_amount) >= 0 ? (
                                  <TrendingUp size={16} />
                                ) : (
                                  <TrendingDown size={16} />
                                )}
                                <span className="font-semibold">
                                  {parseFloat(result.change_amount) >= 0 ? '+' : ''}
                                  {formatAmount(result.change_amount)}
                                </span>
                              </div>
                              {result.change_percent !== null && (
                                <div className="text-sm text-slate-500">
                                  {result.change_percent >= 0 ? '+' : ''}
                                  {result.change_percent.toFixed(2)}%
                                </div>
                              )}
                            </div>
                          </div>
                          <div className="mt-2 flex items-center justify-between">
                            <div className="text-xs text-slate-400">
                              当前: {formatAmount(result.current_amount)} · 对比: {formatAmount(result.compare_amount)}
                            </div>
                            {result.details && result.details.length > 0 && (
                              <button
                                onClick={() => toggleExpand(result.rule_id)}
                                className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 transition-colors"
                              >
                                {expandedResults.has(result.rule_id) ? (
                                  <><ChevronUp size={14} /> 收起详情</>
                                ) : (
                                  <><ChevronDown size={14} /> 查看详情 ({result.details.length}项)</>
                                )}
                              </button>
                            )}
                          </div>
                          {expandedResults.has(result.rule_id) && result.details && result.details.length > 0 && (
                            <DetailTable details={result.details} />
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {notTriggeredResults.length > 0 && (
                  <div>
                    <div className="flex items-center gap-2 mb-3">
                      <span className="text-sm font-medium text-slate-500">未触发 ({notTriggeredResults.length})</span>
                    </div>
                    <div className="space-y-2">
                      {notTriggeredResults.map((result) => (
                        <div
                          key={result.rule_id}
                          className="p-4 rounded-lg border border-slate-200 bg-slate-50"
                        >
                          <div className="flex items-center justify-between">
                            <div>
                              <div className="font-medium text-slate-600">{result.rule_name}</div>
                              <div className="text-sm text-slate-400 mt-1">
                                {result.target_name} · {result.current_period}
                                {result.compare_period ? ` vs ${result.compare_period}` : ' (无对比数据)'}
                              </div>
                              {result.message && (
                                <div className="text-xs text-amber-500 mt-1">{result.message}</div>
                              )}
                            </div>
                            <div className="text-right">
                              <div className={`flex items-center gap-1 ${
                                parseFloat(result.change_amount) >= 0 ? 'text-slate-600' : 'text-slate-500'
                              }`}>
                                <span className="font-medium">
                                  {parseFloat(result.change_amount) >= 0 ? '+' : ''}
                                  {formatAmount(result.change_amount)}
                                </span>
                              </div>
                              {result.change_percent !== null && (
                                <div className="text-sm text-slate-400">
                                  {result.change_percent >= 0 ? '+' : ''}
                                  {result.change_percent.toFixed(2)}%
                                </div>
                              )}
                            </div>
                          </div>
                          <div className="mt-2 flex items-center justify-between">
                            <div className="text-xs text-slate-400">
                              当前: {formatAmount(result.current_amount)} · 对比: {formatAmount(result.compare_amount)}
                            </div>
                            {result.details && result.details.length > 0 && (
                              <button
                                onClick={() => toggleExpand(result.rule_id)}
                                className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 transition-colors"
                              >
                                {expandedResults.has(result.rule_id) ? (
                                  <><ChevronUp size={14} /> 收起详情</>
                                ) : (
                                  <><ChevronDown size={14} /> 查看详情 ({result.details.length}项)</>
                                )}
                              </button>
                            )}
                          </div>
                          {expandedResults.has(result.rule_id) && result.details && result.details.length > 0 && (
                            <DetailTable details={result.details} />
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="text-center py-8 text-slate-400">
                <Bell size={32} className="mx-auto mb-2 opacity-50" />
                <p>暂无预警结果</p>
                <p className="text-xs mt-1">请先创建并启用预警规则</p>
              </div>
            )}
          </div>
        </div>
      </div>

      <CreateRuleModal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        rule={editingRule}
      />
    </div>
  )
}
