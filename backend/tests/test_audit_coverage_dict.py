"""「口径桶·字典类」写端点留痕（ADR-0027 D5/D9）。

被测面＝契约表里 `entity='classification'` 的 18 个写端点：
`/api/classification/{asset-classes,channels,institutions,members,tags}` 的 POST/PUT/DELETE
（15 个）＋ `/api/asset-owners` 的 POST/PUT/DELETE（3 个，转接同一份 members 服务）。

锁的是**形状与边界**，不是「有没有调 record」：
1. create → 1 行 `classification/create`，`before=NULL`、`after`＝关键字段（含 code/name）；
2. update → 1 行 `classification/update`，`before`/`after` **只含真实改动键**（未改字段
   不出现）；同值 PUT＝没变，不留行；
3. delete → 1 行 `classification/delete`，`before`＝关键字段全集、`after=NULL`；
4. 删除被引用 → 409 `DICT_IN_USE`，审计**零新增**（什么都没变就不该有行）；
5. 校验失败（422）→ 审计零新增；
6. 上述写入之后 `audit_chain.verify()` 仍 ok，链头行数与审计行数相等。

沙箱：内存库 fixture（同 `test_audit.py`），不触真实 data/。路由函数直接调用（无需
TestClient）：它们只是同步函数，`db` 走依赖注入的同一个参数。
"""
import json

import pytest
from sqlalchemy import func, select

from errors import ApiError
from models import AssetItem, AssetItemTag, AuditLog, Member, Tag
from routers import asset_owners as owner_router
from routers import classification as cl_router
from schemas import (
    AssetClassCreate,
    AssetClassUpdate,
    MemberCreate,
    TagCreate,
    TagUpdate,
)
from services import audit_chain

ENTITY = "classification"


# ---------------------------------------------------------------- 助手

def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _rows(db, action: str | None = None) -> list[AuditLog]:
    q = select(AuditLog).where(AuditLog.entity == ENTITY)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return db.execute(q.order_by(AuditLog.id)).scalars().all()


def _payload(row: AuditLog, which: str):
    text = getattr(row, f"{which}_json")
    return None if text is None else json.loads(text)


# ---------------------------------------------------------------- 1. create

def test_create_asset_class_leaves_create_row_with_key_fields(db):
    """建字典项 → 1 行 create；`after` 是关键字段（含 code/name），`before` 为空。"""
    out = cl_router.create_asset_class(
        data=AssetClassCreate(name="示例-审计类别", code="Z9"), db=db)

    rows = _rows(db, "create")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "create" and row.actor == "owner"
    assert row.row_id == out["id"]                      # row_id＝该行主键
    assert row.before_json is None                      # 创建前没有「旧值」
    after = _payload(row, "after")
    assert after["code"] == "Z9" and after["name"] == "示例-审计类别"
    assert set(after) == {"code", "name", "parent_id", "side_scope"}


def test_create_tag_leaves_name_and_kind(db):
    """标签字典是平的（无 code 列）：`after` 收 name/kind，同类端点走同一份留痕。"""
    out = cl_router.create_tag(data=TagCreate(name="示例-标签"), db=db)

    row = _rows(db, "create")[0]
    assert row.row_id == out["id"]
    assert _payload(row, "after") == {"name": "示例-标签", "kind": "free"}


# ---------------------------------------------------------------- 2. update

def test_update_records_only_changed_keys(db):
    """改字典项 → before/after **只放改动键**：未改的 code/side_scope 不得出现。"""
    created = cl_router.create_asset_class(
        data=AssetClassCreate(name="示例-旧名", code="Z8", side_scope="any"), db=db)

    cl_router.update_asset_class(
        created["id"], AssetClassUpdate(name="示例-新名"), db=db)

    rows = _rows(db, "update")
    assert len(rows) == 1
    row = rows[0]
    assert row.row_id == created["id"]
    assert _payload(row, "before") == {"name": "示例-旧名"}
    assert _payload(row, "after") == {"name": "示例-新名"}
    for untouched in ("code", "parent_id", "side_scope"):
        assert untouched not in _payload(row, "after")
        assert untouched not in _payload(row, "before")


def test_update_ignores_unchanged_fields_sent_along(db):
    """PUT 里带了但值没变 → 不算变更键（kind 未进 before/after）。"""
    created = cl_router.create_tag(data=TagCreate(name="示例-改名前", kind="controlled"), db=db)

    cl_router.update_tag(
        created["id"], TagUpdate(name="示例-改名后", kind="controlled"), db=db)

    row = _rows(db, "update")[0]
    assert _payload(row, "after") == {"name": "示例-改名后"}
    assert "kind" not in _payload(row, "before")


