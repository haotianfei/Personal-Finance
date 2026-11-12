"""三期-2 判据解析与判灯纯函数（C2 双阈值 / C1 单侧；ADR-0012 D1–D4）。

单点纪律（spec §2.3）：页面（`allocation_service`）与指标面（`metric_service`）、
目标面（`goal_service`）**共用**本模块；禁止第二套比较逻辑。

- `resolve_band(target_row)`：③ allocation_targets 行 + ④ 代码兜底 → 有效黄/红
  （绝对 pp 与相对 % 折算后取先触发＝取 min）；`source` 逐项标注来源。
- `resolve_goal_band(version)`：① goal_versions.target_rate_annual → 单侧带；
  空/≤0 → None（无判据，不发明带，ADR-0010 §3.7）。
- `evaluate(value, band, one_sided=)`：四态纯函数；边界一律**严格大于**。
- `validate_thresholds(...)`：写路径校验（422 VALIDATION_ERROR + details.fields）。

代码默认（④，可被 ③ 覆盖；系统不写阈值）：
黄 5.00pp / 红 2×黄 / 相对 25%（红 2×）/ 相对判据适用下限 10%。
"""
from decimal import Decimal

from errors import ApiError, ErrorCode

PP_Q = Decimal("0.01")
RATE_Q = Decimal("0.0001")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

DEFAULT_YELLOW = Decimal("5.00")
DEFAULT_RELATIVE = Decimal("25.00")
REL_APPLICABLE_MIN = Decimal("10.00")

SOURCE_YELLOW = "allocation_targets.alert_threshold"
SOURCE_RED = "allocation_targets.alert_threshold_red"
SOURCE_RELATIVE = "allocation_targets.relative_threshold"
SOURCE_GOAL_RATE = "goal_versions.target_rate_annual"

KIND_C2 = "dual_threshold"
KIND_C1 = "dual_threshold_one_sided"

_SEVERITY = {"green": 1, "yellow": 2, "red": 3}


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _q(value: Decimal, quantum: Decimal = PP_Q) -> str:
    return str(Decimal(value).quantize(quantum))


def resolve_band(target_row) -> dict:
    """解析 C2 目标行的有效带（绝对/相对取先触发）。

    返回 `{kind, threshold, red, source, components}`：
    - `threshold`＝有效黄阈值（pp，字符串）；`red`＝有效红阈值；
    - `components` 保留绝对/相对原值与适用性（前端可解释）。
    """
    abs_y_raw = _dec(getattr(target_row, "alert_threshold", None))
    if abs_y_raw is None or abs_y_raw <= ZERO:
        # 存量脏数据/未设：视为未设，回退代码默认并标注（spec §2.2）
        abs_y = DEFAULT_YELLOW
        src_y = "code_default:yellow_5.00"
    else:
        abs_y = abs_y_raw
        src_y = SOURCE_YELLOW

    red_raw = _dec(getattr(target_row, "alert_threshold_red", None))
    if red_raw is not None and red_raw > ZERO:
        abs_r = red_raw
        src_r = SOURCE_RED
    else:
        abs_r = abs_y * 2
        src_r = "code_default:red=2xyellow"

    rel_raw = _dec(getattr(target_row, "relative_threshold", None))
    if rel_raw is not None and rel_raw > ZERO:
        rel_y = rel_raw
        src_rel = SOURCE_RELATIVE
    else:
        rel_y = DEFAULT_RELATIVE
        src_rel = "code_default:relative_25-50"
    rel_r = rel_y * 2

    target_pct = _dec(getattr(target_row, "target_percent", None)) or ZERO
    rel_on = target_pct >= REL_APPLICABLE_MIN and target_pct > ZERO

    rel_y_pp = (target_pct * rel_y / HUNDRED).quantize(PP_Q) if rel_on else None
    rel_r_pp = (target_pct * rel_r / HUNDRED).quantize(PP_Q) if rel_on else None

    eff_y = min(abs_y, rel_y_pp) if rel_on else abs_y
    eff_r = min(abs_r, rel_r_pp) if rel_on else abs_r

    parts = [src_y, src_r]
    if rel_on:
        parts.append(src_rel)

    return {
        "kind": KIND_C2,
        "threshold": _q(eff_y),
        "red": _q(eff_r),
        "source": "+".join(parts),
        "components": {
            "abs_yellow": _q(abs_y),
            "abs_red": _q(abs_r),
            "rel_yellow": _q(rel_y),
            "rel_red": _q(rel_r),
            "rel_applicable": rel_on,
            "rel_yellow_pp": _q(rel_y_pp) if rel_y_pp is not None else None,
            "rel_red_pp": _q(rel_r_pp) if rel_r_pp is not None else None,
            "target_percent": _q(target_pct),
            "rel_applicable_min": _q(REL_APPLICABLE_MIN),
        },
    }


