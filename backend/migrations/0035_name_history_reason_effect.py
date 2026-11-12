"""0035：显示名史扩 reason/effect/别名 + 改名确认令牌表（ADR-0026，L2）。

规格：`.scratch/asset-name-identity/issues/03-rename-reason-and-effect.md`（D4）。

改什么：
1. `asset_item_names` **一次表重建**（SQLite 不能在列上改 CHECK / 改 NOT NULL）：
   - `origin` CHECK 从 `('migration','rename')` 扩到 `('migration','rename','alias')`；
   - `effective_from` 由 NOT NULL 改为可空（别名不设生效日）；
   - 新增 `reason`（correction|evolution|normalization）、`effect`
     （current|history_backfill）、`reason_note`（≤200，应用层校验）三列，均可空。
2. 新增 `asset_item_rename_tokens`（改名预览 → 确认的一次性令牌，D11）。

为什么可空：存量 146 行是「未记录原因」的历史，不能倒填一个假的 reason/effect；
`NULL` 的语义是「不知道」，解析层把 `effect IS NULL` 视同 `current`（不回写历史）。
新写入由应用层强制带 reason/effect，不靠 DB 默认值兜底。

幂等：
- 表不存在 → `skipped`（`create_all` 会按新模型建表，迁移不掺和）；
- 已是新形状（三列齐 + DDL 含 `'alias'`）→ 只补建令牌表，返回 `noop`；
- 否则重建，并在迁移后逐条校验（行数守恒、列形状、CHECK 文本、索引与触发器齐、
  `integrity_check=ok`），任一不符即抛错回滚。

**不动行数据**：只 `INSERT ... SELECT` 原样搬运 7 列，不新增/删除任何名字行。
无 CLAIM、无 down；`schema_version` 由 runner 在同一事务内登记。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "asset_item_names"
OLD_COLUMNS = ["id", "item_id", "name", "effective_from", "origin", "created_at"]
NEW_ONLY_COLUMNS = ["reason", "effect", "reason_note"]
TOKEN_TABLE = "asset_item_rename_tokens"

_NEW_DDL = '''
CREATE TABLE "asset_item_names__new" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES asset_items(id),
  name VARCHAR(100) NOT NULL,
  effective_from DATE,
  origin VARCHAR(10) NOT NULL,
  reason VARCHAR(20),
  effect VARCHAR(20),
  reason_note VARCHAR(200),
  created_at DATETIME NOT NULL,
  CONSTRAINT ck_asset_item_names_origin
    CHECK (origin IN ('migration','rename','alias')),
  CONSTRAINT ck_asset_item_names_reason
    CHECK (reason IS NULL OR reason IN ('correction','evolution','normalization')),
  CONSTRAINT ck_asset_item_names_effect
    CHECK (effect IS NULL OR effect IN ('current','history_backfill')),
  CONSTRAINT ck_asset_item_names_alias_date
    CHECK ((origin = 'alias' AND effective_from IS NULL)
           OR (origin <> 'alias' AND effective_from IS NOT NULL))
)
'''

_INDEXES = [
    ("ix_asset_item_names_item", "asset_item_names", "item_id"),
    ("ix_asset_item_names_name", "asset_item_names", "name"),
]

_TRIGGERS = [
    ("asset_item_names_no_update",
     "CREATE TRIGGER IF NOT EXISTS asset_item_names_no_update"
     " BEFORE UPDATE ON asset_item_names FOR EACH ROW"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_item_names is append-only: UPDATE forbidden'); END"),
    ("asset_item_names_no_delete",
     "CREATE TRIGGER IF NOT EXISTS asset_item_names_no_delete"
     " BEFORE DELETE ON asset_item_names FOR EACH ROW"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_item_names is append-only: DELETE forbidden'); END"),
]

_TOKEN_DDL = f'''
CREATE TABLE IF NOT EXISTS "{TOKEN_TABLE}" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  token VARCHAR(64) NOT NULL,
  item_id INTEGER NOT NULL REFERENCES asset_items(id),
  payload_json TEXT NOT NULL,
  impact_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  used_at DATETIME,
  created_at DATETIME NOT NULL,
  CONSTRAINT uq_asset_item_rename_tokens_token UNIQUE (token)
)
'''


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _ddl(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else ""


def _is_new_shape(conn) -> bool:
    cols = set(_columns(conn, TABLE))
    return set(NEW_ONLY_COLUMNS) <= cols and "'alias'" in _ddl(conn, TABLE)


def _ensure_token_table(conn) -> None:
    conn.execute(_TOKEN_DDL)
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "ix_asset_item_rename_tokens_item"'
        f' ON "{TOKEN_TABLE}" (item_id)')


def _rebuild(conn) -> None:
    before = conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]
    conn.execute(_NEW_DDL)
    collist = ", ".join(OLD_COLUMNS)
    conn.execute(
        f'INSERT INTO "asset_item_names__new" ({collist})'
        f' SELECT {collist} FROM "{TABLE}"')
    conn.execute(f'DROP TABLE "{TABLE}"')          # 触发器随表消失
    conn.execute(f'ALTER TABLE "asset_item_names__new" RENAME TO "{TABLE}"')
    for name, table, cols in _INDEXES:
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})')
    for name, ddl in _TRIGGERS:
        conn.execute(ddl)

    after = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{TABLE}'"
    ).fetchone()[0]
    rows_after = conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]
    if after != 1 or rows_after != before:
        raise RuntimeError(
            f"0035 中止：{TABLE} 重建后行数/表存在不符"
            f"（重建前 {before} 行 → 重建后 {rows_after} 行）")

    cols = _columns(conn, TABLE)
    expected = ["id", "item_id", "name", "effective_from", "origin",
                "reason", "effect", "reason_note", "created_at"]
    if cols != expected:
        raise RuntimeError(f"0035 中止：列形状不符（期望 {expected} / 实际 {cols}）")
    if "'alias'" not in _ddl(conn, TABLE):
        raise RuntimeError("0035 中止：origin CHECK 未扩到 alias")

    for obj_type, name in (("index", "ix_asset_item_names_item"),
                           ("index", "ix_asset_item_names_name"),
                           ("trigger", "asset_item_names_no_update"),
                           ("trigger", "asset_item_names_no_delete")):
        got = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            (obj_type, name)).fetchone()
        if got is None:
            raise RuntimeError(f"0035 中止：{obj_type} {name} 缺失")


def upgrade(conn) -> str:
    if not _table_exists(conn, TABLE):
        log.warning("0035 跳过：缺少 %s 表（新库由 create_all 建新形状）", TABLE)
        _ensure_token_table(conn)
        return "skipped"

    if _is_new_shape(conn):
        _ensure_token_table(conn)
        log.info("0035 noop：%s 已是新形状", TABLE)
        return "noop"

    _rebuild(conn)
    _ensure_token_table(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0035 中止：integrity_check={integrity}")
    log.info("0035 rebuild: %s 扩 reason/effect/alias + 令牌表", TABLE)
    return "rebuilt"