"""资产项资源（v5 身份层，spec §5）。

list：id/display_name/channel/status/note/updated_at/aliases；
动作端点：rename（留史）、archive/unarchive（状态机，R-37）、
principal（本金口径登记/查询，二期 2.1：更正＝追加，无 update/delete）、
note（项级备注整条替换，ADR-0031；只落审计与 updated_at，不进 attr_history）。
均为裁决动作；返回结构化变更记录（审计行形状）。
"""
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError, ErrorCode
from models import AssetItem
from schemas import AUDIT_REASON_NOTE_MAX, NOTE_MAX, AuditReason
from services.audit_contract import rule_for
from services.idempotency_service import idempotency_guard
from services import (asset_history_service, asset_owner_service,
                      asset_rename_service, asset_service, audit_service,
                      principal_service)

router = APIRouter()


class RenamePreviewRequest(BaseModel):
    """改名预览（ADR-0026 D11）：只算影响面 + 发令牌，不写业务数据。"""
    name: str
    reason: str | None = None          # correction | evolution | normalization
    effect: str | None = None          # current | history_backfill
    reason_note: str | None = None


class RenameRequest(RenamePreviewRequest):
    """改名执行：必须带 preview 返回的 confirm_token（一次性，10 分钟）。"""
    confirm_token: str | None = None


class OwnerRequest(BaseModel):
    """归属写（ADR-0019 D6）：唯一真相源是 `asset_items.owner_id`，值域 members。"""
    owner_kind: str
    owner_id: int | None = None
    reason: str | None = None


class PrincipalRequest(BaseModel):
    """口径登记（R-9）：amount 带符号（注入正/撤出负）；reason 必填。"""
    amount: str
    effective_date: str          # YYYY-MM-DD（R-36）
    reason: str
    evidence_ref: int | None = None


class BatchOwnerRequest(BaseModel):
    """批量归属：逐项独立事务；ids 去重 1..200。"""
    ids: list[int]
    owner_kind: str
    owner_id: int | None = None
    reason: str
    effective_date: date | None = None


class ItemNoteRequest(BaseModel):
    """项级备注写（ADR-0031 D2/D3）：整条替换，空串/纯空白＝清空（落 NULL）。

    `note` 是**必填键**（值可为 null）：漏传这个键＝调用方写错，直接 422，而不是
    静默把已有备注清掉。`reason`/`reason_note` 只进审计行（ADR-0027 封闭枚举），
    与备注正文无关；缺省＝审计行 NULL，不阻塞写入。
    """
    note: str | None
    reason: AuditReason | None = None
    reason_note: str | None = Field(default=None, max_length=AUDIT_REASON_NOTE_MAX)


@router.get("", response_model=dict)
def list_asset_items(
    include_archived: bool = True,
    db: Session = Depends(get_db),
):
    """资产项列表（含历史名 aliases、项级备注 note 与 updated_at）。

    备注读回（ADR-0031 D2/D4，票 14）：备注只落审计、不进
    `asset_item_attr_history`，而审计是**变更流**不是当前值（清库/回填不产审计行），
    故当前值必须由业务列直接读。这里在列表里一并补齐，**一条查询取全量**——
    逐项读会变列表页 N+1，正是本票要消除的形态。

    `note` 语义与写入侧（`PUT /{item_id}/note`）一致：只有文本或 `NULL`；
    trim 后空串在写入时就已落 `NULL`，这里再做一次归一（`""` → `null`），
    免得历史脏数据在前端被当成「有备注」而点亮便签标记。
    """
    items = asset_service.list_items(db, include_archived=include_archived)
    ids = [it["id"] for it in items]
    meta: dict[int, tuple[Optional[str], object]] = {}
    if ids:
        meta = {
            item_id: (note, updated_at)
            for item_id, note, updated_at in db.execute(
                select(AssetItem.id, AssetItem.note, AssetItem.updated_at)
                .where(AssetItem.id.in_(ids))).all()
        }
    for it in items:
        note, updated_at = meta.get(it["id"], (None, None))
        it["note"] = note or None            # 空串＝没写（与写入侧同一语义）
        it["updated_at"] = updated_at.isoformat() if updated_at else None
    return {"items": items, "total": len(items)}


