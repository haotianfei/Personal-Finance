"""0027：拆除 EAV 分类两表（`attributes` / `asset_attributes`）——ADR-0019 D11 收尾。

背景（同 0026 的承接）：0026 把「资产类型 / 流动性 / 账户 / 归属 / 标签」从
`attributes(type)` ＋ `asset_attributes`（记录级打点 15,892 行）搬到
`asset_items` 六列 ＋ 五张独立字典 ＋ `asset_item_tags`。当时**故意不删表**：
服务层仍有 11 个文件引用旧 ORM 类，一次删完会让主干不可启动。本迁移与同批提交
的 `backend/models.py`（删 `Attribute` / `AssetAttribute` 两个类、五处外键改指）
共同完成物理拆除。

为什么删（而不是留表兼容）：
  1. 两表是「同维多值」的根因载体——留在库里，任何绕过服务层的裸 SQL 都能再造
     出 `fund_type` 双值、`liquidity_rating` 自相矛盾；
  2. 幂等/一致性由 schema 表达（0026 的四个触发器），旧表存在即有两套真相源；
  3. 新结构已覆盖全部读路径（ADR-0019 D2/D6），旧表数据在 0026 落地时已完成
     「记录级 → 项级」的上移与压缩（15,892 → 609 行），此后旧表不再被任何写路径
     写入（import/screenshot/batch 全部只写项级），保留它只剩误用风险。

引用迁到哪（**先迁引用，再删表**；SQLite 不能改 FK → 一律 12 步重建）：

  | 旧引用 | 新指向 | 存量映射规则 |
  |---|---|---|
  | `source_group_accounts.attribute_id` | `institutions.id` | 账户名 == 机构名；查不到同名机构即中止 |
  | `goals.scope_owner_id` | `members.id` | 属性名 == 成员名；查不到即中止 |
  | `expenses.account_id` | `institutions.id` | 同上（名字映射） |
  | `fixed_expense_templates.account_id` | `institutions.id` | 同上（名字映射） |
  | `asset_item_owner_changes.owner_id` | `members.id` | 属性名 == 成员名（归属史值域随 D6 换成员） |

  映射规则一律「按名字查新字典，查不到**中止**」：既不静默置 NULL（会丢分类且
  事后无从追查），也不静默新建字典行（凭空造出用户没确认过的机构/成员）。中止时
  报错列出受影响行与名字，人工补字典后重跑即可（每步都有幂等守卫，可续跑）。

`asset_item_owner_changes` 不在原清单的四个业务入口里，但它是 0016 建表时
`owner_id INTEGER REFERENCES attributes (id)` 的**存量 FK**：不重建的话
`PRAGMA foreign_key_check` 会因父表消失而报警（0027 的落地断言直接失败）。因此
同样按「名字 → members」重建；新库的该表由 `models.py` 建出时已指 members，此步
是 no-op（`already`）。

源组账户关联表除改指向外还**收窄形状**（列 `id, source_group_id, institution_id`）：
旧表带 `value` / `created_at` 两列全库零引用（关联语义只有「挂了没挂」），且旧
复合主键换成代理主键 + `UNIQUE(source_group_id, institution_id)`，与
`models.SourceGroupAccount` 逐列对齐；旧索引 `idx_sga_*` 随重建消失，新名
`ix_sga_group` / `ix_sga_institution`（与 models 一致）。

幂等：
  - `attributes` 表不存在 → 立即返回 `"missing"`（**空库重建路径**：
    `create_all` 已按新 models 建表，本脚本不碰任何东西，只登记编号）；
  - 每个引用重建步骤先读 `PRAGMA foreign_key_list`：外键已指新字典（或该表/列
    不存在）→ `already` / `missing` / `no-column`，不重建；
  - 删表用 `DROP TABLE IF EXISTS`。
  无 CLAIM、无 down；回滚＝从备份恢复（本迁移删数据，按仓库纪律走前置备份）。
  `schema_version` 由 runner 在同一事务内登记。

落地断言（三条，任一不满足即抛错回滚）：`PRAGMA foreign_key_check` 为空、
`PRAGMA integrity_check` 为 `ok`、`sqlite_master` 里不再有这两张表。
"""
import re

