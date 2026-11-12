"""审计日志只读端点（v5 步骤 4，spec §5）。

仅查询；无任何写/改/删端口（append-only）。响应形状按现行 FastAPI 风格，
错误信封等契约层改造留给步骤 5。before/after 以解析后的 JSON 返回。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from services import audit_chain, audit_integrity_service, audit_service

router = APIRouter()


@router.get("", response_model=dict)
def list_audit_log(
    entity: Optional[str] = None,
    row_id: Optional[int] = None,
    action: Optional[str] = None,
    evidence_ref: Optional[int] = None,
    request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,  # 精确匹配（7 天 TTL 后可能悬空）
    date_from: Optional[str] = None,   # YYYY-MM-DD（含）
    date_to: Optional[str] = None,     # YYYY-MM-DD（含）
    actor: Optional[str] = None,       # owner | system（精确匹配）
    order: str = "desc",               # asc | desc（其它值 422）
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """审计日志列表（分页同 list_assets 风格；默认时间倒序＝id 倒序）。"""
    items, total = audit_service.list_audit_log(
        db, entity=entity, row_id=row_id, action=action,
        evidence_ref=evidence_ref, request_id=request_id,
        idempotency_key=idempotency_key,
        date_from=date_from, date_to=date_to, actor=actor, order=order,
        page=page, page_size=page_size,
    )
    total_pages = (total + page_size - 1) // page_size
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/chain/head", response_model=dict)
def chain_head(db: Session = Depends(get_db)):
    """审计链头（链尾 hash＋最新日封存）；只读、无副作用。"""
    return audit_chain.head(db)


@router.get("/chain/verify", response_model=dict)
def chain_verify(
    from_id: int = Query(0, ge=0, description="校验起点（不含该行；0＝从首行开始）"),
    to_id: Optional[int] = Query(None, ge=0, description="校验终点（含；缺省＝链尾）"),
    limit: int = Query(5000, ge=1, le=20000),
    db: Session = Depends(get_db),
):
    """审计链校验（7 判定码；`ok=false` 仍 200——这是报告，不是错误）。"""
    return audit_chain.verify(db, from_id=from_id, to_id=to_id, limit=limit)


@router.get("/integrity", response_model=dict)
def chain_integrity(db: Session = Depends(get_db)):
    """库外锚对账（只读）：当前链头 + 备份清单里的锚 + 判定。

    `chain/verify` 只能在同一库内自证（整库回退时它仍然通过）；本端点拿库外
    `manifest.audit_head` 与当前库链头比对，`worst_status` ∈
    `db_older`（回退）/`diverged`（分叉）才是真问题。只读、不写审计、不查权限。
    """
    return audit_integrity_service.integrity_report(db)


@router.get("/{log_id}", response_model=dict)
def get_audit_log(log_id: int, db: Session = Depends(get_db)):
    """审计日志详情（before/after 解析为 JSON）。"""
    row = audit_service.get_audit_log(db, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audit log entry not found")
    return row
