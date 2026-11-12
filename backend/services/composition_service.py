"""构成关系服务（父子「由哪些项构成」，ADR-0029 D3/D4；票 02）。

三条不变量，全部在这一处落地（任何出口不得另写一套判定）：

1. **一子一父**：同一子项只能挂一个父；撞 `uq_item_compositions_child` → 409
   `COMPOSITION_PARENT_CONFLICT`（details 带 `existing_parent_item_id`）。换父必须
   先解除旧关系，不做隐式接管（票 02 D2）。
2. **禁环**：允许多层（子也可作父），但祖先链上不得出现待挂的子项 → 409
   `COMPOSITION_CYCLE`。DB 只兜自环（`CHECK(parent_item_id <> child_item_id)`），
   环检测只能在应用层——`CHECK` 表达不了传递闭包，而环会把计量谓词变成不动点计算
   （D4）。
3. **父=子** → 400 `COMPOSITION_SELF_PARENT`（在应用层先拦；DB 的 CHECK 是兜底）。

**计量口径不在这里**：父项「退出计量」由 `snapshot_identity.active_item_predicate`
里的一条 `NOT EXISTS` 派生（ADR-0029 D1/D2）——本服务只维护关系，不改任何读数。
解除关系后父项**自动恢复**计量，不留例外标记：不需要「清理」步骤，也就不会有
「忘清一次就永久漏计」的静默错账（D1 否决布尔位的理由）。

单事务：每次写操作只 `commit` 一次，审计行与业务行同事务（`audit_service.record`
在 commit 前 add/flush，回滚即不留孤儿审计）。
"""

from datetime import datetime

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import AssetItem, ItemComposition
from services import audit_service
from services.audit_service import make_change as _change

ENTITY = "item_composition"
NOTE_MAX = 200
# 祖先链上限：正常树远小于此；超过即视为数据已坏（防损坏数据把请求拖成死循环）
_MAX_DEPTH = 64


def _item_or_404(db: Session, item_id: int, *, role: str) -> AssetItem:
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404,
                       f"资产项不存在: {item_id}",
                       {"item_id": item_id, "role": role})
    return item


def _edge_of_child(db: Session, child_item_id: int) -> ItemComposition | None:
    """该子项当前挂的构成边（一子一父 ⇒ 至多一条）。"""
    return db.execute(
        select(ItemComposition)
        .where(ItemComposition.child_item_id == child_item_id)
    ).scalar_one_or_none()


def ancestors(db: Session, item_id: int) -> list[dict]:
    """沿父链向上走，返回 `[直接父, 祖父, ...]`（不含自身）。

    同时用作环检测（`create_edge` 看待挂子项是否已在待挂父项的祖先链里）。数据若
    已被外部改坏成环，用 `visited` 集合截断而不是死循环。
    """
    chain: list[dict] = []
    visited = {item_id}
    current = item_id
    while len(chain) < _MAX_DEPTH:
        edge = _edge_of_child(db, current)
        if edge is None:
            return chain
        parent = db.get(AssetItem, edge.parent_item_id)
        chain.append({
            "edge_id": edge.id,
            "item_id": edge.parent_item_id,
            "display_name": parent.display_name if parent else None,
            "status": parent.status if parent else None,
            "note": edge.note,
            "created_at": edge.created_at,
        })
        current = edge.parent_item_id
        if current in visited:      # 存量脏数据成环：截断，不抛（读取路径不阻断）
            return chain
        visited.add(current)
    return chain


def list_children(db: Session, parent_item_id: int) -> list[dict]:
    """该父项的直接子项（一层）；按 id 稳定排序，供树形展示与核对面板使用。"""
    rows = db.execute(
        select(ItemComposition, AssetItem)
        .join(AssetItem, AssetItem.id == ItemComposition.child_item_id)
        .where(ItemComposition.parent_item_id == parent_item_id)
        .order_by(ItemComposition.id)
    ).all()
    return [{
        "edge_id": edge.id,
        "child_item_id": edge.child_item_id,
        "display_name": item.display_name,
        "status": item.status,
        "note": edge.note,
        "created_at": edge.created_at,
    } for edge, item in rows]