# 需要把「指向 attributes 的外键」改指的既有表（表名 → 目标新字典在 _rebuild 里给）
_TARGET_TABLES = {
    "source_group_accounts", "goals", "expenses",
    "fixed_expense_templates", "asset_item_owner_changes",
}

# 本迁移物理删除的表（先删绑定表，再删字典表：自引用 parent_id 随表消失）
_DROP_TABLES = ("asset_attributes", "attributes")

_CREATE_RE = re.compile(
    r"(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(\"?)([A-Za-z_][A-Za-z0-9_]*)(\"?)(.*)$"
)
_FK_RE = re.compile(r"(?i)(REFERENCES\s+)attributes(\s*\(\s*id\s*\))")

# 源组 ↔ 机构关联表的新形状（与 models.SourceGroupAccount 逐列对齐）
_SGA_DDL = """
CREATE TABLE "source_group_accounts__new" (
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  source_group_id INTEGER NOT NULL,
  institution_id INTEGER NOT NULL,
  CONSTRAINT uq_sga_group_institution UNIQUE (source_group_id, institution_id),
  FOREIGN KEY(source_group_id) REFERENCES source_groups (id),
  FOREIGN KEY(institution_id) REFERENCES institutions (id)
)"""

_SGA_INDEXES = (
    ("ix_sga_group", "source_group_id"),
    ("ix_sga_institution", "institution_id"),
)


# --------------------------------------------------------------------------- #
# 通用工具（与 0024/0026 同型）
# --------------------------------------------------------------------------- #
def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> list[str]:
    """按定义顺序返回列名（INSERT 列清单与 SELECT 一一对应，顺序必须稳定）。"""
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_sql(conn, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else None


def _capture(conn, table: str) -> tuple[list[str], list[str], set[str], set[str]]:
    """捕获原索引/触发器 DDL 与名字集合（重建后逐字重放并核对）。"""
    idx_sqls = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL ORDER BY name", (table,))]
    trg_sqls = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?"
        " ORDER BY name", (table,))]
    idx_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}
    trg_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
    return idx_sqls, trg_sqls, idx_names, trg_names


def _as_new_table(sql: str, table: str) -> str:
    """原样复制 DDL，仅把表名换成 `<table>__new`（逐字节保留列定义/约束）。"""
    m = _CREATE_RE.match(sql)
    if not m:
        raise RuntimeError(f"无法解析 CREATE TABLE：{table}")
    return f'{m.group(1)}"{table}__new"{m.group(5)}'


def _retarget(sql: str, parent: str) -> tuple[str, int]:
    """DDL 里的 `REFERENCES attributes (id)` → `REFERENCES <parent> (id)`。

    只替换「父表名」这一段，其余字节（列定义、CHECK、ON DELETE 动作）原样保留。
    """
    new, n = _FK_RE.subn(
        lambda m: f"{m.group(1)}{parent}{m.group(2)}", sql)
    return new, n


def _fk_parent(conn, table: str, column: str) -> str | None:
    """`table.column` 当前外键指向的父表名；无该外键返回 None。"""
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        # (id, seq, table, from, to, on_update, on_delete, match)
        if row[3] == column:
            return row[2]
    return None


