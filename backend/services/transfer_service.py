"""内部买卖转移服务（v5 二期·子步 2.3，规格 .scratch/v5-appreciation/spec.md §2.3）。

- 登记不变量：**同一事务**两条 principal_changes（from −amount / to +amount、同
  effective_date、origin='transfer'、transfer_event_id 回填、reason=「内部买卖转移
  #id」）——两侧本金等额、净值不变、不产生流水（R-34/拍板 9）。
- 登记日即快照日（R-34 注）：两侧 item 于 event_date 须各有 active 快照行，
  缺 → 409 SNAPSHOT_REQUIRED（details 提示先走 F1/更正建行再重登记）。
- 累计基准走 `history_basis`（纯历史链）而非 resolve 行定格分支：同日多条追加
  需看到「此刻累计」；两侧均未设口径按 0 起算（from 侧可为负＝净撤出，合法）。
- **更正＝登记反向转移**（追加不改史）；transfer_events 与本行均 append-only
  （0013 触发器），无 update/delete 端口；审计 action＝transfer_register 单行。
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import Asset, AssetItem, PrincipalChange, TransferEvent
from services import audit_service
from services.audit_service import make_change as _change
from services.principal_service import history_basis

ZERO = Decimal("0")


def _require_item(db: Session, item_id: int, role: str) -> AssetItem:
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       details={"field": role})
    if item.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                       f"已归档资产项不可参与转移（{role}＝{item.display_name}，R-37 终态）")
    return item


def _has_active_snapshot(db: Session, item_id: int, d: date) -> bool:
    return db.execute(
        select(Asset.id)
        .where(Asset.asset_item_id == item_id, Asset.asset_date == d,
               Asset.superseded_by.is_(None))
    ).first() is not None


def register_transfer(db: Session, from_item_id: int, to_item_id: int, amount,
                      event_date: date, reason: str | None,
                      evidence_ref: int | None = None) -> tuple[TransferEvent, dict]:
    """登记一笔内部转移（人裁决，R-9 同理由）。返回 (事件行, 审计 change dict)。

    Raises:
        ApiError: 项不存在(404)、归档(409 ITEM_ARCHIVED)、from==to／金额非法
            (400 BAD_REQUEST)、reason 缺失(400 PRINCIPAL_REASON_REQUIRED 复用，
            转移即口径变更)、当日缺快照(409 SNAPSHOT_REQUIRED)。
    """
    if from_item_id == to_item_id:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "转移两侧不可为同一资产项",
                       details={"field": "to_item_id"})
    src = _require_item(db, from_item_id, "from_item_id")
    dst = _require_item(db, to_item_id, "to_item_id")
    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "转移登记必须附理由（R-9：两侧口径同刻变动是人裁决的值）")
    try:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"转移金额非法: {amount!r}")
    if amt <= ZERO:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "转移金额恒正（方向由 from/to 表达，spec §2.3）",
                       details={"field": "amount"})
    if not isinstance(event_date, date):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "event_date 必须为 date")

    # 登记日即快照日（R-34 注）：两侧当日均须有 active 快照行
    missing = [
        {"item_id": it.id, "display_name": it.display_name}
        for it in (src, dst) if not _has_active_snapshot(db, it.id, event_date)
    ]
    if missing:
        raise ApiError(ErrorCode.SNAPSHOT_REQUIRED, 409,
                       f"登记日 {event_date} 缺 active 快照行（登记日即快照日，R-34 注）",
                       details={"missing_items": missing,
                                "event_date": event_date.isoformat(),
                                "hint": "请先对该日补录/更正快照（截图 F1 采纳或账本登记），再重登记转移"})

    event = TransferEvent(
        from_item_id=src.id, to_item_id=dst.id, amount=amt,
        event_date=event_date, reason=str(reason).strip(),
        evidence_ref=evidence_ref, created_at=datetime.now().isoformat(),
    )
    db.add(event)
    db.flush()

    # 同事务两条口径变更：from −amt / to +amt（累计基准＝纯历史链，None 视作 0）
    basis_from = history_basis(db, src.id, event_date) or ZERO
    basis_to = history_basis(db, dst.id, event_date) or ZERO
    txt = f"内部买卖转移 #{event.id}"
    rows = [
        PrincipalChange(asset_item_id=src.id, new_amount=basis_from - amt,
                        effective_date=event_date, reason=txt,
                        evidence_ref=evidence_ref, origin="transfer",
                        transfer_event_id=event.id,
                        created_at=datetime.now().isoformat()),
        PrincipalChange(asset_item_id=dst.id, new_amount=basis_to + amt,
                        effective_date=event_date, reason=txt,
                        evidence_ref=evidence_ref, origin="transfer",
                        transfer_event_id=event.id,
                        created_at=datetime.now().isoformat()),
    ]
    db.add_all(rows)

    change = _change("transfer_event", event.id, "transfer_register",
                     before=None,
                     after={"from_item_id": src.id, "to_item_id": dst.id,
                            "from_basis": str(basis_from), "to_basis": str(basis_to),
                            "amount": str(amt), "event_date": event_date.isoformat()})
    audit_service.record(db, change)            # 同事务：失败阻断业务
    db.commit()
    db.refresh(event)
    return event, change


def event_dict(db: Session, ev: TransferEvent) -> dict:
    src = db.get(AssetItem, ev.from_item_id)
    dst = db.get(AssetItem, ev.to_item_id)
    return {
        "id": ev.id,
        "from_item_id": ev.from_item_id,
        "from_name": src.display_name if src else str(ev.from_item_id),
        "to_item_id": ev.to_item_id,
        "to_name": dst.display_name if dst else str(ev.to_item_id),
        "amount": str(ev.amount),
        "event_date": ev.event_date.isoformat(),
        "reason": ev.reason,
        "evidence_ref": ev.evidence_ref,
        "created_at": ev.created_at,
    }


def list_transfers(db: Session, item_id: int | None = None,
                   date_from: date | None = None,
                   date_to: date | None = None) -> list[dict]:
    """转移事件列表（只读）：按 item（任一侧）/日期区间筛选，登记时序。"""
    q = select(TransferEvent)
    if item_id is not None:
        q = q.where((TransferEvent.from_item_id == item_id)
                    | (TransferEvent.to_item_id == item_id))
    if date_from is not None:
        q = q.where(TransferEvent.event_date >= date_from)
    if date_to is not None:
        q = q.where(TransferEvent.event_date <= date_to)
    q = q.order_by(TransferEvent.event_date, TransferEvent.id)
    return [event_dict(db, ev) for ev in db.execute(q).scalars().all()]