@router.get("/owners", response_model=dict)
def bulk_item_owners(
    ids: str = Query(..., description="逗号分隔 item id，≤200 个"),
    db: Session = Depends(get_db),
):
    """批量当前归属（记录页避免逐项 N+1；无行=未标注，返回 null 字段）。"""
    raw = [part.strip() for part in (ids or "").split(",") if part.strip()]
    if not raw:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 不能为空",
                       details={"fields": [{"field": "ids",
                                            "message": "ids 不能为空"}]})
    if len(raw) > 200:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 最多 200 个",
                       details={"fields": [{"field": "ids",
                                            "message": "最多 200 个 id"}]})
    try:
        parsed = [int(part) for part in raw]
    except ValueError:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 含非法 id",
                       details={"fields": [{"field": "ids",
                                            "message": "id 须为整数"}]})
    ordered = list(dict.fromkeys(parsed))
    existing = {row[0] for row in db.execute(
        select(AssetItem.id).where(AssetItem.id.in_(ordered))).all()}
    owner_map = asset_owner_service.resolve_owner_map(db, existing)
    items = []
    for item_id in ordered:
        if item_id not in existing:
            continue
        owner = owner_map.get(item_id) or {}
        items.append({
            "id": item_id,
            "owner_kind": owner.get("owner_kind"),
            "owner_id": owner.get("owner_id"),
            "owner_name": owner.get("owner_name"),
            "effective_date": owner.get("effective_date"),
        })
    return {"items": items, "total": len(items)}


