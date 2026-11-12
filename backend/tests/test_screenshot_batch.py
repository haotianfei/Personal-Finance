"""批次与跨图核对（ADR-0029 D5/D6/D9/D10；票 04）。

这组用例钉死 finalize 的**校验顺序与出口**：

1. 批次壳：`open` 起步、`expected_members` 校验、未知来源组 400；
2. 成员登记：`open → partial` 单向推进，同图重复登记原地刷新（幂等）；
3. 成员不齐 → 409 `BATCH_NOT_COMPLETE`（details 带已知/失败成员与缺失条数）；
4. 差额为 0 → `matched`，并**建立**构成边（持久父子关系）；
5. 差额≠0 无理由 → 409 `RECONCILE_MISMATCH`（details 含父额、子项明细、差额）；
6. 差额≠0 带理由 → `accepted_mismatch` 落库，审计含理由与接受计数；
7. 重复 finalize → 409 `BATCH_ALREADY_FINALIZED`；同 `Idempotency-Key` 重放 →
   首次响应原样回放、构成边**不重复建**；
8. 汇总行解析不到既有资产项 → 409 `RECONCILE_PARENT_NOT_FOUND`（**不新建**资产项）。

数据一律合成（「示例-」、虚构金额），不含真实机构/账号/金额。
"""
import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  —— 在 Base.metadata 上注册全部表
from database import Base, get_db
from errors import ApiError, ErrorCode
from main import register_exception_handlers
from models import (
    AssetItem, AuditLog, ItemComposition, ScreenshotBatch, ScreenshotBatchMember,
    ScreenshotEvidence, ScreenshotReconciliation,
    ScreenshotReconciliationChild, SourceGroup, SourceGroupLearnedRule,
)
from services import batch_service, composition_service, screenshot_service
from services.idempotency_service import IdempotencyCaptureMiddleware

D = date(2026, 4, 30)


# ---------------------------------------------------------------- 构造
def mk_item(db, name, status="draft") -> AssetItem:
    """资产项；默认 draft（四维全空）——本组只关心核对与构成。"""
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.flush()
    db.commit()
    return item


def mk_group(db, slug="demo-batch-group", label="示例批次来源组") -> SourceGroup:
    sg = SourceGroup(slug=slug, label=label, asset_prefix="示例-")
    db.add(sg)
    db.flush()
    db.commit()
    return sg


def mk_evidence(db, source_group_id, tag) -> ScreenshotEvidence:
    """一张图一行凭证；sha 由 tag 派生（不同 tag → 不同 sha，避开 (sha, group) 唯一索引）。"""
    ev = ScreenshotEvidence(
        source_group_id=source_group_id,
        file_path=f"2026/04/{tag}.png",
        file_sha256=hashlib.sha256(tag.encode()).hexdigest(),
        created_at=datetime.now(),
    )
    db.add(ev)
    db.flush()
    db.commit()
    return ev


def _raise(fn, *args, **kwargs) -> ApiError:
    with pytest.raises(ApiError) as excinfo:
        fn(*args, **kwargs)
    return excinfo.value


def recon_entry(parent, parent_row_index, children, aggregate_amount,
                *, name=None):
    """一条核对载荷——**冻结契约形状**（编排拍板，前端已按它对接）。

    children: [(item, evidence_id, row_index, amount)]；`item` 可为 None（子项
    `item_id` 允许为 null，靠 `name` 解析或记为 unresolved）。
    """
    children_payload = []
    for item, ev_id, row_index, amount in children:
        child = {"evidence_id": ev_id, "row_index": row_index,
                 "item_id": item.id if item is not None else None,
                 "amount": str(amount)}
        if item is None:
            child["name"] = None            # 解析不到 → 只留痕，不算失败
        children_payload.append(child)
    entry = {
        "parent_item_id": parent["item"].id,
        "parent_evidence_id": parent["evidence_id"],
        "parent_row_index": parent_row_index,
        "aggregate_amount": str(aggregate_amount),
        "children": children_payload,
    }
    if name is not None:
        entry["name"] = name
    return entry


