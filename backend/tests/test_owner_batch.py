"""批量历史归属端点测试（P3a）。

覆盖：成员；同日更正；同值跳过；归档/不存在逐项失败；超限/空/去重；
Idempotency-Key 必填与重放；owner/date/reason 校验；审计与 append-only；
以及「列与史同事务」——写归属必须让 `asset_items.owner_id` 立刻可见。

ADR-0019 D6：归属值域是 `members`（「家庭池」合成归属退役，请求 422 指路）。
"""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import AssetItem, AssetItemOwnerChange, AuditLog, Member
from routers import asset_items as asset_items_router
from services import asset_owner_service, asset_service
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware

M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
TODAY = date.today()


@pytest.fixture()
def batch_app(tmp_path):
    path = str(tmp_path / "batch.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        M0010.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    Session = sessionmaker(bind=engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override
    app.include_router(asset_items_router.router, prefix="/api/asset-items")
    return {"app": app, "Session": Session, "path": path}


def _item(db, name="示例-资产"):
    return create_asset(db, name, Decimal("100"), TODAY)


def _owner(db, name="示例成员"):
    """归属主体＝members 行（ADR-0019 D6）。"""
    member = Member(name=name, role="self")
    db.add(member)
    db.commit()
    return member


def _post(client, body, key="batch-1", audit_reason="evolution"):
    """spec E3：批量改归属是纠正类写入，审计 reason 走 query（请求体 reason 是变更史文本）。"""
    headers = {"Idempotency-Key": key} if key is not None else {}
    params = {"reason": audit_reason} if audit_reason is not None else None
    return client.post("/api/asset-items/owners/batch", json=body, headers=headers,
                       params=params)


def _body(ids, owner_kind="member", owner_id=None, reason="批量修正",
          effective_date=None):
    return {"ids": ids, "owner_kind": owner_kind, "owner_id": owner_id,
            "reason": reason, "effective_date": effective_date}


def test_batch_member_success(batch_app):
    Session = batch_app["Session"]
    db = Session()
    first, second = _item(db, "示例-甲"), _item(db, "示例-乙")
    owner = _owner(db)
    ids = [first.asset_item_id, second.asset_item_id]
    owner_id = owner.id
    db.close()

    client = TestClient(batch_app["app"])
    response = _post(client, _body(ids, owner_id=owner_id,
                                   effective_date="2026-01-15"),
                     key="m-1")
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2, "skipped": 0, "failed": []}

    db = Session()
    rows = db.execute(select(AssetItemOwnerChange)
                      .order_by(AssetItemOwnerChange.id)).scalars().all()
    assert len(rows) == 2
    assert all(r.owner_kind == "member" and r.owner_id == owner_id for r in rows)
    assert all(r.effective_date == date(2026, 1, 15) for r in rows)
    assert all(r.reason == "批量修正" for r in rows)
    # 真相源：列与史同事务落库（只写史会让列表/导出看不到这次变更）
    assert {db.get(AssetItem, i).owner_id for i in ids} == {owner_id}
    audited = db.execute(select(func.count()).select_from(AuditLog)
                         .where(AuditLog.entity == "asset_item_owner")).scalar()
    assert audited == 2
    db.close()


def test_batch_family_pool_retired(batch_app):
    """「家庭池」合成归属随 ADR-0019 D6 退役：值域是 members，NULL 表达不了共有归属。

    旧行为（`owner_kind='family_pool'` + `owner_id=NULL`）在新读模型里等于「未标注」，
    继续接受就是留一条静默不生效的写路径 → 现在整批 422 + 指路 joint 成员。
    """
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    response = _post(client, _body([item_id], owner_kind="family_pool",
                                   owner_id=None), key="pool-1")
    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"][0]["field"] == "owner_kind"
    assert "joint" in response.json()["error"]["message"]
    db = Session()
    assert db.execute(select(func.count()).select_from(
        AssetItemOwnerChange)).scalar() == 0
    assert db.get(AssetItem, item_id).owner_id is None
    db.close()


def test_batch_same_day_correction(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    first = _owner(db, "示例成员甲")
    second = _owner(db, "示例成员乙")
    asset_owner_service.set_item_owner(
        db, item.asset_item_id, "member", first.id, reason="初始",
        effective_date=TODAY, audit_reason="evolution")
    item_id = item.asset_item_id
    second_id = second.id
    db.close()

    client = TestClient(batch_app["app"])
    response = _post(client, _body([item_id], owner_id=second_id,
                                   effective_date=TODAY.isoformat()), key="c-1")
    assert response.json()["updated"] == 1
    db = Session()
    rows = db.execute(select(AssetItemOwnerChange).order_by(
        AssetItemOwnerChange.id)).scalars().all()
    assert len(rows) == 2                          # 同日追加，保留史
    assert rows[-1].owner_id == second_id
    assert asset_owner_service.current_owner(db, item_id)["owner_id"] == second_id
    assert db.get(AssetItem, item_id).owner_id == second_id     # 列同步
    db.close()


def test_batch_same_value_skipped(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    owner = _owner(db)
    owner_id = owner.id
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    body = _body([item_id], owner_id=owner_id)
    assert _post(client, body, key="s-1").json()["updated"] == 1
    second = _post(client, body, key="s-2")
    assert second.json() == {"updated": 0, "skipped": 1, "failed": []}
    db = Session()
    assert db.execute(select(func.count()).select_from(
        AssetItemOwnerChange)).scalar() == 1
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.entity == "asset_item_owner")).scalar() == 1
    assert db.get(AssetItem, item_id).owner_id == owner_id
    db.close()


