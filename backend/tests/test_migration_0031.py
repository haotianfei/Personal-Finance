"""0031：教学规则拆表 + 四个历史死键清理 迁移测试。

被测对象是迁移脚本本身（不是服务），用 `_load_script_module` 按文件加载（同
`test_owner_batch.py` / `test_owner_column_backfill.py` 手法）。库在 `tmp_path` 里
现建真实文件 SQLite——迁移走裸 `sqlite3.Connection`，内存库在连接间不共享，不能用。

覆盖（spec §3 P1-⑤）：

1. 两张表的列/CHECK/索引/外键与 `models.py` 逐字一致（含与 `create_all` 的产物对拍）；
2. `rename` + `ignore` + 混用形态的 `exclude`（裸 str 与 dict）逐条搬表，`sort_order`＝
   数组下标，`origin='migration'`，裸 str 的 `replace` 为 NULL；
3. 迁移后 JSON 里三键与四个死键都没了，`layout` / `amount_header` 与未知键仍在；
4. 删完为空的组 `rules` 整列变 NULL（"无配置"与"空配置"统一）；
5. 坏 JSON / 非对象 / 三键不是数组 → 中止且零写入（解析全部完成后才开始写）；
6. 二跑幂等（`noop`，行数不变）；
7. 唯一索引挡重复；
8. `COALESCE(replace,'')` 让「同组同型同 match、replace 都为空」的行**互相冲突**——
   这正是表达式索引的目的：普通唯一约束下 SQLite 认为 NULL 互不相等，两条都会被放过。
9. 守卫：缺 `source_groups` 的桩库 → `skipped`。

测试数据一律合成（`示例支付平台` 之类），不抄运行库的真实来源组标识。
"""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine

import models  # noqa: F401  全表注册进 Base.metadata
from database import Base
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0031 = _load_script_module(f"{_MIGRATIONS_DIR}/0031_learned_rules_split.py")

RULE_TABLE = "source_group_learned_rules"
VOCAB_TABLE = "parser_vocabularies"

# 迁移只碰 rules 一列，其余列按运行库形状裁剪
SCHEMA = """
CREATE TABLE source_groups (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    label TEXT NOT NULL,
    asset_prefix TEXT NOT NULL,
    guide TEXT,
    guide_image TEXT,
    rules TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
"""


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None          # 手动事务：与 runner 同口径
    return conn


def _sorted_indexes(conn, table):
    """`PRAGMA index_list` 的第 0 列 `seq` 是建索引先后（内部序号），不是语义。
    只比 (名称, 唯一, 来源, 是否 partial)，再按名称排序。"""
    rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    return sorted((r[1], r[2], r[3], r[4]) for r in rows)


def _run(conn) -> str:
    """按 runner 的事务口径跑一次：BEGIN → upgrade → COMMIT（异常则 ROLLBACK 后抛出）。"""
    conn.execute("BEGIN")
    try:
        result = M0031.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _add_group(conn, gid: int, rules) -> None:
    """插一个来源组；rules 传 dict（自动 JSON 化）/ str（原样，造坏 JSON 用）/ None。"""
    if rules is None:
        payload = None
    elif isinstance(rules, str):
        payload = rules
    else:
        payload = json.dumps(rules, ensure_ascii=False)
    conn.execute(
        "INSERT INTO source_groups (id, slug, label, asset_prefix, rules)"
        " VALUES (?,?,?,?,?)",
        (gid, f"grp{gid}", f"示例组{gid}", f"示例前缀{gid}", payload))


def _rules_of(conn, gid: int):
    raw = conn.execute("SELECT rules FROM source_groups WHERE id = ?", (gid,)).fetchone()[0]
    return None if raw is None else json.loads(raw)


def _rows(conn) -> list[tuple]:
    return conn.execute(
        f'SELECT source_group_id, rule_type, "match", "replace", note, sort_order,'
        ' origin, hit_count, last_hit_at'
        f' FROM "{RULE_TABLE}" ORDER BY id').fetchall()


def _count(conn) -> int:
    return conn.execute(f'SELECT count(*) FROM "{RULE_TABLE}"').fetchone()[0]


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "m0031.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


# ---------------- 1. 结构与 ORM 对拍 ----------------

