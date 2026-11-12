"""三期-1 W3「资产项级归属收口（后端）」测试（ADR-0011 / spec §2.3/§3.2/§6）。

覆盖：0016 迁移幂等/CHECK/UNIQUE/append-only 触发器、回填确定性 tie-break 与
审计计数、保留名 fail-fast、读路径 item 级优先/记录级回退、列表过滤、写路径
旧入口翻译（PUT/批量/创建）、canonical 端点契约（404/409 ITEM_ARCHIVED/422/
幂等同值不追加）、导出、owner 删除保护（归属史 + 目标范围）。

数据一律合成（「示例-」、虚构金额与日期）。
"""
import importlib.util
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import (Asset, AssetClass, AssetItem, AssetItemOwnerChange,
                    AuditLog, Institution, Member)
from services import (asset_owner_service, asset_service, classification_service,
                     export_service)
from services.db_migration_service import _MIGRATIONS_DIR
from services.idempotency_service import IdempotencyCaptureMiddleware

from conftest import legacy_eav_tables

TODAY = date.today()


# ---------------------------------------------------------------- fixtures


def _load_migration(name: str):
    spec = importlib.util.spec_from_file_location(name, f"{_MIGRATIONS_DIR}/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(conn, module):
    conn.execute("BEGIN")
    module.upgrade(conn)
    conn.execute("COMMIT")


# 0016 的输入形状（0027 已把这两张表物理删除）：只给「验历史迁移行为」的用例用。
# 0016 仍在迁移链里，它读的是旧 EAV 两表，所以这里用裸 SQL 造旧形状——但不能再用
# ORM 类（`Attribute` / `AssetAttribute` 已随 0027 从 models.py 删除）。
LEGACY_EAV_DDL = """
CREATE TABLE IF NOT EXISTS attributes (
  id INTEGER PRIMARY KEY, name TEXT, type TEXT, level INTEGER,
  remind_enabled BOOLEAN, created_at DATETIME, updated_at DATETIME);
CREATE TABLE IF NOT EXISTS asset_attributes (
  asset_id INTEGER NOT NULL, attribute_id INTEGER NOT NULL,
  created_at DATETIME, PRIMARY KEY (asset_id, attribute_id));
"""


def mk_legacy_owner(conn, name="示例成员甲"):
    """旧 `attributes(type='owner')` 行（返回 id）；仅供 0016 回填用例。"""
    return conn.execute(
        "INSERT INTO attributes (name, type, level, remind_enabled, created_at,"
        " updated_at) VALUES (?, 'owner', 0, 1, 't', 't')", (name,)).lastrowid


def link_legacy_owner(conn, asset_id, owner_id, created_at="2026-02-01 09:00:00"):
    """旧 `asset_attributes` 记录级绑定（0016 的 tie-break 输入）。"""
    conn.execute(
        "INSERT INTO asset_attributes (asset_id, attribute_id, created_at)"
        " VALUES (?, ?, ?)", (asset_id, owner_id, created_at))


def mk_member(db, name="示例成员甲", role="self"):
    """ADR-0019 D6：归属值域改指 members（角色，不存真人姓名）。"""
    member = Member(name=name, role=role)
    db.add(member)
    db.commit()
    return member


def _get_or_create(db, model, *, name, defaults=None):
    """按 name 取字典行，缺失则建（测试内最小 helper）。"""
    row = db.execute(
        select(model).where(model.name == name)).scalar_one_or_none()
    if row is None:
        row = model(name=name, **(defaults or {}))
        db.add(row)
        db.commit()
    return row


def mk_item(db, name="示例-资产"):
    item = AssetItem(display_name=name, status="draft")  # ADR-0019: 未分类→draft
    db.add(item)
    db.commit()
    return item


def mk_classified_item(db, name="示例-资产", *, owner_id=None, member=None):
    """建 **active** 资产项（四维齐全）。

    ADR-0019 D9：草稿项不进任何对外口径（导出/汇总），需要走这些口径的用例必须
    把四维补齐。`owner_id` 是项级列（ADR-0019 D6：值域 members）。
    """
    klass = _get_or_create(db, AssetClass, name="示例类别",
                           defaults={"code": "T1", "level": 1,
                                     "side_scope": "any"})
    inst = _get_or_create(db, Institution, name="示例机构",
                          defaults={"kind": "bank"})
    owner = member or _get_or_create(db, Member, name="本人",
                                     defaults={"role": "self"})
    item = AssetItem(display_name=name, status="draft")
    db.add(item)
    db.commit()
    classification_service.set_classification(db, item.id, {
        "asset_class_id": klass.id,
        "liquidity_days": 0,
        "institution_id": inst.id,
        "owner_id": owner_id or owner.id,
    })
    return db.get(AssetItem, item.id)


def mk_asset(db, item, amount, on_date):
    asset = Asset(asset_name=item.display_name, asset_item_id=item.id,
                  amount=Decimal(str(amount)), asset_date=on_date, side="asset")
    db.add(asset)
    db.commit()
    return asset


def audits(db, entity=None, action=None):
    q = select(AuditLog)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if action:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars().all())