def is_aggregate(db: Session, item_id: int) -> bool:
    """该项是否**汇总项**（＝在构成关系里作为父项出现）。

    与 `snapshot_identity.active_item_predicate` 同一定义（派生、不存布尔位）：
    这个 Python 便利函数只供展示层打「汇总项 · 不计入合计」标记，**不得**用于
    任何求和/派生的口径判定（口径唯一实现是 SQL 谓词）。
    """
    return db.execute(
        select(ItemComposition.id)
        .where(ItemComposition.parent_item_id == item_id)
        .limit(1)
    ).first() is not None


def create_edge(db: Session, parent_item_id: int, child_item_id: int,
                note: str | None = None, *, commit: bool = True) -> ItemComposition:
    """建立一条构成边（父 ← 子）。**单事务**：插边 ＋ 审计一起提交。

    `commit=False` 供**多边同一事务**的调用方（batch_service.finalize 一次写多条边，
    并与核对记录、批次状态同一个事务）；此时本函数只 `flush()`＋`record()` 审计，
    提交与回滚由调用方负责。默认 True＝既有行为（单边一次提交），旧调用方零改动。

    Raises:
        ApiError: 父/子不存在（404 `NOT_FOUND`）；父=子（400
            `COMPOSITION_SELF_PARENT`）；祖先链成环（409 `COMPOSITION_CYCLE`）；
            子项已有父（409 `COMPOSITION_PARENT_CONFLICT`）。
    """
    if parent_item_id == child_item_id:
        raise ApiError(ErrorCode.COMPOSITION_SELF_PARENT, 400,
                       "父项与子项不能是同一个资产项",
                       {"item_id": child_item_id})

    parent = _item_or_404(db, parent_item_id, role="parent")
    child = _item_or_404(db, child_item_id, role="child")

    if note is not None and len(note) > NOTE_MAX:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                       f"构成备注超长（≤{NOTE_MAX} 字）",
                       {"fields": [{"field": "note",
                                    "message": f"最多 {NOTE_MAX} 字"}]})

    # 一子一父：先查后插（并发兜底见下方 IntegrityError 分支）
    existing = _edge_of_child(db, child_item_id)
    if existing is not None:
        raise ApiError(ErrorCode.COMPOSITION_PARENT_CONFLICT, 409,
                       f"该子项已归属其它汇总项: {existing.parent_item_id}",
                       {"child_item_id": child_item_id,
                        "existing_parent_item_id": existing.parent_item_id})

    # 禁环：待挂子项若已是待挂父项的祖先，再连一条就成环（A→B→A）
    for hop in ancestors(db, parent_item_id):
        if hop["item_id"] == child_item_id:
            raise ApiError(ErrorCode.COMPOSITION_CYCLE, 409,
                           "该构成会形成环（子项已是父项的祖先）",
                           {"parent_item_id": parent_item_id,
                            "child_item_id": child_item_id,
                            "cycle": [child_item_id, parent_item_id]})

    edge = ItemComposition(parent_item_id=parent_item_id,
                           child_item_id=child_item_id,
                           note=note,
                           created_at=datetime.now())
    db.add(edge)
    try:
        db.flush()
    except IntegrityError as e:        # 另一写者抢先插入同一子项（UNIQUE 兜底）
        db.rollback()
        raise ApiError(ErrorCode.COMPOSITION_PARENT_CONFLICT, 409,
                       "该子项已归属其它汇总项（并发写入冲突）",
                       {"child_item_id": child_item_id}) from e

    audit_service.record(db, _change(
        ENTITY, edge.id, "create",
        before=None,
        after={"parent_item_id": parent_item_id,
               "child_item_id": child_item_id,
               "parent_display_name": parent.display_name,
               "child_display_name": child.display_name,
               "note": note}))
    if commit:
        db.commit()
    return edge


def remove_edge(db: Session, child_item_id: int) -> ItemComposition:
    """解除该子项与父项的关系（父项随即**自动恢复**计量，ADR-0029 D4）。

    Raises:
        ApiError: 该子项没有构成边（404 `NOT_FOUND`）。
    """
    edge = _edge_of_child(db, child_item_id)
    if edge is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404,
                       f"该资产项没有构成关系: {child_item_id}",
                       {"child_item_id": child_item_id})

    snapshot = {"parent_item_id": edge.parent_item_id,
                "child_item_id": edge.child_item_id,
                "note": edge.note}
    edge_id = edge.id
    db.execute(sql_delete(ItemComposition).where(ItemComposition.id == edge_id))

    audit_service.record(db, _change(
        ENTITY, edge_id, "remove", before=snapshot, after=None))
    db.commit()
    return edge