"""0020：配置目标阈值史（`allocation_target_revisions`，append-only）。

规格：`.scratch/phase3-review/spec.md` v1.0 §4.1；ADR-0014 D4/D5。

- 与 `allocation_targets` 同名列（target_percent/alert_threshold/
  alert_threshold_red/relative_threshold），`band_service.resolve_band(revision)`
  鸭子类型零改动复用。
- **无 FK**：历史须存活于目标行删除之后；写侧由 `allocation_service` 单点追加，
  **不在 `allocation_targets` 上建自动抓取触发器**（拿不到 reason，且测试
  create_all 底座会制造「史表空」的假绿）。
- 回填：每个现有目标一行 `op='backfill'`、`origin='migration'`、
  `effective_date = DATE(updated_at)`（保守：当前值至少自上次更新起生效，
  不伪造更早历史）；`NOT EXISTS` 逐目标守卫，整套幂等。
- append-only 触发器拒 UPDATE/DELETE；无 CLAIM、无 down；双跑幂等。
"""
from datetime import datetime


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _create_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS allocation_target_revisions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  allocation_target_id INTEGER NOT NULL,"
        "  op TEXT NOT NULL CHECK (op IN ('create','update','delete','backfill')),"
        "  dimension TEXT NOT NULL,"
        "  target_id TEXT NOT NULL,"
        "  parent_id INTEGER,"
        "  target_percent NUMERIC(5,2) NOT NULL,"
        "  alert_threshold NUMERIC(5,2) NOT NULL,"
        "  alert_threshold_red NUMERIC(5,2),"
        "  relative_threshold NUMERIC(5,2),"
        "  effective_date DATE NOT NULL,"
        "  reason TEXT,"
        "  origin TEXT NOT NULL CHECK (origin IN ('manual','migration')),"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_atr_target_eff"
        " ON allocation_target_revisions"
        " (allocation_target_id, effective_date, id)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS allocation_target_revisions_no_update"
        " BEFORE UPDATE ON allocation_target_revisions"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'allocation_target_revisions is append-only');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS allocation_target_revisions_no_delete"
        " BEFORE DELETE ON allocation_target_revisions"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'allocation_target_revisions is append-only');"
        " END"
    )


def _backfill(conn):
    if "allocation_targets" not in _table_names(conn):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(allocation_targets)")}
    if "target_percent" not in cols or "alert_threshold" not in cols:
        return
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO allocation_target_revisions"
        " (allocation_target_id, op, dimension, target_id, parent_id,"
        "  target_percent, alert_threshold, alert_threshold_red,"
        "  relative_threshold, effective_date, reason, origin, created_at)"
        " SELECT t.id, 'backfill', t.dimension, t.target_id, t.parent_id,"
        "        t.target_percent, t.alert_threshold,"
        "        t.alert_threshold_red, t.relative_threshold,"
        "        DATE(t.updated_at), 'migration: DATE(updated_at)',"
        "        'migration', ?"
        " FROM allocation_targets t"
        " WHERE NOT EXISTS (SELECT 1 FROM allocation_target_revisions r"
        "                   WHERE r.allocation_target_id = t.id)",
        (now,)
    )


def upgrade(conn):
    _create_table(conn)
    _backfill(conn)