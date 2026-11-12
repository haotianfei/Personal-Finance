"""0038：教学规则扩第三类 `aggregate`（汇总行词表，ADR-0029 D7 / 票 03 D1）。

问题：`source_group_learned_rules.rule_type` 的 CHECK 只有
`('rename','ignore','exclude')`（迁移 0031 冻结的取值域），而「哪些行是汇总行」要
按组学成规则，否则用户每批都要手勾一次。新增取值必须扩 CHECK——SQLite 不能
`ALTER TABLE ... DROP/ADD CONSTRAINT`，只能**重建表**（同一手法见 0035）。

改什么（**只改约束，零 DML**）：
- `rule_type` 的 CHECK 从 `('rename','ignore','exclude')` 扩到
  `('rename','ignore','exclude','aggregate')`；
- 其余列、索引、外键**逐项照抄 0031**：不新增列、不改可空性、不动唯一索引
  （`(source_group_id, rule_type, match, COALESCE(replace,''))`）、不动
  `ON DELETE CASCADE`。

**行数据一条不动**：`INSERT ... SELECT` 原样搬运全部列，重建后校验行数守恒。
`aggregate` 行的 `replace` 恒 NULL（它只表达「这个词算汇总行」，不做改名）。

幂等三态：
- 表不存在 → `skipped`（新库由 `create_all` 按新模型建表，迁移不掺和）；
- DDL 里已有 `'aggregate'` → `noop`（二跑即此态，也是 create_all 先行时的态）；
- 否则重建 → `rebuilt`，并校验列形状 / 两个 CHECK / 两个索引 / 行数守恒 /
  `integrity_check=ok`，任一不符即 raise（由 runner 回滚该脚本）。

回退：本迁移只扩值域，旧代码写入的三类规则照常工作；要回退需删掉所有
`rule_type='aggregate'` 行后重建回 0031 形状（未提供 down，符合本仓惯例）。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "source_group_learned_rules"
GROUP_TABLE = "source_groups"

# 列序＝0031 建表序（重建后 `PRAGMA table_info` 必须逐行相等，否则 ORM 列序漂移）
_COLUMNS = ["id", "source_group_id", "rule_type", "match", "replace", "note",
            "sort_order", "origin", "hit_count", "last_hit_at",
            "created_at", "updated_at"]

_NEW_DDL = '''
CREATE TABLE "source_group_learned_rules__new" (
\tid INTEGER NOT NULL,
\tsource_group_id INTEGER NOT NULL,
\trule_type TEXT NOT NULL,
\t"match" TEXT NOT NULL,
\t"replace" TEXT,
\tnote TEXT,
\tsort_order INTEGER NOT NULL,
\torigin TEXT NOT NULL,
\thit_count INTEGER NOT NULL,
\tlast_hit_at DATETIME,
\tcreated_at DATETIME NOT NULL,
\tupdated_at DATETIME NOT NULL,
\tPRIMARY KEY (id),
\tCONSTRAINT ck_sglr_type CHECK (rule_type IN ('rename','ignore','exclude','aggregate')),
\tCONSTRAINT ck_sglr_origin CHECK (origin IN ('manual','import','migration')),
\tFOREIGN KEY(source_group_id) REFERENCES source_groups (id) ON DELETE CASCADE
)
'''

_INDEX_DDLS = (
    ('ix_sglr_group',
     'CREATE INDEX IF NOT EXISTS ix_sglr_group'
     f' ON "{TABLE}" (source_group_id, sort_order)'),
    # 唯一性口径不变：`replace` 可空，SQLite 里 NULL 互不相等 → 用 COALESCE 归一
    ('uq_sglr_group_type_match_replace',
     'CREATE UNIQUE INDEX IF NOT EXISTS uq_sglr_group_type_match_replace'
     f' ON "{TABLE}" (source_group_id, rule_type, "match", COALESCE("replace", \'\'))'),
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


def _has_aggregate(conn) -> bool:
    """新值域判定：只认 `'aggregate'` 出现在 DDL 里（不猜别的写法）。"""
    return "'aggregate'" in _ddl(conn, TABLE)


def _rebuild(conn) -> None:
    old = f"{TABLE}__old"
    new = f"{TABLE}__new"
    before = conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]
    conn.execute(f'ALTER TABLE "{TABLE}" RENAME TO "{old}"')
    conn.execute(_NEW_DDL)
    collist = ", ".join(f'"{c}"' for c in _COLUMNS)
    conn.execute(f'INSERT INTO "{new}" ({collist})'
                 f' SELECT {collist} FROM "{old}"')
    conn.execute(f'DROP TABLE "{old}"')     # 旧索引随表消失，下面重补
    conn.execute(f'ALTER TABLE "{new}" RENAME TO "{TABLE}"')
    for _name, ddl in _INDEX_DDLS:
        conn.execute(ddl)

    after = conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"0038 中止：{TABLE} 重建后行数不符（重建前 {before} → 重建后 {after}）")

    cols = _columns(conn, TABLE)
    if cols != _COLUMNS:
        raise RuntimeError(f"0038 中止：列形状不符（期望 {_COLUMNS} / 实际 {cols}）")

    ddl = _ddl(conn, TABLE)
    for check, marker in (("ck_sglr_type", "'aggregate'"),
                          ("ck_sglr_origin", "'migration'")):
        if check not in ddl or marker not in ddl:
            raise RuntimeError(f"0038 中止：{TABLE} 的 {check} 缺失或值域不符")

    for obj_type, name in [("index", n) for n, _ in _INDEX_DDLS]:
        got = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            (obj_type, name)).fetchone()
        if got is None:
            raise RuntimeError(f"0038 中止：{obj_type} {name} 缺失")

    fks = conn.execute(f'PRAGMA foreign_key_list("{TABLE}")').fetchall()
    if [(r[2], r[3], r[6]) for r in fks] != [(GROUP_TABLE, "source_group_id", "CASCADE")]:
        raise RuntimeError(f"0038 中止：{TABLE} 外键形状不符（实际 {fks}）")


def upgrade(conn) -> str:
    if not _table_exists(conn, TABLE):
        log.warning("0038 跳过：缺少 %s 表（新库由 create_all 建新值域）", TABLE)
        return "skipped"

    if _has_aggregate(conn):
        log.info("0038 noop：%s 的 rule_type 已含 aggregate", TABLE)
        return "noop"

    _rebuild(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0038 中止：integrity_check={integrity}")
    log.info("0038 rebuilt: %s 的 rule_type 扩宽到 aggregate", TABLE)
    return "rebuilt"