@pytest.fixture()
def scene(db):
    """父 100 ＋ 子 60 ＋ 子 40 ＋ 三张图 ＋ 一个批次壳（3 张图）。"""
    sg = mk_group(db)
    parent = {"item": mk_item(db, "示例-贵金属汇总"),
              "evidence_id": mk_evidence(db, sg.id, "a1").id}
    child_a = {"item": mk_item(db, "示例-金产品A"),
               "evidence_id": mk_evidence(db, sg.id, "b2").id}
    child_b = {"item": mk_item(db, "示例-金产品B"),
               "evidence_id": mk_evidence(db, sg.id, "c3").id}
    batch = batch_service.create_batch(db, data_date=D, source_group=sg.slug,
                                       expected_members=3, note="示例批次")
    for holder in (parent, child_a, child_b):
        batch_service.register_member(db, batch.id, holder["evidence_id"])
    return {"group": sg, "batch": batch, "parent": parent,
            "child_a": child_a, "child_b": child_b}


# ---------------------------------------------------------------- 批次壳
def test_create_batch_returns_open_shell(db):
    sg = mk_group(db)
    batch = batch_service.create_batch(db, data_date=D, source_group=sg.slug,
                                       expected_members=2, note=" 两张图 ")

    assert batch.status == "open"
    assert batch.expected_members == 2
    assert batch.source_group_id == sg.id
    assert batch.data_date == D
    assert batch.finalized_at is None
    assert batch.note == "两张图"          # 首尾空白剥掉
    # 建壳不产生成员、不产生核对记录（零副作用）
    assert db.execute(select(func.count()).select_from(ScreenshotBatchMember)
                      .where(ScreenshotBatchMember.batch_id == batch.id)).scalar() == 0
    assert db.execute(select(func.count())
                      .select_from(ScreenshotReconciliation)).scalar() == 0


def test_create_batch_rejects_bad_expected_members(db):
    for bad in (0, -1, "3", 1.5, True):
        err = _raise(batch_service.create_batch, db, expected_members=bad)
        assert err.code == ErrorCode.VALIDATION_ERROR and err.status_code == 400
        assert err.details["fields"][0]["field"] == "expected_members"
    # 未声明是合法形态（NULL＝未声明，不是 0 张）
    assert batch_service.create_batch(db).expected_members is None


def test_create_batch_rejects_unknown_source_group(db):
    err = _raise(batch_service.create_batch, db, source_group="不存在的来源组")
    assert err.code == ErrorCode.SOURCE_GROUP_UNKNOWN and err.status_code == 400


# ---------------------------------------------------------------- 成员登记
def test_register_member_promotes_open_to_partial_and_is_idempotent(db):
    sg = mk_group(db)
    ev = mk_evidence(db, sg.id, "d4")
    batch = batch_service.create_batch(db, expected_members=2)
    assert batch.status == "open"

    member = batch_service.register_member(db, batch.id, ev.id)
    assert (member.status, member.evidence_id) == ("applied", ev.id)
    assert db.get(ScreenshotBatch, batch.id).status == "partial"   # 单向：open → partial

    # 同图重复登记（重试路径）：原地刷新，不新增行；状态不回退
    again = batch_service.register_member(db, batch.id, ev.id)
    assert again.id == member.id
    rows = db.execute(select(ScreenshotBatchMember)
                      .where(ScreenshotBatchMember.batch_id == batch.id)
                      .order_by(ScreenshotBatchMember.id)).scalars().all()
    assert len(rows) == 1
    assert db.get(ScreenshotBatch, batch.id).status == "partial"


