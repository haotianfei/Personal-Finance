"""当前值读模型 / 就地写（ADR-0016，spec §4.1/§4.2）。

覆盖：估值日距今（valuation_date / valuation_age_days / valuation_stale 90 天阈值，
旧三态 freshness 已随 ADR-0019 D12 删除）、include_archived、as_of 与 is_latest、
auto 建行（模板复制）、auto 命中同日行 → corrected、correct_only 无行 → 409、
金额不变 → unchanged（不建行不写审计）、superseded 行不入最新值、幂等回放。
数据一律合成（示例- 前缀、虚构机构、12000.00 式金额）。
"""
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import (Asset, AssetClass, AssetItem, AuditLog, Institution,
                    Member, Tag)
from routers import assets as assets_router
from services import asset_service, classification_service
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware

from conftest import legacy_eav_tables

M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
TODAY = date.today()


# ---------------------------------------------------------------- 分类字典（项级）

def _leaf_class(db, name="示例货币基金"):
    """建一个叶子类别（side_scope=any，资产/负债两侧都能挂）。"""
    node = AssetClass(code=f"T{name[:1]}{db.execute(select(func.count()).select_from(AssetClass)).scalar() + 1}",
                      name=name, parent_id=None, level=1, side_scope="any")
    db.add(node)
    db.commit()
    return node.id


def _institution(db, name="示例银行"):
    row = db.execute(select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind="bank")
        db.add(row)
        db.commit()
    return row.id


def _member(db, name="本人"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role="self")
        db.add(row)
        db.commit()
    return row.id


def _tag(db, name):
    row = db.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
    if row is None:
        row = Tag(name=name, kind="free")
        db.add(row)
        db.commit()
    return row.id


