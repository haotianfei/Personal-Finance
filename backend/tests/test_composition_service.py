"""构成关系（父子「由哪些项构成」）与**叶子计量**口径（ADR-0029 D1–D4；票 01/02）。

这组用例钉死四件事：

1. **一子一父**：同一子项挂第二个父 → 409 `COMPOSITION_PARENT_CONFLICT`
   （details 带 `existing_parent_item_id`），要换父必须先解除旧关系；
2. **禁环**：自环 400 `COMPOSITION_SELF_PARENT`（DB 的 `CHECK` 也兜底），两节点互指
   与三层祖父闭环 409 `COMPOSITION_CYCLE`；多层（子也可作父）本身是允许的；
3. **计量护栏（关键）**：父 100 ＋ 子 60 ＋ 子 40，同日同 active → `_sum_as_of` 与
   净值出口读 **100**（不是 200）；占比分母同源；解除构成后**自动恢复**到 200
   （可逆、无例外标记）；
4. **展示口径不动**：父项仍在 `GET /api/assets/current` 的读模型里带自己的金额，
   只是标注归前端（D14：隐藏会让用户以为数据丢了）。

另附迁移 `0037` 的双跑幂等与「模型 create_all 形状 = 迁移形状」校验（spec §3 验收
「迁移 0037 双跑幂等（第一次建表、第二次 noop）」）。

数据一律合成（「示例-」、虚构金额），不含任何真实机构/账号/金额。
"""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

import models  # noqa: F401  —— 在 Base.metadata 上注册全部表
from database import Base
from errors import ApiError, ErrorCode
from models import Asset, AssetItem, AuditLog, ItemComposition
from services import (
    analysis_service,
    asset_service,
    composition_service,
    proportion_service,
)
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0037 = _load_script_module(
    f"{_MIGRATIONS_DIR}/0037_item_compositions_and_batches.py")

D = date(2026, 3, 31)


# ---------------------------------------------------------------- 数据构造


def mk_item(db, name, status="draft"):
    """建资产项；默认 draft（四维全空）——避开 `ck_asset_items_dims_required`。

    与 `tests/test_draft_disclosure.py::mk_item` 同款：本组用例只关心计量口径，
    不关心分类，draft 是最省的合法形状（draft 照常计入合计，ADR-0020 D1）。
    """
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.flush()
    db.commit()
    return item


def add_row(db, item, amount, on_date=D, side="asset"):
    return create_asset(db, item.display_name, Decimal(str(amount)), on_date,
                        side=side, item_id=item.id)


@pytest.fixture()
def trio(db):
    """父 100 ＋ 子 60 ＋ 子 40（三个独立资产项，同日同 active）。"""
    parent = mk_item(db, "示例-贵金属汇总")
    child_a = mk_item(db, "示例-金产品A")
    child_b = mk_item(db, "示例-金产品B")
    add_row(db, parent, 100)
    add_row(db, child_a, 60)
    add_row(db, child_b, 40)
    return {"parent": parent, "child_a": child_a, "child_b": child_b}


def _raise(fn, *args, **kwargs) -> ApiError:
    """调用并断言抛 ApiError，返回它（便于断 code/details）。"""
    with pytest.raises(ApiError) as excinfo:
        fn(*args, **kwargs)
    return excinfo.value


# ---------------------------------------------------------------- 建边与读取


def test_create_edge_happy_path_and_is_aggregate(db, trio):
    parent, child = trio["parent"], trio["child_a"]

    edge = composition_service.create_edge(db, parent.id, child.id,
                                           note="示例：一页汇总、多页明细")

    assert edge.id is not None
    assert (edge.parent_item_id, edge.child_item_id) == (parent.id, child.id)
    assert edge.created_at is not None
    # 派生判定：父项＝汇总项（退出计量），子项不是
    assert composition_service.is_aggregate(db, parent.id) is True
    assert composition_service.is_aggregate(db, child.id) is False

    # 单层读取：children 带展示名；ancestors 从直接父往上
    children = composition_service.list_children(db, parent.id)
    assert [c["child_item_id"] for c in children] == [child.id]
    assert children[0]["display_name"] == child.display_name
    assert children[0]["note"] == "示例：一页汇总、多页明细"
    assert [a["item_id"] for a in composition_service.ancestors(db, child.id)] == [parent.id]
    assert composition_service.ancestors(db, parent.id) == []

    # 审计：entity=item_composition / action=create，after 带双端 id（同事务落库）
    rows = db.execute(
        select(AuditLog).where(AuditLog.entity == "item_composition")
    ).scalars().all()
    assert [r.action for r in rows] == ["create"]
    assert rows[0].row_id == edge.id
    assert f'"child_item_id": {child.id}' in rows[0].after_json


