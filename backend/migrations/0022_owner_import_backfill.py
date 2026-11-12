"""0022：导入归属回填——记录级 owner → item 级归属史（一次性、幂等）。

背景：CSV 导入历史把 owner 写成记录级 `asset_attributes`；ADR-0011 后
item 级 `asset_item_owner_changes` 为唯一真源。本迁移把「记录级 owner 存在、
item 级无任何归属史」的资产项回填为 item 级行：
- `origin='migration'`、`reason='import-backfill'`、`owner_kind='member'`；
- 取该项**最新 active 快照行**上的 owner 属性；同一行多个 owner → 记为冲突，
  **不猜**、不写入（只读清单进审计）；
- `effective_date = date('now','localtime')`（保守：回填日至少生效）；
- 逐项 `NOT EXISTS` 归属史守卫，双跑幂等；无候选时空跑成功；
- 审计 `entity='schema', action='owner_backfill'`（actor=system）记录
  候选/回填/冲突计数与冲突 item 清单；无 CLAIM、无 down。
"""
import json
from datetime import datetime


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ready(conn) -> bool:
    required = ("asset_items", "assets", "asset_attributes", "attributes",
                "asset_item_owner_changes")
    return all(_table_exists(conn, t) for t in required)


def _candidate_rows(conn) -> list[tuple[int, int, int]]:
    """[(item_id, owner_id, owner_count)]：最新 active 行 + 无 any 归属史。"""
    rows = conn.execute(
        "WITH latest AS ("
        "  SELECT a.id, a.asset_item_id,"
        "         ROW_NUMBER() OVER (PARTITION BY a.asset_item_id"
        "                            ORDER BY a.asset_date DESC, a.id DESC) rn"
        "  FROM assets a"
        "  WHERE a.superseded_by IS NULL AND a.asset_item_id IS NOT NULL"
        "), owners AS ("
        "  SELECT l.asset_item_id, aa.attribute_id,"
        "         COUNT(*) OVER (PARTITION BY l.asset_item_id) cnt,"
        "         ROW_NUMBER() OVER (PARTITION BY l.asset_item_id"
        "                            ORDER BY aa.attribute_id) rn"
        "  FROM latest l"
        "  JOIN asset_attributes aa ON aa.asset_id = l.id"
        "  JOIN attributes attr ON attr.id = aa.attribute_id"
        "                        AND attr.type = 'owner'"
        "  WHERE l.rn = 1"
        ") "
        "SELECT o.asset_item_id, o.attribute_id, o.cnt"
        " FROM owners o"
        " WHERE o.rn = 1"
        "   AND NOT EXISTS (SELECT 1 FROM asset_item_owner_changes x"
        "                   WHERE x.asset_item_id = o.asset_item_id)"
        " ORDER BY o.asset_item_id"
    ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def _write_audit(conn, counts: dict):
    if not _table_exists(conn, "audit_log"):
        return
    cols = _columns(conn, "audit_log")
    payload = {
        "occurred_at": datetime.now().isoformat(),
        "actor": "system",
        "entity": "schema",
        "action": "owner_backfill",
        "row_id": None,
        "before_json": None,
        "after_json": json.dumps(counts, ensure_ascii=False),
        "evidence_ref": None,
        "request_id": None,
        "idempotency_key": None,
        "prev_hash": None,
    }
    use = [k for k in payload if k in cols]
    conn.execute(
        f"INSERT INTO audit_log ({', '.join(use)})"
        f" VALUES ({', '.join('?' for _ in use)})",
        tuple(payload[k] for k in use),
    )


def upgrade(conn):
    if not _ready(conn):
        return
    rows = _candidate_rows(conn)
    if not rows:
        return

    conflicts = [item_id for item_id, _, count in rows if count > 1]
    writable = [(item_id, owner_id) for item_id, owner_id, count in rows
                if count == 1]
    now = datetime.now().isoformat()
    for item_id, owner_id in writable:
        conn.execute(
            "INSERT INTO asset_item_owner_changes"
            " (asset_item_id, owner_kind, owner_id, effective_date, reason,"
            "  origin, created_at)"
            " VALUES (?, 'member', ?, date('now','localtime'),"
            "         'import-backfill', 'migration', ?)",
            (item_id, owner_id, now),
        )
    _write_audit(conn, {
        "candidates": len(rows),
        "backfilled": len(writable),
        "conflicts": len(conflicts),
        "conflict_item_ids": conflicts,
        "origin": "migration",
        "reason": "import-backfill",
    })