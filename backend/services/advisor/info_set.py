"""信息集冻结（R-26；spec §2.2/§5.1）：封闭键空间＋payload_sha256＋write-once。

本模块顶层只有纯函数（无 DB 依赖），`freeze_info_set/load_info_set` 内延迟
import models/audit_service，使 `gate.py` 复用规范化逻辑时保持纯函数。

键空间（`builder_version=advisor.info_set.v1`，只由版本定义）：
    meta.anchor_date / meta.schema_generation
    nw.total / nw.assets / nw.liabilities / nw.change_pct
    appreciation.total / appreciation.pct / appreciation.items_missing_principal
    returns.twr / returns.xirr
    goal.version_no / goal.target_amount / goal.progress / goal.gap /
    goal.required_annualized / goal.remaining_months / goal.grade_state
    alloc.<dim>.<target_id>.actual_pct / .target_pct / .deviation_pp /
        .deviation_amount / .band / .recommendation
    signal.<domain>.state / signal.event.<rule_id>.<period>.severity
"""
import hashlib
import json
from datetime import date
from decimal import Decimal

BUILDER_VERSION = "advisor.info_set.v1"
MAX_ENTRIES = 40

ENTRY_FIELDS = ("key", "kind", "label", "value", "value_scaled", "unit",
                "trust", "source", "source_ref", "as_of")

MONEY_Q = Decimal("0.01")
SCALED_Q = Decimal("0.1")
RATE_Q = Decimal("0.0001")
PP_Q = Decimal("0.01")


def _text(value) -> str | None:
    return None if value is None else str(value)


