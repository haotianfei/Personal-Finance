"""迁移 0039：`asset_items` 加项级备注列 `note TEXT NULL`（ADR-0031 D0）。

被测对象是迁移脚本本身（不是服务），用 `_load_script_module` 按文件加载（同
`test_migration_0034.py` / `test_migration_0038.py`）。库在 `tmp_path` 里现建真实
文件 SQLite——迁移走裸 `sqlite3.Connection`，内存库在连接间不共享，不能用。

覆盖：

1. 老形状加列 → `added`，类型 `TEXT`、`notnull=0`、无默认；存量行 note 为 `NULL`
   （不回填），其余列一字不变；多行/中文文本可写可读；二跑 `noop`；
2. `create_all` 建出的新形状 → `noop`，且 `note` 是物理末列（迁移 `ADD COLUMN`
   只能追加到末位，两边列序必须一致）；
3. 表不存在（桩库）→ `skipped`（upgrade / downgrade 都是）；
4. 回退：`downgrade` 删列后**行与子表 FK 数据全在**、子表外键形状不变，
   note 值随列一起消失；再 `upgrade` 列长回来（数据不可恢复，符合预期）；
5. 不 commit：`BEGIN → upgrade → ROLLBACK` 后列消失（事务归 runner 管）；
6. 形状不符（列已是 NOT NULL）→ `RuntimeError`，不静默放过。

数据一律合成（「示例项」/「示例备注」），不含真实机构名或金额。
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  —— 在 Base.metadata 上注册全部表
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0039 = _load_script_module(f"{_MIGRATIONS_DIR}/0039_asset_item_note.py")

TABLE = "asset_items"
CHILD_TABLE = "asset_item_tags"


def _run_script(conn, module, entry="upgrade") -> str:
    """与 test_migration_0038 同款：单脚本一事务（runner 语义）。"""
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        result = getattr(module, entry)(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _note_def(conn, table=TABLE):
    """`note` 的 (type, notnull, dflt_value, pk)；不存在返回 None。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == "note":
            return (r[2] or "").upper(), int(r[3]), r[4], int(r[5])
    return None


def _old_shape_db(path) -> sqlite3.Connection:
    """0039 之前的最小形状（无 note 列，含一行存量项 + 一张 FK 子表）。"""
    conn = sqlite3.connect(path)
    conn.executescript(f'''
    CREATE TABLE "{TABLE}" (
      id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
      display_name VARCHAR(100) NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'active',
      archived_effective_from DATE,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL
    );
    CREATE TABLE "tags" (
      id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
      name VARCHAR(50) NOT NULL,
      kind VARCHAR(20) NOT NULL
    );
    CREATE TABLE "{CHILD_TABLE}" (
      item_id INTEGER NOT NULL REFERENCES "{TABLE}"(id),
      tag_id INTEGER NOT NULL REFERENCES "tags"(id),
      PRIMARY KEY (item_id, tag_id)
    );
    ''')
    conn.execute(
        f'INSERT INTO "{TABLE}" (id, display_name, status, created_at, updated_at)'
        " VALUES (1, '示例项', 'draft', '2026-11-01T00:00:00',"
        "         '2026-11-01T00:00:00')")
    conn.execute("INSERT INTO tags (id, name, kind) VALUES (1, '示例标签', 'free')")
    conn.execute(f'INSERT INTO "{CHILD_TABLE}" (item_id, tag_id) VALUES (1, 1)')
    conn.commit()
    return conn


def _create_all_db(path) -> str:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return str(path)


