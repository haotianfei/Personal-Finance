"""重建留痕与旧链桥（`.scratch/audit-best-practice/issues/04`）测试。

覆盖：旧 `audit_log` 逐行原样导出（行数/字段/id 升序）；链头重算与 JSONL sha256；
产物只留文件名（公开安全红线：JSON 里不含 tmp 路径、返回值不含金额/机构名）；
`record_rebuild` 在新库写**恰好一条** `schema/rebuild_import` 且 after 键集固定、
不含金额与绝对路径；写入后 `audit_chain.verify()` 仍 ok（链能续接）；
旧库缺 `audit_log` 表或根本不存在时跳过而不抛。

数据全部合成（金额、机构名均为示例）。
"""
import hashlib
import json
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from models import AuditLog
from services import audit_chain
from services.rebuild_audit_service import (
    CHAIN_EXPORT_VERSION, export_chain_bridge, record_rebuild)

# 源库文件名（bridge 的 source_label 只许出现这个，不许出现目录）
SOURCE_LABEL = "person_fin_archived.db"
AMOUNT = "1234.56"                      # 示例金额：只许留在本机副档里
INSTITUTION = "示例银行某支行"           # 示例机构名：同上

# 旧链三行（迁移记账 / 新增快照 / 更正快照）——after/before 里带金额与机构名
OLD_ROWS = [
    {"occurred_at": "2026-10-01T09:00:00", "actor": "system", "entity": "schema",
     "action": "migration_run", "row_id": None, "before_json": None,
     "after_json": json.dumps({"version": 35}, ensure_ascii=False),
     "evidence_ref": None, "request_id": None, "idempotency_key": None},
    {"occurred_at": "2026-10-02T10:00:00", "actor": "owner", "entity": "asset_snapshot",
     "action": "create", "row_id": 7, "before_json": None,
     "after_json": json.dumps({"amount": AMOUNT, "institution": INSTITUTION},
                              ensure_ascii=False),
     "evidence_ref": None, "request_id": "req-1", "idempotency_key": None},
    {"occurred_at": "2026-10-03T11:00:00", "actor": "owner", "entity": "asset_snapshot",
     "action": "correct", "row_id": 7,
     "before_json": json.dumps({"amount": AMOUNT}, ensure_ascii=False),
     "after_json": json.dumps({"amount": "9999.99"}, ensure_ascii=False),
     "evidence_ref": None, "request_id": None, "idempotency_key": "idem-1"},
]

