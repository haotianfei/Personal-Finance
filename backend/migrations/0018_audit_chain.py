"""0018：审计 hash-chain（链状态＋日封存＋一次性回填 `prev_hash`）。

规格：`.scratch/phase3-advisor/spec.md` v1.0 §4.4/§8.1；决策 ADR-0013 D10–D12。

- 建 `audit_chain_state`（单行 id=1：algorithm/last_id/last_hash/row_count）与
  `audit_chain_seals`（日封存自链，append-only 触发器）。
- 回填（单事务）：状态行已存在 → **整体跳过**；空表 → 种 `(0,GENESIS,0)`；
  否则临时删 `audit_log_no_update`，按 id 升序逐行计算 row_hash 并 UPDATE
  `prev_hash`，写状态行，**重建触发器**，并断言 count/last_id/无 NULL。
- 兼容：要求 `audit_log.idempotency_key` 列存在（0010）；缺失 → 显式报错不猜。
- 幂等：DDL 全 IF NOT EXISTS；状态行存在即回填跳过；无 CLAIM、无 down。
  回滚后新增行 `prev_hash=NULL`，由启动 `audit_chain.ensure_chain()` 续接。
"""
import hashlib
import json
from datetime import datetime

ALGORITHM = "sha256.v1"
FIELDS = (
    "occurred_at", "actor", "entity", "action", "row_id",
    "before_json", "after_json", "evidence_ref", "request_id", "idempotency_key",
)
GENESIS = hashlib.sha256(b"person_f.audit.genesis.v1").hexdigest()
_AUDIT_SELECT_COLS = "id, " + ", ".join(FIELDS) + ", prev_hash"

_TRIGGER_UPDATE = (
    "CREATE TRIGGER IF NOT EXISTS audit_log_no_update"
    " BEFORE UPDATE ON audit_log"
    " FOR EACH ROW BEGIN"
    "   SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE forbidden');"
    " END"
)


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _row_hash(prev: str, values: dict) -> str:
    payload = json.dumps(
        [prev] + [values.get(f) for f in FIELDS],
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat()


def _create_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_chain_state ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  algorithm TEXT NOT NULL,"
        "  last_id INTEGER NOT NULL,"
        "  last_hash CHAR(64) NOT NULL,"
        "  row_count INTEGER NOT NULL,"
        "  updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_chain_seals ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  sealed_date TEXT NOT NULL UNIQUE,"
        "  sealed_at TEXT NOT NULL,"
        "  algorithm TEXT NOT NULL,"
        "  last_id INTEGER NOT NULL,"
        "  last_hash CHAR(64) NOT NULL,"
        "  row_count INTEGER NOT NULL,"
        "  prev_seal_hash CHAR(64) NOT NULL,"
        "  seal_hash CHAR(64) NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_chain_seals_sealed_at"
        " ON audit_chain_seals (sealed_at)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_chain_seals_no_update"
        " BEFORE UPDATE ON audit_chain_seals"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'audit_chain_seals is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_chain_seals_no_delete"
        " BEFORE DELETE ON audit_chain_seals"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'audit_chain_seals is append-only: DELETE forbidden');"
        " END"
    )


def _write_state(conn, last_id: int, last_hash: str, row_count: int):
    conn.execute(
        "INSERT INTO audit_chain_state"
        " (id, algorithm, last_id, last_hash, row_count, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET last_id=excluded.last_id,"
        " last_hash=excluded.last_hash, row_count=excluded.row_count,"
        " updated_at=excluded.updated_at",
        (ALGORITHM, last_id, last_hash, row_count, _now()))


def _row_dict(row) -> dict:
    return dict(zip(("id",) + FIELDS + ("prev_hash",), row))


def _backfill(conn):
    if "audit_log" not in _table_names(conn):
        return
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    if count == 0:
        _write_state(conn, 0, GENESIS, 0)
        return
    if "idempotency_key" not in _columns(conn, "audit_log"):
        raise RuntimeError(
            "0018 中止：audit_log.idempotency_key 列缺失（需先执行 0010）")

    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    try:
        prev = GENESIS
        max_id = 0
        for row in conn.execute(
                f"SELECT {_AUDIT_SELECT_COLS} FROM audit_log ORDER BY id"
        ).fetchall():
            data = _row_dict(row)
            row_id = data["id"]
            new_hash = _row_hash(prev, data)
            if data.get("prev_hash") != prev:
                conn.execute("UPDATE audit_log SET prev_hash=? WHERE id=?",
                             (prev, row_id))
            prev = new_hash
            max_id = row_id
        _write_state(conn, max_id, prev, count)
        _assert(conn, max_id, count)
    finally:
        conn.execute(_TRIGGER_UPDATE)


def _assert(conn, last_id: int, count: int):
    actual_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    actual_max = int(conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE prev_hash IS NULL").fetchone()[0]
    if actual_count != count or actual_max != last_id or nulls:
        raise RuntimeError(
            f"0018 断言失败：count={actual_count}/{count} "
            f"last_id={actual_max}/{last_id} null_prev={nulls}")
    state = conn.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state WHERE id=1"
    ).fetchone()
    if state is None or state[0] != last_id or state[2] != count:
        raise RuntimeError("0018 断言失败：状态行与 audit_log 不一致")


def upgrade(conn):
    _create_tables(conn)
    exists = conn.execute(
        "SELECT 1 FROM audit_chain_state WHERE id=1").fetchone()
    if exists:
        return                       # 已回填 → 整体跳过（幂等）
    _backfill(conn)