def test_update_with_same_value_writes_no_row(db):
    """同值 PUT＝口径没变，不留行（审计回答「改成什么」，不是「谁点了保存」）。"""
    created = cl_router.create_tag(data=TagCreate(name="示例-同值"), db=db)
    before_n = _count(db)

    cl_router.update_tag(created["id"], TagUpdate(name="示例-同值"), db=db)

    assert _count(db) == before_n and _rows(db, "update") == []


# ---------------------------------------------------------------- 3. delete

def test_delete_leaves_before_without_after(db):
    created = cl_router.create_tag(data=TagCreate(name="示例-待删", kind="controlled"), db=db)

    cl_router.delete_tag(created["id"], db=db, reason="normalization")

    rows = _rows(db, "delete")
    assert len(rows) == 1
    row = rows[0]
    assert row.row_id == created["id"]
    assert row.after_json is None                       # 删完没有现值
    assert _payload(row, "before") == {"name": "示例-待删", "kind": "controlled"}
    assert db.get(Tag, created["id"]) is None           # 行确实没了


# ---------------------------------------------------------------- 4. 被引用 → 不留行

def test_delete_in_use_writes_no_audit(db):
    """字典项仍被引用 → 409 DICT_IN_USE；拒绝发生在任何写入之前，审计零新增。"""
    created = cl_router.create_tag(data=TagCreate(name="示例-在用一个"), db=db)
    item = AssetItem(display_name="示例-引用项", status="draft")   # draft：四维可空
    db.add(item)
    db.flush()
    db.add(AssetItemTag(item_id=item.id, tag_id=created["id"]))
    db.commit()
    before_n = _count(db)

    with pytest.raises(ApiError) as ei:
        cl_router.delete_tag(created["id"], db=db, reason="normalization")
    assert ei.value.status_code == 409 and ei.value.code == "DICT_IN_USE"
    db.rollback()

    assert _count(db) == before_n
    assert _rows(db, "delete") == []
    assert db.get(Tag, created["id"]) is not None        # 行还在


# ---------------------------------------------------------------- 5. 校验失败 → 不留行

def test_validation_failure_writes_no_audit(db):
    """校验失败（422）在写入之前抛出 → 没有可留痕的事实。"""
    before_n = _count(db)

    with pytest.raises(ApiError) as role_err:
        cl_router.create_member(data=MemberCreate(name="示例-成员", role="bogus"), db=db)
    assert role_err.value.status_code == 422
    db.rollback()

    created = cl_router.create_tag(data=TagCreate(name="示例-重名"), db=db)
    before_n = _count(db)                                 # 已含上面那次成功创建的 create 行
    with pytest.raises(ApiError) as dup_err:
        cl_router.create_tag(data=TagCreate(name="示例-重名"), db=db)
    assert dup_err.value.status_code == 409
    db.rollback()

    with pytest.raises(ApiError) as blank_err:
        cl_router.update_tag(created["id"], TagUpdate(name="   "), db=db)
    assert blank_err.value.status_code == 422
    db.rollback()

    assert _count(db) == before_n
    assert _rows(db, "update") == []


# ---------------------------------------------------------------- 6. asset-owners 转接同一台账

def test_asset_owners_endpoints_share_classification_ledger(db):
    """`/api/asset-owners` 的 POST/PUT/DELETE 转接 members 服务 → 同一 entity/action。"""
    created = owner_router.create_asset_owner(
        data={"name": "示例-归属人", "role": "self"}, db=db)
    owner_router.update_asset_owner(created["id"], {"name": "示例-归属人2"}, db=db)
    owner_router.delete_asset_owner(created["id"], db=db, reason="normalization")

    rows = _rows(db)
    assert [r.action for r in rows] == ["create", "update", "delete"]
    assert {r.row_id for r in rows} == {created["id"]}
    assert _payload(rows[0], "after") == {"name": "示例-归属人", "role": "self", "active": True}
    assert _payload(rows[1], "after") == {"name": "示例-归属人2"}
    assert _payload(rows[2], "before") == {
        "name": "示例-归属人2", "role": "self", "active": True}
    assert db.get(Member, created["id"]) is None


# ---------------------------------------------------------------- 7. 链仍可续

def test_chain_verifies_after_dict_writes(db):
    cl_router.create_tag(data=TagCreate(name="示例-链标签"), db=db)
    node = cl_router.create_asset_class(data=AssetClassCreate(name="示例-链类别"), db=db)
    cl_router.update_asset_class(node["id"], AssetClassUpdate(name="示例-链类别2"), db=db)
    cl_router.delete_asset_class(node["id"], db=db, reason="normalization")

    out = audit_chain.verify(db)
    assert out["ok"] is True and out["first_broken"] is None
    assert out["head"]["row_count"] == _count(db)