# ---------------------------------------------------------------- 迁移 0016


def test_0016_double_run_idempotent(tmp_path):
    module = _load_migration("0016_asset_item_owner_changes")
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.executescript(
        "CREATE TABLE attributes (id INTEGER PRIMARY KEY, name TEXT, type TEXT);"
        "CREATE TABLE asset_items (id INTEGER PRIMARY KEY, display_name TEXT);"
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_name TEXT, asset_item_id INTEGER,"
        " asset_date DATE, superseded_by INTEGER);"
        "CREATE TABLE asset_attributes (asset_id INTEGER, attribute_id INTEGER,"
        " created_at DATETIME, PRIMARY KEY (asset_id, attribute_id));"
    )
    for _ in range(2):
        _run_upgrade(conn, module)

    objs = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE tbl_name='asset_item_owner_changes'"
        " AND type IN ('table','index','trigger')").fetchall()
    names = [n for _, n in objs]
    # 表 + 2 索引（UNIQUE 自动索引不计名）+ 2 触发器，不倍增
    assert names.count("asset_item_owner_changes") == 1
    assert "ix_item_owner_item_eff" in names and "ix_item_owner_owner" in names
    assert "uq_item_owner_migration" in names
    assert "asset_item_owner_changes_no_update" in names
    assert "asset_item_owner_changes_no_delete" in names
    conn.close()


def test_0016_constraints_and_append_only_triggers(tmp_path):
    module = _load_migration("0016_asset_item_owner_changes")
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.executescript(
        "CREATE TABLE attributes (id INTEGER PRIMARY KEY, name TEXT, type TEXT);"
        "CREATE TABLE asset_items (id INTEGER PRIMARY KEY, display_name TEXT);"
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_name TEXT, asset_item_id INTEGER,"
        " asset_date DATE, superseded_by INTEGER);"
        "CREATE TABLE asset_attributes (asset_id INTEGER, attribute_id INTEGER,"
        " created_at DATETIME, PRIMARY KEY (asset_id, attribute_id));"
        "INSERT INTO asset_items (id, display_name) VALUES (1, '示例-资产');"
        "INSERT INTO attributes (id, name, type) VALUES (10, '示例成员甲', 'owner');"
    )
    _run_upgrade(conn, module)

    def insert(kind, owner_id, eff="2026-03-01", origin="manual", created_at="t"):
        conn.execute(
            "INSERT INTO asset_item_owner_changes (asset_item_id, owner_kind, owner_id,"
            " effective_date, origin, created_at) VALUES (1, ?, ?, ?, ?, ?)",
            (kind, owner_id, eff, origin, created_at))

    # member 必须带 owner_id；family_pool 必须为空
    with pytest.raises(sqlite3.IntegrityError):
        insert("member", None)
    with pytest.raises(sqlite3.IntegrityError):
        insert("family_pool", 10)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO asset_item_owner_changes (asset_item_id, owner_kind, owner_id,"
            " effective_date, origin, created_at)"
            " VALUES (1, 'member', 10, '2026-03-01', 'bogus', 't')")

    insert("family_pool", None)
    # 手写变更同日可追加（更正语义）：不触发唯一约束
    insert("member", 10, created_at="t2")
    # 仅回填行受部分唯一索引约束（每 item 每日至多一行 migration）
    insert("member", 10, eff="2026-03-02", origin="migration")
    with pytest.raises(sqlite3.IntegrityError):
        insert("family_pool", None, eff="2026-03-02", origin="migration", created_at="t3")
    # append-only：UPDATE / DELETE 均被触发器拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE asset_item_owner_changes SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM asset_item_owner_changes")
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_item_owner_changes").fetchone()[0] == 3
    conn.close()