def test_child_cannot_have_two_parents(db, trio):
    parent, child, other = trio["parent"], trio["child_a"], trio["child_b"]
    composition_service.create_edge(db, parent.id, child.id)

    # 「一子一父」：第二个父必须先解除旧关系，不做隐式接管（票 02 D2）
    err = _raise(composition_service.create_edge, db, other.id, child.id)
    assert err.code == ErrorCode.COMPOSITION_PARENT_CONFLICT
    assert err.status_code == 409
    assert err.details["existing_parent_item_id"] == parent.id
    assert err.details["child_item_id"] == child.id

    # 同父重复挂也走同一码（撞 UNIQUE），且不产生第二条边
    assert _raise(composition_service.create_edge, db, parent.id,
                  child.id).code == ErrorCode.COMPOSITION_PARENT_CONFLICT
    assert len(db.execute(select(ItemComposition)).scalars().all()) == 1


def test_missing_items_raise_not_found(db, trio):
    child = trio["child_a"]
    for parent_id, child_id in ((999_001, child.id), (child.id, 999_002)):
        err = _raise(composition_service.create_edge, db, parent_id, child_id)
        assert err.code == ErrorCode.NOT_FOUND and err.status_code == 404


# ---------------------------------------------------------------- 自环与环


def test_self_parent_is_rejected_by_service_and_by_db(db, trio):
    parent = trio["parent"]

    err = _raise(composition_service.create_edge, db, parent.id, parent.id)
    assert err.code == ErrorCode.COMPOSITION_SELF_PARENT and err.status_code == 400

    # 兜底在 DB（CHECK）：绕过服务层直插也进不去（spec §3「父=子 → DB 约束拒绝」）
    db.add(ItemComposition(parent_item_id=parent.id, child_item_id=parent.id))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_two_node_cycle_is_rejected(db, trio):
    a, b = trio["parent"], trio["child_a"]
    composition_service.create_edge(db, a.id, b.id)

    err = _raise(composition_service.create_edge, db, b.id, a.id)
    assert err.code == ErrorCode.COMPOSITION_CYCLE and err.status_code == 409
    assert err.details["parent_item_id"] == b.id
    assert err.details["child_item_id"] == a.id
    # 环被拦下 → 没有多出第二条边
    assert len(db.execute(select(ItemComposition)).scalars().all()) == 1


def test_three_level_grandparent_cycle_is_rejected(db, trio):
    """A←B（A 为父）、B←C，再让 C 作父挂 A：A 已是 C 的祖父 → 成环。"""
    a, b, c = trio["parent"], trio["child_a"], trio["child_b"]
    composition_service.create_edge(db, a.id, b.id)
    composition_service.create_edge(db, b.id, c.id)      # 多层本身合法

    assert [x["item_id"] for x in composition_service.ancestors(db, c.id)] == [b.id, a.id]

    err = _raise(composition_service.create_edge, db, c.id, a.id)
    assert err.code == ErrorCode.COMPOSITION_CYCLE


# ---------------------------------------------------------------- 计量护栏


def _net_worth_exports(db):
    """三个读出口的同一口径读数（净值 / 占比分母 / 资产合计）。"""
    summary = analysis_service.get_summary(db)
    proportion = proportion_service.get_proportion_data(
        db, "asset_name", snapshot_date=D)
    net_worth, assets, _liab = analysis_service._sum_as_of(db, D)
    return {
        "sum_as_of": net_worth,
        "sum_assets": assets,
        "summary_net_worth": summary.net_worth,
        "summary_assets": summary.total_assets,
        "proportion_assets": Decimal(str(proportion["total_assets"])),
        "proportion_net_worth": Decimal(str(proportion["net_worth"])),
    }


