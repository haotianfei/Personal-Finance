from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# --- 审计变更原因（ADR-0027 D2/D7） ---
# 端点入参类型：与 `services.audit_contract.REASON` **同集**的封闭枚举。用 Literal
# 让 pydantic 在进入 service 之前就挡掉非法值（422）——校验失败不写任何业务/审计行。
# 两处取值必须一致，由 `tests/test_audit_reason_capture.py` 断言，防止单边漂移。
AuditReason = Literal["correction", "evolution", "normalization",
                      "import", "migration", "other"]
AUDIT_REASON_NOTE_MAX = 200

# 项级备注上限（ADR-0031 D2）：500 **字符**（Unicode 码点），按 `trim` 后的文本计。
# SQLite 的 TEXT 没有长度约束，超长只能靠应用层挡；放宽到「覆盖换行记账备注、又不至于
# 变成文档」的尺度。与 `AUDIT_REASON_NOTE_MAX`（审计理由说明）是两个不同的上限，
# 一个量备注正文，一个量「为什么改」的解释，勿合并。
NOTE_MAX = 500


# --- 口径元数据（v5 契约层，spec §4） ---
class Degradation(BaseModel):
    """降级标注：DATA_MISSING_CARRY_FORWARD | NO_DATA | RATE_UNAVAILABLE（二期预留）。"""
    code: str
    scope: Optional[str] = None   # 日期/资产项等定位
    message: str


class MetaBasis(BaseModel):
    schema_generation: int = 5    # V5 数据世代
    identity: str = "asset_item"  # 身份口径＝持久资产项 ID（非显示名）
    principal: Optional[str] = None  # 二期本金口径版本预留位


class Meta(BaseModel):
    snapshot_date: Optional[date] = None
    basis: MetaBasis = MetaBasis()
    degradations: list[Degradation] = []
    # 漂移诊断能力探测位（spec §3）：仅 total-trend / mixed-chart 置 true，
    # 旧前端忽略即兼容；前端可据此决定是否读 TrendPoint 的 drift_* 字段。
    drift_enabled: bool = False


class MetaMixin(BaseModel):
    """计算类读响应基类（列表类不挂，避免过度设计）。"""
    meta: Optional[Meta] = None


# --- Screenshot Entry (截图录入) ---
class IdentityCandidate(BaseModel):
    """身份裁决候选（同账户 active 资产项，旧命名/同期持仓）。"""
    item_id: int
    name: str
    amount: str                 # 该候选最后一条快照金额（字符串，与其它金额字段同风格）
    last_date: Optional[str] = None   # 该候选最后一条快照日期 YYYY-MM-DD


class IdentityGuess(BaseModel):
    """预览行的身份猜测（**提示，非自动裁决**，item-identity-drift D4）。

    reason 首版恒为 "same_account_new_name"；candidates 为空时不发本字段。
    """
    reason: str
    candidates: list[IdentityCandidate]


class ArchivedMergedInto(BaseModel):
    """归档项的血缘去向（spec `.scratch/archived-ingest-scope` D3，additive）。

    只在 create 行出现：本行名称在血缘上属于一个**已归档**项，且该归档项有
    `link_type='merge'` 的后继。前端据此显示「历史上已并入 X」——纯提示，
    不参与任何判定（action / 金额 / 合计都不受影响）。只认 merge：split/revision
    不是「并入」。
    """
    item_id: int                              # merge 目标（在用项）id
    display_name: str                         # 目标项现名
    archived_effective_from: Optional[str] = None   # 被命中归档项的归档生效日


class ScreenshotPreviewRow(BaseModel):
    index: int                  # 预览行号（前端编辑回传用，采纳时 identity_decisions 的键）
    source_name: Optional[str] = None
    asset_name: Optional[str] = None
    action: str                 # update | create | zero
    source_amount: Decimal
    current_amount: Optional[Decimal] = None
    diff: Decimal
    # 身份裁决（additive，仅「会新建资产项」且同账户已有疑似同源旧项时出现）
    identity_guess: Optional[IdentityGuess] = None
    # 该行命中的既有资产项（ADDITIVE）：update/zero/keep 行有，create 行为 None。
    # 前端用它把「同一项多行」去重，并在 bind 后以目标项现值为差额基线。
    item_id: Optional[int] = None
    # 疑似汇总行（ADDITIVE，ADR-0029 D7）：命中内置汇总词表或该组 aggregate 规则 → true，
    # 前端**预勾「汇总行」但可取消**。纯提示：动作仍由人给（options.reconciles）
    aggregate_hint: bool = False
    # 归档项去向（ADDITIVE，spec `.scratch/archived-ingest-scope` D3）：**仅 create 行**可能非空。
    # 命中已归档名 → 该行是 create（不是 update）+ 这里给出「历史上已并入谁」。纯提示。
    archived_merged_into: Optional[ArchivedMergedInto] = None


