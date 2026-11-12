"""0034：`llm_providers` 四列测试状态 迁移测试。

规格：`.scratch/llm-provider-status/spec.md` D1（2026-11-06 冻结）。

被测对象是迁移脚本本身（不是服务），用 `_load_script_module` 按文件加载（同
`test_migration_0032.py` / `test_migration_0033.py`）。库在 `tmp_path` 里现建真实
文件 SQLite——迁移走裸 `sqlite3.Connection`，内存库在连接间不共享，不能用。

覆盖：

1. 11 列旧表 → 15 列，四列均为可空、无默认值（`NULL` 而非 `false`/`0`）；
2. 存量行的其它列一字不变（只加列、不动数据），四列全 `NULL`；
3. 二跑 `noop`（不重建表、不动数据）；
4. `skipped` 两个分支：表不存在；表在但缺基准列（桩表形状）；
5. 只缺部分列 → 只补缺的那些（整体不跳）；
6. 与 `Base.metadata.create_all` 的产物对拍（列序 + 四列的 type/notnull 逐字）；
7. 不 commit：`BEGIN → upgrade → ROLLBACK` 后列与数据全部回退（事务归 runner 管）；
8. 迁移后 `integrity_check=ok`。

数据一律合成（`示例供应商` / `https://llm.example.invalid/v1`），不抄运行库内容。
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  全表注册进 Base.metadata
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0034 = _load_script_module(f"{_MIGRATIONS_DIR}/0034_add_llm_provider_test_status.py")

TABLE = "llm_providers"

# 0034 之前的 11 列（列序＝ORM 定义序，也是真实 DDL 序）
BASE_COLUMNS = [
    "id", "label", "kind", "base_url", "api_key", "model",
    "is_default", "enabled", "sort_order", "created_at", "updated_at",
]
# 本迁移新增的四列 → (列名, 期望 type)
NEW_COLUMNS = [
    ("last_tested_at", "DATETIME"),
    ("last_test_ok", "BOOLEAN"),
    ("last_test_message", "VARCHAR(300)"),
    ("last_test_elapsed_ms", "INTEGER"),
]
EXPECTED = BASE_COLUMNS + [name for name, _ in NEW_COLUMNS]

# 迁移前运行库形状（11 列 + 两个索引）
SCHEMA = """
CREATE TABLE llm_providers (
    id INTEGER NOT NULL,
    label VARCHAR(100) NOT NULL,
    kind VARCHAR(50) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    api_key VARCHAR(500) NOT NULL,
    model VARCHAR(200) NOT NULL,
    is_default BOOLEAN,
    enabled BOOLEAN,
    sort_order INTEGER,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (label)
);

CREATE INDEX ix_llm_providers_enabled ON llm_providers (enabled);
CREATE INDEX ix_llm_providers_sort ON llm_providers (sort_order);
"""


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None          # 手动事务：与 runner 同口径
    return conn


def _run(conn) -> str:
    """按 runner 的事务口径跑一次：BEGIN → upgrade → COMMIT（异常则 ROLLBACK 后抛出）。"""
    conn.execute("BEGIN")
    try:
        result = M0034.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _columns(conn, table=TABLE) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _col_row(conn, name, table=TABLE):
    """某一列的 (name, type, notnull, dflt_value, pk)。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == name:
            return (r[1], (r[2] or "").upper(), r[3], r[4], r[5])
    return None


