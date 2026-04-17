'use client'

import { useState, useRef, useCallback, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatAmount, formatDate, PERIOD_OPTIONS } from '@/lib/utils'
import type { AssetRecord, AssetRecordCreate, FundType, Account, BatchUpdateAssets, AssetHistoryByNameResponse, BatchCreateByPeriodResult, LiquidityRating, AssetOwner } from '@/types'
import {
  Plus, Copy, Trash2, Pencil, X, Check, Search, Edit3, AlertTriangle, History, CalendarPlus, ChevronDown
} from 'lucide-react'
import { Button, Space, Tag, Popconfirm, message, Table, Input } from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

// 动态导入 ProComponents，避免 SSR 问题
import type { ProColumns, ActionType } from '@ant-design/pro-components'
const ProTable = dynamic(() => import('@ant-design/pro-components').then(mod => mod.ProTable), { ssr: false })
import dynamic from 'next/dynamic'

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
  liquidityRatings: LiquidityRating[]
  onSubmit: (data: BatchUpdateAssets, summary: { count: number; changes: string[] }) => void
}) {
  const [historyData, setHistoryData] = useState<AssetHistoryByNameResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)

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

  useEffect(() => {
    if (open && assetName) {
      setIsLoading(true)
      api.getAssetHistoryByName(assetName)
        .then(data => {
          setHistoryData(data)
          setFieldValues(prev => ({ ...prev, asset_name: assetName }))
        })
        .finally(() => setIsLoading(false))
    }
  }, [open, assetName])

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
            <div className="bg-brand-50 border border-brand-200 rounded-lg p-4">
              <div className="flex items-center gap-2 text-brand-800">
                <History size={20} />
                <span className="font-medium">将影响 {historyData.total_count} 条历史记录</span>
              </div>
              <p className="text-sm text-brand-600 mt-1">
                以下展示最新的 3 条记录作为示例
              </p>
            </div>

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

            <div className="border-t border-slate-100 pt-4">
              <h4 className="text-sm font-medium text-slate-700 mb-3">选择要修改的字段</h4>
              <div className="grid grid-cols-2 gap-4">
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
  liquidityRatings: LiquidityRating[]
  assetNames: string[]
  onSubmit: (data: BatchUpdateAssets) => void
}) {
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
function RecordForm({ fundTypes, accounts, liquidityRatings, assetOwners, initial, onSubmit, onCancel, onBatchHistory, isEditing = false }: {
  fundTypes: FundType[]; accounts: Account[]; liquidityRatings: LiquidityRating[]; assetOwners: AssetOwner[]
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
    owner_id: initial?.owner_id,
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
          <label className="block text-sm font-medium text-slate-600 mb-1">资产拥有者</label>
          <select
            value={form.owner_id || ''}
            onChange={(e) => setForm({ ...form, owner_id: e.target.value ? parseInt(e.target.value) : undefined })}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
          >
            <option value="">请选择</option>
            {assetOwners.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
          </select>
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
          <Table
            dataSource={drafts.map((d, i) => ({ ...d, key: i }))}
            columns={[
              {
                title: '资产名称',
                dataIndex: 'asset_name',
                key: 'asset_name',
                sorter: (a, b) => String(a.asset_name).localeCompare(String(b.asset_name)),
              },
              {
                title: '账户',
                dataIndex: 'account_name',
                key: 'account_name',
                sorter: (a, b) => String(a.account_name).localeCompare(String(b.account_name)),
              },
              {
                title: '金额',
                dataIndex: 'amount',
                key: 'amount',
                align: 'right',
                sorter: (a, b) => parseFloat(String(a.amount || 0)) - parseFloat(String(b.amount || 0)),
                render: (value, record) => {
                  // 使用 record.key（原始索引）来更新数据，而不是 render 的 index 参数
                  const originalIndex = record.key as number
                  return (
                    <Input
                      type="number"
                      step="0.01"
                      value={value as string}
                      onChange={(e) => {
                        const newDrafts = [...drafts]
                        newDrafts[originalIndex] = { ...newDrafts[originalIndex], amount: e.target.value }
                        setDrafts(newDrafts)
                      }}
                      style={{ width: 120, textAlign: 'right' }}
                    />
                  )
                },
              },
            ]}
            pagination={false}
            scroll={{ y: 400 }}
            size="small"
          />
          <div className="flex justify-end gap-3">
            <Button onClick={onClose}>
              取消
            </Button>
            <Button
              type="primary"
              onClick={handleSave}
              disabled={saveMutation.isPending}
              loading={saveMutation.isPending}
            >
              {saveMutation.isPending ? '保存中...' : `保存 ${drafts.length} 条记录`}
            </Button>
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
  assetOwners,
  onClose,
}: {
  fundTypes: FundType[]
  accounts: Account[]
  liquidityRatings: LiquidityRating[]
  assetOwners: AssetOwner[]
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
    owner_id: '',
    asset_name: '',
    amount: '',
  })
  const [previewResult, setPreviewResult] = useState<BatchCreateByPeriodResult | null>(null)
  const [conflictResolution, setConflictResolution] = useState<'skip' | 'overwrite'>('skip')

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
          owner_id: form.owner_id ? Number(form.owner_id) : undefined,
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
      message.error('请填写完整的记录信息')
      return
    }
    setStep('preview')
  }

  const handleConfirm = () => {
    batchCreateMutation.mutate()
  }

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

      <div className="p-4 bg-slate-50 rounded-lg space-y-3">
        <h4 className="font-medium text-slate-700">记录信息</h4>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">流动性评级</label>
            <select
              value={form.liquidity_rating_id}
              onChange={(e) => setForm((prev) => ({ ...prev, liquidity_rating_id: Number(e.target.value) }))}
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
              onChange={(e) => setForm((prev) => ({ ...prev, fund_type_id: e.target.value }))}
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
              onChange={(e) => setForm((prev) => ({ ...prev, account_id: e.target.value }))}
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
            <label className="block text-sm font-medium text-slate-600 mb-1">资产拥有者</label>
            <select
              value={form.owner_id}
              onChange={(e) => setForm((prev) => ({ ...prev, owner_id: e.target.value }))}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            >
              <option value="">请选择</option>
              {assetOwners.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">资产名称</label>
            <input
              type="text"
              value={form.asset_name}
              onChange={(e) => setForm((prev) => ({ ...prev, asset_name: e.target.value }))}
              placeholder="如: 定期存款"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-600 mb-1">金额 (元)</label>
            <input
              type="number"
              step="0.01"
              value={form.amount}
              onChange={(e) => setForm((prev) => ({ ...prev, amount: e.target.value }))}
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
  const actionRef = useRef<ActionType>()
  const [selectedIds, setSelectedIds] = useState<number[]>([])

  // Modal states
  const [showAddModal, setShowAddModal] = useState(false)
  const [showCopyModal, setShowCopyModal] = useState(false)
  const [showBatchCreateByPeriodModal, setShowBatchCreateByPeriodModal] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [showBatchEditModal, setShowBatchEditModal] = useState(false)
  const [showBatchHistoryModal, setShowBatchHistoryModal] = useState(false)
  const [editingAssetName, setEditingAssetName] = useState<string>('')

  // Data queries
  const { data: fundTypes } = useQuery({ queryKey: ['fundTypes'], queryFn: api.getFundTypes })
  const { data: accounts } = useQuery({ queryKey: ['accounts'], queryFn: api.getAccounts })
  const { data: liquidityRatings } = useQuery({ queryKey: ['liquidityRatings'], queryFn: api.getLiquidityRatings })
  const { data: assetOwners } = useQuery({ queryKey: ['assetOwners'], queryFn: api.getAssetOwners })
  const { data: assetNames } = useQuery({ queryKey: ['assetNames'], queryFn: api.getAssetNames })

  // Get leaf types for fund type filter
  const leafTypes = fundTypes?.filter((ft) => !fundTypes.some((c) => c.parent_id === ft.id)) || []

  // Mutations
  const createMutation = useMutation({
    mutationFn: api.createAsset,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      setShowAddModal(false)
      actionRef.current?.reload()
      message.success('创建成功')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: api.deleteAsset,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      actionRef.current?.reload()
      message.success('删除成功')
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<AssetRecordCreate> }) => api.updateAsset(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setEditingId(null)
      actionRef.current?.reload()
      message.success('更新成功')
    },
  })

  const batchUpdateMutation = useMutation({
    mutationFn: (data: BatchUpdateAssets) => api.batchUpdateAssets(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setShowBatchEditModal(false)
      actionRef.current?.reload()
      message.success('批量更新成功')
    },
  })

  const batchDeleteMutation = useMutation({
    mutationFn: (ids: number[]) => api.batchDeleteAssets(ids),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setSelectedIds([])
      actionRef.current?.reload()
      message.success('批量删除成功')
    },
  })

  // Table columns definition
  const columns: ProColumns<AssetRecord>[] = [
    {
      title: '日期',
      dataIndex: 'asset_date',
      key: 'asset_date',
      width: 120,
      sorter: true,
      render: (_, record) => formatDate(record.asset_date),
      valueType: 'dateRange',
      search: {
        transform: (value) => ({
          date_from: value[0],
          date_to: value[1],
        }),
      },
    },
    {
      title: '资产名称',
      dataIndex: 'asset_name',
      key: 'asset_name',
      width: 180,
      sorter: true,
      render: (_, record) => <span className="font-medium text-brand-950">{record.asset_name}</span>,
    },
    {
      title: '资产类型',
      dataIndex: 'fund_type_name',
      key: 'fund_type_name',
      width: 140,
      sorter: true,
      valueType: 'select',
      fieldProps: {
        options: leafTypes.map(ft => ({ label: ft.name, value: ft.id })),
        mode: 'multiple',
      },
      search: {
        transform: (value) => ({ fund_type_id: value.join(',') }),
      },
      render: (_, record) => record.fund_type_name || '-',
    },
    {
      title: '账户',
      dataIndex: 'account_name',
      key: 'account_name',
      width: 140,
      sorter: true,
      valueType: 'select',
      fieldProps: {
        options: accounts?.map(a => ({ label: a.name, value: a.id })) || [],
        mode: 'multiple',
      },
      search: {
        transform: (value) => ({ account_id: value.join(',') }),
      },
      render: (_, record) => record.account_name || '-',
    },
    {
      title: '流动性评级',
      dataIndex: 'liquidity_rating_name',
      key: 'liquidity_rating_name',
      width: 120,
      sorter: true,
      valueType: 'select',
      fieldProps: {
        options: liquidityRatings?.map(lr => ({ label: lr.name, value: lr.id })) || [],
        mode: 'multiple',
      },
      search: {
        transform: (value) => ({ liquidity_rating_id: value.join(',') }),
      },
      render: (_, record) => (
        <Tag color="blue">{record.liquidity_rating_name}</Tag>
      ),
    },
    {
      title: '资产拥有者',
      dataIndex: 'owner_name',
      key: 'owner_name',
      width: 120,
      sorter: true,
      valueType: 'select',
      fieldProps: {
        options: assetOwners?.map(o => ({ label: o.name, value: o.id })) || [],
        mode: 'multiple',
      },
      search: {
        transform: (value) => ({ owner_id: value.join(',') }),
      },
      render: (_, record) => record.owner_name || '-',
    },
    {
      title: '金额',
      dataIndex: 'amount',
      key: 'amount',
      width: 140,
      align: 'right',
      sorter: true,
      valueType: 'digitRange',
      search: {
        transform: (value) => ({
          amount_min: value[0],
          amount_max: value[1],
        }),
      },
      render: (_, record) => (
        <span className={`font-mono ${parseFloat(record.amount) >= 0 ? 'amount-positive' : 'amount-negative'}`}>
          {formatAmount(record.amount)}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      fixed: 'right',
      search: false,
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<Pencil size={14} />}
            onClick={() => setEditingId(record.id)}
          />
          <Popconfirm
            title="确认删除"
            description="确定要删除这条记录吗？"
            onConfirm={() => deleteMutation.mutate(record.id)}
            okText="确定"
            cancelText="取消"
          >
            <Button
              type="text"
              danger
              icon={<Trash2 size={14} />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  // 排序字段映射：将前端显示的字段名映射到后端排序字段名
  const sortFieldMapping: Record<string, string> = {
    'fund_type_name': 'fund_type_name',
    'account_name': 'account_name',
    'liquidity_rating_name': 'liquidity_rating_name',
    'owner_name': 'owner_name',
    'asset_date': 'asset_date',
    'asset_name': 'asset_name',
    'amount': 'amount',
  }

  // Fetch data function for ProTable
  const fetchData = useCallback(async (params: any, sort: any, filter: any) => {
    const queryParams: Record<string, string> = {
      page: String(params.current || 1),
      page_size: String(params.pageSize || 10),
    }

    // Add search params
    if (params.asset_name) {
      queryParams.asset_name = params.asset_name
    }

    // Add date range
    if (params.date_from) {
      queryParams.date_from = params.date_from
    }
    if (params.date_to) {
      queryParams.date_to = params.date_to
    }

    // Add fund type filter
    if (params.fund_type_id) {
      queryParams.fund_type_id = params.fund_type_id
    }

    // Add account filter
    if (params.account_id) {
      queryParams.account_id = params.account_id
    }

    // Add liquidity rating filter
    if (params.liquidity_rating_id) {
      queryParams.liquidity_rating_id = params.liquidity_rating_id
    }

    // Add owner filter
    if (params.owner_id) {
      queryParams.owner_id = params.owner_id
    }

    // Add amount range
    if (params.amount_min) {
      queryParams.amount_min = params.amount_min
    }
    if (params.amount_max) {
      queryParams.amount_max = params.amount_max
    }

    // Add sorting
    if (sort) {
      const sortField = Object.keys(sort)[0]
      if (sortField) {
        // 使用字段映射获取后端排序字段名
        queryParams.sort_field = sortFieldMapping[sortField] || sortField
        queryParams.sort_order = sort[sortField] === 'ascend' ? 'asc' : 'desc'
      }
    }

    const response = await api.getAssets(queryParams)

    return {
      data: response.items,
      success: true,
      total: response.total,
    }
  }, [])

  // Row selection config
  const rowSelection = {
    selectedRowKeys: selectedIds,
    onChange: (selectedRowKeys: React.Key[]) => {
      setSelectedIds(selectedRowKeys as number[])
    },
  }

  // Store table data for editing
  const [tableData, setTableData] = useState<AssetRecord[]>([])

  // Get editing record from table data
  const editingRecord = editingId
    ? tableData.find((r: AssetRecord) => r.id === editingId)
    : null

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-brand-950">资产记录</h1>
        </div>
        <Space>
          <Button
            icon={<Edit3 size={16} />}
            onClick={() => setShowBatchEditModal(true)}
          >
            批量修改
          </Button>
          <Button
            icon={<Copy size={16} />}
            onClick={() => setShowCopyModal(true)}
          >
            复制上期
          </Button>
          <Button
            icon={<CalendarPlus size={16} />}
            onClick={() => setShowBatchCreateByPeriodModal(true)}
          >
            批量按账期添加
          </Button>
          <Button
            type="primary"
            icon={<Plus size={16} />}
            onClick={() => setShowAddModal(true)}
            className="bg-brand-600 hover:bg-brand-700"
          >
            新增记录
          </Button>
        </Space>
      </div>

      <ProTable<AssetRecord>
        actionRef={actionRef}
        columns={columns}
        request={fetchData}
        rowKey="id"
        pagination={{
          showSizeChanger: true,
          showQuickJumper: true,
          pageSizeOptions: [10, 20, 50, 100],
          defaultPageSize: 10,
        }}
        rowSelection={rowSelection}
        onDataSourceChange={(data) => setTableData(data)}
        tableAlertRender={({ selectedRowKeys }) => (
          <div className="flex items-center gap-2">
            <span>已选择 <strong>{selectedRowKeys.length}</strong> 条记录</span>
          </div>
        )}
        tableAlertOptionRender={({ selectedRowKeys }) => (
          <Space>
            <Button
              danger
              size="small"
              icon={<Trash2 size={14} />}
              onClick={() => batchDeleteMutation.mutate(selectedRowKeys as number[])}
            >
              批量删除
            </Button>
            <Button
              size="small"
              onClick={() => setSelectedIds([])}
            >
              取消选择
            </Button>
          </Space>
        )}
        search={{
          labelWidth: 'auto',
          defaultCollapsed: false,
          optionRender: (searchConfig, formProps, dom) => [
            ...dom,
            <Button
              key="reset"
              onClick={() => {
                formProps.form?.resetFields()
                actionRef.current?.reload()
              }}
            >
              重置
            </Button>,
          ],
        }}
        toolBarRender={false}
        cardProps={false}
        className="bg-white rounded-xl shadow-card"
        scroll={{ x: 1200 }}
      />

      {/* Add Modal */}
      <Modal open={showAddModal} onClose={() => setShowAddModal(false)} title="新增资产记录">
        {fundTypes && accounts && liquidityRatings && assetOwners && (
          <RecordForm
            fundTypes={fundTypes}
            accounts={accounts}
            liquidityRatings={liquidityRatings}
            assetOwners={assetOwners}
            onSubmit={(data) => createMutation.mutate(data)}
            onCancel={() => setShowAddModal(false)}
          />
        )}
      </Modal>

      {/* Edit Modal */}
      <Modal open={editingId !== null} onClose={() => setEditingId(null)} title="编辑资产记录">
        {editingRecord && fundTypes && accounts && liquidityRatings && assetOwners && (
          <RecordForm
            fundTypes={fundTypes}
            accounts={accounts}
            liquidityRatings={liquidityRatings}
            assetOwners={assetOwners}
            initial={{
              asset_date: editingRecord.asset_date,
              liquidity_rating_id: editingRecord.liquidity_rating_id,
              fund_type_id: editingRecord.fund_type_id,
              asset_name: editingRecord.asset_name,
              account_id: editingRecord.account_id,
              owner_id: editingRecord.owner_id || undefined,
              amount: editingRecord.amount,
            }}
            onSubmit={(data) => updateMutation.mutate({ id: editingId!, data })}
            onCancel={() => setEditingId(null)}
            onBatchHistory={() => {
              setEditingAssetName(editingRecord.asset_name)
              setShowBatchHistoryModal(true)
            }}
            isEditing={true}
          />
        )}
      </Modal>

      {/* Copy Modal */}
      <Modal open={showCopyModal} onClose={() => setShowCopyModal(false)} title="复制上期记录">
        {fundTypes && accounts && (
          <CopyFromLastPanel fundTypes={fundTypes} accounts={accounts} onClose={() => setShowCopyModal(false)} />
        )}
      </Modal>

      {/* Batch Create By Period Modal */}
      <Modal open={showBatchCreateByPeriodModal} onClose={() => setShowBatchCreateByPeriodModal(false)} title="批量按账期添加记录" size="lg">
        {fundTypes && accounts && liquidityRatings && assetOwners && (
          <BatchCreateByPeriodPanel
            fundTypes={fundTypes}
            accounts={accounts}
            liquidityRatings={liquidityRatings}
            assetOwners={assetOwners}
            onClose={() => setShowBatchCreateByPeriodModal(false)}
          />
        )}
      </Modal>

      {/* New Batch Edit Modal */}
      {fundTypes && accounts && liquidityRatings && assetNames && (
        <NewBatchEditModal
          open={showBatchEditModal}
          onClose={() => setShowBatchEditModal(false)}
          fundTypes={fundTypes as FundType[]}
          accounts={accounts as Account[]}
          liquidityRatings={liquidityRatings as LiquidityRating[]}
          assetNames={assetNames as string[]}
          onSubmit={(data) => batchUpdateMutation.mutate(data)}
        />
      )}

      {/* Batch Update History Modal */}
      {fundTypes && accounts && liquidityRatings && (
        <BatchUpdateHistoryModal
          open={showBatchHistoryModal}
          onClose={() => setShowBatchHistoryModal(false)}
          assetName={editingAssetName}
          fundTypes={fundTypes as FundType[]}
          accounts={accounts as Account[]}
          liquidityRatings={liquidityRatings as LiquidityRating[]}
          onSubmit={(data) => {
            batchUpdateMutation.mutate(data)
            setShowBatchHistoryModal(false)
            setEditingId(null)
          }}
        />
      )}
    </div>
  )
}