class ScreenshotPreviewResponse(BaseModel):
    source_group: str
    data_date: Optional[str] = None      # YYYY-MM-DD，截图内解析（无则 None）
    # 疑似日期提示契约（前端冻结字段）：suspected_date 与 data_date 同源；
    # suspected_date_text 为命中那条行的拼接原文，供展示「原文：…」判断误报。
    suspected_date: Optional[str] = None
    suspected_date_text: Optional[str] = None
    total_screenshot: Optional[Decimal] = None  # 截图合计
    total_current_system: Decimal
    existing_count: int
    image_filename: str
    rows: list[ScreenshotPreviewRow]
    # v5 步骤 6 存证前置（additive）：上传即建凭证行，预览阶段即可引用 #id
    evidence_id: Optional[int] = None
    # 来源组现有资产项的 item 级现值（ADDITIVE，ADR-0026 补遗）：
    # [{item_id, name, amount, as_of}]。前端据此算绑定行差额基线 + 系统合计去重。
    existing_items: list[dict] = []


class SupplementDropOut(BaseModel):
    """被丢弃的补录项（reason ∈ conflict_update|conflict_create|conflict_zero|
    duplicate_target；invalid 在解析层直接 400，不进此列表）。"""
    name: str
    reason: str
    asset_name: Optional[str] = None


class ScreenshotApplyResult(BaseModel):
    evidence_id: int
    data_date: str
    rows_written: int
    message: str
    supplements_written: int = 0            # 本次实际写入的补录行数
    supplements_dropped: list[SupplementDropOut] = []  # 丢弃必须可见
    # v5 写语义：create 撞同日 active 行的显式冲突清单（不静默覆盖；additive）
    conflicts: list[dict] = []
    # ADR-0019 账本即变更日志：同日同项同 (amount, side) 未写入、未写审计
    # （additive；与 ImportBatchOut / asset_service 同名同形）
    skipped_unchanged: list[dict] = []
    # ADR-0026 绑定回执：哪一行识别结果被写进了哪个既有项（additive）
    bindings: list[dict] = []
    # 绑定优先于未识别项清零：这些项本来要被 zero，因本批已绑到它们而跳过（additive）
    zero_skipped_due_to_bind: list[dict] = []
    # 汇总行回执（additive，ADR-0029 D6）：本批被标为汇总行、零写入的行
    # [{preview_index, asset_name, amount, asset_item_id}]
    reconciled: list[dict] = []
    # 汇总行优先于未识别项清零：因本批的汇总行指向而跳过的清零（additive，同 bind 形状）
    zero_skipped_due_to_reconcile: list[dict] = []


class EvidenceGroupRef(BaseModel):
    """凭证的归属来源组（evidence-manager D1）：id 是身份，slug/label 是当前展示值。"""
    id: int
    slug: str
    label: str


class EvidenceListItem(BaseModel):
    """凭证列表单条（spec D1）。`used_rows` 为**全行**口径（含被更正的历史行）。

    `image_meta` / `recognition_summary` 是 evidence-naming 的**加法**字段（ADR-0022 /
    ADR-0023）：

    - `image_meta`：库里 `image_meta` 列反序列化后的对象（白名单
      `width/height/bytes/format` + `exif` 子集）；无数据或解析失败均为 None。
    - `recognition_summary`：识别快照的**摘要**，不是快照本体——完整
      `recognition_json`（含 OCR 原始块与全部识别行）体积可达 1 MiB，列表接口不吐它，
      只给「哪次、什么引擎、识别多少行、采纳多少行、丢没丢块」。
    """
    id: int
    source_group: EvidenceGroupRef
    file_path: str
    original_filename: Optional[str] = None
    data_date: Optional[date] = None          # 未采纳富化前为 None（上传即存证）
    engine: Optional[str] = None              # "ocr" | "ai"；历史行 NULL＝ocr
    created_at: datetime
    used_rows: int
    image_meta: Optional[dict] = None                  # ADR-0022；未回填/读不出 → None
    recognition_summary: Optional[dict] = None         # ADR-0023；未识别过 → None