def test_batch_archived_and_missing_partial_failure(batch_app):
    Session = batch_app["Session"]
    db = Session()
    active, archived = _item(db, "示例-活跃"), _item(db, "示例-归档")
    owner = _owner(db)
    asset_service.set_item_status(db, archived.asset_item_id, "archived",
                                  reason="normalization")
    active_id = active.asset_item_id
    archived_id = archived.asset_item_id
    owner_id = owner.id
    db.close()
    client = TestClient(batch_app["app"])
    response = _post(client, _body([active_id, archived_id, 999999],
                                   owner_id=owner_id), key="f-1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["updated"] == 1
    assert payload["failed"] == [
        {"id": archived_id, "code": "ITEM_ARCHIVED"},
        {"id": 999999, "code": "NOT_FOUND"},
    ]
    db = Session()
    assert db.execute(select(func.count()).select_from(
        AssetItemOwnerChange)).scalar() == 1        # 仅成功项落史
    db.close()


def test_batch_ids_validation_and_dedupe(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    owner = _owner(db)
    owner_id = owner.id
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    # 去重后按一项处理
    dup = _post(client, _body([item_id, item_id], owner_id=owner_id), key="d-1")
    assert dup.json() == {"updated": 1, "skipped": 0, "failed": []}

    empty = _post(client, _body([]), key="d-2")
    assert empty.status_code == 422
    too_many = _post(client, _body(list(range(1, 202))), key="d-3")
    assert too_many.status_code == 422


def test_batch_owner_and_reason_validation(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    missing = _post(client, _body([item_id], owner_kind="member",
                                  owner_id=None), key="v-1")
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["fields"][0]["field"] == "owner_id"

    pool_with_id = _post(client, _body([item_id], owner_kind="family_pool",
                                       owner_id=1), key="v-2")
    assert pool_with_id.status_code == 422

    no_reason = _post(client, _body([item_id], owner_id=1, reason="  "),
                      key="v-3")
    assert no_reason.status_code == 422
    assert no_reason.json()["error"]["details"]["fields"][0]["field"] == "reason"

    bad_date = _post(client, _body([item_id], owner_id=1,
                                   effective_date="not-a-date"), key="v-4")
    assert bad_date.status_code == 422


def test_batch_missing_audit_reason_fails_each_item(batch_app):
    """批量改归属同属纠正类（`action='change'`）：不带 query reason → 逐项进 failed[]。

    逐项独立事务 ⇒ 不是整批 422；但「不该有业务写入」照样成立——列 / 史 / 审计行都为空。
    请求体 `reason`（变更史文本）带得再全也替代不了 query 里的审计理由。
    """
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    owner = _owner(db)
    item_id, owner_id = item.asset_item_id, owner.id
    db.close()

    client = TestClient(batch_app["app"])
    response = _post(client, _body([item_id], owner_id=owner_id), key="nr-1",
                     audit_reason=None)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "updated": 0, "skipped": 0,
        "failed": [{"id": item_id, "code": "AUDIT_REASON_REQUIRED"}],
    }
    db = Session()
    assert db.execute(select(func.count()).select_from(
        AssetItemOwnerChange)).scalar() == 0
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.entity == "asset_item_owner")).scalar() == 0
    assert db.get(AssetItem, item_id).owner_id is None
    db.close()


def test_batch_requires_idempotency_key(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    owner = _owner(db)
    owner_id = owner.id
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    response = _post(client, _body([item_id], owner_id=owner_id), key=None)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]["fields"][0]["field"] == \
        "Idempotency-Key"


def test_batch_idempotent_replay_single_write(batch_app):
    Session = batch_app["Session"]
    db = Session()
    item = _item(db)
    owner = _owner(db)
    owner_id = owner.id
    item_id = item.asset_item_id
    db.close()
    client = TestClient(batch_app["app"])
    body = _body([item_id], owner_id=owner_id)
    first = _post(client, body, key="replay-1")
    second = _post(client, body, key="replay-1")
    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    db = Session()
    assert db.execute(select(func.count()).select_from(
        AssetItemOwnerChange)).scalar() == 1
    db.close()

    reused = _post(client, _body([item_id], owner_id=owner_id, reason="换个体"),
                   key="replay-1")
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"