def test_0039_adds_nullable_note_and_is_idempotent(tmp_path):
    conn = _old_shape_db(str(tmp_path / "old_items.db"))

    assert _run_script(conn, M0039) == "added"

    # 形状：TEXT、可空（notnull=0）、无默认、非主键
    assert _note_def(conn) == ("TEXT", 0, None, 0)
    row = dict(zip([r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")')],
                   conn.execute(f'SELECT * FROM "{TABLE}"').fetchone()))
    # 存量行不回填：note 保持 NULL，其余列一字不动
    assert row == {"id": 1, "display_name": "示例项", "status": "draft",
                   "archived_effective_from": None,
                   "created_at": "2026-11-01T00:00:00",
                   "updated_at": "2026-11-01T00:00:00", "note": None}

    # 可写可读：多行 + 中文（列级无长度 CHECK，上限在应用层）
    text = "示例备注第一行\n示例备注第二行：含中文与 emoji 💰"
    conn.execute(f'UPDATE "{TABLE}" SET note=? WHERE id=1', (text,))
    assert conn.execute(f'SELECT note FROM "{TABLE}" WHERE id=1').fetchone()[0] == text

    # 二跑：列已在 → noop（不重复 ALTER、不动数据）
    assert _run_script(conn, M0039) == "noop"
    assert conn.execute(f'SELECT note FROM "{TABLE}" WHERE id=1').fetchone()[0] == text
    conn.close()


def test_0039_is_noop_on_create_all_shape_and_note_is_last_column(tmp_path):
    """create_all 建出的新形状必须判定为 noop，且 note 是物理末列。"""
    path = _create_all_db(tmp_path / "shape.db")
    conn = sqlite3.connect(path)

    assert _note_def(conn) == ("TEXT", 0, None, 0)
    cols = [r[1] for r in conn.execute('PRAGMA table_info("asset_items")')]
    assert cols[-1] == "note"   # 迁移只能追加到末位，两边列序必须一致

    assert _run_script(conn, M0039) == "noop"
    assert _note_def(conn) == ("TEXT", 0, None, 0)
    conn.close()


def test_0039_skips_without_table(tmp_path):
    """表不存在（桩库）：不猜形状，显式跳过；回退同。"""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert _run_script(conn, M0039) == "skipped"
    assert _run_script(conn, M0039, entry="downgrade") == "skipped"
    conn.close()


def test_0039_downgrade_drops_column_and_up_restores_it(tmp_path):
    """回退＝只摘 note 列：行、子表 FK 行、外键形状全部不受影响。"""
    conn = _old_shape_db(str(tmp_path / "undo.db"))
    assert _run_script(conn, M0039) == "added"
    conn.execute(f'UPDATE "{TABLE}" SET note=\'示例备注\' WHERE id=1')
    fks_before = conn.execute(f'PRAGMA foreign_key_list("{CHILD_TABLE}")').fetchall()

    assert _run_script(conn, M0039, entry="downgrade") == "dropped"
    assert _note_def(conn) is None
    assert conn.execute(f'SELECT id, display_name, status FROM "{TABLE}"').fetchall() == [
        (1, "示例项", "draft")]
    assert conn.execute(f'SELECT item_id, tag_id FROM "{CHILD_TABLE}"').fetchall() == [(1, 1)]
    assert conn.execute(f'PRAGMA foreign_key_list("{CHILD_TABLE}")').fetchall() == fks_before

    # 再 up：列长回来（数据随列一起丢弃，符合「回退即丢该列内容」的语义）
    assert _run_script(conn, M0039) == "added"
    assert _note_def(conn) == ("TEXT", 0, None, 0)
    assert conn.execute(f'SELECT note FROM "{TABLE}" WHERE id=1').fetchone()[0] is None
    conn.close()


def test_0039_does_not_commit_by_itself(tmp_path):
    """不在脚本内 commit：BEGIN → upgrade → ROLLBACK 后列与数据全部回退。"""
    conn = _old_shape_db(str(tmp_path / "tx.db"))
    conn.isolation_level = None
    conn.execute("BEGIN")
    assert M0039.upgrade(conn) == "added"
    conn.execute("ROLLBACK")

    assert _note_def(conn) is None
    assert conn.execute(f'SELECT id FROM "{TABLE}"').fetchall() == [(1,)]
    conn.close()


def test_0039_rejects_wrong_shape(tmp_path):
    """列已存在但不可空（手改过的库）→ 报错，不静默放过。"""
    conn = sqlite3.connect(str(tmp_path / "bad.db"))
    conn.execute(f'CREATE TABLE "{TABLE}" (id INTEGER PRIMARY KEY, note TEXT NOT NULL)')
    conn.commit()

    with pytest.raises(RuntimeError, match="0039 中止"):
        _run_script(conn, M0039)
    conn.close()