"""C3 建议闭环端点（三期-3 W4/W5，挂 `/api/planning/advice`；spec §6）。

- 生成/裁决只写建议隔离区＋audit_log；**无账本写端口**。
- 写端点挂 `Idempotency-Key`；同事务审计由 service 负责。
- 路由顺序：`/settings` 必须在 `/{suggestion_id}` 之前。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (AdvisorCreateRequest, AdvisorDecisionRequest,
                     AdvisorExecutionRequest, AdvisorSettingsUpdate)
from services.advisor import service
from services.idempotency_service import idempotency_guard

router = APIRouter()


@router.get("", response_model=dict)
def list_suggestions(
    status: Optional[str] = None,
    horizon: Optional[str] = None,
    goal_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """收件箱（awaiting_review 优先，时间倒序）。"""
    return service.list_suggestions(
        db, status=status, horizon=horizon, goal_id=goal_id,
        page=page, page_size=page_size)


@router.post("", status_code=202, response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/advice"))])
def create_suggestion(request: Optional[AdvisorCreateRequest] = None,
                      db: Session = Depends(get_db)):
    """手动触发：创建建议＋job，同步生成一次（202 返回 generating 快照）。"""
    body = request or AdvisorCreateRequest()
    return service.create_suggestion(
        db, trigger_kind="manual", horizon=body.horizon,
        goal_id=body.goal_id, background=True)


@router.get("/settings", response_model=dict)
def get_settings(db: Session = Depends(get_db)):
    """自动触发开关／冷却／默认 horizon。"""
    return service.get_settings(db)


@router.put("/settings", response_model=dict,
            dependencies=[Depends(idempotency_guard(
                "PUT /api/planning/advice/settings"))])
def update_settings(request: AdvisorSettingsUpdate,
                    db: Session = Depends(get_db)):
    """设置变更（三字段 before/after 入审计）。"""
    fields = {k: getattr(request, k) for k in request.model_fields_set}
    settings, change = service.update_settings(db, fields)
    return {**settings, "change": change}


@router.get("/{suggestion_id}", response_model=dict)
def get_suggestion(suggestion_id: int, db: Session = Depends(get_db)):
    """详情（items/gate/info_set/decisions/provider/meta）。"""
    return service.suggestion_detail(db, suggestion_id)


@router.get("/{suggestion_id}/info-set", response_model=dict)
def get_info_set(suggestion_id: int, db: Session = Depends(get_db)):
    """信息集只读；重算 hash 不一致 → 409 ADVISOR_INFO_SET_CORRUPT。"""
    return service.get_info_set(db, suggestion_id)


@router.post("/{suggestion_id}/accept", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/advice/accept"))])
def accept_suggestion(suggestion_id: int,
                      request: Optional[AdvisorDecisionRequest] = None,
                      db: Session = Depends(get_db)):
    """采纳（只记录人的决定，不改任何账本）。"""
    return service.accept_suggestion(db, suggestion_id,
                                     reason=request.reason if request else None)


@router.post("/{suggestion_id}/reject", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/advice/reject"))])
def reject_suggestion(suggestion_id: int,
                      request: Optional[AdvisorDecisionRequest] = None,
                      db: Session = Depends(get_db)):
    """驳回（reason 必填，422 details.fields）。"""
    return service.reject_suggestion(db, suggestion_id,
                                     reason=request.reason if request else None)


@router.post("/{suggestion_id}/execution", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/advice/execution"))])
def record_execution(suggestion_id: int, request: AdvisorExecutionRequest,
                     db: Session = Depends(get_db)):
    """执行备注（仅 accepted；不改建议状态、不写金额）。"""
    return service.record_execution(db, suggestion_id,
                                    request.execution_status, request.note)