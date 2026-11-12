"""0028：存量归属回填迁移测试（史最新一行 → `asset_items.owner_id`）。

ADR-0020 D3：归属唯一真相源是列；史档只是 append-only 审计。0026 建列未回填，
本迁移把「史最新一行」搬到列上。覆盖：

- 只填 `owner_id IS NULL` 的项，列已有值不覆盖（回填≠重置）；
- 「最新」取 `effective_date DESC, created_at DESC, id DESC`（同 current_owner 口径）；
- `family_pool`（D6 已退役的存量合成归属）保持 NULL；脏 owner（members 无此 id）不写；
- **不新增任何史行**（回填不是归属变更事件）；
- 双跑幂等（第二次无候选）；
- 迁移文件能被 runner 按编号发现并排在最后（否则等于没写）。
"""
import sqlite3
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from models import AssetItem, AssetItemOwnerChange, Member
from services.db_migration_service import (
    _MIGRATIONS_DIR,
    _discover_scripts,
    _load_script_module,
)

M0028 = _load_script_module(f"{_MIGRATIONS_DIR}/0028_owner_column_backfill.py")
TODAY = date.today()


@pytest.fixture()
def store(tmp_path):
    """文件库（迁移用裸 sqlite3 连接，不能用内存库）。返回 (path, Session)。"""
    path = str(tmp_path / "backfill.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield path, Session
    engine.dispose()


def _run(path: str):
    """按 runner 的事务口径跑一次 upgrade（BEGIN → upgrade → COMMIT）。"""
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        result = M0028.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return result


def _history_snapshot(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT id, asset_item_id, owner_kind, owner_id, effective_date,"
            " reason, origin, created_at"
            " FROM asset_item_owner_changes ORDER BY id").fetchall()
    finally:
        conn.close()


def _owner_of(path: str, item_id: int):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT owner_id FROM asset_items WHERE id = ?", (item_id,)).fetchone()[0]
    finally:
        conn.close()


def _seed(Session, path: str) -> dict:
    """四项存量形状 + 成员字典。"""
    db = Session()
    a = Member(name="示例成员甲", role="self")
    b = Member(name="示例成员乙", role="spouse")
    db.add_all([a, b])
    db.commit()
    a_id, b_id = a.id, b.id          # 关库前取值（DetachedInstance 之后不可再读属性）

    def item(name: str, owner_id=None):
        row = AssetItem(display_name=name, status="draft", owner_id=owner_id)
        db.add(row)
        db.commit()
        return row.id

    def hist(item_id, kind, owner_id, on_date, created_at):
        db.add(AssetItemOwnerChange(
            asset_item_id=item_id, owner_kind=kind, owner_id=owner_id,
            effective_date=on_date, reason="合成", origin="manual",
            created_at=created_at))
        db.commit()

    latest_wins = item("示例-两项史")              # 取最新一行
    hist(latest_wins, "member", a_id, TODAY - timedelta(days=2), "2026-01-01T00:00:00")
    hist(latest_wins, "member", b_id, TODAY - timedelta(days=1), "2026-01-02T00:00:00")

    family_pool = item("示例-家庭池")             # 最新是退役的 family_pool
    hist(family_pool, "member", a_id, TODAY - timedelta(days=3), "2026-01-01T00:00:00")
    hist(family_pool, "family_pool", None, TODAY - timedelta(days=1), "2026-01-02T00:00:00")

    already = item("示例-列已有值", owner_id=a_id)  # 列有值：不覆盖
    hist(already, "member", b_id, TODAY, "2026-01-03T00:00:00")

    dirty = item("示例-脏值")                      # 史指向不存在的成员
    hist(dirty, "member", 987654, TODAY, "2026-01-04T00:00:00")

    db.close()
    return {"latest": latest_wins, "family_pool": family_pool,
            "already": already, "dirty": dirty,
            "a": a_id, "b": b_id}


def test_backfill_fills_null_column_with_latest_member_row(store):
    path, Session = store
    ids = _seed(Session, path)
    before = _history_snapshot(path)

    assert _run(path) == "backfilled=1"           # 只有 latest 这一项可回填
    assert _owner_of(path, ids["latest"]) == ids["b"]      # 最新行胜出（不是较早的甲）
    assert _owner_of(path, ids["family_pool"]) is None     # 退役合成归属：保持未标注
    assert _owner_of(path, ids["already"]) == ids["a"]     # 列已有值：不覆盖
    assert _owner_of(path, ids["dirty"]) is None           # 脏 owner：不猜、不清洗
    assert _history_snapshot(path) == before               # 回填不写史（审计不改写）


def test_backfill_second_run_is_noop(store):
    path, Session = store
    ids = _seed(Session, path)
    _run(path)
    after_first_owner = _owner_of(path, ids["latest"])
    after_first_hist = _history_snapshot(path)

    assert _run(path) == "backfilled=0"            # 有候选但无可写项：重跑零副作用
    assert _owner_of(path, ids["latest"]) == after_first_owner
    assert _history_snapshot(path) == after_first_hist


def test_migration_is_discovered_and_sorted_after_0027():
    scripts = [(v, n) for v, n, _ in _discover_scripts(_MIGRATIONS_DIR)]
    versions = [v for v, _ in scripts]
    assert versions == sorted(versions)            # runner 按编号升序执行
    assert 28 in versions                          # 新迁移不会被漏扫（文件名即真相）
    name = scripts[versions.index(28)][1]
    assert name.strip("_") == "owner_column_backfill"
    assert versions[-1] >= 28                      # 排在既有迁移之后，不会被跳过