def test_tables_columns_constraints_and_indexes(db_path):
    conn = _connect(db_path)
    assert _run(conn) == "noop"        # 无组数据：只建表

    vocab = [(r[1], r[2], r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{VOCAB_TABLE}")')]
    assert vocab == [
        ("key", "TEXT", 1, 1),
        ("add_items", "TEXT", 1, 0),
        ("remove_items", "TEXT", 1, 0),
        ("updated_at", "DATETIME", 1, 0),
    ]

    learned = [(r[1], r[2], r[3], r[5])
               for r in conn.execute(f'PRAGMA table_info("{RULE_TABLE}")')]
    assert learned == [
        ("id", "INTEGER", 1, 1),
        ("source_group_id", "INTEGER", 1, 0),
        ("rule_type", "TEXT", 1, 0),
        ("match", "TEXT", 1, 0),
        ("replace", "TEXT", 0, 0),
        ("note", "TEXT", 0, 0),
        ("sort_order", "INTEGER", 1, 0),
        ("origin", "TEXT", 1, 0),
        ("hit_count", "INTEGER", 1, 0),
        ("last_hit_at", "DATETIME", 0, 0),
        ("created_at", "DATETIME", 1, 0),
        ("updated_at", "DATETIME", 1, 0),
    ]

    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (RULE_TABLE,)
    ).fetchone()[0]
    assert "ck_sglr_type" in ddl and "ck_sglr_origin" in ddl
    assert "CASCADE" in ddl

    fk = conn.execute(f'PRAGMA foreign_key_list("{RULE_TABLE}")').fetchall()
    assert [(r[2], r[3], r[4], r[6]) for r in fk] == [
        ("source_groups", "source_group_id", "id", "CASCADE")]

    indexes = {r[1]: r[2] for r in conn.execute(f'PRAGMA index_list("{RULE_TABLE}")')}
    assert indexes["uq_sglr_group_type_match_replace"] == 1   # unique
    assert indexes["ix_sglr_group"] == 0                     # 普通索引
    uq_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ("uq_sglr_group_type_match_replace",)).fetchone()[0]
    assert "COALESCE" in uq_sql and '"replace"' in uq_sql
    conn.close()


def test_migration_ddl_parity_with_orm(tmp_path, db_path):
    """迁移建的表与 `Base.metadata.create_all` 建的表逐列/逐索引相等。

    两条路径都会在真实部署里出现（启动链是 create_all → run_migrations，修复/重建
    脚本则可能只跑迁移），分叉的后果是 ORM 读不存在的列或约束口径不一致。
    """
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    migrated, orm = _connect(db_path), _connect(orm_path)
    assert _run(migrated) == "noop"
    for table in (VOCAB_TABLE, RULE_TABLE):
        assert (migrated.execute(f'PRAGMA table_info("{table}")').fetchall()
                == orm.execute(f'PRAGMA table_info("{table}")').fetchall()), table
        assert (migrated.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
                == orm.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()), table
        # index_list 的行序取决于建索引的先后，不是语义——按索引名排序后比
        assert (_sorted_indexes(migrated, table)
                == _sorted_indexes(orm, table)), table
        assert (migrated.execute(f'PRAGMA index_info("uq_sglr_group_type_match_replace")').fetchall()
                == orm.execute(f'PRAGMA index_info("uq_sglr_group_type_match_replace")').fetchall())
    migrated.close()
    orm.close()


# ---------------- 2~4. 搬迁、JSON 重推、空配置归一 ----------------

def test_three_keys_migrated_with_shapes_normalized(db_path):
    conn = _connect(db_path)
    _add_group(conn, 1, {
        "layout": "card",
        "amount_header": ["金额", "份额"],
        "rename": [{"match": "示例旧名", "replace": "示例新名", "note": "改名说明"}],
        "ignore": [{"match": "示例忽略词", "note": "噪声"}],
        # exclude 混用两种形态：裸 str（旧）+ dict（新）
        "exclude": ["示例支付平台", {"match": "示例合计行", "note": "汇总"}],
    })

    result = _run(conn)
    assert result.startswith("inserted=4 ")
    assert _rows(conn) == [
        (1, "rename", "示例旧名", "示例新名", "改名说明", 0, "migration", 0, None),
        (1, "ignore", "示例忽略词", None, "噪声", 0, "migration", 0, None),
        (1, "exclude", "示例支付平台", None, None, 0, "migration", 0, None),
        (1, "exclude", "示例合计行", None, "汇总", 1, "migration", 0, None),
    ]
    conn.close()


