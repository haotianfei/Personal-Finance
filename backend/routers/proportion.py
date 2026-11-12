from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from services import proportion_service

router = APIRouter()


@router.get("/by-dimension")
def get_proportion_by_dimension(
    dimension: str = Query(
        ...,
        description=(
            "分析维度（ADR-0019 新词汇）：asset_class, liquidity, institution, "
            "channel, asset_name, owner, risk_level（risk 为同义写法）, tag。"
            "旧值 fund_type / liquidity_rating / account 已废弃 → 422 并指路新名。"
        ),
    ),
    snapshot_date: Optional[date] = Query(None, description="快照日期，默认为最新日期（与period/period_type二选一）"),
    period: Optional[str] = Query(None, description="账期，如2026, 2026-05, 2026-Q1, 2026-W01（与snapshot_date二选一）"),
    period_type: Optional[str] = Query("month", description="账期类型: day, week, month, quarter, year"),
    level: Optional[int] = Query(None, description="资产类别树层级（仅 asset_class 维度有效），None表示所有层级"),
    db: Session = Depends(get_db),
):
    """按指定维度获取资产占比分析

    支持两种查询方式：
    1. 通过 snapshot_date 指定具体日期
    2. 通过 period + period_type 指定账期（如 period=2026-05, period_type=month）

    维度值域由 `proportion_service.SUPPORTED_DIMENSIONS` 单点定义：此处先过
    `require_dimension()`，旧名（fund_type/liquidity_rating/account）拿 422 与替换名，
    不做任何兼容解析——旧名指向已删除的 `attributes` 节点 id 空间。
    """
    # 先校验维度：非法/废弃值不该走到取数（也会污染 snapshot_date 解析的报错顺序）
    dimension = proportion_service.require_dimension(dimension)

    # 如果提供了period，根据period和period_type计算snapshot_date
    if period and period_type:
        snapshot_date = proportion_service.get_snapshot_date_by_period(db, period, period_type)
    
    return proportion_service.get_proportion_data(db, dimension, snapshot_date, level)


@router.get("/all")
def get_all_proportions(
    snapshot_date: Optional[date] = Query(None, description="快照日期，默认为最新日期（与period/period_type二选一）"),
    period: Optional[str] = Query(None, description="账期，如2026, 2026-05, 2026-Q1, 2026-W01（与snapshot_date二选一）"),
    period_type: Optional[str] = Query("month", description="账期类型: day, week, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """获取所有维度的资产占比分析（键＝`SUPPORTED_DIMENSIONS` 全 8 个新维度名）
    
    支持两种查询方式：
    1. 通过 snapshot_date 指定具体日期
    2. 通过 period + period_type 指定账期
    """
    # 如果提供了period，根据period和period_type计算snapshot_date
    if period and period_type:
        snapshot_date = proportion_service.get_snapshot_date_by_period(db, period, period_type)
    
    return proportion_service.get_all_proportions(db, snapshot_date)


@router.get("/available-dates")
def get_available_dates(
    db: Session = Depends(get_db),
):
    """获取有数据的日期列表"""
    from sqlalchemy import select, distinct
    from models import Asset

    dates = db.execute(
        select(distinct(Asset.asset_date)).order_by(Asset.asset_date.desc())
    ).scalars().all()

    return [d.isoformat() for d in dates]


@router.get("/available-periods")
def get_available_periods(
    period_type: str = Query("month", description="账期类型: day, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """获取按账期分组的数据日期列表"""
    return proportion_service.get_available_periods(db, period_type)