def _guard_mapping(conn, table: str, column: str, parent: str) -> None:
    """存量值 → 新字典 id 的映射守卫：查不到同名行即中止（不置 NULL、不新建）。

    映射口径统一为「旧属性名 == 新字典名」。`attributes` 行本身缺失（悬空 id）也
    算查不到——那正是需要人工看一眼的行，不静默吞掉。
    """
    if parent not in _tables(conn):
        raise RuntimeError(
            f"0027 中止：{table}.{column} 需要改指 {parent}，但 {parent} 表不存在"
            f"（应由 0026 先建；请确认迁移顺序未被改动）")
    where = (
        f'SELECT t."{column}", a.name FROM "{table}" t'
        f' LEFT JOIN attributes a ON a.id = t."{column}"'
        f' LEFT JOIN "{parent}" p ON p.name = a.name'
        f' WHERE t."{column}" IS NOT NULL AND p.id IS NULL')
    total = conn.execute(f'SELECT COUNT(*) FROM ({where})').fetchone()[0]
    if not total:
        return
    sample = "; ".join(
        f"{table}.{column}={r[0]}(name={r[1]!r})"
        for r in conn.execute(where + " ORDER BY 1 LIMIT 10"))
    raise RuntimeError(
        f"0027 中止：{table}.{column} 有 {total} 行无法按名字映射到 {parent}.name。"
        f"映射规则是「旧属性名 == 新字典名」，不做置 NULL、不自动建字典行。"
        f"请先在 {parent} 补齐同名行后重跑（本脚本各步幂等，可续跑）。样例：{sample}")


def _rebuild_retarget(conn, table: str, column: str, parent: str) -> str:
    """把 `table.column` 的外键从 attributes 改指 parent，值按名字映射。"""
    sql = _table_sql(conn, table)
    if sql is None:
        return "missing"
    cols = _columns(conn, table)
    if column not in cols:
        return "no-column"
    if _fk_parent(conn, table, column) != "attributes":
        return "already"                 # 已指新字典（新库/models 建的）或另有指向

    new_sql, n = _retarget(sql, parent)
    if n == 0:
        # 理论不可达：foreign_key_list 说是 attributes，DDL 里却找不到该片段
        raise RuntimeError(f"0027 中止：{table} 外键指向 attributes，但 DDL 无法重写")

    _guard_mapping(conn, table, column, parent)

    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    idx_sqls, trg_sqls, idx_names, trg_names = _capture(conn, table)

    collist = ", ".join(f'"{c}"' for c in cols)
    select_expr = ", ".join(
        f'p."id"' if c == column else f't."{c}"' for c in cols)

    conn.execute(_as_new_table(new_sql, table))
    conn.execute(
        f'INSERT INTO "{table}__new" ({collist}) SELECT {select_expr}'
        f' FROM "{table}" t'
        f' LEFT JOIN attributes a ON a.id = t."{column}"'
        f' LEFT JOIN "{parent}" p ON p.name = a.name')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{table}__new" RENAME TO "{table}"')
    for ddl in idx_sqls + trg_sqls:
        conn.execute(ddl)

    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if before != after:
        raise RuntimeError(f"0027：{table} 行数不一致：{before} → {after}")
    now_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}
    now_trg = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
    if idx_names != now_idx or trg_names != now_trg:
        raise RuntimeError(
            f"0027：{table} 索引/触发器未复原："
            f"{idx_names ^ now_idx} {trg_names ^ now_trg}")
    if _fk_parent(conn, table, column) != parent:
        raise RuntimeError(f"0027：{table}.{column} 未改指 {parent}")
    return "rebuilt"


