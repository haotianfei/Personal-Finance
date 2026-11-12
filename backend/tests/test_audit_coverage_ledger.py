"""留痕覆盖率台账（ADR-0027 D3/D4）：①值桶批量写 + ⑤整库桶。

本文件锁的是「批量写」的留痕形状与边界，不是全量契约表：
1. batch-delete 2 行 → 1 条 `entity='batch', action='execute'`（after.count=2）＋
   2 条 `entity='asset', action='delete'`（before 含 id/amount），且行确实消失；
2. batch-delete 501 行 → 400 `BATCH_LIMIT_EXCEEDED`，审计零新增、行数不变；
3. 业务中途抛错 → 同事务回滚，审计一行不留（ADR-0007 语义）；
4. imports confirm / seed → `entity='import'` 头行 + 逐行 create 明细，条数对齐；
5. 整库恢复 → 留痕写在**替换后的新库**上（含 chain_head_before），替换失败则零留痕；
6. 这些写入之后 `audit_chain.verify()` 仍 ok（链能续接）。

沙箱：内存库 fixture（同 `test_audit.py`）＋ 整库场景用 tmp_path 文件库 +
`DATABASE_PATH` 注入；不触真实 data/。
"""
import json
import os
import shutil
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models
from errors import ApiError
from models import Asset, AuditLog
from services import asset_service, audit_chain, backup_service, import_service


# ---------------------------------------------------------------- 助手

def _rows(db, **f):
    q = select(AuditLog)
    if "entity" in f:
        q = q.where(AuditLog.entity == f["entity"])
    if "action" in f:
        q = q.where(AuditLog.action == f["action"])
    return db.execute(q.order_by(AuditLog.id)).scalars().all()


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _asset_count(db) -> int:
    return db.execute(select(func.count()).select_from(Asset)).scalar() or 0


def _make_assets(db, n: int, name: str = "示例-批量") -> list[Asset]:
    """直插 n 行资产（不经 create_asset → 不产审计行，便于数审计）。"""
    db.add_all([Asset(asset_name=f"{name}{i}", amount=Decimal("1.00"), side="asset",
                      asset_date=date(2026, 3, 1)) for i in range(n)])
    db.commit()
    return db.execute(select(Asset).order_by(Asset.id)).scalars().all()


CSV_HEADER = "asset_date,asset_name,amount\n"


# ---------------------------------------------------------------- ① batch-delete 形状

def test_batch_delete_leaves_head_and_detail_rows(db):
    a1 = asset_service.create_asset(db, "示例-批删A", Decimal("100.00"), date(2026, 3, 1))
    a2 = asset_service.create_asset(db, "示例-批删B", Decimal("200.00"), date(2026, 3, 1))

    assert asset_service.batch_delete_records(db, [a1.id, a2.id],
        reason="normalization") == 2

    heads = _rows(db, entity="batch", action="execute")
    assert len(heads) == 1
    head = heads[0]
    assert head.row_id is None and head.before_json is None
    payload = json.loads(head.after_json)
    assert payload["op"] == "delete" and payload["count"] == 2
    assert payload["ids_hash"] == asset_service.ids_fingerprint([a1.id, a2.id])
    assert payload["scope"]["via"] == "batch-delete"

    details = _rows(db, entity="asset", action="delete")
    assert len(details) == 2
    befores = {json.loads(d.before_json)["id"]: json.loads(d.before_json) for d in details}
    assert set(befores) == {a1.id, a2.id}
    assert befores[a1.id]["amount"] == "100.00"      # 金额字符串保精度，不经 float
    assert befores[a2.id]["amount"] == "200.00"
    assert befores[a1.id]["date"] == "2026-03-01"
    assert all(d.after_json is None for d in details)
    # 头行先于明细行（同批 id 递增，读端即「批次 → 明细」）
    assert head.id < min(d.id for d in details)

    assert db.get(Asset, a1.id) is None and db.get(Asset, a2.id) is None


def test_batch_delete_limit_500_rejects_without_audit(db):
    assets = _make_assets(db, 501)
    ids = [a.id for a in assets]
    assert _count(db) == 0 and _asset_count(db) == 501

    with pytest.raises(ApiError) as ei:
        asset_service.batch_delete_records(db, ids,
        reason="normalization")
    assert ei.value.status_code == 400
    assert ei.value.code == "BATCH_LIMIT_EXCEEDED"
    assert "500" in ei.value.message and "先导出" in ei.value.message
    db.rollback()

    assert _count(db) == 0            # 审计零新增
    assert _asset_count(db) == 501    # 行数不变

    # 路由层同一语义（错误信封由 main handler 统一转 400）
    from routers.assets import batch_delete as route_batch_delete
    with pytest.raises(ApiError) as ei2:
        route_batch_delete({"ids": ids}, db)
    assert ei2.value.code == "BATCH_LIMIT_EXCEEDED"
    db.rollback()
    assert _count(db) == 0 and _asset_count(db) == 501


