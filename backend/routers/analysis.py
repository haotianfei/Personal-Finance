from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    SummaryData, TrendPoint, ComparisonPoint, MixedChartData,
    ItemTrendPoint,
)
from services import analysis_service

router = APIRouter()


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


@router.get("/by-item", response_model=list[ItemTrendPoint])
def by_item(
    asset_name: str = Query(...),
    period_type: str = Query("day"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_trend_by_item(db, asset_name, period_type, date_from, date_to)


@router.get("/by-type", response_model=list[ItemTrendPoint])
def by_type(
    fund_type_id: int = Query(...),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_trend_by_type(db, fund_type_id, period_type, date_from, date_to)


@router.get("/by-liquidity-rating", response_model=list[ItemTrendPoint])
def by_liquidity_rating(
    liquidity_rating: str = Query(...),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_trend_by_liquidity_rating(db, liquidity_rating, period_type, date_from, date_to)


@router.get("/by-account", response_model=list[ItemTrendPoint])
def by_account(
    account_id: int = Query(...),
    period_type: str = Query("month"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return analysis_service.get_trend_by_account(db, account_id, period_type, date_from, date_to)