def test_parent_leaves_measurement_but_stays_visible(db, trio):
    """父 100 ＋ 子 60 ＋ 子 40 → 计量读 100；父项仍在展示读模型里。"""
    parent, child_a, child_b = trio["parent"], trio["child_a"], trio["child_b"]

    # 建构成前：三项独立计入 = 200
    before = _net_worth_exports(db)
    assert before["sum_as_of"] == Decimal("200.00")
    assert before["proportion_assets"] == Decimal("200.00")

    composition_service.create_edge(db, parent.id, child_a.id)
    composition_service.create_edge(db, parent.id, child_b.id)

    after = _net_worth_exports(db)
    # **关键护栏**：父项退出计量 → 100（不是 200）；四个读出一致
    assert after["sum_as_of"] == Decimal("100.00")
    assert after["sum_assets"] == Decimal("100.00")
    assert after["summary_net_worth"] == Decimal("100.00")
    assert after["summary_assets"] == Decimal("100.00")
    assert after["proportion_assets"] == Decimal("100.00")
    assert after["proportion_net_worth"] == Decimal("100.00")

    # 父项的金额**没有被改动**（只退出计量，不删不改；ADR-0029 代价一节）
    row = db.execute(
        select(Asset).where(Asset.asset_item_id == parent.id)
    ).scalars().one()
    assert Decimal(str(row.amount)) == Decimal("100.00")

    # 展示口径不动：资产当前值读模型照常列出父项及其金额（D14）
    current = asset_service.get_current_values(db, as_of=D)
    rows = {r["item_id"]: r for r in current["items"]}
    assert parent.id in rows
    assert rows[parent.id]["amount"] == "100.00"
    assert rows[child_a.id]["amount"] == "60.00"
    # 展示层要知道「这是汇总项」才知道标注什么（判定仍由构成关系派生）
    assert composition_service.is_aggregate(db, parent.id) is True

    # 披露口径随之收敛：合计里不再有父项份额 → 披露只算两个子项
    assert analysis_service.get_summary(db).unclassified_count == 2


def test_removing_composition_restores_measurement(db, trio):
    """解除构成 → 父项自动恢复计量（无开关、无例外标记，D4）。"""
    parent, child_a, child_b = trio["parent"], trio["child_a"], trio["child_b"]
    composition_service.create_edge(db, parent.id, child_a.id)
    composition_service.create_edge(db, parent.id, child_b.id)
    assert analysis_service._sum_as_of(db, D)[0] == Decimal("100.00")

    removed = composition_service.remove_edge(db, child_a.id)
    assert removed.child_item_id == child_a.id
    assert composition_service.is_aggregate(db, parent.id) is True   # 还有一个子项
    # 父项仍未恢复计量（还有子项构成它）：读数仍是两个子项之和 60＋40
    assert analysis_service._sum_as_of(db, D)[0] == Decimal("100.00")

    composition_service.remove_edge(db, child_b.id)
    assert composition_service.is_aggregate(db, parent.id) is False
    # 最后一个子项解除 → 父项自动恢复计量：100＋60＋40
    assert analysis_service._sum_as_of(db, D)[0] == Decimal("200.00")

    # 审计：解除同样留痕（action=remove，before 带双端 id）
    actions = [r.action for r in db.execute(
        select(AuditLog).where(AuditLog.entity == "item_composition")
    ).scalars().all()]
    assert actions.count("remove") == 2
    assert actions.count("create") == 2


def test_remove_missing_edge_raises_not_found(db, trio):
    err = _raise(composition_service.remove_edge, db, trio["child_a"].id)
    assert err.code == ErrorCode.NOT_FOUND and err.status_code == 404


# ---------------------------------------------------------------- 迁移 0037


