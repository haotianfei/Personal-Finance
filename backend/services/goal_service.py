"""C1 目标版本与达成度（三期-1 W1+W2）。

规格：`.scratch/phase3-goal/spec.md` v1.0 §2/§4/§5；决策 `docs/adr/0010` §3/§4/§6。

纪律：
- **状态机**：draft → active → archived；每 Goal 至多一条 active；版本号
  `max+1` 单调；active/archived 内容不可改（更正＝新版本）；归档是 Goal 终态。
- **每次写同事务 `audit_service.record`**（ADR-0007：审计失败阻断业务）；
  entity/action 见 ADR-0010 §6.2。
- **达成度口径单点**：`compute_progress` 为纯函数，`/progress` 与
  `/api/metrics/status` 共用 `progress_payload`（公式不漂移）；NW 一律复用
  `analysis_service._sum_as_of`（与 B1 同源）。
- 金额 2 位、比率 4 位，字符串 Decimal；C1 判灯（三期-2 W3）只读
  `target_rate_annual` 并与指标状态面共用 `band_service`（不反写 criterion_json）。
"""
import calendar
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import Goal, GoalVersion
from services import analysis_service, audit_service, band_service
from services.audit_service import make_change as _change
from services.meta_service import (
    NO_DATA, RATE_UNAVAILABLE, build_meta,
)

ZERO = Decimal("0")
MONEY_Q = Decimal("0.01")
RATE_Q = Decimal("0.0001")

RISK_PROFILES = ("conservative", "balanced", "growth")
LIFE_STAGES = ("accumulation", "transition", "decumulation")
SCOPE_KINDS = ("family", "member")

CRITERION_NOTE = "C1 no default band; grade=null until 三期-2"


# ------------------------------------------------------------------ 纯函数


def calendar_month_diff(start: date, end: date) -> int:
    """整月差（日历月，end<start 时夹到 0）。"""
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def compute_progress(nw: Decimal, version, as_of: date) -> dict:
    """达成度纯函数（spec §4.2）：progress / gap / required_annualized + gates。

    `required_annualized` 是「不再追加/撤出本金」的 CAGR 门槛；分段：
    gap<=0 → 0；NW<=0 或已过期限 → null + RATE_UNAVAILABLE（阻断）。
    """
    target = Decimal(str(version.target_amount))
    nw = Decimal(str(nw))
    gap = target - nw
    progress = (nw / target) if target != ZERO else None
    remaining_days = (version.target_date - as_of).days
    remaining_months = calendar_month_diff(as_of, version.target_date)

    required = None
    gates: list[dict] = []
    if gap <= ZERO:
        required = ZERO
    elif nw <= ZERO:
        gates.append({
            "code": RATE_UNAVAILABLE, "blocking": True,
            "scope": "goal_required_annualized",
            "note": "净资产 ≤0，所需年化不可得",
        })
    elif remaining_days <= 0:
        gates.append({
            "code": RATE_UNAVAILABLE, "blocking": True,
            "scope": "goal_required_annualized",
            "note": "已过目标期限且未达成，所需年化不可得",
        })
    else:
        years = Decimal(remaining_days) / Decimal("365.25")
        required = (target / nw) ** (Decimal(1) / years) - Decimal(1)

    return {
        "progress": (progress.quantize(RATE_Q) if progress is not None else None),
        "gap": gap.quantize(MONEY_Q),
        "required_annualized": (required.quantize(RATE_Q)
                                if required is not None else None),
        "remaining_days": remaining_days,
        "remaining_months": remaining_months,
        "gates": gates,
        "assumption": "no_future_contributions",
    }


def resolve_partition(version: GoalVersion) -> dict:
    """分区解析缝（ADR §3.6）：当前 active 版本 → 分区单值；band_basis 恒 current。"""
    return {
        "goal_version_id": version.id,
        "risk_profile": version.risk_profile,
        "life_stage": version.life_stage,
        "band_basis": "current",
    }


def criterion_snapshot(version: GoalVersion) -> str:
    """激活时的判据快照（本期 kind=null，为三期-2/4 防反写）。"""
    return json.dumps({
        "schema": 1,
        "kind": None,
        "band_basis": "current",
        "partition": {"risk_profile": version.risk_profile,
                      "life_stage": version.life_stage},
        "note": CRITERION_NOTE,
    }, ensure_ascii=False)


