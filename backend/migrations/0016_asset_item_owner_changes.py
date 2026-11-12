"""0016：资产项级归属变更史（`asset_item_owner_changes`）＋贷款级 owner 回填。

规格：`.scratch/phase3-goal/spec.md` v1.0 §2.3/§3.2；决策 `docs/adr/0011`、`docs/adr/0010` §5。

- `asset_item_owner_changes`：归属唯一真源（append-only）。当前值＝最新一行
  （`effective_date DESC, created_at DESC, id DESC`）；无行＝未标注；不设当前列（单真源）。
  家庭池＝`owner_kind='family_pool'` + `owner_id=NULL`；`member` 必带 owner_id。
  `origin∈manual|migration`；同日可多次变更（更正＝追加），仅回填行受
  `uq_item_owner_migration`（部分唯一：`UNIQUE(item, effective_date) WHERE
  origin='migration'`）约束——迁移每项每日至多一行，手写变更不受限。
- 双向 append-only 触发器（BEFORE UPDATE/DELETE → RAISE(ABORT)），同 0009/0012
  模式；更正＝追加新行，无 update/delete 端口。
- 回填：每个资产项的**最新 active 快照行**（superseded_by IS NULL，
  asset_date DESC, id DESC）上的 owner 属性；一行多 owner 时按
  asset_attributes.created_at DESC, attribute_id DESC 取最后挂接（确定性
  tie-break）。**不回退到更早记录**（最新行无 owner＝未标注，宁可人工重挂）。
  effective_date＝迁移日本地时区，origin='migration'；按 item NOT EXISTS 幂等。
- 迁移前保留名检查：已存在 owner 名「家庭池」→ fail-fast（先改名再升级）。
- 回填审计：仅本次实际插入 changes>0 时写一条 audit_log（entity=schema,
  action=owner_backfill, actor=system），after 含四项计数；audit_log 不存在则跳过。
- 全守卫幂等（IF NOT EXISTS）；无 CLAIM 标记＝首跑也实跑；无 down，回滚＝revert
  （新表/旧表均不被本脚本 UPDATE/DELETE）。
"""
import json
from datetime import datetime

RESERVED_FAMILY_POOL_NAME = "家庭池"

_BACKFILL_REASON = "migration: latest active record owner"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _assert_no_reserved_owner_name(conn):
    if not _table_exists(conn, "attributes"):
        return
    hit = conn.execute(
        "SELECT id, name FROM attributes WHERE type='owner' AND name=?",
        (RESERVED_FAMILY_POOL_NAME,),
    ).fetchone()
    if hit:
        raise RuntimeError(
            "0016 中止：已存在 owner 字典项「家庭池」（id=%s）。"
            "合成家庭池与该成员同名会破坏语义，请先用旧 UI 改名后重试。" % hit[0]
        )


def _assets_ready(conn) -> bool:
    """回填前置：assets 已由 0008 重建（含 asset_item_id/superseded_by）且属性表就位。"""
    if not (_table_exists(conn, "assets") and _table_exists(conn, "asset_attributes")
            and _table_exists(conn, "attributes") and _table_exists(conn, "asset_items")):
        return False
    cols = _columns(conn, "assets")
    return "asset_item_id" in cols and "superseded_by" in cols


