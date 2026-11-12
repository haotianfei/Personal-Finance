"""审计理由**强制**（`.scratch/audit-reason-enforce/spec.md` E1–E3/E7，验收 1–8 后端侧）。

本文件是「可选 → 强制」翻转后的正面契约：

1. `record()` 单点：`actor='owner'` 的纠正类缺 reason → 422 `AUDIT_REASON_REQUIRED`
   且**零业务写入 + 零审计行**；`actor='system'` 不拦（spec E1）；
2. 12 个写端点（alert / allocation / backup×2 / 字典×6 / evidence×3 / goals×2）
   两态：不带 reason → 422 且原行仍在、审计行数不变；带 reason → 成功且审计行带
   reason（spec E3 验收 4/5/6）；
3. **自动路径改身份**（spec E2）：备份保留策略 job 的删除行
   `actor='system'` + `reason='normalization'` + `after.source='retention'`；
   `activate_version` 的隐式归档 `after.source='activation'`（后者在 test_goals 里也锁了）；
4. 策略选择逻辑（count/days/rollback 豁免/关掉即不动）与 scheduler job 注册。

数据全部合成（「示例-」、虚构金额），不触真实 data/。
"""
import os
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode
from models import (AssetClass, AssetItem, AuditLog, Institution, Member,
                    ScreenshotEvidence, SourceGroup)
from services import (alert_service, allocation_service, audit_service,
                      backup_service, classification_service, evidence_service,
                      goal_service)
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

