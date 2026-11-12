from datetime import date
from typing import NoReturn, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError, ErrorCode
from schemas import (
    SummaryData, TrendPoint, ComparisonPoint, MixedChartData,
    ItemTrendPoint,
)
from services import (
    analysis_service, appreciation_service, attribution_service,
    classification_service,
)

router = APIRouter()

# 旧分类维度端点已随 ADR-0019 下线（`attributes` / `asset_attributes` 两表删除，
# 分类改成资产项级单值列）。三个旧路径一律 410 + 指路，而不是 404：
# 调用方（含前端旧版）拿到的必须是「这个名字永久没了，请改用 X」，
# 否则会被当成「后台还没实现」继续重试。
ANALYSIS_DIMENSION_REMOVED = "ANALYSIS_DIMENSION_REMOVED"
# 新值域的枚举入口（旧调用方缺的就是这张「有哪些合法取值」的表）。
# `risk_level` 没有字典表——R1..R5 是列上的 CHECK 白名单，随项级分类一起读。
VOCABULARY = {
    "asset_class": "/api/classification/asset-classes",
    "liquidity": "/api/classification/liquidity-levels",
    "institution": "/api/classification/institutions",
    "channel": "/api/classification/channels",
    "owner": "/api/classification/members",
    "tag": "/api/classification/tags",
    "risk_level": "/api/asset-items/{item_id}/classification",
}


def _deprecated_dimension(legacy: str, use: str, **extra) -> NoReturn:
    raise ApiError(
        ANALYSIS_DIMENSION_REMOVED, 410,
        f"{legacy} 已下线（ADR-0019：分类从记录级上移到资产项）：请改用 {use}",
        {"use": use, "deprecated": legacy, "vocabulary": VOCABULARY, **extra},
    )


@router.get("/appreciation", response_model=dict)
def appreciation(
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """升值表（二期·2.1）：组合卡片＋逐项表；as_of 缺省＝最新快照日（历史定格）。"""
    return appreciation_service.get_appreciation(db, as_of)


@router.get("/returns", response_model=dict)
def returns(
    scope: str = Query("portfolio"),
    id: Optional[int] = None,
    period: Optional[str] = None,
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """双口径收益（二期·2.2）：TWR（衡量策略）＋XIRR（衡量本人操作），只读。

    scope=portfolio|item（item 需 id）；窗口＝period 账期标签 ＞ date_from/date_to
    ＞ 全历史。率不可得一律 null＋meta.degradations（RATE_UNAVAILABLE／
    TWR_LARGE_FLOW_APPROX），绝不编数。
    """
    if scope not in ("portfolio", "item"):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "scope 仅支持 portfolio|item", details={"field": "scope"})
    if scope == "item" and id is None:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "scope=item 需提供 id", details={"field": "id"})
    return analysis_service.get_returns(db, scope=scope, item_id=id, period=period,
                                        period_type=period_type,
                                        date_from=date_from, date_to=date_to)


@router.get("/attribution", response_model=dict)
def attribution(
    scope: str = Query("portfolio"),
    id: Optional[int] = None,
    period: Optional[str] = None,
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """期间归因（v5 二期增强，规格 .scratch/v5-attribution/spec.md）：
    Δ净资产＝外部净投入＋内部转移净额＋内生变动（残差）。只读。

    scope=portfolio|item（item 需 id）；窗口＝period ＞ date_from/date_to ＞ 全历史。
    参数错误 → 422 VALIDATION_ERROR（details.fields 定位）；项不存在 → 404。
    """
    fields: list[dict] = []
    if scope not in ("portfolio", "item"):
        fields.append({"field": "scope", "message": "仅支持 portfolio|item"})
    if scope == "item" and id is None:
        fields.append({"field": "id", "message": "scope=item 必填"})
    if date_from and date_to and date_from > date_to:
        fields.append({"field": "date_from", "message": "不得晚于 date_to"})
    if fields:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "归因参数不合法",
                       details={"fields": fields})
    return attribution_service.get_attribution(
        db, scope=scope, item_id=id, period=period, period_type=period_type,
        date_from=date_from, date_to=date_to)


@router.get("/summary", response_model=SummaryData)
def summary(db: Session = Depends(get_db)):
    return analysis_service.get_summary(db)


