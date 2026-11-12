"""三期-4 W1 阈值史测试（spec §4.1；ADR-0014 D4/D5）。

覆盖：create/update/delete 单点追加 revision＋审计；父级下调子项自动调整也留史；
as-of 解析（早于首条=None、同日多改取最新、删除后史仍在）；revision 鸭子类型
复用 `resolve_band`；0020 回填/双跑幂等/触发器；旧当前值行为不变。数据合成。

维度口径按 ADR-0019：目标维度是项级的 `asset_class`（旧 `fund_type` 已 422），
故本文件的「类型」一律落到 `asset_classes` 行上。0020 迁移用例仍用旧字符串
（它们只测迁移脚本对历史表的原地行为，不经过服务层）。
"""
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from models import (AllocationTarget, AllocationTargetRevision, AssetClass, AuditLog)
from schemas import AllocationTargetCreate, AllocationTargetUpdate
from services import allocation_service, band_service
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0020 = _load_script_module(
    f"{_MIGRATIONS_DIR}/0020_allocation_target_revisions.py")
TODAY = date.today()


def _ensure_class(db, code, name=None):
    """按 code 取/建叶子类别（目标 target_id 必须指向真实存在的 asset_classes 行）。"""
    node = db.execute(
        select(AssetClass).where(AssetClass.code == code)).scalar_one_or_none()
    if node is None:
        node = AssetClass(code=code, name=name or f"示例类别{code}",
                          parent_id=None, level=1, side_scope="asset")
        db.add(node)
        db.commit()
    return node


def _create(db, target_id="Z1", dimension="asset_class", percent="60",
            yellow="5", red=None, relative=None, parent_id=None):
    if dimension == "asset_class":
        target_id = str(_ensure_class(db, str(target_id)).id)
    return allocation_service.create_allocation_target(db, AllocationTargetCreate(
        dimension=dimension, target_id=target_id, parent_id=parent_id,
        target_percent=Decimal(percent), alert_threshold=Decimal(yellow),
        alert_threshold_red=Decimal(red) if red else None,
        relative_threshold=Decimal(relative) if relative else None))


def _revisions(db, target_id=None):
    query = select(AllocationTargetRevision).order_by(AllocationTargetRevision.id)
    if target_id is not None:
        query = query.where(AllocationTargetRevision.allocation_target_id == target_id)
    return list(db.execute(query).scalars().all())


# ---------------------------------------------------------------- 写侧单点


def test_create_appends_manual_revision_and_audit(db):
    target = _create(db, yellow="5", red="10", relative="25")
    rows = _revisions(db, target.id)
    assert len(rows) == 1
    rev = rows[0]
    assert rev.op == "create" and rev.origin == "manual"
    assert rev.effective_date == TODAY
    assert rev.dimension == "asset_class" and rev.target_id == target.target_id
    assert rev.target_percent == Decimal("60.00")
    assert (rev.alert_threshold, rev.alert_threshold_red,
            rev.relative_threshold) == (Decimal("5.00"), Decimal("10.00"),
                                        Decimal("25.00"))
    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "allocation_target",
        AuditLog.action == "create")).scalars().all()
    assert len(audits) == 1


def test_update_appends_new_snapshot(db):
    target = _create(db)
    allocation_service.update_allocation_target(
        db, target.id, AllocationTargetUpdate(
            target_percent=Decimal("50"), alert_threshold=Decimal("6"),
            alert_threshold_red=Decimal("12"), relative_threshold=Decimal("30")))
    rows = _revisions(db, target.id)
    assert [r.op for r in rows] == ["create", "update"]
    latest = rows[-1]
    assert latest.target_percent == Decimal("50.00")
    assert latest.alert_threshold == Decimal("6.00")
    assert latest.alert_threshold_red == Decimal("12.00")
    assert latest.relative_threshold == Decimal("30.00")
    # 当前值仍是唯一真源
    current = allocation_service.get_allocation_target(db, target.id)
    assert current.target_percent == Decimal("50.00")
    assert current.alert_threshold == Decimal("6.00")


