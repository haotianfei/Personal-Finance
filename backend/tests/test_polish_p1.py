"""三期收尾 P1 写侧正确性测试（导入归属/保留名/删除保护/主目标/上限/0022）。"""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from errors import ApiError
from models import (Asset, AssetItem, AssetItemOwnerChange, AuditLog, Goal,
                    Member)
from routers import attributes as attributes_router
from services import (asset_owner_service, asset_service, classification_service,
                      goal_service, import_service)
from services.advisor import service as advisor_service
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

from conftest import legacy_eav_tables

M0022 = _load_script_module(
    f"{_MIGRATIONS_DIR}/0022_owner_import_backfill.py")
TODAY = date.today()

CSV_OWNER = (
    "asset_date,asset_name,amount,side,asset_class,liquidity_days,institution,"
    "channel,owner,risk_level,tags\n"
    "2026-01-01,示例-导入股,1000,asset,A1,1,示例银行,,示例成员,,\n"
)
CSV_UNKNOWN_OWNER = CSV_OWNER.replace("示例成员", "不存在的成员")


class FakeGateway:
    def complete_json(self, *, prompt, schema, model=None):
        return {"title": "示例建议",
                "summary_template": "净资产 {{info.nw.total}}",
                "items": [{"seq": 1, "subject_kind": "narrative",
                           "subject_ref": None, "direction": "hold",
                           "rationale_template": "{{info.nw.total}}",
                           "info_refs": ["nw.total"]}]}


def _owner(db, name="示例成员"):
    """归属主体＝members 行（ADR-0019 D6：值域不再是 `attributes(type='owner')`）。"""
    member = Member(name=name, role="self")
    db.add(member)
    db.commit()
    return member


# ---------------------------------------------------------------- 导入归属
#
# ADR-0019 D1/D2/D6：归属是资产项上的 `owner_id`（值域 members.role），记录行与旧
# `attributes(type='owner')` 字典都不再承载它。CSV 的 owner 列按**成员名**解析后写项。


def _seed_import_dict(db):
    """导入用的最小字典：一个类别叶子 + 一家机构 + 一个成员。"""
    from models import AssetClass, Institution, Member
    root = AssetClass(code="A", name="示例类别", level=1, side_scope="any")
    db.add(root)
    db.flush()
    leaf = AssetClass(code="A1", name="示例叶子", parent_id=root.id, level=2,
                      side_scope="any")
    inst = Institution(name="示例银行", kind="bank")
    member = Member(name="示例成员", role="self")
    db.add_all([leaf, inst, member])
    db.commit()
    return leaf, inst, member


def test_import_writes_item_level_owner_not_record_level(db):
    """CSV owner → 资产项 `owner_id`（成员）；不写记录级属性、不建 EAV owner 字典。"""
    _leaf, _inst, member = _seed_import_dict(db)
    batch = import_service.import_csv_records(db, CSV_OWNER, "owner.csv", "skip", {})
    assert batch.warnings == []

    asset = db.query(Asset).filter_by(asset_name="示例-导入股").one()
    item = db.get(AssetItem, asset.asset_item_id)
    assert item.owner_id == member.id                 # 归属在项上
    assert item.status == "active"                    # 四维补齐 → 转正

    # 记录级不写任何属性（含 owner）：0027 已把这两张表物理删除，
    # 断言从「旧表零写入」升级为「旧表已不存在」——记录级分类无处可写即无写路径。
    assert legacy_eav_tables(db) == set()
    # 归属变更史必须落一行：`asset_items.owner_id` 是唯一真相源，
    # `asset_item_owner_changes` 是 append-only 审计（列 + 史同事务，ADR-0020 D3）。
    # 修复前这里断言 == 0，编码的是「导入写列不写史」的缺陷口径。
    assert db.execute(select(func.count()).select_from(AssetItemOwnerChange)).scalar() == 1


def test_import_unknown_owner_warns_and_does_not_invent_member(db):
    """成员名查不到 → 只记 warning 并留空（draft 允许缺维），绝不静默新建成员。"""
    from models import Member
    _seed_import_dict(db)
    batch = import_service.import_csv_records(db, CSV_UNKNOWN_OWNER, "bad.csv", "skip", {})

    assert [(w["field"], w["reason"]) for w in batch.warnings] == \
        [("owner", "owner_unknown")]
    assert db.query(Member).filter_by(name="不存在的成员").first() is None
    asset = db.query(Asset).filter_by(asset_name="示例-导入股").one()
    item = db.get(AssetItem, asset.asset_item_id)
    assert item.owner_id is None
    assert item.status == "draft"


