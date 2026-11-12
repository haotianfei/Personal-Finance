"""0037：构成关系边表 ＋ 截图批次四表（ADR-0029 汇总项与构成关系，L2）。

规格：`.scratch/multi-page-aggregate/spec.md`（D1/D2/D3/D8/D9/D10）；
票 02（构成数据模型）、票 04（跨图核对与批次记录）。

改什么（**只建表，零 DML**）：

1. `item_compositions` —— 父子构成边（持久关系，ADR-0029 D3）：
   - 一子一父：`UNIQUE(child_item_id)`（撞约束 → 409 `COMPOSITION_PARENT_CONFLICT`）；
   - 禁自环：`CHECK(parent_item_id <> child_item_id)`；
   - `ix_item_compositions_parent`：父侧 EXISTS 查询（计量谓词）走这个索引；
   - **不存权重列**（D3：全额归属，无比例分摊）。
2. `screenshot_batches` ＋ `screenshot_batch_members` ＋ `screenshot_reconciliations`
   ＋ `screenshot_reconciliation_children` —— 批次与「这一次」的差额痕迹（票 04 D1）。
   持久构成（`item_compositions`）与本次核对（`screenshot_reconciliations`）两处都记：
   前者管「父子是谁」，后者管「这次差多少、谁接受了」。
   - `screenshot_batches.expected_members`（可空）：建批次时声明的**预计成员数**，
     finalize 据此判「成员齐没齐」（409 `BATCH_NOT_COMPLETE`）；NULL＝未声明。
   - `screenshot_batch_members.status` 值域 `('pending','applied','failed')`：本期只写
     `applied`（行只在写入成功时落），另两个值给「批次重试编排」预留（spec §4 不做，
     但值域先留全，免得将来越过 CHECK 又要重建表）。
  - 核对两表的 `parent_evidence_id` / `parent_row_index` / `child_item_id` /
    `child_evidence_id` / `child_row_index` **一律可空**（前端实测契约补丁）：
    父项可以是用户**手选**的既有资产项——那个项在本批截图里没有行，`(evidence_id,
    row_index)` 无处可取，`parent_item_id` 是唯一锚；其余列只用于追溯展示。
    `amount` 与 `parent_item_id` 仍非空（核对的最小事实）。

**零数据回填（票 02 D4）**：本迁移不建任何构成边、不动任何既有行的金额。存量汇总型
资产项（如「示例银行-贵金属」）与子项的关系由用户第一次「归到…」时建立——自动猜错
会静默改动净值，而净值是这个系统唯一的硬数字。

**created_at 不设 DB 默认值**：全仓建表路径是 `create_all`（模型 `default=datetime.now`
是应用层默认），迁移里的 DDL 必须与它同形。两套路径若形状不同，同一份代码在「新库
（create_all 建表）」与「老库（本迁移建表）」上会得到不同 schema。

幂等三态（不依赖 create_all 是否先跑过——启动链是 create_all → run_migrations）：
- 缺 `asset_items`（桩库/更老的形状）→ `skipped`（无处挂 FK，不猜）；
- 五表齐且逐表校验通过 → `noop`（二跑即此态）；
- 其余 → 只建缺的那些表（`IF NOT EXISTS`）＋索引，并就地升级旧形状（后加列
  `ALTER TABLE ADD COLUMN`、CHECK 值域扩宽走「重建 + 搬行」），逐表校验后返回
  `created=<表名>`（重建过的表带 `(rebuilt)` 后缀）。

校验（任一不符即 raise，由 runner 回滚该脚本）：逐表列形状、CHECK 约束名、UNIQUE 约束、
外键指向、索引齐、`integrity_check=ok`。
"""
import logging

log = logging.getLogger(__name__)

# 父表：构成边与本迁移的批次四表都挂在它上面
PARENT_TABLE = "asset_items"

