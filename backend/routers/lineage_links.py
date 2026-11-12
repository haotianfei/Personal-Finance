"""血缘链接端点（v5 二期·2.4，spec §2.4）：登记（幂等键）＋前身/后继只读查询。"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError
from services import lineage_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


class LineageRequest(BaseModel):
    from_item_ids: list[int]            # 旧项组（1..n；多条＝merge 求和承接）
    to_item_name: str                   # 新项名称（必须未占用）
    link_type: str                      # merge | split | revision
    split_ratio: Optional[str] = None   # 仅 split；(0,1]
    principal: str                      # 承接值＝预填建议、人确认后携带（可改）
    effective_date: Optional[str] = None    # 缺省＝今天
    reason: str                         # R-9 必填
    note: Optional[str] = None


class SplitTarget(BaseModel):
    name: str
    ratio: str
    principal: str
    note: Optional[str] = None


class AttachRequest(BaseModel):
    """既有项之间补登记血缘（`.scratch/lineage-attach/spec.md` §1.1）。"""
    from_item_ids: Optional[list[int]] = None   # 1..n 前身（必须 active、已存在）
    to_item_ids: Optional[list[int]] = None     # 1..n 后继（已存在）
    link_type: str
    ratios: Optional[list[str]] = None          # 仅 split 且多后继
    effective_date: Optional[str] = None        # 必需（无 today 缺省）
    reason: Optional[str] = None                # R-9 必需


class SplitRequest(BaseModel):
    from_item_id: int                   # 父项（拆分后归档）
    targets: list[SplitTarget]          # >= 2；ratio 合计 1±1e-4
    effective_date: Optional[str] = None    # 缺省＝今天
    reason: str                         # R-9 必填


@router.post("/split", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/lineage-links/split"))])
def split_lineage(request: SplitRequest, db: Session = Depends(get_db)):
    """一对多血缘拆分（1→N）：父项归档＋子项创建＋split 链＋口径承接。"""
    eff: Optional[date] = None
    if request.effective_date:
        try:
            eff = date.fromisoformat(request.effective_date.strip())
        except ValueError:
            raise ApiError("BAD_REQUEST", 400, "effective_date 必须为 YYYY-MM-DD")
    return lineage_service.register_split(
        db, request.from_item_id,
        [t.model_dump() for t in request.targets],
        effective_date=eff, reason=request.reason)


@router.post("", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/lineage-links"))])
def create_lineage(request: LineageRequest, db: Session = Depends(get_db)):
    """登记血缘（单事务编排）：旧项归档＋新项创建＋链＋口径承接（origin='lineage'）。

    链创建即固定（拍板 8）；改名不走血缘（拍板 10）；承接值人确认（预填可改）。
    """
    eff: Optional[date] = None
    if request.effective_date:
        try:
            eff = date.fromisoformat(request.effective_date.strip())
        except ValueError:
            raise ApiError("BAD_REQUEST", 400, "effective_date 必须为 YYYY-MM-DD")
    first, links, row = lineage_service.register_lineage(
        db, request.from_item_ids, request.to_item_name, request.link_type,
        request.split_ratio, request.principal, request.reason,
        effective_date=eff, note=request.note)
    return {
        "link": lineage_service.link_dict(db, links[0]),
        "links": [lineage_service.link_dict(db, lk) for lk in links],
        "entry": {
            "id": row.id,
            "item_id": row.asset_item_id,
            "amount": str(row.new_amount),
            "effective_date": row.effective_date.isoformat(),
            "reason": row.reason,
            "origin": row.origin,
        },
    }


@router.post("/attach", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/lineage-links/attach"))])
def attach_lineage(request: AttachRequest, db: Session = Depends(get_db)):
    """既有项之间补登记血缘：不迁移历史、不写 principal_changes。"""
    eff: Optional[date] = None
    if request.effective_date:
        try:
            eff = date.fromisoformat(request.effective_date.strip())
        except ValueError:
            raise ApiError("BAD_REQUEST", 400, "effective_date 必须为 YYYY-MM-DD",
                           details={"field": "effective_date"})
    return lineage_service.attach_links(
        db, request.from_item_ids or [], request.to_item_ids or [],
        request.link_type, request.ratios, eff, request.reason)


@router.get("/family", response_model=dict)
def lineage_family(
    item_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """身份族视图（双向传递闭包，含自身）。"""
    return lineage_service.get_family(db, item_id)


@router.get("", response_model=dict)
def list_lineage(
    item_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """链查询（只读）：item_id 给定时返回前身/后继双向；否则返回全部链。"""
    return lineage_service.list_lineage(db, item_id)
