'use client'

import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import dynamic from 'next/dynamic'
import { useRouter } from 'next/navigation'
import { api, ApiError, newIdemKey } from '@/lib/api'
import { formatCurrency, formatDate, formatPercent, formatRate, formatValue, todayDate } from '@/lib/format'
import { carriedDetailText, hasCarried } from '@/lib/carried'
import { resolveItemIdByName } from '@/lib/lineage'
import {
  DRIFT_LABEL,
  DRIFT_OBSERVATION,
  driftBadgeTitle,
  driftCountsText,
  driftDetail,
  driftObservationText,
  driftTooltipBlock,
  isDriftSuspect,
  type DriftDetail,
} from '@/lib/drift'
import type { TrendSeries, TrendWindowOption, TrendWindowValue } from '@/components/charts'
import { useChartResponsive } from '@/components/charts/useChartResponsive'
const LazyCompositionChart = dynamic(
  () => import('@/components/charts').then((m) => m.CompositionChart),
  { ssr: false, loading: () => <div className="h-[320px] animate-pulse rounded-card bg-subtle" aria-label="图表加载中" /> },
)
const LazyTrendChart = dynamic(
  () => import('@/components/charts').then((m) => m.TrendChart),
  { ssr: false, loading: () => <div className="h-[320px] animate-pulse rounded-card bg-subtle" aria-label="图表加载中" /> },
)

import { CarriedBadge } from '@/components/ui/CarriedBadge'
import { actionsWidth } from '@/components/ui/DataTable'
import { tokens } from '@/styles/tokens'
import { MetricCard, MetricCardSkeleton, GradeLegend } from '@/components/ui/MetricCard'
import { DomainStrip } from '@/components/metrics/DomainStrip'
import { EventList } from '@/components/metrics/EventList'
import { useMetricStatus } from '@/components/metrics/useMetricStatus'
import { MetaBadge, degradationLabel } from '@/components/ui/MetaBadge'
import { PieChart, LayoutGrid, BarChart3, Wallet, GitBranch, Tag } from 'lucide-react'
import { PageContainer } from '@/components/layout/PageContainer'
import { PageHeader } from '@/components/layout/PageHeader'
import {
  Card, DirectionText, EmptyState, ErrorState, InlineError, KpiCard, LoadingState, Money,
} from '@/components/ui'
// 带符号文本（+¥… / −…%）与 DirectionText 同源，避免符号逻辑在页面抄第二份
import { formatDelta } from '@/components/ui/DirectionText'
import { Modal, Input, message } from 'antd'
import PeriodPicker from '@/app/alerts/components/PeriodPicker'
import type { ChartType, MetricStatusResponse, PeriodType, ProportionDimension, TrendPoint } from '@/types'

// 趋势行：net_worth 维度带沿用事实（carried_*）；其余维度没有这些字段
type TrendRow = {
  period: string
  total_amount: string
  /** 族口径下本项自身当日值（与 total_amount 不同才画淡线）。 */
  self_amount?: string
  /** 该点计入项中当前值来自更早估值日的项数（0＝全为当日实测）。 */
  carried_items?: number
  /** 这些项里最久的已沿用天数。 */
  carried_max_days?: number
  date?: string
  // 身份漂移诊断（additive；旧后端不返回 → 界面与改造前一致）
  added?: TrendPoint['added']
  vanished?: TrendPoint['vanished']
  drift_suspect?: boolean
  drift_amount?: string
}

type ComparisonRow = {
  period: string
  change_amount: string
  carried_items?: number
  carried_max_days?: number
}

const TABS = [
  { key: 'trend', label: '净资产趋势' },
  { key: 'proportion', label: '占比分析' },
  { key: 'appreciation', label: '升值' },
]

const TREND_DIMENSIONS = [
  { key: 'net_worth', label: '净资产', icon: Wallet },
  { key: 'liquidity', label: '流动性', icon: BarChart3 },
  { key: 'asset_class', label: '资产类别', icon: PieChart },
  { key: 'asset_name', label: '资产名称', icon: LayoutGrid },
  { key: 'institution', label: '机构', icon: Wallet },
]

/** 分项趋势口径切换（spec §2）：族合计 / 仅本项。 */
const FAMILY_SCOPE_OPTIONS: { key: 'family' | 'self'; label: string }[] = [
  { key: 'family', label: '含前身后继' },
  { key: 'self', label: '仅本项' },
]

const PROPORTION_DIMENSIONS: Array<{ key: ProportionDimension; label: string; icon: typeof PieChart }> = [
  { key: 'liquidity', label: '流动性', icon: BarChart3 },
  { key: 'asset_class', label: '资产类别', icon: PieChart },
  { key: 'asset_name', label: '资产名称', icon: LayoutGrid },
  { key: 'institution', label: '机构', icon: Wallet },
  // 标签挂项且可多值：同一资产可挂多个标签（ADR-0019 D7），因此是重叠口径而非互斥分桶（spec §2 D8）
  { key: 'tag', label: '标签', icon: Tag },
]

/**
 * 标签维度的多值口径提示（spec §2 D8 / D16 + 票 08 补充约束）：一笔金额会同时计入它的每个标签，
 * 各标签占比之和可超 100%，各标签之和也可能高于按项并集。除本句外，标签维度不标注任何互斥合计口径。
 */
const TAG_OVERLAP_NOTE =
  '标签可重叠：同一资产可挂多个标签，各标签占比之和可超 100%，各标签之和可能高于按项并集，不是互斥分桶。'

function SelectRow({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-center tablet:gap-4">
      <span className="shrink-0 text-meta font-medium text-muted">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="min-h-[44px] w-full min-w-0 rounded-control border border-line-strong bg-surface px-3 text-body text-ink tablet:w-auto tablet:min-w-[220px]"
      >
        <option value="">请选择</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </div>
  )
}

const CHART_TYPES: { key: ChartType; label: string }[] = [
  { key: 'pie', label: '饼图' },
  { key: 'donut', label: '环形图' },
  { key: 'rose', label: '玫瑰图' },
  { key: 'treemap', label: '树图' },
  { key: 'sunburst', label: '旭日图' },
]

/** 趋势窗口档位：保留原「近 2 年」默认（月24/季8/年2/周104/日730），另给更短档。 */
const TREND_WINDOW_OPTIONS: Record<string, TrendWindowOption[]> = {
  month: [
    { value: 12, label: '近12期' },
    { value: 24, label: '近24期' },
    { value: 'all', label: '全部' },
  ],
  quarter: [
    { value: 4, label: '近4期' },
    { value: 8, label: '近8期' },
    { value: 'all', label: '全部' },
  ],
  year: [
    { value: 2, label: '近2期' },
    { value: 5, label: '近5期' },
    { value: 'all', label: '全部' },
  ],
  week: [
    { value: 26, label: '近26期' },
    { value: 52, label: '近52期' },
    { value: 104, label: '近104期' },
    { value: 'all', label: '全部' },
  ],
  day: [
    { value: 90, label: '近90期' },
    { value: 365, label: '近365期' },
    { value: 730, label: '近730期' },
    { value: 'all', label: '全部' },
  ],
}

const TREND_DEFAULT_WINDOW: Record<string, number> = {
  month: 24,
  quarter: 8,
  year: 2,
  week: 104,
  day: 730,
}

function trendWindowOptions(periodType: string): TrendWindowOption[] {
  return TREND_WINDOW_OPTIONS[periodType] ?? TREND_WINDOW_OPTIONS.month
}

function defaultTrendWindow(periodType: string): number {
  return TREND_DEFAULT_WINDOW[periodType] ?? TREND_DEFAULT_WINDOW.month
}

/**
 * 「同日新增/消失」角标：与 `CarriedBadge` 同一套小注记语言（琥珀细点 + 短词），
 * 只在后端返回 `drift_suspect` 时出现；菱形点与折线上的角标同形。
 */
function DriftBadge({ title }: { title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-amber-50 px-2 py-0.5 text-meta font-medium leading-4 text-amber-700"
    >
      <span className="h-1.5 w-1.5 rotate-45 rounded-[1px] bg-amber-400" aria-hidden />
      {DRIFT_LABEL}
    </span>
  )
}

/**
 * 逐期明细（页内展开，不浮在画布上）：移动端与键盘用户的明细入口。
 * 只陈述观测，不做归因；金额与行序与 tooltip 同源（`lib/drift`）。
 */
function DriftDetailPanel({
  period,
  detail,
  driftAmount,
  onClose,
}: {
  period: string
  detail: DriftDetail | null
  driftAmount?: string
  onClose: () => void
}) {
  // drift_suspect 但后端没返回任何新增/消失项：这一态不可达（后端先丢零再算漂移），
  // 与其渲染一块空明细和误导文案，不如整块不出现。
  if (!detail || detail.total === 0) return null
  return (
    <section
      id="drift-detail"
      aria-label={`${formatDate(period)} 同日新增/消失明细`}
      className="mt-3 rounded-control border border-line bg-subtle px-3 py-3 tablet:px-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h4 className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-body font-semibold text-ink">
          同日新增/消失
          <span className="pf-num text-meta font-normal text-muted">{formatDate(period)}</span>
        </h4>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex min-h-[44px] shrink-0 items-center rounded-control px-2 text-meta font-medium text-link"
        >
          收起
        </button>
      </div>

      <p className="text-meta text-secondary">{driftObservationText(detail)}</p>

      {detail.entries.length > 0 && (
        <ul className="mt-2 divide-y divide-line">
          {detail.entries.map((entry) => (
            <li
              key={entry.key}
              className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 py-1.5"
            >
              <span className="min-w-0 flex-1 text-body text-secondary">
                <span
                  className={`mr-2 inline-block rounded px-1 align-baseline text-[11px] font-medium leading-4 ${
                    entry.kind === 'added' ? 'bg-surface text-brand-700' : 'bg-surface text-muted'
                  }`}
                >
                  {entry.tag}
                </span>
                {entry.name}
                {entry.note && (
                  <span className="ml-2 whitespace-nowrap text-meta text-muted">{entry.note}</span>
                )}
              </span>
              <Money value={entry.amount} unit="cny" size="xs" />
            </li>
          ))}
        </ul>
      )}

      <p className="mt-2 text-meta text-muted">
        {`共 ${detail.total} 项${detail.truncated ? `，按该期金额列前 ${detail.entries.length} 项` : ''}。`}
        {driftAmount ? `涉及金额 ${formatValue(driftAmount, 'cny')}（新增与消失合计取大者）。` : null}
      </p>
    </section>
  )
}

