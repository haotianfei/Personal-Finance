"""0026：资产分类重构——7 张新表 + `asset_items` 六列 + 4 个不变量触发器（ADR-0019）。

背景（ADR-0019 §1）：旧模型把「资产类型 / 流动性 / 账户 / 归属 / 标签」全部塞进
`attributes(type)` 一张 EAV 表，再用 `asset_attributes` 挂到**每一条资产记录**上。
实测：绑定 15,892 行 / 4,282 条记录，上移到项级只需 609 行（压缩 96%）；同维多值 75 处
（`fund_type` 40、`liquidity_rating` 35 自相矛盾）；覆盖率只有 62% 的 (项,属性) 组合
覆盖该项全部记录。根因不是数据脏，是**基数只写在文档里**。

本迁移把基数交给 schema：
  - 单值维度 → `asset_items` 的列（`asset_class_id` / `liquidity_days` /
    `institution_id` / `owner_id` / `channel_id` / `risk_level`）；
  - 多值维度 → `asset_item_tags`；
  - 三张独立字典表（`institutions` / `channels` / `members`）**不合并**成伪多态表；
  - 变更留痕 → `asset_item_attr_history`（append-only 触发器）。

四个触发器守的是「应用层守不住」的三件事：
  1. `asset_items_leaf_class_*`：被引用的类别必须是叶子（有子节点者不得被挂）；
  2. `assets_side_matches_class_*`：`assets.side` 必须与类别的 `side_scope` 同侧；
  3. `asset_item_attr_history_no_update` / `_no_delete`：分类变更史 append-only。

draft 草稿态（ADR-0019 D9）：`status` 允许 `'draft'`，只有它豁免四维必填，
截图采纳 / CSV 导入先建项后补分类时不撞 NOT NULL；转 `active` 即强制补齐。
`archived` 同样豁免——存量归档项在迁移中拿不到分类，且归档是终态、不再参与录入校验。

**不改账本**：不触碰 `assets` 任何行、不改任何金额、不删任何列。
`attributes` / `asset_attributes` 的物理删除留给 0027（同一发布窗口内）：本迁移落地时
服务层仍引用旧表，一次删完会让主干不可启动。空库重导入路径从不写这两张表。

幂等：表已存在即跳过；`asset_items` 已含新列且 CHECK 已就位即跳过重建。
无 CLAIM、无 down；`schema_version` 由 runner 在同一事务内登记。
"""

# --------------------------------------------------------------------------- #
# 新表（与 backend/models.py 的同名类逐列对齐；差异即为 drift）
# --------------------------------------------------------------------------- #
_NEW_TABLES: list[tuple[str, str]] = [
    ("asset_classes", """
CREATE TABLE IF NOT EXISTS asset_classes (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  code VARCHAR(10) NOT NULL,
  name VARCHAR(50) NOT NULL,
  parent_id INTEGER REFERENCES asset_classes(id),
  level INTEGER NOT NULL,
  side_scope VARCHAR(10) NOT NULL,
  CONSTRAINT uq_asset_classes_code UNIQUE (code),
  CONSTRAINT uq_asset_classes_parent_name UNIQUE (parent_id, name),
  CONSTRAINT ck_asset_classes_side_scope CHECK (side_scope IN ('asset','liability','any')),
  CONSTRAINT ck_asset_classes_level CHECK (level >= 1)
)"""),
    ("institutions", """
CREATE TABLE IF NOT EXISTS institutions (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  name VARCHAR(50) NOT NULL,
  kind VARCHAR(20) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT 1,
  CONSTRAINT uq_institutions_name UNIQUE (name),
  CONSTRAINT ck_institutions_kind CHECK
    (kind IN ('bank','broker','fund','insurer','gov','platform','other'))
)"""),
    ("channels", """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  name VARCHAR(50) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT 1,
  CONSTRAINT uq_channels_name UNIQUE (name)
)"""),
    ("members", """
CREATE TABLE IF NOT EXISTS members (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  name VARCHAR(50) NOT NULL,
  role VARCHAR(20) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT 1,
  CONSTRAINT uq_members_name UNIQUE (name),
  CONSTRAINT ck_members_role CHECK (role IN ('self','spouse','joint','child','nominee'))
)"""),
    ("tags", """
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  name VARCHAR(50) NOT NULL,
  kind VARCHAR(20) NOT NULL,
  CONSTRAINT uq_tags_name UNIQUE (name),
  CONSTRAINT ck_tags_kind CHECK (kind IN ('controlled','free'))
)"""),
    ("asset_item_tags", """
CREATE TABLE IF NOT EXISTS asset_item_tags (
  item_id INTEGER NOT NULL REFERENCES asset_items(id),
  tag_id INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (item_id, tag_id)
)"""),
    ("asset_item_attr_history", """
CREATE TABLE IF NOT EXISTS asset_item_attr_history (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES asset_items(id),
  dim VARCHAR(20) NOT NULL,
  old_value TEXT,
  new_value TEXT,
  changed_at DATETIME NOT NULL,
  changed_by VARCHAR(50),
  reason TEXT,
  CONSTRAINT ck_item_attr_history_dim CHECK
    (dim IN ('asset_class','liquidity','institution','channel','owner','risk','tag'))
)"""),
]

