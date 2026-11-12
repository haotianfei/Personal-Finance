"""归档生效日（0025 / ADR-0017 D2 修订）：归档＝自该日起终止计量，历史值保留。

数据全部合成（虚构机构、示例条目、示例金额），不含真实财务信息。

缺陷形状：初版 D2 把 `status='archived'` 的项从**一切 as-of 日期**剔除，等价于
追溯抹除该项全部历史测量值——把某项标为归档后，归档日之前一整年的净值会塌陷
（实测伪造出 −1.2M 的坑），比 D2 要修的事故更严重。修订后：

- `status='archived'` + `archived_effective_from = D` → 仅 `D` 及之后的 as-of
  日期不再计量，`D` 之前照常计入其真实历史值；
- `status='archived'` + `archived_effective_from IS NULL` → 归档日未知（存量形态）
  → 对一切日期保守剔除（与改前行为一致）；
- 解除归档 → 生效日清回 NULL，该项重新进入聚合。

覆盖：迁移 0025、合并场景（无历史塌陷、无双计）、存量 NULL 形态、解除归档、
`register_lineage` 端到端、占比与预警两条读路径的日期作用域。
"""
import sqlite3
from datetime import date
from decimal import Decimal

from models import Asset, AssetItem
from services import alert_service, analysis_service, asset_service, lineage_service
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.proportion_service import get_proportion_by_asset_name

M0025 = _load_script_module(
    f"{_MIGRATIONS_DIR}/0025_archived_effective_from.py")

D_START = date(2025, 8, 8)      # 合并前一年的首录
D_MID = date(2025, 12, 31)
D_PRE = date(2026, 8, 31)       # 合并前最后一个账期
D_MERGE = date(2026, 9, 16)     # 合并生效日：前身终止计量、后继首录

ZERO = Decimal("0")


def _mk(db, name, amount, on_date, item_id=None):
    return asset_service.create_asset(db, name, Decimal(str(amount)), on_date,
                                      item_id=item_id)


def _trend(db, period_type="day"):
    return [(p.period, p.total_amount) for p in
            analysis_service.get_total_trend(db, period_type)]


# ---------------------------------------------------------------- 0025 迁移


def _stub_conn(tmp_path, *, table=True):
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.isolation_level = None
    if table:
        conn.execute("CREATE TABLE asset_items ("
                     " id INTEGER PRIMARY KEY,"
                     " status TEXT NOT NULL DEFAULT 'active')")
        conn.execute("INSERT INTO asset_items (id, status) VALUES (1, 'archived')")
    return conn


def test_0025_adds_column_nullable_and_idempotent(tmp_path):
    """加列成功、存量行不 backfill（保持 NULL＝归档日未知）、双跑幂等。"""
    conn = _stub_conn(tmp_path)
    assert M0025.upgrade(conn) == "added"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_items)")}
    assert "archived_effective_from" in cols
    row = conn.execute(
        "SELECT status, archived_effective_from FROM asset_items").fetchone()
    assert row == ("archived", None)          # 不猜测日期、不改任何行
    assert M0025.upgrade(conn) == "skipped"   # 双跑：列已存在即跳过
    conn.close()


def test_0025_missing_table_is_noop(tmp_path):
    conn = _stub_conn(tmp_path, table=False)
    assert M0025.upgrade(conn) == "missing"   # 早期最小化库不炸
    conn.close()


# ---------------------------------------------------------------- 合并场景


def _merge_shape(db):
    """合并前：前身 S 有全年历史；合并后：后继 T 于 D_MERGE 首录。"""
    _mk(db, "示例-境外账户-旧", "1000000", D_START)
    _mk(db, "示例-境外账户-旧", "1100000", D_MID)
    _mk(db, "示例-境外账户-旧", "1200000", D_PRE)      # 前身最后一条
    _mk(db, "示例-活期", "100", D_START)               # 无关项（对照）
    _mk(db, "示例-活期", "100", D_MERGE)


