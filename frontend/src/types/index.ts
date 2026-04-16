export interface FundType {
  id: number
  name: string
  parent_id: number | null
  level: number
  children: FundType[]
}

export interface Account {
  id: number
  name: string
}

export interface LiquidityRating {
  id: number
  name: string
  sort_order: number
  created_at: string
}

export interface AssetRecord {
  id: number
  asset_date: string
  liquidity_rating_id: number
  liquidity_rating_name: string | null
  fund_type_id: number
  fund_type_name: string | null
  asset_name: string
  account_id: number
  account_name: string | null
  owner_id: number | null
  owner_name: string | null
  amount: string
  import_batch_id: number | null
  created_at: string
  updated_at: string
}

export interface AssetRecordCreate {
  asset_date: string
  liquidity_rating_id: number
  fund_type_id: number
  asset_name: string
  account_id: number
  owner_id?: number
  amount: string
}

export interface BatchUpdateAssets {
  ids: number[]
  liquidity_rating_id?: number
  fund_type_id?: number
  asset_name?: string
  account_id?: number
}

export interface AssetHistoryByNameResponse {
  total_count: number
  sample_records: AssetRecord[]
  all_ids: number[]
}

export interface BatchCreateByPeriodResult {
  success: boolean
  created_count: number
  periods: string[]
  records: AssetRecord[]
  message: string
}

