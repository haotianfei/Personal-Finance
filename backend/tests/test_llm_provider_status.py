"""LLM 供应商：测试结果留存与序列化（spec llm-provider-status D1 / D2 / D6）。

规格：`.scratch/llm-provider-status/spec.md` §2 D1/D2/D6、§3 第 2~6 条（2026-11-06 冻结）。

三件事分层锁：

1. **序列化增量**（D6）：`GET /api/llm-providers` 每项＝改造前 9 键 + 新 4 键，
   `api_key_masked` 仍脱敏，其余键逐字段与原响应相等（用 dict 全等而不是「存在性」断言）；
2. **落库**（D1 / D6）：`POST /{id}/test` 对外仍恰为 `{ok, message, elapsed_ms}` 三键，
   同时四列被写；成功与失败两条路径都要落；从未测试的行四列是 `None`（不是 `False`/`0`）；
3. **配置变更作废旧结果**（D2）：`base_url` / `model` / `api_key` 值真的变了才清，
   `label` / `sort_order` / `enabled` / `is_default` 变更不清，`api_key: ""` 不算变更。

网络层用 monkeypatch 打桩 `llv.httpx.Client`（照 `test_llm_providers.py` 模式），
不跑真实请求。DB＝内存 SQLite（`db` fixture），数据全部合成。
"""
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.llm_verification_service as llv
from database import get_db
from errors import ApiError
from models import LLMProvider
from routers import llm_providers as llm_router
from routers.llm_providers import create_provider, update_provider
from routers.screenshots import resolve_verify_provider
from services.advisor.service import resolve_provider

# 改造前 _serialize_provider 的 9 个键（顺序即既有契约）
BASE_KEYS = ("id", "label", "kind", "base_url", "model", "api_key_masked",
             "is_default", "enabled", "sort_order")
# 本票新增的 4 键
STATUS_KEYS = ("last_tested_at", "last_test_ok", "last_test_message",
               "last_test_elapsed_ms")

PROVIDER = {
    "label": "示例供应商",
    "kind": "openai_compatible",
    "base_url": "https://llm.example.invalid/v1",
    "api_key": "sk-demo-1234567",
    "model": "demo-model",
}


# ---------------- 夹具与助手 ----------------
@pytest.fixture()
def client(db):
    """只挂 llm_providers 路由的 TestClient（db 用内存库覆盖）。"""
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(llm_router.router, prefix="/api/llm-providers")
    return TestClient(app)


def make(db, **over):
    data = dict(PROVIDER)
    data.update(over)
    return create_provider(data, db)


