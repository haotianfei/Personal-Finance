// ===== 分类字典（ADR-0019，`/api/classification/*`）=====
// 分类只存在于**资产项**上：记录侧只读回显，字典是唯一真相源。
// 旧 EAV（attributes 表 + type=fund_type/account/liquidity_rating/owner）已整体下线。

/** 资产类别树节点（平铺返回，树由调用方按 parent_id 拼）。 */
export interface AssetClass {
  id: number
  code: string
  name: string
  parent_id: number | null
  level: number
  side_scope: 'asset' | 'liability' | 'any'
  /** 全路径，如 `"B 固定收益/B1 货币基金"`。 */
  path: string
  /** 无子节点＝叶子（只有叶子可挂资产项，由数据库触发器守）。 */
  leaf: boolean
}

/** 资产类别创建/更新入参（code 缺省由后端按层级自动编号）。 */
export interface AssetClassInput {
  name: string
  parent_id?: number | null
  code?: string | null
  side_scope?: 'asset' | 'liability' | 'any' | null
}

/**
 * 资产类别**写接口**的返回：只有这六个字段。
 * 后端 `create_asset_class` / `update_asset_class` 不回 `path` / `leaf`
 * （它们由列表端点现算），所以调用方写完必须重新拉列表，别拿返回值当完整节点用。
 */
export type AssetClassWriteResult = Omit<AssetClass, 'path' | 'leaf'>

/** 流动性档位：只读 8 档；`days` 是权威值，`label` 是展示派生。 */
export interface LiquidityLevel {
  days: number
  label: string
}

export type InstitutionKind =
  | 'bank' | 'broker' | 'fund' | 'insurer' | 'gov' | 'platform' | 'other'

/** 机构（银行/券商/基金/保险/政府/平台/其他）。 */
export interface Institution {
  id: number
  name: string
  kind: InstitutionKind
  active: boolean
}

/** 渠道（App / 子钱包 / 代销口）——与「机构」是两个独立维度。 */
export interface Channel {
  id: number
  name: string
  active: boolean
}

/** 成员角色；共有归属用 `role='joint'` 的成员表达（旧 `family_pool` 已退役）。 */
export type MemberRole = 'self' | 'spouse' | 'joint' | 'child' | 'nominee'

/** 家庭成员（归属维度的唯一值域）。 */
export interface Member {
  id: number
  name: string
  role: MemberRole
  active: boolean
}

/** 标签字典项（controlled＝受控词表 / free＝自由词）。 */
export interface TagItem {
  id: number
  name: string
  kind: 'controlled' | 'free'
}

/** 标签引用（记录/当前值内联回显）。 */
export interface TagRef {
  id: number
  name: string
}

/**
 * 必填分类维度缺哪几个（draft 项补齐即转 active）。
 *
 * **后端给的是列名**（`missing_dims()` 直接取 REQUIRED_DIMS，见
 * `classification_service.missing_dims`），所以线上真实取值是
 * `asset_class_id / liquidity_days / institution_id / owner_id`。
 * 旧写法（无 `_id`/`_days` 后缀）从未在后端出现过，为了不静默丢字，
 * 两种形态都收进来，展示层统一用 `dimLabel()`。
 */
export type ClassificationDim =
  | 'asset_class'
  | 'liquidity'
  | 'institution'
  | 'owner'
  | 'asset_class_id'
  | 'liquidity_days'
  | 'institution_id'
  | 'owner_id'

/** 资产项分类读写视图（`GET|PUT /api/asset-items/{id}/classification`）。 */
export interface ItemClassification {
  item_id: number
  display_name: string
  status: 'active' | 'draft' | 'archived'
  asset_class_id: number | null
  asset_class_name: string | null
  asset_class_code: string | null
  liquidity_days: number | null
  liquidity_label: string | null
  institution_id: number | null
  institution_name: string | null
  channel_id: number | null
  owner_id: number | null
  owner_name: string | null
  risk_level: string | null
  /** 该项当前标签（后端 `classification_of` 会带上）。 */
  tags?: TagRef[]
  /** 该端点用 `missing_dims` 作键；记录/当前值端点的同一个语义叫 `classification_missing`。 */
  missing_dims?: ClassificationDim[]
  classification_missing?: ClassificationDim[]
}

export interface ItemClassificationUpdate {
  asset_class_id?: number
  liquidity_days?: number
  institution_id?: number
  /** 可选维度：显式传 `null` ＝清空（后端 `exclude_unset` 支持）。 */
  channel_id?: number | null
  owner_id?: number
  risk_level?: string | null
  reason?: string
}

// 资产记录（分类在资产项上，记录侧只读回显）
export interface AssetRecord {
  id: number
  // v5 身份层（spec §5）：快照行归属的资产项 id + 该项当前显示名
  // asset_name 保留＝写入时显示名快照（时点语义，改名不回写历史行）
  asset_item_id: number | null
  asset_name: string
  display_name: string
  amount: string
  side: 'asset' | 'liability'
  asset_date: string
  source_evidence_id?: number | null
  // v5 更正链（列表 include_superseded 时含被更正历史行）：非空＝已被更正，指向承接新行
  superseded_by?: number | null
  is_superseded?: boolean
  // ---- 分类（项级只读回显） ----
  asset_class_id: number | null
  asset_class_name: string | null
  asset_class_code: string | null
  /** 全路径，如 `"B 固定收益/B1 货币基金"`。 */
  asset_class_path: string | null
  liquidity_days: number | null
  liquidity_label: string | null
  institution_id: number | null
  institution_name: string | null
  channel_id: number | null
  channel_name: string | null
  owner_id: number | null
  owner_name: string | null
  /** 归属成员的角色（members.role）；未标注为 null。 */
  owner_kind: MemberRole | null
  risk_level: string | null
  tags: TagRef[]
  classification_missing: ClassificationDim[]
  created_at: string
  updated_at: string
}

// 创建资产记录（账本事实；分类不在记录上，改分类走资产项端点）
export interface AssetRecordCreate {
  asset_date: string
  asset_name: string
  amount: string
  side?: 'asset' | 'liability'
  /** 工作台/复制写入的稳定身份；名称仅作兼容回退。 */
  asset_item_id?: number | null
}

// 更新资产记录（账本事实 write-once；分类字段会被后端 422）
export interface AssetRecordUpdate {
  asset_date?: string
  asset_name?: string
  amount?: string
  side?: 'asset' | 'liability'
}

// 批量更新资产记录（仅 side 可批量；amount/date/name 走单条更正/改名）
export interface BatchUpdateAssets {
  ids: number[]
  asset_name?: string
  amount?: string
  side?: 'asset' | 'liability'
}

export interface AssetHistoryByNameResponse {
  total_count: number
  sample_records: AssetRecord[]
  all_ids: number[]
}

// v5 身份层：资产项（/api/asset-items，spec §5）
export interface AssetItemInfo {
  id: number
  display_name: string
  channel: string | null
  /** draft＝分类未补齐（ADR-0019 D9）。 */
  status: 'active' | 'draft' | 'archived'
  legacy_name?: string | null
  aliases: string[]
  /**
   * 项级备注（ADR-0031 D2；`null`＝没写，trim 后空串落 NULL）。
   *
   * 列表端点**直接回传该列**（票 14）：`lib/api.ts::fetchItemNotes` 一次请求取回
   * 全部项备注，不再从 `note_update` 审计行派生——审计是变更流，不是当前值。
   */
  note?: string | null
  /** 最近更新时间（备注/改名/分类等写入都会推它；`null`＝未知）。 */
  updated_at?: string | null
}

// ===== 项级备注（ADR-0031 D2/D3，票 09/11）=====

/**
 * 备注写请求（`PUT /api/asset-items/{item_id}/note`）：整条替换，`note:null`/空串＝清空。
 * 长度上限 500 字符（Unicode 码点，trim 后计；后端 `schemas.py::NOTE_MAX`）。
 */
export interface AssetItemNoteUpdate {
  /** 必填键（值可为 null）：漏传＝调用方写错，后端 422。 */
  note: string | null
  /** 审计原因（封闭枚举，ADR-0027）；缺省＝审计行不记原因，不阻塞写入。 */
  reason?: string | null
  reason_note?: string | null
}

/** 备注写回执：回读值以服务端为准（trim 后；空即 null）。 */
export interface AssetItemNoteResponse {
  item_id: number
  note: string | null
  updated_at: string | null
}

// ===== 改名：原因 + 影响范围 + 预览确认（票 03 D1/D2/D11）=====
/** 三因：旧名写错（纠正）/ 同一资产改名（演进）/ 书写归一（空格、分隔符、全半角）。 */
export type RenameReason = 'correction' | 'evolution' | 'normalization'
/** 两效：只改当前名（历史快照保留旧名文本）/ 同时改写历史快照名（资产项 id 不变）。 */
export type RenameEffect = 'current' | 'history_backfill'