def canonical_entries(entries: list[dict]) -> str:
    """规范化串：按 key 升序、固定字段序、紧凑 JSON、不转义中文。"""
    payload = [
        {f: _text(entry.get(f)) for f in ENTRY_FIELDS}
        for entry in sorted(entries, key=lambda e: e["key"])
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def payload_sha256(entries: list[dict]) -> str:
    return hashlib.sha256(canonical_entries(entries).encode("utf-8")).hexdigest()


def _entry(key, kind, label, value, unit, source, *, trust="measured",
           source_ref=None, as_of=None) -> dict:
    value_scaled = None
    if kind == "money":
        amount = Decimal(str(value))
        value_text = str(amount.quantize(MONEY_Q))
        value_scaled = str((amount / Decimal("10000")).quantize(SCALED_Q))
    elif kind == "ratio":
        value_text = str(Decimal(str(value)).quantize(RATE_Q))
    elif kind == "pp":
        value_text = str(Decimal(str(value)).quantize(PP_Q))
    elif kind == "count":
        value_text = str(int(value))
    elif kind == "date":
        value_text = value.isoformat() if isinstance(value, date) else str(value)
    else:  # state / text
        value_text = str(value)
    return {
        "key": key, "kind": kind, "label": label, "value": value_text,
        "value_scaled": value_scaled, "unit": unit, "trust": trust,
        "source": source, "source_ref": source_ref,
        "as_of": (as_of.isoformat() if isinstance(as_of, date) else as_of),
    }


#: 配置行通用类目名（ADR-0019 词汇；**不含**机构名/账户名/持仓名——隔离纪律）。
#: 旧维度名（fund_type / liquidity_rating / account）已随分类重构下线，留旧名等于
#: 让两套词汇长期并存。
_ALLOC_LABELS = {
    "liquidity": "流动性档",
    "institution": "机构·档位",
    "channel": "渠道·档位",
    "risk_level": "风险等级·档位",
}


def _alloc_label(dimension: str, name: str | None) -> str:
    if dimension == "asset_class":
        return f"资产类别·{name}"          # 类别本身是通用类目，可带名
    return _ALLOC_LABELS.get(dimension, "配置·档位")


def _alloc_key(dimension: str, target_id: str, suffix: str) -> str:
    safe = str(target_id).replace(".", "_").replace(" ", "_")
    return f"alloc.{dimension}.{safe}.{suffix}"


def build_info_set(read, *, as_of=None, goal_id=None,
                   trigger_kind: str = "manual") -> dict:
    """构造信息集（不落库）。无数据时 `as_of=None`、条目仅元信息。"""
    anchor = read.anchor(as_of)
    entries: list[dict] = []

    if anchor is not None:
        entries.append(_entry("meta.anchor_date", "date", "锚点日", anchor,
                              "state", "metric", as_of=anchor))
    entries.append(_entry("meta.schema_generation", "count", "数据世代",
                          read.schema_generation(), "state", "metric",
                          as_of=anchor))

    nw = read.net_worth(anchor)
    if nw.get("total") is not None:
        entries.extend([
            _entry("nw.total", "money", "净资产", nw["total"], "CNY", "ledger",
                   as_of=anchor),
            _entry("nw.assets", "money", "总资产", nw["assets"], "CNY", "ledger",
                   as_of=anchor),
            _entry("nw.liabilities", "money", "总负债", nw["liabilities"], "CNY",
                   "ledger", as_of=anchor),
        ])
    if nw.get("change_pct") is not None:
        entries.append(_entry("nw.change_pct", "ratio", "净资产环比",
                              nw["change_pct"], "ratio", "ledger", as_of=anchor))

    # 覆盖度三条（coverage.ratio / .partial / .carried_max_days）已随 ADR-0019 D12
    # 整体下线：分母（`attributes.remind_enabled`）已随 0026 迁移消失，「该日没有某项
    # 的记录」＝值没变（变更日志语义），不再存在可判定的「部分录入」。确定性门里与之
    # 配套的 `GATE_DATA_PARTIAL` 同批删除（恒通过的闸门＝假承诺）。
    # 沿用事实改由趋势点 `carried_items` / `carried_max_days` 对外暴露。
    appreciation = read.appreciation(anchor)
    if appreciation.get("total") is not None:
        entries.append(_entry("appreciation.total", "money", "升值额",
                              appreciation["total"], "CNY", "principal",
                              as_of=anchor))
    if appreciation.get("pct") is not None:
        entries.append(_entry("appreciation.pct", "ratio", "升值率",
                              appreciation["pct"], "ratio", "principal",
                              as_of=anchor))
    entries.append(_entry("appreciation.items_missing_principal", "count",
                          "未登记口径项数",
                          appreciation.get("items_missing_principal", 0),
                          "state", "principal", as_of=anchor))

    returns = read.returns(anchor)
    for key, label in (("twr", "TWR"), ("xirr", "XIRR")):
        if returns.get(key) is not None:
            entries.append(_entry(f"returns.{key}", "ratio", label,
                                  returns[key], "ratio", "metric", as_of=anchor))

    goal = read.goal_progress(anchor, goal_id=goal_id)
    if goal is not None:
        ref = f"goal:{goal['goal_id']}"
        entries.extend([
            _entry("goal.version_no", "count", "目标版本号",
                   goal["version_no"], "state", "goal", source_ref=ref,
                   as_of=anchor),
            _entry("goal.target_amount", "money", "目标金额",
                   goal["target_amount"], "CNY", "goal", source_ref=ref,
                   as_of=anchor),
            _entry("goal.gap", "money", "目标缺口", goal["gap"], "CNY", "goal",
                   source_ref=ref, as_of=anchor),
        ])
        for key, kind, label, unit in (
                ("progress", "ratio", "目标达成度", "ratio"),
                ("required_annualized", "ratio", "所需年化", "ratio")):
            if goal.get(key) is not None:
                entries.append(_entry(f"goal.{key}", kind, label, goal[key],
                                      unit, "goal", source_ref=ref,
                                      as_of=anchor))
        if goal.get("remaining_months") is not None:
            entries.append(_entry("goal.remaining_months", "count", "剩余月数",
                                  goal["remaining_months"], "days", "goal",
                                  source_ref=ref, as_of=anchor))
        if goal.get("grade_state") is not None:
            entries.append(_entry("goal.grade_state", "state", "目标灯",
                                  goal["grade_state"], "state", "goal",
                                  source_ref=ref, as_of=anchor))

    for row in read.allocation_rows(anchor):
        dimension = row["dimension"]
        target = row["target_id"]
        label = _alloc_label(dimension, row.get("name"))
        ref = f"attr:{dimension}:{target}"
        entries.extend([
            _entry(_alloc_key(dimension, target, "actual_pct"), "ratio", label,
                   row["actual_pct"], "ratio", "allocation", source_ref=ref,
                   as_of=anchor),
            _entry(_alloc_key(dimension, target, "target_pct"), "ratio", label,
                   row["target_pct"], "ratio", "allocation", source_ref=ref,
                   as_of=anchor),
            _entry(_alloc_key(dimension, target, "deviation_pp"), "pp", label,
                   row["deviation_pp"], "pp", "allocation", source_ref=ref,
                   as_of=anchor),
        ])
        if row.get("deviation_amount") is not None:
            entries.append(_entry(
                _alloc_key(dimension, target, "deviation_amount"), "money", label,
                row["deviation_amount"], "CNY", "allocation", source_ref=ref,
                as_of=anchor))
        if row.get("band") is not None:
            entries.append(_entry(
                _alloc_key(dimension, target, "band"), "state", label,
                row["band"], "state", "allocation", source_ref=ref,
                as_of=anchor))
        if row.get("recommendation"):
            entries.append(_entry(
                _alloc_key(dimension, target, "recommendation"), "text", label,
                row["recommendation"], "state", "allocation", source_ref=ref,
                as_of=anchor))

    signals = read.signals(anchor)
    for domain in signals.get("domains") or []:
        entries.append(_entry(f"signal.{domain['domain']}.state", "state",
                              f"域灯·{domain['domain']}", domain["state"],
                              "state", "signal",
                              source_ref=f"domain:{domain['domain']}",
                              as_of=anchor))
    for event in signals.get("events") or []:
        entries.append(_entry(
            f"signal.event.{event['rule_id']}.{event['period']}.severity",
            "state", f"异动·{event['rule_id']}", event["severity"], "state",
            "signal", source_ref=f"rule:{event['rule_id']}",
            as_of=anchor))

    entries = entries[:MAX_ENTRIES]
    return {
        "as_of": anchor,
        "goal_id": goal["goal_id"] if goal else None,
        "goal_version_id": goal["goal_version_id"] if goal else None,
        "builder_version": BUILDER_VERSION,
        "entry_count": len(entries),
        "payload_sha256": payload_sha256(entries),
        "entries": entries,
        "meta": {"schema_generation": read.schema_generation(),
                 "degradations": [], "trigger_kind": trigger_kind},
    }


def freeze_info_set(db, read, *, as_of=None, goal_id=None,
                    trigger_kind: str = "manual",
                    created_at: str | None = None) -> dict:
    """冻结并落库（write-once）＋审计 `advisor_info_set/freeze`（同事务）。"""
    from models import AdvisorInfoEntry, AdvisorInfoSet
    from services import audit_service
    from services.audit_service import make_change

    data = build_info_set(read, as_of=as_of, goal_id=goal_id,
                          trigger_kind=trigger_kind)
    if data["as_of"] is None:
        raise ValueError("no_data: 库内暂无快照数据，无法冻结信息集")

    from datetime import datetime
    stamp = created_at or datetime.now().isoformat()
    row = AdvisorInfoSet(
        created_at=stamp, as_of=data["as_of"], goal_id=data["goal_id"],
        goal_version_id=data["goal_version_id"],
        builder_version=data["builder_version"],
        entry_count=data["entry_count"],
        payload_sha256=data["payload_sha256"],
        meta_json=json.dumps(data["meta"], ensure_ascii=False))
    db.add(row)
    db.flush()
    for entry in data["entries"]:
        db.add(AdvisorInfoEntry(
            info_set_id=row.id, key=entry["key"], kind=entry["kind"],
            label=entry["label"], value=entry["value"],
            value_scaled=entry["value_scaled"], unit=entry["unit"],
            trust=entry["trust"], source=entry["source"],
            source_ref=entry["source_ref"],
            as_of=(date.fromisoformat(entry["as_of"])
                   if entry["as_of"] else None)))
    db.flush()
    audit_service.record(db, make_change(
        "advisor_info_set", row.id, "freeze", None,
        {"as_of": (data["as_of"].isoformat() if data["as_of"] else None),
         "entry_count": data["entry_count"],
         "payload_sha256": data["payload_sha256"],
         "builder_version": data["builder_version"]}), actor="system")
    db.commit()
    db.refresh(row)
    return {**data, "id": row.id, "created_at": row.created_at}


def load_info_set(db, info_set_id: int) -> dict | None:
    """读信息集＋条目；`payload_ok` 为重算结果（损坏由调用方 409）。"""
    from models import AdvisorInfoEntry, AdvisorInfoSet

    row = db.get(AdvisorInfoSet, info_set_id)
    if row is None:
        return None
    entries = list(db.query(AdvisorInfoEntry)
                   .filter(AdvisorInfoEntry.info_set_id == info_set_id)
                   .order_by(AdvisorInfoEntry.key).all())
    payload = [{
        "key": e.key, "kind": e.kind, "label": e.label, "value": e.value,
        "value_scaled": e.value_scaled, "unit": e.unit, "trust": e.trust,
        "source": e.source, "source_ref": e.source_ref,
        "as_of": e.as_of.isoformat() if e.as_of else None,
    } for e in entries]
    ok = (len(payload) == row.entry_count
          and payload_sha256(payload) == row.payload_sha256)
    return {
        "id": row.id, "created_at": row.created_at,
        "as_of": row.as_of, "goal_id": row.goal_id,
        "goal_version_id": row.goal_version_id,
        "builder_version": row.builder_version,
        "entry_count": row.entry_count, "payload_sha256": row.payload_sha256,
        "meta": json.loads(row.meta_json) if row.meta_json else None,
        "entries": payload, "payload_ok": ok,
    }