class _Resp:
    """最小 httpx.Response 替身（只用到 status_code / text / json / reason_phrase）。"""

    def __init__(self, status_code=200, payload=None, text="", reason=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.reason_phrase = reason

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def stub(monkeypatch, *, resp=None, exc=None, delay=0.0):
    """打桩 llv.httpx.Client：返回固定响应或抛指定异常（可加延时保证耗时为正）。"""
    import time

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            if delay:
                time.sleep(delay)
            if exc:
                raise exc
            return resp

    monkeypatch.setattr(llv.httpx, "Client", _Client)


def pong(content="pong"):
    return {"choices": [{"message": {"content": content}}]}


def stamp(db, pid, *, ok=True, message="连接成功", ms=12,
          at=datetime(2026, 11, 6, 10, 30, 0)):
    """直接给行盖上「已测试」痕迹（D2 用例只需一个非空基线）。"""
    row = db.get(LLMProvider, pid)
    row.last_tested_at = at
    row.last_test_ok = ok
    row.last_test_message = message
    row.last_test_elapsed_ms = ms
    db.commit()


def raw_status(db, pid):
    """绕过序列化直读库中四列（证明落的是真值，不是序列化层的假象）。"""
    row = db.get(LLMProvider, pid)
    db.refresh(row)
    return (row.last_tested_at, row.last_test_ok, row.last_test_message,
            row.last_test_elapsed_ms)


# =====================================================================
# 一、序列化增量（D6）——只多 4 键，其余 9 键逐字段不变
# =====================================================================
def test_list_adds_exactly_four_keys_and_keeps_others(client, db):
    make(db)
    items = client.get("/api/llm-providers").json()
    assert len(items) == 1
    item = items[0]

    # ① 键集合恰为「9 旧 + 4 新」，不多不少
    assert list(item) == [*BASE_KEYS, *STATUS_KEYS]
    # ② 其余 9 键逐字段等于改造前的契约（硬编码期望，不拿被测代码自证）
    assert {k: item[k] for k in BASE_KEYS} == {
        "id": 1,
        "label": "示例供应商",
        "kind": "openai_compatible",
        "base_url": "https://llm.example.invalid/v1",
        "model": "demo-model",
        "api_key_masked": "sk-***4567",
        "is_default": False,
        "enabled": True,
        "sort_order": 0,
    }
    # ③ 新 4 键存在且为 None（从未测试 → NULL，不是 false / 0）
    for key in STATUS_KEYS:
        assert item[key] is None, key


def test_list_keeps_api_key_masked(client, db):
    make(db, api_key="sk-demo-1234567")
    resp = client.get("/api/llm-providers")
    item = resp.json()[0]
    assert item["api_key_masked"] == "sk-***4567"
    assert "api_key" not in item
    assert "sk-demo-1234567" not in resp.text  # 明文永不出口


def test_never_tested_row_is_null_not_false(client, db):
    """spec §3 第 4 条：「从未测试」必须是 NULL，与「测试失败」（false）可分。"""
    pid = make(db)["id"]
    row = db.get(LLMProvider, pid)
    assert (row.last_tested_at, row.last_test_ok,
            row.last_test_message, row.last_test_elapsed_ms) == (None, None, None, None)
    item = client.get("/api/llm-providers").json()[0]
    assert item["last_test_ok"] is None       # 不是 False
    assert item["last_test_elapsed_ms"] is None  # 不是 0


# =====================================================================
# 二、测试端点：对外三键不变，同时对内落库（D1 / D6）
# =====================================================================
def test_test_endpoint_success_persists_status(client, db, monkeypatch):
    pid = make(db)["id"]
    stub(monkeypatch, resp=_Resp(200, pong("pong")), delay=0.003)

    resp = client.post(f"/api/llm-providers/{pid}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["ok", "message", "elapsed_ms"]   # 对外形状不扩不缩
    assert body["ok"] is True and body["message"] == "pong"

    item = client.get("/api/llm-providers").json()[0]
    assert item["last_tested_at"] is not None
    assert datetime.fromisoformat(item["last_tested_at"])  # 可解析的时间串
    assert item["last_test_ok"] is True
    assert item["last_test_message"] == "pong"
    assert isinstance(item["last_test_elapsed_ms"], int)
    assert item["last_test_elapsed_ms"] > 0               # 正整数
    assert item["last_test_elapsed_ms"] == body["elapsed_ms"]

    # 库中真值同形（不依赖序列化层）
    at, ok, msg, ms = raw_status(db, pid)
    assert at is not None and ok is True and msg == "pong" and ms > 0


def test_test_endpoint_failure_persists_status(client, db, monkeypatch):
    """不可达 → 200 且 ok=false；库中 last_test_ok=false、message 非空。"""
    pid = make(db)["id"]
    stub(monkeypatch, exc=httpx.ConnectError("connection refused"))

    resp = client.post(f"/api/llm-providers/{pid}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["ok", "message", "elapsed_ms"]
    assert body["ok"] is False and body["message"]

    item = client.get("/api/llm-providers").json()[0]
    assert item["last_test_ok"] is False
    assert item["last_test_message"]
    assert item["last_test_message"] == body["message"]
    assert item["last_tested_at"] is not None

    _at, ok, msg, _ms = raw_status(db, pid)
    assert ok is False and msg


def test_test_endpoint_message_truncated_to_column_width(client, db, monkeypatch):
    """超长错误摘要不撑爆 String(300)：落库前截断，库内值仍非空。"""
    pid = make(db)["id"]
    stub(monkeypatch, exc=httpx.ConnectError("x" * 500))

    assert client.post(f"/api/llm-providers/{pid}/test").json()["ok"] is False

    _at, ok, msg, _ms = raw_status(db, pid)
    assert ok is False and 0 < len(msg) <= 300


def test_test_endpoint_blank_key_still_400_and_no_status(client, db):
    """既有的 400（未填 key）不得写库：没测就还是「从未测试」。"""
    pid = make(db, api_key="")["id"]
    assert client.post(f"/api/llm-providers/{pid}/test").status_code == 400
    assert raw_status(db, pid) == (None, None, None, None)


# =====================================================================
# 三、D2：配置真的变了才清空（真值表逐格）
# =====================================================================
def _put(client, pid, **over):
    payload = dict(PROVIDER)
    payload.update(over)
    return client.put(f"/api/llm-providers/{pid}", json=payload)


@pytest.mark.parametrize("field, new_value", [
    ("base_url", "https://llm2.example.invalid/v1"),
    ("model", "another-model"),
    ("api_key", "sk-newkey-7654321"),
])
def test_config_value_change_clears_status(client, db, field, new_value):
    """三分支：base_url / model / api_key 值真的变了 → 四列清空。"""
    pid = make(db)["id"]
    stamp(db, pid)

    resp = _put(client, pid, **{field: new_value})
    assert resp.status_code == 200
    assert all(resp.json()[key] is None for key in STATUS_KEYS)
    assert raw_status(db, pid) == (None, None, None, None)


def test_label_and_sort_order_change_keeps_status(client, db):
    """只改 label + sort_order → 四列保留（旧结论仍成立）。"""
    pid = make(db)["id"]
    stamp(db, pid)
    before = raw_status(db, pid)

    resp = _put(client, pid, label="改过的名称", sort_order=7)
    assert resp.status_code == 200
    assert resp.json()["label"] == "改过的名称" and resp.json()["sort_order"] == 7
    assert raw_status(db, pid) == before


def test_enabled_and_is_default_change_keeps_status(client, db):
    """enabled / is_default 变更一律不清（D2 只认 base_url/model/api_key）。"""
    pid = make(db)["id"]
    stamp(db, pid)
    before = raw_status(db, pid)

    assert _put(client, pid, enabled=False, is_default=True).status_code == 200
    assert raw_status(db, pid) == before

    assert _put(client, pid, is_default=False).status_code == 200
    assert raw_status(db, pid) == before


def test_blank_api_key_keeps_status_and_old_key(client, db):
    """`api_key: ""` 是既有「不改 key」语义 → 不算变更，四列保留。"""
    pid = make(db)["id"]
    stamp(db, pid)
    before = raw_status(db, pid)

    payload = dict(PROVIDER)
    payload.update(label="改个名", api_key="")
    resp = client.put(f"/api/llm-providers/{pid}", json=payload)
    assert resp.status_code == 200

    assert raw_status(db, pid) == before
    assert db.get(LLMProvider, pid).api_key == PROVIDER["api_key"]  # 旧 key 未被清


def test_resubmitting_identical_full_payload_keeps_status(client, db):
    """前端编辑表单回传全量字段：值与库中相同 → 不算变更（D2 判定细节）。"""
    pid = make(db)["id"]
    stamp(db, pid)
    before = raw_status(db, pid)

    payload = dict(PROVIDER)          # base_url / model / api_key 全量回传，值与库一致
    payload.update(api_key="")        # 前端只拿得到脱敏值，回传空即保持
    assert client.put(f"/api/llm-providers/{pid}", json=payload).status_code == 200
    assert raw_status(db, pid) == before


def test_config_change_with_only_whitespace_differs_not_counted(client, db):
    """比较按写入后的值（strip）判等：纯空白差异不算变更。"""
    pid = make(db)["id"]
    stamp(db, pid)
    before = raw_status(db, pid)

    assert _put(client, pid, base_url=PROVIDER["base_url"] + "  ").status_code == 200
    assert raw_status(db, pid) == before


# =====================================================================
# 四、零默认后果未被顺带改动（只读观察，spec §4 第 1 条）
# =====================================================================
def test_cancel_default_keeps_zero_default_degradation(client, db):
    """取消默认后：截图校验仍降级 None、advisor 仍 409 且文案不变。"""
    pid = make(db, is_default=True)["id"]
    assert resolve_verify_provider(db) is not None

    update_provider(pid, {"is_default": False}, db)
    flags = {p["id"]: p["is_default"] for p in client.get("/api/llm-providers").json()}
    assert flags[pid] is False and sum(flags.values()) == 0

    assert resolve_verify_provider(db) is None          # 截图校验降级（不改）
    with pytest.raises(ApiError) as ei:
        resolve_provider(db)
    assert ei.value.status_code == 409
    assert ei.value.code == "ADVISOR_PROVIDER_UNAVAILABLE"
    assert ei.value.message == "无默认且启用的 LLM 供应商，请先在设置中配置"