def resolve_as_of(dates: list[date], as_of: date | None) -> date | None:
    """默认＝最新快照日；as_of 越出快照日期范围 → 422（与指标状态面同语义）。"""
    if not dates:
        return as_of
    earliest, latest = dates[0], dates[-1]
    if as_of is None:
        return latest
    if as_of < earliest or as_of > latest:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422, "as_of 越界：不在快照日期范围内",
            details={"fields": [{
                "field": "as_of",
                "message": f"须在 {earliest.isoformat()}..{latest.isoformat()} 之间",
            }]})
    return as_of


# 覆盖度闸门（DATA_MISSING_CARRY_FORWARD）已随 ADR-0019 D12 整体删除：
# 它的分母是 `attributes(type='account').remind_enabled`（表已删），而「该日没有
# 某项的记录」在变更日志语义下＝值没变，不是「没核对」。需要沿用事实请读趋势点的
# `carried_items` / `carried_max_days`（`analysis_service._carry_stats`），
# 不要在这里重建一条「部分录入」判定。


# ------------------------------------------------------------------ 序列化


def _money(value) -> str:
    return str(Decimal(str(value)).quantize(MONEY_Q))


def _rate(value) -> str | None:
    return str(Decimal(str(value)).quantize(RATE_Q)) if value is not None else None


def version_out(version: GoalVersion) -> dict:
    return {
        "id": version.id,
        "goal_id": version.goal_id,
        "version_no": version.version_no,
        "status": version.status,
        "target_amount": _money(version.target_amount),
        "target_date": version.target_date.isoformat(),
        "target_rate_annual": _rate(version.target_rate_annual),
        "risk_profile": version.risk_profile,
        "life_stage": version.life_stage,
        "reason": version.reason,
        "effective_from": (version.effective_from.isoformat()
                           if version.effective_from else None),
        "criterion": (json.loads(version.criterion_json)
                      if version.criterion_json else None),
        "created_at": version.created_at,
        "activated_at": version.activated_at,
        "archived_at": version.archived_at,
    }


def goal_out(goal: Goal, versions: list[GoalVersion]) -> dict:
    return {
        "id": goal.id,
        "name": goal.name,
        "scope": {"kind": goal.scope_kind, "owner_id": goal.scope_owner_id},
        "is_primary": bool(goal.is_primary),
        "status": goal_status(versions),
        "created_at": goal.created_at,
    }


def goal_status(versions: list[GoalVersion]) -> str:
    """Goal 根状态由版本派生：有 active→active；有 draft→draft；否则 archived。"""
    if any(v.status == "active" for v in versions):
        return "active"
    if any(v.status == "draft" for v in versions):
        return "draft"
    return "archived"


def _version_snapshot(version: GoalVersion) -> dict:
    """审计 before/after 结构化快照（ADR §6.2：含 target_amount/date/status/version_no）。"""
    return {
        "version_no": version.version_no,
        "status": version.status,
        "target_amount": _money(version.target_amount),
        "target_date": version.target_date.isoformat(),
        "target_rate_annual": _rate(version.target_rate_annual),
        "risk_profile": version.risk_profile,
        "life_stage": version.life_stage,
        "reason": version.reason,
        "effective_from": (version.effective_from.isoformat()
                           if version.effective_from else None),
    }


def _goal_snapshot(goal: Goal) -> dict:
    return {
        "name": goal.name,
        "scope_kind": goal.scope_kind,
        "scope_owner_id": goal.scope_owner_id,
        "is_primary": int(goal.is_primary or 0),
    }


# ------------------------------------------------------------------ 校验


def _invalid(field: str, message: str):
    raise ApiError(ErrorCode.VALIDATION_ERROR, 422, message,
                   details={"fields": [{"field": field, "message": message}]})


def validate_name(name: str | None) -> str:
    text = (name or "").strip()
    if not text:
        _invalid("name", "目标名称不能为空")
    if len(text) > 100:
        _invalid("name", "目标名称不能超过 100 字")
    return text


