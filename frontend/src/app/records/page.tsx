'use client'

import { useState, useEffect, useCallback, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatAmount, formatDate, PERIOD_OPTIONS } from '@/lib/utils'
import type { AssetRecord, AssetRecordCreate, FundType, Account, BatchUpdateAssets, AssetHistoryByNameResponse, BatchCreateByPeriodResult, LiquidityRating } from '@/types'
import {
  Plus, Copy, Trash2, Pencil, X, Check, ChevronLeft, ChevronRight, Search, Edit3, ChevronDown, Square, CheckSquare, AlertTriangle, History, CalendarPlus
} from 'lucide-react'
import { DatePicker } from 'antd'
import dayjs from 'dayjs'

// --- Reusable Modal ---
function Modal({ open, onClose, title, children, size = 'md' }: {
  open: boolean; onClose: () => void; title: string; children: React.ReactNode; size?: 'md' | 'lg' | 'xl'
}) {
  if (!open) return null
  const sizeClasses = {
    md: 'max-w-2xl',
    lg: 'max-w-4xl',
    xl: 'max-w-6xl'
  }
  return (
    <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className={`bg-white rounded-xl shadow-xl w-full ${sizeClasses[size]} min-h-[40vh] max-h-[90vh] overflow-auto animate-slide-up`}
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <h3 className="text-lg font-semibold text-brand-950">{title}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X size={20} /></button>
        </div>
        <div className="p-6">{children}</div>
      </div>
    </div>
  )
}

