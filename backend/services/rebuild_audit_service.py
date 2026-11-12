"""离线重建的审计留痕与「旧链桥」（`.scratch/audit-best-practice/issues/04`）。

背景：`scripts/rebuild/s4_rebuild_db.py` 在空库上重建时，按 ADR-0019 / spec D8 把
`audit_log` / `audit_chain_*` 当运维台账整体丢弃。2026-11-04 那次重建的后果：新库
2595 行资产**零创建留痕**，旧链（166 行）只剩在归档库里——两代链之间断代且无桥，
事后无法回答「这批数据从哪一代链来、旧链还在不在、它有没有被改过」。

本模块补的正是这座桥，只做两件事：

1. `export_chain_bridge(source_db, out_dir)` —— **只读**（`mode=ro`，不改旧库一个
   字节）把旧库 `audit_log` 全量导出成 JSONL 副档，并写一份可校验摘要
   （链头 hash / 封存清单 / 副档 sha256）。旧库不存在或没有 `audit_log` 表时
   返回 `{'skipped': True, 'reason': <code>}`，**不抛异常**（重建是应急路径）。
2. `rebuild_change()` / `record_rebuild()` —— 在**新库**写一条
   `entity='schema'` / `action='rebuild_import'` 的审计行，指向上面那份副档。

产物（落在 `out_dir`，通常是 `data/rebuild/new/`，已 gitignore、不入库）：

    audit-chain-<YYYYMMDDTHHMMSS>.jsonl   旧 audit_log 逐行原样（全部字段，id 升序）
    audit-bridge-<同一时间戳>.json        桥摘要（见 `export_chain_bridge` 返回值）

**哈希口径一字不改**：链头用 `services.audit_chain.row_hash` 原函数重算，
`FIELDS` 也取自那里；本模块不复制、不改写哈希实现。

**公开安全红线**（AGENTS.md）：
- JSONL 是**本机产物**，可含金额/机构名（审计行 `after_json` 原文）——这是重建
  副档的价值所在，按 brief 原样保留；
- 但 `export_chain_bridge()` 的**返回值**与 `record_rebuild()` 写的**审计行**只放
  计数、`sha256` 前 16 位与**文件名**：绝不放绝对路径、主机名、用户名、金额、真名。
  `source_label` 因此只有 basename；跳过原因用**代码**（如 `source_db_missing`）
  而不是带路径的句子。
- `reason` / `reason_note`（0036）本次**不填**：系统上下文写入者不落该列，NULL 的
  语义就是「未记录原因」（`tests/test_audit_contract.py::KNOWN_DEBT` 的既有口径）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 副档格式版本（bridge JSON 的 `version`，也是审计行 after.version 的来源）
CHAIN_EXPORT_VERSION = 1

# 审计行上的受控取值（`services.audit_contract` 已登记，勿在此另造拼写）
BRIDGE_ENTITY = "schema"
BRIDGE_ACTION = "rebuild_import"

# 摘要位数：审计行只留前 16 位 hex（够比对，不够被误当完整凭据）
SHORT_HASH_LEN = 16

_JSONL_PREFIX = "audit-chain-"
_BRIDGE_PREFIX = "audit-bridge-"
_TS_FORMAT = "%Y%m%dT%H%M%S"

# 跳过原因（代码，不含路径/主机名）
SKIP_SOURCE_MISSING = "source_db_missing"
SKIP_NO_AUDIT_LOG = "source_audit_log_missing"
SKIP_UNREADABLE = "source_db_unreadable"


# ------------------------------------------------------------------ 小工具


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _short(value) -> str | None:
    """完整 hash → 前 16 位；None/空保持 None（NULL 与 "" 不混）。"""
    if not value:
        return None
    return str(value)[:SHORT_HASH_LEN]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _skipped(reason: str, source_label: str | None) -> dict:
    return {"skipped": True, "reason": reason, "source_label": source_label}


def _connect_ro(src: Path) -> sqlite3.Connection:
    """只读打开旧库（同 s1 的姿势）。WAL 旧库只读打不开时退到 immutable。"""
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        con.execute("SELECT 1").fetchone()
        return con
    except sqlite3.Error:
        con = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
        con.execute("SELECT 1").fetchone()
        return con


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _quote(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


# ------------------------------------------------------------------ 导出旧链


def export_chain_bridge(source_db: Path, out_dir: Path) -> dict:
    """只读打开旧库（mode=ro，不改一字节），把旧链导出成可校验副档。

    产物两个文件（同名时间戳）：
        `audit-chain-<ts>.jsonl`  —— 旧 `audit_log` 逐行原样，`id` 升序，
                                     `ensure_ascii=False` + `sort_keys=True`；
        `audit-bridge-<ts>.json`  —— 摘要：版本/导出时间/来源标签/行数/链头/
                                     封存清单/JSONL 文件名+sha256+字节数。

    返回 bridge dict（含文件级 `bridge_file`/`bridge_sha256`，供审计行引用）；
    **不含路径**。旧库缺失/无 `audit_log` 表/打不开 → `{'skipped': True, 'reason': ...}`。
    """
    src = Path(source_db)
    label = src.name or None

    if not src.is_file():
        logger.warning("audit_bridge_skipped reason=%s source_label=%s",
                       SKIP_SOURCE_MISSING, label)
        return _skipped(SKIP_SOURCE_MISSING, label)

    try:
        con = _connect_ro(src)
    except sqlite3.Error:
        logger.warning("audit_bridge_skipped reason=%s source_label=%s",
                       SKIP_UNREADABLE, label)
        return _skipped(SKIP_UNREADABLE, label)

    try:
        con.row_factory = sqlite3.Row
        if "audit_log" not in _tables(con):
            logger.warning("audit_bridge_skipped reason=%s source_label=%s",
                           SKIP_NO_AUDIT_LOG, label)
            return _skipped(SKIP_NO_AUDIT_LOG, label)

        # 哈希口径取自 audit_chain 单点（本模块不复制实现）
        from services import audit_chain
        fields = tuple(audit_chain.FIELDS)

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime(_TS_FORMAT)
        jsonl_path = out / f"{_JSONL_PREFIX}{stamp}.jsonl"
        bridge_path = out / f"{_BRIDGE_PREFIX}{stamp}.json"

        cols = _columns(con, "audit_log")
        try:
            rows, head, chain_ok, state = _dump_jsonl(
                con, cols, fields, jsonl_path, audit_chain)
        except BaseException:
            jsonl_path.unlink(missing_ok=True)   # 不留半截副档
            raise

        seals = _read_seals(con)
        payload = {
            "version": CHAIN_EXPORT_VERSION,
            "exported_at": _now_iso(),
            "source_label": label,
            "source_rows": rows,
            "source_head": head,
            "source_seals": seals,
            "jsonl_file": jsonl_path.name,
            "jsonl_sha256": _sha256_file(jsonl_path),
            "jsonl_bytes": jsonl_path.stat().st_size,
            # 以下为诊断字段（判读「旧链是否自洽」，不含任何载荷）
            "algorithm": audit_chain.ALGORITHM,
            "source_chain_ok": bool(chain_ok and state),
            "source_state": state,
        }
        bridge_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")

        bridge = dict(payload)
        bridge["bridge_file"] = bridge_path.name
        bridge["bridge_sha256"] = _sha256_file(bridge_path)
        bridge["bridge_bytes"] = bridge_path.stat().st_size
        logger.info("audit_bridge_exported source_label=%s rows=%s head=%s",
                    label, rows, _short(head))
        return bridge
    finally:
        con.close()


def _dump_jsonl(con: sqlite3.Connection, cols: list[str], fields: tuple[str, ...],
                dest: Path, audit_chain) -> tuple[int, str | None, bool, bool]:
    """逐行原样写 JSONL，同时按 `audit_chain.row_hash` 重算链头。

    返回 `(行数, 链头, 各行 prev_hash 是否自洽, 旧 audit_chain_state 是否与重算一致)`。
    行是原样 dump（含 `before_json/after_json` 的金额与机构名——本机产物允许），
    链头只用 `FIELDS` 子集，口径与 `audit_chain.row_hash` 完全一致。
    """
    sql = (f"SELECT {', '.join(_quote(c) for c in cols)}"
           f" FROM audit_log ORDER BY id")
    prev = audit_chain.GENESIS
    rows = 0
    max_id = 0
    chain_ok = True
    with dest.open("w", encoding="utf-8", newline="\n") as f:
        for row in con.execute(sql):
            data = dict(row)
            f.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
            values = {name: data.get(name) for name in fields}
            stored = data.get("prev_hash")
            if stored is not None and stored != prev:
                chain_ok = False          # NULL＝源库当时未续接（0018 前），不算断
            prev = audit_chain.row_hash(prev, values)
            rows += 1
            if isinstance(data.get("id"), int):
                max_id = data["id"]

    head = prev if rows else None
    state_ok = _state_matches(con, max_id, head, rows)
    return rows, head, chain_ok, state_ok


def _state_matches(con: sqlite3.Connection, max_id: int, head: str | None,
                   rows: int) -> bool:
    """旧库 `audit_chain_state` 是否与重算结果一致（缺表/缺状态行 → False）。"""
    if "audit_chain_state" not in _tables(con):
        return False
    row = con.execute(
        "SELECT last_id, last_hash, row_count FROM audit_chain_state WHERE id=1"
    ).fetchone()
    if row is None:
        return False
    last_id, last_hash, row_count = row
    if int(last_id or 0) != max_id or int(row_count or 0) != rows:
        return False
    if head is None:
        return last_hash is not None
    return str(last_hash) == head


def _read_seals(con: sqlite3.Connection) -> list[dict]:
    """旧库日封存清单（纯 hash/日期，无载荷）；缺表 → 空清单。"""
    if "audit_chain_seals" not in _tables(con):
        return []
    cols = _columns(con, "audit_chain_seals")
    if not cols:
        return []
    seals = []
    for row in con.execute(
            f"SELECT {', '.join(_quote(c) for c in cols)}"
            f" FROM audit_chain_seals ORDER BY id"):
        seals.append(dict(row))
    return seals


# ------------------------------------------------------------------ 新库留痕


def bridge_after(bridge: dict, *, script: str, assets_imported: int) -> dict:
    """审计行 after（唯一构造点）：只放得出的事实，不含金额/真名/路径。"""
    skipped = bool(bridge.get("skipped"))
    after = {
        "op": BRIDGE_ACTION,
        "assets_imported": int(assets_imported),
        "source_rows": int(bridge.get("source_rows") or 0),
        "source_head": _short(bridge.get("source_head")),
        "source_seals": len(bridge.get("source_seals") or []),
        "bridge_file": bridge.get("bridge_file"),
        "bridge_sha256": _short(bridge.get("bridge_sha256")),
        "script": script,
        "version": int(bridge.get("version") or CHAIN_EXPORT_VERSION),
    }
    if skipped:
        # 旧链未能导出（旧库缺失/无 audit_log）：仍留痕，但显式标注桥不存在
        after["bridge_skipped"] = str(bridge.get("reason") or "unknown")
    return after


def rebuild_change(bridge: dict, *, script: str, assets_imported: int) -> dict:
    """构造 `entity='schema'` / `action='rebuild_import'` 的 change dict。

    统一走 `audit_service.make_change()`：受控枚举校验 + `scrub()` 脱敏单点，
    不在本模块另造校验路径。
    """
    from services.audit_service import make_change

    return make_change(
        BRIDGE_ENTITY, None, BRIDGE_ACTION,
        before=None, after=bridge_after(
            bridge, script=script, assets_imported=assets_imported))


def record_rebuild(db, *, bridge: dict, script: str, assets_imported: int) -> None:
    """在新库同事务写一条重建留痕（调用方负责 commit/rollback）。

    `db` 可以是裸 `sqlite3.Connection`（s4 的建库路径）或 SQLAlchemy `Session`
    （应用内/测试路径）；两条通道的落行语义与链 CAS 相同
    （`audit_service.record_sqlite` / `audit_service.record`）。
    不传 reason：系统上下文写入者不落该列（0036 NULL＝未记录原因）。
    """
    from services import audit_service

    change = rebuild_change(
        bridge, script=script, assets_imported=assets_imported)
    if isinstance(db, sqlite3.Connection):
        audit_service.record_sqlite(db, change, actor="system")
    else:
        audit_service.record(db, change, actor="system")


def bridge_summary(bridge: dict) -> dict:
    """报告用摘要（非敏感、无路径）：封存清单只留条数。"""
    if bridge.get("skipped"):
        return {"skipped": True, "reason": bridge.get("reason"),
                "source_label": bridge.get("source_label")}
    summary = {k: v for k, v in bridge.items() if k != "source_seals"}
    summary["source_seals"] = len(bridge.get("source_seals") or [])
    return summary