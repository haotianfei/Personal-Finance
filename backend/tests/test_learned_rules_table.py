"""0031 拆表后教学规则的读写路径验收（表 `source_group_learned_rules`）。

覆盖面（`rename` / `ignore` / `exclude` 三型）：

1. 增：POST 落表 → `origin='manual'`，`created_at` / `updated_at` / `hit_count` 显式落值；
2. 删：DELETE 只删目标组的目标行，其他组同 match 的行不受影响；
3. 改：PATCH 原地更新 `match` / `replace` / `note`，保留 `id` / `created_at`，刷新 `updated_at`；
4. 换序：swap 两条 `sort_order` 互换且整体顺序正确（rename 的级联顺序靠它保）；
5. 唯一约束：同组同型同 (match, replace) 重复添加 → **幂等 200**（不追加、不改写 note）；
6. 迁移行：`origin='migration'` 的行可正常读出并按 `sort_order` → `id` 参与排序；
7. 读出形状与拆表前 JSON 时代的响应**逐字段相等**（键名 / 值 / 顺序）；
8. HTTP 契约：四个端点的方法 / 路径 / 状态码（200 / 400 / 404 / 409）不变。

测试数据一律合成：来源组 `demo-group` /「示例来源组」，规则内容「示例基金 A」之类，
不含真实机构名、真实金额或本机路径。
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from database import get_db
from models import SourceGroup, SourceGroupLearnedRule
from routers.screenshots import (
    delete_correction_rule,
    patch_correction_rule,
    save_correction_rule,
    swap_correction_rules,
)
from routers.source_groups import create_source_group
from services.idempotency_service import IdempotencyCaptureMiddleware
from services.source_group_rules_service import (
    RuleConflictError,
    correction_rules_of,
    get_group,
    list_learned_rules,
    serialize_group,
)


# ---------------------------------------------------------------- 助手
def make_group(db, slug="demo-group", label="示例来源组", rules=None):
    return create_source_group({
        "slug": slug, "label": label, "asset_prefix": "示例-", "rules": rules,
    }, db)


def rows_of(db, sg_id, rule_type=None):
    """该组（可限定类型）的规则行，按服务层同一口径排序。"""
    rows = list_learned_rules(db, sg_id)
    return [r for r in rows if rule_type is None or r.rule_type == rule_type]


def only_row(db, sg_id, rule_type):
    rows = rows_of(db, sg_id, rule_type)
    assert len(rows) == 1, f"{rule_type} 行数应为 1，实际 {len(rows)}"
    return rows[0]


def rules_json(response):
    return json.loads(response["rules"]) if response["rules"] else {}


@pytest.fixture()
def client(db):
    """最小应用：只挂本车道涉及的两条路由（装配方式同 test_parser_vocabulary_api）。"""
    from main import register_exception_handlers
    from routers import screenshots as screenshots_router
    from routers import source_groups as sg_router

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(screenshots_router.router, prefix="/api/screenshots")
    app.include_router(sg_router.router, prefix="/api/source-groups")
    return TestClient(app)


# =====================================================================
# 1. 增（POST）→ 表里多一行，origin='manual'，created_at / hit_count 非空
# =====================================================================
def test_post_inserts_manual_row(db):
    sg = make_group(db)
    out = save_correction_rule(
        sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "示例新名"}, db)

    row = only_row(db, sg["id"], "rename")
    assert (row.source_group_id, row.rule_type) == (sg["id"], "rename")
    assert (row.match, row.replace) == ("示例旧名", "示例新名")
    assert row.origin == "manual"                    # 手工入口一律 manual
    assert row.note is None
    assert row.sort_order == 0                       # 空表首条
    assert row.created_at is not None and row.updated_at is not None
    assert row.hit_count == 0                        # P2 占位，本期恒 0（但不为 NULL）
    # 响应仍带三键视图（与拆表前同形）
    assert rules_json(out)["rename"] == [{"match": "示例旧名", "replace": "示例新名"}]


def test_post_appends_at_end_of_same_type(db):
    sg = make_group(db)
    for name in ("示例基金 A", "示例基金 B", "示例基金 C"):
        save_correction_rule(
            sg["slug"], {"type": "ignore", "match": name}, db)
    assert [(r.match, r.sort_order) for r in rows_of(db, sg["id"], "ignore")] == [
        ("示例基金 A", 0), ("示例基金 B", 1), ("示例基金 C", 2)]


def test_post_ignore_with_note_lands_note_column(db):
    sg = make_group(db)
    save_correction_rule(
        sg["slug"], {"type": "ignore", "match": "示例噪声", "note": " 总金额行 "}, db)
    row = only_row(db, sg["id"], "ignore")
    assert row.note == "总金额行"                     # trim 后落列
    assert row.replace is None                       # 仅 rename 用 replace


# =====================================================================
# 2. 删（DELETE）→ 行消失，其他组不受影响
# =====================================================================
def test_delete_removes_row(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例噪声"}, db)
    assert len(rows_of(db, sg["id"])) == 1

    delete_correction_rule(sg["slug"], {"type": "ignore", "match": "示例噪声"}, db, reason="normalization")
    assert rows_of(db, sg["id"]) == []


def test_delete_does_not_touch_other_groups(db):
    sg_a = make_group(db, slug="demo-group", label="示例来源组")
    sg_b = make_group(db, slug="demo-other", label="示例来源组二")

    # 两组同型同 match（唯一约束按组隔离，理应各存一行）
    save_correction_rule(sg_a["slug"], {"type": "ignore", "match": "示例噪声"}, db)
    save_correction_rule(sg_b["slug"], {"type": "ignore", "match": "示例噪声"}, db)

    delete_correction_rule(sg_a["slug"], {"type": "ignore", "match": "示例噪声"}, db, reason="normalization")

    assert rows_of(db, sg_a["id"]) == []
    assert [r.match for r in rows_of(db, sg_b["id"])] == ["示例噪声"]


def test_delete_identity_includes_replace(db):
    sg = make_group(db)
    save_correction_rule(
        sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "示例新名"}, db)
    # rename 身份是 (match, replace)：replace 不同 → 定位不到（上层 404）
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        delete_correction_rule(
            sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "示例别的"}, db, reason="normalization")
    assert ei.value.status_code == 404
    assert len(rows_of(db, sg["id"], "rename")) == 1


# =====================================================================
# 3. 改（PATCH）→ match / replace / note 更新，updated_at 变化
# =====================================================================
def test_patch_updates_fields_in_place(db):
    sg = make_group(db)
    save_correction_rule(
        sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "示例新名"}, db)
    before = only_row(db, sg["id"], "rename")
    row_id = before.id
    # 把时间戳压到过去，避免同微秒内 updated_at 比较失效
    past = datetime.now() - timedelta(days=1)
    before.created_at = past
    before.updated_at = past
    db.commit()

    patch_correction_rule(sg["slug"], {
        "type": "rename", "match": "示例旧名", "replace": "示例新名",
        "new_match": "示例旧名二", "new_replace": "示例新名二", "new_note": "官方名",
    }, db)

    after = only_row(db, sg["id"], "rename")
    assert (after.match, after.replace, after.note) == ("示例旧名二", "示例新名二", "官方名")
    assert after.id == row_id                        # 原地更新，不换行
    assert after.created_at == past                  # created_at 不动
    assert after.updated_at > past                   # updated_at 刷新
    assert after.origin == "manual" and after.hit_count == 0


def test_patch_clears_note_without_changing_position(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例 A"}, db)
    save_correction_rule(
        sg["slug"], {"type": "ignore", "match": "示例 B", "note": "旧备注"}, db)

    out = patch_correction_rule(sg["slug"], {
        "type": "ignore", "match": "示例 B", "new_note": ""}, db)

    row = [r for r in rows_of(db, sg["id"], "ignore") if r.match == "示例 B"][0]
    assert row.note is None                          # 空串 = 清空
    assert row.sort_order == 1                       # 编辑不改位置
    assert rules_json(out)["ignore"] == ["示例 A", "示例 B"]   # 无 note → 裸字符串形态


def test_patch_conflict_raises_rule_conflict(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例 A"}, db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例 B"}, db)
    with pytest.raises(RuleConflictError):
        # 服务层直接抛冲突（路由层映射 409，见 HTTP 契约用例）
        from services.source_group_rules_service import update_learned_rule
        update_learned_rule(db, sg["slug"], "ignore", "示例 A", None, new_match="示例 B")
    assert [r.match for r in rows_of(db, sg["id"], "ignore")] == ["示例 A", "示例 B"]


# =====================================================================
# 4. swap → 两条 sort_order 互换且整体顺序正确
# =====================================================================
def test_swap_renames_swaps_sort_order(db):
    sg = make_group(db)
    for match, replace in (("示例 A", "示例 A1"), ("示例 B", "示例 B1"), ("示例 C", "示例 C1")):
        save_correction_rule(
            sg["slug"], {"type": "rename", "match": match, "replace": replace}, db)

    out = swap_correction_rules(sg["slug"], {
        "type": "rename",
        "a": {"match": "示例 A", "replace": "示例 A1"},
        "b": {"match": "示例 C", "replace": "示例 C1"}}, db)

    rows = rows_of(db, sg["id"], "rename")
    assert [(r.match, r.sort_order) for r in rows] == [
        ("示例 C", 0), ("示例 B", 1), ("示例 A", 2)]
    # 响应里数组顺序同步（rename 的级联顺序就是数组顺序）
    assert [e["match"] for e in rules_json(out)["rename"]] == ["示例 C", "示例 B", "示例 A"]


def test_swap_does_not_affect_other_types(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例噪声 A"}, db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例噪声 B"}, db)
    save_correction_rule(
        sg["slug"], {"type": "rename", "match": "示例 X", "replace": "示例 X1"}, db)

    swap_correction_rules(sg["slug"], {
        "type": "ignore",
        "a": {"match": "示例噪声 A"}, "b": {"match": "示例噪声 B"}}, db)

    assert [r.match for r in rows_of(db, sg["id"], "ignore")] == ["示例噪声 B", "示例噪声 A"]
    row = only_row(db, sg["id"], "rename")
    assert (row.match, row.sort_order) == ("示例 X", 0)


# =====================================================================
# 5. 唯一约束：同组同型同 (match, replace) 重复添加 → 幂等 200
# =====================================================================
def test_duplicate_add_is_idempotent_200(db):
    sg = make_group(db)
    payload = {"type": "rename", "match": "示例旧名", "replace": "示例新名"}
    first = save_correction_rule(sg["slug"], payload, db)
    second = save_correction_rule(sg["slug"], payload, db)      # 不抛错

    assert len(rows_of(db, sg["id"], "rename")) == 1
    assert rules_json(first) == rules_json(second)


def test_duplicate_add_does_not_overwrite_note(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例噪声"}, db)
    save_correction_rule(
        sg["slug"], {"type": "ignore", "match": "示例噪声", "note": "补注"}, db)
    row = only_row(db, sg["id"], "ignore")
    assert row.note is None            # 已存在条目不被补写/升级（旧幂等语义）


def test_same_match_different_replace_is_not_duplicate(db):
    sg = make_group(db)
    save_correction_rule(sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "甲"}, db)
    save_correction_rule(sg["slug"], {"type": "rename", "match": "示例旧名", "replace": "乙"}, db)
    assert [(r.match, r.replace) for r in rows_of(db, sg["id"], "rename")] == [
        ("示例旧名", "甲"), ("示例旧名", "乙")]


def test_unique_index_is_per_group(db):
    sg_a = make_group(db, slug="demo-group", label="示例来源组")
    sg_b = make_group(db, slug="demo-other", label="示例来源组二")
    save_correction_rule(sg_a["slug"], {"type": "exclude", "match": "示例-已清仓"}, db)
    save_correction_rule(sg_b["slug"], {"type": "exclude", "match": "示例-已清仓"}, db)
    assert len(rows_of(db, sg_a["id"], "exclude")) == 1
    assert len(rows_of(db, sg_b["id"], "exclude")) == 1


# =====================================================================
# 6. 迁移后兼容：origin='migration' 的行能读出并参与排序
# =====================================================================
def _add_row(db, sg_id, rule_type, match, replace=None, note=None,
             sort_order=0, origin="migration"):
    now = datetime.now()
    row = SourceGroupLearnedRule(
        source_group_id=sg_id, rule_type=rule_type, match=match, replace=replace,
        note=note, sort_order=sort_order, origin=origin, hit_count=0,
        created_at=now, updated_at=now)
    db.add(row)
    db.commit()
    return row


def test_migrated_rows_are_read_and_ordered(db):
    sg = make_group(db)
    # 造数与 id 顺序相反：sort_order 才是语义顺序（rename 的级联顺序）
    _add_row(db, sg["id"], "rename", "示例 C", "示例 C1", sort_order=2)
    _add_row(db, sg["id"], "rename", "示例 B", "示例 B1", sort_order=1)
    _add_row(db, sg["id"], "rename", "示例 A", "示例 A1", sort_order=0)

    assert [r.origin for r in rows_of(db, sg["id"])] == ["migration"] * 3
    assert [e["match"] for e in correction_rules_of(db, sg["id"])["rename"]] == [
        "示例 A", "示例 B", "示例 C"]


def test_migrated_and_manual_rows_sort_together(db):
    sg = make_group(db)
    _add_row(db, sg["id"], "ignore", "示例迁移项", sort_order=0)
    save_correction_rule(sg["slug"], {"type": "ignore", "match": "示例新项"}, db)

    rows = rows_of(db, sg["id"], "ignore")
    assert [(r.match, r.origin, r.sort_order) for r in rows] == [
        ("示例迁移项", "migration", 0), ("示例新项", "manual", 1)]


def test_migrated_exclude_dict_and_string_forms(db):
    """迁移把两种历史形态都归一成 match 列；读回时按有无 note 折回旧形态。"""
    sg = make_group(db)
    _add_row(db, sg["id"], "exclude", "示例-已清仓", sort_order=0)
    _add_row(db, sg["id"], "exclude", "示例-已转出", note="已转出", sort_order=1)

    assert correction_rules_of(db, sg["id"])["exclude"] == [
        "示例-已清仓", {"match": "示例-已转出", "note": "已转出"}]


# =====================================================================
# 7. 读出形状 == 拆表前 JSON 时代的响应（逐字段相等）
# =====================================================================
LEGACY_JSON = {
    "layout": "vertical",
    "rename": [{"match": "示例基金 A", "replace": "示例基金 A（官方名）"},
               {"match": "示例固收", "replace": "示例固收 C", "note": "补注"}],
    "ignore": ["示例噪声", {"match": "示例总计", "note": "总金额行"}],
    "exclude": ["示例-已清仓"],
}


def test_read_shape_matches_legacy_json_payload(db):
    sg = make_group(db)
    for idx, entry in enumerate(LEGACY_JSON["rename"]):
        _add_row(db, sg["id"], "rename", entry["match"], entry["replace"],
                 entry.get("note"), sort_order=idx)
    for idx, entry in enumerate(LEGACY_JSON["ignore"]):
        match = entry["match"] if isinstance(entry, dict) else entry
        note = entry.get("note") if isinstance(entry, dict) else None
        _add_row(db, sg["id"], "ignore", match, None, note, sort_order=idx)
    for idx, entry in enumerate(LEGACY_JSON["exclude"]):
        _add_row(db, sg["id"], "exclude", entry, sort_order=idx)

    got = correction_rules_of(db, sg["id"])
    # 键名与值逐字段相等（顺序即 sort_order 顺序）
    assert got == {
        "rename": LEGACY_JSON["rename"],
        "ignore": LEGACY_JSON["ignore"],
        "exclude": LEGACY_JSON["exclude"],
    }


def test_serialize_group_merges_learned_rules_into_rules_payload(db):
    """响应里的 `rules` 键仍是「组级解析配置 + 三键」的合并视图（前端契约）。"""
    sg = make_group(db, rules=json.dumps({"layout": "vertical"}, ensure_ascii=False))
    for idx, entry in enumerate(LEGACY_JSON["rename"]):
        _add_row(db, sg["id"], "rename", entry["match"], entry["replace"],
                 entry.get("note"), sort_order=idx)

    payload = serialize_group(get_group(db, sg["slug"]))
    merged = json.loads(payload["rules"])
    assert merged["layout"] == "vertical"            # 列里的组级解析键原样保留
    assert merged["rename"] == LEGACY_JSON["rename"]  # 表里的教学规则拼回
    assert set(merged) == {"layout", "rename"}       # 空类型不写键（「空」= 「缺」）


def test_serialize_group_without_learned_rules_returns_column_verbatim(db):
    """没有教学规则时 `rules` 原样返回（纯读回场景的逐字节契约）。"""
    raw = json.dumps({"layout": "card", "amount_col": 1}, ensure_ascii=False)
    sg = make_group(db, rules=raw)
    assert serialize_group(get_group(db, sg["slug"]))["rules"] == raw


# =====================================================================
# 8. HTTP 契约：四个端点的方法 / 路径 / 状态码不变
# =====================================================================
def test_http_correction_rules_contract(client, db):
    make_group(db)
    base = "/api/screenshots/source-groups/demo-group"

    r = client.post(f"{base}/correction-rules",
                    json={"type": "rename", "match": "示例 A", "replace": "示例 A1"})
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["rules"])["rename"] == [
        {"match": "示例 A", "replace": "示例 A1"}]
    assert set(r.json()) == {"id", "slug", "label", "asset_prefix", "guide",
                             "guide_image", "rules", "enabled", "sort_order",
                             "account_ids", "created_at", "updated_at"}

    r = client.post(f"{base}/correction-rules",
                    json={"type": "ignore", "match": "示例 B"})
    assert r.status_code == 200, r.text

    # swap：200
    r = client.post(f"{base}/correction-rules/swap", json={
        "type": "ignore", "a": {"match": "示例 B"}, "b": {"match": "示例 B"}})
    assert r.status_code == 400           # a 与 b 同一条 → 400

    r = client.post(f"{base}/correction-rules/swap", json={
        "type": "rename",
        "a": {"match": "示例 A", "replace": "示例 A1"},
        "b": {"match": "不存在的", "replace": "示例"}})
    assert r.status_code == 404           # 一侧定位不到 → 404

    # PATCH：200 / 409 / 400
    client.post(f"{base}/correction-rules",
                json={"type": "ignore", "match": "示例 C"})
    r = client.patch(f"{base}/correction-rules",
                     json={"type": "ignore", "match": "示例 C", "new_match": "示例 B"})
    assert r.status_code == 409           # 与现有条目重复
    r = client.patch(f"{base}/correction-rules",
                     json={"type": "ignore", "match": "示例 C", "new_note": "补注"})
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["rules"])["ignore"] == [
        "示例 B", {"match": "示例 C", "note": "补注"}]
    r = client.patch(f"{base}/correction-rules",
                     json={"type": "rename", "match": "示例 A", "new_note": "缺 replace"})
    assert r.status_code == 400

    # DELETE：200 / 404
    # spec E3：删规则＝纠正类，必须带 reason（缺则 422）
    r = client.request("DELETE", f"{base}/correction-rules",
                       json={"type": "ignore", "match": "示例 B"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"

    r = client.request("DELETE", f"{base}/correction-rules",
                       params={"reason": "normalization"},
                       json={"type": "ignore", "match": "示例 B"})
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["rules"])["ignore"] == [
        {"match": "示例 C", "note": "补注"}]
    r = client.request("DELETE", f"{base}/correction-rules",
                       json={"type": "ignore", "match": "示例 B"})
    assert r.status_code == 404

    # 组不存在 → 404
    r = client.post("/api/screenshots/source-groups/no-such-group/correction-rules",
                    json={"type": "ignore", "match": "示例 X"})
    assert r.status_code == 404


def test_http_create_rejects_unknown_rules_keys(client, db):
    """创建口已接 validate_group_rules：未知键（含拆表三键）→ 422，且零写入。"""
    r = client.post("/api/source-groups", json={
        "label": "示例来源组", "slug": "demo-group",
        "rules": {"ignore": ["示例噪声"]}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SOURCE_GROUP_RULES_INVALID"
    assert db.execute(select(SourceGroup)).scalars().all() == []

    r = client.post("/api/source-groups", json={
        "label": "示例来源组", "slug": "demo-group",
        "rules": {"layout": "vertical", "nmae_col": 1}})
    assert r.status_code == 422
    assert db.execute(select(SourceGroup)).scalars().all() == []

    # 合法键照旧可建（并归一：str → list、空配置 → NULL）
    r = client.post("/api/source-groups", json={
        "label": "示例来源组", "slug": "demo-group",
        "rules": {"amount_header": "示例金额"}})
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["rules"]) == {"amount_header": ["示例金额"]}