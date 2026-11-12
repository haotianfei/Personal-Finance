"""解析词表管理 API + 组级规则写入校验收口（P1b）的接口级验收。

覆盖 `.scratch/parser-rules-refactor/spec.md` §3 P1 的判据 4/6/7：
词表 add/remove/reset 三态、未知键 422、缺 reason 422、remove 无用项 422、
add∩remove 冲突 422、同 payload 二次 PUT 不写审计；以及两条组级规则写路径
（`PUT /api/source-groups/{id}` 与 `/{id}/parse-rules`）的未知键 422 与
`amount_header` 的 str→list 归一。

沙箱：conftest 的 in-memory `db` fixture（`create_all`，不跑迁移）+ 最小 FastAPI
装配（parser + source_groups 两条路由，`get_db` 覆盖到同一会话），装配方式照抄
`tests/test_allocations_dual.py`。数据全部合成：来源组 `demo-group` /「示例来源组」，
词表项一律「示例*」，不含真实机构名、真实金额或本机路径。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database import get_db
from models import AuditLog, ParserVocabulary, SourceGroup
from services.idempotency_service import IdempotencyCaptureMiddleware

VOCAB_KEYS = ("amount_headers", "ignore_words", "total_labels",
              "name_exclude", "profit_zone", "row_close_tokens", "noise_patterns")


@pytest.fixture()
def client(db):
    """最小应用：只挂本车道涉及的两条路由，异常处理器用 main 的同一套。"""
    from main import register_exception_handlers
    from routers import parser as parser_router
    from routers import source_groups as sg_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(parser_router.router, prefix="/api/parser")
    app.include_router(sg_router.router, prefix="/api/source-groups")
    return TestClient(app)


# ---------------------------------------------------------------- 助手
def audit_rows(db, action=None):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars())


def audit_count(db, action=None):
    q = select(func.count()).select_from(AuditLog)
    if action:
        q = q.where(AuditLog.action == action)
    return db.execute(q).scalar() or 0


def make_group(db, slug="demo-group", label="示例来源组", rules=None):
    from routers.source_groups import create_source_group

    return create_source_group(
        {"slug": slug, "label": label, "asset_prefix": "示例-", "rules": rules}, db)


def legacy_group(db, rules_text, slug="demo-legacy"):
    """直接写 ORM 造「0031 之前」的存量行（含已搬表的教学键）。

    创建口现在会拒三键，所以要模拟迁移前的脏行只能绕过写入口。
    """
    sg = SourceGroup(slug=slug, label="示例来源组", asset_prefix="示例-", rules=rules_text)
    db.add(sg)
    db.commit()
    db.refresh(sg)
    return {"id": sg.id, "slug": sg.slug, "rules": sg.rules}


def vocab_of(client, key):
    items = client.get("/api/parser/vocabularies").json()
    return next(item for item in items if item["key"] == key)


def error_code(resp):
    assert resp.status_code >= 400, resp.text
    return resp.json()["error"]["code"]


# ---------------------------------------------------------------- GET 全量视图
def test_list_vocabularies_returns_seven_keys_with_effective_union_minus_remove(client):
    resp = client.get("/api/parser/vocabularies")
    assert resp.status_code == 200, resp.text
    items = resp.json()

    assert [item["key"] for item in items] == list(VOCAB_KEYS)   # 顺序即前端展示序
    for item in items:
        assert item["add"] == [] and item["remove"] == []
        # 无增量覆盖时 effective 逐项等于出厂值（顺序也相等）
        assert item["effective"] == item["builtin"]
        assert item["builtin"], item["key"]          # 出厂词表非空
        assert item["changed"] is False
        assert item["affected_groups"] == 0          # 还没有来源组


def test_list_effective_equals_builtin_union_add_minus_remove(client, db):
    added = "示例新增列"
    removed = vocab_of(client, "amount_headers")["builtin"][0]
    resp = client.put("/api/parser/vocabularies/amount_headers",
                      json={"add": [added], "remove": [removed], "reason": "示例：换列头"})
    assert resp.status_code == 200, resp.text

    view = vocab_of(client, "amount_headers")
    builtin = view["builtin"]
    assert view["add"] == [added]
    assert view["remove"] == [removed]
    expected = [w for w in builtin if w != removed] + [added]
    assert view["effective"] == expected             # (builtin ∪ add) − remove


def test_affected_groups_counts_enabled_groups(client, db):
    make_group(db)                                   # enabled=True（默认）
    sg = make_group(db, slug="demo-group-2", label="示例来源组二")
    sg["enabled"] = False
    db.expire_all()
    db.get(SourceGroup, sg["id"]).enabled = False
    db.commit()

    assert vocab_of(client, "ignore_words")["affected_groups"] == 1


# ---------------------------------------------------------------- PUT / reset
def test_put_add_extends_effective_then_reset_restores_builtin(client, db):
    builtin = vocab_of(client, "ignore_words")["builtin"]

    resp = client.put("/api/parser/vocabularies/ignore_words",
                      json={"add": ["示例按钮词"], "remove": [],
                            "reason": "示例：本机 App 多一个按钮"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["effective"] == builtin + ["示例按钮词"]
    assert db.get(ParserVocabulary, "ignore_words") is not None

    # 落库（表里存的是增量 JSON 数组，不是整表）
    row = db.get(ParserVocabulary, "ignore_words")
    db.refresh(row)
    assert json.loads(row.add_items) == ["示例按钮词"]
    assert json.loads(row.remove_items) == []

    reset = client.post("/api/parser/vocabularies/ignore_words/reset",
                        json={"reason": "示例：撤销新增"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["changed"] is True
    assert reset.json()["effective"] == builtin          # 完全回退出厂值
    db.expire_all()
    assert db.get(ParserVocabulary, "ignore_words") is None

    assert audit_count(db, "parser_vocabulary_update") == 1
    assert audit_count(db, "parser_vocabulary_reset") == 1


def test_put_remove_builtin_word_takes_effect_and_reason_lands_in_audit(client, db):
    builtin = vocab_of(client, "total_labels")["builtin"]
    word = builtin[0]
    resp = client.put("/api/parser/vocabularies/total_labels",
                      json={"add": [], "remove": [word], "reason": "示例：该词在本机不适用"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective"] == builtin[1:]

    rows = audit_rows(db, "parser_vocabulary_update")
    assert len(rows) == 1
    before = json.loads(rows[0].before_json) if rows[0].before_json else None
    after = json.loads(rows[0].after_json)
    assert before is None                            # 首次落库：before NULL＝创建
    assert after == {"add": [], "remove": [word], "reason": "示例：该词在本机不适用"}


def test_put_remove_word_absent_from_builtin_and_add_is_422(client, db):
    resp = client.put("/api/parser/vocabularies/ignore_words",
                      json={"add": [], "remove": ["示例不存在的词"], "reason": "示例：无用项"})
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    assert resp.status_code == 422
    db.expire_all()
    assert db.get(ParserVocabulary, "ignore_words") is None      # 零写入


def test_put_add_and_remove_same_word_is_422(client, db):
    word = vocab_of(client, "ignore_words")["builtin"][0]
    resp = client.put("/api/parser/vocabularies/ignore_words",
                      json={"add": [word], "remove": [word], "reason": "示例：自相矛盾"})
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    db.expire_all()
    assert db.get(ParserVocabulary, "ignore_words") is None


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_put_without_reason_is_422(client, db, reason):
    payload = {"add": ["示例词"], "remove": []}
    if reason is not None:
        payload["reason"] = reason
    resp = client.put("/api/parser/vocabularies/ignore_words", json=payload)
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    db.expire_all()
    assert db.get(ParserVocabulary, "ignore_words") is None
    assert audit_count(db) == 0


def test_reset_without_reason_is_422(client, db):
    client.put("/api/parser/vocabularies/ignore_words",
               json={"add": ["示例词"], "remove": [], "reason": "示例：先加"})
    before = audit_count(db)
    resp = client.post("/api/parser/vocabularies/ignore_words/reset", json={})
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    db.expire_all()
    assert db.get(ParserVocabulary, "ignore_words") is not None  # 未删
    assert audit_count(db) == before


def test_same_payload_twice_is_changed_false_and_writes_no_extra_audit(client, db):
    payload = {"add": ["示例词"], "remove": [], "reason": "示例：保存两次"}
    first = client.put("/api/parser/vocabularies/ignore_words", json=payload)
    assert first.status_code == 200
    assert first.json()["changed"] is True
    after_first = audit_count(db, "parser_vocabulary_update")
    assert after_first == 1

    second = client.put("/api/parser/vocabularies/ignore_words", json=payload)
    assert second.status_code == 200
    assert second.json()["changed"] is False
    assert second.json()["effective"] == first.json()["effective"]
    assert audit_count(db, "parser_vocabulary_update") == after_first   # 审计行数不增


def test_reset_without_existing_row_is_changed_false_and_no_audit(client, db):
    resp = client.post("/api/parser/vocabularies/noise_patterns/reset",
                       json={"reason": "示例：本来就未改过"})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    assert audit_count(db, "parser_vocabulary_reset") == 0


def test_unknown_vocabulary_key_is_404(client):
    put = client.put("/api/parser/vocabularies/no_such_key",
                     json={"add": [], "remove": [], "reason": "示例"})
    assert put.status_code == 404
    assert error_code(put) == "NOT_FOUND"
    assert "no_such_key" in put.json()["error"]["message"]

    reset = client.post("/api/parser/vocabularies/no_such_key/reset",
                        json={"reason": "示例"})
    assert reset.status_code == 404
    assert error_code(reset) == "NOT_FOUND"


# ---------------------------------------------------------------- 组级 rules 校验
def test_put_source_group_unknown_key_is_422_and_rules_untouched(client, db):
    original = json.dumps({"layout": "vertical"}, ensure_ascii=False)
    g = make_group(db, rules=original)

    resp = client.put(f"/api/source-groups/{g['id']}",
                      json={"rules": {"nmae_col": 1}})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "SOURCE_GROUP_RULES_INVALID"
    assert "nmae_col" in body["message"]             # 未知键名（字段名）可回显
    assert body["details"]["unknown_keys"] == ["nmae_col"]
    assert "layout" in body["details"]["allowed_keys"]

    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules == original      # 库里未被修改


@pytest.mark.parametrize("dead_or_learned", [
    "rename", "ignore", "exclude",                     # 0031 已搬表的三个教学键
    "name_col", "total_header", "amount_suffix", "anchor_summary",  # 0031 清掉的死键
])
def test_put_source_group_rejects_migrated_and_dead_keys(client, db, dead_or_learned):
    g = make_group(db)
    resp = client.put(f"/api/source-groups/{g['id']}",
                      json={"rules": {dead_or_learned: []}})
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules is None


def test_put_source_group_amount_header_str_is_normalized_to_list(client, db):
    g = make_group(db)
    resp = client.put(f"/api/source-groups/{g['id']}",
                      json={"rules": {"layout": "vertical", "amount_header": "金额"}})
    assert resp.status_code == 200, resp.text
    stored = json.loads(resp.json()["rules"])
    assert stored == {"layout": "vertical", "amount_header": ["金额"]}

    db.expire_all()
    assert json.loads(db.get(SourceGroup, g["id"]).rules) == stored


def test_put_source_group_amount_header_list_is_cleaned_and_deduped(client, db):
    g = make_group(db)
    resp = client.put(f"/api/source-groups/{g['id']}",
                      json={"rules": {"amount_header": [" 金额 ", "金额", "", "持有金额"]}})
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["rules"]) == {"amount_header": ["金额", "持有金额"]}


def test_put_source_group_bad_type_is_422(client, db):
    g = make_group(db)
    for rules in ({"layout": 1}, {"amount_header": {"a": 1}},
                  {"amount_col": -1}, {"amount_col": True}, {"ignore_words": "示例"}):
        resp = client.put(f"/api/source-groups/{g['id']}", json={"rules": rules})
        assert resp.status_code == 422, rules
        assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules is None


def test_put_source_group_empty_rules_clears_column(client, db):
    g = make_group(db, rules=json.dumps({"layout": "card"}))
    resp = client.put(f"/api/source-groups/{g['id']}", json={"rules": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rules"] is None              # 空配置归一成 NULL（同 0031 口径）
    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules is None


def test_put_source_group_accepts_json_string_form(client, db):
    """既有契约里 rules 是 JSON 字符串（create/restore/脚本都这么传），照样校验+归一。"""
    g = make_group(db)
    resp = client.put(f"/api/source-groups/{g['id']}",
                      json={"rules": "{\"amount_header\": \"金额\"}"})
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["rules"]) == {"amount_header": ["金额"]}


# ---------------------------------------------------------------- parse-rules
def test_parse_rules_unknown_key_is_422_and_rules_untouched(client, db):
    original = json.dumps({"layout": "vertical"}, ensure_ascii=False)
    g = make_group(db, rules=original)

    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules",
                      json={"nmae_col": 1})
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"

    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules == original      # 库内容未变


def test_parse_rules_legacy_three_keys_are_rejected(client, db):
    """迁移后三键属独立表（校正规则面板），本端点不再接受。"""
    g = make_group(db)
    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules",
                      json={"rename": [{"match": "示例旧名", "replace": "示例新名"}]})
    assert resp.status_code == 422
    assert error_code(resp) == "SOURCE_GROUP_RULES_INVALID"
    assert "rename" in resp.json()["error"]["message"]


def test_parse_rules_partial_merge_and_amount_header_str_normalized(client, db):
    g = make_group(db, rules=json.dumps({"layout": "vertical", "amount_col": 0}))
    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules",
                      json={"amount_header": "持有金额"})
    assert resp.status_code == 200, resp.text
    stored = json.loads(resp.json()["rules"])
    assert stored == {"layout": "vertical", "amount_col": 0,
                      "amount_header": ["持有金额"]}     # 未发送的键原样保留


def test_parse_rules_null_and_empty_values_drop_keys(client, db):
    g = make_group(db, rules=json.dumps(
        {"layout": "card", "amount_col": 2, "amount_header": ["示例列"],
         "ignore_words": ["示例词"]}))
    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules",
                      json={"layout": None, "amount_col": None,
                            "amount_header": [], "ignore_words": ["  "]})
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["rules"]) == {}


def test_parse_rules_missing_group_is_404(client):
    resp = client.put("/api/source-groups/999999/parse-rules", json={"layout": "card"})
    assert resp.status_code == 404


def test_parse_rules_empty_body_on_legacy_row_is_pure_readback(client, db):
    """空 body ＝ 纯读回：存量脏键不该把「只读」也拦成 422（写入才 fail-fast）。"""
    legacy = json.dumps({"ignore": [{"match": "示例旧键"}]}, ensure_ascii=False)
    g = legacy_group(db, legacy)
    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rules"] == legacy


def test_parse_rules_stored_unknown_key_blocks_real_write(client, db):
    """合并结果整体校验：存量里的迁移前键在**真正写入**时立刻暴露（D4 fail-fast）。"""
    legacy = json.dumps({"ignore": [{"match": "示例旧键"}]}, ensure_ascii=False)
    g = legacy_group(db, legacy)

    resp = client.put(f"/api/source-groups/{g['id']}/parse-rules",
                      json={"layout": "card"})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "SOURCE_GROUP_RULES_INVALID"
    assert "ignore" in body["message"]
    db.expire_all()
    assert db.get(SourceGroup, g["id"]).rules == legacy      # 失败不落库


# ---------------------------------------------------------------- 路由注册
def test_parser_routes_registered_in_main():
    """三个端点必须真的挂在 main 的 app 上（前缀 /api/parser），不只是本测试的装配。"""
    from main import app

    paths = {(r.path, tuple(sorted(getattr(r, "methods", set()) - {"HEAD"})))
             for r in app.routes}
    assert ("/api/parser/vocabularies", ("GET",)) in paths
    assert ("/api/parser/vocabularies/{key}", ("PUT",)) in paths
    assert ("/api/parser/vocabularies/{key}/reset", ("POST",)) in paths