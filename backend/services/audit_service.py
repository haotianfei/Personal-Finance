"""审计日志服务（v5 步骤 4，spec §3）。

单点发射：`record(db, change, ...)` 接收步骤 3 的 change dict
（{entity,row_id,action,before,after,evidence_ref}），入 audit_log，
**与业务写入同事务**（commit 前 db.add + flush）：
- 业务回滚 → 审计不留（无孤儿审计行）；
- 审计写失败 → 异常上抛、整事务失败、阻断业务（可信 > 便利）。

本模块只有 `record`（写）与查询函数，无任何 update/delete 端口；
append-only 由 0009 存储层触发器兜底。

runner（裸 sqlite3 连接）用 `record_sqlite(conn, change, ...)` 同一形状落行
（独立事务；actor=system）。

ADR-0027（审计留痕契约）：
- `entity`/`action`/`reason` 的受控枚举与脱敏规则**只在
  `services.audit_contract` 定义**，本模块只做校验与调用（单一真源）。
- `make_change()` 是唯一构造点：未登记的 entity/action → ValueError；
  `before`/`after` 过 `scrub()`（api_key/token/secret/password/... 一律脱敏）。
- `reason`/`reason_note` 是审计行的新列（0036），**不进 hash-chain 的 FIELDS**
  ——链口径冻结，加列不改哈希。
"""
import json
import sqlite3
from datetime import datetime

import structlog
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import AuditLog
from services import audit_chain
from services.audit_contract import (
    ACTION, ENTITY, REASON, REASON_REQUIRED_ACTIONS, scrub)

logger = structlog.get_logger(__name__)

_REQUIRED_KEYS = {"entity", "row_id", "action", "before", "after", "evidence_ref"}

_REASON_NOTE_MAX = 200

# 缺 reason 时的可读提示：一律点名 action（spec E6「message 点名 action」）。
_ACTION_LABEL = {
    "correct": "更正操作",
    "delete": "删除操作",
    "archive": "归档操作",
    "restore": "还原操作",
    "owner_change": "归属改判",
    "classification_change": "分类改判",
    "change": "改判操作",
}


def make_change(entity: str, row_id: int | None, action: str,
               before=None, after=None, evidence_ref: int | None = None,
               reason: str | None = None,
               reason_note: str | None = None) -> dict:
    """结构化变更描述（步骤 4 审计表的行形状，identity spec §7）——单点构造。

    放本模块供各 ledger/principal service 共用，避免 asset_service↔principal_service
    环 import；before=NULL 表创建、after=NULL 表删除。

    ADR-0027：
    - `entity`/`action` 必须已登记（`audit_contract.ENTITY/ACTION`）——枚举外即编码错误，
      当场 ValueError（不允许「先写后登记」，历史行会被口径劈成两半）。
    - `reason` **可不传**（缺省 NULL＝未记录原因）；**提供时**必须 ∈ `audit_contract.REASON`。
      `reason_note` ≤200 字。强制**不在本函数**（拿不到 actor）而在 `record()`：
      `actor='owner'` 的纠正类 `action`（`REASON_REQUIRED_ACTIONS`）缺 reason 即 422
      `AUDIT_REASON_REQUIRED`；`actor='system'` 不拦（spec E1/E2）。
    - `before`/`after` 过 `scrub()`：密钥类字段脱敏成 `"<redacted>"` + `_fp`/`_len`，
      原值不进审计表（审计是永久保留、可整库导出的表面）。
    """
    if entity not in ENTITY or action not in ACTION:
        raise ValueError(
            f"未登记的审计 entity/action: entity={entity!r} action={action!r}"
            f"（新增取值必须先登记 backend/services/audit_contract.py）")
    _check_reason(reason, reason_note)
    return {"entity": entity, "row_id": row_id, "action": action,
            "before": scrub(before), "after": scrub(after),
            "evidence_ref": evidence_ref,
            "reason": reason, "reason_note": reason_note}


def _check_reason(reason: str | None, reason_note: str | None):
    """reason/reason_note 形状校验（make_change 与 dict 路径共用）。"""
    if reason is not None and reason not in REASON:
        raise ValueError(
            f"非法审计 reason: {reason!r}（合法值 {sorted(REASON)}）")
    if reason_note is not None:
        if not isinstance(reason_note, str):
            raise ValueError(f"reason_note 必须是字符串: {type(reason_note).__name__}")
        if len(reason_note) > _REASON_NOTE_MAX:
            raise ValueError(
                f"reason_note 超长：{len(reason_note)} > {_REASON_NOTE_MAX}")


