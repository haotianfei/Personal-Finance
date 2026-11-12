"""v5 一期·步骤 4「审计日志」测试（spec §7 全部）。

留痕 ①correct ②apply_row ③runner migration_run ④rename/archive/create/delete；
append-only 触发器＋service 无修改端口；事务两向；形状契约；request_id；
只读查询端点。沙箱：in-memory db fixture＋tmp 文件库；不触真实 data/。
"""
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
import structlog
from sqlalchemy import select

from errors import ApiError
from models import (Asset, AssetClass, AssetItemAttrHistory, AuditLog,
                    Institution, Member, ScreenshotEvidence)
from services import asset_service, audit_chain, audit_service, classification_service
from services import db_migration_service as dbs


def _classify_item(db, item_id, name="示例货币基金"):
    """在资产项上设分类（ADR-0019：项级唯一写入口，每次真实变更留一行变更史）。"""
    node = AssetClass(code=f"T{item_id}", name=name, level=1, side_scope="any")
    institution = Institution(name=f"示例银行{item_id}", kind="bank")
    member = Member(name=f"本人{item_id}", role="self")
    db.add_all([node, institution, member])
    db.commit()
    return classification_service.set_classification(db, item_id, {
        "asset_class_id": node.id, "liquidity_days": 0,
        "institution_id": institution.id, "owner_id": member.id,
    })


def _attr_history(db, item_id):
    return db.execute(
        select(AssetItemAttrHistory)
        .where(AssetItemAttrHistory.item_id == item_id)
        .order_by(AssetItemAttrHistory.id)
    ).scalars().all()



def _rows(db, **f):
    q = select(AuditLog)
    if "action" in f:
        q = q.where(AuditLog.action == f["action"])
    if "entity" in f:
        q = q.where(AuditLog.entity == f["entity"])
    return db.execute(q.order_by(AuditLog.id)).scalars().all()


def confirms(p) -> dict:
    """D4：create 行必须显式表态。测试里统一走「确认新增」（不是本特性的被测对象）。"""
    return {str(r.index if r.index is not None else pos): {"action": "create"}
            for pos, r in enumerate(p.rows) if r.action == "create"}


# ---------------------------------------------------------------- 留痕

def test_01_correct_leaves_audit_with_amounts_and_evidence(db):
    from services.screenshot_service import (
        get_source_group, seed_default_source_groups)
    a = asset_service.create_asset(db, "示例-审计A", 100, date(2026, 3, 1))
    seed_default_source_groups(db)          # 0029：凭证只存 source_group_id，先要有组行
    ev = ScreenshotEvidence(
        source_group_id=get_source_group(db, "bantuou").id,
        file_path="x/1.png", file_sha256="f" * 64, created_at=datetime.now())
    db.add(ev)
    db.flush()
    asset_service.correct_asset(db, a.id, "180", source_evidence_id=ev.id,
                                reason="correction")

    rows = _rows(db, action="correct")
    assert len(rows) == 1
    r = rows[0]
    assert r.entity == "asset_snapshot" and r.actor == "owner"
    assert json.loads(r.before_json) == {"amount": "100.00", "side": "asset"}
    assert json.loads(r.after_json) == {"amount": "180.00", "side": "asset"}
    assert r.evidence_ref == ev.id and r.row_id != a.id  # row_id＝新行
    rows_all = _rows(db)
    assert rows_all[0].prev_hash == audit_chain.GENESIS
    assert r.prev_hash == audit_chain.row_hash(rows_all[0].prev_hash, rows_all[0])
    assert audit_chain.verify(db)["ok"] is True


