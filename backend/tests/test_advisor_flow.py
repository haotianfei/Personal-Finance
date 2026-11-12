"""三期-3 W4/W5 生成管线＋裁决闭环测试（spec §2.9/§6；ADR-0013 D5–D9）。

覆盖：fake 网关全链路（信息集→生成→门→awaiting_review）、门失败终态、
LLM 失败/无供应商降级、accept/reject/execution 状态机＋幂等、过期扫描、
settings 校验、自动触发默认关/冷却、重启续跑最小实现、账本零写入、
prompt 不含精确金额。数据全部合成。
"""
import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode
from models import (Asset, AssetClass, AssetItem, AssetItemOwnerChange,
                    AuditChainState, AdvisorJob, AdvisorSuggestion, AuditLog,
                    Institution, LineageLink, Member, PrincipalChange,
                    TransferEvent)
from services import classification_service, goal_service
from services.advisor import service
from services.advisor.llm import AdvisorLlmError
from services.advisor.prompt import build_prompt
from services.advisor.read import DbReadModel
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware

M0019 = _load_script_module(f"{_MIGRATIONS_DIR}/0019_advisor.py")
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
TODAY = date.today()


class FakeGateway:
    """可编程 fake（默认返回合法占位符建议）。"""

    def __init__(self):
        self.result = {
            "title": "示例建议",
            "summary_template": "当前净资产 {{info.nw.total}}，保持观察",
            "items": [{
                "seq": 1, "subject_kind": "narrative", "subject_ref": None,
                "direction": "hold",
                "rationale_template": "净资产 {{info.nw.total}}，无需调整",
                "info_refs": ["nw.total"],
            }],
        }
        self.error = None
        self.calls = 0

    def complete_json(self, *, prompt, schema, model=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _run_script(conn, module):
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@pytest.fixture()
def advisor_app(tmp_path):
    path = str(tmp_path / "advice.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    _run_script(conn, M0010)
    _run_script(conn, M0019)
    conn.close()
    Session = sessionmaker(bind=engine)

    def _seed():
        db = Session()
        # ADR-0019：分类（机构/类别/流动性/归属）在**资产项**上，记录只读
        account = Institution(name="示例账户甲", kind="bank")
        klass = AssetClass(code="T1", name="示例类别", level=1, side_scope="any")
        owner = Member(name="本人", role="self")
        db.add_all([account, klass, owner])
        db.commit()
        account_id = account.id
        record = create_asset(db, "示例-基金", Decimal("15000"), TODAY)
        classification_service.set_classification(db, record.asset_item_id, {
            "asset_class_id": klass.id, "liquidity_days": 0,
            "institution_id": account.id, "owner_id": owner.id,
        })
        goal, version, _ = goal_service.create_goal(
            db, name="示例目标", target_amount="30000.00",
            target_date=date(2030, 12, 31), target_rate_annual="0.05",
            reason="合成")
        goal_id, version_id = goal.id, version.id
        db.close()
        return {"account_id": account_id, "goal_id": goal_id,
                "version_id": version_id}

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import advisor as advisor_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(advisor_router.router, prefix="/api/planning/advice")
    service.configure_worker(session_factory=Session)
    try:
        yield {"app": app, "Session": Session, "seed": _seed, "path": path}
    finally:
        service.reset_worker()


def _patch_gateway(monkeypatch, fake):
    def _build(db, gateway=None, provider=None):
        return gateway or fake, {"id": None, "label": "fake", "model": "fake"}
    monkeypatch.setattr(service, "build_gateway", _build)


def _wait(client, suggestion_id, timeout=10.0):
    """轮询至非 generating 终态（后台执行器）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(
            f"/api/planning/advice/{suggestion_id}").json()
        if detail.get("status") != "generating":
            return detail
        time.sleep(0.02)
    raise AssertionError(f"建议未在 {timeout}s 内完成: {suggestion_id}")


def _create(client, **body):
    response = client.post("/api/planning/advice", json=body)
    assert response.status_code == 202, response.text
    return response.json()


# ---------------------------------------------------------------- 全链路


def test_post_returns_immediately_and_polls_to_terminal(advisor_app, monkeypatch):
    advisor_app["seed"]()

    class BlockingGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def complete_json(self, *, prompt, schema, model=None):
            self.calls += 1
            self.release.wait(5)
            return self.result

    fake = BlockingGateway()
    _patch_gateway(monkeypatch, fake)
    client = TestClient(advisor_app["app"])
    created = _create(client)
    # 后台仍在等待 LLM：接口已返回且状态为 generating
    immediate = client.get(
        f"/api/planning/advice/{created['suggestion_id']}").json()
    assert created["status"] == "generating"
    assert immediate["status"] == "generating"
    fake.release.set()
    detail = _wait(client, created["suggestion_id"])
    assert detail["status"] == "awaiting_review"


def test_generation_success_awaiting_review(advisor_app, monkeypatch):
    seed = advisor_app["seed"]()
    fake = FakeGateway()
    _patch_gateway(monkeypatch, fake)
    client = TestClient(advisor_app["app"])

    created = _create(client, horizon="mid")
    assert created["status"] == "generating" and created["job_id"]
    detail = _wait(client, created["suggestion_id"])
    assert detail["status"] == "awaiting_review"
    assert detail["gate"]["result"] == "passed"
    assert "15,000.00" in detail["summary"]          # 服务端插值（非模型）
    assert detail["items"][0]["rationale"].startswith("净资产 15,000.00")
    assert detail["provider"]["label"] == "fake"
    assert detail["info_set"]["entry_count"] > 0
    assert detail["goal"]["version_no"] == 1
    assert detail["meta"]["market_basis"] == "model_knowledge"

    info = client.get(
        f"/api/planning/advice/{created['suggestion_id']}/info-set").json()
    assert info["payload_ok"] is True and info["entries"]
    assert fake.calls == 1


def test_gate_failure_is_terminal_with_audit(advisor_app, monkeypatch):
    advisor_app["seed"]()
    fake = FakeGateway()
    fake.result = {
        "title": "示例建议",
        "summary_template": "建议增持 12.3%",
        "items": [{"seq": 1, "subject_kind": "narrative",
                   "direction": "hold",
                   "rationale_template": "建议增持 12.3%",
                   "info_refs": ["nw.total"]}],
    }
    _patch_gateway(monkeypatch, fake)
    client = TestClient(advisor_app["app"])

    created = _create(client)
    detail = _wait(client, created["suggestion_id"])
    assert detail["status"] == "gate_failed"
    assert detail["gate"]["result"] == "failed"
    assert detail["items"] == []
    db = advisor_app["Session"]()
    audit = db.execute(select(AuditLog).where(
        AuditLog.entity == "advisor_suggestion",
        AuditLog.action == "generated")).scalars().all()
    assert audit and json.loads(audit[-1].after_json)["status"] == "gate_failed"
    db.close()


def test_llm_failure_marks_generation_failed(advisor_app, monkeypatch):
    advisor_app["seed"]()
    fake = FakeGateway()
    fake.error = AdvisorLlmError("timeout")
    _patch_gateway(monkeypatch, fake)
    client = TestClient(advisor_app["app"])

    created = _create(client)
    detail = _wait(client, created["suggestion_id"])
    assert detail["status"] == "generation_failed"
    assert fake.calls == 2                          # 上限 1 次重试
    db = advisor_app["Session"]()
    job = db.execute(select(AdvisorJob).where(
        AdvisorJob.suggestion_id == created["suggestion_id"])).scalar_one()
    assert job.status == "error" and job.error
    db.close()


def test_no_provider_returns_409(advisor_app, monkeypatch):
    advisor_app["seed"]()
    client = TestClient(advisor_app["app"])
    response = client.post("/api/planning/advice", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ADVISOR_PROVIDER_UNAVAILABLE"


# ---------------------------------------------------------------- 裁决状态机


def test_accept_then_closed_and_execution(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    client = TestClient(advisor_app["app"])
    sid = _create(client)["suggestion_id"]
    _wait(client, sid)

    accepted = client.post(f"/api/planning/advice/{sid}/accept",
                           json={"reason": "同意方向"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["change"]["action"] == "accept"

    again = client.post(f"/api/planning/advice/{sid}/accept", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ADVISOR_SUGGESTION_CLOSED"
    assert again.json()["error"]["details"]["current_status"] == "accepted"

    executed = client.post(f"/api/planning/advice/{sid}/execution",
                           json={"execution_status": "partial",
                                 "note": "本月调仓一次"})
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "accepted"   # 执行备注不改状态
    assert executed.json()["change"]["action"] == "execution_note"

    # 同值不重复追加（无新审计行）
    same = service.record_execution(
        advisor_app["Session"](), sid, "partial", "本月调仓一次")
    assert same["change"] is None
    assert len(same["decisions"]) == 2               # accept + 1 execution_note


def test_reject_requires_reason_and_closes(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    client = TestClient(advisor_app["app"])
    sid = _create(client)["suggestion_id"]
    _wait(client, sid)

    missing = client.post(f"/api/planning/advice/{sid}/reject", json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["fields"][0]["field"] == "reason"

    rejected = client.post(f"/api/planning/advice/{sid}/reject",
                           json={"reason": "方向不认同"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["closed_at"]


def test_execution_requires_accepted(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    client = TestClient(advisor_app["app"])
    sid = _create(client)["suggestion_id"]
    _wait(client, sid)
    response = client.post(f"/api/planning/advice/{sid}/execution",
                           json={"execution_status": "done"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ADVISOR_NOT_ACCEPTED"


def test_expiry_scan(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    client = TestClient(advisor_app["app"])
    sid = _create(client)["suggestion_id"]
    _wait(client, sid)

    db = advisor_app["Session"]()
    row = db.get(AdvisorSuggestion, sid)
    row.expires_at = TODAY - timedelta(days=1)
    db.commit()
    assert service.expire_due(db) == 1
    assert db.get(AdvisorSuggestion, sid).status == "expired"
    assert service.expire_due(db) == 0
    decision = db.execute(select(models.AdvisorDecision).where(
        models.AdvisorDecision.suggestion_id == sid)).scalar_one()
    assert decision.action == "expire" and decision.actor == "system"
    db.close()


# ---------------------------------------------------------------- 幂等/去重/设置


def test_idempotent_create_replays(advisor_app, monkeypatch):
    advisor_app["seed"]()
    fake = FakeGateway()
    _patch_gateway(monkeypatch, fake)
    client = TestClient(advisor_app["app"])
    headers = {"Idempotency-Key": "adv-1"}
    first = client.post("/api/planning/advice", json={"horizon": "mid"},
                        headers=headers)
    second = client.post("/api/planning/advice", json={"horizon": "mid"},
                         headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.text == second.text
    _wait(client, first.json()["suggestion_id"])
    db = advisor_app["Session"]()
    assert db.execute(select(func.count()).select_from(
        AdvisorSuggestion)).scalar() == 1
    db.close()
    reused = client.post("/api/planning/advice", json={"horizon": "long"},
                         headers=headers)
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_duplicate_dedupe_key_409(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    db = advisor_app["Session"]()
    service.create_suggestion(db, dedupe_key="manual:same")
    with pytest.raises(ApiError) as ei:
        service.create_suggestion(db, dedupe_key="manual:same")
    assert ei.value.code == ErrorCode.ADVISOR_DUPLICATE_TRIGGER
    db.close()


def test_settings_validation_audit_and_idempotency(advisor_app):
    advisor_app["seed"]()
    client = TestClient(advisor_app["app"])
    current = client.get("/api/planning/advice/settings").json()
    assert current["auto_enabled"] is False and current["cooldown_days"] == 7

    bad = client.put("/api/planning/advice/settings",
                     json={"cooldown_days": -1})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["fields"][0]["field"] == "cooldown_days"

    headers = {"Idempotency-Key": "settings-1"}
    updated = client.put("/api/planning/advice/settings",
                         json={"auto_enabled": True, "cooldown_days": 3,
                               "default_horizon": "short"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["auto_enabled"] is True
    assert updated.json()["change"]["before"]["cooldown_days"] == 7
    replay = client.put("/api/planning/advice/settings",
                        json={"auto_enabled": True, "cooldown_days": 3,
                              "default_horizon": "short"}, headers=headers)
    assert replay.text == updated.text
    db = advisor_app["Session"]()
    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "advisor_settings")).scalars().all()
    assert len(audits) == 1
    db.close()


# ---------------------------------------------------------------- 自动触发/续跑


def test_auto_trigger_default_off_and_cooldown(advisor_app, monkeypatch):
    advisor_app["seed"]()
    fake = FakeGateway()
    _patch_gateway(monkeypatch, fake)
    db = advisor_app["Session"]()
    assert service.run_auto(db) == []               # 默认关闭
    settings = service._ensure_settings(db)
    settings.auto_enabled = 1
    settings.last_auto_at = None
    db.commit()
    created = service.run_auto(db)
    assert len(created) == 1
    assert db.get(AdvisorSuggestion, created[0]).trigger_kind in ("signal", "sched")
    assert service.run_auto(db) == []               # 冷却期内
    db.close()


def test_resume_pending_resets_jobs(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    db = advisor_app["Session"]()
    suggestion = service.create_suggestion(db, dedupe_key="manual:resume")
    job = db.execute(select(AdvisorJob)).scalars().all()[-1]
    job.status = "running"
    db.commit()
    assert service.resume_pending(db) == 1
    assert db.get(AdvisorJob, job.job_id).status == "queued"
    db.close()


# ---------------------------------------------------------------- 隐私/零账本


def test_prompt_contains_scaled_not_exact_money(advisor_app):
    advisor_app["seed"]()
    db = advisor_app["Session"]()
    frozen = service.freeze_info_set(db, DbReadModel(db))
    prompt = build_prompt(frozen)
    assert "15000.00" not in prompt              # 精确金额不外发
    assert "1.5" in prompt                        # value_scaled（万元/1 位）
    db.close()


def test_info_set_corrupt_returns_409(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    client = TestClient(advisor_app["app"])
    sid = _create(client)["suggestion_id"]
    _wait(client, sid)
    conn = sqlite3.connect(advisor_app["path"])
    conn.execute("DROP TRIGGER IF EXISTS advisor_info_entries_no_update")
    conn.execute("UPDATE advisor_info_entries SET value='999.00' WHERE id=1")
    conn.commit()
    conn.close()
    response = client.get(f"/api/planning/advice/{sid}/info-set")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ADVISOR_INFO_SET_CORRUPT"


def test_no_ledger_writes_across_flow(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    db = advisor_app["Session"]()

    def snapshot():
        return (
            db.execute(select(func.count()).select_from(Asset)).scalar(),
            db.execute(select(func.max(Asset.id))).scalar(),
            db.execute(select(func.count()).select_from(AssetItem)).scalar(),
            db.execute(select(func.max(AssetItem.id))).scalar(),
            db.execute(select(func.count()).select_from(PrincipalChange)).scalar(),
            db.execute(select(func.count()).select_from(TransferEvent)).scalar(),
            db.execute(select(func.count()).select_from(LineageLink)).scalar(),
            db.execute(select(func.count()).select_from(
                AssetItemOwnerChange)).scalar(),
        )

    before = snapshot()
    created = service.create_suggestion(db)
    service.accept_suggestion(db, created["suggestion_id"], "同意")
    service.record_execution(db, created["suggestion_id"], "done", "已完成")
    assert snapshot() == before
    db.close()


def test_list_filters_and_order(advisor_app, monkeypatch):
    advisor_app["seed"]()
    _patch_gateway(monkeypatch, FakeGateway())
    db = advisor_app["Session"]()
    first = service.create_suggestion(db, dedupe_key="manual:l1")
    second = service.create_suggestion(db, dedupe_key="manual:l2")
    service.accept_suggestion(db, first["suggestion_id"], "ok")
    db.close()

    db = advisor_app["Session"]()
    listing = service.list_suggestions(db)
    assert listing["total"] == 2
    assert listing["items"][0]["status"] == "awaiting_review"   # 待审优先
    assert service.list_suggestions(db, status="accepted")["total"] == 1
    with pytest.raises(ApiError):
        service.list_suggestions(db, status="bogus")
    db.close()