def resolve_goal_band(version) -> dict | None:
    """C1 单侧带：所需年化 vs target_rate_annual（黄＝超过，红＝超过 2×）。

    `target_rate_annual` 为空或 ≤0 → None（无判据，不判灯）。
    """
    rate = _dec(getattr(version, "target_rate_annual", None))
    if rate is None or rate <= ZERO:
        return None
    yellow = rate.quantize(RATE_Q)
    red = (rate * 2).quantize(RATE_Q)
    return {
        "kind": KIND_C1,
        "threshold": _q(yellow, RATE_Q),
        "red": _q(red, RATE_Q),
        "source": SOURCE_GOAL_RATE,
        "components": {
            "target_rate_annual": _q(yellow, RATE_Q),
            "red": _q(red, RATE_Q),
            "multiplier": "2",
        },
    }


def resolve_revision_as_of(db, allocation_target_id: int, as_of):
    """阈值史 as-of 解析（ADR-0014 D4/D5）：`effective_date <= as_of` 的最新行。

    - 同日多改按 `effective_date DESC, created_at DESC, id DESC` 取最后一条；
    - **无史 → None**（调用方 `band=null`，不回退当期尺、不伪装判据）。
    """
    from sqlalchemy import select
    from models import AllocationTargetRevision

    row = db.execute(
        select(AllocationTargetRevision)
        .where(
            AllocationTargetRevision.allocation_target_id == allocation_target_id,
            AllocationTargetRevision.effective_date <= as_of,
        )
        .order_by(AllocationTargetRevision.effective_date.desc(),
                  AllocationTargetRevision.created_at.desc(),
                  AllocationTargetRevision.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None and row.op == "delete":
        return None                    # 目标已删除：无判据（不回退更早行）
    return row


def evaluate(value, band: dict | None, *, one_sided: bool = False) -> str:
    """四态判灯（纯函数）：green | yellow | red | gray（值/带缺失）。

    边界纪律：黄/红均**严格大于**（等于阈值不触发）。
    """
    if value is None or band is None:
        return "gray"
    measured = Decimal(str(value))
    if not one_sided:
        measured = abs(measured)
    red = _dec(band.get("red"))
    yellow = _dec(band.get("threshold"))
    if red is not None and measured > red:
        return "red"
    if yellow is not None and measured > yellow:
        return "yellow"
    return "green"


def severity_rank(state: str | None) -> int:
    """严重度序（最差行选择用）：red > yellow > green；gray/None 最低。"""
    return _SEVERITY.get(state or "", 0)


def grade(value, band: dict | None, gates=(), *, one_sided: bool = False,
          basis: str = "current") -> dict | None:
    """判灯载荷（单点）：阻断闸门 → ⚪；无判据 → None；值不可得 → ⚪。

    `metric_service.evaluate_grade` 与 `goal_service` 的 `/progress` 共用，
    避免两处判灯文案/边界漂移。
    """
    blocking = [g for g in gates if g.get("blocking")]
    if blocking:
        return {"state": "gray", "band": None, "basis": basis,
                "note": "；".join(g.get("note") or g.get("code", "")
                                 for g in blocking)}
    if band is None:
        return None
    if value is None:
        return {"state": "gray", "band": None, "basis": basis,
                "note": "值不可得，无法判灯"}

    state = evaluate(value, band, one_sided=one_sided)
    measured = Decimal(str(value))
    if not one_sided:
        measured = abs(measured)
    yellow = Decimal(band["threshold"])
    red = _dec(band.get("red"))
    label = "所需年化" if one_sided else "|偏离|"
    if state == "red" and red is not None:
        note = f"{label} {measured} ＞ 红阈值 {red}（黄 {yellow}）"
    elif state == "yellow":
        note = f"{label} {measured} ＞ 黄阈值 {yellow}"
    else:
        note = f"{label} {measured} ≤ 黄阈值 {yellow}"

    band_out = {
        "kind": band.get("kind") or (KIND_C1 if one_sided else KIND_C2),
        "threshold": band["threshold"],
        "source": band.get("source") or "",
    }
    if band.get("red") is not None:
        band_out["red"] = band["red"]
    if band.get("components") is not None:
        band_out["components"] = band["components"]
    return {"state": state, "band": band_out, "basis": basis, "note": note}


def validate_thresholds(yellow, red, relative) -> None:
    """写路径校验：0<黄≤100；红为空或黄<红≤100；相对为空或 0<相对≤100。"""
    y = _dec(yellow)
    r = _dec(red)
    rel = _dec(relative)
    fields: list[dict] = []
    if y is None or y <= ZERO or y > HUNDRED:
        fields.append({"field": "alert_threshold",
                       "message": "黄阈值须满足 0 < 值 ≤ 100（pp）"})
    if r is not None:
        if r <= ZERO or r > HUNDRED:
            fields.append({"field": "alert_threshold_red",
                           "message": "红阈值须为空或满足 0 < 值 ≤ 100（pp）"})
        elif y is not None and ZERO < y <= HUNDRED and r <= y:
            fields.append({"field": "alert_threshold_red",
                           "message": "红阈值须大于黄阈值"})
    if rel is not None and (rel <= ZERO or rel > HUNDRED):
        fields.append({"field": "relative_threshold",
                       "message": "相对阈值须为空或满足 0 < 值 ≤ 100（%）"})
    if fields:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422, "阈值非法",
            details={"fields": fields})