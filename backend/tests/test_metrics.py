"""三期-0「指标骨架」后端测试（spec §2/§3/§5）。

覆盖：注册表自检／灯引擎真值表／契约字段与数值精度／口径闸门映射（NO_DATA、
RATE_UNAVAILABLE、PRINCIPAL_NOT_SET、TWR_LARGE_FLOW_APPROX）／
as_of（默认/指定/越界 422）／配置偏离阈值行为／与现有分析端点对账。
数据一律合成（「示例-」，虚构类型/账户/金额）。

> 覆盖度（`data_coverage`）及其「部分录入」判据已于 ADR-0019 D12 整体下线：
> 账本是变更日志，「该日没有某项的记录」＝值没变，覆盖率分母（旧
> `attributes.remind_enabled`）已随 0026 迁移消失。相关用例随指标一起删除。
"""
import json
import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import AllocationTarget, AssetClass, Institution, Member
from routers.asset_items import PrincipalRequest, set_principal as route_set_principal
from services import (
    allocation_service, analysis_service, appreciation_service, classification_service,
    metric_service,
)
from services.asset_service import create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)
D_MAR = date(2026, 3, 31)
RATE_Q = Decimal("0.0001")
MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")
RATE_RE = re.compile(r"^-?\d+\.\d{4}$")


# ---------------------------------------------------------------- fixtures
# ADR-0019：分类在**资产项**上，记录只承载金额/日期。旧 fixture 的
# `Attribute(type='account'|'fund_type')` + `create_asset(attribute_ids=…)` 已废弃
# （记录侧写入路径直接 422），这里改为「机构/类别/成员」字典行 + 项级分类。


def mk_account(db, name):
    """旧「账户」→ 机构（`institutions`）。同名复用，便于逐用例复用字典。"""
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind="bank")
        db.add(row)
        db.commit()
    return row


def mk_class(db, name="示例类型"):
    """叶子类别（`asset_classes`，旧 fund_type 的替代）。"""
    row = db.execute(
        select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if row is None:
        row = AssetClass(code=f"T{len(name)}{len(name.encode('utf-8'))}",
                         name=name, level=1, side_scope="any")
        db.add(row)
        db.commit()
    return row


def mk_member(db, name="本人", role="self"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role=role)
        db.add(row)
        db.commit()
    return row


def mk_asset(db, name, amount, on_date, attrs=(), side="asset", liquidity_days=0):
    """合成一条记录并在其资产项上打分类。

    `attrs` 保留旧位置参数形状：传 `Institution` / `AssetClass` 行即可（类别与机构
    各取第一个，缺省则建示例字典）。分类是项级属性，与记录条数脱钩。
    """
    row = create_asset(db, name, Decimal(str(amount)), on_date, side=side)
    klass = next((a for a in attrs if isinstance(a, AssetClass)), None)
    institution = next((a for a in attrs if isinstance(a, Institution)), None)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": (klass or mk_class(db)).id,
        "liquidity_days": liquidity_days,
        "institution_id": (institution or mk_account(db, "示例默认机构")).id,
        "owner_id": mk_member(db).id,
    })
    return row


def set_principal(db, item_id, amount, eff):
    route_set_principal(item_id, PrincipalRequest(
        amount=str(amount), effective_date=eff.isoformat(), reason="期初投入"), db)


def fmt(value: Decimal, quantum=RATE_Q) -> str:
    return str(Decimal(value).quantize(quantum))


def mk_portfolio(db):
    """完整期基准：两只项＋一条负债＋一个配置目标＋一条本金口径。

    1/31：示例-股 1000（资产）/ 示例-债 500（负债）；2/28 均更新（覆盖度满）。
    目标：示例类型 60%（实际 100% → 偏离 40pp）；本金 1000 → 升值 200。
    """
    alice = mk_account(db, "示例账户甲")
    bob = mk_account(db, "示例账户乙")
    ft = mk_class(db, "示例类型")
    stock = mk_asset(db, "示例-股", 1000, D_JAN, [alice, ft])
    mk_asset(db, "示例-股", 1200, D_FEB, [alice, ft])
    mk_asset(db, "示例-债", 500, D_JAN, [bob, ft], side="liability")
    mk_asset(db, "示例-债", 500, D_FEB, [bob, ft], side="liability")
    set_principal(db, stock.asset_item_id, 1000, D_JAN)
    db.add(AllocationTarget(dimension="asset_class", target_id=str(ft.id),
                            target_percent=Decimal("60"),
                            alert_threshold=Decimal("5")))
    db.commit()
    return {"stock": stock, "ft": ft, "alice": alice, "bob": bob}