_NEW_INDEXES: list[tuple[str, str, str]] = [
    ("ix_asset_classes_parent", "asset_classes", "parent_id"),
    ("ix_asset_item_tags_tag", "asset_item_tags", "tag_id"),
    ("ix_item_attr_history_item", "asset_item_attr_history", "item_id, changed_at"),
    ("ix_asset_items_class", "asset_items", "asset_class_id"),
    ("ix_asset_items_institution", "asset_items", "institution_id"),
    ("ix_asset_items_owner", "asset_items", "owner_id"),
]

# asset_items 目标定义（重建用）：新六列 + 放宽的 status + 三条 CHECK
_ASSET_ITEMS_NEW_COLS = [
    "asset_class_id", "liquidity_days", "institution_id",
    "owner_id", "channel_id", "risk_level",
]

_ASSET_ITEMS_DDL = """
CREATE TABLE "asset_items__new" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  display_name VARCHAR(100) NOT NULL,
  channel VARCHAR(100),
  status VARCHAR(10) NOT NULL DEFAULT 'active',
  legacy_name VARCHAR(100),
  archived_effective_from DATE,
  asset_class_id INTEGER REFERENCES asset_classes(id),
  liquidity_days INTEGER,
  institution_id INTEGER REFERENCES institutions(id),
  owner_id INTEGER REFERENCES members(id),
  channel_id INTEGER REFERENCES channels(id),
  risk_level VARCHAR(2),
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_asset_items_legacy_name UNIQUE (legacy_name),
  CONSTRAINT ck_asset_items_status CHECK (status IN ('active','archived','draft')),
  CONSTRAINT ck_asset_items_dims_required CHECK (
    status IN ('draft','archived')
    OR (asset_class_id IS NOT NULL AND liquidity_days IS NOT NULL
        AND institution_id IS NOT NULL AND owner_id IS NOT NULL)),
  CONSTRAINT ck_asset_items_liquidity_days CHECK (
    liquidity_days IS NULL OR liquidity_days IN (0,1,7,30,90,180,365,9999)),
  CONSTRAINT ck_asset_items_risk_level CHECK (
    risk_level IS NULL OR risk_level IN ('R1','R2','R3','R4','R5'))
)"""

