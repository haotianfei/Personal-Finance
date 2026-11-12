"""LLM 供应商 CRUD + 脱敏 + is_default 唯一 + seed + 连通性测试（契约测试）。"""
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

import services.llm_verification_service as lv
from models import LLMProvider
from routers.llm_providers import (
    create_provider,
    delete_provider,
    list_providers,
    update_provider,
)
# 逻辑函数名 test_provider 会被 pytest 误收集为用例，导入时改名
from routers.llm_providers import test_provider as provider_connection_check
from services.llm_verification_service import seed_default_llm_providers
from services.db_migration_service import (
    TOKEN_PLAN_LLM_PROVIDER_SEED,
    seed_token_plan_llm_provider,
)


TOKEN_PLAN_LABEL = "百炼TokenPlan(个人版)"


def seed_all(db):
    """复刻启动路径 seed_llm_providers 的完整预置（两条表空 seed + 第三条按 label 补种）。"""
    seed_default_llm_providers(db)
    seed_token_plan_llm_provider(db)


def make_provider(db, **over):
    data = {"label": "DeepSeek", "kind": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-abcdefg1234567", "model": "deepseek-flash"}
    data.update(over)
    return create_provider(data, db)


def raw_key(db, pid):
    """绕过序列化直读库中明文 key（验证不更新逻辑）。"""
    return db.get(LLMProvider, pid).api_key


# ---------------- seed ----------------
def test_seed_three_providers_idempotent(db):
    seed_all(db)
    rows = db.execute(select(LLMProvider).order_by(LLMProvider.id)).scalars().all()
    assert len(rows) == 3
    labels = {r.label for r in rows}
    assert labels == {"DeepSeek", "百炼 Qwen", TOKEN_PLAN_LABEL}
    kinds = {r.label: r.kind for r in rows}
    assert kinds["DeepSeek"] == "deepseek"
    assert kinds["百炼 Qwen"] == "dashscope"
    assert kinds[TOKEN_PLAN_LABEL] == "dashscope"  # 复用 dashscope quirks，不新建 kind
    for r in rows:  # 预置一律禁用、无 key、非默认（用户必须自行配置）
        assert r.enabled is False
        assert r.api_key == ""
        assert r.is_default is False
    seed_all(db)  # 幂等：已有记录不再插入（含第三条按 label 查存否）
    assert len(db.execute(select(LLMProvider)).scalars().all()) == 3


def test_seed_token_plan_fields_verbatim(db):
    """第三条预置记录字段逐字断言 + sort_order 排在现有两条之后。"""
    seed_all(db)
    row = db.execute(select(LLMProvider).where(
        LLMProvider.label == TOKEN_PLAN_LABEL)).scalar_one()
    assert row.label == TOKEN_PLAN_LLM_PROVIDER_SEED["label"] == TOKEN_PLAN_LABEL
    assert row.kind == "dashscope"
    assert row.base_url == "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    assert row.model == "qwen3.8-flash"
    assert row.api_key == ""
    assert row.enabled is False
    assert row.is_default is False
    others = db.execute(select(LLMProvider).where(
        LLMProvider.label != TOKEN_PLAN_LABEL)).scalars().all()
    assert others  # 现有两条
    assert all(row.sort_order > o.sort_order for o in others)


def test_seed_token_plan_backfills_legacy_db(db):
    """老库（只预置过旧两条）下次启动自动补种第三条，且重复跑幂等不产生重复。"""
    seed_default_llm_providers(db)
    assert len(db.execute(select(LLMProvider)).scalars().all()) == 2
    seed_token_plan_llm_provider(db)
    seed_token_plan_llm_provider(db)  # 幂等重跑
    rows = db.execute(select(LLMProvider)).scalars().all()
    assert len(rows) == 3
    assert {r.label for r in rows} == {"DeepSeek", "百炼 Qwen", TOKEN_PLAN_LABEL}


# ---------------- CRUD ----------------
def test_create_list_update_delete(db):
    created = make_provider(db)
    assert created["label"] == "DeepSeek"
    assert created["kind"] == "deepseek"
    assert created["is_default"] is False
    assert created["enabled"] is True

    items = list_providers(db)
    assert len(items) == 1
    assert items[0]["id"] == created["id"]

    updated = update_provider(created["id"], {"model": "deepseek-chat", "sort_order": 3}, db)
    assert updated["model"] == "deepseek-chat"
    assert updated["sort_order"] == 3
    assert updated["label"] == "DeepSeek"  # 未提供字段保持

    assert delete_provider(created["id"], db, audit_reason="normalization") == {"ok": True}
    assert list_providers(db) == []


def test_get_missing_returns_404(db):
    with pytest.raises(HTTPException) as ei:
        update_provider(999, {"label": "x"}, db)
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        delete_provider(999, db, audit_reason="normalization")
    assert ei.value.status_code == 404


# ---------------- 校验 ----------------
def test_invalid_kind_returns_400(db):
    with pytest.raises(HTTPException) as ei:
        make_provider(db, kind="claude")
    assert ei.value.status_code == 400
    created = make_provider(db)
    with pytest.raises(HTTPException) as ei:
        update_provider(created["id"], {"kind": "nope"}, db)
    assert ei.value.status_code == 400


def test_missing_required_fields_400(db):
    for field in ("label", "base_url", "model"):
        payload = {"kind": "deepseek", "label": "L", "base_url": "u", "model": "m"}
        payload[field] = ""
        with pytest.raises(HTTPException) as ei:
            create_provider(payload, db)
        assert ei.value.status_code == 400


def test_duplicate_label_400(db):
    make_provider(db)
    with pytest.raises(HTTPException) as ei:
        make_provider(db, kind="dashscope", base_url="https://x", model="m")
    assert ei.value.status_code == 400


# ---------------- 脱敏契约 ----------------
def test_api_key_masked_never_plaintext(db):
    created = make_provider(db, api_key="sk-abcdefg1234567")  # 长度>7
    assert created["api_key_masked"] == "sk-***4567"
    assert created["api_key_masked"].startswith("sk-")
    assert "api_key" not in created  # 明文键绝不出现
    assert "sk-abcdefg1234567" not in str(created)
    for item in list_providers(db):
        assert "api_key" not in item
        assert item["api_key_masked"] == "sk-***4567"

    short = make_provider(db, label="短", api_key="abc123")  # 长度<=7
    assert short["api_key_masked"] == "***"
    empty = make_provider(db, label="无Key", api_key="")
    assert empty["api_key_masked"] == "***"


def test_update_blank_api_key_keeps_old(db):
    created = make_provider(db, api_key="sk-supersecret99")
    assert raw_key(db, created["id"]) == "sk-supersecret99"
    # 缺省不动
    update_provider(created["id"], {"label": "改名"}, db)
    assert raw_key(db, created["id"]) == "sk-supersecret99"
    # 空串不动（前端只拿得到脱敏值，回传空即保持）
    update_provider(created["id"], {"api_key": ""}, db)
    assert raw_key(db, created["id"]) == "sk-supersecret99"
    # 显式新值才更新
    updated = update_provider(created["id"], {"api_key": "sk-brandnewkey00"}, db)
    assert raw_key(db, created["id"]) == "sk-brandnewkey00"
    assert updated["api_key_masked"] == "sk-***ey00"


# ---------------- is_default 唯一 ----------------
def test_is_default_unique_on_create(db):
    a = make_provider(db, is_default=True)
    assert a["is_default"] is True
    b = make_provider(db, label="百炼", kind="dashscope", is_default=True)
    assert b["is_default"] is True
    flags = {p["id"]: p["is_default"] for p in list_providers(db)}
    assert flags[a["id"]] is False and flags[b["id"]] is True
    assert sum(flags.values()) == 1


def test_is_default_unique_on_update(db):
    a = make_provider(db, is_default=True)
    b = make_provider(db, label="其它")
    update_provider(b["id"], {"is_default": True}, db)
    flags = {p["id"]: p["is_default"] for p in list_providers(db)}
    assert flags[a["id"]] is False and flags[b["id"]] is True


# ---------------- 删除默认供应商 → 自动补位 ----------------
def test_delete_default_reassigns_smallest_sort_order(db):
    a = make_provider(db, is_default=True, sort_order=9)
    make_provider(db, label="高序", sort_order=1)          # sort_order 最小
    b = make_provider(db, label="中序", sort_order=5)
    delete_provider(a["id"], db, audit_reason="normalization")
    defaults = [p for p in list_providers(db) if p["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["label"] == "高序"
    assert defaults[0]["sort_order"] == 1
    assert b["label"] == "中序"


def test_delete_default_no_enabled_left_no_default(db):
    a = make_provider(db, is_default=True)
    make_provider(db, label="停用者", enabled=False)
    delete_provider(a["id"], db, audit_reason="normalization")
    assert not any(p["is_default"] for p in list_providers(db))


def test_delete_non_default_keeps_default(db):
    a = make_provider(db, is_default=True)
    b = make_provider(db, label="陪跑")
    delete_provider(b["id"], db, audit_reason="normalization")
    assert list_providers(db)[0]["is_default"] is True


# =====================================================================
# 连通性测试：test_provider_connection（httpx 打桩）+ test_provider 逻辑
# =====================================================================
class FakeResp:
    def __init__(self, status_code=200, payload=None, text="", reason=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason_phrase = reason

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def pong(content):
    return {"choices": [{"message": {"content": content}}]}


def stub_client(monkeypatch, resp=None, exc=None):
    """打桩 lv.httpx.Client（照 test_llm_verification 模式），返回调用记录。"""
    calls = []

    class _Client:
        def __init__(self, **kw):
            calls.append({"init": kw})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls[-1].update({"url": url, "headers": headers, "body": json})
            if exc:
                raise exc
            return resp

    monkeypatch.setattr(lv.httpx, "Client", _Client)
    return calls


PROV = {"base_url": "https://api.deepseek.com/", "api_key": "sk-x", "model": "m"}


def test_connection_ok_body_headers_and_truncation(monkeypatch):
    long_content = "pong " + "字" * 60
    calls = stub_client(monkeypatch, FakeResp(200, pong(long_content)))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is True
    assert out["message"] == (long_content[:50])
    assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0
    # 尾斜杠 base_url → 规范端点；10s 短超时
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["init"]["timeout"] == 10.0
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-x"
    body = calls[0]["body"]
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["max_tokens"] == 5
    assert "response_format" not in body  # 极简文本对话，不带 json 模式


def test_connection_2xx_bad_json_still_ok(monkeypatch):
    stub_client(monkeypatch, FakeResp(200, payload=None))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is True and out["message"] == "连接成功"


def test_connection_non_2xx_message_field(monkeypatch):
    stub_client(monkeypatch, FakeResp(401, {"error": {"message": "Invalid API key"}}))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is False
    assert out["message"].startswith("HTTP 401:")
    assert "Invalid API key" in out["message"]


def test_connection_non_2xx_falls_back_to_text_and_reason(monkeypatch):
    stub_client(monkeypatch, FakeResp(404, payload=None, text="Not Found"))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is False and out["message"] == "HTTP 404: Not Found"
    stub_client(monkeypatch, FakeResp(500, payload=None, text="", reason="Internal Server Error"))
    out = lv.test_provider_connection(PROV)
    assert out["message"] == "HTTP 500: Internal Server Error"
    assert out["elapsed_ms"] >= 0


def test_connection_exception_degrades(monkeypatch):
    stub_client(monkeypatch, exc=httpx.TimeoutException("read timeout"))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is False and "read timeout" in out["message"]
    assert isinstance(out["elapsed_ms"], int)
    stub_client(monkeypatch, exc=httpx.RequestError("dns fail"))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is False and "dns fail" in out["message"]


def test_connection_message_capped_200(monkeypatch):
    stub_client(monkeypatch, FakeResp(400, {"error": {"message": "E" * 300}}))
    out = lv.test_provider_connection(PROV)
    assert out["ok"] is False
    assert len(out["message"]) <= len("HTTP 400: ") + 200


# ---------------- 路由逻辑函数 test_provider ----------------
def test_route_test_provider_404(db):
    with pytest.raises(HTTPException) as ei:
        provider_connection_check(999, db)
    assert ei.value.status_code == 404


def test_route_test_provider_blank_key_400(db):
    created = make_provider(db, api_key="")
    with pytest.raises(HTTPException) as ei:
        provider_connection_check(created["id"], db)
    assert ei.value.status_code == 400


def test_route_test_provider_happy_and_ignores_enabled(db, monkeypatch):
    stub_client(monkeypatch, FakeResp(200, pong("pong")))
    created = make_provider(db)  # enabled=True
    out = provider_connection_check(created["id"], db)
    assert set(out) == {"ok", "message", "elapsed_ms"}
    assert out["ok"] is True and out["message"] == "pong"
    # disabled 行也可测连通（不拦截）
    update_provider(created["id"], {"enabled": False}, db)
    assert provider_connection_check(created["id"], db)["ok"] is True