# ---------------------------------------------------------------- finalize 校验
def test_finalize_requires_complete_members(db, scene):
    sg = mk_group(db, slug="demo-group-2", label="示例来源组二")
    batch = batch_service.create_batch(db, source_group=sg.slug, expected_members=3)
    batch_service.register_member(db, batch.id, mk_evidence(db, sg.id, "e5").id)

    err = _raise(batch_service.finalize, db, batch.id, [])
    assert err.code == ErrorCode.BATCH_NOT_COMPLETE and err.status_code == 409
    # details 冻结：只认 missing_evidence_ids（从未上传的图没有成员行，故此处为空）
    assert err.details == {"missing_evidence_ids": []}
    assert db.get(ScreenshotBatch, batch.id).status == "partial"   # 未被误标定案


def test_finalize_rejects_unresolvable_parent_without_creating_items(db, scene):
    parent = scene["parent"]
    entry = recon_entry(parent, 3, [], Decimal("100"), name="示例-不存在的汇总项")
    entry["parent_item_id"] = 999_001                  # 解析不到既有资产项
    before_items = db.execute(select(func.count()).select_from(AssetItem)).scalar()

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry])
    assert err.code == ErrorCode.RECONCILE_PARENT_NOT_FOUND
    assert err.status_code == 409
    # details 冻结：{parent_row_index, name}（前端按这两个键定位那一行）
    assert err.details == {"parent_row_index": 3, "name": "示例-不存在的汇总项"}
    # **不在 finalize 期间新建资产项**（零副作用）
    assert db.execute(select(func.count()).select_from(AssetItem)).scalar() == before_items