def test_merge_keeps_history_before_effective_date_no_crater(db):
    """归档生效日之前的账期不得塌陷；生效日起才终止计量（无 −1.2M 假坑）。"""
    _merge_shape(db)
    old = db.query(AssetItem).filter_by(display_name="示例-境外账户-旧").one()
    old_id = old.id

    before = dict(_trend(db))                          # 合并前序列（仅两个点）

    # 合并：前身自 D_MERGE 起终止计量，后继同日首录
    asset_service.set_item_status(db, old_id, "archived", effective_from=D_MERGE,
                                  reason="normalization")
    new = _mk(db, "示例-境外账户-新", "1200000", D_MERGE)
    assert new.asset_item_id != old_id

    # 历史点逐点不变：归档生效日之前的真实测量值必须保留
    after = dict(_trend(db))
    assert {k: after[k] for k in before} == before
    assert after[D_START.isoformat()] == Decimal("1000100.00")
    assert after[D_MID.isoformat()] == Decimal("1100100.00")
    assert after[D_PRE.isoformat()] == Decimal("1200100.00")   # 不塌陷（旧代码：100.00）

    # 生效日当日：只计后继，前身退出（无「旧值沿用 + 新项」双计）
    assert after[D_MERGE.isoformat()] == Decimal("1200100.00")
    rows = analysis_service._get_latest_records_as_of(db, D_MERGE)
    assert len(rows) == 2                              # 后继 + 活期，前身整支退出
    assert new.asset_item_id in {db.get(Asset, r[2]).asset_item_id for r in rows}

    # 生效日之前一日的 as-of 仍带前身（日期作用域正确，不是整支排除）
    assert analysis_service._sum_as_of(db, D_PRE)[0] == Decimal("1200100.00")
    assert len(analysis_service._get_latest_records_as_of(db, D_PRE)) == 2

    # 漂移诊断：合并前的点保持干净（与既有期望一致，不被归档追溯改写）
    pts = {p.period: p for p in analysis_service.get_total_trend(db, "day")}
    assert pts[D_PRE.isoformat()].drift_suspect is False
    assert pts[D_PRE.isoformat()].vanished == []


def test_archived_without_date_excluded_on_every_date(db):
    """存量形态（archived 且生效日 NULL）＝归档日未知 → 对一切日期保守剔除。"""
    _mk(db, "示例-在持", "1000", D_START)
    gone = _mk(db, "示例-未知归档日", "500", D_START)

    db.get(AssetItem, gone.asset_item_id).status = "archived"   # 直改状态，日期留 NULL
    db.commit()

    for d in (D_START, D_PRE, D_MERGE):
        assert analysis_service._sum_as_of(db, d)[0] == Decimal("1000.00"), d
    assert [r[0] for r in analysis_service._get_latest_records_as_of(db, D_START)] \
        == ["示例-在持"]
    assert get_proportion_by_asset_name(db, D_START)["total_assets"] \
        == Decimal("1000.00")


def test_unarchive_clears_date_and_reenters_aggregation(db):
    """解除归档：生效日清回 NULL，审计留下 before/after 日期，聚合重新计入。"""
    _mk(db, "示例-回退项", "500", D_PRE)
    _mk(db, "示例-回退项", "500", D_MERGE)
    iid = db.query(AssetItem).filter_by(display_name="示例-回退项").one().id

    _, ch = asset_service.set_item_status(db, iid, "archived", effective_from=D_MERGE,
                                  reason="normalization")
    assert ch["action"] == "archive"
    assert ch["after"] == {"status": "archived",
                           "archived_effective_from": D_MERGE}
    assert analysis_service._sum_as_of(db, D_MERGE)[0] == ZERO

    item, ch2 = asset_service.set_item_status(db, iid, "active")
    # ADR-0019：active 要求六维分类齐全；未分类项重新启用后退回 draft
    assert item.status == "draft" and item.archived_effective_from is None
    assert ch2["action"] == "unarchive"
    assert ch2["before"]["archived_effective_from"] == D_MERGE
    assert ch2["after"]["archived_effective_from"] is None
    assert analysis_service._sum_as_of(db, D_MERGE)[0] == Decimal("500.00")


