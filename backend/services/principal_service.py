"""本金口径服务（v5 二期·子步 2.1，规格 .scratch/v5-appreciation/spec.md §2.1）。

- `resolve_cost_basis(db, item_id, d)`＝**四子步共同真源函数（单点维护）**：
  行值优先（ledger 写单点建行时冗余的 `assets.cost_basis`）、NULL 回退变更史
  （effective_date ≤ d，按 `effective_date DESC, id DESC` 取首条＝同日追加即更正）；
  两分支等价性由测试锁定。
- `record_principal(...)`＝人裁决写入口（R-9）：首次 principal_set、后续
  principal_change（审计 before=旧生效值/after=新值，走 `_change` 单点）；
  **更正＝追加**（录错不改史，无 update/delete 端口）。
- origin 恒 'external'（transfer/lineage 为 2.3/2.4 预留）；负债侧可登记口径
  （未偿本金），升值率计算仅资产侧（spec §3 修订建议 1，按此执行）。
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import Asset, AssetItem, PrincipalChange
from services import audit_service
from services.audit_service import make_change as _change


def resolve_cost_basis(db: Session, item_id: int, d: date) -> Decimal | None:
    """资产项在日期 d 的生效本金口径（共享真源；None＝未设定）。

    行值优先：(item, d) 的 active 快照行若带 cost_basis（建行时冗余），直接返回
    ——即「写入时点生效口径」定格；否则回退变更史解析。无行或行值为 NULL
    （存量行）都走历史回退，两分支对同一生效序列给出相同结果（等价性测试锁定）。
    """
    row_val = db.execute(
        select(Asset.cost_basis)
        .where(Asset.asset_item_id == item_id,
               Asset.asset_date == d,
               Asset.superseded_by.is_(None))
    ).scalars().first()
    if row_val is not None:
        return Decimal(str(row_val))
    return _resolve_from_history(db, item_id, d)


def _resolve_from_history(db: Session, item_id: int, d: date) -> Decimal | None:
    v = db.execute(
        select(PrincipalChange.new_amount)
        .where(PrincipalChange.asset_item_id == item_id,
               PrincipalChange.effective_date <= d)
        .order_by(PrincipalChange.effective_date.desc(), PrincipalChange.id.desc())
    ).scalars().first()
    return Decimal(str(v)) if v is not None else None


def current_principal(db: Session, item_id: int) -> Decimal | None:
    """当前生效口径＝变更史解析（不依赖某日快照行）。"""
    v = db.execute(
        select(PrincipalChange.new_amount)
        .where(PrincipalChange.asset_item_id == item_id)
        .order_by(PrincipalChange.effective_date.desc(), PrincipalChange.id.desc())
    ).scalars().first()
    return Decimal(str(v)) if v is not None else None


def history_basis(db: Session, item_id: int, d: date) -> Decimal | None:
    """截至 d 的生效口径（**纯历史链**，不走快照行 cost_basis 定格分支）。

    转移登记等同事务累加场景专用：同日多条追加会把链往前推，而行定格值停在
    建行时点——只有权威史能给出「此刻累计」。展示/读侧定格仍用 resolve_cost_basis。
    """
    return _resolve_from_history(db, item_id, d)


def record_principal(db: Session, item_id: int, amount, effective_date: date,
                     reason: str | None,
                     evidence_ref: int | None = None) -> tuple[PrincipalChange, dict]:
    """登记口径变更（人裁决，R-9）。返回 (变更行, 审计 change dict)。

    Raises:
        ApiError: 项不存在(404 NAME_UNRESOLVED 复用 404 语义→NOT_FOUND)、
            归档项(409 ITEM_ARCHIVED)、reason 缺失(400 PRINCIPAL_REASON_REQUIRED)、
            金额非法(400 BAD_REQUEST)。
    """
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}")
    if item.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                       "已归档资产项不可登记口径（R-37 终态）")
    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "口径变更必须附理由（R-9：每次变动是人裁决的值，可追溯）")
    try:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"口径金额非法: {amount!r}")
    if not isinstance(effective_date, date):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "effective_date 必须为 date")

    before = current_principal(db, item_id)     # 旧生效值（追加前解析）
    action = "principal_set" if before is None else "principal_change"
    row = PrincipalChange(
        asset_item_id=item_id, new_amount=amt, effective_date=effective_date,
        reason=str(reason).strip(), evidence_ref=evidence_ref,
        origin="external", transfer_event_id=None,
        created_at=datetime.now().isoformat(),
    )
    db.add(row)
    db.flush()
    change = _change("principal_change", row.id, action,
                     before=str(before) if before is not None else None,
                     after=str(amt),
                     evidence_ref=evidence_ref)
    audit_service.record(db, change)            # 同事务：失败阻断业务
    db.commit()
    db.refresh(row)
    return row, change


def principal_history(db: Session, item_id: int) -> list[dict]:
    """按登记序返回口径变更史（同日多条按 id 升序展示；解析决胜是 DESC）。"""
    rows = db.execute(
        select(PrincipalChange)
        .where(PrincipalChange.asset_item_id == item_id)
        .order_by(PrincipalChange.effective_date, PrincipalChange.id)
    ).scalars().all()
    return [{
        "id": r.id,
        "amount": str(r.new_amount),
        "effective_date": r.effective_date.isoformat(),
        "reason": r.reason,
        "origin": r.origin,
        "evidence_ref": r.evidence_ref,
        "created_at": r.created_at,
    } for r in rows]