def validate_scope(scope) -> tuple[str, int | None]:
    kind = getattr(scope, "kind", None) if scope is not None else "family"
    owner_id = getattr(scope, "owner_id", None) if scope is not None else None
    if kind not in SCOPE_KINDS:
        _invalid("scope.kind", "scope.kind 仅支持 family|member")
    if kind == "member":
        _invalid("scope.kind", "成员目标随四期启用")
    if owner_id is not None:
        _invalid("scope.owner_id", "family 范围的 owner_id 必须为空")
    return kind, None


def validate_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(MONEY_Q)
    except (InvalidOperation, ValueError, TypeError):
        _invalid("target_amount", f"目标金额非法: {value!r}")
    if amount <= ZERO:
        _invalid("target_amount", "目标金额须大于 0（净资产目标）")
    return amount


def validate_target_date(value, *, field: str = "target_date") -> date:
    if not isinstance(value, date):
        _invalid(field, "目标日期须为 ISO 日期（YYYY-MM-DD）")
    if value <= date.today():
        _invalid(field, "目标日期须晚于今日")
    return value


def validate_rate(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        rate = Decimal(str(value)).quantize(RATE_Q)
    except (InvalidOperation, ValueError, TypeError):
        _invalid("target_rate_annual", f"目标年化非法: {value!r}")
    if rate <= Decimal("-1"):
        _invalid("target_rate_annual", "目标年化须大于 -1")
    return rate


def validate_enum(field: str, value, allowed: tuple[str, ...]) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    if value not in allowed:
        _invalid(field, f"{field} 仅支持 {'|'.join(allowed)}")
    return value


def validate_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        _invalid("reason", "目标设定/变更必须附理由（R-18）")
    return text


# ------------------------------------------------------------------ 查询


def _versions(db: Session, goal_id: int) -> list[GoalVersion]:
    return list(db.execute(
        select(GoalVersion).where(GoalVersion.goal_id == goal_id)
        .order_by(GoalVersion.version_no.desc())
    ).scalars().all())


def _load_goal(db: Session, goal_id: int) -> Goal:
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"目标不存在: {goal_id}",
                       details={"goal_id": goal_id})
    return goal


def _load_version(db: Session, goal_id: int, version_no: int) -> GoalVersion:
    version = db.execute(
        select(GoalVersion).where(GoalVersion.goal_id == goal_id,
                                  GoalVersion.version_no == version_no)
    ).scalar_one_or_none()
    if version is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404,
                       f"目标版本不存在: {goal_id}#{version_no}",
                       details={"goal_id": goal_id, "version_no": version_no})
    return version


def _active_version(versions: list[GoalVersion]) -> GoalVersion | None:
    return next((v for v in versions if v.status == "active"), None)


def list_goals(db: Session, *, include_archived: bool = False) -> dict:
    goals = db.execute(
        select(Goal).order_by(Goal.is_primary.desc(), Goal.id)
    ).scalars().all()
    items = []
    for goal in goals:
        versions = _versions(db, goal.id)
        status = goal_status(versions)
        if status == "archived" and not include_archived:
            continue
        active = _active_version(versions)
        item = goal_out(goal, versions)
        item["active_version"] = version_out(active) if active else None
        items.append(item)
    return {"items": items, "total": len(items)}


def get_goal(db: Session, goal_id: int) -> dict:
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    return {
        "goal": goal_out(goal, versions),
        "versions": [version_out(v) for v in versions],
    }


