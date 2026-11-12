"""0039：`asset_items` 加项级备注列 `note TEXT NULL`（ADR-0031 D0，L1）。

规格：`.scratch/asset-record-edit/spec.md` D2/D14 ＋ `.scratch/asset-record-edit/adr-0031-draft.md` D0。

改什么：**只加一列**，其余一律不动。

- `note TEXT` —— 项级备注，单条可编辑、允许多行。长度上限 500 字符（Unicode
  码点，trim 后计）由**应用层**校验，DDL 里不设 CHECK：SQLite 的
  `ALTER TABLE ... ADD COLUMN` 带不了表级 CHECK，而长度口径是裁决项（ADR-0031
  D0 / spec D14），改口径不该再动一次表结构。

**必须可空且无默认**：`NULL` 的语义是「没写备注」；trim 后空串由应用层归一成
`NULL`，故库里不出现 `''`。存量项一律 `NULL`（本期无任何既有备注值，不回填）。

**为什么不做整表重建**：本迁移只加列，SQLite `ALTER TABLE ADD COLUMN` 原生支持，
不需要 0035/0038 的重建手法——`asset_items` 被 5 张子表（`assets` /
`asset_item_tags` / `asset_item_names` / `asset_item_attr_history` /
`asset_item_rename_tokens`）用 FK 引用，重建的风险远大于收益。

幂等三态：
- 表不存在 → `skipped`（新库/桩库，`create_all` 按新模型建表，迁移不掺和）；
- 列已在 → `noop`（不重复 ALTER；SQLite 重复加列会 duplicate column）；
- 否则 → `added`，并校验类型/可空性/无默认 + `integrity_check=ok`。

回退：`downgrade(conn)` 是**对称的人工/测试入口**，不参与启动链（runner 只调
`upgrade`，见 `services/db_migration_service._load_script_module`）。删列用 SQLite
原生 `ALTER TABLE ... DROP COLUMN`（3.35+）：`note` 不参与任何索引/CHECK/触发器/
生成列，因此原生删列可用；SQLite < 3.35 时显式抛错，不静默跳过。**不用 alembic
`batch_alter_table`**：本仓未引入 alembic（`backend/pyproject.toml` /
`requirements.txt` 均无该依赖，全仓零 import），其 batch 模式对 SQLite 做的事
就是「重建表 + 拷贝行」，而这里重建会牵动上述 5 张子表 FK。偏离已记入
`.scratch/asset-record-edit/06-report.md`。

不动任何行数据、不写任何审计行；事务归 runner（`_run_script`），成功即登记
`schema_version=39`。
"""
import logging
import sqlite3

log = logging.getLogger(__name__)

TABLE = "asset_items"
COLUMN = "note"
COLUMN_DDL = "TEXT"

# 原生 DROP COLUMN 的最低 SQLite 版本（3.35.0 引入）
_MIN_DROP_COLUMN_VERSION = (3, 35, 0)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_def(conn, table: str, column: str):
    """(type, notnull, dflt_value) —— 确认「加的是可空、无默认的列」。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == column:
            return (r[2] or "").upper(), int(r[3]), r[4]
    return None


def _verify(conn) -> None:
    got = _column_def(conn, TABLE, COLUMN)
    if got != (COLUMN_DDL, 0, None):
        raise RuntimeError(
            f"0039 中止：{TABLE}.{COLUMN} 的类型/可空性/默认值不符"
            f"（期望 {COLUMN_DDL}、可空、无默认 / 实际 {got}）")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0039 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        log.warning("0039 跳过：缺少 %s 表（新库由 create_all 建新列）", TABLE)
        return "skipped"

    # ② 列已存在 → noop（不重复 ALTER），但仍校验形状（手工补过/上次中断的库）
    if _column_def(conn, TABLE, COLUMN) is not None:
        _verify(conn)
        log.info("0039 noop：%s.%s 已在", TABLE, COLUMN)
        return "noop"

    # ③ 加列 + 迁移后校验
    conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COLUMN}" {COLUMN_DDL}')
    _verify(conn)
    log.info("0039 added: %s.%s %s NULL", TABLE, COLUMN, COLUMN_DDL)
    return "added"


def downgrade(conn) -> str:
    """删列（对称回退；runner 不调用，供人工回滚与单测使用）。"""
    if not _table_exists(conn, TABLE):
        log.warning("0039 回退跳过：缺少 %s 表", TABLE)
        return "skipped"

    if _column_def(conn, TABLE, COLUMN) is None:
        log.info("0039 回退 noop：%s.%s 不存在", TABLE, COLUMN)
        return "noop"

    if sqlite3.sqlite_version_info < _MIN_DROP_COLUMN_VERSION:
        raise RuntimeError(
            f"0039 回退中止：SQLite {sqlite3.sqlite_version} 不支持"
            f" DROP COLUMN（需 ≥{'.'.join(map(str, _MIN_DROP_COLUMN_VERSION))}）")

    conn.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN "{COLUMN}"')

    if _column_def(conn, TABLE, COLUMN) is not None:
        raise RuntimeError(f"0039 回退中止：{TABLE}.{COLUMN} 仍在")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0039 回退中止：integrity_check={integrity}")

    log.info("0039 dropped: %s.%s", TABLE, COLUMN)
    return "dropped"