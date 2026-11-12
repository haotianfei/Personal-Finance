"""三期-1 C1「目标版本与达成度」后端测试（spec §8.1）。

覆盖：迁移幂等/CHECK/部分唯一索引、状态机（draft→active→archived）、唯一 active、
active/archived 内容不可变、版本号单调、主目标切换、成员 scope 422、审计落库
（链连续 prev_hash）、幂等重放、公式真值表、与 B1 同 as_of 对账、无目标/空库/
率不可得/as_of 边界。

数据一律合成（「示例-」、虚构金额与日期）。
"""
import importlib.util
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode
from models import AuditLog, Goal, GoalVersion, Institution
from services import (analysis_service, audit_chain, classification_service,
                      goal_service, metric_service)
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR
from services.idempotency_service import IdempotencyCaptureMiddleware

TODAY = date.today()
FUTURE = date(2030, 12, 31)


# ---------------------------------------------------------------- fixtures


def mk_asset(db, name, amount, on_date, attrs=(), side="asset"):
    """合成一条记录并在其资产项上打分类（ADR-0019：分类是项级属性）。

    `attrs` 保留旧位置参数形状：传 `Institution` 行即指定机构，缺省建示例字典。
    """
    row = create_asset(db, name, Decimal(str(amount)), on_date, side=side)
    institution = next((a for a in attrs if isinstance(a, Institution)), None)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": _mk_class(db).id,
        "liquidity_days": 0,
        "institution_id": (institution or _mk_account(db, "示例默认机构")).id,
        "owner_id": _mk_member(db).id,
    })
    return row


def _mk_class(db, name="示例类别"):
    from models import AssetClass
    node = db.execute(
        select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if node is None:
        node = AssetClass(code=f"T{name}", name=name, level=1, side_scope="any")
        db.add(node)
        db.commit()
    return node


def _mk_member(db, name="本人", role="self"):
    from models import Member
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role=role)
        db.add(row)
        db.commit()
    return row


def mk_goal(db, **overrides):
    kwargs = {
        "name": "示例目标 2030",
        "target_amount": "1000000.00",
        "target_date": FUTURE,
        "reason": "合成理由",
    }
    kwargs.update(overrides)
    return goal_service.create_goal(db, **kwargs)


def audits(db, entity=None, action=None):
    q = select(AuditLog)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if action:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars().all())


def items_of(db, as_of=None):
    return {i.id: i for i in metric_service.get_status(db, as_of).items}


def _load_0015():
    spec = importlib.util.spec_from_file_location(
        "m0015", f"{_MIGRATIONS_DIR}/0015_goals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 迁移


def test_0015_double_run_idempotent(tmp_path):
    module = _load_0015()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    for _ in range(2):
        conn.execute("BEGIN")
        module.upgrade(conn)
        conn.execute("COMMIT")
    objs = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name LIKE 'goals%'"
        " OR name LIKE 'goal_versions%' OR name LIKE 'uq_goal%'"
        " OR name LIKE 'ix_goal%'").fetchall()
    # 2 表 + 4 索引（goals 部分唯一 + versions 部分唯一 + 2 普通索引）不倍增
    assert len(objs) == 6
    conn.close()


