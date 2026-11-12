"""三期-3 W1：审计 hash-chain 底座（ADR-0013 D10/D11/D12；spec §4）。

形态（跨实现冻结，勿漂移）：
    ALGORITHM = "sha256.v1"
    FIELDS = occurred_at / actor / entity / action / row_id /
             before_json / after_json / evidence_ref / request_id / idempotency_key
    GENESIS = sha256(b"person_f.audit.genesis.v1").hexdigest()
    row_hash(prev, values) = sha256(utf8(json.dumps([prev] + FIELDS values,
                               ensure_ascii=False, separators=(",", ":"))))
- `before_json/after_json` 取**库内已存文本原样**（不重序列化）；NULL≠""。
- `prev_hash`＝前一行的 row_hash；首行＝GENESIS；本行 id 不参与哈希。
- 插入同事务：逐行算 prev_hash → INSERT → `audit_chain_state` CAS
  （`WHERE id=1 AND last_id=:expected`）；rowcount≠1 → 409 AUDIT_CHAIN_CONFLICT。
- 日封存：`audit_chain_seals(prev_seal_hash→seal_hash)`，写日志＋备份 manifest。
- 两段初始化：`ensure_state_row()`（请求内可调，空库种状态；有行缺状态→500）
  与 `ensure_chain()`（**仅启动/迁移**，可临时摘 no_update 触发器做回填/续接）。
"""
import hashlib
import json
import logging
import os
import sqlite3
from datetime import date, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import AuditChainSeal, AuditChainState, AuditLog

logger = logging.getLogger(__name__)

ALGORITHM = "sha256.v1"
FIELDS = (
    "occurred_at", "actor", "entity", "action", "row_id",
    "before_json", "after_json", "evidence_ref", "request_id", "idempotency_key",
)
SEAL_FIELDS = ("sealed_at", "last_id", "last_hash", "row_count")

GENESIS = hashlib.sha256(b"person_f.audit.genesis.v1").hexdigest()

_MAX_LIMIT = 20000
_AUDIT_SELECT_COLS = "id, " + ", ".join(FIELDS) + ", prev_hash"
_AUDIT_LABELS = ("id",) + FIELDS + ("prev_hash",)


def _row_dict(row) -> dict:
    """裸 sqlite3 行（tuple）→ 字段字典（默认连接未设 row_factory）。"""
    return dict(zip(_AUDIT_LABELS, row))
_TRIGGER_UPDATE = (
    "CREATE TRIGGER IF NOT EXISTS audit_log_no_update"
    " BEFORE UPDATE ON audit_log"
    " FOR EACH ROW BEGIN"
    "   SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE forbidden');"
    " END"
)
_TRIGGER_SEAL_NO_UPDATE = (
    "CREATE TRIGGER IF NOT EXISTS audit_chain_seals_no_update"
    " BEFORE UPDATE ON audit_chain_seals"
    " FOR EACH ROW BEGIN"
    "   SELECT RAISE(ABORT, 'audit_chain_seals is append-only: UPDATE forbidden');"
    " END"
)
_TRIGGER_SEAL_NO_DELETE = (
    "CREATE TRIGGER IF NOT EXISTS audit_chain_seals_no_delete"
    " BEFORE DELETE ON audit_chain_seals"
    " FOR EACH ROW BEGIN"
    "   SELECT RAISE(ABORT, 'audit_chain_seals is append-only: DELETE forbidden');"
    " END"
)


# ------------------------------------------------------------------ 纯函数


def canonical_json(prev: str, values) -> str:
    """链哈希输入（跨实现冻结）：`[prev] + FIELDS`，紧凑 JSON、不转义中文。"""
    return json.dumps(
        [prev] + [_field(values, f) for f in FIELDS],
        ensure_ascii=False, separators=(",", ":"))


def _field(values, name: str):
    if isinstance(values, dict):
        return values.get(name)
    return getattr(values, name, None)


