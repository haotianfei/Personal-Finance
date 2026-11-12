"""三期-3 W1 审计 hash-chain 测试（spec §4.3–4.6、§8.3；ADR-0013 D10–D12）。

覆盖：固定 hex／NULL 与空串区分；ORM 链＋CAS 冲突；状态行两段初始化；
0018 回填双跑幂等/缺列报错/触发器恢复；ensure_chain NULL 续接/拒绝不符；
verify 7 判定码＋分页；日封存单日幂等与自链；双线程无分叉；单写者扫描；
备份 manifest.audit_head；只读端点。数据全部合成。
"""
import json
import sqlite3
import threading
import time
import zipfile
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from errors import ApiError, ErrorCode
from models import AuditChainState, AuditLog
from services import audit_chain, audit_service, backup_service
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0018 = _load_script_module(f"{_MIGRATIONS_DIR}/0018_audit_chain.py")
M0009 = _load_script_module(f"{_MIGRATIONS_DIR}/0009_audit_log.py")
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")

CHANGE = {"entity": "asset_snapshot", "row_id": 1, "action": "create",
          "before": None, "after": {"amount": "100.00"}, "evidence_ref": None}


# ---------------------------------------------------------------- fixtures


def _run_script(conn, module):
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@pytest.fixture()
def chain_env(tmp_path):
    """create_all（无 0009 触发器）＋ 0018 种链状态；返回 (path, Session)。"""
    path = str(tmp_path / "chain.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    _run_script(conn, M0018)
    conn.close()
    return path, sessionmaker(bind=engine)


def _seed(db, n=5):
    rows = []
    for i in range(n):
        change = dict(CHANGE, row_id=i + 1,
                      after={"amount": f"{100 + i}.00"})
        rows.append(audit_service.record(db, change))
    db.commit()
    return rows


def _sql(path, statement, params=()):
    conn = sqlite3.connect(path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _drop_audit_triggers(path):
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 纯函数


def test_row_hash_fixed_vector_and_canonical_json():
    values = {
        "occurred_at": "2026-03-01T00:00:00", "actor": "owner",
        "entity": "asset_snapshot", "action": "create", "row_id": 1,
        "before_json": None, "after_json": '{"amount":"100.00"}',
        "evidence_ref": None, "request_id": None, "idempotency_key": None,
    }
    assert audit_chain.GENESIS == (
        "abfe3632d9384bc8d3e612c747c5df3898e3b30270e9cfa16e6ae02f6b1fec4e")
    assert audit_chain.row_hash(audit_chain.GENESIS, values) == (
        "3d1d5518ca5b6fc9cc5aa9412076d87b7006ca186090ecd2d41b2b42c09cde39")
    assert audit_chain.canonical_json(audit_chain.GENESIS, values).startswith(
        '["abfe3632d9384bc8d3e612c747c5df3898e3b30270e9cfa16e6ae02f6b1fec4e",'
        '"2026-03-01T00:00:00","owner",')


def test_null_and_empty_string_are_distinct():
    base = {f: None for f in audit_chain.FIELDS}
    base.update(occurred_at="t", actor="owner", entity="e", action="create")
    with_null = audit_chain.row_hash(audit_chain.GENESIS, base)
    with_empty = audit_chain.row_hash(audit_chain.GENESIS,
                                      {**base, "before_json": ""})
    assert with_null != with_empty


def test_compute_seal_hash_deterministic():
    values = {"sealed_at": "2026-03-01T03:30:00", "last_id": 5,
              "last_hash": "a" * 64, "row_count": 5}
    h1 = audit_chain.compute_seal_hash(audit_chain.GENESIS, values)
    h2 = audit_chain.compute_seal_hash(audit_chain.GENESIS, dict(values))
    assert h1 == h2
    assert h1 != audit_chain.compute_seal_hash("b" * 64, values)


# ---------------------------------------------------------------- ORM 链


def test_record_chains_rows_and_head(chain_env):
    path, Session = chain_env
    db = Session()
    _seed(db, 3)
    rows = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    assert rows[0].prev_hash == audit_chain.GENESIS
    for prev, cur in zip(rows, rows[1:]):
        assert cur.prev_hash == audit_chain.row_hash(prev.prev_hash, prev)
    state = db.get(AuditChainState, 1)
    assert (state.last_id, state.row_count) == (3, 3)
    assert state.last_hash == audit_chain.row_hash(rows[-1].prev_hash, rows[-1])
    report = audit_chain.verify(db)
    assert report["ok"] is True and report["checked"] == 3
    assert report["head"]["last_id"] == 3 and report["genesis_ok"] is True
    assert audit_chain.head(db)["last_hash"] == state.last_hash
    db.close()


def test_cas_conflict_raises_409(chain_env, monkeypatch):
    path, Session = chain_env
    db = Session()
    _seed(db, 1)
    monkeypatch.setattr(audit_chain, "_read_state",
                        lambda _db: (0, audit_chain.GENESIS, 0))
    with pytest.raises(ApiError) as ei:
        audit_service.record(db, dict(CHANGE, row_id=99))
    assert ei.value.code == ErrorCode.AUDIT_CHAIN_CONFLICT
    assert ei.value.status_code == 409
    db.rollback()
    db.close()


def test_ensure_state_row_seeds_empty_db(chain_env):
    path, Session = chain_env
    db = Session()
    db.execute(AuditLog.__table__.delete())  # 无触发器，清空便于断言
    db.execute(AuditChainState.__table__.delete())
    db.commit()
    assert audit_chain.ensure_state_row(db) == (0, audit_chain.GENESIS, 0)
    state = db.get(AuditChainState, 1)
    assert state.last_hash == audit_chain.GENESIS and state.row_count == 0
    db.close()


def test_ensure_state_row_uninitialized_with_rows(chain_env):
    path, Session = chain_env
    db = Session()
    db.execute(AuditChainState.__table__.delete())
    db.add(AuditLog(occurred_at="t", actor="owner", entity="e", action="create"))
    db.commit()
    with pytest.raises(ApiError) as ei:
        audit_chain.ensure_state_row(db)
    assert ei.value.code == ErrorCode.AUDIT_CHAIN_UNINITIALIZED
    assert ei.value.status_code == 500
    db.close()


# ---------------------------------------------------------------- 0018 回填


def _legacy_audit_db(path, rows=3, with_idem=True):
    conn = sqlite3.connect(path)
    _run_script(conn, M0009)
    if with_idem:
        _run_script(conn, M0010)
    for i in range(rows):
        cols = "occurred_at, actor, entity, action, row_id, after_json"
        vals = ["2026-03-%02dT00:00:00" % (i + 1), "owner", "asset_snapshot",
                "create", i + 1, '{"amount":"%d.00"}' % (100 + i)]
        if with_idem:
            cols += ", idempotency_key"
            vals.append(None)
        conn.execute(f"INSERT INTO audit_log ({cols}) VALUES"
                     f" ({', '.join('?' for _ in vals)})", tuple(vals))
    conn.commit()
    return conn


def test_0018_backfills_and_double_run_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = _legacy_audit_db(path, 3)
    _run_script(conn, M0018)
    first = conn.execute(
        "SELECT id, prev_hash FROM audit_log ORDER BY id").fetchall()
    state = conn.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state").fetchone()

    # 触发器已重建：UPDATE/DELETE 仍 ABORT
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET actor='x' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=1")

    # 第二次 upgrade：状态行存在 → 整体跳过
    _run_script(conn, M0018)
    second = conn.execute(
        "SELECT id, prev_hash FROM audit_log ORDER BY id").fetchall()
    assert first == second
    assert conn.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state"
    ).fetchone() == state
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE prev_hash IS NULL").fetchone()[0] == 0
    assert state[0] == 3 and state[2] == 3
    conn.close()


def test_0018_missing_idempotency_key_raises(tmp_path):
    path = str(tmp_path / "old.db")
    conn = _legacy_audit_db(path, 1, with_idem=False)
    with pytest.raises(RuntimeError) as ei:
        _run_script(conn, M0018)
    assert "idempotency_key" in str(ei.value)
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE prev_hash IS NULL").fetchone()[0] == 1
    conn.close()


def test_0018_empty_table_seeds_genesis(tmp_path):
    path = str(tmp_path / "empty.db")
    conn = _legacy_audit_db(path, 0)
    _run_script(conn, M0018)
    state = conn.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state").fetchone()
    assert state == (0, audit_chain.GENESIS, 0)
    conn.close()


# ---------------------------------------------------------------- ensure_chain


def test_ensure_chain_seeds_empty_and_continues_null_tail(tmp_path):
    path = str(tmp_path / "selfheal.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    assert audit_chain.ensure_chain(path) is True
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT last_id, row_count FROM audit_chain_state").fetchone() == (0, 0)
    conn.close()

    # 有 3 条链行后模拟回滚期新增 2 条 NULL 行
    _, Session = path, sessionmaker(bind=engine)
    db = Session()
    _seed(db, 3)
    db.close()
    conn = sqlite3.connect(path)
    for i in range(4, 6):
        conn.execute(
            "INSERT INTO audit_log (occurred_at, actor, entity, action, row_id)"
            " VALUES (?, 'owner', 'asset_snapshot', 'create', ?)",
            (f"2026-04-0{i}T00:00:00", i))
    conn.commit()
    conn.close()

    assert audit_chain.ensure_chain(path) is True
    db = Session()
    assert audit_chain.verify(db)["ok"] is True
    assert db.get(AuditChainState, 1).last_id == 5
    # 续接后 update 触发器恢复
    with pytest.raises(IntegrityError):
        db.execute(AuditLog.__table__.update().values(actor="x"))
    db.rollback()
    db.close()


def test_ensure_chain_refuses_state_mismatch(chain_env):
    path, Session = chain_env
    db = Session()
    _seed(db, 3)
    db.close()
    _sql(path, "UPDATE audit_chain_state SET last_hash=? WHERE id=1",
         ("deadbeef" * 8,))
    assert audit_chain.ensure_chain(path) is False


# ---------------------------------------------------------------- verify 判定码


def _tampered(chain_env, statement, params=()):
    path, Session = chain_env
    db = Session()
    _seed(db, 5)
    db.close()
    _drop_audit_triggers(path)
    _sql(path, statement, params)
    return Session()


def test_verify_genesis_mismatch(chain_env):
    db = _tampered(chain_env, "UPDATE audit_log SET prev_hash=? WHERE id=1",
                   ("0" * 64,))
    report = audit_chain.verify(db)
    assert report["ok"] is False
    assert report["first_broken"] == {"id": 1, "reason": "genesis_mismatch"}
    assert report["genesis_ok"] is False
    db.close()


def test_verify_link_broken(chain_env):
    db = _tampered(chain_env, "UPDATE audit_log SET before_json=? WHERE id=2",
                   ('{"tampered":true}',))
    report = audit_chain.verify(db)
    assert report["first_broken"]["reason"] == "link_broken"
    assert report["first_broken"]["id"] == 2
    assert report["ok"] is False
    db.close()


def test_verify_row_missing(chain_env):
    db = _tampered(chain_env, "DELETE FROM audit_log WHERE id=2")
    report = audit_chain.verify(db)
    assert report["first_broken"] == {"id": 2, "reason": "row_missing"}
    db.close()


def test_verify_head_mismatch(chain_env):
    db = _tampered(chain_env,
                   "UPDATE audit_chain_state SET last_hash=? WHERE id=1",
                   ("f" * 64,))
    report = audit_chain.verify(db)
    assert report["first_broken"]["reason"] == "head_mismatch"
    db.close()


def test_verify_unchained_row(chain_env):
    db = _tampered(chain_env, "UPDATE audit_log SET prev_hash=NULL WHERE id=3")
    report = audit_chain.verify(db)
    assert report["first_broken"] == {"id": 3, "reason": "unchained_row"}
    assert report["unchained_rows"] == 1
    db.close()


def test_verify_seal_mismatch_and_tail_truncated(chain_env):
    path, Session = chain_env
    db = Session()
    _seed(db, 5)
    audit_chain.seal(db, date(2026, 3, 1))
    db.commit()
    assert audit_chain.verify(db)["seals"] == {
        "checked": 1, "first_broken_seal_id": None, "tail_truncated": False}

    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS audit_chain_seals_no_update")
    conn.execute("UPDATE audit_chain_seals SET last_hash=? WHERE id=1",
                 ("e" * 64,))
    conn.commit()
    conn.close()
    report = audit_chain.verify(db)
    assert report["seals"]["first_broken_seal_id"] == 1
    assert report["ok"] is False

    # 尾部截断：删掉链尾两行 → 封存 last_id(5) > 当前 max_id(3)
    _drop_audit_triggers(path)
    _sql(path, "DELETE FROM audit_log WHERE id>3")
    report = audit_chain.verify(db)
    assert report["seals"]["tail_truncated"] is True
    assert report["ok"] is False
    db.close()


def test_verify_pagination_next_from_id(chain_env):
    path, Session = chain_env
    db = Session()
    _seed(db, 7)
    first = audit_chain.verify(db, limit=3)
    assert first["checked"] == 3 and first["truncated"] is True
    assert first["next_from_id"] == 3 and first["to_id"] == 3
    second = audit_chain.verify(db, from_id=3, limit=3)
    assert second["checked"] == 3 and second["from_id"] == 3
    tail = audit_chain.verify(db, from_id=6)
    assert tail["checked"] == 1 and tail["truncated"] is False
    assert tail["ok"] is True
    db.close()


# ---------------------------------------------------------------- 日封存


def test_seal_single_per_day_and_prev_link(chain_env):
    path, Session = chain_env
    db = Session()
    _seed(db, 2)
    first = audit_chain.seal(db, date(2026, 3, 1))
    db.commit()
    again = audit_chain.seal(db, date(2026, 3, 1))
    assert again["id"] == first["id"]
    _seed(db, 1)
    next_day = audit_chain.seal(db, date(2026, 3, 2))
    db.commit()
    assert next_day["prev_seal_hash"] is not None
    assert next_day["last_id"] == 3
    head = audit_chain.head(db)
    assert head["latest_seal"]["id"] == next_day["id"]
    assert audit_chain.verify(db)["seals"]["checked"] == 2
    db.close()


# ---------------------------------------------------------------- 并发 / 单写者


def _thread_writer(Session, marker, results):
    attempt = 0
    while attempt < 200:
        db = Session()
        try:
            audit_service.record(db, dict(CHANGE, row_id=attempt + 1,
                                          after={"marker": marker}))
            db.commit()
            results.append(attempt)
            db.close()
            return
        except (ApiError, OperationalError):
            db.rollback()
            db.close()
            attempt += 1
            time.sleep(0.002)


def test_two_writers_with_retry_no_fork(chain_env):
    path, Session = chain_env
    results = []
    threads = [threading.Thread(target=lambda m=m: _thread_writer(Session, m, results))
               for m in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db = Session()
    rows = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    assert len(rows) == 2
    report = audit_chain.verify(db)
    assert report["ok"] is True and report["checked"] == 2
    assert db.get(AuditChainState, 1).row_count == 2
    db.close()


def test_single_writer_scan():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    hits = set()
    for pattern in ("services/*.py", "routers/*.py"):
        for path in root.glob(pattern):
            if "INSERT INTO audit_log" in path.read_text(encoding="utf-8"):
                hits.add(path.name)
            if "AuditLog(" in path.read_text(encoding="utf-8"):
                hits.add(path.name)
    assert hits <= {"audit_service.py", "audit_chain.py"}


# ---------------------------------------------------------------- 外锚 / 端点


def test_backup_manifest_audit_head(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "person_fin.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE audit_chain_state (id INTEGER PRIMARY KEY, algorithm TEXT,"
        " last_id INTEGER, last_hash TEXT, row_count INTEGER, updated_at TEXT)")
    conn.execute("INSERT INTO audit_chain_state VALUES (1,'sha256.v1',7,?,7,'t')",
                 ("a" * 64,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(path))

    info = backup_service.create_backup()
    with zipfile.ZipFile(info["path"]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["audit_head"] == {
        "algorithm": "sha256.v1", "last_id": 7, "last_hash": "a" * 64,
        "row_count": 7}


def test_chain_endpoints_direct(chain_env):
    from routers.audit_log import chain_head, chain_verify
    path, Session = chain_env
    db = Session()
    _seed(db, 2)
    assert chain_head(db=db)["last_id"] == 2
    verify = chain_verify(from_id=0, to_id=None, limit=5000, db=db)
    assert verify["ok"] is True and verify["checked"] == 2
    db.close()