/** 改名预览请求：与执行请求同参，只是不带确认令牌。 */
export interface RenamePreviewRequest {
  name: string
  reason: RenameReason
  effect: RenameEffect
  reason_note?: string
}

/** 影响面：将改写的资产记录规模与跨度（current 时为 0 行跨度）。 */
export interface RenameImpact {
  rows: number
  first_date: string | null
  last_date: string | null
  superseded_rows: number
  archived_item: boolean
}

/** 预览回执：confirm_token 一次性、有有效期，改任一参数都要重新预览。 */
export interface RenamePreviewResponse {
  item_id: number
  current_name: string
  new_name: string
  reason: RenameReason
  effect: RenameEffect
  reason_note: string | null
  impact: RenameImpact
  confirm_token: string
  expires_at: string
}

/** 执行改名：必须带预览签发的令牌（缺原因/影响范围由后端 400 拒绝）。 */
export interface RenameRequest extends RenamePreviewRequest {
  confirm_token: string
}

export interface RenameResponse {
  /** 后端只回这四个字段（不含 aliases）；别名史靠 itemsQuery 重新拉。 */
  item: Pick<AssetItemInfo, 'id' | 'display_name' | 'channel' | 'status'>
  effect: RenameEffect
  /** 实际被改写名称的历史记录行数（current 时为 0）。 */
  backfill_rows: number
  name_history_id: number | null
  change: LedgerChange
}

// ===== 当前值读模型（GET /api/assets/current，spec §4.1） =====
// 派生视图：每个资产项截至参考日的最新有效记录。不是实体、不落库。
// 陈旧度口径（ADR-0019 D12）：不再有三态枚举，只给事实 + 一个 90 天阈值布尔。

export interface CurrentValueItem {
  item_id: number
  display_name: string
  side: 'asset' | 'liability'
  item_status: 'active' | 'draft' | 'archived'
  /** 参考日当时的最新有效记录；从未录入为 null。 */
  record_id: number | null
  asset_date: string | null
  /** 字符串小数；无记录为 null（≠ 0）。 */
  amount: string | null
  /** 该值来自哪一天（ISO 日期）；无记录为 null。 */
  valuation_date: string | null
  /** valuation_date 距参考日的天数；无记录为 null。 */
  valuation_age_days: number | null
  /** `valuation_age_days > 90`；无记录为 null（不把「没录过」混同「不陈旧」）。 */
  valuation_stale: boolean | null
  // ---- 分类（项级只读回显，与有无记录无关） ----
  asset_class_id: number | null
  asset_class_name: string | null
  asset_class_code: string | null
  asset_class_path: string | null
  liquidity_days: number | null
  liquidity_label: string | null
  institution_id: number | null
  institution_name: string | null
  channel_id: number | null
  channel_name: string | null
  owner_id: number | null
  owner_name: string | null
  owner_kind: MemberRole | null
  risk_level: string | null
  tags: TagRef[]
  classification_missing: ClassificationDim[]
  /**
   * 项级备注（ADR-0031 D2，`.scratch/current-value-note/spec.md` D1）。
   *
   * 与明细视图**同源同归一**：后端由 `asset_items.note` 批量读回，trim 后空串落
   * `null`（绝不回 `''`）。备注挂在项上，所以无记录行也可能有值。
   */
  note?: string | null
}

export interface CurrentValueResponse {
  as_of: string
  /** as_of 是否等于后端「今天」——决定前端编辑模式（auto / correct_only）。 */
  is_latest: boolean
  items: CurrentValueItem[]
}

// ===== 当前值就地写（PUT /api/assets/current/{item_id}，spec §4.2） =====
/** auto：当日无行则新建；correct_only：只更正当日已有行（历史视图）。 */
export type CurrentValueEditMode = 'auto' | 'correct_only'

export interface CurrentValueUpdateRequest {
  /** 十进制字符串（两位小数）；**永不传 number**。 */
  amount: string
  /** 缺省＝后端今天；只在历史视图显式传。 */
  as_of?: string
  mode: CurrentValueEditMode
  /** 仅「该资产项从未有记录」时生效，缺省 asset。 */
  side?: 'asset' | 'liability'
}

export interface CurrentValueUpdateResponse {
  kind: 'created' | 'corrected' | 'unchanged'
  record_id: number
  asset_date: string
  /** 十进制字符串。 */
  amount: string
  /** 撤销用；`unchanged` 与新增时为 null。 */
  previous_amount: string | null
  previous_record_id: number | null
}

// ===== v5 二期·2.1 本金口径与升值 =====
export interface PrincipalEntry {
  id: number
  amount: string            // 带符号：注入正/撤出负
  effective_date: string
  reason: string
  origin: 'external' | 'transfer' | 'lineage' | string  // 后两值 2.3/2.4 预留
  evidence_ref: number | null
  created_at?: string
}

export interface PrincipalHistoryResponse {
  item_id: number
  history: PrincipalEntry[]
  current: string | null    // 当前生效口径（史末解析；null＝未登记）
}

export interface PrincipalSetResponse {
  entry: PrincipalEntry
  current: string | null
  change: LedgerChange
}

export interface AppreciationItem {
  item_id: number
  display_name: string
  side: 'asset' | 'liability'
  market: string
  cost_basis: string | null
  appreciation: string | null   // 口径缺失时 null
  rate: string | null           // 口径 ≤0 或缺失 → null（RATE_UNAVAILABLE）
  degradations: Degradation[]
}

export interface AppreciationResponse {
  as_of: string | null
  portfolio: {
    market_total: string
    basis_total: string
    appreciation_total: string
    rate: string | null
    items_missing_principal: string[]
  }
  items: AppreciationItem[]
  meta: Meta
}

// ===== v5 二期·子步 2.2 TWR/XIRR 双口径收益（GET /analysis/returns，只读） =====

export interface ReturnsFlow {
  date: string
  amount: string          // Δ本金：注入正/撤出负
}

export interface ReturnsSubperiod {
  start: string
  end: string
  v0: string
  v1: string
  flows: ReturnsFlow[]
  return: string          // 该段 Modified Dietz 收益率（小数）
}

export interface ReturnsResponse {
  scope: 'portfolio' | 'item'
  item_id: number | null
  window: { start: string | null; end: string | null; period: string | null }
  twr: {
    period: {
      start: string | null
      end: string | null
      subperiods: ReturnsSubperiod[]
      period_return: string | null
    }
    annualized: string | null      // 不可算 → null＋RATE_UNAVAILABLE（绝不编数）
  }
  xirr: string | null
  assumptions: string[]
  meta: Meta
}

// ===== v5 二期·子步 2.3 转移事件（/transfer-events，登记＝两条口径变更同事务） =====

export interface TransferEvent {
  id: number
  from_item_id: number
  from_name: string
  to_item_id: number
  to_name: string
  amount: string            // 恒正；方向由 from/to 表达
  event_date: string        // 登记日＝快照日（R-34 注）
  reason: string
  evidence_ref: number | null
  created_at: string
}

export interface TransferCreateResponse {
  event: TransferEvent
  change: LedgerChange
}

export interface TransferListResponse {
  items: TransferEvent[]
}

/** SNAPSHOT_REQUIRED 409 的 details 形状（缺侧清单＋引导文案）。 */
export interface SnapshotRequiredDetails {
  missing_items: { item_id: number; display_name: string }[]
  event_date: string
  hint: string
}

// ===== v5 二期增强 净资产变动归因瀑布（GET /analysis/attribution，只读） =====

export interface AttributionSegment {
  key: 'external' | 'transfer' | 'endogenous' | string
  label: string
  amount: string
  inflows?: string           // external 段：正贡献合计
  outflows?: string          // external 段：负贡献合计（负值字符串）
  gross?: string             // transfer 段：单边的转移总额
  note?: string
}

export interface AttributionResponse {
  scope: 'portfolio' | 'item'
  item_id: number | null
  date_from: string | null
  date_to: string | null
  net_worth_start: string
  net_worth_start_date: string | null   // carry-forward 时回显实际取值快照日
  net_worth_end: string
  net_worth_end_date: string | null
  delta: string                          // 恒等式：delta ＝ Σ segments（精确）
  segments: AttributionSegment[]
  coverage: { principal_set_ratio: string; uncovered_items: string[] }
  assumptions: string[]
  meta: Meta
}

// ===== v5 二期·子步 2.4 血缘链接（/lineage-links，链不可变；改名不走血缘） =====

export interface LineageLinkView {
  id: number
  from_item_id: number
  from_name: string
  to_item_id: number
  to_name: string
  link_type: 'merge' | 'split' | 'revision' | string
  split_ratio: string | null            // 仅 split
  note: string | null
  created_at: string
}

export interface LineageCreateResponse {
  link: LineageLinkView
  links: LineageLinkView[]              // merge＝多旧项各一条
  entry: PrincipalEntry & { item_id: number }   // 承接行（origin='lineage'）
}

export interface LineageByItemResponse {
  item_id: number
  predecessors: LineageLinkView[]       // 前身（本项在 to 侧）
  successors: LineageLinkView[]         // 后继（本项在 from 侧）
}