def row_hash(prev: str, values) -> str:
    """sha256.v1 行哈希（prev＝前一行的 row_hash 或 GENESIS）。"""
    return hashlib.sha256(canonical_json(prev, values).encode("utf-8")).hexdigest()


def compute_seal_hash(prev_seal_hash: str, values) -> str:
    """封存行哈希（字段冻结）：`[prev_seal_hash] + SEAL_FIELDS`。"""
    payload = json.dumps(
        [prev_seal_hash] + [_field(values, f) for f in SEAL_FIELDS],
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat()


# ------------------------------------------------------------------ 状态行 / 写路径


def ensure_state_row(db: Session) -> tuple[int, str, int]:
    """请求内初始化单点：空库种 `(0, GENESIS, 0)`；有行缺状态 → 500（不猜）。"""
    state = _read_state(db)
    if state is not None:
        return state
    count = db.execute(text("SELECT COUNT(*) FROM audit_log")).scalar() or 0
    if count:
        raise ApiError(
            ErrorCode.AUDIT_CHAIN_UNINITIALIZED, 500,
            "审计链未初始化（0018 未执行或状态行缺失），请运行迁移或 ensure_chain()",
            details={"row_count": int(count)})
    db.execute(text(
        "INSERT OR IGNORE INTO audit_chain_state"
        " (id, algorithm, last_id, last_hash, row_count, updated_at)"
        " VALUES (1, :alg, 0, :genesis, 0, :now)"),
        {"alg": ALGORITHM, "genesis": GENESIS, "now": _now()})
    return (0, GENESIS, 0)


def append(db: Session, row: AuditLog) -> AuditLog:
    """ORM 写路径：算 prev_hash → INSERT → 状态 CAS（同事务；失败 409）。"""
    expected_id, prev_hash, row_count = ensure_state_row(db)
    row.prev_hash = prev_hash
    db.add(row)
    db.flush()
    new_hash = row_hash(prev_hash, row)
    result = db.execute(text(
        "UPDATE audit_chain_state SET last_id=:last_id, last_hash=:last_hash,"
        " row_count=row_count+1, updated_at=:now"
        " WHERE id=1 AND last_id=:expected"),
        {"last_id": row.id, "last_hash": new_hash, "now": _now(),
         "expected": expected_id})
    if result.rowcount != 1:
        raise ApiError(
            ErrorCode.AUDIT_CHAIN_CONFLICT, 409, "审计链并发冲突，请重试",
            details={"expected_last_id": expected_id, "row_id": row.id})
    return row


def append_sqlite(conn: sqlite3.Connection, values: dict) -> int:
    """裸连接写路径（runner / 迁移）：同 ORM 语义，返回 lastrowid。"""
    expected_id, prev_hash, _ = _ensure_state_row_sqlite(conn)
    payload = dict(values)
    payload["prev_hash"] = prev_hash
    cols = [c for c in (*FIELDS, "prev_hash")
            if c in _table_columns(conn, "audit_log")]
    conn.execute(
        f"INSERT INTO audit_log ({', '.join(cols)})"
        f" VALUES ({', '.join('?' for _ in cols)})",
        tuple(payload.get(c) for c in cols))
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    new_hash = row_hash(prev_hash, payload)
    cur = conn.execute(
        "UPDATE audit_chain_state SET last_id=?, last_hash=?,"
        " row_count=row_count+1, updated_at=? WHERE id=1 AND last_id=?",
        (row_id, new_hash, _now(), expected_id))
    if cur.rowcount != 1:
        raise ApiError(
            ErrorCode.AUDIT_CHAIN_CONFLICT, 409, "审计链并发冲突，请重试",
            details={"expected_last_id": expected_id, "row_id": row_id})
    return row_id


# ------------------------------------------------------------------ 校验


def head(db: Session) -> dict:
    """链头（含最新封存；只读）。"""
    last_id, last_hash, row_count = ensure_state_row(db)
    state = db.get(AuditChainState, 1)
    seal = db.execute(
        select(AuditChainSeal).order_by(AuditChainSeal.id.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "algorithm": ALGORITHM,
        "last_id": last_id,
        "last_hash": last_hash,
        "row_count": row_count,
        "updated_at": state.updated_at if state is not None else None,
        "latest_seal": (_seal_out(seal) if seal is not None else None),
    }


def verify(db: Session, from_id: int = 0, to_id: int | None = None,
           limit: int = 5000) -> dict:
    """链校验（只读；`ok=false` 仍 200）。返回 spec §4.6 形状。"""
    state_id, state_hash, state_count = ensure_state_row(db)
    limit = max(1, min(int(limit or 5000), _MAX_LIMIT))

    query = select(AuditLog).where(AuditLog.id > from_id)
    if to_id is not None:
        query = query.where(AuditLog.id <= to_id)
    rows = list(db.execute(
        query.order_by(AuditLog.id).limit(limit + 1)).scalars().all())
    truncated = len(rows) > limit
    rows = rows[:limit]

    first_broken = None
    unchained = 0
    expected_id = int(from_id) + 1
    genesis_ok = True
    if from_id == 0 and rows:
        genesis_ok = rows[0].prev_hash == GENESIS
        if not genesis_ok and rows[0].prev_hash is not None:
            first_broken = {"id": rows[0].id, "reason": "genesis_mismatch"}
    for index, row in enumerate(rows):
        if row.prev_hash is None:
            unchained += 1
            if first_broken is None:
                first_broken = {"id": row.id, "reason": "unchained_row"}
        if row.id != expected_id and first_broken is None:
            first_broken = {"id": expected_id, "reason": "row_missing"}
        if index > 0:
            prev_row = rows[index - 1]
            if (row.prev_hash is not None and first_broken is None
                    and row.prev_hash != row_hash(prev_row.prev_hash, prev_row)):
                first_broken = {"id": prev_row.id, "reason": "link_broken"}
        expected_id = row.id + 1

    max_id = int(db.execute(select(func.max(AuditLog.id))).scalar() or 0)
    last_checked = rows[-1].id if rows else int(from_id)
    if to_id is None:
        last_checked = rows[-1].id if rows else min(int(from_id), max_id)
    head_payload = {"last_id": state_id, "last_hash": state_hash,
                    "row_count": state_count}

    if (first_broken is None and not truncated and to_id is None
            and last_checked == max_id):
        if rows:
            tail_hash = row_hash(rows[-1].prev_hash, rows[-1])
            if state_id != rows[-1].id or state_hash != tail_hash:
                first_broken = {"id": rows[-1].id, "reason": "head_mismatch"}
        elif state_id != 0 or state_hash != GENESIS:
            first_broken = {"id": 0, "reason": "head_mismatch"}

    seals = _check_seals(db, max_id)
    ok = (first_broken is None
          and seals["first_broken_seal_id"] is None
          and not seals["tail_truncated"])
    return {
        "ok": ok,
        "algorithm": ALGORITHM,
        "checked": len(rows),
        "from_id": int(from_id),
        "to_id": last_checked if to_id is None else int(to_id),
        "truncated": truncated,
        "next_from_id": (rows[-1].id if truncated and rows else None),
        "genesis_ok": genesis_ok,
        "head": head_payload,
        "first_broken": first_broken,
        "seals": seals,
        "unchained_rows": unchained,
    }


def _check_seals(db: Session, max_audit_id: int) -> dict:
    seals = list(db.execute(
        select(AuditChainSeal).order_by(AuditChainSeal.id)).scalars().all())
    prev_hash = GENESIS
    first_broken = None
    for seal in seals:
        expected = compute_seal_hash(prev_hash, seal)
        if seal.prev_seal_hash != prev_hash or seal.seal_hash != expected:
            first_broken = seal.id
            break
        prev_hash = seal.seal_hash
    tail_truncated = any(seal.last_id > max_audit_id for seal in seals)
    return {"checked": len(seals), "first_broken_seal_id": first_broken,
            "tail_truncated": tail_truncated}


# ------------------------------------------------------------------ 日封存


def seal(db: Session, on_date: date | None = None) -> dict:
    """日封存（单日一条，幂等）；prev_seal_hash 自链。返回封存行。"""
    last_id, last_hash, row_count = ensure_state_row(db)
    day = (on_date or date.today()).isoformat()
    existing = db.execute(
        select(AuditChainSeal).where(AuditChainSeal.sealed_date == day)
    ).scalar_one_or_none()
    if existing is not None:
        return _seal_out(existing)

    previous = db.execute(
        select(AuditChainSeal).order_by(AuditChainSeal.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_seal_hash = previous.seal_hash if previous is not None else GENESIS
    values = {"sealed_at": _now(), "last_id": last_id,
              "last_hash": last_hash, "row_count": row_count}
    seal_row = AuditChainSeal(
        sealed_date=day, algorithm=ALGORITHM,
        prev_seal_hash=prev_seal_hash,
        seal_hash=compute_seal_hash(prev_seal_hash, values), **values)
    db.add(seal_row)
    db.flush()
    return _seal_out(seal_row)


def _seal_out(seal_row) -> dict:
    return {
        "id": seal_row.id,
        "sealed_at": seal_row.sealed_at,
        "last_id": seal_row.last_id,
        "last_hash": seal_row.last_hash,
        "row_count": seal_row.row_count,
        "prev_seal_hash": seal_row.prev_seal_hash,
    }


def seal_daily() -> dict | None:
    """调度器入口：封存当日（幂等；失败仅日志，不中断调度）。"""
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            payload = seal(db)
            db.commit()
            logger.info("audit_chain_sealed last_id=%s last_hash=%s sealed_at=%s",
                        payload["last_id"], payload["last_hash"],
                        payload["sealed_at"])
            return payload
    except Exception:
        logger.exception("audit_chain_seal_failed")
        return None


# ------------------------------------------------------------------ 启动/迁移续接


def ensure_chain(db_path: str | None = None) -> bool:
    """启动/迁移专用：建状态、全量回填或从链尾续接 NULL 行。

    返回 True＝链可用；False＝检测到已链行与状态不符（CRITICAL，拒绝续接）。
    **不在请求事务内调用**（会临时摘 audit_log_no_update 触发器）。
    """
    path = db_path or _resolve_db_path()
    if not os.path.exists(path):
        return False
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "audit_chain_state" not in tables or "audit_log" not in tables:
            return False
        conn.execute("BEGIN")
        try:
            state = _read_state_sqlite(conn)
            if state is None:
                ok = _backfill_all(conn)
            else:
                ok = _continue_tail(conn, state)
            if ok:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
            return ok
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _backfill_all(conn) -> bool:
    """全量回填（临时摘 update 触发器）：空表种 GENESIS；否则逐行算链。"""
    if "audit_log" not in _table_names(conn):
        return False
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    _drop_update_trigger(conn)
    try:
        if count == 0:
            _write_state_sqlite(conn, 0, GENESIS, 0)
            return True
        if "idempotency_key" not in _table_columns(conn, "audit_log"):
            raise RuntimeError(
                "0018 中止：audit_log.idempotency_key 列缺失（需先执行 0010）")
        prev = GENESIS
        max_id = 0
        for row in conn.execute(
                f"SELECT {_AUDIT_SELECT_COLS} FROM audit_log ORDER BY id").fetchall():
            data = _row_dict(row)
            row_id = data["id"]
            new_hash = row_hash(prev, data)
            if data.get("prev_hash") != prev:
                conn.execute("UPDATE audit_log SET prev_hash=? WHERE id=?",
                             (prev, row_id))
            prev = new_hash
            max_id = row_id
        _write_state_sqlite(conn, max_id, prev, count)
        _assert_chain(conn, max_id, count)
        return True
    finally:
        conn.execute(_TRIGGER_UPDATE)


def _continue_tail(conn, state) -> bool:
    """状态存在：校验已链前缀，只补 NULL 尾部（不回改已链行）。"""
    last_id, last_hash, row_count = state
    max_id = int(conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0)
    if max_id < last_id:
        logger.critical("audit_chain_tail_lost last_id=%s max_id=%s",
                        last_id, max_id)
        return False

    if last_id:
        anchor = conn.execute(
            f"SELECT {_AUDIT_SELECT_COLS} FROM audit_log WHERE id=?",
            (last_id,)).fetchone()
        if anchor is None:
            logger.critical("audit_chain_anchor_missing last_id=%s", last_id)
            return False
        data = _row_dict(anchor)
        if data.get("prev_hash") is None or row_hash(data["prev_hash"], data) != last_hash:
            logger.critical("audit_chain_head_mismatch last_id=%s", last_id)
            return False

    tail = conn.execute(
        f"SELECT {_AUDIT_SELECT_COLS} FROM audit_log WHERE id>? ORDER BY id",
        (last_id,)).fetchall()
    if not tail:
        return True

    _drop_update_trigger(conn)
    try:
        prev = last_hash
        expected = last_id + 1
        new_max = last_id
        for row in tail:
            data = _row_dict(row)
            if data["id"] != expected:
                logger.critical("audit_chain_gap expected=%s got=%s",
                                expected, data["id"])
                return False
            if data.get("prev_hash") is not None and data["prev_hash"] != prev:
                logger.critical("audit_chain_tail_mismatch id=%s", data["id"])
                return False
            if data.get("prev_hash") is None:
                conn.execute("UPDATE audit_log SET prev_hash=? WHERE id=?",
                             (prev, data["id"]))
            prev = row_hash(prev, data)
            new_max = data["id"]
            expected += 1
        total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        _write_state_sqlite(conn, new_max, prev, total)
        _assert_chain(conn, new_max, total)
        return True
    finally:
        conn.execute(_TRIGGER_UPDATE)


# ------------------------------------------------------------------ 低层工具


def _read_state(db: Session):
    row = db.execute(text(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state WHERE id=1"
    )).first()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2])


def _read_state_sqlite(conn):
    row = conn.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2])