def _rebuild_source_group_accounts(conn) -> str:
    """源组账户关联表：`attribute_id` → `institution_id`，并收窄为三列新形状。"""
    table = "source_group_accounts"
    cols = _columns(conn, table)
    if not cols:
        return "missing"
    if "institution_id" in cols and "attribute_id" not in cols:
        return "already"
    if "attribute_id" not in cols:
        return "no-column"
    if "source_groups" not in _tables(conn):
        raise RuntimeError("0027 中止：source_group_accounts 存在但 source_groups 不存在")

    _guard_mapping(conn, table, "attribute_id", "institutions")

    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    expected = conn.execute(
        'SELECT COUNT(*) FROM (SELECT DISTINCT sga.source_group_id, p."id"'
        f' FROM "{table}" sga'
        ' JOIN attributes a ON a.id = sga.attribute_id'
        ' JOIN institutions p ON p.name = a.name)').fetchone()[0]
    conn.execute(_SGA_DDL)
    # DISTINCT：不同 type 的同名属性（uq_attribute_name_type 允许）会映射到同一机构，
    # 目标表的 UNIQUE(source_group_id, institution_id) 不接受重复关联。
    conn.execute(
        'INSERT INTO "source_group_accounts__new" (source_group_id, institution_id)'
        f' SELECT DISTINCT sga.source_group_id, p."id"'
        f' FROM "{table}" sga'
        ' JOIN attributes a ON a.id = sga.attribute_id'
        ' JOIN institutions p ON p.name = a.name'
        ' ORDER BY sga.source_group_id, p."id"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute('ALTER TABLE "source_group_accounts__new" RENAME TO "source_group_accounts"')
    for name, col in _SGA_INDEXES:
        conn.execute(f'CREATE INDEX IF NOT EXISTS {name} ON source_group_accounts ({col})')

    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if after != expected:
        raise RuntimeError(
            f"0027：{table} 行数不符：期望 {expected}（去重后），落库 {after}")
    if _fk_parent(conn, table, "institution_id") != "institutions":
        raise RuntimeError("0027：source_group_accounts.institution_id 未指 institutions")
    merged = before - expected
    # 仅当不同 type 的同名属性映射到同一机构时才会合并；合并数记入返回值便于审计
    return f"rebuilt(merged={merged})" if merged else "rebuilt"


def _guard_unhandled_fk(conn) -> None:
    """前置扫描：除本迁移处理的表外，还有谁指着 attributes 就中止。

    防的是「以后新增的表也挂了 attributes，而 0027 不知道自己该迁」——那种情况
    删表后 `foreign_key_check` 会因父表消失而报警，报错点却离现场很远。
    """
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if table in _TARGET_TABLES or table in _DROP_TABLES:
            continue
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            if row[2] == "attributes":
                raise RuntimeError(
                    f"0027 中止：{table}.{row[3]} 也指向 attributes，本迁移未覆盖该表。"
                    f"请先补映射规则再升级。")


def upgrade(conn) -> str:
    # ① 空库重建路径：新 models 已不含这两张表，什么都不做（安全 no-op）
    if "attributes" not in _tables(conn):
        return "missing"

    # 表重建需 FK off（runner 连接默认 off；事务内无法改，显式断言）
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("0027 要求 PRAGMA foreign_keys=off 下执行表重建")

    _guard_unhandled_fk(conn)

    # ② 先迁引用（逐项幂等），③ 再删表
    steps = {
        "source_group_accounts": _rebuild_source_group_accounts(conn),
        "goals": _rebuild_retarget(conn, "goals", "scope_owner_id", "members"),
        "expenses": _rebuild_retarget(conn, "expenses", "account_id", "institutions"),
        "fixed_expense_templates": _rebuild_retarget(
            conn, "fixed_expense_templates", "account_id", "institutions"),
        "asset_item_owner_changes": _rebuild_retarget(
            conn, "asset_item_owner_changes", "owner_id", "members"),
    }

    dropped: list[str] = []
    for table in _DROP_TABLES:
        if table in _tables(conn):
            conn.execute(f'DROP TABLE "{table}"')     # 绑定表先删，字典表后删
            dropped.append(table)

    # ④ 落地断言
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"0027 后外键不一致：{problems[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0027 后完整性检查失败：{integrity}")
    left = _tables(conn) & set(_DROP_TABLES)
    if left:
        raise RuntimeError(f"0027 后旧表仍存在：{sorted(left)}")

    refs = ",".join(f"{k}={v}" for k, v in steps.items())
    return f"dropped={'+'.join(dropped)};{refs}"