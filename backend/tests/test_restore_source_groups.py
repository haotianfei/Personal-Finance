"""s4b_restore_config 的恢复语义（凭证组随迁 / 只补空 / 幂等 / 名字匹配）。

被测对象是脚本而非服务，用 importlib 直接加载（同 test_owner_batch.py 的
`_load_script_module` 手法）。旧库与新库都在 tmp_path 里现建，结构按其真实形状裁剪。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rebuild" / "s4b_restore_config.py"

OLD_SCHEMA = """
CREATE TABLE attributes (id INTEGER PRIMARY KEY, name TEXT, type TEXT);
CREATE TABLE source_groups (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE, label TEXT, asset_prefix TEXT,
    guide TEXT, guide_image TEXT, rules TEXT, enabled INTEGER, sort_order INTEGER,
    created_at TEXT, updated_at TEXT);
CREATE TABLE source_group_accounts (
    source_group_id INTEGER, attribute_id INTEGER, value TEXT, created_at TEXT,
    PRIMARY KEY (source_group_id, attribute_id));
"""

NEW_SCHEMA = """
CREATE TABLE source_groups (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE, label TEXT, asset_prefix TEXT,
    guide TEXT, guide_image TEXT, rules TEXT, enabled INTEGER, sort_order INTEGER,
    created_at TEXT, updated_at TEXT);
CREATE TABLE institutions (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, active INTEGER);
CREATE TABLE source_group_accounts (
    id INTEGER PRIMARY KEY, source_group_id INTEGER, institution_id INTEGER,
    UNIQUE (source_group_id, institution_id));
CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY, source_group TEXT);
"""


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("s4b_restore_config", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mk(path: Path, schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


@pytest.fixture()
def dbs(tmp_path):
    old = _mk(tmp_path / "old.db", OLD_SCHEMA)
    # 旧库：3 组（1 组带规则、1 组无规则、1 组无对应机构映射）
    old.executemany(
        "INSERT INTO attributes (id, name, type) VALUES (?,?,?)",
        [(1, "A机构", "account"), (2, "B机构", "account"), (3, "未收录机构", "account")],
    )
    old.executemany(
        "INSERT INTO source_groups (id, slug, label, asset_prefix, guide, rules, "
        "enabled, sort_order) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "alpha", "甲组", "甲", "g1", '{"exclude": ["x"]}', 1, 0),
            (2, "beta", "乙组", "乙", None, '{"ignore": ["y"]}', 1, 0),
            (3, "gamma", "丙组", None, None, None, 1, 0),
        ],
    )
    old.executemany(
        "INSERT INTO source_group_accounts (source_group_id, attribute_id, value, created_at) "
        "VALUES (?,?,?,?)",
        [(1, 1, "ignored", "t"), (2, 2, "ignored", "t"), (3, 3, "ignored", "t")],
    )
    old.commit()

    new = _mk(tmp_path / "new.db", NEW_SCHEMA)
    # 新库：已 seed 1 组（slug 与旧库第 1 组同名，但 rules 为空 → 应被补）
    new.execute(
        "INSERT INTO source_groups (id, slug, label, enabled, sort_order) VALUES (1,'alpha','甲组',1,0)"
    )
    new.execute("INSERT INTO institutions (id, name, kind, active) VALUES (11,'A机构','bank',1)")
    new.execute("INSERT INTO institutions (id, name, kind, active) VALUES (12,'B机构','bank',1)")
    # 2 条历史凭证引用 beta / gamma，1 条引用 alpha
    new.executemany("INSERT INTO screenshot_evidence (id, source_group) VALUES (?,?)",
                    [(1, "alpha"), (2, "beta"), (3, "gamma")])
    new.commit()
    yield old, new, tmp_path
    old.close()
    new.close()


def _dangling(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM screenshot_evidence e WHERE NOT EXISTS "
        "(SELECT 1 FROM source_groups g WHERE g.slug = e.source_group)"
    ).fetchone()[0]


def _plan(mod, old, new):
    groups = mod.read_old_groups(old)
    links_raw = mod.read_old_accounts(old)
    inserts, fills = mod.plan_groups(new, groups)
    links, skipped = mod.plan_links(new, links_raw, {g["slug"] for g in inserts})
    return inserts, fills, links, skipped


def test_plan_does_not_write(mod, dbs):
    old, new, _ = dbs
    before = (new.execute("SELECT COUNT(*) FROM source_groups").fetchone()[0],
              new.execute("SELECT COUNT(*) FROM source_group_accounts").fetchone()[0])
    _plan(mod, old, new)
    after = (new.execute("SELECT COUNT(*) FROM source_groups").fetchone()[0],
             new.execute("SELECT COUNT(*) FROM source_group_accounts").fetchone()[0])
    assert before == after == (1, 0)


def test_apply_restores_groups_fills_rules_and_links(mod, dbs):
    old, new, _ = dbs
    inserts, fills, links, skipped = _plan(mod, old, new)
    assert {g["slug"] for g in inserts} == {"beta", "gamma"}
    # alpha 已存在但 rules/guide/asset_prefix 为空 → 只补空，不新增
    assert {f["col"] for f in fills} == {"rules", "guide", "asset_prefix"}
    assert all(f["slug"] == "alpha" for f in fills)
    assert len(links) == 2          # A机构 已存在（跳过 dup），B机构 新增，gamma 的机构未收录
    assert _dangling(new) == 2

    mod.apply_all(new, inserts, fills, links)

    assert new.execute("SELECT COUNT(*) FROM source_groups").fetchone()[0] == 3
    assert new.execute(
        "SELECT rules FROM source_groups WHERE slug='alpha'").fetchone()[0] == '{"exclude": ["x"]}'
    assert new.execute(
        "SELECT COUNT(*) FROM source_groups WHERE rules IS NOT NULL").fetchone()[0] == 2
    assert new.execute("SELECT COUNT(*) FROM source_group_accounts").fetchone()[0] == 2
    # gamma 引用的机构不在新库字典里 → 不新建机构、不写映射
    assert new.execute("SELECT COUNT(*) FROM institutions").fetchone()[0] == 2
    assert any(s["slug"] == "gamma" for s in skipped)
    # 凭证悬空：gamma 仍悬空（组已补回，故应为 0）
    assert _dangling(new) == 0


def test_second_apply_is_noop(mod, dbs):
    old, new, _ = dbs
    mod.apply_all(new, *_plan(mod, old, new)[:3])
    inserts, fills, links, _ = _plan(mod, old, new)
    assert (inserts, fills, links) == ([], [], [])
    mod.apply_all(new, inserts, fills, links)
    assert new.execute("SELECT COUNT(*) FROM source_groups").fetchone()[0] == 3


def test_existing_non_empty_value_not_overwritten(mod, dbs):
    old, new, _ = dbs
    new.execute(
        "UPDATE source_groups SET rules='{\"manual\": true}', guide='手工', "
        "asset_prefix='甲' WHERE slug='alpha'"
    )
    new.commit()
    _, fills, _, _ = _plan(mod, old, new)
    assert fills == []


def test_missing_institution_does_not_create_dictionary_row(mod, dbs):
    old, new, _ = dbs
    inserts, _, links, skipped = _plan(mod, old, new)
    mod.apply_all(new, inserts, [], links)
    assert new.execute(
        "SELECT COUNT(*) FROM institutions WHERE name='未收录机构'").fetchone()[0] == 0
    assert [s["reason"] for s in skipped] == ["机构名未匹配"]