# ---------------------------------------------------------------- 回填


def _seed_backfill(db, legacy_conn):
    """造 0016 回填的输入：ORM 建项/行 + 裸 SQL 造记录级 owner 绑定。"""
    owner_a = mk_legacy_owner(legacy_conn, "示例成员甲")
    owner_b = mk_legacy_owner(legacy_conn, "示例成员乙")
    i1, i2, i3 = (mk_item(db, n) for n in ("示例-甲", "示例-乙", "示例-丙"))

    # item1：最新 active 行带 owner B（更早 active 行带 A）→ 取最新行
    old = mk_asset(db, i1, 100, TODAY - timedelta(days=30))
    new = mk_asset(db, i1, 120, TODAY)
    link_legacy_owner(legacy_conn, old.id, owner_a)
    link_legacy_owner(legacy_conn, new.id, owner_b)
    old.superseded_by = new.id
    db.commit()
    # item2：最新 active 行无 owner，更早 active 行有 A → 未标注（lost candidate）
    old2 = mk_asset(db, i2, 200, TODAY - timedelta(days=10))
    link_legacy_owner(legacy_conn, old2.id, owner_a)
    mk_asset(db, i2, 210, TODAY)
    # item3：同一 active 行挂两个 owner，取 created_at 更晚的 B
    a3 = mk_asset(db, i3, 300, TODAY)
    link_legacy_owner(legacy_conn, a3.id, owner_a, "2026-02-01 09:00:00")
    link_legacy_owner(legacy_conn, a3.id, owner_b, "2026-02-02 09:00:00")
    return owner_a, owner_b, (i1, i2, i3)


def test_0016_backfill_deterministic_and_audit_counts(tmp_path):
    db_path = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    legacy = sqlite3.connect(db_path)
    legacy.isolation_level = None
    legacy.executescript(LEGACY_EAV_DDL)
    owner_a, owner_b, (i1, i2, i3) = _seed_backfill(db, legacy)
    db.close()
    legacy.close()

    module = _load_migration("0016_asset_item_owner_changes")
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    _run_upgrade(conn, module)

    rows = conn.execute(
        "SELECT asset_item_id, owner_kind, owner_id, effective_date, origin, reason"
        " FROM asset_item_owner_changes ORDER BY asset_item_id").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (i1.id, "member", owner_b),
        (i3.id, "member", owner_b),
    ]          # item2 未标注不插行；item1 最新行胜；item3 created_at tie-break
    assert all(r[3] == TODAY.isoformat() for r in rows)
    assert all(r[4] == "migration" and "migration" in r[5] for r in rows)

    audit = conn.execute(
        "SELECT actor, after_json FROM audit_log WHERE entity='schema'"
        " AND action='owner_backfill'").fetchone()
    counts = json.loads(audit[1])
    assert audit[0] == "system"
    assert {k: counts[k] for k in ("items_with_owner", "changes", "conflicts",
                                   "owner_lost_candidates")} == {
        "items_with_owner": 3, "changes": 2, "conflicts": 1,
        "owner_lost_candidates": 1}
    assert counts["conflict_item_ids"] == [i3.id]
    assert counts["owner_lost_item_ids"] == [i2.id]

    # 幂等重跑：不重复插行、不重复审计
    _run_upgrade(conn, module)
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_item_owner_changes").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='owner_backfill'").fetchone()[0] == 1
    conn.close()


def test_0016_reserved_family_pool_name_fail_fast(tmp_path):
    db_path = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    module = _load_migration("0016_asset_item_owner_changes")
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    # 旧形状：保留名「家庭池」是 owner 字典行（0027 已删表，用裸 SQL 造）
    conn.executescript(LEGACY_EAV_DDL)
    mk_legacy_owner(conn, "家庭池")
    with pytest.raises(RuntimeError) as ei:
        _run_upgrade(conn, module)
    assert "家庭池" in str(ei.value)
    conn.close()


