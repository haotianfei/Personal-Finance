"""0028：存量归属回填——归属史最新一行 → `asset_items.owner_id`（一次性、幂等）。

背景：0026 重建 `asset_items` 时新增 `owner_id INTEGER REFERENCES members(id)`
（ADR-0020 D3 起它是归属的**唯一真相源**），但**没有回填**。0026 之前归属只存在
变更史 `asset_item_owner_changes`（0022 把记录级 owner 回填成史行），于是迁移后
所有存量归属在列上是 NULL —— 列表/导出/筛选一律显示「未标注」，而审计史里明明有行。

本迁移把「史表最新一行」搬到列上：

- 只处理 `asset_items.owner_id IS NULL` 的项：回填不是「重置归属」，列已有值不覆盖；
- 「最新」＝ `effective_date DESC, created_at DESC, id DESC`（与
  `asset_owner_service.current_owner` 同口径，逐项取 rn=1 那一行）；
- `owner_kind='member'` 才写列；`owner_kind='family_pool'` 保持 NULL——该合成归属
  已随 ADR-0019 D6 退役，列（`members.id` 外键）无法表达「家庭共有」，NULL 在
  新读模型里就等于「未标注」，这是既有裁决而非本迁移的取舍；
- 脏值（史行 `owner_id` 指向 `members` 里不存在的 id，如 0026 之前的旧
  `attributes.id` 落位）不写：不猜、不清洗，与 `asset_owner_service`
  读路径的 `_warn_unknown_owner_id` 防御口径一致，只在日志里计数；
- **不写任何史行**：史是 append-only 审计，「把旧值搬到列上」不是归属变更事件，
  伪造一行会污染审计链。留痕只进迁移日志（`entity='schema'` 口径同 0022，但因
  audit_log 自 0018 起有哈希链，裸 INSERT 会留下 unchained_row 断点，故不写表）；
- 幂等：`owner_id IS NULL` 守卫 + 回填后即无候选，重跑零副作用；无 down、无 CLAIM。
"""
import logging

log = logging.getLogger(__name__)

MEMBER = "member"

_READY_TABLES = ("asset_items", "asset_item_owner_changes", "members")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _ready(conn) -> bool:
    if not all(_table_exists(conn, t) for t in _READY_TABLES):
        return False
    # 0026 之前的库没有 owner_id 列（理论上不会走到，守卫成 no-op 更安全）
    return "owner_id" in _columns(conn, "asset_items")


def _candidate_rows(conn) -> list[tuple[int, str, int | None]]:
    """[(item_id, owner_kind, owner_id)]：每项最新一行 + 列仍为 NULL。"""
    rows = conn.execute(
        "WITH latest AS ("
        "  SELECT c.asset_item_id, c.owner_kind, c.owner_id,"
        "         ROW_NUMBER() OVER (PARTITION BY c.asset_item_id"
        "                            ORDER BY c.effective_date DESC,"
        "                                     c.created_at DESC, c.id DESC) rn"
        "  FROM asset_item_owner_changes c"
        ") "
        "SELECT l.asset_item_id, l.owner_kind, l.owner_id"
        " FROM latest l"
        " JOIN asset_items i ON i.id = l.asset_item_id"
        " WHERE l.rn = 1 AND i.owner_id IS NULL"
        " ORDER BY l.asset_item_id"
    ).fetchall()
    return [(int(r[0]), r[1], None if r[2] is None else int(r[2])) for r in rows]


def _existing_member_ids(conn) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT id FROM members")}


def upgrade(conn):
    if not _ready(conn):
        return "skipped"
    rows = _candidate_rows(conn)
    if not rows:
        return "noop"

    members = _existing_member_ids(conn)
    written: list[int] = []
    family_pool = unknown = 0
    for item_id, owner_kind, owner_id in rows:
        if owner_kind != MEMBER or owner_id is None:
            family_pool += 1          # 存量合成归属：列无法表达，保持 NULL
            continue
        if owner_id not in members:
            unknown += 1              # 脏值：不猜、不清洗
            continue
        cur = conn.execute(
            "UPDATE asset_items SET owner_id = ?"
            " WHERE id = ? AND owner_id IS NULL", (owner_id, item_id))
        if cur.rowcount:
            written.append(item_id)

    log.info(
        "owner_column_backfill: candidates=%d backfilled=%d"
        " skipped_family_pool=%d skipped_unknown_owner=%d",
        len(rows), len(written), family_pool, unknown)
    return f"backfilled={len(written)}"