def items_of(db, as_of=None):
    return {i.id: i for i in metric_service.get_status(db, as_of).items}


# ---------------------------------------------------------------- 注册表


def test_registry_self_check_and_dictionary_contract():
    dictionary = metric_service.get_dictionary()
    ids = [i.id for i in dictionary.items]
    assert len(ids) == 13 and len(set(ids)) == 13
    assert ids[0] == "asset_total" and ids[-1] == "goal_required_annualized"
    assert "data_coverage" not in ids        # ADR-0019 D12：覆盖度指标已下线

    # 本期判据：C2 双阈值 + C1 单侧（其余不发明默认带）
    with_criterion = [i.id for i in dictionary.items if i.criterion is not None]
    assert with_criterion == ["allocation_deviation", "goal_required_annualized"]
    dev = next(i for i in dictionary.items if i.id == "allocation_deviation").criterion
    assert dev.kind == "dual_threshold"
    assert dev.source == "allocation_targets.alert_threshold"
    c1 = next(i for i in dictionary.items
              if i.id == "goal_required_annualized").criterion
    assert c1.kind == "dual_threshold_one_sided"
    assert c1.source == "goal_versions.target_rate_annual"

    # 字典：静态、无 meta；字段集与 spec §3 一致
    payload = dictionary.model_dump()
    assert set(payload) == {"items"}
    first = payload["items"][0]
    assert set(first) == {"id", "name", "domain", "tags", "unit", "direction",
                          "criterion", "as_of_semantics", "drill"}
    assert set(first["tags"]) == {"layer", "form", "time", "direction",
                                  "purpose", "source", "trust"}
    assert first["tags"]["layer"] == "原子" and first["direction"] == "中性"


def test_grade_engine_truth_table():
    criterion = {"kind": "abs_threshold", "threshold": Decimal("5"),
                 "source": "allocation_targets.alert_threshold"}

    # 无判据 → None（不是第五态）
    assert metric_service.evaluate_grade(Decimal("1"), None, []) is None
    # 有判据：等于阈值不触发；超阈值（双向）→ yellow
    green = metric_service.evaluate_grade(Decimal("5"), criterion, [])
    assert green["state"] == "green" and green["band"]["threshold"] == "5"
    assert green["basis"] == "current"
    assert metric_service.evaluate_grade(Decimal("5.01"), criterion, [])["state"] == "yellow"
    assert metric_service.evaluate_grade(Decimal("-6"), criterion, [])["state"] == "yellow"
    # 阻断性闸门优先于判据（口径降级永不给绿）
    blocked = [{"code": "NO_DATA", "note": "空库", "blocking": True}]
    assert metric_service.evaluate_grade(Decimal("100"), criterion, blocked)["state"] == "gray"
    assert metric_service.evaluate_grade(Decimal("1"), None, blocked)["state"] == "gray"
    # 非阻断闸门不改变灯：无判据仍为 None
    note_only = [{"code": "DATA_MISSING_CARRY_FORWARD", "note": "含沿用值",
                  "blocking": False}]
    assert metric_service.evaluate_grade(Decimal("1"), None, note_only) is None
    # 有判据但值不可得 → gray
    assert metric_service.evaluate_grade(None, criterion, [])["state"] == "gray"


# ---------------------------------------------------------------- 契约与对账


def test_status_contract_shape_and_decimal_precision(db):
    mk_portfolio(db)
    status = metric_service.get_status(db)

    payload = status.model_dump()
    assert set(payload) == {"meta", "items", "domains", "events", "summary"}
    for item in payload["items"]:
        assert set(item) == {"id", "value", "unit", "direction", "grade",
                             "gates", "tags", "details", "drill"}
        assert isinstance(item["gates"], list)
        assert set(item["tags"]) == {"layer", "form", "time", "direction",
                                     "purpose", "source", "trust"}
        if item["unit"] == "CNY" and item["value"] is not None:
            assert MONEY_RE.match(item["value"]), item
        if item["unit"] in ("ratio", "pp") and item["value"] is not None:
            assert RATE_RE.match(item["value"]), item

    by_id = {i["id"]: i for i in payload["items"]}
    assert by_id["asset_total"]["value"] == "1200.00"
    assert by_id["liability_total"]["value"] == "500.00"
    assert by_id["net_worth"]["value"] == "700.00"
    assert by_id["net_worth_change_pct"]["value"] == "0.4000"
    # 本期无判据 → grade=null（不显示灯）
    assert by_id["net_worth"]["grade"] is None
    assert status.meta.snapshot_date == D_FEB
    assert status.meta.basis.principal == "pc-1"
    # HTTP 序列化形态（date/None/字符串数值均可 json 化）
    json.dumps(status.model_dump(mode="json"))


