'use client'

import { useState, useRef, useCallback, useMemo, useEffect, Suspense } from 'react'
import Link from 'next/link'
import dynamic from 'next/dynamic'
import { useSearchParams, useRouter } from 'next/navigation'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, newIdemKey, fetchItemNotes, AUDIT_REASON_REQUIRED_TEXT, isAuditReasonRequired } from '@/lib/api'
import type { AuditReasonInput } from '@/lib/api'
import { formatCurrency, formatDate, formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AssetRecord, AssetRecordCreate, BatchUpdateAssets, BatchOwnerResponse, AssetTagBatchResponse, AssetClass, Institution, Member, TagItem, LiquidityLevel, CurrentValueItem, CurrentValueResponse } from '@/types'
import { Plus, Trash2, Pencil, Check, Search, Edit3, ChevronDown, Filter, Users, Tags, Layers, X, Image as ImageIcon, StickyNote, ArrowUpRight } from 'lucide-react'
import { BatchOwnerDialog } from '@/components/records/BatchOwnerDialog'
import { BatchTagDialog } from '@/components/records/BatchTagDialog'
// 项级聚焦面板（spec D6 / 原型变体 C）：记录页改标签与备注的唯一写入口
import { AssetItemSheet } from '@/components/records/AssetItemSheet'
import type { AssetItemSheetSaveResult } from '@/components/records/AssetItemSheet'
import { CurrentValueTable } from '@/components/records/CurrentValueTable'
import {
  AuditReasonField,
  EMPTY_AUDIT_REASON,
  toAuditReasonInput,
  useAuditReasonDraft,
} from '@/components/records/AuditReasonField'
import type { AuditReasonValue } from '@/components/records/AuditReasonField'
// 理由必填的判定与门禁只有一份实现：确认框（门禁登记）与更正框（自画保存按钮）都走它
import { isReasonMissing, runConfirmGate } from '@/components/ui/confirmGate'
import type { BatchOwnerFormData } from '@/components/records/BatchOwnerDialog'
import {
  BatchClassificationDialog,
  DraftClassificationBanner,
  ItemClassificationDialog,
  MEMBER_ROLE_LABEL,
  dimsText,
  loadClassificationSnapshots,
  requiredMissing,
  snapshotFromRecord,
} from '@/components/classification'
import type { ClassificationSnapshot } from '@/components/classification'
import Decimal from 'decimal.js'
import { message, Radio } from 'antd'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import { ConfirmDialog, DataTable, actionsWidth, FilterSheet, Money, ResponsiveDialog } from '@/components/ui'
import type { DataTableColumn, DataTableSort } from '@/components/ui'
import { tokens } from '@/styles/tokens'
import { AmountField } from '@/components/form/AmountField'
import { SelectField } from '@/components/form/SelectField'

const LazyAmountInput = dynamic(
  () => import('@/components/form/AmountInput').then((m) => m.AmountInput),
  { ssr: false },
)
const LazyDateField = dynamic(
  () => import('@/components/form/DateField').then((m) => m.DateField),
  { ssr: false },
)
const LazyItemOwnerDialog = dynamic(
  () => import('@/components/records/ItemOwnerDialog').then((m) => m.ItemOwnerDialog),
  { ssr: false },
)

/**
 * 当前值行 → 资产项面板入参（`.scratch/current-value-note/spec.md` D4）。
 *
 * 抽屉的 props 是 `AssetRecord`（既有契约，本票不改面板），而当前值读模型是**每项一行**
 * 的派生视图。面板只读其中的**项级回显**字段（asset_item_id / 显示名 / 标签 / 分类四维）；
 * 历史条数与最新一期由面板自己按 item_id 取 `GET /api/assets?item_id=`，金额与日期不读
 * 这里的占位值——所以记录级字段只用行上已有的值（无记录时给空串），不做任何推算。
 */
function sheetRecordFromCurrentValue(item: CurrentValueItem): AssetRecord {
  return {
    id: item.record_id ?? 0,
    asset_item_id: item.item_id,
    // 记录行的 name 是写入时快照；当前值表只有项显示名，两者同源（改名不回写历史行）
    asset_name: item.display_name,
    display_name: item.display_name,
    amount: item.amount ?? '',
    side: item.side,
    asset_date: item.asset_date ?? '',
    asset_class_id: item.asset_class_id,
    asset_class_name: item.asset_class_name,
    asset_class_code: item.asset_class_code,
    asset_class_path: item.asset_class_path,
    liquidity_days: item.liquidity_days,
    liquidity_label: item.liquidity_label,
    institution_id: item.institution_id,
    institution_name: item.institution_name,
    channel_id: item.channel_id,
    channel_name: item.channel_name,
    owner_id: item.owner_id,
    owner_name: item.owner_name,
    owner_kind: item.owner_kind,
    risk_level: item.risk_level,
    tags: item.tags,
    classification_missing: item.classification_missing,
    created_at: '',
    updated_at: '',
  }
}

