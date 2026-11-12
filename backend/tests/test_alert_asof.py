"""预警 as-of 口径与外部分叉防护（issue 06；spec §1.1、ADR-0017 D2/D3）。

预警阈值过去在 `alert_service` 里各持一份 SQL：日期列表不过滤 superseded 行、
as-of 求和不过滤归档项。于是归档项退出净值后预警仍按旧值报警（「趋势说 300 万、
预警说 290 万」）。本文件把三条口径钉住：

1. 归档项的有值残留不参与预警金额、也不出现在明细输出里；
2. 只有 superseded 行的日期不产生预警评估点（账期列表与净值 x 轴同口径）；
3. 机械防护：同一份种子库上，预警侧 as-of 合计 == `analysis_service._sum_as_of`，
   任何一方单独改口径都会红；记录页的日期列表（`asset_service.get_snapshot_dates`）
   同口径——只有 superseded 行的日期不出现。

数据全部合成（虚构名称、示例金额），不含真实财务信息。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError
from models import (
    AlertRule, Asset, AssetClass, AssetItem, AssetItemTag, Institution, Member, Tag,
)
from schemas import AlertRuleCreate
from services import alert_service, analysis_service, asset_service
from services.asset_service import create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)


def _mk(db, name, amount, on_date, item_id=None):
    return create_asset(db, name, Decimal(str(amount)), on_date, item_id=item_id)


def _archive(db, item_id):
    """R-37：人裁决归档（状态机 active→archived，此处直接置状态）。"""
    db.get(AssetItem, item_id).status = "archived"
    db.commit()


def _mk_rule(db, **overrides):
    kwargs = dict(name="示例预警规则", dimension="asset_name", target_id=None,
                  period_type="month", compare_type="previous",
                  amount_threshold=Decimal("100"), percent_threshold=None,
                  direction="up", is_active=True)
    kwargs.update(overrides)
    rule = AlertRule(**kwargs)
    db.add(rule)
    db.commit()
    return rule


# ---------------- 1. 归档项退出预警口径 ----------------
def test_archived_item_residual_excluded_from_alert_amount_and_details(db):
    """归档项即使留着有值行，也不得进入预警金额或明细（ADR-0017 D2）。"""
    _mk(db, "示例-在持", 1000, D_JAN)
    _mk(db, "示例-在持", 1000, D_FEB)
    gone = _mk(db, "示例-已归档", 500, D_JAN)
    _mk(db, "示例-已归档", 900, D_FEB, item_id=gone.asset_item_id)
    _archive(db, gone.asset_item_id)

    rule = _mk_rule(db)
    res = alert_service.check_alert(db, rule.id, "2026-02")

    # 若归档项仍参与：current=1900 / compare=1500 / change=+400 → 会误触发上限告警
    assert res.current_amount == Decimal("1000.00")
    assert res.compare_amount == Decimal("1000.00")
    assert res.change_amount == Decimal("0")
    assert res.triggered is False

    # 明细输出（派生读）里也不得出现已归档身份
    assert [d.name for d in res.details] == ["示例-在持"]


# ---------------- 2. 只有 superseded 行的日期不产生评估点 ----------------
def test_superseded_only_date_is_not_an_alert_evaluation_point(db):
    live = _mk(db, "示例-在持", 100, date(2026, 3, 31))
    # 该日期只留一条被更正（superseded）的行 → 不得成为账期/评估点
    db.add(Asset(asset_name="示例-旧读数", amount=Decimal("999"),
                 asset_date=date(2026, 3, 10), side="asset",
                 superseded_by=live.id))
    db.commit()

    assert alert_service._get_all_snapshot_dates(db) == [date(2026, 3, 31)]
    assert [p["label"] for p in alert_service.get_available_periods(db, "day")] \
        == ["2026-03-31"]

    rule = _mk_rule(db, period_type="day")
    res = alert_service.check_alert(db, rule.id)          # 默认取最后一个账期
    assert res.current_period == "2026-03-31"


# ---------------- 3. 机械防护：预警侧合计 == 净值侧 as-of 合计 ----------------
def test_alert_asof_total_matches_analysis_sum_as_of(db):
    """同一份种子库、同一日期：两读端口径必须逐点相等（防再次分叉）。

    预警侧的 `_compute_amount_for_period` 是「该日 active 行求和」，分析侧
    `_sum_as_of` 是「各身份最近值之和」；在全覆盖的日期上二者恒等——这正是
    本测试种子的形态（每项在该日都有行）。归档项 G 是分歧探针：任何一侧漏掉
    `active_item_predicate()` 都会让等式破裂。
    """
    d1, d2 = date(2026, 9, 30), date(2026, 10, 31)
    _mk(db, "示例-甲", 1000, d1)
    _mk(db, "示例-甲", 1200, d2)
    _mk(db, "示例-乙", 300, d1)
    _mk(db, "示例-乙", 300, d2)
    gone = _mk(db, "示例-已归档", 500, d1)
    _mk(db, "示例-已归档", 500, d2, item_id=gone.asset_item_id)
    _archive(db, gone.asset_item_id)

    # 只有 superseded 行的日期也不得进入记录页日期选择器（get_snapshot_dates 同口径）
    live = db.execute(select(Asset).where(Asset.asset_date == d2)).scalars().first()
    db.add(Asset(asset_name="示例-旧读数", amount=Decimal("999"),
                 asset_date=date(2026, 10, 15), side="asset",
                 superseded_by=live.id))
    db.commit()
    assert date(2026, 10, 15) not in asset_service.get_snapshot_dates(db)

    # 账期列表本身同口径
    assert alert_service._get_all_snapshot_dates(db) == \
        analysis_service._get_all_snapshot_dates(db)

    for d in (d1, d2):
        alert_total = alert_service._compute_amount_for_period(
            db, d.isoformat(), "asset_name", None)
        _net, assets, _liab = analysis_service._sum_as_of(db, d)
        assert alert_total == assets, f"{d}: 预警口径与净值口径分叉"

    assert alert_service._compute_amount_for_period(
        db, d2.isoformat(), "asset_name", None) == Decimal("1500.00")

# ---------------- 4. ADR-0019：维度口径（项级分类） ----------------
def _mk_classified(db, name, amount, on_date, *, klass, institution, owner,
                   days=0, tag=None, risk_level=None):
    """建一个已分类的资产项＋一条记录（分类在项上，记录只承载金额/日期）。"""
    item = AssetItem(display_name=name, status="active",
                     asset_class_id=klass.id, liquidity_days=days,
                     institution_id=institution.id, owner_id=owner.id,
                     risk_level=risk_level)
    db.add(item)
    db.flush()
    if tag is not None:
        db.add(AssetItemTag(item_id=item.id, tag_id=tag.id))
    db.commit()
    create_asset(db, name, Decimal(str(amount)), on_date, item_id=item.id)
    return item


def _mk_dictionaries(db):
    parent = AssetClass(code="A", name="现金与等价物", parent_id=None,
                        level=1, side_scope="asset")
    db.add(parent)
    db.flush()
    demand = AssetClass(code="A1", name="活期存款", parent_id=parent.id,
                        level=2, side_scope="asset")
    fund = AssetClass(code="A2", name="货币基金", parent_id=parent.id,
                      level=2, side_scope="asset")
    bank = Institution(name="示例银行", kind="bank")
    broker = Institution(name="示例券商", kind="broker")
    owner = Member(name="本人", role="self")
    tag = Tag(name="示例标签", kind="controlled")
    db.add_all([demand, fund, bank, broker, owner, tag])
    db.commit()
    return {"parent": parent, "demand": demand, "fund": fund,
            "bank": bank, "broker": broker, "owner": owner, "tag": tag}


def test_rule_write_rejects_legacy_and_unknown_dimensions(db):
    """旧维度名在写路径 422 并提示新名（不存在「静默兼容」这条路）。"""
    cases = {"fund_type": "asset_class", "liquidity_rating": "liquidity",
             "account": "institution"}
    for legacy, replacement in cases.items():
        with pytest.raises(ApiError) as ei:
            alert_service.create_alert_rule(db, AlertRuleCreate(
                name=f"规则-{legacy}", dimension=legacy, target_id=None,
                period_type="month", compare_type="previous"))
        assert ei.value.status_code == 422
        assert replacement in ei.value.message

    with pytest.raises(ApiError) as ei2:
        alert_service.create_alert_rule(db, AlertRuleCreate(
            name="规则-未知", dimension="nope", target_id=None,
            period_type="month", compare_type="previous"))
    assert ei2.value.status_code == 422


def test_legacy_rule_marked_invalid_on_read_not_deleted(db):
    """存量旧维度规则：读时标 invalid（triggered=False ＋ 说明），不自动删除。"""
    _mk(db, "示例-在持", 1000, D_JAN)
    legacy = _mk_rule(db, dimension="fund_type", target_id="1")

    res = alert_service.check_alert(db, legacy.id, "2026-01")
    assert res.triggered is False
    assert res.current_amount == Decimal("0")      # 不按「全部」求和伪造异动
    assert res.compare_amount == Decimal("0")
    assert "asset_class" in res.message

    # 规则仍在库里（删掉＝替用户放弃一条他配过的告警）
    assert alert_service.get_alert_rule(db, legacy.id) is not None
    invalid = alert_service.invalid_rules(db)
    assert [r["rule_id"] for r in invalid] == [legacy.id]
    assert "asset_class" in invalid[0]["message"]

    # 结果列表里也带同一条说明（首页/信号面按 invalid 展示）
    results = alert_service.get_alert_results(db, "2026-01")
    assert [r.rule_id for r in results] == [legacy.id]
    assert results[0].triggered is False and results[0].message


def test_item_dimension_amounts_and_detail_rows(db):
    """项级维度取数：类别含后代、流动性按 days、机构/标签/归属命中项级绑定。"""
    ids = _mk_dictionaries(db)
    _mk_classified(db, "示例-活期", 600, D_JAN, klass=ids["demand"],
                   institution=ids["bank"], owner=ids["owner"], days=0,
                   tag=ids["tag"])
    _mk_classified(db, "示例-货基", 300, D_JAN, klass=ids["fund"],
                   institution=ids["broker"], owner=ids["owner"], days=1,
                   risk_level="R1")
    # 第二期：货基 +100（用于明细行对比）
    _mk_classified(db, "示例-货基", 400, D_FEB, klass=ids["fund"],
                   institution=ids["broker"], owner=ids["owner"], days=1)

    def amount(dimension, target_id):
        return alert_service._compute_amount_for_period(
            db, D_JAN.isoformat(), dimension, target_id)

    assert amount("asset_class", str(ids["parent"].id)) == Decimal("900")   # 含后代
    assert amount("asset_class", str(ids["demand"].id)) == Decimal("600")
    assert amount("liquidity", "0") == Decimal("600")
    assert amount("liquidity", "1") == Decimal("300")
    assert amount("institution", str(ids["bank"].id)) == Decimal("600")
    assert amount("owner", str(ids["owner"].id)) == Decimal("900")
    assert amount("tag", str(ids["tag"].id)) == Decimal("600")
    # 风险等级：主名与同义词必须同值。旧拼写 `risk` 曾漏加 join（条件落在
    # asset_items 上却是 `assets, asset_items` 交叉积），把「R1 那一项」放大成
    # 全部资产之和（活期 600 ＋ 货基 300 ＝ 900）——issue 01。
    assert amount("risk_level", "R1") == Decimal("300")
    assert amount("risk", "R1") == Decimal("300")
    assert amount("risk", "R5") == Decimal("0")        # 未评级/空档不命中
    # 旧维度（且带 target_id）不得退化成「全部求和」
    assert amount("fund_type", str(ids["parent"].id)) == Decimal("0")

    # 明细行：维度取值来自字典/枚举，不发明新取值
    rule = alert_service.create_alert_rule(db, AlertRuleCreate(
        name="机构异动", dimension="institution", target_id=None,
        period_type="month", compare_type="previous",
        amount_threshold=Decimal("10"), direction="up"))
    detail = alert_service.check_alert(db, rule.id, "2026-02")
    assert detail.compare_period == "2026-01"
    rows = {d.id: d for d in detail.details}
    # 预警取数是「该账期当日 active 行」，不是 as-of 结转：活期在 2 月无行 → 0
    assert rows[str(ids["bank"].id)].change_amount == Decimal("-600.00")
    assert rows[str(ids["broker"].id)].change_amount == Decimal("100.00")
    assert rows[str(ids["broker"].id)].triggered is True     # 方向 up 且 ≥ 门槛 10
    assert detail.details[0].id == str(ids["bank"].id)       # 按 |变化| 降序
