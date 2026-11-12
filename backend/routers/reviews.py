"""C4 复盘端点（三期-4 W2/W4，挂 `/api/planning/reviews`；spec §5）。

- 只写复盘隔离区＋audit_log；**无账本/目标/阈值写端口**（提案采纳只记决定）。
- 写端点挂 `Idempotency-Key`；同事务审计在 service。
- 路由顺序：`/candidates` 必须在 `/{review_id}` 之前。
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import (ReviewCloseRequest, ReviewConclusionRequest,
                     ReviewCreateRequest, ReviewProposalCreate,
                     ReviewProposalDecision, ReviewProposalDismiss)
from services.advisor import review as review_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


@router.get("", response_model=dict)
def list_reviews(
    goal_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """列表（open 优先，时间倒序）。"""
    return review_service.list_reviews(
        db, goal_id=goal_id, status=status, page=page, page_size=page_size)


@router.post("", status_code=201, response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/reviews"))])
def create_review(request: Optional[ReviewCreateRequest] = None,
                  db: Session = Depends(get_db)):
    """建复盘并立即冻结快照（201）。"""
    body = request or ReviewCreateRequest()
    return review_service.create_review(
        db, goal_id=body.goal_id, as_of=body.as_of,
        start_as_of=body.start_as_of,
        suggestion_ids=body.suggestion_ids, label=body.label, note=body.note)


@router.get("/candidates", response_model=dict)
def list_candidates(
    goal_id: Optional[int] = None,
    as_of: Optional[date] = None,
    start_as_of: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """候选建议（accepted、窗口内、未被任何复盘引用）。"""
    return review_service.list_candidates(
        db, goal_id=goal_id, as_of=as_of, start_as_of=start_as_of)


@router.get("/{review_id}", response_model=dict)
def get_review(review_id: int, db: Session = Depends(get_db)):
    """详情（快照/建议/提案；payload_ok）。"""
    return review_service.review_detail(db, review_id)


@router.put("/{review_id}/conclusion", response_model=dict,
            dependencies=[Depends(idempotency_guard(
                "PUT /api/planning/reviews/conclusion"))])
def update_conclusion(review_id: int,
                      request: ReviewConclusionRequest,
                      db: Session = Depends(get_db)):
    """写归因＋结论（仅 open）。"""
    return review_service.update_conclusion(
        db, review_id, attribution=request.attribution,
        conclusion=request.conclusion)


@router.post("/{review_id}/close", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/reviews/close"))])
def close_review(review_id: int,
                 request: Optional[ReviewCloseRequest] = None,
                 db: Session = Depends(get_db)):
    """关闭（结论必填；终态不可改）。"""
    body = request or ReviewCloseRequest()
    return review_service.close_review(db, review_id,
                                       conclusion=body.conclusion)


@router.post("/{review_id}/proposals", status_code=201, response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/reviews/proposals"))])
def create_proposal(review_id: int, request: ReviewProposalCreate,
                    db: Session = Depends(get_db)):
    """新建结构化提案（仅 open）。"""
    return review_service.create_proposal(
        db, review_id, kind=request.kind, subject_ref=request.subject_ref,
        payload=request.payload, rationale=request.rationale)


@router.post("/{review_id}/proposals/{proposal_id}/adopt", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/reviews/proposals/adopt"))])
def adopt_proposal(review_id: int, proposal_id: int,
                   request: Optional[ReviewProposalDecision] = None,
                   db: Session = Depends(get_db)):
    """采纳（只记决定；不改目标/阈值/账本）。"""
    body = request or ReviewProposalDecision()
    return review_service.adopt_proposal(
        db, review_id, proposal_id, reason=body.reason,
        applied_kind=body.applied_kind, applied_ref=body.applied_ref)


@router.post("/{review_id}/proposals/{proposal_id}/dismiss", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/planning/reviews/proposals/dismiss"))])
def dismiss_proposal(review_id: int, proposal_id: int,
                     request: Optional[ReviewProposalDismiss] = None,
                     db: Session = Depends(get_db)):
    """驳回（理由必填；终态不可改）。"""
    body = request or ReviewProposalDismiss()
    return review_service.dismiss_proposal(db, review_id, proposal_id,
                                           reason=body.reason)