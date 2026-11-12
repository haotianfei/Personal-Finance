"""确定性门（R-25；spec §2.4/§5.2/§5.4）：9 项纯函数检查（8 blocking + 1 warning）。

输入 `(suggestion_json, info_set)`；**无 DB、无网络、无 LLM 客户端**。
门失败 = 终态（无人工放行通道）。输出：
    {result: passed|failed, checks: [{code,severity,passed,detail}],
     checked_at, gate_version}
`result=failed` ⇔ 任一 blocking 未过（warning 只注记）。

原第 10 项 `GATE_DATA_PARTIAL`（覆盖度闸门）已随 ADR-0019 D12 删除：`data_coverage`
下线后信息集不再产出 `coverage.ratio` / `coverage.partial`，该检查对任何**真实**
信息集都不可能失败，留着是假承诺。
"""
import json
import re
from datetime import date, datetime
from decimal import Decimal

from services.advisor.info_set import payload_sha256

GATE_VERSION = "advisor.gate.v1"
FRESHNESS_DAYS = 45

GATE_SCHEMA = "GATE_SCHEMA"
GATE_INFO_SET_SEALED = "GATE_INFO_SET_SEALED"
GATE_REF_EXISTS = "GATE_REF_EXISTS"
GATE_NUMERIC_PROVENANCE = "GATE_NUMERIC_PROVENANCE"
GATE_PLACEHOLDER_RESOLVES = "GATE_PLACEHOLDER_RESOLVES"
GATE_GOAL_ANCHOR = "GATE_GOAL_ANCHOR"
GATE_FRESHNESS = "GATE_FRESHNESS"
GATE_NO_EXECUTION = "GATE_NO_EXECUTION"
GATE_THRESHOLD_SOURCE = "GATE_THRESHOLD_SOURCE"

BLOCKING = (GATE_SCHEMA, GATE_INFO_SET_SEALED, GATE_REF_EXISTS,
            GATE_NUMERIC_PROVENANCE, GATE_PLACEHOLDER_RESOLVES,
            GATE_GOAL_ANCHOR, GATE_FRESHNESS, GATE_NO_EXECUTION)
WARNING = (GATE_THRESHOLD_SOURCE,)

SUBJECT_KINDS = ("attribute", "goal", "threshold", "narrative")
DIRECTIONS = ("increase", "decrease", "hold", "rebalance")
_EXECUTION_WORDS = ("下单", "转账", "代扣", "自动执行", "委托买入", "委托卖出",
                    "代为交易", "自动划转", "自动买入", "自动卖出")

_TOP_FIELDS = {"title", "summary_template", "items", "goal_version_id"}
_ITEM_FIELDS = {"seq", "subject_kind", "subject_ref", "direction",
                "rationale_template", "info_refs"}
_PLACEHOLDER_RE = re.compile(r"\{\{info\.([A-Za-z0-9_.\-\u4e00-\u9fff]+)\}\}")
_WHITELIST_RES = (
    re.compile(r"T\+\d+"),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    re.compile(r"第[0-9A-Za-z一二三四五六七八九十百千]+[步期月季年周天次档层]?"),
)
_MAX_RATIONALE = 400


def _check(code: str, passed: bool, detail: str | None = None) -> dict:
    return {"code": code,
            "severity": "blocking" if code in BLOCKING else "warning",
            "passed": bool(passed), "detail": detail}


def _entries_map(info_set) -> dict[str, dict]:
    entries = (info_set or {}).get("entries") or []
    if isinstance(entries, dict):
        return entries
    return {e["key"]: e for e in entries}


def _templates(suggestion: dict) -> list[str]:
    texts = [suggestion.get("summary_template") or ""]
    texts.append(suggestion.get("title") or "")
    for item in suggestion.get("items") or []:
        if isinstance(item, dict):
            texts.append(item.get("rationale_template") or "")
    return texts