def _run_script(conn, module) -> str:
    """与 test_audit_contract 同款：单脚本一事务（runner 语义）。"""
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        result = module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _legacy_db(path) -> sqlite3.Connection:
    """0037 之前的形状：只有三张父表（构成表与批次四表都还不存在）。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE asset_items (id INTEGER PRIMARY KEY, display_name TEXT);"
        "CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY);"
        "CREATE TABLE source_groups (id INTEGER PRIMARY KEY);")
    conn.commit()
    return conn


def test_0037_creates_tables_then_noop(tmp_path):
    conn = _legacy_db(str(tmp_path / "legacy_0037.db"))

    first = _run_script(conn, M0037)
    assert first.startswith("created=")
    for table in ("item_compositions", "screenshot_batches",
                  "screenshot_batch_members", "screenshot_reconciliations",
                  "screenshot_reconciliation_children"):
        assert table in first
    # 二跑：零建表、零 DML
    assert _run_script(conn, M0037) == "noop"

    # 零数据回填：五张新表一行都没有
    for table in ("item_compositions", "screenshot_batches",
                  "screenshot_batch_members", "screenshot_reconciliations",
                  "screenshot_reconciliation_children"):
        assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0

    # DB 层兜底：自环被 CHECK 拒、一子一父被 UNIQUE 拒
    conn.execute("INSERT INTO asset_items (id, display_name) VALUES"
                 " (1,'示例-甲'),(2,'示例-乙'),(3,'示例-丙')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO item_compositions (parent_item_id, child_item_id,"
                     " created_at) VALUES (1, 1, '2026-11-09T00:00:00')")
    conn.execute("INSERT INTO item_compositions (parent_item_id, child_item_id,"
                 " created_at) VALUES (2, 3, '2026-11-09T00:00:00')")
    with pytest.raises(sqlite3.IntegrityError):   # 子项 3 已有父 2
        conn.execute("INSERT INTO item_compositions (parent_item_id, child_item_id,"
                     " created_at) VALUES (1, 3, '2026-11-09T00:00:00')")
    conn.close()


def test_0037_is_noop_on_create_all_shape(tmp_path):
    """模型 create_all 建出的形状必须通过迁移校验（启动链是 create_all → 迁移）。

    两套建表路径若形状不同（列序/约束名/索引名/外键），这条会红——生产库上
    「create_all 先建、迁移再校验」就会当场失败。
    """
    path = str(tmp_path / "shape.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    assert _run_script(conn, M0037) == "noop"
    conn.close()


def test_0037_skips_without_asset_items(tmp_path):
    """缺父表（桩库/更老形状）：无处挂外键，显式跳过而不是猜。"""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert _run_script(conn, M0037) == "skipped"
    conn.close()

def test_0037_upgrades_old_shape_in_place(tmp_path):
    """上生产前改过形状的库：就地补 `expected_members` 列 + 扩宽成员状态值域。

    这两个改动都是本期 P1 的（尚未上生产的 0037 被就地改过形状）：老库里
    `screenshot_batches` 没有 `expected_members`、成员表 CHECK 只有
    `('applied','failed')`。SQLite 不能 ALTER CHECK，故成员表走「重建 + 搬行」，
    批次表走 ALTER ADD COLUMN——两者都必须**保留既有行**且二跑 `noop`。
    """
    path = str(tmp_path / "old_0037.db")
    conn = sqlite3.connect(path)
    conn.executescript('''
    CREATE TABLE asset_items (id INTEGER PRIMARY KEY, display_name TEXT);
    CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY);
    CREATE TABLE source_groups (id INTEGER PRIMARY KEY);
    CREATE TABLE screenshot_batches (
      id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
      created_at DATETIME NOT NULL, data_date DATE,
      source_group_id INTEGER REFERENCES source_groups (id),
      status VARCHAR(12) NOT NULL DEFAULT 'open', finalized_at DATETIME,
      note VARCHAR(200),
      CONSTRAINT ck_screenshot_batches_status
        CHECK (status IN ('open','partial','finalized','abandoned')));
    CREATE TABLE screenshot_batch_members (
      id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
      batch_id INTEGER NOT NULL REFERENCES screenshot_batches (id),
      evidence_id INTEGER NOT NULL REFERENCES screenshot_evidence (id),
      applied_at DATETIME NOT NULL, status VARCHAR(12) NOT NULL DEFAULT 'applied',
      CONSTRAINT uq_screenshot_batch_members_batch_evidence UNIQUE (batch_id, evidence_id),
      CONSTRAINT ck_screenshot_batch_members_status
        CHECK (status IN ('applied','failed')));
    CREATE INDEX ix_screenshot_batch_members_evidence
      ON screenshot_batch_members (evidence_id);
    ''')
    conn.execute("INSERT INTO screenshot_batches (id, created_at, status)"
                 " VALUES (1, '2026-11-09T00:00:00', 'open')")
    conn.execute("INSERT INTO screenshot_batch_members"
                 " (batch_id, evidence_id, applied_at, status)"
                 " VALUES (1, 42, '2026-11-09T00:00:00', 'applied')")
    conn.commit()

    first = _run_script(conn, M0037)
    assert "screenshot_batches.expected_members" in first
    assert "screenshot_batch_members(rebuilt)" in first

    # 既有成员行原样保留；状态值域已含 pending（重建生效）
    assert conn.execute("SELECT batch_id, evidence_id, status"
                        " FROM screenshot_batch_members").fetchall() == [
        (1, 42, "applied")]
    conn.execute("INSERT INTO screenshot_batch_members"
                 " (batch_id, evidence_id, applied_at, status)"
                 " VALUES (1, 43, '2026-11-09T00:00:00', 'pending')")
    # 批次表新列可空且读到 NULL（= 未声明，不是 0 张）
    assert conn.execute("SELECT expected_members FROM screenshot_batches"
                        " WHERE id=1").fetchone() == (None,)

    # 二跑：形状已对齐 → noop
    assert _run_script(conn, M0037) == "noop"
    conn.close()