def test_finalize_resolves_parent_by_name_when_id_missing(db, scene):
    """前端只给名称/只给 null id 也能核对：名称走 `resolve_item_id` 单点解析。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry["parent_item_id"] = None                      # 只靠 name 解析
    entry["name"] = parent["item"].display_name

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["reconciliations"][0]["parent_item_id"] == parent["item"].id
    assert db.execute(select(ScreenshotReconciliation)).scalar_one().parent_item_id \
        == parent["item"].id


def test_finalize_tolerates_unresolvable_child(db, scene):
    """子项 `item_id` 解析不到 **不算失败**（冻结契约第 2 条）：只进 unresolved 回执。

    金额照常进合计（核对的是金额），但**不建构成边**（没有项可挂）。
    """
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (None, child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry["children"][1]["item_id"] = 999_002          # 解析不到的 id

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["status"] == "finalized"
    assert out["reconciliations"][0]["status"] == "matched"   # 差额 0
    assert [(u["row_index"], u["amount"]) for u in out["unresolved_children"]] \
        == [(4, "40.00")]
    # 只有解析到的那一条建了边
    edges = db.execute(select(ItemComposition)).scalars().all()
    assert [(e.parent_item_id, e.child_item_id) for e in edges] == [
        (parent["item"].id, child_a["item"].id)]
    # 未解析的子项仍留一行明细（金额可追溯）
    kids = db.execute(select(ScreenshotReconciliationChild)
                      .order_by(ScreenshotReconciliationChild.amount.desc())
                      ).scalars().all()
    assert [(k.child_item_id, k.amount) for k in kids] == [
        (child_a["item"].id, Decimal("60.00")), (None, Decimal("40.00"))]


def test_finalize_rejects_mismatch_without_reason(db, scene):
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("39.99")),
    ], Decimal("100.00"))

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry])
    assert err.code == ErrorCode.RECONCILE_MISMATCH and err.status_code == 409
    # details 冻结：reconciliations[{parent_item_id, aggregate_amount,
    #                               children_sum, diff}] + accepted(False)
    assert err.details == {
        "reconciliations": [{
            "parent_item_id": parent["item"].id,
            "aggregate_amount": "100.00",
            "children_sum": "99.99",
            "diff": "0.01",
        }],
        "accepted": False,
    }
    # 被拒 → 零写入（没有核对记录、没有构成边、批次仍未定案）
    assert db.execute(select(func.count())
                      .select_from(ScreenshotReconciliation)).scalar() == 0
    assert db.execute(select(func.count())
                      .select_from(ItemComposition)).scalar() == 0
    assert db.get(ScreenshotBatch, scene["batch"].id).status == "partial"


def test_finalize_rejects_blank_accept_reason(db, scene):
    parent, child_a = scene["parent"], scene["child_a"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("99.99")),
    ], Decimal("100.00"))

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry],
                 accept_mismatch={"reason": "   "})
    assert err.code == ErrorCode.RECONCILE_REASON_REQUIRED and err.status_code == 400
    assert err.details == {}          # details 冻结为空对象


# ---------------------------------------------------------------- finalize 成功
def test_finalize_with_zero_diff_matches_and_builds_compositions(db, scene):
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["status"] == "finalized"
    assert out["reconciliation_count"] == 1
    assert out["mismatch_accepted"] == 0
    assert out["reconciliations"][0]["status"] == "matched"
    assert out["reconciliations"][0]["diff"] == "0.00"

    batch = db.get(ScreenshotBatch, scene["batch"].id)
    assert batch.status == "finalized" and batch.finalized_at is not None

    rec = db.execute(select(ScreenshotReconciliation)).scalar_one()
    assert (rec.aggregate_amount, rec.child_amount_sum, rec.diff) == (
        Decimal("100.00"), Decimal("100.00"), Decimal("0.00"))
    assert rec.status == "matched" and rec.accepted_reason is None
    kids = db.execute(select(ScreenshotReconciliationChild)
                      .order_by(ScreenshotReconciliationChild.child_row_index)
                      ).scalars().all()
    assert [(k.child_item_id, k.child_row_index, k.amount) for k in kids] == [
        (child_a["item"].id, 3, Decimal("60.00")),
        (child_b["item"].id, 4, Decimal("40.00")),
    ]

    # 构成边已建立（持久父子关系），父项随即退出计量口径
    edges = db.execute(select(ItemComposition)
                       .order_by(ItemComposition.child_item_id)).scalars().all()
    assert [(e.parent_item_id, e.child_item_id) for e in edges] == [
        (parent["item"].id, child_a["item"].id),
        (parent["item"].id, child_b["item"].id),
    ]
    assert composition_service.is_aggregate(db, parent["item"].id) is True

    # 审计：建壳一条 create、定案一条 finalize；finalize 的 after 含成员与核对计数
    logs = db.execute(select(AuditLog)
                      .where(AuditLog.entity == "screenshot_batch")
                      .order_by(AuditLog.id)).scalars().all()
    assert [log.action for log in logs] == ["create", "finalize"]
    assert logs[-1].row_id == scene["batch"].id
    assert '"reconciliation_count": 1' in logs[-1].after_json
    # 父项来自预览行（有凭证 id）→ 审计记 preview_row
    assert '"preview_row"' in logs[-1].after_json


def test_finalize_accepts_mismatch_with_reason(db, scene):
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("39.99")),
    ], Decimal("100.00"))

    out = batch_service.finalize(db, scene["batch"].id, [entry],
                                 accept_mismatch={"reason": "明细少记 0.01，已核对"})

    assert out["mismatch_accepted"] == 1
    rec = db.execute(select(ScreenshotReconciliation)).scalar_one()
    assert rec.status == "accepted_mismatch"
    assert rec.diff == Decimal("0.01")
    assert rec.accepted_reason == "明细少记 0.01，已核对"
    assert rec.accepted_at is not None

    log = db.execute(select(AuditLog)
                     .where(AuditLog.entity == "screenshot_batch",
                            AuditLog.action == "finalize")).scalar_one()
    assert '"mismatch_accepted": 1' in log.after_json
    assert "明细少记 0.01，已核对" in log.after_json


def test_finalize_twice_is_rejected(db, scene):
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    batch_service.finalize(db, scene["batch"].id, [entry])

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry])
    assert err.code == ErrorCode.BATCH_ALREADY_FINALIZED and err.status_code == 409
    # 只有一份核对记录、两条边（重复调用没有追加任何东西）
    assert db.execute(select(func.count())
                      .select_from(ScreenshotReconciliation)).scalar() == 1
    assert db.execute(select(func.count())
                      .select_from(ItemComposition)).scalar() == 2


def test_finalize_is_rejected_on_abandoned_batch(db, scene):
    batch = db.get(ScreenshotBatch, scene["batch"].id)
    batch.status = "abandoned"
    db.commit()

    err = _raise(batch_service.finalize, db, scene["batch"].id, [])
    assert err.code == ErrorCode.BATCH_ABANDONED and err.status_code == 409


# ---------------------------------------------------------------- Idempotency-Key
@pytest.fixture()
def client(tmp_path):
    """最小应用：tmp 文件库（create_all ＋ 0010 DDL）＋ 幂等中间件 ＋ 批次路由。"""
    from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

    db_path = tmp_path / "batch_idem.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py").upgrade(conn)
    conn.execute("COMMIT")
    conn.close()

    Session = sessionmaker(bind=engine)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db

    from routers import screenshot_batches as batches_router
    app.include_router(batches_router.router, prefix="/api/screenshot-batches")

    test_client = TestClient(app)
    test_client.session_factory = Session       # 供用例造数
    return test_client


def test_finalize_idempotency_key_replays_without_duplicate_edges(client):
    db = client.session_factory()
    try:
        sg = mk_group(db, slug="demo-idem-group", label="示例幂等来源组")
        parent = mk_item(db, "示例-贵金属汇总")
        child = mk_item(db, "示例-金产品A")
        ev_ids = [mk_evidence(db, sg.id, f"f{i}").id for i in (6, 7)]
        batch = batch_service.create_batch(db, data_date=D, source_group=sg.slug,
                                           expected_members=2)
        for ev_id in ev_ids:
            batch_service.register_member(db, batch.id, ev_id)
        payload = {"reconciliations": [{
            "parent_evidence_id": ev_ids[0], "parent_row_index": 0,
            "parent_item_id": parent.id, "aggregate_amount": "100.00",
            "children": [{"evidence_id": ev_ids[1], "row_index": 2,
                          "item_id": child.id, "amount": "100.00"}],
        }]}
        headers = {"Idempotency-Key": "batch-finalize-1"}

        first = client.post(f"/api/screenshot-batches/{batch.id}/finalize",
                            json=payload, headers=headers)
        second = client.post(f"/api/screenshot-batches/{batch.id}/finalize",
                             json=payload, headers=headers)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.text == first.text           # 逐字节回放首次响应
        # 重放不重复建边、不重复记核对
        assert db.execute(select(func.count())
                          .select_from(ItemComposition)).scalar() == 1
        assert db.execute(select(func.count())
                          .select_from(ScreenshotReconciliation)).scalar() == 1
        assert db.execute(select(func.count())
                          .select_from(AuditLog)
                          .where(AuditLog.entity == "screenshot_batch",
                                 AuditLog.action == "finalize")).scalar() == 1
    finally:
        db.close()


def test_create_endpoint_returns_201_with_batch_id(client):
    resp = client.post("/api/screenshot-batches",
                       json={"data_date": "2026-04-30", "expected_members": 2,
                             "note": "示例"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "open" and isinstance(body["batch_id"], int)


def test_create_endpoint_rejects_bad_data_date(client):
    resp = client.post("/api/screenshot-batches", json={"data_date": "2026/04/30"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR

# ---------------------------------------------------------------- apply 侧接线
def test_apply_with_batch_id_registers_member_and_promotes_status(db, scene):
    """apply 载荷带 `batch_id` → 采纳成功后登记成员（票 04 D2）。"""
    from services.screenshot_service import PreviewRow, ScreenshotPreview

    group = scene["group"]
    ev = mk_evidence(db, group.id, "h8")
    batch = batch_service.create_batch(db, data_date=D, source_group=group.slug,
                                       expected_members=1)
    assert batch.status == "open"

    # keep 行：不改任何账本，只走凭证富化 + 成员登记
    preview = ScreenshotPreview(rows=[PreviewRow(
        source_name="", source_amount=Decimal("0"), action="keep",
        asset_name="示例-某产品", current_amount=Decimal("1"),
        diff=Decimal("0"), index=0)])
    screenshot_service.apply_preview(
        db, preview, source_group=group.slug, data_date=D,
        evidence_id=ev.id, batch_id=batch.id)

    members = db.execute(select(ScreenshotBatchMember)
                         .where(ScreenshotBatchMember.batch_id == batch.id)
                         ).scalars().all()
    assert [(m.evidence_id, m.status) for m in members] == [(ev.id, "applied")]
    assert db.get(ScreenshotBatch, batch.id).status == "partial"


def test_apply_on_finalized_batch_is_rejected_before_writing(db, scene):
    """批次已定案 → 409，且**先于**任何写入（凭证字段都不该被富化）。"""
    from services.screenshot_service import PreviewRow, ScreenshotPreview

    group = scene["group"]
    ev = mk_evidence(db, group.id, "i9")
    batch = batch_service.create_batch(db, source_group=group.slug)
    batch.status = "finalized"
    db.commit()

    preview = ScreenshotPreview(rows=[PreviewRow(
        source_name="", source_amount=Decimal("0"), action="keep",
        asset_name="示例-某产品", current_amount=Decimal("1"),
        diff=Decimal("0"), index=0)])
    err = _raise(screenshot_service.apply_preview, db, preview,
                 source_group=group.slug, data_date=D,
                 evidence_id=ev.id, batch_id=batch.id)
    assert err.code == ErrorCode.BATCH_ALREADY_FINALIZED and err.status_code == 409
    assert db.get(ScreenshotEvidence, ev.id).data_date is None   # 零写入：未富化


# ---------------------------------------------------------------- 契约细节（编排冻结）
def test_finalize_tolerates_missing_evidence_and_row_index(db, scene):
    """缺 `parent_evidence_id` / `parent_row_index` 只记日志不做强校验（契约第 2 条）。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry.pop("parent_evidence_id")          # 前端可能只给 item_id
    entry.pop("parent_row_index")

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["status"] == "finalized"
    rec = db.execute(select(ScreenshotReconciliation)).scalar_one()
    assert rec.parent_evidence_id is None    # 留空（不是伪造一个 id）
    assert rec.parent_row_index is None      # 也留空（不占位 0，0 会指向第 1 行）
    assert out["reconciliations"][0]["parent_item_id"] == parent["item"].id
    # 没有凭证 id ⇒ 判为「手选既有资产项」，审计可区分
    assert out["reconciliations"][0]["parent_source"] == "manual"


