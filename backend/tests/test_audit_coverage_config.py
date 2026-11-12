"""留痕覆盖率：②口径桶·配置类（ADR-0027 D1/D5/D6）。

本文件只锁「配置类写端点到底留没留痕、留的形状对不对」，不重测业务校验：

1. `/api/alerts/rules` POST/PUT/DELETE ＋ `/api/alerts/import/execute`
   → `alert/create|update|delete|execute`。update 只记**变更字段**，delete 落 before 全集；
   导入头行 `after={'count', 'source'}`。
2. `/api/source-groups` POST/PUT/DELETE、`/guide-image`、`/parse-rules`
   → `source_group/create|update|delete`。**引导图只落 `{'has_image', 'image_sha256'}`**
   （二进制/ base64 绝不进审计表：断言整段 JSON < 500 字符）。
3. `correction-rules` POST/PATCH/DELETE ＋ `/swap`
   → `source_group/create|update|delete`。swap 的 after **只有** `{'order': [...]}`。
4. `/api/llm-providers` POST/PUT/DELETE → `llm_provider/create|update|delete`。
   **硬断言**：审计文本里出现 `<redacted>`、`_fp`、`_len`，且**不含明文 key**——
   密钥脱敏是 audit_service 的单点职责（D6），调用方不得手工拼脱敏串。
5. 422/400/404 被拒的请求 → 审计行数一条不涨（没有写入事实就没有行）。
6. 这些写入之后 `audit_chain.verify()` 仍 ok（链能续接）。

沙箱：`conftest.db`（内存 SQLite）＋ guide-image 目录注入 tmp_path；不触真实 data/。
"""
import asyncio
import hashlib
import io
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select

from errors import ApiError
from models import AuditLog
from routers.alerts import execute_import_alert_rules
from routers.llm_providers import create_provider, delete_provider, update_provider
from routers.screenshots import (
    delete_correction_rule,
    patch_correction_rule,
    save_correction_rule,
    swap_correction_rules,
)
from routers import source_groups as sg_mod
from routers.source_groups import (
    create_source_group,
    delete_source_group,
    save_guide_image,
    update_parse_rules,
    update_source_group,
)
from schemas import AlertRuleCreate, AlertRuleUpdate
from services import alert_service, audit_chain


# ---------------------------------------------------------------- 助手

def _rows(db, entity=None, action=None):
    q = select(AuditLog).order_by(AuditLog.id)
    if entity is not None:
        q = q.where(AuditLog.entity == entity)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q).scalars().all())


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _last(db, entity, action):
    rows = _rows(db, entity, action)
    assert rows, f"缺 {entity}/{action} 审计行"
    return rows[-1]


def _side(row, side: str):
    """审计行的 before/after → dict（NULL → None）。"""
    text = getattr(row, f"{side}_json")
    return json.loads(text) if text else None


def _alert(db, **over):
    data = {"name": "示例-大额异动", "dimension": "asset_name",
            "target_id": "示例标的A", "period_type": "month",
            "compare_type": "previous", "amount_threshold": Decimal("10000.00"),
            "direction": "both"}
    data.update(over)
    return alert_service.create_alert_rule(db, AlertRuleCreate(**data))


# ---------------------------------------------------------------- 1. alerts 规则

def test_alert_rule_create_update_delete_leave_snapshots(db):
    rule = _alert(db)
    row = _last(db, "alert", "create")
    assert row.row_id == rule.id and row.before_json is None
    after = _side(row, "after")
    assert after["id"] == rule.id
    assert after["name"] == "示例-大额异动" and after["dimension"] == "asset_name"
    assert after["amount_threshold"] == "10000.00"      # Decimal 文本，不经 float
    assert after["direction"] == "both" and after["is_active"] is True

    n = _count(db)
    alert_service.update_alert_rule(
        db, rule.id, AlertRuleUpdate(amount_threshold=Decimal("20000.00")))
    assert _count(db) == n + 1                          # 一次 update 一行
    row = _last(db, "alert", "update")
    assert row.row_id == rule.id and row.action == "update"
    before, after = _side(row, "before"), _side(row, "after")
    assert after == {"amount_threshold": "20000.00"}    # **只含变更键**（D5）
    assert before == {"amount_threshold": "10000.00"}
    assert "name" not in after and "dimension" not in after

    # 值没变 → 没有写入事实，不产行
    n = _count(db)
    alert_service.update_alert_rule(
        db, rule.id, AlertRuleUpdate(amount_threshold=Decimal("20000.00")))
    assert _count(db) == n

    assert alert_service.delete_alert_rule(
        db, rule.id, audit_reason="normalization") is True
    row = _last(db, "alert", "delete")
    assert row.row_id == rule.id and row.after_json is None
    before = _side(row, "before")
    assert {"id", "name", "dimension", "amount_threshold", "direction"} <= set(before)
    assert before["id"] == rule.id