def _ensure_state_row_sqlite(conn) -> tuple[int, str, int]:
    state = _read_state_sqlite(conn)
    if state is not None:
        return state
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    if count:
        raise ApiError(
            ErrorCode.AUDIT_CHAIN_UNINITIALIZED, 500,
            "审计链未初始化（0018 未执行或状态行缺失）",
            details={"row_count": int(count)})
    conn.execute(
        "INSERT OR IGNORE INTO audit_chain_state"
        " (id, algorithm, last_id, last_hash, row_count, updated_at)"
        " VALUES (1, ?, 0, ?, 0, ?)", (ALGORITHM, GENESIS, _now()))
    return (0, GENESIS, 0)


def _write_state_sqlite(conn, last_id: int, last_hash: str, row_count: int):
    conn.execute(
        "INSERT INTO audit_chain_state"
        " (id, algorithm, last_id, last_hash, row_count, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET last_id=excluded.last_id,"
        " last_hash=excluded.last_hash, row_count=excluded.row_count,"
        " updated_at=excluded.updated_at",
        (ALGORITHM, last_id, last_hash, row_count, _now()))


def _assert_chain(conn, last_id: int, count: int):
    actual_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    actual_max = int(conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE prev_hash IS NULL").fetchone()[0]
    if (actual_count != count or actual_max != last_id or nulls):
        raise RuntimeError(
            f"0018 断言失败：count={actual_count}/{count} "
            f"last_id={actual_max}/{last_id} null_prev={nulls}")
    state = _read_state_sqlite(conn)
    if state is None or state[0] != last_id or state[2] != count:
        raise RuntimeError("0018 断言失败：audit_chain_state 与 audit_log 不一致")


def _drop_update_trigger(conn):
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _resolve_db_path() -> str:
    from database import DB_PATH
    return os.environ.get("DATABASE_PATH") or DB_PATH