def test_json_rewrite_keeps_group_config_and_unknown_keys(db_path):
    conn = _connect(db_path)
    _add_group(conn, 1, {
        "layout": "vertical",
        "amount_header": "金额",          # 单串形态（运行库里两种写法都出现过）
        "future_key": {"nested": 1},     # 未知键：不是教学规则也不是死键，必须原样保留
        "name_col": "名称",
        "total_header": "合计",
        "amount_suffix": "元",
        "anchor_summary": "总金额",
        "rename": [{"match": "示例旧名", "replace": "示例新名"}],
        "ignore": ["示例忽略词"],
        "exclude": ["示例支付平台"],
    })

    _run(conn)
    kept = _rules_of(conn, 1)
    assert set(kept) == {"layout", "amount_header", "future_key"}
    assert kept["layout"] == "vertical"
    assert kept["amount_header"] == "金额"
    assert kept["future_key"] == {"nested": 1}
    conn.close()


def test_rules_column_becomes_null_when_nothing_left(db_path):
    conn = _connect(db_path)
    _add_group(conn, 1, {
        "rename": [{"match": "示例旧名", "replace": "示例新名"}],
        "exclude": ["示例支付平台"],
    })
    _add_group(conn, 2, {"layout": "card"})     # 对照组：没有三键 → 一行不动
    _add_group(conn, 3, None)                   # 无配置：保持 NULL

    result = _run(conn)
    assert "nulled=1" in result and "rewritten=1" in result
    assert conn.execute(
        "SELECT rules FROM source_groups WHERE id = 1").fetchone()[0] is None
    assert _rules_of(conn, 2) == {"layout": "card"}
    assert conn.execute(
        "SELECT rules FROM source_groups WHERE id = 3").fetchone()[0] is None
    conn.close()


# ---------------- 5. 坏输入 fail-fast 且零写入 ----------------

def test_bad_json_aborts_before_any_write(db_path):
    conn = _connect(db_path)
    assert _run(conn) == "noop"                  # 先建表，便于断言"零行"
    _add_group(conn, 1, {"exclude": ["示例支付平台"]})   # 合法：解析通过但排在坏组之前
    bad = '{"exclude": ["示例支付平台"], "ignore": [{"match": "示例忽略词"'
    _add_group(conn, 2, bad)

    with pytest.raises(RuntimeError) as excinfo:
        _run(conn)
    message = str(excinfo.value)
    assert "0031 中止" in message
    assert "id=2" in message
    assert bad not in message                    # 绝不回显 rules 内容
    assert "示例支付平台" not in message

    # 事务回滚后：新表零行、合法组的 JSON 一字未改（坏 JSON 不能换取部分迁移）
    assert _count(conn) == 0
    assert _rules_of(conn, 1) == {"exclude": ["示例支付平台"]}
    conn.close()


@pytest.mark.parametrize("rules", [
    "[]",                                        # JSON 合法但不是对象
    '"示例源组"',
    '{"exclude": "示例支付平台"}',                # 三键不是数组
    '{"rename": {"match": "示例旧名"}}',
])
def test_unusable_rules_shape_aborts(db_path, rules):
    conn = _connect(db_path)
    assert _run(conn) == "noop"
    _add_group(conn, 1, rules)

    with pytest.raises(RuntimeError) as excinfo:
        _run(conn)
    assert "0031 中止" in str(excinfo.value) and "id=1" in str(excinfo.value)
    assert _count(conn) == 0
    assert conn.execute(
        "SELECT rules FROM source_groups WHERE id = 1").fetchone()[0] == rules
    conn.close()


def test_entry_without_match_is_skipped_and_counted(db_path):
    """取不到 match 的条目跳过并计数；纯空白 match 照搬（运行期就是它当子串用）。"""
    conn = _connect(db_path)
    _add_group(conn, 1, {
        "exclude": ["", {"note": "没有 match 键"}, "示例支付平台"],
        "ignore": [{"match": "   "}, {"match": "示例忽略词"}],
        "rename": ["裸字符串不是 rename 形态", {"match": "示例旧名", "replace": "示例新名"}],
    })

    result = _run(conn)
    assert "inserted=4 " in result
    rows = _rows(conn)
    assert rows == [
        (1, "rename", "示例旧名", "示例新名", None, 1, "migration", 0, None),
        (1, "ignore", "   ", None, None, 0, "migration", 0, None),
        (1, "ignore", "示例忽略词", None, None, 1, "migration", 0, None),
        (1, "exclude", "示例支付平台", None, None, 2, "migration", 0, None),
    ]
    # 跳过 3 条：空串 / 缺 match 键 / rename 里的裸 str（跳过的那条 rename 也计一笔）
    assert "skipped=3 " in result
    conn.close()