# ---------------------------------------------------------------- 读路径


@pytest.fixture()
def api(tmp_path):
    """真实文件库 + assets/asset_items/asset_owners 路由（错误信封 + 幂等中间件）。"""
    db_path = tmp_path / "owner.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    m10 = _load_migration("0010_idempotency")
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    _run_upgrade(conn, m10)
    conn.close()
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import asset_items, asset_owners, assets

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(assets.router, prefix="/api/assets")
    app.include_router(asset_items.router, prefix="/api/asset-items")
    app.include_router(asset_owners.router, prefix="/api/asset-owners")
    return SimpleNamespace(app=app, client=TestClient(app), Session=Session)


def test_read_owner_from_item_column(api):
    """归属读模型：`asset_items.owner_id` 是唯一显示源（ADR-0019 D2/D6）。

    旧「记录级 EAV 回退」（ADR-0011 读路径 item 级优先 / 记录级回退）随
    `asset_attributes` 表删除（0027）而彻底退役：记录上不再承载任何归属，读模型
    只看 `asset_items.owner_id`——旧表已不存在，连「挂着也不看」的形态都无法构造。
    """
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    owner_b = mk_member(db, "示例成员乙", "spouse")
    item = mk_item(db)
    asset = mk_asset(db, item, 100, TODAY)
    db.close()

    body = api.client.get(f"/api/assets/{asset.id}").json()
    assert (body["owner_id"], body["owner_name"], body["owner_kind"]) == (
        None, None, None)                                  # 项级未标注 → 空

    # 项级归属（列）→ 立即回显
    db = api.Session()
    db.get(AssetItem, item.id).owner_id = owner_a.id
    db.commit()
    db.close()
    body = api.client.get(f"/api/assets/{asset.id}").json()
    assert (body["owner_id"], body["owner_name"], body["owner_kind"]) == (
        owner_a.id, "示例成员甲", "self")

    # 改项级归属 → 同一条记录跟着变（记录行从不被回写）
    db = api.Session()
    db.get(AssetItem, item.id).owner_id = owner_b.id
    db.commit()
    db.close()
    body = api.client.get(f"/api/assets?item_id={item.id}").json()["items"][0]
    assert (body["owner_id"], body["owner_name"], body["owner_kind"]) == (
        owner_b.id, "示例成员乙", "spouse")


def test_list_filter_item_owner_and_role(api):
    """列表过滤走项级 `owner_id` + `members.role`（不再是记录级属性筛选）。"""
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    member_b = mk_member(db, "示例成员乙", "joint")
    i1, i2, i3 = (mk_item(db, n) for n in ("示例-一", "示例-二", "示例-三"))
    a1 = mk_asset(db, i1, 100, TODAY)
    a2 = mk_asset(db, i2, 200, TODAY)
    a3 = mk_asset(db, i3, 300, TODAY)                      # 未标注归属
    i1.owner_id = member_a.id
    i2.owner_id = member_b.id
    db.commit()
    db.close()

    ids = lambda r: {x["id"] for x in r.json()["items"]}
    assert ids(api.client.get(f"/api/assets?owner_id={member_a.id}")) == {a1.id}
    assert ids(api.client.get(f"/api/assets?owner_id={member_b.id}")) == {a2.id}
    assert ids(api.client.get("/api/assets?owner_kind=joint")) == {a2.id}
    assert ids(api.client.get("/api/assets?owner_kind=self")) == {a1.id}
    assert ids(api.client.get(
        f"/api/assets?owner_id={member_a.id}&owner_kind=joint")) == set()
    assert a3.id not in ids(api.client.get(f"/api/assets?owner_id={member_a.id}"))


def test_export_uses_item_level_owner(api):
    """导出读项级归属（草稿项整体不入导出，故用补齐四维的 active 项）。"""
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    item = mk_classified_item(db, "示例-导出项", owner_id=member_a.id)
    mk_asset(db, item, 100, TODAY)
    records = export_service.get_export_records(db)
    db.close()
    assert records[0]["owner"] == "示例成员甲"


