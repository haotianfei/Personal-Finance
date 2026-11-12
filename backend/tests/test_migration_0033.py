"""0033：凭证按（sha, 来源组）去重 + 复合唯一索引 迁移测试。

被测对象是迁移脚本本身（不是服务），用 `_load_script_module` 按文件加载（同
`test_migration_0032.py`）。库在 `tmp_path` 里现建真实文件 SQLite——迁移走裸
`sqlite3.Connection`，内存库在连接间不共享，不能用。

覆盖（`.scratch/evidence-dedup-index/issues/01-dedupe-and-index.md` §5 + spec §3 的 2/3/4/6）：

1. 同组重复（一行被引用、一行四类引用全 0）→ 只删零引用那行，保留被引用行；
2. 四类引用（`assets.source_evidence_id` / `audit_log.evidence_ref` /
   `principal_changes.evidence_ref` / `transfer_events.evidence_ref`）**任一**非 0 → 该行不删；
3. 不同组同 sha 两行都不删（复合维度才是唯一性口径）；
4. 索引生效：同组重复插入被 DB 拒（`IntegrityError`），跨组同 sha 可并存；
5. 二跑 `noop`，行数不变；
6. 不可自动收敛（同组两行都被引用）→ 抛错中止且不建索引（判据从严的活口必须响）；
7. `skipped` 两个分支：表不存在；表在但缺关键列（无 `source_group_id`）；
8. 不 commit：`BEGIN → upgrade → ROLLBACK` 后行与索引全部回退（事务归 runner 管）；
9. 索引形状与 `create_all` 对拍（列序 + unique）。

合成数据：`示例组` / 形如 `aaaa…` 的假 sha / 虚构资产名，不抄运行库的真实摘要与数据。
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  全表注册进 Base.metadata
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0033 = _load_script_module(f"{_MIGRATIONS_DIR}/0033_dedupe_evidence_sha_group.py")

TABLE = "screenshot_evidence"
INDEX = "ux_screenshot_evidence_sha_group"
INDEX_COLUMNS = ["file_sha256", "source_group_id"]

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

# 0032 之后的运行库形状（11 列）+ 四类引用子表（只保留本迁移读取的列）
SCHEMA = """
CREATE TABLE source_groups (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    label TEXT NOT NULL
);

CREATE TABLE screenshot_evidence (
    id INTEGER NOT NULL,
    source_group_id INTEGER NOT NULL REFERENCES source_groups(id),
    file_path VARCHAR(500) NOT NULL,
    file_sha256 VARCHAR(64) NOT NULL,
    original_filename VARCHAR(255),
    data_date DATE,
    supplement_rows TEXT,
    engine VARCHAR(10),
    created_at DATETIME NOT NULL,
    image_meta TEXT,
    recognition_json TEXT,
    PRIMARY KEY (id),
    CONSTRAINT ck_evidence_engine CHECK (engine IS NULL OR engine IN ('ocr','ai'))
);

CREATE INDEX ix_screenshot_evidence_group_date
    ON screenshot_evidence (source_group_id, data_date);

CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    asset_name VARCHAR(100) NOT NULL,
    amount NUMERIC(15,2) NOT NULL,
    source_evidence_id INTEGER REFERENCES screenshot_evidence(id)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    evidence_ref INTEGER REFERENCES screenshot_evidence(id)
);

CREATE TABLE principal_changes (
    id INTEGER PRIMARY KEY,
    evidence_ref INTEGER REFERENCES screenshot_evidence(id)
);

