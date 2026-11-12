"""资产分类服务（ADR-0019）——七维分类的唯一读写入口。

为什么要有这个模块：旧结构把「资产类型 / 流动性 / 账户 / 归属 / 标签」塞进一张
`attributes(type)` 通用表，再靠 `asset_attributes` 挂到**每一条记录**上，基数靠应用层
口头约定。结果是同维多值 75 处、15,892 条记录级打点里 229 对只覆盖个别记录。

新结构把基数交给 schema：四个单值维度是 `asset_items` 上的 NOT NULL 列，多值的标签走
`asset_item_tags`，字典拆成四张独立表。本模块是这些表的唯一读写面：

- 预算好错误再落库（叶子性、side 一致性），数据库触发器兜底
- 每次变更写 `asset_item_attr_history`（append-only），因为「这条记录当时怎么分类的」
  已经不可查，只能靠变更史
- 归属维度额外同事务追加 `asset_item_owner_changes`（ADR-0020 D3：列是真相源、史是
  纯审计），复用 `asset_owner_service.append_change`，保证与归属端点同一字段语义
- `draft` ↔ `active` 的迁移收敛在 `_sync_status`，任何入口都不会漏

调用方不应直接改 `AssetItem.asset_class_id` 等列——那会绕过变更史。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from errors import ApiError, ErrorCode
from models import (
    AssetClass,
    AssetItem,
    AssetItemAttrHistory,
    AssetItemOwnerChange,
    AssetItemTag,
    Asset,
    Channel,
    Institution,
    Member,
    Tag,
)
from services import audit_service
from services.audit_contract import rule_for
from services.audit_service import make_change as _change

# 流动性档位：days 是唯一权威，label 是派生的展示名（ADR-0019 D4：不建 lookup 表）。
LIQUIDITY_LEVELS: tuple[tuple[int, str], ...] = (
    (0, "T+0 随时"),
    (1, "1 天"),
    (7, "7 天"),
    (30, "30 天"),
    (90, "90 天"),
    (180, "180 天"),
    (365, "1 年"),
    (9999, "1 年以上/封闭"),
)
_LIQUIDITY_DAYS = frozenset(d for d, _ in LIQUIDITY_LEVELS)

RISK_LEVELS: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")
MAX_TAGS = 5
MEMBER_ROLES: tuple[str, ...] = ("self", "spouse", "joint", "child", "nominee")

# 必填四维（与 ck_asset_items_dims_required 完全一致，改一处必须改另一处）
REQUIRED_DIMS: tuple[str, ...] = (
    "asset_class_id", "liquidity_days", "institution_id", "owner_id",
)

_ASSET_CLASS_FIELDS = ("name", "parent_id", "code", "side_scope")

# 自动编号的探测上限：正常数据远达不到，只用来兜住病态数据（全表 code 塞满）时的死循环
_MAX_AUTO_PROBE = 1000


# ---------------------------------------------------------------- 审计留痕（ADR-0027）
#
# 字典表（资产类别树 ＋ 机构/渠道/成员/标签四张平表）是「口径」桶：改一行就改了一批
# 历史记录的归组与读数。留痕按 ADR-0027 D5 的快照分型走：create→after、update→**仅改动键**、
# delete→before。三条铁律（与 `allocation_target` 同款）：
# - **同一事务**：`audit_service.record()` 在 `db.commit()` 之前调，业务回滚审计不留、
#   审计写失败整事务失败（ADR-0007：可信 > 便利）；
# - **失败不留行**：校验（422）与「仍被引用」（409 `DICT_IN_USE`/`CLASS_IN_USE`）都在
#   任何写入之前抛出——什么都没变，就不该有审计行；
# - 实体/动作取值照抄契约表（`entity='classification'`，`action=create|update|delete`），
#   18 个端点（`/api/classification/*` 15 个 ＋ `/api/asset-owners` 3 个）共用本模块，
#   故留痕也只需落在这里一处。

# 字典项的「关键字段」＝确实改变口径的真实列。
# `AssetClass.level` 刻意不收：它由 `parent_id` 唯一推算（create/update 都按父级定档），
# 记两遍只会让审计读者以为层级能被单独改。主键走审计行 `row_id`，不重复进快照。
_AUDIT_FIELDS: dict[type, tuple[str, ...]] = {
    AssetClass: ("code", "name", "parent_id", "side_scope"),
    Institution: ("name", "kind", "active"),
    Channel: ("name", "active"),
    Member: ("name", "role", "active"),
    Tag: ("name", "kind"),
}


def _audit_snapshot(model, obj) -> dict:
    """字典项的关键字段全集（create 的 `after` / delete 的 `before` 用它）。"""
    return {field: getattr(obj, field) for field in _AUDIT_FIELDS[model]}


def _audit_create(db: Session, model, obj) -> None:
    """新建字典项：`before=None`、`after`＝关键字段（调用方已 flush 拿到主键）。"""
    audit_service.record(db, _change("classification", obj.id, "create", None,
                                     _audit_snapshot(model, obj)))


def _audit_update(db: Session, model, obj, before: dict) -> None:
    """改字典项：`before`/`after` **只放真实改动键**（D5）；同值写入＝没变，不留行。

    同值不写行的理由：审计行要回答「口径被改成什么」，不是「谁点了保存」。把一次
    无变化的提交记成变更，会让口径史看起来比事实更频繁（同 `set_item_owner`
    的幂等口径：同值不追加）。
    """
    after_all = _audit_snapshot(model, obj)
    changed = [f for f in _AUDIT_FIELDS[model] if before.get(f) != after_all.get(f)]
    if not changed:
        return
    audit_service.record(db, _change(
        "classification", obj.id, "update",
        {field: before[field] for field in changed},
        {field: after_all[field] for field in changed}))


def _audit_delete(db: Session, row_id: int, before: dict, *,
                  reason: str | None = None,
                  reason_note: str | None = None) -> None:
    """删字典项：`before`＝关键字段全集、`after=None`（行已不在，取不到现值）。

    **6 个字典删除端点的单点收口**（spec E3）：`action='delete'` 属纠正类，
    `actor='owner'` 缺 reason 由 `audit_service.record()` 422 拦下
    （`AUDIT_REASON_REQUIRED`）——守卫/引用检查都发生在更早的 `delete_*` 里，
    故 422 前不产生任何业务写入，本行也不落库（同事务回滚）。
    """
    audit_service.record(db, _change(
        "classification", row_id, "delete", before, None,
        reason=reason, reason_note=reason_note))


def _delete_dict_row(db: Session, model, obj, *,
                     reason: str | None = None,
                     reason_note: str | None = None) -> None:
    """删一行字典项＋留痕＋提交（`before` 必须在 flush 前取，删后实例不可读）。"""
    before = _audit_snapshot(model, obj)
    row_id = obj.id
    db.delete(obj)
    db.flush()
    _audit_delete(db, row_id, before, reason=reason,
                  reason_note=reason_note)      # 同事务：删行成功才留痕
    db.commit()


# ------------------------------------------------- 项级改判留痕（ADR-0027 口径桶）
#
# 分类/标签挂在**资产项**上：改一项即改它名下所有历史记录的归组与读数，故与字典项
# 同属「口径」桶、共用 `entity='classification'`。三处与字典项不同：
# - `row_id`＝**资产项 id**（取证从项往回查；assets 行 id 在这里没有意义）；
# - 快照分型是 D5 的 update：`before`/`after` **只放真实改判的维度**，同值改判＝口径
#   未变、**不留行**（矩阵见 `set_classification` 的 `changes` 列表）；
# - `asset_item_attr_history` **照旧写**：它服务业务读（`classification/history`
#   端点），审计是旁路取证——两个机制并存，不是二选一。
#
# entity/action **从契约表取**，不在这里另抄一份（D9：表是唯一真源，改表即改留痕）。
def _contract_rule(method: str, path: str):
    """取契约表里该端点的规则行；缺行/无 entity/action ＝表与代码脱节，当场炸。"""
    rule = rule_for(method, path)
    if rule is None or not rule.entity or not rule.action:
        raise RuntimeError(
            f"审计契约表缺行：{method} {path}（ADR-0027 D9/D12）——"
            "项级分类/标签留痕必须以契约表为准，不得自创 entity/action")
    return rule


_ITEM_CLASSIFY_RULE = _contract_rule("PUT", "/api/asset-items/{item_id}/classification")
_ITEM_TAGS_RULE = _contract_rule("PUT", "/api/asset-items/{item_id}/tags")
_BATCH_CLASSIFY_RULE = _contract_rule("POST", "/api/asset-items/classifications/batch")


def _item_change(rule, item_id: int, before: dict, after: dict, *,
                 reason: str | None = None,
                 reason_note: str | None = None) -> dict:
    """项级改判的审计 change：`row_id` 恒为资产项 id，快照只含改动键（D5）。

    `reason`/`reason_note` 是**审计行**的字段（ADR-0027 封闭枚举 + ≤200 字说明），
    由端点显式传入：本域的请求体 `reason` 是变更史的自由文本（进
    `asset_item_attr_history.reason`），同名不同义，故不复用那个字段。缺省＝NULL，
    运行时绝不因缺 reason 报错（ADR-0027 D7）。
    """
    return _change(rule.entity, item_id, rule.action, before=before, after=after,
                   reason=reason, reason_note=reason_note)


def _batch_head_change(rule, op: str, count: int, ids, scope: dict) -> dict:
    """批次头行（D3 形状：`batch/execute`，`after={op,count,ids_hash,scope}`）。

    `entity`/`action` 照契约表那行取（`batch`/`execute`）；`ids_hash` 与
    `asset_service.ids_fingerprint` 同口径（排序去重后 sha256 前 16），故延迟 import
    复用那一个实现——`asset_service` 顶层 import 本模块，反向顶层 import 会成环。
    """
    from services.asset_service import ids_fingerprint
    return _change(rule.entity, None, rule.action, after={
        "op": op,
        "count": count,
        "ids_hash": ids_fingerprint(ids),
        "scope": scope,
    })


# ---------------------------------------------------------------- 展示辅助

def liquidity_label(days: int | None) -> str | None:
    """档位 → 展示名。未知档位返回 None（CHECK 已挡住非法值，这里防御脏数据）。"""
    if days is None:
        return None
    for d, label in LIQUIDITY_LEVELS:
        if d == days:
            return label
    return None


def liquidity_levels() -> list[dict]:
    """流动性的完整枚举（只读，不提供增删改——档位是产品定义不是用户数据）。"""
    return [{"days": d, "label": label} for d, label in LIQUIDITY_LEVELS]


def _class_path(node: AssetClass, by_id: dict[int, AssetClass]) -> str:
    """从根到叶的 `code name` 路径，如 `A 现金与等价物/A1 活期存款`。"""
    parts: list[str] = []
    cur: AssetClass | None = node
    seen: set[int] = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        parts.append(f"{cur.code} {cur.name}" if cur.code else cur.name)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return "/".join(reversed(parts))


# ---------------------------------------------------------------- 资产类别树

def _child_counts(db: Session) -> dict[int, int]:
    """每个节点的子节点数（判断 leaf 的唯一依据）。"""
    return dict(
        db.execute(
            select(AssetClass.parent_id, func.count())
            .where(AssetClass.parent_id.is_not(None))
            .group_by(AssetClass.parent_id)
        ).all()
    )


def _class_dict(node: AssetClass, by_id: dict[int, AssetClass],
                child_count: dict[int, int]) -> dict:
    """类别节点的对外形状。

    列表端点与写路径（POST/PUT）共用这一个函数：前端拿到的列表项与写入返回值
    必须同形（含 `path`/`leaf`），否则前端要在两种类型之间来回切换。
    """
    return {
        "id": node.id,
        "code": node.code,
        "name": node.name,
        "parent_id": node.parent_id,
        "level": node.level,
        "side_scope": node.side_scope,
        "path": _class_path(node, by_id),
        "leaf": child_count.get(node.id, 0) == 0,
    }


def _class_snapshot(db: Session, node: AssetClass) -> dict:
    """写路径的单节点返回：与 `list_asset_classes` 的列表项同形。

    path 需要整棵树（祖先链），leaf 需要子节点数——两条查询，代价与列表端点同级。
    """
    by_id = {r.id: r for r in db.execute(select(AssetClass)).scalars().all()}
    return _class_dict(node, by_id, _child_counts(db))


def list_asset_classes(db: Session) -> list[dict]:
    rows = db.execute(select(AssetClass).order_by(AssetClass.level, AssetClass.code)).scalars().all()
    by_id = {r.id: r for r in rows}
    child_count = _child_counts(db)
    return [_class_dict(r, by_id, child_count) for r in rows]


def _require_class(db: Session, class_id: int) -> AssetClass:
    node = db.get(AssetClass, class_id)
    if node is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产类别 {class_id} 不存在")
    return node


def _reject_class_cycle(db: Session, node_id: int, parent_id: int | None) -> None:
    """把 node 挂到 parent 下会不会成环：沿 parent 向上找，遇到自己即环。"""
    cur = parent_id
    seen: set[int] = set()
    while cur is not None:
        if cur == node_id:
            raise ApiError(ErrorCode.CLASS_CYCLE, 400, "类别父子关系不能成环",
                           {"parent_id": parent_id})
        if cur in seen:
            return
        seen.add(cur)
        parent = db.get(AssetClass, cur)
        cur = parent.parent_id if parent else None


def create_asset_class(db: Session, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "类别名称不能为空")
    parent_id = data.get("parent_id")
    parent = _require_class(db, parent_id) if parent_id else None
    level = (parent.level + 1) if parent else 1
    code = (data.get("code") or "").strip().upper() or _auto_code(db, parent, name)
    side_scope = data.get("side_scope") or (parent.side_scope if parent else "any")
    node = AssetClass(code=code, name=name, parent_id=parent_id,
                      level=level, side_scope=side_scope)
    db.add(node)
    db.flush()
    _audit_create(db, AssetClass, node)          # 同事务留痕（ADR-0027 D5）
    db.commit()
    return _class_snapshot(db, node)


def _auto_code(db: Session, parent: AssetClass | None, name: str) -> str:
    """未给 code 时自动编号：根节点用字母，子节点用 `父code+序号`。

    子节点取**已有兄弟的最大序号 + 1**，不是「兄弟数 + 1」——删过子类别会留空档，
    按数量推会撞上已占用的编号（`UNIQUE(code)` → 500）。候选码再对全表做一次占用
    探测（code 是全局唯一，冲突不看层级），探测到上限仍无空位则 409 而不是 500。
    """
    used = _used_codes(db)
    limit = _MAX_AUTO_PROBE
    if parent is None:
        for idx in range(1, limit + 1):
            candidate = _alpha_label(idx)
            if candidate not in used:
                return candidate
        raise ApiError(
            ErrorCode.CLASS_CODE_EXHAUSTED, 409,
            f"根类别自动编号探测 {limit} 个候选（A..{_alpha_label(limit)}）仍无空位，请显式指定 code",
            {"parent_id": None, "level": 1, "probed": limit},
        )

    prefix = (parent.code or "").strip().upper()
    seq = 0
    for (code,) in db.execute(
        select(AssetClass.code).where(AssetClass.parent_id == parent.id)
    ).all():
        tail = (code or "").strip().upper()
        if prefix:
            if not tail.startswith(prefix):
                continue          # 兄弟里手写的、不符合父前缀的编号不参与推序号
            tail = tail[len(prefix):]
        if tail.isdigit():
            seq = max(seq, int(tail))
    for offset in range(1, limit + 1):
        candidate = f"{parent.code}{seq + offset}"
        if candidate not in used:
            return candidate
    raise ApiError(
        ErrorCode.CLASS_CODE_EXHAUSTED, 409,
        f"类别 {parent.code} 下的自动编号探测 {limit} 个候选仍无空位，请显式指定 code",
        {"parent_id": parent.id, "level": parent.level + 1, "probed": limit},
    )


def _used_codes(db: Session) -> set[str]:
    """全表已占用的 code（`UNIQUE(code)` 是全局约束，探测时不分层级）。"""
    return {(r or "").strip().upper() for (r,) in db.execute(select(AssetClass.code)).all()}


def _alpha_label(index: int) -> str:
    """1→A、26→Z、27→AA…（Excel 列名式）：26 个字母用尽后继续递增。"""
    label, rest = "", index
    while rest > 0:
        rest, rem = divmod(rest - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def update_asset_class(db: Session, class_id: int, data: dict) -> dict:
    node = _require_class(db, class_id)
    unknown = set(data) - set(_ASSET_CLASS_FIELDS)
    if unknown:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, f"不支持修改字段：{sorted(unknown)}")
    before = _audit_snapshot(AssetClass, node)        # 改之前的 key 字段（update 用）

    if "name" in data and data["name"] is not None:
        node.name = str(data["name"]).strip()
        if not node.name:
            raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "类别名称不能为空")
    if "code" in data and data["code"] is not None:
        node.code = str(data["code"]).strip().upper()
    if "side_scope" in data and data["side_scope"] is not None:
        _check_side_scope_change(db, node, data["side_scope"])
        node.side_scope = data["side_scope"]
    if "parent_id" in data:
        new_parent_id = data["parent_id"]
        if new_parent_id == node.id:
            raise ApiError(ErrorCode.CLASS_CYCLE, 400, "不能把类别挂到自己下面")
        _reject_class_cycle(db, node.id, new_parent_id)
        parent = _require_class(db, new_parent_id) if new_parent_id else None
        node.parent_id = new_parent_id
        node.level = (parent.level + 1) if parent else 1
    db.flush()
    _audit_update(db, AssetClass, node, before)   # 只记真实改动键（ADR-0027 D5）
    db.commit()
    return _class_snapshot(db, node)


def _check_side_scope_change(db: Session, node: AssetClass, new_scope: str) -> None:
    """改 side_scope 前确认已有引用不会因此失配。"""
    sides = {
        s for (s,) in db.execute(
            select(Asset.side).where(Asset.asset_item_id.in_(
                select(AssetItem.id).where(AssetItem.asset_class_id == node.id)
            )).distinct()
        ).all()
    }
    bad = sides - {"any", new_scope}
    if bad:
        raise ApiError(
            ErrorCode.CLASS_SIDE_CONFLICT, 409,
            "该类别下已有相反侧别的资产记录，不能改成这个 side_scope",
            {"conflicting_sides": sorted(bad)},
        )


def delete_asset_class(db: Session, class_id: int, *,
                       reason: str | None = None,
                       reason_note: str | None = None) -> None:
    node = _require_class(db, class_id)
    children = db.execute(
        select(func.count()).select_from(AssetClass).where(AssetClass.parent_id == class_id)
    ).scalar_one()
    if children:
        raise ApiError(ErrorCode.CLASS_HAS_CHILDREN, 409,
                       f"该类别还有 {children} 个子类别，不能删除",
                       {"children": children})
    used = db.execute(
        select(func.count()).select_from(AssetItem).where(AssetItem.asset_class_id == class_id)
    ).scalar_one()
    if used:
        raise ApiError(ErrorCode.CLASS_IN_USE, 409,
                       f"该类别仍被 {used} 个资产项使用，不能删除",
                       {"items": used, "field": "asset_class_id"})
    _delete_dict_row(db, AssetClass, node, reason=reason,
                     reason_note=reason_note)


# ---------------------------------------------------------------- 三张平表

def _list_simple(db: Session, model) -> list[dict]:
    rows = db.execute(select(model).order_by(model.name)).scalars().all()
    if model is Member:
        return [{"id": r.id, "name": r.name, "role": r.role, "active": r.active} for r in rows]
    if model is Tag:
        return [{"id": r.id, "name": r.name, "kind": r.kind} for r in rows]
    if model is Channel:
        # 渠道没有 kind 列（机构才分 bank/broker/...），单独一支，否则会 AttributeError
        return [{"id": r.id, "name": r.name, "active": r.active} for r in rows]
    return [{"id": r.id, "name": r.name, "kind": r.kind, "active": r.active} for r in rows]


def _simple_out(obj) -> dict:
    """字典对象的统一出参。kind/role/active 只在模型真有该列时才带。"""
    out = {"id": obj.id, "name": obj.name}
    for field in ("kind", "role", "active"):
        if hasattr(obj, field):
            out[field] = getattr(obj, field)
    return out


def _create_simple(db: Session, model, data: dict, **extra) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "名称不能为空")
    exists = db.execute(select(model).where(model.name == name)).scalar_one_or_none()
    if exists is not None:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 409, f"「{name}」已存在")
    obj = model(name=name, **extra)
    db.add(obj)
    db.flush()
    _audit_create(db, model, obj)                # 同事务留痕（ADR-0027 D5）
    db.commit()
    return _simple_out(obj)


def _require_simple(db: Session, model, obj_id: int, label: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"{label} {obj_id} 不存在")
    return obj


def _rename_simple(db: Session, model, obj_id: int, data: dict, label: str) -> dict:
    obj = _require_simple(db, model, obj_id, label)
    before = _audit_snapshot(model, obj)          # 改之前的 key 字段（update 用）
    for field in ("name", "kind", "role", "active"):
        if field in data and data[field] is not None and hasattr(obj, field):
            value = data[field]
            if field == "role" and value not in MEMBER_ROLES:
                raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                               "role 必须是 self/spouse/joint/child/nominee")
            if field == "name":
                value = str(value).strip()
                if not value:
                    raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "名称不能为空")
                dup = db.execute(
                    select(model).where(model.name == value, model.id != obj_id)
                ).scalar_one_or_none()
                if dup is not None:
                    raise ApiError(ErrorCode.VALIDATION_ERROR, 409, f"「{value}」已存在")
            setattr(obj, field, value)
    db.flush()
    _audit_update(db, model, obj, before)         # 只记真实改动键（ADR-0027 D5）
    db.commit()
    return _simple_out(obj)


def _in_use_count(db: Session, column, obj_id: int, extra_checks: Sequence[tuple[Any, Any]] = ()) -> int:
    total = db.execute(select(func.count()).select_from(AssetItem).where(column == obj_id)).scalar_one()
    for model_or_col, col in extra_checks:
        total += db.execute(select(func.count()).select_from(model_or_col).where(col == obj_id)).scalar_one()
    return total


# 机构 ------------------------------------------------------------------

def list_institutions(db: Session) -> list[dict]:
    return _list_simple(db, Institution)


def create_institution(db: Session, data: dict) -> dict:
    kind = data.get("kind") or "other"
    return _create_simple(db, Institution, data, kind=kind)


def update_institution(db: Session, institution_id: int, data: dict) -> dict:
    return _rename_simple(db, Institution, institution_id, data, "机构")


def delete_institution(db: Session, institution_id: int, *,
                       reason: str | None = None,
                       reason_note: str | None = None) -> None:
    obj = _require_simple(db, Institution, institution_id, "机构")
    used = _in_use_count(db, AssetItem.institution_id, institution_id)
    if used:
        raise ApiError(ErrorCode.DICT_IN_USE, 409, f"该机构仍被 {used} 个资产项使用，不能删除",
                       {"items": used, "field": "institution_id"})
    _delete_dict_row(db, Institution, obj, reason=reason,
                     reason_note=reason_note)


# 渠道 ------------------------------------------------------------------

def list_channels(db: Session) -> list[dict]:
    return _list_simple(db, Channel)


def create_channel(db: Session, data: dict) -> dict:
    return _create_simple(db, Channel, data)


def update_channel(db: Session, channel_id: int, data: dict) -> dict:
    return _rename_simple(db, Channel, channel_id, data, "渠道")


def delete_channel(db: Session, channel_id: int, *,
                   reason: str | None = None,
                   reason_note: str | None = None) -> None:
    obj = _require_simple(db, Channel, channel_id, "渠道")
    used = _in_use_count(db, AssetItem.channel_id, channel_id)
    if used:
        raise ApiError(ErrorCode.DICT_IN_USE, 409, f"该渠道仍被 {used} 个资产项使用，不能删除",
                       {"items": used, "field": "channel_id"})
    _delete_dict_row(db, Channel, obj, reason=reason,
                     reason_note=reason_note)


# 成员 ------------------------------------------------------------------

def list_members(db: Session) -> list[dict]:
    return _list_simple(db, Member)


def create_member(db: Session, data: dict) -> dict:
    role = data.get("role")
    if role not in MEMBER_ROLES:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                       "role 必须是 self/spouse/joint/child/nominee")
    return _create_simple(db, Member, data, role=role)


def update_member(db: Session, member_id: int, data: dict) -> dict:
    return _rename_simple(db, Member, member_id, data, "成员")


def delete_member(db: Session, member_id: int, *,
                  reason: str | None = None,
                  reason_note: str | None = None) -> None:
    obj = _require_simple(db, Member, member_id, "成员")
    used = _in_use_count(
        db, AssetItem.owner_id, member_id,
        extra_checks=[(AssetItemOwnerChange, AssetItemOwnerChange.owner_id)],
    )
    if used:
        raise ApiError(ErrorCode.DICT_IN_USE, 409, f"该成员仍被 {used} 处引用，不能删除",
                       {"items": used, "field": "owner_id"})
    _delete_dict_row(db, Member, obj, reason=reason,
                     reason_note=reason_note)


# 标签 ------------------------------------------------------------------

def list_tags(db: Session) -> list[dict]:
    return _list_simple(db, Tag)


def create_tag(db: Session, data: dict) -> dict:
    kind = data.get("kind") or "free"
    if kind not in ("controlled", "free"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "kind 必须是 controlled/free")
    return _create_simple(db, Tag, data, kind=kind)


def update_tag(db: Session, tag_id: int, data: dict) -> dict:
    return _rename_simple(db, Tag, tag_id, data, "标签")


def delete_tag(db: Session, tag_id: int, *,
               reason: str | None = None,
               reason_note: str | None = None) -> None:
    obj = _require_simple(db, Tag, tag_id, "标签")
    used = db.execute(
        select(func.count()).select_from(AssetItemTag).where(AssetItemTag.tag_id == tag_id)
    ).scalar_one()
    if used:
        raise ApiError(ErrorCode.DICT_IN_USE, 409, f"该标签仍被 {used} 个资产项使用，不能删除",
                       {"items": used, "field": "tag_id"})
    _delete_dict_row(db, Tag, obj, reason=reason,
                     reason_note=reason_note)


# ---------------------------------------------------------------- 项级分类

def _require_item(db: Session, item_id: int) -> AssetItem:
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项 {item_id} 不存在")
    return item


def _item_sides(db: Session, item_id: int) -> set[str]:
    return {
        s for (s,) in db.execute(
            select(Asset.side).where(Asset.asset_item_id == item_id).distinct()
        ).all()
    }


def _check_leaf(db: Session, class_id: int) -> AssetClass:
    node = _require_class(db, class_id)
    children = db.execute(
        select(func.count()).select_from(AssetClass).where(AssetClass.parent_id == class_id)
    ).scalar_one()
    if children:
        raise ApiError(ErrorCode.CLASS_NOT_LEAF, 422,
                       "资产类别必须落到叶子节点，该节点还有子类别",
                       {"class_id": class_id, "children": children})
    return node


def _check_side(db: Session, item_id: int, node: AssetClass) -> None:
    bad = {s for s in _item_sides(db, item_id) if node.side_scope not in ("any", s)}
    if bad:
        raise ApiError(
            ErrorCode.CLASS_SIDE_CONFLICT, 422,
            "资产类别与该项已有记录的侧别不符",
            {"class_id": node.id, "side_scope": node.side_scope,
             "conflicting_sides": sorted(bad)},
        )


def _record_count(db: Session, item_id: int) -> int:
    """该项的**有效**记录行数（`superseded_by IS NULL`）。

    这就是「改分类的影响面」：项级分类一次改，覆盖它名下所有历史记录的口径。
    """
    return db.execute(
        select(func.count()).select_from(Asset).where(
            Asset.asset_item_id == item_id, Asset.superseded_by.is_(None))
    ).scalar_one()


def record_counts_for_items(db: Session, item_ids: Sequence[int]) -> dict[int, int]:
    """批量「有效记录行数」：一条 GROUP BY 覆盖整页，逐项 COUNT 就是 N+1。"""
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(Asset.asset_item_id, func.count())
        .where(Asset.asset_item_id.in_(ids), Asset.superseded_by.is_(None))
        .group_by(Asset.asset_item_id)
    ).all()
    return {item_id: count for item_id, count in rows}


def classification_of(db: Session, item: AssetItem, *,
                      record_count: int | None = None) -> dict:
    """单项的分类视图。延迟加载的 relationship 在 session 内直接取即可。

    `record_count` 由批量调用方预取传入（避免逐项 COUNT）；单项端点不传则现查。
    """
    dims = missing_dims(item)
    return {
        "item_id": item.id,
        "display_name": item.display_name,
        "status": item.status,
        "asset_class_id": item.asset_class_id,
        "asset_class_name": item.asset_class.name if item.asset_class else None,
        "asset_class_code": item.asset_class.code if item.asset_class else None,
        "liquidity_days": item.liquidity_days,
        "liquidity_label": liquidity_label(item.liquidity_days),
        "institution_id": item.institution_id,
        "institution_name": item.institution.name if item.institution else None,
        "channel_id": item.channel_id,
        "owner_id": item.owner_id,
        "owner_name": item.owner.name if item.owner else None,
        "risk_level": item.risk_level,
        "tags": [{"id": t.id, "name": t.name} for t in item.tags],
        # 缺哪些必填维度（值＝列名，如 `owner_id`）。`missing_dims` 是本端点的既有
        # 规范名；`classification_missing` 是与 /api/assets、/api/assets/current
        # 对齐的**兼容别名**（同值），过渡期两者并存，前端可任取其一。
        "missing_dims": dims,
        "classification_missing": dims,
        # 改分类的影响面：名下有效记录行数（不含被更正取代的历史行）
        "record_count": _record_count(db, item.id) if record_count is None else record_count,
    }


def missing_dims(item: AssetItem) -> list[str]:
    """还差哪些必填维度（draft 项补齐这些就能转 active）。"""
    return [d for d in REQUIRED_DIMS if getattr(item, d) is None]


def classifications_for_items(db: Session, item_ids: Sequence[int]) -> dict[int, dict]:
    """批量读，避免列表页 N+1。"""
    if not item_ids:
        return {}
    # selectinload 三个单值关系，否则 classification_of 逐项懒加载 = N+1
    items = db.execute(
        select(AssetItem)
        .where(AssetItem.id.in_(item_ids))
        .options(
            selectinload(AssetItem.asset_class),
            selectinload(AssetItem.institution),
            selectinload(AssetItem.owner),
        )
    ).scalars().all()
    # 记录行数同样一次 GROUP BY 取齐（每次额外 COUNT 就又变回 N+1）
    counts = record_counts_for_items(db, [item.id for item in items])
    return {
        item.id: classification_of(db, item, record_count=counts.get(item.id, 0))
        for item in items
    }


def _record_history(db: Session, item_id: int, dim: str, old: Any, new: Any,
                    reason: str | None, actor: str) -> None:
    db.add(AssetItemAttrHistory(
        item_id=item_id, dim=dim,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
        changed_at=datetime.now(), changed_by=actor, reason=reason,
    ))


def set_classification(db: Session, item_id: int, data: dict, *,
                       reason: str | None = None, actor: str = "owner",
                       commit: bool = True,
                       changed_out: list[str] | None = None,
                       audit_reason: str | None = None,
                       audit_reason_note: str | None = None) -> dict:
    """改分类。只写传进来的字段；每处真实变更留一行变更史 **＋ 一行审计**。

    留痕双轨（ADR-0027 口径桶）：
    - `asset_item_attr_history`：业务读面（`classification/history` 端点），逐维度
      一行、append-only，**语义与形状不变**；
    - `audit_log`：`classification/update` 一行，`row_id`＝资产项 id，`before`/`after`
      只含真实改判的维度（同值改判＝口径没变 → 零行）。两者同一次 flush 提交。

    Args:
        commit: 批量入口传 False —— 由调用方在一个事务里收口提交，好让批次头行与
            逐项明细同进同退（失败项的 SAVEPOINT 回滚带走它自己的审计行）。
        changed_out: 收集本次真实改判的维度名；批量入口用它数「实际生效行数」
            （同值改判不算生效：既不写审计明细，也不进头行 count）。
        audit_reason / audit_reason_note: **审计行**的封闭枚举与说明（ADR-0027 D7），
            可选、缺省 NULL。与 `reason`（变更史自由文本）不是同一个字段。
    """
    item = _require_item(db, item_id)

    new_class_id = data.get("asset_class_id", item.asset_class_id)
    new_liquidity = data.get("liquidity_days", item.liquidity_days)
    new_institution = data.get("institution_id", item.institution_id)
    new_channel = data.get("channel_id", item.channel_id)
    old_owner = item.owner_id
    new_owner = data.get("owner_id", item.owner_id)
    new_risk = data.get("risk_level", item.risk_level)

    if new_class_id is not None and new_class_id != item.asset_class_id:
        node = _check_leaf(db, new_class_id)
        _check_side(db, item_id, node)
    if new_liquidity is not None and new_liquidity not in _LIQUIDITY_DAYS:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                       f"流动性档位必须是 {sorted(_LIQUIDITY_DAYS)} 之一")
    if new_institution is not None:
        _require_simple(db, Institution, new_institution, "机构")
    if new_channel is not None:
        _require_simple(db, Channel, new_channel, "渠道")
    if new_owner is not None:
        _require_simple(db, Member, new_owner, "成员")
    if new_risk is not None and new_risk not in RISK_LEVELS:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, f"风险等级必须是 {list(RISK_LEVELS)} 之一")

    changes = [
        ("asset_class", item.asset_class_id, new_class_id),
        ("liquidity", item.liquidity_days, new_liquidity),
        ("institution", item.institution_id, new_institution),
        ("channel", item.channel_id, new_channel),
        ("owner", item.owner_id, new_owner),
        ("risk", item.risk_level, new_risk),
    ]

    item.asset_class_id = new_class_id
    item.liquidity_days = new_liquidity
    item.institution_id = new_institution
    item.channel_id = new_channel
    item.owner_id = new_owner
    item.risk_level = new_risk

    changed_dims = [(dim, old, new) for dim, old, new in changes if old != new]
    for dim, old, new in changed_dims:
        _record_history(db, item_id, dim, old, new, reason, actor)

    # 审计旁路（同事务）：只记改判的维度。`status` 不进快照——它是四维齐全与否的
    # 派生结果（`_sync_status` 算出来的），记进「口径」快照会让人以为状态能单独改。
    if changed_dims:
        audit_service.record(db, _item_change(
            _ITEM_CLASSIFY_RULE, item_id,
            {dim: old for dim, old, new in changed_dims},
            {dim: new for dim, old, new in changed_dims},
            reason=audit_reason, reason_note=audit_reason_note))
    if changed_out is not None:
        changed_out.extend(dim for dim, _, _ in changed_dims)

    if new_owner is not None and new_owner != old_owner:
        # 归属列（真相源，ADR-0020 D3）与归属史必须同事务成对写入：只写列会留下
        # 「列有值、史无行」的断链——`current_owner` 只在最新史行的 owner_id 与列
        # 一致时才回填生效日/原因。延迟 import：asset_owner_service 顶层 import 本
        # 模块，顶层互指会成环。清除归属（new_owner is None）不写史行，与
        # `set_item_owner` 一致（family_pool 已退役，无第三种语义可写）。
        from services import asset_owner_service
        asset_owner_service.append_change(db, item, new_owner, reason=reason,
                                          origin="manual")

    _sync_status(db, item, allow_incomplete=False)
    db.flush()
    if commit:
        db.commit()
    else:
        # 未提交：手工过期，避免关系缓存让返回值（以及同一 session 后续读）看到旧分类
        db.expire(item)
    return classification_of(db, item)


def sync_status(db: Session, item: AssetItem, *, allow_incomplete: bool = False) -> None:
    """`_sync_status` 的公开入口：分类之外的写路径（如归属）也走同一状态机。"""
    _sync_status(db, item, allow_incomplete=allow_incomplete)


def _sync_status(db: Session, item: AssetItem, *, allow_incomplete: bool) -> None:
    """draft ↔ active 的唯一迁移点。

    - 补齐四维且当前是 draft → active
    - 四维不全：allow_incomplete 时退回 draft（如 unarchive），否则拒绝
    """
    complete = not missing_dims(item)
    if complete:
        if item.status == "draft":
            item.status = "active"
        return
    if item.status == "draft":
        return
    if allow_incomplete:
        item.status = "draft"
        return
    raise ApiError(ErrorCode.DIMS_INCOMPLETE, 422,
                   "四个必填维度（类别/流动性/机构/归属）不能为空",
                   {"missing": missing_dims(item)})


def batch_set_classification(db: Session, item_ids: Sequence[int], data: dict, *,
                             reason: str | None = None, actor: str = "owner") -> dict:
    """按项批量改分类。逐项独立成败——一项失败不该回滚其它项。

    留痕（ADR-0027 D3）：1 条批次头行（`batch/execute`，
    `after={op:'update', count, ids_hash, scope}`）＋ 逐项 `classification/update`
    明细（`row_id`＝资产项 id，只含真实改判的维度）。

    - 头行 `count` ＝ **实际生效的明细行数**（同值改判的项不算生效：既不写明细，
      也不计数）；
    - 整批**一次提交**：逐项用 SAVEPOINT 隔离失败（失败项回滚带走它自己的明细行），
      头行与成功项的明细同一事务落库——不会出现「有明细没头行」或「有头行没明细」。

    `ids_hash` 取**实际生效的资产项 id**（不是入参 id 清单）：同一次操作重算可复现，
    失败/未变的项不该混进指纹。
    """
    updated, failed = 0, []
    changed_items: list[int] = []
    for item_id in item_ids:
        changed: list[str] = []
        nested = db.begin_nested()               # SAVEPOINT：逐项独立成败
        try:
            set_classification(db, item_id, data, reason=reason, actor=actor,
                              commit=False, changed_out=changed)
        except ApiError as exc:
            nested.rollback()                    # 连带回滚该项自己的审计行
            failed.append({"item_id": item_id, "code": exc.code, "message": exc.message})
            continue
        nested.commit()
        updated += 1
        if changed:
            changed_items.append(int(item_id))

    if changed_items:
        audit_service.record(db, _batch_head_change(
            _BATCH_CLASSIFY_RULE, "update", len(changed_items), changed_items,
            scope={"via": "asset-items/classifications/batch",
                   "fields": sorted(data),
                   "item_count": len(item_ids)}))
    db.commit()
    return {"updated": updated, "failed": failed}


def set_tags(db: Session, item_id: int, tag_ids: Sequence[int], *,
             reason: str | None = None, actor: str = "owner",
             commit: bool = True) -> dict:
    """整组替换该项标签；真实变化写一行变更史 ＋ 一行审计（同值＝两条都不写）。

    审计行：`classification/update`，`row_id`＝资产项 id，快照只有 `tags` 一个键
    （`before`/`after` 各是排序后的标签名列表——与变更史 `dim='tag'` 的
    `old_value`/`new_value` 同一口径，两轨可直接对账）。标签 id 不落快照：名字是
    取证要读的东西，id 变更本身另有 `classification/tags` 字典行负责留痕。

    Args:
        commit: 批量入口传 False（由 `asset_service.batch_tag_records` 收口提交）。
    """
    item = _require_item(db, item_id)
    if item.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409, "资产项已归档，不能再改标签")

    unique_ids = list(dict.fromkeys(int(t) for t in tag_ids))
    if len(unique_ids) > MAX_TAGS:
        raise ApiError(ErrorCode.TAG_LIMIT_EXCEEDED, 422, f"单个资产项最多 {MAX_TAGS} 个标签")
    if unique_ids:
        found = db.execute(select(Tag).where(Tag.id.in_(unique_ids))).scalars().all()
        if len(found) != len(unique_ids):
            missing = sorted(set(unique_ids) - {t.id for t in found})
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"标签不存在：{missing}")

    old_names = sorted(t.name for t in item.tags)
    new_names = sorted(t.name for t in found) if unique_ids else []

    db.execute(delete(AssetItemTag).where(AssetItemTag.item_id == item_id))
    for tid in unique_ids:
        db.add(AssetItemTag(item_id=item_id, tag_id=tid))
    if old_names != new_names:
        _record_history(db, item_id, "tag", ",".join(old_names), ",".join(new_names),
                        reason, actor)
        audit_service.record(db, _item_change(
            _ITEM_TAGS_RULE, item_id, {"tags": old_names}, {"tags": new_names}))
    db.flush()
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.expire(item)                          # 未提交：过期后重读，别返回旧标签
    return classification_of(db, item)


def classification_history(db: Session, item_id: int, limit: int = 200) -> list[dict]:
    _require_item(db, item_id)
    rows = db.execute(
        select(AssetItemAttrHistory)
        .where(AssetItemAttrHistory.item_id == item_id)
        .order_by(AssetItemAttrHistory.changed_at.desc(), AssetItemAttrHistory.id.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {"id": r.id, "dim": r.dim, "old_value": r.old_value, "new_value": r.new_value,
         "changed_at": r.changed_at, "changed_by": r.changed_by, "reason": r.reason}
        for r in rows
    ]