def _counts(conn) -> dict:
    """迁移前只读清单计数（冲突/丢失候选），供审计与报告使用。"""
    if not _assets_ready(conn):
        return {"items_with_owner": 0, "conflicts": 0, "owner_lost_candidates": 0}

    items_with_owner = conn.execute(
        "SELECT COUNT(DISTINCT a.asset_item_id) FROM assets a"
        " JOIN asset_attributes aa ON aa.asset_id = a.id"
        " JOIN attributes attr ON attr.id = aa.attribute_id AND attr.type='owner'"
        " WHERE a.superseded_by IS NULL AND a.asset_item_id IS NOT NULL"
    ).fetchone()[0] or 0

    conflicts = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT a.asset_item_id FROM assets a"
        "  JOIN asset_attributes aa ON aa.asset_id = a.id"
        "  JOIN attributes attr ON attr.id = aa.attribute_id AND attr.type='owner'"
        "  WHERE a.superseded_by IS NULL AND a.asset_item_id IS NOT NULL"
        "  GROUP BY a.asset_item_id HAVING COUNT(DISTINCT aa.attribute_id) > 1"
        ")"
    ).fetchone()[0] or 0

    owner_lost_candidates = conn.execute(
        "SELECT COUNT(*) FROM asset_items i"
        " JOIN assets la ON la.asset_item_id = i.id AND la.superseded_by IS NULL"
        " WHERE la.id = (SELECT a2.id FROM assets a2"
        "                WHERE a2.asset_item_id = i.id AND a2.superseded_by IS NULL"
        "                ORDER BY a2.asset_date DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM asset_attributes aa"
        "                   JOIN attributes attr ON attr.id = aa.attribute_id"
        "                                        AND attr.type='owner'"
        "                   WHERE aa.asset_id = la.id)"
        "   AND EXISTS (SELECT 1 FROM assets a3"
        "               JOIN asset_attributes aa3 ON aa3.asset_id = a3.id"
        "               JOIN attributes attr3 ON attr3.id = aa3.attribute_id"
        "                                      AND attr3.type='owner'"
        "               WHERE a3.asset_item_id = i.id"
        "                 AND a3.superseded_by IS NULL AND a3.id <> la.id)"
    ).fetchone()[0] or 0

    return {"items_with_owner": items_with_owner, "conflicts": conflicts,
            "owner_lost_candidates": owner_lost_candidates}


def _read_only_lists(conn) -> dict:
    """迁移前只读清单（人工复核用）：多 owner 冲突项、owner_lost 候选项。"""
    if not _assets_ready(conn):
        return {"conflict_item_ids": [], "owner_lost_item_ids": []}
    conflicts = [r[0] for r in conn.execute(
        "SELECT a.asset_item_id FROM assets a"
        " JOIN asset_attributes aa ON aa.asset_id = a.id"
        " JOIN attributes attr ON attr.id = aa.attribute_id AND attr.type='owner'"
        " WHERE a.superseded_by IS NULL AND a.asset_item_id IS NOT NULL"
        " GROUP BY a.asset_item_id HAVING COUNT(DISTINCT aa.attribute_id) > 1"
        " ORDER BY a.asset_item_id"
    )]
    lost = [r[0] for r in conn.execute(
        "SELECT i.id FROM asset_items i"
        " JOIN assets la ON la.asset_item_id = i.id AND la.superseded_by IS NULL"
        " WHERE la.id = (SELECT a2.id FROM assets a2"
        "                WHERE a2.asset_item_id = i.id AND a2.superseded_by IS NULL"
        "                ORDER BY a2.asset_date DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM asset_attributes aa"
        "                   JOIN attributes attr ON attr.id = aa.attribute_id"
        "                                        AND attr.type='owner'"
        "                   WHERE aa.asset_id = la.id)"
        "   AND EXISTS (SELECT 1 FROM assets a3"
        "               JOIN asset_attributes aa3 ON aa3.asset_id = a3.id"
        "               JOIN attributes attr3 ON attr3.id = aa3.attribute_id"
        "                                      AND attr3.type='owner'"
        "               WHERE a3.asset_item_id = i.id"
        "                 AND a3.superseded_by IS NULL AND a3.id <> la.id)"
        " ORDER BY i.id"
    )]
    return {"conflict_item_ids": conflicts, "owner_lost_item_ids": lost}


def _owner_pick_order(conn) -> str:
    """同行多 owner 的确定性 tie-break：created_at DESC, attribute_id DESC。

    v4 形态库的 `asset_attributes` 可能没有 created_at 列（0008 未重建该表），
    缺列时退化为 attribute_id DESC（仍是确定性选择）。
    """
    if _table_exists(conn, "asset_attributes") and \
            "created_at" in _columns(conn, "asset_attributes"):
        return "aa.created_at DESC, aa.attribute_id DESC"
    return "aa.attribute_id DESC"


