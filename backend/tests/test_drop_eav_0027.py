"""0027：EAV 分类两表拆除迁移测试（引用改指 → 删表 → 三条落地断言）。

覆盖：
  - 空库路径（新 `create_all` 已不含两表）→ `missing`，不动任何表；
  - 存量形状（外键全指 `attributes`）→ 先迁引用（按名字映射）再删表，索引/CHECK/
    触发器随重建复原；
  - 映射查不到同名机构 → 中止并给出可读错误（不置 NULL、不新建字典行）；
  - 有未被覆盖的表仍指向 `attributes` → 前置扫描中止（防删表后才发现）。
"""
import importlib.util
import sqlite3

import pytest

from services.db_migration_service import _MIGRATIONS_DIR


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0027", f"{_MIGRATIONS_DIR}/0027_drop_eav_attributes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M0027 = _load()

# 0027 之前的存量形状：五处外键都指向 attributes（0016/0015/create_all 的历史产物）
LEGACY_DDL = """
CREATE TABLE attributes (
  id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, type VARCHAR(50) NOT NULL,
  parent_id INTEGER REFERENCES attributes(id), level INTEGER NOT NULL DEFAULT 0);
CREATE TABLE asset_attributes (
  asset_id INTEGER NOT NULL, attribute_id INTEGER NOT NULL,
  PRIMARY KEY (asset_id, attribute_id),
  FOREIGN KEY(attribute_id) REFERENCES attributes (id));
CREATE TABLE institutions (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, kind VARCHAR(20) NOT NULL);
CREATE TABLE members (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, role VARCHAR(20) NOT NULL);
CREATE TABLE source_groups (id INTEGER PRIMARY KEY, slug TEXT);
CREATE TABLE source_group_accounts (
  source_group_id INTEGER NOT NULL, attribute_id INTEGER NOT NULL,
  value VARCHAR(255), created_at DATETIME,
  PRIMARY KEY (source_group_id, attribute_id),
  FOREIGN KEY(source_group_id) REFERENCES source_groups (id),
  FOREIGN KEY(attribute_id) REFERENCES attributes (id));
CREATE INDEX idx_sga_group ON source_group_accounts (source_group_id);
CREATE INDEX idx_sga_attr ON source_group_accounts (attribute_id);
CREATE TABLE asset_items (id INTEGER PRIMARY KEY);
CREATE TABLE asset_item_owner_changes (
  id INTEGER PRIMARY KEY,
  asset_item_id INTEGER NOT NULL REFERENCES asset_items (id),
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('member','family_pool')),
  owner_id INTEGER REFERENCES attributes (id),
  effective_date DATE NOT NULL,
  origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','migration')),
  created_at TEXT NOT NULL,
  CHECK ((owner_kind='member' AND owner_id IS NOT NULL)
      OR (owner_kind='family_pool' AND owner_id IS NULL)));
CREATE INDEX ix_item_owner_owner ON asset_item_owner_changes (owner_id);
CREATE TABLE goals (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, scope_kind TEXT NOT NULL,
  scope_owner_id INTEGER, is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  CONSTRAINT ck_goals_scope CHECK (scope_kind IN ('family','member')),
  CONSTRAINT ck_goals_scope_owner CHECK (
    (scope_kind='family' AND scope_owner_id IS NULL)
    OR (scope_kind='member' AND scope_owner_id IS NOT NULL)),
  FOREIGN KEY(scope_owner_id) REFERENCES attributes (id));
CREATE INDEX ix_goals_scope_owner ON goals (scope_owner_id);
CREATE TABLE expense_categories (id INTEGER PRIMARY KEY);
CREATE TABLE fixed_expense_templates (
  id INTEGER PRIMARY KEY, account_id INTEGER,
  FOREIGN KEY(account_id) REFERENCES attributes (id) ON DELETE SET NULL);
CREATE INDEX ix_fixed_expense_account ON fixed_expense_templates (account_id);
CREATE TABLE expenses (
  id INTEGER PRIMARY KEY, account_id INTEGER,
  FOREIGN KEY(account_id) REFERENCES attributes (id));
CREATE INDEX ix_expenses_account ON expenses (account_id);
"""

LEGACY_ROWS = """
INSERT INTO attributes (id, name, type) VALUES
  (1, '示例银行', 'account'), (2, '示例基金', 'account'),
  (3, '示例成员甲', 'owner'), (4, '示例成员乙', 'owner');
INSERT INTO institutions (id, name, kind) VALUES (7, '示例银行', 'bank'), (8, '示例基金', 'fund');
INSERT INTO members (id, name, role) VALUES (5, '示例成员甲', 'self'), (6, '示例成员乙', 'spouse');
INSERT INTO source_groups (id, slug) VALUES (1, 'sg');
INSERT INTO source_group_accounts (source_group_id, attribute_id, created_at)
  VALUES (1, 1, 't'), (1, 2, 't');
INSERT INTO asset_items (id) VALUES (1), (2);
INSERT INTO asset_item_owner_changes
  (id, asset_item_id, owner_kind, owner_id, effective_date, origin, created_at)
  VALUES (1, 1, 'member', 3, '2026-01-01', 'migration', 't'),
         (2, 2, 'family_pool', NULL, '2026-01-01', 'manual', 't');
INSERT INTO goals (id, name, scope_kind, scope_owner_id, is_primary, created_at)
  VALUES (1, '示例家庭目标', 'family', NULL, 1, 't'),
         (2, '示例成员目标', 'member', 3, 0, 't');
INSERT INTO fixed_expense_templates (id, account_id) VALUES (1, 1);
INSERT INTO expenses (id, account_id) VALUES (1, 2);
INSERT INTO asset_attributes (asset_id, attribute_id) VALUES (10, 1);
"""


