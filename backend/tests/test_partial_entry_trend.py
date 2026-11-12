"""趋势点的**沿用事实**（`carried_items` / `carried_max_days`）— ADR-0019 D12 改判后。

旧文件测的是「覆盖度」（`updated_accounts` / `expected_accounts` / `partial`），分母是
`attributes(type='account').remind_enabled`——该表随 `0027` 迁移整表删除，而账本已确定为
**变更日志**语义：「这一天这个项没有记录」＝值没变，不是「没核对」。分母消失后覆盖率
不再有意义，故整文件重写：不再判「部分录入」，只把可核对的事实原样报出。

口径（定义见 `analysis_service._carry_stats`）：

- `carried_items`＝该日**计入**的项里，当前值来自更早估值日的项数；
- `carried_max_days`＝这些沿用项中最久的一个已沿用多少天（无沿用 → 0）；
- 归档项按既有归档谓词（`active_item_predicate`）排除，不参与沿用天数统计。

数据全部合成（虚构机构/类别/条目/金额），不含真实财务信息。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from models import AssetClass, AssetItem, Institution, Member
from services import classification_service
from services.analysis_service import (
    get_mixed_chart,
    get_period_comparison,
    get_summary,
    get_total_trend,
    get_trend_by_institution,
    get_trend_by_item,
    get_trend_by_type,
)
from services.asset_service import create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)
D_MAR10 = date(2026, 3, 10)
D_MAR = date(2026, 3, 31)
D_APR = date(2026, 4, 30)


# ---------------------------------------------------------------- fixtures
# ADR-0019：分类在**资产项**上（六列＋字典表），记录只承载金额/日期/side。
# 旧 fixture 的 `Attribute(type='account'|'fund_type')` + `create_asset(attribute_ids=…)`
# 已废弃（记录侧写入路径直接 422），这里改为字典行 + 项级分类。


def mk_institution(db, name="示例银行"):
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind="bank")
        db.add(row)
        db.commit()
    return row


def mk_class(db, name="示例货币基金"):
    """叶子类别（`side_scope='any'`：资产/负债两侧都能挂）。"""
    row = db.execute(
        select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if row is None:
        row = AssetClass(code=f"T{abs(hash(name)) % 10**6}", name=name, level=1,
                         side_scope="any")
        db.add(row)
        db.commit()
    return row


def mk_member(db, name="本人"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role="self")
        db.add(row)
        db.commit()
    return row


def mk_asset(db, name, amount, on_date, *, klass=None, institution=None,
             side="asset"):
    """建一条记录，并在其资产项上补全四维（项补齐即转 active）。"""
    row = create_asset(db, name, Decimal(str(amount)), on_date, side=side)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": (klass or mk_class(db)).id,
        "liquidity_days": 0,
        "institution_id": (institution or mk_institution(db)).id,
        "owner_id": mk_member(db).id,
    })
    return row


def archive(db, item_id, effective_from):
    """归档（ADR-0017：自 `archived_effective_from` 起终止计量）。"""
    item = db.get(AssetItem, item_id)
    item.status = "archived"
    item.archived_effective_from = effective_from
    db.commit()
    return item


def points_by_period(db, **kw):
    return {p.period: p for p in get_total_trend(db, "month", **kw)}


# ---------------- 1. 沿用事实：项数 + 最久天数（含「无断崖」回归） ----------------
def test_carried_items_and_max_days(db):
    mk_asset(db, "甲-活期", 1000, D_JAN)
    mk_asset(db, "乙-基金", 500, D_JAN)
    mk_asset(db, "甲-活期", 1100, D_FEB)
    mk_asset(db, "乙-基金", 600, D_FEB)
    # 3 月只有甲录入：乙沿用 2 月的 600（不是断崖到 1200）
    mk_asset(db, "甲-活期", 1200, D_MAR)

    pts = points_by_period(db)
    assert pts["2026-01"].carried_items == 0          # 首录当日：无沿用
    assert pts["2026-01"].carried_max_days == 0
    assert pts["2026-02"].carried_items == 0          # 两项当日都有行
    assert pts["2026-03"].carried_items == 1          # 乙沿用
    assert pts["2026-03"].carried_max_days == 31      # 2/28 → 3/31

    mar = pts["2026-03"]
    assert mar.date == D_MAR
    assert mar.net_assets == Decimal("1800")          # 1200 + 沿用 600
    assert mar.total_amount == Decimal("1800")        # 沿用不制造断崖
    assert mar.liabilities == Decimal("0")


# ---------------- 2. 沿用天数随期递增（不判对错，只报事实） ----------------
def test_carried_days_grow_across_periods(db):
    mk_asset(db, "甲-长尾", 300, D_JAN)               # 只 1 月有行
    for d in (D_FEB, D_MAR, D_APR):
        mk_asset(db, "乙-活期", 100, d)               # 每期都有行（保持快照日存在）

    pts = points_by_period(db)
    assert pts["2026-01"].carried_max_days == 0
    assert pts["2026-02"].carried_max_days == 28      # 1/31 → 2/28
    assert pts["2026-03"].carried_max_days == 59      # 1/31 → 3/31
    assert pts["2026-04"].carried_max_days == 89      # 1/31 → 4/30
    # 每期都是「1 项沿用」，且金额一直是 300＋100（沿用值照常计入）
    assert [pts[k].carried_items for k in
            ("2026-02", "2026-03", "2026-04")] == [1, 1, 1]
    assert pts["2026-04"].net_assets == Decimal("400")


# ---------------- 3. 归档项不计入沿用（归档谓词与求和同源） ----------------
def test_archived_item_not_counted_as_carried(db):
    mk_asset(db, "甲-旧项", 300, D_JAN)               # 归档前最后一条在 1 月
    mk_asset(db, "乙-活期", 100, D_JAN)
    mk_asset(db, "乙-活期", 100, D_FEB)
    mk_asset(db, "乙-活期", 100, D_MAR)

    feb = points_by_period(db)["2026-02"]
    assert feb.carried_items == 1                     # 归档前：甲-旧项算沿用
    assert feb.carried_max_days == 28

    item = db.execute(select(AssetItem).where(
        AssetItem.display_name == "甲-旧项")).scalar_one()
    archive(db, item.id, D_FEB)

    # 归档生效日及之后：该项退出计量 → 既不计入沿用项数，也不再计入金额
    mar = points_by_period(db)["2026-03"]
    assert mar.carried_items == 0
    assert mar.carried_max_days == 0
    assert mar.net_assets == Decimal("100")           # 只剩乙-活期

    feb_after = points_by_period(db)["2026-02"]
    assert feb_after.carried_items == 0               # 生效日当日即退出
    assert feb_after.net_assets == Decimal("100")


# ---------------- 4. 负债沿用：计入 carried，且仍按 side 拆分 ----------------
def test_liability_carried_counts_and_splits(db):
    mk_asset(db, "甲-活期", 1000, D_JAN)
    mk_asset(db, "甲-借款", 200, D_JAN, side="liability")
    mk_asset(db, "甲-活期", 1000, D_FEB)              # 2 月未更新负债

    feb = points_by_period(db)["2026-02"]
    assert feb.carried_items == 1                     # 负债沿用也计入项数
    assert feb.carried_max_days == 28
    assert feb.net_assets == Decimal("1000")          # 资产侧
    assert feb.liabilities == Decimal("200")          # 沿用负债
    assert feb.total_amount == Decimal("800")         # 1000 - 200


# ---------------- 5. 同月多日期：沿用按代表日（组内最大日期）锚定 ----------------
def test_same_month_multiple_dates_anchor_on_rep(db):
    mk_asset(db, "甲-活期", 1000, D_FEB)
    mk_asset(db, "乙-基金", 500, D_FEB)
    mk_asset(db, "甲-活期", 1050, D_MAR10)
    mk_asset(db, "甲-活期", 1100, D_MAR)

    mar = points_by_period(db)["2026-03"]
    assert mar.date == D_MAR                          # 代表日 = 组内最大日期
    assert mar.net_assets == Decimal("1600")           # 1100（非 1050+1100）+ 沿用 500
    assert mar.carried_items == 1                     # 乙停在 2/28
    assert mar.carried_max_days == 31                 # 2/28 → 3/31（不是 3/10 起算）


# ---------------- 6. 首录前的期不贡献、也不算沿用 ----------------
def test_first_entry_contribution_and_no_carry_before(db):
    mk_asset(db, "甲-活期", 100, D_JAN)
    mk_asset(db, "甲-活期", 100, D_FEB)
    mk_asset(db, "甲-活期", 100, D_MAR)
    mk_asset(db, "乙-迟录", 40, D_APR)                # 4 月才首录

    pts = points_by_period(db)
    # 首录前各期：该项根本不出现（不是按 0 计入），因此不产生沿用项
    assert [pts[k].carried_items for k in
            ("2026-01", "2026-02", "2026-03")] == [0, 0, 0]
    assert pts["2026-03"].net_assets == Decimal("100")
    assert pts["2026-04"].net_assets == Decimal("140")  # 迟录首录才贡献
    assert pts["2026-04"].carried_items == 1            # 甲-活期沿用 3/31 → 4/30
    assert pts["2026-04"].carried_max_days == 30

    by_item = {p.period: p for p in get_trend_by_item(db, "甲-活期", "month")}
    assert [by_item[k].amount for k in
            ("2026-01", "2026-02", "2026-03", "2026-04")] == [Decimal("100")] * 4


# ---------------- 7. 环比点与趋势点同源（沿用事实原样透传） ----------------
def test_comparison_points_mirror_carried_facts(db):
    mk_asset(db, "甲-活期", 1000, D_JAN)
    mk_asset(db, "乙-基金", 500, D_JAN)
    mk_asset(db, "甲-活期", 1100, D_FEB)              # 2 月乙沿用

    trend = points_by_period(db)
    cmp_pts = {p.period: p for p in get_period_comparison(db, "month")}
    assert (cmp_pts["2026-02"].carried_items,
            cmp_pts["2026-02"].carried_max_days) == (
        trend["2026-02"].carried_items, trend["2026-02"].carried_max_days) == (1, 28)
    assert cmp_pts["2026-01"].carried_items == 0
    assert cmp_pts["2026-02"].change_amount == Decimal("100")   # 1600 - 1500
    # 图表同源：mixed-chart 的 trend/comparison 一次聚合产出
    chart = get_mixed_chart(db, "month")
    assert [(t.carried_items, t.carried_max_days) for t in chart.trend] == \
           [(c.carried_items, c.carried_max_days) for c in chart.comparison]


# ---------------- 8. 「部分录入」字段已整体删除（契约回归） ----------------
def test_partial_fields_removed_from_contract(db):
    from schemas import ComparisonPoint, SummaryData, TrendPoint

    for model in (TrendPoint, ComparisonPoint):
        assert "partial" not in model.model_fields
        assert "updated_accounts" not in model.model_fields
        assert "expected_accounts" not in model.model_fields
        assert {"carried_items", "carried_max_days"} <= set(model.model_fields)
    for gone in ("updated_accounts", "expected_accounts", "partial"):
        assert gone not in SummaryData.model_fields

    mk_asset(db, "甲-活期", 1000, D_JAN)
    mk_asset(db, "乙-基金", 700, D_JAN)
    mk_asset(db, "甲-活期", 1200, D_FEB)              # 最新期仅甲录入
    s = get_summary(db)
    assert s.latest_date == D_FEB
    assert s.net_worth == Decimal("1900")             # 1200 + 沿用 700
    assert s.change_amount == Decimal("200")          # 1900 - 1700
    assert s.record_count == 1                        # 当日实际写入行数不变


# ---------------- 9. 分类维度趋势沿用（类别含后代 / 机构） ----------------
def test_dimension_trends_carry(db):
    fund = mk_class(db, "示例权益基金")
    broker = mk_institution(db, "示例证券")
    bank = mk_institution(db, "示例银行")
    mk_asset(db, "示例证券-基金", 500, D_JAN, klass=fund, institution=broker)
    mk_asset(db, "示例证券-基金", 600, D_FEB, klass=fund, institution=broker)
    mk_asset(db, "示例银行-存款", 1000, D_FEB, institution=bank)
    mk_asset(db, "示例银行-存款", 1100, D_MAR, institution=bank)  # 3 月基金未更新

    by_class = {p.period: p for p in get_trend_by_type(db, fund.id, "month")}
    assert by_class["2026-03"].amount == Decimal("600")            # 沿用

    by_inst = {p.period: p for p in get_trend_by_institution(
        db, broker.id, "month")}
    assert by_inst["2026-03"].amount == Decimal("600")             # 沿用
    assert by_inst["2026-02"].amount == Decimal("600")