def _backfill(conn) -> dict:
    """最新 active 快照行 → item 级归属（单值、确定性 tie-break）。"""
    if not _assets_ready(conn):
        return _counts(conn)

    counts = _counts(conn)
    pick_order = _owner_pick_order(conn)
    conn.execute(
        "WITH latest_asset AS ("
        "  SELECT a.id, a.asset_item_id,"
        "         ROW_NUMBER() OVER (PARTITION BY a.asset_item_id"
        "                            ORDER BY a.asset_date DESC, a.id DESC) rn"
        "  FROM assets a"
        "  WHERE a.superseded_by IS NULL AND a.asset_item_id IS NOT NULL"
        "), picked AS ("
        "  SELECT la.asset_item_id, aa.attribute_id,"
        "         ROW_NUMBER() OVER (PARTITION BY la.asset_item_id"
        "                            ORDER BY " + pick_order + ") rn"
        "  FROM latest_asset la"
        "  JOIN asset_attributes aa ON aa.asset_id = la.id"
        "  JOIN attributes attr ON attr.id = aa.attribute_id AND attr.type = 'owner'"
        "  WHERE la.rn = 1"
        ") "
        "INSERT INTO asset_item_owner_changes"
        "  (asset_item_id, owner_kind, owner_id, effective_date, reason, origin, created_at)"
        " SELECT p.asset_item_id, 'member', p.attribute_id, date('now','localtime'),"
        "        ?, 'migration', strftime('%Y-%m-%dT%H:%M:%f','now','localtime')"
        " FROM picked p"
        " WHERE p.rn = 1"
        "   AND NOT EXISTS (SELECT 1 FROM asset_item_owner_changes x"
        "                   WHERE x.asset_item_id = p.asset_item_id)",
        (_BACKFILL_REASON,),
    )
    return counts


def _write_backfill_audit(conn, inserted: int, counts: dict):
    if inserted <= 0 or not _table_exists(conn, "audit_log"):
        return
    cols = _columns(conn, "audit_log")
    payload = {
        "occurred_at": datetime.now().isoformat(),
        "actor": "system",
        "entity": "schema",
        "action": "owner_backfill",
        "row_id": None,
        "before_json": None,
        "after_json": json.dumps({
            "items_with_owner": counts["items_with_owner"],
            "changes": inserted,
            "conflicts": counts["conflicts"],
            "owner_lost_candidates": counts["owner_lost_candidates"],
            **_read_only_lists(conn),
        }, ensure_ascii=False),
        "evidence_ref": None,
        "request_id": None,
        "idempotency_key": None,
        "prev_hash": None,
    }
    use = [k for k in payload if k in cols]
    conn.execute(
        f"INSERT INTO audit_log ({', '.join(use)}) VALUES"
        f" ({', '.join('?' for _ in use)})",
        tuple(payload[k] for k in use),
    )


def upgrade(conn):
    _assert_no_reserved_owner_name(conn)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS asset_item_owner_changes ("
        "  id INTEGER PRIMARY KEY,"
        "  asset_item_id INTEGER NOT NULL REFERENCES asset_items (id),"
        "  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('member','family_pool')),"
        "  owner_id INTEGER REFERENCES attributes (id),"
        "  effective_date DATE NOT NULL,"
        "  reason TEXT,"
        "  origin TEXT NOT NULL DEFAULT 'manual'"
        "    CHECK (origin IN ('manual','migration')),"
        "  created_at TEXT NOT NULL,"
        "  CHECK ((owner_kind='member' AND owner_id IS NOT NULL)"
        "      OR (owner_kind='family_pool' AND owner_id IS NULL))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_owner_item_eff"
        " ON asset_item_owner_changes (asset_item_id, effective_date, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_owner_owner"
        " ON asset_item_owner_changes (owner_id)"
    )
    # 回填幂等兜底：仅 migration 行受 (item, effective_date) 唯一约束；
    # 手写变更同日可追加（更正语义）。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_item_owner_migration"
        " ON asset_item_owner_changes (asset_item_id, effective_date)"
        " WHERE origin = 'migration'"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS asset_item_owner_changes_no_update"
        " BEFORE UPDATE ON asset_item_owner_changes"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'asset_item_owner_changes is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS asset_item_owner_changes_no_delete"
        " BEFORE DELETE ON asset_item_owner_changes"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'asset_item_owner_changes is append-only: DELETE forbidden');"
        " END"
    )

    before = conn.execute(
        "SELECT COUNT(*) FROM asset_item_owner_changes").fetchone()[0] or 0
    result = _backfill(conn)
    after = conn.execute(
        "SELECT COUNT(*) FROM asset_item_owner_changes").fetchone()[0] or 0
    inserted = after - before
    _write_backfill_audit(conn, inserted, {
        "items_with_owner": result["items_with_owner"],
        "conflicts": result["conflicts"],
        "owner_lost_candidates": result["owner_lost_candidates"],
    })