def test_batch_failure_rolls_back_audit_too(db, monkeypatch):
    a1 = asset_service.create_asset(db, "示例-批删-回滚A", Decimal("1.00"), date(2026, 3, 1))
    a2 = asset_service.create_asset(db, "示例-批删-回滚B", Decimal("2.00"), date(2026, 3, 1))
    before_n = _count(db)

    real = asset_service.asset_key_fields
    calls = {"n": 0}

    def boom(asset):
        calls["n"] += 1
        if calls["n"] >= 2:          # 头行 + 第一条明细已写，第二条炸掉
            raise RuntimeError("business blew up")
        return real(asset)

    monkeypatch.setattr(asset_service, "asset_key_fields", boom)

    with pytest.raises(RuntimeError):
        asset_service.batch_delete_records(db, [a1.id, a2.id],
        reason="normalization")
    db.rollback()                    # 同事务回滚 → 审计不留

    assert _count(db) == before_n
    assert db.get(Asset, a1.id) is not None and db.get(Asset, a2.id) is not None


# ---------------------------------------------------------------- ① batch

def test_batch_update_leaves_head_row_before_corrects(db):
    """`/batch-update`（改 side＝append 更正）：1 条头行 + 2 条 correct 明细。"""
    a1 = asset_service.create_asset(db, "示例-批改A", Decimal("10.00"), date(2026, 6, 1))
    a2 = asset_service.create_asset(db, "示例-批改B", Decimal("20.00"), date(2026, 6, 1))

    updated = asset_service.batch_update_records(
        db, [a1.id, a2.id], {"side": "liability"}, reason="normalization")
    assert updated == 2

    heads = _rows(db, entity="batch", action="execute")
    assert len(heads) == 1
    payload = json.loads(heads[0].after_json)
    assert payload["op"] == "update" and payload["count"] == 2
    assert payload["ids_hash"] == asset_service.ids_fingerprint([a1.id, a2.id])
    assert payload["scope"]["via"] == "batch-update"

    corrects = _rows(db, entity="asset_snapshot", action="correct")
    assert len(corrects) == 2
    assert json.loads(corrects[0].after_json)["side"] == "liability"
    # 头行在逐行更正之前
    assert heads[0].id < min(c.id for c in corrects)


def test_batch_create_leaves_head_and_details(db):
    created, skipped = asset_service.batch_create_assets(db, [
        {"asset_name": "示例-批建A", "amount": "10", "asset_date": date(2026, 5, 1)},
        {"asset_name": "示例-批建B", "amount": "20", "asset_date": date(2026, 5, 1)},
    ], "skip")
    assert not skipped and len(created) == 2

    heads = _rows(db, entity="batch", action="execute")
    assert len(heads) == 1 and json.loads(heads[0].after_json)["op"] == "create"
    assert json.loads(heads[0].after_json)["count"] == 2
    creates = _rows(db, entity="asset", action="create")
    assert {json.loads(c.after_json)["id"] for c in creates} == {a.id for a in created}
    assert all(c.before_json is None for c in creates)


# ---------------------------------------------------------------- imports confirm / seed

def test_import_confirm_and_seed_leave_head_and_detail_rows(db, tmp_path):
    text = CSV_HEADER + "2026/03/01,示例-导入A,100\n2026/03/01,示例-导入B,200\n"
    batch = import_service.import_csv_records(db, text, "示例确认.csv")
    assert batch.record_count == 2

    heads = _rows(db, entity="import", action="import_confirm")
    assert len(heads) == 1
    payload = json.loads(heads[0].after_json)
    assert payload["count"] == 2 and payload["source"] == "示例确认.csv"
    assert payload["ids_hash"] and heads[0].row_id == batch.id
    assert len(_rows(db, entity="asset", action="create")) == 2

    import_dir = tmp_path / "import"
    import_dir.mkdir()
    (import_dir / "2026-04-01.csv").write_text(
        CSV_HEADER + "2026/04/01,示例-导入C,300\n2026/04/01,示例-导入D,400\n",
        encoding="utf-8")
    batches = import_service.seed_import_directory(db, str(import_dir))
    assert len(batches) == 1 and batches[0].record_count == 2

    seed_heads = _rows(db, entity="import", action="import_seed")
    assert len(seed_heads) == 1
    assert json.loads(seed_heads[0].after_json)["count"] == 2
    assert json.loads(seed_heads[0].after_json)["source"] == "2026-04-01.csv"
    # 明细行数 == 两次导入实际落库行数
    assert len(_rows(db, entity="asset", action="create")) == 4


# ---------------------------------------------------------------- ⑤ 整库替换

