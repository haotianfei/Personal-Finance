"""三期-3 W3 确定性门真值表（spec §5.2/§5.4；ADR-0013 D6）。

覆盖：全通过；坏 JSON/缺字段/6 项/未知字段/超长 rationale；引用不存在；
数字字面量（含 %）；占位符渲染（pp/比率/金额千分位/state）；占位符拼错；
信息集篡改；目标锚；新鲜度 45/46 天；执行意图词；warning 不阻断；
输出 9 项固定形状。纯函数、零 DB。

ADR-0019 D12 之后覆盖度下线，恒通过的第 10 项 `GATE_DATA_PARTIAL` 已删除。
"""
from datetime import date, timedelta

import pytest

from services.advisor import gate
from services.advisor.info_set import payload_sha256

TODAY = date(2026, 11, 1)
AS_OF = date(2026, 10, 31)
GOAL_VERSION = 7


def entry(key, kind, value, unit, *, source="allocation", label="示例类目",
          source_ref="attr:fund_type:3"):
    return {"key": key, "kind": kind, "label": label, "value": value,
            "value_scaled": "1.2" if kind == "money" else None,
            "unit": unit, "trust": "measured", "source": source,
            "source_ref": source_ref, "as_of": AS_OF.isoformat()}


def make_info(entries=None, *, as_of=AS_OF, goal_version_id=GOAL_VERSION):
    rows = entries if entries is not None else [
        entry("alloc.fund_type.3.deviation_pp", "pp", "12.30", "pp"),
        entry("alloc.fund_type.3.band", "state", "red", "state"),
        entry("nw.total", "money", "12345.60", "CNY", source="ledger",
              label="净资产"),
        entry("returns.twr", "ratio", "0.1234", "ratio", source="metric",
              label="TWR"),
    ]
    return {"entries": rows, "entry_count": len(rows),
            "payload_sha256": payload_sha256(rows), "as_of": as_of,
            "goal_version_id": goal_version_id}


def make_suggestion(**overrides):
    payload = {
        "title": "权益类超配，建议分步回调",
        "summary_template": "当前偏离 {{info.alloc.fund_type.3.deviation_pp}}，建议回调至目标",
        "goal_version_id": GOAL_VERSION,
        "items": [{
            "seq": 1,
            "subject_kind": "attribute",
            "subject_ref": "attr:fund_type:3",
            "direction": "decrease",
            "rationale_template": "偏离 {{info.alloc.fund_type.3.deviation_pp}}，已超红档",
            "info_refs": ["alloc.fund_type.3.deviation_pp",
                          "alloc.fund_type.3.band"],
        }],
    }
    payload.update(overrides)
    return payload


def by_code(result):
    return {c["code"]: c for c in result["checks"]}


def run(suggestion=None, info=None, **kwargs):
    return gate.run_gate(suggestion if suggestion is not None
                         else make_suggestion(),
                         info if info is not None else make_info(),
                         today=TODAY, active_goal_version_id=GOAL_VERSION,
                         checked_at="2026-11-01T00:00:00", **kwargs)


# ---------------------------------------------------------------- 形状/全通过


def test_all_pass_shape_and_codes():
    result = run()
    assert result["result"] == "passed"
    assert result["gate_version"] == "advisor.gate.v1"
    assert result["checked_at"] == "2026-11-01T00:00:00"
    assert [c["code"] for c in result["checks"]] == [
        "GATE_SCHEMA", "GATE_INFO_SET_SEALED", "GATE_REF_EXISTS",
        "GATE_NUMERIC_PROVENANCE", "GATE_PLACEHOLDER_RESOLVES",
        "GATE_GOAL_ANCHOR", "GATE_FRESHNESS", "GATE_NO_EXECUTION",
        "GATE_THRESHOLD_SOURCE"]
    assert sum(1 for c in result["checks"]
               if c["severity"] == "blocking") == 8
    assert sum(1 for c in result["checks"]
               if c["severity"] == "warning") == 1
    assert all(c["passed"] for c in result["checks"])


# ---------------------------------------------------------------- 1 schema


@pytest.mark.parametrize("payload,detail", [
    ("not-json", "JSON"),
    ({}, "title"),
    (make_suggestion(title=""), "title"),
    (make_suggestion(items=[]), "items"),
    (make_suggestion(items=[make_suggestion()["items"][0]] * 6), "items"),
    (make_suggestion(extra="x"), "未知字段"),
    (make_suggestion(items=[dict(make_suggestion()["items"][0],
                                 subject_kind="asset_item")]), "subject_kind"),
    (make_suggestion(items=[dict(make_suggestion()["items"][0],
                                 rationale_template="长" * 401)]), "400"),
])
def test_gate_schema_failures(payload, detail):
    result = run(payload)
    check = by_code(result)["GATE_SCHEMA"]
    assert check["passed"] is False
    assert detail in check["detail"]
    assert result["result"] == "failed"


# ---------------------------------------------------------------- 2/3 sealed/ref


