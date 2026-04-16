from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel


# --- Fund Type ---
class FundTypeOut(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    level: int
    children: list["FundTypeOut"] = []

    model_config = {"from_attributes": True}


# --- Account ---
class AccountOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


# --- Liquidity Rating ---
class LiquidityRatingBase(BaseModel):
    name: str
    sort_order: int = 0


class LiquidityRatingCreate(LiquidityRatingBase):
    pass


class LiquidityRatingUpdate(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None


class LiquidityRatingOut(BaseModel):
    id: int
    name: str
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Asset Record ---
class AssetRecordCreate(BaseModel):
    asset_date: date
    liquidity_rating_id: int
    fund_type_id: int
    asset_name: str
    account_id: int
    amount: Decimal


class AssetRecordUpdate(BaseModel):
    asset_date: Optional[date] = None
    liquidity_rating_id: Optional[int] = None
    fund_type_id: Optional[int] = None
    asset_name: Optional[str] = None
    account_id: Optional[int] = None
    amount: Optional[Decimal] = None


class AssetRecordOut(BaseModel):
    id: int
    asset_date: date
    liquidity_rating_id: int
    liquidity_rating_name: Optional[str] = None
    fund_type_id: int
    fund_type_name: Optional[str] = None
    asset_name: str
    account_id: int
    account_name: Optional[str] = None
    amount: Decimal
    import_batch_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AssetRecordBatchCreate(BaseModel):
    records: list[AssetRecordCreate]


class AssetRecordBatchUpdate(BaseModel):
    ids: list[int]
    liquidity_rating_id: Optional[int] = None
    fund_type_id: Optional[int] = None
    asset_name: Optional[str] = None
    account_id: Optional[int] = None


class AssetRecordBatchDelete(BaseModel):
    ids: list[int]


class AssetHistoryByNameQuery(BaseModel):
    """根据资产名称查询历史记录的请求"""
    asset_name: str


class AssetHistoryByNameResponse(BaseModel):
    """根据资产名称查询历史记录的响应"""
    total_count: int
    sample_records: list[AssetRecordOut]
    all_ids: list[int]


# --- Copy From Last ---
class CopyFromLastRequest(BaseModel):
    target_date: date
    period_type: str  # day, month, quarter, year


# --- Batch Create By Period ---
class AssetRecordTemplate(BaseModel):
    """资产记录模板（不含日期，用于批量创建）"""
    model_config = {"arbitrary_types_allowed": True}
    
    liquidity_rating_id: int
    fund_type_id: int
    asset_name: str
    account_id: int
    amount: Decimal


class BatchCreateByPeriodRequest(BaseModel):
    """批量按账期添加记录请求"""
    record_template: AssetRecordTemplate  # 记录模板（不含日期）
    period_type: str  # day, month, quarter, year
    start_period: Optional[str] = None  # 开始账期，null表示从最早账期开始
    end_period: Optional[str] = None  # 结束账期，null表示到最新账期结束
    conflict_resolution: str = "skip"  # 冲突处理方式: skip(跳过) 或 overwrite(覆盖)


class BatchCreateByPeriodResult(BaseModel):
    """批量按账期添加记录结果"""
    success: bool
    created_count: int
    periods: list[str]  # 处理的账期列表
    records: list[AssetRecordOut]  # 创建的记录
    message: str


# --- Import ---
class ImportPreviewRow(BaseModel):
    row_num: int
    asset_date: date
    liquidity_rating: str
    fund_type: str
    asset_name: str
    account: str
    amount: Decimal
    has_conflict: bool = False
    error: Optional[str] = None


class NewAttribute(BaseModel):
    """导入时发现的新属性"""
    type: str  # "liquidity_rating" | "fund_type" | "account"
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


class ImportConfirmRequest(BaseModel):
    batch_id: int
    conflict_resolution: str = "skip"  # skip or overwrite
    attribute_actions: dict[str, str] = {}  # 属性操作: {"属性名": "create" | "ignore"}


class ImportBatchOut(BaseModel):
    id: int
    filename: str
    record_count: int
    imported_at: datetime
    status: str

    model_config = {"from_attributes": True}


# --- Analysis ---
class TrendPoint(BaseModel):
    period: str
    date: date
    total_amount: Decimal
    net_assets: Decimal
    liabilities: Decimal

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class ComparisonPoint(BaseModel):
    period: str
    date: date
    total: Decimal
    change_amount: Decimal
    change_percent: Optional[float] = None

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class MixedChartData(BaseModel):
    trend: list[TrendPoint]
    comparison: list[ComparisonPoint]


class ItemTrendPoint(BaseModel):
    period: str
    date: date
    amount: Decimal
    change_amount: Optional[Decimal] = None

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


class SummaryData(BaseModel):
    latest_date: Optional[date] = None
    total_assets: Decimal = Decimal("0")
    total_liabilities: Decimal = Decimal("0")
    net_worth: Decimal = Decimal("0")
    change_amount: Decimal = Decimal("0")
    change_percent: Optional[float] = None
    record_count: int = 0
    snapshot_count: int = 0

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


# --- Pagination ---
class PaginatedResponse(BaseModel):
    items: list[AssetRecordOut]
    total: int
    page: int
    page_size: int
    total_pages: int


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
    dimension: str  # fund_type, liquidity_rating, account
    target_id: str
    parent_id: Optional[int] = None
    target_percent: Decimal


class AllocationTargetUpdate(BaseModel):
    target_percent: Optional[Decimal] = None
    parent_id: Optional[int] = None


class AllocationTargetOut(BaseModel):
    id: int
    dimension: str
    target_id: str
    parent_id: Optional[int] = None
    target_percent: Decimal
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "json_encoders": {Decimal: str}}


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
    priority: Optional[int] = None  # 调整优先级（数值越大优先级越高）
    children: list["AllocationAnalysisItem"] = []  # 子项

    model_config = {"json_encoders": {Decimal: str}}


class AllocationAnalysis(BaseModel):
    """资产配置分析结果"""
    dimension: str
    snapshot_date: date
    total_amount: Decimal
    items: list[AllocationAnalysisItem]
    unallocated_percent: Decimal  # 未配置比例

    model_config = {"json_encoders": {Decimal: str}}