def test_02_apply_preview_one_audit_per_written_row(db, tmp_path):
    from services.screenshot_service import (
        apply_preview, compute_preview, ParsedItem, SOURCE_GROUPS,
        seed_default_source_groups)
    seed_default_source_groups(db)      # 0029：apply 自建凭证行需要组行存在
    prefix = SOURCE_GROUPS["bantuou"]["asset_prefix"]
    D = date(2026, 3, 1)
    # 既有行（update 目标）经 ledger 单点挂 item
    asset_service.create_asset(db, prefix + "既有项", Decimal("100"), D)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    known = [(prefix + "既有项", Decimal("100"))]
    p = compute_preview([ParsedItem(name="既有项", amount=Decimal("2345.67")),
                         ParsedItem(name="新项", amount=Decimal("50"))],
                        known, prefix)
    res = apply_preview(db, p, source_group="bantuou", data_date=D,
                        image_path=str(img),
                        evidence_root=str(tmp_path / "ev"),
                        identity_decisions=confirms(p))
    assert res.rows_written == 2
    applies = _rows(db, action="apply_row")
    assert len(applies) == 2
    upd = [r for r in applies if r.before_json and json.loads(r.before_json)]
    assert len(upd) == 1  # update 行带覆盖前旧值；create 行 before=NULL
    assert json.loads(upd[0].before_json)["amount"] == "100.00"
    assert json.loads(upd[0].after_json)["amount"] == "2345.67"
    assert all(r.evidence_ref == res.evidence_id for r in applies)


def test_02b_apply_conflict_leaves_no_audit(db, tmp_path):
    from services.screenshot_service import (
        apply_preview, compute_preview, ParsedItem, SOURCE_GROUPS,
        seed_default_source_groups)
    seed_default_source_groups(db)      # 0029：apply 自建凭证行需要组行存在
    prefix = SOURCE_GROUPS["bantuou"]["asset_prefix"]
    D = date(2026, 3, 1)
    asset_service.create_asset(db, prefix + "撞项", Decimal("100"), D)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    # 以 known=[] 让 compute_preview 判 create → 同日同项 active 行 → 显式冲突
    p = compute_preview([ParsedItem(name="撞项", amount=Decimal("777"))], [], prefix)
    before_n = len(_rows(db, action="apply_row"))
    res = apply_preview(db, p, source_group="bantuou", data_date=D,
                        image_path=str(img),
                        evidence_root=str(tmp_path / "ev"),
                        identity_decisions=confirms(p))
    assert res.rows_written == 0 and len(res.conflicts) == 1
    assert len(_rows(db, action="apply_row")) == before_n  # 冲突行不留


def test_03_runner_emits_migration_run_with_backup(v4_env):
    report = dbs.run_migrations(db_path=v4_env["db"])
    conn = sqlite3.connect(v4_env["db"])
    row = conn.execute("SELECT actor, before_json, after_json FROM audit_log"
                       " WHERE action='migration_run'").fetchall()
    conn.close()
    assert len(row) == 1
    actor, before_json, after_json = row[0]
    assert actor == "system" and before_json is None
    after = json.loads(after_json)
    assert after["applied"] == report.applied and after["baseline"] == report.baseline
    assert after["backup"] == report.backup and after["backup"].endswith(".zip")


def test_03b_noop_rerun_emits_nothing(v4_env):
    dbs.run_migrations(db_path=v4_env["db"])
    dbs.run_migrations(db_path=v4_env["db"])  # 二次启动 no-op
    conn = sqlite3.connect(v4_env["db"])
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='migration_run'").fetchone()[0]
    conn.close()
    assert n == 1  # 纯 no-op 不刷审计行（幂等重跑）


