"""「口径桶·项级分类/标签」写端点留痕（ADR-0027 D3/D5/D9）。

被测面＝契约表里这 5 行（实现前是 planned，本文件锁的就是「实现之后必须成立」）：

| method | path | 头行 | 明细 |
|---|---|---|---|
| PUT  | `/api/asset-items/{item_id}/classification` | — | `classification/update` |
| PUT  | `/api/asset-items/{item_id}/tags`           | — | `classification/update` |
| POST | `/api/asset-items/classifications/batch`    | `batch/execute` | 逐项 `classification/update` |
| PUT  | `/api/assets/{asset_id}/tags`               | — | `classification/update` |
| POST | `/api/assets/tags/batch`                    | `batch/execute` | 逐项 `classification/update` |

锁的是**形状与边界**，不是「有没有调 record」：
1. 单条改判 → 1 行 `classification/update`，`before`/`after` **只含真实改动键**；
   `row_id` ＝ **资产项 id**（不是 assets 行 id —— 取证从资产项往回查）；
2. 同值改判＝口径未变 → **零行**（审计回答「改成什么」，不是「谁点了保存」）；
3. 批量 → 1 条头行（`after={op,count,ids_hash,scope}`）＋ N 条明细，
   头行 `count` ＝ **实际生效行数**（同值/失败的项不计）；
4. 404 / 409 / 422（本域无鉴权，取「校验失败」这一面）→ 零审计行；
5. `asset_item_attr_history` **照旧写**：两轨并存且同口径（维度名/值一一对上）；
6. 上述写入之后 `audit_chain.verify()` 仍 ok，链头行数＝审计行数。

沙箱：内存库 fixture（同 `test_audit.py`），不触真实 data/。路由函数直接调用
（无需 TestClient）：它们只是同步函数，`db` 走依赖注入的同一个参数。
"""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from errors import ApiError
from models import (AssetClass, AssetItem, AssetItemAttrHistory, AuditLog,
                    Institution, Member, Tag)
from routers import classification as cl_router
from schemas import (BatchClassificationInput, ItemClassificationUpdate,
                     ItemTagsUpdate)
from services import asset_service, audit_chain, classification_service

TODAY = date.today()
DETAIL = ("classification", "update")     # 单条与批量明细共用的 entity/action
HEAD = ("batch", "execute")               # 批次头行（D3）


# ---------------------------------------------------------------- 助手

def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _rows(db, entity: str | None = None, action: str | None = None) -> list[AuditLog]:
    q = select(AuditLog)
    if entity is not None:
        q = q.where(AuditLog.entity == entity)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return db.execute(q.order_by(AuditLog.id)).scalars().all()


def _detail_rows(db) -> list[AuditLog]:
    return _rows(db, *DETAIL)


def _head_rows(db) -> list[AuditLog]:
    return _rows(db, *HEAD)


def _payload(row: AuditLog, which: str):
    text = getattr(row, f"{which}_json")
    return None if text is None else json.loads(text)


def _hist(db, item_id: int, dim: str | None = None) -> list[AssetItemAttrHistory]:
    q = select(AssetItemAttrHistory).where(AssetItemAttrHistory.item_id == item_id)
    if dim is not None:
        q = q.where(AssetItemAttrHistory.dim == dim)
    return db.execute(q.order_by(AssetItemAttrHistory.id)).scalars().all()


@pytest.fixture()
def seed(db):
    """两个 draft 资产项 ＋ 字典项（名称一律合成，示例- 前缀）。"""
    tag_a, tag_b = Tag(name="示例-标签甲", kind="free"), Tag(name="示例-标签乙", kind="free")
    inst = Institution(name="示例-机构", kind="bank")
    member = Member(name="示例-成员", role="self")
    node = AssetClass(code="Z1", name="示例-类别", level=1, side_scope="any")
    db.add_all([tag_a, tag_b, inst, member, node])
    db.flush()
    first = AssetItem(display_name="示例-项一", status="draft")
    second = AssetItem(display_name="示例-项二", status="draft")
    db.add_all([first, second])
    db.commit()
    return {"item": first, "other": second, "tag_a": tag_a, "tag_b": tag_b,
            "inst": inst, "member": member, "class": node}