def test_0015_constraints_and_partial_unique_active(tmp_path):
    module = _load_0015()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.execute("BEGIN")
    module.upgrade(conn)
    conn.execute("COMMIT")

    conn.execute("INSERT INTO goals (name, scope_kind, is_primary, created_at)"
                 " VALUES ('示例目标', 'family', 1, 't')")
    # is_primary 全表至多一条 1（部分唯一索引）
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goals (name, scope_kind, is_primary, created_at)"
                     " VALUES ('示例目标2', 'family', 1, 't')")
    # family 必须 owner 为空；member 必须带 owner
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goals (name, scope_kind, scope_owner_id, created_at)"
                     " VALUES ('示例目标3', 'family', 9, 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goals (name, scope_kind, scope_owner_id, created_at)"
                     " VALUES ('示例目标4', 'member', NULL, 't')")

    def insert_version(no, status, active=False):
        eff = "2026-01-01" if active else None
        criterion = '{"schema":1}' if active else None
        conn.execute(
            "INSERT INTO goal_versions (goal_id, version_no, status, target_amount,"
            " target_date, reason, effective_from, criterion_json, created_at)"
            " VALUES (1, ?, ?, 1000, '2030-12-31', 'r', ?, ?, 't')",
            (no, status, eff, criterion))

    insert_version(1, "active", active=True)
    # 每 Goal 至多一条 active（部分唯一索引）
    with pytest.raises(sqlite3.IntegrityError):
        insert_version(2, "active", active=True)
    # 版本号唯一
    with pytest.raises(sqlite3.IntegrityError):
        insert_version(1, "draft")
    # 金额 > 0 / active 必须有 effective_from 与 criterion_json
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goal_versions (goal_id, version_no, status,"
                     " target_amount, target_date, reason, created_at)"
                     " VALUES (1, 3, 'draft', 0, '2030-12-31', 'r', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goal_versions (goal_id, version_no, status,"
                     " target_amount, target_date, reason, created_at)"
                     " VALUES (1, 4, 'active', 1000, '2030-12-31', 'r', 't')")
    conn.close()


# ---------------------------------------------------------------- 状态机


def test_state_machine_draft_active_archived(db):
    goal, version, change = mk_goal(db, activate=False)
    assert goal_status(db, goal.id) == "draft"
    assert goal.is_primary == 0                       # draft-only 恒 0
    assert version.status == "draft"
    assert version.effective_from is None and version.criterion_json is None
    assert change["entity"] == "goal_version" and change["action"] == "create"
    assert change["after"]["status"] == "draft"

    version, superseded, change = goal_service.activate_version(db, goal.id, 1)
    assert superseded is None
    assert version.status == "active"
    assert version.effective_from == TODAY
    assert version.activated_at is not None
    assert change["action"] == "activate"
    snapshot = json.loads(version.criterion_json)
    assert snapshot["kind"] is None and snapshot["band_basis"] == "current"
    assert set(snapshot["partition"]) == {"risk_profile", "life_stage"}
    assert db.get(Goal, goal.id).is_primary == 1      # 全库无主 → 自动设主

    # 新版本激活 → 旧 active 归档（archived 同时承载「被取代」）
    v2, change2 = goal_service.create_version(
        db, goal.id, target_amount="1200000.00", target_date=FUTURE,
        reason="家庭共识调整", activate=True)
    assert v2.version_no == 2 and v2.status == "active"
    old = db.execute(select(GoalVersion).where(GoalVersion.version_no == 1)
                     ).scalar_one()
    assert old.status == "archived" and old.archived_at is not None
    assert change2["action"] == "create" and change2["after"]["status"] == "active"

    # 唯一 active
    active = db.execute(select(GoalVersion).where(
        GoalVersion.goal_id == goal.id,
        GoalVersion.status == "active")).scalars().all()
    assert len(active) == 1 and active[0].version_no == 2

    # 归档终态：所有未归档版本置 archived、主目标清零、不再接受新版本
    archived, change3 = goal_service.archive_goal(db, goal.id,
                                                  audit_reason="normalization")
    assert archived.is_primary == 0
    assert change3["action"] == "archive"
    assert goal_status(db, goal.id) == "archived"
    assert all(v.status == "archived" for v in goal_service._versions(db, goal.id))
    with pytest.raises(ApiError) as ei:
        goal_service.create_version(db, goal.id, target_amount="1.00",
                                    target_date=FUTURE, reason="再试")
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.status_code == 422


def goal_status(db, goal_id):
    return goal_service.goal_status(goal_service._versions(db, goal_id))


def test_version_numbers_monotonic(db):
    goal, _, _ = mk_goal(db, activate=False)
    numbers = [1]
    for expected in (2, 3):
        version, _ = goal_service.create_version(
            db, goal.id, target_amount="1000000.00", target_date=FUTURE,
            reason="草案迭代", activate=False)
        numbers.append(version.version_no)
        assert version.version_no == expected
    assert numbers == sorted(numbers) and len(set(numbers)) == 3


