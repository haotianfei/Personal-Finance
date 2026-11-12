"""三期-3 W2 信息集测试（spec §2.2/§5.1；ADR-0013 D4）。

覆盖：封闭键空间与 ≤40 条、payload_sha256 可重算、value_scaled、
通用 label（不含机构名）、与 metrics/analysis 对账、write-once 落库＋审计、
篡改可检出、无数据拒绝冻结。数据全部合成。

ADR-0019 迁移（D2/D12）：分类在**资产项**上（字典行 + 项级列），记录侧不再收
`attribute_ids`；覆盖度三条键（`coverage.ratio/.partial/.carried_max_days`）随
`data_coverage` 一起下线，信息集不再产出它们。
"""
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from models import (AdvisorInfoEntry, AdvisorInfoSet, AllocationTarget,
                    AssetClass, AuditLog, Institution, Member)
from services import allocation_service, analysis_service, classification_service
from services.advisor import info_set as info_set_service
from services.advisor.read import DbReadModel
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services import goal_service

M0019 = _load_script_module(f"{_MIGRATIONS_DIR}/0019_advisor.py")
TODAY = date.today()
PRIOR = TODAY - timedelta(days=30)


@pytest.fixture()
def advisor_env(tmp_path):
    path = str(tmp_path / "advisor_info.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0019.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()
    Session = sessionmaker(bind=engine)
    db = Session()
    yield path, db
    db.close()


# ---------------------------------------------------------------- 字典与项
# ADR-0019：旧 `Attribute(type='account'|'fund_type'|'owner')` 已被
# institutions / asset_classes / members 三张字典表取代。


def _institution(db, name="示例银行", kind="bank"):
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind=kind)
        db.add(row)
        db.commit()
    return row


def _class(db, name="示例货币基金"):
    row = db.execute(
        select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if row is None:
        row = AssetClass(code="T1", name=name, level=1, side_scope="any")
        db.add(row)
        db.commit()
    return row


def _member(db, name="本人"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role="self")
        db.add(row)
        db.commit()
    return row


def _mk_asset(db, name, amount, on_date, *, side="asset", klass=None,
              institution=None):
    """建记录 + 在其资产项上补全四维（项级分类是唯一真源）。"""
    row = create_asset(db, name, Decimal(str(amount)), on_date, side=side)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": (klass or _class(db)).id,
        "liquidity_days": 0,
        "institution_id": (institution or _institution(db)).id,
        "owner_id": _member(db).id,
    })
    return row


def _seed(db):
    account = _institution(db, "示例银行")
    klass = _class(db, "示例货币基金")
    _mk_asset(db, "示例-基金", "18000", PRIOR, klass=klass, institution=account)
    _mk_asset(db, "示例-基金", "20000", TODAY, klass=klass, institution=account)
    _mk_asset(db, "示例-贷", "5000", TODAY, side="liability")
    db.add(AllocationTarget(dimension="asset_class", target_id=str(klass.id),
                            target_percent=Decimal("60"),
                            alert_threshold=Decimal("5")))
    db.commit()
    goal, version, _ = goal_service.create_goal(
        db, name="示例目标", target_amount="30000.00",
        target_date=date(2030, 12, 31), target_rate_annual="0.05",
        reason="合成")
    return {"account": account, "asset_class": klass, "goal": goal,
            "version": version}


def test_builder_keys_values_and_scaled(advisor_env):
    path, db = advisor_env
    seed = _seed(db)
    data = info_set_service.build_info_set(DbReadModel(db))
    keys = {e["key"] for e in data["entries"]}
    assert data["as_of"] == TODAY
    assert data["builder_version"] == "advisor.info_set.v1"
    assert data["entry_count"] == len(data["entries"]) <= 40
    assert {"meta.anchor_date", "meta.schema_generation", "nw.total",
            "nw.assets", "nw.liabilities", "appreciation.total",
            "goal.target_amount", "goal.progress", "signal.B1.state"} <= keys
    # 覆盖度三条随 data_coverage 一起下线（ADR-0019 D12）——不再产出
    assert not [k for k in keys if k.startswith("coverage.")]
    assert f"alloc.asset_class.{seed['asset_class'].id}.deviation_pp" in keys
    assert data["goal_id"] == seed["goal"].id
    assert data["goal_version_id"] == seed["version"].id

    by_key = {e["key"]: e for e in data["entries"]}
    assert by_key["nw.total"]["value"] == "15000.00"
    assert by_key["nw.total"]["value_scaled"] == "1.5"       # 万元/1 位
    assert by_key["nw.total"]["kind"] == "money"
    assert by_key["goal.progress"]["kind"] == "ratio"
    dev = by_key[f"alloc.asset_class.{seed['asset_class'].id}.deviation_pp"]
    assert dev["kind"] == "pp" and dev["value"] == "40.00"
    assert dev["unit"] == "pp"
    assert by_key[
        f"alloc.asset_class.{seed['asset_class'].id}.band"]["value"] == "red"
    # 通用 label 不含机构名（隔离纪律）
    assert all(seed["account"].name not in e["label"] for e in data["entries"])


