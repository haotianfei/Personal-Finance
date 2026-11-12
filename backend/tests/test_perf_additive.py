"""三期遗留性能/契约加性测试（W7）。

覆盖：建议列表 `summary/gate_failure`、复盘列表 `suggestion_batch_size/
conclusion_excerpt/band_history_missing`、资产项归属批量端点（成功/未标注/
缺失/超限/非法）、OpenAPI 路由抽查、`GET /api/assets` 已含 item 归属。
"""
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError
from models import (AdvisorReview, AdvisorReviewSuggestion, AssetItem, Member)
from routers.asset_items import bulk_item_owners
from services import asset_owner_service, asset_service
from services.advisor import service as advisor_service
from services.advisor import review as review_service
from services.asset_service import create_asset

TODAY = date.today()


class FakeGateway:
    def __init__(self, result=None):
        self.result = result or {
            "title": "示例建议",
            "summary_template": "当前净资产 {{info.nw.total}}，保持观察",
            "items": [{"seq": 1, "subject_kind": "narrative",
                       "subject_ref": None, "direction": "hold",
                       "rationale_template": "净资产 {{info.nw.total}}",
                       "info_refs": ["nw.total"]}],
        }

    def complete_json(self, *, prompt, schema, model=None):
        return self.result


def _item(db, name, amount="1000"):
    return create_asset(db, name, Decimal(amount), TODAY)


def _owner(db, name):
    """归属主体＝members 行（ADR-0019 D6）。"""
    member = Member(name=name, role="self")
    db.add(member)
    db.commit()
    return member


# ---------------------------------------------------------------- 建议列表


def test_advice_list_additive_summary_and_gate_failure(db):
    _item(db, "示例-基金")
    ok = advisor_service.create_suggestion(
        db, gateway=FakeGateway(), dedupe_key="manual:ok")
    gateway = FakeGateway({
        "title": "示例建议",
        "summary_template": "建议增持 12.3%",
        "items": [{"seq": 1, "subject_kind": "narrative",
                   "subject_ref": None, "direction": "hold",
                   "rationale_template": "建议增持 12.3%",
                   "info_refs": ["nw.total"]}],
    })
    bad = advisor_service.create_suggestion(
        db, gateway=gateway, dedupe_key="manual:num")

    by_id = {item["id"]: item
             for item in advisor_service.list_suggestions(db)["items"]}
    assert by_id[ok["suggestion_id"]]["summary"].startswith("当前净资产")
    assert by_id[ok["suggestion_id"]]["gate_failure"] is None
    assert by_id[bad["suggestion_id"]]["gate_failure"] == {
        "code": "GATE_NUMERIC_PROVENANCE", "label": "含数字字面量"}


# ---------------------------------------------------------------- 复盘列表


def test_review_list_additive_fields(db):
    conclusion = "示例结论" * 40
    snapshot = json.dumps({
        "delta": {"comparable": True, "progress_delta": "0.0100"},
        "degradations": [{"code": "REVIEW_BAND_HISTORY_MISSING",
                          "message": "x"}],
    }, ensure_ascii=False)
    row = AdvisorReview(
        goal_id=1, goal_version_id=1, as_of=TODAY, status="open",
        snapshot_json=snapshot, snapshot_sha256="h",
        conclusion=conclusion, created_at="2026-01-01T00:00:00")
    db.add(row)
    db.flush()
    db.add(AdvisorReviewSuggestion(review_id=row.id, suggestion_id=1))
    db.commit()

    item = review_service.list_reviews(db)["items"][0]
    assert item["suggestion_batch_size"] == 1
    assert item["conclusion_excerpt"] == conclusion[:120]
    assert len(item["conclusion_excerpt"]) == 120
    assert item["band_history_missing"] is True

    row2 = AdvisorReview(
        goal_id=1, goal_version_id=1, as_of=TODAY, status="open",
        snapshot_json=json.dumps({"delta": {}, "degradations": []}),
        snapshot_sha256="h2", created_at="2026-01-02T00:00:00")
    db.add(row2)
    db.commit()
    second = next(i for i in review_service.list_reviews(db)["items"]
                  if i["id"] == row2.id)
    assert second["conclusion_excerpt"] is None
    assert second["band_history_missing"] is False
    assert second["suggestion_batch_size"] == 0