def _file_session(path):
    """文件库会话（create_all 建全 schema，含 audit_log / audit_chain_state）。"""
    engine = create_engine(f"sqlite:///{path}")
    models.Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def restore_sandbox(tmp_path, monkeypatch):
    """当前库（带审计链）+ 目标库（另一条链），DATABASE_PATH 指向沙箱。"""
    data = tmp_path / "data"
    data.mkdir()
    current = data / "person_fin.db"
    engine, session = _file_session(current)
    asset_service.create_asset(session, "示例-恢复前", Decimal("100.00"), date(2026, 3, 1))
    asset_service.create_asset(session, "示例-恢复前2", Decimal("200.00"), date(2026, 3, 2))
    before_head = audit_chain.head(session)
    session.close()
    engine.dispose()

    target = tmp_path / "target.db"
    t_engine, t_session = _file_session(target)
    asset_service.create_asset(t_session, "示例-恢复目标", Decimal("999.00"), date(2026, 4, 1))
    t_session.close()
    t_engine.dispose()
    target_head = backup_service._audit_head(str(target))

    monkeypatch.setenv("DATABASE_PATH", str(current))
    return {"current": current, "target": target, "data": data,
            "before_head": before_head, "target_head": target_head}


def _read_audit(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT entity, action, after_json FROM audit_log ORDER BY id").fetchall()
    finally:
        conn.close()


def _fake_replace(calls):
    """替身：只在本沙箱里「换库」（真 src.copy 到 current），不 dispose 真引擎。"""
    def _replace(new_db_path):
        calls.append(new_db_path)
        dst = os.environ["DATABASE_PATH"]
        shutil.copy2(new_db_path, dst)
        for suffix in ("-wal", "-shm"):
            side = dst + suffix
            if os.path.exists(side):
                os.remove(side)
    return _replace


def test_restore_writes_audit_into_replaced_db(restore_sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(backup_service, "_replace_current_db", _fake_replace(calls))

    result = backup_service.restore_database(
        str(restore_sandbox["target"]), action="restore",
        source_label="示例备份.zip")
    assert result["kind"] == "legacy_db"
    assert calls == [str(restore_sandbox["target"])]

    rows = _read_audit(restore_sandbox["current"])
    backup_rows = [r for r in rows if r[0] == "backup"]
    assert len(backup_rows) == 1
    entity, action, after_json = backup_rows[0]
    assert action == "restore"
    payload = json.loads(after_json)
    assert payload["chain_head_before"] == restore_sandbox["before_head"]["last_hash"]
    assert payload["chain_rows_before"] == restore_sandbox["before_head"]["row_count"]
    assert payload["chain_head_target"] == restore_sandbox["target_head"]["last_hash"]
    assert payload["source"] == "示例备份.zip"
    assert len(payload["db_sha256"]) == 64

    # 留痕确实在新库上：新库原有 1 条链行 + 本次 1 条 = 2 条
    assert len(rows) == 2
    engine, session = _file_session(restore_sandbox["current"])
    try:
        assert audit_chain.verify(session)["ok"] is True
    finally:
        session.close()
        engine.dispose()


def test_restore_upload_db_action_and_no_audit_on_failure(restore_sandbox, monkeypatch):
    # 先验 action='upload_db' 的写法
    calls = []
    monkeypatch.setattr(backup_service, "_replace_current_db", _fake_replace(calls))
    # 目标库先克隆一份（第一次恢复会把 current 换成 target，第二次要用同样的目标）
    backup_service.restore_database(str(restore_sandbox["target"]), action="upload_db",
                                    source_label="示例上传.db")
    rows = _read_audit(restore_sandbox["current"])
    assert [r[1] for r in rows if r[0] == "backup"] == ["upload_db"]

    # 失败路径：替换炸掉 → 审计零新增、现库原样
    before_n = len(rows)
    target_copy = restore_sandbox["data"] / "target-copy.db"
    shutil.copy2(restore_sandbox["target"], target_copy)

    def boom(_path):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(backup_service, "_replace_current_db", boom)
    with pytest.raises(RuntimeError):
        backup_service.restore_database(str(target_copy), action="restore")

    after_rows = _read_audit(restore_sandbox["current"])
    assert len(after_rows) == before_n          # 零新增
    assert [r[1] for r in after_rows if r[0] == "backup"] == ["upload_db"]


# ---------------------------------------------------------------- 链仍然可续

def test_chain_still_verifies_after_batch_writes(db, tmp_path):
    a = asset_service.create_asset(db, "示例-链续接", Decimal("7.00"), date(2026, 3, 1))
    asset_service.batch_create_assets(db, [
        {"asset_name": "示例-链续接2", "amount": "8", "asset_date": date(2026, 3, 2)},
    ], "skip")
    asset_service.batch_delete_records(db, [a.id],
        reason="normalization")
    import_service.import_csv_records(
        db, CSV_HEADER + "2026/03/03,示例-链续接3,9\n", "示例链.csv")

    out = audit_chain.verify(db)
    assert out["ok"] is True
    assert out["first_broken"] is None
    assert out["head"]["row_count"] == _count(db)