# 建表顺序＝外键依赖顺序；校验按同一顺序（失败时报错定位稳定）
#
# 每条 spec 可选键（都是为了「0037 上生产前改过形状」的库能就地升级）：
# - `add_columns`：后加列 `[(列名, 列类型)]`——表已存在但缺列时 `ALTER TABLE ADD COLUMN`；
# - `rebuild_if`：`(CHECK 约束名, 值域标志文本)`——表已存在但该 CHECK 不含标志文本时
#   就地重建（SQLite 不能 ALTER CHECK），行数据按 `copy_columns` 原样搬运。
_TABLES: tuple[dict, ...] = (
    {
        "name": "item_compositions",
        "ddl": '''
CREATE TABLE IF NOT EXISTS "item_compositions" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  parent_item_id INTEGER NOT NULL REFERENCES asset_items (id),
  child_item_id INTEGER NOT NULL REFERENCES asset_items (id),
  note VARCHAR(200),
  created_at DATETIME NOT NULL,
  CONSTRAINT uq_item_compositions_child UNIQUE (child_item_id),
  CONSTRAINT ck_item_compositions_not_self
    CHECK (parent_item_id <> child_item_id)
)
''',
        "columns": ["id", "parent_item_id", "child_item_id", "note", "created_at"],
        "indexes": [
            ("ix_item_compositions_parent", "item_compositions", "parent_item_id"),
        ],
        "checks": ["ck_item_compositions_not_self"],
        "uniques": ["uq_item_compositions_child"],
        "fks": [("parent_item_id", "asset_items"),
                ("child_item_id", "asset_items")],
    },
    {
        "name": "screenshot_batches",
        "ddl": '''
CREATE TABLE IF NOT EXISTS "screenshot_batches" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  created_at DATETIME NOT NULL,
  data_date DATE,
  source_group_id INTEGER REFERENCES source_groups (id),
  status VARCHAR(12) NOT NULL DEFAULT 'open',
  finalized_at DATETIME,
  note VARCHAR(200),
  expected_members INTEGER,
  CONSTRAINT ck_screenshot_batches_status
    CHECK (status IN ('open','partial','finalized','abandoned'))
)
''',
        "columns": ["id", "created_at", "data_date", "source_group_id",
                    "status", "finalized_at", "note", "expected_members"],
        # 后加列：本迁移若已在旧形状的库上跑过（0037 上生产前），就地 ALTER 补齐，
        # 不靠「重建表」搬运批次数据（批次是用户数据，能少动就少动）
        "add_columns": [("expected_members", "INTEGER")],
        "indexes": [
            ("ix_screenshot_batches_status", "screenshot_batches", "status"),
        ],
        "checks": ["ck_screenshot_batches_status"],
        "uniques": [],
        "fks": [("source_group_id", "source_groups")],
    },
    {
        "name": "screenshot_batch_members",
        "ddl": '''
CREATE TABLE IF NOT EXISTS "screenshot_batch_members" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES screenshot_batches (id),
  evidence_id INTEGER NOT NULL REFERENCES screenshot_evidence (id),
  applied_at DATETIME NOT NULL,
  status VARCHAR(12) NOT NULL DEFAULT 'applied',
  CONSTRAINT uq_screenshot_batch_members_batch_evidence
    UNIQUE (batch_id, evidence_id),
  CONSTRAINT ck_screenshot_batch_members_status
    CHECK (status IN ('pending','applied','failed'))
)
''',
        "columns": ["id", "batch_id", "evidence_id", "applied_at", "status"],
        # CHECK 无法就地扩值域（SQLite 不支持 ALTER CHECK）：旧形状（只有
        # applied/failed）就地重建，行数据逐列原样搬运
        "rebuild_if": ("ck_screenshot_batch_members_status", "'pending'"),
        "copy_columns": ["id", "batch_id", "evidence_id", "applied_at", "status"],
        "indexes": [
            ("ix_screenshot_batch_members_evidence",
             "screenshot_batch_members", "evidence_id"),
        ],
        "checks": ["ck_screenshot_batch_members_status"],
        "uniques": ["uq_screenshot_batch_members_batch_evidence"],
        "fks": [("batch_id", "screenshot_batches"),
                ("evidence_id", "screenshot_evidence")],
    },
    {
        "name": "screenshot_reconciliations",
        "ddl": '''
CREATE TABLE IF NOT EXISTS "screenshot_reconciliations" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES screenshot_batches (id),
  parent_item_id INTEGER NOT NULL REFERENCES asset_items (id),
  parent_evidence_id INTEGER REFERENCES screenshot_evidence (id),
  parent_row_index INTEGER,
  aggregate_amount NUMERIC(15, 2) NOT NULL,
  child_amount_sum NUMERIC(15, 2) NOT NULL,
  diff NUMERIC(15, 2) NOT NULL,
  status VARCHAR(20) NOT NULL,
  accepted_reason VARCHAR(200),
  accepted_at DATETIME,
  CONSTRAINT ck_screenshot_reconciliations_status
    CHECK (status IN ('matched','accepted_mismatch')),
  CONSTRAINT ck_screenshot_reconciliations_reason
    CHECK (status <> 'accepted_mismatch' OR accepted_reason IS NOT NULL)
)
''',
        "columns": ["id", "batch_id", "parent_item_id", "parent_evidence_id",
                    "parent_row_index", "aggregate_amount", "child_amount_sum",
                    "diff", "status", "accepted_reason", "accepted_at"],
        "indexes": [
            ("ix_screenshot_reconciliations_batch",
             "screenshot_reconciliations", "batch_id"),
        ],
        "checks": ["ck_screenshot_reconciliations_status",
                   "ck_screenshot_reconciliations_reason"],
        "uniques": [],
        "fks": [("batch_id", "screenshot_batches"),
                ("parent_item_id", "asset_items"),
                ("parent_evidence_id", "screenshot_evidence")],
    },
    {
        "name": "screenshot_reconciliation_children",
        "ddl": '''
CREATE TABLE IF NOT EXISTS "screenshot_reconciliation_children" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  reconciliation_id INTEGER NOT NULL REFERENCES screenshot_reconciliations (id),
  child_item_id INTEGER REFERENCES asset_items (id),
  child_evidence_id INTEGER REFERENCES screenshot_evidence (id),
  child_row_index INTEGER,
  amount NUMERIC(15, 2) NOT NULL
)
''',
        "columns": ["id", "reconciliation_id", "child_item_id",
                    "child_evidence_id", "child_row_index", "amount"],
        "indexes": [
            ("ix_screenshot_reconciliation_children_rec",
             "screenshot_reconciliation_children", "reconciliation_id"),
        ],
        "checks": [],
        "uniques": [],
        "fks": [("reconciliation_id", "screenshot_reconciliations"),
                ("child_item_id", "asset_items"),
                ("child_evidence_id", "screenshot_evidence")],
    },
)


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