def _current_request_id() -> str | None:
    """自 contextvars 拾取 request_id（main.py RequestContextMiddleware 绑定）。"""
    try:
        return structlog.contextvars.get_contextvars().get("request_id")
    except Exception:  # pragma: no cover - contextvars 不可用时降级为 NULL
        return None


def _current_idem_key() -> str | None:
    """步骤 5：幂等键与 request_id 并列落审计（contextvars，无键则 NULL）。"""
    try:
        from services.idempotency_service import current_idem_key
        return current_idem_key()
    except Exception:  # pragma: no cover
        return None


def _dump(value) -> str | None:
    """业务语义值 → JSON 文本；None 保持 SQL NULL（before NULL＝创建，after NULL＝删除）。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _validate(change: dict):
    missing = _REQUIRED_KEYS - set(change)
    if missing:
        raise ValueError(f"change dict 缺键: {sorted(missing)}")


def record(db: Session, change: dict, *, actor: str = "owner",
           request_id: str | None = None) -> AuditLog:
    """ORM 会话路径（业务事务内调用，commit 前）。返回审计行（未提交）。

    **纠正类强制说明原因（ADR-0027 D7 收口，spec E1）**：`actor='owner'` 且
    `action ∈ REASON_REQUIRED_ACTIONS` 且 `reason` 为空 → `AUDIT_REASON_REQUIRED`
    （422），异常上抛使**业务与审计一并回滚**（零业务写入、零审计新增）。

    `actor='system'` 不拦：强制表达的是「人做的改判要留理由」，不是「系统动作
    要人填」。系统的自动路径（迁移/恢复/重建/保留策略/激活隐式归档）本就应当
    以 `actor='system'` 落行——让它们伪造 `owner` 行才是留假证据（spec E2）。

    拦截点选在本函数、不选 `make_change()`：后者拿不到 actor，也不该知道是谁在写。
    """
    _validate(change)
    if actor not in ("owner", "system"):
        raise ValueError(f"非法 actor: {actor}（单用户系统仅 owner|system）")
    reason = change.get("reason")
    reason_note = change.get("reason_note")
    if actor == "owner" and change["action"] in REASON_REQUIRED_ACTIONS \
            and not reason:
        action = change["action"]
        label = _ACTION_LABEL.get(action, action)
        raise ApiError(
            ErrorCode.AUDIT_REASON_REQUIRED, 422,
            f"{label}需要说明原因（action={action}）",
            {"action": action, "entity": change["entity"],
             "allowed": sorted(REASON)})
    _check_reason(reason, reason_note)     # dict 路径（未经 make_change）也兜底校验
    row = AuditLog(
        occurred_at=datetime.now().isoformat(),
        actor=actor,
        entity=change["entity"],
        action=change["action"],
        row_id=change.get("row_id"),
        # 兜底 scrub：make_change 已脱敏过，此处对「直接拼 dict」的老调用点再防一次
        # （scrub 幂等，重复调用不改已脱敏值的指纹/长度）
        before_json=_dump(scrub(change.get("before"))),
        after_json=_dump(scrub(change.get("after"))),
        evidence_ref=change.get("evidence_ref"),
        request_id=request_id if request_id is not None else _current_request_id(),
        idempotency_key=_current_idem_key(),
        reason=reason,
        reason_note=reason_note,
        prev_hash=None,
    )
    # 三期-3：算 prev_hash → INSERT → 链状态 CAS（同事务；失败 409）
    return audit_chain.append(db, row)


def record_sqlite(conn: sqlite3.Connection, change: dict, *, actor: str = "system",
                  request_id: str | None = None) -> int:
    """裸 sqlite3 连接路径（runner 专用；调用方管理事务）。返回 lastrowid。

    注：`audit_chain.append_sqlite` 的列集＝FIELDS＋prev_hash（链口径冻结，本 lane
    不改 audit_chain.py），因此本路径的 `reason`/`reason_note` 目前**不落库**
    （列保持 NULL＝未记录原因）；reason 仍做形状校验，非法值当场 ValueError。
    """
    _validate(change)
    if actor not in ("owner", "system"):
        raise ValueError(f"非法 actor: {actor}（单用户系统仅 owner|system）")
    _check_reason(change.get("reason"), change.get("reason_note"))
    values = {
        "occurred_at": datetime.now().isoformat(),
        "actor": actor,
        "entity": change["entity"],
        "action": change["action"],
        "row_id": change.get("row_id"),
        "before_json": _dump(scrub(change.get("before"))),
        "after_json": _dump(scrub(change.get("after"))),
        "evidence_ref": change.get("evidence_ref"),
        "request_id": (request_id if request_id is not None
                       else _current_request_id()),
        "idempotency_key": None,
    }
    return audit_chain.append_sqlite(conn, values)


# ==================== 只读查询（spec §5） ====================

def _out(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "occurred_at": row.occurred_at,
        "actor": row.actor,
        "entity": row.entity,
        "action": row.action,
        "row_id": row.row_id,
        "before": json.loads(row.before_json) if row.before_json else None,
        "after": json.loads(row.after_json) if row.after_json else None,
        "evidence_ref": row.evidence_ref,
        "request_id": row.request_id,
        "idempotency_key": getattr(row, "idempotency_key", None),
        "prev_hash": getattr(row, "prev_hash", None),
        "reason": getattr(row, "reason", None),
        "reason_note": getattr(row, "reason_note", None),
    }


def list_audit_log(db: Session, *, entity: str | None = None, row_id: int | None = None,
                   action: str | None = None, evidence_ref: int | None = None,
                   request_id: str | None = None,
                   idempotency_key: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   reason: str | None = None, actor: str | None = None,
                   order: str = "desc",
                   page: int = 1, page_size: int = 50) -> tuple[list[dict], int]:
    """筛选＋分页（风格同 list_assets）；occurred_at 为 ISO 文本，字符串序即时间序。

    票 02 扩参：`actor`（精确匹配 owner|system）、`order`（`asc`|`desc`，仅这两种；
    其它值 422——与代码库现有校验风格一致：`ApiError(VALIDATION_ERROR, 422, fields)`）。
    排序口径沿用既有的按 `id`（插入序＝时间序），asc 即「最早在前」。
    """
    if order not in ("asc", "desc"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, f"order 非法：{order!r}",
                       {"fields": [{"field": "order",
                                    "message": "只接受 asc 或 desc"}]})
    q = _filtered(entity=entity, row_id=row_id, action=action,
                  evidence_ref=evidence_ref, request_id=request_id,
                  idempotency_key=idempotency_key,
                  date_from=date_from, date_to=date_to, reason=reason,
                  actor=actor)
    from sqlalchemy import func, select
    total = db.execute(
        select(func.count()).select_from(q.subquery())
    ).scalar() or 0
    order_by = AuditLog.id.asc() if order == "asc" else AuditLog.id.desc()
    rows = db.execute(
        q.order_by(order_by).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return [_out(r) for r in rows], total


def get_audit_log(db: Session, log_id: int) -> dict | None:
    from sqlalchemy import select
    row = db.execute(select(AuditLog).where(AuditLog.id == log_id)).scalar_one_or_none()
    return _out(row) if row else None


def _filtered(**f):
    from sqlalchemy import select
    q = select(AuditLog)
    if f.get("entity"):
        q = q.where(AuditLog.entity == f["entity"])
    if f.get("row_id") is not None:
        q = q.where(AuditLog.row_id == f["row_id"])
    if f.get("action"):
        q = q.where(AuditLog.action == f["action"])
    if f.get("actor"):
        q = q.where(AuditLog.actor == f["actor"])
    if f.get("evidence_ref") is not None:
        q = q.where(AuditLog.evidence_ref == f["evidence_ref"])
    if f.get("request_id"):
        q = q.where(AuditLog.request_id == f["request_id"])
    if f.get("idempotency_key"):
        q = q.where(AuditLog.idempotency_key == f["idempotency_key"])
    if f.get("reason"):
        q = q.where(AuditLog.reason == f["reason"])
    if f.get("date_from"):
        q = q.where(AuditLog.occurred_at >= str(f["date_from"]))
    if f.get("date_to"):
        q = q.where(AuditLog.occurred_at <= str(f["date_to"]) + "\uffff")
    return q