def _seed(conn, n=2) -> None:
    """插合成供应商行（四列还不存在，故只写 11 列）。"""
    rows = [
        (i + 1, f"示例供应商{i + 1}", "openai_compatible",
         f"https://llm{i + 1}.example.invalid/v1", "sk-demo-1234567", "demo-model",
         1 if i == 0 else 0, 1, i, "2026-11-06T09:00:00", "2026-11-06T09:00:00")
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO llm_providers (id, label, kind, base_url, api_key, model,"
        " is_default, enabled, sort_order, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def _snapshot(conn) -> list[tuple]:
    """11 个旧列的全部行内容（迁移不该动它们）。"""
    return conn.execute(
        f'SELECT {", ".join(BASE_COLUMNS)} FROM "{TABLE}" ORDER BY id').fetchall()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "m0034.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


# ---------------- 1. 加列 ----------------

def test_adds_four_columns_to_empty_table(db_path):
    conn = _connect(db_path)
    assert _columns(conn) == BASE_COLUMNS        # 迁移前：11 列

    assert _run(conn) == "added=" + ",".join(name for name, _ in NEW_COLUMNS)

    assert _columns(conn) == EXPECTED            # 迁移后：15 列，追加在尾部
    for name, type_name in NEW_COLUMNS:
        # 可空（notnull=0）+ 无默认值（dflt_value=None）＝「从未测试」可表达为 NULL
        assert _col_row(conn, name)[1:] == (type_name, 0, None, 0), name

    assert conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0] == 0
    conn.close()


# ---------------- 2. 存量行：四列 NULL，其余不变 ----------------

def test_existing_rows_untouched_and_new_columns_null(db_path):
    conn = _connect(db_path)
    _seed(conn)

    before = _snapshot(conn)
    assert len(before) == 2

    assert _run(conn).startswith("added=")

    # ① 其它 11 列逐字段不变、行数不变（只加列，不动数据）
    assert _snapshot(conn) == before

    # ② 四列全 NULL（不是 false、不是 0）——「从未测试」与「测试失败」可分
    for name, _t in NEW_COLUMNS:
        assert conn.execute(
            f'SELECT count(*) FROM "{TABLE}" WHERE "{name}" IS NOT NULL'
        ).fetchone()[0] == 0, name

    # ③ 迁移后校验面
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


# ---------------- 3. 二跑幂等 ----------------

def test_second_run_is_noop(db_path):
    conn = _connect(db_path)
    _seed(conn)

    assert _run(conn).startswith("added=")
    after_first = _snapshot(conn)
    cols_first = _columns(conn)

    assert _run(conn) == "noop"                  # 二跑：没加列
    assert _columns(conn) == cols_first
    assert _columns(conn) == EXPECTED
    assert _snapshot(conn) == after_first
    conn.close()


# ---------------- 4. skipped 两个分支 ----------------

def test_skips_when_table_missing(tmp_path):
    conn = _connect(tmp_path / "no_table.db")
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    assert _run(conn) == "skipped"
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()[0] == 0
    conn.close()


def test_skips_when_base_columns_missing(tmp_path):
    """桩表形状（如只有 id）：不是本迁移的活，幂等跳过而不是加四列造出半张表。"""
    conn = _connect(tmp_path / "stub.db")
    conn.execute(f'CREATE TABLE "{TABLE}" (id INTEGER PRIMARY KEY, note TEXT)')

    assert _run(conn) == "skipped"
    assert _columns(conn) == ["id", "note"]
    conn.close()


# ---------------- 5. 逐列判定（已有部分列只补缺的） ----------------

@pytest.mark.parametrize("present", [name for name, _ in NEW_COLUMNS])
def test_partial_columns_adds_only_missing_ones(db_path, present):
    conn = _connect(db_path)
    ddl = dict(NEW_COLUMNS)[present]
    conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{present}" {ddl}')
    _seed(conn)

    missing = [name for name, _ in NEW_COLUMNS if name != present]

    assert _run(conn) == "added=" + ",".join(missing)

    assert _columns(conn) == BASE_COLUMNS + [present] + missing
    conn.close()


# ---------------- 6. 与 create_all 对拍 ----------------

def test_column_layout_parity_with_orm(tmp_path, db_path):
    """两条建表路径的列序与四列定义必须一致（否则 ORM 读到不同的物理布局）。"""
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    migrated, orm = _connect(db_path), _connect(orm_path)
    assert _run(migrated).startswith("added=")

    assert _columns(migrated) == _columns(orm)          # 15 列，顺序也一致
    for name, _t in NEW_COLUMNS:
        assert _col_row(migrated, name) == _col_row(orm, name), name
    migrated.close()
    orm.close()


# ---------------- 7. 不 commit（事务归 runner） ----------------

def test_upgrade_does_not_commit(db_path):
    conn = _connect(db_path)
    _seed(conn)
    before = _snapshot(conn)

    conn.execute("BEGIN")
    M0034.upgrade(conn)
    conn.execute("ROLLBACK")

    # DDL 在 SQLite 里可事务化：回滚后列应回到迁移前
    assert _columns(conn) == BASE_COLUMNS
    assert _snapshot(conn) == before
    conn.close()