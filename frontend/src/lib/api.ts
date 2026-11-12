import type {
  ScreenshotPreviewResponse,
  ScreenshotApplyResult,
  ScreenshotEvidenceInfo,
} from '@/types'

const BASE = '/api'

// 截图录入的请求基址：开发模式直连后端 8000——Next dev 代理对 >30s 的
// upstream 有约 30s 超时（socket hang up → 500），而 OCR 解析慢达 4 分钟。
// 生产（Docker 内）继续走相对路径 /api，由 next.config rewrites 服务端转发（无 30s 限制）。
const SCREENSHOT_BASE =
  process.env.NODE_ENV === 'development' ? 'http://localhost:8000/api' : ''

// ===== v5 契约层（spec §5）：错误信封 / 幂等键 =====

/** 后端统一错误信封 `{"error":{code,message,details}}` 的运行时异常。 */
export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown> | null

  constructor(code: string, status: number, message: string,
              details: Record<string, unknown> | null = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

/** 幂等键规则（spec §3）：**每请求一个键**；一次用户动作若包含多个子请求
 *  （如 records 编辑＝PUT＋correct＋rename），每个子请求各生成一键、随该请求的
 *  mutate 变量传入；同请求失败重试时复用其键（由调用方持有）。 */
export function newIdemKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

/** 识别快照回传（ADR-0023 D7）：`recognition` 表单字段**只在有值时** append。
 *  undefined / null 一律不发 —— 后端对「没传」与「传了空数组」是两种语义：
 *  没传 → `items: null` + `note:"client-side"`（那次识别内容未知）；
 *  空数组 → 机器确实识别出 0 项。OCR 路径永远不传本字段（服务端自跑 OCR，自有快照）。 */
function appendRecognition(formData: FormData, recognition?: unknown): void {
  if (recognition === undefined || recognition === null) return
  formData.append('recognition', JSON.stringify(recognition))
}

type RequestOptions = RequestInit & { idemKey?: string }

async function request<T>(url: string, options?: RequestOptions): Promise<T> {
  const isFormData = options?.body instanceof FormData
  const headers: Record<string, string> = isFormData
    ? { ...(options?.headers as Record<string, string>) }  // FormData 不设置 Content-Type，让浏览器自动设置
    : { 'Content-Type': 'application/json', ...(options?.headers as Record<string, string>) }
  if (options?.idemKey) headers['Idempotency-Key'] = options.idemKey
  const init = { ...options } as RequestOptions
  delete init.idemKey
  // 绝对 URL（直连后端）原样使用；相对路径统一拼上 /api 前缀
  const res = await fetch(url.startsWith('http') ? url : `${BASE}${url}`, {
    ...init,
    headers,  // 计算后的 headers 覆盖原 options.headers（含 Content-Type / Idempotency-Key）
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    if (err && err.error && typeof err.error.code === 'string') {
      // v5 信封：业务码与结构完整保留
      throw new ApiError(err.error.code, res.status,
                         err.error.message || 'Request failed',
                         err.error.details ?? null)
    }
    // 兜底（防漏网端点）：旧 `{"detail": ...}` 形状
    const detailMsg = typeof err.detail === 'object'
      ? JSON.stringify(err.detail)
      : (err.detail || 'Request failed')
    throw new Error(detailMsg)
  }
  // 处理 204 No Content 响应
  if (res.status === 204) {
    return undefined as T
  }
  return res.json()
}

// ===== 审计原因：纠正类操作「为什么改」（ADR-0027 D7 / spec audit-reason-capture R1）=====
// 六个用户端点的统一形态是 **query 参数** `?reason=&reason_note=`（correct 另兼容请求体，
// 但前端一律走 query，免得多一处形状要维护）。`correct / delete / archive` 这三类写入
// **必须带 reason**：`actor='owner'` 缺 reason 后端一律 422（见下方 AUDIT_REASON_REQUIRED），
// 所以界面上的理由框都是必填——`AuditReasonField` 不给「不填」这个选项。
// 非法枚举 / 说明 >200 字同样由后端 422。理由不是写事实的一部分，故从不进请求体。

/** 审计原因封闭枚举（与后端 `audit_contract.REASON` 同集）。 */
export type AuditReasonCode =
  | 'correction'
  | 'evolution'
  | 'normalization'
  | 'import'
  | 'migration'
  | 'other'

/** 说明文字上限（与后端 `AUDIT_REASON_NOTE_MAX` 一致；前端只做输入约束，不代替校验）。 */
export const AUDIT_REASON_NOTE_MAX = 200

/** 一次操作要附带的理由；字段类型上都可缺，缺 reason 的 correct / delete / archive 会被后端 422。 */
export interface AuditReasonInput {
  reason?: AuditReasonCode | '' | null
  reason_note?: string | null
}

/**
 * 理由 → query 串；没有理由时返回空串（URL 与改动前逐字节相同）。
 * `reason_note` 只在选了 reason 时才发：单独一条说明无法归类，后端不会把它放进审计行。
 */
function auditReasonQuery(audit?: AuditReasonInput): string {
  const reason = audit?.reason?.trim()
  if (!reason) return ''
  const params = new URLSearchParams({ reason })
  const note = audit?.reason_note?.trim()
  if (note) params.set('reason_note', note)
  return `?${params.toString()}`
}

// ===== 理由转强制（spec audit-reason-enforce E1/E6）=====
// 删除 / 归档这类「改判」写入，`actor='owner'` 且没带 reason 时后端一律 422。
// 前端不替用户编理由：控件不给「不填」这一项，并在点确认前先拦一道（请求根本发不出去）；
// 这里的 422 识别是**兜底**——别的入口（如自画按钮的弹窗）或后端更严时，把话说清楚、弹窗留着。

/** 缺理由时后端的业务码（422）；四个删除/归档入口按它识别。 */
export const AUDIT_REASON_REQUIRED = 'AUDIT_REASON_REQUIRED'

/** 这个错误是不是「缺理由」？（非 ApiError、码不同都算否） */
export function isAuditReasonRequired(error: unknown): boolean {
  return error instanceof ApiError && error.code === AUDIT_REASON_REQUIRED
}

/** 缺理由时的统一说法：四个入口共用一句，避免长出四套文案。 */
export const AUDIT_REASON_REQUIRED_TEXT =
  '这类操作需要说明原因：请先在上面选一条理由，然后再点一次（刚才没有任何改动）。'

// ===== CSV 导入预览的兼容归一（唯一允许出现旧 EAV 列名的位置）=====
// 后端 `/api/imports/upload` 的响应行仍沿用旧列名（asset_class←fund_type 等三个载体列），
// schemas.py 改名由并行车道负责；改名完成后此适配层自动走新键（优先读新键）。

interface RawImportPreviewRow {
  row_num: number
  asset_date: string
  asset_name: string
  amount: string
  side?: 'asset' | 'liability'
  has_conflict?: boolean
  error?: string | null
  // 新列名（改名后出现）
  asset_class?: string
  liquidity_days?: string | number
  institution?: string
  channel?: string
  owner?: string
  // 旧载体列名（当前后端响应）
  fund_type?: string
  liquidity_rating?: string
  account?: string
}

interface RawImportPreviewResponse {
  batch_id: number
  filename: string
  valid_rows: RawImportPreviewRow[]
  invalid_rows: RawImportPreviewRow[]
  total_rows: number
  conflict_count: number
}

function normalizePreviewRow(row: RawImportPreviewRow): import('@/types').ImportPreviewRow {
  const liquidity = row.liquidity_days ?? row.liquidity_rating ?? ''
  return {
    row_num: row.row_num,
    asset_date: row.asset_date,
    asset_name: row.asset_name,
    amount: row.amount,
    side: row.side,
    asset_class: row.asset_class ?? row.fund_type ?? '',
    liquidity_days: liquidity === null ? '' : String(liquidity),
    institution: row.institution ?? row.account ?? '',
    channel: row.channel ?? '',
    owner: row.owner ?? '',
    has_conflict: row.has_conflict ?? false,
    error: row.error ?? null,
  }
}

function normalizeImportPreview(raw: RawImportPreviewResponse): import('@/types').ImportPreviewResponse {
  return {
    batch_id: raw.batch_id,
    filename: raw.filename,
    valid_rows: (raw.valid_rows ?? []).map(normalizePreviewRow),
    invalid_rows: (raw.invalid_rows ?? []).map(normalizePreviewRow),
    total_rows: raw.total_rows,
    conflict_count: raw.conflict_count,
  }
}

export const api = {
  // Assets
  // 列表查询参数支持 include_superseded（'true' 时含被更正历史行；默认仅 active，v5 更正＝追加）
  // source_evidence_id（可选）：按来源凭证反查关联行（spec D8）；不传则请求与改造前完全一致。
  getAssets: (params: Record<string, string>, source_evidence_id?: number) => {
    const search = new URLSearchParams(params)
    if (source_evidence_id !== undefined) {
      search.set('source_evidence_id', String(source_evidence_id))
    }
    return request<import('@/types').PaginatedResponse>(`/assets?${search.toString()}`)
  },
  getAsset: (id: number) => request<import('@/types').AssetRecord>(`/assets/${id}`),
  getDates: () => request<string[]>('/assets/dates'),
  getSnapshot: (date: string) => request<import('@/types').AssetRecord[]>(`/assets/snapshot/${date}`),
  getLastSnapshot: (before_date: string, period_type: string) =>
    request<{ date: string | null; count: number }>(`/assets/last-snapshot?before_date=${before_date}&period_type=${period_type}`),

  // 当前值读模型（spec §4.1）：每资产项截至 as_of 的最新有效记录；不分页，前端筛选/排序。
  // as_of 缺省＝后端今天（前端不传，避免客户端时区把「今天」算成昨天）。
  getCurrentValues: (params: { as_of?: string; include_archived?: boolean } = {}) => {
    const search = new URLSearchParams()
    if (params.as_of) search.set('as_of', params.as_of)
    if (params.include_archived) search.set('include_archived', 'true')
    const qs = search.toString()
    return request<import('@/types').CurrentValueResponse>(`/assets/current${qs ? `?${qs}` : ''}`)
  },

  // 当前值就地写（spec §4.2）：auto＝当日无行则新建、有则更正；correct_only＝仅更正当日已有行。
  // 幂等：只走既有 `Idempotency-Key` 请求头（后端不接受 body 内的幂等键作为去重来源）。
  // amount 一律传十进制字符串（两位小数）；后端响应同样是字符串，不做 number 往返。
  updateCurrentValue: (
    itemId: number,
    data: import('@/types').CurrentValueUpdateRequest,
    idemKey?: string,
  ) =>
    request<import('@/types').CurrentValueUpdateResponse>(
      `/assets/current/${itemId}`,
      { method: 'PUT', body: JSON.stringify(data), idemKey }),
  
  // 创建资产记录（账本事实；分类在资产项上，记录侧不收分类字段——传了会 422）
  createAsset: (data: import('@/types').AssetRecordCreate, idemKey?: string) =>
    request<import('@/types').AssetRecord>('/assets', { method: 'POST', body: JSON.stringify(data), idemKey }),
  
  // 批量创建资产记录（冲突策略：skip / raise；overwrite 已冻结移除）
  // v5 写语义（spec §4/§5）：overwrite 静默覆盖已冻结移除，仅 skip / raise
  batchCreate: (
    records: import('@/types').AssetRecordCreate[],
    conflictStrategy: 'skip' | 'raise' = 'skip',
    idemKey?: string
  ) =>
    request<{
      success: boolean
      created_count: number
      skipped: { asset_name: string; asset_date: string; existing_id: number; reason: string }[]
      records: import('@/types').AssetRecord[]
    }>(
      '/assets/batch',
      { method: 'POST', body: JSON.stringify({ records, conflict_strategy: conflictStrategy }), idemKey }
    ),
  
  // 从扁平字段创建（历史组件入口；只有账本事实字段，不再拼五维 id）
  createAssetFromFields: (data: {
    asset_date: string
    asset_name: string
    amount: string
    side?: 'asset' | 'liability'
    asset_item_id?: number | null
  }) =>
    api.createAsset({
      asset_date: data.asset_date,
      asset_name: data.asset_name,
      amount: data.amount,
      side: data.side,
      asset_item_id: data.asset_item_id,
    }),
  
  // 批量从扁平字段创建
  batchCreateFromFields: (records: {
    asset_date: string
    asset_name: string
    amount: string
    side?: 'asset' | 'liability'
    asset_item_id?: number | null
  }[]) => {
    const convertedRecords: import('@/types').AssetRecordCreate[] = records.map(r => ({
      asset_date: r.asset_date,
      asset_name: r.asset_name,
      amount: r.amount,
      side: r.side,
      asset_item_id: r.asset_item_id,
    }))
    return api.batchCreate(convertedRecords)
  },
  
  updateAsset: (id: number, data: import('@/types').AssetRecordUpdate, idemKey?: string) =>
    request<import('@/types').AssetRecord>(`/assets/${id}`, { method: 'PUT', body: JSON.stringify(data), idemKey }),

  // v5 写语义（spec §4/§5）：金额更正＝同日追加新行＋旧行 supersede（write-once 唯一入口）
  // 更正理由走 query（`audit` 缺省＝不记录原因）；请求体只放写事实（amount/side）。
  correctAsset: (id: number, data: { amount: string; side?: string }, idemKey?: string,
                 audit?: AuditReasonInput) =>
    request<{ record: import('@/types').AssetRecord; change: import('@/types').LedgerChange }>(
      `/assets/${id}/correct${auditReasonQuery(audit)}`,
      { method: 'POST', body: JSON.stringify(data), idemKey }),

  // v5 资产项（/api/asset-items）
  listAssetItems: (includeArchived: boolean = true) =>
    request<{ items: import('@/types').AssetItemInfo[]; total: number }>(
      `/asset-items?include_archived=${includeArchived}`),
  // 改名（票 03 D11/D12）：两步契约 —— preview 出影响面并签发一次性 confirm_token，
  // rename 带 token 执行；reason/effect 必填（缺省由后端 400 拒绝，不做静默兼容）。
  // 反常组合（如 correction+current）必须带 reason_note，否则后端 400。
  previewRenameItem: (itemId: number, data: import('@/types').RenamePreviewRequest) =>
    request<import('@/types').RenamePreviewResponse>(
      `/asset-items/${itemId}/rename/preview`, { method: 'POST', body: JSON.stringify(data) }),
  renameAssetItem: (itemId: number, data: import('@/types').RenameRequest, idemKey?: string) =>
    request<import('@/types').RenameResponse>(
      `/asset-items/${itemId}/rename`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  // 归档：**暂无 UI 调用点**（records 页不提供归档入口），`audit` 参数为将来的归档入口预留，
  // 形状与其余五个端点一致（query `?reason=&reason_note=`），调用点按需传即可。
  archiveAssetItem: (itemId: number, idemKey?: string, audit?: AuditReasonInput) =>
    request<{ item: import('@/types').AssetItemInfo; change: import('@/types').LedgerChange }>(
      `/asset-items/${itemId}/archive${auditReasonQuery(audit)}`, { method: 'POST', idemKey }),
  unarchiveAssetItem: (itemId: number, idemKey?: string) =>
    request<{ item: import('@/types').AssetItemInfo; change: import('@/types').LedgerChange }>(
      `/asset-items/${itemId}/unarchive`, { method: 'POST', idemKey }),

  // ===== v5 二期·2.1 本金口径与升值 =====
  getPrincipal: (itemId: number) =>
    request<import('@/types').PrincipalHistoryResponse>(`/asset-items/${itemId}/principal`),
  setPrincipal: (itemId: number,
    data: { amount: string; effective_date: string; reason: string; evidence_ref?: number | null },
    idemKey?: string) =>
    request<import('@/types').PrincipalSetResponse>(
      `/asset-items/${itemId}/principal`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  getAppreciation: (asOf?: string) =>
    request<import('@/types').AppreciationResponse>(
      `/analysis/appreciation${asOf ? `?as_of=${asOf}` : ''}`),
  // v5 二期·2.2 双口径收益（只读；缺省 scope=portfolio、窗口=全历史）
  getReturns: (params?: Record<string, string | number>) =>
    request<import('@/types').ReturnsResponse>(
      `/analysis/returns?${new URLSearchParams(
        Object.entries({ scope: 'portfolio', ...params })
          .filter(([, v]) => v !== undefined && v !== null)
          .map(([k, v]) => [k, String(v)] as [string, string]))}`),
  // v5 二期·2.3 转移事件：登记＝同事务两条口径变更；更正＝登记反向转移（追加）
  createTransferEvent: (data: {
    from_item_id: number; to_item_id: number; amount: string
    event_date: string; reason: string; evidence_ref?: number | null
  }, idemKey?: string) =>
    request<import('@/types').TransferCreateResponse>(
      '/transfer-events', { method: 'POST', body: JSON.stringify(data), idemKey }),
  listTransferEvents: (params?: { item_id?: number; date_from?: string; date_to?: string }) =>
    request<import('@/types').TransferListResponse>(
      `/transfer-events?${new URLSearchParams(
        Object.entries({ ...params })
          .filter(([, v]) => v !== undefined && v !== null)
          .map(([k, v]) => [k, String(v)] as [string, string]))}`),
  // v5 二期增强：期间归因（只读；缺省 scope=portfolio、窗口=全历史）
  getAttribution: (params?: Record<string, string | number>) =>
    request<import('@/types').AttributionResponse>(
      `/analysis/attribution?${new URLSearchParams(
        Object.entries({ scope: 'portfolio', ...params })
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)] as [string, string]))}`),
  // v5 二期·2.4 血缘链接：登记＝旧项归档＋新项＋链＋口径承接（人确认预填值）
  createLineageLink: (data: {
    from_item_ids: number[]; to_item_name: string; link_type: string
    split_ratio?: string | null; principal: string
    effective_date?: string | null; reason: string; note?: string | null
  }, idemKey?: string) =>
    request<import('@/types').LineageCreateResponse>(
      '/lineage-links', { method: 'POST', body: JSON.stringify(data), idemKey }),
  getLineageByItem: (itemId: number) =>
    request<import('@/types').LineageByItemResponse>(
      `/lineage-links?item_id=${itemId}`),
  getLineageAll: () =>
    request<import('@/types').LineageAllResponse>('/lineage-links'),
  // 一对多拆分（1→N，spec `.scratch/lineage-split/spec.md` §1）：
  // 父项归档（archived_effective_from = effective_date）＋子项建链＋承接。
  splitLineageLink: (data: import('@/types').LineageSplitRequest, idemKey?: string) =>
    request<import('@/types').LineageSplitResponse>(
      '/lineage-links/split', { method: 'POST', body: JSON.stringify(data), idemKey }),
  // 既有项之间补登记血缘（spec `.scratch/lineage-attach/spec.md` §1.1）：
  // 前身自生效日起归档；历史行不改写，读数按身份族合并。不写本金口径。
  attachLineageLinks: (data: import('@/types').LineageAttachRequest, idemKey?: string) =>
    request<import('@/types').LineageAttachResponse>(
      '/lineage-links/attach', { method: 'POST', body: JSON.stringify(data), idemKey }),
  // 身份族（spec §1.2）：双向传递闭包，含自身。
  getLineageFamily: (itemId: number) =>
    request<import('@/types').LineageFamilyResponse>(`/lineage-links/family?item_id=${itemId}`),
  
  // 从扁平字段更新（只有账本事实字段；分类改资产项端点）
  updateAssetFromFields: (id: number, data: {
    asset_date?: string
    asset_name?: string
    amount?: string
    side?: 'asset' | 'liability'
  }) => {
    const updateData: import('@/types').AssetRecordUpdate = {}
    if (data.asset_date !== undefined) updateData.asset_date = data.asset_date
    if (data.asset_name !== undefined) updateData.asset_name = data.asset_name
    if (data.amount !== undefined) updateData.amount = data.amount
    if (data.side !== undefined) updateData.side = data.side
    return api.updateAsset(id, updateData)
  },
  
  // 删除（理由走 query：DELETE 带 body 兼容性差）
  deleteAsset: (id: number, idemKey?: string, audit?: AuditReasonInput) =>
    request<{ ok: boolean }>(`/assets/${id}${auditReasonQuery(audit)}`, { method: 'DELETE', idemKey }),
  
  // 批量更新记录（仅 side 可批量；amount/date/name 走单条更正/改名，后端 409）
  batchUpdateAssets: (data: import('@/types').BatchUpdateAssets, idemKey?: string) =>
    request<{ updated_count: number }>('/assets/batch-update', { method: 'POST', body: JSON.stringify(data), idemKey }),
  
  // 从扁平字段批量更新
  batchUpdateAssetsFromFields: (ids: number[], data: {
    asset_name?: string
    amount?: string
    side?: 'asset' | 'liability'
  }) => {
    const updateData: import('@/types').BatchUpdateAssets = { ids }
    if (data.asset_name !== undefined) updateData.asset_name = data.asset_name
    if (data.amount !== undefined) updateData.amount = data.amount
    if (data.side !== undefined) updateData.side = data.side
    return api.batchUpdateAssets(updateData)
  },
  
  // 批量删除：理由记在头行，整批共用一条（逐行明细不带，spec R5）
  batchDeleteAssets: (ids: number[], idemKey?: string, audit?: AuditReasonInput) =>
    request<{ deleted_count: number }>(
      `/assets/batch-delete${auditReasonQuery(audit)}`,
      { method: 'POST', body: JSON.stringify({ ids }), idemKey }),
  getAssetsByNames: (asset_names: string[]) =>
    request<import('@/types').AssetRecord[]>('/assets/by-names', { method: 'POST', body: JSON.stringify(asset_names) }),
  getAssetHistoryByName: (asset_name: string) =>
    request<import('@/types').AssetHistoryByNameResponse>('/assets/history-by-name', { method: 'POST', body: JSON.stringify({ asset_name }) }),
  
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
  // 分项趋势·身份族口径（spec `.scratch/lineage-attach/spec.md` §1.3）：
  // 按 item_id 取「族合计 / 本项 / 当日构成」；family 缺省 true。
  getByItemFamily: (params: { item_id: number | string; period_type: string; family?: boolean }) => {
    const search = new URLSearchParams({
      item_id: String(params.item_id),
      period_type: params.period_type,
      family: String(params.family ?? true),
    })
    return request<import('@/types').ItemFamilyTrendResponse>(`/analysis/by-item?${search}`)
  },
  // 分项趋势（ADR-0019）：按资产类别（含后代）/ 流动性档位 / 机构 / 渠道 / 归属 / 标签
  getByClass: (params: { asset_class_id: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-class?${new URLSearchParams({
        asset_class_id: String(params.asset_class_id),
        period_type: params.period_type,
      })}`),
  getByLiquidity: (params: { days: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-liquidity?${new URLSearchParams({
        days: String(params.days),
        period_type: params.period_type,
      })}`),
  getByInstitution: (params: { institution_id: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-institution?${new URLSearchParams({
        institution_id: String(params.institution_id),
        period_type: params.period_type,
      })}`),
  getByChannel: (params: { channel_id: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-channel?${new URLSearchParams({
        channel_id: String(params.channel_id),
        period_type: params.period_type,
      })}`),
  getByOwner: (params: { owner_id: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-owner?${new URLSearchParams({
        owner_id: String(params.owner_id),
        period_type: params.period_type,
      })}`),
  getByTag: (params: { tag_id: number | string; period_type: string }) =>
    request<import('@/types').ItemTrendPoint[]>(
      `/analysis/by-tag?${new URLSearchParams({
        tag_id: String(params.tag_id),
        period_type: params.period_type,
      })}`),

  // ===== 三期-0 指标骨架（spec §3 冻结契约；只读）=====
  // 字典静态（注册表＝代码真源），适合长缓存；状态面缺省 as_of＝最新快照日（越界 422）。
  getMetrics: () =>
    request<import('@/types').MetricDictResponse>('/metrics'),
  getMetricStatus: (asOf?: string) =>
    request<import('@/types').MetricStatusResponse>(
      `/metrics/status${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`),

  // ===== 三期-1 C1：目标 / 达成度 / item 归属（spec §5）=====
  getGoals: (includeArchived = false) =>
    request<import('@/types').GoalListResponse>(
      `/planning/goals${includeArchived ? '?include_archived=true' : ''}`),
  getGoal: (id: number) =>
    request<import('@/types').GoalDetailResponse>(`/planning/goals/${id}`),
  createGoal: (data: import('@/types').GoalCreateInput, idemKey?: string) =>
    request<{ goal: import('@/types').Goal; version: import('@/types').GoalVersion; change: unknown }>(
      '/planning/goals', { method: 'POST', body: JSON.stringify(data), idemKey }),
  renameGoal: (id: number, name: string, idemKey?: string) =>
    request<{ goal: import('@/types').Goal; change: unknown }>(
      `/planning/goals/${id}`, { method: 'PUT', body: JSON.stringify({ name }), idemKey }),
  createGoalVersion: (id: number, data: import('@/types').GoalVersionInput, idemKey?: string) =>
    request<{ version: import('@/types').GoalVersion; change: unknown }>(
      `/planning/goals/${id}/versions`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  updateGoalVersion: (id: number, versionNo: number, data: Partial<import('@/types').GoalVersionInput>, idemKey?: string) =>
    request<{ version: import('@/types').GoalVersion; change: unknown }>(
      `/planning/goals/${id}/versions/${versionNo}`,
      { method: 'PUT', body: JSON.stringify(data), idemKey }),
  deleteGoalVersion: (id: number, versionNo: number, idemKey?: string) =>
    request<{ deleted: boolean; change: unknown }>(
      `/planning/goals/${id}/versions/${versionNo}`, { method: 'DELETE', idemKey }),
  activateGoalVersion: (id: number, versionNo: number, idemKey?: string) =>
    request<{ version: import('@/types').GoalVersion; superseded_version_id?: number | null; change: unknown }>(
      `/planning/goals/${id}/versions/${versionNo}/activate`, { method: 'POST', idemKey }),
  archiveGoal: (id: number, idemKey?: string, audit?: AuditReasonInput) =>
    request<{ goal: import('@/types').Goal; change: unknown }>(
      `/planning/goals/${id}/archive${auditReasonQuery(audit)}`, { method: 'POST', idemKey }),
  setPrimaryGoal: (id: number, idemKey?: string) =>
    request<{ goal: import('@/types').Goal; change: unknown }>(
      `/planning/goals/${id}/primary`, { method: 'POST', idemKey }),
  getGoalProgress: (id: number, asOf?: string) =>
    request<import('@/types').GoalProgress>(
      `/planning/goals/${id}/progress${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`),
  getItemOwner: (itemId: number) =>
    request<import('@/types').ItemOwnerResponse>(`/asset-items/${itemId}/owner`),
  getItemOwners: (itemIds: number[]) =>
    request<import('@/types').ItemOwnerBulkResponse>(
      `/asset-items/owners?ids=${itemIds.join(',')}`),
  // 改归属是纠正类写入（审计 action='change' ∈ REASON_REQUIRED_ACTIONS）：审计理由走 query。
  // 请求体里的 reason 是归属变更史的自由文本，与审计理由**不是同一个字段**，不能互相顶替。
  // `audit` 刻意放在 `idemKey` **之前**且不给默认值：漏传会当场编译失败，不会静默错位。
  batchSetItemOwners: (
    data: import('@/types').BatchOwnerInput,
    audit: AuditReasonInput,
    idemKey?: string,
  ) =>
    request<import('@/types').BatchOwnerResponse>(
      `/asset-items/owners/batch${auditReasonQuery(audit)}`,
      { method: 'POST', body: JSON.stringify(data), idemKey }),
  updateAssetTags: (id: number, tagIds: number[], idemKey?: string) =>
    request<{ record: import('@/types').AssetRecord }>(
      `/assets/${id}/tags`, { method: 'PUT', body: JSON.stringify({ tag_ids: tagIds }), idemKey }),
  batchSetAssetTags: (data: import('@/types').AssetTagBatchInput, idemKey?: string) =>
    request<import('@/types').AssetTagBatchResponse>(
      '/assets/tags/batch', { method: 'POST', body: JSON.stringify(data), idemKey }),
  setItemOwner: (
    itemId: number,
    data: { owner_kind: import('@/types').ItemOwnerKind; owner_id?: number | null; reason?: string | null },
    audit: AuditReasonInput,
    idemKey?: string,
  ) =>
    request<{ entry: import('@/types').ItemOwnerEntry; current: import('@/types').ItemOwnerEntry | null; change: unknown }>(
      `/asset-items/${itemId}/owner${auditReasonQuery(audit)}`,
      { method: 'POST', body: JSON.stringify(data), idemKey }),

  // ===== 三期-3 C3：建议收件箱（spec §6）=====
  getAdviceList: (params: {
    status?: string
    horizon?: string
    goal_id?: number
    page?: number
    page_size?: number
  } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') q.append(key, String(value))
    })
    return request<import('@/types').AdvisorSuggestionList>(
      `/planning/advice${q.toString() ? `?${q}` : ''}`)
  },
  createAdvice: (data: import('@/types').AdvisorCreateInput, idemKey?: string) =>
    request<{ suggestion_id: number; job_id: string; status: string; dedupe_key: string }>(
      '/planning/advice', { method: 'POST', body: JSON.stringify(data), idemKey }),
  getAdvice: (id: number) =>
    request<import('@/types').AdvisorSuggestionDetail>(`/planning/advice/${id}`),
  getAdviceInfoSet: (id: number) =>
    request<import('@/types').AdvisorInfoSet>(`/planning/advice/${id}/info-set`),
  acceptAdvice: (id: number, reason?: string, idemKey?: string) =>
    request<import('@/types').AdvisorSuggestionDetail>(
      `/planning/advice/${id}/accept`, { method: 'POST', body: JSON.stringify({ reason: reason || null }), idemKey }),
  rejectAdvice: (id: number, reason: string, idemKey?: string) =>
    request<import('@/types').AdvisorSuggestionDetail>(
      `/planning/advice/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }), idemKey }),
  recordAdviceExecution: (id: number, data: { execution_status: import('@/types').AdvisorExecutionStatus; note?: string | null }, idemKey?: string) =>
    request<import('@/types').AdvisorSuggestionDetail>(
      `/planning/advice/${id}/execution`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  getAdviceSettings: () =>
    request<import('@/types').AdvisorSettings>('/planning/advice/settings'),
  updateAdviceSettings: (data: Partial<Pick<import('@/types').AdvisorSettings, 'auto_enabled' | 'cooldown_days' | 'default_horizon'>>, idemKey?: string) =>
    request<import('@/types').AdvisorSettings & { change: unknown }>(
      '/planning/advice/settings', { method: 'PUT', body: JSON.stringify(data), idemKey }),

  // ===== 三期-4 C4：复盘与提案（spec §5）=====
  getReviews: (params: { goal_id?: number; status?: string; page?: number; page_size?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') q.append(key, String(value))
    })
    return request<import('@/types').ReviewListResponse>(`/planning/reviews${q.toString() ? `?${q}` : ''}`)
  },
  getReviewCandidates: (params: { goal_id?: number; as_of?: string; start_as_of?: string }) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') q.append(key, String(value))
    })
    return request<import('@/types').ReviewCandidateList>(`/planning/reviews/candidates?${q}`)
  },
  createReview: (data: import('@/types').ReviewCreateInput, idemKey?: string) =>
    request<{ review: import('@/types').ReviewSummary; change: unknown }>(
      '/planning/reviews', { method: 'POST', body: JSON.stringify(data), idemKey }),
  getReview: (id: number) =>
    request<import('@/types').ReviewDetailResponse>(`/planning/reviews/${id}`),
  updateReviewConclusion: (id: number, data: { attribution?: import('@/types').ReviewAttribution[]; conclusion?: string | null }, idemKey?: string) =>
    request<{ review: import('@/types').ReviewSummary; change: unknown }>(
      `/planning/reviews/${id}/conclusion`, { method: 'PUT', body: JSON.stringify(data), idemKey }),
  closeReview: (id: number, conclusion: string, idemKey?: string) =>
    request<{ review: import('@/types').ReviewSummary; change: unknown }>(
      `/planning/reviews/${id}/close`, { method: 'POST', body: JSON.stringify({ conclusion }), idemKey }),
  createReviewProposal: (reviewId: number, data: import('@/types').ReviewProposalInput, idemKey?: string) =>
    request<{ proposal: import('@/types').ReviewProposal; change: unknown }>(
      `/planning/reviews/${reviewId}/proposals`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  adoptReviewProposal: (reviewId: number, proposalId: number, data: { reason?: string | null; applied_kind?: string | null; applied_ref?: string | null } = {}, idemKey?: string) =>
    request<{ proposal: import('@/types').ReviewProposal; change: unknown }>(
      `/planning/reviews/${reviewId}/proposals/${proposalId}/adopt`, { method: 'POST', body: JSON.stringify(data), idemKey }),
  dismissReviewProposal: (reviewId: number, proposalId: number, reason: string, idemKey?: string) =>
    request<{ proposal: import('@/types').ReviewProposal; change: unknown }>(
      `/planning/reviews/${reviewId}/proposals/${proposalId}/dismiss`, { method: 'POST', body: JSON.stringify({ reason }), idemKey }),

  // ===== 分类字典（ADR-0019，`/api/classification/*`，唯一字典入口） =====
  // 资产类别树（平铺返回；`level`/`path`/`leaf` 由后端给）
  // 注意：POST / PUT 的响应**不含** `path` / `leaf`（后端 `create_asset_class` 只回
  // 六个字段），因此写操作一律按「返回不完全体」处理，写完重新拉列表。
  getAssetClasses: () =>
    request<{ items: import('@/types').AssetClass[]; total: number }>('/classification/asset-classes'),
  createAssetClass: (data: import('@/types').AssetClassInput) =>
    request<import('@/types').AssetClassWriteResult>('/classification/asset-classes', { method: 'POST', body: JSON.stringify(data) }),
  updateAssetClass: (id: number, data: Partial<import('@/types').AssetClassInput>) =>
    request<import('@/types').AssetClassWriteResult>(`/classification/asset-classes/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  // 删除字典项：删除属于「改判」，理由走 query（spec audit-reason-enforce E5）；
  // 不传 audit 时 URL 与改造前完全一致。
  deleteAssetClass: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/classification/asset-classes/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 流动性档位（只读 8 档：days 是权威，label 是展示派生；旧 CRUD 已 410）
  getLiquidityLevels: () =>
    request<{ items: import('@/types').LiquidityLevel[]; total: number }>('/classification/liquidity-levels'),

  // 机构（银行/券商/基金/保险/政府/平台/其他）
  getInstitutions: () =>
    request<{ items: import('@/types').Institution[]; total: number }>('/classification/institutions'),
  createInstitution: (data: { name: string; kind?: import('@/types').InstitutionKind }) =>
    request<import('@/types').Institution>('/classification/institutions', { method: 'POST', body: JSON.stringify(data) }),
  updateInstitution: (id: number, data: { name?: string; kind?: import('@/types').InstitutionKind; active?: boolean }) =>
    request<import('@/types').Institution>(`/classification/institutions/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteInstitution: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/classification/institutions/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 渠道（App / 子钱包 / 代销口）
  getChannels: () =>
    request<{ items: import('@/types').Channel[]; total: number }>('/classification/channels'),
  createChannel: (data: { name: string }) =>
    request<import('@/types').Channel>('/classification/channels', { method: 'POST', body: JSON.stringify(data) }),
  updateChannel: (id: number, data: { name?: string; active?: boolean }) =>
    request<import('@/types').Channel>(`/classification/channels/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteChannel: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/classification/channels/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 家庭成员（归属维度唯一值域；role = self|spouse|joint|child|nominee）
  getMembers: () =>
    request<{ items: import('@/types').Member[]; total: number }>('/classification/members'),
  createMember: (data: { name: string; role: import('@/types').MemberRole }) =>
    request<import('@/types').Member>('/classification/members', { method: 'POST', body: JSON.stringify(data) }),
  updateMember: (id: number, data: { name?: string; role?: import('@/types').MemberRole; active?: boolean }) =>
    request<import('@/types').Member>(`/classification/members/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteMember: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/classification/members/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 标签（受控词表 + 自由词；挂资产项，不挂记录）
  getTags: () =>
    request<{ items: import('@/types').TagItem[]; total: number }>('/classification/tags'),
  createTag: (data: { name: string; kind?: 'controlled' | 'free' }) =>
    request<import('@/types').TagItem>('/classification/tags', { method: 'POST', body: JSON.stringify(data) }),
  updateTag: (id: number, data: { name?: string; kind?: 'controlled' | 'free' }) =>
    request<import('@/types').TagItem>(`/classification/tags/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteTag: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/classification/tags/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // ===== 资产项级分类读写（记录侧只读） =====
  getItemClassification: (itemId: number) =>
    request<import('@/types').ItemClassification>(`/asset-items/${itemId}/classification`),
  // 批量读（≤200 个 id，逗号分隔），避免记录页逐项 N+1。
  // 注意：后端 `classifications_for_items` 返回的是**以 item_id 为键的对象**，
  // 不是数组（见 classification_service）；这里如实声明两种形态，由
  // `normalizeItemClassifications` 归一。
  getItemClassifications: (itemIds: number[]) =>
    request<{
      items:
        | import('@/types').ItemClassification[]
        | Record<string, import('@/types').ItemClassification>
    }>(`/asset-items/classifications?ids=${itemIds.join(',')}`),
  // 单项改分类：只改传进来的维度；四维不能置 null（清空请把项转 draft）
  setItemClassification: (itemId: number, data: import('@/types').ItemClassificationUpdate, idemKey?: string) =>
    request<import('@/types').ItemClassification>(
      `/asset-items/${itemId}/classification`, { method: 'PUT', body: JSON.stringify(data), idemKey }),
  // 整组替换项级标签（不是增量；上限 5 个）。
  // 响应是该资产项的最新分类视图（含 tags），不是记录对象。
  setItemTags: (itemId: number, tagIds: number[], idemKey?: string) =>
    request<import('@/types').ItemClassification>(
      `/asset-items/${itemId}/tags`, { method: 'PUT', body: JSON.stringify({ tag_ids: tagIds }), idemKey }),

  // 项级备注（ADR-0031 D2/D3）：整条替换，`note:null`/空串＝清空（后端 trim 后落 NULL）。
  // trim 后 >500 字符 → 422 `NOTE_TOO_LONG`；项不存在 → 404 `ASSET_ITEM_NOT_FOUND`。
  // **读**走列表端点（`GET /api/asset-items` 每项含 `note`，见 `fetchItemNotes`），
  // 没有单独的 GET /note 端点：这里只有写方法是对的，不是缺读方法。
  updateAssetItemNote: (itemId: number, data: import('@/types').AssetItemNoteUpdate, idemKey?: string) =>
    request<import('@/types').AssetItemNoteResponse>(
      `/asset-items/${itemId}/note`, { method: 'PUT', body: JSON.stringify(data), idemKey }),

  // Dimensions
  getAssetNames: () => request<string[]>('/dimensions/asset-names'),

  // Import
  // 导入预览：后端响应仍是旧 EAV 三列载体名（schemas.py 待改），这里一次性归一到新语义，
  // 上层一律只见 asset_class / liquidity_days / institution / channel / owner。
  uploadCsv: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const raw = await request<RawImportPreviewResponse>('/imports/upload', {
      method: 'POST',
      body: formData,
    })
    return normalizeImportPreview(raw)
  },
  confirmImport: (file: File, conflictResolution: string, idemKey?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('conflict_resolution', conflictResolution)
    return request<import('@/types').ImportBatch>('/imports/confirm', {
      method: 'POST',
      body: formData,
      idemKey,
    })
  },
  getImportHistory: () => request<import('@/types').ImportBatch[]>('/imports/history'),
  getImportBackups: () => request<import('@/types').ImportBackup[]>('/imports/backups'),
  deleteImportBackup: (filename: string) => request<{ message: string }>(`/imports/backups/${filename}`, { method: 'DELETE' }),

  // Export
  downloadExport: (params: Record<string, string>, operator?: string) => {
    const queryParams = new URLSearchParams(params)
    if (operator) queryParams.append('operator', operator)
    return fetch(`${BASE}/exports/download?${queryParams}`).then(res => res.blob())
  },
  previewExport: (params: Record<string, string>) =>
    request<import('@/types').ExportPreviewResponse>(`/exports/preview?${new URLSearchParams(params)}`),

  // Proportions
  getProportions: (dimension: string, date: string) =>
    request<import('@/types').ProportionData>(`/proportions/by-dimension?dimension=${dimension}&snapshot_date=${date}`),
  getAllProportions: (date: string) =>
    request<import('@/types').AllProportionsData>(`/proportions/all?snapshot_date=${date}`),
  getAvailableDates: () => request<string[]>('/proportions/available-dates'),
  // 兼容旧代码的别名
  getProportionByDimension: (dimension: string, date?: string, level?: number, period?: string, periodType?: string) => {
    const params = new URLSearchParams({ dimension })
    if (date) params.append('snapshot_date', date)
    if (level !== undefined) params.append('level', level.toString())
    if (period) params.append('period', period)
    if (periodType) params.append('period_type', periodType)
    return request<import('@/types').ProportionData>(`/proportions/by-dimension?${params}`)
  },
  getProportionAvailablePeriods: (periodType: string) =>
    request<{ label: string; date: string; count: number }[]>(`/proportions/available-periods?period_type=${periodType}`),

  // Allocation
  getAllocationTargets: (dimension?: string) =>
    request<import('@/types').AllocationTarget[]>(dimension ? `/allocation/targets?dimension=${dimension}` : '/allocation/targets'),
  createAllocationTarget: (data: import('@/types').AllocationTargetCreate) =>
    request<import('@/types').AllocationTarget>('/allocation/targets', { method: 'POST', body: JSON.stringify(data) }),
  updateAllocationTarget: (id: number, data: Partial<import('@/types').AllocationTargetCreate>) =>
    request<import('@/types').AllocationTarget>(`/allocation/targets/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAllocationTarget: (id: number) => request<{ ok: boolean }>(`/allocation/targets/${id}`, { method: 'DELETE' }),
  analyzeAllocation: (dimension: string, date?: string) => {
    const params = new URLSearchParams({ dimension })
    if (date) params.append('snapshot_date', date)
    return request<import('@/types').AllocationAnalysis>(`/allocation/analysis?${params}`)
  },
  // 兼容旧代码的别名
  getAllocationAnalysis: (dimension: string, date?: string, period?: string, periodType?: string) => {
    const params = new URLSearchParams({ dimension })
    if (date) params.append('snapshot_date', date)
    if (period) params.append('period', period)
    if (periodType) params.append('period_type', periodType)
    return request<import('@/types').AllocationAnalysis>(`/allocation/analysis?${params}`)
  },
  validateAllocation: (dimension: string, targetId: string, targetPercent: number, parentId?: number) => {
    const params = new URLSearchParams({ dimension, target_id: targetId, target_percent: targetPercent.toString() })
    if (parentId !== undefined) params.append('parent_id', parentId.toString())
    return request<{ valid: boolean; message: string }>(`/allocation/validation?${params}`)
  },

  // Alerts
  getAlertRules: () => request<import('@/types').AlertRule[]>('/alerts/rules'),
  createAlertRule: (data: import('@/types').AlertRuleCreate) =>
    request<import('@/types').AlertRule>('/alerts/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateAlertRule: (id: number, data: Partial<import('@/types').AlertRuleCreate>) =>
    request<import('@/types').AlertRule>(`/alerts/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAlertRule: (id: number, audit?: AuditReasonInput) =>
    request<{ ok: boolean }>(`/alerts/rules/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),
  checkAlerts: () => request<import('@/types').AlertResult[]>('/alerts/results'),
  getAlertHistory: () => request<import('@/types').AlertResult[]>('/alerts/results'),
  // 兼容旧代码的别名
  getAlertPeriods: (periodType: string) =>
    request<import('@/types').AvailablePeriod[]>(`/proportions/available-periods?period_type=${periodType}`),
  getAlertResults: (period?: string) => {
    const params = period ? `?current_period=${period}` : ''
    return request<import('@/types').AlertResult[]>(`/alerts/results${params}`)
  },

  // Auto Export Rules
  getAutoExportRules: () => request<import('@/types').AutoExportRule[]>('/export-history/rules'),
  createAutoExportRule: (data: import('@/types').AutoExportRuleCreate) =>
    request<import('@/types').AutoExportRule>('/export-history/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateAutoExportRule: (id: number, data: Partial<import('@/types').AutoExportRuleCreate>) =>
    request<import('@/types').AutoExportRule>(`/export-history/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  // 删除属纠正类：缺 reason 后端 422（auditReasonQuery 无理由时为空串，老调用 URL 不变）
  deleteAutoExportRule: (id: number, audit?: AuditReasonInput) =>
    request<{ message: string }>(`/export-history/rules/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),
  validateCronExpression: (expression: string) =>
    request<import('@/types').CronValidationResponse>('/export-history/validate-cron', {
      method: 'POST',
      body: JSON.stringify({ cron_expression: expression }),
    }),

  // Export History
  getExportHistory: (params?: Record<string, string>) =>
    request<import('@/types').ExportHistoryList>(`/export-history/history?${new URLSearchParams(params || {})}`),

  // Backup Restore (V2)
  uploadDbFile: (file: File, idemKey?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    return request<{
      action: 'restore'
      success: boolean
      message: string
    }>('/backup/upload-db', {
      method: 'POST',
      body: formData,
      idemKey,
    })
  },

  // 上传备份文件恢复：.zip bundle（含凭证）或裸 .db，走 /backup/restore 文件通道
  restoreBackupFile: (file: File, idemKey?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    return request<{
      success: boolean
      message: string
      warnings?: string[]
    }>('/backup/restore', {
      method: 'POST',
      body: formData,
      idemKey,
    })
  },

  // Screenshot Entry（截图录入）—— 来源组管理（快速 DB 操作，走相对路径 /api）
  getSourceGroups: () => request<import('@/types').SourceGroup[]>('/source-groups'),
  createSourceGroup: (data: Partial<import('@/types').SourceGroup>) =>
    request<import('@/types').SourceGroup>('/source-groups', { method: 'POST', body: JSON.stringify(data) }),
  updateSourceGroup: (id: number, data: Partial<import('@/types').SourceGroup>) =>
    request<import('@/types').SourceGroup>(`/source-groups/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSourceGroup: (id: number, audit?: AuditReasonInput) =>
    request<{ ok: boolean }>(`/source-groups/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),
  getRecommendSourceGroup: (filename: string) =>
    request<import('@/types').SourceGroupRecommend>(`/source-groups/recommend?filename=${encodeURIComponent(filename)}`),
  // 解析规则保存（原子端点，返回更新后的 SourceGroup）：后端只 merge
  // layout / amount_col / amount_header / ignore_words 四键，null/[] = 删键
  // 回落内置默认；其余键（校正规则/历史键）原样保留
  updateParseRules: (
    id: number,
    data: {
      layout?: string | null
      amount_col?: number | null
      amount_header?: string[] | null
      ignore_words?: string[] | null
    },
  ) =>
    request<import('@/types').SourceGroup>(`/source-groups/${id}/parse-rules`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  // 指引示例图上传（multipart 字段 file：png/jpg/jpeg/webp ≤5MB）；成功后
  // guide_image = /api/static/guide-images/{filename}（根相对 URL，可直接 <img src>）
  uploadGuideImage: (id: number, file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return request<import('@/types').SourceGroup>(`/source-groups/${id}/guide-image`, {
      method: 'POST',
      body: formData,
    })
  },

  // LLM 供应商配置（共享契约：/api/llm-providers，快速 DB 操作，走相对路径 /api）
  getLlmProviders: () => request<import('@/types').LlmProvider[]>('/llm-providers'),
  createLlmProvider: (data: import('@/types').LlmProviderCreate) =>
    request<import('@/types').LlmProvider>('/llm-providers', { method: 'POST', body: JSON.stringify(data) }),
  updateLlmProvider: (id: number, data: import('@/types').LlmProviderUpdate) =>
    request<import('@/types').LlmProvider>(`/llm-providers/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLlmProvider: (id: number, audit?: AuditReasonInput) =>
    request<{ ok: boolean }>(`/llm-providers/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),
  // 连通性测试：无 body；400/404 走 request 异常分支抛 Error
  testLlmProvider: (id: number) =>
    request<import('@/types').LlmProviderTestResult>(`/llm-providers/${id}/test`, { method: 'POST' }),

  // Screenshot Entry（截图录入）—— preview/apply/verify 必须走 SCREENSHOT_BASE（OCR 可长达 4 分钟，LLM 校验同样慢）
  // sourceGroup 必填：渠道由调用方（截图页选中的启用组）显式决定，不设兜底 slug
  // engine（peer-entry-design §5）：'ai' = AI 主识别（跳过 OCR，空骨架 + keep 行）；
  // 缺省 / 'ocr' 不 append 该字段 —— OCR 路径请求载荷与既往逐字节一致
  screenshotPreview: (file: File, sourceGroup: string, unrecognized: 'keep' | 'all' = 'keep', engine?: 'ocr' | 'ai', recognition?: unknown) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('source_group', sourceGroup)
    formData.append('unrecognized', unrecognized)
    if (engine === 'ai') formData.append('engine', 'ai')
    // 识别快照回传（ADR-0023 D7）：仅 AI 路径、有识别行时才有值；OCR 路径恒不 append
    appendRecognition(formData, recognition)
    return request<ScreenshotPreviewResponse>(`${SCREENSHOT_BASE}/screenshots/preview`, { method: 'POST', body: formData })
  },
  // recognition（ADR-0023 D7）：AI 主识别图的**机器识别行**（name/amount 数组），随 apply
  // 一并回传落快照；无值不 append（后端记 items:null + note:"client-side"），OCR 路径绝不传
  screenshotApply: (file: File, opts: {
    source_group?: string
    skipped: number[]
    amounts: Record<string, string>
    unrecognized?: 'keep' | 'all'
    zero_names?: string[]
    names?: Record<string, string>
    // 补录项（AI 额外识别 → 用户手动加入）：只发 name/amount/side，行号通道之外独立一条；
    // 无补录时不带该键（与今天逐字段等价，后端缺省 = []）
    supplements?: import('@/types').SupplementResolveItem[]
    // AI 主识别图（peer-entry-design §5）：跳过 OCR，anchor=null，日期=用户指定或今天；
    // OCR 图不带该键 —— 载荷与既往逐字段一致
    engine?: 'ai'
    // 身份裁决（D4）：键＝预览行 index，只含有 identity_guess 且用户已表态的行；
    // 无裁决时不带该键（旧后端零影响）
    identity_decisions?: Record<string, import('@/types').IdentityDecisionPayload>
    // 汇总行（ADR-0029 D5）：本次勾为「只核对、不入账」的行号，与 skipped 同形；
    // 无汇总行时不带该键（载荷与既往逐字段一致）
    reconciles?: number[]
    // 批次成员（ADR-0029 D8）：POST /screenshot-batches 拿到的 id；成功后后端写成员行。
    // 缺省＝旧单图语义，行为与本期之前逐字段一致
    batch_id?: number
  }, dataDate?: string, idemKey?: string, recognition?: unknown) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('options', JSON.stringify(opts))
    // 顶层 data_date：用户确认的数据日期（YYYY-MM-DD）。不传（undefined/空）= 后端旧语义（识别日期 or 今天）
    if (dataDate) formData.append('data_date', dataDate)
    // 识别快照回传（ADR-0023 D7）：AI 路径把机器识别行带上；无值/OCR 路径不 append
    appendRecognition(formData, recognition)
    return request<ScreenshotApplyResult>(`${SCREENSHOT_BASE}/screenshots/apply`, { method: 'POST', body: formData, idemKey })
  },
  // AI 一致性校验（第二意见）：multipart(file, rows, provider_id?)，与 preview/apply 同走 SCREENSHOT_BASE
  // providerId 缺省时后端用 is_default 且 enabled 的供应商；显式传入则临时用该供应商
  verifyScreenshot: (formData: FormData, providerId?: number | null) => {
    if (providerId != null) formData.append('provider_id', String(providerId))
    return request<import('@/types').VerifyResponse>(`${SCREENSHOT_BASE}/screenshots/verify`, { method: 'POST', body: formData })
  },
  // 补录预判定（只读，契约 §7）：POST /api/screenshots/supplements/resolve
  // body {source_group, supplements:[{name,amount,side}]} → 逐项、与请求同序的判定结论
  // 只查库 + 复用后端同一套 match_existing/apply_supplements 判定，不跑 OCR、不落库 ⇒ 快接口，走相对 /api。
  // 用途：写入前把「将更新谁 / 将新增什么 / 为什么冲突」摆给用户看（补录不是盲写）；apply 时后端仍权威重算
  resolveSupplements: (sourceGroup: string, supplements: import('@/types').SupplementResolveItem[]) =>
    request<import('@/types').SupplementResolveResult[]>('/screenshots/supplements/resolve', {
      method: 'POST',
      body: JSON.stringify({ source_group: sourceGroup, supplements }),
    }),
  // 单条凭证详情（record-provenance D7）：返回体 = 列表项 ∪ `{file_sha256}`
  // （票 06 已把后端收敛成同源构造）。凭证详情页与截图页的 `EvidencePanel` 共用这一个方法。
  getEvidence: (id: number) =>
    request<ScreenshotEvidenceInfo>(`/screenshots/evidence/${id}`),

  // --- 凭证管理（evidence-manager spec D1/D2/D5；票 04）---
  // 列表：分页 + 来源组/日期/使用状态/关键字筛选，排序由后端固定 id 倒序。
  // 快速 DB 操作 → 走相对 /api（与 getExpenses 同族；不走 SCREENSHOT_BASE 的 OCR 长超时通道）。
  listEvidence: (params: {
    source_group_id?: number
    date_from?: string
    date_to?: string
    usage?: 'all' | 'used' | 'unused'
    q?: string
    page?: number
    page_size?: number
  }) => {
    const searchParams = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') searchParams.append(key, String(value))
    })
    const qs = searchParams.toString()
    return request<import('@/types').EvidenceListResponse>(`/screenshots/evidence${qs ? `?${qs}` : ''}`)
  },
  // 原图 URL（供 <img src> / <a href> 直接使用，不是 fetch 调用）：默认 inline，
  // 自行在末尾拼 `?download=1` 即得附件下载地址（spec D2）。
  evidenceImageUrl: (id: number) => `${BASE}/screenshots/evidence/${id}/image`,
  // 删除单张凭证：未使用 → 2xx；已使用 → 409（ApiError.details 含引用计数）；不存在 → 404。
  // 响应体不使用（204 与 JSON 都可），由 request 兜底。
  deleteEvidence: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/screenshots/evidence/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),
  // AI 识别结果回写（evidence-recognition-writeback D1/D2/D6）：把 AI 给出的识别行补进
  // 该凭证快照的 `recognized` 段（只这一段；`adopted` 段只由 /apply 写）。
  // 用于「只预览不采纳」的 AI 会话——提交 preview job 时识别还没跑，不补写就永远是 null。
  // 载荷与 /apply 的 recognition 同构（数组或 {items,total,date_text}），此处传数组。
  // 快速 DB 操作 → 走相对 /api；失败由调用方静默（旁路回写不阻断识别流程）。
  writebackEvidenceRecognition: (id: number, recognition: unknown) =>
    request<{ ok: boolean; saved: boolean; evidence_id: number; recognized_items: number | null }>(
      `/screenshots/evidence/${id}/recognition`,
      { method: 'PATCH', body: JSON.stringify({ recognition }) },
    ),
  // 识别快照**本体**（evidence-recognition-view spec D2）：只读，不写库不写审计。
  // 列表接口只给摘要 `recognition_summary`，故详情弹层默认不调它：
  // 用户点「查看识别行」才发一次（`useQuery({ enabled })` 懒加载）。
  // 行在但从未识别 → 200 + `snapshot: null`（合法状态）；行不存在 → 404 EVIDENCE_NOT_FOUND。
  getEvidenceRecognition: (id: number) =>
    request<import('@/types').EvidenceRecognitionResponse>(
      `/screenshots/evidence/${id}/recognition`,
    ),
  // 批量清理未使用凭证（spec D5）：**dry_run 默认 true**，前端必须两段式——
  // 先 dry_run=true 出示清单与总字节，用户确认后才发 dry_run=false。
  // 幂等键**每次调用各生成一个**：dry_run 两次请求体不同，复用同一个键会让中间件
  // 直接回放第一段（统计）的响应，历史 bug 面很大。
  purgeUnusedEvidence: (body: { source_group_id?: number; dry_run: boolean; limit?: number }, audit?: AuditReasonInput) =>
    request<import('@/types').EvidencePurgeResult>(`/screenshots/evidence/purge-unused${auditReasonQuery(audit)}`, {
      method: 'POST',
      body: JSON.stringify(body),
      idemKey: newIdemKey(),
    }),
  // 异步 OCR 预览：提交秒回 job_id（202），前端轮询进度；提交与轮询均为快接口，
  // 走相对 /api（长耗时发生在后端 worker，不占 HTTP 连接，无需 SCREENSHOT_BASE 直连）
  // forceOcr=true：本次忽略后端 OCR 缓存重跑并刷新缓存（不传 = 后端默认走缓存）；
  // 用于 OCR 与 AI 校验出现单边缺项/分歧时，对单张截图重新识别复核。响应结构与轮询接口不变
  // engine='ai'（peer-entry-design §5）：AI 主识别，job 跳过 OCR 秒级完成；缺省/'ocr' 不 append（载荷不变）
  // recognition（ADR-0023 D7）：AI 路径前端回传的机器识别行，走与 /apply 同一个
  // appendRecognition() 口径（只在有值时 append；未传 → 后端记 items:null + note）。
  // 后端对非法 JSON 只记 warning 不报错（预览是主路径），但前端仍只发合法结构。
  createPreviewJob: (file: File, sourceGroup: string, forceOcr: boolean = false, engine?: 'ocr' | 'ai', recognition?: unknown) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('source_group', sourceGroup)
    if (forceOcr) formData.append('force_ocr', 'true')
    if (engine === 'ai') formData.append('engine', 'ai')
    appendRecognition(formData, recognition)
    // v5 步骤 6 存证前置：响应 additive evidence_id（上传即建凭证，预览阶段即可引用）
    return request<{ job_id: string; evidence_id?: number | null }>('/screenshots/preview-jobs', { method: 'POST', body: formData })
  },
  getPreviewJob: (jobId: string) =>
    request<import('@/types').PreviewJobStatus>(`/screenshots/preview-jobs/${encodeURIComponent(jobId)}`),
  // --- 截图批次（ADR-0029 D8/D9）---
  // 建壳：拿到 batch_id 后逐图 apply 带上它，后端才写得进成员行；finalize 才有「成员是否齐」
  // 可校验。幂等键由调用方持有：一次写入动作一个键，重试复用（避免建出两个空批次）。
  // 都是快速 DB 操作 → 走相对 /api（与凭证管理同族，不走 OCR 长超时通道）。
  createScreenshotBatch: (
    body: { data_date?: string | null; source_group?: string | null; expected_members: number; note?: string },
    idemKey?: string,
  ) =>
    request<import('@/types').ScreenshotBatchCreateResult>('/screenshot-batches', {
      method: 'POST',
      body: JSON.stringify(body),
      idemKey,
    }),
  // 结算：唯一校验点（成员齐 / 差额处理完 / 构成可建）。409 的业务码由页面翻成中文并指到具体行。
  // 幂等键同样由调用方持有：写入已成功、只是结算失败时重试要回放同一结果，不能重复建构成边。
  finalizeScreenshotBatch: (
    id: number,
    body: import('@/types').ScreenshotBatchFinalizeBody,
    idemKey?: string,
  ) =>
    request<import('@/types').ScreenshotBatchFinalizeResult>(
      `/screenshot-batches/${id}/finalize`,
      { method: 'POST', body: JSON.stringify(body), idemKey },
    ),
  // 把用户对噪声行的修正/跳过固化成该来源组的可复用规则（快速 DB 操作，走相对路径）
  // note 为可选备注（只给人看，不参与匹配）；不传时后端保持旧存储格式
  // exclude=「不录入」：match 为资产全名（含前缀）精确匹配，仅过滤未识别(keep)列表
  saveCorrectionRule: (slug: string, data: { type: 'rename' | 'ignore' | 'exclude'; match: string; replace?: string; note?: string }) =>
    request<import('@/types').SourceGroup>(
      `/screenshots/source-groups/${encodeURIComponent(slug)}/correction-rules`,
      { method: 'POST', body: JSON.stringify(data) },
    ),
  // 校正规则原子 CRUD（与 saveCorrectionRule 同族；均为快速 DB 操作走相对路径）。
  // 服务端在事务内定位单条并改写 rules JSON，消除前端「整份 PUT」的并发覆盖窗口。
  // 编辑：body = 旧身份 {type, match, replace?, note?} + 新值 {new_match?, new_replace?, new_note?}；
  // new_note 空串 = 删除备注。删除：body = {type, match, replace?}。
  // 排序：POST …/swap，body = {type, a:{match, replace?}, b:{match, replace?}}（rename 相邻交换）。
  patchCorrectionRule: (
    slug: string,
    data: {
      type: 'rename' | 'ignore' | 'exclude'
      match: string
      replace?: string
      note?: string
      new_match?: string
      new_replace?: string
      new_note?: string
    },
  ) =>
    request<import('@/types').SourceGroup>(
      `/screenshots/source-groups/${encodeURIComponent(slug)}/correction-rules`,
      { method: 'PATCH', body: JSON.stringify(data) },
    ),
  deleteCorrectionRule: (
    slug: string,
    data: { type: 'rename' | 'ignore' | 'exclude'; match: string; replace?: string },
    audit?: AuditReasonInput,
  ) =>
    request<import('@/types').SourceGroup>(
      `/screenshots/source-groups/${encodeURIComponent(slug)}/correction-rules${auditReasonQuery(audit)}`,
      { method: 'DELETE', body: JSON.stringify(data) },
    ),
  swapCorrectionRules: (
    slug: string,
    data: {
      type: 'rename' | 'ignore' | 'exclude'
      a: { match: string; replace?: string }
      b: { match: string; replace?: string }
    },
  ) =>
    request<import('@/types').SourceGroup>(
      `/source-groups/${encodeURIComponent(slug)}/correction-rules/swap`,
      { method: 'POST', body: JSON.stringify(data) },
    ),

  // --- 支出录入（expense-entry，契约：.scratch/expense-entry/spec.md §4/§6.2） ---
  // 快速 DB 操作，走相对路径 /api；金额请求/响应均为字符串，204 → request 内已兜底 undefined

  // 支出分类（独立维度 CRUD；删除被引用分类 → 400，detail 为中文说明）
  getExpenseCategories: () => request<import('@/types').ExpenseCategory[]>('/expense-categories'),
  createExpenseCategory: (data: import('@/types').ExpenseCategoryCreate) =>
    request<import('@/types').ExpenseCategory>('/expense-categories', { method: 'POST', body: JSON.stringify(data) }),
  updateExpenseCategory: (id: number, data: import('@/types').ExpenseCategoryUpdate) =>
    request<import('@/types').ExpenseCategory>(`/expense-categories/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteExpenseCategory: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/expense-categories/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 固定支出模板（删除模板 → 已生成记录解绑 template_id，不删支出）
  getExpenseTemplates: (params?: { active?: boolean }) => {
    const query = params?.active !== undefined ? `?active=${params.active}` : ''
    return request<import('@/types').FixedExpenseTemplate[]>(`/expense-templates${query}`)
  },
  createExpenseTemplate: (data: import('@/types').FixedExpenseTemplateCreate) =>
    request<import('@/types').FixedExpenseTemplate>('/expense-templates', { method: 'POST', body: JSON.stringify(data) }),
  updateExpenseTemplate: (id: number, data: import('@/types').FixedExpenseTemplateUpdate) =>
    request<import('@/types').FixedExpenseTemplate>(`/expense-templates/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteExpenseTemplate: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/expense-templates/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 支出记录（分页列表：expense_date DESC, id DESC；undefined/空值不进 query）
  getExpenses: (params: {
    page?: number
    page_size?: number
    expense_type?: import('@/types').ExpenseType
    category_id?: number
    account_id?: number
    date_from?: string
    date_to?: string
    name?: string
  }) => {
    const searchParams = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') searchParams.append(key, String(value))
    })
    const qs = searchParams.toString()
    return request<import('@/types').PaginatedExpenses>(`/expenses${qs ? `?${qs}` : ''}`)
  },
  createExpense: (data: import('@/types').ExpenseRecordCreate) =>
    request<import('@/types').ExpenseRecord>('/expenses', { method: 'POST', body: JSON.stringify(data) }),
  updateExpense: (id: number, data: import('@/types').ExpenseRecordUpdate) =>
    request<import('@/types').ExpenseRecord>(`/expenses/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteExpense: (id: number, audit?: AuditReasonInput) =>
    request<void>(`/expenses/${id}${auditReasonQuery(audit)}`, { method: 'DELETE' }),

  // 一键生成当月固定支出（预览 → 生成；(template_id, period) 幂等）与汇总
  getExpenseGeneratePreview: (period: string) =>
    request<import('@/types').ExpenseGeneratePreview>(`/expenses/generate-preview?period=${encodeURIComponent(period)}`),
  generateFixedExpenses: (data: import('@/types').ExpenseGenerateRequest) =>
    request<import('@/types').ExpenseGenerateResult>('/expenses/generate', { method: 'POST', body: JSON.stringify(data) }),
  getExpenseSummary: (params?: { year?: number; month?: number }) => {
    const searchParams = new URLSearchParams()
    if (params?.year !== undefined) searchParams.append('year', String(params.year))
    if (params?.month !== undefined) searchParams.append('month', String(params.month))
    const qs = searchParams.toString()
    return request<import('@/types').ExpenseSummary>(`/expenses/summary${qs ? `?${qs}` : ''}`)
  },
}

// ===== 取证视图（票 02）：资产项操作历史 / 全局操作日志 / 链状态 =====
//
// 契约裁定见 `.scratch/audit-best-practice/issues/02-forensics-view.md` 的 Answer。
// 术语三分：`source==='audit'` 的行是**操作记录**，`source==='trace'` 的行是**数据自述**
// （来自数据内痕迹），改名史叫**曾用名**。合并、排序、去重、中文 `title` 全部由后端出，
// 前端只做展示——别在这里第二次映射词表，否则两个页面必然漂移。

/** 一行的来源：审计行（操作记录）或数据内痕迹（数据自述）。 */
export type AuditSource = 'audit' | 'trace'

/** 单个字段的变化：`from`/`to` 是已排版好的展示串（金额仍是十进制字符串）。 */
export interface AuditChange {
  field: string
  label: string
  from: string | null
  to: string | null
}

export interface AuditEntry {
  source: AuditSource
  /** 后端本地时间（不带时区），如 `2026-11-07T12:20:45.896204`。 */
  occurred_at: string
  kind: string
  /** 后端给的中文动作名（「更正」「新建」…）：前端不自造词。 */
  title: string
  actor: string
  /** 「我」/「系统」；`actor` 原值走 title 提示。 */
  actor_label: string
  row_id: number
  /** 来源凭证（`screenshot_evidence.id`，库内 INTEGER 外键）；无凭证为 null。 */
  evidence_ref: number | null
  note: string | null
  changes: AuditChange[]
  /** 排障用字段：entity/action/request_id/idempotency_key/prev_hash/…… */
  tech: Record<string, string | null>
}

export interface ItemHistoryPage {
  item_id: number
  display_name: string
  entries: AuditEntry[]
  /** 不透明游标：原样回传给下一次请求；null＝没有更早的记录。 */
  next_cursor: string | null
  truncated: boolean
}

/**
 * 一个资产项的完整变更史（审计行 ∪ 数据内痕迹，后端合并成一条时间线）。
 *
 * - `limit` 默认 50（首屏）；
 * - `cursor` 不透明，原样透传，不要解析或重排；
 * - `sources` 默认 `audit,trace`；只看操作时传 `audit`。
 */
export async function getItemHistory(
  itemId: number,
  opts: { limit?: number; cursor?: string | null; sources?: string } = {},
): Promise<ItemHistoryPage> {
  const search = new URLSearchParams()
  search.set('limit', String(opts.limit ?? 50))
  search.set('sources', opts.sources ?? 'audit,trace')
  // 游标可能含 `/`、`+`、`=` 等字符：交给 URLSearchParams 编码，不要手拼
  if (opts.cursor) search.set('cursor', opts.cursor)
  return request<ItemHistoryPage>(`/asset-items/${itemId}/history?${search.toString()}`)
}

/** 全局操作日志的筛选（设置页用）。空值一律不进 query。 */
export interface AuditLogQuery {
  entity?: string
  action?: string
  row_id?: number
  evidence_ref?: string
  actor?: string
  request_id?: string
  /** 精确匹配；幂等键 7 天 TTL 之后该值为悬空指针（历史事实，不清空） */
  idempotency_key?: string
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
  order?: 'asc' | 'desc'
}

/** 全局操作日志（设置页用；行形状与抽屉里的条目同源）。 */
export async function getAuditLog(
  q: AuditLogQuery,
): Promise<{ items: AuditEntry[]; total: number; page: number; page_size: number }> {
  const search = new URLSearchParams()
  Object.entries(q).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.append(key, String(value))
  })
  const qs = search.toString()
  return request<{ items: AuditEntry[]; total: number; page: number; page_size: number }>(
    `/audit-log${qs ? `?${qs}` : ''}`,
  )
}

/** 链状态（库外锚对账结论）：五值取自后端，前端不推断。 */
export interface AuditIntegrity {
  status: 'match' | 'db_newer' | 'db_older' | 'diverged' | 'unknown'
  audit_head: string | null
  audit_rows: number
  seals?: { last_seal_id: number | null; max_id: number; lag: number }
  anchors?: unknown[]
}

/** 审计链与库外锚的对账结论（设置页顶部的链状态徽标）。 */
export async function getAuditIntegrity(): Promise<AuditIntegrity> {
  return request<AuditIntegrity>('/audit-log/integrity')
}

// ===== 项级备注读回（ADR-0031 D2/D4；票 14）=====
//
// 备注变更**只落审计**（不进 `asset_item_attr_history`，`assets` 也没有该列），
// 但审计是变更流而不是当前值（审计被清理/直接改库都会读空），所以当前值由业务列读：
// `GET /api/asset-items` 每项回传 `note`（`null`＝没写）+ `updated_at`。
//
// 契约：**一次请求**取回全部项备注（无按项循环＝无 N+1）；返回 `Map<item_id, note>`，
// `null` 与「不在 map 里」在调用侧同样表达「没备注」。写入成功后调用侧会把新值
// 直接塞进这份缓存（records 页），不必立刻重取。

/** 资产项列表每项的备注（`GET /api/asset-items` 的 `items[].note`；空串后端已归一为 `null`）。 */
export async function fetchItemNotes(): Promise<Map<number, string | null>> {
  const { items } = await api.listAssetItems(true)
  const notes = new Map<number, string | null>()
  for (const item of items) {
    const value = item.note
    notes.set(item.id, typeof value === 'string' && value.trim() !== '' ? value : null)
  }
  return notes
}

// ===== 归档项说明（录入侧关联选择器，spec `.scratch/archived-ingest-scope/spec.md` D5）=====
//
// 选择器要能回答「这个名字已归档、并入了谁」。`GET /api/asset-items` 的行形状只有 `status`，
// 归档生效日与 merge 去向都在血缘侧，因此只用**既有**端点取，不为此新增后端接口：
// `GET /api/lineage-links/family?item_id=` 的 members 里，`relationship='self'` 的
// `effective_from` 即该归档项的归档生效日，`link_type='merge'` 的后继成员即并入目标。
//
// 该端点按项查询（无批量参数）→ 按项并发；单项失败只降级成「不知道日期/去向」，
// 不抛错也不阻塞其它项：选择器在旧后端 / 链缺失时仍要可用。

/** 批量取归档项的说明（键＝item_id）；调用方按 `hint.archived_effective_from`/`merged_into` 空值降级。 */
export async function fetchArchivedItemHints(
  itemIds: number[],
): Promise<Map<number, import('@/types').ArchivedItemHint>> {
  const uniqueIds = Array.from(new Set(itemIds))
  const pairs = await Promise.all(uniqueIds.map(async (itemId) => {
    const fallback: import('@/types').ArchivedItemHint = {
      archived_effective_from: null,
      merged_into: [],
    }
    try {
      const family = await api.getLineageFamily(itemId)
      const self = family.members.find((member) => member.item_id === itemId)
      const mergedInto = family.members
        .filter((member) => member.relationship === 'successor' && member.link_type === 'merge')
        .map((member) => member.name)
      const hint: import('@/types').ArchivedItemHint = {
        archived_effective_from: self?.effective_from ?? null,
        merged_into: Array.from(new Set(mergedInto)),
      }
      return [itemId, hint] as [number, import('@/types').ArchivedItemHint]
    } catch {
      // 血缘读不到（旧后端 / 网络抖动）不影响选择器：只显示「已归档」，不显示日期与去向
      return [itemId, fallback] as [number, import('@/types').ArchivedItemHint]
    }
  }))
  return new Map(pairs)
}