class EvidenceListResponse(BaseModel):
    """凭证列表响应（分页壳对齐 routers/assets.py 列表惯例）。"""
    items: list[EvidenceListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class EvidencePurgeRequest(BaseModel):
    """批量清理未使用凭证的入参（spec D5）。

    `dry_run` 默认 **True**：不传参数时只出「将删清单 + 总字节」，库与盘零变化；
    前端必须先把清单摆给用户确认，才发 `dry_run=false`。
    """
    source_group_id: Optional[int] = None     # 限定清理范围（不传＝全库）
    dry_run: bool = True
    limit: int = Field(default=200, ge=1, le=1000)   # 防一次删穿


class EvidenceRecognitionRequest(BaseModel):
    """AI 识别结果回写的入参（evidence-recognition-writeback D2）。

    `recognition` 与 `/apply` 的同名回传字段**完全同构**——数组形态
    `[{name, amount}, …]` 或对象形态 `{items, total, date_text}`；不新立第二套结构，
    校验与规范化沿用 `evidence_service.parse_client_recognition()`（同一实现）。

    用 `Any` 而非具体模型：两种合法形态都在同一个字段里，形状校验放在服务层
    （与 `/apply` 同一处），避免规则在这里分叉；**键本身必填**（缺键由 FastAPI 拦成
    422 `VALIDATION_ERROR`，显式 null 由路由拦成同一个码）。
    """
    recognition: Any


# --- Import ---
class ImportPreviewRow(BaseModel):
    row_num: int
    asset_date: date
    liquidity_rating: str
    fund_type: str
    asset_name: str
    account: str
    owner: str = ""
    amount: Decimal
    has_conflict: bool = False
    error: Optional[str] = None


class NewAttribute(BaseModel):
    """导入时发现的新属性"""
    type: str  # "liquidity_rating" | "fund_type" | "account" | "owner"
    name: str
    rows: list[int]  # 涉及的行号列表


class ImportPreviewResponse(BaseModel):
    batch_id: int
    filename: str
    valid_rows: list[ImportPreviewRow]
    invalid_rows: list[ImportPreviewRow]
    total_rows: int
    conflict_count: int
    new_attributes: list[NewAttribute] = []  # 新属性列表


class ImportBatchOut(BaseModel):
    id: int
    filename: str
    record_count: int
    imported_at: datetime
    status: str
    # v5 写语义：同日同项 active 冲突显式清单（不静默覆盖；additive）
    skipped: list[dict] = []
    # ADR-0019 账本即变更日志：同日同项同 (amount, side) 未建行、未写审计
    skipped_unchanged: list[dict] = []
    # 分类只补空不覆盖、字典查不到等非阻断性提示
    warnings: list[dict] = []

    model_config = {"from_attributes": True}


# --- Analysis ---
class DriftAddedItem(BaseModel):
    """漂移诊断：该点「首次出现」的资产项（spec §3，additive）。

    `amount` 为该点口径下计入该项的值（首录当日即其首行金额）。
    """
    item_id: Optional[int] = None   # 无 item 的存量行（双轨回退口径）为 None
    name: str
    amount: Decimal

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class DriftVanishedItem(DriftAddedItem):
    """漂移诊断：该点仍在沿用、但已无后继的「幽灵值」项（spec §3，additive）。

    `amount` 为沿用的旧值，`last_date` 为该旧值所在行的日期。
    """
    last_date: date


class TrendPoint(BaseModel):
    period: str
    date: date
    total_amount: Decimal
    net_assets: Decimal
    liabilities: Decimal
    # 沿用事实（§2.2 改判，ADR-0019 D12 / db-rebuild D4）：账本是变更日志，
    # 「该日没有某项的记录」＝值没变，不判「部分录入」。只暴露可核对的事实。
    carried_items: int = 0         # 计入项中，当前值来自更早估值日的项数
    carried_max_days: int = 0      # 这些项里最久的已沿用天数（无沿用 → 0）
    # 身份漂移诊断（spec §3，additive）：空数组＋false＝无漂移，旧前端忽略即兼容
    added: list[DriftAddedItem] = []
    vanished: list[DriftVanishedItem] = []
    drift_suspect: bool = False
    drift_amount: Decimal = Decimal("0")

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class ComparisonPoint(BaseModel):
    period: str
    date: date
    total: Decimal
    change_amount: Decimal
    change_percent: Optional[float] = None
    # 与趋势点同源的沿用事实（派生自 TrendPoint，不另算）
    carried_items: int = 0
    carried_max_days: int = 0

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class MixedChartData(MetaMixin):
    trend: list[TrendPoint]
    comparison: list[ComparisonPoint]


class ItemTrendPoint(BaseModel):
    period: str
    date: date
    amount: Decimal
    change_amount: Optional[Decimal] = None

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class SummaryData(MetaMixin):
    latest_date: Optional[date] = None
    total_assets: Decimal = Decimal("0")
    total_liabilities: Decimal = Decimal("0")
    net_worth: Decimal = Decimal("0")
    change_amount: Decimal = Decimal("0")
    change_percent: Optional[float] = None
    record_count: int = 0
    snapshot_count: int = 0
    # 分类披露（additive，ADR-0019 D9 的 draft 状态）：`status='draft'` 的资产项
    # **照常计入**上面三个合计（钱是真实存在的），这三个字段只回答「合计里有多少
    # 来自尚未补齐分类的项」——按 side 拆毛额，便于界面一句人话说清规模。
    # 只统计该 as-of 日有有效记录的 draft 项；无记录的项不进 count（净值里也没有它）。
    unclassified_count: int = 0
    unclassified_asset_amount: Decimal = Decimal("0")
    unclassified_liability_amount: Decimal = Decimal("0")

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


# --- Alert Rule ---
class AlertRuleCreate(BaseModel):
    name: str
    dimension: str  # asset_name, fund_type, liquidity_rating, account
    target_id: Optional[str] = None
    period_type: str  # day, month, quarter, year
    compare_type: str  # previous, custom
    compare_period: Optional[str] = None
    amount_threshold: Optional[Decimal] = None
    percent_threshold: Optional[Decimal] = None
    direction: str = "both"  # up, down, both


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    dimension: Optional[str] = None
    target_id: Optional[str] = None
    period_type: Optional[str] = None
    compare_type: Optional[str] = None
    compare_period: Optional[str] = None
    amount_threshold: Optional[Decimal] = None
    percent_threshold: Optional[Decimal] = None
    direction: Optional[str] = None
    is_active: Optional[bool] = None


class AlertRuleOut(BaseModel):
    id: int
    name: str
    dimension: str
    target_id: Optional[str] = None
    period_type: str
    compare_type: str
    compare_period: Optional[str] = None
    amount_threshold: Optional[Decimal] = None
    percent_threshold: Optional[Decimal] = None
    direction: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class AlertDetailItem(BaseModel):
    """预警细项详情"""
    id: str  # 细项ID（资产名称、类型ID、评级名称、账户ID）
    name: str  # 细项名称
    current_amount: Decimal
    compare_amount: Decimal
    change_amount: Decimal
    change_percent: Optional[float] = None
    triggered: bool  # 该细项是否触发预警

    model_config = {"json_encoders": {Decimal: str}}


# --- Export History ---
class ExportHistoryOut(BaseModel):
    """导出历史记录输出"""
    id: int
    export_time: datetime
    export_type: str  # manual, auto
    filename: str
    file_size: Optional[int] = None
    operator: Optional[str] = None
    rule_name: Optional[str] = None
    file_path: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExportHistoryList(BaseModel):
    """导出历史记录列表"""
    items: list[ExportHistoryOut]
    total: int
    page: int
    page_size: int


# --- Auto Export Rule ---
class AutoExportRuleCreate(BaseModel):
    """创建自动导出规则"""
    name: str
    cron_expression: str
    export_format: str = "csv"  # csv, json
    filename_template: Optional[str] = None


class AutoExportRuleUpdate(BaseModel):
    """更新自动导出规则"""
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    export_format: Optional[str] = None
    filename_template: Optional[str] = None
    is_active: Optional[bool] = None


class AutoExportRuleOut(BaseModel):
    """自动导出规则输出"""
    id: int
    name: str
    cron_expression: str
    export_format: str
    filename_template: Optional[str] = None
    is_active: bool
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CronValidationRequest(BaseModel):
    """Cron 表达式验证请求"""
    cron_expression: str


class CronValidationResponse(BaseModel):
    """Cron 表达式验证响应"""
    valid: bool
    description: str
    next_run_times: list[datetime] = []


class AlertResult(BaseModel):
    rule_id: int
    rule_name: str
    dimension: str
    target_name: str
    current_period: str
    compare_period: Optional[str] = None  # 改为可选，无上一期数据时为null
    current_amount: Decimal
    compare_amount: Decimal
    change_amount: Decimal
    change_percent: Optional[float] = None
    amount_threshold: Optional[Decimal] = None
    percent_threshold: Optional[Decimal] = None
    direction: str
    triggered: bool
    details: list[AlertDetailItem] = []  # 细项详情列表（当target_id为null时填充）
    message: Optional[str] = None  # 提示信息（如无上一期数据时）

    model_config = {"json_encoders": {Decimal: str}}


# --- Allocation Target ---
class AllocationTargetCreate(BaseModel):
    dimension: str  # asset_class / liquidity / institution / channel / risk_level（risk 同义）
    target_id: str
    parent_id: Optional[int] = None
    target_percent: Decimal
    alert_threshold: Optional[Decimal] = Decimal("5.00")  # 黄阈值·绝对pp，默认5%
    alert_threshold_red: Optional[Decimal] = None  # 红阈值·绝对pp；NULL=2×黄（0017）
    relative_threshold: Optional[Decimal] = None   # 黄阈值·相对%；NULL=25（0017）


class AllocationTargetUpdate(BaseModel):
    target_percent: Optional[Decimal] = None
    parent_id: Optional[int] = None
    alert_threshold: Optional[Decimal] = None  # 黄阈值·绝对pp
    alert_threshold_red: Optional[Decimal] = None  # 红阈值·绝对pp（显式 null＝清空→兜底）
    relative_threshold: Optional[Decimal] = None   # 黄阈值·相对%（显式 null＝清空→兜底）


class AllocationTargetOut(BaseModel):
    id: int
    dimension: str
    target_id: str
    parent_id: Optional[int] = None
    target_percent: Decimal
    alert_threshold: Decimal  # 黄阈值·绝对pp
    alert_threshold_red: Optional[Decimal] = None  # 红阈值·绝对pp（NULL=2×黄）
    relative_threshold: Optional[Decimal] = None   # 黄阈值·相对%（NULL=25）
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class AllocationBand(BaseModel):
    """逐目标行的有效带型（C2 双阈值；三期-2 ADR-0012 D2）。"""
    state: str                                    # green | yellow | red | gray
    yellow: str                                   # 有效黄阈值（pp）
    red: str                                      # 有效红阈值（pp）
    source: str                                   # ③ 人设行 + ④ code_default 标注
    components: Optional[dict] = None


class AllocationAnalysisItem(BaseModel):
    """资产配置分析单项"""
    id: str  # 项目ID
    name: str  # 项目名称
    parent_id: Optional[str] = None  # 父级ID
    level: int  # 层级
    target_percent: Optional[Decimal] = None  # 目标比例
    actual_percent: Decimal  # 实际比例
    actual_amount: Decimal  # 实际金额
    deviation: Decimal  # 偏离值（实际-目标）
    deviation_percent: Optional[Decimal] = None  # 相对偏离率
    deviation_amount: Optional[Decimal] = None  # 偏离金额
    recommendation: str  # 调整建议
    priority: Optional[int] = None  # 调整优先级（red 200+/yellow 100+/green 0）
    alert_threshold: Optional[Decimal] = None  # 黄阈值·绝对pp（兼容旧前端）
    band: Optional[AllocationBand] = None  # 有效带型（无目标 → null）
    children: list["AllocationAnalysisItem"] = []  # 子项

    model_config = {"json_encoders": {Decimal: str}}


class AllocationAnalysis(MetaMixin):
    """资产配置分析结果"""
    dimension: str
    snapshot_date: date
    total_amount: Decimal
    items: list[AllocationAnalysisItem]
    unallocated_percent: Decimal  # 未配置比例

    model_config = {"json_encoders": {Decimal: str}}


# --- Expense (支出录入) ---
class ExpenseCategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ExpenseCategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class FixedExpenseTemplateCreate(BaseModel):
    name: str
    amount: str
    cycle: str  # monthly | quarterly | yearly
    start_date: str  # YYYY-MM-DD
    end_date: Optional[str] = None
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None
    active: bool = True


class FixedExpenseTemplateUpdate(BaseModel):
    name: Optional[str] = None
    amount: Optional[str] = None
    cycle: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None
    active: Optional[bool] = None


class ExpenseCreate(BaseModel):
    expense_date: str  # YYYY-MM-DD
    expense_name: str
    amount: str
    expense_type: str  # fixed | large_oneoff
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None


class ExpenseUpdate(BaseModel):
    expense_date: Optional[str] = None
    expense_name: Optional[str] = None
    amount: Optional[str] = None
    expense_type: Optional[str] = None
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    note: Optional[str] = None


class ExpenseGenerateItem(BaseModel):
    template_id: int
    amount: Optional[str] = None
    expense_date: Optional[str] = None


class ExpenseGenerateRequest(BaseModel):
    period: str  # YYYY-MM
    conflict_resolution: str = "skip"  # skip | overwrite
    items: list[ExpenseGenerateItem] = []


# --- Metrics（三期-0 指标骨架，spec §3 契约） ---
class MetricTags(BaseModel):
    """受控标签集（口径层级/形态/时间语义/方向/用途/来源/信赖度）。"""
    layer: str
    form: str
    time: str
    direction: str
    purpose: str
    source: str
    trust: str


class MetricCriterion(BaseModel):
    """判据描述（kind/source）；无判据＝null（本期不预置默认带）。"""
    kind: str
    source: Optional[str] = None


class MetricDrill(BaseModel):
    """下钻引用：前端路径/锚点＋展示名。"""
    target: str
    label: Optional[str] = None


class MetricDictionaryItem(BaseModel):
    id: str
    name: str
    domain: str
    tags: MetricTags
    unit: str
    direction: str
    criterion: Optional[MetricCriterion] = None
    as_of_semantics: str
    drill: Optional[MetricDrill] = None


class MetricDictionary(BaseModel):
    """指标字典（静态、无 meta）。"""
    items: list[MetricDictionaryItem]


class MetricBand(BaseModel):
    """判据带：threshold 恒为有效黄阈值；red/components 为三期-2 加性字段。"""
    kind: str
    threshold: str
    source: str
    red: Optional[str] = None
    components: Optional[dict] = None


class MetricGrade(BaseModel):
    """灯：green | yellow | red | gray；无判据＝null 不构第五态。"""
    state: str
    band: Optional[MetricBand] = None
    basis: str = "current"
    note: Optional[str] = None


class MetricGate(BaseModel):
    """口径闸门：code/note/blocking（阻断→⚪）。"""
    code: str
    note: str
    blocking: bool = False


class MetricStatusItem(BaseModel):
    id: str
    value: Optional[str] = None
    unit: str
    direction: str
    grade: Optional[MetricGrade] = None
    gates: list[MetricGate] = []
    tags: MetricTags
    details: Optional[dict] = None
    drill: Optional[MetricDrill] = None


# --- B4 域级汇总与异动（三期-2 W4，spec §3/§5.1；全部加性） ---
class MetricDomainCounts(BaseModel):
    """域内 item 灯计数（行级红只进 reds，不撑大 red）。"""
    green: int = 0
    yellow: int = 0
    red: int = 0
    gray: int = 0
    unjudged: int = 0


class MetricRedSignal(BaseModel):
    """红信号（kind=item 单灯 / kind=row 复合项展开行）。"""
    kind: str
    id: Optional[str] = None
    name: Optional[str] = None
    domain: Optional[str] = None
    dimension: Optional[str] = None
    target_id: Optional[str] = None
    deviation: Optional[str] = None
    drill: Optional[MetricDrill] = None


class MetricDomain(BaseModel):
    """域灯（注册表 domain；一票否决，不做加权；不设全局灯）。"""
    domain: str
    name: str
    state: str
    counts: MetricDomainCounts
    judged: int = 0
    total: int = 0
    worst: Optional[str] = None          # red>yellow>green（不含 gray）
    gray_blocked: bool = False
    active_events: int = 0
    event_state: Optional[str] = None
    reds: list[MetricRedSignal] = []
    needs_diagnosis: bool = False
    drill: Optional[MetricDrill] = None


class MetricEventThresholds(BaseModel):
    amount: Optional[str] = None
    percent: Optional[str] = None


class MetricEvent(BaseModel):
    """异动＝事件（上升沿）：new 本期触发且上一有数据账期未触发；ongoing 连续触发。"""
    id: str
    rule_id: int
    rule_name: str
    domain: Optional[str] = None
    severity: str = "yellow"
    status: str                           # new | ongoing
    period: str
    period_end: str
    previous_period: Optional[str] = None
    change_amount: str
    change_percent: Optional[str] = None
    thresholds: MetricEventThresholds
    direction: Optional[str] = None
    drill: Optional[MetricDrill] = None


class MetricSummary(BaseModel):
    """计数汇总（不设总分/全局灯）。"""
    red: int = 0
    yellow: int = 0
    green: int = 0
    gray: int = 0
    unjudged: int = 0
    judged: int = 0
    total: int = 0
    domains: int = 0
    active_events: int = 0
    new_events: int = 0


class MetricStatus(BaseModel):
    """指标状态面（计算类、顶层 meta）＋ B4 域级汇总（加性）。"""
    meta: Meta
    items: list[MetricStatusItem]
    domains: list[MetricDomain] = []
    events: list[MetricEvent] = []
    summary: MetricSummary = Field(default_factory=MetricSummary)


# --- Advisor（三期-3 W2：信息集/门契约；无路由，W4/W5 消费） ---
class AdvisorInfoEntryOut(BaseModel):
    key: str
    kind: str
    label: str
    value: str
    value_scaled: Optional[str] = None
    unit: str
    trust: str
    source: str
    source_ref: Optional[str] = None
    as_of: Optional[date] = None


class AdvisorInfoSetSummary(BaseModel):
    id: int
    created_at: str
    as_of: Optional[date] = None
    goal_id: Optional[int] = None
    goal_version_id: Optional[int] = None
    builder_version: str
    entry_count: int
    payload_sha256: str


class AdvisorGateCheck(BaseModel):
    code: str
    severity: str          # blocking | warning
    passed: bool
    detail: Optional[str] = None


class AdvisorGateResult(BaseModel):
    result: str            # passed | failed
    checks: list[AdvisorGateCheck]
    checked_at: str
    gate_version: str


class AdvisorCreateRequest(BaseModel):
    """手动触发（其余两型由内部触发，不经此 body）。"""
    horizon: Optional[str] = None
    goal_id: Optional[int] = None
    note: Optional[str] = None


class AdvisorDecisionRequest(BaseModel):
    reason: Optional[str] = None


class AdvisorExecutionRequest(BaseModel):
    execution_status: str
    note: Optional[str] = None


class AdvisorSettingsUpdate(BaseModel):
    auto_enabled: Optional[bool] = None
    cooldown_days: Optional[int] = None
    default_horizon: Optional[str] = None


class ReviewCreateRequest(BaseModel):
    goal_id: Optional[int] = None
    as_of: Optional[date] = None
    start_as_of: Optional[date] = None
    suggestion_ids: list[int] = []
    label: Optional[str] = None
    note: Optional[str] = None


class ReviewConclusionRequest(BaseModel):
    attribution: Optional[list[dict]] = None
    conclusion: Optional[str] = None


class ReviewCloseRequest(BaseModel):
    conclusion: Optional[str] = None


class ReviewProposalCreate(BaseModel):
    kind: str
    subject_ref: Optional[str] = None
    payload: dict
    rationale: str


class ReviewProposalDecision(BaseModel):
    reason: Optional[str] = None
    applied_kind: Optional[str] = None
    applied_ref: Optional[str] = None


class ReviewProposalDismiss(BaseModel):
    reason: Optional[str] = None


# --- Goals（三期-1 C1：目标版本与达成度，spec §5 契约） ---
class GoalScope(BaseModel):
    """目标范围：本期 API 只放行 family（member 422，四期启用）。"""
    kind: str = "family"
    owner_id: Optional[int] = None


class GoalCreate(BaseModel):
    """创建目标＋首个版本（target_amount 为净资产目标，字符串 Decimal）。"""
    name: str
    scope: Optional[GoalScope] = None
    target_amount: str
    target_date: date
    target_rate_annual: Optional[str] = None
    risk_profile: Optional[str] = None
    life_stage: Optional[str] = None
    reason: str
    activate: bool = True


class GoalRename(BaseModel):
    """仅改名称（版本字段改动一律新建版本）。"""
    name: str


class GoalVersionCreate(BaseModel):
    """新版本（金额/期限/可选年化/分区/理由）；activate 时同事务取代旧 active。"""
    target_amount: str
    target_date: date
    target_rate_annual: Optional[str] = None
    risk_profile: Optional[str] = None
    life_stage: Optional[str] = None
    reason: str
    activate: bool = True


class GoalVersionUpdate(BaseModel):
    """改 draft 内容；仅请求体出现的字段生效（显式 null 用于清空可空列）。"""
    target_amount: Optional[str] = None
    target_date: Optional[date] = None
    target_rate_annual: Optional[str] = None
    risk_profile: Optional[str] = None
    life_stage: Optional[str] = None
    reason: Optional[str] = None


# --- 资产分类（ADR-0019）---
# 七维分类的唯一对外契约：单值维度用显式 id/档位，多值的标签用 id 数组。
# 旧契约的 `attribute_ids: number[]`（把五类 id 压进一个数组）在此废止。

class AssetClassCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None
    code: Optional[str] = None          # 缺省按层级自动编号（根=A..Z，子=父code+序号）
    side_scope: Optional[str] = None    # asset | liability | any，缺省继承父级/根为 any


class AssetClassUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    code: Optional[str] = None
    side_scope: Optional[str] = None


class AssetClassOut(BaseModel):
    """资产类别节点的对外形状（`GET /api/classification/asset-classes` 列表项 +
    `POST`/`PUT` 的返回值**同一个形状**，前端不需要在两套类型之间切换）。

    `path` 为根到本节点的 `code name` 串（如 `A 现金与等价物/A1 活期存款`）；
    `leaf` 表示该节点能否直接挂到资产项（有子节点者不可引用）。
    """
    id: int
    code: str
    name: str
    parent_id: Optional[int] = None
    level: int
    side_scope: str
    path: str
    leaf: bool


class TagRef(BaseModel):
    """分类视图里的标签引用（只 id + 名字，标签字典的完整字段走 /classification/tags）。"""
    id: int
    name: str


class ItemClassificationOut(BaseModel):
    """资产项分类视图（`GET|PUT /api/asset-items/{id}/classification` 的响应）。

    两处容易混的字段名，这里一次说清：

    - `missing_dims`：**规范名**（本端点的既有契约），值＝列名，如 `owner_id`；
    - `classification_missing`：与 `/api/assets`、`/api/assets/current` 对齐的
      **兼容别名**，与 `missing_dims` 同值。过渡期两者并存、不得只留一个；
    - `record_count`：该项的**有效**记录行数（`assets.superseded_by IS NULL`），
      即「改分类会一起改变口径」的历史记录条数——前端直接影响面文案用它，
      不要从 message 里抠中文数字。
    """
    item_id: int
    display_name: str
    status: str
    asset_class_id: Optional[int] = None
    asset_class_name: Optional[str] = None
    asset_class_code: Optional[str] = None
    liquidity_days: Optional[int] = None
    liquidity_label: Optional[str] = None
    institution_id: Optional[int] = None
    institution_name: Optional[str] = None
    channel_id: Optional[int] = None
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
    risk_level: Optional[str] = None
    tags: list[TagRef] = []
    missing_dims: list[str]
    classification_missing: list[str]
    record_count: int


class InstitutionCreate(BaseModel):
    name: str
    kind: str = "other"                 # bank|broker|fund|insurer|gov|platform|other


class InstitutionUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    active: Optional[bool] = None


class ChannelCreate(BaseModel):
    name: str


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None


class MemberCreate(BaseModel):
    name: str
    role: str                            # self|spouse|joint|child|nominee


class MemberUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


class TagCreate(BaseModel):
    # trim 后最多 20 字：与设置页 `maxLength: 20` 同口径（spec `.scratch/tag-create-in-sheet`
    # D5）。原串长度 ≥ trim 后长度，故限制原串即保证 trim 后不超限。
    name: str = Field(..., max_length=20)
    kind: str = "free"                   # controlled|free


class TagUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None


class ItemClassificationUpdate(BaseModel):
    """只改传进来的维度；四个必填维度不能置 null（要清空请把项转 draft）。"""
    asset_class_id: Optional[int] = None
    liquidity_days: Optional[int] = None
    institution_id: Optional[int] = None
    channel_id: Optional[int] = None
    owner_id: Optional[int] = None
    risk_level: Optional[str] = None
    reason: Optional[str] = None


class ItemTagsUpdate(BaseModel):
    tag_ids: list[int] = []
    reason: Optional[str] = None


class BatchClassificationInput(BaseModel):
    item_ids: list[int]
    asset_class_id: Optional[int] = None
    liquidity_days: Optional[int] = None
    institution_id: Optional[int] = None
    channel_id: Optional[int] = None
    owner_id: Optional[int] = None
    risk_level: Optional[str] = None
    reason: Optional[str] = None
