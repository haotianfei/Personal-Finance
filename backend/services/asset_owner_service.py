"""资产项级归属服务（ADR-0011 变更史 / ADR-0019 D6 值域收口）——归属唯一读写入口。

**唯一真相源：`asset_items.owner_id`（值域 `members.id`）。**

为什么改了读路径：ADR-0011 时代把「当前归属」定义成变更史最新一行（`effective_date
DESC, created_at DESC, id DESC`），并刻意不设当前列。ADR-0019 把分类移到资产项级后，
`asset_items.owner_id` 成了列表/记录/导出/筛选的统一读源；而写路径仍只追加变更史，
于是同一件事有两个真相——**改了归属，列表和导出看不到**。本模块现在把读写都收口到列：

- 写：`set_item_owner` 在**同一事务**里更新 `asset_items.owner_id` **并**追加
  `asset_item_owner_changes`（变更史降级为纯审计，不再是读路径的数据源）；
- 读：`current_owner` / `resolve_owner_map` 一律 `owner_id` + `members` 出，
  变更史只用于回填 `effective_date` / `reason` / `origin` 这些**展示用元数据**
  （且仅当最新史行与列一致时才回填，避免拿旧口径冒充现值）；
- `owner_history`：变更史（登记序升序），只给「归属怎么变成现在这样」的审计视图。

其余既有契约不变：归档项 409 ITEM_ARCHIVED；参数非法 422 VALIDATION_ERROR；
同值不追加（幂等）；同日多改＝追加（更正）；仅迁移行受 `uq_item_owner_migration`。

**「家庭池」随 ADR-0019 D6 退役**：`asset_items.owner_id` 是 NOT NULL 外键（只有
`draft`/`archived` 可空），NULL 无法表达「家庭共有」这一归属，`family_pool` 在新
读模型里会退化成「未标注」——那正是要根治的双口径。共有归属改用 `members.role='joint'`
的成员表达；旧 `owner_kind='family_pool'` 请求显式 422 并指路，不做静默降级。

**脏值防御**：历史行若指向 `members` 里不存在的 id（0026 之前的旧 `attributes.id`
就被写在这个列位上），读到未知 id 时 `owner_name` 返回 null 并记 WARNING ——
不静默清理、不猜测映射（清理属 0027 车道的裁决）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import AssetItem, AssetItemOwnerChange, Member
from services import audit_service, classification_service
from services.audit_service import make_change as _change

log = logging.getLogger(__name__)

MEMBER = "member"
FAMILY_POOL = "family_pool"           # 已退役：只用于识别旧调用方并给出指路错误
OWNER_KINDS = (MEMBER,)

_FAMILY_POOL_RETIRED = (
    "「家庭池」合成归属已随 ADR-0019 D6 退役：归属唯一真相源是 "
    "asset_items.owner_id（值域 members.id），NULL 无法表达共有归属。"
    "共有归属请先建 role='joint' 的成员（POST /api/classification/members），"
    "再把它设为该项归属（POST /api/asset-items/{item_id}/owner）。")
_OWNER_KIND_MESSAGE = "owner_kind 仅支持 member（值域为 members.id）"


# ---------------------------------------------------------------- 脏值防御


def _warn_unknown_owner_id(owner_id, where: str) -> None:
    """读到 members 里不存在的 owner id：WARNING + 调用方按 null 处理（不清理数据）。"""
    log.warning(
        "归属 owner_id=%s 在 members 中不存在（疑似 0026 之前的旧 attributes.id "
        "脏值，来源：%s）；按「未知」返回 null，不清理、不猜测映射",
        owner_id, where)


def _member_names(db: Session, owner_ids) -> dict[int, str]:
    ids = sorted({int(i) for i in owner_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(Member.id, Member.name).where(Member.id.in_(ids))
    ).all()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------- 读路径


def _dated(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _row_dict(row: AssetItemOwnerChange, owner_name: str | None) -> dict:
    """变更史行 → 出参（史是审计视图，owner_kind 原样回显）。"""
    return {
        "id": row.id,
        "asset_item_id": row.asset_item_id,
        "owner_kind": row.owner_kind,
        "owner_id": row.owner_id,
        "owner_name": owner_name,
        "effective_date": _dated(row.effective_date),
        "reason": row.reason,
        "origin": row.origin,
        "created_at": row.created_at,
    }


def _current_dict(item: AssetItem, owner_name: str | None,
                  meta: AssetItemOwnerChange | None) -> dict:
    """当前归属出参：值来自列，元数据（生效日/原因/来源）来自与之相符的最新史行。"""
    return {
        "id": meta.id if meta is not None else None,
        "asset_item_id": item.id,
        "owner_kind": MEMBER,
        "owner_id": item.owner_id,
        "owner_name": owner_name,
        "effective_date": _dated(meta.effective_date) if meta is not None else None,
        "reason": meta.reason if meta is not None else None,
        "origin": meta.origin if meta is not None else None,
        "created_at": meta.created_at if meta is not None else None,
    }


def _items_with_owner(db: Session, item_ids) -> dict[int, tuple[AssetItem, str | None]]:
    """批量取「有归属」的项 → {item_id: (item, member_name)}；无 owner_id 的项不出现。"""
    ids = sorted({int(i) for i in item_ids if i is not None})
    if not ids:
        return {}
    items = db.execute(
        select(AssetItem)
        .where(AssetItem.id.in_(ids), AssetItem.owner_id.is_not(None))
    ).scalars().all()
    names = _member_names(db, [item.owner_id for item in items])
    out: dict[int, tuple[AssetItem, str | None]] = {}
    for item in items:
        name = names.get(item.owner_id)
        if name is None:
            _warn_unknown_owner_id(item.owner_id, f"asset_items.id={item.id}")
        out[item.id] = (item, name)
    return out


def _latest_history(db: Session, item_ids) -> dict[int, AssetItemOwnerChange]:
    """item → 最新一行变更史（item 内按 effective_date/created_at/id 降序）。

    只用于展示元数据；现值一律取 `asset_items.owner_id`。
    """
    ids = sorted({int(i) for i in item_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(AssetItemOwnerChange)
        .where(AssetItemOwnerChange.asset_item_id.in_(ids))
        .order_by(AssetItemOwnerChange.asset_item_id,
                  AssetItemOwnerChange.effective_date.desc(),
                  AssetItemOwnerChange.created_at.desc(),
                  AssetItemOwnerChange.id.desc())
    ).scalars().all()
    out: dict[int, AssetItemOwnerChange] = {}
    for row in rows:
        out.setdefault(row.asset_item_id, row)
    return out


def current_owner(db: Session, item_id: int) -> dict | None:
    """当前归属（读 `asset_items.owner_id`；无归属＝未标注 → None）。"""
    found = _items_with_owner(db, [item_id]).get(item_id)
    if found is None:
        return None
    item, name = found
    meta = _latest_history(db, [item_id]).get(item_id)
    return _current_dict(item, name,
                         meta if meta is not None and meta.owner_id == item.owner_id
                         else None)


# 规格 §6.1 命名（resolve_current == current_owner）
resolve_current = current_owner


def resolve_owner_map(db: Session, item_ids) -> dict[int, dict]:
    """批量当前归属（列表/导出，避免 N+1）：{item_id: owner dict}，无归属项不出现。"""
    found = _items_with_owner(db, item_ids)
    if not found:
        return {}
    metas = _latest_history(db, list(found))
    out: dict[int, dict] = {}
    for item_id, (item, name) in found.items():
        meta = metas.get(item_id)
        out[item_id] = _current_dict(
            item, name,
            meta if meta is not None and meta.owner_id == item.owner_id else None)
    return out


def owner_history(db: Session, item_id: int) -> list[dict]:
    """变更史（登记序升序；同日多条按 created_at、id 升序）——纯审计视图。"""
    rows = db.execute(
        select(AssetItemOwnerChange)
        .where(AssetItemOwnerChange.asset_item_id == item_id)
        .order_by(AssetItemOwnerChange.effective_date,
                  AssetItemOwnerChange.created_at,
                  AssetItemOwnerChange.id)
    ).scalars().all()
    names = _member_names(db, [row.owner_id for row in rows])
    out: list[dict] = []
    for row in rows:
        name = names.get(row.owner_id) if row.owner_id is not None else None
        if row.owner_id is not None and name is None:
            _warn_unknown_owner_id(
                row.owner_id,
                f"asset_item_owner_changes.id={row.id} (item={row.asset_item_id})")
        out.append(_row_dict(row, name))
    return out


# ---------------------------------------------------------------- 写入口


def _validation_error(field: str, message: str) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_ERROR, 422, message,
                    {"fields": [{"field": field, "message": message}]})


def _validate_owner(db: Session, owner_kind: str, owner_id: int | None) -> None:
    if owner_kind == FAMILY_POOL:
        raise _validation_error("owner_kind", _FAMILY_POOL_RETIRED)
    if owner_kind not in OWNER_KINDS:
        raise _validation_error("owner_kind",
                                f"{_OWNER_KIND_MESSAGE}：{owner_kind!r}")
    if owner_id is None:
        raise _validation_error("owner_id", "成员归属必须指定 owner_id")
    if db.get(Member, owner_id) is None:
        raise _validation_error(
            "owner_id", f"owner_id 必须是已存在的成员（members.id）：{owner_id}")


def _same(owner: dict | None, owner_id: int | None) -> bool:
    """同值判定以列现值（`asset_items.owner_id`）为准，不看变更史。"""
    return owner is not None and owner.get("owner_id") == owner_id


def append_change(db: Session, item: AssetItem, owner_id: int, *,
                  reason: str | None = None, origin: str = "manual",
                  effective_date: date | None = None) -> AssetItemOwnerChange:
    """追加一行归属史（**只进 session：不 flush、不提交、不改列**）。

    给「同事务写列 + 追加史」的多入口复用：`set_item_owner`（归属端点）与
    `classification_service.set_classification`（分类页/CSV 导入）共用同一字段语义，
    避免两处各写一份而漂移。提交由调用方负责（本函数不 commit）。

    `owner_kind` 恒 `member`：`family_pool` 随 ADR-0019 D6 退役（请求一律 422），
    且 `ck_item_owner_pair` 拒绝 `member` + `owner_id IS NULL` —— 因此**「清除归属」
    不写史行**（写 `family_pool`+NULL 会伪造「家庭共有」这一已退役语义），
    与 `set_item_owner` 保持一致（它也不允许清空）。
    """
    row = AssetItemOwnerChange(
        asset_item_id=item.id,
        owner_kind=MEMBER,
        owner_id=owner_id,
        effective_date=effective_date or date.today(),
        reason=(str(reason).strip() if reason else None),
        origin=origin,
        created_at=datetime.now().isoformat(),
    )
    db.add(row)
    return row


def set_item_owner(db: Session, item_id: int, owner_kind: str,
                   owner_id: int | None = None, reason: str | None = None,
                   origin: str = "manual", commit: bool = True,
                   effective_date: date | None = None,
                   audit_reason: str | None = None,
                   audit_reason_note: str | None = None
                   ) -> tuple[dict | None, dict | None]:
    """写归属（唯一写入口）。返回 (entry, change)；同值不追加时 change=None。

    **同一事务里两件事**：更新 `asset_items.owner_id`（真相源）+ 追加变更史行
    （审计）。只追加史而不改列会让列表/导出/筛选永远看不到这次变更。

    append-only 追加史：同日多次变更允许（更正＝追加）；仅迁移行受
    `uq_item_owner_migration` 部分唯一索引约束。

    **两套「原因」别混**：`reason` 是归属变更史的自由文本（既有语义，进
    `asset_item_owner_changes.reason`）；`audit_reason`/`audit_reason_note` 是审计行的
    封闭枚举与说明（ADR-0027 D7），可选、缺省 NULL、运行时绝不强制。
    """
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       {"item_id": item_id})
    if item.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                       "已归档资产项不可修改归属（R-37 终态）",
                       {"item_id": item_id})
    if origin not in ("manual", "migration"):
        raise _validation_error("origin", f"origin 非法：{origin!r}")
    _validate_owner(db, owner_kind, owner_id)

    before = current_owner(db, item_id)
    if _same(before, owner_id):
        return before, None                       # 幂等：同值不追加

    eff = effective_date or date.today()
    row = append_change(db, item, owner_id, reason=reason, origin=origin,
                        effective_date=eff)
    item.owner_id = owner_id                      # 真相源：与史同事务落库
    # draft → active 的唯一迁移点（ADR-0019 D9）；归属补齐四维时即转正。
    classification_service.sync_status(db, item, allow_incomplete=True)
    db.flush()
    entry = _row_dict(row, db.get(Member, owner_id).name)
    change = _change(
        "asset_item_owner", row.id, "change",
        before={k: before[k] for k in ("owner_kind", "owner_id", "owner_name")}
        if before else None,
        after={"owner_kind": entry["owner_kind"], "owner_id": entry["owner_id"],
               "owner_name": entry["owner_name"]},
        reason=audit_reason, reason_note=audit_reason_note,
    )
    audit_service.record(db, change)              # 同事务：失败阻断业务
    if commit:
        db.commit()
        db.refresh(row)
    return entry, change


def batch_set_owner(db: Session, item_ids, owner_kind: str,
                    owner_id: int | None, reason: str,
                    effective_date: date | None = None, *,
                    audit_reason: str | None = None,
                    audit_reason_note: str | None = None) -> dict:
    """批量归属（逐项独立事务：成功即提交，失败只回滚该项，不阻断其它项）。

    返回 `{updated, skipped, failed:[{id, code}]}`；同值=skipped（不追加审计）。
    参数非法（ids/owner/reason/effective_date）在循环前 422，全批拒绝。
    """
    ids: list[int] = []
    for item_id in item_ids or []:
        try:
            value = int(item_id)
        except (TypeError, ValueError):
            raise _validation_error("ids", "ids 含非法 id")
        if value not in ids:
            ids.append(value)
    if not ids or len(ids) > 200:
        raise _validation_error("ids", "ids 须为 1..200 个去重 id")
    text = (reason or "").strip()
    if not text:
        raise _validation_error("reason", "reason 不能为空")
    _validate_owner(db, owner_kind, owner_id)

    updated = skipped = 0
    failed: list[dict] = []
    for item_id in ids:
        try:
            _entry, change = set_item_owner(
                db, item_id, owner_kind, owner_id, reason=text,
                effective_date=effective_date, commit=True,
                audit_reason=audit_reason, audit_reason_note=audit_reason_note)
            if change is None:
                skipped += 1
            else:
                updated += 1
        except ApiError as exc:
            db.rollback()                    # 仅回滚当前项；已提交项保留
            failed.append({"id": item_id, "code": str(exc.code)})
    return {"updated": updated, "skipped": skipped, "failed": failed}


def has_owner_history(db: Session, item_id: int) -> bool:
    return db.execute(
        select(AssetItemOwnerChange.id)
        .where(AssetItemOwnerChange.asset_item_id == item_id).limit(1)
    ).first() is not None


def initial_owner_if_absent(db: Session, item_id: int, owner_id: int,
                            reason: str | None = None) -> dict | None:
    """新建项初始归属：仅当该项**当前无归属**时写入（导入/建记录不覆盖人工归属）。

    判据是 `asset_items.owner_id`（真相源），不是「有没有变更史」——历史上没有，
    也不代表现在没有归属。
    """
    item = db.get(AssetItem, item_id)
    if item is None or item.owner_id is not None:
        return None
    entry, _change_row = set_item_owner(
        db, item_id, MEMBER, owner_id, reason=reason, commit=False)
    return entry