CREATE TABLE transfer_events (
    id INTEGER PRIMARY KEY,
    evidence_ref INTEGER REFERENCES screenshot_evidence(id)
);
"""

# (引用表, 引用列) —— spec D3 的四类引用，逐字
REFERENCES = [
    ("assets", "source_evidence_id"),
    ("audit_log", "evidence_ref"),
    ("principal_changes", "evidence_ref"),
    ("transfer_events", "evidence_ref"),
]


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None          # 手动事务：与 runner 同口径
    return conn


def _run(conn) -> str:
    """按 runner 的事务口径跑一次：BEGIN → upgrade → COMMIT（异常则 ROLLBACK 后抛出）。"""
    conn.execute("BEGIN")
    try:
        result = M0033.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _columns(conn, table=TABLE) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _ids(conn) -> list[int]:
    return [r[0] for r in conn.execute(f'SELECT id FROM "{TABLE}" ORDER BY id')]


def _index_shape(conn, name=INDEX):
    """(unique, [列名…])；不存在 → None。"""
    for row in conn.execute(f'PRAGMA index_list("{TABLE}")'):
        if row[1] == name:
            return bool(row[2]), [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
    return None


def _ev(conn, ev_id, *, sha, gid, path=None) -> None:
    conn.execute(
        "INSERT INTO screenshot_evidence"
        " (id, source_group_id, file_path, file_sha256, original_filename,"
        "  data_date, engine, created_at)"
        " VALUES (?,?,?,?,?, '2026-11-06', 'ocr', '2026-11-06T10:00:00')",
        (ev_id, gid, path or f"2026/11/示例-{ev_id}.png", sha, f"示例-{ev_id}.png"))


def _ref(conn, table: str, column: str, ev_id: int, row_id: int = 1) -> None:
    """在引用表里插一条指向 ev_id 的行（表名/列名是常量，非用户输入）。"""
    if table == "assets":
        conn.execute(
            'INSERT INTO assets (id, asset_name, amount, source_evidence_id)'
            " VALUES (?, '示例-资产', 100.00, ?)", (row_id, ev_id))
    else:
        conn.execute(
            f'INSERT INTO "{table}" (id, "{column}") VALUES (?, ?)', (row_id, ev_id))


def _seed_groups(conn) -> None:
    conn.executemany(
        "INSERT INTO source_groups (id, slug, label) VALUES (?,?,?)",
        [(1, "demo-a", "示例组甲"), (2, "demo-b", "示例组乙")])


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "m0033.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


# ---------------- 1. 同组重复：只删零引用行 ----------------

def test_deletes_zero_ref_duplicate_in_same_group(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)        # 被账本引用 → 留
    _ev(conn, 2, sha=SHA_A, gid=1)        # 四类引用全 0 → 删
    _ref(conn, "assets", "source_evidence_id", 1)

    untouched = conn.execute(
        f'SELECT id, file_path, file_sha256, created_at FROM "{TABLE}" ORDER BY id').fetchall()

    assert _run(conn) == "deduped=1 ids=2 index=created"

    assert _ids(conn) == [1]
    # 保留行逐字未动（迁移只删行，不改任何列）
    assert conn.execute(
        f'SELECT id, file_path, file_sha256, created_at FROM "{TABLE}"').fetchall() \
        == [row for row in untouched if row[0] == 1]
    # 索引就位且是复合唯一
    assert _index_shape(conn) == (True, INDEX_COLUMNS)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


# ---------------- 2. 四类引用任一非 0 → 该行不删 ----------------

@pytest.mark.parametrize("table,column", REFERENCES)
def test_any_reference_keeps_row(db_path, table, column):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)        # 有引用 → 留
    _ev(conn, 2, sha=SHA_A, gid=1)        # 零引用 → 删
    _ref(conn, table, column, 1)

    assert _run(conn) == "deduped=1 ids=2 index=created"
    assert _ids(conn) == [1]
    conn.close()


def test_two_referenced_rows_keep_both_and_abort(db_path):
    """同组两行都被引用（各命中不同引用表）→ 判据不允许删任何一行 → 建不上索引。

    这时必须抛错点名（而不是把 `UNIQUE constraint failed` 甩给启动链），
    且整脚本回滚：两行都还在、索引没建。
    """
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)
    _ev(conn, 2, sha=SHA_A, gid=1)
    _ref(conn, "assets", "source_evidence_id", 1)
    _ref(conn, "audit_log", "evidence_ref", 2)

    with pytest.raises(RuntimeError, match="同组同 sha"):
        _run(conn)

    assert _ids(conn) == [1, 2]                  # 回滚：一行没删
    assert _index_shape(conn) is None            # 索引也没建
    conn.close()


# ---------------- 3. 跨组同 sha：两行都不删 ----------------

def test_same_sha_in_different_groups_both_kept(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)
    _ev(conn, 2, sha=SHA_A, gid=2)

    assert _run(conn) == "deduped=0 ids=- index=created"

    assert _ids(conn) == [1, 2]
    assert _index_shape(conn) == (True, INDEX_COLUMNS)
    conn.close()


# ---------------- 4. 索引生效：同组拒、跨组允 ----------------

def test_unique_index_rejects_same_group_duplicate(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)
    assert _run(conn) == "deduped=0 ids=- index=created"

    with pytest.raises(sqlite3.IntegrityError):
        _ev(conn, 2, sha=SHA_A, gid=1)

    _ev(conn, 3, sha=SHA_A, gid=2)               # 跨组同 sha 可并存
    _ev(conn, 4, sha=SHA_B, gid=1)               # 同组不同 sha 也可
    assert _ids(conn) == [1, 3, 4]
    conn.close()


# ---------------- 5. 二跑 noop ----------------

def test_second_run_is_noop(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)
    _ev(conn, 2, sha=SHA_A, gid=1)
    _ev(conn, 3, sha=SHA_B, gid=1)
    _ref(conn, "principal_changes", "evidence_ref", 1)

    assert _run(conn) == "deduped=1 ids=2 index=created"
    snapshot = conn.execute(f'SELECT * FROM "{TABLE}" ORDER BY id').fetchall()

    assert _run(conn) == "noop"                  # 索引已在 + 无重复行
    assert conn.execute(f'SELECT * FROM "{TABLE}" ORDER BY id').fetchall() == snapshot
    assert _index_shape(conn) == (True, INDEX_COLUMNS)
    conn.close()


# ---------------- 6. 并列零引用：保留 id 最小 ----------------

def test_all_zero_ref_duplicates_keep_smallest_id(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    for ev_id in (9, 3, 5):                      # 插入顺序与 id 顺序刻意不同
        _ev(conn, ev_id, sha=SHA_C, gid=1)

    assert _run(conn) == "deduped=2 ids=5,9 index=created"
    assert _ids(conn) == [3]
    conn.close()


# ---------------- 7. skipped 两个分支 ----------------

def test_skips_when_table_missing(tmp_path):
    path = tmp_path / "no_table.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    assert _run(conn) == "skipped"
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()[0] == 0
    conn.close()


def test_skips_without_source_group_id(tmp_path):
    """桩表形状（0029 之前的 `source_group` 快照列）：不是本迁移的活，幂等跳过。"""
    path = tmp_path / "stub.db"
    conn = _connect(path)
    conn.execute(
        f'CREATE TABLE "{TABLE}" (id INTEGER PRIMARY KEY, source_group TEXT,'
        " file_sha256 VARCHAR(64))")

    assert _run(conn) == "skipped"
    assert _columns(conn) == ["id", "source_group", "file_sha256"]
    assert _index_shape(conn) is None
    conn.close()


def test_missing_reference_tables_count_as_zero(db_path):
    """引用表缺失（桩库）→ 该表引用数按 0 计，去重照跑（表不在＝没有引用行）。"""
    conn = _connect(db_path)
    _seed_groups(conn)
    for table in ("assets", "audit_log", "principal_changes", "transfer_events"):
        conn.execute(f'DROP TABLE "{table}"')
    _ev(conn, 1, sha=SHA_A, gid=1)
    _ev(conn, 2, sha=SHA_A, gid=1)

    assert _run(conn) == "deduped=1 ids=2 index=created"
    assert _ids(conn) == [1]
    conn.close()


# ---------------- 8. 不 commit（事务归 runner） ----------------

def test_upgrade_does_not_commit(db_path):
    conn = _connect(db_path)
    _seed_groups(conn)
    _ev(conn, 1, sha=SHA_A, gid=1)
    _ev(conn, 2, sha=SHA_A, gid=1)

    conn.execute("BEGIN")
    M0033.upgrade(conn)
    conn.execute("ROLLBACK")

    # DDL 与 DML 在 SQLite 里都可事务化：回滚后行与索引都回到迁移前
    assert _ids(conn) == [1, 2]
    assert _index_shape(conn) is None
    conn.close()


# ---------------- 9. 与 create_all 对拍 ----------------

def test_index_shape_parity_with_orm(tmp_path, db_path):
    """两条建表路径（迁移 / create_all）给出的索引必须同形同列序。"""
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    migrated, orm = _connect(db_path), _connect(orm_path)
    _seed_groups(migrated)
    assert _run(migrated) == "deduped=0 ids=- index=created"

    assert _index_shape(migrated) == _index_shape(orm) == (True, INDEX_COLUMNS)
    # 全新库由 create_all 建好索引 → 迁移在它上面就是 noop
    assert _run(orm) == "noop"
    migrated.close()
    orm.close()