# ---------------------------------------------------------------- 归属批量


def test_bulk_owners_success_and_unmarked(db):
    first = _item(db, "示例-甲")
    second = _item(db, "示例-乙")
    member = _owner(db, "示例成员")
    joint = Member(name="示例共有", role="joint")
    db.add(joint)
    db.commit()
    joint_item = _item(db, "示例-丙")
    asset_owner_service.set_item_owner(db, first.asset_item_id, "member",
                                       member.id, reason="合成",
                                       audit_reason="evolution")
    asset_owner_service.set_item_owner(db, joint_item.asset_item_id,
                                       "member", joint.id, reason="共有",
                                       audit_reason="evolution")
    db.commit()

    result = bulk_item_owners(
        ids=f"{first.asset_item_id},{second.asset_item_id},"
            f"{joint_item.asset_item_id}", db=db)
    by_id = {i["id"]: i for i in result["items"]}
    assert by_id[first.asset_item_id]["owner_kind"] == "member"
    assert by_id[first.asset_item_id]["owner_id"] == member.id
    assert by_id[first.asset_item_id]["owner_name"] == "示例成员"
    # 共有归属＝role='joint' 的成员（「家庭池」已随 ADR-0019 D6 退役）
    assert by_id[joint_item.asset_item_id]["owner_id"] == joint.id
    assert by_id[joint_item.asset_item_id]["owner_name"] == "示例共有"
    # 无归属行 → 未标注（字段 null），仍返回该 id
    assert by_id[second.asset_item_id] == {
        "id": second.asset_item_id, "owner_kind": None, "owner_id": None,
        "owner_name": None, "effective_date": None}


def test_bulk_owners_missing_id_omitted(db):
    existing = _item(db, "示例-甲")
    result = bulk_item_owners(ids=f"{existing.asset_item_id},999999", db=db)
    assert [i["id"] for i in result["items"]] == [existing.asset_item_id]
    assert result["total"] == 1


def test_bulk_owners_dedupes_preserving_order(db):
    first = _item(db, "示例-甲")
    second = _item(db, "示例-乙")
    result = bulk_item_owners(
        ids=f"{second.asset_item_id},{first.asset_item_id},"
            f"{second.asset_item_id}", db=db)
    assert [i["id"] for i in result["items"]] == [
        second.asset_item_id, first.asset_item_id]


def test_bulk_owners_limits_and_invalid(db):
    with pytest.raises(ApiError) as ei:
        bulk_item_owners(ids=",".join(str(i) for i in range(1, 202)), db=db)
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "ids"
    with pytest.raises(ApiError):
        bulk_item_owners(ids="1,x", db=db)
    with pytest.raises(ApiError):
        bulk_item_owners(ids="", db=db)


def test_assets_list_already_includes_item_owner(db):
    """ADR-0019：归属来自资产项（`asset_items.owner_id` → members），随列表一次取齐。"""
    item = AssetItem(display_name="示例-甲", status="draft")
    member = Member(name="示例成员", role="self")
    db.add_all([item, member])
    db.commit()
    item.owner_id = member.id
    db.commit()
    a = create_asset(db, "示例-甲", Decimal("100"), TODAY, item_id=item.id)

    from routers.assets import build_asset_response, response_context
    assets, _ = asset_service.list_assets(db)
    payload = build_asset_response(db, assets[0], response_context(db, assets))
    assert payload["asset_item_id"] == a.asset_item_id
    assert payload["owner_kind"] == "self"          # members.role
    assert payload["owner_name"] == "示例成员"
    assert "owner" not in payload["classification_missing"]


# ---------------------------------------------------------------- OpenAPI


def test_openapi_contains_new_routes():
    from main import app
    paths = app.openapi()["paths"]
    assert "/api/asset-items/owners" in paths
    assert "/api/planning/advice" in paths
    assert "/api/planning/reviews" in paths