def test_info_set_tampered_fails_sealed():
    info = make_info()
    info["entries"][0]["value"] = "99.99"       # 不改 payload_sha256
    assert by_code(run(info=info))["GATE_INFO_SET_SEALED"]["passed"] is False
    info = make_info()
    info["entry_count"] += 1
    assert by_code(run(info=info))["GATE_INFO_SET_SEALED"]["passed"] is False


def test_ref_exists_fail():
    suggestion = make_suggestion()
    suggestion["items"][0]["info_refs"] = ["alloc.fund_type.999.deviation_pp"]
    result = run(suggestion)
    assert by_code(result)["GATE_REF_EXISTS"]["passed"] is False
    assert "999" in by_code(result)["GATE_REF_EXISTS"]["detail"]
    assert result["result"] == "failed"


# ---------------------------------------------------------------- 4 numeric


def test_numeric_provenance_rejects_literal_and_percent():
    suggestion = make_suggestion()
    suggestion["items"][0]["rationale_template"] = "建议增持 12.3%"
    check = by_code(run(suggestion))["GATE_NUMERIC_PROVENANCE"]
    assert check["passed"] is False
    assert run(suggestion)["result"] == "failed"


def test_numeric_provenance_whitelist():
    suggestion = make_suggestion(
        summary_template="按 T+1 处理，2026 年第2步再评估",
        items=[dict(make_suggestion()["items"][0],
                    rationale_template="第3步后复核，见 {{info.nw.total}}")])
    assert by_code(run(suggestion))["GATE_NUMERIC_PROVENANCE"]["passed"] is True


# ---------------------------------------------------------------- 5 render


def test_placeholder_resolves_and_renders():
    result = run()
    assert by_code(result)["GATE_PLACEHOLDER_RESOLVES"]["passed"] is True
    info = make_info()
    rendered = gate.render_template(
        "偏离 {{info.alloc.fund_type.3.deviation_pp}}，"
        "金额 {{info.nw.total}}，比率 {{info.returns.twr}}，"
        "状态 {{info.alloc.fund_type.3.band}}", info)
    assert "12.30pp" in rendered
    assert "12,345.60" in rendered
    assert "12.34%" in rendered
    assert "red" in rendered


def test_placeholder_typo_fails():
    suggestion = make_suggestion()
    suggestion["items"][0]["rationale_template"] = "偏离 {{info.alloc.fund_type.3.deviation}}"
    check = by_code(run(suggestion))["GATE_PLACEHOLDER_RESOLVES"]
    assert check["passed"] is False
    assert "deviation" in check["detail"]


def test_render_unknown_raises():
    with pytest.raises(ValueError):
        gate.render_template("{{info.nope}}", make_info())


# ---------------------------------------------------------------- 6 goal anchor


def test_goal_anchor_requires_active_version():
    suggestion = make_suggestion(goal_version_id=None)
    assert by_code(run(suggestion))["GATE_GOAL_ANCHOR"]["passed"] is False
    # 冻结版本与当前 active 不一致
    result = gate.run_gate(make_suggestion(), make_info(), today=TODAY,
                           active_goal_version_id=8)
    assert by_code(result)["GATE_GOAL_ANCHOR"]["passed"] is False
    # 无 active 目标 ⇒ 不要求锚
    result = gate.run_gate(make_suggestion(goal_version_id=None), make_info(),
                           today=TODAY, active_goal_version_id=None)
    assert by_code(result)["GATE_GOAL_ANCHOR"]["passed"] is True


# ---------------------------------------------------------------- 7 freshness


def test_freshness_boundary_45_vs_46():
    assert by_code(run(info=make_info(as_of=TODAY - timedelta(days=45))))[
        "GATE_FRESHNESS"]["passed"] is True
    stale = make_info(as_of=TODAY - timedelta(days=46))
    assert by_code(run(info=stale))["GATE_FRESHNESS"]["passed"] is False
    assert run(info=stale)["result"] == "failed"


# ---------------------------------------------------------------- 8 execution


@pytest.mark.parametrize("word", ["自动执行", "代扣", "下单", "委托买入"])
def test_execution_words_fail(word):
    suggestion = make_suggestion()
    suggestion["items"][0]["rationale_template"] = f"建议{word}该笔调整"
    result = run(suggestion)
    assert by_code(result)["GATE_NO_EXECUTION"]["passed"] is False
    assert result["result"] == "failed"


# ---------------------------------------------------------------- warning


def test_threshold_source_warning_does_not_fail():
    entries = make_info()["entries"]
    band = next(e for e in entries if e["key"].endswith(".band"))
    band["source"] = ""
    info = make_info(entries=entries)
    result = run(info=info)
    assert by_code(result)["GATE_THRESHOLD_SOURCE"]["passed"] is False
    assert result["result"] == "passed"          # warning 不阻断
    assert all(c["passed"] for c in result["checks"]
               if c["severity"] == "blocking")


def test_data_partial_check_is_gone():
    """覆盖度闸门已删（ADR-0019 D12）：信息集不再产出 coverage.*，恒通过的检查不留。"""
    codes = [c["code"] for c in run()["checks"]]
    assert "GATE_DATA_PARTIAL" not in codes
    assert not hasattr(gate, "GATE_DATA_PARTIAL")
    assert not hasattr(gate, "_data_partial_check")