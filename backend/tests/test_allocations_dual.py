"""三期-2 W2 C2 双阈值测试（spec §5.2/§5.3、§7.2；ADR-0012 D2/D7）＋ ADR-0019 维度口径。

覆盖：0017 迁移双跑幂等/无回填；analysis `band` 字段与按带型 recommendation/
priority；阈值 CRUD 校验（422 fields）；审计 before/after 四字段；Idempotency-Key
重放不双写；维度已是**资产项级**（`institution` 取代旧 `account`）、旧维度名 422。数据全部合成。
"""
import importlib.util
import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode
from models import (
    AllocationTarget, AssetClass, AssetItem, AuditLog, Institution, Member,
)
from schemas import AllocationTargetCreate, AllocationTargetUpdate
from services import allocation_service
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR
from services.idempotency_service import IdempotencyCaptureMiddleware

D = date(2026, 1, 31)


# ---------------------------------------------------------------- 迁移 0017


def _m0017():
    spec = importlib.util.spec_from_file_location(
        "m0017", f"{_MIGRATIONS_DIR}/0017_allocation_dual_threshold.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mk_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE allocation_targets ("
        " id INTEGER PRIMARY KEY, dimension TEXT NOT NULL,"
        " target_id TEXT NOT NULL, parent_id INTEGER,"
        " target_percent DECIMAL(5,2) NOT NULL,"
        " alert_threshold DECIMAL(5,2) NOT NULL DEFAULT 5.00)")
    conn.execute(
        "INSERT INTO allocation_targets (dimension, target_id, target_percent)"
        " VALUES ('fund_type', '1', 60.00)")
    conn.commit()
    conn.close()


def test_0017_double_run_idempotent_and_no_backfill(tmp_path):
    db_path = str(tmp_path / "m.db")
    _mk_legacy_db(db_path)
    module = _m0017()
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    for _ in range(2):
        conn.execute("BEGIN")
        module.upgrade(conn)
        conn.execute("COMMIT")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(allocation_targets)")]
    assert cols.count("alert_threshold_red") == 1
    assert cols.count("relative_threshold") == 1
    # 无回填：存量行两新列仍为 NULL
    row = conn.execute(
        "SELECT alert_threshold_red, relative_threshold FROM allocation_targets"
    ).fetchone()
    assert row == (None, None)
    assert conn.execute(
        "SELECT COUNT(*) FROM allocation_targets"
        " WHERE alert_threshold_red IS NOT NULL").fetchone()[0] == 0
    # 无新表/索引/触发器
    extra = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('index','trigger')"
        " AND name LIKE '%allocation_targets%'").fetchone()[0]
    assert extra == 0
    conn.close()


def test_0017_noop_without_table(tmp_path):
    db_path = str(tmp_path / "empty.db")
    sqlite3.connect(db_path).close()
    conn = sqlite3.connect(db_path)
    _m0017().upgrade(conn)          # 不 raise、不建表
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "allocation_targets" not in tables
    conn.close()


# ---------------------------------------------------------------- analysis 带型


def _mk_leaf_class(db, code="Z1", name="示例类别"):
    """叶子类别（项级分类的 asset_class_id 目标）。"""
    cls = AssetClass(code=code, name=name, parent_id=None, level=1, side_scope="asset")
    db.add(cls)
    db.commit()
    return cls


def _mk_institution(db, name):
    institution = Institution(name=name, kind="bank")
    db.add(institution)
    db.commit()
    return institution


def _mk_member(db, name="本人", role="self"):
    member = Member(name=name, role=role)
    db.add(member)
    db.commit()
    return member


def _mk_item(db, name, amount, *, cls, institution, member,
             liquidity_days=0, channel=None, risk_level=None):
    """建一个已分类的资产项＋当日一条记录（分类在项上，记录侧只读）。"""
    item = AssetItem(display_name=name, status="active",
                     asset_class_id=cls.id, liquidity_days=liquidity_days,
                     institution_id=institution.id, owner_id=member.id,
                     channel_id=channel.id if channel is not None else None,
                     risk_level=risk_level)
    db.add(item)
    db.commit()
    create_asset(db, name, Decimal(str(amount)), D, item_id=item.id)
    return item