@router.get("/total-trend", response_model=list[TrendPoint])
def total_trend(
    period_type: str = Query("day"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_total_trend(db, period_type, date_from, date_to)


@router.get("/period-comparison", response_model=list[ComparisonPoint])
def period_comparison(
    period_type: str = Query("day"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_period_comparison(db, period_type, date_from, date_to)


@router.get("/mixed-chart", response_model=MixedChartData)
def mixed_chart(
    period_type: str = Query("day"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_mixed_chart(db, period_type, date_from, date_to)


@router.get("/by-item", response_model=None)
def by_item(
    asset_name: Optional[str] = Query(None),
    item_id: Optional[int] = Query(None),
    family: bool = Query(True),
    period_type: str = Query("day"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """分项趋势：`asset_name`＝旧单身份数组（向后兼容）；`item_id`＝身份族结构。

    两条口径响应形状不同，故 `response_model=None`：
    - `asset_name`：沿用 `get_trend_by_item` 的 `list[ItemTrendPoint]` 数组；
    - `item_id`：`{item, points:[{period,date,total,self,members}]}`，
      `family` 缺省 true（族合计），false 等价单身份读数。
    """
    if asset_name is not None and item_id is not None:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "asset_name 与 item_id 只能二选一",
                       details={"field": "item_id"})
    if asset_name is not None:
        return analysis_service.get_trend_by_item(
            db, asset_name, period_type, date_from, date_to)
    if item_id is None:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "需提供 asset_name 或 item_id",
                       details={"field": "item_id"})
    return analysis_service.get_item_trend(
        db, item_id, family=family, period_type=period_type,
        date_from=date_from, date_to=date_to)


@router.get("/by-class", response_model=list[ItemTrendPoint])
def by_class(
    asset_class_id: int = Query(..., description="资产类别 id（含其所有后代类别）"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按资产类别（**含所有后代**）的 as-of 趋势——取代旧 `by-type`。

    分类是**资产项级**属性（ADR-0019 D2），`asset_class_id` 是类别树节点 id，
    与旧 `fund_type_id`（已删除的 `attributes` 节点 id）不是同一 id 空间。
    """
    return analysis_service.get_trend_by_type(
        db, asset_class_id, period_type, date_from, date_to)


@router.get("/by-liquidity", response_model=list[ItemTrendPoint])
def by_liquidity(
    days: str = Query(..., description="流动性档位天数：0/1/7/30/90/180/365/9999"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按流动性档位（整数天数）的 as-of 趋势——取代旧 `by-liquidity-rating`。

    档位唯一权威是 `asset_items.liquidity_days`（ADR-0019 D4）；名称只用于展示。
    非白名单值 → 422（不静默返回空序列：空序列在图上会显示成「一分钱都没有」）。
    """
    return analysis_service.get_trend_by_liquidity(
        db, days, period_type, date_from, date_to)


@router.get("/by-institution", response_model=list[ItemTrendPoint])
def by_institution(
    institution_id: int = Query(..., description="机构 id（institutions）"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按机构的 as-of 趋势——取代旧 `by-account`（账户维度已拆成机构/渠道）。"""
    return analysis_service.get_trend_by_institution(
        db, institution_id, period_type, date_from, date_to)


@router.get("/by-channel", response_model=list[ItemTrendPoint])
def by_channel(
    channel_id: int = Query(..., description="渠道 id（channels）"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按渠道的 as-of 趋势（ADR-0019 D3：渠道从旧「账户」维度拆出）。"""
    return analysis_service.get_trend_by_channel(
        db, channel_id, period_type, date_from, date_to)


@router.get("/by-owner", response_model=list[ItemTrendPoint])
def by_owner(
    owner_id: int = Query(..., description="归属成员 id（members）"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按归属（家庭成员）的 as-of 趋势。"""
    return analysis_service.get_trend_by_owner(
        db, owner_id, period_type, date_from, date_to)


@router.get("/by-tag", response_model=list[ItemTrendPoint])
def by_tag(
    tag_id: int = Query(..., description="标签 id（tags；项级多值）"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """按标签的 as-of 趋势（标签是项级**多值**维度，一项可命中多个标签）。"""
    return analysis_service.get_trend_by_tag(
        db, tag_id, period_type, date_from, date_to)


# ---------- 已下线：旧分类维度趋势（ADR-0019，一律 410）----------
# 保留路径不保留语义：旧 id 空间（`attributes` 节点 id）已随 0027 迁移消失，
# 沿用旧名会被解析成另一个实体的 id 而悄悄返回空序列——那比报错更危险。


@router.get("/by-type", response_model=None)
def by_type(
    fund_type_id: Optional[int] = Query(None, description="[已下线] 旧 EAV 基金类型 id"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """【410】旧「按资产类型」趋势 → 改用 `/by-class?asset_class_id=`。"""
    _deprecated_dimension(
        "GET /api/analysis/by-type?fund_type_id=",
        "/api/analysis/by-class?asset_class_id=<资产类别 id>",
        legacy_param="fund_type_id", new_param="asset_class_id",
    )


@router.get("/by-liquidity-rating", response_model=None)
def by_liquidity_rating(
    liquidity_rating: Optional[str] = Query(None, description="[已下线] 旧档位名称"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """【410】旧「按流动性档位名称」趋势 → 改用 `/by-liquidity?days=`。"""
    _deprecated_dimension(
        "GET /api/analysis/by-liquidity-rating?liquidity_rating=<档位名称>",
        "/api/analysis/by-liquidity?days=<整数天数>（0/1/7/30/90/180/365/9999）",
        legacy_param="liquidity_rating", new_param="days",
        allowed_days=[days for days, _label in classification_service.LIQUIDITY_LEVELS],
    )


@router.get("/by-account", response_model=None)
def by_account(
    account_id: Optional[int] = Query(None, description="[已下线] 旧 EAV 账户 id"),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """【410】旧「按账户」趋势 → 改用 `/by-institution`（或 `/by-channel`）。"""
    _deprecated_dimension(
        "GET /api/analysis/by-account?account_id=",
        "/api/analysis/by-institution?institution_id=<机构 id>"
        "（渠道请用 /api/analysis/by-channel?channel_id=）",
        legacy_param="account_id", new_param="institution_id / channel_id",
    )
