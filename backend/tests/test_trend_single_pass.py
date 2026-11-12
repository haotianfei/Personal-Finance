"""趋势取数「单次扫描」vs「逐期 as-of 查询」——等价性与往返次数回归。

`get_total_trend` 改前对**每个账期**调一次 `_get_latest_records_as_of`
（day 粒度 730 期＝730 条相关子查询，耗时随账期线性）；改后一次取全量 active 行，
在内存按账期代表日扫掠（`_latest_records_by_as_of`）。

本文件的参考线是**改前实现的副本**（`reference_trend`，即下方那段逐期循环）。
它只调用既有函数（`_get_latest_records_as_of` / `_aggregate_records` /
`_carry_stats` / `group_dates_by_period` / `get_representative_date`），
**不经过新代码路径**——否则「新旧一起变形」会让对账失去意义。

对账口径：逐点逐字段、`Decimal` 精确相等（金额是定点数，`pytest.approx` 会把
真漂移吃成噪声），字段为 `period` / `date` / `total_amount` / `net_assets` /
`liabilities` / `carried_items` / `carried_max_days`，覆盖 day / week / month /
quarter / year 五档，另加 date_from/date_to 窗口调用一档。

边界用例：

- 归档生效日恰好落在账期代表日 → **生效日当天即退出**（谓词是
  `archived_effective_from <= as_of`，写成 `<` 会把当天算进来、写成 `>` 会少算一天）；
- 归档生效日为 NULL（归档日未知的存量形态）→ 该 as_of 也剔除；
- 某期无任何纳入计量的记录 → 该点全零、沿用事实为 (0, 0)，新旧一致；
- superseded（被更正）行存在 → 永不入 as-of 口径；
- 同身份同日多行（无 item 的存量行不受同日唯一索引约束）→ 整块计入，新旧一致。

数据全部合成（「示例-」前缀、虚构机构/成员、整数金额），不含任何真实财务信息。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, update

from models import Asset, AssetClass, AssetItem, Institution, Member
from services import classification_service
from services.analysis_service import (
    _aggregate_records,
    _carry_stats,
    _get_all_snapshot_dates,
    _get_latest_records_as_of,
    _latest_records_by_as_of,
    get_total_trend,
)
from services.asset_service import create_asset
from services.period_service import get_representative_date, group_dates_by_period

PERIOD_TYPES = ("day", "week", "month", "quarter", "year")

# 对账字段（TrendPoint 上的 7 个口径字段；漂移诊断由未改动的
# `_drift_for_points` 产出，不属本次取数方式的差异面）
COMPARED_FIELDS = (
    "period", "date", "total_amount", "net_assets", "liabilities",
    "carried_items", "carried_max_days",
)

# 快照日（合成）：跨两个自然年、含同月双日、含空月（2026-06 / 08 / 10 无快照）
D0 = date(2025, 12, 31)
D1 = date(2026, 1, 31)
D2 = date(2026, 2, 28)
D3 = date(2026, 3, 10)
D4 = date(2026, 3, 31)      # 归档生效日恰好落在 2026-03 的代表日
D5 = date(2026, 4, 30)
D6 = date(2026, 5, 31)
D7 = date(2026, 7, 15)
D8 = date(2026, 9, 30)
D9 = date(2026, 11, 8)


# ---------------------------------------------------------------- 参考实现（改前副本）


def reference_trend(db, period_type="month", date_from=None, date_to=None):
    """**改前实现的参考副本**：逐期一条 as-of 查询（逐点求和 + 沿用事实）。

    刻意直接调用既有函数、不碰新代码路径——它必须能在**旧实现**上跑出与
    新实现相同的点，才能证明对账断言本身正确。
    """
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    out = []
    for label, group_dates in group_dates_by_period(dates, period_type).items():
        rep_date = get_representative_date(group_dates)
        records = _get_latest_records_as_of(db, rep_date)
        _total, assets, liabilities = _aggregate_records(records)
        carried_items, carried_max_days = _carry_stats(records, rep_date)
        out.append({
            "period": label,
            "date": rep_date,
            "total_amount": assets - liabilities,
            "net_assets": assets,
            "liabilities": liabilities,
            "carried_items": carried_items,
            "carried_max_days": carried_max_days,
        })
    return out


def _as_dict(point):
    return {field: getattr(point, field) for field in COMPARED_FIELDS}


def diff_points(db, period_type, **kw):
    """新旧实现逐点逐字段对账 → (新点, 旧点, 差异清单)。"""
    new = [_as_dict(p) for p in get_total_trend(db, period_type, **kw)]
    old = reference_trend(db, period_type, **kw)
    diffs = []
    if len(new) != len(old):
        diffs.append(("首尾", "point_count", len(new), len(old)))
    for a, b in zip(new, old):
        for field in COMPARED_FIELDS:
            if a[field] != b[field]:
                diffs.append((a["period"], field, a[field], b[field]))
    return new, old, diffs


# ---------------------------------------------------------------- 数据构造


def mk_item(db, name, status="draft"):
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.commit()
    return item


def classify(db, item_id):
    """补齐四个必填维度 → 状态机把 draft 转 active（`_sync_status` 唯一迁移点）。"""
    klass = db.query(AssetClass).filter_by(name="示例类型").first()
    if klass is None:
        klass = AssetClass(code="TP01", name="示例类型", level=1, side_scope="any")
        db.add(klass)
        db.commit()
    inst = db.query(Institution).filter_by(name="示例机构").first()
    if inst is None:
        inst = Institution(name="示例机构", kind="bank")
        db.add(inst)
        db.commit()
    owner = db.query(Member).filter_by(name="示例本人").first()
    if owner is None:
        owner = Member(name="示例本人", role="self")
        db.add(owner)
        db.commit()
    classification_service.set_classification(db, item_id, {
        "asset_class_id": klass.id,
        "liquidity_days": 0,
        "institution_id": inst.id,
        "owner_id": owner.id,
    })
    return db.get(AssetItem, item_id)


def add_row(db, item, amount, on_date, side="asset"):
    return create_asset(db, item.display_name, Decimal(str(amount)), on_date,
                        side=side, item_id=item.id)


def archive(db, item_id, effective_from):
    """归档（ADR-0017 D2）：自 `archived_effective_from` 起终止计量。"""
    item = db.get(AssetItem, item_id)
    item.status = "archived"
    item.archived_effective_from = effective_from
    db.commit()
    return item


@pytest.fixture()
def trend_db(db):
    """合成数据集：普通/负债/归档（生效日＝代表日）/归档（日期未知）/迟录/被更正。"""
    a = classify(db, mk_item(db, "示例-活期A").id)
    add_row(db, a, 1000, D0)
    add_row(db, a, 1050, D1)
    add_row(db, a, 1100, D2)
    add_row(db, a, 1200, D5)                 # D2 → D5 之间多期沿用

    b = classify(db, mk_item(db, "示例-基金B").id)
    add_row(db, b, 500, D0)
    add_row(db, b, 600, D4)
    add_row(db, b, 650, D7)

    liab = classify(db, mk_item(db, "示例-借款L").id)
    add_row(db, liab, 200, D0, side="liability")
    add_row(db, liab, 150, D6, side="liability")

    # 归档项：生效日＝D4（恰好是 2026-03 的代表日）→ D3 仍计量、D4 当天起退出
    c = classify(db, mk_item(db, "示例-归档C").id)
    add_row(db, c, 700, D0)
    add_row(db, c, 750, D3)
    archive(db, c.id, D4)

    # 归档项：生效日未知（NULL）→ 任何 as_of 都不计入
    g = classify(db, mk_item(db, "示例-归档无日期G").id)
    add_row(db, g, 300, D0)
    add_row(db, g, 320, D5)
    archive(db, g.id, None)

    # 迟录项：D8 才首录 → 此前的期不贡献
    e = classify(db, mk_item(db, "示例-迟录E").id)
    add_row(db, e, 40, D8)

    # 被更正项：D6 的 999 行被 supersede（永不入 as-of），D9 才有有效行
    f = classify(db, mk_item(db, "示例-被更正F").id)
    bad = add_row(db, f, 999, D6)
    live = add_row(db, f, 50, D9)
    db.execute(update(Asset).where(Asset.id == bad.id)
               .values(superseded_by=live.id))
    db.commit()

    return {
        "db": db,
        "items": {"a": a.id, "b": b.id, "liability": liab.id,
                  "archived_on_rep": c.id, "archived_unknown": g.id,
                  "late": e.id, "corrected": f.id},
        "superseded_amount": Decimal("999"),
    }


# ---------------------------------------------------------------- 1. 五档粒度全量对账


@pytest.mark.parametrize("period_type", PERIOD_TYPES)
def test_single_pass_equals_reference_all_granularities(trend_db, period_type):
    """day / week / month / quarter / year 五档：逐点逐字段相等，差异 0 行。"""
    new, old, diffs = diff_points(trend_db["db"], period_type)

    assert diffs == []
    assert len(new) == len(old) > 1, f"{period_type}: 数据集应产出多个点"
    # 期数覆盖自检：五档分别对应不同的分组规模
    assert {p["period"] for p in new} == {p["period"] for p in old}


@pytest.mark.parametrize("period_type", PERIOD_TYPES)
def test_single_pass_equals_reference_windowed(trend_db, period_type):
    """带 date_from/date_to 的窗口调用同样逐点相等（取数范围变了，口径不变）。"""
    _new, _old, diffs = diff_points(
        trend_db["db"], period_type, date_from=D2, date_to=D8)
    assert diffs == []


def test_equivalence_covers_expected_period_counts(trend_db):
    """对账覆盖度自检：五档各自的期数（防止某档退化成空序列后「全都相等」）。"""
    db = trend_db["db"]
    counts = {pt: len(get_total_trend(db, pt)) for pt in PERIOD_TYPES}
    assert counts == {"day": 10, "week": 10, "month": 9,
                      "quarter": 5, "year": 2}, counts


# ---------------------------------------------------------------- 2. 记录级等价（更严格的形状对账）


def _norm(rows):
    """记录多重集规范化：同形状五元组、稳定排序（Decimal 精确比较）。"""
    return sorted(
        (tuple(row) for row in rows),
        key=lambda r: (r[0], str(r[1]), r[2] if r[2] is not None else -1,
                       str(r[3]), str(r[4])),
    )


def test_records_by_as_of_equal_per_period_query(trend_db):
    """批量扫掠的**记录集合**必须与逐期查询逐元组相等（形状可直接喂给聚合函数）。"""
    db = trend_db["db"]
    dates = _get_all_snapshot_dates(db)
    reps = {get_representative_date(group_dates)
            for pt in PERIOD_TYPES
            for group_dates in group_dates_by_period(dates, pt).values()}

    batch = _latest_records_by_as_of(db, reps)

    assert set(batch) == reps
    for rep in sorted(reps):
        assert _norm(batch[rep]) == _norm(_get_latest_records_as_of(db, rep)), rep
    for records in batch.values():
        for name, amount, asset_id, side, asset_date in records:
            assert isinstance(amount, Decimal)      # 与逐期查询同为 Numeric → Decimal
            assert side in ("asset", "liability")
            assert isinstance(asset_date, date)


# ---------------------------------------------------------------- 3. 边界：归档生效日


def test_archive_boundary_exits_on_effective_date(trend_db):
    """归档生效日**当天即退出**：D3（生效日前）计入，D4（生效日）起剔除。"""
    db = trend_db["db"]
    batch = _latest_records_by_as_of(db, [D3, D4])

    names_before = {r[0] for r in batch[D3]}
    names_on = {r[0] for r in batch[D4]}
    assert "示例-归档C" in names_before              # 生效日之前：真实历史值照常计入
    assert "示例-归档C" not in names_on              # 生效日当天：已退出（不是次日）
    assert any(r[0] == "示例-归档C" and r[1] == Decimal("750")
               for r in batch[D3])

    # 逐期对比：D4 正是 2026-03 的代表日
    pts = {p.date: p for p in get_total_trend(db, "month")}
    assert pts[D4].net_assets == (Decimal("1100") + Decimal("600"))  # A 沿用 + B 当日
    assert D3 not in pts                             # D3 只是组内日期，不是代表日
    day_pts = {p.date: p for p in get_total_trend(db, "day")}
    # D3：A 沿用 D2 的 1100、B 沿用 D0 的 500、归档项 C 当日行 750
    assert day_pts[D3].net_assets == (
        Decimal("1100") + Decimal("500") + Decimal("750"))
    assert day_pts[D4].net_assets == Decimal("1700")
    # 归档项退出后不再计入沿用事实（A 沿用 D2、L 沿用 D0）
    assert day_pts[D4].carried_items == 2
    assert day_pts[D4].carried_max_days == 90        # 2025-12-31 → 2026-03-31


def test_archived_with_null_effective_date_excluded_everywhere(trend_db):
    """归档生效日为 NULL＝日期未知 → 该 as_of 也剔除（与 SQL 谓词同语义）。

    对照口径：该项的额定值是 D0 起 300、D5 起 320；谓词若写成「NULL → 一直计量」，
    这份金额会立刻出现在下面的合计里。
    """
    db = trend_db["db"]
    batch = _latest_records_by_as_of(db, [D0, D5, D9])
    for as_of, records in batch.items():
        assert all(r[0] != "示例-归档无日期G" for r in records), as_of

    # D0：1000（A）+ 500（B）+ 700（C，生效日前照常计入）－ 200（L）；不含 G 的 300
    day_points = {p.date: p for p in get_total_trend(db, "day")}
    assert day_points[D0].net_assets == Decimal("2200")
    assert day_points[D0].liabilities == Decimal("200")


def test_period_without_any_includable_record(db):
    """所有项在该期都已归档 → 该点全零、沿用事实 (0, 0)，新旧一致。"""
    g = classify(db, mk_item(db, "示例-唯一项").id)
    add_row(db, g, 100, D1)
    add_row(db, g, 120, D2)                  # 让 D2 成为快照日
    archive(db, g.id, D2)                    # 生效日＝D2 → D2 当天起退出

    new, old, diffs = diff_points(db, "day")
    assert diffs == []
    by_date = {p["date"]: p for p in new}
    assert by_date[D1]["net_assets"] == Decimal("100")
    assert by_date[D2]["net_assets"] == Decimal("0")     # 无任何纳入计量的记录
    assert by_date[D2]["total_amount"] == Decimal("0")
    assert by_date[D2]["liabilities"] == Decimal("0")
    assert (by_date[D2]["carried_items"], by_date[D2]["carried_max_days"]) == (0, 0)


# ---------------------------------------------------------------- 4. 边界：superseded / 同日多行


def test_superseded_rows_never_counted(trend_db):
    """被更正行（superseded_by 非空）不入 as-of 口径；改动前后一致。"""
    db = trend_db["db"]
    bad_amount = trend_db["superseded_amount"]

    batch = _latest_records_by_as_of(db, [D6, D9])
    assert all(r[1] != bad_amount for r in batch[D6])
    assert all(r[1] != bad_amount for r in batch[D9])

    new, old, diffs = diff_points(db, "day")
    assert diffs == []
    # D6 的合计：A 沿用 D5 的 1200 ＋ B 沿用 D4 的 600 － 借款 L 当日 150
    # （被更正的 999 若入账会变成 2799 / 2649）
    by_date = {p["date"]: p for p in new}
    assert by_date[D6]["net_assets"] == Decimal("1800")
    assert by_date[D6]["total_amount"] == Decimal("1650")


def test_same_identity_same_date_rows_counted_as_block(db):
    """同身份同日多行（无 item 的存量行）整块计入——同 SQL 联接语义。"""
    for amount in ("70", "30"):
        db.add(Asset(asset_name="示例-存量同名", amount=Decimal(amount),
                     asset_date=D1, side="asset", asset_item_id=None))
    db.commit()

    new, old, diffs = diff_points(db, "day")
    assert diffs == []
    records = _latest_records_by_as_of(db, [D1])[D1]
    same = [r for r in records if r[0] == "示例-存量同名"]
    assert len(same) == 2                                # 两行都在，不被折叠成一行
    assert sum((r[1] for r in same), Decimal("0")) == Decimal("100")
    assert {p["date"]: p for p in new}[D1]["net_assets"] == Decimal("100")


# ---------------------------------------------------------------- 5. 往返次数：不再随账期线性


def _counted(db, fn):
    """执行 fn，返回 (结果, 期间发出的 SQL 条数)。"""
    engine = db.get_bind()
    seen = []

    def _listen(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _listen)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _listen)
    return result, seen


def test_query_count_decoupled_from_period_count(trend_db):
    """D3 的契约：SQL 条数与账期数解耦（改前＝1 + 期数，改后恒定 3 条）。"""
    db = trend_db["db"]
    n_periods = len(get_total_trend(db, "day"))

    _new, new_stmts = _counted(db, lambda: get_total_trend(db, "day"))
    _old, old_stmts = _counted(db, lambda: reference_trend(db, "day"))

    assert len(new_stmts) <= 3, [s.split("FROM")[0] for s in new_stmts]
    assert len(old_stmts) == n_periods + 1        # 快照日 1 条 + 每期 1 条
    assert len(new_stmts) < len(old_stmts)