"""三期-4 W2/W4 复盘测试（spec §2/§5/§7；ADR-0014 D4–D9）。

覆盖：0021 双跑/触发器；ef 版本 as-of 解析（含 draft/archived）；阈值史 as-of
与无史降级；创建即冻结＋快照 sha；建议批次/候选；结论/关闭状态机；提案校验/
重复/采纳只记决定/驳回；快照篡改 409；幂等；列表；账本与目标表零写入。
数据全部合成。
"""
import json
import sqlite3
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
from models import (AllocationTarget, AllocationTargetRevision, Asset,
                    AssetClass, AssetItem, AssetItemOwnerChange, AuditLog,
                    GoalVersion, Institution, LineageLink, Member,
                    PrincipalChange, TransferEvent)
from services import allocation_service, classification_service, goal_service
from services.advisor import review as review_service
from services.advisor import service as advisor_service
from services.advisor.read import DbReadModel
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware
from schemas import AllocationTargetCreate

M0021 = _load_script_module(f"{_MIGRATIONS_DIR}/0021_advisor_reviews.py")
M0019 = _load_script_module(f"{_MIGRATIONS_DIR}/0019_advisor.py")
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
TODAY = date.today()
PRIOR = TODAY - timedelta(days=30)


class FakeGateway:
    def complete_json(self, *, prompt, schema, model=None):
        return {
            "title": "示例建议",
            "summary_template": "当前净资产 {{info.nw.total}}，保持观察",
            "items": [{"seq": 1, "subject_kind": "narrative",
                       "subject_ref": None, "direction": "hold",
                       "rationale_template": "净资产 {{info.nw.total}}，无需调整",
                       "info_refs": ["nw.total"]}],
        }


def _run(conn, module):
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _mk_record(db, name, amount, on_date):
    """一条记录 + 项级分类（分类是项的固有属性，与记录条数脱钩）。"""
    row = create_asset(db, name, Decimal(str(amount)), on_date)
    klass = db.execute(select(AssetClass)).scalars().first()
    institution = db.execute(select(Institution)).scalars().first()
    owner = db.execute(select(Member)).scalars().first()
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": klass.id, "liquidity_days": 0,
        "institution_id": institution.id, "owner_id": owner.id,
    })
    return row


