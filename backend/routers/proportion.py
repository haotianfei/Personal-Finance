from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from services import proportion_service

router = APIRouter()


@router.get("/by-dimension")
def get_proportion_by_dimension(
    dimension: str = Query(..., description="分析维度: liquidity_rating, fund_type, asset_name, account"),
    snapshot_date: Optional[date] = Query(None, description="快照日期，默认为最新日期"),
    level: Optional[int] = Query(None, description="资产类型层级（仅对fund_type维度有效），None表示所有层级"),
    db: Session = Depends(get_db),
):
    """按指定维度获取资产占比分析"""
    return proportion_service.get_proportion_data(db, dimension, snapshot_date, level)


@router.get("/all")
def get_all_proportions(
    snapshot_date: Optional[date] = Query(None, description="快照日期，默认为最新日期"),
    db: Session = Depends(get_db),
):
    """获取所有维度的资产占比分析"""
    return proportion_service.get_all_proportions(db, snapshot_date)


@router.get("/available-dates")
def get_available_dates(
    db: Session = Depends(get_db),
):
    """获取有数据的日期列表"""
    from sqlalchemy import select, distinct
    from models import AssetRecord

    dates = db.execute(
        select(distinct(AssetRecord.asset_date)).order_by(AssetRecord.asset_date.desc())
    ).scalars().all()

    return [d.isoformat() for d in dates]


@router.get("/available-periods")
def get_available_periods(
    period_type: str = Query("month", description="账期类型: day, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """获取按账期分组的数据日期列表"""
    return proportion_service.get_available_periods(db, period_type)