def test_delete_appends_revision_and_history_survives(db):
    target = _create(db, percent="30")
    target_id = target.id
    assert allocation_service.delete_allocation_target(
        db, target_id, audit_reason="normalization") is True
    assert allocation_service.get_allocation_target(db, target_id) is None
    rows = _revisions(db, target_id)
    assert [r.op for r in rows] == ["create", "delete"]
    assert rows[-1].target_percent == Decimal("30.00")
    # 史仍可查（append-only 表），但 as-of 解析无判据（不回退更早行）
    assert band_service.resolve_revision_as_of(db, target_id, TODAY) is None


def test_parent_decrease_leaves_child_revisions(db):
    parent = _create(db, target_id="1", percent="60")
    child = _create(db, target_id="2", percent="30", parent_id=parent.id)
    allocation_service.update_allocation_target(
        db, parent.id, AllocationTargetUpdate(target_percent=Decimal("30")))
    child_rows = _revisions(db, child.id)
    assert [r.op for r in child_rows] == ["create", "update"]
    assert child_rows[-1].target_percent == Decimal("15.00")
    assert child_rows[-1].reason == "auto_adjust:parent"
    parent_rows = _revisions(db, parent.id)
    assert parent_rows[-1].target_percent == Decimal("30.00")


# ---------------------------------------------------------------- as-of 解析


def test_resolve_returns_none_before_first_revision(db):
    assert band_service.resolve_revision_as_of(db, 999, TODAY) is None
    target = _create(db)
    assert band_service.resolve_revision_as_of(
        db, target.id, TODAY - timedelta(days=1)) is None


def test_resolve_same_day_latest_by_created_at_and_id(db):
    target = _create(db)
    db.add_all([
        AllocationTargetRevision(
            allocation_target_id=target.id, op="update", dimension="asset_class",
            target_id="1", parent_id=None, target_percent=Decimal("55"),
            alert_threshold=Decimal("8"), alert_threshold_red=None,
            relative_threshold=None, effective_date=TODAY,
            reason="second", origin="manual", created_at="2999-01-01T10:00:00"),
        AllocationTargetRevision(
            allocation_target_id=target.id, op="update", dimension="asset_class",
            target_id="1", parent_id=None, target_percent=Decimal("57"),
            alert_threshold=Decimal("9"), alert_threshold_red=None,
            relative_threshold=None, effective_date=TODAY,
            reason="third", origin="manual", created_at="2999-01-01T11:00:00"),
    ])
    db.commit()
    resolved = band_service.resolve_revision_as_of(db, target.id, TODAY)
    assert resolved.reason == "third" and resolved.target_percent == Decimal("57.00")
    # 早于同日某条不取该条（同日整体按最后一条）
    older = band_service.resolve_revision_as_of(
        db, target.id, TODAY - timedelta(days=1))
    assert older is None


def test_resolve_picks_as_of_date_then_latest(db):
    target = _create(db, yellow="5")
    db.add(AllocationTargetRevision(
        allocation_target_id=target.id, op="update", dimension="asset_class",
        target_id="1", parent_id=None, target_percent=Decimal("60"),
        alert_threshold=Decimal("7"), alert_threshold_red=Decimal("14"),
        relative_threshold=Decimal("25"), effective_date=date(2027, 1, 10),
        reason="jan", origin="manual", created_at="2027-01-10T09:00:00"))
    db.add(AllocationTargetRevision(
        allocation_target_id=target.id, op="update", dimension="asset_class",
        target_id="1", parent_id=None, target_percent=Decimal("60"),
        alert_threshold=Decimal("9"), alert_threshold_red=Decimal("18"),
        relative_threshold=Decimal("25"), effective_date=date(2027, 3, 10),
        reason="mar", origin="manual", created_at="2027-03-10T09:00:00"))
    db.commit()
    # 初始 create 修订的 effective_date 是 date.today()，故不能用写死日期（时间炸弹）
    assert band_service.resolve_revision_as_of(db, target.id, TODAY).reason == "create"
    assert band_service.resolve_revision_as_of(
        db, target.id, date(2027, 2, 1)).reason == "jan"
    assert band_service.resolve_revision_as_of(
        db, target.id, date(2027, 4, 1)).reason == "mar"


