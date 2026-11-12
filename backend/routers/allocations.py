from datetime import date
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from services import allocation_service, proportion_service
from services.idempotency_service import idempotency_guard
from schemas import (
    AUDIT_REASON_NOTE_MAX, AllocationTargetCreate, AllocationTargetUpdate,
    AllocationTargetOut, AllocationAnalysis, AuditReason
)

router = APIRouter()


@router.get("/targets", response_model=list[AllocationTargetOut])
def get_allocation_targets(
    dimension: Optional[str] = Query(
        None,
        description=(
            "Filter by dimension（ADR-0019）：asset_class, liquidity, institution, "
            "channel, risk_level（risk 为同义写法）。旧值 fund_type / liquidity_rating / "
            "account 已废弃 → 422 并指路新名。"
        ),
    ),
    db: Session = Depends(get_db),
):
    """Get all allocation targets, optionally filtered by dimension."""
    return allocation_service.get_allocation_targets(db, dimension)


@router.post("/targets", response_model=AllocationTargetOut,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/allocations/targets"))])
def create_allocation_target(
    target: AllocationTargetCreate,
    db: Session = Depends(get_db),
):
    """Create a new allocation target."""
    # Validate constraints
    is_valid, error_msg = allocation_service.validate_allocation_target(
        db, target.dimension, target.target_id, target.target_percent, target.parent_id
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)
    
    return allocation_service.create_allocation_target(db, target)


@router.put("/targets/{target_id}", response_model=AllocationTargetOut,
            dependencies=[Depends(idempotency_guard(
                "PUT /api/allocations/targets"))])
def update_allocation_target(
    target_id: int,
    target: AllocationTargetUpdate,
    db: Session = Depends(get_db),
):
    """Update an existing allocation target."""
    # Get existing target
    existing = allocation_service.get_allocation_target(db, target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Allocation target not found")
    
    # Validate constraints if target_percent is being updated
    # Skip sibling sum validation if parent target is being decreased (auto-adjust will handle it)
    if target.target_percent is not None:
        # Check basic constraints only (value range, parent limit)
        if target.target_percent < 0:
            raise HTTPException(status_code=400, detail="比例不能小于0%")
        if target.target_percent > 100:
            raise HTTPException(status_code=400, detail="比例不能超过100%")
        
        # Check parent limit for child targets
        if existing.parent_id:
            parent_target = allocation_service.get_allocation_target(db, existing.parent_id)
            if parent_target and target.target_percent > parent_target.target_percent:
                raise HTTPException(status_code=400, detail=f"不能超过父级目标比例 ({parent_target.target_percent:.2f}%)")
        
        # Check root level total only if increasing or same
        if existing.parent_id is None and target.target_percent >= existing.target_percent:
            existing_targets = allocation_service.get_allocation_targets(db, existing.dimension)
            current_total = sum(
                t.target_percent for t in existing_targets
                if t.target_id != existing.target_id and t.parent_id is None
            )
            new_total = current_total + target.target_percent
            if new_total > 100:
                raise HTTPException(status_code=400, detail=f"该层级总配置比例不能超过100%，当前已配置{current_total:.2f}%")
    
    result, auto_adjusted_children = allocation_service.update_allocation_target(db, target_id, target)
    if not result:
        raise HTTPException(status_code=404, detail="Allocation target not found")
    
    # Build response with auto-adjusted children info
    response_data = AllocationTargetOut.model_validate(result).model_dump()
    if auto_adjusted_children:
        response_data["auto_adjusted_children"] = auto_adjusted_children
    
    return response_data


@router.delete("/targets/{target_id}",
               dependencies=[Depends(idempotency_guard(
                   "DELETE /api/allocations/targets"))])
def delete_allocation_target(
    target_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """Delete an allocation target（**无 UI 入口，供维护脚本**，spec E4）。

    `action='delete'` 属纠正类：缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    success = allocation_service.delete_allocation_target(
        db, target_id, audit_reason=reason, audit_reason_note=reason_note)
    if not success:
        raise HTTPException(status_code=404, detail="Allocation target not found")
    return {"success": True}


@router.get("/analysis", response_model=AllocationAnalysis)
def get_allocation_analysis(
    dimension: str = Query(
        ...,
        description=(
            "Dimension（ADR-0019）：asset_class, liquidity, institution, channel, "
            "risk_level（risk 为同义写法）。旧值 → 422。"
        ),
    ),
    snapshot_date: Optional[date] = Query(None, description="Snapshot date (YYYY-MM-DD, 与period/period_type二选一)"),
    period: Optional[str] = Query(None, description="账期，如2026, 2026-05, 2026-Q1, 2026-W01（与snapshot_date二选一）"),
    period_type: Optional[str] = Query("month", description="账期类型: day, week, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """Get allocation analysis for a specific dimension and date.
    
    支持两种查询方式：
    1. 通过 snapshot_date 指定具体日期
    2. 通过 period + period_type 指定账期（如 period=2026-05, period_type=month）

    维度校验由 `allocation_service.require_dimension()` 负责：旧值/未知值抛
    `ApiError(VALIDATION_ERROR, 422)`，由全局信封统一转码——本层不捕获、不改写状态码
    （曾有的 `except ValueError` 已成死代码：服务层早已不再抛 ValueError）。
    """
    # 如果提供了period，根据period和period_type计算snapshot_date
    if period and period_type:
        snapshot_date = proportion_service.get_snapshot_date_by_period(db, period, period_type)
    
    # If no date provided, use the latest date
    if snapshot_date is None:
        from sqlalchemy import select, func
        from models import Asset
        result = db.execute(select(func.max(Asset.asset_date))).scalar()
        if result:
            snapshot_date = result
        else:
            raise HTTPException(status_code=404, detail="No asset data available")
    
    return allocation_service.analyze_allocation(db, dimension, snapshot_date)


@router.get("/validation")
def validate_allocation(
    dimension: str = Query(
        ...,
        description=(
            "Dimension（ADR-0019）：asset_class, liquidity, institution, channel, "
            "risk_level（risk 为同义写法）。旧值 → 422。"
        ),
    ),
    target_id: str = Query(..., description="Target ID"),
    target_percent: float = Query(..., description="Target percentage (0-100)"),
    parent_id: Optional[int] = Query(None, description="Parent target ID for hierarchical validation"),
    db: Session = Depends(get_db),
):
    """Validate allocation target constraints without saving."""
    from decimal import Decimal
    is_valid, error_msg = allocation_service.validate_allocation_target(
        db, dimension, target_id, Decimal(str(target_percent)), parent_id
    )
    return {"valid": is_valid, "message": error_msg}


@router.get("/suggestions")
def get_adjustment_suggestions(
    dimension: str = Query(
        ...,
        description=(
            "Dimension（ADR-0019）：asset_class, liquidity, institution, channel, "
            "risk_level（risk 为同义写法）。旧值 → 422。"
        ),
    ),
    snapshot_date: Optional[date] = Query(None, description="Snapshot date (YYYY-MM-DD, 与period/period_type二选一)"),
    period: Optional[str] = Query(None, description="账期，如2026, 2026-05, 2026-Q1, 2026-W01（与snapshot_date二选一）"),
    period_type: Optional[str] = Query("month", description="账期类型: day, week, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """Get prioritized adjustment suggestions for allocation deviations.
    
    支持两种查询方式：
    1. 通过 snapshot_date 指定具体日期
    2. 通过 period + period_type 指定账期（如 period=2026-05, period_type=month）
    """
    # 如果提供了period，根据period和period_type计算snapshot_date
    if period and period_type:
        snapshot_date = proportion_service.get_snapshot_date_by_period(db, period, period_type)
    
    # If no date provided, use the latest date
    if snapshot_date is None:
        from sqlalchemy import select, func
        from models import Asset
        result = db.execute(select(func.max(Asset.asset_date))).scalar()
        if result:
            snapshot_date = result
        else:
            raise HTTPException(status_code=404, detail="No asset data available")
    
    suggestions = allocation_service.get_adjustment_suggestions(db, dimension, snapshot_date)
    return {
        "dimension": dimension,
        "snapshot_date": snapshot_date,
        "suggestions": suggestions,
        "total_count": len(suggestions),
    }