def _portfolio(db):
    """三个机构：600/300/100；目标 40/30/20 → +20pp 红、0 绿、-10pp 黄（项级聚合）。"""
    cls = _mk_leaf_class(db)
    member = _mk_member(db)
    institutions = [_mk_institution(db, f"示例机构{n}") for n in "甲乙丙"]
    for institution, amount in zip(institutions, (600, 300, 100)):
        _mk_item(db, f"示例-{institution.name}", amount,
                 cls=cls, institution=institution, member=member)
    for institution, target_pct in zip(institutions, (40, 30, 20)):
        db.add(AllocationTarget(
            dimension="institution", target_id=str(institution.id),
            target_percent=Decimal(str(target_pct)),
            alert_threshold=Decimal("5")))
    db.commit()
    return institutions


def test_analysis_band_recommendation_and_priority(db):
    institutions = _portfolio(db)
    analysis = allocation_service.analyze_allocation(db, "institution", D)
    items = {i.id: i for i in analysis.items}

    red = items[str(institutions[0].id)]
    assert red.band.state == "red"
    assert red.band.yellow == "5.00" and red.band.red == "10.00"
    assert red.band.source.startswith("allocation_targets.alert_threshold+")
    assert red.band.components["rel_applicable"] is True
    assert red.recommendation == "超阈，建议回调至目标（超配减持约200元）"
    assert red.priority >= 200
    assert red.alert_threshold == Decimal("5.00")      # 兼容旧前端＝黄·绝对pp

    green = items[str(institutions[1].id)]
    assert green.band.state == "green"
    assert green.recommendation == "在带内" and green.priority == 0

    yellow = items[str(institutions[2].id)]
    assert yellow.band.state == "yellow"
    assert yellow.recommendation == "近阈值，下次调仓时校正"
    assert 100 <= yellow.priority < 200

    # 排序：先红后黄（suggestions 同步）
    assert [i.id for i in analysis.items] == [
        str(institutions[0].id), str(institutions[2].id), str(institutions[1].id)]
    suggestions = allocation_service.get_adjustment_suggestions(db, "institution", D)
    assert [s["id"] for s in suggestions] == [
        str(institutions[0].id), str(institutions[2].id)]


def test_analysis_band_null_without_target(db):
    cls = _mk_leaf_class(db)
    member = _mk_member(db)
    institution = _mk_institution(db, "示例机构甲")
    _mk_item(db, "示例-甲", 100, cls=cls, institution=institution, member=member)
    item = allocation_service.analyze_allocation(db, "institution", D).items[0]
    assert item.target_percent is None and item.band is None
    assert item.recommendation == "未设目标" and item.priority == 0


# ---------------------------------------------------------------- ADR-0019 维度


def test_legacy_dimensions_rejected_with_replacement(db):
    """旧维度名一律 422 且提示新名（不静默返回空分析/空列表）。"""
    cases = {"fund_type": "asset_class", "liquidity_rating": "liquidity",
             "account": "institution"}
    for legacy, replacement in cases.items():
        with pytest.raises(ApiError) as ei:
            allocation_service.create_allocation_target(db, AllocationTargetCreate(
                dimension=legacy, target_id="1", target_percent="10"))
        assert ei.value.status_code == 422
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert replacement in ei.value.message

        with pytest.raises(ApiError) as ei2:
            allocation_service.analyze_allocation(db, legacy, D)
        assert ei2.value.status_code == 422 and replacement in ei2.value.message

        with pytest.raises(ApiError) as ei3:
            allocation_service.get_allocation_targets(db, legacy)
        assert ei3.value.status_code == 422