# ---------------------------------------------------------------- 保留名路由（已随 ADR-0019 退役）
#
# 「家庭池」保留名与 EAV 属性字典一起下线（D6 / D2）：值域改成 `members.role`，
# 归属主体不再存真人姓名，也就不存在「与合成归属同名」的冲突。旧 attributes 端点
# 全部 410（EAV_ATTRIBUTES_REMOVED）。服务层的保留名守卫（import_service /
# asset_service 里对旧 owner 字典的写入）仍由上面两个用例覆盖。


def test_eav_attribute_routes_are_gone(db):
    """`/api/attributes` 整组 410 + 指路，不再能凭空造分类节点。"""
    for call in (lambda: attributes_router.create_attribute(
                     {"name": "示例类型", "type": "fund_type"}),
                 lambda: attributes_router.update_attribute(1, {"name": "示例类型"}),
                 lambda: attributes_router.delete_attribute(1),
                 lambda: attributes_router.get_attribute(1),
                 lambda: attributes_router.list_attributes()):
        with pytest.raises(ApiError) as ei:
            call()
        assert ei.value.status_code == 410
        assert ei.value.code == "EAV_ATTRIBUTES_REMOVED"
        assert ei.value.details["use"].startswith("/api/classification/")


def test_asset_owners_router_delegates_to_members(db):
    """`/api/asset-owners` 转接 members：role 由服务层校验，返回值带回 role。"""
    from routers import asset_owners

    created = asset_owners.create_asset_owner(
        {"name": "示例成员甲", "role": "self"}, db)
    assert (created["name"], created["role"]) == ("示例成员甲", "self")

    with pytest.raises(ApiError) as ei:            # 缺 role 不再有静默默认值
        asset_owners.create_asset_owner({"name": "示例成员乙"}, db)
    assert ei.value.status_code == 422


# ---------------------------------------------------------------- 删除保护


def _asset(db, name="示例-资产"):
    return create_asset(db, name, Decimal("100"), TODAY)


def test_delete_member_blocked_by_item_history(db):
    """成员删除保护＝members 的两处引用（`asset_items.owner_id` + 归属变更史）。

    旧 `asset_service.delete_attribute` 已被 0027 前的清理一并删除：删除保护归
    `classification_service.delete_member` 单点实现（`/api/asset-owners` 与
    `/api/classification/members` 共用），EAV 路由仍是 410。
    """
    member = _owner(db)
    asset = _asset(db)
    asset_owner_service.set_item_owner(
        db, asset.asset_item_id, "member", member.id, reason="合成",
        audit_reason="evolution")
    db.commit()
    with pytest.raises(ApiError) as ei:
        classification_service.delete_member(db, member.id,
        reason="normalization")
    assert ei.value.status_code == 409
    assert ei.value.code == "DICT_IN_USE"
    assert "引用" in ei.value.message
    # 路由已下线（分类改挂资产项）
    with pytest.raises(ApiError) as gone:
        attributes_router.delete_attribute(member.id)
    assert gone.value.status_code == 410


def test_delete_member_blocked_by_item_owner_column(db):
    """项级当前归属列（真相源）也计入引用：只擦史不擦列不算可删。"""
    member = _owner(db)
    asset = _asset(db)
    db.get(AssetItem, asset.asset_item_id).owner_id = member.id
    db.commit()
    with pytest.raises(ApiError) as ei:
        classification_service.delete_member(db, member.id,
        reason="normalization")
    assert ei.value.status_code == 409
    assert ei.value.code == "DICT_IN_USE"


def test_delete_unreferenced_member_succeeds(db):
    member = _owner(db)
    assert classification_service.delete_member(db, member.id,
        reason="normalization") is None
    assert db.get(Member, member.id) is None


# ---------------------------------------------------------------- 主目标守卫


def _member(db, name="示例成员"):
    """成员目标的范围主体：`goals.scope_owner_id` 0027 起外键指 `members.id`。"""
    member = Member(name=name, role="self")
    db.add(member)
    db.commit()
    return member


def test_mark_primary_skips_member_goal(db):
    owner = _member(db)
    member = Goal(name="成员目标", scope_kind="member",
                  scope_owner_id=owner.id, is_primary=0, created_at="t")
    db.add(member)
    db.commit()
    assert goal_service._mark_primary(db, member) is None
    db.refresh(member)
    assert member.is_primary == 0
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.action == "set_primary")).scalar() == 0


# ---------------------------------------------------------------- 自动触发上限