@pytest.fixture()
def cur_app(tmp_path):
    path = str(tmp_path / "current.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0010.upgrade(conn)
    conn.execute("COMMIT")
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
    app.include_router(assets_router.router, prefix="/api/assets")
    return {"client": TestClient(app), "Session": Session}


def _seed(Session, name, amount, on_date, institution="示例银行", tags=(),
          liquidity_days=0):
    """经 ledger 单点建一条记录 + 项级分类；返回 {record_id, item_id}。

    ADR-0019：分类（机构/流动性/类别/归属/标签）在**资产项**上，记录只承载金额与
    日期。旧 fixture 用 `create_asset(attribute_ids=…)` 打记录级分类，已废弃。
    """
    db = Session()
    row = create_asset(db, name, Decimal(str(amount)), on_date)
    item_id = row.asset_item_id
    classification_service.set_classification(db, item_id, {
        "asset_class_id": _leaf_class(db),
        "liquidity_days": liquidity_days,
        "institution_id": _institution(db, institution),
        "owner_id": _member(db),
    })
    if tags:
        classification_service.set_tags(db, item_id, [_tag(db, t) for t in tags])
    out = {"record_id": row.id, "item_id": item_id}
    db.close()
    return out


def _make_empty_item(Session, name, status="draft"):  # ADR-0019
    db = Session()
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.commit()
    item_id = item.id
    db.close()
    return item_id


def _items_by_id(payload):
    return {i["item_id"]: i for i in payload["items"]}


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar()


# ------------------------------------------------------------------ 读模型


def test_current_fresh_item_shape(cur_app):
    seeded = _seed(cur_app["Session"], "示例银行-存款", "12000.00",
                   TODAY - timedelta(days=3), institution="示例银行",
                   tags=("示例标签",))
    resp = cur_app["client"].get("/api/assets/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["as_of"] == TODAY.isoformat()
    assert body["is_latest"] is True
    row = _items_by_id(body)[seeded["item_id"]]
    assert row["display_name"] == "示例银行-存款"
    assert row["record_id"] == seeded["record_id"]
    assert row["asset_date"] == (TODAY - timedelta(days=3)).isoformat()
    assert row["amount"] == "12000.00"          # 字符串小数（与 /api/assets 一致）
    assert isinstance(row["amount"], str)
    assert row["valuation_age_days"] == 3
    assert row["valuation_date"] == (TODAY - timedelta(days=3)).isoformat()
    assert row["valuation_stale"] is False
    assert row["side"] == "asset"
    assert row["item_status"] != "archived"     # draft 也是正常态（不是错误）
    # ADR-0019 契约：机构/渠道/类别/流动性/归属/风险全部取自资产项
    assert row["institution_name"] == "示例银行"
    assert row["asset_class_name"] == "示例货币基金"
    assert row["asset_class_code"].startswith("T")
    assert row["asset_class_path"].endswith("示例货币基金")
    assert row["liquidity_days"] == 0 and row["liquidity_label"] == "T+0 随时"
    assert row["owner_name"] == "本人" and row["owner_kind"] == "self"
    assert row["channel_id"] is None and row["channel_name"] is None
    assert row["risk_level"] is None
    assert row["classification_missing"] == []
    assert [t["name"] for t in row["tags"]] == ["示例标签"]
    # 旧 EAV 字段已移除
    assert "account_id" not in row and "account_name" not in row


def test_valuation_age_and_stale_threshold_90d(cur_app):
    """估值日距今：只给天数与一个 90 天阈值布尔（旧 30 天三态 freshness 已删）。"""
    on_date = TODAY - timedelta(days=45)
    seeded = _seed(cur_app["Session"], "示例基金-稳健", "8000.00", on_date)
    client = cur_app["client"]

    default = client.get("/api/assets/current").json()
    row = _items_by_id(default)[seeded["item_id"]]
    assert row["valuation_date"] == on_date.isoformat()
    assert row["valuation_age_days"] == 45
    assert row["valuation_stale"] is False       # 45 天不再是「陈旧」（阈值 90）
    assert "freshness" not in row and "age_days" not in row

    past = client.get("/api/assets/current",
                      params={"as_of": (TODAY - timedelta(days=20)).isoformat()})
    assert past.status_code == 200
    body = past.json()
    assert body["is_latest"] is False
    assert body["as_of"] == (TODAY - timedelta(days=20)).isoformat()
    row = _items_by_id(body)[seeded["item_id"]]
    assert row["valuation_age_days"] == 25 and row["valuation_stale"] is False

    # 参考日推到 +46 天 → 距估值日 91 天 > 90：前端打标为陈旧
    later = client.get("/api/assets/current",
                       params={"as_of": (TODAY + timedelta(days=46)).isoformat()})
    row_91 = _items_by_id(later.json())[seeded["item_id"]]
    assert row_91["valuation_age_days"] == 91
    assert row_91["valuation_stale"] is True

    # 边界：正好 90 天不算陈旧（严格大于）
    edge = client.get("/api/assets/current",
                      params={"as_of": (TODAY + timedelta(days=45)).isoformat()})
    row_90 = _items_by_id(edge.json())[seeded["item_id"]]
    assert row_90["valuation_age_days"] == 90
    assert row_90["valuation_stale"] is False


def test_current_zero_rows_has_no_valuation_date(cur_app):
    item_id = _make_empty_item(cur_app["Session"], "示例-未录入")
    body = cur_app["client"].get("/api/assets/current").json()
    row = _items_by_id(body)[item_id]
    # 「没有记录」不再是第三种状态：估值日/天数/布尔一律 null
    assert row["valuation_date"] is None
    assert row["valuation_age_days"] is None
    assert row["valuation_stale"] is None
    assert "freshness" not in row
    assert row["record_id"] is None and row["asset_date"] is None
    assert row["amount"] is None
    assert row["tags"] == []
    # 分类来自资产项，与「有没有记录」无关；draft 项四维全缺
    assert row["classification_missing"] == [
        "asset_class_id", "liquidity_days", "institution_id", "owner_id"]
    assert row["asset_class_id"] is None and row["institution_id"] is None


def test_current_include_archived(cur_app):
    seeded = _seed(cur_app["Session"], "示例-已归档", "3000.00", TODAY)
    db = cur_app["Session"]()
    db.get(AssetItem, seeded["item_id"]).status = "archived"
    db.commit()
    db.close()
    client = cur_app["client"]

    default = client.get("/api/assets/current").json()
    assert seeded["item_id"] not in _items_by_id(default)

    with_archived = client.get("/api/assets/current",
                               params={"include_archived": "true"}).json()
    row = _items_by_id(with_archived)[seeded["item_id"]]
    assert row["item_status"] == "archived" and row["amount"] == "3000.00"


def test_superseded_row_not_picked_as_latest(cur_app):
    db = cur_app["Session"]()
    seeded = _seed(cur_app["Session"], "示例-活期", "100.00",
                   TODAY - timedelta(days=10))
    old = db.get(Asset, seeded["record_id"])
    new, _ = asset_service.correct_asset(db, old.id, amount="200.00",
        reason="correction")
    new_id = new.id
    db.close()

    body = cur_app["client"].get("/api/assets/current",
                                 params={"as_of": (TODAY - timedelta(days=10)).isoformat()}
                                 ).json()
    row = _items_by_id(body)[seeded["item_id"]]
    assert row["amount"] == "200.00"
    assert row["record_id"] == new_id != seeded["record_id"]
    assert row["valuation_age_days"] == 0 and row["valuation_stale"] is False


# ------------------------------------------------------------------ 就地写


def test_put_auto_creates_row_for_today(cur_app):
    seeded = _seed(cur_app["Session"], "示例银行-存款", "1000.00",
                   TODAY - timedelta(days=40), institution="示例银行")
    resp = cur_app["client"].put(
        f"/api/assets/current/{seeded['item_id']}", json={"amount": "12500.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "created"
    assert body["asset_date"] == TODAY.isoformat()
    assert body["amount"] == "12500.00"
    assert body["previous_amount"] is None and body["previous_record_id"] is None

    db = cur_app["Session"]()
    rows = db.execute(select(Asset).where(
        Asset.asset_item_id == seeded["item_id"]).order_by(Asset.id)).scalars().all()
    assert len(rows) == 2
    new = db.get(Asset, body["record_id"])
    assert new.asset_date == TODAY and new.amount == Decimal("12500.00")
    assert new.superseded_by is None and new.side == "asset"
    # ADR-0019：分类在资产项上，新行不再复制记录级属性（0027 起该载体表已删除）
    assert legacy_eav_tables(db) == set()
    assert new.asset_item_id == seeded["item_id"]
    assert ("create", new.id) in [
        (r.action, r.row_id) for r in db.execute(
            select(AuditLog).where(AuditLog.entity == "asset_snapshot")).scalars().all()]
    db.close()


def test_put_auto_corrects_existing_row_same_day(cur_app):
    seeded = _seed(cur_app["Session"], "示例基金-稳健", "1000.00", TODAY)
    # spec E1：走「更正」分支＝纠正类写入，必须带 reason；缺则 422 且零写入
    resp = cur_app["client"].put(
        f"/api/assets/current/{seeded['item_id']}", json={"amount": "1500.00"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"

    resp = cur_app["client"].put(
        f"/api/assets/current/{seeded['item_id']}", json={"amount": "1500.00"},
        params={"reason": "correction", "reason_note": "示例：定额调整"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "corrected"
    assert body["asset_date"] == TODAY.isoformat()
    assert body["amount"] == "1500.00"
    assert body["previous_amount"] == "1000.00"
    assert body["previous_record_id"] == seeded["record_id"]

    db = cur_app["Session"]()
    active = db.execute(select(func.count()).select_from(Asset).where(
        Asset.asset_item_id == seeded["item_id"],
        Asset.asset_date == TODAY,
        Asset.superseded_by.is_(None))).scalar()
    assert active == 1                                   # 同日同项唯一有效行
    assert _count(db, Asset) == 2                        # 旧行追加保留（superseded）
    db.close()

    current = _items_by_id(
        cur_app["client"].get("/api/assets/current").json())[seeded["item_id"]]
    assert current["amount"] == "1500.00"
    assert current["record_id"] == body["record_id"]


def test_put_correct_only_without_row_conflicts(cur_app):
    seeded = _seed(cur_app["Session"], "示例-存款2", "700.00",
                   TODAY - timedelta(days=5))
    resp = cur_app["client"].put(
        f"/api/assets/current/{seeded['item_id']}",
        json={"amount": "900.00", "mode": "correct_only"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_record_for_date"

    db = cur_app["Session"]()
    assert _count(db, Asset) == 1                        # 未落新行
    db.close()


def test_put_unchanged_writes_nothing(cur_app):
    seeded = _seed(cur_app["Session"], "示例-现金", "500.00", TODAY)
    db = cur_app["Session"]()
    rows_before, audit_before = _count(db, Asset), _count(db, AuditLog)
    db.close()

    resp = cur_app["client"].put(
        f"/api/assets/current/{seeded['item_id']}", json={"amount": "500.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "unchanged"
    assert body["record_id"] == seeded["record_id"]
    assert body["amount"] == "500.00"

    db = cur_app["Session"]()
    assert _count(db, Asset) == rows_before
    assert _count(db, AuditLog) == audit_before
    db.close()


def test_put_auto_first_record_uses_body_side(cur_app):
    item_id = _make_empty_item(cur_app["Session"], "示例-房贷")
    resp = cur_app["client"].put(
        f"/api/assets/current/{item_id}",
        json={"amount": "480000.00", "side": "liability"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "created" and body["previous_amount"] is None

    db = cur_app["Session"]()
    row = db.get(Asset, body["record_id"])
    assert row.asset_item_id == item_id
    assert row.asset_name == "示例-房贷"                 # 用 item 当前显示名
    assert row.side == "liability" and row.amount == Decimal("480000.00")
    assert row.asset_date == TODAY
    db.close()

    current = _items_by_id(
        cur_app["client"].get("/api/assets/current").json())[item_id]
    assert current["side"] == "liability" and current["amount"] == "480000.00"
    assert current["valuation_age_days"] == 0 and current["valuation_stale"] is False


def test_put_rejects_negative_amount_and_bad_side(cur_app):
    item_id = _make_empty_item(cur_app["Session"], "示例-校验")
    client = cur_app["client"]

    neg = client.put(f"/api/assets/current/{item_id}", json={"amount": "-1.00"})
    assert neg.status_code == 422
    assert neg.json()["error"]["details"]["fields"][0]["field"] == "amount"

    bad_side = client.put(f"/api/assets/current/{item_id}",
                          json={"amount": "1.00", "side": "weird"})
    assert bad_side.status_code == 400
    assert bad_side.json()["error"]["code"] == "INVALID_SIDE"

    missing_item = client.put("/api/assets/current/999999",
                              json={"amount": "1.00"})
    assert missing_item.status_code == 404


def test_put_idempotency_key_replays(cur_app):
    seeded = _seed(cur_app["Session"], "示例-理财", "2000.00", TODAY)
    url = f"/api/assets/current/{seeded['item_id']}"
    headers = {"Idempotency-Key": "cur-1"}
    client = cur_app["client"]

    params = {"reason": "correction"}
    first = client.put(url, json={"amount": "2100.00"}, headers=headers,
                       params=params)
    second = client.put(url, json={"amount": "2100.00"}, headers=headers,
                        params=params)
    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert first.json()["kind"] == "corrected"

    db = cur_app["Session"]()
    # 同键同体只落一次（首次 correct；回放不再追加行）
    assert db.execute(select(func.count()).select_from(Asset).where(
        Asset.asset_item_id == seeded["item_id"])).scalar() == 2
    db.close()