def test_alert_import_execute_writes_head_row(db):
    rules = {"rules": [{"name": "示例-导入规则A", "dimension": "asset_name",
                        "period_type": "month", "compare_type": "previous",
                        "amount_threshold": 5000},
                       {"name": "示例-导入规则B", "dimension": "liquidity",
                        "target_id": "7", "period_type": "month",
                        "compare_type": "previous", "amount_threshold": 800}]}
    file = UploadFile(filename="alert_rules_demo.json",
                      file=io.BytesIO(json.dumps(rules).encode("utf-8")))
    result = asyncio.run(execute_import_alert_rules(
        file=file, conflict_strategy="skip", db=db))
    assert result["imported"] == 2

    row = _last(db, "alert", "execute")
    assert row.row_id is None and row.before_json is None
    assert _side(row, "after") == {"count": 2, "source": "alert_rules_demo.json"}
    # 逐条明细仍是 create（D3：头行答「哪次导入」，明细答「改了哪条」）
    assert len(_rows(db, "alert", "create")) == 2


# ---------------------------------------------------------------- 2. source-groups

def test_source_group_create_update_delete_leave_snapshots(db):
    group = create_source_group(
        {"slug": "demo-app", "label": "示例应用", "asset_prefix": "示例-"}, db)
    row = _last(db, "source_group", "create")
    assert row.row_id == group["id"] and row.before_json is None
    after = _side(row, "after")
    assert after["slug"] == "demo-app" and after["label"] == "示例应用"
    assert after["account_ids"] == [] and after["guide_image"] is None

    update_source_group(group["id"], {"label": "示例应用（改名）"}, db)
    row = _last(db, "source_group", "update")
    before, after = _side(row, "before"), _side(row, "after")
    assert after == {"label": "示例应用（改名）"}       # 只含变更键
    assert before == {"label": "示例应用"}

    # parse-rules：改一个解析键 → 只记这一个键
    update_parse_rules(group["id"], {"layout": "vertical"}, db)
    row = _last(db, "source_group", "update")
    before, after = _side(row, "before"), _side(row, "after")
    assert set(after) == {"layout"} and after["layout"] == "vertical"
    assert before["layout"] is None

    assert delete_source_group(group["id"], db, reason="normalization")["ok"] is True
    row = _last(db, "source_group", "delete")
    assert row.after_json is None
    assert _side(row, "before")["slug"] == "demo-app"


def test_guide_image_upload_audits_digest_not_bytes(db, tmp_path, monkeypatch):
    monkeypatch.setattr(sg_mod, "guide_image_root", lambda: str(tmp_path))
    group = create_source_group({"slug": "guide-app", "label": "示例引导图应用"}, db)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256

    save_guide_image(group["id"],
                     UploadFile(filename="guide.png", file=io.BytesIO(png)), db)

    row = _last(db, "source_group", "update")
    assert row.row_id == group["id"]
    after = _side(row, "after")
    assert after == {"has_image": True,
                     "image_sha256": hashlib.sha256(png).hexdigest()[:16]}
    # 硬断言：整段审计载荷里没有二进制/base64，也没有把图片塞进来
    text = (row.before_json or "") + (row.after_json or "")
    assert len(text) < 500
    assert "base64" not in text and "\\x00" not in text
    assert _side(row, "before")["guide_image"] is None


# ---------------------------------------------------------------- 3. correction-rules