# ---------------------------------------------------------------- 0. 取值以契约表为准

def test_emitted_entity_action_matches_contract_table():
    """5 个端点的 entity/action **照契约表**（D9），不自创拼写。"""
    from services.audit_contract import rule_for

    expected = {
        ("PUT", "/api/asset-items/{item_id}/classification"): DETAIL,
        ("PUT", "/api/asset-items/{item_id}/tags"): DETAIL,
        ("POST", "/api/asset-items/classifications/batch"): HEAD,
        ("PUT", "/api/assets/{asset_id}/tags"): DETAIL,
        ("POST", "/api/assets/tags/batch"): HEAD,
    }
    for (method, path), (entity, action) in expected.items():
        rule = rule_for(method, path)
        assert rule is not None, f"{method} {path} 未登记进契约表"
        assert (rule.entity, rule.action) == (entity, action), (
            f"{method} {path} 契约行取值变了：{rule.entity}/{rule.action}")
    # 两个批量端点的头行形状来自同一个构造点
    head = asset_service.batch_head_change("update", 0, [])
    assert (head["entity"], head["action"]) == HEAD


# ---------------------------------------------------------------- 1. 单条改判

def test_item_classification_leaves_only_changed_keys(db, seed):
    """单条改分类 → 1 行 `classification/update`，`row_id`＝**资产项 id**。"""
    item_id = seed["item"].id

    cl_router.set_item_classification(
        item_id, ItemClassificationUpdate(liquidity_days=0), db=db)

    rows = _detail_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.row_id == item_id                    # 资产项 id，不是 assets 行 id
    assert row.actor == "owner"
    assert _payload(row, "before") == {"liquidity": None}
    assert _payload(row, "after") == {"liquidity": 0}
    # 未改的维度一个都不许出现（D5：只放真实改动键）
    for untouched in ("asset_class", "institution", "channel", "owner", "risk"):
        assert untouched not in _payload(row, "after")


def test_item_classification_records_every_changed_dimension(db, seed):
    """一次改多个维度 → 同一行覆盖全部改动键（不是每维一行）。"""
    cl_router.set_item_classification(item_id=seed["item"].id, data=ItemClassificationUpdate(
        asset_class_id=seed["class"].id, liquidity_days=7,
        institution_id=seed["inst"].id, owner_id=seed["member"].id,
        risk_level="R2"), db=db)

    rows = _detail_rows(db)
    assert len(rows) == 1
    after = _payload(rows[0], "after")
    assert set(after) == {"asset_class", "liquidity", "institution", "owner", "risk"}
    assert after["asset_class"] == seed["class"].id and after["liquidity"] == 7
    assert after["risk"] == "R2"


def test_item_tags_audit_row_id_is_asset_item_id(db, seed):
    """`PUT /api/asset-items/{item_id}/tags` → 快照只有 `tags`（名字口径）。"""
    item_id = seed["item"].id
    cl_router.set_item_tags(
        item_id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id, seed["tag_b"].id]), db=db)

    row = _detail_rows(db)[0]
    assert row.row_id == item_id
    assert _payload(row, "before") == {"tags": []}
    assert _payload(row, "after") == {"tags": ["示例-标签乙", "示例-标签甲"]}