_TRIGGERS: list[tuple[str, str]] = [
    ("asset_items_leaf_class_ins",
     "CREATE TRIGGER IF NOT EXISTS asset_items_leaf_class_ins"
     " BEFORE INSERT ON asset_items FOR EACH ROW"
     " WHEN NEW.asset_class_id IS NOT NULL AND EXISTS ("
     "  SELECT 1 FROM asset_classes WHERE parent_id = NEW.asset_class_id)"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_class must be a leaf node: children exist'); END"),
    ("asset_items_leaf_class_upd",
     "CREATE TRIGGER IF NOT EXISTS asset_items_leaf_class_upd"
     " BEFORE UPDATE OF asset_class_id ON asset_items FOR EACH ROW"
     " WHEN NEW.asset_class_id IS NOT NULL AND EXISTS ("
     "  SELECT 1 FROM asset_classes WHERE parent_id = NEW.asset_class_id)"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_class must be a leaf node: children exist'); END"),
    ("assets_side_matches_class_ins",
     "CREATE TRIGGER IF NOT EXISTS assets_side_matches_class_ins"
     " BEFORE INSERT ON assets FOR EACH ROW"
     " WHEN EXISTS (SELECT 1 FROM asset_items i"
     "  JOIN asset_classes c ON c.id = i.asset_class_id"
     "  WHERE i.id = NEW.asset_item_id AND c.side_scope NOT IN ('any', NEW.side))"
     " BEGIN SELECT RAISE(ABORT,"
     " 'assets.side conflicts with asset_class.side_scope'); END"),
    ("assets_side_matches_class_upd",
     "CREATE TRIGGER IF NOT EXISTS assets_side_matches_class_upd"
     " BEFORE UPDATE OF side, asset_item_id ON assets FOR EACH ROW"
     " WHEN EXISTS (SELECT 1 FROM asset_items i"
     "  JOIN asset_classes c ON c.id = i.asset_class_id"
     "  WHERE i.id = NEW.asset_item_id AND c.side_scope NOT IN ('any', NEW.side))"
     " BEGIN SELECT RAISE(ABORT,"
     " 'assets.side conflicts with asset_class.side_scope'); END"),
    ("asset_item_attr_history_no_update",
     "CREATE TRIGGER IF NOT EXISTS asset_item_attr_history_no_update"
     " BEFORE UPDATE ON asset_item_attr_history FOR EACH ROW"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_item_attr_history is append-only: UPDATE forbidden'); END"),
    ("asset_item_attr_history_no_delete",
     "CREATE TRIGGER IF NOT EXISTS asset_item_attr_history_no_delete"
     " BEFORE DELETE ON asset_item_attr_history FOR EACH ROW"
     " BEGIN SELECT RAISE(ABORT,"
     " 'asset_item_attr_history is append-only: DELETE forbidden'); END"),
]


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _rebuild_asset_items(conn) -> str:
    """12 步重建：加六列 + 换 status CHECK + 补三条 CHECK。保留原索引/触发器。"""
    table = "asset_items"
    old_cols = _columns(conn, table)
    has_all = set(_ASSET_ITEMS_NEW_COLS) <= old_cols
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    # 0024 会无条件把旧 status CHECK 追加到表上（空库全链时 create_all 已建新表，
    # 0024 仍会再补一条 'active'|'archived' 的旧约束，与 draft 互斥）。
    # 因此「有旧约束」也算需要重建——否则新库插入 draft 会被这条陈旧 CHECK 拒绝。
    legacy_check = "CHECK (status IN ('active','archived'))"
    if has_all and "('active','archived','draft')" in sql and legacy_check not in sql:
        return "skipped"

    idx_sqls = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL ORDER BY name", (table,))]
    trg_sqls = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?"
        " ORDER BY name", (table,))]
    idx_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}

    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    # 存量行没有分类 → 一律降为 draft（归档行保留 archived，见模块 docstring）。
    # 直接在 SELECT 里转换：新表的 dims 列尚未拷贝就参与 CHECK，先插 active 会失败。
    conn.execute(_ASSET_ITEMS_DDL)
    copy_cols = [c for c in old_cols]
    collist = ", ".join(f'"{c}"' for c in copy_cols)
    select_expr = ", ".join(
        "CASE WHEN status = 'active' THEN 'draft' ELSE status END" if c == "status"
        else f'"{c}"' for c in copy_cols)
    conn.execute(
        f'INSERT INTO "asset_items__new" ({collist}) SELECT {select_expr} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "asset_items__new" RENAME TO "{table}"')
    for ddl in idx_sqls + trg_sqls:
        conn.execute(ddl)
    conn.execute('CREATE INDEX IF NOT EXISTS ix_asset_items_status ON asset_items (status)')

    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if before != after:
        raise RuntimeError(f"asset_items 行数不一致：{before} → {after}")
    now_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}
    if not idx_names <= now_idx:
        raise RuntimeError(f"asset_items 索引未复原：{idx_names - now_idx}")
    return "rebuilt"


def upgrade(conn):
    tables = _tables(conn)
    if "asset_items" not in tables:
        return "missing"          # 早期最小化 fixture：留给 0008 去建

    created: list[str] = []
    for name, ddl in _NEW_TABLES:
        # asset_item_tags / asset_item_attr_history 依赖 asset_items，已在上面守卫
        conn.execute(ddl)
        created.append(name)

    result = _rebuild_asset_items(conn)

    for name, table, cols in _NEW_INDEXES:
        if table in _tables(conn):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")

    for _, ddl in _TRIGGERS:
        conn.execute(ddl)

    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"0026 后外键不一致：{problems[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0026 后完整性检查失败：{integrity}")
    return {"tables": created, "asset_items": result}