"""指标端点（三期-0 指标骨架，spec §3）：字典＋状态面。纯读。"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from schemas import MetricDictionary, MetricStatus
from services import metric_service

router = APIRouter()


@router.get("", response_model=MetricDictionary)
def metrics_dictionary():
    """指标字典（静态、无 meta）：11 项首版指标集。"""
    return metric_service.get_dictionary()


@router.get("/status", response_model=MetricStatus)
def metrics_status(
    as_of: Optional[date] = None,
    include_details: bool = False,
    db: Session = Depends(get_db),
):
    """指标状态面：值＋口径闸门＋灯＋下钻＋B4 域级汇总；as_of 缺省＝最新快照日。

    as_of 晚于最新快照日 → 422（VALIDATION_ERROR，details.fields 定位）。
    include_details=False（默认）时异动不取逐项明细。
    """
    return metric_service.get_status(db, as_of, include_details=include_details)