def test_status_reconciles_with_existing_endpoints(db):
    mk_portfolio(db)
    status = metric_service.get_status(db)
    by_id = {i.id: i for i in status.items}

    summary = analysis_service.get_summary(db)
    assert by_id["asset_total"].value == fmt(summary.total_assets, Decimal("0.01"))
    assert by_id["liability_total"].value == fmt(summary.total_liabilities, Decimal("0.01"))
    assert by_id["net_worth"].value == fmt(summary.net_worth, Decimal("0.01"))
    assert abs(Decimal(str(summary.change_percent)) / 100
               - Decimal(by_id["net_worth_change_pct"].value)) <= RATE_Q

    returns = analysis_service.get_returns(db)
    assert by_id["twr"].value == fmt(Decimal(returns["twr"]["period"]["period_return"]))
    assert by_id["xirr"].value == returns["xirr"]

    appr = appreciation_service.get_appreciation(db)
    assert by_id["appreciation_amount"].value == fmt(
        Decimal(appr["portfolio"]["appreciation_total"]), Decimal("0.01"))
    assert by_id["appreciation_rate"].value == appr["portfolio"]["rate"]

    alloc = allocation_service.analyze_allocation(db, "asset_class", D_FEB)
    target_row = next(i for i in alloc.items if i.target_percent is not None)
    assert by_id["allocation_deviation"].value == fmt(Decimal(target_row.deviation))
    assert by_id["allocation_deviation"].grade.state == "red"   # 40pp > 红档 10pp
    assert by_id["allocation_deviation"].grade.band.threshold == "5.00"
    assert by_id["allocation_deviation"].grade.band.red == "10.00"


# ---------------------------------------------------------------- 闸门映射


def test_no_data_blocks_every_metric(db):
    status = metric_service.get_status(db)
    assert len(status.items) == 10   # 无目标 → 无 C1 项；无 data_coverage（D12）
    assert status.meta.snapshot_date is None
    assert "NO_DATA" in [d.code for d in status.meta.degradations]
    for item in status.items:
        assert item.value is None
        assert item.grade is not None and item.grade.state == "gray"
        assert any(g.code == "NO_DATA" and g.blocking for g in item.gates)
        assert item.tags.trust != "实测"


def test_liability_only_rate_unavailable_gray(db):
    mk_asset(db, "示例-贷", 300, D_JAN, side="liability")
    mk_asset(db, "示例-贷", 350, D_FEB, side="liability")

    by_id = items_of(db)
    for metric_id in ("twr", "xirr"):
        item = by_id[metric_id]
        assert item.value is None
        assert item.grade.state == "gray"
        assert any(g.code == "RATE_UNAVAILABLE" and g.blocking for g in item.gates)
        assert item.tags.trust == "率不可得"

    ratio = by_id["liability_ratio"]
    assert ratio.value is None and ratio.grade.state == "gray"
    assert any(g.code == "RATE_UNAVAILABLE" and "总资产为 0" in g.note
               for g in ratio.gates)

    rate = by_id["appreciation_rate"]
    assert rate.value is None and rate.grade.state == "gray"
    assert any(g.code == "RATE_UNAVAILABLE" and g.blocking for g in rate.gates)


def test_principal_not_set_is_note_when_combo_available(db):
    """有口径项 → 升值额可得，PRINCIPAL_NOT_SET 仅为注记（不阻断）。"""
    mk_portfolio(db)
    amount = items_of(db)["appreciation_amount"]
    assert amount.value == "200.00"
    assert amount.grade is None
    assert not [g for g in amount.gates if g.code == "PRINCIPAL_NOT_SET"]


def test_principal_not_set_blocks_when_combo_unavailable(db):
    """全部资产项无口径 → 组合升值不可得：阻断 ⚪。"""
    mk_asset(db, "示例-股", 1000, D_JAN)
    mk_asset(db, "示例-股", 1200, D_FEB)

    amount = items_of(db)["appreciation_amount"]
    assert amount.value is None
    assert amount.grade.state == "gray"
    assert any(g.code == "PRINCIPAL_NOT_SET" and g.blocking for g in amount.gates)