TODAY = date.today()
FUTURE = TODAY + timedelta(days=365)
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """真实文件库 + 相关路由；DATABASE_PATH 指向沙箱（备份/凭证目录随之隔离）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "person_fin.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0010.upgrade(conn)                    # 幂等表（中间件依赖）
    conn.execute("COMMIT")
    conn.close()

    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import (alerts, allocations, asset_items, asset_owners, assets,
                         backup, classification, goals, imports, screenshots,
                         source_groups)

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(alerts.router, prefix="/api/alerts")
    app.include_router(allocations.router, prefix="/api/allocation")
    app.include_router(backup.router, prefix="/api/backup")
    app.include_router(imports.router, prefix="/api/imports")
    app.include_router(assets.router, prefix="/api/assets")
    app.include_router(classification.router, prefix="/api/classification")
    app.include_router(classification.item_router, prefix="/api/asset-items")
    app.include_router(asset_owners.router, prefix="/api/asset-owners")
    app.include_router(asset_items.router, prefix="/api/asset-items")
    app.include_router(screenshots.router, prefix="/api/screenshots")
    app.include_router(source_groups.router, prefix="/api/source-groups")
    app.include_router(goals.router, prefix="/api/planning")
    return SimpleNamespace(client=TestClient(app), Session=Session,
                           data=data_dir)


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _last(db, entity=None, action=None):
    q = select(AuditLog)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if action:
        q = q.where(AuditLog.action == action)
    return db.execute(q.order_by(AuditLog.id.desc())).scalars().first()


def _member(db, name="示例成员"):
    row = Member(name=name, role="self")
    db.add(row)
    db.commit()
    return row


def _item(db, name="示例-资产"):
    klass = db.execute(select(AssetClass).where(
        AssetClass.name == "示例类别")).scalar_one_or_none()
    if klass is None:
        klass = AssetClass(name="示例类别", code="T1", level=1, side_scope="any")
        db.add(klass)
        db.commit()
    inst = db.execute(select(Institution).where(
        Institution.name == "示例机构")).scalar_one_or_none()
    if inst is None:
        inst = Institution(name="示例机构", kind="bank")
        db.add(inst)
        db.commit()
    owner = db.execute(select(Member)).scalars().first() or _member(db)
    item = AssetItem(display_name=name, status="active",
                     asset_class_id=klass.id, liquidity_days=1,
                     institution_id=inst.id, owner_id=owner.id)
    db.add(item)
    db.commit()
    return item


# ---------------------------------------------------------------- 1. 单点（E1）


def test_record_owner_reason_required_matrix(api):
    """E1：owner + 纠正类 + 无 reason → 422；带 reason / system 均放行。"""
    db = api.Session()
    change = audit_service.make_change(
        "asset_item", 1, "delete", before={"status": "active"}, after=None)
    with pytest.raises(ApiError) as ei:
        audit_service.record(db, change)
    assert (ei.value.code, ei.value.status_code) == \
        (ErrorCode.AUDIT_REASON_REQUIRED, 422)
    db.rollback()
    assert _count(db) == 0

    audit_service.record(db, audit_service.make_change(
        "asset_item", 1, "delete", before={"status": "active"}, after=None,
        reason="normalization"), actor="system")
    db.commit()
    row = _last(db, "asset_item", "delete")
    assert row.actor == "system" and row.reason == "normalization"
    db.close()


def test_non_curation_action_needs_no_reason(api):
    """非纠正类（create/update/activate…）不受强制：本项目仍可无 reason 落行。"""
    db = api.Session()
    audit_service.record(db, audit_service.make_change(
        "asset_item", 1, "create", before=None, after={"status": "active"}))
    db.commit()
    assert _last(db, "asset_item", "create").reason is None
    db.close()


# ---------------------------------------------------------------- 2. 端点两态（E3）

def _expect_422(resp):
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"


def test_alert_delete_requires_reason(api):
    db = api.Session()
    owner = _member(db, "示例-预警归属")
    from schemas import AlertRuleCreate
    rule = alert_service.create_alert_rule(db, AlertRuleCreate(
        name="示例预警", dimension="owner", target_id=str(owner.id),
        period_type="month", compare_type="previous",
        amount_threshold=Decimal("1000.00"), direction="down"))
    rule_id = rule.id
    db.close()

    db = api.Session()
    before = _count(db)
    db.close()
    r = api.client.delete(f"/api/alerts/rules/{rule_id}")
    _expect_422(r)
    db = api.Session()
    assert _count(db) == before
    assert db.get(type(rule), rule_id) is not None      # 规则仍在
    db.close()

    r = api.client.delete(f"/api/alerts/rules/{rule_id}",
                          params={"reason": "normalization"})
    assert r.status_code == 200, r.text
    db = api.Session()
    row = _last(db, "alert", "delete")
    assert row.reason == "normalization" and row.actor == "owner"
    db.close()


def test_allocation_target_delete_requires_reason(api):
    db = api.Session()
    klass = db.execute(select(AssetClass)).scalars().first()
    if klass is None:
        klass = AssetClass(name="示例类别A", code="A1", level=1,
                           side_scope="any")
        db.add(klass)
        db.commit()
    from schemas import AllocationTargetCreate
    target = allocation_service.create_allocation_target(db, AllocationTargetCreate(
        dimension="asset_class", target_id=str(klass.id),
        target_percent=Decimal("30"), alert_threshold=Decimal("5")))
    target_id = target.id
    db.close()

    _expect_422(api.client.delete(f"/api/allocation/targets/{target_id}"))
    r = api.client.delete(f"/api/allocation/targets/{target_id}",
                          params={"reason": "normalization"})
    assert r.status_code == 200, r.text


def test_backup_delete_two_endpoints_ask_no_reason(api):
    """业主 2026-11-08 裁决：删备份**不索取理由**，服务端归一化 + 标触发源。

    这条是本文件里**唯一**不 422 的删除族：两个端点都不收 reason 参数，
    审计行仍是 `backup/delete`（离场桶），`reason='normalization'`、
    `after.source='manual_cleanup'`（与保留策略 job 的 `'retention'` 区分）。
    """
    backup_dir = api.data / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "backup_20260101_000000.zip").write_bytes(b"fake")
    (backup_dir / "backup_20260102_000000.zip").write_bytes(b"fake")

    r = api.client.delete("/api/backup/delete/backup_20260101_000000.zip")
    assert r.status_code == 200, r.text           # 不传 reason 也 200（不索取）
    assert not (backup_dir / "backup_20260101_000000.zip").exists()

    r = api.client.delete("/api/imports/backups/backup_20260102_000000.zip")
    assert r.status_code == 200, r.text
    assert not (backup_dir / "backup_20260102_000000.zip").exists()

    db = api.Session()
    rows = db.execute(select(AuditLog).where(
        AuditLog.entity == "backup", AuditLog.action == "delete")
    ).scalars().all()
    assert len(rows) == 2
    assert [r.reason for r in rows] == ["normalization", "normalization"]
    assert [r.actor for r in rows] == ["owner", "owner"]
    assert ['"source": "manual_cleanup"' in r.after_json for r in rows] == \
        [True, True]
    db.close()


def test_backup_delete_reason_passthrough_not_required_by_record(api):
    """确认「传参路径接上了」：服务端填的 reason 真被 `record()` 接受（不 422）。

    spec E7 的 KNOWN_DEBT 里曾有 `services/backup_service.py:delete`——本裁决后它
    不该缺 reason。若 `record()` 仍判定缺失，说明归一化没生效，这里会红。
    """
    backup_dir = api.data / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "backup_20260103_000000.zip").write_bytes(b"fake")
    db = api.Session()
    assert backup_service.delete_backup("backup_20260103_000000.zip", db=db) is True
    row = _last(db, "backup", "delete")
    assert row.reason == "normalization"
    assert '"source": "manual_cleanup"' in row.after_json
    db.close()


@pytest.mark.parametrize("path,payload", [
    ("/api/classification/asset-classes", {"name": "示例-待删类", "code": "Z1"}),
    ("/api/classification/institutions", {"name": "示例-待删行", "kind": "bank"}),
    ("/api/classification/channels", {"name": "示例-待删渠道"}),
    ("/api/classification/members", {"name": "示例-待删成员", "role": "self"}),
    ("/api/classification/tags", {"name": "示例-待删标签"}),
])
def test_dict_delete_endpoints_require_reason(api, path, payload):
    created = api.client.post(path, json=payload)
    assert created.status_code == 201, created.text
    obj_id = created.json()["id"]

    _expect_422(api.client.delete(f"{path}/{obj_id}"))
    db = api.Session()
    assert not db.execute(select(AuditLog).where(
        AuditLog.action == "delete")).scalars().all()
    db.close()

    r = api.client.delete(f"{path}/{obj_id}", params={"reason": "normalization"})
    assert r.status_code == 204, r.text
    db = api.Session()
    row = _last(db, "classification", "delete")
    assert row.row_id == obj_id and row.reason == "normalization"
    db.close()


def test_asset_owner_delete_requires_reason(api):
    created = api.client.post("/api/asset-owners",
                              json={"name": "示例-归属人", "role": "self"})
    assert created.status_code == 201, created.text
    owner_id = created.json()["id"]

    _expect_422(api.client.delete(f"/api/asset-owners/{owner_id}"))
    r = api.client.delete(f"/api/asset-owners/{owner_id}",
                          params={"reason": "normalization"})
    assert r.status_code == 204, r.text
    db = api.Session()
    assert _last(db, "classification", "delete").reason == "normalization"
    db.close()


# ------------------------------------------- 2b. 批量：明细行必须与头行同 reason
#
# 早前 R5「理由只落批次头行」在 E1 收口后会**自伤**：明细行的 action 正是
# `delete`/`correct`（∈ REASON_REQUIRED_ACTIONS）、actor=owner，缺 reason 就被
# 自己的强制点 422 掉（整批失败）。故裁决：明细行带与头行相同的 reason。
# 理由不构成伪造——本来就是同一次裁决，只是逐行各记一遍。


def _asset(db, name, amount="10.00"):
    """建一条记录（各自独立的资产项：同项同日只能有一行 active）。"""
    from services import asset_service
    return asset_service.create_asset(db, name, Decimal(amount), TODAY)


def test_batch_delete_details_carry_same_reason_as_header(api):
    """批量删 2 行 → 头行与两条明细的 reason 相同且都非 NULL（spec 实施备注）。"""
    db = api.Session()
    a1, a2 = _asset(db, "示例-批删甲"), _asset(db, "示例-批删乙")
    ids = [a1.id, a2.id]
    db.close()

    r = api.client.post("/api/assets/batch-delete",
                        params={"reason": "correction",
                                "reason_note": "示例：清掉错录"},
                        json={"ids": ids})
    assert r.status_code == 200, r.text
    assert r.json()["deleted_count"] == 2

    db = api.Session()
    head = _last(db, "batch", "execute")
    details = db.execute(select(AuditLog).where(
        AuditLog.entity == "asset", AuditLog.action == "delete")
        .order_by(AuditLog.id)).scalars().all()
    assert head is not None and head.reason == "correction"
    assert len(details) == 2
    assert [d.reason for d in details] == ["correction", "correction"]
    assert {d.reason_note for d in details} == {"示例：清掉错录"}
    assert all(d.reason and d.reason == head.reason for d in details)
    db.close()


def test_batch_update_details_carry_same_reason_as_header(api):
    """批量改 side（逐行 correct 明细）同样携带头行 reason，不被强制点打断。"""
    db = api.Session()
    a1, a2 = _asset(db, "示例-批改甲"), _asset(db, "示例-批改乙")
    ids = [a1.id, a2.id]
    db.close()

    r = api.client.post("/api/assets/batch-update",
                        params={"reason": "normalization"},
                        json={"ids": ids, "side": "liability"})
    assert r.status_code == 200, r.text
    assert r.json()["updated_count"] >= 2

    db = api.Session()
    head = _last(db, "batch", "execute")
    details = db.execute(select(AuditLog).where(
        AuditLog.entity == "asset_snapshot", AuditLog.action == "correct")
        .order_by(AuditLog.id)).scalars().all()
    assert head is not None and head.reason == "normalization"
    assert len(details) == 2
    assert [d.reason for d in details] == ["normalization", "normalization"]
    db.close()


def test_batch_classification_details_not_forced(api):
    """批量改分类：明细 action=`classification/update` **不在**强制集，故不 422。

    这一条是「不需要改」的确认（用户裁决第四点）：确认它不会被 E1 打断，
    且明细行 reason 保持 NULL（理由只落变更史自由文本，不冒充审计枚举）。
    """
    from schemas import BatchClassificationInput
    from services import classification_service

    db = api.Session()
    item_a = _item(db, "示例-批分类甲")
    item_b = _item(db, "示例-批分类乙")
    ids = [item_a.id, item_b.id]
    res = classification_service.batch_set_classification(
        db, ids, {"liquidity_days": 7}, reason="correction")
    asserts = res == {"updated": 2, "failed": []}
    details = db.execute(select(AuditLog).where(
        AuditLog.entity == "classification", AuditLog.action == "update")
        .order_by(AuditLog.id)).scalars().all()
    reasons = [d.reason for d in details]
    db.close()
    assert asserts, res
    assert len(details) == 2
    assert reasons == [None, None]        # update 不在强制集：不写审计 reason 也合法
    assert BatchClassificationInput is not None


# ---------------------------------------------------------------- 3. evidence 三端点

def _evidence(db, rel="2026/11/a.png", sha="a"):
    sg = db.execute(select(SourceGroup)).scalars().first()
    if sg is None:
        sg = SourceGroup(slug="demo-evi", label="示例凭证组",
                         asset_prefix="示例")
        db.add(sg)
        db.commit()
    ev = ScreenshotEvidence(source_group_id=sg.id, file_path=rel,
                            file_sha256=sha * 64, created_at=datetime.now())
    db.add(ev)
    db.commit()
    return ev


def test_evidence_delete_endpoints_require_reason(api):
    db = api.Session()
    ev = _evidence(db)
    ev_id = ev.id
    db.close()

    _expect_422(api.client.delete(f"/api/screenshots/evidence/{ev_id}"))
    db = api.Session()
    assert db.get(ScreenshotEvidence, ev_id) is not None
    assert not db.execute(select(AuditLog)).scalars().all()
    db.close()

    r = api.client.delete(f"/api/screenshots/evidence/{ev_id}",
                          params={"reason": "normalization"})
    assert r.status_code == 204, r.text
    db = api.Session()
    row = _last(db, "screenshot_evidence", "delete")
    assert row.reason == "normalization" and row.evidence_ref is None
    db.close()


def test_evidence_purge_requires_reason_each_row(api):
    """验收 5：批量 purge 每条明细行都带同一 reason（无头行）。"""
    db = api.Session()
    ev1 = _evidence(db, "2026/11/p1.png", sha="1")
    ev2 = _evidence(db, "2026/11/p2.png", sha="2")
    ids = (ev1.id, ev2.id)
    db.close()

    _expect_422(api.client.post("/api/screenshots/evidence/purge-unused",
                                json={"dry_run": False}))
    db = api.Session()
    assert db.get(ScreenshotEvidence, ids[0]) is not None      # 一条没删
    assert db.get(ScreenshotEvidence, ids[1]) is not None
    assert not db.execute(select(AuditLog)).scalars().all()
    db.close()

    # dry_run（只出清单，必然零写入）不需要 reason
    r = api.client.post("/api/screenshots/evidence/purge-unused", json={})
    assert r.status_code == 200 and r.json()["dry_run"] is True

    r = api.client.post("/api/screenshots/evidence/purge-unused",
                        json={"dry_run": False},
                        params={"reason": "normalization"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    db = api.Session()
    rows = db.execute(select(AuditLog).where(
        AuditLog.entity == "screenshot_evidence",
        AuditLog.action == "delete")).scalars().all()
    assert len(rows) == 2
    assert {row.reason for row in rows} == {"normalization"}
    db.close()


def test_source_group_purge_requires_reason(api):
    db = api.Session()
    sg = SourceGroup(slug="demo-purge", label="示例待删组",
                     asset_prefix="示例")
    db.add(sg)
    db.commit()
    ev = ScreenshotEvidence(source_group_id=sg.id, file_path="2026/11/g.png",
                            file_sha256="b" * 64, created_at=datetime.now())
    db.add(ev)
    db.commit()
    sg_id, ev_id = sg.id, ev.id
    db.close()

    _expect_422(api.client.delete(f"/api/source-groups/{sg_id}",
                                  params={"purge_unused": True}))
    db = api.Session()
    assert db.get(SourceGroup, sg_id) is not None
    assert db.get(ScreenshotEvidence, ev_id) is not None
    db.close()

    r = api.client.delete(f"/api/source-groups/{sg_id}",
                          params={"purge_unused": True,
                                  "reason": "normalization"})
    assert r.status_code == 200, r.text
    db = api.Session()
    rows = db.execute(select(AuditLog).where(
        AuditLog.action == "delete")).scalars().all()
    assert {row.entity for row in rows} == {"source_group", "screenshot_evidence"}
    assert {row.reason for row in rows} == {"normalization"}
    db.close()


# ---------------------------------------------------------------- 4. goals

def _goal(api):
    resp = api.client.post("/api/planning/goals", json={
        "name": "示例目标", "target_amount": "1000000.00",
        "target_date": FUTURE.isoformat(), "activate": True,
        "reason": "示例建目标"})
    assert resp.status_code == 200, resp.text
    return resp.json()["goal"]["id"]


def test_goal_archive_requires_reason_but_activation_is_system(api):
    """验收 6：归档缺 reason → 422；激活新版本的隐式归档＝system/normalization。"""
    goal_id = _goal(api)

    _expect_422(api.client.post(f"/api/planning/goals/{goal_id}/archive"))

    r = api.client.post(f"/api/planning/goals/{goal_id}/versions",
                        json={"target_amount": "1200000.00",
                              "target_date": FUTURE.isoformat(),
                              "reason": "示例调高", "activate": True})
    assert r.status_code == 200, r.text

    db = api.Session()
    implicit = _last(db, "goal_version", "archive")
    assert implicit.actor == "system"
    assert implicit.reason == "normalization"
    assert '"source": "activation"' in implicit.after_json
    db.close()

    r = api.client.post(f"/api/planning/goals/{goal_id}/archive",
                        params={"reason": "normalization",
                                "reason_note": "示例：周期结束"})
    assert r.status_code == 200, r.text
    db = api.Session()
    row = _last(db, "goal", "archive")
    assert row.reason == "normalization" and row.actor == "owner"
    db.close()


def test_goal_version_delete_requires_reason(api):
    goal_id = _goal(api)
    made = api.client.post("/api/planning/goals",
                           json={"name": "示例目标B", "target_amount": "1.00",
                                 "target_date": FUTURE.isoformat(),
                                 "activate": False, "reason": "示例草稿"})
    assert made.status_code == 200, made.text
    goal_b, version_no = made.json()["goal"]["id"], 1

    _expect_422(api.client.delete(
        f"/api/planning/goals/{goal_b}/versions/{version_no}"))
    r = api.client.delete(f"/api/planning/goals/{goal_b}/versions/{version_no}",
                          params={"reason": "normalization"})
    assert r.status_code == 200, r.text
    db = api.Session()
    assert _last(db, "goal_version", "delete").reason == "normalization"
    db.close()
    assert goal_id  # 保持夹具使用（第一个目标仅用于串行断言）


# ---------------------------------------------------------------- 5. 自动路径（E2 ①）

def _fake_backups(api, names):
    backup_dir = api.data / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = []
    now = datetime.now()
    for idx, name in enumerate(names):
        path = backup_dir / name
        path.write_bytes(b"fake")
        stamp = (now - timedelta(days=idx)).timestamp()
        os.utime(path, (stamp, stamp))         # 越靠后越旧
        out.append(path)
    return out


def test_retention_policy_deletes_with_system_identity(api):
    """验收 7：超限文件被删，审计行 system + normalization + after.source=retention。"""
    _fake_backups(api, [f"backup_2026010{i}_000000.zip" for i in range(2, 7)])

    db = api.Session()
    out = backup_service.apply_retention_policy(
        {"enabled": True, "type": "count", "count": 3, "days": 30}, db=db)
    db.close()

    # 5 个文件按 mtime 倒序（102 最新）留前 3 个 → 删最旧的两个
    assert out["deleted"] == ["backup_20260105_000000.zip",
                              "backup_20260106_000000.zip"]
    db = api.Session()
    rows = db.execute(select(AuditLog).where(
        AuditLog.entity == "backup", AuditLog.action == "delete")
    ).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.actor == "system"
        assert row.reason == "normalization"          # 服务端归一化（非人声明）
        assert '"source": "retention"' in row.after_json   # 触发源＝保留策略
    db.close()


def test_retention_policy_days_mode_and_rollback_exempt(api):
    paths = _fake_backups(api, ["backup_old.zip", "backup_new.zip",
                                "rollback_old.zip"])
    old = datetime.now() - timedelta(days=40)
    for path in paths[:1] + paths[2:]:
        os.utime(path, (old.timestamp(), old.timestamp()))

    db = api.Session()
    out = backup_service.apply_retention_policy(
        {"enabled": True, "type": "days", "count": 10, "days": 30}, db=db)
    db.close()

    assert out["deleted"] == ["backup_old.zip"]          # rollback 永不删
    assert (api.data / "backup" / "rollback_old.zip").exists()
    assert (api.data / "backup" / "backup_new.zip").exists()


def test_retention_policy_disabled_is_noop(api):
    _fake_backups(api, ["backup_a.zip", "backup_b.zip"])
    db = api.Session()
    out = backup_service.apply_retention_policy(
        {"enabled": False, "type": "count", "count": 1, "days": 30}, db=db)
    db.close()
    assert out["deleted"] == [] and out["enabled"] is False
    assert (api.data / "backup" / "backup_b.zip").exists()


def test_retention_policy_reads_env_defaults_off(monkeypatch):
    monkeypatch.delenv("BACKUP_RETENTION_ENABLED", raising=False)
    assert backup_service.retention_policy_from_env()["enabled"] is False
    monkeypatch.setenv("BACKUP_RETENTION_ENABLED", "true")
    monkeypatch.setenv("BACKUP_RETENTION_TYPE", "days")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "7")
    policy = backup_service.retention_policy_from_env()
    assert policy == {"enabled": True, "type": "days", "count": 10, "days": 7}


def test_scheduler_registers_retention_job():
    """job 必须在 init_scheduler 里注册（前端删除循环退役后，执行权在后端）。"""
    from services.scheduler_service import init_scheduler, shutdown_scheduler
    sched = init_scheduler()
    try:
        ids = {job.id for job in sched.get_jobs()}
    finally:
        shutdown_scheduler()
    assert "backup_retention_policy" in ids