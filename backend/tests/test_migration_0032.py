"""0032：凭证两列（`image_meta` / `recognition_json`）+ 清空脏原始名 迁移测试。

被测对象是迁移脚本本身（不是服务），用 `_load_script_module` 按文件加载（同
`test_migration_0031.py`）。库在 `tmp_path` 里现建真实文件 SQLite——迁移走裸
`sqlite3.Connection`，内存库在连接间不共享，不能用。

覆盖（票 01 范围 + spec §3 第 5 条）：

1. 空表（9 列、无数据）→ 加到 11 列，两列均为可空 TEXT；
2. 脏名 `shot_upload_%` 命中数 23 → 0，5 条真实客户端原名不变，28 行一行不少，
   `file_path` / `file_sha256` 逐字不变（迁移**不搬文件**）；
3. 二跑 `noop`（含 UPDATE 影响 0 行），数据零变化；
4. `skipped` 两个分支：表不存在；表在但缺基准列（桩表形状）；
5. 只缺一列 → 只补缺的那列（整体不跳），脏名照清；
6. 迁移结构与 `Base.metadata.create_all` 的产物对拍（列序 + 两列的
   type/notnull 逐字），两条路径在真实部署里都会出现；
7. `PRAGMA foreign_keys=ON` 下正常执行（0032 只加列不重建，与 0030 的「必须 FK off」
   相反——加列不需要摘外键，故 FK 开着手动重放也必须成功）；
8. 不 commit：`BEGIN → upgrade → ROLLBACK` 后列与数据全部回退（事务归 runner 管）；
9. 迁移后 `integrity_check=ok` + `foreign_key_check` 空。

测试数据一律合成（`示例组` / `示例-A.png` / `shot_upload_ab12cd34.jpg` 之类），
不抄运行库的真实文件名、机构名或摘要。
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  全表注册进 Base.metadata
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0032 = _load_script_module(f"{_MIGRATIONS_DIR}/0032_add_evidence_image_meta_and_recognition.py")

TABLE = "screenshot_evidence"
BASE_COLUMNS = [
    "id", "source_group_id", "file_path", "file_sha256", "original_filename",
    "data_date", "supplement_rows", "engine", "created_at",
]
NEW_COLUMNS = ["image_meta", "recognition_json"]
EXPECTED = BASE_COLUMNS + NEW_COLUMNS

# 0030 重建后的运行库形状（9 列）+ 外键父表；本迁移只碰 original_filename 一列
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
    PRIMARY KEY (id),
    CONSTRAINT ck_evidence_engine CHECK (engine IS NULL OR engine IN ('ocr','ai'))
);

CREATE INDEX ix_screenshot_evidence_group_date
    ON screenshot_evidence (source_group_id, data_date);
"""

N_DIRTY = 23        # spec §1.2：运行库 23 条临时名
N_REAL = 5          # 真实客户端原名 5 条（本迁移不得改动）


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None          # 手动事务：与 runner 同口径
    return conn


def _run(conn) -> str:
    """按 runner 的事务口径跑一次：BEGIN → upgrade → COMMIT（异常则 ROLLBACK 后抛出）。"""
    conn.execute("BEGIN")
    try:
        result = M0032.upgrade(conn)
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


def _seed(conn, *, dirty=N_DIRTY, real=N_REAL) -> None:
    """插合成凭证：`dirty` 条临时名 + `real` 条真实客户端名。"""
    conn.execute("INSERT INTO source_groups (id, slug, label) VALUES (1,'demo','示例组')")
    rows = []
    for i in range(dirty):
        rows.append((i + 1, f"2026/07/shot_upload_ab12cd{i:02d}_0f0f0f0f0f0f.jpg",
                     f"{i:02d}" * 32, f"shot_upload_ab12cd{i:02d}.jpg"))
    for j in range(real):
        rows.append((dirty + j + 1, f"2026/10/示例-{j}.png", "ff" * 32,
                     f"示例-{j}.png"))
    conn.executemany(
        "INSERT INTO screenshot_evidence"
        " (id, source_group_id, file_path, file_sha256, original_filename,"
        "  data_date, engine, created_at)"
        " VALUES (?,1,?,?,?, '2026-10-30', 'ocr', '2026-10-30T23:22:13')",
        rows)


def _dirty_count(conn) -> int:
    return conn.execute(
        "SELECT count(*) FROM screenshot_evidence"
        " WHERE original_filename LIKE 'shot_upload_%'").fetchone()[0]


def _snapshot(conn) -> list[tuple]:
    """除 `original_filename` 外的全部行内容（迁移不该动它们）。"""
    return conn.execute(
        "SELECT id, source_group_id, file_path, file_sha256, data_date,"
        " supplement_rows, engine, created_at FROM screenshot_evidence ORDER BY id"
    ).fetchall()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "m0032.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


# ---------------- 1. 加列 ----------------

def test_adds_two_columns_to_empty_table(db_path):
    conn = _connect(db_path)
    assert _columns(conn) == BASE_COLUMNS        # 迁移前：9 列

    assert _run(conn) == "added=image_meta,recognition_json cleaned_original_filename=0"

    assert _columns(conn) == EXPECTED            # 迁移后：11 列，追加在尾部
    for name in NEW_COLUMNS:
        assert _col_row(conn, name)[1:] == ("TEXT", 0, None, 0), name

    remaining = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    assert remaining == 0
    conn.close()


# ---------------- 2~3. 脏名清理 + 二跑幂等 ----------------

