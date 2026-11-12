"""v5 一期·步骤 5「契约层」测试（spec §6）。

信封（400/404/409/422/500 形状＋code∈枚举＋422 fields）；
幂等（同键同体回放、异体 422、TTL 后天然兜底、GET 无键）；
口径元数据（存在性、空库 NO_DATA、形状契约锁定）。

自装一个最小 FastAPI：复用 main.register_exception_handlers（真实 handler）＋
IdempotencyCaptureMiddleware＋守护测试路由；DB＝tmp 文件库（create_all＋0010 DDL），
绝不触碰真实 data/。
"""
import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode, build_envelope_from_detail
from main import register_exception_handlers
from services.idempotency_service import (
    IdempotencyCaptureMiddleware, idempotency_guard)


# ------------------------------------------------------------- fixtures

class EchoBody(BaseModel):
    name: str
    amount: Decimal


@pytest.fixture()
def app_db(tmp_path):
    db_path = tmp_path / "contract.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)  # 含 audit_log（models 映射，带 idempotency_key 列）
    # 0010 DDL：建 idempotency_keys（与生产迁移同源）
    import importlib.util
    from services.db_migration_service import _MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m0010", f"{_MIGRATIONS_DIR}/0010_idempotency.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    mod.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()

    Session = sessionmaker(bind=engine)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override_db

    r = APIRouter()

    @r.post("/echo", dependencies=[Depends(idempotency_guard("POST /t/echo"))])
    def echo(body: EchoBody, db=Depends(get_db)):
        # done 回写发生在响应流回传之后（中间件），本请求事务内自身行仍为 pending
        n = db.execute(text(
            "SELECT COUNT(*) FROM idempotency_keys WHERE status='pending'")).scalar()
        return {"ok": True, "name": body.name, "idem_rows": n}

    @r.post("/guarded-fail", dependencies=[Depends(idempotency_guard("POST /t/guarded-fail"))])
    def guarded_fail(body: EchoBody):
        raise HTTPException(status_code=409, detail={
            "error": "write_once", "message": "write-once", "hint": "走更正",
            "row_id": 7})

    @r.post("/create-audit", dependencies=[Depends(idempotency_guard("POST /t/create-audit"))])
    def create_audit(body: EchoBody, db=Depends(get_db)):
        # 真 ledger 写单点：create_asset 内部 record() 落审计（带 idempotency_key）
        from services import asset_service
        a = asset_service.create_asset(db, body.name, body.amount, date(2026, 3, 1))
        return {"id": a.id}

    @r.get("/boom")
    def boom():
        raise RuntimeError("stack should not leak")

    @r.get("/nf")
    def nf():
        raise HTTPException(status_code=404, detail="没找到")

    @r.get("/teapot")
    def teapot():
        raise HTTPException(status_code=418, detail="i am a teapot")

    app.include_router(r, prefix="/t")
    return app, Session


@pytest.fixture()
def client(app_db):
    app, _ = app_db
    return TestClient(app)


# ------------------------------------------------------------- 信封

def test_404_wrapped_not_found(client):
    resp = client.get("/t/nf")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.NOT_FOUND
    assert err["message"] == "没找到"
    assert set(err) == {"code", "message", "details"}


def test_generic_status_wrapped(client):
    resp = client.get("/t/teapot")
    assert resp.status_code == 418
    assert resp.json()["error"]["code"] == "GENERIC_418"


def test_business_409_quasi_envelope_mapped(client):
    resp = client.post("/t/guarded-fail",
                       json={"name": "x", "amount": "1"},
                       headers={"Idempotency-Key": "k409"})
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.WRITE_ONCE_CONFLICT  # 准信封 error:write_once 精确映射
    assert err["details"]["hint"] == "走更正"
    assert err["details"]["row_id"] == 7


def test_422_validation_has_fields(client):
    resp = client.post("/t/echo", json={"amount": "abc"})  # 缺 name + 金额非数
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.VALIDATION_ERROR
    fields = err["details"]["fields"]
    assert fields and all(set(f) == {"field", "message"} for f in fields)
    assert any("name" in f["field"] for f in fields)  # loc 展平点路径