def test_activate_supersedes_and_conflicts(db):
    goal, _, _ = mk_goal(db, activate=False)
    goal_service.activate_version(db, goal.id, 1)
    # 已 active 再激活 → 409 GOAL_VERSION_CONFLICT
    with pytest.raises(ApiError) as ei:
        goal_service.activate_version(db, goal.id, 1)
    assert ei.value.code == ErrorCode.GOAL_VERSION_CONFLICT
    assert ei.value.status_code == 409
    assert ei.value.details["active_version_no"] == 1

    first_id = goal_service._versions(db, goal.id)[-1].id
    v2, _ = goal_service.create_version(
        db, goal.id, target_amount="1000000.00", target_date=FUTURE,
        reason="切换", activate=False)
    version, superseded, _ = goal_service.activate_version(db, goal.id, v2.version_no)
    assert superseded == first_id
    # 归档版本不可再激活（归档终态）
    with pytest.raises(ApiError) as ei:
        goal_service.activate_version(db, goal.id, 1)
    assert ei.value.code == ErrorCode.GOAL_VERSION_IMMUTABLE


def test_active_and_archived_are_immutable(db):
    goal, version, _ = mk_goal(db)
    before = goal_service.version_out(version)

    with pytest.raises(ApiError) as ei:
        goal_service.update_draft(db, goal.id, 1, {"target_amount": "1.00"})
    assert ei.value.code == ErrorCode.GOAL_VERSION_IMMUTABLE
    assert ei.value.details["status"] == "active"
    with pytest.raises(ApiError) as ei:
        goal_service.delete_draft(db, goal.id, 1, audit_reason="normalization")
    assert ei.value.code == ErrorCode.GOAL_VERSION_IMMUTABLE

    db.expire_all()
    assert goal_service.version_out(db.get(GoalVersion, version.id)) == before


def test_draft_can_be_edited_and_deleted_with_audit(db):
    goal, version, _ = mk_goal(db, activate=False)
    updated, change = goal_service.update_draft(
        db, goal.id, version.version_no,
        {"target_amount": "1500000.00", "risk_profile": "growth"})
    assert updated.target_amount == Decimal("1500000.00")
    assert change["action"] == "update"
    assert change["before"]["target_amount"] == "1000000.00"

    change = goal_service.delete_draft(db, goal.id, version.version_no,
                                       audit_reason="normalization")
    assert change["action"] == "delete" and change["after"] is None
    assert db.execute(select(GoalVersion).where(
        GoalVersion.goal_id == goal.id)).scalars().all() == []
    rows = audits(db, "goal_version", "delete")
    assert len(rows) == 1 and json.loads(rows[0].before_json)["version_no"] == 1


def test_primary_switch(db):
    a, _, _ = mk_goal(db, name="示例目标 A")
    b, _, _ = mk_goal(db, name="示例目标 B")
    assert db.get(Goal, a.id).is_primary == 1
    assert db.get(Goal, b.id).is_primary == 0          # 已有主目标 → 保持非主

    goal, change = goal_service.set_primary(db, b.id)
    assert change["entity"] == "goal" and change["action"] == "set_primary"
    db.expire_all()
    assert db.get(Goal, b.id).is_primary == 1
    assert db.get(Goal, a.id).is_primary == 0
    # 清零旧主也留审计
    cleared = [r for r in audits(db, "goal", "set_primary")
               if json.loads(r.after_json)["is_primary"] == 0]
    assert len(cleared) == 1

    draft_goal, _, _ = mk_goal(db, name="示例目标 C", activate=False)
    with pytest.raises(ApiError) as ei:
        goal_service.set_primary(db, draft_goal.id)
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "goal_id"


# ---------------------------------------------------------------- 校验