_OLD_DDL = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    entity TEXT,
    action TEXT,
    row_id INTEGER,
    before_json TEXT,
    after_json TEXT,
    evidence_ref INTEGER,
    request_id TEXT,
    idempotency_key TEXT,
    prev_hash VARCHAR(64)
);
CREATE TABLE audit_chain_state (
    id INTEGER PRIMARY KEY,
    algorithm TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    last_hash VARCHAR(64) NOT NULL,
    row_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE audit_chain_seals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sealed_date TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    last_hash VARCHAR(64) NOT NULL,
    row_count INTEGER NOT NULL,
    prev_seal_hash VARCHAR(64) NOT NULL,
    seal_hash VARCHAR(64) NOT NULL
);
"""


def _build_source_db(path, *, with_audit_log: bool = True) -> dict:
    """造 mini 旧库：链自洽的 audit_log ＋ 状态行 ＋ 一条封存。返回期望值与文件。"""
    con = sqlite3.connect(path)
    try:
        if not with_audit_log:
            con.executescript("CREATE TABLE unrelated (id INTEGER PRIMARY KEY);")
            con.commit()
            return {"head": None, "seal_hash": None}

        con.executescript(_OLD_DDL)
        cols = ["id", "prev_hash"] + list(audit_chain.FIELDS)
        prev = audit_chain.GENESIS
        for index, row in enumerate(OLD_ROWS, start=1):
            values = {name: row.get(name) for name in audit_chain.FIELDS}
            payload = {"id": index, "prev_hash": prev, **values}
            con.execute(
                f"INSERT INTO audit_log ({', '.join(cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                tuple(payload[c] for c in cols))
            prev = audit_chain.row_hash(prev, values)   # 与生产同一哈希函数
        head = prev

        seal_values = {"sealed_at": "2026-10-03T23:59:00", "last_id": len(OLD_ROWS),
                       "last_hash": head, "row_count": len(OLD_ROWS)}
        seal_hash = audit_chain.compute_seal_hash(audit_chain.GENESIS, seal_values)
        con.execute(
            "INSERT INTO audit_chain_seals (sealed_date, sealed_at, algorithm, last_id,"
            " last_hash, row_count, prev_seal_hash, seal_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-03", seal_values["sealed_at"], audit_chain.ALGORITHM,
             seal_values["last_id"], head, seal_values["row_count"],
             audit_chain.GENESIS, seal_hash))
        con.execute(
            "INSERT INTO audit_chain_state (id, algorithm, last_id, last_hash,"
            " row_count, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
            (audit_chain.ALGORITHM, len(OLD_ROWS), head, len(OLD_ROWS),
             "2026-10-03T23:59:00"))
        con.commit()
        return {"head": head, "seal_hash": seal_hash}
    finally:
        con.close()


def _table_columns(path, table: str) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info('{table}')")]
    finally:
        con.close()


def _rebuild_audit_rows(db) -> list:
    return list(db.execute(
        select(AuditLog)
        .where(AuditLog.entity == "schema", AuditLog.action == "rebuild_import")
    ).scalars().all())


# ---------------------------------------------------------------- 1/2. 副档内容


def test_jsonl_is_verbatim_copy_with_all_fields(tmp_path):
    src = tmp_path / SOURCE_LABEL
    _build_source_db(src)

    bridge = export_chain_bridge(src, tmp_path)
    assert not bridge.get("skipped")
    jsonl = tmp_path / bridge["jsonl_file"]

    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(OLD_ROWS) == bridge["source_rows"]
    parsed = [json.loads(line) for line in lines]        # 逐行 JSON 可解析
    assert [p["id"] for p in parsed] == [1, 2, 3]        # 按 id 升序

    expected_cols = set(_table_columns(src, "audit_log"))
    for got, want in zip(parsed, OLD_ROWS):
        assert set(got) == expected_cols                 # 字段完整（全部物理列）
        for key, value in want.items():
            assert got[key] == value                     # 原样，不重序列化
        assert got["prev_hash"] == _expected_prev_hash(parsed, got["id"])

    # 金额/机构名只在副档里（brief：本机产物可以保留原样）
    assert AMOUNT in jsonl.read_text(encoding="utf-8")


def _expected_prev_hash(parsed: list[dict], row_id: int) -> str:
    prev = audit_chain.GENESIS
    for row in parsed:
        if row["id"] == row_id:
            return prev
        prev = audit_chain.row_hash(
            prev, {name: row.get(name) for name in audit_chain.FIELDS})
    raise AssertionError(f"没找到 id={row_id}")


def test_bridge_head_and_jsonl_sha256_match(tmp_path):
    src = tmp_path / SOURCE_LABEL
    info = _build_source_db(src)

    bridge = export_chain_bridge(src, tmp_path)
    jsonl = tmp_path / bridge["jsonl_file"]
    bridge_file = tmp_path / bridge["bridge_file"]

    assert bridge["version"] == CHAIN_EXPORT_VERSION
    assert bridge["source_head"] == info["head"]          # 链头＝旧库链头
    assert bridge["jsonl_sha256"] == hashlib.sha256(jsonl.read_bytes()).hexdigest()
    assert bridge["jsonl_bytes"] == jsonl.stat().st_size
    assert bridge["source_chain_ok"] is True
    assert [s["seal_hash"] for s in bridge["source_seals"]] == [info["seal_hash"]]
    assert bridge["bridge_sha256"] == hashlib.sha256(bridge_file.read_bytes()).hexdigest()

    on_disk = json.loads(bridge_file.read_text(encoding="utf-8"))
    assert on_disk["source_head"] == info["head"]         # 落盘与返回值同源
    assert on_disk["source_label"] == SOURCE_LABEL
    assert "bridge_file" not in on_disk                   # 自引用字段只在返回值里


# ---------------------------------------------------------------- 3. 公开安全


def test_artifacts_carry_file_names_only(tmp_path):
    src = tmp_path / SOURCE_LABEL
    _build_source_db(src)
    out = tmp_path / "new"                                # 模拟 data/rebuild/new/

    bridge = export_chain_bridge(src, out)
    jsonl_text = (out / bridge["jsonl_file"]).read_text(encoding="utf-8")
    bridge_text = (out / bridge["bridge_file"]).read_text(encoding="utf-8")

    for text in (jsonl_text, bridge_text):
        assert str(tmp_path) not in text                  # 无绝对路径
        assert str(out) not in text
    assert "/" not in json.loads(bridge_text)["source_label"]   # label 只是文件名
    assert bridge["jsonl_file"] == f"audit-chain-{_stamp(bridge)}.jsonl"
    assert bridge["bridge_file"] == f"audit-bridge-{_stamp(bridge)}.json"

    # 返回值同样不带路径、不带载荷（金额/机构名）
    blob = json.dumps(bridge, ensure_ascii=False)
    assert str(tmp_path) not in blob
    for payload_bit in (AMOUNT, INSTITUTION):
        assert payload_bit not in blob


def _stamp(bridge: dict) -> str:
    """从产物文件名反推时间戳段（两文件必须同名时间戳）。"""
    return bridge["jsonl_file"][len("audit-chain-"):-len(".jsonl")]


# ---------------------------------------------------------------- 4/5. 新库留痕


def test_record_rebuild_writes_single_row_and_chain_stays_ok(db, tmp_path):
    src = tmp_path / SOURCE_LABEL
    _build_source_db(src)
    bridge = export_chain_bridge(src, tmp_path / "new")

    record_rebuild(db, bridge=bridge, script="s4_rebuild_db.py", assets_imported=2595)
    db.commit()

    rows = _rebuild_audit_rows(db)
    assert len(rows) == 1                                  # 恰好一条
    row = rows[0]
    assert (row.actor, row.entity, row.action) == ("system", "schema", "rebuild_import")
    assert row.reason is None and row.reason_note is None   # 系统上下文不落 reason

    after = json.loads(row.after_json)
    assert set(after) == {                                 # 键集固定：只有事实
        "op", "assets_imported", "source_rows", "source_head",
        "source_seals", "bridge_file", "bridge_sha256", "script", "version"}
    assert after["op"] == "rebuild_import"
    assert after["assets_imported"] == 2595
    assert after["source_rows"] == len(OLD_ROWS)
    assert after["source_head"] == bridge["source_head"][:16]
    assert after["source_seals"] == 1
    assert after["bridge_file"] == bridge["bridge_file"]
    assert after["bridge_sha256"] == bridge["bridge_sha256"][:16]
    assert after["script"] == "s4_rebuild_db.py"
    assert after["version"] == CHAIN_EXPORT_VERSION

    payload = row.after_json + (row.before_json or "")
    assert str(tmp_path) not in payload                    # 无绝对路径
    for payload_bit in (AMOUNT, INSTITUTION, "9999.99"):
        assert payload_bit not in payload                  # 无金额/真名

    report = audit_chain.verify(db)
    assert report["ok"] is True, report                    # 链能续接
    assert report["head"]["last_id"] == row.id
    assert report["checked"] == 1


def test_record_rebuild_over_raw_connection(tmp_path):
    """s4 的真实通道：裸 sqlite3.Connection + 显式事务。"""
    src = tmp_path / SOURCE_LABEL
    _build_source_db(src)
    bridge = export_chain_bridge(src, tmp_path / "new")

    db_path = tmp_path / "new.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        record_rebuild(conn, bridge=bridge, script="s4_rebuild_db.py",
                       assets_imported=3)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    Session = sessionmaker(bind=engine)
    with Session() as db:
        rows = _rebuild_audit_rows(db)
        assert len(rows) == 1
        assert json.loads(rows[0].after_json)["assets_imported"] == 3
        assert audit_chain.verify(db)["ok"] is True
    engine.dispose()


# ---------------------------------------------------------------- 6. 跳过而降级


def test_skips_when_source_has_no_audit_log_table(db, tmp_path):
    src = tmp_path / SOURCE_LABEL
    _build_source_db(src, with_audit_log=False)
    out = tmp_path / "new"

    result = export_chain_bridge(src, out)                 # 不抛异常
    assert result["skipped"] is True
    assert result["reason"] == "source_audit_log_missing"
    assert result["source_label"] == SOURCE_LABEL
    assert not out.exists()                                # 不写半截副档

    # 无副档也要留痕：显式标注桥不存在，形状不缩水
    record_rebuild(db, bridge=result, script="s4_rebuild_db.py", assets_imported=2595)
    db.commit()
    row = _rebuild_audit_rows(db)[0]
    after = json.loads(row.after_json)
    assert after["bridge_skipped"] == "source_audit_log_missing"
    assert after["bridge_file"] is None and after["source_head"] is None
    assert after["assets_imported"] == 2595
    assert str(tmp_path) not in row.after_json
    assert audit_chain.verify(db)["ok"] is True


def test_skips_when_source_db_missing(tmp_path):
    out = tmp_path / "new"
    result = export_chain_bridge(tmp_path / "nope.db", out)
    assert result["skipped"] is True
    assert result["reason"] == "source_db_missing"
    assert not out.exists()