def _index_names(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA index_list("{table}")')}


def _foreign_keys(conn, table: str) -> set[tuple[str, str]]:
    """(本表列, 目标表) 集合；FK 在 SQLite 是「声明」，只看形状不看是否启用。"""
    return {(r[3], r[2]) for r in conn.execute(f'PRAGMA foreign_key_list("{table}")')}


def _ensure_columns(conn, spec: dict) -> list[str]:
    """补齐后加列（表已存在、只缺列时）。返回实际补上的列名（供 created= 回执）。"""
    added: list[str] = []
    have = _columns(conn, spec["name"])
    for column, column_type in spec.get("add_columns") or ():
        if column in have:
            continue
        # 后加列一律可空、无默认值：已有行按 NULL 读（NULL 在本域的含义是「未声明」，
        # 不是「0 个成员」——不能靠 DEFAULT 伪造一个业务事实）
        conn.execute(
            f'ALTER TABLE "{spec["name"]}" ADD COLUMN "{column}" {column_type}')
        added.append(f"{spec['name']}.{column}")
    return added


def _ensure_check_values(conn, spec: dict) -> bool:
    """CHECK 值域扩宽（`rebuild_if`）：旧形状就地重建，行数据逐列原样搬运。

    SQLite 不能 `ALTER TABLE ... DROP/ADD CONSTRAINT`，扩值域只能「建新表 → 搬行 →
    删旧表 → 改名」。**只搬声明的列**（`copy_columns`），不新增/删除任何行、不改任何
    值；重建后由 `_verify_table` 逐项校验（列形状 / CHECK / UNIQUE / FK / 索引）。
    返回 True 表示发生了重建。
    """
    trigger = spec.get("rebuild_if")
    if not trigger:
        return False
    check_name, marker = trigger
    table = spec["name"]
    ddl = _ddl(conn, table)
    if check_name in ddl and marker in ddl:
        return False                       # 已是新值域（二跑即此态）

    old = f"{table}__old"
    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
    conn.execute(spec["ddl"])
    collist = ", ".join(f'"{c}"' for c in spec["copy_columns"])
    conn.execute(f'INSERT INTO "{table}" ({collist})'
                 f' SELECT {collist} FROM "{old}"')
    conn.execute(f'DROP TABLE "{old}"')    # 旧表的索引随表消失，由 upgrade 重补
    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"0037 中止：{table} 重建后行数不符（重建前 {before} → 重建后 {after}）")
    return True