def test_member_scope_rejected(db):
    with pytest.raises(ApiError) as ei:
        goal_service.create_goal(
            db, name="示例成员目标", target_amount="1000.00",
            target_date=FUTURE, reason="合成",
            scope=SimpleNamespace(kind="member", owner_id=7))
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "scope.kind"
    assert "四期" in ei.value.details["fields"][0]["message"]
    assert db.execute(select(Goal)).scalars().all() == []


@pytest.mark.parametrize("overrides,field", [
    ({"target_amount": "0"}, "target_amount"),
    ({"target_amount": "-1"}, "target_amount"),
    ({"target_amount": "abc"}, "target_amount"),
    ({"target_date": TODAY}, "target_date"),
    ({"reason": "  "}, "reason"),
    ({"name": ""}, "name"),
    ({"risk_profile": "aggressive"}, "risk_profile"),
    ({"life_stage": "retired"}, "life_stage"),
])
def test_validation_errors_422_with_fields(db, overrides, field):
    with pytest.raises(ApiError) as ei:
        mk_goal(db, **overrides)
    assert ei.value.status_code == 422
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.details["fields"][0]["field"] == field
    assert db.execute(select(Goal)).scalars().all() == []


# ---------------------------------------------------------------- 审计


def test_every_write_leaves_audit_row(db):
    goal, version, _ = mk_goal(db)
    goal_service.rename_goal(db, goal.id, "示例目标改名")
    v2, _ = goal_service.create_version(
        db, goal.id, target_amount="1100000.00", target_date=FUTURE,
        reason="调整", activate=True)
    goal_service.set_primary(db, goal.id)
    goal_service.archive_goal(db, goal.id, audit_reason="normalization")

    assert [r.action for r in audits(db, "goal", "create")] == ["create"]
    assert [r.action for r in audits(db, "goal", "update")] == ["update"]
    assert len(audits(db, "goal", "archive")) == 1
    created = audits(db, "goal_version", "create")
    assert len(created) == 2
    after = json.loads(created[0].after_json)
    assert after["target_amount"] == "1000000.00"
    assert after["version_no"] == 1 and after["status"] == "active"
    assert len(audits(db, "goal_version", "archive")) == 2   # v1 被取代 + v2 归档
    rows = audits(db)
    assert rows[0].prev_hash == audit_chain.GENESIS
    assert all(cur.prev_hash == audit_chain.row_hash(prev.prev_hash, prev)
               for prev, cur in zip(rows, rows[1:]))
    # spec E2 ②：唯一的 system 行是「激活新版本时隐式归档旧版本」那条，其余都是 owner
    system_rows = [r for r in rows if r.actor == "system"]
    assert len(system_rows) == 1
    assert system_rows[0].action == "archive" and system_rows[0].entity == "goal_version"
    assert system_rows[0].reason == "normalization"
    assert json.loads(system_rows[0].after_json)["source"] == "activation"
    assert all(r.actor == "owner" for r in rows if r is not system_rows[0])
    # spec E1：owner 的纠正类行一律有 reason（不许遗留无理由改判）
    assert all(r.reason for r in rows
               if r.actor == "owner" and r.action in ("archive", "delete", "correct"))


# ---------------------------------------------------------------- 公式真值表


def _fake_version(amount, target):
    return SimpleNamespace(target_amount=Decimal(amount), target_date=target)


