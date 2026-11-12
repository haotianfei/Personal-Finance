"""三期-2 W4 B4 域级汇总＋异动测试（spec §3.4/§3.3、§7.2；ADR-0012 D5/D6）。

覆盖：域聚合真值表（含 gray 阻断、judged/total、双红诊断、复合项红行展开、
空域省略）；异动上升沿 new/ongoing/回落再触发/空档账期/停用规则/无 target_id/
域映射；as_of 历史；空库；include_details 旁路。数据全部合成。
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import (AlertRule, AllocationTarget, AssetClass, Institution,
                    Member)
from services import (alert_service, classification_service, metric_service,
                      signal_service)
from services.asset_service import create_asset

DOMAINS_OF = {"net_worth": "B1", "asset_total": "B1", "allocation_deviation": "C2",
              "not_in_registry": "D9"}   # 未注册的域：聚合时整块丢弃


def grade_item(metric_id, state, details=None):
    grade = SimpleNamespace(state=state) if state else None
    return SimpleNamespace(id=metric_id, grade=grade, details=details, drill=None)


def domain_of(domains, code):
    return next(d for d in domains if d["domain"] == code)


# ---------------------------------------------------------------- 域聚合真值表


def test_domain_green_with_unjudged_keeps_judged_ratio():
    domains = signal_service.build_domains(
        [grade_item("net_worth", "green"), grade_item("asset_total", None)],
        DOMAINS_OF, [])
    b1 = domain_of(domains, "B1")
    assert b1["state"] == "green"
    assert b1["judged"] == 1 and b1["total"] == 2
    assert b1["counts"] == {"green": 1, "yellow": 0, "red": 0, "gray": 0,
                            "unjudged": 1}
    assert b1["worst"] == "green"


def test_domain_yellow_from_event_and_gray_does_not_block_yellow():
    domains = signal_service.build_domains(
        [grade_item("net_worth", "green")], DOMAINS_OF,
        [{"domain": "B1", "status": "new"}])
    b1 = domain_of(domains, "B1")
    assert b1["state"] == "yellow"
    assert b1["active_events"] == 1 and b1["event_state"] == "yellow"
    # 1 黄 + 1 ⚪ → 黄（黄优先于 ⚪）
    domains = signal_service.build_domains(
        [grade_item("net_worth", "yellow"), grade_item("asset_total", "gray")],
        DOMAINS_OF, [])
    assert domain_of(domains, "B1")["state"] == "yellow"


def test_domain_gray_blocks_green():
    domains = signal_service.build_domains(
        [grade_item("net_worth", "gray"), grade_item("asset_total", "green")],
        DOMAINS_OF, [])
    b1 = domain_of(domains, "B1")
    assert b1["state"] == "gray" and b1["gray_blocked"] is True
    assert b1["counts"]["gray"] == 1 and b1["counts"]["green"] == 1


def test_domain_all_unjudged_is_gray_judged_zero():
    domains = signal_service.build_domains(
        [grade_item("net_worth", None), grade_item("asset_total", None)],
        DOMAINS_OF, [])
    b1 = domain_of(domains, "B1")
    assert b1["state"] == "gray" and b1["judged"] == 0 and b1["total"] == 2
    assert b1["worst"] is None


def test_domain_red_and_double_red_diagnosis():
    domains = signal_service.build_domains(
        [grade_item("net_worth", "red"), grade_item("asset_total", "red")],
        DOMAINS_OF, [])
    b1 = domain_of(domains, "B1")
    assert b1["state"] == "red"
    assert b1["counts"]["red"] == 2
    assert [r["kind"] for r in b1["reds"]] == ["item", "item"]
    assert b1["needs_diagnosis"] is True


def test_domain_composite_red_item_expands_rows():
    rows = [
        {"dimension": "asset_class", "target_id": "1", "name": "示例类型",
         "deviation": "40.0000", "state": "red"},
        {"dimension": "institution", "target_id": "2", "name": "示例账户",
         "deviation": "-20.0000", "state": "red"},
        {"dimension": "institution", "target_id": "3", "name": "示例账户乙",
         "deviation": "1.0000", "state": "green"},
    ]
    domains = signal_service.build_domains(
        [grade_item("allocation_deviation", "red", {"rows": rows})],
        DOMAINS_OF, [])
    c2 = domain_of(domains, "C2")
    assert c2["state"] == "red"
    assert c2["counts"]["red"] == 1                  # counts 只计 item
    assert len([r for r in c2["reds"] if r["kind"] == "row"]) == 2
    assert c2["reds"][1]["dimension"] == "asset_class"
    assert c2["reds"][1]["deviation"] == "40.0000"
    assert c2["needs_diagnosis"] is True


def test_domain_empty_is_omitted_and_order_fixed():
    """空域省略 + 固定序；未注册域（旧「底座」已删）整块丢弃，不占位。"""
    domains = signal_service.build_domains(
        [grade_item("allocation_deviation", "green"),
         grade_item("not_in_registry", None)],
        DOMAINS_OF, [])
    assert [d["domain"] for d in domains] == ["C2"]   # B1/B2/B3 空域省略

    domains = signal_service.build_domains(
        [grade_item("net_worth", "green"),
         grade_item("allocation_deviation", "green")],
        DOMAINS_OF, [])
    assert [d["domain"] for d in domains] == ["B1", "C2"]   # 注册表固定序


# ---------------------------------------------------------------- 汇总


def test_summary_counts_no_global_grade():
    items = [grade_item("net_worth", "red"), grade_item("asset_total", None),
             grade_item("allocation_deviation", "gray")]
    domains = signal_service.build_domains(items, DOMAINS_OF,
                                           [{"domain": "C2", "status": "new"}])
    summary = signal_service.summarize(items, domains, [{"domain": "C2",
                                                         "status": "new"}])
    assert summary == {"green": 0, "yellow": 0, "red": 1, "gray": 1,
                       "unjudged": 1, "judged": 2, "total": 3,
                       "domains": 2, "active_events": 1, "new_events": 1}
    assert "state" not in summary                       # 不设全局灯


# ---------------------------------------------------------------- 异动


def mk_account(db, name="示例账户甲"):
    """旧「账户」→ 机构（ADR-0019 D3：机构/渠道拆分，账户维度退役）。"""
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is None:
        row = Institution(name=name, kind="bank")
        db.add(row)
        db.commit()
    return row


def mk_class(db, name="示例类别"):
    node = db.execute(
        select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if node is None:
        node = AssetClass(code=f"T{len(name)}", name=name, level=1, side_scope="any")
        db.add(node)
        db.commit()
    return node


def mk_member(db, name="本人", role="self"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is None:
        row = Member(name=name, role=role)
        db.add(row)
        db.commit()
    return row


def mk_snapshot(db, account, on_date, amount, name="示例-基金"):
    """一条记录 + 项级分类（ADR-0019：分类挂资产项，机构取代旧 account）。"""
    row = create_asset(db, name, Decimal(str(amount)), on_date)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": mk_class(db).id,
        "liquidity_days": 0,
        "institution_id": account.id,
        "owner_id": mk_member(db).id,
    })
    return row


def mk_rule(db, **overrides):
    kwargs = dict(name="示例规则", dimension="institution", target_id="1",
                  period_type="month", compare_type="previous",
                  amount_threshold=Decimal("100"), percent_threshold=None,
                  direction="up", is_active=True)
    kwargs.update(overrides)
    rule = AlertRule(**kwargs)
    db.add(rule)
    db.commit()
    return rule


def test_event_new_then_ongoing_then_falloff_and_retrigger(db):
    acc = mk_account(db)
    rule = mk_rule(db, target_id=str(acc.id))
    for month, amount in ((1, 1000), (2, 1500), (3, 1600)):
        mk_snapshot(db, acc, date(2026, month, 28), amount)

    # 2 月：首期触发（1 月为上一有数据账期但未触发）→ new
    feb = signal_service.collect_events(db, date(2026, 2, 28))
    assert len(feb) == 1 and feb[0]["status"] == "new"
    assert feb[0]["previous_period"] == "2026-01"
    assert feb[0]["id"] == f"alert:{rule.id}:2026-02"
    assert feb[0]["period_end"] == "2026-02-28"
    assert feb[0]["change_amount"] == "500.00"
    assert feb[0]["change_percent"] == "50.00"
    assert feb[0]["thresholds"] == {"amount": "100.00", "percent": None}
    assert feb[0]["severity"] == "yellow" and feb[0]["domain"] == "B3"

    # 3 月：连续触发 → ongoing（上一账期也触发）
    mar = signal_service.collect_events(db, date(2026, 3, 31))
    assert mar[0]["status"] == "ongoing"
    assert mar[0]["previous_period"] == "2026-02"

    # 4 月回落（金额不变）→ 无事件；5 月再触发 → new
    mk_snapshot(db, acc, date(2026, 4, 28), 1600)
    assert signal_service.collect_events(db, date(2026, 4, 30)) == []
    mk_snapshot(db, acc, date(2026, 5, 28), 1800)
    may = signal_service.collect_events(db, date(2026, 5, 31))
    assert len(may) == 1 and may[0]["status"] == "new"
    assert may[0]["previous_period"] == "2026-04"


def test_event_previous_resolves_last_period_with_data_across_gap(db):
    acc = mk_account(db)
    mk_rule(db, target_id=str(acc.id))
    for month, amount in ((1, 1000), (3, 1500), (5, 1700)):
        mk_snapshot(db, acc, date(2026, month, 28), amount)
    events = signal_service.collect_events(db, date(2026, 5, 31))
    assert events[0]["period"] == "2026-05"
    assert events[0]["previous_period"] == "2026-03"   # 跳过空档 4 月
    assert events[0]["status"] == "ongoing"


def test_event_ignores_disabled_rules_and_maps_tag_dimension(db):
    acc = mk_account(db)
    mk_rule(db, target_id=str(acc.id), is_active=False)
    mk_snapshot(db, acc, date(2026, 1, 28), 1000)
    mk_snapshot(db, acc, date(2026, 2, 28), 1500)
    assert signal_service.collect_events(db) == []

    # ADR-0019 前：tag/owner 这类维度「出事件但不归域」（域视图里等于消失）。
    # 重构后每个可建规则的新维度都必须归域：项级分类维度一律 B3（结构）。
    rule = mk_rule(db, name="示例标签规则", dimension="tag", target_id=None,
                   amount_threshold=Decimal("100"))
    events = signal_service.collect_events(db)
    assert events and events[0]["rule_id"] == rule.id
    assert events[0]["domain"] == "B3"


def test_event_domain_covers_all_alert_dimensions():
    """域映射表＝预警维度表：不留「只出事件、点不亮任何域」的盲区。"""
    assert set(alert_service.SUPPORTED_DIMENSIONS) <= set(signal_service.EVENT_DOMAIN)
    assert signal_service.EVENT_DOMAIN["asset_name"] == "B1"      # 行级聚合维度
    # 主名 risk_level；存量规则行的落库拼写 `risk` 仍须归域，否则那批异动掉出域态
    assert signal_service.EVENT_DOMAIN["risk_level"] == "B3"
    assert signal_service.EVENT_DOMAIN["risk"] == "B3"
    assert alert_service.require_dimension("risk") == "risk_level"   # 写入归一
    assert signal_service.EVENT_DOMAIN.get("不存在的维度") is None
    # 旧维度名（EAV 时代）不得再出现在映射表里
    assert not {"fund_type", "liquidity_rating", "account"} & set(
        signal_service.EVENT_DOMAIN)


def test_event_without_target_id_skips_details_but_still_triggers(db):
    acc = mk_account(db)
    mk_rule(db, target_id=None)
    mk_snapshot(db, acc, date(2026, 1, 28), 1000)
    mk_snapshot(db, acc, date(2026, 2, 28), 1500)

    events = signal_service.collect_events(db, include_details=False)
    assert len(events) == 1 and events[0]["status"] == "new"

    # include_details 旁路：默认取明细、False 不取（既有 /alerts/results 契约不变）
    rule = alert_service.get_alert_rules(db, active_only=True)[0]
    assert alert_service.check_alert(db, rule.id, "2026-02").details
    assert alert_service.check_alert(db, rule.id, "2026-02",
                                     include_details=False).details == []


def test_event_as_of_uses_latest_period_end_le_cutoff(db):
    acc = mk_account(db)
    mk_rule(db, target_id=str(acc.id))
    for month, amount in ((1, 1000), (2, 1500), (3, 1600)):
        mk_snapshot(db, acc, date(2026, month, 28), amount)
    # as_of 落在两账期之间 → 只用「账期终点 ≤ as_of」的最新账期（2 月）
    events = signal_service.collect_events(db, date(2026, 3, 15))
    assert events[0]["period"] == "2026-02"
    assert events[0]["period_end"] == "2026-02-28"


# ---------------------------------------------------------------- 集成


def test_status_empty_db_has_domains_but_no_events(db):
    status = metric_service.get_status(db)
    assert status.events == []
    assert status.summary.total == len(status.items) == 10
    assert status.summary.gray == 10 and status.summary.unjudged == 0
    # 「底座」域已删（唯一成员 data_coverage 随 ADR-0019 D12 下线）→ 域清单只剩四域
    assert [d.domain for d in status.domains] == ["B1", "B2", "B3", "C2"]
    assert all(d.state == "gray" for d in status.domains)
    assert all(d.active_events == 0 for d in status.domains)


def test_status_c2_red_domain_and_row_expansion(db):
    acc = mk_account(db)
    mk_snapshot(db, acc, date(2026, 1, 31), 600)
    db.add(AllocationTarget(dimension="institution", target_id=str(acc.id),
                            target_percent=Decimal("30"),
                            alert_threshold=Decimal("5")))
    db.commit()

    status = metric_service.get_status(db)
    c2 = next(d for d in status.domains if d.domain == "C2")
    assert c2.state == "red" and c2.counts.red == 1
    assert c2.needs_diagnosis is True
    assert [r.kind for r in c2.reds] == ["item", "row"]
    assert c2.reds[1].target_id == str(acc.id)
    assert c2.drill.target == "/analysis?section=allocation"
    assert status.summary.red == 1
    assert status.summary.judged >= 1


def test_status_event_lights_domain_yellow(db):
    acc = mk_account(db)
    mk_rule(db, target_id=str(acc.id))
    for month, amount in ((1, 1000), (2, 1500)):
        mk_snapshot(db, acc, date(2026, month, 28), amount)

    status = metric_service.get_status(db)
    b3 = next(d for d in status.domains if d.domain == "B3")
    assert b3.state == "yellow"
    assert b3.active_events == 1 and b3.event_state == "yellow"
    assert status.summary.active_events == 1
    assert status.summary.new_events == 1
    event = status.events[0]
    assert event.domain == "B3" and event.drill.target == \
        f"/alerts?rule={event.rule_id}"


def test_status_as_of_out_of_range_bubbles_422(db):
    mk_rule(db)
    mk_snapshot(db, mk_account(db), date(2026, 1, 28), 1000)
    with pytest.raises(ApiError) as ei:
        metric_service.get_status(db, as_of=date(2026, 3, 31))
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.details["fields"][0]["field"] == "as_of"