def test_finalize_resolves_child_by_name(db, scene):
    """子项 `item_id` 为 null 但带名称 → 按名称解析成功（建边、不进 unresolved）。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry["children"][0]["item_id"] = None
    entry["children"][0]["name"] = child_a["item"].display_name

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["unresolved_children"] == []
    edges = db.execute(select(ItemComposition)
                       .order_by(ItemComposition.child_item_id)).scalars().all()
    assert [e.child_item_id for e in edges] == [child_a["item"].id, child_b["item"].id]


def test_top_level_accept_mismatch_applies_to_all_entries(db, scene):
    """顶层 `accept_mismatch` 作用于所有差额条目（契约第 2 条）。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    # 两条核对共用同一个父项、各自差 0.01（构成边只建一次，第二次是「已一致」）
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("59.99")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))

    out = batch_service.finalize(db, scene["batch"].id, [entry],
                                 accept_mismatch={"reason": "顶层理由：明细口径差异"})

    assert out["mismatch_accepted"] == 1
    assert out["accepted_mismatch"] is True
    rec = db.execute(select(ScreenshotReconciliation)).scalar_one()
    assert rec.status == "accepted_mismatch"
    assert rec.accepted_reason == "顶层理由：明细口径差异"


def test_entry_level_accept_mismatch_is_ignored(db, scene):
    """`accept_mismatch` **只在顶层**（前端已确认）：条目内带了不改变结果。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("59.98")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry["accept_mismatch"] = {"reason": "条目级理由（不该生效）"}

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry])
    assert err.code == ErrorCode.RECONCILE_MISMATCH
    assert err.details["accepted"] is False
    assert err.details["reconciliations"][0]["diff"] == "0.02"

    # 顶层给了理由才放行（覆盖同一批）
    out = batch_service.finalize(db, scene["batch"].id, [entry],
                                 accept_mismatch={"reason": "顶层理由"})
    assert out["reconciliations"][0]["status"] == "accepted_mismatch"
    assert db.execute(select(ScreenshotReconciliation)).scalar_one().accepted_reason \
        == "顶层理由"


def test_reconcile_uses_direct_children_only(db, scene):
    """核对口径＝直接子项之和：子项自己是汇总项时按其快照金额算，不展开孙层。"""
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    grandchild = mk_item(db, "示例-黄金账户明细")
    # 子项 child_a 自己也是汇总项（它 ← 孙项）：核对仍只算 child_a 的 60
    composition_service.create_edge(db, child_a["item"].id, grandchild.id)

    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    out = batch_service.finalize(db, scene["batch"].id, [entry])

    assert out["reconciliations"][0]["diff"] == "0.00"
    # 边只有「父←子」两条，没有把孙项直接挂到父上
    edges = db.execute(select(ItemComposition)
                       .order_by(ItemComposition.id)).scalars().all()
    assert sorted((e.parent_item_id, e.child_item_id) for e in edges) == sorted([
        (parent["item"].id, child_a["item"].id),
        (parent["item"].id, child_b["item"].id),
        (child_a["item"].id, grandchild.id),
    ])


def test_apply_options_json_carries_batch_id_and_reconciles(db, tmp_path, monkeypatch):
    """契约第 1 条：`batch_id` 在 apply 的 **options JSON** 里（不是 multipart 顶层字段）。

    同时串起三条 options 通道：`reconciles`（零写入）＋ `batch_id`（成员登记）＋
    `remember_aggregate_names`（写规则表）。OCR 管线在这里用替身（本用例只验协议面）。
    """
    import asyncio
    import io
    import json

    from fastapi import UploadFile

    from routers import screenshots as screenshots_router
    from services.screenshot_service import (
        ParsedScreenshot, PreviewRow, ScreenshotPreview,
    )

    sg = mk_group(db, slug="demo-opt-group", label="示例选项来源组")
    item = mk_item(db, "示例-贵金属汇总")
    ev = mk_evidence(db, sg.id, "j1")
    preview = ScreenshotPreview(rows=[PreviewRow(
        source_name="示例-贵金属汇总", source_amount=Decimal("100"),
        action="update", asset_name="示例-贵金属汇总",
        current_amount=Decimal("100"), diff=Decimal("0"), index=0)])

    async def fake_pipeline(path, sha, source_group, session, **kwargs):
        return ParsedScreenshot(), [("示例-贵金属汇总", Decimal("100"))], preview, "示例-"

    def fake_persist(file, source_group, session):
        path = tmp_path / "upload.png"
        path.write_bytes(b"\x89PNG fake")
        return str(path), "sha-fake", ev.id

    monkeypatch.setattr(screenshots_router, "_run_pipeline", fake_pipeline)
    monkeypatch.setattr(screenshots_router, "persist_upload", fake_persist)

    batch = batch_service.create_batch(db, data_date=D, source_group=sg.slug,
                                       expected_members=1)
    options = json.dumps({
        "source_group": sg.slug,
        "reconciles": [0],                     # 汇总行：零写入
        "batch_id": batch.id,                  # 批次：成员登记
        "remember_aggregate_names": ["示例贵金属汇总"],   # 学规则
    })

    result = asyncio.run(screenshots_router.screenshot_apply(
        file=UploadFile(file=io.BytesIO(b"\x89PNG fake"), filename="shot.png"),
        options=options, data_date="", recognition="", db=db))

    assert result.rows_written == 0                       # 汇总行不落账
    assert result.reconciled[0]["asset_item_id"] == item.id
    assert result.reconciled[0]["amount"] == "100.00"
    # batch_id 来自 options → 成员行落库、批次推进到 partial
    members = db.execute(select(ScreenshotBatchMember)
                         .where(ScreenshotBatchMember.batch_id == batch.id)
                         ).scalars().all()
    assert [(m.evidence_id, m.status) for m in members] == [(ev.id, "applied")]
    assert db.get(ScreenshotBatch, batch.id).status == "partial"
    # remember_aggregate_names 落在规则表
    rows = db.execute(select(SourceGroupLearnedRule).where(
        SourceGroupLearnedRule.rule_type == "aggregate")).scalars().all()
    assert [r.match for r in rows] == ["示例贵金属汇总"]


def test_manual_parent_without_evidence_is_supported(db, scene):
    """手选既有资产项当汇总行：两个定位字段都为 null，`parent_item_id` 是唯一锚。

    审计按 `parent_source` 区分两条来路（manual / preview_row），否则事后分不清
    「这行来自某张图」还是「人手工挑的」。
    """
    parent, child_a, child_b = scene["parent"], scene["child_a"], scene["child_b"]
    entry = recon_entry(parent, 0, [
        (child_a["item"], child_a["evidence_id"], 3, Decimal("60.00")),
        (child_b["item"], child_b["evidence_id"], 4, Decimal("40.00")),
    ], Decimal("100.00"))
    entry["parent_evidence_id"] = None       # 手选父项：本批截图里没有这一行
    entry["parent_row_index"] = None

    out = batch_service.finalize(db, scene["batch"].id, [entry])

    rec = db.execute(select(ScreenshotReconciliation)).scalar_one()
    assert (rec.parent_evidence_id, rec.parent_row_index) == (None, None)
    assert rec.parent_item_id == parent["item"].id
    assert out["reconciliations"][0]["parent_source"] == "manual"
    # 构成边照常建立（父项＝那个手选项）
    assert db.execute(select(func.count()).select_from(ItemComposition)).scalar() == 2

    log = db.execute(select(AuditLog).where(
        AuditLog.entity == "screenshot_batch",
        AuditLog.action == "finalize")).scalar_one()
    assert '"parent_sources"' in log.after_json
    assert f'"{parent["item"].id}": "manual"' in log.after_json


def test_reconciliation_without_children_is_allowed_but_differs(db, scene):
    """契约：`children` 可省（默认 `[]`）——合计 0、差额全额，必须显式接受才定案。"""
    parent = scene["parent"]
    entry = recon_entry(parent, 0, [], Decimal("100.00"))
    entry.pop("children")

    err = _raise(batch_service.finalize, db, scene["batch"].id, [entry])
    assert err.code == ErrorCode.RECONCILE_MISMATCH
    assert err.details["reconciliations"][0]["children_sum"] == "0.00"
    assert err.details["reconciliations"][0]["diff"] == "100.00"

    out = batch_service.finalize(db, scene["batch"].id, [entry],
                                 accept_mismatch={"reason": "明细暂缺，已人工确认"})
    assert out["reconciliations"][0]["status"] == "accepted_mismatch"
    assert db.execute(select(func.count()).select_from(ItemComposition)).scalar() == 0