def test_record_tags_endpoint_uses_item_id_not_asset_id(db):
    """`PUT /api/assets/{asset_id}/tags`：row_id 是**资产项 id**，不是记录 id。"""
    first = asset_service.create_asset(db, "示例-记录", Decimal("100"), TODAY)
    second = asset_service.create_asset(db, "示例-记录", Decimal("150"), TODAY - timedelta(days=1))
    assert second.asset_item_id == first.asset_item_id       # 同一项两条记录
    assert second.id != second.asset_item_id                 # 两个 id 空间确实不同
    tag = Tag(name="示例-记录标签", kind="free")
    db.add(tag)
    db.commit()

    asset_service.replace_asset_tags(db, second.id, [tag.id])

    row = _detail_rows(db)[0]
    assert row.row_id == first.asset_item_id
    assert row.row_id != second.id
    assert _payload(row, "after") == {"tags": ["示例-记录标签"]}


# ---------------------------------------------------------------- 2. 同值改判 → 零行

def test_same_value_writes_no_audit_row(db, seed):
    """同值改判＝口径没变：分类与标签都**不留行**，变更史同样不写。"""
    item_id = seed["item"].id
    cl_router.set_item_classification(
        item_id, ItemClassificationUpdate(liquidity_days=7), db=db)
    cl_router.set_item_tags(item_id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id]), db=db)
    baseline = _count(db)
    hist_baseline = len(_hist(db, item_id))

    cl_router.set_item_classification(
        item_id, ItemClassificationUpdate(liquidity_days=7), db=db)
    cl_router.set_item_tags(item_id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id]), db=db)

    assert _count(db) == baseline
    assert len(_hist(db, item_id)) == hist_baseline
    assert len(_detail_rows(db)) == 2               # 只有前两次真实改判


def test_tags_reordered_ids_is_same_value(db, seed):
    """标签整组替换：入参顺序不同但集合相同 → 不算改判（快照比对按名字排序）。"""
    item_id = seed["item"].id
    cl_router.set_item_tags(
        item_id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id, seed["tag_b"].id]), db=db)
    baseline = _count(db)

    cl_router.set_item_tags(
        item_id, ItemTagsUpdate(tag_ids=[seed["tag_b"].id, seed["tag_a"].id]), db=db)

    assert _count(db) == baseline


# ---------------------------------------------------------------- 3. 失败/无改动 → 零行

def test_missing_item_and_bad_tag_leave_no_audit(db, seed):
    """404（项/标签不存在）发生在任何写入之前 → 零审计行。"""
    baseline = _count(db)

    with pytest.raises(ApiError) as not_found:
        cl_router.set_item_classification(999999, ItemClassificationUpdate(liquidity_days=0),
                                          db=db)
    assert not_found.value.status_code == 404
    db.rollback()

    with pytest.raises(ApiError) as bad_tag:
        cl_router.set_item_tags(seed["item"].id, ItemTagsUpdate(tag_ids=[999999]), db=db)
    assert bad_tag.value.status_code == 404
    db.rollback()

    with pytest.raises(ApiError) as bad_class:
        cl_router.set_item_classification(
            seed["item"].id, ItemClassificationUpdate(asset_class_id=999999), db=db)
    assert bad_class.value.status_code == 404
    db.rollback()

    assert _count(db) == baseline
    assert _detail_rows(db) == []


def test_archived_item_tags_leave_no_audit(db, seed):
    """归档项不可改标签 → 409，审计零新增（什么都没变）。"""
    archived = AssetItem(display_name="示例-归档项", status="archived")
    db.add(archived)
    db.commit()
    baseline = _count(db)

    with pytest.raises(ApiError) as err:
        cl_router.set_item_tags(archived.id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id]), db=db)
    assert err.value.status_code == 409 and err.value.code == "ITEM_ARCHIVED"
    db.rollback()

    assert _count(db) == baseline
    assert _detail_rows(db) == []


def test_missing_asset_record_leaves_no_audit(db, seed):
    """`PUT /api/assets/{asset_id}/tags`：记录不存在 → 路由 404，写不出审计行。"""
    baseline = _count(db)
    assert asset_service.replace_asset_tags(db, 999999, [seed["tag_a"].id]) is None
    assert _count(db) == baseline


# ---------------------------------------------------------------- 4. 批量：头行 + 明细