def test_payload_sha256_recomputable_and_canonical(advisor_env):
    path, db = advisor_env
    _seed(db)
    data = info_set_service.build_info_set(DbReadModel(db))
    assert data["payload_sha256"] == info_set_service.payload_sha256(
        data["entries"])
    # 顺序无关（按 key 排序）
    shuffled = list(reversed(data["entries"]))
    assert info_set_service.payload_sha256(shuffled) == data["payload_sha256"]
    canonical = info_set_service.canonical_entries(data["entries"])
    assert canonical == json.dumps(
        sorted(json.loads(canonical), key=lambda e: e["key"]),
        ensure_ascii=False, separators=(",", ":"))


def test_info_set_reconciles_with_metric_and_allocation(advisor_env):
    path, db = advisor_env
    seed = _seed(db)
    data = info_set_service.build_info_set(DbReadModel(db))
    by_key = {e["key"]: e for e in data["entries"]}

    total, assets, liabilities = analysis_service._sum_as_of(db, TODAY)
    assert Decimal(by_key["nw.total"]["value"]) == total
    assert Decimal(by_key["nw.assets"]["value"]) == assets
    assert Decimal(by_key["nw.liabilities"]["value"]) == liabilities

    analysis = allocation_service.analyze_allocation(db, "asset_class", TODAY)
    item = next(i for i in analysis.items if i.target_percent is not None)
    dev = by_key[f"alloc.asset_class.{seed['asset_class'].id}.deviation_pp"]
    assert Decimal(dev["value"]) == Decimal(item.deviation).quantize(
        Decimal("0.01"))

    progress = goal_service.get_goal_progress(db, seed["goal"].id)
    assert Decimal(by_key["goal.progress"]["value"]) == Decimal(
        progress["progress"])
    # 达成度不再携带覆盖度注记（分母已不存在）
    assert not [g for g in progress["gates"]
                if g["code"] == "DATA_MISSING_CARRY_FORWARD"]


def test_freeze_persists_write_once_and_audits(advisor_env):
    path, db = advisor_env
    _seed(db)
    read = DbReadModel(db)
    frozen = info_set_service.freeze_info_set(db, read)
    assert frozen["id"] and frozen["entry_count"] == len(frozen["entries"])

    row = db.get(AdvisorInfoSet, frozen["id"])
    assert row.payload_sha256 == frozen["payload_sha256"]
    assert db.query(AdvisorInfoEntry).filter_by(
        info_set_id=row.id).count() == row.entry_count

    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "advisor_info_set", AuditLog.action == "freeze")
    ).scalars().all()
    assert len(audits) == 1
    after = json.loads(audits[0].after_json)
    assert after["entry_count"] == row.entry_count
    assert after["payload_sha256"] == row.payload_sha256

    loaded = info_set_service.load_info_set(db, row.id)
    assert loaded["payload_ok"] is True and len(loaded["entries"]) == row.entry_count

    # write-once：触发器拒改条目
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE advisor_info_entries SET value='0.00' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM advisor_info_entries WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE advisor_info_sets SET entry_count=99 WHERE id=1")
    conn.close()


def test_tampered_entry_detected(advisor_env):
    path, db = advisor_env
    _seed(db)
    frozen = info_set_service.freeze_info_set(db, DbReadModel(db))
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS advisor_info_entries_no_update")
    conn.execute("UPDATE advisor_info_entries SET value='999.00' WHERE id=1")
    conn.commit()
    conn.close()
    loaded = info_set_service.load_info_set(db, frozen["id"])
    assert loaded["payload_ok"] is False


def test_freeze_without_data_rejected(tmp_path):
    path = str(tmp_path / "empty.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0019.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()
    Session = sessionmaker(bind=engine)
    db = Session()
    with pytest.raises(ValueError) as ei:
        info_set_service.freeze_info_set(db, DbReadModel(db))
    assert "no_data" in str(ei.value)
    db.close()


def test_alloc_label_is_generic(advisor_env):
    """配置行 label 只放通用类目名：机构名/账户名不得进 label（隔离纪律）。"""
    path, db = advisor_env
    _seed(db)
    inst = _institution(db, "示例证券", kind="broker")
    _mk_asset(db, "示例证券-股票", "1000", TODAY, institution=inst)
    db.add(AllocationTarget(dimension="institution", target_id=str(inst.id),
                            target_percent=Decimal("10"),
                            alert_threshold=Decimal("5")))
    db.commit()
    data = info_set_service.build_info_set(DbReadModel(db))
    labels = [e["label"] for e in data["entries"]
              if e["key"].startswith("alloc.institution.")]
    assert labels and all(label == "机构·档位" for label in labels)
    assert all("示例证券" not in e["label"] for e in data["entries"])