def test_twr_large_flow_gate_non_blocking(db):
    a = mk_asset(db, "示例-股", 1000, date(2026, 1, 1))
    set_principal(db, a.asset_item_id, 1000, date(2026, 1, 1))
    mk_asset(db, "示例-股", 1600, date(2026, 3, 1))
    set_principal(db, a.asset_item_id, 1600, date(2026, 2, 1))

    twr = items_of(db)["twr"]
    assert twr.value is not None                        # 率可得
    assert twr.grade is None                            # 无判据
    gate = next(g for g in twr.gates if g.code == "TWR_LARGE_FLOW_APPROX")
    assert gate.blocking is False and gate.note


# ---------------------------------------------------------------- 路由

def test_routes_direct(db):
    """端点直调透传（字典静态无 meta；状态面 meta 顶层）。"""
    from routers.metrics import metrics_dictionary, metrics_status

    dictionary = metrics_dictionary()
    assert len(dictionary.items) == 13

    mk_portfolio(db)
    status = metrics_status(as_of=D_FEB, db=db)
    assert status.meta.snapshot_date == D_FEB
    assert {i.id for i in status.items} >= {"twr", "xirr", "net_worth"}


# ---------------------------------------------------------------- as_of


def test_as_of_default_specified_and_out_of_range(db):
    mk_portfolio(db)

    default = metric_service.get_status(db)
    assert default.meta.snapshot_date == D_FEB

    specified = metric_service.get_status(db, as_of=D_JAN)
    by_id = {i.id: i for i in specified.items}
    assert specified.meta.snapshot_date == D_JAN
    assert by_id["asset_total"].value == "1000.00"
    assert by_id["net_worth"].value == "500.00"
    assert by_id["net_worth_change_pct"].value is None   # 无更早快照

    for bad in (date(2026, 1, 1), date(2026, 3, 1)):
        with pytest.raises(ApiError) as ei:
            metric_service.get_status(db, as_of=bad)
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.status_code == 422
        assert ei.value.details["fields"][0]["field"] == "as_of"

    # 区间内非快照日：取 ≤as_of 最新快照数据，环比对上一快照（不是自身）
    mid = metric_service.get_status(db, as_of=date(2026, 2, 15))
    mid_by = {i.id: i for i in mid.items}
    assert mid.meta.snapshot_date == date(2026, 2, 15)
    assert mid_by["asset_total"].value == "1000.00"
    assert mid_by["net_worth_change_pct"].value is None


# ---------------------------------------------------------------- 配置偏离


def test_allocation_deviation_threshold_behavior(db):
    mk_portfolio(db)
    item = items_of(db)["allocation_deviation"]
    assert item.value == "40.0000"                      # 实际 100% − 目标 60%
    assert item.grade.state == "red"                    # 40 > 红 10（默认 2×黄）
    assert item.grade.band.kind == "dual_threshold"
    assert item.grade.band.red == "10.00"
    assert item.grade.band.source.startswith("allocation_targets.alert_threshold+")
    assert item.grade.band.components["rel_applicable"] is True
    rows = item.details["rows"]
    assert len(rows) == 1
    assert rows[0]["dimension"] == "asset_class"
    assert rows[0]["deviation"] == "40.0000"
    assert rows[0]["band"]["state"] == "red"

    # 放宽黄/红并关闭相对先触发：恰等于黄阈值 → 绿（严格大于）
    target = db.query(AllocationTarget).one()
    target.alert_threshold = Decimal("40")
    target.relative_threshold = Decimal("100")
    db.commit()
    relaxed = items_of(db)["allocation_deviation"]
    assert relaxed.grade.state == "green"
    assert relaxed.grade.band.threshold == "40.00"
    assert relaxed.grade.band.red == "80.00"


def test_allocation_deviation_absent_without_targets(db):
    alice = mk_account(db, "示例账户甲")
    ft = mk_class(db, "示例类型")
    mk_asset(db, "示例-股", 1000, D_JAN, [alice, ft])

    by_id = items_of(db)
    assert "allocation_deviation" not in by_id          # 无目标 → 无行
    assert len(by_id) == 9


