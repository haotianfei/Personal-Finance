from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from database import get_db
from models import ExportHistory, AutoExportRule
from schemas import (
    ExportHistoryOut, ExportHistoryList,
    AutoExportRuleCreate, AutoExportRuleUpdate, AutoExportRuleOut,
    CronValidationRequest, CronValidationResponse
)
from services.auto_export_service import reload_scheduler

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
    """创建导出历史记录（内部使用）"""
    history = ExportHistory(
        export_type=export_type,
        filename=filename,
        file_size=file_size,
        operator=operator,
        rule_name=rule_name,
        file_path=file_path,
    )
    db.add(history)
    db.commit()
    db.refresh(history)
    return history


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
    """创建自动导出规则"""
    # 验证 Cron 表达式
    from services.scheduler_service import validate_cron_expression, get_next_run_time
    
    is_valid, description = validate_cron_expression(data.cron_expression)
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {description}")
    
    # 计算下次执行时间
    next_run = get_next_run_time(data.cron_expression)
    
    rule = AutoExportRule(
        name=data.name,
        cron_expression=data.cron_expression,
        export_format=data.export_format,
        filename_template=data.filename_template,
        next_run_at=next_run,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    
    # 重新加载调度器
    reload_scheduler()
    
    return rule


@router.put("/rules/{rule_id}", response_model=AutoExportRuleOut)
def update_auto_export_rule(
    rule_id: int,
    data: AutoExportRuleUpdate,
    db: Session = Depends(get_db),
):
    """更新自动导出规则"""
    from services.scheduler_service import validate_cron_expression, get_next_run_time
    from fastapi import HTTPException
    
    rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    
    # 如果更新了 Cron 表达式，验证并重新计算下次执行时间
    if data.cron_expression is not None:
        is_valid, description = validate_cron_expression(data.cron_expression)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {description}")
        rule.cron_expression = data.cron_expression
        rule.next_run_at = get_next_run_time(data.cron_expression)
    
    if data.name is not None:
        rule.name = data.name
    if data.export_format is not None:
        rule.export_format = data.export_format
    if data.filename_template is not None:
        rule.filename_template = data.filename_template
    if data.is_active is not None:
        rule.is_active = data.is_active
        # 如果禁用规则，清除下次执行时间
        if not data.is_active:
            rule.next_run_at = None
        else:
            # 重新计算下次执行时间
            rule.next_run_at = get_next_run_time(rule.cron_expression)
    
    db.commit()
    db.refresh(rule)
    
    # 重新加载调度器
    reload_scheduler()
    
    return rule


@router.delete("/rules/{rule_id}")
def delete_auto_export_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """删除自动导出规则"""
    from fastapi import HTTPException
    
    rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    
    db.delete(rule)
    db.commit()
    
    # 重新加载调度器
    reload_scheduler()
    
    return {"message": "规则已删除"}


@router.post("/validate-cron", response_model=CronValidationResponse)
def validate_cron(
    data: CronValidationRequest,
):
    """验证 Cron 表达式并返回描述和下次执行时间"""
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