@pytest.fixture()
def review_app(tmp_path):
    path = str(tmp_path / "review.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    for module in (M0010, M0019, M0021):
        _run(conn, module)
    conn.close()
    Session = sessionmaker(bind=engine)

    def _seed():
        db = Session()
        # ADR-0019：分类在**资产项**上（机构/类别/流动性/归属），记录只承载金额/日期。
        account = Institution(name="示例账户甲", kind="bank")
        klass = AssetClass(code="T1", name="示例类别", level=1, side_scope="any")
        owner = Member(name="本人", role="self")
        db.add_all([account, klass, owner])
        db.commit()
        account_id = account.id
        _mk_record(db, "示例-基金", "10000", PRIOR)
        _mk_record(db, "示例-基金", "12000", TODAY)
        goal, version, _ = goal_service.create_goal(
            db, name="示例目标", target_amount="30000.00",
            target_date=date(2030, 12, 31), target_rate_annual="0.05",
            reason="合成")
        version.effective_from = PRIOR
        db.commit()
        target = allocation_service.create_allocation_target(
            db, AllocationTargetCreate(
                dimension="institution", target_id=str(account_id),
                target_percent="50", alert_threshold="5"))
        created = advisor_service.create_suggestion(db, gateway=FakeGateway())
        advisor_service.accept_suggestion(
            db, created["suggestion_id"], "同意方向")
        ids = {"account_id": account_id, "goal_id": goal.id,
               "version_id": version.id, "target_id": target.id,
               "suggestion_id": created["suggestion_id"]}
        db.close()
        return ids

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import reviews as reviews_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override
    app.include_router(reviews_router.router, prefix="/api/planning/reviews")
    return {"app": app, "Session": Session, "seed": _seed, "path": path}


def _create(client, seed, **overrides):
    body = {"goal_id": seed["goal_id"], "as_of": TODAY.isoformat(),
            "start_as_of": PRIOR.isoformat(), "label": "示例季度复盘"}
    body.update(overrides)
    return client.post("/api/planning/reviews", json=body)


# ---------------------------------------------------------------- 迁移


def _legacy_tables(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE goals (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE goal_versions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE advisor_suggestions (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO goals VALUES (1)")
    conn.execute("INSERT INTO goal_versions VALUES (1)")
    conn.commit()
    return conn


def test_0021_double_run_and_triggers(tmp_path):
    path = str(tmp_path / "m.db")
    conn = _legacy_tables(path)
    _run(conn, M0021)
    _run(conn, M0021)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name LIKE 'advisor_review%'")}
    assert tables == {"advisor_reviews", "advisor_review_suggestions",
                      "advisor_review_proposals"}
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_advisor_reviews_goal",
            "uq_advisor_review_proposal_content"} <= indexes
    triggers = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"advisor_reviews_snapshot_immutable",
            "advisor_reviews_closed_immutable",
            "advisor_review_proposals_decided_immutable"} <= triggers
    conn.execute(
        "INSERT INTO advisor_reviews (goal_id, goal_version_id, as_of, status,"
        " snapshot_json, snapshot_sha256, created_at)"
        " VALUES (1,1,'2026-01-01','open','{}','h','t')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE advisor_reviews SET snapshot_json='{\"x\":1}'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM advisor_reviews")
    conn.close()


# ---------------------------------------------------------------- 版本 as-of


def test_resolve_version_as_of_boundaries(review_app):
    seed = review_app["seed"]()
    db = review_app["Session"]()
    assert goal_service.resolve_version_as_of(db, seed["goal_id"], PRIOR) is not None
    assert goal_service.resolve_version_as_of(
        db, seed["goal_id"], PRIOR - timedelta(days=1)) is None

    # 新版本：换代前一日取旧，换代当日取新；draft 永不参与
    new_version, _ = goal_service.create_version(
        db, seed["goal_id"], target_amount="31000.00",
        target_date=date(2031, 12, 31), reason="调高")
    new_version.effective_from = TODAY
    db.commit()
    old_id = seed["version_id"]
    assert goal_service.resolve_version_as_of(
        db, seed["goal_id"], PRIOR)[1].id == old_id
    assert goal_service.resolve_version_as_of(
        db, seed["goal_id"], TODAY)[1].id == new_version.id
    draft = GoalVersion(goal_id=seed["goal_id"], version_no=99, status="draft",
                        target_amount=Decimal("1"), target_date=date(2032, 1, 1),
                        reason="draft", created_at="t")
    db.add(draft)
    db.commit()
    assert goal_service.resolve_version_as_of(
        db, seed["goal_id"], date(2031, 1, 1))[1].id == new_version.id
    db.close()


# ---------------------------------------------------------------- 冻结快照


def test_create_review_freezes_snapshot(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    response = _create(client, seed)
    assert response.status_code == 201, response.text
    review = response.json()["review"]
    assert review["status"] == "open"
    assert review["goal_version_id"] == seed["version_id"]
    assert review["version_no"] == 1
    detail = client.get(f"/api/planning/reviews/{review['id']}").json()
    snap = detail["snapshot"]
    assert detail["payload_ok"] is True and snap["schema"] == 1
    assert snap["anchors"]["end"]["as_of"] == TODAY.isoformat()
    assert snap["anchors"]["start"]["as_of"] == PRIOR.isoformat()
    assert snap["goal"]["version_changed"] is False
    assert snap["delta"]["comparable"] is True
    assert snap["delta"]["net_worth_change"] == "2000.00"
    row = snap["allocation"]["rows"][0]
    assert row["band"]["threshold"] == "5.00"
    assert row["revision_id"] is not None
    assert snap["signals"]["basis"] == "current_rules"
    assert snap["hints"]["extend_1y"]["target_date"] == "2031-12-31"
    db = review_app["Session"]()
    audit = db.execute(select(AuditLog).where(
        AuditLog.entity == "review", AuditLog.action == "create")).scalars().all()
    assert len(audit) == 1
    after = json.loads(audit[0].after_json)
    assert after["snapshot_sha256"] == review["snapshot_sha256"]
    db.close()


def test_threshold_as_of_not_current_and_missing_history(review_app):
    seed = review_app["seed"]()
    db = review_app["Session"]()
    version = db.get(GoalVersion, seed["version_id"])
    version.effective_from = PRIOR - timedelta(days=4)
    db.commit()
    _mk_record(db, "示例-基金", "9000", PRIOR - timedelta(days=3))
    # 另一目标：两条史 r1(5) / r2(8)，当前表故意改成 12
    target = AllocationTarget(dimension="asset_class", target_id="77",
                              target_percent=Decimal("40"),
                              alert_threshold=Decimal("12"),
                              created_at=datetime.combine(
                                  PRIOR - timedelta(days=1), datetime.min.time()))
    db.add(target)
    db.flush()
    db.add_all([
        AllocationTargetRevision(
            allocation_target_id=target.id, op="create", dimension="asset_class",
            target_id="77", target_percent=Decimal("40"),
            alert_threshold=Decimal("5"),
            effective_date=PRIOR - timedelta(days=3),
            origin="manual", created_at="t1"),
        AllocationTargetRevision(
            allocation_target_id=target.id, op="update", dimension="asset_class",
            target_id="77", target_percent=Decimal("40"),
            alert_threshold=Decimal("8"), effective_date=PRIOR,
            origin="manual", created_at="t2"),
    ])
    db.commit()
    # 无史目标（当前有行、史表无行）
    no_history = AllocationTarget(dimension="liquidity",
                                  target_id="30",
                                  target_percent=Decimal("10"),
                                  alert_threshold=Decimal("5"),
                                  created_at=datetime.combine(
                                      PRIOR - timedelta(days=2),
                                      datetime.min.time()))
    db.add(no_history)
    db.commit()
    target_id = target.id
    db.close()

    client = TestClient(review_app["app"])
    created = _create(client, seed,
                      as_of=(PRIOR - timedelta(days=1)).isoformat(),
                      start_as_of=(PRIOR - timedelta(days=2)).isoformat())
    detail = client.get(
        f"/api/planning/reviews/{created.json()['review']['id']}").json()
    rows = {(r["dimension"], r["target_ref"]): r
            for r in detail["snapshot"]["allocation"]["rows"]}
    # as_of 在 r1 与 r2 之间 → 取 r1 的 5，而不是当前 12
    assert rows[("asset_class", "77")]["band"]["threshold"] == "5.00"
    # 流动性目标（档位天数 30）：新结构里它是**可解析**的维度，快照行带展示名；
    # 史表仍无行 → band/state 为 null 并出 REVIEW_BAND_HISTORY_MISSING。
    liq = [r for r in detail["snapshot"]["allocation"]["rows"]
           if r["dimension"] == "liquidity"]
    assert len(liq) == 1
    assert liq[0]["target_ref"] == classification_service.liquidity_label(30)
    # 目标百分比取自 revision（as-of 口径），无史即未知 → null，绝不回退到当前表
    assert liq[0]["target_percent"] is None
    assert liq[0]["history"] == "missing"
    assert liq[0]["band"] is None
    assert liq[0]["state"] is None
    codes = [d["code"] for d in detail["snapshot"]["degradations"]]
    assert "REVIEW_BAND_HISTORY_MISSING" in codes
    assert target_id


def test_create_review_errors(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    early = _create(client, seed, as_of="2020-01-01")
    assert early.status_code == 422 and early.json()["error"]["code"] == "VALIDATION_ERROR"
    bad_start = _create(client, seed, start_as_of=TODAY.isoformat())
    assert bad_start.status_code == 422
    assert bad_start.json()["error"]["details"]["fields"][0]["field"] == "start_as_of"


def test_deleted_target_not_in_rows(review_app):
    seed = review_app["seed"]()
    db = review_app["Session"]()
    allocation_service.delete_allocation_target(
        db, seed["target_id"], audit_reason="normalization")
    db.close()
    client = TestClient(review_app["app"])
    created = _create(client, seed)
    detail = client.get(
        f"/api/planning/reviews/{created.json()['review']['id']}").json()
    assert detail["snapshot"]["allocation"]["rows"] == []
    assert detail["snapshot"]["allocation"]["skipped_deleted_targets"] == 1


# ---------------------------------------------------------------- 建议批次/候选


def test_suggestions_batch_and_candidates(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    candidates = client.get("/api/planning/reviews/candidates",
                            params={"goal_id": seed["goal_id"],
                                    "as_of": TODAY.isoformat()}).json()
    assert candidates["total"] == 1
    assert candidates["items"][0]["suggestion_id"] == seed["suggestion_id"]

    created = _create(client, seed,
                      suggestion_ids=[seed["suggestion_id"]]).json()
    detail = client.get(f"/api/planning/reviews/{created['review']['id']}").json()
    assert detail["suggestions"][0]["status"] == "accepted"
    assert detail["suggestions"][0]["info_set"]["as_of"] == TODAY.isoformat()

    after = client.get("/api/planning/reviews/candidates",
                       params={"goal_id": seed["goal_id"],
                               "as_of": TODAY.isoformat()}).json()
    assert after["total"] == 0                     # 已被复盘引用

    # 后续执行备注不改写旧复盘快照
    db = review_app["Session"]()
    advisor_service.record_execution(db, seed["suggestion_id"], "partial", "新备注")
    db.close()
    again = client.get(f"/api/planning/reviews/{created['review']['id']}").json()
    assert again["suggestions"][0]["execution"] is None


def test_unaccepted_suggestion_rejected(review_app, monkeypatch):
    seed = review_app["seed"]()
    db = review_app["Session"]()
    created = advisor_service.create_suggestion(db, gateway=FakeGateway(),
                                               dedupe_key="manual:pending")
    db.close()
    client = TestClient(review_app["app"])
    response = _create(client, seed, suggestion_ids=[created["suggestion_id"]])
    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"][0]["field"] == "suggestion_ids"


# ---------------------------------------------------------------- 结论/关闭


def test_conclusion_and_close_state_machine(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]

    bad = client.put(f"/api/planning/reviews/{rid}/conclusion",
                     json={"attribution": [{"category": "bogus"}]})
    assert bad.status_code == 422

    ok = client.put(f"/api/planning/reviews/{rid}/conclusion", json={
        "attribution": [{"category": "not_executed",
                         "subject_kind": "suggestion",
                         "subject_ref": f"advisor:{seed['suggestion_id']}",
                         "note": "未执行"}],
        "conclusion": "配置仅移动 3pp",
    })
    assert ok.status_code == 200
    assert ok.json()["review"]["conclusion"] == "配置仅移动 3pp"
    assert ok.json()["change"]["action"] == "conclusion"

    no_conclusion = client.post(f"/api/planning/reviews/{rid}/close", json={})
    # 已通过 PUT 写入结论 → 关闭允许沿用
    assert no_conclusion.status_code == 200
    assert no_conclusion.json()["review"]["status"] == "closed"

    closed = client.put(f"/api/planning/reviews/{rid}/conclusion",
                        json={"conclusion": "再改"})
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "REVIEW_CLOSED"
    assert closed.json()["error"]["details"]["status"] == "closed"
    again = client.post(f"/api/planning/reviews/{rid}/close",
                        json={"conclusion": "再关"})
    assert again.status_code == 409


def test_close_requires_conclusion(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    response = client.post(f"/api/planning/reviews/{rid}/close", json={})
    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"][0]["field"] == "conclusion"


# ---------------------------------------------------------------- 提案


def _proposal(client, rid, **overrides):
    body = {"kind": "threshold",
            "subject_ref": f"allocation_target:{overrides.get('target_id', 1)}",
            "payload": {"allocation_target_id": overrides.get("target_id", 1),
                        "alert_threshold": "8"},
            "rationale": "示例理由"}
    body.update({k: v for k, v in overrides.items() if k != "target_id"})
    return client.post(f"/api/planning/reviews/{rid}/proposals", json=body)


def test_proposal_validation_and_duplicate(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    target_id = seed["target_id"]

    ok = _proposal(client, rid, target_id=target_id)
    assert ok.status_code == 201, ok.text
    assert ok.json()["proposal"]["status"] == "proposed"
    dup = _proposal(client, rid, target_id=target_id)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "REVIEW_PROPOSAL_DUPLICATE"

    bad_threshold = _proposal(client, rid, target_id=target_id,
                              payload={"allocation_target_id": target_id,
                                       "alert_threshold": "0"},
                              subject_ref=f"allocation_target:{target_id}")
    assert bad_threshold.status_code == 422

    narrative = _proposal(client, rid, kind="narrative", subject_ref=None,
                          payload={"text": "仅作备注"})
    assert narrative.status_code == 201

    goal_version = _proposal(client, rid, kind="goal_version",
                             subject_ref=f"goal:{seed['goal_id']}",
                             payload={"target_amount": "1000000",
                                      "target_date": "2031-12-31",
                                      "target_rate_annual": "0.06",
                                      "reason": "延长一年"})
    assert goal_version.status_code == 201

    bad_goal = _proposal(client, rid, kind="goal_version",
                         subject_ref="goal:999",
                         payload={"target_amount": "1",
                                  "target_date": "2031-12-31",
                                  "reason": "x"})
    assert bad_goal.status_code == 422


def test_proposal_adopt_only_records_decision(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    proposal = _proposal(client, rid, target_id=seed["target_id"]).json()["proposal"]

    db = review_app["Session"]()

    def ledger_snapshot():
        return tuple(
            (db.execute(select(func.count()).select_from(m)).scalar(),
             db.execute(select(func.max(m.id))).scalar())
            for m in (Asset, AssetItem, PrincipalChange, TransferEvent,
                      LineageLink, AssetItemOwnerChange, GoalVersion,
                      AllocationTarget))

    before = ledger_snapshot()
    response = client.post(
        f"/api/planning/reviews/{rid}/proposals/{proposal['id']}/adopt",
        json={"reason": "同意方向", "applied_kind": "threshold",
              "applied_ref": str(seed["target_id"])})
    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == "adopted"
    assert ledger_snapshot() == before

    again = client.post(
        f"/api/planning/reviews/{rid}/proposals/{proposal['id']}/adopt", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "REVIEW_PROPOSAL_DECIDED"

    db.close()


def test_narrative_cannot_adopt_and_dismiss_requires_reason(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    narrative = _proposal(client, rid, kind="narrative", subject_ref=None,
                          payload={"text": "备注"}).json()["proposal"]
    adopt = client.post(
        f"/api/planning/reviews/{rid}/proposals/{narrative['id']}/adopt", json={})
    assert adopt.status_code == 422

    threshold = _proposal(client, rid, target_id=seed["target_id"]).json()["proposal"]
    missing = client.post(
        f"/api/planning/reviews/{rid}/proposals/{threshold['id']}/dismiss", json={})
    assert missing.status_code == 422
    ok = client.post(
        f"/api/planning/reviews/{rid}/proposals/{threshold['id']}/dismiss",
        json={"reason": "不采纳"})
    assert ok.status_code == 200 and ok.json()["proposal"]["status"] == "dismissed"


def test_proposals_require_open_but_decisions_allowed_after_close(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    proposal = _proposal(client, rid, target_id=seed["target_id"]).json()["proposal"]
    client.post(f"/api/planning/reviews/{rid}/close", json={"conclusion": "结论"})

    blocked = _proposal(client, rid, target_id=seed["target_id"],
                        payload={"allocation_target_id": seed["target_id"],
                                 "alert_threshold": "9"},
                        subject_ref=f"allocation_target:{seed['target_id']}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "REVIEW_CLOSED"

    # 关闭后仍可裁决历史提案
    adopted = client.post(
        f"/api/planning/reviews/{rid}/proposals/{proposal['id']}/adopt", json={})
    assert adopted.status_code == 200


# ---------------------------------------------------------------- 篡改/幂等/列表


def test_snapshot_tamper_detected(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    conn = sqlite3.connect(review_app["path"])
    conn.execute("DROP TRIGGER IF EXISTS advisor_reviews_snapshot_immutable")
    conn.execute("UPDATE advisor_reviews SET snapshot_json='{\"schema\":1}'"
                 " WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    response = client.get(f"/api/planning/reviews/{rid}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_SNAPSHOT_CORRUPT"


def test_idempotent_create_replays(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    headers = {"Idempotency-Key": "rev-1"}
    body = {"goal_id": seed["goal_id"], "as_of": TODAY.isoformat(),
            "start_as_of": PRIOR.isoformat()}
    first = client.post("/api/planning/reviews", json=body, headers=headers)
    second = client.post("/api/planning/reviews", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.text == second.text
    db = review_app["Session"]()
    assert db.execute(select(func.count()).select_from(
        review_service.AdvisorReview)).scalar() == 1
    db.close()


def test_list_open_priority_and_proposal_counts(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    first = _create(client, seed).json()["review"]["id"]
    _proposal(client, first, target_id=seed["target_id"])
    second = _create(client, seed, label="第二个").json()["review"]["id"]
    client.post(f"/api/planning/reviews/{second}/close", json={"conclusion": "x"})

    listing = client.get("/api/planning/reviews").json()
    assert listing["total"] == 2
    assert listing["items"][0]["id"] == first        # open 优先
    assert listing["items"][0]["proposal_counts"]["proposed"] == 1
    assert listing["items"][0]["comparable"] is True
    filtered = client.get("/api/planning/reviews",
                          params={"status": "closed"}).json()
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == second


def test_proposal_allocation_kind_and_invalid_values(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    ok = _proposal(client, rid, kind="allocation_target",
                   subject_ref=f"allocation_target:{seed['target_id']}",
                   payload={"allocation_target_id": seed["target_id"],
                            "target_percent": "55"})
    assert ok.status_code == 201
    bad = _proposal(client, rid, kind="allocation_target",
                    subject_ref=f"allocation_target:{seed['target_id']}",
                    payload={"allocation_target_id": seed["target_id"],
                             "target_percent": "101"})
    assert bad.status_code == 422
    bad_date = _proposal(client, rid, kind="goal_version",
                         subject_ref=f"goal:{seed['goal_id']}",
                         payload={"target_amount": "1",
                                  "target_date": "not-a-date",
                                  "reason": "x"})
    assert bad_date.status_code == 422


def test_candidates_window_filters(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    after = client.get("/api/planning/reviews/candidates",
                       params={"goal_id": seed["goal_id"],
                               "as_of": TODAY.isoformat(),
                               "start_as_of": (TODAY + timedelta(days=1)).isoformat()})
    assert after.status_code == 422
    before = client.get("/api/planning/reviews/candidates",
                        params={"goal_id": seed["goal_id"],
                                "as_of": TODAY.isoformat(),
                                "start_as_of": PRIOR.isoformat()}).json()
    assert before["total"] == 1


def test_attribution_limit_and_no_active_goal(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    too_many = [{"category": "market_moved"} for _ in range(21)]
    response = client.put(f"/api/planning/reviews/{rid}/conclusion",
                          json={"attribution": too_many})
    assert response.status_code == 422

    db = review_app["Session"]()
    goal_service.archive_goal(db, seed["goal_id"],
                              audit_reason="normalization")
    db.close()
    blocked = _create(client, seed)
    assert blocked.status_code == 422
    assert blocked.json()["error"]["details"]["fields"][0]["field"] == "goal_id"


def test_review_detail_proposals_and_snapshot_derivation(review_app):
    seed = review_app["seed"]()
    client = TestClient(review_app["app"])
    rid = _create(client, seed).json()["review"]["id"]
    proposal = _proposal(client, rid, target_id=seed["target_id"]).json()["proposal"]
    detail = client.get(f"/api/planning/reviews/{rid}").json()
    assert detail["proposals"][0]["id"] == proposal["id"]
    assert detail["proposals"][0]["payload"]["alert_threshold"] == "8"
    assert detail["snapshot"]["anchors"]["end"]["goal_version_id"] == \
        seed["version_id"]


def test_review_isolation_ast_and_no_ledger_routes(review_app):
    from pathlib import Path
    import ast

    root = Path(__file__).resolve().parents[1]
    banned_services = {"asset_service", "principal_service", "transfer_service",
                       "lineage_service", "screenshot_service"}
    banned_models = {"Asset", "AssetItem", "AssetItemName",
                     "AssetItemOwnerChange", "PrincipalChange", "TransferEvent",
                     "LineageLink"}
    files = [root / "services/advisor/review.py", root / "routers/reviews.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update((node.module or "").split("."))
                names.update(a.name for a in node.names)
        flat = {n.split(".")[-1] for n in names}
        assert not (flat & banned_services), path.name
        assert not (flat & banned_models), path.name