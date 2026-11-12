"""资产项改名：原因 / 影响面 / 可选历史回写（ADR-0026，票 03）。

契约（D1–D16）：

- `reason` 三值：`correction`（旧名写错了）/ `evolution`（名称演进）/
  `normalization`（书写归一）；`effect` 两值：`current`（只改当前名）/
  `history_backfill`（连历史快照的名称文本一起改写，item id 不变）。
- 默认强绑定：correction → history_backfill；其余 → current。选非默认组合
  必须写 `reason_note`（否则 400）——「为什么要破例」是留痕的一部分。
- 两步：`preview` 算影响面并发一次性令牌（10 分钟）；`execute` 带令牌执行，
  并重新校验影响面哈希——预览后被别的进程改过行，就必须重新预览。
- 撞名：目标名归一后（`_cmp_norm`）与任何**其他**项的三层名（现名/历史名/别名）
  相同即 409；自己的历史名不算（撤销改名 = 再改回旧名）。
- 单事务：名史两行 + display_name + 历史回写 + 审计 + 令牌作废，一起提交。
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import Asset, AssetItem, AssetItemName, AssetItemRenameToken
from services import audit_service, name_resolution_service
from services.name_norm import _cmp_norm

REASONS = ("correction", "evolution", "normalization")
EFFECTS = ("current", "history_backfill")
DEFAULT_EFFECT = {"correction": "history_backfill",
                  "evolution": "current",
                  "normalization": "current"}
TOKEN_TTL_MINUTES = 10

# 改名 → 审计 reason（ADR-0027 D2/D7 映射，R2）：回写历史＝**纠正过去**（correction），
# 只改当前名＝**名称演进**（evolution）。改名自己的三值语义（含 normalization）已落在
# 审计行的 `after.reason`，故这里只按「影响面」二分，不新造输入字段。
AUDIT_REASON_BY_EFFECT = {"history_backfill": "correction", "current": "evolution"}


# --------------------------- 校验与影响面 --------------------------- #
def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "显示名不可为空")
    if len(name) > 100:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "显示名不得超过 100 字")
    return name


def _clean_note(note: str | None) -> str | None:
    note = (note or "").strip()
    if not note:
        return None
    if len(note) > 200:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "备注不得超过 200 字")
    return note


def _validate_choice(reason: str | None, effect: str | None,
                     note: str | None) -> tuple[str, str, str | None]:
    """原因/影响面/备注三者的联合校验（D1–D3）。"""
    if not reason or reason not in REASONS:
        raise ApiError(
            ErrorCode.RENAME_REASON_REQUIRED, 400,
            "必须选择改名原因：correction（旧名错误）/ evolution（名称演进）/ "
            "normalization（书写归一）",
            {"allowed": list(REASONS)})
    if not effect:
        effect = DEFAULT_EFFECT[reason]      # D3：默认强绑定，可省略
    elif effect not in EFFECTS:
        raise ApiError(
            ErrorCode.RENAME_REASON_REQUIRED, 400,
            "影响范围只能是 current（只改当前名）或 history_backfill（回写历史记录）",
            {"allowed": list(EFFECTS)})
    note = _clean_note(note)
    if effect != DEFAULT_EFFECT[reason] and not note:
        raise ApiError(
            ErrorCode.RENAME_NOTE_REQUIRED, 400,
            f"{reason} 的默认影响范围是 {DEFAULT_EFFECT[reason]}，"
            f"选 {effect} 属于破例，必须在备注里写清理由",
            {"default_effect": DEFAULT_EFFECT[reason]})
    return reason, effect, note


def _load_item(db: Session, item_id: int) -> AssetItem:
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       {"item_id": item_id})
    return item


def _impact(db: Session, item_id: int, item: AssetItem) -> dict:
    row = db.execute(
        select(func.count(Asset.id),
               func.min(Asset.asset_date),
               func.max(Asset.asset_date),
               func.sum(case((Asset.superseded_by.isnot(None), 1), else_=0)))
        .where(Asset.asset_item_id == item_id)
    ).one()
    rows, first, last, superseded = row
    return {
        "rows": int(rows or 0),
        "first_date": first.isoformat() if first else None,
        "last_date": last.isoformat() if last else None,
        "superseded_rows": int(superseded or 0),
        "archived_item": item.status == "archived",
    }


def _impact_hash(impact: dict) -> str:
    raw = json.dumps(impact, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_no_conflict(db: Session, name: str, item_id: int) -> None:
    index = name_resolution_service.build_index(db, include_archived=True)
    conflicts = name_resolution_service.conflicts_with(
        index, name, exclude_item_id=item_id)
    if conflicts:
        other_id, level = conflicts[0]
        other = index.items[other_id]
        raise ApiError(
            ErrorCode.NAME_CONFLICT, 409,
            f"目标名「{name}」与资产项 #{other_id}「{other.display_name}」"
            f"的{_LEVEL_LABEL.get(level, '名称')}冲突（归一后相同）",
            {"conflicting_item_id": other_id,
             "conflicting_display_name": other.display_name,
             "matched_level": level,
             "normalized": _cmp_norm(name)})


_LEVEL_LABEL = {"display": "当前名", "history": "历史名", "alias": "别名"}


# --------------------------- 预览 --------------------------- #
def preview(db: Session, item_id: int, *, name: str, reason: str | None,
            effect: str | None, reason_note: str | None = None) -> dict:
    """算影响面 + 发一次性令牌；不写任何业务数据（只落令牌行）。"""
    name = _clean_name(name)
    reason, effect, note = _validate_choice(reason, effect, reason_note)
    item = _load_item(db, item_id)
    if name == item.display_name:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "新名与当前名相同，无需改名",
                       {"item_id": item_id, "display_name": item.display_name})
    _ensure_no_conflict(db, name, item_id)
    impact = _impact(db, item_id, item)
    impact_hash = _impact_hash(impact)

    token = secrets.token_urlsafe(24)
    expires_at = datetime.now() + timedelta(minutes=TOKEN_TTL_MINUTES)
    payload = {"item_id": item_id, "name": name, "reason": reason,
               "effect": effect, "reason_note": note,
               "impact_hash": impact_hash}
    db.add(AssetItemRenameToken(
        token=token, item_id=item_id,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        impact_hash=impact_hash, expires_at=expires_at))
    db.commit()

    return {
        "item_id": item_id,
        "current_name": item.display_name,
        "new_name": name,
        "reason": reason,
        "effect": effect,
        "reason_note": note,
        "default_effect": DEFAULT_EFFECT[reason],
        "impact": impact,
        "confirm_token": token,
        "expires_at": expires_at.isoformat(),
    }


# --------------------------- 执行 --------------------------- #
def _consume_token(db: Session, confirm_token: str, *, item_id: int, name: str,
                   reason: str, effect: str, note: str | None) -> AssetItemRenameToken:
    token = (confirm_token or "").strip()
    if not token:
        raise ApiError(ErrorCode.RENAME_CONFIRM_INVALID, 409,
                       "缺少确认令牌：请先调用 preview 预览影响面",
                       {"item_id": item_id})
    row = db.execute(
        select(AssetItemRenameToken)
        .where(AssetItemRenameToken.token == token)
    ).scalars().first()
    if row is None or row.item_id != item_id:
        raise ApiError(ErrorCode.RENAME_CONFIRM_INVALID, 409,
                       "确认令牌无效：请重新预览", {"item_id": item_id})
    if row.used_at is not None:
        raise ApiError(ErrorCode.RENAME_CONFIRM_INVALID, 409,
                       "确认令牌已使用：请重新预览", {"item_id": item_id})
    if row.expires_at < datetime.now():
        raise ApiError(ErrorCode.RENAME_CONFIRM_INVALID, 409,
                       "确认令牌已过期（10 分钟）：请重新预览",
                       {"item_id": item_id, "expires_at": row.expires_at.isoformat()})
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):  # pragma: no cover - 落库内容受本模块控制
        raise ApiError(ErrorCode.RENAME_CONFIRM_INVALID, 409,
                       "确认令牌载荷损坏：请重新预览", {"item_id": item_id})
    expected = {"item_id": item_id, "name": name, "reason": reason,
                "effect": effect, "reason_note": note}
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ApiError(
                ErrorCode.RENAME_CONFIRM_INVALID, 409,
                f"确认令牌与本次请求不一致（{key}）：请重新预览",
                {"item_id": item_id, "field": key})
    return row


def execute(db: Session, item_id: int, *, name: str, reason: str | None,
            effect: str | None, reason_note: str | None = None,
            confirm_token: str | None = None) -> dict:
    """执行改名（单事务）。调用方负责 commit 之外的错误映射。"""
    name = _clean_name(name)
    reason, effect, note = _validate_choice(reason, effect, reason_note)
    item = _load_item(db, item_id)
    token_row = _consume_token(db, confirm_token or "", item_id=item_id, name=name,
                               reason=reason, effect=effect, note=note)
    if name == item.display_name:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "新名与当前名相同，无需改名",
                       {"item_id": item_id, "display_name": item.display_name})
    _ensure_no_conflict(db, name, item_id)      # 令牌校验后、写之前再判一次
    impact = _impact(db, item_id, item)
    if _impact_hash(impact) != token_row.impact_hash:
        raise ApiError(
            ErrorCode.RENAME_CONFIRM_INVALID, 409,
            "预览后该资产项的历史记录发生了变化：请重新预览",
            {"item_id": item_id, "impact": impact})

    before_name = item.display_name
    history_max = db.execute(
        select(func.max(AssetItemName.id))).scalar() or 0

    recorded = set(db.execute(
        select(AssetItemName.name).where(AssetItemName.item_id == item_id)
    ).scalars().all())
    if before_name and before_name not in recorded:
        created = item.created_at if isinstance(item.created_at, datetime) else datetime.now()
        db.add(AssetItemName(item_id=item_id, name=before_name,
                             effective_from=created.date(), origin="rename",
                             reason=reason, effect=effect))
    new_row = AssetItemName(item_id=item_id, name=name,
                            effective_from=date.today(), origin="rename",
                            reason=reason, effect=effect, reason_note=note)
    db.add(new_row)
    item.display_name = name
    item.updated_at = datetime.now()
    db.flush()                                   # 取 name_history_id

    backfill_rows = 0
    if effect == "history_backfill":
        backfill_rows = db.query(Asset).filter(
            Asset.asset_item_id == item_id
        ).update({Asset.asset_name: name, Asset.updated_at: datetime.now()},
                 synchronize_session=False)

    change = audit_service.make_change(
        "asset_item", item_id, "rename",
        before={"display_name": before_name, "name_history_max_id": history_max},
        after={"display_name": name, "reason": reason, "effect": effect,
               "reason_note": note, "backfill_rows": int(backfill_rows),
               "name_history_id": new_row.id},
        reason=AUDIT_REASON_BY_EFFECT[effect], reason_note=note)
    audit_service.record(db, change)
    token_row.used_at = datetime.now()
    db.commit()
    db.refresh(item)

    return {
        "item": {"id": item.id, "display_name": item.display_name,
                 "channel": item.channel, "status": item.status},
        "effect": effect,
        "backfill_rows": int(backfill_rows),
        "name_history_id": new_row.id,
        "change": change,
    }