def test_revision_duck_types_into_resolve_band(db):
    target = _create(db, percent="40", yellow="5", red="10", relative="25")
    rev = _revisions(db, target.id)[0]
    allocation_service.delete_allocation_target(  # 当前行消失
        db, target.id, audit_reason="normalization")

    band = band_service.resolve_band(rev)
    assert band["threshold"] == "5.00" and band["red"] == "10.00"
    assert band["components"]["rel_applicable"] is True
    assert band_service.evaluate(Decimal("12.3"), band) == "red"


# ---------------------------------------------------------------- 迁移 0020


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE allocation_targets ("
        " id INTEGER PRIMARY KEY, dimension TEXT NOT NULL,"
        " target_id TEXT NOT NULL, parent_id INTEGER,"
        " target_percent NUMERIC(5,2) NOT NULL,"
        " alert_threshold NUMERIC(5,2) NOT NULL DEFAULT 5.00,"
        " alert_threshold_red NUMERIC(5,2), relative_threshold NUMERIC(5,2),"
        " created_at DATETIME, updated_at DATETIME)")
    conn.execute(
        "INSERT INTO allocation_targets (id, dimension, target_id,"
        " target_percent, alert_threshold, alert_threshold_red,"
        " relative_threshold, created_at, updated_at)"
        " VALUES (1,'fund_type','1',60,5,10,25,'2026-01-01 00:00:00',"
        "'2026-03-05 10:30:00')")
    conn.execute(
        "INSERT INTO allocation_targets (id, dimension, target_id,"
        " target_percent, alert_threshold, created_at, updated_at)"
        " VALUES (2,'account','9',20,5,'2026-01-01 00:00:00',"
        "'2026-02-01 08:00:00')")
    conn.commit()
    return conn


def _run(conn):
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        M0020.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def test_0020_backfill_double_run_idempotent(tmp_path):
    path = str(tmp_path / "rev.db")
    conn = _legacy_db(path)
    _run(conn)
    rows = conn.execute(
        "SELECT allocation_target_id, op, origin, effective_date"
        " FROM allocation_target_revisions"
        " ORDER BY allocation_target_id").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert all(r[1] == "backfill" and r[2] == "migration" for r in rows)
    assert [r[3] for r in rows] == ["2026-03-05", "2026-02-01"]
    first = conn.execute(
        "SELECT alert_threshold_red, relative_threshold"
        " FROM allocation_target_revisions WHERE allocation_target_id=1"
    ).fetchone()
    assert first == (10, 25)
    _run(conn)                                    # 双跑幂等
    assert conn.execute(
        "SELECT COUNT(*) FROM allocation_target_revisions").fetchone()[0] == 2
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_atr_target_eff" in idx
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE allocation_target_revisions SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM allocation_target_revisions")
    conn.close()


def test_0020_skips_targets_with_existing_history(tmp_path):
    path = str(tmp_path / "rev2.db")
    conn = _legacy_db(path)
    _run(conn)
    conn.execute(
        "INSERT INTO allocation_target_revisions"
        " (allocation_target_id, op, dimension, target_id, target_percent,"
        "  alert_threshold, effective_date, origin, created_at)"
        " VALUES (3,'create','fund_type','3',10,5,'2026-04-01','manual','t')")
    conn.execute(
        "INSERT INTO allocation_targets (id, dimension, target_id,"
        " target_percent, alert_threshold, created_at, updated_at)"
        " VALUES (3,'fund_type','3',10,5,'2026-04-01','2026-04-01')")
    conn.commit()
    _run(conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM allocation_target_revisions"
        " WHERE allocation_target_id=3").fetchone()[0]
    assert count == 1                              # 已有史不重复回填
    conn.close()


def test_0020_no_allocation_targets_table(tmp_path):
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    _run(conn)                                     # 无目标表：建史表、无行
    assert conn.execute(
        "SELECT COUNT(*) FROM allocation_target_revisions").fetchone()[0] == 0
    conn.close()