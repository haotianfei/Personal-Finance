"""三期-2 W4：B4 域级汇总＋异动（spec §3；ADR-0012 D5/D6）。

- **域**＝指标注册表 `domain`（B1/B2/B3/C1/C2），固定序；B4 是汇总者，
  不建 `domain="B4"` 条目；**不设全局灯/总分**。
  （旧「底座」域已删：它唯一的成员 `data_coverage` 随 ADR-0019 D12 下线，
   域因此恒为空、永远不出现，留着只会让「域清单」多一个不存在的名字。）
- **聚合**：任一 red → red；否则任一 yellow 或域内异动 → yellow；否则 gray →
  gray；否则有 ≥1 判灯 → green；无 item 的域不出现。`grade=null` 不阻断绿但
  回传 `judged/total`；gray（判了但降级）阻断绿。
- **异动＝事件**：源＝现有 `alert_rules` 检查结果（复用 `check_alert`）；
  只取上升沿（本期触发、上一有数据账期未触发 → new；连续 → ongoing）；
  去重键 `(rule_id, period)`；默认黄；零落表、读时重算。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from services import alert_service
from services.period_service import group_dates_by_period

MONEY_Q = Decimal("0.01")

DOMAIN_ORDER = ("B1", "B2", "B3", "C1", "C2")

DOMAIN_META = {
    "B1": {"name": "资产水位",
           "drill": {"target": "/?section=net-worth", "label": "概览·净资产"}},
    "B2": {"name": "收益",
           "drill": {"target": "/analysis?section=returns", "label": "分析·双口径收益"}},
    "B3": {"name": "结构",
           "drill": {"target": "/analysis?section=structure", "label": "分析·结构"}},
    "C1": {"name": "目标",
           "drill": {"target": "/planning/goals", "label": "目标与达成度"}},
    "C2": {"name": "配置",
           "drill": {"target": "/analysis?section=allocation", "label": "分析·资产配置"}},
}

# 异动 → 域：`asset_name`（行级聚合维度）→ B1；项级分类维度 → B3（结构）。
# ADR-0019：旧维度名（fund_type / liquidity_rating / account）已下线，新词汇与
# `alert_service.SUPPORTED_DIMENSIONS` 一一对应——不留「只出事件、点不亮任何域」的
# 盲区（那种事件在域级视图里等于消失）。
# 主名是 `risk_level`；`risk` 是存量规则行的落库拼写，读侧一并认，否则那批规则
# 触发的异动会掉出域态。
EVENT_DOMAIN = {
    "asset_name": "B1",
    "asset_class": "B3",
    "liquidity": "B3",
    "institution": "B3",
    "channel": "B3",
    "owner": "B3",
    "risk_level": "B3",
    "tag": "B3",
    "risk": "B3",           # 同义拼写（存量 alert_rules.dimension='risk'）
}

_SEVERITY = {"green": 1, "yellow": 2, "red": 3}
_COMPOSITE_ROW_METRIC = "allocation_deviation"


def build_domains(items, domains_of: dict[str, str], events: list[dict]) -> list[dict]:
    """按注册表 domain 聚合单灯 → 域灯（固定序；空域省略）。"""
    grouped: dict[str, list] = {}
    for item in items:
        domain = domains_of.get(item.id)
        if domain in DOMAIN_META:
            grouped.setdefault(domain, []).append(item)

    out: list[dict] = []
    for domain in DOMAIN_ORDER:
        group = grouped.get(domain)
        if group:
            out.append(_domain_payload(domain, group, events))
    return out


def _domain_payload(domain: str, group: list, events: list[dict]) -> dict:
    counts = {"green": 0, "yellow": 0, "red": 0, "gray": 0, "unjudged": 0}
    reds: list[dict] = []
    worst = None
    for item in group:
        state = item.grade.state if item.grade is not None else None
        if state is None:
            counts["unjudged"] += 1
            continue
        if state not in counts:
            counts["gray"] += 1
            continue
        counts[state] += 1
        if state in _SEVERITY and _SEVERITY[state] > _SEVERITY.get(worst, 0):
            worst = state
        if state == "red":
            reds.append(_item_signal(item))
            reds.extend(_row_signals(item))

    total = len(group)
    judged = total - counts["unjudged"]
    domain_events = [e for e in events if e.get("domain") == domain]
    active_events = len(domain_events)

    if counts["red"]:
        state = "red"
    elif counts["yellow"] or active_events:
        state = "yellow"
    elif counts["gray"]:
        state = "gray"
    elif judged:
        state = "green"
    else:
        state = "gray"

    meta = DOMAIN_META[domain]
    return {
        "domain": domain,
        "name": meta["name"],
        "state": state,
        "counts": counts,
        "judged": judged,
        "total": total,
        "worst": worst,
        "gray_blocked": counts["gray"] > 0,
        "active_events": active_events,
        "event_state": "yellow" if active_events else None,
        "reds": reds,
        "needs_diagnosis": len(reds) >= 2,
        "drill": dict(meta["drill"]),
    }


def _item_signal(item) -> dict:
    return {
        "kind": "item",
        "id": item.id,
        "name": _item_name(item),
        "drill": (item.drill.model_dump() if item.drill is not None else None),
    }


def _row_signals(item) -> list[dict]:
    """复合项（C2 配置偏离）把 details.rows[] 中的红行展开为子信号。"""
    if item.id != _COMPOSITE_ROW_METRIC or not isinstance(item.details, dict):
        return []
    out = []
    for row in item.details.get("rows") or []:
        if row.get("state") != "red":
            continue
        out.append({
            "kind": "row",
            "dimension": row.get("dimension"),
            "target_id": (str(row.get("target_id"))
                          if row.get("target_id") is not None else None),
            "name": row.get("name"),
            "deviation": row.get("deviation"),
            "drill": dict(DOMAIN_META["C2"]["drill"]),
        })
    return out


def _item_name(item) -> str:
    """注册表显示名（MetricStatusItem 不带 name，从字典取）。"""
    from services import metric_service
    entry = metric_service._BY_ID.get(item.id)
    return entry["name"] if entry else item.id


# ------------------------------------------------------------------ 异动


def collect_events(db: Session, as_of: date | None = None,
                   include_details: bool = False) -> list[dict]:
    """读时投影 alert_rules → 异动事件（零落表；同键同账期至多一条）。"""
    dates = alert_service._get_all_snapshot_dates(db)
    if not dates:
        return []
    cutoff = as_of or dates[-1]
    visible = [d for d in dates if d <= cutoff]
    if not visible:
        return []

    events: list[dict] = []
    groups_by_type: dict[str, dict] = {}
    amount_cache: dict[tuple, Decimal] = {}
    for rule in alert_service.get_alert_rules(db, active_only=True):
        groups = groups_by_type.get(rule.period_type)
        if groups is None:
            groups = group_dates_by_period(visible, rule.period_type)
            groups_by_type[rule.period_type] = groups
        if not groups:
            continue
        current, period_dates = max(
            groups.items(), key=lambda kv: (max(kv[1]), kv[0]))
        period_end = max(period_dates)

        result = alert_service.check_alert(
            db, rule.id, current, include_details=include_details,
            dates=dates, groups_by_type=groups_by_type,
            amount_cache=amount_cache)
        if result is None or not result.triggered:
            continue

        previous = alert_service._get_last_period_with_data(
            db, current, rule.period_type, dates=dates, groups=groups)
        prev_triggered = False
        if previous:
            prev_result = alert_service.check_alert(
                db, rule.id, previous, include_details=False,
                dates=dates, groups_by_type=groups_by_type,
                amount_cache=amount_cache)
            prev_triggered = bool(prev_result and prev_result.triggered)

        events.append(_event_payload(rule, result, current, period_end,
                                     previous, prev_triggered))

    events.sort(key=lambda e: (e["period"], e["rule_id"]), reverse=True)
    return events


def _event_payload(rule, result, period: str, period_end: date,
                   previous: str | None, prev_triggered: bool) -> dict:
    return {
        "id": f"alert:{rule.id}:{period}",
        "rule_id": rule.id,
        "rule_name": rule.name,
        "domain": EVENT_DOMAIN.get(rule.dimension),
        "severity": "yellow",       # 事件是「变化大」，不是「判断差」
        "status": "ongoing" if prev_triggered else "new",
        "period": period,
        "period_end": period_end.isoformat(),
        "previous_period": previous,
        "change_amount": _money(result.change_amount),
        "change_percent": _percent(result.change_percent),
        "thresholds": {
            "amount": (_money(rule.amount_threshold)
                       if rule.amount_threshold is not None else None),
            "percent": (_percent(rule.percent_threshold)
                        if rule.percent_threshold is not None else None),
        },
        "direction": rule.direction,
        "drill": {"target": f"/alerts?rule={rule.id}", "label": "预警规则"},
    }


def _money(value) -> str:
    return str(Decimal(str(value)).quantize(MONEY_Q))


def _percent(value) -> str | None:
    return None if value is None else f"{float(value):.2f}"


# ------------------------------------------------------------------ 汇总


def summarize(items, domains: list[dict], events: list[dict]) -> dict:
    """item 灯计数＋judged/total＋域/事件计数（无总分/全局灯）。"""
    counts = {"green": 0, "yellow": 0, "red": 0, "gray": 0, "unjudged": 0}
    for item in items:
        state = item.grade.state if item.grade is not None else None
        if state in counts:
            counts[state] += 1
        else:
            counts["unjudged"] += 1
    total = len(items)
    return {
        **counts,
        "judged": total - counts["unjudged"],
        "total": total,
        "domains": len(domains),
        "active_events": len(events),
        "new_events": sum(1 for e in events if e.get("status") == "new"),
    }