def test_register_lineage_end_to_end_no_crater_no_double_count(db):
    """`register_lineage` 传递人声明的生效日：合并前序列不变、合并后无双计。"""
    _mk(db, "示例-基金-旧", "1000000", D_START)
    _mk(db, "示例-基金-旧", "1200000", D_PRE)
    _mk(db, "示例-活期", "100", D_START)
    _mk(db, "示例-活期", "100", D_MERGE)
    old = db.query(AssetItem).filter_by(display_name="示例-基金-旧").one()

    before = _trend(db)                       # 合并前序列：两个点
    _, links, row = lineage_service.register_lineage(
        db, [old.id], "示例-基金-新", "merge", None, Decimal("1200000"),
        reason="示例合并收购", effective_date=D_MERGE)
    assert links[0].link_type == "merge" and row.effective_date == D_MERGE

    # 归档自同一生效日起算（而非 today）
    db.refresh(old)
    assert old.status == "archived" and old.archived_effective_from == D_MERGE

    # 后继首次录入（人确认值）
    new = _mk(db, "示例-基金-新", "1200000", D_MERGE)
    assert new.asset_item_id != old.id

    after = dict(_trend(db))
    assert {k: after[k] for k in dict(before)} == dict(before)   # 历史一点不变
    assert after[D_PRE.isoformat()] == Decimal("1200100.00")
    assert after[D_MERGE.isoformat()] == Decimal("1200100.00")   # 无双计（旧口径：2400100）
    assert set(dict(before)) <= set(after)


# ---------------------------------------------------------------- 读路径接线


def test_proportion_honours_archive_effective_date(db):
    """占比：生效日之前的账期含该项，生效日起不含（同一谓词、同一日期）。"""
    _mk(db, "示例-在持", "1000", D_PRE)
    _mk(db, "示例-在持", "1200", D_MERGE)
    gone = _mk(db, "示例-终止计量", "500", D_PRE)
    _mk(db, "示例-终止计量", "500", D_MERGE, item_id=gone.asset_item_id)  # 残留行
    asset_service.set_item_status(db, gone.asset_item_id, "archived",
                                  effective_from=D_MERGE,
                                  reason="normalization")

    pre = get_proportion_by_asset_name(db, D_PRE)
    assert pre["total_assets"] == Decimal("1500.00")
    assert {i["name"] for i in pre["asset_items"]} == {"示例-在持", "示例-终止计量"}

    post = get_proportion_by_asset_name(db, D_MERGE)
    assert post["total_assets"] == Decimal("1200.00")
    assert [i["name"] for i in post["asset_items"]] == ["示例-在持"]


def test_alert_honours_archive_effective_date(db):
    """预警：金额侧与明细候选列表都按各账期 max_date 判定（不用 today）。"""
    _mk(db, "示例-在持", "1000", D_PRE)
    _mk(db, "示例-在持", "1200", D_MERGE)
    gone = _mk(db, "示例-终止计量", "500", D_PRE)
    _mk(db, "示例-终止计量", "500", D_MERGE, item_id=gone.asset_item_id)
    asset_service.set_item_status(db, gone.asset_item_id, "archived",
                                  effective_from=D_MERGE,
                                  reason="normalization")

    assert alert_service._compute_amount_for_period(
        db, D_PRE.isoformat(), "asset_name", None) == Decimal("1500.00")
    assert alert_service._compute_amount_for_period(
        db, D_MERGE.isoformat(), "asset_name", None) == Decimal("1200.00")

    # 明细候选：比较期仍在计量 → 名字仍进候选；当期已终止 → 当期金额为 0（不是幽灵沿用）
    details = alert_service._get_detail_changes(
        db, "asset_name", D_MERGE.isoformat(), D_PRE.isoformat(),
        None, None, "both")
    by_name = {d.name: d for d in details}
    assert by_name["示例-终止计量"].current_amount == ZERO
    assert by_name["示例-终止计量"].compare_amount == Decimal("500.00")
    assert by_name["示例-在持"].current_amount == Decimal("1200.00")