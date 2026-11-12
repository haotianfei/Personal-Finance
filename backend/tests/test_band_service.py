"""三期-2 W1 判据解析/判灯纯函数测试（spec §2.7、§7.2；ADR-0012 D1–D4）。

覆盖：C2 两张真值表（严格大于、绝对/相对取先触发、rel_on 下限、红档兜底）、
存量脏数据回退、来源标注；C1 单侧表；四态/闸门；写路径阈值校验。
数据全部合成。
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from errors import ApiError, ErrorCode
from services import band_service as bs


def target(percent, yellow="5.00", red=None, relative=None):
    return SimpleNamespace(
        target_percent=Decimal(str(percent)),
        alert_threshold=None if yellow is None else Decimal(str(yellow)),
        alert_threshold_red=None if red is None else Decimal(str(red)),
        relative_threshold=None if relative is None else Decimal(str(relative)),
    )


# ---------------------------------------------------------------- C2 解析


def test_resolve_band_defaults_and_source_markers():
    band = bs.resolve_band(target("40"))
    assert band["kind"] == "dual_threshold"
    assert band["threshold"] == "5.00"          # 绝对先触发（相对 10.00）
    assert band["red"] == "10.00"               # 2×黄
    assert band["source"] == (
        "allocation_targets.alert_threshold"
        "+code_default:red=2xyellow+code_default:relative_25-50")
    comp = band["components"]
    assert comp["abs_yellow"] == "5.00" and comp["abs_red"] == "10.00"
    assert comp["rel_yellow"] == "25.00" and comp["rel_red"] == "50.00"
    assert comp["rel_applicable"] is True
    assert comp["rel_yellow_pp"] == "10.00" and comp["rel_red_pp"] == "20.00"


def test_resolve_band_human_set_red_and_relative():
    band = bs.resolve_band(target("40", yellow="4.00", red="8.00",
                                  relative="20"))
    assert band["threshold"] == "4.00" and band["red"] == "8.00"
    assert band["source"] == ("allocation_targets.alert_threshold"
                              "+allocation_targets.alert_threshold_red"
                              "+allocation_targets.relative_threshold")
    assert band["components"]["rel_yellow_pp"] == "8.00"


def test_resolve_band_relative_first_trigger_and_min():
    # 10% 目标：相对 25% = 2.50pp < 绝对 5.00 → 相对先触发
    band = bs.resolve_band(target("10"))
    assert band["threshold"] == "2.50"
    assert band["red"] == "5.00"                # min(10.00, 10*50%=5.00)
    # 60% 目标：相对 25% = 15pp > 绝对 5.00 → 绝对先触发
    band = bs.resolve_band(target("60"))
    assert band["threshold"] == "5.00"
    assert band["red"] == "10.00"               # min(10.00, 30.00)


def test_resolve_band_below_applicable_floor_disables_relative():
    # 5% < 10%：相对判据关闭，仅绝对；components 标注 rel_applicable=False
    band = bs.resolve_band(target("5"))
    assert band["threshold"] == "5.00" and band["red"] == "10.00"
    assert band["components"]["rel_applicable"] is False
    assert band["components"]["rel_yellow_pp"] is None
    assert "code_default:relative" not in band["source"]
    # 目标权重 0 / 负数（脏数据）：同理关闭相对
    for pct in ("0", "-1"):
        assert bs.resolve_band(target(pct))["components"]["rel_applicable"] is False


def test_resolve_band_dirty_yellow_falls_back_to_code_default():
    band = bs.resolve_band(target("40", yellow="0"))
    assert band["threshold"] == "5.00"
    assert band["source"].startswith("code_default:yellow_5.00")
    assert band["components"]["abs_red"] == "10.00"


def test_resolve_band_legacy_null_columns_use_defaults():
    band = bs.resolve_band(target("40"))
    assert band["components"]["abs_red"] == "10.00"
    assert band["components"]["rel_yellow"] == "25.00"


# ---------------------------------------------------------------- C2 真值表


def test_c2_truth_table_absolute_boundaries():
    band = bs.resolve_band(target("40", yellow="5.00", red="10.00",
                                  relative="25"))
    cases = [("0", "green"), ("5.00", "green"), ("5.01", "yellow"),
             ("10.00", "yellow"), ("10.01", "red"), ("-10.01", "red")]
    for dev, expected in cases:
        assert bs.evaluate(Decimal(dev), band) == expected, dev


def test_c2_truth_table_relative_first_trigger_and_floor():
    band10 = bs.resolve_band(target("10"))
    assert bs.evaluate(Decimal("3.0"), band10) == "yellow"   # 2.50 < 3.0
    assert bs.evaluate(Decimal("2.5"), band10) == "green"    # 等于有效阈值
    band5 = bs.resolve_band(target("5"))
    assert bs.evaluate(Decimal("3.0"), band5) == "green"     # 相对不适用
    band60 = bs.resolve_band(target("60"))
    assert bs.evaluate(Decimal("6.0"), band60) == "yellow"   # 绝对先触发


def test_evaluate_missing_inputs_are_gray():
    assert bs.evaluate(None, bs.resolve_band(target("40"))) == "gray"
    assert bs.evaluate(Decimal("1"), None) == "gray"


# ---------------------------------------------------------------- C1


def test_resolve_goal_band_one_sided():
    band = bs.resolve_goal_band(SimpleNamespace(target_rate_annual=Decimal("0.05")))
    assert band["kind"] == "dual_threshold_one_sided"
    assert band["threshold"] == "0.0500" and band["red"] == "0.1000"
    assert band["source"] == "goal_versions.target_rate_annual"
    for rate in (None, Decimal("0"), Decimal("-0.01")):
        assert bs.resolve_goal_band(
            SimpleNamespace(target_rate_annual=rate)) is None


def test_c1_truth_table():
    band = bs.resolve_goal_band(SimpleNamespace(target_rate_annual=Decimal("0.05")))
    cases = [("0.0000", "green"), ("0.0500", "green"), ("0.0501", "yellow"),
             ("0.1000", "yellow"), ("0.1001", "red")]
    for required, expected in cases:
        assert bs.evaluate(Decimal(required), band, one_sided=True) == expected
    # 单向：比较 value 本身而不是 |value|（负值不触发）
    assert bs.evaluate(Decimal("-0.5"), band, one_sided=True) == "green"
    # required=null（阻断）→ ⚪
    gates = [{"code": "RATE_UNAVAILABLE", "note": "率不可得", "blocking": True}]
    graded = bs.grade(None, band, gates, one_sided=True)
    assert graded["state"] == "gray" and graded["band"] is None


def test_grade_carries_band_and_note():
    band = bs.resolve_goal_band(SimpleNamespace(target_rate_annual=Decimal("0.05")))
    graded = bs.grade(Decimal("0.1001"), band, [], one_sided=True)
    assert graded["state"] == "red"
    assert graded["band"] == {
        "kind": "dual_threshold_one_sided", "threshold": "0.0500",
        "source": "goal_versions.target_rate_annual", "red": "0.1000",
        "components": band["components"]}
    assert graded["basis"] == "current" and "红阈值" in graded["note"]
    # 无判据 → None（不是第五态）；阻断闸门 → ⚪
    assert bs.grade(Decimal("0.1"), None, []) is None
    assert bs.grade(Decimal("0.1"), None,
                    [{"code": "NO_DATA", "note": "空库", "blocking": True}]
                    )["state"] == "gray"


# ---------------------------------------------------------------- 写路径校验


def test_validate_thresholds_accepts_valid():
    bs.validate_thresholds(Decimal("5"), None, None)
    bs.validate_thresholds(Decimal("5"), Decimal("10"), Decimal("25"))
    bs.validate_thresholds(Decimal("90"), Decimal("100"), Decimal("100"))


@pytest.mark.parametrize("yellow,red,relative,field", [
    (Decimal("0"), None, None, "alert_threshold"),
    (Decimal("-1"), None, None, "alert_threshold"),
    (Decimal("101"), None, None, "alert_threshold"),
    (Decimal("5"), Decimal("5"), None, "alert_threshold_red"),     # red <= yellow
    (Decimal("5"), Decimal("4"), None, "alert_threshold_red"),
    (Decimal("5"), Decimal("101"), None, "alert_threshold_red"),
    (Decimal("5"), None, Decimal("0"), "relative_threshold"),
    (Decimal("5"), None, Decimal("101"), "relative_threshold"),
])
def test_validate_thresholds_rejects_with_fields(yellow, red, relative, field):
    with pytest.raises(ApiError) as ei:
        bs.validate_thresholds(yellow, red, relative)
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.status_code == 422
    assert [f["field"] for f in ei.value.details["fields"]] == [field]