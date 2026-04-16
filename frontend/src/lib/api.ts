const BASE = '/api'

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export const api = {
  // Assets
  getAssets: (params: Record<string, string>) =>
    request<import('@/types').PaginatedResponse>(`/assets?${new URLSearchParams(params)}`),
  getAsset: (id: number) => request<import('@/types').AssetRecord>(`/assets/${id}`),
  getDates: () => request<string[]>('/assets/dates'),
  getSnapshot: (date: string) => request<import('@/types').AssetRecord[]>(`/assets/snapshot/${date}`),
  getLastSnapshot: (before_date: string, period_type: string) =>
    request<{ date: string | null; count: number }>(`/assets/last-snapshot?before_date=${before_date}&period_type=${period_type}`),
  createAsset: (data: import('@/types').AssetRecordCreate) =>
    request<import('@/types').AssetRecord>('/assets', { method: 'POST', body: JSON.stringify(data) }),
  batchCreate: (records: import('@/types').AssetRecordCreate[]) =>
    request<import('@/types').AssetRecord[]>('/assets/batch', { method: 'POST', body: JSON.stringify({ records }) }),
  updateAsset: (id: number, data: Partial<import('@/types').AssetRecordCreate>) =>
    request<import('@/types').AssetRecord>(`/assets/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAsset: (id: number) => request<{ ok: boolean }>(`/assets/${id}`, { method: 'DELETE' }),
  batchUpdateAssets: (data: import('@/types').BatchUpdateAssets) =>
    request<{ updated_count: number }>('/assets/batch-update', { method: 'POST', body: JSON.stringify(data) }),
  batchDeleteAssets: (ids: number[]) =>
    request<{ deleted_count: number }>('/assets/batch-delete', { method: 'POST', body: JSON.stringify({ ids }) }),
  getAssetsByNames: (asset_names: string[]) =>
    request<import('@/types').AssetRecord[]>('/assets/by-names', { method: 'POST', body: JSON.stringify(asset_names) }),
  getAssetHistoryByName: (asset_name: string) =>
    request<import('@/types').AssetHistoryByNameResponse>('/assets/history-by-name', { method: 'POST', body: JSON.stringify({ asset_name }) }),
  copyFromLast: (target_date: string, period_type: string) =>
    request<{ source_date: string; records: Record<string, unknown>[] }>('/assets/copy-from-last', {
      method: 'POST', body: JSON.stringify({ target_date, period_type }),
    }),
  batchCreateByPeriod: (data: {
    record_template: {
      liquidity_rating_id: number
      fund_type_id: number
      asset_name: string
      account_id: number
      owner_id?: number
      amount: string | number
    }
    period_type: string
    start_period?: string | null
    end_period?: string | null
    conflict_resolution?: string
  }) =>
    request<import('@/types').BatchCreateByPeriodResult>('/assets/batch-create-by-period', {
      method: 'POST', body: JSON.stringify(data),
    }),

  // Analysis
  getSummary: () => request<import('@/types').SummaryData>('/analysis/summary'),
  getTotalTrend: (params: Record<string, string>) =>
    request<import('@/types').TrendPoint[]>(`/analysis/total-trend?${new URLSearchParams(params)}`),
  getPeriodComparison: (params: Record<string, string>) =>
    request<import('@/types').ComparisonPoint[]>(`/analysis/period-comparison?${new URLSearchParams(params)}`),
  getMixedChart: (params: Record<string, string>) =>
    request<import('@/types').MixedChartData>(`/analysis/mixed-chart?${new URLSearchParams(params)}`),
  getByItem: (params: Record<string, string>) =>
    request<import('@/types').ItemTrendPoint[]>(`/analysis/by-item?${new URLSearchParams(params)}`),
  getByType: (params: Record<string, string>) =>
    request<import('@/types').ItemTrendPoint[]>(`/analysis/by-type?${new URLSearchParams(params)}`),
  getByLiquidityRating: (params: Record<string, string>) =>
    request<import('@/types').ItemTrendPoint[]>(`/analysis/by-liquidity-rating?${new URLSearchParams(params)}`),
  getByAccount: (params: Record<string, string>) =>
    request<import('@/types').ItemTrendPoint[]>(`/analysis/by-account?${new URLSearchParams(params)}`),

  // Dimensions
  getFundTypes: () => request<import('@/types').FundType[]>('/dimensions/fund-types'),
  getAccounts: () => request<import('@/types').Account[]>('/dimensions/accounts'),
  getAssetNames: () => request<string[]>('/dimensions/asset-names'),
  getLiquidityRatings: () => request<import('@/types').LiquidityRating[]>('/dimensions/liquidity-ratings'),

  // Management (CRUD for dimensions)
  createFundType: (data: { name: string; parent_id?: number | null; level?: number }) =>
    request<import('@/types').FundType>('/management/fund-types', { method: 'POST', body: JSON.stringify(data) }),
  updateFundType: (id: number, data: { name?: string; parent_id?: number | null; level?: number }) =>
    request<import('@/types').FundType>(`/management/fund-types/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteFundType: (id: number) => request<{ ok: boolean }>(`/management/fund-types/${id}`, { method: 'DELETE' }),

  createAccount: (data: { name: string }) =>
    request<import('@/types').Account>('/management/accounts', { method: 'POST', body: JSON.stringify(data) }),
  updateAccount: (id: number, data: { name?: string }) =>
    request<import('@/types').Account>(`/management/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAccount: (id: number) => request<{ ok: boolean }>(`/management/accounts/${id}`, { method: 'DELETE' }),

  // Liquidity Ratings Management
  createLiquidityRating: (data: { name: string; sort_order?: number }) =>
    request<import('@/types').LiquidityRating>('/liquidity-ratings', { method: 'POST', body: JSON.stringify(data) }),
  updateLiquidityRating: (id: number, data: { name?: string; sort_order?: number }) =>
    request<import('@/types').LiquidityRating>(`/liquidity-ratings/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLiquidityRating: (id: number) => request<{ ok: boolean }>(`/liquidity-ratings/${id}`, { method: 'DELETE' }),

  // Import
  uploadCsv: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch(`${BASE}/imports/upload`, { method: 'POST', body: formData })
    if (!res.ok) throw new Error('Upload failed')
    return res.json() as Promise<import('@/types').ImportPreviewResponse>
  },
  confirmImport: async (file: File, conflict_resolution: string, attribute_actions?: Record<string, string>) => {
    const formData = new FormData()
    formData.append('file', file)
    const actionsParam = attribute_actions ? `&attribute_actions=${encodeURIComponent(JSON.stringify(attribute_actions))}` : ''
    const res = await fetch(`${BASE}/imports/confirm?conflict_resolution=${conflict_resolution}${actionsParam}`, {
      method: 'POST', body: formData,
    })
    if (!res.ok) throw new Error('Import failed')
    return res.json() as Promise<import('@/types').ImportBatch>
  },
  getImportHistory: () => request<import('@/types').ImportBatch[]>('/imports/history'),
  seed: () => request<{ message: string }>('/imports/seed', { method: 'POST' }),

  // Export
  previewExport: (params: Record<string, string>) =>
    request<import('@/types').ExportPreviewResponse>(`/exports/preview?${new URLSearchParams(params)}`),
  downloadExport: (params: Record<string, string>, operator?: string) => {
    const queryParams = new URLSearchParams(params)
    if (operator) queryParams.set('operator', operator)
    const url = `${BASE}/exports/download?${queryParams}`
    return fetch(url).then(res => {
      if (!res.ok) throw new Error('Export failed')
      return res.blob()
    })
  },

  // Proportion Analysis
  getProportionByDimension: (dimension: string, snapshot_date?: string, level?: number) =>
    request<import('@/types').ProportionData>(`/proportion/by-dimension?dimension=${dimension}${snapshot_date ? `&snapshot_date=${snapshot_date}` : ''}${level !== undefined ? `&level=${level}` : ''}`),
  getAllProportions: (snapshot_date?: string) =>
    request<import('@/types').AllProportionsData>(`/proportion/all${snapshot_date ? `?snapshot_date=${snapshot_date}` : ''}`),
  getProportionAvailableDates: () =>
    request<string[]>('/proportion/available-dates'),
  getProportionAvailablePeriods: (period_type: string = 'day') =>
    request<import('@/types').PeriodOption[]>(`/proportion/available-periods?period_type=${period_type}`),

  // Alerts
  getAlertRules: (activeOnly: boolean = false) =>
    request<import('@/types').AlertRule[]>(`/alerts/rules?active_only=${activeOnly}`),
  createAlertRule: (data: import('@/types').AlertRuleCreate) =>
    request<import('@/types').AlertRule>('/alerts/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateAlertRule: (id: number, data: Partial<import('@/types').AlertRuleCreate>) =>
    request<import('@/types').AlertRule>(`/alerts/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAlertRule: (id: number) =>
    request<{ success: boolean }>(`/alerts/rules/${id}`, { method: 'DELETE' }),
  checkAlert: (ruleId: number, currentPeriod?: string) =>
    request<import('@/types').AlertResult>(`/alerts/check/${ruleId}${currentPeriod ? `?current_period=${currentPeriod}` : ''}`),
  getAlertResults: (currentPeriod?: string) =>
    request<import('@/types').AlertResult[]>(`/alerts/results${currentPeriod ? `?current_period=${currentPeriod}` : ''}`),
  getAlertPeriods: (periodType: string = 'month') =>
    request<import('@/types').PeriodOption[]>(`/alerts/periods?period_type=${periodType}`),

  // Allocation
  getAllocationTargets: (dimension?: string) =>
    request<import('@/types').AllocationTarget[]>(`/allocation/targets${dimension ? `?dimension=${dimension}` : ''}`),
  createAllocationTarget: (data: import('@/types').AllocationTargetCreate) =>
    request<import('@/types').AllocationTarget>('/allocation/targets', { method: 'POST', body: JSON.stringify(data) }),
  updateAllocationTarget: (id: number, data: Partial<import('@/types').AllocationTargetCreate>) =>
    request<import('@/types').AllocationTarget>(`/allocation/targets/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAllocationTarget: (id: number) =>
    request<{ success: boolean }>(`/allocation/targets/${id}`, { method: 'DELETE' }),
  getAllocationAnalysis: (dimension: string, snapshotDate?: string) =>
    request<import('@/types').AllocationAnalysis>(`/allocation/analysis?dimension=${dimension}${snapshotDate ? `&snapshot_date=${snapshotDate}` : ''}`),
  validateAllocation: (dimension: string, targetId: string, targetPercent: number, parentId?: number) =>
    request<{ valid: boolean; message: string }>(`/allocation/validation?dimension=${dimension}&target_id=${targetId}&target_percent=${targetPercent}${parentId ? `&parent_id=${parentId}` : ''}`),
  getAdjustmentSuggestions: (dimension: string, snapshotDate?: string) =>
    request<{ dimension: string; snapshot_date: string; suggestions: Array<{
      id: string;
      name: string;
      target_percent: string;
      actual_percent: string;
      deviation: string;
      deviation_amount: string | null;
      recommendation: string;
      priority: number;
    }>; total_count: number }>(`/allocation/suggestions?dimension=${dimension}${snapshotDate ? `&snapshot_date=${snapshotDate}` : ''}`),

  // Export History
  getExportHistory: (params?: { export_type?: string; page?: string; page_size?: string; limit?: string }) =>
    request<import('@/types').ExportHistoryList>(`/export-history/history?${new URLSearchParams(params as Record<string, string> || {})}`),
  
  // Auto Export Rules
  getAutoExportRules: (is_active?: boolean) =>
    request<import('@/types').AutoExportRule[]>(`/export-history/rules${is_active !== undefined ? `?is_active=${is_active}` : ''}`),
  createAutoExportRule: (data: import('@/types').AutoExportRuleCreate) =>
    request<import('@/types').AutoExportRule>('/export-history/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateAutoExportRule: (id: number, data: Partial<import('@/types').AutoExportRuleCreate>) =>
    request<import('@/types').AutoExportRule>(`/export-history/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAutoExportRule: (id: number) =>
    request<{ message: string }>(`/export-history/rules/${id}`, { method: 'DELETE' }),
  validateCronExpression: (cron_expression: string) =>
    request<import('@/types').CronValidationResponse>('/export-history/validate-cron', { method: 'POST', body: JSON.stringify({ cron_expression }) }),
  
  // Import Backups
  getImportBackups: () => request<import('@/types').ImportBackup[]>('/imports/backups'),
  deleteImportBackup: (filename: string) =>
    request<{ message: string }>(`/imports/backups/${filename}`, { method: 'DELETE' }),

  // Asset Owners
  getAssetOwners: () => request<import('@/types').AssetOwner[]>('/asset-owners'),
  createAssetOwner: (data: { name: string; description?: string }) =>
    request<import('@/types').AssetOwner>('/asset-owners', { method: 'POST', body: JSON.stringify(data) }),
  updateAssetOwner: (id: number, data: { name?: string; description?: string }) =>
    request<import('@/types').AssetOwner>(`/asset-owners/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAssetOwner: (id: number) => request<{ ok: boolean }>(`/asset-owners/${id}`, { method: 'DELETE' }),

  // Data Export
  listExports: () => request<{ total: number; files: import('@/types').ExportFile[] }>('/exports'),
  exportTables: (tables: string[], format: 'json' | 'csv') =>
    request<import('@/types').ExportResponse>('/exports/tables', {
      method: 'POST',
      body: JSON.stringify({ tables, format }),
    }),
  downloadExport: (filename: string) => {
    window.open(`${BASE}/exports/download/${filename}`, '_blank')
  },

  // Database Import (.db files)
  uploadDatabaseForImport: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch(`${BASE}/imports/db/analyze`, {
      method: 'POST',
      body: formData,
    }).then(res => {
      if (!res.ok) throw new Error('Upload failed')
      return res.json()
    })
  },
  previewImport: (tempFileId: string, tableName: string) =>
    request<import('@/types').ImportPreviewResponse>('/imports/db/preview', {
      method: 'POST',
      body: JSON.stringify({ temp_file_id: tempFileId, table_name: tableName }),
    }),
  executeImport: (tempFileId: string, tables: string[], conflictStrategy: string) =>
    request<import('@/types').ImportResult>('/imports/db/execute', {
      method: 'POST',
      body: JSON.stringify({
        temp_file_id: tempFileId,
        table_configs: tables.map(table_name => ({
          table_name,
          conflict_strategy: conflictStrategy,
          merge_rules: {}
        })),
      }),
    }),

  // Data File Import (CSV/JSON files)
  uploadDataFileForImport: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch(`${BASE}/imports/data/analyze`, {
      method: 'POST',
      body: formData,
    }).then(res => {
      if (!res.ok) throw new Error('Upload failed')
      return res.json()
    })
  },
  previewDataFileImport: (tempFileId: string, tableName: string) =>
    request<import('@/types').ImportPreviewResponse>('/imports/data/preview', {
      method: 'POST',
      body: JSON.stringify({ temp_file_id: tempFileId, table_name: tableName }),
    }),
  executeDataFileImport: (tempFileId: string, tableName: string, conflictStrategy: string) =>
    request<import('@/types').ImportResult>('/imports/data/execute', {
      method: 'POST',
      body: JSON.stringify({
        temp_file_id: tempFileId,
        table_name: tableName,
        conflict_strategy: conflictStrategy,
      }),
    }),
}