def test_batch_classification_writes_head_plus_details(db, seed):
    """批量改分类：1 头行 + 逐项明细，头行 `count`＝**实际生效行数**。"""
    seed["other"].liquidity_days = 7          # 该项已是目标值 → 不产生明细
    db.commit()
    ids = [seed["item"].id, seed["other"].id]

    res = cl_router.batch_set_classification(
        BatchClassificationInput(item_ids=ids, liquidity_days=7), db=db)

    assert res == {"updated": 2, "failed": []}
    heads, details = _head_rows(db), _detail_rows(db)
    assert len(heads) == 1 and len(details) == 1          # 只有一项真改判
    assert details[0].row_id == ids[0]
    after = _payload(heads[0], "after")
    assert after["op"] == "update" and after["count"] == 1
    assert len(after["ids_hash"]) == 16
    assert after["scope"]["via"] == "asset-items/classifications/batch"
    assert after["scope"]["fields"] == ["liquidity_days"]
    assert after["scope"]["item_count"] == 2
    assert heads[0].row_id is None                        # 头行不对应单行


def test_batch_classification_counts_only_successful_items(db, seed):
    """失败项不进头行 count（也不留明细）：head.count ＝ 明细行数。"""
    res = cl_router.batch_set_classification(
        BatchClassificationInput(item_ids=[seed["item"].id, 999999], liquidity_days=0), db=db)

    assert res["updated"] == 1
    assert [f["code"] for f in res["failed"]] == ["NOT_FOUND"]
    heads, details = _head_rows(db), _detail_rows(db)
    assert len(heads) == 1 and len(details) == 1
    assert _payload(heads[0], "after")["count"] == len(details)


def test_batch_tags_writes_head_plus_details(db, seed):
    """批量打标签：同一项的多条记录只产生**一条**明细，头行 count＝项数。"""
    first = asset_service.create_asset(db, "示例-批标签", Decimal("100"), TODAY)
    second = asset_service.create_asset(db, "示例-批标签", Decimal("200"), TODAY - timedelta(days=1))
    assert first.asset_item_id == second.asset_item_id

    res = asset_service.batch_tag_records(
        db, [first.id, second.id], [seed["tag_a"].id], [])

    assert res == {"updated": 2, "skipped": 0, "failed": []}     # 计数仍按记录条数
    heads, details = _head_rows(db), _detail_rows(db)
    assert len(heads) == 1 and len(details) == 1                 # 明细按资产项
    assert details[0].row_id == first.asset_item_id
    after = _payload(heads[0], "after")
    assert after["op"] == "update" and after["count"] == 1
    assert after["scope"]["via"] == "assets/tags/batch"
    assert after["scope"]["record_count"] == 2
    assert after["scope"]["add_tag_ids"] == [seed["tag_a"].id]


def test_batch_tags_noop_writes_no_audit(db, seed):
    """批量打标签但全项同值（skipped）→ 零审计行（没有改动就没有批次）。"""
    first = asset_service.create_asset(db, "示例-批空转", Decimal("100"), TODAY)
    asset_service.replace_asset_tags(db, first.id, [seed["tag_a"].id])
    db.commit()
    baseline = _count(db)

    res = asset_service.batch_tag_records(db, [first.id], [seed["tag_a"].id], [])

    assert res["skipped"] == 1 and res["updated"] == 0
    assert _count(db) == baseline
    assert _head_rows(db) == []