def _placeholders(text: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(text or "")


def _strip_whitelist(text: str) -> str:
    text = _PLACEHOLDER_RE.sub("", text or "")
    for pattern in _WHITELIST_RES:
        text = pattern.sub("", text)
    return text


def format_value(entry: dict) -> str:
    """占位符渲染（权威条目值）：金额 2 位＋千分位；比率 2 位＋unit 后缀。"""
    if entry is None or entry.get("value") is None:
        return "—"
    kind = entry.get("kind")
    unit = entry.get("unit")
    value = entry["value"]
    if kind == "money":
        return f"{Decimal(value):,.2f}"
    if kind in ("ratio", "pp"):
        if unit == "ratio":
            return f"{Decimal(value) * 100:,.2f}%"
        if unit == "pp":
            return f"{Decimal(value):,.2f}pp"
        return f"{Decimal(value):,.2f}"
    return str(value)


def render_template(template: str, info_set) -> str:
    """按冻结条目插值；未知 key → ValueError（门应先拦）。"""
    entries = _entries_map(info_set)

    def _replace(match):
        key = match.group(1)
        if key not in entries:
            raise ValueError(f"占位符未解析: {key}")
        return format_value(entries[key])

    return _PLACEHOLDER_RE.sub(_replace, template or "")


def run_gate(suggestion_json, info_set, *, today: date | None = None,
             active_goal_version_id: int | None = None,
             checked_at: str | None = None) -> dict:
    """确定性门主入口（纯函数；一次返回全部 9 项检查）。"""
    parsed, parse_error = _parse(suggestion_json)
    suggestion = parsed if isinstance(parsed, dict) else {}
    entries = _entries_map(info_set)
    templates = _templates(suggestion)
    placeholders = [key for text in templates for key in _placeholders(text)]

    checks = [
        _schema_check(parsed, parse_error),
        _sealed_check(info_set, entries),
        _ref_check(suggestion, entries),
        _numeric_check(templates),
        _placeholder_check(placeholders, entries, templates, info_set),
        _goal_anchor_check(suggestion, info_set, active_goal_version_id),
        _freshness_check(info_set, today),
        _execution_check(suggestion),
        _threshold_source_check(suggestion, entries),
    ]
    failed = any(c["severity"] == "blocking" and not c["passed"] for c in checks)
    return {
        "result": "failed" if failed else "passed",
        "checks": checks,
        "checked_at": checked_at or datetime.now().isoformat(),
        "gate_version": GATE_VERSION,
    }


# ------------------------------------------------------------------ checks


def _parse(value):
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            return json.loads(value), None
        except (ValueError, TypeError):
            return None, "输出不是合法 JSON"
    return None, "输出不是 JSON 对象"


def _schema_check(parsed, parse_error) -> dict:
    if not isinstance(parsed, dict):
        return _check(GATE_SCHEMA, False, parse_error or "输出不是 JSON 对象")
    unknown = set(parsed) - _TOP_FIELDS
    if unknown:
        return _check(GATE_SCHEMA, False, f"未知字段: {sorted(unknown)}")
    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        return _check(GATE_SCHEMA, False, "title 为空")
    summary = parsed.get("summary_template")
    if not isinstance(summary, str) or not summary.strip():
        return _check(GATE_SCHEMA, False, "summary_template 为空")
    items = parsed.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 5:
        return _check(GATE_SCHEMA, False, "items 须为 1~5 项")
    seen_seq = set()
    for item in items:
        if not isinstance(item, dict):
            return _check(GATE_SCHEMA, False, "item 不是对象")
        unknown = set(item) - _ITEM_FIELDS
        if unknown:
            return _check(GATE_SCHEMA, False, f"item 未知字段: {sorted(unknown)}")
        seq = item.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq in seen_seq:
            return _check(GATE_SCHEMA, False, "seq 非法或重复")
        seen_seq.add(seq)
        if item.get("subject_kind") not in SUBJECT_KINDS:
            return _check(GATE_SCHEMA, False, "subject_kind 非法")
        if item.get("direction") not in DIRECTIONS:
            return _check(GATE_SCHEMA, False, "direction 非法")
        rationale = item.get("rationale_template")
        if not isinstance(rationale, str) or not rationale.strip():
            return _check(GATE_SCHEMA, False, "rationale_template 为空")
        if len(rationale) > _MAX_RATIONALE:
            return _check(GATE_SCHEMA, False, "rationale 超过 400 字")
        refs = item.get("info_refs")
        if not isinstance(refs, list) or not refs or \
                not all(isinstance(r, str) and r for r in refs):
            return _check(GATE_SCHEMA, False, "info_refs 须为非空 key 数组")
    goal_version = parsed.get("goal_version_id")
    if goal_version is not None and not isinstance(goal_version, int):
        return _check(GATE_SCHEMA, False, "goal_version_id 须为整数")
    return _check(GATE_SCHEMA, True, "结构合法")


def _sealed_check(info_set, entries) -> dict:
    if not info_set:
        return _check(GATE_INFO_SET_SEALED, False, "信息集缺失")
    entry_count = info_set.get("entry_count")
    declared = info_set.get("payload_sha256")
    if entry_count is None or declared is None:
        return _check(GATE_INFO_SET_SEALED, False, "信息集头缺 entry_count/payload_sha256")
    payload = list(entries.values())
    if len(payload) != entry_count:
        return _check(GATE_INFO_SET_SEALED, False,
                      f"条目数不符: {len(payload)}≠{entry_count}")
    actual = payload_sha256(payload)
    if actual != declared:
        return _check(GATE_INFO_SET_SEALED, False, "payload_sha256 不一致")
    return _check(GATE_INFO_SET_SEALED, True, "封存一致")


def _ref_check(suggestion, entries) -> dict:
    missing = []
    for item in suggestion.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in item.get("info_refs") or []:
            if key not in entries:
                missing.append(key)
    if missing:
        return _check(GATE_REF_EXISTS, False, f"不存在 key: {sorted(set(missing))}")
    return _check(GATE_REF_EXISTS, True, "引用均可解析")


def _numeric_check(templates) -> dict:
    for text in templates:
        stripped = _strip_whitelist(text)
        if re.search(r"\d|%", stripped):
            return _check(GATE_NUMERIC_PROVENANCE, False,
                          "模板含数字字面量（应使用占位符）")
    return _check(GATE_NUMERIC_PROVENANCE, True, "无数字字面量")


def _placeholder_check(placeholders, entries, templates, info_set) -> dict:
    unknown = [k for k in placeholders if k not in entries]
    if unknown:
        return _check(GATE_PLACEHOLDER_RESOLVES, False,
                      f"占位符不存在: {sorted(set(unknown))}")
    bad_kind = [k for k in placeholders
                if entries[k].get("kind") not in
                ("money", "ratio", "pp", "count", "date", "state", "text")]
    if bad_kind:
        return _check(GATE_PLACEHOLDER_RESOLVES, False, "占位符 kind 非法")
    try:
        rendered = [render_template(text, info_set) for text in templates]
    except ValueError as exc:
        return _check(GATE_PLACEHOLDER_RESOLVES, False, str(exc))
    if any("{{" in text for text in rendered):
        return _check(GATE_PLACEHOLDER_RESOLVES, False, "渲染后仍残留占位符")
    return _check(GATE_PLACEHOLDER_RESOLVES, True, "占位符全部解析")


def _goal_anchor_check(suggestion, info_set, active_goal_version_id) -> dict:
    frozen = (info_set or {}).get("goal_version_id")
    if active_goal_version_id is not None:
        if frozen != active_goal_version_id:
            return _check(GATE_GOAL_ANCHOR, False, "冻结版本≠当前 active 版本")
        if suggestion.get("goal_version_id") != active_goal_version_id:
            return _check(GATE_GOAL_ANCHOR, False, "建议未锚定 active 目标版本")
    return _check(GATE_GOAL_ANCHOR, True, "目标锚一致")


def _freshness_check(info_set, today) -> dict:
    as_of = (info_set or {}).get("as_of")
    if as_of is None:
        return _check(GATE_FRESHNESS, False, "信息集无 as_of")
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    today = today or date.today()
    age = (today - as_of).days
    if age > FRESHNESS_DAYS:
        return _check(GATE_FRESHNESS, False, f"信息集已过期 {age} 天（>45）")
    return _check(GATE_FRESHNESS, True, f"信息集 {age} 天内")


def _execution_check(suggestion) -> dict:
    for item in suggestion.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("subject_kind") not in SUBJECT_KINDS:
            return _check(GATE_NO_EXECUTION, False, "subject_kind 非法")
        if item.get("direction") not in DIRECTIONS:
            return _check(GATE_NO_EXECUTION, False, "direction 非法")
    texts = _templates(suggestion)
    for word in _EXECUTION_WORDS:
        if any(word in text for text in texts):
            return _check(GATE_NO_EXECUTION, False, f"含执行意图词: {word}")
    return _check(GATE_NO_EXECUTION, True, "无执行意图")


def _threshold_source_check(suggestion, entries) -> dict:
    missing = []
    for item in suggestion.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in item.get("info_refs") or []:
            if not (key.endswith(".band") or key.endswith(".grade")):
                continue
            entry = entries.get(key)
            if entry is None or not entry.get("source"):
                missing.append(key)
    if missing:
        return _check(GATE_THRESHOLD_SOURCE, False,
                      f"阈值引用缺 source: {sorted(set(missing))}")
    return _check(GATE_THRESHOLD_SOURCE, True, "阈值来源齐备")