def test_allocation_deviation_worst_row_by_severity(db):
    """逐行阈值后最差行按严重度取，而非 max(|dev|)（spec §2.5）。"""
    accs = [mk_account(db, f"示例账户{n}") for n in "甲乙丙"]
    for acc, amount in zip(accs, (0, 150, 850)):
        if amount:
            mk_asset(db, f"示例-{acc.name}", amount, D_JAN, [acc])
    db.add_all([
        # A：偏离 -30pp，但黄 50/相对 100 → 30pp 等于有效黄 → 绿
        AllocationTarget(dimension="institution", target_id=str(accs[0].id),
                         target_percent=Decimal("30.00"),
                         alert_threshold=Decimal("50.00"),
                         relative_threshold=Decimal("100.00")),
        # B：偏离 +10pp，默认带 → 等于红 10 → 黄
        AllocationTarget(dimension="institution", target_id=str(accs[1].id),
                         target_percent=Decimal("5.00"),
                         alert_threshold=Decimal("5.00")),
        AllocationTarget(dimension="institution", target_id=str(accs[2].id),
                         target_percent=Decimal("85.00"),
                         alert_threshold=Decimal("5.00")),
    ])
    db.commit()

    item = items_of(db)["allocation_deviation"]
    assert item.value == "10.0000"                     # 不是 |−30| 的 A 行
    assert item.grade.state == "yellow"
    rows = {r["target_id"]: r for r in item.details["rows"]}
    assert rows[str(accs[0].id)]["state"] == "green"
    assert rows[str(accs[1].id)]["state"] == "yellow"


def test_allocation_deviation_total_assets_zero_blocks(db):
    """总资产=0 → 值 null + RATE_UNAVAILABLE 阻断（不出全红假信号）。"""
    acc = mk_account(db, "示例账户甲")
    mk_asset(db, "示例-贷", 300, D_JAN, [acc], side="liability")
    db.add(AllocationTarget(dimension="institution", target_id=str(acc.id),
                            target_percent=Decimal("50.00"),
                            alert_threshold=Decimal("5.00")))
    db.commit()

    item = items_of(db)["allocation_deviation"]
    assert item.value is None
    assert item.grade.state == "gray"
    assert any(g.code == "RATE_UNAVAILABLE" and g.blocking
               and "总资产为 0" in g.note for g in item.gates)
    assert all(r["state"] == "gray" for r in item.details["rows"])
    degr = [(d.code, d.scope) for d in metric_service.get_status(db).meta.degradations]
    assert ("RATE_UNAVAILABLE", "allocation_deviation") in degr


def test_goal_required_annualized_one_sided_grade(db):
    """C1 判灯：所需年化 > target_rate_annual → 黄；> 2× → 红（严格大于）。"""
    from services import goal_service
    mk_asset(db, "示例-股", 1000, date.today())
    goal, version, _ = goal_service.create_goal(
        db, name="示例目标", target_amount="2000.00",
        target_date=date(2030, 12, 31), reason="合成",
        target_rate_annual="0.0001")
    by_id = items_of(db)
    required = by_id["goal_required_annualized"]
    assert required.value is not None
    assert required.grade.state == "red"
    assert required.grade.band.kind == "dual_threshold_one_sided"
    assert required.grade.band.threshold == "0.0001"
    assert required.grade.band.red == "0.0002"

    progress = goal_service.get_goal_progress(db, goal.id)
    assert progress["grade"]["state"] == "red"          # 与状态面同源
    assert progress["grade"]["band"]["kind"] == "dual_threshold_one_sided"


def test_goal_required_annualized_green_and_no_criterion(db):
    from services import goal_service
    mk_asset(db, "示例-股", 1000, date.today())
    goal, _, _ = goal_service.create_goal(
        db, name="示例目标", target_amount="2000.00",
        target_date=date(2030, 12, 31), reason="合成",
        target_rate_annual="1.0")
    required = items_of(db)["goal_required_annualized"]
    assert required.grade.state == "green"               # 所需年化 < 100%

    goal2, _, _ = goal_service.create_goal(
        db, name="示例目标乙", target_amount="2000.00",
        target_date=date(2030, 12, 31), reason="合成")
    progress = goal_service.get_goal_progress(db, goal2.id)
    assert progress["grade"] is None                     # 无判据 → 不判灯
    assert required.grade.basis == "current"


def test_historical_as_of_note_marks_current_criterion(db):
    mk_portfolio(db)
    status = metric_service.get_status(db, as_of=D_JAN)
    item = {i.id: i for i in status.items}["allocation_deviation"]
    assert item.grade is not None and item.grade.band is not None
    assert item.grade.note.endswith("（判据为当期）")