def test_progress_formula_truth_table():
    v = _fake_version("1000", date(2027, 10, 29))
    as_of = date(2026, 10, 29)

    normal = goal_service.compute_progress(Decimal("500"), v, as_of)
    assert str(normal["progress"]) == "0.5000"
    assert str(normal["gap"]) == "500.00"
    assert str(normal["required_annualized"]) == "1.0009"   # 金值：365 天 ≈ 1 年
    assert normal["remaining_days"] == 365
    assert normal["remaining_months"] == 12
    assert normal["gates"] == []
    assert normal["assumption"] == "no_future_contributions"

    achieved = goal_service.compute_progress(Decimal("1200"), v, as_of)
    assert str(achieved["progress"]) == "1.2000"            # 不封顶
    assert str(achieved["gap"]) == "-200.00"
    assert str(achieved["required_annualized"]) == "0.0000"

    zero = goal_service.compute_progress(Decimal("0"), v, as_of)
    assert str(zero["progress"]) == "0.0000"
    assert str(zero["gap"]) == "1000.00"
    assert zero["required_annualized"] is None
    assert zero["gates"][0]["code"] == "RATE_UNAVAILABLE"
    assert zero["gates"][0]["blocking"] is True
    assert zero["gates"][0]["scope"] == "goal_required_annualized"

    negative = goal_service.compute_progress(Decimal("-100"), v, as_of)
    assert str(negative["progress"]) == "-0.1000"
    assert str(negative["gap"]) == "1100.00"
    assert negative["required_annualized"] is None
    assert negative["gates"][0]["code"] == "RATE_UNAVAILABLE"

    expired = goal_service.compute_progress(Decimal("500"), v, date(2028, 1, 1))
    assert expired["remaining_days"] < 0
    assert expired["remaining_months"] == 0            # 夹到 0
    assert str(expired["progress"]) == "0.5000"
    assert expired["required_annualized"] is None
    assert expired["gates"][0]["code"] == "RATE_UNAVAILABLE"

    rounded = goal_service.compute_progress(Decimal("333.333"), v, as_of)
    assert str(rounded["progress"]) == "0.3333"
    assert str(rounded["gap"]) == "666.67"


def test_resolve_partition(db):
    _, version, _ = mk_goal(db, risk_profile="balanced",
                            life_stage="accumulation")
    partition = goal_service.resolve_partition(version)
    assert partition == {
        "goal_version_id": version.id,
        "risk_profile": "balanced",
        "life_stage": "accumulation",
        "band_basis": "current",
    }
    assert json.loads(version.criterion_json)["partition"] == {
        "risk_profile": "balanced", "life_stage": "accumulation"}


# ---------------------------------------------------------------- 达成度端点/状态面


def test_progress_and_metric_status_share_values_and_reconcile_b1(db):
    mk_asset(db, "示例-股", 1000, TODAY)
    mk_asset(db, "示例-债", 400, TODAY, side="liability")
    goal, version, _ = mk_goal(db, target_amount="1000.00")

    progress = goal_service.get_goal_progress(db, goal.id)
    assert progress["net_worth"] == "600.00"
    assert progress["gap"] == "400.00"
    assert progress["progress"] == "0.6000"
    assert progress["required_annualized"] is not None
    assert progress["assumption"] == "no_future_contributions"
    assert progress["band_basis"] == "current"
    assert progress["gates"] == []

    by_id = items_of(db)
    assert by_id["goal_progress"].value == progress["progress"]
    assert by_id["goal_gap"].value == progress["gap"]
    assert by_id["goal_required_annualized"].value == progress["required_annualized"]
    for metric_id in ("goal_progress", "goal_gap", "goal_required_annualized"):
        item = by_id[metric_id]
        assert item.grade is None                     # 无判据
        assert item.drill.target == "/planning/goals"
        assert item.details["goal_id"] == goal.id
        assert item.details["goal_version_id"] == version.id
        assert item.details["target_amount"] == "1000.00"
        assert item.details["net_worth"] == "600.00"

    # 同一 as_of 与 B1 净值对账
    summary = analysis_service.get_summary(db)
    assert by_id["net_worth"].value == f"{summary.net_worth:.2f}"
    assert Decimal(progress["net_worth"]) == Decimal(str(summary.net_worth))
    assert Decimal(by_id["goal_progress"].details["net_worth"]) == \
        Decimal(str(summary.net_worth))