def test_04_use_cases_each_leave_one_row(db, monkeypatch):
    a = asset_service.create_asset(db, "示例-审计B", 10, date(2026, 3, 1))
    item_id = a.asset_item_id
    asset_service.rename_item(db, item_id, "示例-审计B2")
    asset_service.set_item_status(db, item_id, "archived", reason="normalization")
    asset_service.set_item_status(db, item_id, "active")

    # 记录级分类写入已随 0026 迁移退役（ADR-0019 D2）：显式 422，审计里不再有
    # update_attrs —— 静默忽略会让调用方以为分类存上了。
    with pytest.raises(ApiError) as ei:
        asset_service.update_asset(db, a.id, attribute_ids=[1])
    assert ei.value.status_code == 422

    # 项级分类改判**双轨留痕**（ADR-0027 口径桶）：变更史（append-only，服务业务读）
    # ＋ `classification/update` 审计行（旁路取证，row_id＝资产项 id）。旧断言曾锁
    # 「只写 attr_history、不写 audit_log」，ADR-0027 判定改分类＝改口径，故翻转。
    _classify_item(db, item_id)
    hist = _attr_history(db, item_id)
    assert {"asset_class", "liquidity", "institution", "owner"} <= {
        h.dim for h in hist}

    audits = _rows(db, action="update")
    assert len(audits) == 1
    aud = audits[0]
    assert aud.entity == "classification" and aud.row_id == item_id  # 资产项 id
    before, after = json.loads(aud.before_json), json.loads(aud.after_json)
    assert set(after) == set(before) == {h.dim for h in hist}   # 两轨同口径（仅改判维度）
    assert before["liquidity"] is None and after["liquidity"] == 0
    hist_by_dim = {h.dim: h for h in hist}
    assert str(after["asset_class"]) == hist_by_dim["asset_class"].new_value

    # 同一事务：审计写失败 → 维度改动与变更史一起回滚（不留「改了却无痕」）
    def _boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")
    with monkeypatch.context() as patch:
        patch.setattr(audit_service, "record", _boom)
        with pytest.raises(RuntimeError):
            classification_service.set_classification(db, item_id, {"liquidity_days": 30})
    db.rollback()
    assert all(h.new_value != "30" for h in _attr_history(db, item_id))

    asset_service.delete_asset(db, a.id, reason="other",
                               reason_note="审计用例：删除以凑齐动作面")

    by = {r.action for r in _rows(db)}
    assert {"create", "rename", "archive", "unarchive", "delete"} <= by
    assert "update_attrs" not in by            # 记录级属性写入路径已退役
    dele = _rows(db, action="delete")[0]
    assert dele.after_json is None and json.loads(dele.before_json)["amount"] == "10.00"
    ren = _rows(db, action="rename")[0]
    assert ren.entity == "asset_item" and ren.row_id == item_id


# ---------------------------------------------------------------- append-only（0009）