def test_cleans_dirty_names_only(db_path):
    conn = _connect(db_path)
    _seed(conn)
    
    assert _dirty_count(conn) == N_DIRTY

    before = _snapshot(conn)
    total_before = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    real_before = sorted(
        r[0] for r in conn.execute(
            f'SELECT original_filename FROM "{TABLE}" WHERE original_filename IS NOT NULL'
            " AND original_filename NOT LIKE 'shot_upload_%'"))
    assert total_before == N_DIRTY + N_REAL == 28
    assert len(real_before) == N_REAL

    assert _run(conn) == (
        f"added=image_meta,recognition_json cleaned_original_filename={N_DIRTY}")

    # ① 脏名归零，真名逐条不变
    assert _dirty_count(conn) == 0
    real_after = sorted(
        r[0] for r in conn.execute(
            f'SELECT original_filename FROM "{TABLE}" WHERE original_filename IS NOT NULL'))
    assert real_after == real_before

    # ② 行数不变、非 original_filename 的每一列都不变（不搬文件、不动 file_path/sha）
    assert conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0] == total_before
    assert _snapshot(conn) == before

    # ③ 新列全 NULL（本票不写数据）
    for name in NEW_COLUMNS:
        assert conn.execute(
            f'SELECT count(*) FROM "{TABLE}" WHERE {name} IS NOT NULL').fetchone()[0] == 0

    # ④ 迁移后校验面
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_second_run_is_noop(db_path):
    conn = _connect(db_path)
    _seed(conn)
    
    assert _run(conn).startswith("added=")

    after_first = _snapshot(conn)
    names_first = conn.execute(
        f'SELECT id, original_filename FROM "{TABLE}" ORDER BY id').fetchall()

    assert _run(conn) == "noop"                  # 二跑：没加列 + UPDATE 影响 0 行
    assert _columns(conn) == EXPECTED
    assert _snapshot(conn) == after_first
    assert conn.execute(
        f'SELECT id, original_filename FROM "{TABLE}" ORDER BY id').fetchall() == names_first
    conn.close()


# ---------------- 4. skipped 两个分支 ----------------

def test_skips_when_table_missing(tmp_path):
    path = tmp_path / "no_table.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    

    assert _run(conn) == "skipped"
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()[0] == 0
    conn.close()


def test_skips_when_base_columns_missing(tmp_path):
    """桩表形状（如只有 id）：不是本迁移的活，幂等跳过而不是加两列造出半张表。"""
    path = tmp_path / "stub.db"
    conn = _connect(path)
    conn.execute(f'CREATE TABLE "{TABLE}" (id INTEGER PRIMARY KEY, note TEXT)')
    

    assert _run(conn) == "skipped"
    assert _columns(conn) == ["id", "note"]
    conn.close()


# ---------------- 5. 逐列判定（单列存在只补缺的那列） ----------------

@pytest.mark.parametrize("present", ["image_meta", "recognition_json"])
def test_partial_columns_adds_only_missing_one(db_path, present):
    conn = _connect(db_path)
    conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{present}" TEXT')
    _seed(conn)
    
    missing = next(c for c in NEW_COLUMNS if c != present)

    assert _run(conn) == f"added={missing} cleaned_original_filename={N_DIRTY}"

    assert _columns(conn) == BASE_COLUMNS + [present, missing]
    assert _dirty_count(conn) == 0
    conn.close()


def test_columns_exist_but_dirty_names_still_cleaned(db_path):
    """两列已在（create_all 先行）但脏名还在 → 不当 noop 放过，仍要清。

    spec D3 的清理是**数据**义务，不是加列的副作用：加列已就位不等于脏名已清。
    """
    conn = _connect(db_path)
    for name in NEW_COLUMNS:
        conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" TEXT')
    _seed(conn)
    

    assert _run(conn) == f"added=- cleaned_original_filename={N_DIRTY}"
    assert _dirty_count(conn) == 0
    assert _run(conn) == "noop"                  # 再跑才是 noop
    conn.close()


# ---------------- 6. 与 create_all 对拍 ----------------

def test_column_layout_parity_with_orm(tmp_path, db_path):
    """两条建表路径的列序与两列定义必须一致（否则 ORM 读到不同的物理布局）。"""
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    migrated, orm = _connect(db_path), _connect(orm_path)
    assert _run(migrated).startswith("added=")

    assert _columns(migrated) == _columns(orm)          # 11 列，顺序也一致
    for name in NEW_COLUMNS:
        assert _col_row(migrated, name) == _col_row(orm, name), name
    migrated.close()
    orm.close()


# ---------------- 7. FK 开着也能跑（只加列不重建） ----------------

def test_runs_with_foreign_keys_on(db_path):
    conn = _connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    _seed(conn)
    

    # 0030 是表重建，必须 FK off；0032 只加列 + UPDATE，FK 开着必须照样成功
    assert _run(conn).startswith("added=")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert _dirty_count(conn) == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


# ---------------- 8. 不 commit（事务归 runner） ----------------

def test_upgrade_does_not_commit(db_path):
    conn = _connect(db_path)
    _seed(conn)
    

    conn.execute("BEGIN")
    M0032.upgrade(conn)
    conn.execute("ROLLBACK")

    # DDL 在 SQLite 里可事务化：回滚后列与脏名都应回到迁移前
    assert _columns(conn) == BASE_COLUMNS
    assert _dirty_count(conn) == N_DIRTY
    conn.close()