def test_batch_tags_partial_failure_keeps_success_and_head(db, seed):
    """批量里一项失败（超 5 个上限）→ 该项零审计，成功项明细与头行照写。"""
    full = asset_service.create_asset(db, "示例-满标签", Decimal("100"), TODAY)
    extra_tags = [Tag(name=f"示例-填充{i}", kind="free") for i in range(5)]
    db.add_all(extra_tags)
    db.commit()
    asset_service.replace_asset_tags(db, full.id, [t.id for t in extra_tags])
    ok = asset_service.create_asset(db, "示例-可写", Decimal("50"), TODAY)
    db.commit()
    baseline_details = len(_detail_rows(db))       # 上面那次单条改判也留了明细

    res = asset_service.batch_tag_records(
        db, [full.id, ok.id], [seed["tag_a"].id], [])

    assert res["updated"] == 1
    assert [f["code"] for f in res["failed"]] == ["TAG_LIMIT_EXCEEDED"]
    heads, details = _head_rows(db), _detail_rows(db)
    assert len(heads) == 1 and len(details) == baseline_details + 1
    assert details[-1].row_id == ok.asset_item_id                 # 失败项没有明细
    assert _payload(heads[0], "after")["count"] == 1


# ---------------------------------------------------------------- 5. 两轨并存且一致

def test_attr_history_and_audit_log_coexist_and_agree(db, seed):
    """变更史服务业务读、审计是旁路取证：两者都写，且维度/值一一对上。"""
    item_id = seed["item"].id
    cl_router.set_item_classification(
        item_id, ItemClassificationUpdate(asset_class_id=seed["class"].id,
                                          liquidity_days=30), db=db)
    cl_router.set_item_tags(item_id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id]), db=db)

    hist = {h.dim: h for h in _hist(db, item_id)}
    assert set(hist) == {"asset_class", "liquidity", "tag"}

    detail = {r.row_id: r for r in _detail_rows(db)}
    assert set(detail) == {item_id}
    # 分类的两次改判合并成一行（同一事务内一次 flush）；标签是另一次调用 → 两行
    rows = _detail_rows(db)
    assert len(rows) == 2
    cls_row = rows[0]
    after = _payload(cls_row, "after")
    assert set(after) == {"asset_class", "liquidity"}
    assert str(after["asset_class"]) == hist["asset_class"].new_value
    assert str(after["liquidity"]) == hist["liquidity"].new_value

    tags_row = rows[1]
    assert _payload(tags_row, "after")["tags"] == hist["tag"].new_value.split(",")


def test_chain_verifies_after_item_writes(db, seed):
    """上述 5 个端点写完之后，哈希链仍可续、链头行数＝审计行数。"""
    cl_router.set_item_classification(
        seed["item"].id, ItemClassificationUpdate(liquidity_days=0), db=db)
    cl_router.set_item_tags(
        seed["item"].id, ItemTagsUpdate(tag_ids=[seed["tag_a"].id]), db=db)
    cl_router.batch_set_classification(
        BatchClassificationInput(item_ids=[seed["item"].id, seed["other"].id],
                                 risk_level="R3"), db=db)
    record = asset_service.create_asset(db, "示例-链记录", Decimal("10"), TODAY)
    asset_service.replace_asset_tags(db, record.id, [seed["tag_b"].id])
    asset_service.batch_tag_records(db, [record.id], [], [seed["tag_b"].id])

    out = audit_chain.verify(db)
    assert out["ok"] is True and out["first_broken"] is None
    assert out["head"]["row_count"] == _count(db)


def test_audit_rows_carry_no_reason(db, seed):
    """reason/reason_note 未传即 NULL（ADR-0027 D7：缺 reason 不报错，属登记债务）。"""
    cl_router.set_item_classification(
        seed["item"].id, ItemClassificationUpdate(liquidity_days=0), db=db)

    row = _detail_rows(db)[0]
    assert row.reason is None and row.reason_note is None


def test_batch_writes_are_atomic_per_item(db, seed, monkeypatch):
    """S2：批量里审计写失败 → 该次业务改动与其审计一起回滚（不留半截）。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(classification_service.audit_service, "record", _boom)
    with pytest.raises(RuntimeError):
        cl_router.batch_set_classification(
            BatchClassificationInput(item_ids=[seed["item"].id], liquidity_days=0), db=db)
    db.rollback()

    assert seed["item"].liquidity_days is None      # 列未落库
    assert _detail_rows(db) == [] and _head_rows(db) == []