@pytest.fixture()
def v4_env(tmp_path, monkeypatch):
    """tmp v4 形态文件库（真实 migrations 目录首跑：认领 1-7＋实跑 8/9＋migration_run）。"""
    data = tmp_path / "data"
    data.mkdir()
    db = str(data / "person_fin.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY);"
        "CREATE TABLE attributes (id INTEGER PRIMARY KEY, name VARCHAR(50), type VARCHAR(50));"
        "CREATE TABLE asset_attributes (asset_id INTEGER NOT NULL, attribute_id INTEGER NOT NULL,"
        " PRIMARY KEY (asset_id, attribute_id));"
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_name VARCHAR(100) NOT NULL,"
        " amount NUMERIC(15,2) NOT NULL, side VARCHAR(10) NOT NULL DEFAULT 'asset',"
        " asset_date DATE NOT NULL, source_evidence_id INTEGER REFERENCES screenshot_evidence(id),"
        " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);"
        "INSERT INTO assets (id, asset_name, amount, side, asset_date,"
        " source_evidence_id, created_at, updated_at)"
        " VALUES (1,'示例-项','100.00','asset','2026-01-15',NULL,'x','x');"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", db)
    return {"db": db, "data": data}


def _run_0009(db_path):
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    m = dbs._load_script_module(f"{dbs._MIGRATIONS_DIR}/0009_audit_log.py")
    conn.execute("BEGIN")
    m.upgrade(conn)
    conn.execute("COMMIT")
    return conn


def test_05_audit_log_is_append_only(tmp_path):
    db = str(tmp_path / "a.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    conn = _run_0009(db)
    conn.execute("INSERT INTO audit_log (occurred_at, actor, entity, action)"
                 " VALUES ('2026-03-01T00:00:00', 'owner', 'asset_snapshot', 'create')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET actor='x' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=1")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    # 触发器回滚后连接仍可用，且 INSERT 照常
    conn.execute("INSERT INTO audit_log (occurred_at, actor) VALUES ('t', 'owner')")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
    conn.close()


def test_05b_0009_idempotent_rerun(tmp_path):
    db = str(tmp_path / "b.db")
    conn = _run_0009(db)
    m = dbs._load_script_module(f"{dbs._MIGRATIONS_DIR}/0009_audit_log.py")
    conn.execute("BEGIN")  # 重跑无异常（IF NOT EXISTS 全守卫）
    m.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()


def test_06_service_exposes_no_mutation_ports():
    public = {n for n in vars(audit_service) if not n.startswith("_")}
    assert {"record", "record_sqlite", "list_audit_log", "get_audit_log"} <= public
    assert not any(n.startswith(("update_", "delete_", "purge")) for n in public)


# ---------------------------------------------------------------- 事务两向

def test_07_business_rollback_leaves_no_audit(db):
    a = asset_service.create_asset(db, "示例-回滚", 1, date(2026, 3, 1))
    db.add(Asset(asset_name="示例-回滚2", asset_item_id=a.asset_item_id,
                 amount=Decimal("2"), asset_date=date(2026, 3, 2), side="asset"))
    audit_service.record(db, {"entity": "asset_snapshot", "row_id": None,
                              "action": "create", "before": None,
                              "after": {"amount": "2"}, "evidence_ref": None})
    db.rollback()  # 业务回滚 → 审计不留
    assert db.query(AuditLog).filter(AuditLog.action == "create",
                                     AuditLog.entity == "asset_snapshot").count() == 1
    assert db.query(Asset).filter_by(asset_name="示例-回滚2").first() is None


def test_07b_audit_failure_blocks_business(db, monkeypatch):
    a = asset_service.create_asset(db, "示例-阻断", 100, date(2026, 3, 1))
    n_before = db.query(AuditLog).count()
    assert n_before == 1  # create 自身已留一行

    def boom(*args, **kwargs):
        raise RuntimeError("audit insert failed")
    monkeypatch.setattr(audit_service, "record", boom)

    with pytest.raises(RuntimeError):
        asset_service.correct_asset(db, a.id, "999")
    db.rollback()
    old = db.get(Asset, a.id)
    assert old.superseded_by is None and old.amount == Decimal("100.00")  # 业务同事务回滚


# ---------------------------------------------------------------- 形状契约 & request_id

def test_08_before_after_key_shapes(db):
    a = asset_service.create_asset(db, "示例-形状", 100, date(2026, 3, 1))
    created = _rows(db, action="create")[0]
    assert created.before_json is None
    assert set(json.loads(created.after_json)) == {"amount", "side", "asset_date"}

    asset_service.correct_asset(db, a.id, "200", reason="correction")
    corr = _rows(db, action="correct")[0]
    assert set(json.loads(corr.before_json)) == {"amount", "side"}
    assert set(json.loads(corr.after_json)) == {"amount", "side"}

    # 记录侧分类写入退役（ADR-0019 D2）→ 分类留痕改由项级变更史承担（D10）：
    # 形状契约为「维度名 + 旧值/新值字符串」，append-only，无 update 端口。
    with pytest.raises(ApiError):
        asset_service.update_asset(db, a.id, attribute_ids=[1])
    _classify_item(db, a.asset_item_id)
    hist = {h.dim: h for h in _attr_history(db, a.asset_item_id)}
    assert set(hist) == {"asset_class", "liquidity", "institution", "owner"}
    assert hist["liquidity"].old_value is None and hist["liquidity"].new_value == "0"
    assert hist["asset_class"].new_value.isdigit()

    _, ch = asset_service.rename_item(db, a.asset_item_id, "示例-形状2")
    ren = _rows(db, action="rename")[0]
    assert isinstance(json.loads(ren.before_json), str)
    assert ch["after"] == "示例-形状2"


def test_09_request_id_from_contextvars(db):
    structlog.contextvars.bind_contextvars(request_id="abc123")
    try:
        asset_service.create_asset(db, "示例-reqid", 1, date(2026, 3, 1))
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    row = _rows(db, action="create")[-1]
    assert row.request_id == "abc123"
    # 无绑定上下文 → NULL（人工/系统路径）
    asset_service.create_asset(db, "示例-noreq", 1, date(2026, 3, 1))
    assert _rows(db, action="create")[-1].request_id is None


def test_09b_record_rejects_bad_change(db):
    with pytest.raises(ValueError):
        audit_service.record(db, {"entity": "x"})          # 缺键
    a = asset_service.create_asset(db, "示例-actor", 1, date(2026, 3, 1))
    with pytest.raises(ValueError):
        audit_service.record(db, {"entity": "asset_snapshot", "row_id": a.id,
                                  "action": "create", "before": None,
                                  "after": None, "evidence_ref": None}, actor="robot")


# ---------------------------------------------------------------- 只读端点

def test_10_query_endpoints(db):
    from routers.audit_log import list_audit_log as route_list, get_audit_log as route_get
    a = asset_service.create_asset(db, "示例-查询", 100, date(2026, 3, 1))
    asset_service.correct_asset(db, a.id, "200", reason="correction")

    out = route_list(entity="asset_snapshot", action="correct", row_id=None,
                     evidence_ref=None, request_id=None, date_from=None, date_to=None,
                     page=1, page_size=50, db=db)
    assert out["total"] == 1 and out["items"][0]["action"] == "correct"
    assert out["items"][0]["after"]["amount"] == "200.00"  # 已解析为 JSON

    by_evidence = route_list(entity=None, row_id=None, action=None,
                             evidence_ref=999, request_id=None, date_from=None,
                             date_to=None, page=1, page_size=50, db=db)
    assert by_evidence["total"] == 0  # 无人挂该凭证

    one = route_get(out["items"][0]["id"], db)
    assert one["before"]["amount"] == "100.00"

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        route_get(99999, db)


def test_10d_query_filters_actor_order_and_idempotency_key(db):
    """票 02 J5：actor 等值、order 双向、幂等键精确匹配。"""
    from routers.audit_log import list_audit_log as route_list
    from services.idempotency_service import active_idem_key

    a = asset_service.create_asset(db, "示例-筛选", 100, date(2026, 3, 1))  # owner
    token = active_idem_key.set("idem-abc")
    try:
        asset_service.correct_asset(db, a.id, "300", reason="correction")   # owner + 幂等键
    finally:
        active_idem_key.reset(token)

    def q(**kw):
        base = dict(entity=None, row_id=None, action=None, evidence_ref=None,
                    request_id=None, date_from=None, date_to=None,
                    page=1, page_size=50, db=db)
        base.update(kw)
        return route_list(**base)

    assert q(actor="system")["total"] == 0          # 本用例只造了 owner 行
    assert q(actor="owner")["total"] == 2
    assert q(idempotency_key="idem-abc")["total"] == 1
    assert q(idempotency_key="idem-nope")["total"] == 0
    asc = q(order="asc")["items"]
    desc = q(order="desc")["items"]
    assert [r["id"] for r in asc] == [r["id"] for r in desc][::-1]
    assert asc[0]["action"] == "create" and desc[-1]["action"] == "create"

    from errors import ApiError
    with pytest.raises(ApiError) as exc:
        q(order="sideways")
    assert exc.value.status_code == 422