def test_metric_details_contract_shape_and_formats(db):
    mk_asset(db, "示例-股", 900, TODAY)
    mk_goal(db, target_amount="1000.00")
    by_id = items_of(db)
    details = by_id["goal_progress"].details
    assert set(details) == {
        "goal_id", "goal_version_id", "version_no", "is_primary", "scope",
        "target_amount", "target_date", "target_rate_annual", "net_worth",
        "gap", "progress", "required_annualized", "remaining_days",
        "remaining_months", "assumption", "band_basis", "partition"}
    assert details["scope"] == {"kind": "family", "owner_id": None}
    assert details["target_date"] == FUTURE.isoformat()
    assert details["partition"] == {"risk_profile": None, "life_stage": None}
    assert details["is_primary"] is True
    assert by_id["goal_gap"].unit == "CNY"
    assert by_id["goal_progress"].unit == "ratio"
    assert by_id["goal_required_annualized"].unit == "ratio"
    # 字典：3 条 C1 指标；仅「所需年化」有判据（读时解析 target_rate_annual）
    dictionary = {i.id: i for i in metric_service.get_dictionary().items}
    for metric_id in ("goal_progress", "goal_gap", "goal_required_annualized"):
        assert dictionary[metric_id].domain == "C1"
        assert dictionary[metric_id].drill.target == "/planning/goals"
    assert dictionary["goal_progress"].criterion is None
    assert dictionary["goal_gap"].criterion is None
    assert dictionary["goal_required_annualized"].criterion.kind == \
        "dual_threshold_one_sided"


def test_no_goal_means_no_c1_items(db):
    mk_asset(db, "示例-股", 1000, TODAY)
    by_id = items_of(db)
    assert not [k for k in by_id if k.startswith("goal_")]
    assert len(by_id) == 9                             # B1-B3 + C2 维持原状（无覆盖度项）


def test_no_data_with_goal_is_blocked_but_present(db):
    goal, _, _ = mk_goal(db)
    status = metric_service.get_status(db)
    by_id = {i.id: i for i in status.items}
    for metric_id in ("goal_progress", "goal_gap", "goal_required_annualized"):
        item = by_id[metric_id]
        assert item.value is None
        assert item.grade.state == "gray"
        assert any(g.code == "NO_DATA" and g.blocking for g in item.gates)
    details = by_id["goal_progress"].details
    assert details["net_worth"] == "0.00"
    assert details["progress"] == "0.0000"
    assert details["gap"] == "1000000.00"
    assert details["required_annualized"] is None

    progress = goal_service.get_goal_progress(db, goal.id)
    assert progress["as_of"] == TODAY.isoformat()      # 空库按今日显示期数
    assert progress["meta"].snapshot_date is None
    codes = [(d.code, d.scope) for d in progress["meta"].degradations]
    assert ("NO_DATA", None) in codes
    assert ("RATE_UNAVAILABLE", "goal_required_annualized") in codes


def test_rate_unavailable_scoped_to_required_only(db):
    mk_asset(db, "示例-贷", 300, TODAY, side="liability")
    mk_goal(db, target_amount="1000.00")
    by_id = items_of(db)
    required = by_id["goal_required_annualized"]
    assert required.value is None
    assert any(g.code == "RATE_UNAVAILABLE" and g.blocking for g in required.gates)
    # 另两项仍有值，且无判据 → 非灰
    assert by_id["goal_progress"].value == "-0.3000"
    assert by_id["goal_gap"].value == "1300.00"
    assert by_id["goal_progress"].grade is None
    assert by_id["goal_gap"].grade is None
    codes = [(d.code, d.scope) for d in metric_service.get_status(db).meta.degradations]
    assert ("RATE_UNAVAILABLE", "goal_required_annualized") in codes


def test_achieved_goal_required_is_zero(db):
    mk_asset(db, "示例-股", 1500, TODAY)
    mk_goal(db, target_amount="1000.00")
    by_id = items_of(db)
    assert by_id["goal_required_annualized"].value == "0.0000"
    assert by_id["goal_required_annualized"].gates == []
    assert by_id["goal_progress"].value == "1.5000"
    assert by_id["goal_gap"].value == "-500.00"


