"""advisor 生成/裁决编排（三期-3 W4/W5；spec §2/§5/§6；ADR-0013 D5–D9）。

纪律：
- 只写建议隔离区＋audit_log；**零账本写入/无账本写端口**。
- 生成：冻结信息集 → LLM 措辞 → 确定性门（10 项）→ 落 suggestions/items；
  失败整单终态（`generation_failed`/`gate_failed`），不落半成品。
- 裁决 append-only：accept/reject/expire/execution_note 各留审计行；
  采纳≠落账（不触发任何 F1/口径变更）。
- 自动触发默认关闭（settings 种子 0），冷却 7 天；`dedupe_key` 唯一。
"""
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (
    AdvisorDecision, AdvisorInfoSet, AdvisorJob, AdvisorSettings,
    AdvisorSuggestion, AdvisorSuggestionItem, GoalVersion, LLMProvider,
)
from services import audit_service, metric_service
from services.advisor.gate import run_gate, render_template
from services.advisor.info_set import freeze_info_set, load_info_set
from services.advisor.llm import AdvisorLlmError, HttpLlmGateway
from services.advisor.prompt import PROMPT_VERSION, build_prompt
from services.advisor.read import DbReadModel
from services.audit_service import make_change
from services.goal_service import _active_version, _load_goal, _versions

logger = logging.getLogger(__name__)

STATUSES = ("generating", "awaiting_review", "accepted", "rejected",
            "expired", "gate_failed", "generation_failed")
TERMINAL = {"accepted", "rejected", "expired", "gate_failed",
            "generation_failed"}
HORIZONS = ("short", "mid", "long")
EXECUTION_STATUSES = ("done", "partial", "not_done")
TRIGGER_KINDS = ("manual", "signal", "sched")
EXPIRY_DAYS = 30
AUTO_TRIGGER_LIMIT = 3
MARKET_DEGRADATION = {
    "code": "MARKET_DATA_NOT_FETCHED",
    "message": "本期不联网检索，市场判断为模型常识层",
}

GATE_LABELS = {
    "GATE_SCHEMA": "结构非法",
    "GATE_INFO_SET_SEALED": "信息集未封存/被改动",
    "GATE_REF_EXISTS": "引用不存在",
    "GATE_NUMERIC_PROVENANCE": "含数字字面量",
    "GATE_PLACEHOLDER_RESOLVES": "占位符不可解析",
    "GATE_GOAL_ANCHOR": "目标锚不一致",
    "GATE_FRESHNESS": "信息集过期",
    "GATE_NO_EXECUTION": "含执行意图",
    "GATE_THRESHOLD_SOURCE": "阈值来源缺失",
}

# 单进程后台执行器（POST 立即返回；job 状态落库，重启由 resume_pending 续跑）。
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor-gen")
_SESSION_FACTORY = None


def configure_worker(session_factory=None, executor=None):
    """测试/宿主注入：后台线程使用的 Session 工厂（默认 database.SessionLocal）。"""
    global _SESSION_FACTORY, _EXECUTOR
    if session_factory is not None:
        _SESSION_FACTORY = session_factory
    if executor is not None:
        _EXECUTOR = executor


def _worker_session_factory():
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY
    from database import SessionLocal
    return SessionLocal


def reset_worker():
    """测试隔离：恢复默认 SessionLocal（后台线程工厂）。"""
    global _SESSION_FACTORY
    _SESSION_FACTORY = None


def _submit_generation(suggestion_id: int, gateway, provider_snapshot: dict,
                       session_factory=None):
    """后台执行生成（独立 Session；异常不冒泡到请求线程）。"""
    factory = session_factory or _worker_session_factory()

    def _work():
        db = factory()
        try:
            run_generation(db, suggestion_id, gateway=gateway,
                           provider_snapshot=provider_snapshot)
        except Exception as exc:                    # 最后兜底：终态化，不吞成 generating
            logger.exception("advisor_generation_worker_failed id=%s", suggestion_id)
            try:
                if db.get(AdvisorSuggestion, suggestion_id) is not None:
                    _finish_failed(db, suggestion_id,
                                   _latest_job(db, suggestion_id),
                                   "generation_failed", str(exc)[:200])
            except Exception:
                db.rollback()
        finally:
            db.close()

    return _EXECUTOR.submit(_work)