def test_item_level_grouping_for_tree_and_enums(db):
    """项级聚合：类别树父节点＝子树和；流动性按 days 分档；风险 R1..R5 全枚举。"""
    from models import Channel

    parent = AssetClass(code="A", name="现金与等价物", parent_id=None,
                        level=1, side_scope="asset")
    db.add(parent)
    db.flush()
    demand = AssetClass(code="A1", name="活期存款", parent_id=parent.id,
                        level=2, side_scope="asset")
    fund = AssetClass(code="A2", name="货币基金", parent_id=parent.id,
                      level=2, side_scope="asset")
    db.add_all([demand, fund])
    member = _mk_member(db)
    bank = _mk_institution(db, "示例银行")
    broker = _mk_institution(db, "示例券商")
    app = Channel(name="示例App")
    db.add(app)
    db.commit()

    _mk_item(db, "示例-活期", 600, cls=demand, institution=bank,
             member=member, liquidity_days=0, channel=app, risk_level="R1")
    _mk_item(db, "示例-货基", 300, cls=fund, institution=broker,
             member=member, liquidity_days=1, risk_level="R2")

    tree = allocation_service.analyze_allocation(db, "asset_class", D)
    root = tree.items[0]
    assert root.id == str(parent.id) and root.actual_amount == Decimal("900")
    assert [child.name for child in root.children] == ["活期存款", "货币基金"]
    assert tree.total_amount == Decimal("900")

    liquidity = allocation_service.analyze_allocation(db, "liquidity", D)
    by_days = {i.id: i for i in liquidity.items}
    assert len(liquidity.items) == 8
    assert by_days["0"].name == "T+0 随时"
    assert by_days["0"].actual_amount == Decimal("600")
    assert by_days["1"].actual_amount == Decimal("300")
    assert by_days["7"].actual_amount == Decimal("0")

    risk = allocation_service.analyze_allocation(db, "risk", D)
    assert [i.id for i in risk.items] == ["R1", "R2", "R3", "R4", "R5"]
    assert risk.items[0].actual_amount == Decimal("600")

    channel = allocation_service.analyze_allocation(db, "channel", D)
    assert channel.items[0].id == str(app.id)
    assert channel.items[0].actual_amount == Decimal("600")


def test_target_reference_must_exist_in_its_dimension(db):
    """目标引用不合法（空集）→ 422：空集目标在页面上看起来「没有偏差」。"""
    with pytest.raises(ApiError) as ei:
        allocation_service.create_allocation_target(db, AllocationTargetCreate(
            dimension="liquidity", target_id="2", target_percent="10"))
    assert ei.value.status_code == 422

    with pytest.raises(ApiError) as ei2:
        allocation_service.create_allocation_target(db, AllocationTargetCreate(
            dimension="risk", target_id="R9", target_percent="10"))
    assert ei2.value.status_code == 422

    with pytest.raises(ApiError) as ei3:
        allocation_service.create_allocation_target(db, AllocationTargetCreate(
            dimension="institution", target_id="999", target_percent="10"))
    assert ei3.value.status_code == 422 and "不存在" in ei3.value.message


# ---------------------------------------------------------------- 阈值校验


def test_create_rejects_invalid_thresholds(db):
    institution = _mk_institution(db, "示例机构")
    cases = [
        ({"alert_threshold": "0"}, "alert_threshold"),
        ({"alert_threshold_red": "5"}, "alert_threshold_red"),
        ({"alert_threshold_red": "4"}, "alert_threshold_red"),
        ({"relative_threshold": "0"}, "relative_threshold"),
        ({"relative_threshold": "101"}, "relative_threshold"),
    ]
    for extra, field in cases:
        with pytest.raises(ApiError) as ei:
            allocation_service.create_allocation_target(db, AllocationTargetCreate(
                dimension="institution", target_id=str(institution.id),
                target_percent="20", **extra))
        assert ei.value.status_code == 422
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.details["fields"][0]["field"] == field


def test_update_merges_existing_yellow_before_validating(db):
    institution = _mk_institution(db, "示例机构")
    target = allocation_service.create_allocation_target(db, AllocationTargetCreate(
        dimension="institution", target_id=str(institution.id),
        target_percent="20",
        alert_threshold="10", alert_threshold_red="20"))
    with pytest.raises(ApiError) as ei:
        allocation_service.update_allocation_target(
            db, target.id, AllocationTargetUpdate(alert_threshold_red="8"))
    assert ei.value.details["fields"][0]["field"] == "alert_threshold_red"
    assert allocation_service.get_allocation_target(db, target.id).alert_threshold_red \
        == Decimal("20.00")