/** 结构图四态的展示分派（不改数据/口径）。 */
function compositionQueryState(
  isError: boolean,
  isLoading: boolean,
  count: number,
): 'error' | 'loading' | 'ready' | 'empty' {
  if (isError) return 'error'
  if (isLoading) return 'loading'
  return count > 0 ? 'ready' : 'empty'
}

function TrendTab() {
  const { windowWidth } = useChartResponsive()
  const [periodType, setPeriodType] = useState('month')
  const [dimension, setDimension] = useState('net_worth')
  const [selectedItem, setSelectedItem] = useState('')
  const [selectedClass, setSelectedClass] = useState<number | ''>('')
  const [selectedLiquidity, setSelectedLiquidity] = useState<number | ''>('')
  const [selectedInstitution, setSelectedInstitution] = useState<number | ''>('')
  const [windowValue, setWindowValue] = useState<TrendWindowValue>(defaultTrendWindow('month'))
  // 分项趋势口径：family＝族合计（含前身/后继），self＝仅本项
  const [familyScope, setFamilyScope] = useState<'family' | 'self'>('family')
  // 页内展开的漂移期（点折线或点下方日期 chip 都会切换它）
  const [driftPeriod, setDriftPeriod] = useState<string | null>(null)

  // 账期切换时回到该账期的默认近 2 年窗口
  useEffect(() => {
    setWindowValue(defaultTrendWindow(periodType))
  }, [periodType])

  // 换账期/换维度后，旧账期标签可能已不存在：收起明细，避免读出别的维度的同名期
  useEffect(() => {
    setDriftPeriod(null)
  }, [periodType, dimension])

  // 换资产项 / 账期：口径回到「含前身后继」（spec §2 缺省）
  useEffect(() => {
    setFamilyScope('family')
  }, [selectedItem, periodType])

  // 根据维度选择获取数据
  const {
    data: mixedChartData, isLoading: isLoadingTrend, isError: isErrorTrend, refetch: refetchTrend,
  } = useQuery({
    queryKey: ['mixedChart', periodType],
    queryFn: () => api.getMixedChart({ period_type: periodType }),
    enabled: dimension === 'net_worth',
  })

  // 名称 → 资产项 id：只有拿到 item id 才能用族口径（无 id 时整段回落到旧调用）
  const { data: itemDirectory } = useQuery({
    queryKey: ['assetItems', true],
    queryFn: () => api.listAssetItems(true),
    enabled: dimension === 'asset_name' && !!selectedItem,
    staleTime: 30_000,
  })
  const selectedItemId = useMemo(
    () => (dimension === 'asset_name' && selectedItem
      ? resolveItemIdByName(itemDirectory?.items ?? [], selectedItem)
      : null),
    [dimension, selectedItem, itemDirectory],
  )

  // 分项趋势·身份族口径（spec §1.3）：族合计 + 本项 + 当日构成
  const {
    data: familyData, isLoading: isLoadingFamily, isError: isErrorFamily, refetch: refetchFamily,
  } = useQuery({
    queryKey: ['byItemFamily', selectedItemId, periodType],
    queryFn: () => api.getByItemFamily({ item_id: selectedItemId!, period_type: periodType }),
    enabled: dimension === 'asset_name' && selectedItemId !== null,
    retry: false,
  })

  const {
    data: itemData, isLoading: isLoadingItem, isError: isErrorItem, refetch: refetchItem,
  } = useQuery({
    queryKey: ['byItem', selectedItem, periodType],
    queryFn: () => api.getByItem({ asset_name: selectedItem, period_type: periodType }),
    // 身份族模式可用时优先族口径；无 item id 或族模式报错才回落到旧调用（spec §1.3）
    enabled: dimension === 'asset_name'
      && !!selectedItem
      && (selectedItemId === null || isErrorFamily),
  })

  const {
    data: classData, isLoading: isLoadingClass, isError: isErrorClass, refetch: refetchClass,
  } = useQuery({
    queryKey: ['byClass', selectedClass, periodType],
    queryFn: () => api.getByClass({ asset_class_id: selectedClass as number, period_type: periodType }),
    enabled: dimension === 'asset_class' && selectedClass !== '',
  })

  const {
    data: liquidityData, isLoading: isLoadingLiquidity, isError: isErrorLiquidity, refetch: refetchLiquidity,
  } = useQuery({
    queryKey: ['byLiquidity', selectedLiquidity, periodType],
    queryFn: () => api.getByLiquidity({ days: selectedLiquidity as number, period_type: periodType }),
    enabled: dimension === 'liquidity' && selectedLiquidity !== '',
  })

  const {
    data: institutionData, isLoading: isLoadingInstitution, isError: isErrorInstitution, refetch: refetchInstitution,
  } = useQuery({
    queryKey: ['byInstitution', selectedInstitution, periodType],
    queryFn: () => api.getByInstitution({ institution_id: selectedInstitution as number, period_type: periodType }),
    enabled: dimension === 'institution' && selectedInstitution !== '',
  })

  const { data: assetNames } = useQuery({
    queryKey: ['assetNames'],
    queryFn: api.getAssetNames,
    enabled: dimension === 'asset_name',
  })

  const { data: assetClassesData } = useQuery({
    queryKey: ['assetClasses'],
    queryFn: api.getAssetClasses,
    enabled: dimension === 'asset_class',
  })
  const assetClasses = useMemo(() => assetClassesData?.items ?? [], [assetClassesData])

  const { data: liquidityLevelsData } = useQuery({
    queryKey: ['liquidityLevels'],
    queryFn: api.getLiquidityLevels,
    enabled: dimension === 'liquidity',
    staleTime: Infinity,
  })
  const liquidityLevels = useMemo(() => liquidityLevelsData?.items ?? [], [liquidityLevelsData])

  const { data: institutionsData } = useQuery({
    queryKey: ['institutions'],
    queryFn: api.getInstitutions,
    enabled: dimension === 'institution',
  })
  const institutions = useMemo(() => institutionsData?.items ?? [], [institutionsData])

  const assetNameLoading = selectedItemId === null
    ? isLoadingItem
    : isErrorFamily ? isLoadingItem : isLoadingFamily
  const assetNameError = selectedItemId === null
    ? isErrorItem
    : isErrorFamily ? isErrorItem : isErrorFamily

  const isLoading =
    dimension === 'net_worth' ? isLoadingTrend :
    dimension === 'asset_name' ? assetNameLoading :
    dimension === 'asset_class' ? isLoadingClass :
    dimension === 'liquidity' ? isLoadingLiquidity :
    dimension === 'institution' ? isLoadingInstitution : false
  const isError =
    dimension === 'net_worth' ? isErrorTrend :
    dimension === 'asset_name' ? assetNameError :
    dimension === 'asset_class' ? isErrorClass :
    dimension === 'liquidity' ? isErrorLiquidity :
    dimension === 'institution' ? isErrorInstitution : false

  const getChartData = (): { trend: TrendRow[]; comparison: ComparisonRow[]; name: string } | null => {
    if (dimension === 'net_worth' && mixedChartData) {
      return {
        trend: mixedChartData.trend,
        comparison: mixedChartData.comparison,
        name: '净资产',
      }
    }
    if (dimension === 'asset_name' && familyData) {
      const useFamily = familyScope === 'family'
      const totals = familyData.points.map((point) => (useFamily ? point.total : point.self))
      return {
        trend: familyData.points.map((point, index) => ({
          period: point.period,
          date: point.date,
          total_amount: totals[index],
          ...(useFamily ? { self_amount: point.self } : {}),
        })),
        comparison: familyData.points.map((point, index) => {
          if (index === 0) return { period: point.period, change_amount: '0' }
          const change = Number(totals[index]) - Number(totals[index - 1])
          return { period: point.period, change_amount: change.toFixed(2) }
        }),
        name: selectedItem,
      }
    }
    if (dimension === 'asset_name' && itemData) {
      return {
        trend: itemData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: itemData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: selectedItem,
      }
    }
    if (dimension === 'asset_class' && classData) {
      return {
        trend: classData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: classData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: assetClasses.find((node) => node.id === selectedClass)?.name || '资产类别',
      }
    }
    if (dimension === 'liquidity' && liquidityData) {
      return {
        trend: liquidityData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: liquidityData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: liquidityLevels.find((level) => level.days === selectedLiquidity)?.label || '流动性档位',
      }
    }
    if (dimension === 'institution' && institutionData) {
      return {
        trend: institutionData.map((d: { period: string; amount: string }) => ({ period: d.period, total_amount: d.amount })),
        comparison: institutionData.map((d: { period: string; change_amount: string | null }) => ({ period: d.period, change_amount: d.change_amount || '0' })),
        name: institutions.find((node) => node.id === selectedInstitution)?.name || '机构',
      }
    }
    return null
  }

  const chartData = getChartData()
  // 身份族（spec §1.3 / §2）：family_size > 1 时默认族合计，本项单独成淡线。
  const familySize = familyData?.item.family_size ?? 1
  const familyActive = dimension === 'asset_name' && !!familyData && familySize > 1
  const familyPoints = familyData?.points ?? []
  const showSelfLine = familyActive
    && familyScope === 'family'
    && familyPoints.some((point) => Math.abs(Number(point.self) - Number(point.total)) >= 0.005)
  const familyBadge = familyActive
    ? (familyScope === 'family'
      ? {
          code: 'LINEAGE_FAMILY',
          label: '含前身/后继',
          detail: `身份族 ${familySize} 项：成员按日取最近值求和，切换日前后不重复计入`,
          tone: 'info' as const,
        }
      : {
          code: 'LINEAGE_SELF_ONLY',
          label: '仅本项',
          detail: '只显示该资产项自身的读数；前身/后继未计入',
          tone: 'muted' as const,
        })
    : null
  const familyToggle = familyActive ? (
    <div
      role="group"
      aria-label="趋势口径"
      className="flex items-center gap-0.5 rounded-lg bg-slate-100 p-0.5"
    >
      {FAMILY_SCOPE_OPTIONS.map((option) => (
        <button
          key={option.key}
          type="button"
          aria-pressed={familyScope === option.key}
          onClick={() => setFamilyScope(option.key)}
          className={`inline-flex min-h-[44px] items-center rounded-md px-2 text-[11px] font-medium leading-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:ring-offset-1 motion-reduce:transition-none ${
            familyScope === option.key
              ? 'bg-white text-brand-700 shadow-sm'
              : 'text-slate-500 hover:text-slate-700'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  ) : null
  // 后端探测位（spec §3）：`meta.drift_enabled` 缺省/true 才认逐点漂移字段；
  // 显式 false → 整块提示关闭，界面与改造前一致。
  const driftEnabled = mixedChartData?.meta?.drift_enabled !== false
  const dimensionLabel = TREND_DIMENSIONS.find((d) => d.key === dimension)?.label ?? dimension
  const categories = chartData?.trend.map((t) => t.period) ?? []
  const windowStart = windowValue === 'all'
    ? 0
    : Math.max(0, categories.length - windowValue)
  const visibleTrend = useMemo(
    () => (chartData ? chartData.trend.slice(windowStart) : []),
    [chartData, windowStart],
  )
  const windowOptions = useMemo(() => trendWindowOptions(periodType), [periodType])

  // 统计卡只跟窗口；明细表始终全量
  const stats = useMemo(() => {
    if (visibleTrend.length === 0) return null

    const amounts = visibleTrend.map((t) => parseFloat(t.total_amount))
    const maxValue = Math.max(...amounts)
    const minValue = Math.min(...amounts)
    const lastValue = amounts[amounts.length - 1]
    const maxIndex = amounts.indexOf(maxValue)
    const minIndex = amounts.indexOf(minValue)
    const maxPoint = visibleTrend[maxIndex]
    const minPoint = visibleTrend[minIndex]
    const maxCarried = hasCarried(maxPoint)
    const minCarried = hasCarried(minPoint)
    // 极值点本身可能是漂移点（同日新增/消失抬高了净值）：角标与「含沿用值」并列提示
    const maxDrift = driftEnabled && isDriftSuspect(maxPoint)
    const minDrift = driftEnabled && isDriftSuspect(minPoint)

    const changeFromMax = lastValue - maxValue
    const changeFromMaxPercent = maxValue !== 0 ? (changeFromMax / maxValue) * 100 : 0
    const changeFromMin = lastValue - minValue
    const changeFromMinPercent = minValue !== 0 ? (changeFromMin / minValue) * 100 : 0

    return {
      maxValue,
      minValue,
      lastValue,
      changeFromMax,
      changeFromMaxPercent,
      changeFromMin,
      changeFromMinPercent,
      maxIndex,
      minIndex,
      maxCarried,
      minCarried,
      maxDrift,
      minDrift,
      maxDetail: maxPoint ? carriedDetailText({ ...maxPoint, date: maxPoint.date ?? maxPoint.period }) ?? undefined : undefined,
      minDetail: minPoint ? carriedDetailText({ ...minPoint, date: minPoint.date ?? minPoint.period }) ?? undefined : undefined,
      maxDriftTitle: driftBadgeTitle(maxPoint ? driftDetail(maxPoint) : null),
      minDriftTitle: driftBadgeTitle(minPoint ? driftDetail(minPoint) : null),
    }
  }, [visibleTrend, driftEnabled])

  // 参考线标签固定在画布内，窄屏只用短标签；「含沿用值」由帧头徽标/统计卡承载
  const refLines = stats
    ? [
        { value: stats.maxValue, label: '最高值' },
        { value: stats.minValue, label: '最低值' },
      ]
    : []

  const carriedPoint = visibleTrend.find(hasCarried)
  // 漂移提示只跟窗口（与「含沿用值」徽标同源；数据本身不改）
  const visibleDriftPoints = driftEnabled ? visibleTrend.filter((t) => isDriftSuspect(t)) : []
  const firstDrift = visibleDriftPoints[0]
  const firstDriftDetail = firstDrift ? driftDetail(firstDrift) : null
  const driftBadge = firstDrift && firstDriftDetail
    ? {
        code: 'IDENTITY_DRIFT_SUSPECT',
        label: DRIFT_LABEL,
        detail:
          visibleDriftPoints.length > 1
            ? `${formatDate(firstDrift.period)} 等 ${visibleDriftPoints.length} 期存在同日新增/消失的资产项；点选下方日期看明细`
            : `${driftCountsText(firstDriftDetail)}；${DRIFT_OBSERVATION}`,
        tone: 'warn' as const,
      }
    : null
  const badges = [
    ...(carriedPoint
      ? [{
          code: 'DATA_MISSING_CARRY_FORWARD',
          label: degradationLabel('DATA_MISSING_CARRY_FORWARD'),
          detail: `${carriedDetailText({ ...carriedPoint, date: carriedPoint.date ?? carriedPoint.period }) ?? '含沿用值'}；空心点/斜纹标注`,
          tone: 'warn' as const,
        }]
      : []),
    ...(driftBadge ? [driftBadge] : []),
    ...(familyBadge ? [familyBadge] : []),
  ]

  const carriedFooters = chartData?.trend.map((t) => {
    if (!hasCarried(t)) return null
    const detail = carriedDetailText({ ...t, date: t.date ?? t.period }) ?? '含沿用值'
    return detail === '含沿用值' ? detail : `含沿用值 · ${detail}`
  }) ?? []
  const tooltipFooters = carriedFooters
  const denseChart = windowWidth < 560
  // 当日构成（族口径）：成员＋金额。窄屏 tooltip 不展示明细行，改把构成写进观察句（限宽换行）。
  const familyTooltipDetails = familyActive && familyScope === 'family'
    ? familyPoints.map((point) => {
        const denseMembers = point.members.slice(0, 4)
        const composition = denseMembers
          .map((member) => `${member.name} ${formatCurrency(member.amount)}`)
          .join(' · ')
        const suffix = point.members.length > denseMembers.length
          ? ` · 等 ${point.members.length} 项`
          : ''
        return {
          heading: '当日构成',
          note: denseChart
            ? `族合计 ${formatCurrency(point.total)} · ${composition}${suffix}`
            : `族合计 ${formatCurrency(point.total)} · ${point.members.length} 项`,
          rows: point.members.map((member) => ({
            label: member.name,
            value: formatCurrency(member.amount),
            note: member.item_id === selectedItemId ? '本项' : undefined,
          })),
        }
      })
    : null
  // 漂移明细：全量长度，preset 随窗口切；窄屏只保留标题＋观察句（明细走页内展开）
  const driftTooltipDetails = driftEnabled
    ? chartData?.trend.map((t) => {
        const detail = driftDetail(t)
        return detail ? driftTooltipBlock(detail) : null
      }) ?? []
    : []
  const tooltipDetails = familyTooltipDetails ?? driftTooltipDetails
  const selectedDriftPoint = driftPeriod
    ? chartData?.trend.find((t) => t.period === driftPeriod) ?? null
    : null
  const handleDatumClick = useCallback((datum: { category: string }) => {
    setDriftPeriod((current) => (current === datum.category ? null : datum.category))
  }, [])
  const trendSeries: TrendSeries[] = chartData
    ? [
        {
          key: 'amount',
          label: familyActive && familyScope === 'family' ? '族合计' : chartData.name,
          kind: 'line',
          color: 'brand',
          area: true,
          emphasis: true,
          unit: 'cny',
          values: chartData.trend.map((t) => Number(t.total_amount)),
          provenance: chartData.trend.map((t) => (hasCarried(t) ? 'carried' : 'measured')),
          // 漂移点：折线上一个空心小菱形（不改线型、不新增系列）
          marks: chartData.trend.map((t) => driftEnabled && isDriftSuspect(t)),
        },
        ...(showSelfLine
          ? [{
              key: 'self',
              label: '本项',
              kind: 'line' as const,
              color: 'muted' as const,
              dashed: true,
              unit: 'cny' as const,
              values: chartData.trend.map((t) => Number(t.self_amount ?? 0)),
            }]
          : []),
        {
          key: 'change',
          label: '变化额',
          kind: 'bar',
          axis: 'right',
          color: 'direction',
          unit: 'cny',
          values: chartData.comparison.map((c) => Number(c.change_amount)),
          provenance: chartData.comparison.map((c) => (hasCarried(c) ? 'carried' : 'measured')),
        },
      ]
    : []

  // 变化额/变化% 并入趋势图数据表（spec D3）：算式与被删除的旧明细卡逐字一致，
  // 按全量账期对齐（数据表始终全量，窗口只切折线）。
  const trendExtraColumns = chartData
    ? [
        {
          label: '较上期变化额',
          values: chartData.trend.map((_, index) => {
            const change = chartData.comparison[index]?.change_amount || '0'
            const n = Number(change)
            return Number.isFinite(n) ? formatDelta(n, 'cny') : '—'
          }),
        },
        {
          label: '较上期变化%',
          values: chartData.trend.map((item, index) => {
            const changeAmount = chartData.comparison[index]?.change_amount || '0'
            const prevAmount = Number(item.total_amount) - Number(changeAmount)
            // prev === 0 落 0（与旧卡一致，不落「—」）
            const changePercent = prevAmount !== 0
              ? (Number(changeAmount) / Math.abs(prevAmount)) * 100
              : 0
            return Number.isFinite(changePercent) ? formatDelta(changePercent, 'percent') : '—'
          }),
        },
      ]
    : undefined

  const periodLabel = PERIOD_TYPE_OPTIONS.find((t) => t.value === periodType)?.label ?? periodType
  const title = chartData
    ? `${chartData.name}趋势`
    : dimension === 'net_worth'
      ? '净资产趋势'
      : `${dimensionLabel}趋势`
  const subtitle = `${periodLabel} · 单位：元`
  const asOf = chartData?.trend[chartData.trend.length - 1]?.date ?? null
  const trendState: 'error' | 'loading' | 'ready' | 'empty' = isError
    ? 'error'
    : isLoading
      ? 'loading'
      : chartData && categories.length > 0
        ? 'ready'
        : 'empty'

  const hasSelection =
    dimension === 'net_worth' ||
    (dimension === 'asset_name' && !!selectedItem) ||
    (dimension === 'asset_class' && selectedClass !== '') ||
    (dimension === 'liquidity' && selectedLiquidity !== '') ||
    (dimension === 'institution' && selectedInstitution !== '')
  const selectionHint = dimension === 'asset_name'
    ? '请选择具体资产后查看趋势'
    : dimension === 'asset_class'
      ? '请选择资产类别后查看趋势'
      : dimension === 'liquidity'
        ? '请选择流动性档位后查看趋势'
        : dimension === 'institution'
          ? '请选择机构后查看趋势'
          : '当前账期没有可用快照；可切换账期'
  const emptyHint = !hasSelection
    ? selectionHint
    : dimension === 'net_worth'
      ? '当前账期没有可用快照；可切换账期'
      : '当前选择在所选账期没有可用数据；可切换账期'
  const emptyState = {
    title: hasSelection ? '暂无趋势数据' : '请选择具体维度',
    hint: emptyHint,
  }

  const retry = isError
    ? () => {
        void refetchTrend()
        void refetchItem()
        void refetchFamily()
        void refetchClass()
        void refetchLiquidity()
        void refetchInstitution()
      }
    : undefined

  const chartFootnote = [
    visibleDriftPoints.length > 0 ? '◇＝同日新增/消失（点选下方日期看明细）' : null,
    familyActive && familyScope === 'family'
      ? '族合计＝前身/后继与本项按日取最近值求和（切换日不双计）；淡线＝本项'
      : null,
  ].filter(Boolean).join(' · ') || undefined

  return (
    <div className="space-y-6">
      <Card padding="sm" className="space-y-4">
        <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-center tablet:gap-4">
          <span className="shrink-0 text-meta font-medium text-muted">分析维度</span>
          <div className="flex flex-wrap gap-2">
            {TREND_DIMENSIONS.map((d) => (
              <button
                key={d.key}
                type="button"
                aria-pressed={dimension === d.key}
                onClick={() => {
                  setDimension(d.key)
                  setSelectedItem('')
                  setSelectedClass('')
                  setSelectedLiquidity('')
                  setSelectedInstitution('')
                }}
                className={`inline-flex min-h-[44px] items-center gap-2 rounded-control px-3 text-body font-medium transition-colors ${
                  dimension === d.key ? 'bg-brand-600 text-white' : 'bg-subtle text-secondary'
                }`}
              >
                <d.icon size={16} aria-hidden />
                {d.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-center tablet:gap-4">
          <span className="shrink-0 text-meta font-medium text-muted">账期类型</span>
          <div className="flex flex-wrap gap-1 rounded-control bg-subtle p-1">
            {PERIOD_TYPE_OPTIONS.map((t) => (
              <button
                key={t.value}
                type="button"
                aria-pressed={periodType === t.value}
                onClick={() => {
                  setPeriodType(t.value)
                  setWindowValue(defaultTrendWindow(t.value))
                }}
                className={`min-w-[44px] min-h-[44px] rounded-control px-3 text-body transition-colors ${
                  periodType === t.value ? 'bg-surface font-medium text-brand-700 shadow-card' : 'text-muted'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        {dimension === 'asset_name' && (
          <SelectRow label="选择项目" value={selectedItem} onChange={setSelectedItem}
            options={assetNames?.map((n: string) => ({ value: n, label: n })) ?? []} />
        )}
        {dimension === 'asset_class' && (
          <SelectRow label="选择资产类别" value={String(selectedClass)}
            onChange={(v) => setSelectedClass(v ? Number(v) : '')}
            options={assetClasses.map((node) => ({
              value: String(node.id), label: node.path || `${'  '.repeat(node.level)}${node.name}`,
            }))} />
        )}
        {dimension === 'liquidity' && (
          <SelectRow label="选择流动性档位" value={String(selectedLiquidity)}
            onChange={(v) => setSelectedLiquidity(v ? Number(v) : '')}
            options={liquidityLevels.map((level) => ({ value: String(level.days), label: level.label }))} />
        )}
        {dimension === 'institution' && (
          <SelectRow label="选择机构" value={String(selectedInstitution)}
            onChange={(v) => setSelectedInstitution(v ? Number(v) : '')}
            options={institutions.map((node) => ({ value: String(node.id), label: node.name }))} />
        )}
      </Card>

      {stats && (
        <div className="grid grid-cols-1 gap-2 tablet:grid-cols-2">
          <KpiCard
            label="最高值"
            value={stats.maxValue}
            delta={stats.changeFromMax}
            deltaPercent={stats.changeFromMaxPercent}
            deltaUnit="cny"
            deltaLabel="较最后一期"
            meta={
              stats.maxDrift || stats.maxCarried ? (
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  {stats.maxDrift && <DriftBadge title={stats.maxDriftTitle} />}
                  {stats.maxCarried && <CarriedBadge title={stats.maxDetail} />}
                </span>
              ) : undefined
            }
          />
          <KpiCard
            label="最低值"
            value={stats.minValue}
            delta={stats.changeFromMin}
            deltaPercent={stats.changeFromMinPercent}
            deltaUnit="cny"
            deltaLabel="较最后一期"
            meta={
              stats.minDrift || stats.minCarried ? (
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  {stats.minDrift && <DriftBadge title={stats.minDriftTitle} />}
                  {stats.minCarried && <CarriedBadge title={stats.minDetail} />}
                </span>
              ) : undefined
            }
          />
        </div>
      )}

      <Card padding="md">
        {familyToggle && (
          <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-line pb-2.5">
            <span className="inline-flex items-center gap-1.5 text-meta text-secondary">
              <GitBranch size={14} className="shrink-0 text-muted" aria-hidden />
              身份族口径
              <span className="pf-num text-micro text-muted">共 {familySize} 项</span>
            </span>
            {familyToggle}
          </div>
        )}
        <LazyTrendChart
          key={`${dimension}-${selectedItem}-${selectedItemId ?? 'none'}-${selectedClass}-${selectedLiquidity}-${selectedInstitution}-${periodType}-${familyScope}`}
          title={title}
          subtitle={subtitle}
          asOf={formatDate(asOf)}
          badges={badges}
          height="xl"
          categories={categories}
          series={trendSeries}
          window={{ value: windowValue, onChange: setWindowValue }}
          windowOptions={windowOptions}
          refLines={windowWidth >= 768 ? refLines : []}
          tooltipFooters={tooltipFooters}
          tooltipDetails={tooltipDetails}
          extraTableColumns={trendExtraColumns}
          footnote={chartFootnote}
          onDatumClick={visibleDriftPoints.length > 0 ? handleDatumClick : undefined}
          state={trendState}
          empty={emptyState}
          onRetry={retry}
        />

        {visibleDriftPoints.length > 0 && (
          <div className="mt-4 border-t border-line pt-3">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="inline-flex items-center gap-1.5 text-meta font-medium text-amber-700">
                <span className="h-1.5 w-1.5 rotate-45 rounded-[1px] bg-amber-400" aria-hidden />
                {DRIFT_LABEL}
              </span>
              <span className="text-meta text-muted">以下日期存在同日新增/消失的资产项</span>
            </div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {visibleDriftPoints.map((point) => {
                const open = driftPeriod === point.period
                return (
                  <button
                    key={point.period}
                    type="button"
                    aria-expanded={open}
                    aria-controls={open ? 'drift-detail' : undefined}
                    onClick={() => setDriftPeriod((current) => (current === point.period ? null : point.period))}
                    className={`inline-flex min-h-[44px] items-center rounded-control px-2.5 text-meta font-medium transition-colors ${
                      open ? 'bg-brand-600 text-white' : 'bg-subtle text-secondary'
                    }`}
                  >
                    <span className="pf-num">{formatDate(point.period)}</span>
                  </button>
                )
              })}
            </div>

            {selectedDriftPoint && (
              <DriftDetailPanel
                period={selectedDriftPoint.period}
                detail={driftDetail(selectedDriftPoint)}
                driftAmount={selectedDriftPoint.drift_amount}
                onClose={() => setDriftPeriod(null)}
              />
            )}
          </div>
        )}
      </Card>
    </div>
  )
}

// ===== 三期-0 指标总览条（spec §4；契约 §3）=====
// 状态面缺省 as_of＝最新快照日；字典只用来取显示名（status 项不带 name，不臆造）。
const METRIC_NAME_FALLBACK: Record<string, string> = {
  asset_total: '总资产',
  liability_total: '总负债',
  net_worth: '净资产',
  net_worth_change_pct: '净资产环比',
  data_coverage: '覆盖度',
  appreciation_amount: '升值额',
  appreciation_rate: '升值率',
  twr: '时间加权收益率（TWR）',
  xirr: '货币加权收益率（XIRR）',
  liability_ratio: '负债占比',
  allocation_deviation: '配置偏离',
  goal_progress: '目标达成度',
  goal_gap: '目标缺口',
  goal_required_annualized: '目标所需年化',
}

// 本页可定位的章节（drill.target 的 ?section=）→ tab ＋ 落点元素。
// allocation 由 handleMetricDrill 转到 /allocation；coverage 落到指标总览条。
const ANALYSIS_SECTIONS: Record<string, { tab: 'trend' | 'proportion' | 'appreciation'; anchor?: string }> = {
  trend: { tab: 'trend', anchor: 'analysis-chart' },
  appreciation: { tab: 'appreciation', anchor: 'analysis-chart' },
  returns: { tab: 'appreciation', anchor: 'section-returns' },
  structure: { tab: 'proportion', anchor: 'analysis-chart' },
  coverage: { tab: 'trend', anchor: 'metrics' },
}

function motionBehavior(): ScrollBehavior {
  if (typeof window === 'undefined') return 'auto'
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
}

function scrollToAnchor(id: string, behavior: ScrollBehavior, tries = 16) {
  // tab 内容是异步区块（如双口径收益），可能还在加载：帧内重试至多约 1.3s，
  // 依旧找不到就静默放弃——定位不到时仅停留本页，不报错（drill 契约行为）。
  requestAnimationFrame(() => {
    const el = document.getElementById(id)
    if (el) {
      el.scrollIntoView({ behavior, block: 'start' })
      return
    }
    if (tries > 0) window.setTimeout(() => scrollToAnchor(id, behavior, tries - 1), 80)
  })
}

function MetricsStrip({
  onDrill,
  status,
  isLoading,
  isError,
  refetch,
}: {
  onDrill: (target: string) => void
  status: MetricStatusResponse | undefined
  isLoading: boolean
  isError: boolean
  refetch: () => void
}) {
  // 字典静态（注册表＝代码真源）→ 长缓存；状态面由页面统一取，域灯/异动共用同一份。
  const { data: dict } = useQuery({
    queryKey: ['metricsDictionary'],
    queryFn: api.getMetrics,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  })
  const { data: goalsData } = useQuery({
    queryKey: ['goals', false],
    queryFn: () => api.getGoals(),
    staleTime: 60_000,
  })

  const names = useMemo(() => {
    const map = new Map<string, string>()
    dict?.items?.forEach((d) => map.set(d.id, d.name))
    return map
  }, [dict])

  // 顶层 meta 只补 NO_DATA（其余降级逐指标 gates 已各自陈述，条头不重复刷屏）
  const noData = status?.meta?.degradations?.find((d) => d.code === 'NO_DATA')
  const [metricsExpanded, setMetricsExpanded] = useState(false)
  const metricItems = useMemo(() => status?.items ?? [], [status])
  const latestSnapshot = status?.meta?.snapshot_date ?? null
  const waitingGoal = useMemo(() => {
    if (metricItems.some((item) => item.id.startsWith('goal_'))) return null
    const goal = (goalsData?.items ?? []).find((item) => item.status === 'active' && item.active_version?.effective_from)
    if (!goal?.active_version?.effective_from) return null
    return !latestSnapshot || goal.active_version.effective_from > latestSnapshot ? goal : null
  }, [goalsData, latestSnapshot, metricItems])

  return (
    <Card as="section" padding="none" aria-label="指标总览" className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 pb-3 pt-4 sm:px-5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 className="text-h3 font-semibold text-ink">指标总览</h2>
          {status?.meta?.snapshot_date && (
            <span className="text-meta text-muted">as-of {formatDate(status.meta.snapshot_date)}</span>
          )}
          {noData && (
            <MetaBadge tone="muted" label={degradationLabel(noData.code)} detail={noData.message} />
          )}
        </div>
        <GradeLegend />
      </div>

      {waitingGoal && !isLoading && status && (
        <div className="border-t border-line bg-status-warn-surface/50 px-4 py-2 text-meta text-status-warn-text sm:px-5">
          等待首个 ≥ 生效日的快照：目标「{waitingGoal.name}」自 {formatDate(waitingGoal.active_version?.effective_from)} 生效，最新快照 {latestSnapshot ? formatDate(latestSnapshot) : '暂无'}。
        </div>
      )}

      {metricItems.length > 4 && (
        <div className="px-4 pb-3 tablet:hidden">
          <button
            type="button"
            onClick={() => setMetricsExpanded((open) => !open)}
            aria-expanded={metricsExpanded}
            className="inline-flex min-w-[44px] min-h-[44px] items-center rounded-control px-2 text-meta font-medium text-link"
          >
            {metricsExpanded ? '收起指标' : `查看全部（${metricItems.length}）`}
          </button>
        </div>
      )}

      {isError ? (
        <div className="px-4 pb-4 sm:px-5">
          <InlineError
            action={
              <button
                type="button"
                onClick={() => refetch()}
                className="min-h-[44px] px-2 text-body-strong font-medium text-status-critical-text"
              >
                重试
              </button>
            }
          >
            指标总览加载失败，其余分析不受影响。
          </InlineError>
        </div>
      ) : isLoading || !status ? (
        <div className="-mb-px -ml-px grid grid-cols-2 border-b border-t border-line sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-6">
          <MetricCardSkeleton count={6} />
        </div>
      ) : metricItems.length === 0 ? (
        <div className="border-t border-line p-4 sm:p-5">
          <EmptyState title="暂无指标" description="状态面没有可展示的指标。" compact />
        </div>
      ) : (
        <div className="-mb-px -ml-px grid grid-cols-2 border-b border-t border-line sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-6">
          {metricItems.map((item, index) => (
            <MetricCard
              key={item.id}
              item={item}
              name={names.get(item.id) ?? METRIC_NAME_FALLBACK[item.id] ?? item.id}
              emphasis={item.id === 'net_worth'}
              onDrill={item.drill ? () => onDrill(item.drill!.target) : undefined}
              className={index >= 4 && !metricsExpanded ? 'hidden tablet:flex' : undefined}
            />
          ))}
        </div>
      )}
    </Card>
  )
}

export default function AnalysisPage() {
  const [tab, setTab] = useState('trend')
  const router = useRouter()
  const metricStatusQuery = useMetricStatus()

  const applySection = useCallback((section: string | null, behavior: ScrollBehavior) => {
    if (!section) return
    const dest = ANALYSIS_SECTIONS[section]
    if (!dest) return
    setTab(dest.tab)
    if (dest.anchor) scrollToAnchor(dest.anchor, behavior)
  }, [])

  // 深链 / 刷新（/analysis?section=…）也落到对应区块；定位不到就停在默认 tab
  useEffect(() => {
    const section = new URLSearchParams(window.location.search).get('section')
    if (section) applySection(section, 'auto')
  }, [applySection])

  const handleMetricDrill = useCallback((target: string) => {
    const [path, search = ''] = target.split('?')
    const section = new URLSearchParams(search).get('section')
    // C2 的契约 drill 是 /analysis?section=allocation，实际配置偏离页在 /allocation
    if (path === '/analysis' && section === 'allocation') {
      router.push('/allocation')
      return
    }
    if (path === window.location.pathname) {
      applySection(section, motionBehavior())
      return
    }
    // 跨页交给目标页按 ?section= 定位；定位不到就只跳页（不报错）
    router.push(target)
  }, [applySection, router])

  return (
    <PageContainer width="wide" className="space-y-6">
      <PageHeader title="数据分析" description="多维度资产变化趋势分析" />

      {/* 指标总览条：值＋灯＋口径注记＋下钻（桌面横排细线分格、移动 2 列紧凑） */}
      <div id="metrics" className="scroll-mt-20">
        <MetricsStrip
          onDrill={handleMetricDrill}
          status={metricStatusQuery.data}
          isLoading={metricStatusQuery.isLoading}
          isError={metricStatusQuery.isError}
          refetch={() => metricStatusQuery.refetch()}
        />
      </div>

      {/* B4 域级信号：与指标条同响应/同 meta；错误由指标条统一承担，避免三处重复报错 */}
      {metricStatusQuery.data && !metricStatusQuery.isError && (
        <div id="signals" className="scroll-mt-20 space-y-4">
          <DomainStrip
            domains={metricStatusQuery.data.domains ?? []}
            events={metricStatusQuery.data.events ?? []}
            summary={metricStatusQuery.data.summary}
            asOf={metricStatusQuery.data.meta?.snapshot_date}
            onDrill={handleMetricDrill}
          />
          <EventList
            events={metricStatusQuery.data.events ?? []}
            domains={metricStatusQuery.data.domains ?? []}
          />
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 overflow-x-auto border-b border-line">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            aria-pressed={tab === t.key}
            className={`min-h-[44px] shrink-0 border-b-2 px-4 text-body font-medium transition-colors ${
              tab === t.key
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-muted'
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Content（scroll-mt：下钻滚动落点让开移动端 sticky 顶栏） */}
      <div id="analysis-chart" className="scroll-mt-20">
        {tab === 'trend' && <TrendTab />}
        {tab === 'proportion' && <ProportionTab />}
        {tab === 'appreciation' && <AppreciationTab />}
      </div>
    </PageContainer>
  )
}

const PERIOD_TYPE_OPTIONS = [
  { value: 'day', label: '日' },
  { value: 'week', label: '周' },
  { value: 'month', label: '月' },
  { value: 'quarter', label: '季度' },
  { value: 'year', label: '年' },
]

function ProportionTab() {
  const [dimension, setDimension] = useState<ProportionDimension>('liquidity')
  const [chartType, setChartType] = useState<ChartType>('pie')
  const [periodType, setPeriodType] = useState<PeriodType>('month')
  const [selectedPeriod, setSelectedPeriod] = useState('')
  // 资产类别是树维度，可按层级聚合（其余维度平铺，无层级）
  const [classLevel, setClassLevel] = useState<number | undefined>(undefined)

  const { data: assetClassesData } = useQuery({
    queryKey: ['assetClasses'],
    queryFn: api.getAssetClasses,
  })

  const maxLevel = assetClassesData
    ? Math.max(...assetClassesData.items.map((node) => node.level), 0)
    : 0

  const { data: proportionData, isLoading, isError, refetch } = useQuery({
    queryKey: ['proportion', dimension, selectedPeriod, periodType, classLevel],
    queryFn: () => api.getProportionByDimension(
      dimension,
      undefined, // 不再使用snapshot_date
      dimension === 'asset_class' ? classLevel : undefined,
      selectedPeriod || undefined,
      periodType
    ),
    enabled: !!dimension,
  })

  // 结构图数据：share 直接用 API 的 percent（0–100），层内不再算占比。
  const dimensionLabel =
    PROPORTION_DIMENSIONS.find((d) => d.key === dimension)?.label ?? dimension
  const assetItems = (proportionData?.asset_items ?? []).map((item) => ({
    key: `asset-${item.name}`,
    label: item.name,
    value: Number(item.amount),
    share: item.percent,
    note: `${item.count} 项`,
  }))
  const liabilityItems = (proportionData?.liability_items ?? []).map((item) => ({
    key: `liability-${item.name}`,
    label: item.name,
    value: Number(item.amount),
    share: item.percent,
    note: `${item.count} 项`,
  }))
  const assetTotal = proportionData ? Number(proportionData.total_assets) : null
  const liabilityTotal = proportionData ? Number(proportionData.total_liabilities) : null
  // 标签维度是多值口径（spec §2 D8/D9 + 票 08 补充约束）：后端 `total_assets` / `total_liabilities`
  // 是**各标签桶之和**（同一资产在每个标签下各计一次），不等于按项总额。故该维度下：
  // ① 金额一律标成「各标签之和（同一资产重复计入）」，绝不以「总资产 / 总负债 / 总额 / 净值」呈现；
  // ② 不再把它交给图表做 donut 中心与 tooltip 脚注（组件内固定写「合计 ¥…」，本页控制不了）；
  // ③ 页面本就没有「合计 100%」也没有百分比合计校验，无需关闭；范畴说明由 TAG_OVERLAP_NOTE 给。
  const multiValued = dimension === 'tag'
  const assetAmountLabel = multiValued ? '各标签之和' : '总资产'
  const liabilityAmountLabel = multiValued ? '各标签之和' : '总负债'
  const multiValuedAmountNote = multiValued ? '（同一资产重复计入）' : ''
  const assetState = compositionQueryState(isError, isLoading, assetItems.length)
  const liabilityState = compositionQueryState(isError, isLoading, liabilityItems.length)
  const assetSubtitle = proportionData && assetItems.length > 0
    ? `按${dimensionLabel} · ${assetAmountLabel} ${formatValue(proportionData.total_assets, 'cny')}${multiValuedAmountNote}`
    : `按${dimensionLabel}`
  const liabilitySubtitle = proportionData && liabilityItems.length > 0
    ? `按${dimensionLabel} · ${liabilityAmountLabel} ${formatValue(proportionData.total_liabilities, 'cny')}${multiValuedAmountNote}`
    : `按${dimensionLabel}`
  const retry = isError ? () => { void refetch() } : undefined

  return (
    <div className="space-y-6">
      <Card padding="sm" className="space-y-4">
        <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-center tablet:gap-4">
          <span className="shrink-0 text-meta font-medium text-muted">分析维度</span>
          <div className="flex flex-wrap gap-2">
            {PROPORTION_DIMENSIONS.map((d) => (
              <button key={d.key} type="button" aria-pressed={dimension === d.key} onClick={() => setDimension(d.key)}
                className={`inline-flex min-h-[44px] items-center gap-2 rounded-control px-3 text-body font-medium transition-colors ${
                  dimension === d.key ? 'bg-brand-600 text-white' : 'bg-subtle text-secondary'
                }`}>
                <d.icon size={16} aria-hidden />
                {d.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-center tablet:gap-4">
          <span className="shrink-0 text-meta font-medium text-muted">图表类型</span>
          <div className="flex flex-wrap gap-1 rounded-control border border-line bg-surface p-1">
            {CHART_TYPES.map((t) => (
              <button key={t.key} type="button" aria-pressed={chartType === t.key} onClick={() => setChartType(t.key)}
                className={`min-w-[44px] min-h-[44px] rounded-control px-3 text-body transition-colors ${
                  chartType === t.key ? 'bg-brand-50 font-medium text-brand-700' : 'text-muted'
                }`}>
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-col items-start gap-2 tablet:flex-row tablet:items-start tablet:gap-4">
          <span className="shrink-0 text-meta font-medium text-muted tablet:pt-2">选择账期</span>
          <PeriodPicker
            value={selectedPeriod}
            onChange={setSelectedPeriod}
            periodType={periodType}
            onPeriodTypeChange={setPeriodType}
            hint="选择用于结构分析的账期；默认最近一期"
          />
        </div>

        {dimension === 'asset_class' && maxLevel > 0 && (
          <SelectRow
            label="分析层级"
            value={classLevel === undefined ? '' : String(classLevel)}
            onChange={(v) => setClassLevel(v === '' ? undefined : Number(v))}
            options={Array.from({ length: maxLevel + 1 }, (_, i) => ({ value: String(i), label: `层级 ${i}` }))}
          />
        )}
      </Card>

      <div className="grid grid-cols-1 gap-4">
        <Card padding="md" className="space-y-6">
          <LazyCompositionChart
            title="资产结构"
            subtitle={assetSubtitle}
            asOf={formatDate(proportionData?.snapshot_date)}
            height="md"
            mode={chartType}
            narrowFallback="donut"
            items={assetItems}
            total={multiValued ? null : assetTotal}
            totalLabel={assetAmountLabel}
            unit="cny"
            footnote={multiValued ? TAG_OVERLAP_NOTE : undefined}
            state={assetState}
            empty={{ title: '暂无资产数据', hint: '当前账期没有资产项；可切换账期' }}
            onRetry={retry}
          />
          <LazyCompositionChart
            title="负债结构"
            subtitle={liabilitySubtitle}
            asOf={formatDate(proportionData?.snapshot_date)}
            height="md"
            mode={chartType}
            narrowFallback="donut"
            items={liabilityItems}
            total={multiValued ? null : liabilityTotal}
            totalLabel={liabilityAmountLabel}
            unit="cny"
            footnote={multiValued ? TAG_OVERLAP_NOTE : undefined}
            state={liabilityState}
            empty={{ title: '暂无负债数据', hint: '当前账期没有负债项' }}
            onRetry={retry}
          />
        </Card>
      </div>
    </div>
  )
}

// ===== v5 二期·2.1 升值 tab：组合卡片＋逐项表＋「设定口径」弹窗（reason 必填，R-9） =====
function inputAmount(value: number): string {
  return String(Math.round(value * 100) / 100)
}

function PrincipalModal({ target, asOf, onClose }: {
  target: { itemId: number; name: string; current: string | null }
  // 当前查看期（升值表 as_of）：默认生效日＝查看期，避免「晚于查看期→视图无变化」的 UX 陷阱
  asOf: string | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [amount, setAmount] = useState('')
  const [effective, setEffective] = useState(asOf || todayDate())
  const [reason, setReason] = useState('')
  // 每个用户动作一个幂等键；失败重试（react-query 复用同变量）复用同键
  const idemRef = useRef<string | null>(null)

  const { data: hist } = useQuery({
    queryKey: ['principal', target.itemId],
    queryFn: () => api.getPrincipal(target.itemId),
  })

  const mutation = useMutation({
    mutationFn: () => {
      if (!idemRef.current) idemRef.current = newIdemKey()
      return api.setPrincipal(target.itemId, {
        amount, effective_date: effective, reason: reason.trim(),
      }, idemRef.current)
    },
    onSuccess: () => {
      idemRef.current = null
      message.success(`本金口径已登记（生效日 ${effective}，追加不改史）`)
      queryClient.invalidateQueries({ queryKey: ['appreciation'] })
      queryClient.invalidateQueries({ queryKey: ['returns'] })   // 口径增量＝现金流源，联动刷新
      queryClient.invalidateQueries({ queryKey: ['attribution'] })
      queryClient.invalidateQueries({ queryKey: ['principal'] })
      onClose()
    },
    onError: (e: unknown) => message.error(e instanceof Error ? e.message : '登记失败'),
  })

  const valid = amount.trim() !== '' && reason.trim() !== '' && /^\d{4}-\d{2}-\d{2}$/.test(effective)
  // 生效日晚于查看期：按 R-36 从生效日起算，当前查看期视图不会变化——醒目提示
  const afterViewPeriod =
    !!asOf && /^\d{4}-\d{2}-\d{2}$/.test(effective) && effective > asOf

  return (
    <Modal open title={`设定本金口径 · ${target.name}`} onCancel={onClose}
      okText="登记" cancelText="取消" okButtonProps={{ disabled: !valid, loading: mutation.isPending }}
      onOk={() => mutation.mutate()}>
      <div className="space-y-4 py-2">
        <p className="text-xs text-secondary">
          口径＝变更后累计投入本金（带符号：注入正、撤出负）；录错＝再登记一条更正（不改史）。
        </p>
        <div>
          <label className="block text-sm text-secondary mb-1">口径金额 *</label>
          <Input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="如 10000 或 -500（净撤出）" />
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">生效日期 *</label>
          <Input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} />
          {afterViewPeriod && (
            <p className="mt-2 rounded-control border border-status-warn/30 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text">
              该生效日在当前查看期（截至 {asOf}）之后——保存后当前视图不会变化，
              需在 {effective} 起的视图查看。
            </p>
          )}
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">理由 *（人裁决标注，必填）</label>
          <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="如：核对银行流水后确定初始本金" />
        </div>
        {(hist?.history?.length ?? 0) > 0 && (
          <div>
            <div className="text-sm text-secondary mb-1">变更史（当前生效 {hist?.current ?? '未设'}）</div>
            <ul className="text-xs text-secondary space-y-1 max-h-40 overflow-auto">
              {hist!.history.map((h) => (
                <li key={h.id}>
                  {h.effective_date} · {formatCurrency(h.amount)} · {h.reason}
                  {h.origin === 'transfer' && (
                    <span className="ml-1 px-1 rounded bg-sky-100 text-sky-700">转移</span>
                  )}
                  {h.origin === 'lineage' && (
                    <span className="ml-1 px-1 rounded bg-violet-100 text-violet-700">血缘</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  )
}

// ===== v5 二期·2.3 转移事件登记弹窗（登记日＝快照日；缺快照显式引导先补行再重登记） =====
function TransferModal({ items, asOf, onClose }: {
  items: { item_id: number; display_name: string }[]
  asOf: string | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [fromId, setFromId] = useState<string>('')
  const [toId, setToId] = useState<string>('')
  const [amount, setAmount] = useState('')
  const [eventDate, setEventDate] = useState(asOf || todayDate())
  const [reason, setReason] = useState('')
  const [snapshotErr, setSnapshotErr] = useState<{
    event_date?: string
    missing_items?: { item_id: number; display_name: string }[]
    hint?: string
  } | null>(null)
  // 每个用户动作一个幂等键；失败重试复用同键（同 PrincipalModal 模式）
  const idemRef = useRef<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => {
      if (!idemRef.current) idemRef.current = newIdemKey()
      return api.createTransferEvent({
        from_item_id: Number(fromId), to_item_id: Number(toId),
        amount: amount.trim(), event_date: eventDate, reason: reason.trim(),
      }, idemRef.current)
    },
    onSuccess: (out) => {
      idemRef.current = null
      message.success(`已登记转移：${out.event.from_name} → ${out.event.to_name} `
        + `${formatCurrency(out.event.amount)}（生效 ${out.event.event_date}，两侧口径等额变动、不改史）`)
      queryClient.invalidateQueries({ queryKey: ['appreciation'] })
      queryClient.invalidateQueries({ queryKey: ['returns'] })
      queryClient.invalidateQueries({ queryKey: ['attribution'] })
      queryClient.invalidateQueries({ queryKey: ['principal'] })
      onClose()
    },
    onError: (e: unknown) => {
      if (e instanceof ApiError && e.code === 'SNAPSHOT_REQUIRED') {
        setSnapshotErr(e.details ?? {})   // 转为引导 UI，不裸抛
        return
      }
      message.error(e instanceof Error ? e.message : '登记失败')
    },
  })

  const valid = fromId !== '' && toId !== '' && fromId !== toId
    && amount.trim() !== '' && /^\d+(\.\d{1,2})?$/.test(amount.trim())
    && reason.trim() !== '' && /^\d{4}-\d{2}-\d{2}$/.test(eventDate)

  const picker = (value: string, onChange: (v: string) => void) => (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className="w-full border border-line-strong rounded px-2 py-1 text-sm bg-surface">
      <option value="">请选择</option>
      {items.map((it) => (
        <option key={it.item_id} value={String(it.item_id)}>{it.display_name}</option>
      ))}
    </select>
  )

  return (
    <Modal open title="登记内部买卖转移" onCancel={onClose}
      okText="登记" cancelText="取消" okButtonProps={{ disabled: !valid, loading: mutation.isPending }}
      onOk={() => { setSnapshotErr(null); mutation.mutate() }}>
      <div className="space-y-4 py-2">
        <p className="text-xs text-secondary">
          本人两标的间买卖转移：同事务生成两条口径变更（转出侧 −金额／转入侧 ＋金额），
          两侧等额、净值不变；更正＝登记**反向转移**（追加不改史）。
        </p>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-secondary mb-1">转出项 *</label>
            {picker(fromId, setFromId)}
          </div>
          <div>
            <label className="block text-sm text-secondary mb-1">转入项 *</label>
            {picker(toId, setToId)}
          </div>
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">转移金额 *（恒正，方向由两侧表达）</label>
          <Input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="如 5000" />
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">登记日期 *（登记日即快照日）</label>
          <Input type="date" value={eventDate} onChange={(e) => setEventDate(e.target.value)} />
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">理由 *（人裁决标注，必填）</label>
          <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="如：赎回基金甲买入理财乙（同账户腾挪）" />
        </div>
        {snapshotErr && (
          <div className="space-y-1 rounded-control border border-status-warn/30 bg-status-warn-surface px-3 py-2 text-meta text-status-warn-text">
            <div className="font-medium">
              登记日 {snapshotErr.event_date ?? eventDate} 缺 active 快照行的资产项：
            </div>
            {(snapshotErr.missing_items ?? []).map((m) => (
              <div key={m.item_id}>· {m.display_name}</div>
            ))}
            <div>
              {snapshotErr.hint ?? '请先在「记录」页为该日补录/更正该侧快照行（截图 F1 采纳或账本登记），再重登记转移。'}
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

/**
 * 「逐项明细」原生表（`app/analysis/page.tsx`）的派生表宽：Σ档位 + 操作列。
 * 只是下限（spec D3）——容器更宽时 fixed 布局会等比放大，不写死像素。
 */
const APPRECIATION_DETAIL_WIDTH = tokens.table.col['text-mid']
  + tokens.table.col.amount * 3
  + tokens.table.col.ratio
  + actionsWidth([{ label: '设定口径' }, { label: '血缘' }], { cellPadX: tokens.table.actions['pad-x-native'] })

// ===== v5 二期·2.4 血缘链接弹窗（合并/拆分/改版；承接额预填可改、人确认；前身/后继链） =====
const LINEAGE_TYPES = [
  { key: 'revision', label: '改版', desc: '同标的换壳（1→1，全额承接）' },
  { key: 'split', label: '拆分', desc: '拆出一部分（按 ratio 建议承接额）' },
  { key: 'merge', label: '合并', desc: '多只并入新项（求和承接）' },
] as const

function LineageModal({ target, items, asOf, onClose }: {
  target: { itemId: number; name: string }
  items: { item_id: number; display_name: string; cost_basis: string | null; side: string }[]
  asOf: string | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [ltype, setLtype] = useState<'revision' | 'split' | 'merge'>('revision')
  const [newName, setNewName] = useState('')
  const [ratio, setRatio] = useState('1')
  const [others, setOthers] = useState<number[]>([])
  const [principal, setPrincipal] = useState<string | null>(null)  // null＝未手改，跟随建议值
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const idemRef = useRef<string | null>(null)

  const { data: chain } = useQuery({
    queryKey: ['lineage', target.itemId],
    queryFn: () => api.getLineageByItem(target.itemId),
  })

  const basisOf = (id: number): number => {
    const it = items.find((x) => x.item_id === id)
    return it?.cost_basis != null ? Number(it.cost_basis) : 0
  }
  const suggest: number = (() => {
    const base = basisOf(target.itemId)
    if (ltype === 'split') {
      const r = Number(ratio)
      return Number.isFinite(r) && r > 0 && r <= 1 ? base * r : NaN
    }
    if (ltype === 'merge') return base + others.reduce((s, id) => s + basisOf(id), 0)
    return base
  })()
  const cur = principal ?? (Number.isFinite(suggest) ? inputAmount(suggest) : '')

  const mutation = useMutation({
    mutationFn: () => {
      if (!idemRef.current) idemRef.current = newIdemKey()
      const fromIds = ltype === 'merge' ? [target.itemId, ...others] : [target.itemId]
      return api.createLineageLink({
        from_item_ids: fromIds,
        to_item_name: newName.trim(),
        link_type: ltype,
        split_ratio: ltype === 'split' ? ratio : null,
        principal: cur,
        effective_date: asOf || undefined,
        reason: reason.trim(),
        note: note.trim() || null,
      }, idemRef.current)
    },
    onSuccess: (out) => {
      idemRef.current = null
      message.success(`已登记血缘（${out.links[0].from_name}… → ${out.link.to_name}`
        + `，承接 ${formatCurrency(out.entry.amount)}；旧项已归档，链不可改）`)
      queryClient.invalidateQueries({ queryKey: ['appreciation'] })
      queryClient.invalidateQueries({ queryKey: ['returns'] })
      queryClient.invalidateQueries({ queryKey: ['attribution'] })
      onClose()
    },
    onError: (e: unknown) => message.error(e instanceof Error ? e.message : '登记失败'),
  })

  const ratioOk = /^\d{1,2}(\.\d{1,4})?$/.test(ratio) && Number(ratio) > 0 && Number(ratio) <= 1
  const valid = newName.trim() !== '' && reason.trim() !== ''
    && /^\d+(\.\d{1,2})?$/.test(cur) && (ltype !== 'split' || ratioOk)
    && (ltype !== 'merge' || others.length > 0)

  return (
    <Modal open title={`血缘链接 · ${target.name}`} onCancel={onClose}
      okText="登记" cancelText="取消" okButtonProps={{ disabled: !valid, loading: mutation.isPending }}
      onOk={() => mutation.mutate()}>
      <div className="space-y-4 py-2">
        <p className="text-xs text-secondary">
          旧项将**立即归档**（R-37），新项以确认后的承接值起簿；链**创建即固定**不可改
          （拍板 8），承接值录错＝对新项追加更正。改名不走血缘（显示名史已承载，拍板 10）。
        </p>
        <div className="flex gap-2">
          {LINEAGE_TYPES.map((t) => (
            <button key={t.key} onClick={() => { setLtype(t.key); setPrincipal(null) }}
              title={t.desc}
              className={`px-3 py-1 rounded border text-sm ${
                ltype === t.key
                  ? 'border-brand-950 bg-brand-950 text-white'
                  : 'border-line-strong hover:bg-subtle'}`}>
              {t.label}
            </button>
          ))}
        </div>
        {ltype === 'merge' && (
          <div>
            <label className="block text-sm text-secondary mb-1">
              并入的其他来源项 *（本项默认在内，口径求和承接）
            </label>
            <div className="max-h-28 overflow-auto border border-line rounded p-2 space-y-1">
              {items.filter((it) => it.item_id !== target.itemId).map((it) => (
                <label key={it.item_id} className="flex items-center gap-2 text-sm text-secondary">
                  <input type="checkbox" checked={others.includes(it.item_id)}
                    onChange={(e) => setOthers((prev) =>
                      e.target.checked ? [...prev, it.item_id]
                        : prev.filter((x) => x !== it.item_id))} />
                  {it.display_name}
                  {it.side === 'liability' && <span className="text-meta text-muted">负债</span>}
                </label>
              ))}
              {items.length <= 1 && <div className="text-xs text-muted">暂无其他资产项</div>}
            </div>
          </div>
        )}
        <div>
          <label className="block text-sm text-secondary mb-1">新项名称 *（必须未被占用）</label>
          <Input value={newName} onChange={(e) => setNewName(e.target.value)}
            placeholder="如 示例-新标的" />
        </div>
        {ltype === 'split' && (
          <div>
            <label className="block text-sm text-secondary mb-1">拆分比例 *（(0,1]，如 0.6）</label>
            <Input value={ratio} onChange={(e) => { setRatio(e.target.value); setPrincipal(null) }} />
          </div>
        )}
        <div>
          <label className="block text-sm text-secondary mb-1">
            承接本金 *（建议值 {Number.isFinite(suggest) ? inputAmount(suggest) : '—'}，可改＝人确认）
          </label>
          <Input value={cur} onChange={(e) => setPrincipal(e.target.value)} />
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">理由 *（人裁决标注，必填）</label>
          <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="如：基金公司公告份额改版，同口径全额承接" />
        </div>
        <div>
          <label className="block text-sm text-secondary mb-1">备注（可选）</label>
          <Input value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
        {chain && (chain.predecessors.length > 0 || chain.successors.length > 0) && (
          <div>
            <div className="text-sm text-secondary mb-1">本项血缘链</div>
            <ul className="text-xs text-secondary space-y-1">
              {chain.predecessors.map((lk) => (
                <li key={`p${lk.id}`}>前身：{lk.from_name} · {lk.link_type}
                  {lk.split_ratio ? ` × ${lk.split_ratio}` : ''} · {formatDate(lk.created_at)}</li>
              ))}
              {chain.successors.map((lk) => (
                <li key={`s${lk.id}`}>后继：<span className="text-ink">{lk.to_name}</span> · {lk.link_type}
                  {lk.split_ratio ? ` × ${lk.split_ratio}` : ''} · {formatDate(lk.created_at)}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  )
}

// ===== v5 二期·2.2 双口径收益卡（TWR＝衡量策略 / XIRR＝衡量本人操作；率不可得诚实降级） =====
function ReturnsSection() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['returns'],
    queryFn: () => api.getReturns(),
  })
  if (isLoading) return <LoadingState rows={2} compact label="正在加载收益" />
  if (isError) {
    return <ErrorState compact title="收益数据加载失败" description="TWR/XIRR 未能读取，请重试。"
      onRetry={() => refetch()} />
  }
  if (!data) return <EmptyState compact title="暂无收益数据" description="需要至少两期快照与本金口径。" />
  const approx = data.meta.degradations.find((d) => d.code === 'TWR_LARGE_FLOW_APPROX')
  const win = data.twr.period
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Card padding="sm">
          <div className="text-meta text-muted">TWR 年化 · 衡量策略</div>
          <div className="mt-1">
            <Money value={data.twr.annualized} unit="ratio" size="m" emptyLabel="率不可得" />
          </div>
          <div className="mt-1 text-meta text-muted">
            区间 {formatDate(win.start)} → {formatDate(win.end)}
            {win.period_return !== null && <> · 区间收益 {formatRate(win.period_return, 'ratio')}</>}
            {win.subperiods.length > 0 && <> · {win.subperiods.length} 段链接</>}
          </div>
        </Card>
        <Card padding="sm">
          <div className="text-meta text-muted">XIRR 年化 · 衡量本人操作</div>
          <div className="mt-1">
            <Money value={data.xirr} unit="ratio" size="m" emptyLabel="率不可得" />
          </div>
          <div className="mt-1 text-meta text-muted">
            现金流＝口径史外部增量（负债不计）；期末市值一笔正
          </div>
        </Card>
      </div>
      {approx && (
        <InlineError>{approx.message}</InlineError>
      )}
      <details className="text-meta text-muted">
        <summary className="min-h-[44px] cursor-pointer select-none py-3">口径与假设（{data.assumptions.length} 条）</summary>
        <ul className="mt-1.5 list-disc space-y-1 pl-4">
          {data.assumptions.map((a, i) => <li key={i}>{a}</li>)}
        </ul>
      </details>
    </div>
  )
}

// ===== v5 二期增强·期间归因卡（Δ＝外部净投入＋内部转移净额＋内生残差；首版分段列表） =====
function AttributionCard() {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['attribution', from, to],
    queryFn: () => api.getAttribution({
      ...(from ? { date_from: from } : {}),
      ...(to ? { date_to: to } : {}),
    }),
  })
  const covered = data ? Number(data.coverage.principal_set_ratio) * 100 : 0
  return (
    <Card padding="sm" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-h3 font-semibold text-ink">期间归因</div>
        <div className="flex flex-wrap items-center gap-2 text-meta text-muted">
          <span>窗口</span>
          <Input type="date" style={{ width: 140 }} value={from}
            onChange={(e) => setFrom(e.target.value)} placeholder="起" />
          <span>→</span>
          <Input type="date" style={{ width: 140 }} value={to}
            onChange={(e) => setTo(e.target.value)} placeholder="止" />
          {!from && !to && <span>（缺省全历史）</span>}
        </div>
      </div>
      {isLoading ? (
        <LoadingState rows={3} compact label="正在加载期间归因" />
      ) : isError ? (
        <InlineError
          action={
            <button type="button" onClick={() => refetch()} className="min-h-[44px] px-2 text-body-strong text-status-critical-text">
              重试
            </button>
          }
        >
          期间归因加载失败，请重试。
        </InlineError>
      ) : !data ? (
        <EmptyState compact title="暂无归因数据" description="窗口内没有可用快照。" />
      ) : !data.date_from ? (
        <p className="text-body text-muted">窗口内无快照数据，归因不可得。</p>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-body text-secondary">
            <span>净资产</span>
            <Money value={data.net_worth_start} unit="cny" size="s" />
            <span className="text-meta text-muted">（实取 {formatDate(data.net_worth_start_date)}）</span>
            <span className="text-muted">→</span>
            <Money value={data.net_worth_end} unit="cny" size="s" />
            <span className="text-meta text-muted">（实取 {formatDate(data.net_worth_end_date)}）</span>
            <span className="text-muted">，Δ</span>
            <DirectionText value={data.delta} unit="cny" size="s" />
          </div>
          <div className="space-y-1.5">
            {data.segments.map((s) => (
              <div key={s.key} className="flex items-baseline justify-between gap-3 border-b border-line pb-1.5 text-body">
                <div className="min-w-0">
                  <span className="text-secondary">{s.label}</span>
                  {s.key === 'external' && s.inflows !== undefined && (
                    <span className="ml-2 text-meta text-muted">
                      流入 <Money value={s.inflows} unit="cny" size="xs" /> ／ 流出 <Money value={s.outflows ?? '0'} unit="cny" size="xs" />
                    </span>
                  )}
                  {s.key === 'transfer' && (
                    <span className="ml-2 text-meta text-muted">
                      单边总额 <Money value={s.gross ?? '0'} unit="cny" size="xs" />{s.note ? ` · ${s.note}` : ''}
                    </span>
                  )}
                </div>
                <DirectionText value={s.amount} unit="cny" size="xs" />
              </div>
            ))}
          </div>
          <p className="text-meta text-muted">
            核对：{data.segments.map((s) => formatCurrency(s.amount)).join(' ＋ ')} ＝ {formatCurrency(data.delta)}
            （恒等式定义性成立；内生段＝残差，含市值涨跌、未覆盖项与始末快照时滞，勿读作「投资收益」）
          </p>
          <p className="text-meta text-muted">
            口径覆盖率 {formatPercent(covered)} · 未设口径项 {data.coverage.uncovered_items.length > 0 ? data.coverage.uncovered_items.join('、') : '无'}
            {data.coverage.uncovered_items.length > 0 && '（其市值变动全部沉入残差）'}
          </p>
          <details className="text-meta text-muted">
            <summary className="min-h-[44px] cursor-pointer select-none py-3">口径与假设（{data.assumptions.length} 条）</summary>
            <ul className="mt-1.5 list-disc space-y-1 pl-4">
              {data.assumptions.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          </details>
        </>
      )}
    </Card>
  )
}

function AppreciationTab() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['appreciation'],
    queryFn: () => api.getAppreciation(),
  })
  const [principalTarget, setPrincipalTarget] = useState<
    { itemId: number; name: string; current: string | null } | null>(null)
  const [transferOpen, setTransferOpen] = useState(false)
  const [lineageTarget, setLineageTarget] = useState<{ itemId: number; name: string } | null>(null)

  if (isLoading) return <LoadingState rows={4} label="正在加载升值分析" />
  if (isError) {
    return <ErrorState compact title="升值分析加载失败" description="无法读取升值与收益数据，请重试。"
      onRetry={() => refetch()} />
  }
  if (!data) return <EmptyState compact title="暂无升值数据" description="录入资产记录后可查看升值分析。" />

  const p = data.portfolio

  return (
    <div className="space-y-6">
      <Card padding="sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-body text-secondary">
            查看期：<span className="font-medium text-ink">{formatDate(data.as_of)}</span>
            <span className="ml-2 text-meta text-muted">每项取该日（含）前最新快照</span>
          </div>
          <button type="button" onClick={() => setTransferOpen(true)}
            className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-3 text-body text-ink">
            登记转移
          </button>
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <KpiCard label="资产市值" value={p.market_total} />
        <KpiCard label="投入本金合计" value={p.basis_total} />
        <KpiCard label="升值额" value={p.appreciation_total} />
        <KpiCard label="升值率" value={p.rate} unit="ratio" emptyLabel="率不可得" />
      </div>

      {data.meta.degradations.filter((d) => d.code === 'PRINCIPAL_NOT_SET').map((d, i) => (
        <InlineError key={i} action={<Link href="/allocation" className="text-body-strong text-status-critical-text">去配置</Link>}>
          {d.message}：{d.scope}（未设口径项不计入合计率；逐项「设定口径」补齐后自动纳入）
        </InlineError>
      ))}

      <div id="section-returns" className="scroll-mt-20">
        <ReturnsSection />
      </div>
      <AttributionCard />

      <Card padding="none" className="overflow-hidden">
        <div className="border-b border-line px-4 py-3 tablet:px-6">
          <h3 className="text-h3 font-semibold text-ink">逐项明细</h3>
        </div>
        <div className="overflow-x-auto">
          {/* 原生表（spec D6/D7）：就地读同一套档位 + 字面类名 .pf-table（Tailwind JIT 按内容扫描）。
              `table-layout: fixed` 由 .pf-table 给；列宽走 colgroup 的 style（spec C2），
              内边距由 .pf-table 统一给 40（原来的 px-4/px-6 会让每一档丢 8px）。 */}
          <table className="pf-table" style={{ minWidth: APPRECIATION_DETAIL_WIDTH }}>
            <colgroup>
              <col style={{ width: tokens.table.col['text-mid'] }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.amount }} />
              <col style={{ width: tokens.table.col.ratio }} />
              <col style={{ width: actionsWidth([{ label: '设定口径' }, { label: '血缘' }], { cellPadX: tokens.table.actions['pad-x-native'] }) }} />
            </colgroup>
            {/* thead 必须 sticky + z-sticky(20)：A10 要求 thead z 序 = 20。 */}
            <thead className="sticky top-0 z-sticky bg-subtle">
              <tr className="text-left text-meta text-muted">
                <th scope="col" className="pf-col-first py-2.5">资产项</th>
                <th scope="col" className="py-2.5 text-right">市值</th>
                <th scope="col" className="py-2.5 text-right">本金口径</th>
                <th scope="col" className="py-2.5 text-right">升值额</th>
                <th scope="col" className="py-2.5 text-right">升值率</th>
                {/* 右冻结：末列是行级操作 → 冻。 */}
                <th scope="col" className="pf-col-last py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.items.map((it) => (
                <tr key={it.item_id}>
                  <td className="pf-col-first whitespace-normal py-3">
                    <span className="text-secondary">{it.display_name}</span>
                    {it.side === 'liability' && <span className="ml-1 text-meta text-muted">负债</span>}
                  </td>
                  <td className="whitespace-nowrap py-3 text-right">
                    <Money value={it.market} unit="cny" size="s" />
                  </td>
                  <td className="whitespace-nowrap py-3 text-right">
                    {it.cost_basis !== null
                      ? <Money value={it.cost_basis} unit="cny" size="s" />
                      : <span className="text-meta text-muted">未设定</span>}
                  </td>
                  <td className="whitespace-nowrap py-3 text-right">
                    <Money value={it.appreciation} unit="cny" size="s" />
                  </td>
                  <td className="whitespace-nowrap py-3 text-right text-meta">
                    <span className={it.rate === null ? 'text-muted' : 'text-secondary'}>
                      {formatRate(it.rate, 'ratio')}
                    </span>
                  </td>
                  <td className="pf-col-last py-3">
                    <div className="flex gap-2">
                      {it.side === 'asset' && (
                        <button type="button" onClick={() => setPrincipalTarget({ itemId: it.item_id, name: it.display_name, current: it.cost_basis })}
                          className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-2.5 text-meta text-ink">
                          设定口径
                        </button>
                      )}
                      <button type="button" onClick={() => setLineageTarget({ itemId: it.item_id, name: it.display_name })}
                        className="inline-flex min-h-[44px] items-center rounded-control border border-line-strong bg-surface px-2.5 text-meta text-ink">
                        血缘
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr><td colSpan={6} className="py-6 text-center text-meta text-muted">暂无快照数据</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {principalTarget && (
        <PrincipalModal target={principalTarget} asOf={data.as_of}
          onClose={() => setPrincipalTarget(null)} />
      )}
      {transferOpen && (
        <TransferModal items={data.items.map((it) => ({ item_id: it.item_id, display_name: it.display_name }))}
          asOf={data.as_of} onClose={() => setTransferOpen(false)} />
      )}
      {lineageTarget && (
        <LineageModal target={lineageTarget} items={data.items} asOf={data.as_of}
          onClose={() => setLineageTarget(null)} />
      )}
    </div>
  )
}