# ------------------------------------------------------------------ 供应商/网关


def _provider_snapshot(row) -> dict:
    return {"id": getattr(row, "id", None), "label": getattr(row, "label", None),
            "model": getattr(row, "model", None)}


def resolve_provider(db: Session):
    """默认且启用供应商；无 → 409 ADVISOR_PROVIDER_UNAVAILABLE。"""
    row = db.execute(
        select(LLMProvider).where(LLMProvider.is_default.is_(True),
                                  LLMProvider.enabled.is_(True))
        .order_by(LLMProvider.id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.ADVISOR_PROVIDER_UNAVAILABLE, 409,
                       "无默认且启用的 LLM 供应商，请先在设置中配置")
    return row


def build_gateway(db: Session, gateway=None, provider=None):
    """返回 (gateway, provider_snapshot)；测试注入 fake 时不查供应商。"""
    if gateway is not None:
        return gateway, (_provider_snapshot(provider) if provider is not None
                         else {"id": None, "label": "test", "model": "fake"})
    row = resolve_provider(db)
    return HttpLlmGateway({
        "id": row.id, "kind": row.kind, "base_url": row.base_url,
        "api_key": row.api_key, "model": row.model,
    }), _provider_snapshot(row)


# ------------------------------------------------------------------ 设置


def _ensure_settings(db: Session) -> AdvisorSettings:
    row = db.get(AdvisorSettings, 1)
    if row is None:
        row = AdvisorSettings(id=1, auto_enabled=0, cooldown_days=7,
                              default_horizon="mid",
                              updated_at=datetime.now().isoformat())
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_settings(db: Session) -> dict:
    row = _ensure_settings(db)
    return {"auto_enabled": bool(row.auto_enabled),
            "cooldown_days": row.cooldown_days,
            "default_horizon": row.default_horizon,
            "last_auto_at": row.last_auto_at,
            "updated_at": row.updated_at}


def _invalid(field: str, message: str):
    raise ApiError(ErrorCode.VALIDATION_ERROR, 422, message,
                   details={"fields": [{"field": field, "message": message}]})


def update_settings(db: Session, fields: dict) -> tuple[dict, dict]:
    row = _ensure_settings(db)
    before = {"auto_enabled": bool(row.auto_enabled),
              "cooldown_days": row.cooldown_days,
              "default_horizon": row.default_horizon}
    if "auto_enabled" in fields and fields["auto_enabled"] is not None:
        row.auto_enabled = 1 if fields["auto_enabled"] else 0
    if "cooldown_days" in fields and fields["cooldown_days"] is not None:
        value = int(fields["cooldown_days"])
        if value < 0 or value > 365:
            _invalid("cooldown_days", "cooldown_days 须在 0..365")
        row.cooldown_days = value
    if "default_horizon" in fields and fields["default_horizon"] is not None:
        value = fields["default_horizon"]
        if value not in HORIZONS:
            _invalid("default_horizon", "default_horizon 仅支持 short|mid|long")
        row.default_horizon = value
    row.updated_at = datetime.now().isoformat()
    after = {"auto_enabled": bool(row.auto_enabled),
             "cooldown_days": row.cooldown_days,
             "default_horizon": row.default_horizon}
    change = make_change("advisor_settings", 1, "update", before, after)
    audit_service.record(db, change)
    db.commit()
    return get_settings(db), change


# ------------------------------------------------------------------ 创建/生成


def _manual_dedupe() -> str:
    return f"manual:{uuid.uuid4().hex}"


def create_suggestion(db: Session, *, trigger_kind: str = "manual",
                      trigger_ref: str | None = None,
                      dedupe_key: str | None = None,
                      horizon: str | None = None,
                      goal_id: int | None = None,
                      gateway=None, provider=None,
                      background: bool = False):
    """创建建议＋job；`background=True` 立即返回，Gate/LLM 交后台执行。

    默认同步（服务内部/测试可直接用）；HTTP 路由传 `background=True`。
    """
    if trigger_kind not in TRIGGER_KINDS:
        _invalid("trigger_kind", "trigger_kind 仅支持 manual|signal|sched")
    settings = _ensure_settings(db)
    horizon = horizon or settings.default_horizon
    if horizon not in HORIZONS:
        _invalid("horizon", "horizon 仅支持 short|mid|long")
    dedupe = dedupe_key or _manual_dedupe()

    if goal_id is not None:
        goal = _load_goal(db, goal_id)
        if _active_version(_versions(db, goal.id)) is None:
            _invalid("goal_id", "目标无生效版本")

    gateway, provider_snapshot = build_gateway(db, gateway, provider)

    now = datetime.now().isoformat()
    suggestion = AdvisorSuggestion(
        trigger_kind=trigger_kind, trigger_ref=trigger_ref,
        dedupe_key=dedupe, horizon=horizon, status="generating",
        goal_id=goal_id, created_at=now)
    db.add(suggestion)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(ErrorCode.ADVISOR_DUPLICATE_TRIGGER, 409,
                       "重复触发（dedupe_key 已存在）",
                       details={"dedupe_key": dedupe})
    audit_service.record(db, make_change(
        "advisor_suggestion", suggestion.id, "create", None,
        {"trigger_kind": trigger_kind, "horizon": horizon,
         "info_set_id": None, "dedupe_key": dedupe}), actor="system")
    job = AdvisorJob(job_id=uuid.uuid4().hex, suggestion_id=suggestion.id,
                     status="queued", stage="queued", attempts=0,
                     created_at=now)
    db.add(job)
    db.commit()
    db.refresh(suggestion)

    if background:
        factory = _SESSION_FACTORY
        if factory is None:
            from sqlalchemy.orm import sessionmaker
            factory = sessionmaker(bind=db.get_bind())
        _submit_generation(suggestion.id, gateway, provider_snapshot,
                           session_factory=factory)
    else:
        run_generation(db, suggestion.id, gateway=gateway,
                       provider_snapshot=provider_snapshot)
    return {"suggestion_id": suggestion.id, "job_id": job.job_id,
            "status": "generating", "dedupe_key": dedupe}


def _latest_job(db: Session, suggestion_id: int) -> AdvisorJob | None:
    return db.execute(
        select(AdvisorJob).where(AdvisorJob.suggestion_id == suggestion_id)
        .order_by(AdvisorJob.created_at.desc(), AdvisorJob.job_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def run_generation(db: Session, suggestion_id: int, *, gateway,
                   provider_snapshot: dict):
    """生成主流程（失败整单终态；异常不冒泡到路由）。"""
    suggestion = db.get(AdvisorSuggestion, suggestion_id)
    if suggestion is None or suggestion.status != "generating":
        return
    job = _latest_job(db, suggestion.id)
    if job is not None:
        job.status, job.stage = "running", "info_set"
        job.started_at = job.started_at or datetime.now().isoformat()
        db.commit()

    try:
        frozen = freeze_info_set(
            db, DbReadModel(db), goal_id=suggestion.goal_id,
            trigger_kind=suggestion.trigger_kind,
            created_at=suggestion.created_at)
    except ValueError as exc:
        _finish_failed(db, suggestion, job, "generation_failed",
                       f"no_data: {exc}")
        return

    suggestion.info_set_id = frozen["id"]
    suggestion.goal_id = frozen["goal_id"] or suggestion.goal_id
    suggestion.goal_version_id = frozen["goal_version_id"]
    if job is not None:
        job.stage = "llm"
    db.commit()

    prompt = build_prompt(frozen)
    parsed = None
    error = None
    for attempt in range(2):               # 上限 1 次重试（spec §2.3）
        try:
            parsed = gateway.complete_json(
                prompt=prompt,
                schema={"type": "object"},
                model=provider_snapshot.get("model"))
            break
        except AdvisorLlmError as exc:
            error = str(exc)
        except Exception as exc:           # fake/第三方实现
            error = str(exc)
        if job is not None:
            job.attempts = attempt + 1
    if parsed is None:
        _finish_failed(db, suggestion, job, "generation_failed",
                       (error or "LLM 失败")[:200])
        return
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            pass

    gate_payload = dict(parsed) if isinstance(parsed, dict) else parsed
    if isinstance(gate_payload, dict):
        gate_payload["goal_version_id"] = suggestion.goal_version_id
    gate_result = run_gate(
        gate_payload, frozen, active_goal_version_id=suggestion.goal_version_id)
    suggestion.gate_json = json.dumps(gate_result, ensure_ascii=False)
    suggestion.gate_version = gate_result["gate_version"]
    suggestion.provider_id = provider_snapshot.get("id")
    suggestion.provider_label = provider_snapshot.get("label")
    suggestion.model = provider_snapshot.get("model")
    suggestion.prompt_version = PROMPT_VERSION
    suggestion.generated_at = datetime.now().isoformat()

    if gate_result["result"] != "passed":
        suggestion.status = "gate_failed"
        if job is not None:
            job.status, job.stage = "done", "gate"
            job.finished_at = datetime.now().isoformat()
        audit_service.record(db, make_change(
            "advisor_suggestion", suggestion.id, "generated", None,
            {"status": suggestion.status,
             "gate": {"result": gate_result["result"]},
             "provider_label": suggestion.provider_label,
             "model": suggestion.model,
             "prompt_version": suggestion.prompt_version,
             "gate_version": suggestion.gate_version}), actor="system")
        db.commit()
        return

    try:
        suggestion.title = render_template(parsed["title"], frozen)
        suggestion.summary_template = parsed["summary_template"]
        suggestion.summary_rendered = render_template(
            parsed["summary_template"], frozen)
        for item in parsed["items"]:
            db.add(AdvisorSuggestionItem(
                suggestion_id=suggestion.id, seq=item["seq"],
                subject_kind=item["subject_kind"],
                subject_ref=item.get("subject_ref"),
                direction=item["direction"],
                rationale_template=item["rationale_template"],
                rationale_rendered=render_template(
                    item["rationale_template"], frozen),
                info_refs_json=json.dumps(item["info_refs"],
                                          ensure_ascii=False)))
    except (KeyError, ValueError, TypeError) as exc:
        suggestion_id_saved = suggestion.id
        db.rollback()
        _finish_failed(db, suggestion_id_saved, job,
                       "generation_failed", f"渲染失败: {exc}"[:200])
        return

    suggestion.status = "awaiting_review"
    suggestion.expires_at = (
        datetime.fromisoformat(suggestion.created_at).date()
        + timedelta(days=EXPIRY_DAYS))
    if job is not None:
        job.status, job.stage = "done", "done"
        job.finished_at = datetime.now().isoformat()

    change = make_change(
        "advisor_suggestion", suggestion.id, "generated", None,
        {"status": suggestion.status,
         "gate": {"result": gate_result["result"]},
         "provider_label": suggestion.provider_label,
         "model": suggestion.model,
         "prompt_version": suggestion.prompt_version,
         "gate_version": suggestion.gate_version})
    audit_service.record(db, change, actor="system")
    db.commit()


def _finish_failed(db: Session, suggestion, job: AdvisorJob | None,
                   status: str, message: str):
    """终态落库（generation_failed）：rollback 半成品后只写状态/审计。"""
    suggestion_id = suggestion.id if hasattr(suggestion, "id") else suggestion
    job_id = job.job_id if job is not None else None
    db.rollback()
    suggestion = db.get(AdvisorSuggestion, suggestion_id)
    if suggestion is None:
        return
    suggestion.status = status
    suggestion.generated_at = datetime.now().isoformat()
    if job_id is not None:
        job = db.get(AdvisorJob, job_id)
        if job is not None:
            job.status = "error" if status == "generation_failed" else "done"
            job.stage = "done"
            job.error = message[:200]
            job.finished_at = datetime.now().isoformat()
    audit_service.record(db, make_change(
        "advisor_suggestion", suggestion.id, "generated", None,
        {"status": status, "error": message[:200]}), actor="system")
    db.commit()


# ------------------------------------------------------------------ 读取


def _gate_of(suggestion: AdvisorSuggestion) -> dict | None:
    if not suggestion.gate_json:
        return None
    try:
        return json.loads(suggestion.gate_json)
    except ValueError:
        return None


def _suggestion_summary(db: Session, row: AdvisorSuggestion) -> dict:
    gate = _gate_of(row)
    failure = None
    if gate is not None and gate.get("result") == "failed":
        broken = next((c for c in gate.get("checks") or []
                       if not c.get("passed")), None)
        if broken is not None:
            failure = {"code": broken.get("code"),
                       "label": GATE_LABELS.get(broken.get("code"),
                                                broken.get("code"))}
    return {
        "id": row.id, "status": row.status, "horizon": row.horizon,
        "title": row.title,
        "summary": row.summary_rendered,
        "gate_failure": failure,
        "trigger": {"kind": row.trigger_kind, "ref": row.trigger_ref},
        "gate_result": gate.get("result") if gate else None,
        "goal": {"goal_id": row.goal_id, "version_id": row.goal_version_id},
        "created_at": row.created_at, "generated_at": row.generated_at,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def list_suggestions(db: Session, *, status: str | None = None,
                     horizon: str | None = None, goal_id: int | None = None,
                     page: int = 1, page_size: int = 20) -> dict:
    if status is not None and status not in STATUSES:
        _invalid("status", "status 非法")
    if horizon is not None and horizon not in HORIZONS:
        _invalid("horizon", "horizon 非法")
    query = select(AdvisorSuggestion)
    if status:
        query = query.where(AdvisorSuggestion.status == status)
    if horizon:
        query = query.where(AdvisorSuggestion.horizon == horizon)
    if goal_id is not None:
        query = query.where(AdvisorSuggestion.goal_id == goal_id)
    total = db.execute(
        select(func.count()).select_from(query.subquery())).scalar() or 0
    order = case((AdvisorSuggestion.status == "awaiting_review", 0), else_=1)
    rows = db.execute(
        query.order_by(order, AdvisorSuggestion.created_at.desc(),
                       AdvisorSuggestion.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {"items": [_suggestion_summary(db, r) for r in rows],
            "total": int(total), "page": page, "page_size": page_size,
            "total_pages": (int(total) + page_size - 1) // page_size}


def _load_suggestion(db: Session, suggestion_id: int) -> AdvisorSuggestion:
    row = db.get(AdvisorSuggestion, suggestion_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404,
                       f"建议不存在: {suggestion_id}",
                       details={"suggestion_id": suggestion_id})
    return row


def suggestion_detail(db: Session, suggestion_id: int) -> dict:
    row = _load_suggestion(db, suggestion_id)
    items = db.execute(
        select(AdvisorSuggestionItem)
        .where(AdvisorSuggestionItem.suggestion_id == row.id)
        .order_by(AdvisorSuggestionItem.seq)).scalars().all()
    decisions = db.execute(
        select(AdvisorDecision)
        .where(AdvisorDecision.suggestion_id == row.id)
        .order_by(AdvisorDecision.id)).scalars().all()
    info = db.get(AdvisorInfoSet, row.info_set_id) if row.info_set_id else None
    version = db.get(GoalVersion, row.goal_version_id) if row.goal_version_id else None
    gate = _gate_of(row)
    return {
        "id": row.id, "status": row.status, "horizon": row.horizon,
        "goal": {"goal_id": row.goal_id,
                 "version_no": version.version_no if version else None,
                 "goal_version_id": row.goal_version_id},
        "trigger": {"kind": row.trigger_kind, "ref": row.trigger_ref},
        "title": row.title,
        "summary": row.summary_rendered,
        "items": [{
            "seq": i.seq, "subject_kind": i.subject_kind,
            "subject_ref": i.subject_ref, "direction": i.direction,
            "rationale": i.rationale_rendered,
            "info_refs": json.loads(i.info_refs_json or "[]"),
        } for i in items],
        "gate": gate,
        "info_set": ({
            "id": info.id,
            "as_of": info.as_of.isoformat() if info.as_of else None,
            "entry_count": info.entry_count,
            "payload_sha256": info.payload_sha256,
            "builder_version": info.builder_version,
        } if info else None),
        "provider": {"id": row.provider_id, "label": row.provider_label,
                     "model": row.model, "prompt_version": row.prompt_version},
        "decisions": [{
            "id": d.id, "action": d.action, "reason": d.reason,
            "execution_status": d.execution_status, "decided_at": d.decided_at,
            "actor": d.actor,
        } for d in decisions],
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at, "generated_at": row.generated_at,
        "closed_at": row.closed_at,
        "meta": {"market_basis": "model_knowledge",
                 "degradations": [MARKET_DEGRADATION]},
    }


def get_info_set(db: Session, suggestion_id: int) -> dict:
    row = _load_suggestion(db, suggestion_id)
    if row.info_set_id is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, "建议尚未冻结信息集")
    info = load_info_set(db, row.info_set_id)
    if info is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, "信息集不存在")
    if not info["payload_ok"]:
        raise ApiError(ErrorCode.ADVISOR_INFO_SET_CORRUPT, 409,
                       "信息集重算不一致（疑似篡改/坏库）",
                       details={"info_set_id": info["id"]})
    return info


# ------------------------------------------------------------------ 裁决


def _ensure_reviewable(suggestion: AdvisorSuggestion):
    if suggestion.status != "awaiting_review":
        raise ApiError(ErrorCode.ADVISOR_SUGGESTION_CLOSED, 409,
                       "建议已终态或不可裁决",
                       details={"current_status": suggestion.status})


def _decision_out(decision: AdvisorDecision) -> dict:
    return {"id": decision.id, "action": decision.action,
            "reason": decision.reason,
            "execution_status": decision.execution_status,
            "decided_at": decision.decided_at, "actor": decision.actor}


def accept_suggestion(db: Session, suggestion_id: int,
                      reason: str | None = None) -> dict:
    return _decide(db, suggestion_id, "accept", reason)


def reject_suggestion(db: Session, suggestion_id: int,
                      reason: str | None = None) -> dict:
    row = _load_suggestion(db, suggestion_id)
    _ensure_reviewable(row)                 # 终态优先 → 409（而非 422）
    if not (reason or "").strip():
        _invalid("reason", "驳回必须填写理由")
    return _decide(db, suggestion_id, "reject", reason.strip())


def _decide(db: Session, suggestion_id: int, action: str,
            reason: str | None) -> dict:
    suggestion = _load_suggestion(db, suggestion_id)
    _ensure_reviewable(suggestion)
    before = suggestion.status
    now = datetime.now().isoformat()
    decision = AdvisorDecision(
        suggestion_id=suggestion.id, action=action, reason=reason,
        decided_at=now, actor="owner")
    db.add(decision)
    db.flush()
    suggestion.status = "accepted" if action == "accept" else "rejected"
    suggestion.closed_at = now
    after = {"status": suggestion.status}
    if reason:
        after["reason"] = reason
    change = make_change("advisor_decision", decision.id, action,
                         {"status": before}, after)
    audit_service.record(db, change)
    db.commit()
    db.refresh(suggestion)
    return {**suggestion_detail(db, suggestion.id), "change": change}


def record_execution(db: Session, suggestion_id: int, execution_status: str,
                     note: str | None = None) -> dict:
    suggestion = _load_suggestion(db, suggestion_id)
    if suggestion.status != "accepted":
        raise ApiError(ErrorCode.ADVISOR_NOT_ACCEPTED, 409,
                       "仅已采纳建议可记录执行情况",
                       details={"current_status": suggestion.status})
    if execution_status not in EXECUTION_STATUSES:
        _invalid("execution_status", "execution_status 仅支持 done|partial|not_done")
    text = (note or "").strip() or None

    latest = db.execute(
        select(AdvisorDecision)
        .where(AdvisorDecision.suggestion_id == suggestion.id,
               AdvisorDecision.action == "execution_note")
        .order_by(AdvisorDecision.id.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.execution_status == execution_status \
            and (latest.reason or None) == text:
        return {**suggestion_detail(db, suggestion.id), "change": None}

    now = datetime.now().isoformat()
    decision = AdvisorDecision(
        suggestion_id=suggestion.id, action="execution_note", reason=text,
        execution_status=execution_status, decided_at=now, actor="owner")
    db.add(decision)
    db.flush()
    change = make_change(
        "advisor_decision", decision.id, "execution_note", None,
        {"execution_status": execution_status, "note": text})
    audit_service.record(db, change)
    db.commit()
    return {**suggestion_detail(db, suggestion.id), "change": change}


def expire_due(db: Session, today: date | None = None) -> int:
    """过期扫描：awaiting_review 且 expires_at < today → expired（系统）。"""
    day = today or date.today()
    rows = db.execute(
        select(AdvisorSuggestion).where(
            AdvisorSuggestion.status == "awaiting_review",
            AdvisorSuggestion.expires_at.is_not(None),
            AdvisorSuggestion.expires_at < day)
    ).scalars().all()
    now = datetime.now().isoformat()
    for row in rows:
        before = row.status
        decision = AdvisorDecision(
            suggestion_id=row.id, action="expire", decided_at=now,
            actor="system")
        db.add(decision)
        db.flush()
        row.status = "expired"
        row.closed_at = now
        audit_service.record(db, make_change(
            "advisor_decision", decision.id, "expire",
            {"status": before}, {"status": "expired"}), actor="system")
    if rows:
        db.commit()
    return len(rows)


# ------------------------------------------------------------------ 自动触发


def _cooldown_ready(settings: AdvisorSettings, now: datetime) -> bool:
    if not settings.auto_enabled:
        return False
    if not settings.last_auto_at:
        return True
    try:
        last = datetime.fromisoformat(settings.last_auto_at)
    except ValueError:
        return True
    return (now - last).days >= settings.cooldown_days


def auto_trigger_candidates(db: Session, today: date | None = None) -> list[dict]:
    """信号优先，其次定时；关闭/冷却中 → []。"""
    settings = _ensure_settings(db)
    now = datetime.now()
    if not _cooldown_ready(settings, now):
        return []
    day = today or now.date()
    status = metric_service.get_status(db, None, include_details=False)
    candidates = []
    for domain in status.domains:
        if domain.state == "red":
            ref = f"signal:{domain.domain}:{day.isoformat()}"
            candidates.append({"trigger_kind": "signal", "trigger_ref": ref,
                               "dedupe_key": ref})
    for event in status.events:
        if event.status == "new":
            ref = f"signal:event:{event.rule_id}:{event.period}"
            candidates.append({"trigger_kind": "signal", "trigger_ref": ref,
                               "dedupe_key": ref})
    if not candidates:
        ref = f"sched:{day.isoformat()}"
        candidates.append({"trigger_kind": "sched", "trigger_ref": ref,
                           "dedupe_key": ref})
    return candidates


def run_auto(db: Session, gateway=None, today: date | None = None) -> list[int]:
    """执行一次自动触发（无供应商/重复 → 静默跳过）。"""
    settings = _ensure_settings(db)
    if not _cooldown_ready(settings, datetime.now()):
        return []
    created: list[int] = []
    candidates = auto_trigger_candidates(db, today)
    if len(candidates) > AUTO_TRIGGER_LIMIT:
        skipped = [c["dedupe_key"] for c in candidates[AUTO_TRIGGER_LIMIT:]]
        logger.warning("advisor_auto_trigger_limit candidates=%s skipped=%s",
                       len(candidates), skipped)
        audit_service.record(db, make_change(
            "advisor_auto_trigger", None, "limit", None,
            {"candidates": len(candidates), "limit": AUTO_TRIGGER_LIMIT,
             "skipped": skipped}), actor="system")
        db.commit()
        candidates = candidates[:AUTO_TRIGGER_LIMIT]
    for candidate in candidates:
        try:
            result = create_suggestion(
                db, trigger_kind=candidate["trigger_kind"],
                trigger_ref=candidate["trigger_ref"],
                dedupe_key=candidate["dedupe_key"], gateway=gateway)
            created.append(result["suggestion_id"])
        except ApiError as exc:
            if exc.code in (ErrorCode.ADVISOR_DUPLICATE_TRIGGER,
                            ErrorCode.ADVISOR_PROVIDER_UNAVAILABLE):
                continue
            raise
    if created:
        settings.last_auto_at = datetime.now().isoformat()
        db.commit()
    return created


# ------------------------------------------------------------------ 任务续跑 / 定时入口


def resume_pending(db: Session, gateway=None) -> int:
    """启动续跑：running→queued；对仍 generating 的建议重跑生成。"""
    jobs = db.execute(
        select(AdvisorJob).where(AdvisorJob.status.in_(("queued", "running")))
    ).scalars().all()
    for job in jobs:
        job.status, job.stage = "queued", "queued"
    db.commit()
    if not jobs:
        return 0
    gateway, snapshot = build_gateway(db, gateway)
    for job in jobs:
        if job.suggestion_id:
            run_generation(db, job.suggestion_id, gateway=gateway,
                           provider_snapshot=snapshot)
    return len(jobs)


def expire_daily() -> int:
    """调度器入口：每日过期扫描（失败仅日志）。"""
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            count = expire_due(db)
            if count:
                logger.info("advisor_expired count=%s", count)
            return count
    except Exception:
        logger.exception("advisor_expire_failed")
        return 0


def auto_trigger_daily() -> list[int]:
    """调度器入口：每日自动触发（默认关闭；失败仅日志）。"""
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            return run_auto(db)
    except Exception:
        logger.exception("advisor_auto_trigger_failed")
        return []