export interface LineageAllResponse {
  items: LineageLinkView[]
}

// ===== 一对多血缘拆分（1→N，spec `.scratch/lineage-split/spec.md` §1 冻结契约） =====
// 父项 archived_effective_from == effective_date；子项各一条 split 链 + 承接行。
export interface LineageSplitTarget {
  name: string
  /** 十进制字符串，(0, 1]，Σ = 1 ± 1e-4。 */
  ratio: string
  /** 十进制字符串，两位小数，>= 0（人确认，不做合计约束）。 */
  principal: string
  note?: string | null
}

export interface LineageSplitRequest {
  from_item_id: number
  targets: LineageSplitTarget[]
  /** 缺省＝后端今天。 */
  effective_date?: string | null
  /** R-9 必填。 */
  reason: string
}

export interface LineageSplitEntry {
  asset_item_id: number
  amount: string
  effective_date: string
  reason: string
  origin: string
}

export interface LineageSplitResponse {
  /** 已归档的父项。 */
  from_item: AssetItemInfo
  links: LineageLinkView[]
  entries: LineageSplitEntry[]
  archived_effective_from: string
}

// ===== 既有资产项之间补登记血缘（`POST /lineage-links/attach`，spec `.scratch/lineage-attach/spec.md` §1.1） =====
// 与合并 / 拆分不同：两侧都是**已存在**的资产项，不新建项、不写本金口径；
// 只登记「从某日起归属」——前身自生效日归档，历史行一律不改写。
export type LineageAttachLinkType = 'revision' | 'merge' | 'split'

export interface LineageAttachRequest {
  from_item_ids: number[]
  to_item_ids: number[]
  link_type: LineageAttachLinkType
  /** 仅 split 且 to 多个时必需：(0,1]、合计 1 ± 1e-4；顺序与 to_item_ids 对齐。 */
  ratios?: string[] | null
  /** 必填：补登记必须显式声明切换日（不复用 today 缺省）。 */
  effective_date: string
  /** R-9 必填。 */
  reason: string
}

export interface LineageAttachResponse {
  links: LineageLinkView[]
  from_items: AssetItemInfo[]
  to_items: AssetItemInfo[]
  archived_effective_from: string
  /** 补登记不写 principal_changes（承接值由后继项自身记录承载）。 */
  principal_written: false
}

// ===== 身份族（`GET /lineage-links/family?item_id=`，spec §1.2） =====
// 以本项为起点在 lineage_links 上做双向传递闭包（含自身）。
export type LineageFamilyRelationship = 'self' | 'predecessor' | 'successor'

export interface LineageFamilyMember {
  item_id: number
  name: string
  relationship: LineageFamilyRelationship
  /** 该成员与本项之间的链类型；self 为 null。 */
  link_type: LineageAttachLinkType | string | null
  /** 承接关系的生效日（YYYY-MM-DD）；self 为 null。 */
  effective_from: string | null
  status: 'active' | 'archived' | string
}

export interface LineageFamilyResponse {
  item_id: number
  members: LineageFamilyMember[]
  /** 族内全部链（形状由后端补齐；界面只用 members）。 */
  links: LineageLinkView[]
}

// ===== 分项趋势·身份族口径（`GET /analysis/by-item?item_id=&family=true`，spec §1.3） =====
export interface ItemFamilyMemberAmount {
  item_id: number
  name: string
  /** 该成员当日 as-of 金额（十进制字符串）。 */
  amount: string
}

export interface ItemFamilyPoint {
  period: string
  date: string
  /** 族合计（family=false 时与本项相同）。 */
  total: string
  /** 本项自身（可能为 0）。 */
  self: string
  /** 当日构成：族内参与求和的成员＋金额。 */
  members: ItemFamilyMemberAmount[]
}

export interface ItemFamilyTrendResponse {
  item: { item_id: number; name: string; family_size: number }
  points: ItemFamilyPoint[]
}

// 写用例返回的结构化变更描述（步骤 4 审计行形状，spec §7）
export interface LedgerChange {
  entity: string
  row_id: number | null
  action: string
  before?: unknown
  after?: unknown
  evidence_ref?: number | null
}