def test_correction_rules_create_patch_delete_and_swap(db):
    create_source_group({"slug": "swap-app", "label": "示例换序应用"}, db)
    base = _count(db)

    save_correction_rule("swap-app",
                         {"type": "rename", "match": "接C", "replace": "示例基金C"}, db)
    save_correction_rule("swap-app",
                         {"type": "rename", "match": "接D", "replace": "示例基金D"}, db)
    creates = [r for r in _rows(db, "source_group", "create")
               if (_side(r, "after") or {}).get("type")]
    assert len(creates) == 2                              # 两条新规则各一行
    first, second = (_side(creates[0], "after"), _side(creates[1], "after"))
    assert first["match"] == "接C" and first["replace"] == "示例基金C"
    assert first["id"] < second["id"]
    ids = [first["id"], second["id"]]

    # PATCH：只改 note → 只记 note
    patch_correction_rule("swap-app", {"type": "rename", "match": "接C",
                                      "replace": "示例基金C", "new_note": "示例备注"}, db)
    row = _last(db, "source_group", "update")
    assert _side(row, "after") == {"note": "示例备注"}
    assert _side(row, "before") == {"note": None}

    # swap：after 只有顺序，不复制规则体
    n = _count(db)
    swap_correction_rules("swap-app",
                          {"type": "rename", "a": {"match": "接C", "replace": "示例基金C"},
                           "b": {"match": "接D", "replace": "示例基金D"}}, db)
    assert _count(db) == n + 1
    row = _last(db, "source_group", "update")
    after = _side(row, "after")
    assert set(after) == {"order"}                        # **只有顺序**
    assert after["order"] == [ids[1], ids[0]]
    assert _side(row, "before") == {"order": ids}
    assert "replace" not in json.dumps(after)             # 没整段复制规则体

    # DELETE：before 落被删那条的关键字段
    delete_correction_rule("swap-app",
                          {"type": "rename", "match": "接C", "replace": "示例基金C"},
                          db, reason="normalization")
    row = _last(db, "source_group", "delete")
    assert row.after_json is None
    before = _side(row, "before")
    assert before["type"] == "rename" and before["match"] == "接C"
    # 2 create ＋ 1 patch ＋ 1 swap ＋ 1 delete（每组行都只记一次写入）
    assert _count(db) == base + 5


# ---------------------------------------------------------------- 4. llm-providers（密钥）

KEY_1 = "config-test-key-0001-plain"
KEY_2 = "config-test-key-0002-rotated"


def test_llm_provider_api_key_never_reaches_audit_plaintext(db):
    provider = create_provider(
        {"label": "示例供应商", "kind": "openai_compatible",
         "base_url": "https://example.invalid/v1", "api_key": KEY_1,
         "model": "demo-model"}, db)
    row = _last(db, "llm_provider", "create")
    assert row.row_id == provider["id"] and row.before_json is None
    after = _side(row, "after")
    assert after["api_key"] == "<redacted>"               # 单点脱敏，调用方未手工替换
    assert len(after["api_key_fp"]) == 8 and after["api_key_len"] == len(KEY_1)
    assert after["label"] == "示例供应商" and after["model"] == "demo-model"

    # 只改名：不动密钥 → 不出现 api_key 键，也不打 rotated_key
    update_provider(provider["id"], {"label": "示例供应商（改名）"}, db)
    row = _last(db, "llm_provider", "update")
    after = _side(row, "after")
    assert set(after) == {"label"} and "rotated_key" not in after

    # 密钥轮换：api_key 脱敏 + rotated_key 备注
    update_provider(provider["id"], {"api_key": KEY_2}, db)
    row = _last(db, "llm_provider", "update")
    before, after = _side(row, "before"), _side(row, "after")
    assert after["api_key"] == "<redacted>" and before["api_key"] == "<redacted>"
    assert after["rotated_key"] is True
    assert after["api_key_len"] == len(KEY_2)
    assert after["api_key_fp"] != before["api_key_fp"]    # 指纹变了＝真换了钥匙

    delete_provider(provider["id"], db, audit_reason="normalization")
    row = _last(db, "llm_provider", "delete")
    assert row.after_json is None
    before = _side(row, "before")
    assert before["id"] == provider["id"] and before["api_key"] == "<redacted>"
    assert before["api_key_len"] == len(KEY_2)

    # 硬断言：**所有** llm_provider 行的整段 JSON 文本都不含明文 key；
    # 带密钥的行还必须是单点脱敏的「三件套」形状（<redacted> + _fp + _len）
    key_rows = [_rows(db, "llm_provider", "create")[-1],
                _rows(db, "llm_provider", "delete")[-1]]
    key_rows.append([r for r in _rows(db, "llm_provider", "update")
                     if (_side(r, "after") or {}).get("rotated_key")][-1])
    for audit_row in _rows(db, "llm_provider"):
        text = (audit_row.before_json or "") + (audit_row.after_json or "")
        assert KEY_1 not in text and KEY_2 not in text
    for audit_row in key_rows:
        text = (audit_row.before_json or "") + (audit_row.after_json or "")
        assert "<redacted>" in text
        assert "_fp" in text and "_len" in text


