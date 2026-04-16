'use client'

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatAmount, formatFileSize, formatDateTime } from '@/lib/utils'
import { Download, FileDown, Eye, Calendar, Filter, History, Settings, Clock, CheckCircle, XCircle, Plus, Trash2, Edit3 } from 'lucide-react'
import { DatePicker, Tabs, Table, Button, Tag, Switch, Modal, Form, Input, Select, message, Pagination } from 'antd'
import type { ExportHistory, AutoExportRule, AutoExportRuleCreate } from '@/types'
import dayjs from 'dayjs'

type ExportPeriod = 'all' | 'year' | 'quarter' | 'month' | 'day' | 'custom'

interface ExportFilters {
  period: ExportPeriod
  year: number | ''
  quarter: number | ''
  month: number | ''
  day: number | ''
  startDate: string
  endDate: string
}

const { Option } = Select

// Cron 表达式模板
const CRON_TEMPLATES = [
  { label: '每天凌晨 2 点', value: '0 2 * * *' },
  { label: '每小时', value: '0 * * * *' },
  { label: '每周一凌晨 3 点', value: '0 3 * * 1' },
  { label: '每月 1 号凌晨 4 点', value: '0 4 1 * *' },
]

export default function ExportPage() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('export')
  const currentYear = new Date().getFullYear()
  const [filters, setFilters] = useState<ExportFilters>({
    period: 'all',
    year: currentYear,
    quarter: '',
    month: '',
    day: '',
    startDate: '',
    endDate: '',
  })
  const [isExporting, setIsExporting] = useState(false)

  // 导出历史分页
  const [historyPage, setHistoryPage] = useState(1)
  const [historyPageSize, setHistoryPageSize] = useState(10)
  const [historyType, setHistoryType] = useState<string | undefined>(undefined)

  // 自动导出规则弹窗
  const [ruleModalVisible, setRuleModalVisible] = useState(false)
  const [editingRule, setEditingRule] = useState<AutoExportRule | null>(null)
  const [cronDescription, setCronDescription] = useState('')
  const [cronValid, setCronValid] = useState(true)
  const [form] = Form.useForm()

  const buildQueryParams = useMemo(() => {
    const params: Record<string, string> = { period_type: filters.period }
    if (filters.year) params.year = String(filters.year)
    if (filters.quarter) params.quarter = String(filters.quarter)
    if (filters.month) params.month = String(filters.month)
    if (filters.day) params.day = String(filters.day)
    if (filters.period === 'custom') {
      if (filters.startDate) params.date_from = filters.startDate
      if (filters.endDate) params.date_to = filters.endDate
    }
    return params
  }, [filters])

  const { data: preview, isLoading: isPreviewLoading } = useQuery({
    queryKey: ['exportPreview', buildQueryParams],
    queryFn: () => api.previewExport(buildQueryParams),
    enabled: filters.period !== 'all' && (filters.period !== 'custom' || (!!filters.startDate && !!filters.endDate)),
  })

  // 导出历史 - 默认加载最近10条全部类型
  const { data: exportHistory, isLoading: isHistoryLoading, refetch: refetchHistory } = useQuery({
    queryKey: ['exportHistory', historyPage, historyPageSize, historyType],
    queryFn: () => {
      // 如果没有选择类型，使用 limit 模式获取最近10条
      if (!historyType) {
        return api.getExportHistory({ limit: '10' })
      }
      // 否则使用分页模式
      return api.getExportHistory({
        page: String(historyPage),
        page_size: String(historyPageSize),
        export_type: historyType,
      })
    },
    enabled: true,
  })

  // 自动导出规则
  const { data: autoExportRules, isLoading: isRulesLoading } = useQuery({
    queryKey: ['autoExportRules'],
    queryFn: () => api.getAutoExportRules(),
  })

  // 创建/更新规则
  const saveRuleMutation = useMutation({
    mutationFn: async (values: AutoExportRuleCreate & { id?: number }) => {
      if (values.id) {
        return api.updateAutoExportRule(values.id, values)
      }
      return api.createAutoExportRule(values)
    },
    onSuccess: () => {
      message.success(editingRule ? '规则更新成功' : '规则创建成功')
      setRuleModalVisible(false)
      setEditingRule(null)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    },
    onError: (error: Error) => {
      message.error(error.message || '操作失败')
    },
  })

  // 删除规则
  const deleteRuleMutation = useMutation({
    mutationFn: (id: number) => api.deleteAutoExportRule(id),
    onSuccess: () => {
      message.success('规则删除成功')
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    },
  })

  // 验证 Cron 表达式
  const validateCron = async (cron: string) => {
    if (!cron) {
      setCronValid(true)
      setCronDescription('')
      return
    }
    try {
      const result = await api.validateCronExpression(cron)
      setCronValid(result.valid)
      setCronDescription(result.description)
    } catch {
      setCronValid(false)
      setCronDescription('验证失败')
    }
  }

  const isExportDisabled = useMemo(() => {
    if (isExporting) return true
    if (filters.period === 'all') return false
    if (filters.period === 'custom') {
      return !filters.startDate || !filters.endDate || dayjs(filters.endDate).isBefore(dayjs(filters.startDate))
    }
    return !preview
  }, [isExporting, filters, preview])

  const getExportButtonText = () => {
    if (filters.period === 'custom') {
      if (!filters.startDate || !filters.endDate) return '请选择日期范围'
      if (dayjs(filters.endDate).isBefore(dayjs(filters.startDate))) return '结束日期不能早于开始日期'
    }
    return isExporting ? '导出中...' : '导出 CSV'
  }

  const years = useMemo(() => {
    const yrs = []
    for (let y = currentYear; y >= currentYear - 5; y--) {
      yrs.push(y)
    }
    return yrs
  }, [currentYear])

  const months = useMemo(() => Array.from({ length: 12 }, (_, i) => i + 1), [])
  const quarters = useMemo(() => [1, 2, 3, 4], [])

  const days = useMemo(() => {
    if (!filters.year || !filters.month) return []
    const daysInMonth = new Date(filters.year, filters.month, 0).getDate()
    return Array.from({ length: daysInMonth }, (_, i) => i + 1)
  }, [filters.year, filters.month])

  const handleExport = async () => {
    setIsExporting(true)
    try {
      // 获取当前用户名（从 localStorage 或默认值）
      const operator = localStorage.getItem('username') || '当前用户'
      const blob = await api.downloadExport(buildQueryParams, operator)
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = preview?.filename || 'assets_export.csv'
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
      message.success('导出成功')
      // 刷新导出历史
      queryClient.invalidateQueries({ queryKey: ['exportHistory'] })
    } catch (error) {
      console.error('Export failed:', error)
      message.error('导出失败，请重试')
    } finally {
      setIsExporting(false)
    }
  }

  const getPeriodLabel = () => {
    switch (filters.period) {
      case 'all': return '全部数据'
      case 'year': return `${filters.year}年`
      case 'quarter': return `${filters.year}年 Q${filters.quarter}`
      case 'month': return `${filters.year}年 ${filters.month}月`
      case 'day': return `${filters.year}年 ${filters.month}月 ${filters.day}日`
      case 'custom': return `${filters.startDate} 至 ${filters.endDate}`
      default: return ''
    }
  }

  // 打开创建规则弹窗
  const openCreateRuleModal = () => {
    setEditingRule(null)
    form.resetFields()
    form.setFieldsValue({ export_format: 'csv' })
    setCronValid(true)
    setCronDescription('')
    setRuleModalVisible(true)
  }

  // 打开编辑规则弹窗
  const openEditRuleModal = (rule: AutoExportRule) => {
    setEditingRule(rule)
    form.setFieldsValue({
      name: rule.name,
      cron_expression: rule.cron_expression,
      export_format: rule.export_format,
      filename_template: rule.filename_template,
    })
    validateCron(rule.cron_expression)
    setRuleModalVisible(true)
  }

  // 保存规则
  const handleSaveRule = async (values: AutoExportRuleCreate) => {
    if (editingRule) {
      await saveRuleMutation.mutateAsync({ ...values, id: editingRule.id })
    } else {
      await saveRuleMutation.mutateAsync(values)
    }
  }

  // 切换规则状态
  const toggleRuleStatus = async (rule: AutoExportRule) => {
    try {
      await api.updateAutoExportRule(rule.id, { is_active: !rule.is_active })
      message.success(rule.is_active ? '规则已禁用' : '规则已启用')
      queryClient.invalidateQueries({ queryKey: ['autoExportRules'] })
    } catch (error) {
      message.error('操作失败')
    }
  }

  // 导出历史表格列
  const historyColumns = [
    {
      title: '导出时间',
      dataIndex: 'export_time',
      key: 'export_time',
      render: (time: string) => formatDateTime(time),
    },
    {
      title: '类型',
      dataIndex: 'export_type',
      key: 'export_type',
      render: (type: string, record: ExportHistory) => (
        <Tag color={type === 'manual' ? 'blue' : 'green'}>
          {type === 'manual' ? '手动' : '自动'}
          {record.rule_name && ` (${record.rule_name})`}
        </Tag>
      ),
    },
    {
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
    },
    {
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      render: (size: number | null) => size ? formatFileSize(size) : '-',
    },
    {
      title: '操作人',
      dataIndex: 'operator',
      key: 'operator',
      render: (operator: string | null) => operator || '-',
    },
  ]

  // 自动导出规则表格列
  const ruleColumns = [
    {
      title: '规则名称',
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: 'Cron 表达式',
      dataIndex: 'cron_expression',
      key: 'cron_expression',
      render: (cron: string) => (
        <code className="bg-slate-100 px-2 py-1 rounded text-sm">{cron}</code>
      ),
    },
    {
      title: '格式',
      dataIndex: 'export_format',
      key: 'export_format',
      render: (format: string) => format.toUpperCase(),
    },
    {
      title: '上次执行',
      dataIndex: 'last_run_at',
      key: 'last_run_at',
      render: (time: string | null) => time ? formatDateTime(time) : '-',
    },
    {
      title: '下次执行',
      dataIndex: 'next_run_at',
      key: 'next_run_at',
      render: (time: string | null) => time ? formatDateTime(time) : '-',
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      render: (isActive: boolean) => (
        <Tag color={isActive ? 'success' : 'default'}>
          {isActive ? '启用' : '禁用'}
        </Tag>
      ),
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: AutoExportRule) => (
        <div className="flex gap-2">
          <Switch
            checked={record.is_active}
            onChange={() => toggleRuleStatus(record)}
            size="small"
          />
          <Button
            type="text"
            size="small"
            icon={<Edit3 size={14} />}
            onClick={() => openEditRuleModal(record)}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            onClick={() => {
              Modal.confirm({
                title: '确认删除',
                content: `确定要删除规则 "${record.name}" 吗？`,
                onOk: () => deleteRuleMutation.mutate(record.id),
              })
            }}
          />
        </div>
      ),
    },
  ]

  // Tabs items 配置
  const tabItems = [
    {
      key: 'export',
      label: (
        <span className="flex items-center gap-1">
          <Download size={16} /> 数据导出
        </span>
      ),
      children: (
        <>
          {/* Filter Panel */}
          <div className="bg-white rounded-xl shadow-card p-6">
            <div className="flex items-center gap-2 mb-4">
              <Filter size={18} className="text-brand-600" />
              <h3 className="font-semibold text-brand-950">导出筛选</h3>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {/* Period Type */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1.5">账期类型</label>
                <select
                  value={filters.period}
                  onChange={(e) => setFilters({
                    period: e.target.value as ExportPeriod,
                    year: currentYear,
                    quarter: '',
                    month: '',
                    day: '',
                    startDate: '',
                    endDate: '',
                  })}
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                >
                  <option value="all">全部数据</option>
                  <option value="year">按年</option>
                  <option value="quarter">按季度</option>
                  <option value="month">按月</option>
                  <option value="day">按日</option>
                  <option value="custom">自定义时间</option>
                </select>
              </div>

              {/* Custom Date Range */}
              {filters.period === 'custom' && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1.5">开始日期</label>
                    <DatePicker
                      placeholder="选择开始日期"
                      value={filters.startDate ? dayjs(filters.startDate) : null}
                      onChange={(date) => setFilters({ ...filters, startDate: date ? date.format('YYYY-MM-DD') : '' })}
                      style={{ width: '100%' }}
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1.5">结束日期</label>
                    <DatePicker
                      placeholder="选择结束日期"
                      value={filters.endDate ? dayjs(filters.endDate) : null}
                      onChange={(date) => setFilters({ ...filters, endDate: date ? date.format('YYYY-MM-DD') : '' })}
                      style={{ width: '100%' }}
                    />
                  </div>
                </>
              )}

              {/* Year */}
              {filters.period !== 'all' && filters.period !== 'custom' && (
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1.5">年份</label>
                  <select
                    value={filters.year}
                    onChange={(e) => setFilters({
                      ...filters,
                      year: parseInt(e.target.value),
                      quarter: '',
                      month: '',
                      day: '',
                    })}
                    className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                  >
                    {years.map(y => <option key={y} value={y}>{y}年</option>)}
                  </select>
                </div>
              )}

              {/* Quarter */}
              {filters.period === 'quarter' && (
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1.5">季度</label>
                  <select
                    value={filters.quarter}
                    onChange={(e) => setFilters({ ...filters, quarter: parseInt(e.target.value) })}
                    className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                  >
                    <option value="">选择季度</option>
                    {quarters.map(q => <option key={q} value={q}>第{q}季度</option>)}
                  </select>
                </div>
              )}

              {/* Month */}
              {(filters.period === 'month' || filters.period === 'day') && (
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1.5">月份</label>
                  <select
                    value={filters.month}
                    onChange={(e) => setFilters({
                      ...filters,
                      month: parseInt(e.target.value),
                      day: '',
                    })}
                    className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                  >
                    <option value="">选择月份</option>
                    {months.map(m => <option key={m} value={m}>{m}月</option>)}
                  </select>
                </div>
              )}

              {/* Day */}
              {filters.period === 'day' && (
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1.5">日期</label>
                  <select
                    value={filters.day}
                    onChange={(e) => setFilters({ ...filters, day: parseInt(e.target.value) })}
                    className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
                  >
                    <option value="">选择日期</option>
                    {days.map(d => <option key={d} value={d}>{d}日</option>)}
                  </select>
                </div>
              )}
            </div>

            {/* Export Button */}
            <div className="mt-6 flex items-center justify-between">
              <div className="text-sm text-slate-500">
                {filters.period === 'all' ? (
                  <span>将导出 <strong>全部</strong> 数据</span>
                ) : preview ? (
                  <span>
                    将导出 <strong>{getPeriodLabel()}</strong> 的数据，
                    共 <strong className="text-brand-600">{preview.total_count}</strong> 条记录
                  </span>
                ) : isPreviewLoading ? (
                  <span>正在计算...</span>
                ) : (
                  <span>请选择完整的筛选条件</span>
                )}
              </div>
              <button
                onClick={handleExport}
                disabled={isExportDisabled}
                className="flex items-center gap-2 px-6 py-2.5 text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <Download size={18} />
                {getExportButtonText()}
              </button>
            </div>
          </div>

          {/* Preview Table */}
          {preview && preview.preview.length > 0 && (
            <div className="bg-white rounded-xl shadow-card overflow-hidden mt-6">
              <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Eye size={18} className="text-brand-600" />
                  <h3 className="font-semibold text-brand-950">数据预览</h3>
                </div>
                <span className="text-sm text-slate-500">显示前 {preview.preview.length} 条记录</span>
              </div>
              <div className="max-h-[400px] overflow-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50 sticky top-0">
                    <tr>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">日期</th>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">流动性</th>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">资产类型</th>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">资产名称</th>
                      <th className="text-left px-4 py-2 text-slate-600 font-medium">账户</th>
                      <th className="text-right px-4 py-2 text-slate-600 font-medium">金额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.preview.map((row, idx) => (
                      <tr key={idx} className="border-t border-slate-50 hover:bg-slate-50/50">
                        <td className="px-4 py-2 text-slate-600">{row.asset_date}</td>
                        <td className="px-4 py-2">
                          <span className="inline-block px-2 py-0.5 rounded text-xs bg-slate-100 text-slate-600">
                            {row.liquidity_rating}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-slate-500">{row.fund_type}</td>
                        <td className="px-4 py-2 font-medium">{row.asset_name}</td>
                        <td className="px-4 py-2 text-slate-500">{row.account}</td>
                        <td className={`px-4 py-2 text-right font-mono ${parseFloat(row.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
                          {formatAmount(row.amount)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      ),
    },
    {
      key: 'rules',
      label: (
        <span className="flex items-center gap-1">
          <Settings size={16} /> 自动导出规则
        </span>
      ),
      children: (
        <div className="bg-white rounded-xl shadow-card p-6">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Clock size={18} className="text-brand-600" />
              <h3 className="font-semibold text-brand-950">自动导出规则</h3>
            </div>
            <Button
              type="primary"
              icon={<Plus size={16} />}
              onClick={openCreateRuleModal}
            >
              创建规则
            </Button>
          </div>

          <Table
            columns={ruleColumns}
            dataSource={autoExportRules || []}
            rowKey="id"
            loading={isRulesLoading}
          />
        </div>
      ),
    },
  ]

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold text-brand-950">数据导出</h1>
        <p className="text-sm text-slate-500 mt-1">导出资产记录到 CSV 文件，管理自动导出规则</p>
      </div>

      <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />

      {/* Export History - 显示在导出格式说明上方 */}
      <div className="bg-white rounded-xl shadow-card p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <History size={18} className="text-brand-600" />
            <h3 className="font-semibold text-brand-950">导出历史记录</h3>
          </div>
          <Select
            placeholder="筛选类型"
            allowClear
            style={{ width: 150 }}
            value={historyType || undefined}
            onChange={setHistoryType}
          >
            <Option value="manual">手动导出</Option>
            <Option value="auto">自动导出</Option>
          </Select>
        </div>

        <Table
          columns={historyColumns}
          dataSource={exportHistory?.items || []}
          rowKey="id"
          loading={isHistoryLoading}
          pagination={{
            current: historyPage,
            pageSize: historyPageSize,
            total: exportHistory?.total || 0,
            onChange: (page, pageSize) => {
              setHistoryPage(page)
              setHistoryPageSize(pageSize || 10)
            },
          }}
        />
      </div>

      {/* Export Format Info */}
      <div className="bg-slate-50 rounded-xl p-6">
        <div className="flex items-center gap-2 mb-3">
          <FileDown size={18} className="text-slate-600" />
          <h3 className="font-medium text-slate-800">导出格式说明</h3>
        </div>
        <p className="text-sm text-slate-600 mb-3">
          导出的 CSV 文件格式与导入格式完全一致，包含以下字段：
        </p>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 text-sm">
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">asset_date</span>
            <p className="text-slate-700 font-medium">日期</p>
          </div>
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">liquidity_rating</span>
            <p className="text-slate-700 font-medium">流动性</p>
          </div>
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">fund_type</span>
            <p className="text-slate-700 font-medium">资产类型</p>
          </div>
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">asset_name</span>
            <p className="text-slate-700 font-medium">资产名称</p>
          </div>
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">account</span>
            <p className="text-slate-700 font-medium">账户</p>
          </div>
          <div className="bg-white px-3 py-2 rounded border border-slate-200">
            <span className="text-slate-500">amount</span>
            <p className="text-slate-700 font-medium">金额</p>
          </div>
        </div>
      </div>

      {/* Rule Modal */}
      <Modal
        title={editingRule ? '编辑自动导出规则' : '创建自动导出规则'}
        open={ruleModalVisible}
        onCancel={() => {
          setRuleModalVisible(false)
          setEditingRule(null)
          form.resetFields()
        }}
        onOk={() => form.submit()}
        confirmLoading={saveRuleMutation.isPending}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSaveRule}
        >
          <Form.Item
            name="name"
            label="规则名称"
            rules={[{ required: true, message: '请输入规则名称' }]}
          >
            <Input placeholder="例如：每日备份" />
          </Form.Item>

          <Form.Item
            name="cron_expression"
            label="Cron 表达式"
            rules={[{ required: true, message: '请输入 Cron 表达式' }]}
            extra={
              <div className="mt-1">
                {cronDescription && (
                  <span className={cronValid ? 'text-green-600' : 'text-red-600'}>
                    {cronValid ? '✓ ' : '✗ '}{cronDescription}
                  </span>
                )}
              </div>
            }
          >
            <Input
              placeholder="0 2 * * *"
              onChange={(e) => validateCron(e.target.value)}
            />
          </Form.Item>

          <Form.Item>
            <Select
              placeholder="选择常用模板"
              onChange={(value) => {
                form.setFieldsValue({ cron_expression: value })
                validateCron(value)
              }}
            >
              {CRON_TEMPLATES.map(t => (
                <Option key={t.value} value={t.value}>{t.label}</Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item
            name="export_format"
            label="导出格式"
            rules={[{ required: true }]}
          >
            <Select>
              <Option value="csv">CSV</Option>
              <Option value="json">JSON</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="filename_template"
            label="文件名模板"
            extra="支持变量：{date} 日期, {timestamp} 时间戳"
          >
            <Input placeholder="backup_{date}.csv" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