# ---------------------------------------------------------------- 写路径翻译


def test_record_write_does_not_overwrite_item_owner(api):
    """记录写入不碰归属：项级 `owner_id` 不因新记录而改变，也不写记录级属性。"""
    db = api.Session()
    member_a = mk_member(db, "示例成员甲")
    item = mk_item(db)
    item.owner_id = member_a.id                            # 项级归属（列，ADR-0019 D6）
    db.commit()
    asset = mk_asset(db, item, 100, TODAY)
    item_id = asset.asset_item_id

    # 次日新建同一项记录：项级归属不变
    asset2 = asset_service.create_asset(
        db, "示例-资产", Decimal("120"), TODAY + timedelta(days=1))
    assert asset2.asset_item_id == item_id
    assert db.get(AssetItem, item_id).owner_id == member_a.id
    assert db.execute(select(AssetItemOwnerChange)).scalars().all() == []
    assert legacy_eav_tables(db) == set()          # 记录级载体已随 0027 删除
    db.close()


def test_put_record_rejects_owner_field(api):
    """ADR-0019：归属是项级列，记录 PUT 传 attribute_ids → 422 并指向资产项端点。

    旧「PUT 把记录级 owner 翻译成项级归属」的写路径已整体取消（分类只存在于
    资产项），翻译能力的替代品是 `PUT /api/asset-items/{item_id}/classification`
    与 `POST /api/asset-items/{item_id}/owner`。
    """
    db = api.Session()
    member_a = mk_member(db, "示例成员甲")
    item = mk_item(db)
    asset = mk_asset(db, item, 100, TODAY)
    db.close()

    resp = api.client.put(f"/api/assets/{asset.id}",
                          json={"attribute_ids": [member_a.id]})
    assert resp.status_code == 422, resp.text
    body = resp.json()["error"]
    assert body["details"]["fields"][0]["field"] == "attribute_ids"
    assert "asset-items" in body["message"]

    db = api.Session()
    assert db.execute(select(AssetItemOwnerChange)).scalars().all() == []
    assert db.get(AssetItem, item.id).owner_id is None       # 项级归属未被写
    db.close()


def test_batch_update_rejects_owner_field(api):
    """批量更新同样只改记录事实：`owner_id` 属项级分类 → 422（归属去项级端点）。"""
    db = api.Session()
    member_a = mk_member(db, "示例成员甲")
    item = mk_item(db)
    asset = mk_asset(db, item, 100, TODAY)
    db.close()

    resp = api.client.post("/api/assets/batch-update",
                           json={"ids": [asset.id], "owner_id": member_a.id})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "owner_id"

    db = api.Session()
    assert db.get(AssetItem, item.id).owner_id is None
    assert db.execute(select(AssetItemOwnerChange)).scalars().all() == []
    db.close()


# ---------------------------------------------------------------- canonical 端点