def _verify_table(conn, spec: dict) -> None:
    table = spec["name"]
    if not _table_exists(conn, table):
        raise RuntimeError(f"0037 中止：{table} 建表后不存在")

    cols = _columns(conn, table)
    if cols != spec["columns"]:
        raise RuntimeError(
            f"0037 中止：{table} 列形状不符（期望 {spec['columns']} / 实际 {cols}）")

    ddl = _ddl(conn, table)
    for check in spec["checks"]:
        if check not in ddl:
            raise RuntimeError(f"0037 中止：{table} 缺 CHECK 约束 {check}")
    for unique in spec["uniques"]:
        if unique not in ddl:
            raise RuntimeError(f"0037 中止：{table} 缺 UNIQUE 约束 {unique}")

    got_fks = _foreign_keys(conn, table)
    for column, target in spec["fks"]:
        if (column, target) not in got_fks:
            raise RuntimeError(
                f"0037 中止：{table}.{column} 缺指向 {target} 的外键"
                f"（实际 {sorted(got_fks)}）")

    indexes = _index_names(conn, table)
    for name, _table, _col in spec["indexes"]:
        if name not in indexes:
            raise RuntimeError(f"0037 中止：{table} 缺索引 {name}")


def _verify(conn) -> None:
    for spec in _TABLES:
        _verify_table(conn, spec)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0037 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 父表守卫：没有 asset_items 就无处挂 FK，不猜形状（桩库/老库）
    if not _table_exists(conn, PARENT_TABLE):
        log.warning("0037 跳过：缺少 %s 表（无处挂构成/批次外键）", PARENT_TABLE)
        return "skipped"

    # ② 只建缺的表＋索引（已存在的表由 create_all 建过，交给 ③ 校验形状）
    created: list[str] = []
    for spec in _TABLES:
        existed = _table_exists(conn, spec["name"])
        conn.execute(spec["ddl"])
        if existed:
            # 上生产前改过形状的库：就地补齐后加列 / 扩宽 CHECK 值域（见 spec 键注释）
            created.extend(_ensure_columns(conn, spec))
            if _ensure_check_values(conn, spec):
                created.append(f"{spec['name']}(rebuilt)")
                existed = False        # 索引随重建消失，下面按「未存在」重补
        have_indexes = _index_names(conn, spec["name"])
        for name, table, col in spec["indexes"]:
            if name not in have_indexes:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({col})')
                if existed:      # 表本就在、只是索引缺了（人工误删/半截迁移）
                    created.append(name)
        if not existed:
            created.append(spec["name"])

    # ③ 逐表校验（列形状 / CHECK / UNIQUE / FK / 索引 / integrity）
    _verify(conn)

    if not created:
        log.info("0037 noop：构成与批次五表已齐")
        return "noop"
    log.info("0037 created=%s", ",".join(created))
    return f"created={','.join(created)}"