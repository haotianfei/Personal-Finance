"""导出历史与自动导出规则 API（离场桶）。

写端点一律**透传**到 `services/export_history_service.py`：业务行 + 审计行
（`entity='export'`）在同一事务里落库（ADR-0027）。本模块只做参数绑定与只读查询。

`POST /validate-cron` 是纯表达式校验、零写入 → 不产审计行（契约表 exempt 行）。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from database import get_db
from models import ExportHistory, AutoExportRule
from schemas import (
    AUDIT_REASON_NOTE_MAX, AuditReason,
    ExportHistoryOut, ExportHistoryList,
    AutoExportRuleCreate, AutoExportRuleUpdate, AutoExportRuleOut,
    CronValidationRequest, CronValidationResponse
)
from services import export_history_service

router = APIRouter()


@router.get("/history", response_model=ExportHistoryList)
def list_export_history(
    export_type: Optional[str] = Query(None, description="导出类型: manual, auto"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="限制返回数量（用于首页展示）"),
    db: Session = Depends(get_db),
):
    """获取导出历史记录列表"""
    query = db.query(ExportHistory)

    if export_type:
        query = query.filter(ExportHistory.export_type == export_type)

    total = query.count()

    # 如果指定了 limit，优先使用 limit（不分页）
    if limit:
        items = query.order_by(desc(ExportHistory.export_time)).limit(limit).all()
        return ExportHistoryList(
            items=items,
            total=total,
            page=1,
            page_size=limit
        )

    # 否则使用分页
    items = query.order_by(desc(ExportHistory.export_time)).offset((page - 1) * page_size).limit(page_size).all()

    return ExportHistoryList(
        items=items,
        total=total,
        page=page,
        page_size=page_size
    )


@router.post("/history", response_model=ExportHistoryOut)
def create_export_history(
    export_type: str,
    filename: str,
    file_size: Optional[int] = None,
    operator: Optional[str] = None,
    rule_name: Optional[str] = None,
    file_path: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """创建导出历史记录（内部使用）；同事务留痕 `export/create`（只记壳，不记文件内容）"""
    return export_history_service.create_history(
        db,
        export_type=export_type,
        filename=filename,
        file_size=file_size,
        operator=operator,
        rule_name=rule_name,
        file_path=file_path,
    )


# --- Auto Export Rules ---

@router.get("/rules", response_model=list[AutoExportRuleOut])
def list_auto_export_rules(
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    """获取自动导出规则列表"""
    query = db.query(AutoExportRule)
    if is_active is not None:
        query = query.filter(AutoExportRule.is_active == is_active)
    return query.order_by(desc(AutoExportRule.created_at)).all()


@router.post("/rules", response_model=AutoExportRuleOut)
def create_auto_export_rule(
    data: AutoExportRuleCreate,
    db: Session = Depends(get_db),
):
    """创建自动导出规则；非法 cron → 400（写入前拒绝，不产审计行）"""
    return export_history_service.create_rule(db, data.model_dump())


@router.put("/rules/{rule_id}", response_model=AutoExportRuleOut)
def update_auto_export_rule(
    rule_id: int,
    data: AutoExportRuleUpdate,
    db: Session = Depends(get_db),
):
    """更新自动导出规则；404 不存在；400 非法 cron；只记变更字段"""
    return export_history_service.update_rule(db, rule_id, data.model_dump())


@router.delete("/rules/{rule_id}")
def delete_auto_export_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除自动导出规则；404 不存在；留痕 `export/delete` 带 before 全集。

    `delete` 属纠正类（spec E1）：缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    export_history_service.delete_rule(db, rule_id, reason=reason,
                                       reason_note=reason_note)
    return {"message": "规则已删除"}


@router.post("/validate-cron", response_model=CronValidationResponse)
def validate_cron(
    data: CronValidationRequest,
):
    """验证 Cron 表达式并返回描述和下次执行时间（纯校验、零写入、不留痕）"""
    from services.scheduler_service import validate_cron_expression, get_next_run_times

    is_valid, description = validate_cron_expression(data.cron_expression)

    next_times = []
    if is_valid:
        next_times = get_next_run_times(data.cron_expression, count=5)

    return CronValidationResponse(
        valid=is_valid,
        description=description,
        next_run_times=next_times
    )