// --- Multi-Select Dropdown with Search ---
function MultiSelectDropdown({
  options,
  selected,
  onChange,
  placeholder = "选择...",
  label
}: {
  options: string[]
  selected: string[]
  onChange: (selected: string[]) => void
  placeholder?: string
  label?: string
}) {
  const [isOpen, setIsOpen] = useState(false)
  const [search, setSearch] = useState('')

  const filteredOptions = useMemo(() => {
    if (!search) return options
    return options.filter(opt => opt.toLowerCase().includes(search.toLowerCase()))
  }, [options, search])

  const toggleOption = (option: string) => {
    if (selected.includes(option)) {
      onChange(selected.filter(s => s !== option))
    } else {
      onChange([...selected, option])
    }
  }

  return (
    <div className="relative">
      {label && <label className="block text-sm font-medium text-slate-600 mb-1">{label}</label>}
      <div
        className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm bg-white cursor-pointer flex items-center justify-between"
        onClick={() => setIsOpen(!isOpen)}
      >
        <span className={selected.length === 0 ? 'text-slate-400' : 'text-slate-700'}>
          {selected.length === 0 ? placeholder : `已选择 ${selected.length} 项`}
        </span>
        <ChevronDown size={16} className={`text-slate-400 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </div>

      {isOpen && (
        <div className="absolute z-50 w-full mt-1 bg-white border border-slate-200 rounded-lg shadow-lg max-h-80 overflow-hidden">
          <div className="p-2 border-b border-slate-100">
            <div className="relative">
              <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索..."
                className="w-full pl-7 pr-2 py-1.5 text-sm border border-slate-200 rounded focus:outline-none focus:ring-2 focus:ring-brand-400"
                onClick={(e) => e.stopPropagation()}
              />
            </div>
          </div>
          <div className="max-h-60 overflow-y-auto">
            {filteredOptions.length === 0 ? (
              <div className="px-3 py-2 text-sm text-slate-400">无匹配选项</div>
            ) : (
              filteredOptions.map(option => (
                <div
                  key={option}
                  className="px-3 py-2 text-sm hover:bg-slate-50 cursor-pointer flex items-center gap-2"
                  onClick={(e) => {
                    e.stopPropagation()
                    toggleOption(option)
                  }}
                >
                  <div className={`w-4 h-4 rounded border flex items-center justify-center ${
                    selected.includes(option) ? 'bg-brand-600 border-brand-600' : 'border-slate-300'
                  }`}>
                    {selected.includes(option) && <Check size={12} className="text-white" />}
                  </div>
                  <span className="text-slate-700">{option}</span>
                </div>
              ))
            )}
          </div>
          <div className="p-2 border-t border-slate-100 flex justify-between items-center">
            <span className="text-xs text-slate-500">{selected.length} 项已选择</span>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onChange([])
              }}
              className="text-xs text-brand-600 hover:text-brand-700"
            >
              清空
            </button>
          </div>
        </div>
      )}

      {isOpen && <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} />}
    </div>
  )
}

// --- Batch Update History Modal ---
function BatchUpdateHistoryModal({
  open,
  onClose,
  assetName,
  fundTypes,
  accounts,
  liquidityRatings,
  onSubmit
}: {
  open: boolean
  onClose: () => void
  assetName: string
  fundTypes: FundType[]
  accounts: Account[]
  liquidityRatings: import('@/types').LiquidityRating[]
  onSubmit: (data: BatchUpdateAssets, summary: { count: number; changes: string[] }) => void
}) {
  const queryClient = useQueryClient()
  const [historyData, setHistoryData] = useState<AssetHistoryByNameResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  // Field selection states
  const [selectedFields, setSelectedFields] = useState<{
    asset_name: boolean
    fund_type_id: boolean
    account_id: boolean
    liquidity_rating_id: boolean
  }>({
    asset_name: false,
    fund_type_id: false,
    account_id: false,
    liquidity_rating_id: false
  })

  // Field values
  const [fieldValues, setFieldValues] = useState<{
    asset_name: string
    fund_type_id: string
    account_id: string
    liquidity_rating_id: string
  }>({
    asset_name: '',
    fund_type_id: '',
    account_id: '',
    liquidity_rating_id: ''
  })

  // Load history data when modal opens
  useEffect(() => {
    if (open && assetName) {
      setIsLoading(true)
      api.getAssetHistoryByName(assetName)
        .then(data => {
          setHistoryData(data)
          // Pre-fill asset name
          setFieldValues(prev => ({ ...prev, asset_name: assetName }))
        })
        .finally(() => setIsLoading(false))
    }
  }, [open, assetName])

  // Reset state when modal closes
  useEffect(() => {
    if (!open) {
      setHistoryData(null)
      setSelectedFields({
        asset_name: false,
        fund_type_id: false,
        account_id: false,
        liquidity_rating_id: false
      })
      setFieldValues({
        asset_name: '',
        fund_type_id: '',
        account_id: '',
        liquidity_rating_id: ''
      })
    }
  }, [open])

  const leafTypes = fundTypes.filter((ft) => !fundTypes.some((c) => c.parent_id === ft.id))

  const toggleField = (field: keyof typeof selectedFields) => {
    setSelectedFields(prev => ({ ...prev, [field]: !prev[field] }))
  }

  const handleSubmit = () => {
    if (!historyData) return

    const updateData: Omit<BatchUpdateAssets, 'ids'> = {}
    const changes: string[] = []

    if (selectedFields.asset_name && fieldValues.asset_name.trim()) {
      updateData.asset_name = fieldValues.asset_name.trim()
      changes.push(`资产名称: "${assetName}" → "${fieldValues.asset_name.trim()}"`)
    }
    if (selectedFields.fund_type_id && fieldValues.fund_type_id) {
      updateData.fund_type_id = parseInt(fieldValues.fund_type_id)
      const typeName = leafTypes.find(ft => ft.id === parseInt(fieldValues.fund_type_id))?.name
      changes.push(`资产类型: 改为 "${typeName}"`)
    }
    if (selectedFields.account_id && fieldValues.account_id) {
      updateData.account_id = parseInt(fieldValues.account_id)
      const accountName = accounts.find(a => a.id === parseInt(fieldValues.account_id))?.name
      changes.push(`账户: 改为 "${accountName}"`)
    }
    if (selectedFields.liquidity_rating_id && fieldValues.liquidity_rating_id) {
      updateData.liquidity_rating_id = parseInt(fieldValues.liquidity_rating_id)
      const ratingName = liquidityRatings.find(lr => lr.id === parseInt(fieldValues.liquidity_rating_id))?.name
      changes.push(`流动性: 改为 "${ratingName}"`)
    }

    onSubmit(
      {
        ids: historyData.all_ids,
        ...updateData
      },
      {
        count: historyData.total_count,
        changes
      }
    )
  }

  const hasSelectedFields = Object.values(selectedFields).some(v => v)
  const hasValues = (
    (selectedFields.asset_name && fieldValues.asset_name.trim()) ||
    (selectedFields.fund_type_id && fieldValues.fund_type_id) ||
    (selectedFields.account_id && fieldValues.account_id) ||
    (selectedFields.liquidity_rating_id && fieldValues.liquidity_rating_id)
  )

  return (
    <Modal open={open} onClose={onClose} title={`批量修改历史属性 - ${assetName}`} size="lg">
      <div className="space-y-6">
        {isLoading ? (
          <div className="text-center py-8 text-slate-500">加载中...</div>
        ) : historyData ? (
          <>
            {/* Impact Info */}
            <div className="bg-brand-50 border border-brand-200 rounded-lg p-4">
              <div className="flex items-center gap-2 text-brand-800">
                <History size={20} />
                <span className="font-medium">将影响 {historyData.total_count} 条历史记录</span>
              </div>
              <p className="text-sm text-brand-600 mt-1">
                以下展示最新的 3 条记录作为示例
              </p>
            </div>

            {/* Sample Records */}
            {historyData.sample_records.length > 0 && (
              <div>
                <h4 className="text-sm font-medium text-slate-700 mb-3">示例记录</h4>
                <div className="max-h-40 overflow-auto border border-slate-200 rounded-lg">
                  <table className="w-full text-sm">
                    <thead className="bg-slate-50 sticky top-0">
                      <tr>
                        <th className="text-left px-3 py-2 text-slate-600 font-medium">日期</th>
                        <th className="text-left px-3 py-2 text-slate-600 font-medium">类型</th>
                        <th className="text-left px-3 py-2 text-slate-600 font-medium">账户</th>
                        <th className="text-right px-3 py-2 text-slate-600 font-medium">金额</th>
                      </tr>
                    </thead>
                    <tbody>
                      {historyData.sample_records.map((record) => (
                        <tr key={record.id} className="border-t border-slate-100">
                          <td className="px-3 py-2 text-slate-500">{formatDate(record.asset_date)}</td>
                          <td className="px-3 py-2 text-slate-500">{record.fund_type_name}</td>
                          <td className="px-3 py-2 text-slate-500">{record.account_name}</td>
                          <td className={`px-3 py-2 text-right font-mono ${parseFloat(record.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
                            {formatAmount(record.amount)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Field Selection */}
            <div className="border-t border-slate-100 pt-4">
              <h4 className="text-sm font-medium text-slate-700 mb-3">选择要修改的字段</h4>
              <div className="grid grid-cols-2 gap-4">
                {/* Asset Name */}
                <div className="space-y-2">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedFields.asset_name}
                      onChange={() => toggleField('asset_name')}
                      className="w-4 h-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-sm font-medium text-slate-700">资产名称</span>
                  </label>
                  {selectedFields.asset_name && (
                    <input
                      type="text"
                      value={fieldValues.asset_name}
                      onChange={(e) => setFieldValues(prev => ({ ...prev, asset_name: e.target.value }))}
                      placeholder="输入新名称"
                      className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                    />
                  )}
                </div>

                {/* Fund Type */}
                <div className="space-y-2">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedFields.fund_type_id}
                      onChange={() => toggleField('fund_type_id')}
                      className="w-4 h-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-sm font-medium text-slate-700">资产类型</span>
                  </label>
                  {selectedFields.fund_type_id && (
                    <select
                      value={fieldValues.fund_type_id}
                      onChange={(e) => setFieldValues(prev => ({ ...prev, fund_type_id: e.target.value }))}
                      className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                    >
                      <option value="">选择类型</option>
                      {leafTypes.map((ft) => <option key={ft.id} value={ft.id}>{ft.name}</option>)}
                    </select>
                  )}
                </div>

                {/* Account */}
                <div className="space-y-2">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedFields.account_id}
                      onChange={() => toggleField('account_id')}
                      className="w-4 h-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-sm font-medium text-slate-700">账户</span>
                  </label>
                  {selectedFields.account_id && (
                    <select
                      value={fieldValues.account_id}
                      onChange={(e) => setFieldValues(prev => ({ ...prev, account_id: e.target.value }))}
                      className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                    >
                      <option value="">选择账户</option>
                      {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  )}
                </div>

                {/* Liquidity Rating */}
                <div className="space-y-2">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedFields.liquidity_rating_id}
                      onChange={() => toggleField('liquidity_rating_id')}
                      className="w-4 h-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-sm font-medium text-slate-700">流动性</span>
                  </label>
                  {selectedFields.liquidity_rating_id && (
                    <select
                      value={fieldValues.liquidity_rating_id}
                      onChange={(e) => setFieldValues(prev => ({ ...prev, liquidity_rating_id: e.target.value }))}
                      className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                    >
                      <option value="">选择流动性</option>
                      {liquidityRatings.map((lr) => <option key={lr.id} value={lr.id}>{lr.name}</option>)}
                    </select>
                  )}
                </div>
              </div>
            </div>

            {/* Preview */}
            {hasSelectedFields && hasValues && (
              <div className="border-t border-slate-100 pt-4">
                <h4 className="text-sm font-medium text-slate-700 mb-3">修改预览</h4>
                <div className="bg-slate-50 rounded-lg p-3 space-y-2">
                  {selectedFields.asset_name && fieldValues.asset_name && (
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-slate-500">资产名称:</span>
                      <span className="text-slate-400 line-through">{assetName}</span>
                      <span className="text-brand-600">→ {fieldValues.asset_name}</span>
                    </div>
                  )}
                  {selectedFields.fund_type_id && fieldValues.fund_type_id && (
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-slate-500">资产类型:</span>
                      <span className="text-brand-600">
                        改为 {leafTypes.find(ft => ft.id === parseInt(fieldValues.fund_type_id))?.name}
                      </span>
                    </div>
                  )}
                  {selectedFields.account_id && fieldValues.account_id && (
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-slate-500">账户:</span>
                      <span className="text-brand-600">
                        改为 {accounts.find(a => a.id === parseInt(fieldValues.account_id))?.name}
                      </span>
                    </div>
                  )}
                  {selectedFields.liquidity_rating_id && fieldValues.liquidity_rating_id && (
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-slate-500">流动性:</span>
                      <span className="text-brand-600">
                        改为 {liquidityRatings.find(lr => lr.id === parseInt(fieldValues.liquidity_rating_id))?.name}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Actions */}
            <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
              <button onClick={onClose}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
              <button
                onClick={handleSubmit}
                disabled={!hasSelectedFields || !hasValues}
                className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                确认修改 ({historyData.total_count} 条记录)
              </button>
            </div>
          </>
        ) : (
          <div className="text-center py-8 text-slate-500">暂无数据</div>
        )}
      </div>
    </Modal>
  )
}

// --- New Batch Edit Modal ---
function NewBatchEditModal({
  open,
  onClose,
  fundTypes,
  accounts,
  liquidityRatings,
  assetNames,
  onSubmit
}: {
  open: boolean
  onClose: () => void
  fundTypes: FundType[]
  accounts: Account[]
  liquidityRatings: import('@/types').LiquidityRating[]
  assetNames: string[]
  onSubmit: (data: BatchUpdateAssets) => void
}) {
  const queryClient = useQueryClient()
  const [selectedAssetNames, setSelectedAssetNames] = useState<string[]>([])
  const [matchedRecords, setMatchedRecords] = useState<AssetRecord[]>([])

  const [updates, setUpdates] = useState<{
    asset_name: string
    fund_type_id: string
    account_id: string
    liquidity_rating_id: string
  }>({
    asset_name: '',
    fund_type_id: '',
    account_id: '',
    liquidity_rating_id: ''
  })

  // Load records when asset names are selected
  useEffect(() => {
    if (selectedAssetNames.length > 0) {
      api.getAssetsByNames(selectedAssetNames).then(records => {
        setMatchedRecords(records)
      })
    } else {
      setMatchedRecords([])
    }
  }, [selectedAssetNames])

  const leafTypes = fundTypes.filter((ft) => !fundTypes.some((c) => c.parent_id === ft.id))

  const handleSubmit = () => {
    const updateData: Omit<BatchUpdateAssets, 'ids'> = {}
    if (updates.asset_name.trim()) updateData.asset_name = updates.asset_name.trim()
    if (updates.fund_type_id) updateData.fund_type_id = parseInt(updates.fund_type_id)
    if (updates.account_id) updateData.account_id = parseInt(updates.account_id)
    if (updates.liquidity_rating_id) updateData.liquidity_rating_id = parseInt(updates.liquidity_rating_id)

    onSubmit({
      ids: matchedRecords.map(r => r.id),
      ...updateData
    })
  }

  const hasChanges = updates.asset_name.trim() || updates.fund_type_id || updates.account_id || updates.liquidity_rating_id
  const hasSelectedAssets = selectedAssetNames.length > 0

  return (
    <Modal open={open} onClose={onClose} title="批量修改资产" size="xl">
      <div className="space-y-6">
        {/* Step 1: Select Asset Names */}
        {/* <div className="bg-slate-50 rounded-lg p-4"> */}
          <h4 className="text-sm font-medium text-slate-700 mb-3">步骤 1: 选择要修改的资产</h4>
          <MultiSelectDropdown
            label="资产名称"
            options={assetNames}
            selected={selectedAssetNames}
            onChange={setSelectedAssetNames}
            placeholder="请选择资产名称（可多选）"
          />
          {hasSelectedAssets && (
            <p className="mt-2 text-sm text-brand-600">
              已选择 {selectedAssetNames.length} 个资产，共 {matchedRecords.length} 条记录
            </p>
          )}
        {/* </div> */}

        {/* Step 2: Show matched records */}
        {hasSelectedAssets && matchedRecords.length > 0 && (
          <div>
            <h4 className="text-sm font-medium text-slate-700 mb-3">匹配的记录</h4>
            <div className="max-h-48 overflow-auto border border-slate-200 rounded-lg">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-slate-600 font-medium">日期</th>
                    <th className="text-left px-3 py-2 text-slate-600 font-medium">资产名称</th>
                    <th className="text-left px-3 py-2 text-slate-600 font-medium">类型</th>
                    <th className="text-left px-3 py-2 text-slate-600 font-medium">账户</th>
                    <th className="text-right px-3 py-2 text-slate-600 font-medium">金额</th>
                  </tr>
                </thead>
                <tbody>
                  {matchedRecords.map((record) => (
                    <tr key={record.id} className="border-t border-slate-100">
                      <td className="px-3 py-2 text-slate-500">{formatDate(record.asset_date)}</td>
                      <td className="px-3 py-2 font-medium text-brand-950">{record.asset_name}</td>
                      <td className="px-3 py-2 text-slate-500">{record.fund_type_name}</td>
                      <td className="px-3 py-2 text-slate-500">{record.account_name}</td>
                      <td className={`px-3 py-2 text-right font-mono ${parseFloat(record.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
                        {formatAmount(record.amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Step 3: Edit Fields */}
        {hasSelectedAssets && matchedRecords.length > 0 && (
          <div className="border-t border-slate-100 pt-4">
            <h4 className="text-sm font-medium text-slate-700 mb-3">步骤 2: 设置要修改的字段（留空表示不修改）</h4>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-slate-600 mb-1">资产名称</label>
                <input
                  type="text"
                  value={updates.asset_name}
                  onChange={(e) => setUpdates({ ...updates, asset_name: e.target.value })}
                  placeholder="不修改"
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-600 mb-1">资产类型</label>
                <select
                  value={updates.fund_type_id}
                  onChange={(e) => setUpdates({ ...updates, fund_type_id: e.target.value })}
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                >
                  <option value="">不修改</option>
                  {leafTypes.map((ft) => <option key={ft.id} value={ft.id}>{ft.name}</option>)}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-600 mb-1">账户</label>
                <select
                  value={updates.account_id}
                  onChange={(e) => setUpdates({ ...updates, account_id: e.target.value })}
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                >
                  <option value="">不修改</option>
                  {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-600 mb-1">流动性</label>
                <select
                  value={updates.liquidity_rating_id}
                  onChange={(e) => setUpdates({ ...updates, liquidity_rating_id: e.target.value })}
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
                >
                  <option value="">不修改</option>
                  {liquidityRatings.map((lr) => <option key={lr.id} value={lr.id}>{lr.name}</option>)}
                </select>
              </div>
            </div>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-4 border-t border-slate-100">
          <button onClick={onClose}
            className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
          <button
            onClick={handleSubmit}
            disabled={!hasChanges || matchedRecords.length === 0}
            className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            确认修改 ({matchedRecords.length} 条记录)
          </button>
        </div>
      </div>
    </Modal>
  )
}

// --- Record Form ---
function RecordForm({ fundTypes, accounts, liquidityRatings, initial, onSubmit, onCancel, onBatchHistory, isEditing = false }: {
  fundTypes: FundType[]; accounts: Account[]; liquidityRatings: import('@/types').LiquidityRating[]
  initial?: Partial<AssetRecordCreate>; onSubmit: (data: AssetRecordCreate) => void; onCancel: () => void
  onBatchHistory?: () => void
  isEditing?: boolean
}) {
  const [form, setForm] = useState<AssetRecordCreate>({
    asset_date: initial?.asset_date || new Date().toISOString().split('T')[0],
    liquidity_rating_id: initial?.liquidity_rating_id || (liquidityRatings[0]?.id ?? 1),
    fund_type_id: initial?.fund_type_id || (fundTypes[0]?.id ?? 1),
    asset_name: initial?.asset_name || '',
    account_id: initial?.account_id || (accounts[0]?.id ?? 1),
    amount: initial?.amount || '0',
  })

  const leafTypes = fundTypes.filter((ft) => !fundTypes.some((c) => c.parent_id === ft.id))

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">日期</label>
          <input type="date" value={form.asset_date}
            onChange={(e) => setForm({ ...form, asset_date: e.target.value })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400" />
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">流动性</label>
          <select value={form.liquidity_rating_id}
            onChange={(e) => setForm({ ...form, liquidity_rating_id: parseInt(e.target.value) })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400">
            {liquidityRatings.map((lr) => <option key={lr.id} value={lr.id}>{lr.name}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">资产类型</label>
          <select value={form.fund_type_id}
            onChange={(e) => setForm({ ...form, fund_type_id: parseInt(e.target.value) })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400">
            {leafTypes.map((ft) => <option key={ft.id} value={ft.id}>{ft.name}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">账户</label>
          <select value={form.account_id}
            onChange={(e) => setForm({ ...form, account_id: parseInt(e.target.value) })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400">
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">资产名称</label>
          <input type="text" value={form.asset_name}
            onChange={(e) => setForm({ ...form, asset_name: e.target.value })}
            placeholder="如: 支付宝-余额宝"
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400" />
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">金额 (元)</label>
          <input type="number" step="0.01" value={form.amount}
            onChange={(e) => setForm({ ...form, amount: e.target.value })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400" />
        </div>
      </div>
      <div className="flex justify-between items-center pt-2">
        {isEditing && onBatchHistory && (
          <button
            onClick={onBatchHistory}
            className="flex items-center gap-1.5 px-4 py-2 text-sm text-brand-700 border border-brand-300 rounded-lg hover:bg-brand-50"
          >
            <History size={16} /> 批量修改历史属性
          </button>
        )}
        <div className="flex gap-3 ml-auto">
          <button onClick={onCancel}
            className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
          <button onClick={() => onSubmit(form)}
            className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700">保存</button>
        </div>
      </div>
    </div>
  )
}

// --- Copy From Last Period ---
function CopyFromLastPanel({ fundTypes, accounts, onClose }: {
  fundTypes: FundType[]; accounts: Account[]; onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [newRecordDate, setNewRecordDate] = useState(new Date().toISOString().split('T')[0])
  const [periodType, setPeriodType] = useState('month')
  const [drafts, setDrafts] = useState<Record<string, unknown>[]>([])
  const [sourceDate, setSourceDate] = useState<string>('')
  const [sortConfig, setSortConfig] = useState<{ key: string; direction: 'asc' | 'desc' } | null>(null)

  const copyMutation = useMutation({
    mutationFn: () => api.copyFromLast(newRecordDate, periodType),
    onSuccess: (data) => {
      setDrafts(data.records)
      setSourceDate(data.source_date)
    },
  })

  const saveMutation = useMutation({
    mutationFn: (records: AssetRecordCreate[]) => api.batchCreate(records),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      onClose()
    },
  })

  const handleSave = () => {
    const records = drafts.map((d) => ({
      asset_date: newRecordDate,
      liquidity_rating_id: d.liquidity_rating_id as number,
      fund_type_id: d.fund_type_id as number,
      asset_name: d.asset_name as string,
      account_id: d.account_id as number,
      amount: d.amount as string,
    }))
    saveMutation.mutate(records)
  }

  // Sorting function
  const handleSort = (key: string) => {
    let direction: 'asc' | 'desc' = 'asc'
    if (sortConfig && sortConfig.key === key && sortConfig.direction === 'asc') {
      direction = 'desc'
    }
    setSortConfig({ key, direction })

    const sortedDrafts = [...drafts].sort((a, b) => {
      const aValue = (a[key] as string) || ''
      const bValue = (b[key] as string) || ''
      if (direction === 'asc') {
        return aValue.localeCompare(bValue)
      }
      return bValue.localeCompare(aValue)
    })
    setDrafts(sortedDrafts)
  }

  const getSortIcon = (key: string) => {
    if (!sortConfig || sortConfig.key !== key) {
      return <span className="text-slate-300 ml-1">↕</span>
    }
    return sortConfig.direction === 'asc' ? <span className="text-brand-600 ml-1">↑</span> : <span className="text-brand-600 ml-1">↓</span>
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-4 items-end">
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">新记录日期</label>
          <input type="date" value={newRecordDate} onChange={(e) => setNewRecordDate(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400" />
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-600 mb-1">账期类型</label>
          <select value={periodType} onChange={(e) => setPeriodType(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400">
            {PERIOD_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <button onClick={() => copyMutation.mutate()}
          disabled={copyMutation.isPending}
          className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50">
          {copyMutation.isPending ? '加载中...' : '加载上期数据'}
        </button>
      </div>

      {sourceDate && <p className="text-sm text-slate-500">数据来源: {sourceDate}，共 {drafts.length} 条</p>}

      {drafts.length > 0 && (
        <>
          <div className="max-h-[400px] overflow-auto border border-slate-200 rounded-lg">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 sticky top-0">
                <tr>
                  <th
                    className="text-left px-3 py-2 text-slate-600 font-medium cursor-pointer hover:bg-slate-100 select-none"
                    onClick={() => handleSort('asset_name')}
                  >
                    资产名称 {getSortIcon('asset_name')}
                  </th>
                  <th
                    className="text-left px-3 py-2 text-slate-600 font-medium cursor-pointer hover:bg-slate-100 select-none"
                    onClick={() => handleSort('account_name')}
                  >
                    账户 {getSortIcon('account_name')}
                  </th>
                  <th className="text-right px-3 py-2 text-slate-600 font-medium">金额</th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((d, i) => (
                  <tr key={i} className="border-t border-slate-100 hover:bg-slate-50/50">
                    <td className="px-3 py-2">{d.asset_name as string}</td>
                    <td className="px-3 py-2 text-slate-500">{d.account_name as string}</td>
                    <td className="px-3 py-2 text-right">
                      <input type="number" step="0.01"
                        value={d.amount as string}
                        onChange={(e) => {
                          const newDrafts = [...drafts]
                          newDrafts[i] = { ...newDrafts[i], amount: e.target.value }
                          setDrafts(newDrafts)
                        }}
                        className="w-32 text-right px-2 py-1 border border-slate-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-brand-400" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex justify-end gap-3">
            <button onClick={onClose}
              className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">取消</button>
            <button onClick={handleSave} disabled={saveMutation.isPending}
              className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50">
              {saveMutation.isPending ? '保存中...' : `保存 ${drafts.length} 条记录`}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

// --- Batch Create By Period Panel ---
function BatchCreateByPeriodPanel({
  fundTypes,
  accounts,
  liquidityRatings,
  onClose,
}: {
  fundTypes: FundType[]
  accounts: Account[]
  liquidityRatings: LiquidityRating[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [step, setStep] = useState<'form' | 'preview' | 'result'>('form')
  const [periodType, setPeriodType] = useState('month')
  const [startPeriod, setStartPeriod] = useState('')
  const [endPeriod, setEndPeriod] = useState('')
  const [form, setForm] = useState({
    liquidity_rating_id: liquidityRatings[0]?.id || 0,
    fund_type_id: '',
    account_id: '',
    asset_name: '',
    amount: '',
  })
  const [previewResult, setPreviewResult] = useState<BatchCreateByPeriodResult | null>(null)
  const [conflictResolution, setConflictResolution] = useState<'skip' | 'overwrite'>('skip')

  // 获取叶子节点资产类型
  const leafTypes = useMemo(() => {
    const leaves: FundType[] = []
    function traverse(nodes: FundType[]) {
      nodes.forEach((node) => {
        if (!node.children || node.children.length === 0) {
          leaves.push(node)
        } else {
          traverse(node.children)
        }
      })
    }
    traverse(fundTypes)
    return leaves
  }, [fundTypes])

  // 初始化默认值
  useEffect(() => {
    if (leafTypes.length > 0 && !form.fund_type_id) {
      setForm((prev) => ({ ...prev, fund_type_id: String(leafTypes[0].id) }))
    }
    if (accounts.length > 0 && !form.account_id) {
      setForm((prev) => ({ ...prev, account_id: String(accounts[0].id) }))
    }
  }, [leafTypes, accounts])

  const batchCreateMutation = useMutation({
    mutationFn: () =>
      api.batchCreateByPeriod({
        record_template: {
          liquidity_rating_id: Number(form.liquidity_rating_id),
          fund_type_id: Number(form.fund_type_id),
          account_id: Number(form.account_id),
          asset_name: form.asset_name,
          amount: Number(form.amount).toFixed(2),
        },
        period_type: periodType,
        start_period: startPeriod || null,
        end_period: endPeriod || null,
        conflict_resolution: conflictResolution,
      }),
    onSuccess: (data) => {
      setPreviewResult(data)
      setStep('result')
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
    },
  })

  const handlePreview = () => {
    if (!form.asset_name || !form.amount) {
      alert('请填写完整的记录信息')
      return
    }
    setStep('preview')
  }

  const handleConfirm = () => {
    batchCreateMutation.mutate()
  }

  // 获取账期示例
  const getPeriodExample = () => {
    switch (periodType) {
      case 'day':
        return '如: 2024-03-15'
      case 'month':
        return '如: 2024-03'
      case 'quarter':
        return '如: 2024-Q1'
      case 'year':
        return '如: 2024'
      default:
        return ''
    }
  }

  if (step === 'result' && previewResult) {
    return (
      <div className="space-y-4">
        <div className={`p-4 rounded-lg ${previewResult.success ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
          <div className="flex items-center gap-2 mb-2">
            {previewResult.success ? (
              <Check size={20} className="text-green-600" />
            ) : (
              <AlertTriangle size={20} className="text-red-600" />
            )}
            <span className={`font-medium ${previewResult.success ? 'text-green-800' : 'text-red-800'}`}>
              {previewResult.success ? '操作成功' : '操作失败'}
            </span>
          </div>
          <p className="text-sm text-slate-600">{previewResult.message}</p>
          {previewResult.success && (
            <div className="mt-3 text-sm text-slate-600">
              <p>成功创建 <strong>{previewResult.created_count}</strong> 条记录</p>
              <p className="mt-1">涉及账期: {previewResult.periods.join(', ')}</p>
            </div>
          )}
        </div>
        <div className="flex justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700">
            完成
          </button>
        </div>
      </div>
    )
  }

  if (step === 'preview') {
    return (
      <div className="space-y-4">
        <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <h4 className="font-medium text-blue-800 mb-2">操作预览</h4>
          <div className="text-sm text-slate-600 space-y-1">
            <p><strong>账期类型:</strong> {PERIOD_OPTIONS.find((o) => o.value === periodType)?.label}</p>
            <p><strong>账期范围:</strong> {startPeriod || '最早'} 至 {endPeriod || '最新'}</p>
            <p><strong>资产名称:</strong> {form.asset_name}</p>
            <p><strong>资产类型:</strong> {leafTypes.find((t) => String(t.id) === form.fund_type_id)?.name}</p>
            <p><strong>账户:</strong> {accounts.find((a) => String(a.id) === form.account_id)?.name}</p>
            <p><strong>金额:</strong> {formatAmount(form.amount)}</p>
          </div>
        </div>
        <div className="p-4 bg-yellow-50 border border-yellow-200 rounded-lg space-y-3">
          <p className="text-sm text-yellow-700">
            <AlertTriangle size={14} className="inline mr-1" />
            将在每个账期的最后一天创建记录，请确认信息无误
          </p>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2">如遇重复记录</label>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  value="skip"
                  checked={conflictResolution === 'skip'}
                  onChange={(e) => setConflictResolution(e.target.value as 'skip' | 'overwrite')}
                  className="w-4 h-4 text-brand-600"
                />
                <span className="text-sm text-slate-600">跳过（保留已有记录）</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  value="overwrite"
                  checked={conflictResolution === 'overwrite'}
                  onChange={(e) => setConflictResolution(e.target.value as 'skip' | 'overwrite')}
                  className="w-4 h-4 text-brand-600"
                />
                <span className="text-sm text-slate-600">覆盖（更新为新的记录）</span>
              </label>
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-3">
          <button onClick={() => setStep('form')} className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">
            返回修改
          </button>
          <button
            onClick={handleConfirm}
            disabled={batchCreateMutation.isPending}
            className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700 disabled:opacity-50"
          >
            {batchCreateMutation.isPending ? '处理中...' : '确认添加'}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* 账期设置 */}
      <div className="p-4 bg-slate-50 rounded-lg space-y-3">
        <h4 className="font-medium text-slate-700">账期设置</h4>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">账期类型</label>
            <select
              value={periodType}
              onChange={(e) => setPeriodType(e.target.value)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              {PERIOD_OPTIONS.filter((o) => o.value !== 'all').map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">开始账期</label>
            <input
              type="text"
              value={startPeriod}
              onChange={(e) => setStartPeriod(e.target.value)}
              placeholder={getPeriodExample()}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
            <p className="text-xs text-slate-400 mt-1">留空表示从最早账期开始</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">结束账期</label>
            <input
              type="text"
              value={endPeriod}
              onChange={(e) => setEndPeriod(e.target.value)}
              placeholder={getPeriodExample()}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
            <p className="text-xs text-slate-400 mt-1">留空表示到最新账期结束</p>
          </div>
        </div>
      </div>

      {/* 记录模板 */}
      <div className="p-4 bg-slate-50 rounded-lg space-y-3">
        <h4 className="font-medium text-slate-700">记录信息</h4>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">流动性评级</label>
            <select
              value={form.liquidity_rating_id}
              onChange={(e) => setForm({ ...form, liquidity_rating_id: Number(e.target.value) })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              {liquidityRatings.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">资产类型</label>
            <select
              value={form.fund_type_id}
              onChange={(e) => setForm({ ...form, fund_type_id: e.target.value })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              {leafTypes.map((ft) => (
                <option key={ft.id} value={ft.id}>
                  {ft.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">账户</label>
            <select
              value={form.account_id}
              onChange={(e) => setForm({ ...form, account_id: e.target.value })}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">资产名称</label>
            <input
              type="text"
              value={form.asset_name}
              onChange={(e) => setForm({ ...form, asset_name: e.target.value })}
              placeholder="如: 定期存款"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-slate-600 mb-1">金额 (元)</label>
            <input
              type="number"
              step="0.01"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
              placeholder="0.00"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
          </div>
        </div>
      </div>

      <div className="flex justify-end gap-3">
        <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">
          取消
        </button>
        <button onClick={handlePreview} className="px-4 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700">
          下一步
        </button>
      </div>
    </div>
  )
}

// --- Main Page ---
export default function RecordsPage() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [searchName, setSearchName] = useState('')
  const [showAddModal, setShowAddModal] = useState(false)
  const [showCopyModal, setShowCopyModal] = useState(false)
  const [showBatchCreateByPeriodModal, setShowBatchCreateByPeriodModal] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [showBatchEditModal, setShowBatchEditModal] = useState(false)
  const [showBatchHistoryModal, setShowBatchHistoryModal] = useState(false)
  const [editingAssetName, setEditingAssetName] = useState<string>('')
  const [batchUpdateSummary, setBatchUpdateSummary] = useState<{
    show: boolean
    count: number
    changes: string[]
  }>({ show: false, count: 0, changes: [] })

  // Period type filtering
  const [periodType, setPeriodType] = useState<string>('custom')
  const [year, setYear] = useState<number | ''>('')
  const [quarter, setQuarter] = useState<number | ''>('')
  const [month, setMonth] = useState<number | ''>('')
  const [day, setDay] = useState<number | ''>('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const params: Record<string, string> = { page: String(page), page_size: String(pageSize) }
  if (searchName) params.asset_name = searchName

  // Handle period type params
  if (periodType && periodType !== 'all') {
    params.period_type = periodType
    if (periodType === 'year' && year) {
      params.year = String(year)
    } else if (periodType === 'quarter' && year && quarter) {
      params.year = String(year)
      params.quarter = String(quarter)
    } else if (periodType === 'month' && year && month) {
      params.year = String(year)
      params.month = String(month)
    } else if (periodType === 'day' && year && month && day) {
      params.year = String(year)
      params.month = String(month)
      params.day = String(day)
    } else if (periodType === 'custom') {
      if (startDate) params.date_from = startDate
      if (endDate) params.date_to = endDate
    }
  }

  const { data, isLoading } = useQuery({
    queryKey: ['assets', params],
    queryFn: () => api.getAssets(params),
  })
  const { data: fundTypes } = useQuery({ queryKey: ['fundTypes'], queryFn: api.getFundTypes })
  const { data: accounts } = useQuery({ queryKey: ['accounts'], queryFn: api.getAccounts })
  const { data: liquidityRatings } = useQuery({ queryKey: ['liquidityRatings'], queryFn: api.getLiquidityRatings })
  const { data: assetNames } = useQuery({ queryKey: ['assetNames'], queryFn: api.getAssetNames })

  const createMutation = useMutation({
    mutationFn: api.createAsset,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      setShowAddModal(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteAsset,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['assets'] }),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<AssetRecordCreate> }) => api.updateAsset(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setEditingId(null)
    },
  })

  const batchUpdateMutation = useMutation({
    mutationFn: (data: BatchUpdateAssets) => api.batchUpdateAssets(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setShowBatchEditModal(false)
    },
  })

  // Batch selection states
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [showBatchDeleteConfirm, setShowBatchDeleteConfirm] = useState(false)

  const batchDeleteMutation = useMutation({
    mutationFn: (ids: number[]) => api.batchDeleteAssets(ids),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setSelectedIds(new Set())
      setShowBatchDeleteConfirm(false)
    },
  })

  // Selection handlers
  const toggleSelect = (id: number) => {
    const newSet = new Set(selectedIds)
    if (newSet.has(id)) {
      newSet.delete(id)
    } else {
      newSet.add(id)
    }
    setSelectedIds(newSet)
  }

  const toggleSelectAll = () => {
    if (data?.items) {
      if (selectedIds.size === data.items.length) {
        setSelectedIds(new Set())
      } else {
        setSelectedIds(new Set(data.items.map(r => r.id)))
      }
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Batch Update Summary Toast */}
      {batchUpdateSummary.show && (
        <div className="fixed top-4 right-4 z-50 bg-green-50 border border-green-200 rounded-lg shadow-lg p-4 max-w-md animate-slide-in-right">
          <div className="flex items-start gap-3">
            <div className="flex-shrink-0 w-8 h-8 bg-green-100 rounded-full flex items-center justify-center">
              <Check size={16} className="text-green-600" />
            </div>
            <div className="flex-1">
              <h4 className="text-sm font-medium text-green-800">
                批量修改成功
              </h4>
              <p className="text-sm text-green-700 mt-1">
                已修改 {batchUpdateSummary.count} 条历史记录
              </p>
              {batchUpdateSummary.changes.length > 0 && (
                <div className="mt-2 space-y-1">
                  <p className="text-xs text-green-600 font-medium">修改内容：</p>
                  <ul className="text-xs text-green-700 space-y-0.5">
                    {batchUpdateSummary.changes.map((change, idx) => (
                      <li key={idx} className="flex items-center gap-1">
                        <span className="w-1 h-1 bg-green-500 rounded-full"></span>
                        {change}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <button
              onClick={() => setBatchUpdateSummary(prev => ({ ...prev, show: false }))}
              className="text-green-400 hover:text-green-600"
            >
              <X size={16} />
            </button>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产记录</h1>
          <p className="text-sm text-slate-500 mt-1">共 {data?.total || 0} 条记录</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowBatchEditModal(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-sm border border-brand-300 rounded-lg hover:bg-brand-50 text-brand-700">
            <Edit3 size={16} /> 批量修改
          </button>
          <button onClick={() => setShowCopyModal(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-sm border border-slate-200 rounded-lg hover:bg-slate-50 text-slate-700">
            <Copy size={16} /> 复制上期
          </button>
          <button onClick={() => setShowBatchCreateByPeriodModal(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-sm border border-brand-300 rounded-lg hover:bg-brand-50 text-brand-700">
            <CalendarPlus size={16} /> 批量按账期添加
          </button>
          <button onClick={() => setShowAddModal(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-white bg-brand-600 rounded-lg hover:bg-brand-700">
            <Plus size={16} /> 新增记录
          </button>
        </div>
      </div>

      {/* Filters */}
        <div className="flex flex-wrap gap-3 items-center">
          {/* Period Type Selector */}
          <select
            value={periodType}
            onChange={(e) => {
              setPeriodType(e.target.value)
              setYear('')
              setQuarter('')
              setMonth('')
              setDay('')
              setStartDate('')
              setEndDate('')
              setPage(1)
            }}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
          >
            <option value="all">全部</option>
            <option value="year">按年</option>
            <option value="quarter">按季度</option>
            <option value="month">按月</option>
            <option value="day">按日</option>
            <option value="custom">自定义日期范围</option>
          </select>

          {/* Year Selector */}
          {(periodType === 'year' || periodType === 'quarter' || periodType === 'month' || periodType === 'day') && (
            <select
              value={year}
              onChange={(e) => { setYear(Number(e.target.value) || ''); setPage(1) }}
              className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              <option value="">选择年份</option>
              {Array.from({ length: 10 }, (_, i) => dayjs().year() - 5 + i).map(y => (
                <option key={y} value={y}>{y}年</option>
              ))}
            </select>
          )}

          {/* Quarter Selector */}
          {periodType === 'quarter' && (
            <select
              value={quarter}
              onChange={(e) => { setQuarter(Number(e.target.value) || ''); setPage(1) }}
              className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              <option value="">选择季度</option>
              {[1, 2, 3, 4].map(q => (
                <option key={q} value={q}>第{q}季度</option>
              ))}
            </select>
          )}

          {/* Month Selector */}
          {(periodType === 'month' || periodType === 'day') && (
            <select
              value={month}
              onChange={(e) => { setMonth(Number(e.target.value) || ''); setPage(1) }}
              className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              <option value="">选择月份</option>
              {Array.from({ length: 12 }, (_, i) => i + 1).map(m => (
                <option key={m} value={m}>{m}月</option>
              ))}
            </select>
          )}

          {/* Day Selector */}
          {periodType === 'day' && (
            <select
              value={day}
              onChange={(e) => { setDay(Number(e.target.value) || ''); setPage(1) }}
              className="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 bg-white"
            >
              <option value="">选择日期</option>
              {Array.from({ length: 31 }, (_, i) => i + 1).map(d => (
                <option key={d} value={d}>{d}日</option>
              ))}
            </select>
          )}

          {/* Custom Date Range */}
          {periodType === 'custom' && (
            <div className="flex items-center gap-2">
              <DatePicker
                placeholder="开始日期"
                value={startDate ? dayjs(startDate) : null}
                onChange={(date) => {
                  setStartDate(date ? date.format('YYYY-MM-DD') : '')
                  setPage(1)
                }}
                style={{ width: 140 }}
              />
              <span className="text-slate-400">-</span>
              <DatePicker
                placeholder="结束日期"
                value={endDate ? dayjs(endDate) : null}
                onChange={(date) => {
                  setEndDate(date ? date.format('YYYY-MM-DD') : '')
                  setPage(1)
                }}
                style={{ width: 140 }}
              />
            </div>
          )}

        {/* Search */}
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input type="text" placeholder="搜索资产名称..." value={searchName}
            onChange={(e) => { setSearchName(e.target.value); setPage(1) }}
            className="pl-9 pr-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 w-56" />
        </div>
      </div>

      {/* Batch Action Toolbar */}
      {selectedIds.size > 0 && (
        <div className="flex items-center justify-between bg-red-50 border border-red-200 rounded-lg px-4 py-3">
          <div className="flex items-center gap-2">
            <CheckSquare size={18} className="text-red-600" />
            <span className="text-sm text-red-800">
              已选择 <strong>{selectedIds.size}</strong> 条记录
            </span>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setShowBatchDeleteConfirm(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-white bg-red-600 rounded-lg hover:bg-red-700"
            >
              <Trash2 size={14} /> 批量删除
            </button>
            <button
              onClick={() => setSelectedIds(new Set())}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 bg-white border border-slate-300 rounded-lg hover:bg-slate-50"
            >
              <X size={14} /> 取消选择
            </button>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded-xl shadow-card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-100">
              <th className="text-left px-4 py-3 text-slate-600 font-medium w-10">
                <button
                  onClick={toggleSelectAll}
                  className="p-1 rounded hover:bg-slate-200"
                  title={selectedIds.size === (data?.items?.length || 0) ? "取消全选" : "全选"}
                >
                  {selectedIds.size === (data?.items?.length || 0) && (data?.items?.length || 0) > 0 ? (
                    <CheckSquare size={18} className="text-brand-600" />
                  ) : (
                    <Square size={18} className="text-slate-400" />
                  )}
                </button>
              </th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">日期</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">资产名称</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">类型</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">账户</th>
              <th className="text-left px-4 py-3 text-slate-600 font-medium">流动性</th>
              <th className="text-right px-4 py-3 text-slate-600 font-medium">金额</th>
              <th className="text-right px-4 py-3 text-slate-600 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.map((record) => (
              <tr key={record.id} className="border-t border-slate-50 hover:bg-slate-50/50 transition-colors">
                <td className="px-4 py-3">
                  <button
                    onClick={() => toggleSelect(record.id)}
                    className="p-1 rounded hover:bg-slate-200"
                  >
                    {selectedIds.has(record.id) ? (
                      <CheckSquare size={18} className="text-brand-600" />
                    ) : (
                      <Square size={18} className="text-slate-400" />
                    )}
                  </button>
                </td>
                <td className="px-4 py-3 text-slate-500">{formatDate(record.asset_date)}</td>
                <td className="px-4 py-3 font-medium text-brand-950">{record.asset_name}</td>
                <td className="px-4 py-3 text-slate-500">{record.fund_type_name}</td>
                <td className="px-4 py-3 text-slate-500">{record.account_name}</td>
                <td className="px-4 py-3">
                  <span className="inline-block px-2 py-0.5 rounded text-xs bg-brand-50 text-brand-700">
                    {record.liquidity_rating_name}
                  </span>
                </td>
                <td className={`px-4 py-3 text-right font-mono ${parseFloat(record.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
                  {formatAmount(record.amount)}
                </td>
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-1">
                    <button onClick={() => setEditingId(record.id)}
                      className="p-1.5 rounded hover:bg-slate-100 text-slate-400 hover:text-brand-600">
                      <Pencil size={14} />
                    </button>
                    <button onClick={() => { if (confirm('确认删除?')) deleteMutation.mutate(record.id) }}
                      className="p-1.5 rounded hover:bg-red-50 text-slate-400 hover:text-danger">
                      <Trash2 size={14} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {isLoading && (
              <tr><td colSpan={8} className="px-4 py-12 text-center text-slate-400">加载中...</td></tr>
            )}
            {!isLoading && data?.items.length === 0 && (
              <tr><td colSpan={8} className="px-4 py-12 text-center text-slate-400">暂无数据</td></tr>
            )}
          </tbody>
        </table>

        {/* Pagination */}
        {data && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-slate-100">
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2">
                <span className="text-sm text-slate-500">第</span>
                <input
                  type="number"
                  min={1}
                  max={data.total_pages}
                  value={page}
                  onChange={(e) => {
                    const newPage = parseInt(e.target.value) || 1
                    if (newPage >= 1 && newPage <= data.total_pages) {
                      setPage(newPage)
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      const newPage = parseInt((e.target as HTMLInputElement).value) || 1
                      const validPage = Math.max(1, Math.min(data.total_pages, newPage))
                      setPage(validPage)
                    }
                  }}
                  className="w-16 px-2 py-1 text-sm text-center border border-slate-200 rounded focus:outline-none focus:ring-2 focus:ring-brand-400"
                />
                <span className="text-sm text-slate-500">/ {data.total_pages} 页</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm text-slate-500">每页</span>
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value))
                    setPage(1)
                  }}
                  className="px-2 py-1 text-sm border border-slate-200 rounded focus:outline-none focus:ring-2 focus:ring-brand-400"
                >
                  {[10, 30, 50, 100, 300, 500, 1000].map(size => (
                    <option key={size} value={size}>{size}</option>
                  ))}
                </select>
                <span className="text-sm text-slate-500">条</span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button 
                onClick={() => setPage(Math.max(1, page - 10))} 
                disabled={page <= 1}
                className="px-2 py-1.5 text-xs rounded hover:bg-slate-100 disabled:opacity-30"
              >
                -10
              </button>
              <button onClick={() => setPage(Math.max(1, page - 1))} disabled={page <= 1}
                className="p-1.5 rounded hover:bg-slate-100 disabled:opacity-30"><ChevronLeft size={16} /></button>
              <button onClick={() => setPage(Math.min(data.total_pages, page + 1))} disabled={page >= data.total_pages}
                className="p-1.5 rounded hover:bg-slate-100 disabled:opacity-30"><ChevronRight size={16} /></button>
              <button 
                onClick={() => setPage(Math.min(data.total_pages, page + 10))} 
                disabled={page >= data.total_pages}
                className="px-2 py-1.5 text-xs rounded hover:bg-slate-100 disabled:opacity-30"
              >
                +10
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Add Modal */}
      <Modal open={showAddModal} onClose={() => setShowAddModal(false)} title="新增资产记录">
        {fundTypes && accounts && liquidityRatings && (
          <RecordForm fundTypes={fundTypes} accounts={accounts} liquidityRatings={liquidityRatings}
            onSubmit={(data) => createMutation.mutate(data)} onCancel={() => setShowAddModal(false)} />
        )}
      </Modal>

      {/* Edit Modal */}
      <Modal open={editingId !== null} onClose={() => setEditingId(null)} title="编辑资产记录">
        {editingId && fundTypes && accounts && liquidityRatings && (() => {
          const record = data?.items.find((r) => r.id === editingId)
          if (!record) return null
          return (
            <RecordForm fundTypes={fundTypes} accounts={accounts} liquidityRatings={liquidityRatings}
              initial={{
                asset_date: record.asset_date,
                liquidity_rating_id: record.liquidity_rating_id,
                fund_type_id: record.fund_type_id,
                asset_name: record.asset_name,
                account_id: record.account_id,
                amount: record.amount,
              }}
              onSubmit={(data) => updateMutation.mutate({ id: editingId, data })}
              onCancel={() => setEditingId(null)}
              onBatchHistory={() => {
                setEditingAssetName(record.asset_name)
                setShowBatchHistoryModal(true)
              }}
              isEditing={true} />
          )
        })()}
      </Modal>

      {/* Copy Modal */}
      <Modal open={showCopyModal} onClose={() => setShowCopyModal(false)} title="复制上期记录">
        {fundTypes && accounts && (
          <CopyFromLastPanel fundTypes={fundTypes} accounts={accounts} onClose={() => setShowCopyModal(false)} />
        )}
      </Modal>

      {/* Batch Create By Period Modal */}
      <Modal open={showBatchCreateByPeriodModal} onClose={() => setShowBatchCreateByPeriodModal(false)} title="批量按账期添加记录" size="lg">
        {fundTypes && accounts && liquidityRatings && (
          <BatchCreateByPeriodPanel
            fundTypes={fundTypes}
            accounts={accounts}
            liquidityRatings={liquidityRatings}
            onClose={() => setShowBatchCreateByPeriodModal(false)}
          />
        )}
      </Modal>

      {/* New Batch Edit Modal */}
      {fundTypes && accounts && liquidityRatings && assetNames && (
        <NewBatchEditModal
          open={showBatchEditModal}
          onClose={() => setShowBatchEditModal(false)}
          fundTypes={fundTypes}
          accounts={accounts}
          liquidityRatings={liquidityRatings}
          assetNames={assetNames}
          onSubmit={(data) => batchUpdateMutation.mutate(data)}
        />
      )}

      {/* Batch Update History Modal */}
      {fundTypes && accounts && liquidityRatings && (
        <BatchUpdateHistoryModal
          open={showBatchHistoryModal}
          onClose={() => setShowBatchHistoryModal(false)}
          assetName={editingAssetName}
          fundTypes={fundTypes}
          accounts={accounts}
          liquidityRatings={liquidityRatings}
          onSubmit={(data, summary) => {
            batchUpdateMutation.mutate(data, {
              onSuccess: () => {
                // 关闭批量修改弹窗
                setShowBatchHistoryModal(false)
                // 关闭编辑窗口
                setEditingId(null)
                // 显示摘要提示
                setBatchUpdateSummary({
                  show: true,
                  count: summary.count,
                  changes: summary.changes
                })
                // 3秒后自动关闭提示
                setTimeout(() => {
                  setBatchUpdateSummary(prev => ({ ...prev, show: false }))
                }, 5000)
              }
            })
          }}
        />
      )}

      {/* Batch Delete Confirm Modal */}
      <Modal open={showBatchDeleteConfirm} onClose={() => setShowBatchDeleteConfirm(false)} title="确认批量删除">
        <div className="space-y-4">
          <div className="flex items-center gap-3 p-4 bg-red-50 rounded-lg">
            <AlertTriangle size={24} className="text-red-600" />
            <div>
              <p className="text-sm font-medium text-red-800">
                确定要删除选中的 <strong>{selectedIds.size}</strong> 条记录吗？
              </p>
              <p className="text-xs text-red-600 mt-1">
                此操作不可恢复，请谨慎操作。
              </p>
            </div>
          </div>
          <div className="flex justify-end gap-3">
            <button
              onClick={() => setShowBatchDeleteConfirm(false)}
              className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
            >
              取消
            </button>
            <button
              onClick={() => batchDeleteMutation.mutate(Array.from(selectedIds))}
              disabled={batchDeleteMutation.isPending}
              className="px-4 py-2 text-sm text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
            >
              {batchDeleteMutation.isPending ? '删除中...' : '确认删除'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
