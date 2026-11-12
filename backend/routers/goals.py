"""C1 目标端点（三期-1 W1+W2，挂 `/api/planning/goals`；spec §5）。

- 读：列表 / 详情 / 达成度（`/progress`，与 `/api/metrics/status` 共用纯函数）。
- 写：创建 / 改名 / 新版本 / 改删 draft / 激活 / 归档 / 设主目标；全部挂
  `Idempotency-Key`，每次写同事务写审计（ADR-0010 §6.2）。
- 错误：成员 scope/金额/日期 → 422 VALIDATION_ERROR；
  版本号/并发 → 409 GOAL_VERSION_CONFLICT；非 draft 改删 → 409 GOAL_VERSION_IMMUTABLE。
"""
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (AUDIT_REASON_NOTE_MAX, AuditReason, GoalCreate, GoalRename,
                     GoalVersionCreate, GoalVersionUpdate)
from services import goal_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


@router.get("/goals", response_model=dict)
def list_goals(
    include_archived: bool = Query(False, description="是否含已归档目标"),
    db: Session = Depends(get_db),
):
    """目标列表（主目标优先；默认不含归档）。"""
    return goal_service.list_goals(db, include_archived=include_archived)


@router.post("/goals", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/planning/goals"))])
def create_goal(request: GoalCreate, db: Session = Depends(get_db)):
    """创建目标＋首个版本（activate 默认 true；全库无主目标时自动设主）。"""
    goal, version, change = goal_service.create_goal(
        db,
        name=request.name,
        scope=request.scope,
        target_amount=request.target_amount,
        target_date=request.target_date,
        target_rate_annual=request.target_rate_annual,
        risk_profile=request.risk_profile,
        life_stage=request.life_stage,
        reason=request.reason,
        activate=request.activate,
    )
    versions = goal_service._versions(db, goal.id)
    return {
        "goal": goal_service.goal_out(goal, versions),
        "version": goal_service.version_out(version),
        "change": change,
    }


@router.get("/goals/{goal_id}", response_model=dict)
def get_goal(goal_id: int, db: Session = Depends(get_db)):
    """目标详情＋版本史（version_no 降序）。"""
    return goal_service.get_goal(db, goal_id)


@router.put("/goals/{goal_id}", response_model=dict,
            dependencies=[Depends(idempotency_guard("PUT /api/planning/goals"))])
def rename_goal(goal_id: int, request: GoalRename,
                db: Session = Depends(get_db)):
    """仅改名称（改金额/期限/分区须新建版本）。"""
    goal, change = goal_service.rename_goal(db, goal_id, request.name)
    versions = goal_service._versions(db, goal.id)
    return {"goal": goal_service.goal_out(goal, versions), "change": change}


@router.post("/goals/{goal_id}/versions", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/goals/versions"))])
def create_version(goal_id: int, request: GoalVersionCreate,
                   db: Session = Depends(get_db)):
    """新版本（版本号 max+1；activate 默认 true → 同事务归档旧 active）。"""
    version, change = goal_service.create_version(
        db, goal_id,
        target_amount=request.target_amount,
        target_date=request.target_date,
        target_rate_annual=request.target_rate_annual,
        risk_profile=request.risk_profile,
        life_stage=request.life_stage,
        reason=request.reason,
        activate=request.activate,
    )
    return {"version": goal_service.version_out(version), "change": change}


@router.put("/goals/{goal_id}/versions/{version_no}", response_model=dict,
            dependencies=[Depends(idempotency_guard(
                "PUT /api/planning/goals/version"))])
def update_version(goal_id: int, version_no: int, request: GoalVersionUpdate,
                   db: Session = Depends(get_db)):
    """改 draft 内容；active/archived → 409 GOAL_VERSION_IMMUTABLE。"""
    fields = {name: getattr(request, name) for name in request.model_fields_set}
    version, change = goal_service.update_draft(db, goal_id, version_no, fields)
    return {"version": goal_service.version_out(version), "change": change}


@router.delete("/goals/{goal_id}/versions/{version_no}", response_model=dict,
               dependencies=[Depends(idempotency_guard(
                   "DELETE /api/planning/goals/version"))])
def delete_version(
    goal_id: int,
    version_no: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除 draft（留审计行）；active/archived → 409 GOAL_VERSION_IMMUTABLE。

    **无 UI 入口，供维护脚本**（spec E4）；`action='delete'` 属纠正类，
    缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    change = goal_service.delete_draft(
        db, goal_id, version_no, audit_reason=reason,
        audit_reason_note=reason_note)
    return {"deleted": True, "change": change}


@router.post("/goals/{goal_id}/versions/{version_no}/activate",
             response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/goals/activate"))])
def activate_version(goal_id: int, version_no: int,
                     db: Session = Depends(get_db)):
    """激活 draft：事务内先归档旧 active，再置新（写 effective_from/criterion 快照）。"""
    version, superseded_id, change = goal_service.activate_version(
        db, goal_id, version_no)
    return {
        "version": goal_service.version_out(version),
        "superseded_version_id": superseded_id,
        "change": change,
    }


@router.post("/goals/{goal_id}/archive", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/goals/archive"))])
def archive_goal(
    goal_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """归档目标（终态）：所有未归档版本置 archived、主目标标记清零。

    这是**人的裁决**（区别于激活新版本的隐式归档，后者 `actor='system'`）：
    缺 reason → 422 `AUDIT_REASON_REQUIRED`（spec 验收 6）。
    """
    goal, change = goal_service.archive_goal(
        db, goal_id, audit_reason=reason, audit_reason_note=reason_note)
    versions = goal_service._versions(db, goal.id)
    return {"goal": goal_service.goal_out(goal, versions), "change": change}


@router.post("/goals/{goal_id}/primary", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/goals/primary"))])
def set_primary(goal_id: int, db: Session = Depends(get_db)):
    """设为主目标（概览北极星用）；无 active 版本 → 422。"""
    goal, change = goal_service.set_primary(db, goal_id)
    versions = goal_service._versions(db, goal.id)
    return {"goal": goal_service.goal_out(goal, versions), "change": change}


@router.get("/goals/{goal_id}/progress", response_model=dict)
def get_progress(
    goal_id: int,
    as_of: Optional[date] = Query(None, description="时点（YYYY-MM-DD，缺省＝最新快照日）"),
    db: Session = Depends(get_db),
):
    """达成度（3 项值＋剩余期数＋分区＋gates＋meta）；与指标状态面共用纯函数。"""
    return goal_service.get_goal_progress(db, goal_id, as_of)