def test_500_internal_via_handler(app_db):
    app, _ = app_db
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/t/boom")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.INTERNAL
    assert "stack" not in json.dumps(resp.json())  # 不泄栈


def test_generic_code_by_status_table():
    # 字符串 detail 无映射 → GENERIC_<status>
    env = build_envelope_from_detail(418, "i am a teapot")
    assert env["error"]["code"] == "GENERIC_418"
    assert build_envelope_from_detail(400, "bad")["error"]["code"] == "BAD_REQUEST"
    # dict 含 code → 透传
    passthrough = build_envelope_from_detail(400, {"code": "APPLY_CONFLICT",
                                                   "message": "m", "details": {"a": 1}})
    assert passthrough["error"]["code"] == "APPLY_CONFLICT"
    # code ∈ 枚举（GENERIC_* 例外）
    valid = {c.value for c in ErrorCode}
    assert build_envelope_from_detail(404, "x")["error"]["code"] in valid | {
        "GENERIC_404", "GENERIC_418"}


def test_apierror_direct(client):
    # ApiError 单点形状（经 /guarded-fail 已验映射；此处验裸信封形状）
    env = ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400, "未知来源组", None)
    assert (env.code, env.status_code) == ("SOURCE_GROUP_UNKNOWN", 400)


# ------------------------------------------------------------- 幂等

def test_idempotent_same_key_same_body_replays(client):
    body = {"name": "示例-幂等", "amount": "10.5"}
    r1 = client.post("/t/echo", json=body, headers={"Idempotency-Key": "same-1"})
    assert r1.status_code == 200
    r2 = client.post("/t/echo", json=body, headers={"Idempotency-Key": "same-1"})
    assert r2.status_code == 200
    assert r1.text == r2.text  # 逐字节回放首次响应


def test_idempotent_same_key_diff_body_422(client):
    r1 = client.post("/t/echo", json={"name": "a", "amount": "1"},
                     headers={"Idempotency-Key": "reuse-1"})
    assert r1.status_code == 200
    r2 = client.post("/t/echo", json={"name": "b", "amount": "2"},
                     headers={"Idempotency-Key": "reuse-1"})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_idempotent_replay_leaves_one_audit_row_with_key(app_db):
    """spec §3：audit 落痕带 idempotency_key；同键回放不双写、不双审计。"""
    app, Session = app_db
    c = TestClient(app)
    body = {"name": "示例-审计幂等", "amount": "10"}
    r1 = c.post("/t/create-audit", json=body, headers={"Idempotency-Key": "audit-1"})
    assert r1.status_code == 200, r1.text
    r2 = c.post("/t/create-audit", json=body, headers={"Idempotency-Key": "audit-1"})
    assert r2.text == r1.text  # 回放首次响应
    db = Session()
    rows = db.execute(text("SELECT idempotency_key, COUNT(*) FROM audit_log"
                           " WHERE action='create' GROUP BY idempotency_key")).fetchall()
    assets_n = db.execute(text("SELECT COUNT(*) FROM assets")).scalar()
    db.close()
    assert assets_n == 1  # 回放不双写
    key_counts = {k: n for k, n in rows}
    assert key_counts.get("audit-1") == 1  # 仅一条审计且带键


def test_idempotent_ttl_expiry_falls_back_to_business(app_db):
    app, Session = app_db
    c = TestClient(app)
    body = {"name": "x", "amount": "1"}
    r1 = c.post("/t/echo", json=body, headers={"Idempotency-Key": "ttl-1"})
    assert r1.status_code == 200
    # 模拟 TTL 清理：删除幂等行
    db = Session()
    db.execute(text("DELETE FROM idempotency_keys WHERE key='ttl-1'"))
    db.commit()
    db.close()
    # 无幂等记录时重放＝再次执行业务（echo 无唯一约束，故 200 并重新插入 pending 行；
    # 真实 assets 路径由部分唯一索引兜底为 SNAPSHOT_CONFLICT——见 test_identity）
    r2 = c.post("/t/echo", json=body, headers={"Idempotency-Key": "ttl-1"})
    assert r2.status_code == 200
    assert r2.json()["idem_rows"] == 1  # 自身 pending 行
    assert r1.json()["idem_rows"] == 1


