from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    AssetRecordCreate, AssetRecordUpdate, AssetRecordOut,
    AssetRecordBatchCreate, AssetRecordBatchUpdate, AssetRecordBatchDelete, CopyFromLastRequest, PaginatedResponse,
    AssetHistoryByNameQuery, AssetHistoryByNameResponse,
    BatchCreateByPeriodRequest, BatchCreateByPeriodResult, AssetRecordTemplate,
)
from services import asset_service

router = APIRouter()


@router.get("", response_model=PaginatedResponse)
def list_assets(
    asset_date: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    fund_type_id: Optional[str] = None,
    account_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    liquidity_rating_id: Optional[str] = None,
    asset_name: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    quarter: Optional[int] = None,
    month: Optional[int] = None,
    day: Optional[int] = None,
    sort_field: Optional[str] = None,
    sort_order: Optional[str] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    records, total = asset_service.list_records(
        db, asset_date, date_from, date_to, fund_type_id, account_id, owner_id, liquidity_rating_id, asset_name,
        amount_min, amount_max, period_type, year, quarter, month, day, sort_field, sort_order, page, page_size
    )
    items = [asset_service.record_to_out(r) for r in records]
    total_pages = (total + page_size - 1) // page_size
    return PaginatedResponse(
        items=items, total=total, page=page, page_size=page_size, total_pages=total_pages
    )


@router.get("/dates", response_model=list[date])
def get_dates(db: Session = Depends(get_db)):
    return asset_service.get_snapshot_dates(db)


@router.get("/snapshot/{snapshot_date}", response_model=list[AssetRecordOut])
def get_snapshot(snapshot_date: date, db: Session = Depends(get_db)):
    records = asset_service.get_snapshot_records(db, snapshot_date)
    return [asset_service.record_to_out(r) for r in records]


@router.get("/last-snapshot-date")
def get_last_snapshot_date(
    before_date: date = Query(..., description="目标日期"),
    period_type: str = Query("month"),
    db: Session = Depends(get_db),
):
    last_date = asset_service.find_last_snapshot_date(db, before_date, period_type)
    if not last_date:
        return {"date": None, "count": 0}
    records = asset_service.get_snapshot_records(db, last_date)
    return {"date": last_date, "count": len(records)}


@router.get("/{record_id}", response_model=AssetRecordOut)
def get_asset(record_id: int, db: Session = Depends(get_db)):
    record = asset_service.get_record(db, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return asset_service.record_to_out(record)


@router.post("", response_model=AssetRecordOut)
def create_asset(data: AssetRecordCreate, db: Session = Depends(get_db)):
    record = asset_service.create_record(db, data)
    return asset_service.record_to_out(record)


@router.post("/batch", response_model=list[AssetRecordOut])
def batch_create(data: AssetRecordBatchCreate, db: Session = Depends(get_db)):
    records = asset_service.batch_create_records(db, data.records)
    return [asset_service.record_to_out(r) for r in records]


@router.post("/copy-from-last")
def copy_from_last(data: CopyFromLastRequest, db: Session = Depends(get_db)):
    drafts = asset_service.copy_from_last_period(db, data.target_date, data.period_type)
    if not drafts:
        raise HTTPException(status_code=404, detail="No previous snapshot found")
    return {"source_date": drafts[0]["source_date"] if drafts else None, "records": drafts}


@router.put("/{record_id}", response_model=AssetRecordOut)
def update_asset(record_id: int, data: AssetRecordUpdate, db: Session = Depends(get_db)):
    update_data = data.model_dump(exclude_unset=True)
    record = asset_service.update_record(db, record_id, update_data)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return asset_service.record_to_out(record)


@router.delete("/{record_id}")
def delete_asset(record_id: int, db: Session = Depends(get_db)):
    if not asset_service.delete_record(db, record_id):
        raise HTTPException(status_code=404, detail="Record not found")
    return {"ok": True}


@router.post("/batch-update")
def batch_update_assets(data: AssetRecordBatchUpdate, db: Session = Depends(get_db)):
    """批量更新资产记录"""
    updates = {
        "liquidity_rating_id": data.liquidity_rating_id,
        "fund_type_id": data.fund_type_id,
        "asset_name": data.asset_name,
        "account_id": data.account_id,
    }
    updated_count = asset_service.batch_update_records(db, data.ids, updates)
    return {"updated_count": updated_count}


@router.post("/batch-delete")
def batch_delete_assets(data: AssetRecordBatchDelete, db: Session = Depends(get_db)):
    """批量删除资产记录"""
    deleted_count = asset_service.batch_delete_records(db, data.ids)
    return {"deleted_count": deleted_count}


@router.post("/by-names", response_model=list[AssetRecordOut])
def get_assets_by_names(
    asset_names: list[str],
    db: Session = Depends(get_db)
):
    """根据资产名称列表获取记录"""
    records = asset_service.get_records_by_asset_names(db, asset_names)
    return [asset_service.record_to_out(r) for r in records]


@router.post("/history-by-name", response_model=AssetHistoryByNameResponse)
def get_asset_history_by_name(
    data: AssetHistoryByNameQuery,
    db: Session = Depends(get_db)
):
    """根据资产名称查询所有历史记录
    
    返回记录总数、几条示例记录（最新/最早）以及所有记录ID
    """
    result = asset_service.get_asset_history_by_name(db, data.asset_name)
    return AssetHistoryByNameResponse(
        total_count=result["total_count"],
        sample_records=[asset_service.record_to_out(r) for r in result["sample_records"]],
        all_ids=result["all_ids"]
    )


@router.post("/batch-create-by-period", response_model=BatchCreateByPeriodResult)
def batch_create_by_period(
    data: BatchCreateByPeriodRequest,
    db: Session = Depends(get_db),
):
    """批量按账期添加记录
    
    根据模板记录，在历史记录的每个账期中自动添加一条相同的记录。
    日期设置为对应账期的最后一天。
    """
    result = asset_service.batch_create_by_period(
        db, 
        record_template=data.record_template,
        period_type=data.period_type,
        start_period=data.start_period,
        end_period=data.end_period,
        conflict_resolution=data.conflict_resolution
    )
    return BatchCreateByPeriodResult(
        success=result["success"],
        created_count=result["created_count"],
        periods=result["periods"],
        records=[asset_service.record_to_out(r) for r in result["records"]],
        message=result["message"]
    )