# ---------------------------------------------------------------- 5. 被拒请求不留痕

def test_rejected_requests_leave_no_audit_row(db):
    n = _count(db)

    with pytest.raises(ApiError) as legacy:          # 旧维度 → 422
        _alert(db, dimension="fund_type")
    assert legacy.value.status_code == 422
    db.rollback()
    assert _count(db) == n

    with pytest.raises(HTTPException) as missing_url:   # base_url 空 → 400
        create_provider({"label": "示例缺地址", "kind": "deepseek", "model": "m"}, db)
    assert missing_url.value.status_code == 400
    db.rollback()
    assert _count(db) == n

    group = create_source_group({"slug": "reject-app", "label": "示例拒绝应用"}, db)
    n = _count(db)

    with pytest.raises(ApiError) as bad_rules:      # 未知解析键 → 422
        update_parse_rules(group["id"], {"bogus_key": 1}, db)
    assert bad_rules.value.status_code == 422
    db.rollback()
    assert _count(db) == n

    with pytest.raises(HTTPException) as gone:      # 定位不到校正规则 → 404
        delete_correction_rule("reject-app", {"type": "ignore", "match": "不存在"}, db)
    assert gone.value.status_code == 404
    db.rollback()
    assert _count(db) == n

    with pytest.raises(HTTPException) as bad_type:   # 规则类型非法 → 400
        save_correction_rule("reject-app", {"type": "blur", "match": "x"}, db)
    assert bad_type.value.status_code == 400
    db.rollback()
    assert _count(db) == n

    with pytest.raises(HTTPException) as bad_strategy:   # 导入冲突策略非法 → 400
        asyncio.run(execute_import_alert_rules(
            file=UploadFile(filename="a.json", file=io.BytesIO(b'{"rules": []}')),
            conflict_strategy="merge", db=db))
    assert bad_strategy.value.status_code == 400
    db.rollback()
    assert _count(db) == n


# ---------------------------------------------------------------- 6. 链完整性

def test_audit_chain_still_verifies_after_config_writes(db, tmp_path, monkeypatch):
    monkeypatch.setattr(sg_mod, "guide_image_root", lambda: str(tmp_path))

    rule = _alert(db)
    alert_service.update_alert_rule(db, rule.id, AlertRuleUpdate(direction="up"))
    alert_service.delete_alert_rule(db, rule.id, audit_reason="normalization")

    group = create_source_group({"slug": "chain-app", "label": "示例链应用"}, db)
    save_guide_image(group["id"],
                     UploadFile(filename="g.png", file=io.BytesIO(b"\x89PNG" + b"\x00" * 32)),
                     db)
    save_correction_rule("chain-app", {"type": "ignore", "match": "可用额度"}, db)
    update_source_group(group["id"], {"enabled": False}, db)

    provider = create_provider({"label": "示例链供应商", "kind": "dashscope",
                                "base_url": "https://example.invalid/v1",
                                "api_key": KEY_1, "model": "demo"}, db)
    update_provider(provider["id"], {"api_key": KEY_2}, db)
    delete_provider(provider["id"], db, audit_reason="normalization")

    result = audit_chain.verify(db)
    assert result["ok"] is True, result
    assert result["checked"] >= 10