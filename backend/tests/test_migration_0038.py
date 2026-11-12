"""迁移 0038：教学规则扩第三类 `aggregate`（表重建，SQLite 不能 ALTER CHECK）。

三态与形状校验都要钉死：

1. 老形状（`rule_type IN ('rename','ignore','exclude')`）→ `rebuilt`，
   **行数据一条不动**，重建后可写 `rule_type='aggregate'`；
2. 二跑 → `noop`；`create_all` 建出的新形状也是 `noop`（启动链是 create_all → 迁移）；
3. 表不存在（桩库）→ `skipped`，不猜形状。

数据一律合成（「示例」词），不含真实机构名或金额。
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  —— 在 Base.metadata 上注册全部表
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0038 = _load_script_module(
    f"{_MIGRATIONS_DIR}/0038_learned_rules_aggregate_type.py")
RULE_TABLE = "source_group_learned_rules"


def _run_script(conn, module) -> str:
    """与 test_composition_service 同款：单脚本一事务（runner 语义）。"""
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        result = module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _old_shape_db(path) -> sqlite3.Connection:
    """0031 建出的老形状（含一行 rename、一行 exclude）。"""
    conn = sqlite3.connect(path)
    conn.executescript('''
    CREATE TABLE source_groups (id INTEGER PRIMARY KEY, label TEXT);
    CREATE TABLE "source_group_learned_rules" (
      id INTEGER NOT NULL,
      source_group_id INTEGER NOT NULL,
      rule_type TEXT NOT NULL,
      "match" TEXT NOT NULL,
      "replace" TEXT,
      note TEXT,
      sort_order INTEGER NOT NULL,
      origin TEXT NOT NULL,
      hit_count INTEGER NOT NULL,
      last_hit_at DATETIME,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      CONSTRAINT ck_sglr_type CHECK (rule_type IN ('rename','ignore','exclude')),
      CONSTRAINT ck_sglr_origin CHECK (origin IN ('manual','import','migration')),
      FOREIGN KEY(source_group_id) REFERENCES source_groups (id) ON DELETE CASCADE
    );
    CREATE INDEX ix_sglr_group
      ON "source_group_learned_rules" (source_group_id, sort_order);
    CREATE UNIQUE INDEX uq_sglr_group_type_match_replace
      ON "source_group_learned_rules"
      (source_group_id, rule_type, "match", COALESCE("replace", ''));
    ''')
    conn.execute("INSERT INTO source_groups (id, label) VALUES (1, '示例来源组')")
    conn.execute(
        'INSERT INTO "source_group_learned_rules"'
        ' (id, source_group_id, rule_type, "match", "replace", note, sort_order,'
        '  origin, hit_count, last_hit_at, created_at, updated_at)'
        " VALUES (1, 1, 'rename', '示例旧名', '示例新名', '示例备注', 0,"
        "         'manual', 0, NULL, '2026-11-01T00:00:00', '2026-11-01T00:00:00')")
    conn.execute(
        'INSERT INTO "source_group_learned_rules"'
        ' (id, source_group_id, rule_type, "match", "replace", note, sort_order,'
        '  origin, hit_count, last_hit_at, created_at, updated_at)'
        " VALUES (2, 1, 'exclude', '示例不录入', NULL, NULL, 1,"
        "         'migration', 0, NULL, '2026-11-01T00:00:00', '2026-11-01T00:00:00')")
    conn.commit()
    return conn


def test_0038_rebuilds_old_shape_and_keeps_rows(tmp_path):
    conn = _old_shape_db(str(tmp_path / "old_rules.db"))

    assert _run_script(conn, M0038) == "rebuilt"

    # 行数据一条不动（含 replace 的 NULL / 非 NULL 两种形态）
    assert conn.execute(
        'SELECT id, rule_type, "match", "replace", note, sort_order, origin'
        f' FROM "{RULE_TABLE}" ORDER BY id').fetchall() == [
        (1, "rename", "示例旧名", "示例新名", "示例备注", 0, "manual"),
        (2, "exclude", "示例不录入", None, None, 1, "migration"),
    ]
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                       (RULE_TABLE,)).fetchone()[0]
    assert "'aggregate'" in ddl and "ck_sglr_type" in ddl

    # 新值域（aggregate）可写；老值域仍可写；越界值仍被 CHECK 拒
    conn.execute(
        'INSERT INTO "source_group_learned_rules"'
        ' (source_group_id, rule_type, "match", "replace", note, sort_order,'
        '  origin, hit_count, last_hit_at, created_at, updated_at)'
        " VALUES (1, 'aggregate', '示例合计', NULL, NULL, 2, 'manual', 0, NULL,"
        "         '2026-11-09T00:00:00', '2026-11-09T00:00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            'INSERT INTO "source_group_learned_rules"'
            ' (source_group_id, rule_type, "match", "replace", note, sort_order,'
            '  origin, hit_count, last_hit_at, created_at, updated_at)'
            " VALUES (1, 'bogus', '示例越界', NULL, NULL, 3, 'manual', 0, NULL,"
            "         '2026-11-09T00:00:00', '2026-11-09T00:00:00')")

    # 二跑：值域已含 aggregate → noop（不重建、不搬行）
    assert _run_script(conn, M0038) == "noop"
    assert conn.execute(f'SELECT COUNT(*) FROM "{RULE_TABLE}"').fetchone()[0] == 3
    conn.close()


def test_0038_is_noop_on_create_all_shape(tmp_path):
    """create_all 建出的新形状（模型已含 aggregate）必须直接判定为 noop。"""
    path = str(tmp_path / "shape.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    assert _run_script(conn, M0038) == "noop"
    conn.close()


def test_0038_skips_without_table(tmp_path):
    """表不存在（桩库）：不猜形状，显式跳过。"""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert _run_script(conn, M0038) == "skipped"
    conn.close()