def test_skips_when_source_groups_missing(tmp_path):
    """桩库（无 source_groups）：返回 skipped，不建任何表。"""
    path = tmp_path / "stub.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    assert _run(conn) == "skipped"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"unrelated"}
    conn.close()


# ---------------- 6. 幂等 ----------------

def test_second_run_is_noop(db_path):
    conn = _connect(db_path)
    _add_group(conn, 1, {
        "layout": "card",
        "rename": [{"match": "示例旧名", "replace": "示例新名"}],
        "exclude": ["示例支付平台"],
    })
    assert _run(conn).startswith("inserted=2 ")
    first_rows = _rows(conn)
    first_rules = _rules_of(conn, 1)

    assert _run(conn) == "noop"
    assert _rows(conn) == first_rows
    assert _rules_of(conn, 1) == first_rules
    conn.close()


def test_second_run_keeps_rows_when_json_has_learned_keys_again(db_path):
    """二跑时表里已有同键行（例如 JSON 被旧写路径写回）：不增行、不覆盖，但仍重推 JSON。"""
    conn = _connect(db_path)
    _add_group(conn, 1, {"exclude": ["示例支付平台"]})
    _run(conn)
    # 模拟旧路径把三键又写回 JSON（同一键）
    conn.execute("UPDATE source_groups SET rules = ? WHERE id = 1",
                 (json.dumps({"exclude": ["示例支付平台"]}, ensure_ascii=False),))

    result = _run(conn)
    assert result.startswith("inserted=0 ")
    assert "already_present=1" in result and "rewritten=1" in result
    assert _count(conn) == 1
    assert conn.execute("SELECT rules FROM source_groups WHERE id = 1").fetchone()[0] is None
    conn.close()


# ---------------- 7~8. 唯一索引 ----------------

def test_unique_index_rejects_duplicate(db_path):
    conn = _connect(db_path)
    _run(conn)
    _add_group(conn, 1, None)
    insert = (f'INSERT INTO "{RULE_TABLE}"'
              ' (source_group_id, rule_type, "match", "replace", sort_order, origin,'
              '  hit_count, created_at, updated_at)'
              " VALUES (1, 'rename', '示例旧名', '示例新名', 0, 'manual', 0, 't', 't')")
    conn.execute(insert)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert)
    assert _count(conn) == 1
    conn.close()


def test_coalesce_unique_index_treats_null_replace_as_empty(db_path):
    """`COALESCE("replace", '')` 让 replace 为空的同 match 行**互相冲突**。

    这是表达式索引存在的理由：SQLite 里 NULL 互不相等，普通唯一约束会放过两条
    `replace IS NULL` 的重复规则（运行期判重口径 `rule_identity` 认为它们是同一条）。
    反过来，`replace` 不同、或 `rule_type` 不同，就不是同一条规则，可以共存。
    """
    conn = _connect(db_path)
    _run(conn)
    _add_group(conn, 1, None)
    sql = (f'INSERT INTO "{RULE_TABLE}"'
           ' (source_group_id, rule_type, "match", "replace", sort_order, origin,'
           '  hit_count, created_at, updated_at)'
           ' VALUES (?, ?, ?, ?, 0, \'manual\', 0, \'t\', \'t\')')

    conn.execute(sql, (1, "exclude", "示例合计行", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (1, "exclude", "示例合计行", None))     # NULL 与 NULL 同一条
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (1, "exclude", "示例合计行", ""))       # NULL 与 '' 也是同一条

    conn.execute(sql, (1, "ignore", "示例合计行", None))          # 型不同 → 可共存
    conn.execute(sql, (1, "rename", "示例合计行", "示例新名"))     # replace 不同 → 可共存
    conn.execute(sql, (1, "rename", "示例合计行", None))           # replace 空/非空 → 可共存
    conn.execute(sql, (2, "exclude", "示例合计行", None))          # 组不同 → 可共存
    assert _count(conn) == 5
    conn.close()