def test_owner_endpoint_contract(api):
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    owner_b = mk_member(db, "示例成员乙", "spouse")
    item = mk_item(db)
    db.close()

    assert api.client.get("/api/asset-items/9999/owner").status_code == 404
    body = api.client.get(f"/api/asset-items/{item.id}/owner").json()
    assert body["current"] is None and body["history"] == []

    resp = api.client.post(f"/api/asset-items/{item.id}/owner",
                           json={"owner_kind": "member", "owner_id": owner_a.id,
                                 "reason": "首次标注"},
                           params={"reason": "evolution"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["current"]["owner_id"] == owner_a.id
    assert data["current"]["owner_name"] == "示例成员甲"
    assert data["current"]["effective_date"] == TODAY.isoformat()
    assert data["entry"]["origin"] == "manual"
    assert data["change"]["entity"] == "asset_item_owner"
    assert data["change"]["before"] is None
    assert data["change"]["after"]["owner_id"] == owner_a.id

    # 真相源：写归属必须同时改列——只追加史会让列表/导出看不到这次变更
    db = api.Session()
    assert db.get(AssetItem, item.id).owner_id == owner_a.id
    db.close()

    # 同值重复：不追加（幂等语义），change=None
    again = api.client.post(f"/api/asset-items/{item.id}/owner",
                            json={"owner_kind": "member", "owner_id": owner_a.id})
    assert again.status_code == 200
    assert again.json()["change"] is None

    # 同日不同值：追加（更正＝追加），当前值取后写者
    same_day = api.client.post(f"/api/asset-items/{item.id}/owner",
                               json={"owner_kind": "member", "owner_id": owner_b.id,
                                     "reason": "同日更正"},
                               params={"reason": "evolution"})
    assert same_day.status_code == 200, same_day.text
    assert same_day.json()["current"]["owner_id"] == owner_b.id
    assert same_day.json()["change"]["before"]["owner_id"] == owner_a.id

    history = api.client.get(f"/api/asset-items/{item.id}/owner").json()
    assert [h["owner_id"] for h in history["history"]] == [owner_a.id, owner_b.id]
    assert history["current"]["owner_id"] == owner_b.id


def test_owner_change_requires_audit_reason(api):
    """改归属是纠正类写入（`action='change'` ∈ `REASON_REQUIRED_ACTIONS`，ADR-0027 D3）。

    审计理由走 **query**；请求体 `reason` 是变更史自由文本——只带后者不足以放行
    （「两套原因别混」）。缺理由时业务与审计一并回滚：列/史/审计行都不该多出来。
    """
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    item = mk_item(db)
    owner_id, item_id = owner_a.id, item.id
    db.close()

    body = {"owner_kind": "member", "owner_id": owner_id, "reason": "变更史文本"}
    missing = api.client.post(f"/api/asset-items/{item_id}/owner", json=body)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"

    db = api.Session()
    assert db.get(AssetItem, item_id).owner_id is None
    assert db.execute(select(AssetItemOwnerChange)).scalars().all() == []
    assert audits(db, "asset_item_owner") == []
    db.close()

    ok = api.client.post(f"/api/asset-items/{item_id}/owner", json=body,
                         params={"reason": "evolution",
                                 "reason_note": "改由配偶管理"})
    assert ok.status_code == 200, ok.text
    db = api.Session()
    rows = audits(db, "asset_item_owner", "change")
    assert len(rows) == 1
    assert rows[0].actor == "owner"
    assert rows[0].reason == "evolution"
    assert rows[0].reason_note == "改由配偶管理"
    # 业务与审计同事务落库：列 / 史 / 审计三者一致
    assert db.get(AssetItem, item_id).owner_id == owner_id
    assert [r.owner_id for r in db.execute(
        select(AssetItemOwnerChange)).scalars().all()] == [owner_id]
    db.close()


def test_owner_endpoint_validation_and_archived(api):
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    item = mk_item(db)
    archived = mk_item(db, "示例-归档")
    archived.status = "archived"
    db.commit()
    db.close()

    bad_kind = api.client.post(f"/api/asset-items/{item.id}/owner",
                               json={"owner_kind": "nobody"})
    assert bad_kind.status_code == 422

    missing_owner = api.client.post(f"/api/asset-items/{item.id}/owner",
                                    json={"owner_kind": "member"})
    assert missing_owner.status_code == 422
    assert missing_owner.json()["error"]["details"]["fields"][0]["field"] == "owner_id"

    not_owner = api.client.post(f"/api/asset-items/{item.id}/owner",
                                json={"owner_kind": "member", "owner_id": 9999})
    assert not_owner.status_code == 422

    # 「家庭池」已随 ADR-0019 D6 退役：422 + 指路 joint 成员（带 owner_id 同样拒绝）
    pool_with_owner = api.client.post(f"/api/asset-items/{item.id}/owner",
                                      json={"owner_kind": "family_pool",
                                            "owner_id": owner_a.id})
    assert pool_with_owner.status_code == 422
    assert pool_with_owner.json()["error"]["details"]["fields"][0]["field"] == \
        "owner_kind"
    assert "joint" in pool_with_owner.json()["error"]["message"]

    arch = api.client.post(f"/api/asset-items/{archived.id}/owner",
                           json={"owner_kind": "member", "owner_id": owner_a.id})
    assert arch.status_code == 409
    assert arch.json()["error"]["code"] == "ITEM_ARCHIVED"


def test_owner_post_idempotent_replay(api):
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    item = mk_item(db)
    db.close()
    body = {"owner_kind": "member", "owner_id": owner_a.id, "reason": "幂等"}
    headers = {"Idempotency-Key": "owner-1"}
    r1 = api.client.post(f"/api/asset-items/{item.id}/owner", json=body,
                         headers=headers, params={"reason": "evolution"})
    r2 = api.client.post(f"/api/asset-items/{item.id}/owner", json=body,
                         headers=headers, params={"reason": "evolution"})
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text
    db = api.Session()
    assert len(db.execute(select(AssetItemOwnerChange)).scalars().all()) == 1
    assert len(audits(db, "asset_item_owner")) == 1
    assert db.get(AssetItem, item.id).owner_id == owner_a.id
    db.close()


def test_same_day_correction_appends_and_latest_wins(api):
    """同日两次改归属：历史两行（append-only），当前值取后写者，部分唯一索引不拦截。"""
    db = api.Session()
    owner_a = mk_member(db, "示例成员甲", "self")
    owner_b = mk_member(db, "示例成员乙", "spouse")
    owner_c = mk_member(db, "示例成员丙", "joint")
    item = mk_item(db)
    db.close()

    r1 = api.client.post(f"/api/asset-items/{item.id}/owner",
                         json={"owner_kind": "member", "owner_id": owner_a.id},
                         params={"reason": "evolution"})
    r2 = api.client.post(f"/api/asset-items/{item.id}/owner",
                         json={"owner_kind": "member", "owner_id": owner_b.id},
                         params={"reason": "evolution"})
    assert r1.status_code == r2.status_code == 200, r2.text

    body = api.client.get(f"/api/asset-items/{item.id}/owner").json()
    assert len(body["history"]) == 2
    assert [h["owner_id"] for h in body["history"]] == [owner_a.id, owner_b.id]
    assert body["current"]["owner_id"] == owner_b.id         # 后写者胜
    assert body["history"][0]["effective_date"] == body["history"][1]["effective_date"]

    # 次日的写入照常覆盖（史按 effective_date 追加）；列始终跟随最新写
    db = api.Session()
    asset_owner_service.set_item_owner(
        db, item.id, "member", owner_c.id, reason="次日换人",
        effective_date=TODAY + timedelta(days=1), audit_reason="evolution")
    db.close()
    after = api.client.get(f"/api/asset-items/{item.id}/owner").json()
    assert after["current"]["owner_id"] == owner_c.id
    assert after["history"][-1]["effective_date"] == \
        (TODAY + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- 真相源单点


def test_current_owner_prefers_column_over_history(api):
    """`asset_items.owner_id` 是唯一真相源：史与列不一致时以列为准。

    这正是「写史不改列」时代留下的形状——若读路径还按史解析，改了归属列表也看不到。
    """
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    member_b = mk_member(db, "示例成员乙", "spouse")
    item = mk_classified_item(db, "示例-口径", owner_id=member_a.id)
    db.add(AssetItemOwnerChange(asset_item_id=item.id, owner_kind="member",
                                owner_id=member_b.id, effective_date=TODAY,
                                origin="manual", created_at="t"))
    db.commit()
    current = asset_owner_service.current_owner(db, item.id)
    db.close()
    assert current["owner_id"] == member_a.id          # 列胜出
    assert current["owner_name"] == "示例成员甲"
    # 最新史与列不符 → 不当成现值元数据回填（不拿旧口径冒充生效日/原因）
    assert current["effective_date"] is None and current["reason"] is None


def test_owner_history_dirty_id_returns_null_with_warning(api, caplog):
    """史里指向未知 id（0026 前旧 `attributes.id` 落位）→ owner_name=null + WARNING。

    防御口径：不静默清理、不猜测映射；现值仍取 `asset_items.owner_id`，
    脏史不污染当前归属。
    """
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    item = mk_classified_item(db, "示例-脏值", owner_id=member_a.id)
    db.add(AssetItemOwnerChange(asset_item_id=item.id, owner_kind="member",
                                owner_id=987654, effective_date=TODAY,
                                origin="migration", created_at="t"))
    db.commit()
    with caplog.at_level(logging.WARNING):
        history = asset_owner_service.owner_history(db, item.id)
        current = asset_owner_service.current_owner(db, item.id)
    db.close()

    # 该用例的本意是「脏史行怎么渲染」，与史行顺序无关：按值取那一行，
    # 避免依赖排序（分类写入口现在也会先落一行合法史，created_at 为 ISO 串）。
    dirty = next(h for h in history if h["owner_id"] == 987654)
    assert dirty["owner_name"] is None
    assert any("987654" in rec.getMessage() for rec in caplog.records)
    assert (current["owner_id"], current["owner_name"]) == (member_a.id, "示例成员甲")


def test_set_owner_updates_column_in_same_transaction(api, monkeypatch):
    """列与史同事务：审计写入失败时整笔回滚，不留「只写了史」的半截状态。"""
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    item = mk_item(db, "示例-事务")

    def _boom(*args, **kwargs):
        raise RuntimeError("审计写入失败")

    monkeypatch.setattr(asset_owner_service.audit_service, "record", _boom)
    with pytest.raises(RuntimeError):
        asset_owner_service.set_item_owner(db, item.id, "member", member_a.id,
                                           reason="合成")
    db.rollback()
    assert db.get(AssetItem, item.id).owner_id is None
    assert db.execute(select(AssetItemOwnerChange)).scalars().all() == []
    db.close()


# ---------------------------------------------------------------- 删除保护


def test_owner_delete_protection(api):
    """DELETE /api/asset-owners 转接 members：被引用即 409 DICT_IN_USE，不级联、不静默。

    「家庭池」取消后，归属值域就是 members，所以删除保护的口径也随之变成
    `asset_items.owner_id` + `asset_item_owner_changes.owner_id` 两处引用计数
    （由 classification_service.delete_member 单点实现，本端点不另写一份）。
    """
    db = api.Session()
    member_a = mk_member(db, "示例成员甲", "self")
    member_b = mk_member(db, "示例成员乙", "spouse")
    member_c = mk_member(db, "示例成员丙", "child")
    item = mk_item(db)
    db.add(AssetItemOwnerChange(asset_item_id=item.id, owner_kind="member",
                                owner_id=member_a.id, effective_date=TODAY,
                                origin="manual", created_at="t"))
    item.owner_id = member_b.id                    # 项级当前归属（draft 允许）
    db.commit()
    db.close()

    blocked = api.client.delete(f"/api/asset-owners/{member_a.id}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "DICT_IN_USE"
    assert "引用" in blocked.json()["error"]["message"]

    # 被 asset_items.owner_id 引用的同样拦住
    assert api.client.delete(f"/api/asset-owners/{member_b.id}").status_code == 409

    # 未被引用 → 204
    assert api.client.delete(f"/api/asset-owners/{member_c.id}",
                             params={"reason": "normalization"}).status_code == 204
    db = api.Session()
    assert db.get(Member, member_c.id) is None
    assert db.get(Member, member_a.id) is not None
    db.close()


def test_owner_endpoints_delegate_to_members(api):
    """值域＝members：POST/PUT 透传 role，「家庭池」保留名校验随 D6 退役。"""
    # 旧前端只发 name：role 必填，服务层直接 422（不做静默默认值）
    missing_role = api.client.post("/api/asset-owners", json={"name": "示例成员甲"})
    assert missing_role.status_code == 422
    assert "role" in missing_role.json()["error"]["message"]

    created = api.client.post("/api/asset-owners",
                              json={"name": "示例成员甲", "role": "spouse"})
    assert created.status_code == 201, created.text
    assert (created.json()["name"], created.json()["role"]) == ("示例成员甲", "spouse")

    listed = api.client.get("/api/asset-owners").json()
    assert [(o["id"], o["name"], o["role"]) for o in listed] == [
        (created.json()["id"], "示例成员甲", "spouse")]

    updated = api.client.put(f"/api/asset-owners/{created.json()['id']}",
                             json={"name": "示例成员乙", "role": "joint"})
    assert updated.status_code == 200, updated.text
    assert (updated.json()["name"], updated.json()["role"]) == ("示例成员乙", "joint")