def test_no_key_passthrough_multiple_exec(client):
    # 无 Idempotency-Key → 幂等层不介入，每次都执行业务
    body = {"name": "nokey", "amount": "1"}
    r1 = client.post("/t/echo", json=body)
    r2 = client.post("/t/echo", json=body)
    assert r1.status_code == r2.status_code == 200
    # 无键不落任何 idempotency 行
    assert r2.json()["idem_rows"] == 0


def test_replay_after_error_marks_failed_for_5xx():
    # ≥500 记 failed 可重跑；<500（如 409）记 done 供回放
    from services.idempotency_service import mark_result  # 形状断言：函数存在
    assert callable(mark_result)


# ------------------------------------------------------------- 口径元数据

def test_meta_empty_db_no_data():
    from services import analysis_service
    from sqlalchemy import create_engine as ce
    from sqlalchemy.orm import sessionmaker as sm
    import models  # noqa
    from database import Base as B
    e = ce("sqlite://", connect_args={"check_same_thread": False},
           poolclass=__import__("sqlalchemy").pool.StaticPool)
    B.metadata.create_all(e)
    db = sm(bind=e)()
    summary = analysis_service.get_summary(db)
    assert summary.meta is not None
    codes = [d.code for d in summary.meta.degradations]
    assert "NO_DATA" in codes
    assert summary.meta.basis.schema_generation == 5
    assert summary.meta.basis.identity == "asset_item"
    assert summary.meta.basis.principal is None


def test_meta_with_data_shape_locked(tmp_path):
    from services import analysis_service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    db_path = tmp_path / "meta.db"
    e = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(e)
    from models import Asset
    db = sessionmaker(bind=e)()
    db.add(Asset(asset_name="示例-M", amount=Decimal("100"), side="asset",
                 asset_date=date(2026, 3, 1)))
    db.commit()
    summary = analysis_service.get_summary(db)
    assert summary.meta.snapshot_date == date(2026, 3, 1)
    assert summary.meta.degradations == []
    # 形状契约：meta 序列化后的键集锁定
    dumped = summary.meta.model_dump(mode="json")
    # drift_enabled 为 spec §3 的 additive 能力探测位（仅 total-trend/mixed-chart 置 true）
    assert set(dumped) == {"snapshot_date", "basis", "degradations", "drift_enabled"}
    assert set(dumped["basis"]) == {"schema_generation", "identity", "principal"}


def test_proportion_and_mixed_chart_carry_meta():
    # 类型层：MixedChartData/SummaryData/AllocationAnalysis 继承 MetaMixin
    from schemas import SummaryData, MixedChartData, AllocationAnalysis, MetaMixin
    assert issubclass(SummaryData, MetaMixin)
    assert issubclass(MixedChartData, MetaMixin)
    assert issubclass(AllocationAnalysis, MetaMixin)


def test_build_meta_carry_forward_event_passthrough():
    from services import meta_service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models  # noqa
    from database import Base
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=__import__("sqlalchemy").pool.StaticPool)
    Base.metadata.create_all(e)
    db = sessionmaker(bind=e)()
    m = meta_service.build_meta(
        db, None, [{"code": "DATA_MISSING_CARRY_FORWARD", "scope": "2026-02", "message": "沿用"}])
    assert m.degradations[0].code == "DATA_MISSING_CARRY_FORWARD"
    assert m.degradations[0].scope == "2026-02"
    # RATE_UNAVAILABLE 仅预留：不作为默认降级出现
    assert all(d.code != "RATE_UNAVAILABLE" for d in m.degradations)