def resolve_version_as_of(db: Session, goal_id: int, as_of: date):
    """C4 as-of 版本解析（ADR-0014 D4）：含 active/archived、排除 draft。

    **不复用** `resolve_target`（只查 active）；`effective_from <= as_of` 含边界，
    同日多版本按 `effective_from DESC, id DESC`。无 -> None。
    """
    goal = db.get(Goal, goal_id)
    if goal is None:
        return None
    version = db.execute(
        select(GoalVersion)
        .where(GoalVersion.goal_id == goal_id,
               GoalVersion.status.in_(("active", "archived")),
               GoalVersion.effective_from.is_not(None),
               GoalVersion.effective_from <= as_of)
        .order_by(GoalVersion.effective_from.desc(), GoalVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        return None
    return goal, version


def resolve_target(db: Session, as_of: date | None):
    """有效目标（ADR §4.3）：主目标优先，无主目标回退最近激活的 active 目标。

    仅当 `effective` 为空（空库）或 `>= effective_from` 时可用；无目标 → None。
    """
    cutoff = as_of or date.today()
    rows = db.execute(
        select(Goal, GoalVersion)
        .join(GoalVersion, GoalVersion.goal_id == Goal.id)
        .where(GoalVersion.status == "active")
        .order_by(Goal.is_primary.desc(), GoalVersion.activated_at.desc(),
                  GoalVersion.id.desc())
    ).all()
    for goal, version in rows:
        if version.effective_from is None or version.effective_from <= cutoff:
            return goal, version
    return None


# ------------------------------------------------------------------ 达成度


def progress_payload(db: Session, goal: Goal, version: GoalVersion,
                     as_of: date | None = None, *, net_worth: Decimal | None = None,
                     dates: list[date] | None = None) -> dict:
    """单点达成度载荷：`/progress` 与 `/api/metrics/status` 共用（公式不漂移）。"""
    if dates is None:
        dates = analysis_service._get_all_snapshot_dates(db)
    effective = resolve_as_of(dates, as_of)
    no_data = not dates
    if net_worth is None:
        net_worth = (analysis_service._sum_as_of(db, effective)[0]
                     if effective else ZERO)

    calc_date = effective or date.today()
    if version.effective_from and calc_date < version.effective_from:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422, "as_of 早于目标生效日",
            details={"fields": [{
                "field": "as_of",
                "message": f"须 >= 生效日 {version.effective_from.isoformat()}",
            }]})

    values = compute_progress(net_worth, version, calc_date)
    gates: list[dict] = []
    if no_data:
        gates.append({"code": NO_DATA, "scope": None, "blocking": True,
                      "note": "库内暂无快照数据，指标不可判"})
    gates.extend(values["gates"])

    payload = {
        "goal_id": goal.id,
        "goal_version_id": version.id,
        "version_no": version.version_no,
        "is_primary": bool(goal.is_primary),
        "as_of": (effective.isoformat() if effective else calc_date.isoformat()),
        "effective_from": (version.effective_from.isoformat()
                           if version.effective_from else None),
        "scope": {"kind": goal.scope_kind, "owner_id": goal.scope_owner_id},
        "target_amount": _money(version.target_amount),
        "target_date": version.target_date.isoformat(),
        "target_rate_annual": _rate(version.target_rate_annual),
        "net_worth": str(Decimal(str(net_worth)).quantize(MONEY_Q)),
        "gap": str(values["gap"]),
        "progress": str(values["progress"]),
        "required_annualized": (str(values["required_annualized"])
                                if values["required_annualized"] is not None
                                else None),
        "remaining_days": values["remaining_days"],
        "remaining_months": values["remaining_months"],
        "assumption": values["assumption"],
        "band_basis": "current",
        "partition": {"risk_profile": version.risk_profile,
                      "life_stage": version.life_stage},
    }
    payload["gates"] = gates
    payload["grade"] = band_service.grade(
        values["required_annualized"],
        band_service.resolve_goal_band(version),
        gates, one_sided=True)
    payload["meta"] = build_meta(db, effective, _degradations(gates))
    return payload


def metric_details(payload: dict) -> dict:
    """C1 指标 item 的稳定 details 契约（ADR §4.3）。"""
    keys = ("goal_id", "goal_version_id", "version_no", "is_primary", "scope",
            "target_amount", "target_date", "target_rate_annual", "net_worth",
            "gap", "progress", "required_annualized", "remaining_days",
            "remaining_months", "assumption", "band_basis", "partition")
    return {k: payload[k] for k in keys}


def _degradations(gates: list[dict]) -> list[dict]:
    out = []
    for gate in gates:
        out.append({"code": gate["code"], "scope": gate.get("scope"),
                    "message": gate.get("note") or gate["code"]})
    return out


degradations_of = _degradations


def get_goal_progress(db: Session, goal_id: int,
                      as_of: date | None = None) -> dict:
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    active = _active_version(versions)
    if active is None:
        _invalid("goal_id", "目标无生效版本，无法计算达成度")
    return progress_payload(db, goal, active, as_of)


# ------------------------------------------------------------------ 写路径


def _has_primary_active(db: Session, exclude_goal_id: int | None = None) -> bool:
    q = (select(func.count()).select_from(Goal)
         .join(GoalVersion, GoalVersion.goal_id == Goal.id)
         .where(Goal.is_primary == 1, GoalVersion.status == "active"))
    if exclude_goal_id is not None:
        q = q.where(Goal.id != exclude_goal_id)
    return bool(db.execute(q).scalar())


def _mark_primary(db: Session, goal: Goal) -> dict | None:
    """无主目标时自动设主（spec §2.1）；仅 family 目标可为主。"""
    if goal.scope_kind != "family":
        return None
    if goal.is_primary:
        return None
    if _has_primary_active(db, exclude_goal_id=goal.id):
        return None
    before = _goal_snapshot(goal)
    goal.is_primary = 1
    after = _goal_snapshot(goal)
    change = _change("goal", goal.id, "set_primary", before, after)
    audit_service.record(db, change)
    return change


def _build_version(db: Session, goal: Goal, *, version_no: int, target_amount,
                   target_date: date, target_rate_annual, risk_profile,
                   life_stage, reason: str, activate: bool) -> GoalVersion:
    now = datetime.now().isoformat()
    version = GoalVersion(
        goal_id=goal.id,
        version_no=version_no,
        status="draft",
        target_amount=target_amount,
        target_date=target_date,
        target_rate_annual=target_rate_annual,
        risk_profile=risk_profile,
        life_stage=life_stage,
        reason=reason,
        effective_from=None,
        criterion_json=None,
        created_at=now,
        activated_at=None,
        archived_at=None,
    )
    if activate:
        version.status = "active"
        version.effective_from = date.today()
        version.activated_at = now
        version.criterion_json = criterion_snapshot(version)
    return version


def create_goal(db: Session, *, name, scope=None, target_amount, target_date,
                target_rate_annual=None, risk_profile=None, life_stage=None,
                reason, activate: bool = True):
    """创建目标（默认同时激活首个版本）。返回 (goal, version, change)。"""
    goal_name = validate_name(name)
    kind, owner_id = validate_scope(scope)
    amount = validate_amount(target_amount)
    tdate = validate_target_date(target_date)
    rate = validate_rate(target_rate_annual)
    risk = validate_enum("risk_profile", risk_profile, RISK_PROFILES)
    life = validate_enum("life_stage", life_stage, LIFE_STAGES)
    goal_reason = validate_reason(reason)

    goal = Goal(name=goal_name, scope_kind=kind, scope_owner_id=owner_id,
                is_primary=0, created_at=datetime.now().isoformat())
    db.add(goal)
    db.flush()
    audit_service.record(db, _change("goal", goal.id, "create", None,
                                     _goal_snapshot(goal)))

    version = _build_version(
        db, goal, version_no=1, target_amount=amount, target_date=tdate,
        target_rate_annual=rate, risk_profile=risk, life_stage=life,
        reason=goal_reason, activate=activate)
    db.add(version)
    db.flush()
    change = _change("goal_version", version.id, "create", None,
                     _version_snapshot(version))
    audit_service.record(db, change)

    if activate:
        _mark_primary(db, goal)

    db.commit()
    db.refresh(goal)
    db.refresh(version)
    return goal, version, change


def create_version(db: Session, goal_id: int, *, target_amount, target_date,
                   target_rate_annual=None, risk_profile=None, life_stage=None,
                   reason, activate: bool = True):
    """新版本（追加史，版本号 max+1）；activate 时同事务归档旧 active。"""
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    if goal_status(versions) == "archived":
        _invalid("goal_id", "Goal 已归档，需新建目标（不提供 unarchive）")

    amount = validate_amount(target_amount)
    tdate = validate_target_date(target_date)
    rate = validate_rate(target_rate_annual)
    risk = validate_enum("risk_profile", risk_profile, RISK_PROFILES)
    life = validate_enum("life_stage", life_stage, LIFE_STAGES)
    goal_reason = validate_reason(reason)

    max_no = db.execute(
        select(func.max(GoalVersion.version_no))
        .where(GoalVersion.goal_id == goal.id)
    ).scalar() or 0
    version = _build_version(
        db, goal, version_no=int(max_no) + 1, target_amount=amount,
        target_date=tdate, target_rate_annual=rate, risk_profile=risk,
        life_stage=life, reason=goal_reason, activate=activate)

    old = None
    if activate:
        old = _active_version(versions)
        if old is not None:
            # 「建版本即激活」与 `activate_version` 是同一语义：旧版本归档是激活的
            # 副作用（系统规则），故同一身份口径（spec E2 ②）。
            _archive_version(db, old, actor="system", reason="normalization",
                             source="activation")

    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(
            ErrorCode.GOAL_VERSION_CONFLICT, 409,
            "目标版本号冲突，请重试",
            details={"goal_id": goal.id,
                     "active_version_no": (old.version_no
                                           if activate and old is not None
                                           else None)})

    change = _change("goal_version", version.id, "create", None,
                     _version_snapshot(version))
    audit_service.record(db, change)
    if activate:
        _mark_primary(db, goal)

    db.commit()
    db.refresh(version)
    return version, change


def _archive_version(db: Session, version: GoalVersion, *,
                     actor: str = "owner",
                     reason: str | None = None,
                     reason_note: str | None = None,
                     source: str | None = None) -> dict:
    """归档一个版本（`action='archive'` 属纠正类，缺 reason 由 `record()` 422 拦下）。

    `source` 只给**自动路径**用（`activate_version` 的隐式归档传 `'activation'`，
    写进 `after.source`）：那条归档是激活的副作用，不是人的裁决，故
    `actor='system'` + `reason='normalization'`（spec E2 ②）。
    """
    before = _version_snapshot(version)
    version.status = "archived"
    version.archived_at = datetime.now().isoformat()
    after = _version_snapshot(version)
    if source:
        after = {**after, "source": source}
    change = _change("goal_version", version.id, "archive", before, after,
                     reason=reason, reason_note=reason_note)
    audit_service.record(db, change, actor=actor)
    return change


def activate_version(db: Session, goal_id: int, version_no: int):
    """激活 draft：事务内先归档旧 active 再置新（唯一 active 索引兜底）。"""
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    version = _load_version(db, goal.id, version_no)
    if version.status == "active":
        raise ApiError(
            ErrorCode.GOAL_VERSION_CONFLICT, 409, "该版本已是 active",
            details={"goal_id": goal.id, "active_version_no": version.version_no})
    if version.status == "archived":
        raise ApiError(
            ErrorCode.GOAL_VERSION_IMMUTABLE, 409,
            "已归档版本不可再激活（归档终态）",
            details={"version_no": version.version_no, "status": version.status})
    validate_target_date(version.target_date)

    old = _active_version(versions)
    superseded_id = None
    if old is not None:
        superseded_id = old.id
        # 隐式归档旧版本＝激活的副作用（系统规则，不是人的裁决）：actor=system，
        # reason=normalization，after.source=activation（spec E2 ②）。
        # 用户显式归档（POST .../archive）不走这里，仍是 owner 且必须带 reason。
        _archive_version(db, old, actor="system", reason="normalization",
                         source="activation")

    before = _version_snapshot(version)
    now = datetime.now().isoformat()
    version.status = "active"
    version.effective_from = date.today()
    version.activated_at = now
    version.archived_at = None
    version.criterion_json = criterion_snapshot(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(
            ErrorCode.GOAL_VERSION_CONFLICT, 409, "并发激活冲突，请重试",
            details={"goal_id": goal.id,
                     "active_version_no": (old.version_no if old else None)})
    change = _change("goal_version", version.id, "activate", before,
                     _version_snapshot(version))
    audit_service.record(db, change)
    _mark_primary(db, goal)

    db.commit()
    db.refresh(version)
    return version, superseded_id, change


def update_draft(db: Session, goal_id: int, version_no: int, fields: dict):
    """改 draft 内容（active/archived → 409 GOAL_VERSION_IMMUTABLE）。"""
    goal = _load_goal(db, goal_id)
    version = _load_version(db, goal.id, version_no)
    if version.status != "draft":
        raise ApiError(
            ErrorCode.GOAL_VERSION_IMMUTABLE, 409,
            "仅 draft 版本可编辑；更正请新建版本",
            details={"version_no": version.version_no, "status": version.status})

    before = _version_snapshot(version)
    if "target_amount" in fields:
        version.target_amount = validate_amount(fields["target_amount"])
    if "target_date" in fields:
        version.target_date = validate_target_date(fields["target_date"])
    if "target_rate_annual" in fields:
        version.target_rate_annual = validate_rate(fields["target_rate_annual"])
    if "risk_profile" in fields:
        version.risk_profile = validate_enum("risk_profile",
                                             fields["risk_profile"], RISK_PROFILES)
    if "life_stage" in fields:
        version.life_stage = validate_enum("life_stage",
                                           fields["life_stage"], LIFE_STAGES)
    if "reason" in fields:
        version.reason = validate_reason(fields["reason"])

    change = _change("goal_version", version.id, "update", before,
                     _version_snapshot(version))
    audit_service.record(db, change)
    db.commit()
    db.refresh(version)
    return version, change


def delete_draft(db: Session, goal_id: int, version_no: int, *,
                 audit_reason: str | None = None,
                 audit_reason_note: str | None = None):
    """物理删除 draft（删除留审计行）；active/archived → 409。

    **无 UI 入口，供维护脚本**（spec E4 孤儿端点）；`action='delete'` 属纠正类，
    缺 reason 由 `audit_service.record()` 422 拦下（`AUDIT_REASON_REQUIRED`）。
    """
    goal = _load_goal(db, goal_id)
    version = _load_version(db, goal.id, version_no)
    if version.status != "draft":
        raise ApiError(
            ErrorCode.GOAL_VERSION_IMMUTABLE, 409,
            "仅 draft 版本可删除",
            details={"version_no": version.version_no, "status": version.status})
    before = _version_snapshot(version)
    row_id = version.id
    db.delete(version)
    db.flush()
    change = _change("goal_version", row_id, "delete", before, None,
                     reason=audit_reason, reason_note=audit_reason_note)
    audit_service.record(db, change)
    db.commit()
    return change


def rename_goal(db: Session, goal_id: int, name):
    goal = _load_goal(db, goal_id)
    new_name = validate_name(name)
    before = _goal_snapshot(goal)
    goal.name = new_name
    change = _change("goal", goal.id, "update", before, _goal_snapshot(goal))
    audit_service.record(db, change)
    db.commit()
    db.refresh(goal)
    return goal, change


def archive_goal(db: Session, goal_id: int, *,
                 audit_reason: str | None = None,
                 audit_reason_note: str | None = None):
    """Goal 归档（终态）：所有未归档版本置 archived、is_primary 清零、留审计。

    显式归档是**人的裁决**：`actor='owner'` + 必须带 reason（缺则 `record()` 422
    `AUDIT_REASON_REQUIRED`）。同一 reason 透传给被连带归档的每个版本行——
    它们与目标归档是同一次裁决。
    """
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    before = _goal_snapshot(goal)

    for version in versions:
        if version.status != "archived":
            _archive_version(db, version, reason=audit_reason,
                             reason_note=audit_reason_note)

    goal.is_primary = 0
    after = _goal_snapshot(goal)
    change = _change("goal", goal.id, "archive", before, after,
                     reason=audit_reason, reason_note=audit_reason_note)
    audit_service.record(db, change)
    db.commit()
    db.refresh(goal)
    return goal, change


def set_primary(db: Session, goal_id: int):
    """设主目标：先清旧主（同事务）再设本目标；draft-only → 422。"""
    goal = _load_goal(db, goal_id)
    versions = _versions(db, goal.id)
    if _active_version(versions) is None:
        _invalid("goal_id", "目标无生效版本，不能设为主目标（先激活）")

    previous = db.execute(
        select(Goal).where(Goal.is_primary == 1, Goal.id != goal.id)
    ).scalars().all()
    for other in previous:
        before = _goal_snapshot(other)
        other.is_primary = 0
        audit_service.record(db, _change("goal", other.id, "set_primary",
                                         before, _goal_snapshot(other)))

    before = _goal_snapshot(goal)
    goal.is_primary = 1
    change = _change("goal", goal.id, "set_primary", before, _goal_snapshot(goal))
    audit_service.record(db, change)

    db.commit()
    db.refresh(goal)
    return goal, change