def _legacy_db(tmp_path, name="m.db", extra_ddl="", extra_rows=""):
    conn = sqlite3.connect(tmp_path / name)
    conn.isolation_level = None
    conn.executescript(LEGACY_DDL + extra_ddl)
    conn.executescript(LEGACY_ROWS + extra_rows)
    return conn


def _run(conn):
    conn.execute("BEGIN")
    try:
        result = M0027.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _fk_parent(conn, table, column):
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        if row[3] == column:
            return row[2]
    return None


# ---------------------------------------------------------------- 空库路径


def test_empty_db_path_is_noop(tmp_path):
    """新库（create_all 已按新 models 建表，不含两表）→ missing，不建任何表。"""
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE goals (id INTEGER PRIMARY KEY)")
    before = _tables(conn)
    assert _run(conn) == "missing"
    assert _tables(conn) == before
    assert "attributes" not in _tables(conn)
    conn.close()


# ---------------------------------------------------------------- 存量路径


def test_legacy_refs_migrated_then_tables_dropped(tmp_path):
    conn = _legacy_db(tmp_path)
    result = _run(conn)

    # ① 两表已物理删除
    assert "attributes" not in _tables(conn)
    assert "asset_attributes" not in _tables(conn)
    assert result.startswith("dropped=asset_attributes+attributes;")

    # ② 源组关联表：按「账户名 == 机构名」映射，形状收窄为三列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(source_group_accounts)")]
    assert cols == ["id", "source_group_id", "institution_id"]
    assert conn.execute(
        "SELECT source_group_id, institution_id FROM source_group_accounts"
        " ORDER BY institution_id").fetchall() == [(1, 7), (1, 8)]
    assert _fk_parent(conn, "source_group_accounts", "institution_id") == "institutions"
    sga_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='source_group_accounts'")}
    assert {"ix_sga_group", "ix_sga_institution"} <= sga_idx

    # ③ goals → members；成员目标的范围值按名字映射（示例成员甲 3 → 5）
    assert _fk_parent(conn, "goals", "scope_owner_id") == "members"
    assert conn.execute(
        "SELECT id, scope_owner_id FROM goals ORDER BY id").fetchall() == [
            (1, None), (2, 5)]
    # 重建保住了原 CHECK 与索引
    assert "ix_goals_scope_owner" in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO goals (id, name, scope_kind, scope_owner_id,"
                     " is_primary, created_at) VALUES (3, 'x', 'member', NULL, 0, 't')")

    # ④ 支出域 → institutions（模板保留 ON DELETE SET NULL 语义）
    assert _fk_parent(conn, "expenses", "account_id") == "institutions"
    assert _fk_parent(conn, "fixed_expense_templates", "account_id") == "institutions"
    assert conn.execute("SELECT account_id FROM expenses WHERE id=1").fetchone()[0] == 8
    assert conn.execute(
        "SELECT account_id FROM fixed_expense_templates WHERE id=1").fetchone()[0] == 7

    # ⑤ 归属史 owner_id → members（family_pool 行仍是 NULL，不受影响）
    assert _fk_parent(conn, "asset_item_owner_changes", "owner_id") == "members"
    assert conn.execute(
        "SELECT id, owner_id FROM asset_item_owner_changes ORDER BY id").fetchall() == [
            (1, 5), (2, None)]
    assert "ix_item_owner_owner" in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}

    # ⑥ 落地断言：FK 一致 + 完整性 ok
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_rerun_after_upgrade_is_noop(tmp_path):
    """已升级库再跑：attributes 已不存在 → missing（幂等，不重复重建）。"""
    conn = _legacy_db(tmp_path)
    _run(conn)
    assert _run(conn) == "missing"
    assert _fk_parent(conn, "goals", "scope_owner_id") == "members"
    conn.close()


# ---------------------------------------------------------------- 守卫


def test_missing_institution_aborts_with_names(tmp_path):
    """机构表缺同名行 → 中止并列出受影响行；不置 NULL、不自动建机构。"""
    conn = _legacy_db(tmp_path, extra_ddl="",
                      extra_rows="DELETE FROM institutions WHERE name='示例基金';")
    with pytest.raises(RuntimeError) as ei:
        _run(conn)
    msg = str(ei.value)
    assert "source_group_accounts.attribute_id" in msg
    assert "示例基金" in msg
    # 中止发生在任何 DDL 之前：两表与数据仍在
    assert {"attributes", "asset_attributes"} <= _tables(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_attributes").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM institutions").fetchone()[0] == 1
    conn.close()


def test_uncovered_fk_to_attributes_aborts(tmp_path):
    """前置扫描：有不认识的表仍指向 attributes → 中止（别等删表后才炸）。"""
    conn = _legacy_db(
        tmp_path,
        extra_ddl="CREATE TABLE note_links (id INTEGER PRIMARY KEY,"
                  " attr_id INTEGER REFERENCES attributes (id));")
    with pytest.raises(RuntimeError) as ei:
        _run(conn)
    assert "note_links" in str(ei.value)
    assert "attributes" in _tables(conn)
    conn.close()