export interface PaginatedResponse {
  items: AssetRecord[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface SummaryData {
  latest_date: string | null
  total_assets: string
  total_liabilities: string
  net_worth: string
  change_amount: string
  change_percent: number | null
  record_count: number
  snapshot_count: number
}

export interface TrendPoint {
  period: string
  date: string
  total_amount: string
  net_assets: string
  liabilities: string
}

export interface ComparisonPoint {
  period: string
  date: string
  total: string
  change_amount: string
  change_percent: number | null
}

export interface MixedChartData {
  trend: TrendPoint[]
  comparison: ComparisonPoint[]
}

export interface ItemTrendPoint {
  period: string
  date: string
  amount: string
  change_amount: string | null
}

export interface ImportBatch {
  id: number
  filename: string
  record_count: number
  imported_at: string
  status: string
  backup_filename?: string
}

export interface ImportPreviewRow {
  row_num: number
  asset_date: string
  liquidity_rating: string
  fund_type: string
  asset_name: string
  account: string
  amount: string
  has_conflict: boolean
  error: string | null
}

export interface NewAttribute {
  type: 'liquidity_rating' | 'fund_type' | 'account'
  name: string
  rows: number[]
}

export interface ImportPreviewResponse {
  batch_id: number
  filename: string
  valid_rows: ImportPreviewRow[]
  invalid_rows: ImportPreviewRow[]
  total_rows: number
  conflict_count: number
  new_attributes: NewAttribute[]
}

export type PeriodType = 'day' | 'month' | 'quarter' | 'year'

export interface ExportPreviewResponse {
  total_count: number
  preview: ImportPreviewRow[]
  filename: string
}

export interface ProportionItem {
  name: string
  amount: string
  count: number
  percent: number
  type?: 'asset' | 'liability'
}

export interface ProportionData {
  dimension: string
  snapshot_date: string
  total_amount: string
  total_count: number
  items: ProportionItem[]
  // 新增字段：资产和负债分开统计
  total_assets: string
  total_liabilities: string
  net_worth: string
  asset_items: ProportionItem[]
  liability_items: ProportionItem[]
}

export interface AllProportionsData {
  snapshot_date: string
  liquidity_rating: ProportionItem[]
  fund_type: ProportionItem[]
  asset_name: ProportionItem[]
  account: ProportionItem[]
}

export interface PeriodOption {
  label: string
  date: string
  count: number
}

export type ChartType = 'pie' | 'donut' | 'rose' | 'treemap' | 'sunburst'
export type ProportionDimension = 'liquidity_rating' | 'fund_type' | 'asset_name' | 'account'

export interface AlertRule {
  id: number
  name: string
  dimension: 'asset_name' | 'fund_type' | 'liquidity_rating' | 'account'
  target_id: string | null
  period_type: 'day' | 'month' | 'quarter' | 'year'
  compare_type: 'previous' | 'custom'
  compare_period: string | null
  amount_threshold: string | null
  percent_threshold: string | null
  direction: 'up' | 'down' | 'both'
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface AlertRuleCreate {
  name: string
  dimension: 'asset_name' | 'fund_type' | 'liquidity_rating' | 'account'
  target_id?: string | null
  period_type: 'day' | 'month' | 'quarter' | 'year'
  compare_type: 'previous' | 'custom'
  compare_period?: string | null
  amount_threshold?: string | null
  percent_threshold?: string | null
  direction?: 'up' | 'down' | 'both'
  is_active?: boolean
}

export interface AlertDetailItem {
  id: string
  name: string
  current_amount: string
  compare_amount: string
  change_amount: string
  change_percent: number | null
  triggered: boolean
}

export interface AlertResult {
  rule_id: number
  rule_name: string
  dimension: string
  target_name: string
  current_period: string
  compare_period: string | null
  current_amount: string
  compare_amount: string
  change_amount: string
  change_percent: number | null
  amount_threshold: string | null
  percent_threshold: string | null
  direction: string
  triggered: boolean
  details: AlertDetailItem[]
  message?: string
}

// --- Allocation ---
export interface AllocationTarget {
  id: number
  dimension: 'fund_type' | 'liquidity_rating' | 'account'
  target_id: string
  parent_id: number | null
  target_percent: string
  created_at: string
  updated_at: string
}

export interface AllocationTargetCreate {
  dimension: 'fund_type' | 'liquidity_rating' | 'account'
  target_id: string
  parent_id?: number | null
  target_percent: string | number
}

export interface AllocationAnalysisItem {
  id: string
  name: string
  parent_id: string | null
  level: number
  target_percent: string | null
  actual_percent: string
  actual_amount: string
  deviation: string
  deviation_percent: string | null
  deviation_amount: string | null
  recommendation: string
  priority?: number
  children: AllocationAnalysisItem[]
}

export interface PeriodConfig {
  periodType: string
  selectedPeriod: string
}

export interface AllocationAnalysis {
  dimension: string
  snapshot_date: string
  total_amount: string
  items: AllocationAnalysisItem[]
  unallocated_percent: string
}

// --- Export History ---
export interface ExportHistory {
  id: number
  export_time: string
  export_type: 'manual' | 'auto'
  filename: string
  file_size: number | null
  operator: string | null
  rule_name: string | null
  file_path: string | null
  created_at: string
}

export interface ExportHistoryList {
  items: ExportHistory[]
  total: number
  page: number
  page_size: number
}

// --- Auto Export Rules ---
export interface AutoExportRule {
  id: number
  name: string
  cron_expression: string
  export_format: string
  filename_template: string | null
  is_active: boolean
  last_run_at: string | null
  next_run_at: string | null
  created_at: string
  updated_at: string
}

export interface AutoExportRuleCreate {
  name: string
  cron_expression: string
  export_format?: string
  filename_template?: string | null
  is_active?: boolean
}

export interface CronValidationResponse {
  valid: boolean
  description: string
  next_run_times: string[]
}

// --- Import Backups ---
export interface ImportBackup {
  filename: string
  path: string
  size: number
  created_at: string
}

// --- Asset Owners ---
export interface AssetOwner {
  id: number
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

// --- Data Export ---
export interface ExportFile {
  filename: string
  file_size: number
  size?: number  // 兼容旧格式
  created_at: string
}

export interface ExportResponse {
  files: ExportFile[]
  message: string
}

// --- Data Import ---
export interface TableInfo {
  name: string
  row_count: number
  columns: string[]
}

export interface StructureDifference {
  added_columns: string[]
  removed_columns: string[]
  type_mismatches: {
    column: string
    source_type: string
    target_type: string
  }[]
}

export interface ImportAnalysisResponse {
  tables: TableInfo[]
  structure_diffs: Record<string, StructureDifference>
  temp_path: string
}

export interface ImportPreviewResponse {
  sample_data: any[]
  total_count: number
  conflict_count: number
}

export interface ImportResult {
  success: boolean
  imported_count: number
  skipped_count: number
  overwritten_count: number
  error_count: number
  message: string
  details?: Record<string, {
    imported: number
    skipped: number
    errors: number
  }>
}