def test_auto_trigger_caps_three_and_audits(db, monkeypatch):
    settings = advisor_service._ensure_settings(db)
    settings.auto_enabled = 1
    settings.last_auto_at = None
    db.commit()
    candidates = [{"trigger_kind": "sched", "trigger_ref": f"sched:{i}",
                   "dedupe_key": f"sched:{i}"} for i in range(5)]
    monkeypatch.setattr(advisor_service, "auto_trigger_candidates",
                        lambda _db, _today=None: candidates)
    created = advisor_service.run_auto(db, gateway=FakeGateway())
    assert len(created) == 3
    audit = db.execute(select(AuditLog).where(
        AuditLog.entity == "advisor_auto_trigger",
        AuditLog.action == "limit")).scalars().all()
    assert len(audit) == 1
    import json
    after = json.loads(audit[0].after_json)
    assert after["candidates"] == 5 and after["limit"] == 3
    assert after["skipped"] == ["sched:3", "sched:4"]


# ---------------------------------------------------------------- 0022 迁移


def _legacy_db(path, conflict=False):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE asset_items (id INTEGER PRIMARY KEY);"
        "CREATE TABLE attributes (id INTEGER PRIMARY KEY, name TEXT, type TEXT);"
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_item_id INTEGER,"
        " asset_date DATE, superseded_by INTEGER);"
        "CREATE TABLE asset_attributes (asset_id INTEGER, attribute_id INTEGER);"
        "CREATE TABLE asset_item_owner_changes (id INTEGER PRIMARY KEY,"
        " asset_item_id INTEGER NOT NULL, owner_kind TEXT NOT NULL,"
        " owner_id INTEGER, effective_date DATE NOT NULL, reason TEXT,"
        " origin TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE audit_log (occurred_at TEXT, actor TEXT, entity TEXT,"
        " action TEXT, row_id INTEGER, before_json TEXT, after_json TEXT,"
        " evidence_ref INTEGER, request_id TEXT, idempotency_key TEXT,"
        " prev_hash TEXT);")
    conn.executescript(
        "INSERT INTO asset_items VALUES (1);"
        "INSERT INTO asset_items VALUES (2);"
        "INSERT INTO attributes VALUES (10,'示例成员','owner');"
        "INSERT INTO attributes VALUES (11,'示例成员2','owner');"
        "INSERT INTO assets VALUES (100,1,'2026-01-01',NULL);"
        "INSERT INTO asset_attributes VALUES (100,10);")
    if conflict:
        conn.execute("INSERT INTO assets VALUES (200,2,'2026-01-01',NULL)")
        conn.execute("INSERT INTO asset_attributes VALUES (200,10)")
        conn.execute("INSERT INTO asset_attributes VALUES (200,11)")
    conn.commit()
    return conn


def _run(conn):
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        M0022.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def test_0022_backfills_item_level_and_double_run(tmp_path):
    path = str(tmp_path / "m.db")
    conn = _legacy_db(path)
    _run(conn)
    rows = conn.execute(
        "SELECT asset_item_id, owner_kind, owner_id, origin, reason"
        " FROM asset_item_owner_changes").fetchall()
    assert rows == [(1, "member", 10, "migration", "import-backfill")]
    audit = conn.execute(
        "SELECT actor, after_json FROM audit_log"
        " WHERE entity='schema' AND action='owner_backfill'").fetchall()
    assert len(audit) == 1 and audit[0][0] == "system"
    import json
    after = json.loads(audit[0][1])
    assert after == {"candidates": 1, "backfilled": 1, "conflicts": 0,
                     "conflict_item_ids": [], "origin": "migration",
                     "reason": "import-backfill"}

    _run(conn)                                  # 双跑：无候选，无新行/审计
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_item_owner_changes").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='owner_backfill'"
    ).fetchone()[0] == 1
    conn.close()


def test_0022_conflict_is_read_only_and_reported(tmp_path):
    path = str(tmp_path / "c.db")
    conn = _legacy_db(path, conflict=True)
    _run(conn)
    rows = conn.execute(
        "SELECT asset_item_id, owner_id FROM asset_item_owner_changes"
        " ORDER BY asset_item_id").fetchall()
    assert rows == [(1, 10)]                     # item2 冲突不写
    import json
    after = json.loads(conn.execute(
        "SELECT after_json FROM audit_log WHERE action='owner_backfill'"
    ).fetchone()[0])
    assert after["candidates"] == 2 and after["backfilled"] == 1
    assert after["conflicts"] == 1 and after["conflict_item_ids"] == [2]
    conn.close()


def test_0022_no_candidates_noop(tmp_path):
    path = str(tmp_path / "e.db")
    conn = sqlite3.connect(path)
    _run(conn)                                   # 无业务表：空跑成功
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='asset_item_owner_changes'"
    ).fetchone() is None
    conn.close()