# ---------------------------------------------------------------- 审计


def _audits(db):
    return list(db.execute(select(AuditLog)
                           .where(AuditLog.entity == "allocation_target")
                           .order_by(AuditLog.id)).scalars().all())


def test_threshold_crud_writes_audit_before_after(db):
    institution = _mk_institution(db, "示例机构")
    target = allocation_service.create_allocation_target(db, AllocationTargetCreate(
        dimension="institution", target_id=str(institution.id),
        target_percent="20",
        alert_threshold="5", alert_threshold_red="10", relative_threshold="25"))
    allocation_service.update_allocation_target(
        db, target.id, AllocationTargetUpdate(alert_threshold="6"))
    allocation_service.delete_allocation_target(
        db, target.id, audit_reason="normalization")

    rows = _audits(db)
    assert [r.action for r in rows] == ["create", "update", "delete"]
    keys = {"target_percent", "alert_threshold", "alert_threshold_red",
            "relative_threshold"}
    create, update, delete = rows
    assert create.before_json is None
    assert keys <= set(json.loads(create.after_json))
    before = json.loads(update.before_json)
    after = json.loads(update.after_json)
    assert before["alert_threshold"] == "5.00" and after["alert_threshold"] == "6.00"
    assert before["alert_threshold_red"] == "10.00"
    assert delete.before_json is not None and delete.after_json is None


# ---------------------------------------------------------------- 幂等


@pytest.fixture()
def allocations_app(tmp_path):
    db_path = tmp_path / "alloc.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    spec = importlib.util.spec_from_file_location(
        "m0010", f"{_MIGRATIONS_DIR}/0010_idempotency.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    mod.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()

    Session = sessionmaker(bind=engine)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import allocations as allocations_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(allocations_router.router, prefix="/api/allocations")

    # 目标引用必须存在（ADR-0019：维度指向项级字典/枚举，不再接受任意字符串）
    session = Session()
    institution = Institution(name="示例机构", kind="bank")
    session.add(institution)
    session.commit()
    institution_id = str(institution.id)
    session.close()
    return app, Session, institution_id


def test_idempotent_target_create_replays_without_double_write(allocations_app):
    app, Session, institution_id = allocations_app
    client = TestClient(app)
    body = {"dimension": "institution", "target_id": institution_id,
            "target_percent": "20",
            "alert_threshold": "5", "alert_threshold_red": "10",
            "relative_threshold": "25"}
    r1 = client.post("/api/allocations/targets", json=body,
                     headers={"Idempotency-Key": "alloc-1"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["alert_threshold_red"] == "10.00"
    r2 = client.post("/api/allocations/targets", json=body,
                     headers={"Idempotency-Key": "alloc-1"})
    assert r2.status_code == 200 and r1.text == r2.text

    db = Session()
    assert len(db.execute(select(AllocationTarget)).scalars().all()) == 1
    assert len(_audits(db)) == 1
    db.close()

    r3 = client.post("/api/allocations/targets",
                     json={**body, "target_percent": "30"},
                     headers={"Idempotency-Key": "alloc-1"})
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_legacy_dimension_rejected_with_replacement(allocations_app):
    """HTTP 面：旧维度 422 并给出替换名（不允许静默落库成空集目标）。"""
    app, _Session, _institution_id = allocations_app
    client = TestClient(app)
    resp = client.post("/api/allocations/targets", json={
        "dimension": "account", "target_id": "1", "target_percent": "20"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "institution" in err["message"]


def test_http_invalid_threshold_returns_fields(allocations_app):
    app, _Session, institution_id = allocations_app
    client = TestClient(app)
    resp = client.post("/api/allocations/targets", json={
        "dimension": "institution", "target_id": institution_id,
        "target_percent": "20",
        "alert_threshold": "5", "alert_threshold_red": "5"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["fields"][0]["field"] == "alert_threshold_red"