"""三期-0「指标骨架」：指标注册表＋状态面编排＋灯引擎＋口径闸门。

规格：`.scratch/metric-skeleton/spec.md` v1.0（冻结，§2 指标集 / §3 契约）。

纪律：
- **零迁移、零新表**：指标字典＝代码即真源；值一律读时计算，全部复用
  `analysis_service` / `appreciation_service` / `returns_service` /
  `allocation_service` / `meta_service`（不新造口径）。
- **灯永不脱离口径**：先过阻断性闸门（→ ⚪ gray），再判判据；无判据
  → `grade=null`（不预置默认带；C2/C1 判据见 ADR-0012）。
- 数值一律字符串 Decimal：金额 2 位、比率/收益率 4 位（% 由前端格式化）。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from schemas import (
    MetricCriterion, MetricDictionary, MetricDictionaryItem, MetricDrill,
    MetricGate, MetricGrade, MetricStatus, MetricStatusItem, MetricTags,
)
from services import allocation_service, analysis_service, appreciation_service
from services import band_service
from services import goal_service
from services import meta_service
from services import signal_service
from services.meta_service import (
    DATA_MISSING_CARRY_FORWARD, NO_DATA, PRINCIPAL_BASIS_VERSION,
    PRINCIPAL_NOT_SET, RATE_UNAVAILABLE, TWR_LARGE_FLOW_APPROX, build_meta,
)

ZERO = Decimal("0")
MONEY_Q = Decimal("0.01")
RATE_Q = Decimal("0.0001")

THRESHOLD_SOURCE = "allocation_targets.alert_threshold"

# 标签枚举（spec §3 / docs/design/metric-system.md §3，受控）
_ENUMS: dict[str, set[str]] = {
    "layer": {"原子", "派生", "复合"},
    "form": {"金额", "比率", "倍数", "时长", "计数", "序列", "状态灯"},
    "time": {"时点", "期间", "跨期"},
    "direction": {"越高越好", "越低越好", "适宜带", "双阈值警戒", "中性"},
    "purpose": {"展示", "诊断", "预警", "引导", "复盘"},
    "source": {"账本事实", "本金·关系对象", "参考区设定", "派生", "外部行情"},
    "trust": {"实测", "含沿用值", "未校验", "率不可得"},
}

# 判据类型：预留五种带型＋本期实现（spec §2.5；ADR-0012 D1）
_CRITERION_KINDS = {
    "abs_threshold", "one_sided_max", "one_sided_min",
    "two_sided_band", "dual_threshold", "dual_threshold_one_sided", "state",
}
_IMPLEMENTED_CRITERION_KINDS = {
    "abs_threshold", "dual_threshold", "dual_threshold_one_sided",
}

_GRADE_STATES = {"green", "yellow", "red", "gray"}


def _tags(layer: str, form: str, time: str, direction: str, purpose: str,
          source: str, trust: str = "实测") -> dict:
    return {"layer": layer, "form": form, "time": time, "direction": direction,
            "purpose": purpose, "source": source, "trust": trust}


# 指标注册表（代码即真源；启动自检 id 唯一 / 枚举合法 / 判据类型合法）
_REGISTRY: list[dict] = [
    {
        "id": "asset_total",
        "name": "总资产",
        "domain": "B1",
        "tags": _tags("原子", "金额", "时点", "中性", "展示", "账本事实"),
        "unit": "CNY",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/?section=net-worth", "label": "概览·净资产"},
    },
    {
        "id": "liability_total",
        "name": "总负债",
        "domain": "B1",
        "tags": _tags("原子", "金额", "时点", "中性", "展示", "账本事实"),
        "unit": "CNY",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/?section=net-worth", "label": "概览·净资产"},
    },
    {
        "id": "net_worth",
        "name": "净资产",
        "domain": "B1",
        "tags": _tags("原子", "金额", "时点", "中性", "展示", "账本事实"),
        "unit": "CNY",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/?section=net-worth", "label": "概览·净资产"},
    },
    {
        "id": "net_worth_change_pct",
        "name": "净资产环比",
        "domain": "B1",
        "tags": _tags("派生", "比率", "跨期", "中性", "诊断", "派生",
                      trust="含沿用值"),
        "unit": "ratio",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "跨期(相邻快照)",
        "drill": {"target": "/analysis?section=trend", "label": "分析·趋势"},
    },
    {
        "id": "appreciation_amount",
        "name": "升值额",
        "domain": "B2",
        "tags": _tags("派生", "金额", "期间", "中性", "展示", "派生"),
        "unit": "CNY",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "期间(至as_of)",
        "drill": {"target": "/analysis?section=appreciation", "label": "分析·升值"},
    },
    {
        "id": "appreciation_rate",
        "name": "升值率",
        "domain": "B2",
        "tags": _tags("派生", "比率", "期间", "越高越好", "诊断", "派生"),
        "unit": "ratio",
        "direction": "越高越好",
        "criterion": None,
        "as_of_semantics": "期间(至as_of)",
        "drill": {"target": "/analysis?section=appreciation", "label": "分析·升值"},
    },
    {
        "id": "twr",
        "name": "时间加权收益率（TWR）",
        "domain": "B2",
        "tags": _tags("派生", "比率", "期间", "越高越好", "诊断", "派生"),
        "unit": "ratio",
        "direction": "越高越好",
        "criterion": None,
        "as_of_semantics": "期间(至as_of)",
        "drill": {"target": "/analysis?section=returns", "label": "分析·双口径收益"},
    },
    {
        "id": "xirr",
        "name": "货币加权收益率（XIRR）",
        "domain": "B2",
        "tags": _tags("派生", "比率", "期间", "越高越好", "诊断", "派生"),
        "unit": "ratio",
        "direction": "越高越好",
        "criterion": None,
        "as_of_semantics": "期间(至as_of)",
        "drill": {"target": "/analysis?section=returns", "label": "分析·双口径收益"},
    },
    {
        "id": "liability_ratio",
        "name": "负债占比",
        "domain": "B3",
        "tags": _tags("派生", "比率", "时点", "越低越好", "展示", "派生"),
        "unit": "ratio",
        "direction": "越低越好",
        "criterion": None,
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/analysis?section=structure", "label": "分析·结构"},
    },
    {
        "id": "allocation_deviation",
        "name": "配置偏离",
        "domain": "C2",
        "tags": _tags("派生", "比率", "时点", "双阈值警戒", "引导", "参考区设定"),
        "unit": "pp",
        "direction": "双阈值警戒",
        "criterion": {"kind": "dual_threshold", "source": THRESHOLD_SOURCE},
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/analysis?section=allocation", "label": "分析·资产配置"},
    },
    {
        "id": "goal_progress",
        "name": "目标达成度",
        "domain": "C1",
        "tags": _tags("派生", "比率", "时点", "越高越好", "展示", "派生"),
        "unit": "ratio",
        "direction": "越高越好",
        "criterion": None,           # 本期无判据 → grade=null（三期-2 接手）
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/planning/goals", "label": "目标与达成度"},
    },
    {
        "id": "goal_gap",
        "name": "目标缺口",
        "domain": "C1",
        "tags": _tags("派生", "金额", "时点", "中性", "诊断", "派生"),
        "unit": "CNY",
        "direction": "中性",
        "criterion": None,
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/planning/goals", "label": "目标与达成度"},
    },
    {
        "id": "goal_required_annualized",
        "name": "所需年化",
        "domain": "C1",
        "tags": _tags("派生", "比率", "时点", "中性", "诊断", "派生"),
        "unit": "ratio",
        "direction": "中性",
        "criterion": {"kind": "dual_threshold_one_sided",
                      "source": "goal_versions.target_rate_annual"},
        "as_of_semantics": "时点(as-of)",
        "drill": {"target": "/planning/goals", "label": "目标与达成度"},
    },
]

_BY_ID = {m["id"]: m for m in _REGISTRY}
_DOMAIN_OF = {m["id"]: m["domain"] for m in _REGISTRY}


def _validate_registry() -> None:
    """启动自检：id 唯一、标签/方向枚举合法、判据类型合法且本期可实现。"""
    ids = [m["id"] for m in _REGISTRY]
    if len(ids) != len(set(ids)):
        raise ValueError("指标注册表 id 不唯一")
    for m in _REGISTRY:
        for key, allowed in _ENUMS.items():
            value = m["tags"].get(key)
            if value not in allowed:
                raise ValueError(f"指标 {m['id']} 标签 {key}={value} 不合法")
        if set(m["tags"]) != set(_ENUMS):
            raise ValueError(f"指标 {m['id']} 标签集不完整")
        if m["direction"] not in _ENUMS["direction"]:
            raise ValueError(f"指标 {m['id']} 方向 {m['direction']} 不合法")
        if m["direction"] != m["tags"]["direction"]:
            raise ValueError(f"指标 {m['id']} 方向与标签方向不一致")
        criterion = m["criterion"]
        if criterion is not None:
            if criterion.get("kind") not in _CRITERION_KINDS:
                raise ValueError(f"指标 {m['id']} 判据类型不合法")
            if criterion["kind"] not in _IMPLEMENTED_CRITERION_KINDS:
                raise ValueError(f"指标 {m['id']} 判据类型本期未实现")


_validate_registry()


# ------------------------------------------------------------------ 灯引擎


def evaluate_grade(value: Decimal | None, criterion: dict | None,
                   gates: list[dict]) -> dict | None:
    """灯引擎（纯函数）：(值, 判据, 闸门集合) → grade 字典 或 None。

    规则（spec §2.5）：阻断闸门 → gray（口径降级永不给绿）；无判据 → None；
    值不可得 → gray；按 kind 分支：`abs_threshold`（兼容旧调用）／
    `dual_threshold`（比 |value|）／`dual_threshold_one_sided`（比 value）。
    边界严格大于；band_basis 本期恒 current（ADR-0012 D8）。判灯载荷单点
    在 `band_service.grade`（与 `/goals/{id}/progress` 共用）。
    """
    kind = criterion.get("kind") if criterion is not None else None
    if kind is not None and kind not in _IMPLEMENTED_CRITERION_KINDS:
        raise ValueError(f"判据类型本期未实现：{kind}")

    band = None
    if criterion is not None:
        band = {
            "kind": kind,
            "threshold": str(Decimal(str(criterion["threshold"]))),
            "red": (str(Decimal(str(criterion["red"])))
                    if criterion.get("red") is not None else None),
            "source": criterion["source"],
            "components": criterion.get("components"),
        }
    return band_service.grade(
        value, band, gates, one_sided=(kind == band_service.KIND_C1))


# ------------------------------------------------------------------ 契约


def get_dictionary() -> MetricDictionary:
    """指标字典（静态、无 meta）。"""
    return MetricDictionary(items=[
        MetricDictionaryItem(
            id=m["id"], name=m["name"], domain=m["domain"],
            tags=MetricTags(**m["tags"]), unit=m["unit"],
            direction=m["direction"],
            criterion=MetricCriterion(**m["criterion"]) if m["criterion"] else None,
            as_of_semantics=m["as_of_semantics"],
            drill=MetricDrill(**m["drill"]) if m["drill"] else None,
        ) for m in _REGISTRY
    ])


def get_status(db: Session, as_of: date | None = None, *,
               include_details: bool = False) -> MetricStatus:
    """指标状态面（计算类、顶层 meta）：值＋口径闸门＋灯＋下钻＋B4 汇总。

    `include_details=False`（默认）时异动不取逐项明细（性能；spec §8.2）。
    """
    dates = analysis_service._get_all_snapshot_dates(db)
    effective = _resolve_as_of(dates, as_of)
    no_data = not dates
    # 升值/配置按「≤as_of 的最新快照日」锚定（as-of 取数机制；无数据则 None）
    anchor = max((d for d in dates if effective and d <= effective), default=None)

    no_data_gates = [{"code": NO_DATA, "note": "库内暂无快照数据，指标不可判",
                      "blocking": True}] if no_data else []

    if effective is None:
        total = assets = liabilities = ZERO
    else:
        total, assets, liabilities = analysis_service._sum_as_of(db, effective)

    # ---- B1 ----
    b1_gates = list(no_data_gates)
    change_pct = None
    if anchor is not None:
        prior = [d for d in dates if d < anchor]
        if prior:
            prev_total, _, _ = analysis_service._sum_as_of(db, prior[-1])
            if prev_total != ZERO:
                change_pct = (total - prev_total) / prev_total

    candidates: list[tuple[str, Decimal | None, list[dict], dict | None, dict | None]] = [
        ("asset_total", assets, b1_gates, None, None),
        ("liability_total", liabilities, b1_gates, None, None),
        ("net_worth", total, b1_gates, None, None),
        ("net_worth_change_pct", change_pct, b1_gates, None, None),
    ]

    # ---- B2：升值（复用 appreciation_service）----
    appr = appreciation_service.get_appreciation(db, anchor)
    appr_portfolio = appr["portfolio"]
    appr_missing = appr_portfolio["items_missing_principal"]
    appr_basis_total = Decimal(appr_portfolio["basis_total"])
    appr_market_total = Decimal(appr_portfolio["market_total"])

    b2_gates = list(no_data_gates)
    appr_amount_gates = list(b2_gates)
    if appr_missing and not no_data:
        # 组合不可得（全部市值项均无口径）时阻断；否则仅注记
        blocking = appr_basis_total <= ZERO and appr_market_total > ZERO
        appr_amount_gates.append({
            "code": PRINCIPAL_NOT_SET,
            "note": (f"{len(appr_missing)} 个资产项未登记本金口径"
                     + ("，组合升值不可得" if blocking else "，组合升值仅含有口径项")),
            "blocking": blocking,
        })
    appr_amount = (Decimal(appr_portfolio["appreciation_total"])
                   if appr_portfolio["appreciation_total"] is not None else None)
    candidates.append(("appreciation_amount", appr_amount, appr_amount_gates, None, None))

    appr_rate_value = (Decimal(appr_portfolio["rate"])
                       if appr_portfolio["rate"] is not None else None)
    appr_rate_gates = list(b2_gates)
    if appr_rate_value is None and not no_data:
        appr_rate_gates.append({
            "code": RATE_UNAVAILABLE,
            "note": "未登记本金口径或口径 ≤0，组合升值率不可得",
            "blocking": True,
        })
    candidates.append(("appreciation_rate", appr_rate_value, appr_rate_gates, None, None))

    # ---- B2：双口径收益（复用 analysis_service.get_returns）----
    returns = analysis_service.get_returns(db, date_to=anchor)
    returns_degr = returns.get("meta", {}).get("degradations", [])
    twr_value = returns["twr"]["period"]["period_return"]
    twr_value = Decimal(twr_value) if twr_value is not None else None
    xirr_value = (Decimal(returns["xirr"]) if returns["xirr"] is not None else None)

    twr_gates = list(b2_gates)
    if twr_value is None and not no_data:
        twr_gates.append({
            "code": RATE_UNAVAILABLE,
            "note": _degradation_message(returns_degr, RATE_UNAVAILABLE, "TWR")
                    or "TWR 率不可得",
            "blocking": True,
        })
    for degr in returns_degr:
        if degr.get("code") == TWR_LARGE_FLOW_APPROX:
            twr_gates.append({"code": TWR_LARGE_FLOW_APPROX,
                              "note": degr.get("message", "TWR 为 MD 近似"),
                              "blocking": False})
    candidates.append(("twr", twr_value, twr_gates, None, None))

    xirr_gates = list(b2_gates)
    if xirr_value is None and not no_data:
        xirr_gates.append({
            "code": RATE_UNAVAILABLE,
            "note": _degradation_message(returns_degr, RATE_UNAVAILABLE, "XIRR")
                    or "XIRR 率不可得",
            "blocking": True,
        })
    candidates.append(("xirr", xirr_value, xirr_gates, None, None))

    # ---- B3：负债占比 ----
    liability_gates = list(no_data_gates)
    liability_ratio = None
    if assets > ZERO:
        liability_ratio = liabilities / assets
    elif not no_data:
        liability_gates.append({
            "code": RATE_UNAVAILABLE,
            "note": "总资产为 0，负债占比不可得",
            "blocking": True,
        })
    candidates.append(("liability_ratio", liability_ratio, liability_gates, None, None))

    # ---- C2：配置偏离（逐目标行；双阈值，ADR-0012 D2）----
    allocation_rows = _deviation_rows(db, anchor) if anchor else []
    if allocation_rows or no_data:
        dev_gates = list(no_data_gates)
        worst = None
        if assets <= ZERO and not no_data:
            # 总资产=0：目标全 -100pp 会造出全红假信号 → 值置 null + 阻断闸门
            dev_gates.append({
                "code": RATE_UNAVAILABLE,
                "note": "总资产为 0，配置偏离不可得",
                "blocking": True,
            })
            for row in allocation_rows:
                row["state"] = "gray"
                if row.get("band"):
                    row["band"]["state"] = "gray"
        else:
            worst = _worst_deviation_row(allocation_rows)
        dev_value = Decimal(worst["deviation"]) if worst else None
        dev_criterion = _row_criterion(worst) if worst else None
        dev_details = {"rows": allocation_rows} if allocation_rows else None
        candidates.append(("allocation_deviation", dev_value, dev_gates,
                           dev_criterion, dev_details))

    # ---- C1：目标达成度（主目标→回退最近激活；无目标→不出现）----
    goal_degr: list[dict] = []
    target = goal_service.resolve_target(db, effective)
    if target is not None:
        goal, version = target
        prog = goal_service.progress_payload(
            db, goal, version, effective, net_worth=total, dates=dates)
        details = goal_service.metric_details(prog)
        # 率不可得只作用于「所需年化」；NO_DATA/沿用值三项共享
        note_gates = [g for g in prog["gates"] if g["code"] != RATE_UNAVAILABLE]
        rate_gates = note_gates + [g for g in prog["gates"]
                                   if g["code"] == RATE_UNAVAILABLE]
        progress_value = (Decimal(prog["progress"])
                          if prog["progress"] is not None else None)
        gap_value = Decimal(prog["gap"]) if prog["gap"] is not None else None
        required_value = (Decimal(prog["required_annualized"])
                          if prog["required_annualized"] is not None else None)
        candidates.append(("goal_progress", progress_value, note_gates, None,
                           details))
        candidates.append(("goal_gap", gap_value, note_gates, None, details))
        goal_band = band_service.resolve_goal_band(version)
        goal_criterion = None
        if goal_band is not None:
            goal_criterion = {
                "kind": band_service.KIND_C1,
                "threshold": goal_band["threshold"],
                "red": goal_band["red"],
                "source": goal_band["source"],
                "components": goal_band["components"],
            }
        candidates.append(("goal_required_annualized", required_value,
                           rate_gates, goal_criterion, details))
        goal_degr = goal_service.degradations_of(prog["gates"])

    # ---- 组装 ----
    items: list[MetricStatusItem] = []
    for metric_id, value, gates, criterion, details in candidates:
        entry = _BY_ID[metric_id]
        blocking = any(g["blocking"] for g in gates)
        grade = evaluate_grade(value, criterion, gates)
        if (grade and grade.get("band") and as_of is not None and dates
                and effective != dates[-1]):
            # as_of 只移动取值锚点，不移动判据尺（ADR-0012 D8）
            grade["note"] = (grade.get("note") or "") + "（判据为当期）"
        rendered = None if blocking else _format_value(metric_id, value)
        tags = _status_tags(entry, gates)
        items.append(MetricStatusItem(
            id=entry["id"], value=rendered, unit=entry["unit"],
            direction=entry["direction"],
            grade=MetricGrade(**grade) if grade else None,
            gates=[MetricGate(**g) for g in gates],
            tags=tags, details=details,
            drill=MetricDrill(**entry["drill"]) if entry["drill"] else None,
        ))

    meta = build_meta(db, effective, _meta_degradations(
        no_data, appr_missing, appr_rate_value,
        assets, returns_degr, goal_degr,
        allocation_unavailable=(assets <= ZERO and not no_data
                                and bool(allocation_rows))),
        principal=PRINCIPAL_BASIS_VERSION)

    # ---- B4：域级汇总＋异动（加性；不新增 item、不改字典）----
    events = signal_service.collect_events(db, effective,
                                           include_details=include_details)
    domains = signal_service.build_domains(items, _DOMAIN_OF, events)
    summary = signal_service.summarize(items, domains, events)
    return MetricStatus(meta=meta, items=items, domains=domains,
                        events=events, summary=summary)


# ------------------------------------------------------------------ 内部


def _resolve_as_of(dates: list[date], as_of: date | None) -> date | None:
    """默认＝最新快照日；as_of 越出快照日期范围 → 422（单点：goal_service）。"""
    return goal_service.resolve_as_of(dates, as_of)


def _format_value(metric_id: str, value: Decimal | None) -> str | None:
    if value is None:
        return None
    entry = _BY_ID[metric_id]
    quantum = MONEY_Q if entry["unit"] == "CNY" else RATE_Q
    return str(Decimal(value).quantize(quantum))


def _degradation_message(degr: list[dict], code: str, prefix: str) -> str | None:
    for d in degr:
        if d.get("code") == code and str(d.get("message", "")).startswith(prefix):
            return d["message"]
    return None


def _status_tags(entry: dict, gates: list[dict]) -> MetricTags:
    """信赖度承接降级：阻断→率不可得/未校验，沿用→含沿用值。"""
    tags = dict(entry["tags"])
    codes = [g["code"] for g in gates]
    blocking = any(g["blocking"] for g in gates)
    if blocking and RATE_UNAVAILABLE in codes:
        tags["trust"] = "率不可得"
    elif DATA_MISSING_CARRY_FORWARD in codes:
        tags["trust"] = "含沿用值"
    elif blocking:
        tags["trust"] = "未校验"
    return MetricTags(**tags)


def _meta_degradations(no_data: bool,
                       appr_missing: list[str], appr_rate: Decimal | None,
                       assets: Decimal, returns_degr: list[dict],
                       goal_degr: list[dict] | None = None,
                       allocation_unavailable: bool = False) -> list[dict]:
    """状态面顶层 meta：汇总本次计算实际发生的降级（去重、保序）。"""
    out: list[dict] = []
    if no_data:
        out.append({"code": NO_DATA, "scope": None,
                    "message": "库内暂无快照数据，指标不可判"})
    if appr_missing:
        out.append({"code": PRINCIPAL_NOT_SET,
                    "scope": ",".join(appr_missing),
                    "message": f"{len(appr_missing)} 个资产项未登记本金口径"})
    if appr_rate is None and not no_data:
        out.append({"code": RATE_UNAVAILABLE, "scope": "appreciation_rate",
                    "message": "组合升值率不可得"})
    if assets <= ZERO and not no_data:
        out.append({"code": RATE_UNAVAILABLE, "scope": "liability_ratio",
                    "message": "总资产为 0，负债占比不可得"})
    if allocation_unavailable:
        out.append({"code": RATE_UNAVAILABLE, "scope": "allocation_deviation",
                    "message": "总资产为 0，配置偏离不可得"})
    out.extend(returns_degr)
    out.extend(goal_degr or [])
    seen: set[tuple] = set()
    unique: list[dict] = []
    for d in out:
        key = (d.get("code"), d.get("scope"), d.get("message"))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _deviation_rows(db: Session, anchor: date) -> list[dict]:
    """逐目标行的配置偏离（复用 allocation_service 的带型；无目标→无行）。

    `band` 由 `allocation_service`（→ `band_service`）单点解析，禁止就地比较。
    总资产=0 时 `analyze_allocation` 仍出行，但 `get_status` 会将其置 gray 并阻断。
    """
    rows: list[dict] = []
    # 维度名跟随 `allocation_service.SUPPORTED_DIMENSIONS`（ADR-0019 新词汇：
    # asset_class / liquidity / institution / channel / risk），不在此处另写一份，
    # 否则又会出现「一处改了、另一处还按旧值查」的分叉。
    for dimension in allocation_service.SUPPORTED_DIMENSIONS:
        analysis = allocation_service.analyze_allocation(db, dimension, anchor)
        for item in _walk_allocation(analysis.items):
            if item.target_percent is None:
                continue
            band = item.band
            row = {
                "dimension": dimension,
                "target_id": item.id,
                "name": item.name,
                "target_percent": str(item.target_percent),
                "actual_percent": str(item.actual_percent),
                "deviation": str(Decimal(item.deviation).quantize(RATE_Q)),
                "deviation_percent": (str(Decimal(item.deviation_percent))
                                      if item.deviation_percent is not None else None),
                "deviation_amount": (str(Decimal(item.deviation_amount))
                                     if item.deviation_amount is not None else None),
                "alert_threshold": str(item.alert_threshold),
                "state": band.state if band is not None else None,
            }
            if band is not None:
                row["band"] = band.model_dump()
            rows.append(row)
    return rows


def _worst_deviation_row(rows: list[dict]) -> dict | None:
    """最差行：先按严重度（red>yellow>green），再 |dev| 降序，最后 dimension/target_id。"""
    if not rows:
        return None
    ranked = sorted(
        rows,
        key=lambda r: (
            -band_service.severity_rank(r.get("state")),
            -abs(Decimal(r["deviation"])),
            r["dimension"],
            str(r["target_id"]),
        ),
    )
    return ranked[0]


def _row_criterion(row: dict) -> dict | None:
    band = row.get("band")
    if not band:
        return None
    return {
        "kind": band_service.KIND_C2,
        "threshold": band["yellow"],
        "red": band["red"],
        "source": band["source"],
        "components": band.get("components"),
    }


def _walk_allocation(items):
    for item in items:
        yield item
        yield from _walk_allocation(item.children)