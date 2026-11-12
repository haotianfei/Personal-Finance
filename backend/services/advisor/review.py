"""复盘编排（三期-4 W2/W4；ADR-0014 D4–D9；spec §2/§5）。

- **创建即冻结**：as-of 目标版本（`resolve_version_as_of`）＋as-of 阈值
  （`resolve_revision_as_of`）＋选定已采纳建议；`snapshot_json` 规范化＋sha256。
- 读时重算 hash；不一致 → 409 `REVIEW_SNAPSHOT_CORRUPT`。
- 归因四分类＋结论；`closed` 后不可改。
- 提案独立表；`adopt` **只记决定**（零领域写），真正生效由人另走 C1/C2 端点。
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (
    AdvisorDecision, AdvisorInfoSet, AdvisorReview, AdvisorReviewProposal,
    AdvisorReviewSuggestion, AdvisorSuggestion, AllocationTarget, Goal,
)
from services import audit_service, band_service, goal_service
from services.advisor.read import DbReadModel
from services.audit_service import make_change

ATTRIBUTION_CATEGORIES = ("market_moved", "advice_wrong", "not_executed",
                          "execution_drift")
PROPOSAL_KINDS = ("goal_version", "threshold", "allocation_target", "narrative")
APPLIED_KINDS = ("goal_version", "allocation_target", "threshold")
MAX_CONCLUSION = 2000
MAX_ATTRIBUTION = 20
MONEY_Q = Decimal("0.01")
RATE_Q = Decimal("0.0001")
PP_Q = Decimal("0.01")

REVIEW_BAND_HISTORY_MISSING = "REVIEW_BAND_HISTORY_MISSING"
REVIEW_SIGNAL_HISTORY_UNAVAILABLE = "REVIEW_SIGNAL_HISTORY_UNAVAILABLE"


def _invalid(field: str, message: str):
    raise ApiError(ErrorCode.VALIDATION_ERROR, 422, message,
                   details={"fields": [{"field": field, "message": message}]})


def _money(value) -> str | None:
    return None if value is None else str(Decimal(str(value)).quantize(MONEY_Q))


def _rate(value) -> str | None:
    return None if value is None else str(Decimal(str(value)).quantize(RATE_Q))


def _pp(value) -> str | None:
    return None if value is None else str(Decimal(str(value)).quantize(PP_Q))


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:                       # 2/29 → 2/28
        return value.replace(year=value.year + years, day=28)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 快照


def _anchor_payload(gv: dict, as_of: date) -> dict:
    grade = gv.get("grade")
    if grade is not None and grade.get("band"):
        band = grade["band"]
        grade = {**grade, "band": {
            "kind": band.get("kind"), "threshold": band.get("threshold"),
            "red": band.get("red"), "source": band.get("source")}}
    return {
        "as_of": as_of.isoformat(),
        "anchor_date": (gv["anchor_date"].isoformat()
                        if gv.get("anchor_date") else None),
        "goal_version_id": gv["goal_version_id"],
        "version_no": gv["version_no"],
        "net_worth": _money(gv["net_worth"]),
        "progress": _rate(gv["progress"]),
        "gap": _money(gv["gap"]),
        "required_annualized": _rate(gv["required_annualized"]),
        "grade": grade,
    }


def _allocation_snapshot(read, start_as_of: date | None,
                         as_of: date) -> dict:
    end = read.allocation_rows_as_of(as_of)
    start = (read.allocation_rows_as_of(start_as_of)
             if start_as_of is not None else None)
    start_rows = ({(r["dimension"], r["target_id"]): r for r in start["rows"]}
                  if start else {})
    rows = []
    missing_history = False
    for row in end["rows"]:
        before = start_rows.get((row["dimension"], row["target_id"]))
        actual_start = before["actual_percent"] if before else None
        actual_end = row["actual_percent"]
        delta = None
        if actual_start is not None and actual_end is not None:
            delta = _pp(Decimal(actual_end) - Decimal(actual_start))
        if row["history"] == "missing":
            missing_history = True
        rows.append({
            "dimension": row["dimension"],
            "target_ref": row["target_ref"],
            "target_percent": row["target_percent"],
            "actual_percent_start": actual_start,
            "actual_percent_end": actual_end,
            "delta_pp": delta,
            "deviation_pp": row["deviation_pp"],
            "revision_id": row["revision_id"],
            "band": row["band"],
            "state": row["state"],
            "history": row["history"],
        })
    return {
        "band_basis": "as-of",
        "classification_basis": "current_tree",
        "history_from": end["history_from"],
        "rows": rows,
        "skipped_deleted_targets": end["skipped_deleted_targets"],
        "_missing_history": missing_history,
    }


def _suggestion_snapshot(db: Session, review_goal_id: int, as_of: date,
                         suggestion_ids: list[int]) -> list[dict]:
    if not suggestion_ids:
        return []
    if len(set(suggestion_ids)) != len(suggestion_ids):
        _invalid("suggestion_ids", "建议 id 重复")
    out = []
    for suggestion_id in suggestion_ids:
        row = db.get(AdvisorSuggestion, suggestion_id)
        if row is None:
            _invalid("suggestion_ids", f"建议不存在: {suggestion_id}")
        if row.status != "accepted":
            _invalid("suggestion_ids", f"建议未采纳: {suggestion_id}")
        if row.goal_id not in (None, review_goal_id):
            _invalid("suggestion_ids", f"建议不属于该目标: {suggestion_id}")
        if row.info_set_id is None:
            _invalid("suggestion_ids", f"建议缺信息集: {suggestion_id}")
        info = db.get(AdvisorInfoSet, row.info_set_id)
        if info is None or info.as_of is None or info.as_of > as_of:
            _invalid("suggestion_ids", f"建议信息集晚于 as_of: {suggestion_id}")
        accepted = db.execute(
            select(AdvisorDecision).where(
                AdvisorDecision.suggestion_id == row.id,
                AdvisorDecision.action == "accept")
            .order_by(AdvisorDecision.id.desc()).limit(1)
        ).scalar_one_or_none()
        execution = db.execute(
            select(AdvisorDecision).where(
                AdvisorDecision.suggestion_id == row.id,
                AdvisorDecision.action == "execution_note")
            .order_by(AdvisorDecision.id.desc()).limit(1)
        ).scalar_one_or_none()
        out.append({
            "suggestion_id": row.id, "title": row.title, "status": row.status,
            "horizon": row.horizon,
            "info_set": {"id": info.id, "as_of": info.as_of.isoformat(),
                         "payload_sha256": info.payload_sha256},
            "accepted_at": accepted.decided_at if accepted else None,
            "execution": ({"status": execution.execution_status,
                           "note": execution.reason,
                           "decided_at": execution.decided_at}
                          if execution else None),
        })
    return out


def _signals_snapshot(read, as_of: date) -> dict:
    signals = read.signals(as_of)
    domains = [d for d in signals.get("domains") or []
               if d["domain"] in ("B1", "B2", "B3")]
    return {"basis": "current_rules", "domains": domains,
            "events": signals.get("events") or []}


def _hints(gv: dict, as_of: date) -> dict:
    net_worth = gv.get("net_worth") or Decimal("0")
    base_date = gv.get("anchor_date") or as_of
    target_amount = gv["target_amount"]
    target_date = gv["target_date"]

    def required_for(years: int):
        version = SimpleNamespace(target_amount=target_amount,
                                  target_date=_add_years(target_date, years))
        value = goal_service.compute_progress(net_worth, version, base_date)
        return _rate(value["required_annualized"])

    return {
        "required_at_current_date": _rate(gv.get("required_annualized")),
        "extend_1y": {"target_date": _add_years(target_date, 1).isoformat(),
                      "required_annualized": required_for(1)},
        "extend_2y": {"target_date": _add_years(target_date, 2).isoformat(),
                      "required_annualized": required_for(2)},
    }


def build_snapshot(db: Session, read, *, goal_id: int, as_of: date,
                   start_as_of: date | None, suggestions: list[dict]) -> dict:
    end_gv = read.goal_version_as_of(goal_id, as_of)
    if end_gv is None:
        _invalid("as_of", "该时点无生效目标版本")
    start_gv = (read.goal_version_as_of(goal_id, start_as_of)
                if start_as_of is not None else None)

    allocation = _allocation_snapshot(read, start_as_of, as_of)
    missing_history = allocation.pop("_missing_history")

    comparable = bool(start_gv and end_gv and
                      start_gv["goal_version_id"] == end_gv["goal_version_id"])
    delta = {
        "comparable": comparable,
        "net_worth_change": (None if not (start_gv and end_gv) else
                             _money(end_gv["net_worth"] - start_gv["net_worth"])),
        "progress_delta": (None if not comparable else
                           _rate(end_gv["progress"] - start_gv["progress"])),
        "not_comparable_reason": (None if comparable else
                                  ("no_start_anchor" if start_gv is None
                                   else "goal_version_changed")),
    }
    degradations = [{"code": REVIEW_SIGNAL_HISTORY_UNAVAILABLE,
                     "message": "预警规则未版本化，信号为复盘时点重算"}]
    if missing_history:
        degradations.append({
            "code": REVIEW_BAND_HISTORY_MISSING,
            "message": "该时点早于首条阈值史，相关配置行不判灯（不回退当期尺）"})

    return {
        "schema": 1,
        "anchors": {
            "start": _anchor_payload(start_gv, start_as_of) if start_gv else None,
            "end": _anchor_payload(end_gv, as_of),
        },
        "delta": delta,
        "goal": {"goal_id": goal_id,
                 "version_changed": bool(start_gv and
                                         start_gv["goal_version_id"]
                                         != end_gv["goal_version_id"])},
        "allocation": allocation,
        "suggestions": suggestions,
        "signals": _signals_snapshot(read, as_of),
        "hints": _hints(end_gv, as_of),
        "degradations": degradations,
    }


# ------------------------------------------------------------------ 创建


def _resolve_goal(db: Session, goal_id: int | None):
    if goal_id is not None:
        goal = db.get(Goal, goal_id)
        if goal is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"目标不存在: {goal_id}",
                           details={"goal_id": goal_id})
    else:
        target = goal_service.resolve_target(db, None)
        if target is None:
            _invalid("goal_id", "无生效目标，无法创建复盘")
        goal = target[0]
    versions = goal_service._versions(db, goal.id)
    if goal_service._active_version(versions) is None:
        _invalid("goal_id", "目标当前无生效版本，不接受新复盘")
    return goal


def create_review(db: Session, *, goal_id: int | None = None,
                  as_of: date | None = None, start_as_of: date | None = None,
                  suggestion_ids: list[int] | None = None,
                  label: str | None = None, note: str | None = None) -> dict:
    goal = _resolve_goal(db, goal_id)
    dates = []
    from services import analysis_service
    dates = analysis_service._get_all_snapshot_dates(db)
    if not dates:
        _invalid("as_of", "库内无快照，无法冻结复盘")
    as_of = goal_service.resolve_as_of(dates, as_of)
    if as_of is None:
        _invalid("as_of", "无法确定观察终点")
    if goal_service.resolve_version_as_of(db, goal.id, as_of) is None:
        _invalid("as_of", "该时点无生效目标版本")

    suggestions = _suggestion_snapshot(db, goal.id, as_of,
                                       list(suggestion_ids or []))
    if start_as_of is None:
        if suggestions:
            start_as_of = date.fromisoformat(
                min(s["info_set"]["as_of"] for s in suggestions))
        else:
            previous = db.execute(
                select(AdvisorReview)
                .where(AdvisorReview.goal_id == goal.id,
                       AdvisorReview.status == "closed")
                .order_by(AdvisorReview.as_of.desc()).limit(1)
            ).scalar_one_or_none()
            if previous is not None:
                start_as_of = previous.as_of
            else:
                version = goal_service.resolve_version_as_of(
                    db, goal.id, as_of)[1]
                start_as_of = version.effective_from
    if start_as_of is not None:
        start_as_of = goal_service.resolve_as_of(dates, start_as_of)
        if start_as_of >= as_of:
            _invalid("start_as_of", "start_as_of 必须早于 as_of")
        if goal_service.resolve_version_as_of(db, goal.id, start_as_of) is None:
            _invalid("start_as_of", "该时点无生效目标版本")

    read = DbReadModel(db)
    snapshot = build_snapshot(db, read, goal_id=goal.id, as_of=as_of,
                              start_as_of=start_as_of, suggestions=suggestions)
    snapshot_json = _canonical(snapshot)
    end_gv = read.goal_version_as_of(goal.id, as_of)
    start_gv = (read.goal_version_as_of(goal.id, start_as_of)
                if start_as_of is not None else None)
    row = AdvisorReview(
        goal_id=goal.id, goal_version_id=end_gv["goal_version_id"],
        start_goal_version_id=(start_gv["goal_version_id"] if start_gv else None),
        as_of=as_of, start_as_of=start_as_of, label=label, note=note,
        status="open", snapshot_json=snapshot_json,
        snapshot_sha256=_sha(snapshot_json),
        created_at=datetime.now().isoformat())
    db.add(row)
    db.flush()
    for suggestion in suggestions:
        db.add(AdvisorReviewSuggestion(review_id=row.id,
                                       suggestion_id=suggestion["suggestion_id"]))
    change = make_change(
        "review", row.id, "create", None,
        {"goal_id": row.goal_id, "goal_version_id": row.goal_version_id,
         "as_of": row.as_of.isoformat(),
         "start_as_of": (row.start_as_of.isoformat()
                         if row.start_as_of else None),
         "snapshot_sha256": row.snapshot_sha256,
         "suggestion_ids": [s["suggestion_id"] for s in suggestions]})
    audit_service.record(db, change, actor="owner")
    db.commit()
    db.refresh(row)
    return {"review": _review_summary(db, row), "change": change}


def _review_summary(db: Session, row: AdvisorReview) -> dict:
    goal = db.get(Goal, row.goal_id)
    version = db.get(models_goal_version(), row.goal_version_id)
    return {
        "id": row.id, "goal_id": row.goal_id,
        "goal_name": goal.name if goal else None,
        "goal_version_id": row.goal_version_id,
        "version_no": version.version_no if version else None,
        "start_goal_version_id": row.start_goal_version_id,
        "status": row.status,
        "as_of": row.as_of.isoformat(),
        "start_as_of": (row.start_as_of.isoformat()
                        if row.start_as_of else None),
        "label": row.label, "note": row.note,
        "snapshot_sha256": row.snapshot_sha256,
        "attribution": (json.loads(row.attribution_json)
                        if row.attribution_json else []),
        "conclusion": row.conclusion,
        "created_at": row.created_at, "closed_at": row.closed_at,
    }


def models_goal_version():
    from models import GoalVersion
    return GoalVersion


def _load_review(db: Session, review_id: int) -> AdvisorReview:
    row = db.get(AdvisorReview, review_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"复盘不存在: {review_id}",
                       details={"review_id": review_id})
    return row


def _snapshot_of(row: AdvisorReview) -> dict:
    if _sha(row.snapshot_json) != row.snapshot_sha256:
        raise ApiError(ErrorCode.REVIEW_SNAPSHOT_CORRUPT, 409,
                       "复盘快照重算不一致（篡改/坏库）",
                       details={"review_id": row.id})
    try:
        return json.loads(row.snapshot_json)
    except ValueError:
        raise ApiError(ErrorCode.REVIEW_SNAPSHOT_CORRUPT, 409,
                       "复盘快照不可解析", details={"review_id": row.id})


def review_detail(db: Session, review_id: int) -> dict:
    row = _load_review(db, review_id)
    snapshot = _snapshot_of(row)
    proposals = db.execute(
        select(AdvisorReviewProposal)
        .where(AdvisorReviewProposal.review_id == row.id)
        .order_by(AdvisorReviewProposal.id)).scalars().all()
    return {
        "review": _review_summary(db, row),
        "snapshot": snapshot,
        "payload_ok": True,
        "suggestions": snapshot.get("suggestions") or [],
        "proposals": [_proposal_out(p) for p in proposals],
    }


def list_reviews(db: Session, *, goal_id: int | None = None,
                 status: str | None = None, page: int = 1,
                 page_size: int = 20) -> dict:
    if status is not None and status not in ("open", "closed"):
        _invalid("status", "status 仅支持 open|closed")
    query = select(AdvisorReview)
    if goal_id is not None:
        query = query.where(AdvisorReview.goal_id == goal_id)
    if status:
        query = query.where(AdvisorReview.status == status)
    total = db.execute(
        select(func.count()).select_from(query.subquery())).scalar() or 0
    from sqlalchemy import case
    order = case((AdvisorReview.status == "open", 0), else_=1)
    rows = db.execute(
        query.order_by(order, AdvisorReview.created_at.desc(),
                       AdvisorReview.id.desc())
        .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    ids = [row.id for row in rows]
    proposal_counts: dict[int, dict[str, int]] = {}
    if ids:
        for review_id, proposal_status, count in db.execute(
                select(AdvisorReviewProposal.review_id,
                       AdvisorReviewProposal.status, func.count())
                .where(AdvisorReviewProposal.review_id.in_(ids))
                .group_by(AdvisorReviewProposal.review_id,
                          AdvisorReviewProposal.status)).all():
            proposal_counts.setdefault(review_id, {})[proposal_status] = count
    batch_sizes = dict(db.execute(
        select(AdvisorReviewSuggestion.review_id, func.count())
        .where(AdvisorReviewSuggestion.review_id.in_(ids))
        .group_by(AdvisorReviewSuggestion.review_id)).all()) if ids else {}
    items = []
    for row in rows:
        summary = _review_summary(db, row)
        try:
            snapshot = json.loads(row.snapshot_json)
        except ValueError:
            snapshot = {}
        delta = snapshot.get("delta") or {}
        counts = proposal_counts.get(row.id, {})
        degradations = snapshot.get("degradations") or []
        summary.update({
            "progress_end": (snapshot.get("anchors", {}).get("end", {})
                             or {}).get("progress"),
            "progress_delta": delta.get("progress_delta"),
            "comparable": delta.get("comparable"),
            "proposal_counts": {"proposed": counts.get("proposed", 0),
                                "adopted": counts.get("adopted", 0)},
            "suggestion_batch_size": int(batch_sizes.get(row.id, 0)),
            "conclusion_excerpt": (row.conclusion[:120]
                                   if row.conclusion else None),
            "band_history_missing": any(
                d.get("code") == REVIEW_BAND_HISTORY_MISSING
                for d in degradations),
        })
        items.append(summary)
    return {"items": items, "total": int(total), "page": page,
            "page_size": page_size}


# ------------------------------------------------------------------ 结论/关闭


def _validate_attribution(attribution) -> list[dict]:
    if attribution is None:
        return []
    if not isinstance(attribution, list) or len(attribution) > MAX_ATTRIBUTION:
        _invalid("attribution", "attribution 须为 ≤20 条数组")
    out = []
    for item in attribution:
        if not isinstance(item, dict):
            _invalid("attribution", "归因项须为对象")
        category = item.get("category")
        if category not in ATTRIBUTION_CATEGORIES:
            _invalid("attribution", "category 仅支持四分类")
        out.append({k: item.get(k) for k in
                    ("category", "subject_kind", "subject_ref", "note")})
    return out


def _validate_conclusion(text: str | None) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str) or len(text) > MAX_CONCLUSION:
        _invalid("conclusion", "结论须为 ≤2000 字字符串")
    return text


def _ensure_open(row: AdvisorReview):
    if row.status != "open":
        raise ApiError(ErrorCode.REVIEW_CLOSED, 409, "复盘已关闭",
                       details={"review_id": row.id, "status": row.status})


def update_conclusion(db: Session, review_id: int,
                      attribution=None, conclusion: str | None = None) -> dict:
    row = _load_review(db, review_id)
    _ensure_open(row)
    attr = _validate_attribution(attribution)
    text = _validate_conclusion(conclusion)
    before = {"attribution": (json.loads(row.attribution_json)
                              if row.attribution_json else []),
              "conclusion": row.conclusion}
    if attribution is not None:
        row.attribution_json = _canonical(attr)
    if conclusion is not None:
        row.conclusion = text
    after = {"attribution": (json.loads(row.attribution_json)
                             if row.attribution_json else []),
             "conclusion": row.conclusion}
    change = make_change("review", row.id, "conclusion", before, after)
    audit_service.record(db, change, actor="owner")
    db.commit()
    return {"review": _review_summary(db, row), "change": change}


def close_review(db: Session, review_id: int,
                 conclusion: str | None = None) -> dict:
    row = _load_review(db, review_id)
    _ensure_open(row)
    text = _validate_conclusion(conclusion) if conclusion is not None \
        else row.conclusion
    if not (text or "").strip():
        _invalid("conclusion", "关闭复盘必须填写结论")
    row.conclusion = text
    before = {"status": row.status}
    row.status = "closed"
    row.closed_at = datetime.now().isoformat()
    change = make_change("review", row.id, "close", before,
                         {"status": row.status})
    audit_service.record(db, change, actor="owner")
    db.commit()
    return {"review": _review_summary(db, row), "change": change}


# ------------------------------------------------------------------ 提案


def _payload_sha(payload: dict) -> str:
    return _sha(_canonical(payload))


def _proposal_out(row: AdvisorReviewProposal) -> dict:
    return {
        "id": row.id, "review_id": row.review_id, "kind": row.kind,
        "subject_ref": row.subject_ref,
        "payload": json.loads(row.payload_json),
        "payload_sha256": row.payload_sha256,
        "rationale": row.rationale, "status": row.status,
        "decided_at": row.decided_at, "decision_note": row.decision_note,
        "applied_kind": row.applied_kind, "applied_ref": row.applied_ref,
        "created_at": row.created_at,
    }


def _validate_proposal(db: Session, review: AdvisorReview, kind: str,
                       subject_ref: str | None, payload: dict) -> str | None:
    if kind not in PROPOSAL_KINDS:
        _invalid("kind", "kind 仅支持 goal_version|threshold|allocation_target|narrative")
    if not isinstance(payload, dict):
        _invalid("payload", "payload 须为对象")
    if kind == "narrative":
        if not str(payload.get("text") or "").strip():
            _invalid("payload.text", "narrative text 不能为空")
        return None
    if kind == "goal_version":
        expected = f"goal:{review.goal_id}"
        if subject_ref not in (None, expected):
            _invalid("subject_ref", f"goal_version 的 subject_ref 须为 {expected}")
        target_date = payload.get("target_date")
        if isinstance(target_date, str):
            try:
                target_date = date.fromisoformat(target_date)
            except ValueError:
                _invalid("target_date", "目标日期须为 ISO 日期")
        goal_service.validate_amount(payload.get("target_amount"))
        goal_service.validate_target_date(target_date)
        goal_service.validate_rate(payload.get("target_rate_annual"))
        goal_service.validate_enum("risk_profile", payload.get("risk_profile"),
                                   goal_service.RISK_PROFILES)
        goal_service.validate_enum("life_stage", payload.get("life_stage"),
                                   goal_service.LIFE_STAGES)
        goal_service.validate_reason(payload.get("reason"))
        return expected

    target_id = payload.get("allocation_target_id")
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        _invalid("payload.allocation_target_id", "allocation_target_id 非法")
    target = db.get(AllocationTarget, target_id)
    if target is None:
        _invalid("payload.allocation_target_id", "配置目标不存在")
    expected = f"allocation_target:{target_id}"
    if subject_ref not in (None, expected):
        _invalid("subject_ref", f"subject_ref 须为 {expected}")
    if kind == "threshold":
        band_service.validate_thresholds(
            payload.get("alert_threshold"),
            payload.get("alert_threshold_red"),
            payload.get("relative_threshold"))
    else:
        percent = payload.get("target_percent")
        try:
            percent = Decimal(str(percent))
        except Exception:
            _invalid("payload.target_percent", "target_percent 非法")
        if percent < 0 or percent > 100:
            _invalid("payload.target_percent", "target_percent 须在 0..100")
    return expected


def create_proposal(db: Session, review_id: int, *, kind: str,
                    subject_ref: str | None, payload: dict,
                    rationale: str) -> dict:
    review = _load_review(db, review_id)
    _ensure_open(review)
    if not str(rationale or "").strip():
        _invalid("rationale", "提案理由不能为空")
    normalized_ref = _validate_proposal(db, review, kind, subject_ref, payload)
    payload_json = _canonical(payload)
    row = AdvisorReviewProposal(
        review_id=review.id, kind=kind, subject_ref=normalized_ref,
        payload_json=payload_json, payload_sha256=_sha(payload_json),
        rationale=rationale.strip(), status="proposed",
        created_at=datetime.now().isoformat())
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(AdvisorReviewProposal).where(
                AdvisorReviewProposal.review_id == review.id,
                AdvisorReviewProposal.kind == kind,
                AdvisorReviewProposal.payload_sha256 == _sha(payload_json),
                AdvisorReviewProposal.status == "proposed")
        ).scalars().first()
        raise ApiError(ErrorCode.REVIEW_PROPOSAL_DUPLICATE, 409,
                       "同内容待裁决提案已存在",
                       details={"existing_id": existing.id if existing else None})
    change = make_change(
        "review_proposal", row.id, "create", None,
        {"review_id": review.id, "kind": kind,
         "subject_ref": normalized_ref, "payload_sha256": row.payload_sha256})
    audit_service.record(db, change, actor="owner")
    db.commit()
    db.refresh(row)
    return {"proposal": _proposal_out(row), "change": change}


def _load_proposal(db: Session, review_id: int,
                   proposal_id: int) -> AdvisorReviewProposal:
    row = db.get(AdvisorReviewProposal, proposal_id)
    if row is None or row.review_id != review_id:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"提案不存在: {proposal_id}",
                       details={"proposal_id": proposal_id})
    return row


def adopt_proposal(db: Session, review_id: int, proposal_id: int, *,
                   reason: str | None = None,
                   applied_kind: str | None = None,
                   applied_ref: str | None = None) -> dict:
    row = _load_proposal(db, review_id, proposal_id)
    if row.status != "proposed":
        raise ApiError(ErrorCode.REVIEW_PROPOSAL_DECIDED, 409,
                       "提案已裁决", details={"proposal_id": row.id,
                                            "status": row.status})
    if row.kind == "narrative":
        _invalid("kind", "narrative 提案不可采纳")
    if applied_kind is not None and applied_kind not in APPLIED_KINDS:
        _invalid("applied_kind", "applied_kind 非法")
    before = {"status": row.status}
    row.status = "adopted"
    row.decided_at = datetime.now().isoformat()
    row.decision_note = reason
    row.applied_kind = applied_kind
    row.applied_ref = applied_ref
    after = {"status": row.status, "applied_kind": applied_kind,
             "applied_ref": applied_ref, "reason": reason}
    change = make_change("review_proposal", row.id, "adopt", before, after)
    audit_service.record(db, change, actor="owner")
    db.commit()
    return {"proposal": _proposal_out(row), "change": change}


def dismiss_proposal(db: Session, review_id: int, proposal_id: int,
                     reason: str | None = None) -> dict:
    row = _load_proposal(db, review_id, proposal_id)
    if row.status != "proposed":
        raise ApiError(ErrorCode.REVIEW_PROPOSAL_DECIDED, 409,
                       "提案已裁决", details={"proposal_id": row.id,
                                            "status": row.status})
    if not (reason or "").strip():
        _invalid("reason", "驳回必须填写理由")
    before = {"status": row.status}
    row.status = "dismissed"
    row.decided_at = datetime.now().isoformat()
    row.decision_note = reason.strip()
    change = make_change("review_proposal", row.id, "dismiss", before,
                         {"status": row.status, "reason": row.decision_note})
    audit_service.record(db, change, actor="owner")
    db.commit()
    return {"proposal": _proposal_out(row), "change": change}


# ------------------------------------------------------------------ 候选建议


def list_candidates(db: Session, *, goal_id: int | None = None,
                    as_of: date | None = None,
                    start_as_of: date | None = None) -> dict:
    from services import analysis_service
    dates = analysis_service._get_all_snapshot_dates(db)
    if as_of is None:
        as_of = dates[-1] if dates else None
    if as_of is None:
        return {"items": [], "total": 0}
    as_of = goal_service.resolve_as_of(dates, as_of)
    if start_as_of is not None:
        start_as_of = goal_service.resolve_as_of(dates, start_as_of)
        if start_as_of >= as_of:
            _invalid("start_as_of", "start_as_of 必须早于 as_of")

    query = (select(AdvisorSuggestion, AdvisorInfoSet)
             .join(AdvisorInfoSet, AdvisorInfoSet.id == AdvisorSuggestion.info_set_id)
             .where(AdvisorSuggestion.status == "accepted",
                    AdvisorInfoSet.as_of <= as_of))
    if goal_id is not None:
        query = query.where(AdvisorSuggestion.goal_id == goal_id)
    if start_as_of is not None:
        query = query.where(AdvisorInfoSet.as_of >= start_as_of)
    rows = db.execute(query.order_by(AdvisorInfoSet.as_of.desc())).all()
    used = {r[0] for r in db.execute(
        select(AdvisorReviewSuggestion.suggestion_id)).all()}

    items = []
    for suggestion, info in rows:
        if suggestion.id in used:
            continue
        accepted = db.execute(
            select(AdvisorDecision).where(
                AdvisorDecision.suggestion_id == suggestion.id,
                AdvisorDecision.action == "accept")
            .order_by(AdvisorDecision.id.desc()).limit(1)
        ).scalar_one_or_none()
        execution = db.execute(
            select(AdvisorDecision).where(
                AdvisorDecision.suggestion_id == suggestion.id,
                AdvisorDecision.action == "execution_note")
            .order_by(AdvisorDecision.id.desc()).limit(1)
        ).scalar_one_or_none()
        items.append({
            "suggestion_id": suggestion.id, "title": suggestion.title,
            "horizon": suggestion.horizon,
            "info_set": {"id": info.id, "as_of": info.as_of.isoformat()},
            "execution": ({"status": execution.execution_status,
                           "note": execution.reason} if execution else None),
            "accepted_at": accepted.decided_at if accepted else None,
        })
    return {"items": items, "total": len(items)}