def test_as_of_rules_and_effective_from(db):
    mk_asset(db, "示例-股", 1000, TODAY - timedelta(days=30))
    mk_asset(db, "示例-股", 1100, TODAY)
    goal, _, _ = mk_goal(db, target_amount="1000.00")

    # 默认 as_of=最新快照日（今日）→ C1 出现
    assert "goal_progress" in items_of(db)
    # as_of 早于生效日 → C1 不出现
    before = items_of(db, TODAY - timedelta(days=30))
    assert "goal_progress" not in before
    # 显式请求早于生效日 → 422（field=as_of）
    with pytest.raises(ApiError) as ei:
        goal_service.get_goal_progress(db, goal.id, TODAY - timedelta(days=30))
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "as_of"
    # as_of 越界 → 422
    with pytest.raises(ApiError) as ei:
        goal_service.get_goal_progress(db, goal.id, TODAY - timedelta(days=31))
    assert ei.value.code == ErrorCode.VALIDATION_ERROR

    # 生效日当日（= 最新快照日）可算；显式 as_of 等于快照日
    today_progress = goal_service.get_goal_progress(db, goal.id, TODAY)
    assert today_progress["net_worth"] == "1100.00"
    assert today_progress["as_of"] == TODAY.isoformat()


def _mk_account(db, name):
    """旧「账户」→ 机构（`institutions`）。`remind_enabled` 随 D12 一起消失。"""
    from models import Institution
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind="bank")
        db.add(row)
        db.commit()
    return row


# ---------------------------------------------------------------- 幂等


@pytest.fixture()
def goals_app(tmp_path):
    db_path = tmp_path / "goals.db"
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
    from routers import goals as goals_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(goals_router.router, prefix="/api/planning")
    return app, Session


def test_idempotent_replay_goal_create(goals_app):
    app, Session = goals_app
    client = TestClient(app)
    body = {
        "name": "示例幂等目标", "scope": {"kind": "family"},
        "target_amount": "1000000.00", "target_date": FUTURE.isoformat(),
        "reason": "合成幂等", "activate": True,
    }
    r1 = client.post("/api/planning/goals", json=body,
                     headers={"Idempotency-Key": "goal-1"})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/planning/goals", json=body,
                     headers={"Idempotency-Key": "goal-1"})
    assert r2.status_code == 200
    assert r1.text == r2.text                          # 逐字节回放

    db = Session()
    assert db.execute(select(Goal)).scalars().all().__len__() == 1
    assert len(audits(db, "goal_version", "create")) == 1
    db.close()

    r3 = client.post("/api/planning/goals", json={**body, "name": "示例另一个目标"},
                     headers={"Idempotency-Key": "goal-1"})
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_endpoints_and_error_codes(goals_app):
    app, Session = goals_app
    client = TestClient(app)
    body = {
        "name": "示例端点目标", "scope": {"kind": "member", "owner_id": 1},
        "target_amount": "1000.00", "target_date": FUTURE.isoformat(),
        "reason": "合成",
    }
    resp = client.post("/api/planning/goals", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "scope.kind"

    body["scope"] = {"kind": "family"}
    resp = client.post("/api/planning/goals", json=body)
    assert resp.status_code == 200, resp.text
    goal_id = resp.json()["goal"]["id"]

    assert client.get("/api/planning/goals").json()["total"] == 1
    assert client.get(f"/api/planning/goals/{goal_id}").json()["versions"][0]["version_no"] == 1

    resp = client.put(f"/api/planning/goals/{goal_id}/versions/1",
                      json={"target_amount": "2000.00"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "GOAL_VERSION_IMMUTABLE"

    resp = client.post(f"/api/planning/goals/{goal_id}/versions",
                       json={"target_amount": "2000.00",
                             "target_date": FUTURE.isoformat(),
                             "reason": "调高", "activate": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"]["version_no"] == 2

    # spec E3：归档是人的裁决，缺 reason → 422；带 reason 才 200
    resp = client.post(f"/api/planning/goals/{goal_id}/archive")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"

    resp = client.post(f"/api/planning/goals/{goal_id}/archive",
                       params={"reason": "normalization",
                               "reason_note": "目标周期结束"})
    assert resp.status_code == 200
    assert resp.json()["goal"]["status"] == "archived"

    resp = client.get(f"/api/planning/goals/{goal_id}/progress")
    assert resp.status_code == 422                      # 无 active 版本

    resp = client.get("/api/planning/goals/9999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"