"""取证视图（票 02）：`GET /api/asset-items/{item_id}/history` 的后端契约测试。

覆盖：纯审计时间线、create 全集快照、四张痕迹表的自述、审计优先去重（2 秒窗口
含边界）、同刻排序、游标分页、sources 过滤、limit 夹取、404、prev_hash 截断、
`/api/audit-log` 的 actor/order 扩参、字典行与项级改判行的区分。

契约冻结（前端车道按此编码）：字段名/嵌套/取值枚举不得漂移——断言逐字比对
`services/asset_history_service` 输出的字面量。
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from errors import ApiError
from models import (AssetItem, AssetItemAttrHistory, AssetItemName,
                    AssetItemOwnerChange, AuditLog, Member, PrincipalChange)
from services import asset_history_service, asset_service, audit_service

T0 = datetime(2026, 11, 7, 12, 0, 0)


# ---------------------------------------------------------------- 造数助手


def _item(db, name="示例标的A"):
    """裸资产项（不写审计，保证「纯 trace」用例干净）。"""
    item = AssetItem(display_name=name, status="draft")
    db.add(item)
    db.commit()
    return item


def _member(db, name="示例本人"):
    member = Member(name=name, role="self")
    db.add(member)
    db.commit()
    return member


def _audit_row(db, *, occurred_at, entity="asset_item", action="rename",
               row_id=1, before=None, after=None, actor="owner", prev_hash=None,
               reason=None, reason_note=None, evidence_ref=None) -> AuditLog:
    """直插审计行：绕过 hash-chain 只为精确控制 occurred_at（链本身另有测试）。"""
    row = AuditLog(
        occurred_at=occurred_at if isinstance(occurred_at, str)
        else occurred_at.isoformat(),
        actor=actor, entity=entity, action=action, row_id=row_id,
        before_json=None if before is None else json.dumps(before, ensure_ascii=False),
        after_json=None if after is None else json.dumps(after, ensure_ascii=False),
        evidence_ref=evidence_ref, request_id="req-1", idempotency_key="idem-1",
        prev_hash=prev_hash, reason=reason, reason_note=reason_note,
    )
    db.add(row)
    db.commit()
    return row


def _bulk_audit(db, item_id, count, *, start=T0, step=1):
    """批量造审计行（一次提交）：用于分页/limit 夹取。"""
    rows = [AuditLog(
        occurred_at=(start + timedelta(seconds=i * step)).isoformat(),
        actor="owner", entity="asset_item", action="rename", row_id=item_id,
        before_json=None,
        after_json=json.dumps({"display_name": f"示例名{i}"}, ensure_ascii=False),
    ) for i in range(count)]
    db.add_all(rows)
    db.commit()
    return rows


def _kinds(out):
    return [entry["kind"] for entry in out["entries"]]


# ---------------------------------------------------------------- 1. 纯审计


def test_01_audit_timeline_create_correct_delete(db):
    """create + correct + delete：倒序、只含变化字段、金额是字符串。"""
    a = asset_service.create_asset(db, "示例标的A", 1000, date(2026, 11, 1))
    b, _ = asset_service.correct_asset(db, a.id, "1200",
        reason="correction")
    asset_service.delete_asset(db, b.id,
        reason="normalization")          # 删除链尾行（靠 superseded_by 仍可归属）

    out = asset_history_service.timeline(db, a.asset_item_id)

    assert out["item_id"] == a.asset_item_id
    assert out["display_name"] == "示例标的A"
    assert _kinds(out) == ["delete", "correct", "create"]      # 最新在上
    assert all(entry["source"] == "audit" for entry in out["entries"])
    assert [entry["title"] for entry in out["entries"]] == ["删除", "更正", "新增"]
    assert out["truncated"] is False and out["next_cursor"] is None

    correct = out["entries"][1]
    assert correct["changes"] == [
        {"field": "amount", "label": "金额", "from": "1000.00", "to": "1200.00"},
        {"field": "side", "label": "方向", "from": "asset", "to": "asset"},
    ]
    assert isinstance(correct["changes"][0]["from"], str)       # 金额绝不经 float

    deleted = out["entries"][0]
    assert deleted["changes"] == [
        {"field": "amount", "label": "金额", "from": "1200.00", "to": None},
        {"field": "side", "label": "方向", "from": "asset", "to": None},
        {"field": "asset_date", "label": "日期", "from": "2026-11-01", "to": None},
    ]


def test_02_create_changes_carry_full_after_keys(db):
    """create 的 changes＝after 全集，from 一律 null。"""
    a = asset_service.create_asset(db, "示例标的B", 888.5, date(2026, 11, 2))

    out = asset_history_service.timeline(db, a.asset_item_id, sources="audit")
    assert len(out["entries"]) == 1
    entry = out["entries"][0]

    assert {change["field"] for change in entry["changes"]} == {
        "amount", "side", "asset_date"}
    assert all(change["from"] is None for change in entry["changes"])
    assert {change["to"] for change in entry["changes"]} == {
        "888.50", "asset", "2026-11-02"}
    assert entry["tech"]["entity"] == "asset_snapshot"
    assert entry["tech"]["action"] == "create"
    assert entry["actor_label"] == "我"


# ---------------------------------------------------------------- 2. 纯痕迹


def test_03_trace_only_four_tables(db):
    """四张痕迹表 → 四种 kind，source 全是 trace，标题走受控词表。"""
    item = _item(db, "示例标的C")
    member = _member(db)
    db.add(AssetItemAttrHistory(
        item_id=item.id, dim="liquidity", old_value="0", new_value="30",
        changed_at=datetime(2026, 11, 2, 10, 0, 0), changed_by="owner",
        reason="流动性口径调整"))
    db.add(AssetItemOwnerChange(
        asset_item_id=item.id, owner_kind="member", owner_id=member.id,
        effective_date=date(2026, 11, 2), reason="归属更正", origin="manual",
        created_at="2026-11-03T10:00:00"))
    db.add(PrincipalChange(
        asset_item_id=item.id, new_amount=Decimal("5000.00"),
        effective_date=date(2026, 11, 4), reason="首次登记口径",
        origin="external", created_at="2026-11-04T10:00:00"))
    db.add(AssetItemName(
        item_id=item.id, name="示例标的C-曾用名", effective_from=date(2026, 11, 1),
        origin="rename", reason="correction", reason_note="改名备注",
        created_at=datetime(2026, 11, 5, 10, 0, 0)))
    db.commit()

    out = asset_history_service.timeline(db, item.id, sources="trace")

    assert _kinds(out) == ["rename", "principal_change", "owner_change",
                           "classification_change"]
    assert all(entry["source"] == "trace" for entry in out["entries"])
    assert [entry["title"] for entry in out["entries"]] == [
        "改名", "本金变更", "归属变更", "分类改判"]

    rename = out["entries"][0]
    assert rename["changes"] == [
        {"field": "display_name", "label": "显示名",
         "from": None, "to": "示例标的C-曾用名"}]
    assert rename["note"] == "改名备注"
    assert rename["tech"]["entity"] == "asset_item_names"

    owner = out["entries"][2]
    assert owner["changes"] == [
        {"field": "owner", "label": "归属", "from": None, "to": "示例本人"}]
    assert owner["tech"]["entity"] == "asset_item_owner_changes"

    principal = out["entries"][1]
    assert principal["changes"][0]["to"] == "5000.00"
    assert principal["tech"]["entity"] == "principal_changes"

    classification = out["entries"][3]
    assert classification["changes"] == [
        {"field": "liquidity", "label": "流动性", "from": "0", "to": "30"}]


# ---------------------------------------------------------------- 3. 去重（2 秒窗口）


def _owner_pair(db, item, member, *, trace_ago_seconds):
    """一条 trace 归属行（created_at = T - Δ）＋ 一条同 kind 的审计行（T）。"""
    moment = T0 - timedelta(seconds=trace_ago_seconds)
    trace = AssetItemOwnerChange(
        asset_item_id=item.id, owner_kind="member", owner_id=member.id,
        effective_date=date(2026, 11, 1), reason="归属更正", origin="manual",
        created_at=moment.isoformat())
    db.add(trace)
    db.commit()
    _audit_row(db, occurred_at=T0, entity="asset_item_owner", action="change",
               row_id=trace.id,
               before={"owner_kind": "member", "owner_id": 7, "owner_name": "示例甲"},
               after={"owner_kind": "member", "owner_id": member.id,
                      "owner_name": member.name})
    return trace


def test_04_dedupe_drops_trace_within_two_seconds(db):
    """同 kind 差 1 秒：只剩审计那条（审计为准）。"""
    item = _item(db, "示例标的D")
    member = _member(db)
    _owner_pair(db, item, member, trace_ago_seconds=1)

    out = asset_history_service.timeline(db, item.id)

    owner_entries = [e for e in out["entries"] if e["kind"] == "owner_change"]
    assert len(owner_entries) == 1
    assert owner_entries[0]["source"] == "audit"
    assert owner_entries[0]["title"] == "归属变更"


def test_05_three_seconds_keeps_both(db):
    """差 3 秒：边界证明窗口是 2 秒——两条都在。"""
    item = _item(db, "示例标的E")
    member = _member(db)
    _owner_pair(db, item, member, trace_ago_seconds=3)

    out = asset_history_service.timeline(db, item.id)

    owner_entries = [e for e in out["entries"] if e["kind"] == "owner_change"]
    assert len(owner_entries) == 2
    assert {entry["source"] for entry in owner_entries} == {"audit", "trace"}


# ---------------------------------------------------------------- 4. 排序


def test_06_same_moment_audit_comes_first(db):
    """同一 occurred_at：audit 排在 trace 之前（kind 不同才不会被去重吃掉）。"""
    item = _item(db, "示例标的F")
    db.add(AssetItemAttrHistory(
        item_id=item.id, dim="risk", old_value="R2", new_value="R3",
        changed_at=T0, changed_by="owner", reason="风险复核"))
    db.commit()
    _audit_row(db, occurred_at=T0, entity="asset_item", action="rename",
               row_id=item.id, before={"display_name": "老名"},
               after={"display_name": "新名"})

    out = asset_history_service.timeline(db, item.id)

    assert [entry["source"] for entry in out["entries"]] == ["audit", "trace"]
    assert _kinds(out) == ["rename", "classification_change"]


# ---------------------------------------------------------------- 5. 分页


def test_07_cursor_pagination_60_rows(db):
    """60 条：首页 50＋truncated＋next_cursor；第二页剩下的 10 条，两页无重叠。"""
    item = _item(db, "示例标的G")
    _bulk_audit(db, item.id, 60)

    first = asset_history_service.timeline(db, item.id)
    assert len(first["entries"]) == 50
    assert first["truncated"] is True
    assert first["next_cursor"]

    second = asset_history_service.timeline(db, item.id, cursor=first["next_cursor"])
    assert len(second["entries"]) == 10
    assert second["truncated"] is False and second["next_cursor"] is None

    ids_first = {entry["row_id"] for entry in first["entries"]}
    ids_second = {entry["row_id"] for entry in second["entries"]}
    assert ids_first & ids_second == set()          # 无重叠
    assert len(ids_first | ids_second) == 60        # 不漏条目
    # 倒序：首页全部晚于第二页
    assert min(e["occurred_at"] for e in first["entries"]) > \
        max(e["occurred_at"] for e in second["entries"])


def test_08_sources_filter(db):
    """sources=audit 只回审计行；sources=trace 只回痕迹行。"""
    item = _item(db, "示例标的H")
    db.add(AssetItemAttrHistory(
        item_id=item.id, dim="channel", old_value="示例渠道甲", new_value="示例渠道乙",
        changed_at=T0, changed_by="owner", reason="渠道更正"))
    db.commit()
    _audit_row(db, occurred_at=T0 + timedelta(seconds=10), entity="asset_item",
               action="archive", row_id=item.id, before={"status": "active"},
               after={"status": "archived"})

    audit_only = asset_history_service.timeline(db, item.id, sources="audit")
    assert _kinds(audit_only) == ["archive"]
    assert audit_only["entries"][0]["source"] == "audit"

    trace_only = asset_history_service.timeline(db, item.id, sources="trace")
    assert _kinds(trace_only) == ["classification_change"]
    assert trace_only["entries"][0]["source"] == "trace"


def test_09_limit_999_clamped_to_200(db):
    """limit 超上限一律取 200（夹取而非 422）。"""
    item = _item(db, "示例标的I")
    _bulk_audit(db, item.id, 210)

    out = asset_history_service.timeline(db, item.id, limit=999)

    assert len(out["entries"]) == 200
    assert out["truncated"] is True and out["next_cursor"]


# ---------------------------------------------------------------- 6. 错误与细节


def test_10_unknown_item_is_404(db):
    """不存在的资产项 → 404（不返回空时间线冒充成功）。"""
    with pytest.raises(ApiError) as excinfo:
        asset_history_service.timeline(db, 999999)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "NOT_FOUND"


def test_11_prev_hash_is_first_8_chars(db):
    """技术细节里的 prev_hash 只出前 8 位，row_hash 恒 null。"""
    item = _item(db, "示例标的J")
    _audit_row(db, occurred_at=T0, entity="asset_item", action="archive",
               row_id=item.id, before={"status": "active"},
               after={"status": "archived"},
               prev_hash="0123456789abcdef" * 4)

    entry = asset_history_service.timeline(db, item.id)["entries"][0]

    assert entry["tech"]["prev_hash"] == "01234567"
    assert entry["tech"]["row_hash"] is None
    assert entry["tech"]["request_id"] == "req-1"
    assert entry["tech"]["idempotency_key"] == "idem-1"


def test_12_invalid_cursor_and_sources(db):
    """游标解析失败 → 400 INVALID_CURSOR；未知来源 → 422。"""
    item = _item(db, "示例标的K")
    _bulk_audit(db, item.id, 3)

    with pytest.raises(ApiError) as excinfo:
        asset_history_service.timeline(db, item.id, cursor="不是游标")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "INVALID_CURSOR"

    with pytest.raises(ApiError) as excinfo:
        asset_history_service.timeline(db, item.id, sources="audit,forensic")
    assert excinfo.value.status_code == 422


def test_13_audit_log_actor_and_order(db):
    """`list_audit_log` 扩参：order=asc 正序、actor 精确过滤、非法 order 422。"""
    item = _item(db, "示例标的L")
    _audit_row(db, occurred_at=T0, entity="asset_item", action="rename",
               row_id=item.id, actor="owner")
    _audit_row(db, occurred_at=T0 + timedelta(seconds=5), entity="asset_item",
               action="archive", row_id=item.id, actor="system")

    asc, total = audit_service.list_audit_log(db, entity="asset_item", order="asc")
    assert total == 2
    assert [row["action"] for row in asc] == ["rename", "archive"]   # 正序

    system_rows, system_total = audit_service.list_audit_log(db, actor="system")
    assert system_total == 1 and system_rows[0]["actor"] == "system"

    with pytest.raises(ApiError) as excinfo:
        audit_service.list_audit_log(db, order="sideways")
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_14_dictionary_rows_do_not_pollute_item_timeline(db):
    """`classification` 字典 CRUD 行与项级改判同 entity：靠快照键集合区分。"""
    item = _item(db, "示例标的M")
    # 字典行：row_id 恰好等于资产项 id（模拟 id 撞车）
    _audit_row(db, occurred_at=T0, entity="classification", action="update",
               row_id=item.id, before={"code": "A01", "name": "示例类别旧"},
               after={"code": "A01", "name": "示例类别新"})
    # 项级改判行：快照只含维度键
    _audit_row(db, occurred_at=T0 + timedelta(seconds=10), entity="classification",
               action="update", row_id=item.id,
               before={"liquidity": 0}, after={"liquidity": 30})

    out = asset_history_service.timeline(db, item.id, sources="audit")

    assert len(out["entries"]) == 1
    entry = out["entries"][0]
    assert entry["kind"] == "classification_change"
    assert entry["title"] == "分类改判"
    assert entry["changes"] == [
        {"field": "liquidity", "label": "流动性", "from": "0", "to": "30"}]