@router.post("/owners/batch", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/asset-items/owners/batch", required=True))])
def batch_item_owners(
    request: BatchOwnerRequest,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """批量修改历史归属（append-only；成功项逐项提交，失败项不阻断）。

    审计原因（spec E3 形状）：`action='change'` 属纠正类，`?reason=` 缺省时
    每项都会以 `AUDIT_REASON_REQUIRED` 进 `failed[]`（逐项独立事务，故不是整批 422）。
    请求体 `reason`（归属变更史自由文本）与 query `reason`（审计枚举）是两回事。
    """
    return asset_owner_service.batch_set_owner(
        db, request.ids, request.owner_kind, request.owner_id,
        request.reason, request.effective_date, audit_reason=reason,
        audit_reason_note=reason_note or None)


@router.post("/{item_id}/rename/preview", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/asset-items/rename/preview"))])
def preview_asset_item_rename(item_id: int, request: RenamePreviewRequest,
                              db: Session = Depends(get_db)):
    """改名预览：回影响面 + 一次性令牌；改名原因/影响范围在此校验（缺则 400）。"""
    return asset_rename_service.preview(
        db, item_id, name=request.name, reason=request.reason,
        effect=request.effect, reason_note=request.reason_note)


@router.post("/{item_id}/rename", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/asset-items/rename"))])
def rename_asset_item(item_id: int, request: RenameRequest,
                      db: Session = Depends(get_db)):
    """改名：名史追加 + display_name 更新 +（纠正类）历史快照回写；id 不变。

    履约顺序：令牌校验 → 撞名再判 → 影响面哈希比对 → 单事务写入。
    """
    return asset_rename_service.execute(
        db, item_id, name=request.name, reason=request.reason,
        effect=request.effect, reason_note=request.reason_note,
        confirm_token=request.confirm_token)


@router.post("/{item_id}/archive", response_model=dict, dependencies=[Depends(idempotency_guard("POST /api/asset-items/archive"))])
def archive_asset_item(
    item_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录，不阻塞）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """归档（R-37 终态）：默认不入现状视图，历史与复盘永久可查，不删记录。

    理由走 query 参数（`?reason=&reason_note=`），只进审计行，不改状态机语义。
    """
    result = asset_service.set_item_status(
        db, item_id, "archived", reason=reason, reason_note=reason_note or None)
    if result is None:
        raise HTTPException(status_code=404, detail="AssetItem not found")
    item, change = result
    return {
        "item": {"id": item.id, "display_name": item.display_name, "status": item.status},
        "change": change,
    }


@router.post("/{item_id}/unarchive", response_model=dict, dependencies=[Depends(idempotency_guard("POST /api/asset-items/unarchive"))])
def unarchive_asset_item(item_id: int, db: Session = Depends(get_db)):
    """撤销归档（人裁决回退状态）。"""
    result = asset_service.set_item_status(db, item_id, "active")
    if result is None:
        raise HTTPException(status_code=404, detail="AssetItem not found")
    item, change = result
    return {
        "item": {"id": item.id, "display_name": item.display_name, "status": item.status},
        "change": change,
    }


@router.post("/{item_id}/principal", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/asset-items/principal"))])
def set_principal(item_id: int, request: PrincipalRequest, db: Session = Depends(get_db)):
    """登记本金口径变更（R-9 人裁决；更正＝再 POST 一条，不改史、无 update/delete）。"""
    try:
        eff = date.fromisoformat((request.effective_date or "").strip())
    except ValueError:
        raise ApiError("BAD_REQUEST", 400, "effective_date 必须为 YYYY-MM-DD")
    row, change = principal_service.record_principal(
        db, item_id, request.amount, eff, request.reason,
        evidence_ref=request.evidence_ref)
    cur = principal_service.current_principal(db, item_id)  # 生效值按史解析（可能回填日期）
    return {
        "entry": {
            "id": row.id,
            "amount": str(row.new_amount),
            "effective_date": row.effective_date.isoformat(),
            "reason": row.reason,
            "origin": row.origin,
            "evidence_ref": row.evidence_ref,
        },
        "current": str(cur) if cur is not None else None,
        "change": change,
    }


@router.get("/{item_id}/principal", response_model=dict)
def get_principal(item_id: int, db: Session = Depends(get_db)):
    """口径变更史＋当前生效值（resolve＝史末值；None＝从未登记）。"""
    return {
        "item_id": item_id,
        "history": principal_service.principal_history(db, item_id),
        "current": (lambda v: str(v) if v is not None else None)(
            principal_service.current_principal(db, item_id)),
    }


@router.get("/{item_id}/history", response_model=dict)
def get_asset_item_history(
    item_id: int,
    limit: int = Query(50, description="条数（默认 50；>200 一律取 200）"),
    cursor: str | None = Query(None, description="不透明游标（上一页 next_cursor）"),
    sources: str = Query("audit,trace", description="来源：audit / trace（逗号分隔）"),
    db: Session = Depends(get_db),
):
    """资产项变更史（取证视图，票 02）：审计行 ∪ 数据自述痕迹，去重后单时间线。

    契约冻结（前端逐字消费，字段名/嵌套不得改）：见
    `services/asset_history_service.timeline` 的模块 docstring。`item_id` 不存在 → 404，
    不返回空时间线冒充成功。
    """
    return asset_history_service.timeline(
        db, item_id, limit=limit, cursor=cursor, sources=sources)


@router.get("/{item_id}/owner", response_model=dict)
def get_item_owner(item_id: int, db: Session = Depends(get_db)):
    """当前归属＋变更史（item 级唯一真源；current=None＝未标注）。"""
    if db.get(AssetItem, item_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}", {"item_id": item_id})
    return {
        "item_id": item_id,
        "current": asset_owner_service.current_owner(db, item_id),
        "history": asset_owner_service.owner_history(db, item_id),
    }


@router.post("/{item_id}/owner", response_model=dict,
             dependencies=[Depends(idempotency_guard("POST /api/asset-items/owner"))])
def set_item_owner(
    item_id: int,
    request: OwnerRequest,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举）。与请求体 `reason`（变更史自由"
                    "文本）不是同一个字段：这里是 query，那边是 body")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """写归属（canonical 写入口）：同一事务更新 `asset_items.owner_id` + 追加变更史 + 审计。

    `owner_kind` 只接受 `member`（值域 members）；「家庭池」随 ADR-0019 D6 退役，
    共有归属用 `role='joint'` 的成员。同值不追加（幂等）、归档项 409、非法 422。

    两套「原因」并存，别混：**请求体** `reason`＝归属变更史的自由文本（既有语义，
    原样进 `asset_item_owner_changes.reason`）；**query** `reason`＝审计行的封闭枚举
    （ADR-0027）。前者不能塞进后者的封闭集，故走不同位置。
    """
    entry, change = asset_owner_service.set_item_owner(
        db, item_id, request.owner_kind, request.owner_id, reason=request.reason,
        audit_reason=reason, audit_reason_note=reason_note or None)
    return {
        "entry": entry,
        "current": asset_owner_service.current_owner(db, item_id),
        "change": change,
    }


def _note_rule():
    """取契约表里备注端点那行；缺行/无 entity/action ＝表与代码脱节，当场炸。

    entity/action **从契约表取**，不在本文件另抄一份（ADR-0027 D9：表是唯一真源，
    改表即改留痕）。与 `classification_service._contract_rule` 同一套路，只是那边
    服务层持有、这里写入口在路由层（本票不动服务层写域）。
    """
    rule = rule_for("PUT", "/api/asset-items/{item_id}/note")
    if rule is None or not rule.entity or not rule.action:
        raise RuntimeError(
            "审计契约表缺行：PUT /api/asset-items/{item_id}/note（ADR-0027 D9/D12）")
    return rule


@router.put("/{item_id}/note", response_model=dict)
def set_item_note(
    item_id: int,
    request: ItemNoteRequest,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录，不阻塞）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """写项级备注（ADR-0031 D2/D3）：整条替换，备注挂**项**不挂记录。

    - trim 后空串/纯空白 → 落 `NULL`（＝清空）；长度按 trim 后的 Unicode 码点算，
      > `NOTE_MAX`（500）→ 422 `NOTE_TOO_LONG` 且库里不动。
    - 归档项**允许**改：备注不参与净值/计量，不是改账本事实（D3）。
    - 留痕只落审计（`asset_item/note_update`，`before`/`after` 各含旧/新 `note`）
      与 `updated_at`；**不写** `asset_item_attr_history`（D4：该表 dim 白名单不含 note）。
    - 同值写入＝口径未变 → 零审计行、`updated_at` 不变（与项级分类同口径）。
    - `reason`/`reason_note` 两处都收：**query 优先**（六个端点统一形态），请求体同名
      两字段是兼容口（与 `POST /api/assets/{id}/correct` 同写法）。缺省＝审计行 NULL。
    """
    audit_reason = reason if reason is not None else request.reason
    audit_note = reason_note if reason_note is not None else request.reason_note

    item = db.get(AssetItem, item_id)
    if item is None:
        # 错误码名实分离：码是本端点的契约（前端逐字消费），不复用通用 NOT_FOUND
        raise ApiError("ASSET_ITEM_NOT_FOUND", 404, f"资产项不存在: {item_id}",
                       {"item_id": item_id})

    text = (request.note or "").strip()
    if len(text) > NOTE_MAX:
        raise ApiError("NOTE_TOO_LONG", 422,
                       f"备注最多 {NOTE_MAX} 字（当前 {len(text)} 字）",
                       {"max_length": NOTE_MAX, "length": len(text)})
    new_note = text or None          # trim 后空串＝清空，落 SQL NULL 而非 ''
    old_note = item.note

    if new_note != old_note:
        item.note = new_note
        db.flush()
        rule = _note_rule()
        audit_service.record(db, audit_service.make_change(
            rule.entity, item_id, rule.action,
            before={"note": old_note}, after={"note": new_note},
            reason=audit_reason, reason_note=audit_note or None))
        db.commit()

    return {
        "item_id": item_id,
        "note": item.note,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
