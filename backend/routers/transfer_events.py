"""转移事件端点（v5 二期·2.3，spec §2.3）：登记（幂等键）＋只读列表。"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError
from services import transfer_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


class TransferRequest(BaseModel):
    from_item_id: int
    to_item_id: int
    amount: str                      # 恒正字符串金额（方向由 from/to 表达）
    event_date: str                  # YYYY-MM-DD（登记日＝快照日）
    reason: str                      # R-9 必填
    evidence_ref: Optional[int] = None


@router.post("", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/transfer-events"))])
def create_transfer(request: TransferRequest, db: Session = Depends(get_db)):
    """登记内部转移：同事务两条口径变更（from −/to ＋，origin='transfer'）。

    更正＝登记**反向转移**（追加不改史）；当日缺 active 快照 → 409
    SNAPSHOT_REQUIRED（details.missing_items＋hint 指引用 F1/更正先建行）。
    """
    try:
        ev_date = date.fromisoformat((request.event_date or "").strip())
    except ValueError:
        raise ApiError("BAD_REQUEST", 400, "event_date 必须为 YYYY-MM-DD")
    event, change = transfer_service.register_transfer(
        db, request.from_item_id, request.to_item_id, request.amount,
        ev_date, request.reason, evidence_ref=request.evidence_ref)
    return {"event": transfer_service.event_dict(db, event), "change": change}


@router.get("", response_model=dict)
def list_transfers(
    item_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """转移事件列表（只读）：可按任一侧 item／日期区间筛选。"""
    return {"items": transfer_service.list_transfers(db, item_id, date_from, date_to)}