export interface PaginatedResponse {
  items: AssetRecord[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

// ===== v5 契约层类型（spec §4/§5；后端 schemas.py 同源） =====

/** 降级标注码：DATA_MISSING_CARRY_FORWARD | NO_DATA | RATE_UNAVAILABLE | PRINCIPAL_NOT_SET | TWR_LARGE_FLOW_APPROX（二期·2.2）。 */
export type DegradationCode =
  | 'DATA_MISSING_CARRY_FORWARD'
  | 'NO_DATA'
  | 'RATE_UNAVAILABLE'
  | 'PRINCIPAL_NOT_SET'
  | 'TWR_LARGE_FLOW_APPROX'

export interface Degradation {
  code: DegradationCode | string
  scope?: string | null
  message: string
}

export interface MetaBasis {
  schema_generation: number   // V5 = 5
  identity: string            // 'asset_item'
  principal?: string | null   // 二期本金口径版本预留
}

/** 计算类读响应顶层口径元数据；列表类不挂。 */
export interface Meta {
  snapshot_date: string | null
  basis: MetaBasis
  degradations: Degradation[]
  /** 后端已开启逐点身份漂移诊断（additive；旧后端不返回 → undefined）。 */
  drift_enabled?: boolean
}

/** 后端错误信封 `{"error":{code,message,details}}`（运行时抛出见 api.ts ApiError）。 */
export interface ApiErrorPayload {
  code: string
  status: number
  message: string
  details: Record<string, unknown> | null
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
  /**
   * 分类披露（与后端 `SummaryData` 同源，additive）：`status='draft'` 的资产项
   * **照常计入**上面三个合计（钱是真实存在的），这三项只回答「合计里有多少来自
   * 未补齐分类的项」。按 side 拆毛额（资产侧/负债侧分别给，不相抵），只统计该
   * as-of 日有有效记录的项；旧后端不返回 → undefined，按 0 处理。
   */
  unclassified_count?: number
  unclassified_asset_amount?: string
  unclassified_liability_amount?: string
  meta?: Meta
}

// ===== 资产项身份漂移（spec §3，additive；旧后端不返回 → undefined，界面与改造前一致） =====
// 只描述「同日哪些身份开始/停止计入」，不做归因结论；字段名与后端 schemas 同源。

/** 该点计入的身份中，当日首次出现的一项（amount = 该点计入值）。 */
export interface TrendDriftAddedItem {
  item_id: number
  name: string
  amount: string
}

/** 该点计入、但已无承接的沿用身份（「这笔值到此为止」的幽灵值）。 */
export interface TrendDriftVanishedItem {
  item_id: number
  name: string
  amount: string
  /** 该身份最后一条记录的日期（早于该点代表日）。 */
  last_date: string
}

export interface TrendPoint {
  period: string
  date: string
  total_amount: string
  net_assets: string
  liabilities: string
  // 沿用事实（ADR-0019 D12）：该点计入项中，当前值来自更早估值日的项数与最久天数
  carried_items: number
  carried_max_days: number
  // 同日首次出现的身份；无则空数组
  added?: TrendDriftAddedItem[]
  // 同日终止的沿用身份；无则空数组
  vanished?: TrendDriftVanishedItem[]
  // 漂移额占净值 ≥ 阈值（后端判定；缺省视为无提示）
  drift_suspect?: boolean
  // max(Σ新增, Σ消失)，十进制字符串
  drift_amount?: string
}

export interface ComparisonPoint {
  period: string
  date: string
  total: string
  change_amount: string
  change_percent: number | null
  // 与趋势点同源的沿用事实（派生自 TrendPoint，不另算）
  carried_items: number
  carried_max_days: number
}

export interface MixedChartData {
  trend: TrendPoint[]
  comparison: ComparisonPoint[]
  meta?: Meta
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
  // v5 写语义：同日同项 active 冲突显式清单（不静默覆盖）
  skipped?: { asset_name: string; asset_date: string; existing_id: number; reason: string }[]
}

/** CSV 导入预览行（已在 api 层把后端载体列名归一到新语义）。 */
export interface ImportPreviewRow {
  row_num: number
  asset_date: string
  asset_name: string
  amount: string
  side?: 'asset' | 'liability'
  /** 资产类别（全路径；空串＝CSV 未给或未匹配）。 */
  asset_class: string
  /** 流动性档位天数（字符串以保持空值可表达）。 */
  liquidity_days: string
  institution: string
  channel: string
  owner: string
  has_conflict: boolean
  error: string | null
}

export interface ImportPreviewResponse {
  batch_id: number
  filename: string
  valid_rows: ImportPreviewRow[]
  invalid_rows: ImportPreviewRow[]
  total_rows: number
  conflict_count: number
}

export type PeriodType = 'day' | 'week' | 'month' | 'quarter' | 'year'

/** 导出预览行（CSV 列口径：分类按全路径/天数/字典名展开）。 */
export interface ExportPreviewRow {
  asset_date: string
  asset_name: string
  amount: string
  side: 'asset' | 'liability'
  asset_class: string
  liquidity_days: number | string
  institution: string
  channel: string
  owner: string
  risk_level: string
  tags: string
}

export interface ExportPreviewResponse {
  total_count: number
  preview: ExportPreviewRow[]
  filename: string
}

export interface ProportionItem {
  name: string
  amount: string
  count: number
  percent: number
  type?: 'asset' | 'liability'
}

export interface AvailablePeriod {
  label: string
  date: string
  count: number
}

export interface ProportionData {
  dimension: string
  snapshot_date: string
  total_amount: string
  total_count: number
  items: ProportionItem[]
  total_assets: string
  total_liabilities: string
  net_worth: string
  asset_items: ProportionItem[]
  liability_items: ProportionItem[]
}

/** 占比/预警/配置目标可用维度（ADR-0019 新词汇；旧名一律 422）。 */
export type AnalysisDimension =
  | 'asset_class' | 'liquidity' | 'institution' | 'channel'
  | 'asset_name' | 'owner' | 'risk_level' | 'tag'

export interface AllProportionsData {
  snapshot_date: string
  asset_class: ProportionItem[]
  liquidity: ProportionItem[]
  institution: ProportionItem[]
  channel: ProportionItem[]
  asset_name: ProportionItem[]
  owner: ProportionItem[]
  risk_level: ProportionItem[]
  tag: ProportionItem[]
  meta?: Meta
}

export interface PeriodOption {
  label: string
  date: string
  count: number
}

export type ChartType = 'pie' | 'donut' | 'rose' | 'treemap' | 'sunburst'
export type ProportionDimension = AnalysisDimension

export interface AlertRule {
  id: number
  name: string
  dimension: AnalysisDimension
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
  dimension: AnalysisDimension
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

export interface AllocationTarget {
  id: number
  /** 配置目标维度（assset_class 支持父子层级，其余平铺）。 */
  dimension: 'asset_class' | 'liquidity' | 'institution' | 'channel' | 'risk_level'
  target_id: string
  parent_id: number | null
  target_percent: string
  alert_threshold: string  // 偏离阈值，默认5%
  created_at: string
  updated_at: string
}

export interface AllocationTargetCreate {
  dimension: 'asset_class' | 'liquidity' | 'institution' | 'channel' | 'risk_level'
  target_id: string
  parent_id?: number | null
  target_percent: string | number
  alert_threshold?: string | number  // 偏离阈值，默认5%
}

export interface AllocationBand {
  state: string
  yellow: string
  red: string
  source: string
  components?: {
    abs_yellow?: string
    abs_red?: string
    rel_yellow?: string
    rel_red?: string
    rel_applicable?: boolean
    rel_yellow_pp?: string | null
    rel_red_pp?: string | null
    target_percent?: string
    rel_applicable_min?: string
    [key: string]: unknown
  } | null
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
  alert_threshold?: string | null  // 偏离预警阈值（兼容旧后端；优先读 band.yellow）
  band?: AllocationBand | null
  children: AllocationAnalysisItem[]
}

export interface PeriodConfig {
  periodType: PeriodType
  selectedPeriod: string
}

export interface AllocationAnalysis {
  dimension: string
  snapshot_date: string
  total_amount: string
  items: AllocationAnalysisItem[]
  unallocated_percent: string
}

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

export interface ImportBackup {
  filename: string
  path: string
  size: number
  created_at: string
}

export interface ExportFile {
  filename: string
  file_size: number
  size?: number
  created_at: string
}

export interface ExportResponse {
  files: ExportFile[]
  message: string
}

// 来源组（Screenshot Entry Source Group）
export interface SourceGroup {
  id: number
  slug: string
  label: string
  asset_prefix: string | null
  guide: string | null
  guide_image: string | null
  rules: unknown
  enabled: boolean
  sort_order: number
  account_ids: number[]
  created_at: string
  updated_at: string
}

// 文件名→推荐来源组（含 App 包名命中时返回 slug；无包名时 source_group 为 null）
export interface SourceGroupRecommend {
  source_group: string | null
  label: string | null
}

// 截图录入（Screenshot Entry）
// 身份线索（item-identity-drift D4）：新名在预览里「会新建资产项」时，后端列出同账户下
// 疑似同源的 active 旧项。它只是提示，采纳前必须由人三选一（沿用/新增/撤销）。
export interface IdentityGuessCandidate {
  item_id: number
  name: string
  // 候选旧项最后一条快照的金额与日期
  amount: string
  last_date: string | null
}

export interface IdentityGuess {
  reason: string
  candidates: IdentityGuessCandidate[]
}

// 采纳时的身份裁决（键 = 预览行 index）：merge 走血缘登记（旧项按生效日归档），
// create = 明确接受新项与旧项同期并存；
// bind = 这一行其实是某个已有资产项（名字识别得不一样）→ 写快照到该目标项、id 不变。
// 撤销该行＝跳过行，不发裁决。
export interface IdentityDecisionPayload {
  action: 'merge' | 'create' | 'bind'
  target_item_id?: number
  link_type?: 'revision' | 'merge' | 'split'
  split_ratio?: string
  principal?: string
  effective_date?: string
  reason?: string
}

export interface ScreenshotPreviewRow {
  index: number
  source_name: string | null
  asset_name: string | null
  action: 'update' | 'create' | 'zero' | 'keep'
  source_amount: string
  current_amount: string | null
  diff: string
  // 旧后端不返回该字段（或为 null）时，前端必须完全按旧行为渲染
  identity_guess?: IdentityGuess | null
  // 该行命中的既有资产项（additive）：update/zero/keep 行有值，create 行为 null。
  // 用途：同一项多行去重；绑定行以目标项现值为差额基线。
  item_id?: number | null
  // 汇总行提示（ADR-0029 D7，migration 0037 同批上线）：命中内置汇总词表 /
  // 该来源组 aggregate 规则 → true。前端据此**预勾可取消**；旧后端为 undefined，按 false 处理。
  // 注意 `action` 取值域不变：汇总与否由 apply 载荷的 `reconciles` 表达，不新增动作语义。
  aggregate_hint?: boolean
  /**
   * 「历史上已并入 X」提示（spec `.scratch/archived-ingest-scope/spec.md` D3，additive）。
   *
   * 本行名称在血缘上属于一个**已归档**项时，后端不再生成 update 行而是生成 create 行，
   * 并在此给出它的 merge 去向（该归档项并入了哪个在用项）。
   *
   * 兼容口径：旧后端不返回该字段 → undefined；或为 null → 前端**不渲染任何提示、不报错**，
   * 与本次改动前逐字段一致。
   */
  archived_merged_into?: {
    item_id: number
    display_name: string
    /** 归档生效日（`YYYY-MM-DD`）；后端取不到时为 null。 */
    archived_effective_from: string | null
  } | null
}

export interface ScreenshotPreviewResponse {
  source_group: string
  // 兜底数据日期（后端语义：识别日期 or 今天）
  data_date: string | null
  // 疑似日期提取命中值 / 命中的原文行（供用户判误报）。
  // 可选属性：后端未上线时为 undefined，前端必须走旧行为（不显示提示条、不报错）
  suspected_date?: string | null
  suspected_date_text?: string | null
  total_screenshot: string | null
  total_current_system: string
  existing_count: number
  image_filename: string
  rows: ScreenshotPreviewRow[]
  // v5 步骤 6 存证前置（additive）：上传即建凭证，预览阶段即可引用；旧后端为 undefined
  evidence_id?: number | null
  // 来源组现有资产项的 item 级现值（additive）：绑定行差额基线与系统合计去重用。
  // 旧后端为 undefined，前端必须回落到按名/null 的旧行为。
  existing_items?: ExistingItemAmount[]
}

/** 预览随附的「已有项现值」（item 级；同一项因改名多名称时取最新那行）。 */
export interface ExistingItemAmount {
  item_id: number | null
  name: string
  amount: string
  as_of?: string | null
}

/**
 * 关联选择器里的「已归档项」说明（spec `.scratch/archived-ingest-scope/spec.md` D5）。
 *
 * 数据来源限既有端点（不为它新增后端接口）：
 * - `archived_effective_from` ← `GET /api/lineage-links/family?item_id=` 的 members 里
 *   `relationship='self'` 成员的 `effective_from`（后端由 `asset_items.archived_effective_from` 派生）；
 * - `merged_into` ← 同一响应里 `link_type='merge'` 的**后继**成员名（该归档项并入的在用项）。
 *
 * 取不到时降级为 `null` / `[]`，界面只说「已归档」，不报错。
 */
export interface ArchivedItemHint {
  archived_effective_from: string | null
  merged_into: string[]
}

// 异步 OCR 预览 job（POST /api/screenshots/preview-jobs → 202 {job_id}，GET 轮询进度）
// 单 worker 串行执行；ocr 仅 stage='ocr' 时非空；line_total 在 det 完成前可能为 0
export type PreviewJobStage = 'queued' | 'cache' | 'ocr' | 'organize' | 'match' | 'done' | null

export interface PreviewJobOcrProgress {
  chunk_index: number
  chunk_total: number
  line_done: number
  line_total: number
}

// 实时原始 OCR 行：逐行累积（上限 1000）；queued / 无逐行模式时为 []
export interface PreviewJobLine {
  t: string // 识别文本
  s: number // 置信度 0-1
  c: number // 片号（0 基）
}

export interface PreviewJobStatus {
  status: 'queued' | 'running' | 'done' | 'error'
  stage: PreviewJobStage
  ocr: PreviewJobOcrProgress | null
  queue_position: number | null
  elapsed: number
  // done 时与同步 POST /preview 响应逐字段一致
  result: ScreenshotPreviewResponse | null
  error: string | null
  lines?: PreviewJobLine[]
}

export interface ScreenshotApplyResult {
  evidence_id: number
  data_date: string
  rows_written: number
  message: string
  // 补录写入条数 / 被丢弃的补录项（后端未上线该字段时为 undefined，前端按 0 / [] 兜底）
  supplements_written?: number
  supplements_dropped?: SupplementDropInfo[]
  // v5 写语义：create 撞同日 active 行的显式冲突清单（不静默覆盖；200 体内警示，非错误）
  conflicts?: { source_name?: string | null; asset_name: string; asset_date: string;
                existing_id: number | null; reason: string }[]
  // 汇总行回执（ADR-0029 D6，additive）：本次被标为汇总行、未落账的行号 / 金额 / 命中的 item_id；
  // 以及从清零候选中剔除的项（与 bind 的 `zero_skipped_due_to_bind` 同构）。旧后端为 undefined
  reconciled?: { index: number; item_id: number | null; amount: string }[]
  zero_skipped_due_to_reconcile?: string[]
}

// ===== 截图批次（ADR-0029 D8/D9；迁移 0037）=====
// 批次是真实业务对象：多页截图里「父在 A 图、子在 B 图」跨图核对必须有个可留痕的载体。
// 三段落：POST /screenshot-batches（建壳，拿 batch_id）→ 逐图 apply 带 batch_id（写成员）
// → POST /screenshot-batches/{id}/finalize（唯一校验点，写核对记录 + 构成边 + 审计）。
// 不带 batch_id 的 apply 行为与本期之前逐字段一致（additive）。

/** POST /api/screenshot-batches 响应。 */
export interface ScreenshotBatchCreateResult {
  batch_id: number
  status?: 'open' | 'partial' | 'finalized' | 'abandoned'
  data_date?: string | null
  expected_members?: number
}

/** finalize 载荷里的一条核对：父项（行锚 + 资产项）+ 直接子项。 */
export interface ScreenshotReconciliationPayload {
  /** 父项资产项 id；null = 该汇总行还没落成资产项（后端 409 RECONCILE_PARENT_NOT_FOUND） */
  parent_item_id: number | null
  /** 父项所在凭证；父项是「本批之外的既有资产项」时为 null（本批没有它的行） */
  parent_evidence_id: number | null
  /** 父项在该凭证 preview 里的行号；同上为 null */
  parent_row_index: number | null
  /** 父项金额（两位小数字符串）；后端仍会用自己的 Decimal 重算 */
  aggregate_amount: string
  /** 子项：行锚 + 落库后的 item_id（本次才新建的行认不出 id，带 null）+ 金额 */
  children: {
    evidence_id: number
    row_index: number
    item_id: number | null
    amount: string
  }[]
}

/** POST /api/screenshot-batches/{id}/finalize 请求体。 */
export interface ScreenshotBatchFinalizeBody {
  reconciliations: ScreenshotReconciliationPayload[]
  /** 差额≠0 时必须带（400 RECONCILE_REASON_REQUIRED）。批次级一条，条目内不重复 */
  accept_mismatch?: { reason: string }
}

/** POST /api/screenshot-batches/{id}/finalize 响应。 */
export interface ScreenshotBatchFinalizeResult {
  batch_id: number
  status: 'finalized'
  finalized_at?: string | null
  reconciliations?: number
  accepted_mismatches?: number
}

// --- 补录（supplements）：把「AI 额外识别」项手动补进本次待写入清单 ---
// 定稿：.scratch/llm-verification/supplement-design.md
// 草稿只活在前端（ImgState.supplements），绝不进 img.rows —— 行号通道（skipped/amounts/names）是唯一真源；
// apply 时只发 {name, amount, side}，匹配与冲突一律由后端权威重算

// 资产 / 负债：默认 asset，用户逐项切换（不按名字猜）
export type SupplementSide = 'asset' | 'liability'

// 预判定动作：update=命中已有资产将更新 / create=将新增 / conflict=与本次已决定写入的行冲突（不写入）
export type SupplementAction = 'update' | 'create' | 'conflict'

// 被丢弃的补录项（apply 响应）：reason ∈ conflict_update|conflict_create|conflict_zero|duplicate_target|invalid
export interface SupplementDropInfo {
  name: string
  reason: string
  asset_name?: string | null
}

// resolve 回填到草稿上的判定结论（写入前必须可见：补录不是盲写）
export interface SupplementResolved {
  action: SupplementAction
  final_name: string | null
  asset_name: string | null
  current_amount: string | null
  // 冲突原因（中文，可直接展示）
  conflict_reason: string | null
}

// 前端补录草稿：uid 仅用于列表 key / 定位，不发给后端
export interface SupplementDraft {
  uid: string
  name: string
  amount: string
  side: SupplementSide
  // 判定结论；undefined = 尚未判定（刚加入 / 刚编辑），null 同义
  resolved?: SupplementResolved | null
  // 判定请求失败原因（就地展示 + 可重试；失败不阻断写入，后端仍会权威重算）
  resolveError?: string | null
}

// POST /api/screenshots/supplements/resolve 请求项
export interface SupplementResolveItem {
  name: string
  amount: string
  side: SupplementSide
}

// 同端点响应：逐项、与请求同序（§7）
export interface SupplementResolveResult extends SupplementResolved {
  name: string
  side?: SupplementSide | null
}

// 凭证详情（GET /api/screenshots/evidence/{id}，record-provenance D7，票 06）。
// 单条响应 = 列表项 ∪ `{file_sha256}`：形状与列表项同源（后端 `evidence_item()`
// 单点构造），只多一个审计血缘用的 sha——列表不为它多背一列。
export type ScreenshotEvidenceInfo = EvidenceListItem & { file_sha256: string }

// ===== 凭证管理（evidence-manager spec D1/D5）=====
// 凭证归属的来源组：id 是身份，slug/label 是当前展示值（组改名后展示自动跟随）。
export interface EvidenceGroupRef {
  id: number
  slug: string
  label: string
}

// 原图体检表的 EXIF 白名单子集（ADR-0022 D2）：**不含 GPS**。
// 无 EXIF 段 → 整个 `exif` 为 `{}`；单个键缺失即为 `undefined`（后端不写 null）。
// 标量类型跟随 PIL 原值：多为 string（日期/厂牌/型号），Orientation 之类为 number。
export interface EvidenceImageExif {
  DateTimeOriginal?: string | number
  Make?: string | number
  Model?: string | number
  Software?: string | number
  Orientation?: string | number
}

// 原图体检表（GET /api/screenshots/evidence 每条已带，ADR-0022 D2）。
// 未回填/坏图/读不出 → 整个对象为 `null`（不是 `{}`）；`format` 在 Pillow 认不出时为 null。
export interface EvidenceImageMeta {
  width?: number | null
  height?: number | null
  bytes?: number | null
  format?: string | null
  exif?: EvidenceImageExif | null
}

// 列表单条（GET /api/screenshots/evidence）。`used_rows` 为**全行**口径：
// 含被更正的 superseded 历史行，故下钻账本时必须带 `view=ledger`（spec D3/D8）。
export interface EvidenceListItem {
  id: number
  source_group: EvidenceGroupRef
  file_path: string
  original_filename: string | null
  data_date: string | null
  // 识别引擎；历史行 NULL（后端语义 = ocr）
  engine: 'ocr' | 'ai' | null
  created_at: string
  used_rows: number
  // 原图体检表（evidence-naming，ADR-0022）：缺数据即 null，展示面一律落「—」
  image_meta?: EvidenceImageMeta | null
  // 识别快照摘要（evidence-naming，ADR-0023 D7）：未识别过即 null。
  // 详情弹层的「识别快照」一节全部取自这里 ⇒ 打开抽屉不额外发请求（spec D1）
  recognition_summary?: EvidenceRecognitionSummary | null
}

export interface EvidenceListResponse {
  items: EvidenceListItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

// ===== 识别快照（evidence-recognition-view spec D1/D2；上游 ADR-0023 D7）=====
// 摘要（列表接口已带）：回答「哪一次、什么引擎、识别多少行、采纳多少行、块丢没丢」。
// 本体（含识别行与 OCR 原始块）只在点「查看识别行」时单独取，见 D2 端点。
export interface EvidenceRecognitionSummary {
  schema: number | null
  engine: string | null
  source: 'server' | 'client' | null
  parser_version: string | null
  // 本地时区秒级 ISO（如 `2026-11-05T16:40:12`），展示走 formatDateTime
  parsed_at: string | null
  // 体积降级丢掉了 OCR 原始块（`recognized.blocks=null` 且此标记为 true）
  blocks_dropped: boolean
  // 条数：体积降级后 items 被摘掉时后端改答 `item_count`，故可能「有数无行」
  recognized_items: number | null
  adopted_items: number | null
  corrected_rows: number | null
}

// 快照里的单条识别/采纳行（金额是字符串：全项目 JSON 金额口径，不用 float）
export interface EvidenceRecognitionItem {
  name: string
  amount: string | null
}

// `recognized` 段：机器识别出什么（服务端 OCR / 前端 AI 回传）
export interface EvidenceRecognitionRecognized {
  engine?: string | null
  source?: 'server' | 'client' | null
  parser_version?: string | null
  parsed_at?: string | null
  // 体积二级降级后为 null，条数改看 item_count
  items?: EvidenceRecognitionItem[] | null
  item_count?: number | null
  total?: string | null
  date_text?: string | null
  // OCR 原始块：界面只展示条数，不渲染内容（spec D3/D5）
  blocks?: unknown[] | null
  blocks_dropped?: boolean
  // 前端未回传识别行时的缺口标记（`client-side`）
  note?: string
}

// `adopted` 段：人改后真正落账本的行（只有 /apply 会写）
export interface EvidenceRecognitionAdopted {
  engine?: string | null
  item_count?: number | null
  corrected_rows?: number | null
  items?: EvidenceRecognitionItem[] | null
}

// 快照本体（`schema: 1`）；四级兜底时退化成 `{schema, note: 'size_limit'}`
export interface EvidenceRecognitionSnapshot {
  schema?: number | null
  note?: string
  recognized?: EvidenceRecognitionRecognized | null
  adopted?: EvidenceRecognitionAdopted | null
}

// GET /api/screenshots/evidence/{id}/recognition 响应：
// `snapshot` 为 null = 行在但从未识别过（合法状态，不是错误）
export interface EvidenceRecognitionResponse {
  snapshot: EvidenceRecognitionSnapshot | null
}

// POST /api/screenshots/evidence/purge-unused 响应（dry_run=true 只是统计，库与盘零变化）。
export interface EvidencePurgeResult {
  deleted: number
  failed: Array<{ id: number; reason: string }>
  freed_bytes: number
  dry_run: boolean
  // 候选清单（dry_run 与执行两次响应都带）：待删/已删的凭证及文件字节，
  // 供界面把「将删哪几张」摆出来核对；total 是**未截断**的候选总数（可大于 items.length）
  items: Array<{ id: number; file_path: string; file_bytes: number }>
  total: number
  truncated: boolean
  limit: number
}

// LLM 供应商（截图一致性校验的第二意见；无明文 key，仅 api_key_masked）
export type LlmProviderKind = 'deepseek' | 'dashscope' | 'openai_compatible'

export interface LlmProvider {
  id: number
  label: string
  kind: LlmProviderKind
  base_url: string
  model: string
  api_key_masked: string | null
  is_default: boolean
  enabled: boolean
  sort_order: number
  // 最近一次连通性测试的结论（ADR-0025）。四列均**可空**，语义不可混：
  // `last_tested_at == null` ＝ 从未测试；`last_test_ok === false` ＝ 测过且失败。
  // 别用 `last_test_ok == false` 判「未测试」（null 也是 falsy），也别把
  // `last_test_elapsed_ms == null` 当成 0ms——0 是一次真实探测的下界。
  // 改 base_url / model / api_key 的值后后端会清空这四列（回到「从未测试」）。
  last_tested_at?: string | null
  last_test_ok?: boolean | null
  last_test_message?: string | null
  last_test_elapsed_ms?: number | null
}

export interface LlmProviderCreate {
  label: string
  kind: LlmProviderKind
  base_url: string
  api_key: string
  model: string
  is_default?: boolean
  enabled?: boolean
}

// 部分字段更新；api_key 留空 = 不修改 key
export interface LlmProviderUpdate {
  label?: string
  kind?: LlmProviderKind
  base_url?: string
  api_key?: string
  model?: string
  is_default?: boolean
  enabled?: boolean
}

// 供应商连通性测试（POST /api/llm-providers/{id}/test）
export interface LlmProviderTestResult {
  ok: boolean
  message: string
  elapsed_ms: number
}

// 截图 AI 校验（verify）
export type VerifyVerdict = 'match' | 'mismatch' | 'ocr_only' | 'unverified'

export interface VerifyRow {
  index: number
  verdict: VerifyVerdict
  ai_name: string | null
  ai_amount: string | null
}

export interface VerifyAiOnlyItem {
  name: string
  amount: string
}

export interface VerifyResponse {
  rows: VerifyRow[]
  ai_only: VerifyAiOnlyItem[]
}

// --- 支出录入（expense-entry，契约：.scratch/expense-entry/spec.md §6.1） ---
// 支出是流水（expenses），资产是快照（assets），两者分表；支出不参与净值计算。
// 金额一律正数、字符串序列化（两位小数）；日期 "YYYY-MM-DD"；period "YYYY-MM"。

export type ExpenseType = 'fixed' | 'large_oneoff';
export type ExpenseCycle = 'monthly' | 'quarterly' | 'yearly';

export interface ExpenseRecord {
  id: number;
  expense_date: string;
  expense_name: string;
  amount: string;
  expense_type: ExpenseType;
  category_id?: number | null;
  category_name?: string | null;
  account_id?: number | null;
  account_name?: string | null;
  note?: string | null;
  template_id?: number | null;
  period?: string | null;
  created_at?: string;
  updated_at?: string;
}
export interface ExpenseRecordCreate {
  expense_date: string;
  expense_name: string;
  amount: string;
  expense_type: ExpenseType;
  category_id?: number | null;
  account_id?: number | null;
  note?: string | null;
}
export type ExpenseRecordUpdate = Partial<ExpenseRecordCreate>;

export interface ExpenseCategory {
  id: number;
  name: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
}
export interface ExpenseCategoryCreate { name: string; description?: string | null }
export type ExpenseCategoryUpdate = Partial<ExpenseCategoryCreate>;

export interface FixedExpenseTemplate {
  id: number;
  name: string;
  amount: string;
  cycle: ExpenseCycle;
  start_date: string;
  end_date?: string | null;
  category_id?: number | null;
  category_name?: string | null;
  account_id?: number | null;
  account_name?: string | null;
  note?: string | null;
  active: boolean;
  created_at?: string;
  updated_at?: string;
}
export interface FixedExpenseTemplateCreate {
  name: string;
  amount: string;
  cycle: ExpenseCycle;
  start_date: string;
  end_date?: string | null;
  category_id?: number | null;
  account_id?: number | null;
  note?: string | null;
  active?: boolean;
}
export type FixedExpenseTemplateUpdate = Partial<FixedExpenseTemplateCreate>;

export interface ExpenseGeneratePreviewItem {
  template_id: number;
  name: string;
  amount: string;
  cycle: ExpenseCycle;
  category_id?: number | null;
  category_name?: string | null;
  account_id?: number | null;
  account_name?: string | null;
  note?: string | null;
  status: 'new' | 'exists';
  existing_expense_id?: number | null;
  expense_date: string;
}
export interface ExpenseGeneratePreview {
  period: string;
  period_start: string;
  period_end: string;
  items: ExpenseGeneratePreviewItem[];
}
export interface ExpenseGenerateItem { template_id: number; amount?: string; expense_date?: string }
export interface ExpenseGenerateRequest {
  period: string;
  conflict_resolution: 'skip' | 'overwrite';
  items: ExpenseGenerateItem[];
}
export interface ExpenseGenerateResult {
  success: boolean;
  created_count: number;
  skipped_count: number;
  overwritten_count: number;
  period: string;
  records: ExpenseRecord[];
  message: string;
}

export interface ExpenseCategoryStat {
  category_id?: number | null;
  category_name: string;
  total: string;
  count: number;
}
export interface ExpensePeriodStats {
  period: string;
  total: string;
  fixed_total: string;
  large_total: string;
  count: number;
  by_category: ExpenseCategoryStat[];
}
export interface ExpenseYearStats {
  year: number;
  total: string;
  fixed_total: string;
  large_total: string;
  count: number;
  by_category: ExpenseCategoryStat[];
}
export interface ExpenseSummary {
  month: ExpensePeriodStats;
  year: ExpenseYearStats;
  fixed_monthly_burden: string;
}
export interface PaginatedExpenses {
  items: ExpenseRecord[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// ===== 三期-0「指标骨架」契约（.scratch/metric-skeleton/spec.md §3，v1.0 冻结） =====
// 只消费契约字段：值（字符串 Decimal，金额 2 位 / 比率 4 位，不可得 null）＋ 口径（gates）
// ＋ 判据（grade；null＝本切片不判灯）＋ 下钻引用。列表型不挂 meta，状态面挂顶层 meta。

/** 指标字典项（GET /api/metrics；静态、无 meta）。 */
export interface MetricDefinition {
  id: string
  name: string
  domain: string
  /** 受控标签集（口径层级/形态/时间语义/方向/用途/来源/信赖度）。 */
  tags: MetricTags
  unit: string
  direction: string
  criterion: MetricCriterion | null
  as_of_semantics: string
  drill: MetricDrill | null
}

/** 标签集（后端 MetricTags 固定七键；索引签名只作前向兼容）。 */
export interface MetricTags {
  layer?: string
  form?: string
  time?: string
  direction?: string
  purpose?: string
  source?: string
  trust?: string
  [key: string]: unknown
}

/** 判据描述；本期只实现绝对阈值型（配置偏离 ← allocation_targets.alert_threshold）。 */
export interface MetricCriterion {
  kind: string
  source: string | null
}

/** 判据带：黄阈值为有效值；红档/相对阈值组件为加性字段（三期-2）。 */
export interface MetricBand {
  kind: string
  threshold: string | null
  source: string | null
  red?: string | null
  components?: Record<string, unknown> | null
}

/** 灯四态；grade=null ＝「无判据」展示档（不点灯、显示方向文字），不是第五态。 */
export type MetricGradeState = 'green' | 'yellow' | 'red' | 'gray'

export interface MetricGrade {
  state: MetricGradeState
  band: MetricBand | null
  basis: 'current' | 'as-of' | string
  note: string | null
}

/** 口径闸门：blocking=true 时不可判（→ ⚪，不出彩灯）；note 为可直接展示的说明。 */
export interface MetricGate {
  code: string
  note: string
  blocking: boolean
}

/** 配置偏离逐目标行（status.details.rows；allocation_deviation 专用）。 */
export interface MetricAllocationRow {
  dimension: string
  target_id: string
  name: string
  target_percent: string
  actual_percent: string
  deviation: string
  deviation_percent: string | null
  deviation_amount: string | null
  alert_threshold: string
  state: MetricGradeState
}

/** details：data_coverage 的覆盖度字段 + allocation_deviation 的逐目标行。 */
export interface MetricDetails {
  /** 沿用事实（data_coverage 指标已随 ADR-0019 D12 下线，仅余沿用天数）。 */
  carried_max_days?: number | null
  rows?: MetricAllocationRow[] | null
  [key: string]: unknown
}

/** 下钻引用：应用内目标（可带 ?section= 锚点）＋展示名。 */
export interface MetricDrill {
  /** 形如 `/analysis?section=trend`、`/?section=net-worth`。 */
  target: string
  label?: string | null
}

export interface MetricStatusItem {
  id: string
  value: string | null
  unit: string
  direction: string
  grade: MetricGrade | null
  gates: MetricGate[]
  tags: MetricTags
  details?: MetricDetails | null
  drill: MetricDrill | null
}

// ===== 三期-2 B4：域级汇总＋异动（加性；spec §5.1）=====
// 域＝指标注册表 domain（B1/B2/B3/C1/C2/底座）；不是 B4、不是 B/C 主域；
// 不设全局灯/总分：只给域灯与计数。

export interface MetricDomainCounts {
  green: number
  yellow: number
  red: number
  gray: number
  unjudged: number
}

/** 红信号：kind=item 单灯；kind=row 为复合项（配置偏离）展开的目标行。 */
export interface MetricRedSignal {
  kind: 'item' | 'row' | string
  id?: string | null
  name?: string | null
  domain?: string | null
  dimension?: string | null
  target_id?: string | null
  deviation?: string | null
  drill?: MetricDrill | null
}

export interface MetricDomain {
  domain: string
  name: string
  state: MetricGradeState
  counts: MetricDomainCounts
  judged: number
  total: number
  /** red>yellow>green（不含 gray）；仅作诊断辅助，域态仍看 state。 */
  worst?: MetricGradeState | null
  /** 域内存在判了但降级的项：⚪ 阻断绿。 */
  gray_blocked?: boolean
  active_events?: number
  event_state?: MetricGradeState | null
  reds: MetricRedSignal[]
  needs_diagnosis?: boolean
  drill?: MetricDrill | null
}

export type MetricEventStatus = 'new' | 'ongoing'

export interface MetricEventThresholds {
  amount: string | null
  percent: string | null
}

/** 异动＝事件（上升沿），不是状态；默认黄，静默期＝未回落不重复报。 */
export interface MetricEvent {
  id: string
  rule_id: number
  rule_name: string
  domain: string | null
  severity: MetricGradeState
  status: MetricEventStatus
  period: string
  period_end: string
  previous_period: string | null
  change_amount: string
  change_percent: string | null
  thresholds: MetricEventThresholds
  direction: string | null
  drill?: MetricDrill | null
}

/** 计数汇总：无总分/全局灯。 */
export interface MetricSummary {
  red: number
  yellow: number
  green: number
  gray: number
  unjudged: number
  judged: number
  total: number
  domains: number
  active_events: number
  new_events: number
}

export interface MetricStatusResponse {
  meta: Meta
  items: MetricStatusItem[]
  /** 加性：旧后端不返回时按空处理。 */
  domains?: MetricDomain[]
  events?: MetricEvent[]
  summary?: MetricSummary
}

export interface MetricDictResponse {
  items: MetricDefinition[]
}


// ===== 三期-1 C1：目标版本与达成度（spec §5）=====

export type GoalStatus = 'draft' | 'active' | 'archived'
export type GoalVersionStatus = 'draft' | 'active' | 'archived'
export type RiskProfile = 'conservative' | 'balanced' | 'growth'
export type LifeStage = 'accumulation' | 'transition' | 'decumulation'

export interface GoalScope {
  kind: 'family' | 'member'
  owner_id: number | null
}

export interface GoalCriterion {
  schema?: number
  kind?: string | null
  band_basis?: string | null
  partition?: { risk_profile?: RiskProfile | null; life_stage?: LifeStage | null }
  note?: string | null
  [key: string]: unknown
}

export interface GoalVersion {
  id: number
  goal_id: number
  version_no: number
  status: GoalVersionStatus
  target_amount: string
  target_date: string
  target_rate_annual: string | null
  risk_profile: RiskProfile | null
  life_stage: LifeStage | null
  reason: string
  effective_from: string | null
  criterion: GoalCriterion | null
  created_at: string
  activated_at: string | null
  archived_at: string | null
}

export interface Goal {
  id: number
  name: string
  scope: GoalScope
  is_primary: boolean
  status: GoalStatus
  created_at: string
  active_version?: GoalVersion | null
}

export interface GoalListResponse {
  items: Goal[]
  total: number
}

export interface GoalDetailResponse {
  goal: Goal
  versions: GoalVersion[]
}

export interface GoalProgressGate extends MetricGate {
  scope?: string | null
}

export interface GoalProgress {
  goal_id: number
  goal_version_id: number
  version_no: number
  is_primary: boolean
  as_of: string
  effective_from: string | null
  scope: GoalScope
  target_amount: string
  target_date: string
  target_rate_annual: string | null
  net_worth: string
  gap: string
  progress: string
  required_annualized: string | null
  remaining_days: number
  remaining_months: number
  assumption: string
  band_basis: string
  partition: { risk_profile: RiskProfile | null; life_stage: LifeStage | null }
  gates: GoalProgressGate[]
  grade: MetricGrade | null
  meta: Meta
}

export interface GoalCreateInput {
  name: string
  scope?: GoalScope
  target_amount: string
  target_date: string
  target_rate_annual?: string | null
  risk_profile?: RiskProfile | null
  life_stage?: LifeStage | null
  reason: string
  activate?: boolean
}

export interface GoalVersionInput {
  target_amount: string
  target_date: string
  target_rate_annual?: string | null
  risk_profile?: RiskProfile | null
  life_stage?: LifeStage | null
  reason: string
  activate?: boolean
}

/** 归属类型：只接受 `member`（值域 members）；共有归属用 role='joint' 的成员表达。 */
export type ItemOwnerKind = 'member'

export interface ItemOwnerEntry {
  id: number
  asset_item_id: number
  owner_kind: ItemOwnerKind
  owner_id: number | null
  owner_name: string | null
  effective_date: string
  reason: string | null
  origin: string
  created_at: string
}

export interface ItemOwnerResponse {
  item_id: number
  current: ItemOwnerEntry | null
  history: ItemOwnerEntry[]
}

export interface ItemOwnerBulkEntry {
  id: number
  owner_kind: ItemOwnerKind | null
  owner_id: number | null
  owner_name: string | null
  effective_date: string | null
}

export interface ItemOwnerBulkResponse {
  items: ItemOwnerBulkEntry[]
  total: number
}

export interface BatchOwnerInput {
  ids: number[]
  owner_kind: ItemOwnerKind
  owner_id?: number | null
  reason: string
  effective_date?: string | null
}

export interface BatchOwnerFailure {
  id: number
  code: string
}

export interface BatchOwnerResponse {
  updated: number
  skipped: number
  failed: BatchOwnerFailure[]
}

export interface AssetTagBatchInput {
  record_ids: number[]
  add_tag_ids?: number[]
  remove_tag_ids?: number[]
}

export interface AssetTagBatchFailure {
  id: number
  code: string
}

export interface AssetTagBatchResponse {
  updated: number
  skipped: number
  failed: AssetTagBatchFailure[]
}

// ===== 三期-3 C3：建议收件箱（spec §6）=====

export type AdvisorStatus =
  | 'generating'
  | 'awaiting_review'
  | 'gate_failed'
  | 'generation_failed'
  | 'accepted'
  | 'rejected'
  | 'expired'

export type AdvisorHorizon = 'short' | 'mid' | 'long'
export type AdvisorExecutionStatus = 'done' | 'partial' | 'not_done'

export interface AdvisorTrigger {
  kind: string
  ref: string | null
}

export interface AdvisorGoalRef {
  goal_id: number | null
  version_no?: number | null
  version_id?: number | null
  goal_version_id?: number | null
}

export interface AdvisorSuggestionSummary {
  id: number
  status: AdvisorStatus
  horizon: AdvisorHorizon
  title: string | null
  summary: string | null
  gate_failure: { code: string; label: string } | null
  trigger: AdvisorTrigger
  gate_result: string | null
  goal: AdvisorGoalRef
  created_at: string
  generated_at: string | null
  expires_at: string | null
}

export interface AdvisorSuggestionList {
  items: AdvisorSuggestionSummary[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface AdvisorGateCheck {
  code: string
  severity: 'blocking' | 'warning' | string
  passed: boolean
  detail?: string | Record<string, unknown> | null
}

export interface AdvisorGate {
  result: 'passed' | 'failed' | string
  gate_version: string
  checks: AdvisorGateCheck[]
  checked_at?: string | null
}

export interface AdvisorSuggestionItem {
  seq: number
  subject_kind: string
  subject_ref: string | null
  direction: string
  rationale: string
  info_refs: string[]
}

export interface AdvisorInfoSetRef {
  id: number
  as_of: string | null
  entry_count: number
  payload_sha256: string
  builder_version: string
}

export interface AdvisorProviderRef {
  id: number | null
  label: string | null
  model: string | null
  prompt_version: string | null
}

export interface AdvisorDecision {
  id: number
  action: string
  reason: string | null
  execution_status: AdvisorExecutionStatus | string | null
  decided_at: string
  actor: string
}

export interface AdvisorSuggestionDetail extends AdvisorSuggestionSummary {
  summary: string | null
  items: AdvisorSuggestionItem[]
  gate: AdvisorGate | null
  info_set: AdvisorInfoSetRef | null
  provider: AdvisorProviderRef | null
  decisions: AdvisorDecision[]
  created_at: string
  generated_at: string | null
  closed_at: string | null
  meta: {
    market_basis?: string
    degradations?: Array<{ code: string; message: string }>
    [key: string]: unknown
  }
}

export interface AdvisorInfoEntry {
  key: string
  kind: string
  label: string
  value: string
  value_scaled?: string | null
  unit: string
  trust: string
  source: string
  source_ref?: string | null
  as_of: string | null
}

export interface AdvisorInfoSet {
  id: number
  created_at: string
  as_of: string | null
  goal_id: number | null
  goal_version_id: number | null
  builder_version: string
  entry_count: number
  payload_sha256: string
  meta: Record<string, unknown> | null
  entries: AdvisorInfoEntry[]
  payload_ok: boolean
}

export interface AdvisorSettings {
  auto_enabled: boolean
  cooldown_days: number
  default_horizon: AdvisorHorizon
  last_auto_at: string | null
  updated_at: string
}

export interface AdvisorCreateInput {
  horizon?: AdvisorHorizon | null
  goal_id?: number | null
  note?: string | null
}

// ===== 三期-4 C4：复盘与反哺提案（spec §5）=====
export type ReviewStatus = 'open' | 'closed'
export type ReviewAttributionCategory =
  | 'market_moved' | 'advice_wrong' | 'not_executed' | 'execution_drift'
export type ReviewProposalKind = 'goal_version' | 'threshold' | 'allocation_target' | 'narrative'
export type ReviewProposalStatus = 'proposed' | 'adopted' | 'dismissed'

export interface ReviewProposalCounts {
  proposed: number
  adopted: number
}

export interface ReviewSummary {
  id: number
  goal_id: number
  goal_name: string | null
  goal_version_id: number
  version_no: number | null
  start_goal_version_id: number | null
  status: ReviewStatus
  as_of: string
  start_as_of: string | null
  label: string | null
  note: string | null
  snapshot_sha256: string
  attribution: ReviewAttribution[]
  conclusion: string | null
  created_at: string
  closed_at: string | null
  progress_end?: string | null
  progress_delta?: string | null
  comparable?: boolean | null
  proposal_counts?: ReviewProposalCounts
  suggestion_batch_size?: number
  conclusion_excerpt?: string | null
  band_history_missing?: boolean
}

export interface ReviewAttribution {
  category: ReviewAttributionCategory
  subject_kind: string | null
  subject_ref: string | null
  note: string | null
}

export interface ReviewAnchor {
  as_of: string
  anchor_date: string | null
  goal_version_id: number
  version_no: number
  net_worth: string
  progress: string
  gap: string
  required_annualized: string | null
  grade: MetricGrade | null
}

export interface ReviewDelta {
  comparable: boolean
  net_worth_change: string | null
  progress_delta: string | null
  not_comparable_reason: string | null
}

export interface ReviewAllocationRow {
  dimension: string
  target_ref: string
  target_percent: string | null
  actual_percent_start: string | null
  actual_percent_end: string | null
  delta_pp: string | null
  deviation_pp: string | null
  revision_id: number | null
  band: Record<string, unknown> | null
  state: string | null
  history: string | null
}

export interface ReviewAllocationSnapshot {
  band_basis: string
  classification_basis: string
  history_from: string | null
  rows: ReviewAllocationRow[]
  skipped_deleted_targets: string[]
}

export interface ReviewSuggestionSnapshot {
  suggestion_id: number
  title: string | null
  status: string
  horizon: string
  info_set: { id: number; as_of: string; payload_sha256: string } | null
  accepted_at?: string | null
  execution?: { status: string | null; note: string | null } | null
}

export interface ReviewSignalDomain {
  domain: string
  state: string
  [key: string]: unknown
}

export interface ReviewSignalEvent {
  id?: string
  rule_id?: number
  rule_name?: string
  domain?: string
  severity?: string
  status?: string
  period?: string
  [key: string]: unknown
}

export interface ReviewHint {
  required_at_current_date: string | null
  extend_1y: { target_date: string; required_annualized: string | null }
  extend_2y: { target_date: string; required_annualized: string | null }
}

export interface ReviewSnapshot {
  schema: number
  anchors: { start: ReviewAnchor | null; end: ReviewAnchor }
  delta: ReviewDelta
  goal: { goal_id: number; version_changed: boolean }
  allocation: ReviewAllocationSnapshot
  suggestions: ReviewSuggestionSnapshot[]
  signals: { basis: string; domains: ReviewSignalDomain[]; events: ReviewSignalEvent[] }
  hints: ReviewHint
  degradations: Array<{ code: string; message: string }>
}

export interface ReviewProposal {
  id: number
  review_id: number
  kind: ReviewProposalKind
  subject_ref: string | null
  payload: Record<string, unknown>
  payload_sha256: string
  rationale: string
  status: ReviewProposalStatus
  decided_at: string | null
  decision_note: string | null
  applied_kind: string | null
  applied_ref: string | null
  created_at: string
}

export interface ReviewDetailResponse {
  review: ReviewSummary
  snapshot: ReviewSnapshot
  payload_ok: boolean
  suggestions: ReviewSuggestionSnapshot[]
  proposals: ReviewProposal[]
}

export interface ReviewCandidate {
  suggestion_id: number
  title: string | null
  horizon: string
  info_set: { id: number; as_of: string } | null
  execution: { status: string | null; note: string | null } | null
  accepted_at: string | null
}

export interface ReviewCandidateList {
  items: ReviewCandidate[]
  total: number
}

export interface ReviewListResponse {
  items: ReviewSummary[]
  total: number
  page: number
  page_size: number
}

export interface ReviewCreateInput {
  goal_id?: number | null
  as_of?: string | null
  start_as_of?: string | null
  suggestion_ids?: number[]
  label?: string | null
  note?: string | null
}

export interface ReviewProposalInput {
  kind: ReviewProposalKind
  subject_ref?: string | null
  payload: Record<string, unknown>
  rationale: string
}