// --- Reusable Modal ---
function Modal({ open, onClose, title, children, size = 'md' }: {
  open: boolean; onClose: () => void; title: string; children: React.ReactNode; size?: 'md' | 'lg' | 'xl'; closeOnOverlayClick?: boolean
}) {
  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title={title}
      size={size === 'xl' ? 'xl' : 'lg'}
      bodyClassName="space-y-4"
    >
      {children}
    </ResponsiveDialog>
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
  const triggerRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({})

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

  // 计算下拉面板位置
  useEffect(() => {
    if (isOpen && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect()
      const dropdownHeight = 320
      const spaceBelow = window.innerHeight - rect.bottom
      const spaceAbove = rect.top

      const showAbove = spaceBelow < dropdownHeight && spaceAbove > spaceBelow

      setDropdownStyle({
        position: 'fixed',
        left: rect.left,
        top: showAbove ? rect.top - dropdownHeight - 4 : rect.bottom + 4,
        width: rect.width,
        maxHeight: Math.min(320, showAbove ? spaceAbove - 8 : spaceBelow - 8),
        zIndex: 9999,
      })
    }
  }, [isOpen])

  // 处理滚轮事件，阻止冒泡并手动滚动
  const handleWheel = (e: React.WheelEvent) => {
    e.stopPropagation()
    const listEl = listRef.current
    if (!listEl) return

    const { deltaY } = e
    const { scrollTop, scrollHeight, clientHeight } = listEl

    // 检查是否可以向上或向下滚动
    const canScrollUp = scrollTop > 0
    const canScrollDown = scrollTop < scrollHeight - clientHeight

    // 如果在顶部且继续向上滚动，或在底部且继续向下滚动，则阻止默认行为
    if ((deltaY < 0 && !canScrollUp) || (deltaY > 0 && !canScrollDown)) {
      e.preventDefault()
    }
  }

  return (
    <div className="relative">
      {label && <label className="block text-sm font-medium text-secondary mb-1">{label}</label>}
      <div
        ref={triggerRef}
        className="w-full px-3 py-2 border border-line rounded-lg text-sm bg-surface cursor-pointer flex flex-wrap items-center justify-between gap-y-2"
        onClick={() => setIsOpen(!isOpen)}
      >
        <span className={selected.length === 0 ? 'text-muted' : 'text-secondary'}>
          {selected.length === 0 ? placeholder : `已选择 ${selected.length} 项`}
        </span>
        <ChevronDown size={16} className={`text-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </div>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-[9998]" onClick={() => setIsOpen(false)} />
          <div
            className="fixed bg-surface border border-line rounded-lg shadow-lg flex flex-col"
            style={dropdownStyle}
            onWheel={(e) => e.stopPropagation()}
            onTouchMove={(e) => e.stopPropagation()}
          >
            <div className="p-2 border-b border-line flex-shrink-0">
              <div className="relative">
                <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索..."
                  className="w-full pl-7 pr-2 py-1.5 text-base lg:text-sm border border-line rounded focus:outline-none focus:ring-2 focus:ring-brand-400"
                  onClick={(e) => e.stopPropagation()}
                />
              </div>
            </div>
            <div 
              ref={listRef}
              className="overflow-y-auto flex-1 min-h-0" 
              onWheel={handleWheel}
            >
              {filteredOptions.length === 0 ? (
                <div className="px-3 py-2 text-sm text-muted">无匹配选项</div>
              ) : (
                filteredOptions.map(option => (
                  <div
                    key={option}
                    className="px-3 py-2 text-sm hover:bg-subtle cursor-pointer flex items-center gap-2"
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleOption(option)
                    }}
                  >
                    <div className={`w-4 h-4 rounded border flex items-center justify-center ${
                      selected.includes(option) ? 'bg-brand-600 border-brand-600' : 'border-line-strong'
                    }`}>
                      {selected.includes(option) && <Check size={12} className="text-white" />}
                    </div>
                    <span className="text-secondary">{option}</span>
                  </div>
                ))
              )}
            </div>
            <div className="p-2 border-t border-line flex justify-between items-center flex-shrink-0">
              <div className="flex items-center gap-2">
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onChange([...options])
                  }}
                  className="text-xs text-brand-600 hover:text-brand-700"
                >
                  全选
                </button>
                <span className="text-xs text-disabled">|</span>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onChange([])
                  }}
                  className="text-xs text-brand-600 hover:text-brand-700"
                >
                  取消选择
                </button>
              </div>
              <span className="text-xs text-secondary">{selected.length} 项已选择</span>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// --- Batch Edit Modal（记录侧只剩「资产/负债」可批量改）---
// 金额/日期/名称是账本事实（write-once）：金额走单条「更正」、改名走资产项 rename，
// 分类（类别/流动性/机构/渠道/归属/标签）挂在资产项上，记录侧一律不可写。
function NewBatchEditModal({
  open,
  onClose,
  assetNames,
  onSubmit
}: {
  open: boolean
  onClose: () => void
  assetNames: string[]
  onSubmit: (data: BatchUpdateAssets) => void
}) {
  const [selectedAssetNames, setSelectedAssetNames] = useState<string[]>([])
  const [matchedRecords, setMatchedRecords] = useState<AssetRecord[]>([])
  const [side, setSide] = useState<'asset' | 'liability'>('asset')

  useEffect(() => {
    if (selectedAssetNames.length > 0) {
      api.getAssetsByNames(selectedAssetNames).then(records => {
        setMatchedRecords(records)
      })
    } else {
      setMatchedRecords([])
    }
  }, [selectedAssetNames])

  const hasSelectedAssets = selectedAssetNames.length > 0

  const handleSubmit = () => {
    onSubmit({ ids: matchedRecords.map(r => r.id), side })
  }

  return (
    <Modal open={open} onClose={onClose} title="批量修改资产" size="xl">
      <div className="space-y-6">
        <h4 className="text-sm font-medium text-secondary mb-3">步骤 1: 选择要修改的资产</h4>
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
            <h4 className="text-sm font-medium text-secondary mb-3">匹配的记录</h4>
            <div className="max-h-48 overflow-auto border border-line rounded-lg">
              {/* 原生表（spec D6/D7）：就地读同一套档位、套 .pf-table（字面类名，Tailwind JIT 按内容扫描）。
                  列宽走 colgroup + style（auto 布局下档位不生效，spec C2），内边距由 .pf-table 给 40。 */}
              <table className="pf-table" style={{ minWidth: BULK_EDIT_TABLE_WIDTH }}>
                <colgroup>
                  <col style={{ width: tokens.table.col.date }} />
                  <col style={{ width: tokens.table.col['text-mid'] }} />
                  <col style={{ width: tokens.table.col.label }} />
                  <col style={{ width: tokens.table.col.label }} />
                  <col style={{ width: tokens.table.col['text-short'] }} />
                  <col style={{ width: tokens.table.col.amount }} />
                </colgroup>
                {/* 冻结列：只冻首列（日期）。末列是金额**数据值**、不是行级操作/判读，
                    按右冻结判据不冻 —— 冻「金额」只会把一列数字钉在右边，无意义。
                    thead 的 sticky top-0 已有，补 z-sticky(20) 满足 A10。 */}
                <thead className="bg-subtle sticky top-0 z-sticky">
                  <tr>
                    <th className="pf-col-first py-2 text-secondary font-medium">日期</th>
                    <th className="py-2 text-secondary font-medium">资产名称</th>
                    <th className="py-2 text-secondary font-medium">资产类别</th>
                    <th className="py-2 text-secondary font-medium">机构</th>
                    <th className="py-2 text-secondary font-medium">侧别</th>
                    <th className="py-2 text-right text-secondary font-medium">金额</th>
                  </tr>
                </thead>
                <tbody>
                  {matchedRecords.map((record) => (
                    <tr key={record.id} className="border-t border-line">
                      <td className="pf-col-first py-2 text-secondary">{formatDate(record.asset_date)}</td>
                      <td className="py-2 font-medium text-brand-950 whitespace-normal">{record.asset_name}</td>
                      <td className="py-2 text-secondary whitespace-normal">{record.asset_class_name || '—'}</td>
                      <td className="py-2 text-secondary whitespace-normal">{record.institution_name || '—'}</td>
                      <td className="py-2 text-secondary">{(record.side ?? 'asset') === 'asset' ? '资产' : '负债'}</td>
                      <td className={`py-2 text-right font-mono ${(record.side ?? 'asset') === 'asset' ? 'amount-positive' : 'amount-negative'}`}>
                        {formatCurrency(record.amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {hasSelectedAssets && matchedRecords.length > 0 && (
          <div className="border-t border-line pt-4">
            <h4 className="text-sm font-medium text-secondary mb-3">步骤 2: 设置要修改的字段</h4>
            <Radio.Group
              value={side}
              onChange={(e) => setSide(e.target.value)}
              className="w-full"
            >
              <Radio.Button value="asset">资产</Radio.Button>
              <Radio.Button value="liability">负债</Radio.Button>
            </Radio.Group>
            <p className="mt-2 text-xs text-muted">
              修改侧别按「更正」逐行追加新记录（原记录保留）。金额/日期/名称与分类不在此处批量修改：
              金额请逐行更正，改名走资产项，分类在资产项上维护。
            </p>
          </div>
        )}

        <div className="flex justify-end gap-3 pt-4 border-t border-line">
          <button onClick={onClose}
            className="px-4 py-2 text-sm text-secondary border border-line rounded-lg hover:bg-subtle">取消</button>
          <button
            onClick={handleSubmit}
            disabled={matchedRecords.length === 0}
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
// 记录表单只写账本事实（日期/名称/侧别/金额）。分类挂在资产项上：
// 编辑时把项级分类**只读**展示出来，提示去哪改，而不是给一组后端会 422 的控件。
interface AssetRecordFormData {
  asset_date: string
  asset_name: string
  amount: string
  side: 'asset' | 'liability'
}

function RecordForm({ initial, classification, audit, onAuditChange, onSubmit, onCancel, isEditing = false }: {
  initial?: Partial<AssetRecordFormData>
  /** 编辑时只读展示的项级分类（label/value）。 */
  classification?: Array<{ label: string; value: string | null }>
  /** 更正理由（进审计，**必填**）：由外层持有，关窗（Esc / 取消）不丢已写内容。 */
  audit?: AuditReasonValue
  onAuditChange?: (value: AuditReasonValue) => void
  onSubmit: (data: AssetRecordCreate, audit?: AuditReasonValue) => void
  onCancel: () => void
  isEditing?: boolean
}) {
  const [form, setForm] = useState<AssetRecordFormData>({
    asset_date: initial?.asset_date || new Date().toISOString().split('T')[0],
    asset_name: initial?.asset_name || '',
    amount: initial?.amount || '0',
    side: initial?.side || 'asset',
  })
  /** 被拦下的次数：+1 让理由框显红字并把焦点拿回去（与确认框门禁同一套反馈）。 */
  const [auditNudge, setAuditNudge] = useState(0)

  const auditValue = audit ?? EMPTY_AUDIT_REASON
  // 只有真的动了账本数字（金额 / 侧别）才问「为什么更正」：没改动就没有纠正可言，
  // 理由框跟着出现，避免把一个纯浏览性的弹窗填成表单。
  const amountChanged = Number(form.amount) !== Number(initial?.amount ?? '0')
  const sideChanged = form.side !== (initial?.side ?? 'asset')
  const correcting = isEditing && (amountChanged || sideChanged)
  // 更正＝改账本事实，后端缺 reason 一律 422。这里用同一份判定先拦：请求不发、弹窗不关。
  const auditGate = {
    blocked: () => isReasonMissing(auditValue.reason),
    nudge: () => setAuditNudge((count) => count + 1),
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <LazyDateField
          label="日期"
          value={form.asset_date}
          onChange={(value) => setForm({ ...form, asset_date: value })}
          required
        />
        <div>
          <label className="block text-sm font-medium text-secondary mb-1">资产名称</label>
          <input type="text" value={form.asset_name}
            onChange={(e) => setForm({ ...form, asset_name: e.target.value })}
            readOnly={isEditing}
            placeholder="如: 示例机构-示例产品A"
            className={`w-full px-3 py-2 border border-line rounded-lg text-base lg:text-sm focus:outline-none focus:ring-2 focus:ring-brand-400 ${isEditing ? 'bg-subtle text-secondary' : ''}`} />
          {isEditing && (
            // 改名要填原因 + 影响范围并先预览影响面：记录侧只读，避免改了个存不进去的名字
            <p className="mt-1 text-micro text-muted">
              改名请点上方「资产当前值」该行的「身份」，在「改名」里填原因与影响范围后确认。
            </p>
          )}
        </div>
        <div>
          <label className="block text-sm font-medium text-secondary mb-1">资产 / 负债</label>
          <Radio.Group
            value={form.side}
            onChange={(e) => setForm({ ...form, side: e.target.value })}
            className="w-full"
          >
            <Radio.Button value="asset">资产</Radio.Button>
            <Radio.Button value="liability">负债</Radio.Button>
          </Radio.Group>
        </div>
        <div>
          <label className="block text-sm font-medium text-secondary mb-1">金额 (元)</label>
          <LazyAmountInput
            value={form.amount}
            onChange={(value) => setForm({ ...form, amount: value })}
            className="w-full"
          />
        </div>
      </div>

      {correcting && onAuditChange && (
        <AuditReasonField
          label="为什么更正"
          value={auditValue}
          onChange={onAuditChange}
          nudgeToken={auditNudge}
          hint="这次更正会追加一行新记录、旧行留在库里；必须选一条原因才能保存。"
        />
      )}

      {isEditing && classification && classification.length > 0 && (
        <div className="rounded-control border border-line bg-subtle p-3">
          <p className="text-meta font-medium text-secondary">分类（资产项级，只读）</p>
          <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-meta sm:grid-cols-2">
            {classification.map((item) => (
              <div key={item.label} className="flex min-w-0 items-baseline justify-between gap-2">
                <dt className="shrink-0 text-muted">{item.label}</dt>
                <dd className="min-w-0 truncate text-ink" title={item.value ?? undefined}>{item.value ?? '未标注'}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-micro text-muted">
            分类按资产项生效（一项多期共用）；记录侧不可修改。标签同属项级。
          </p>
        </div>
      )}

      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onCancel}
          className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-4 text-body text-ink">取消</button>
        <button onClick={() => {
          // 更正但没选原因：不发请求、不关弹窗，红字落在理由框上、焦点回下拉
          if (correcting && runConfirmGate(auditGate)) return
          onSubmit({
            asset_date: form.asset_date,
            asset_name: form.asset_name,
            amount: form.amount,
            side: form.side,
          }, correcting ? auditValue : undefined)
        }}
          className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">保存</button>
      </div>
    </div>
  )
}

// --- Main Page ---
// useSearchParams 在静态预渲染时需 Suspense 包裹（App Router CSR bailout），页面逻辑放在内层组件

export default function RecordsPage() {
  return (
    <Suspense fallback={null}>
      <RecordsPageContent />
    </Suspense>
  )
}

function batchTagErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'TAG_LIMIT_EXCEEDED') return '有记录超过 5 个标签，请减少后重试。'
    if (error.code === 'ITEM_ARCHIVED') return '有记录已归档，不能修改标签。'
    if (error.code === 'SUPERSEDED') return '有记录已被更正，不能修改标签。'
    if (error.code === 'NOT_FOUND') return '有记录不存在或已被删除。'
    return error.message || '批量打标签失败，请重试。'
  }
  return error instanceof Error ? error.message : '批量打标签失败，请重试。'
}

function batchOwnerErrorText(error: unknown): string {
  // 缺审计理由是「这次请求没带理由」，不是归属冲突：单独说清（批量场景通常逐项进 failed[]）
  if (isAuditReasonRequired(error)) return AUDIT_REASON_REQUIRED_TEXT
  if (error instanceof ApiError) {
    if (error.code === 'ITEM_ARCHIVED') return '资产项已归档，不能修改归属。'
    if (error.code === 'NOT_FOUND') return '资产项不存在或已被删除。'
    if (error.code === 'VALIDATION_ERROR') return error.message || '参数校验失败，请检查后重试。'
    return error.message || '批量修改归属失败，请重试。'
  }
  return error instanceof Error ? error.message : '批量修改归属失败，请重试。'
}

type RecordsFilters = {
  asset_name: string
  date_from: string
  date_to: string
  /** 资产类别 id（含后代展开，后端处理）。 */
  asset_class_id: string
  institution_id: string
  channel_id: string
  owner_id: string
  /** 流动性档位天数（整数）。 */
  liquidity_days: string
  /** 标签 id（单个即可；后端 tag_ids 为 AND 语义）。 */
  tag_ids: string
  amount_min: string
  amount_max: string
}

type RecordsQueryParams = Partial<RecordsFilters> & {
  current?: number
  pageSize?: number
}

type RecordsSortParam = Record<string, 'ascend' | 'descend'>

/** /records 的两个视图；默认「当前值」（spec §3.4），URL 记作 `?view=current|ledger`。 */
type RecordsView = 'current' | 'ledger'

/**
 * 「批量修改」弹窗原生表的派生表宽（spec D3 的下限语义：Σ档位）。
 * 与 DataTable 同源 —— 列宽只读 `tokens.table.col`，这里只做加总，不写像素。
 */
const BULK_EDIT_TABLE_WIDTH = tokens.table.col.date
  + tokens.table.col['text-mid']
  + tokens.table.col.label * 2
  + tokens.table.col['text-short']
  + tokens.table.col.amount

const RECORDS_VIEW_TABS: Array<{ key: RecordsView; label: string }> = [
  { key: 'current', label: '当前值' },
  { key: 'ledger', label: '记录明细' },
]

/** 成员角色（members.role）的展示文案由 `@/components/classification` 统一提供。 */

const EMPTY_RECORD_FILTERS: RecordsFilters = {
  asset_name: '', date_from: '', date_to: '', asset_class_id: '', institution_id: '',
  channel_id: '', owner_id: '', liquidity_days: '', tag_ids: '', amount_min: '', amount_max: '',
}

// 列 key → 后端排序字段（分类列不在 records 表上，服务端只支持记录自身字段排序）
const SORT_FIELD_MAPPING: Record<string, string> = {
  asset_date: 'asset_date',
  asset_name: 'asset_name',
  amount: 'amount',
  // 与列 key 同名，登记出来是为了让「这一列可排、排的是 assets.updated_at」可读；
  // 未登记的 key 虽会原样透传，但透传出来的意图要靠读后端才能确认。
  updated_at: 'updated_at',
}

function RecordsPageContent() {
  const queryClient = useQueryClient()
  const actionRef = useRef<{ reload: () => void }>({ reload: () => {} })
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [sort, setSort] = useState<DataTableSort>({ key: 'updated_at', direction: 'desc' })
  const [filters, setFilters] = useState<RecordsFilters>(EMPTY_RECORD_FILTERS)
  const [filterDraft, setFilterDraft] = useState<RecordsFilters>(EMPTY_RECORD_FILTERS)
  const [filterOpen, setFilterOpen] = useState(false)
  const [deletingRecord, setDeletingRecord] = useState<AssetRecord | null>(null)
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  // 「为什么删」的草稿按目标对象保留：Esc / 取消关掉确认框不会抹掉刚写的半句话，
  // 换一条记录（或换到批量）才回到「未选」。理由是必填的（缺了确认框不放行），关窗不丢已选内容。
  const [deleteReason, setDeleteReason] = useAuditReasonDraft(deletingRecord?.id ?? null)
  const [batchDeleteReason, setBatchDeleteReason] = useAuditReasonDraft(batchDeleteOpen ? 'batch-delete' : null)
  const [batchOwnerOpen, setBatchOwnerOpen] = useState(false)
  const [batchOwnerItemIds, setBatchOwnerItemIds] = useState<number[]>([])
  const [batchOwnerFailedIds, setBatchOwnerFailedIds] = useState<number[]>([])
  const [batchOwnerRecordCount, setBatchOwnerRecordCount] = useState(0)
  const [batchOwnerLabels, setBatchOwnerLabels] = useState<Map<number, string>>(new Map())
  const [batchOwnerResult, setBatchOwnerResult] = useState<BatchOwnerResponse | null>(null)
  const [batchOwnerError, setBatchOwnerError] = useState<string | null>(null)
  const [batchOwnerNotice, setBatchOwnerNotice] = useState<string | null>(null)
  const [batchTagOpen, setBatchTagOpen] = useState(false)
  const [batchTagResult, setBatchTagResult] = useState<AssetTagBatchResponse | null>(null)
  const [batchTagError, setBatchTagError] = useState<string | null>(null)
  const [batchTagRecordCount, setBatchTagRecordCount] = useState(0)
  const [batchTagLabels, setBatchTagLabels] = useState<Map<number, string>>(new Map())
  // 分类（项级）：单项编辑 + 批量补齐。快照都来自行数据，不再为开弹窗另发请求。
  const [classificationEditing, setClassificationEditing] = useState<ClassificationSnapshot | null>(null)
  const [batchClassificationItems, setBatchClassificationItems] = useState<ClassificationSnapshot[] | null>(null)
  const [batchClassificationLoading, setBatchClassificationLoading] = useState(false)
  const [classificationNotice, setClassificationNotice] = useState<string | null>(null)
  const [draftBannerDismissed, setDraftBannerDismissed] = useState(false)
  // 项级聚焦面板（spec D6）：记录行对标签/备注完全只读，唯一写入口是「进入资产项」。
  const [itemSheetRecord, setItemSheetRecord] = useState<AssetRecord | null>(null)
  const [itemSheetNotice, setItemSheetNotice] = useState<string | null>(null)

  useEffect(() => {
    setBatchOwnerNotice(null)
  }, [selectedIds])

  // Modal states
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  // 更正理由草稿：同一行重开（含 Esc 关掉）仍保留，换行才清空
  const [correctReason, setCorrectReason] = useAuditReasonDraft(editingId)
  const [showBatchEditModal, setShowBatchEditModal] = useState(false)
  // v5 更正＝追加：列表默认仅 active 行；此开关打开后含被更正历史行（弱化呈现）。
  // 状态定义见下方 `evidenceFilterId` 之后——生效值要由它派生，声明顺序不能倒。
  const [ownerRecord, setOwnerRecord] = useState<AssetRecord | null>(null)

  // URL 预填：仪表盘提醒条「补录」弹窗跳转而来
  // institution_id/date：自动打开新增弹窗并把日期灌入表单初值
  const router = useRouter()
  const searchParams = useSearchParams()
  const urlPrefill = useMemo(() => {
    const rawDate = searchParams.get('date')
    const date = rawDate && /^\d{4}-\d{2}-\d{2}$/.test(rawDate) ? rawDate : undefined
    const rawId = searchParams.get('institution_id')
    const parsedId = rawId ? Number(rawId) : NaN
    const institutionId = Number.isInteger(parsedId) && parsedId > 0 ? parsedId : undefined
    if (institutionId === undefined) return undefined
    const initial: Partial<AssetRecordFormData> = {}
    if (date) initial.asset_date = date
    return { initial }
  }, [searchParams])

  // 视图切换：URL 是唯一事实来源（`?view=current|ledger`，缺省＝当前值），
  // 因此深链与浏览器前进/后退天然一致；切换用 replace，不污染历史栈。
  const view: RecordsView = searchParams.get('view') === 'ledger' ? 'ledger' : 'current'
  // 首页「含 N 项待分类」深链（`?highlight=draft`）：提示条高亮并滚进视野。
  // 记录页没有「只看 draft」的列表筛选（draft 是项级状态，列表按记录分页），
  // 所以定位手段就是把待办提示条本身点亮。
  const highlightDraft = searchParams.get('highlight') === 'draft'
  const switchView = useCallback((next: RecordsView) => {
    const params = new URLSearchParams(searchParams.toString())
    params.set('view', next)
    router.replace(`/records?${params.toString()}`, { scroll: false })
  }, [router, searchParams])

  // 凭证反查深链（spec D8 / 票 05）：`?evidence_id=N` 只给列表加一条筛选，
  // 不改默认视图（view/date/institution_id 分支一律不动；不带该参数时行为零变化）。
  // 非法值（非数字 / ≤0）按「未传」处理，不报错也不过滤。
  const rawEvidenceId = searchParams.get('evidence_id')
  const parsedEvidenceId = rawEvidenceId ? Number(rawEvidenceId) : NaN
  const evidenceFilterId = Number.isInteger(parsedEvidenceId) && parsedEvidenceId > 0
    ? parsedEvidenceId
    : undefined

  // 「显示历史（含被更正）」开关：两级状态——覆盖值只记「用户手动点过没有」，
  // 生效值（下面派生）才进请求与受控勾选框。四条决策写死在此，别「优化」掉：
  //   D1 带 `evidence_id` 进入时默认展开：下钻的唯一用途就是「看这张凭证产出的全部行」，
  //      默认藏起更正历史会让条数少于来源处显示的 used_rows（全行口径），用户会当成漏数据。
  //   D2 手动覆盖优先于 URL 默认：默认值是为了「打开就对」，不是禁止用户收窄回当前有效行。
  //   D3 覆盖值只存 state，不写进 URL：URL 已有 view/evidence_id，再加 show_superseded 会让
  //      深链与前进/后退语义复杂化，而覆盖本身是临时的（刷新后丢失是已知代价）。
  //   D4 清除 evidence_id 不复位：清筛选是「不再按凭证过滤」，不是「把视图重置为初始」，
  //      突然减行会像丢数据；故清除后覆盖值原样保留，未手动选过则回落到默认（未勾选）。
  const [showSupersededOverride, setShowSupersededOverride] = useState<boolean | undefined>(undefined)
  const effectiveShowSuperseded = showSupersededOverride ?? (evidenceFilterId !== undefined)
  // 清除筛选：只摘掉 evidence_id，其余参数（尤其 view）原样保留——
  // 不把用户从「记录明细」甩回默认视图；本就只有该参数时落到干净的 `/records`。
  const clearEvidenceFilter = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString())
    params.delete('evidence_id')
    const qs = params.toString()
    router.replace(qs ? `/records?${qs}` : '/records', { scroll: false })
  }, [router, searchParams])

  // 预填只处理「参数真的变了」：切换视图会重写 searchParams，
  // 若按对象身份触发会重复弹出已关闭的弹窗。
  const appliedPrefillSignature = useRef<string | null>(null)
  useEffect(() => {
    if (!urlPrefill) {
      appliedPrefillSignature.current = null
      return
    }
    const signature = `add|${urlPrefill.initial.asset_date ?? ''}`
    if (appliedPrefillSignature.current === signature) return
    appliedPrefillSignature.current = signature
    setShowAddModal(true)
  }, [urlPrefill])

  // Data queries（字典唯一入口：/api/classification/*）
  const { data: assetClassesData } = useQuery({ queryKey: ['assetClasses'], queryFn: api.getAssetClasses })
  const { data: liquidityLevelsData } = useQuery({ queryKey: ['liquidityLevels'], queryFn: api.getLiquidityLevels, staleTime: Infinity })
  const { data: institutionsData } = useQuery({ queryKey: ['institutions'], queryFn: api.getInstitutions })
  const { data: channelsData } = useQuery({ queryKey: ['channels'], queryFn: api.getChannels })
  const { data: membersData } = useQuery({ queryKey: ['members'], queryFn: api.getMembers })
  const { data: tagsData } = useQuery({ queryKey: ['tags'], queryFn: api.getTags })
  const { data: assetNames } = useQuery({ queryKey: ['assetNames'], queryFn: api.getAssetNames })
  const { data: assetItemsData } = useQuery({ queryKey: ['assetItems', true], queryFn: () => api.listAssetItems(true), staleTime: 60_000 })
  // 分类披露：与首页同一个 queryKey（命中同一份缓存，不重复取数）；缺数据时提示条
  // 只说「已计入净值」不给金额，绝不用 0 冒充规模。
  const { data: summaryData } = useQuery({ queryKey: ['summary'], queryFn: api.getSummary })
  // 项级备注读回（spec D4 的必然结果）：备注只落审计、后端无 GET 备注端点，
  // 故用现成 `GET /api/audit-log` 的 `note_update` 行派生出「每项最新备注」，
  // 一次请求覆盖整页（逐项读会变 N+1）。列表行只读便签标记与资产项面板共用这一份。
  const itemNotesQuery = useQuery({ queryKey: ['itemNotes'], queryFn: fetchItemNotes, staleTime: 30_000 })
  const noteByItem = itemNotesQuery.data

  const assetClasses: AssetClass[] = useMemo(() => assetClassesData?.items ?? [], [assetClassesData])
  const liquidityLevels: LiquidityLevel[] = useMemo(() => liquidityLevelsData?.items ?? [], [liquidityLevelsData])
  const institutions: Institution[] = useMemo(() => institutionsData?.items ?? [], [institutionsData])
  const channels = useMemo(() => channelsData?.items ?? [], [channelsData])
  const members: Member[] = useMemo(() => membersData?.items ?? [], [membersData])
  const tagItems: TagItem[] = useMemo(() => tagsData?.items ?? [], [tagsData])
  const tagOptions = useMemo(
    () => tagItems.map((tag) => ({ value: String(tag.id), label: tag.name })),
    [tagItems],
  )

  // Mutations
  // v5 契约层（spec §3/§5）：idemKey 随 mutate 变量传入 → react-query 自动重试复用同键；
  // 每次用户动作在调用点 newIdemKey() 生成新键。
  const createMutation = useMutation({
    mutationFn: (vars: { data: AssetRecordCreate; idemKey: string }) =>
      api.createAsset(vars.data, vars.idemKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['dates'] })
      setShowAddModal(false)
      actionRef.current?.reload()
      message.success('创建成功')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '创建失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (vars: { id: number; idemKey: string; audit?: AuditReasonValue }) =>
      api.deleteAsset(vars.id, vars.idemKey, toAuditReasonInput(vars.audit)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      actionRef.current?.reload()
      setDeletingRecord(null)
      setDeleteReason(EMPTY_AUDIT_REASON)
      message.success('删除成功')
    },
    onError: (error: unknown) => {
      // 兜底：确认框已在本地拦住「没选原因」，这里只接后端更严/别的路径漏过来的 422。
      message.error(
        isAuditReasonRequired(error)
          ? AUDIT_REASON_REQUIRED_TEXT
          : error instanceof Error ? error.message : '删除失败',
      )
    },
  })

  const updateMutation = useMutation({
    mutationFn: async ({ id, data, original, idemKeys, audit }: {
      id: number
      data: Partial<AssetRecordCreate>
      original?: AssetRecord
      // 更正理由（进审计的 query 参数，必填）；与业务字段无关，不写进请求体
      audit?: AuditReasonValue
      // v5 幂等规则：**每请求一键**。改名已移出记录侧（改走「身份 → 改名」的两步契约：
      // 预览影响面 → 带令牌确认），这里只剩金额更正的 correct 键。
      idemKeys: { correct: string }
    }) => {
      // 记录侧只写账本事实：金额＝write-once（走 correct），名称只读，
      // 快照日期不可直改；分类挂在资产项上，记录端点不再接受分类字段（传即 422）。
      if (data.asset_date && original && data.asset_date !== original.asset_date) {
        throw new Error('快照日期为 write-once 账本事实，不支持直接修改')
      }

      const amountChanged =
        data.amount !== undefined && original !== undefined &&
        Number(data.amount) !== Number(original.amount)
      const sideChanged =
        data.side !== undefined && original !== undefined &&
        data.side !== (original.side ?? 'asset')
      if ((amountChanged || sideChanged) && data.amount !== undefined) {
        await api.correctAsset(id, {
          amount: String(data.amount),
          side: data.side ?? original?.side,
        }, idemKeys.correct, toAuditReasonInput(audit))
      }

      // 改名现在是两步契约（原因 + 影响范围 + 预览确认「身份 → 改名」）：
      // 记录侧不再直接改名——否则会绕过原因记录，甚至无意改写历史快照名。
      // 这里只跳过改名并回报，金额/侧别等账本事实照常写入。
      const renameSkipped = !!data.asset_name && original?.asset_item_id != null &&
        data.asset_name !== original.asset_name
      return { renameSkipped }
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setEditingId(null)
      setCorrectReason(EMPTY_AUDIT_REASON)
      actionRef.current?.reload()
      if (result.renameSkipped) {
        message.warning('名称改动未保存：改名要先填原因与影响范围，请到「身份 → 改名」预览后确认。')
        return
      }
      message.success('更新成功')
    },
    onError: (error: unknown) => {
      // v5 信封：WRITE_ONCE_CONFLICT → 引导走更正流（结构化）
      if (error instanceof ApiError && error.code === 'WRITE_ONCE_CONFLICT') {
        const hint = (error.details?.hint as string) ||
          '账本事实不可直改，请在编辑弹窗修改金额以走「更正」'
        message.warning(hint)
      } else {
        message.error(error instanceof Error ? error.message : '更新失败')
      }
    },
  })

  const batchUpdateMutation = useMutation({
    mutationFn: ({ data, idemKey }: { data: BatchUpdateAssets; idemKey: string }) =>
      api.batchUpdateAssets(data, idemKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setShowBatchEditModal(false)
      actionRef.current?.reload()
      message.success('批量更新成功')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '批量更新失败')
    },
  })

  const batchDeleteMutation = useMutation({
    mutationFn: ({ ids, idemKey, audit }: { ids: number[]; idemKey: string; audit?: AuditReasonValue }) =>
      api.batchDeleteAssets(ids, idemKey, toAuditReasonInput(audit)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setSelectedIds([])
      setBatchDeleteOpen(false)
      setBatchDeleteReason(EMPTY_AUDIT_REASON)
      actionRef.current?.reload()
      message.success('批量删除成功')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '批量删除失败，请重试')
    },
  })

  // 列表查询：服务端分页/排序/筛选
  const fetchData = useCallback(async (params: RecordsQueryParams, sort: RecordsSortParam) => {
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

    // 分类过滤（ADR-0019）：全部落到资产项；旧参数名会被后端 422，故只发新名
    if (params.asset_class_id) {
      queryParams.asset_class_id = params.asset_class_id
    }
    if (params.institution_id) {
      queryParams.institution_id = params.institution_id
    }
    if (params.channel_id) {
      queryParams.channel_id = params.channel_id
    }
    if (params.owner_id) {
      queryParams.owner_id = params.owner_id
    }
    if (params.liquidity_days) {
      queryParams.liquidity_days = params.liquidity_days
    }
    if (params.tag_ids) {
      queryParams.tag_ids = params.tag_ids
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
        queryParams.sort_field = SORT_FIELD_MAPPING[sortField] || sortField
        queryParams.sort_order = sort[sortField] === 'ascend' ? 'asc' : 'desc'
      }
    }

    // v5 更正＝追加：默认仅 active 行，开关打开时含被更正历史行
    if (effectiveShowSuperseded) {
      queryParams.include_superseded = 'true'
    }

    // 凭证反查（spec D8）：只按来源凭证减行，不改分页/排序语义；
    // evidenceFilterId 未传（undefined）时 getAssets 不会追加该参数，URL 与改造前一致。
    const response = await api.getAssets(queryParams, evidenceFilterId)

    return {
      data: response.items,
      success: true,
      total: response.total,
    }
  }, [effectiveShowSuperseded, evidenceFilterId])

// DataTable 数据源：服务端分页/排序/筛选。
  const sortParam: RecordsSortParam = {
    [sort.key]: sort.direction === 'asc' ? 'ascend' : 'descend',
  }
  const listQuery = useQuery({
    queryKey: ['assetsList', page, pageSize, sort, filters, effectiveShowSuperseded, evidenceFilterId],
    // 记录明细查询按视图门禁（spec D7）：当前值视图自带 `/api/assets/current` 读模型，
    // 这条账本列表查询在那边纯属无效请求（改前改后都会发，默认排序变了才被验收抓出来）。
    enabled: view === 'ledger',
    queryFn: () => fetchData(
      { current: page, pageSize, ...filters },
      sortParam,
    ),
  })
  actionRef.current.reload = () => { void listQuery.refetch() }

  const tableData = useMemo(() => listQuery.data?.data ?? [], [listQuery.data])
  const archivedItemIds = useMemo(
    () => new Set((assetItemsData?.items ?? []).filter((item) => item.status === 'archived').map((item) => item.id)),
    [assetItemsData],
  )
  /** 分类未补齐的资产项（本站的核心待办）：照常计入净值，但按类别统计归不进去、导出也会跳过。 */
  const draftItems = useMemo(
    () => (assetItemsData?.items ?? []).filter((item) => item.status === 'draft'),
    [assetItemsData],
  )
  const isRecordSelectable = useCallback(
    (record: AssetRecord) => !record.asset_item_id || !archivedItemIds.has(record.asset_item_id),
    [archivedItemIds],
  )
  const selectedRows = useMemo(
    () => tableData.filter((row) => selectedIds.includes(row.id)),
    [tableData, selectedIds],
  )
  // 勾选合计只算当前页能解析的行：列表接口一次只回一页，翻页/改筛选后残留的勾选取不到金额，
  // 也不为此新增接口，差额由选择条上的「仅当前页 M 条」如实标注。
  const selectionTotals = useMemo(() => {
    let asset = new Decimal(0)
    let liability = new Decimal(0)
    let assetRows = 0
    let liabilityRows = 0

    for (const row of selectedRows) {
      const raw = String(row.amount).trim()
      if (!raw || !Number.isFinite(Number(raw))) continue
      // 金额按绝对值存储，方向只由 side 表达（缺省＝资产，与表格金额列一致）
      if ((row.side ?? 'asset') === 'liability') {
        liability = liability.plus(raw)
        liabilityRows += 1
      } else {
        asset = asset.plus(raw)
        assetRows += 1
      }
    }

    // 单侧只给一个带符号总额；两侧都有才分项，负债取绝对值避免「−」与「负债」重复表达方向
    const parts: Array<{ label: string; value: string }> = []
    if (assetRows > 0 && liabilityRows > 0) {
      parts.push(
        { label: '资产', value: asset.toString() },
        { label: '负债', value: liability.abs().toString() },
        { label: '净值', value: asset.minus(liability).toString() },
      )
    } else if (assetRows > 0) {
      parts.push({ label: '合计', value: asset.toString() })
    } else if (liabilityRows > 0) {
      parts.push({ label: '合计', value: liability.negated().toString() })
    }

    return {
      parts,
      // 跨页残留勾选数＝已选但当前页解析不到的条数
      missingCount: selectedIds.length - selectedRows.length,
    }
  }, [selectedIds.length, selectedRows])
  const selectedItemIds = useMemo(
    () => Array.from(new Set(selectedRows.map((row) => row.asset_item_id).filter((id): id is number => typeof id === 'number'))),
    [selectedRows],
  )
  const batchOwnerMutation = useMutation({
    mutationFn: ({ ids, data, audit, idemKey }: { ids: number[]; data: BatchOwnerFormData; audit: AuditReasonInput; idemKey: string }) =>
      api.batchSetItemOwners({ ids, ...data }, audit, idemKey),
    onSuccess: (response) => {
      const failedIds = response.failed.map((item) => item.id)
      const failedSet = new Set(failedIds)
      setBatchOwnerResult(response)
      setBatchOwnerFailedIds(failedIds)
      setBatchOwnerError(null)
      // 只保留当前页可映射的失败记录选择；跨页失败项仍可在弹窗内重试。
      setSelectedIds(tableData.filter((row) => row.asset_item_id && failedSet.has(row.asset_item_id)).map((row) => row.id))
      queryClient.invalidateQueries({ queryKey: ['assetsList'] })
      queryClient.invalidateQueries({ queryKey: ['itemOwners'] })
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      actionRef.current?.reload()
      setBatchOwnerNotice(null)
    },
    onError: (error) => setBatchOwnerError(batchOwnerErrorText(error)),
  })

  const openBatchOwner = useCallback(() => {
    if (selectedIds.length === 0) return
    if (selectedRows.length !== selectedIds.length) {
      setBatchOwnerNotice('批量改归属仅处理当前页选中的记录，请先清空跨页选择后再试。')
      return
    }
    const itemIds = selectedItemIds.filter((id) => !archivedItemIds.has(id))
    if (itemIds.length === 0) {
      setBatchOwnerNotice('所选记录没有可修改归属的资产项。')
      return
    }
    const labels = new Map<number, string>()
    for (const row of selectedRows) {
      if (row.asset_item_id) labels.set(row.asset_item_id, row.display_name ?? row.asset_name)
    }
    setBatchOwnerItemIds(itemIds)
    setBatchOwnerRecordCount(selectedRows.length)
    setBatchOwnerLabels(labels)
    setBatchOwnerFailedIds([])
    setBatchOwnerResult(null)
    setBatchOwnerError(null)
    setBatchOwnerNotice(null)
    setBatchOwnerOpen(true)
  }, [archivedItemIds, selectedIds, selectedItemIds, selectedRows])

  const submitBatchOwner = useCallback((data: BatchOwnerFormData, audit: AuditReasonInput) => {
    const ids = batchOwnerResult ? batchOwnerFailedIds : batchOwnerItemIds
    if (ids.length === 0) return
    setBatchOwnerError(null)
    batchOwnerMutation.mutate({ ids, data, audit, idemKey: newIdemKey() })
  }, [batchOwnerFailedIds, batchOwnerItemIds, batchOwnerMutation, batchOwnerResult])

  const batchTagMutation = useMutation({
    mutationFn: ({ recordIds, data, idemKey }: { recordIds: number[]; data: { add_tag_ids: number[]; remove_tag_ids: number[] }; idemKey: string }) =>
      api.batchSetAssetTags({ record_ids: recordIds, ...data }, idemKey),
    onSuccess: (response) => {
      const failedIds = response.failed.map((item) => item.id)
      setBatchTagResult(response)
      setBatchTagError(null)
      setSelectedIds(failedIds)
      queryClient.invalidateQueries({ queryKey: ['assetsList'] })
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      actionRef.current?.reload()
    },
    onError: (error) => setBatchTagError(batchTagErrorText(error)),
  })

  const openBatchTags = useCallback(() => {
    if (selectedIds.length === 0) return
    const labels = new Map<number, string>()
    for (const row of selectedRows) labels.set(row.id, row.display_name ?? row.asset_name)
    setBatchTagRecordCount(selectedIds.length)
    setBatchTagLabels(labels)
    setBatchTagResult(null)
    setBatchTagError(null)
    setBatchTagOpen(true)
  }, [selectedIds, selectedRows])

  const submitBatchTags = useCallback((data: { add_tag_ids: number[]; remove_tag_ids: number[] }) => {
    const ids = batchTagResult ? batchTagResult.failed.map((item) => item.id) : selectedIds
    if (ids.length === 0) return
    setBatchTagError(null)
    batchTagMutation.mutate({ recordIds: ids, data, idemKey: newIdemKey() })
  }, [batchTagMutation, batchTagResult, selectedIds])

  // --- 分类（项级）：单项编辑与批量补齐的入口 ---

  /** 从一条记录打开该项的分类编辑（分类挂在资产项上，同一项多行共用一个入口）。 */
  const openItemClassification = useCallback((record: AssetRecord) => {
    const archived = !!record.asset_item_id && archivedItemIds.has(record.asset_item_id)
    const snapshot = snapshotFromRecord(record, { archived })
    if (!snapshot) {
      setClassificationNotice('这条记录还没有关联资产项，无法按项设置分类。')
      return
    }
    setClassificationNotice(null)
    setClassificationEditing(snapshot)
  }, [archivedItemIds])

  // --- 项级元数据（标签 / 备注）：唯一写入口是「进入资产项」→ 右侧面板 ---

  /** 记录行「进入资产项」：元数据挂项，没有关联项的行改不了（先归一成项）。 */
  const openItemSheet = useCallback((record: AssetRecord) => {
    if (!record.asset_item_id) {
      message.info('这条记录还没有关联资产项，无法按项改标签或备注。')
      return
    }
    setItemSheetRecord(record)
  }, [])

  /**
   * 当前值视图「进入资产项」（`.scratch/current-value-note/spec.md` D3/D4）：同一个抽屉、
   * 同一份开关状态——只是入参换成「每项一行」的当前值行；历史清单/最新一期仍由面板
   * 按 item_id 自取，所以两个视图看到的是同一份历史。
   */
  const openItemSheetFromCurrentValue = useCallback((item: CurrentValueItem) => {
    setItemSheetRecord(sheetRecordFromCurrentValue(item))
  }, [])

  /**
   * 面板保存回执（spec §3「保存标签 → 同项所有行 chips 同步」）：
   * - 备注：写回本地映射，同一项的多行便签标记立刻一致（不必等重新取数）；
   * - 标签：标签随记录行数据下发，重取当前页让同项每一行同步；
   * - 只提示真的存下来的部分（标签失败时备注已落库，如实只说「备注」）。
   */
  const handleItemSheetSaved = useCallback((result: AssetItemSheetSaveResult) => {
    if (result.noteSaved) {
      queryClient.setQueryData<Map<number, string | null>>(['itemNotes'], (previous) => {
        const next = new Map(previous ?? [])
        next.set(result.itemId, result.note)
        return next
      })
      // 当前值视图的备注来自读模型本身（每行带 `note`，`.scratch/current-value-note` D1）：
      // 先就地改缓存让便签立刻出现/消失，再失效重取把服务端口径拉回来（不必等用户手动刷新）。
      queryClient.setQueriesData<CurrentValueResponse>({ queryKey: ['assetsCurrent'] }, (previous) =>
        previous
          ? {
              ...previous,
              items: previous.items.map((row) =>
                row.item_id === result.itemId ? { ...row, note: result.note } : row),
            }
          : previous,
      )
      void queryClient.invalidateQueries({ queryKey: ['assetsCurrent'] })
    }
    const name = itemSheetRecord?.display_name || itemSheetRecord?.asset_name || ''
    const parts: string[] = []
    if (result.noteSaved) parts.push('备注')
    if (result.tagsSaved) parts.push('标签')
    if (parts.length > 0) {
      setItemSheetNotice(`已保存到资产项「${name}」：${parts.join(' / ')}对这一项的全部历史记录生效。`)
    }
    if (result.tagsSaved) actionRef.current?.reload()
  }, [itemSheetRecord, queryClient])

  /** 提示条入口：把全部待补项一次带进批量弹窗（含尚无记录的项）。 */
  const openBatchClassificationForDrafts = useCallback(async () => {
    if (draftItems.length === 0) return
    setClassificationNotice(null)
    setBatchClassificationLoading(true)
    try {
      const snapshots = await loadClassificationSnapshots(draftItems.map((item) => item.id))
      if (snapshots.length === 0) {
        setClassificationNotice('没有取到待补分类的资产项，请刷新后重试。')
        return
      }
      setBatchClassificationItems(snapshots)
    } catch (batchError) {
      message.error(batchError instanceof Error ? batchError.message : '加载待补分类的资产项失败')
    } finally {
      setBatchClassificationLoading(false)
    }
  }, [draftItems])

  /** 勾选入口：只处理当前页选中的记录（跨页勾选取不到金额与分类）。 */
  const openBatchClassificationForSelection = useCallback(() => {
    if (selectedIds.length === 0) return
    if (selectedRows.length !== selectedIds.length) {
      setClassificationNotice('批量补分类只处理当前页选中的记录；请先取消跨页选择。')
      return
    }
    const snapshots = selectedRows
      .map((row) => snapshotFromRecord(row, {
        archived: !!row.asset_item_id && archivedItemIds.has(row.asset_item_id),
      }))
      .filter((item): item is ClassificationSnapshot => Boolean(item))
    if (snapshots.length === 0) {
      setClassificationNotice('所选记录没有关联资产项，无法按项设置分类。')
      return
    }
    setClassificationNotice(null)
    setBatchClassificationItems(snapshots)
  }, [archivedItemIds, selectedIds, selectedRows])
  const editingRecord = editingId ? tableData.find((row) => row.id === editingId) : null
  const total = listQuery.data?.total ?? 0
  const activeFilterCount = Object.entries(filters).filter(([key, value]) => key !== 'asset_name' && value).length

  const columns: DataTableColumn<AssetRecord>[] = [
    {
      key: 'asset_date', header: '日期', sortable: true, widthTier: 'date',
      cell: (record) => <span className="pf-num whitespace-nowrap text-meta text-secondary">{formatDate(record.asset_date)}</span>,
      mobile: { role: 'meta', label: '日期' },
    },
    {
      key: 'asset_name', header: '资产名称', sortable: true, widthTier: 'text-mid',
      cell: (record) => {
        const archived = !!record.asset_item_id && archivedItemIds.has(record.asset_item_id)
        const tags = record.tags ?? []
        // 项级备注（只读）：行里只给一个「有备注」标记 + 悬停看全文，
        // 写入口在资产项面板（spec D6：记录行对元数据 100% 只读）。
        const note = record.asset_item_id != null ? noteByItem?.get(record.asset_item_id) ?? null : null
        return (
          <div className="min-w-0">
            <span className="flex min-w-0 flex-wrap items-center gap-1">
              {/* 名称本身就是「进入资产项」的入口：操作列在窄视口要横向滚才看见，
                  而元数据的唯一写入口不能靠滚动才找得到（同一动作在操作列也有显式按钮）。
                  `text-mid` 档是唯一允许换行的文本档（spec D1）——不截断，长名换行。 */}
              <button
                type="button"
                onClick={() => openItemSheet(record)}
                aria-label={`进入资产项${record.display_name || record.asset_name}，改这一项的标签与备注`}
                title="进入资产项：改这一项的标签与备注（影响它的全部历史记录）"
                className="min-w-0 text-left font-medium text-ink transition-colors hover:text-brand-700 hover:underline"
              >
                {record.asset_name}
              </button>
              {/* 来源凭证（D1/D2/D5）：有来源才是真链接，没有就整块不渲染——不留破折号，也不留空胶囊。
                  图标取 lucide 的 Image，别名 ImageIcon 以避开 jsx-a11y 把它当 <img> 的误报（实际渲染 svg）。 */}
              {record.source_evidence_id != null && (
                <Link
                  href={`/data/evidence/${record.source_evidence_id}`}
                  className="inline-flex shrink-0 items-center gap-0.5 rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted hover:text-ink"
                >
                  <ImageIcon size={12} aria-hidden />
                  凭证
                </Link>
              )}
              {note && (
                <span
                  role="img"
                  aria-label={`这个资产项有备注：${note}`}
                  title={note}
                  className="inline-flex shrink-0 items-center rounded-pill bg-status-warn-surface px-1 py-0.5 text-status-warn-text"
                >
                  <StickyNote size={12} aria-hidden />
                </span>
              )}
              {archived && <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">已归档</span>}
              {/* 「已替代」与「已归档」是两件事（spec D11）：一条记录可以既被更正替代、
                  又属于已归档的资产项，两枚徽章并存。删掉整行 opacity-60 后，
                  这枚徽章是「被替代」唯一的可见信号。 */}
              {record.superseded_by != null && (
                <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">已替代</span>
              )}
            </span>
            {tags.length > 0 && <span className="mt-1 flex flex-wrap gap-1" title={tags.map((tag) => tag.name).join('、')}>{tags.slice(0, 2).map((tag) => <span key={tag.id} className="rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-secondary">{tag.name}</span>)}{tags.length > 2 && <span className="rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">+{tags.length - 2}</span>}</span>}
          </div>
        )
      },
      mobile: { role: 'title' },
    },
    {
      key: 'asset_class_name', header: '资产类别', widthTier: 'label',
      cell: (record) => <span className="text-secondary" title={record.asset_class_path ?? undefined}>{record.asset_class_name ?? '—'}</span>,
      mobile: { role: 'meta', label: '资产类别' },
    },
    {
      key: 'institution_name', header: '机构 / 渠道', widthTier: 'text-mid',
      cell: (record) => {
        const label = [record.institution_name, record.channel_name].filter(Boolean).join(' / ')
        return <span className="text-secondary" title={label || undefined}>{label || '—'}</span>
      },
      mobile: { role: 'meta', label: '机构' },
    },
    {
      key: 'liquidity_days', header: '流动性', widthTier: 'text-short',
      cell: (record) => <span className="whitespace-nowrap text-secondary" title={record.liquidity_label ?? undefined}>{record.liquidity_label ?? '—'}</span>,
      mobile: { role: 'meta', label: '流动性' },
    },
    {
      key: 'owner_name', header: '归属', widthTier: 'person',
      cell: (record) => (
        <button
          type="button"
          onClick={() => setOwnerRecord(record)}
          aria-label={`设置${record.asset_name}归属`}
          className="inline-flex max-w-full flex-wrap items-center gap-1 text-left text-secondary"
        >
          <span>{record.owner_name ?? '未标注'}</span>
          {record.owner_kind && <span className="shrink-0 rounded-pill bg-subtle px-1.5 py-0.5 text-meta text-muted">{MEMBER_ROLE_LABEL[record.owner_kind] ?? record.owner_kind}</span>}
        </button>
      ),
      mobile: { role: 'meta', label: '归属' },
    },
    {
      key: 'classification_missing', header: '分类', widthTier: 'label',
      cell: (record) => {
        const missing = requiredMissing(record.classification_missing)
        const hasItem = record.asset_item_id != null
        if (!hasItem) {
          return <span className="text-meta text-muted" title="这条记录没有关联资产项，分类只能按项设置">—</span>
        }
        return (
          <button
            type="button"
            onClick={() => openItemClassification(record)}
            title={missing.length > 0 ? `还差：${dimsText(missing)}；点击补齐` : '分类已齐；点击查看或修改'}
            aria-label={missing.length > 0
              ? `补齐「${record.asset_name}」的分类，还差${dimsText(missing)}`
              : `查看「${record.asset_name}」的分类`}
            className={cn(
              'inline-flex min-h-[32px] items-center gap-1 whitespace-nowrap rounded-pill px-2 text-meta transition-colors',
              missing.length > 0
                ? 'bg-status-warn-surface text-status-warn-text hover:bg-status-warn/20'
                : 'bg-subtle text-secondary hover:text-ink',
            )}
          >
            {missing.length > 0 ? `补分类 · 缺 ${missing.length}` : '已齐'}
            <Pencil size={11} aria-hidden className="opacity-60" />
          </button>
        )
      },
      mobile: { role: 'meta', label: '分类' },
    },
    {
      key: 'amount', header: '金额（元）', sortable: true, align: 'right', widthTier: 'amount-extra',
      cell: (record) => (
        <span className="inline-flex items-center justify-end gap-1.5">
          <Money value={record.amount} unit="cny" size="s" />
          {record.side === 'liability' && <span className="text-meta text-muted">负债</span>}
        </span>
      ),
      mobile: { role: 'value' },
    },
    {
      // 更新时间＝该行最后写入时间（批量归一化/改名回写会一次刷掉多行），属系统元数据；
      // 放最右、右对齐，与最左的「日期」（账期/业务时间）分区，避免被读成同一类时间。
      key: 'updated_at', header: '更新时间', sortable: true, align: 'right', widthTier: 'datetime',
      cell: (record) => (
        <span className="pf-num whitespace-nowrap text-meta text-secondary" title={record.updated_at}>
          {formatDateTime(record.updated_at)}
        </span>
      ),
      // span: 2 —— 它是 meta 的第 7 项（奇数），单格会落在第 4 行左格、右格空着；
      // 占满整行收掉空洞，值仍是「标签在上、值在下」的同一套 meta 排版。
      mobile: { role: 'meta', label: '更新时间', span: 2 },
    },
  ]

  const openFilters = () => {
    setFilterDraft(filters)
    setFilterOpen(true)
  }

  const rowActions = (record: AssetRecord) => (
    <>
      {/* 项级元数据的唯一写入口（spec D6）：记录行本身对标签/备注只读 */}
      <button
        type="button"
        onClick={() => openItemSheet(record)}
        aria-label={`进入资产项${record.display_name || record.asset_name}，改这一项的标签与备注`}
        title="进入资产项：改这一项的标签与备注（影响它的全部历史记录）"
        className="inline-flex min-h-[44px] shrink-0 items-center gap-1 whitespace-nowrap rounded-control px-2 text-meta font-medium text-brand-700 transition-colors hover:bg-brand-50"
      >
        进入资产项
        <ArrowUpRight size={14} aria-hidden />
      </button>
      <button
        type="button"
        onClick={() => {
          setFilters((current) => ({ ...current, asset_name: record.asset_name }))
          setPage(1)
        }}
        aria-label={`搜索同名资产${record.asset_name}`}
        title="搜索同名资产"
        className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"
      >
        <Search size={15} aria-hidden />
      </button>
      <button
        type="button"
        onClick={() => setEditingId(record.id)}
        aria-label={`编辑${record.asset_name}`}
        title="编辑"
        className="inline-flex h-11 w-11 items-center justify-center rounded-control text-muted"
      >
        <Pencil size={15} aria-hidden />
      </button>
      <button
        type="button"
        onClick={() => setDeletingRecord(record)}
        aria-label={`删除${record.asset_name}`}
        title="删除"
        className="inline-flex h-11 w-11 items-center justify-center rounded-control text-status-critical-text"
      >
        <Trash2 size={15} aria-hidden />
      </button>
    </>
  )

  return (
    <PageContainer width="wide" className="space-y-5">
      <PageHeader
        title="资产记录"
        description={view === 'current'
          ? '每个资产项截至参考日的最新值；点金额就地修改，分类按资产项一次设置。'
          : '资产快照记录；支持筛选、排序、批量修改与更正历史。'}
        actions={
          <>
            <button type="button" onClick={() => setShowBatchEditModal(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
              <Edit3 size={16} aria-hidden /> 批量修改
            </button>
            <button type="button" onClick={() => setShowAddModal(true)} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control bg-action-primary px-4 text-body-strong font-medium text-white">
              <Plus size={16} aria-hidden /> 新增记录
            </button>
          </>
        }
      />

      <nav aria-label="记录类型" className="flex w-fit gap-1 rounded-control bg-subtle p-1">
        <Link href="/records" aria-current="page" className="inline-flex min-h-[44px] items-center rounded-control bg-surface px-4 text-body font-medium text-brand-700 shadow-card">资产记录</Link>
        <Link href="/expenses" className="inline-flex min-h-[44px] items-center rounded-control px-4 text-body text-muted">支出记录</Link>
      </nav>

      {/* 视图切换：URL 唯一事实来源（?view=），缺省「当前值」 */}
      <div className="flex gap-1 overflow-x-auto border-b border-line" role="group" aria-label="记录视图">
        {RECORDS_VIEW_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            aria-pressed={view === tab.key}
            onClick={() => switchView(tab.key)}
            className={cn(
              'min-h-[44px] shrink-0 border-b-2 px-4 text-body font-medium transition-colors duration-fast',
              view === tab.key ? 'border-brand-600 text-brand-700' : 'border-transparent text-muted',
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* 凭证反查提示（spec D8 / 票 05）：只在 `?evidence_id=N` 时出现；
          不带该参数时页面与改造前完全一致。筛选落在列表接口，本页不自算。 */}
      {evidenceFilterId !== undefined && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-card border border-line bg-surface px-4 py-2 shadow-card">
          <span className="min-w-0 text-body text-secondary">
            仅显示凭证 <strong className="text-ink">#{evidenceFilterId}</strong> 关联的行
            {view !== 'ledger' && (
              <span className="text-meta text-muted">（当前为「当前值」视图，切到「记录明细」查看筛选结果）</span>
            )}
          </span>
          <button
            type="button"
            onClick={clearEvidenceFilter}
            className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body font-medium text-ink"
          >
            <X size={15} aria-hidden /> 清除筛选
          </button>
        </div>
      )}

      {/* 分类待办提示：两个视图共用，点进批量补齐（首页「含 N 项待分类」深链会点亮它） */}
      {!draftBannerDismissed && draftItems.length > 0 && (
        <DraftClassificationBanner
          count={draftItems.length}
          assetAmount={(summaryData?.unclassified_count ?? 0) > 0 ? summaryData?.unclassified_asset_amount : undefined}
          liabilityAmount={(summaryData?.unclassified_count ?? 0) > 0 ? summaryData?.unclassified_liability_amount : undefined}
          highlight={highlightDraft}
          loading={batchClassificationLoading}
          onOpenBatch={() => { void openBatchClassificationForDrafts() }}
          onDismiss={() => setDraftBannerDismissed(true)}
        />
      )}
      {classificationNotice && (
        <p className="rounded-control border border-line bg-subtle px-3 py-2 text-meta text-secondary">
          {classificationNotice}
        </p>
      )}

      {view === 'current' ? (
        <CurrentValueTable
          onEditClassification={setClassificationEditing}
          // 抽屉状态机留在本页（与记录明细视图同一个面板）：这里只把回调透传下去
          onOpenItemSheet={openItemSheetFromCurrentValue}
        />
      ) : (
        <>
          {selectedIds.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-card border border-line bg-surface px-4 py-2 shadow-card">
              {/* 条数与合计同句、同一个 aria-live：勾选变化时读屏一次读完，不必再追问合计 */}
              <span className="min-w-0 text-body text-secondary" aria-live="polite" aria-atomic="true">
                已选择 <strong className="text-ink">{selectedIds.length}</strong> 条记录
                {selectionTotals.parts.length > 0 && <span aria-hidden className="text-muted">{' · '}</span>}
                {selectionTotals.parts.map((part, index) => (
                  <span key={part.label}>
                    {index > 0 && <span aria-hidden className="text-muted">{' · '}</span>}
                    <span className="whitespace-nowrap">
                      <span className="text-meta text-secondary">{part.label}</span>{' '}
                      <Money value={part.value} size="s" className="font-mono" />
                    </span>
                  </span>
                ))}
                {selectionTotals.missingCount > 0 && (
                  <span className="whitespace-nowrap text-meta text-muted">（仅当前页 {selectedRows.length} 条）</span>
                )}
              </span>
              <div className="grid w-full grid-cols-2 gap-2 tablet:flex tablet:w-auto">
                <button type="button" onClick={openBatchOwner} className="inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body font-medium text-ink">
                  <Users size={15} aria-hidden /> 批量改归属
                </button>
                <button type="button" onClick={openBatchTags} className="inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body font-medium text-ink">
                  <Tags size={15} aria-hidden /> 批量打标签
                </button>
                <button
                  type="button"
                  onClick={openBatchClassificationForSelection}
                  disabled={batchClassificationLoading}
                  className="col-span-2 inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body font-medium text-ink disabled:opacity-60 tablet:col-span-1"
                >
                  <Layers size={15} aria-hidden /> {batchClassificationLoading ? '加载中…' : '批量补分类'}
                </button>
                <button type="button" onClick={() => setBatchDeleteOpen(true)} className="inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-control border border-status-critical/30 bg-status-critical-surface px-3 text-body font-medium text-status-critical-text">
                  <Trash2 size={15} aria-hidden /> 批量删除
                </button>
                <button type="button" onClick={() => setSelectedIds([])} className="col-span-2 inline-flex min-h-[44px] items-center justify-center rounded-control px-3 text-body text-secondary tablet:col-span-1">取消选择</button>
              </div>
              {batchOwnerNotice && <p className="w-full text-meta text-status-warn-text">{batchOwnerNotice}</p>}
            </div>
          )}

          {/* 项级保存回执（spec D6）：面板关掉之后，改的结果要留在页面上 */}
          {itemSheetNotice && (
            <div className="flex items-start justify-between gap-3 rounded-control border border-brand-200 bg-brand-50 px-3 py-2 text-meta text-brand-800">
              <span className="min-w-0">{itemSheetNotice}</span>
              <button
                type="button"
                onClick={() => setItemSheetNotice(null)}
                aria-label="关闭提示"
                className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-brand-700"
              >
                <X size={14} aria-hidden />
              </button>
            </div>
          )}

          <DataTable
            label="资产记录列表"
            rowActionsWidth={actionsWidth([{ label: '进入资产项', icon: true }, 'icon', 'icon', 'icon'])}
            rows={tableData}
            columns={columns}
            rowKey={(record) => record.id}
            loading={listQuery.isLoading}
            error={listQuery.error}
            onRetry={() => listQuery.refetch()}
            sort={sort}
            onSortChange={(next) => { setSort(next); setPage(1) }}
            pagination={{
              page, pageSize, total,
              onPageChange: setPage,
              pageSizeOptions: [10, 20, 50, 100],
              onPageSizeChange: (size) => { setPageSize(size); setPage(1) },
            }}
            selection={{ selectedKeys: selectedIds, onChange: (keys) => setSelectedIds(keys as number[]) }}
            isRowSelectable={isRecordSelectable}
            rowActions={rowActions}
            empty={{
              title: activeFilterCount || filters.asset_name ? '当前筛选无结果' : '暂无资产记录',
              description: activeFilterCount || filters.asset_name ? '调整筛选条件后重试。' : '先新增一条资产记录。',
              action: (
                <button type="button" onClick={() => setShowAddModal(true)} className="inline-flex min-h-[44px] items-center rounded-control bg-action-primary px-4 text-body-strong text-white">新增记录</button>
              ),
            }}
            toolbar={
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative w-full max-w-[320px]">
                  <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
                  <input
                    type="text"
                    inputMode="search"
                    value={filters.asset_name}
                    onChange={(e) => { setFilters((current) => ({ ...current, asset_name: e.target.value })); setPage(1) }}
                    placeholder="搜索资产名称"
                    aria-label="搜索资产名称"
                    className="min-h-[44px] w-full rounded-control border border-line-strong bg-surface pl-9 pr-3 text-body text-ink placeholder:text-muted"
                  />
                </div>
                <button type="button" onClick={openFilters} className="inline-flex min-h-[44px] items-center gap-1.5 rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
                  <Filter size={15} aria-hidden /> 筛选
                  {activeFilterCount > 0 && <span className="pf-num inline-flex min-w-5 items-center justify-center rounded-pill bg-brand-600 px-1.5 text-meta text-white">{activeFilterCount}</span>}
                </button>
                <label className="inline-flex min-h-[44px] items-center gap-2 px-1 text-meta text-secondary">
                  <input type="checkbox" checked={effectiveShowSuperseded} onChange={(e) => { setShowSupersededOverride(e.target.checked); setPage(1) }} className="h-4 w-4 accent-[var(--pf-color-action-primary)]" />
                  显示历史（含被更正）
                </label>
                <button type="button" onClick={() => { void listQuery.refetch() }} className="inline-flex min-h-[44px] items-center rounded-control px-3 text-meta text-secondary">刷新</button>
                <span className="pf-num ml-auto text-meta text-muted">{listQuery.isLoading || listQuery.isError ? "—" : `共 ${total} 条`}</span>
              </div>
            }
          />

          <FilterSheet
            open={filterOpen}
            onClose={() => setFilterOpen(false)}
            onReset={() => setFilterDraft(EMPTY_RECORD_FILTERS)}
            onApply={() => { setFilters(filterDraft); setPage(1); setFilterOpen(false) }}
          >
            <div className="grid grid-cols-1 gap-4 tablet:grid-cols-2">
              <LazyDateField label="起始日期" value={filterDraft.date_from} onChange={(value) => setFilterDraft((draft) => ({ ...draft, date_from: value }))} />
              <LazyDateField label="结束日期" value={filterDraft.date_to} onChange={(value) => setFilterDraft((draft) => ({ ...draft, date_to: value }))} />
              <SelectField
                label="资产类别" value={filterDraft.asset_class_id}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, asset_class_id: value }))}
                options={assetClasses.map((item) => ({ value: String(item.id), label: item.path || item.name }))}
              />
              <SelectField
                label="机构" value={filterDraft.institution_id}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, institution_id: value }))}
                options={institutions.map((item) => ({ value: String(item.id), label: item.name }))}
              />
              <SelectField
                label="渠道" value={filterDraft.channel_id}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, channel_id: value }))}
                options={channels.map((item) => ({ value: String(item.id), label: item.name }))}
              />
              <SelectField
                label="流动性档位" value={filterDraft.liquidity_days}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, liquidity_days: value }))}
                options={liquidityLevels.map((level) => ({ value: String(level.days), label: level.label }))}
              />
              <SelectField
                label="归属成员" value={filterDraft.owner_id}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, owner_id: value }))}
                options={members.map((member) => ({ value: String(member.id), label: `${member.name}（${MEMBER_ROLE_LABEL[member.role] ?? member.role}）` }))}
              />
              <SelectField
                label="标签" value={filterDraft.tag_ids}
                onChange={(value) => setFilterDraft((draft) => ({ ...draft, tag_ids: value }))}
                options={tagOptions}
              />
              <AmountField label="金额下限" value={filterDraft.amount_min} onChange={(value) => setFilterDraft((draft) => ({ ...draft, amount_min: value }))} />
              <AmountField label="金额上限" value={filterDraft.amount_max} onChange={(value) => setFilterDraft((draft) => ({ ...draft, amount_max: value }))} />
            </div>
          </FilterSheet>
        </>
      )}

      {/* Add Modal */}
      <Modal open={showAddModal} onClose={() => setShowAddModal(false)} title="新增资产记录">
        <RecordForm
          initial={urlPrefill?.initial}
          onSubmit={(data) => createMutation.mutate({ data, idemKey: newIdemKey() })}
          onCancel={() => setShowAddModal(false)}
        />
      </Modal>

      {/* Edit Modal */}
      <Modal open={editingId !== null} onClose={() => setEditingId(null)} title="编辑资产记录">
        {editingRecord && (
          <RecordForm
            initial={{
              asset_date: editingRecord.asset_date,
              asset_name: editingRecord.asset_name,
              amount: editingRecord.amount,
              side: editingRecord.side,
            }}
            classification={[
              { label: '资产类别', value: editingRecord.asset_class_path ?? editingRecord.asset_class_name },
              { label: '流动性', value: editingRecord.liquidity_label },
              { label: '机构', value: editingRecord.institution_name },
              { label: '渠道', value: editingRecord.channel_name },
              { label: '归属', value: editingRecord.owner_name },
              { label: '风险等级', value: editingRecord.risk_level },
              { label: '标签', value: (editingRecord.tags ?? []).map((tag) => tag.name).join('、') || null },
            ]}
            onSubmit={(data, audit) => updateMutation.mutate({
              id: editingId!, data, original: editingRecord ?? undefined, audit,
              // 每请求一键：重试复用同键
              idemKeys: { correct: newIdemKey() },
            })}
            audit={correctReason}
            onAuditChange={setCorrectReason}
            onCancel={() => setEditingId(null)}
            isEditing={true}
          />
        )}
      </Modal>

      {/* New Batch Edit Modal */}
      {assetNames && (
        <NewBatchEditModal
          open={showBatchEditModal}
          onClose={() => setShowBatchEditModal(false)}
          assetNames={assetNames as string[]}
          onSubmit={(data) => batchUpdateMutation.mutate({ data, idemKey: newIdemKey() })}
        />
      )}

      <BatchOwnerDialog
        open={batchOwnerOpen}
        selectedRecordCount={batchOwnerRecordCount}
        selectedItemCount={batchOwnerItemIds.length}
        members={members}
        pending={batchOwnerMutation.isPending}
        result={batchOwnerResult}
        error={batchOwnerError}
        itemLabelById={batchOwnerLabels}
        onSubmit={submitBatchOwner}
        onClose={() => setBatchOwnerOpen(false)}
      />

      <BatchTagDialog
        open={batchTagOpen}
        selectedRecordCount={batchTagRecordCount}
        tags={tagItems}
        pending={batchTagMutation.isPending}
        result={batchTagResult}
        error={batchTagError}
        recordLabelById={batchTagLabels}
        onSubmit={submitBatchTags}
        onClose={() => setBatchTagOpen(false)}
      />

      <ConfirmDialog
        open={!!deletingRecord}
        onClose={() => setDeletingRecord(null)}
        onConfirm={() => deletingRecord && deleteMutation.mutate({
          id: deletingRecord.id, idemKey: newIdemKey(), audit: deleteReason,
        })}
        title={`删除资产记录「${deletingRecord?.asset_name ?? ''}」？`}
        description={`将删除 ${deletingRecord ? formatDate(deletingRecord.asset_date) : ''} 的这条快照记录；删掉后不可恢复，这次操作本身会留在操作记录里。`}
        confirmLabel="删除记录"
        danger
        loading={deleteMutation.isPending}
      >
        <AuditReasonField
          label="为什么删"
          value={deleteReason}
          onChange={setDeleteReason}
        />
      </ConfirmDialog>

      <ConfirmDialog
        open={batchDeleteOpen}
        onClose={() => setBatchDeleteOpen(false)}
        onConfirm={() => batchDeleteMutation.mutate({
          ids: selectedIds, idemKey: newIdemKey(), audit: batchDeleteReason,
        })}
        title={`批量删除 ${selectedIds.length} 条记录？`}
        description="删除后不可恢复；如需保留历史，请先导出。"
        confirmLabel="批量删除"
        danger
        loading={batchDeleteMutation.isPending}
      >
        <AuditReasonField
          label="为什么删这一批"
          value={batchDeleteReason}
          onChange={setBatchDeleteReason}
          hint="整批共用这一条理由；记录会留下谁在何时删了哪些行；必须选一条原因才能删。"
        />
      </ConfirmDialog>

      <LazyItemOwnerDialog
        open={!!ownerRecord}
        record={ownerRecord}
        onClose={() => setOwnerRecord(null)}
        onSaved={() => { void listQuery.refetch() }}
      />

      {/* 分类（项级）：单项编辑 / 批量补齐 */}
      <ItemClassificationDialog
        item={classificationEditing}
        onClose={() => setClassificationEditing(null)}
        onSaved={() => { void listQuery.refetch() }}
      />
      <BatchClassificationDialog
        open={batchClassificationItems !== null}
        items={batchClassificationItems ?? []}
        onClose={() => setBatchClassificationItems(null)}
        onDone={() => { void listQuery.refetch() }}
      />

      {/* 资产项面板（spec D6）：标签/备注的唯一写入口。
          `note` 传 undefined＝还在读（面板会等），读失败传 null＋noteReadFailed 明确提示覆盖风险。 */}
      <AssetItemSheet
        record={itemSheetRecord}
        tags={tagItems}
        note={itemNotesQuery.isError
          ? null
          : noteByItem
            ? (itemSheetRecord?.asset_item_id != null
                ? noteByItem.get(itemSheetRecord.asset_item_id) ?? null
                : null)
            : undefined}
        noteReadFailed={itemNotesQuery.isError}
        archived={!!itemSheetRecord?.asset_item_id && archivedItemIds.has(itemSheetRecord.asset_item_id)}
        onClose={() => setItemSheetRecord(null)}
        onSaved={handleItemSheetSaved}
      />
    </PageContainer>
  )
}
