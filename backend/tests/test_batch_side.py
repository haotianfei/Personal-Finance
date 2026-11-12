"""批量 append 更正 side（冻结契约）测试。

覆盖：side 翻转→旧行 superseded/新行 active/金额日期不变/审计 correct；
已 superseded 行不被动；side 相同不新增行；side+attribute_ids 组合属性挂新行；
非法 side→ValueError（服务层）/路由 400；amount 仍 WriteOnceConflict（409）。
数据一律合成（示例- 前缀、虚构机构）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from models import Asset, AuditLog
from services import asset_service
from services.asset_service import WriteOnceConflict


def _audit_actions(db):
    return [(r.action, r.row_id) for r in db.execute(
        select(AuditLog).where(AuditLog.entity == "asset_snapshot")
    ).scalars().all()]


# ---------------------------------------------------------------- 服务层

def test_batch_side_flip_appends_and_supersedes(db):
    a = asset_service.create_asset(db, "示例-活期", 500, date(2026, 3, 1))
    n = asset_service.batch_update_records(
        db, [a.id], {"side": "liability"}, reason="normalization")
    db.commit()
    old = db.get(Asset, a.id)
    assert old.superseded_by is not None
    new = db.get(Asset, old.superseded_by)
    assert new.superseded_by is None
    assert new.side == "liability" and old.side == "asset"
    # 金额不变、同日
    assert new.amount == Decimal("500.00") == old.amount
    assert new.asset_date == old.asset_date == date(2026, 3, 1)
    # 审计含 correct（append 语义）
    assert ("correct", new.id) in _audit_actions(db)
    assert n >= 1


def test_batch_side_skips_superseded_rows(db):
    a = asset_service.create_asset(db, "示例-稳健基金", 100, date(2026, 3, 1))
    asset_service.correct_asset(db, a.id, "100",
        reason="correction")  # 先制造历史行
    old_id = a.id
    n_before = db.query(Asset).count()
    asset_service.batch_update_records(db, [old_id], {"side": "liability"},
                                       reason="normalization")
    db.commit()
    # 历史行不被动：side 不变、不产生新行
    assert db.get(Asset, old_id).side == "asset"
    assert db.query(Asset).count() == n_before


def test_batch_side_same_side_no_new_row(db):
    a = asset_service.create_asset(db, "示例-指数ETF", 300, date(2026, 3, 1))
    n_before = db.query(Asset).count()
    asset_service.batch_update_records(db, [a.id], {"side": "asset"})
    db.commit()
    assert db.query(Asset).count() == n_before
    assert db.get(Asset, a.id).superseded_by is None
    # 相同 side 不产生 correct 审计
    assert not [x for x in _audit_actions(db) if x[0] == "correct"]


def test_batch_side_with_attribute_ids_rejected(db):
    """ADR-0019：批量更新不再接受分类字段（分类只存在于资产项）→ 422。

    字段名本身就是被拒的依据，`attribute_ids` 里的值不参与任何查询——所以这里不需要
    真造一条旧 EAV 属性行（那张表随 0027 删除）。
    """
    from errors import ApiError

    a = asset_service.create_asset(db, "示例-现金", 88, date(2026, 3, 1))
    with pytest.raises(ApiError) as ei:
        asset_service.batch_update_records(
            db, [a.id], {"side": "liability", "attribute_ids": [1]})
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "attribute_ids"
    db.rollback()
    # 记录未被改动：side 原样、无新行
    db.expire_all()
    assert db.get(Asset, a.id).side == "asset"
    assert db.get(Asset, a.id).superseded_by is None
    assert db.query(Asset).count() == 1


def test_batch_side_mixed_ids_flips_only_active_diff(db):
    a1 = asset_service.create_asset(db, "示例-标的A", 10, date(2026, 3, 1))
    a2 = asset_service.create_asset(db, "示例-标的B", 20, date(2026, 3, 1))
    a3 = asset_service.create_asset(db, "示例-借款C", 30, date(2026, 3, 1),
                                    side="liability")
    n_before = db.query(Asset).count()
    asset_service.batch_update_records(db, [a1.id, a2.id, a3.id],
                                       {"side": "liability"},
                                       reason="normalization")
    db.commit()
    assert db.query(Asset).count() == n_before + 2  # a3 已同侧，不新增
    assert db.get(Asset, a1.id).superseded_by is not None
    assert db.get(Asset, a2.id).superseded_by is not None
    assert db.get(Asset, a3.id).superseded_by is None


def test_batch_side_invalid_raises_valueerror(db):
    a = asset_service.create_asset(db, "示例-标的D", 1, date(2026, 3, 1))
    with pytest.raises(ValueError):
        asset_service.batch_update_records(db, [a.id], {"side": "equity"})
    db.rollback()


def test_batch_amount_still_write_once_conflict(db):
    a = asset_service.create_asset(db, "示例-标的E", 1, date(2026, 3, 1))
    with pytest.raises(WriteOnceConflict):
        asset_service.batch_update_records(db, [a.id], {"amount": "999"})
    with pytest.raises(WriteOnceConflict):
        asset_service.batch_update_records(
            db, [a.id], {"side": "liability", "amount": "999"})
    db.rollback()


# ---------------------------------------------------------------- 路由层

def test_route_batch_update_invalid_side_400(db):
    from routers.assets import batch_update as route_batch
    a = asset_service.create_asset(db, "示例-标的F", 1, date(2026, 3, 1))
    with pytest.raises(HTTPException) as ei:
        route_batch({"ids": [a.id], "side": "bogus"}, db)
    assert ei.value.status_code == 400
    assert ei.value.detail["error"] == "invalid_side"


def test_route_batch_update_invalid_side_envelope_code(db):
    """非法 side 的 400 经契约层包装器归一为信封 code INVALID_SIDE（非 GENERIC_400）。"""
    from errors import ErrorCode, build_envelope_from_detail
    from routers.assets import batch_update as route_batch
    a = asset_service.create_asset(db, "示例-标的F2", 1, date(2026, 3, 1))
    with pytest.raises(HTTPException) as ei:
        route_batch({"ids": [a.id], "side": "equity"}, db)
    env = build_envelope_from_detail(ei.value.status_code, ei.value.detail)
    assert env["error"]["code"] == ErrorCode.INVALID_SIDE == "INVALID_SIDE"
    assert "仅 asset/liability" in env["error"]["message"]


def test_route_batch_update_other_valueerror_not_400(db, monkeypatch):
    """路由已收窄：非 InvalidSideError 的 ValueError 不被误标 400（原样上抛）。"""
    from routers.assets import batch_update as route_batch
    a = asset_service.create_asset(db, "示例-标的F3", 1, date(2026, 3, 1))
    def _boom(*args, **kwargs):
        raise ValueError("unrelated")
    monkeypatch.setattr(asset_service, "batch_update_records", _boom)
    with pytest.raises(ValueError):
        route_batch({"ids": [a.id], "side": "liability"}, db)


def test_route_batch_update_amount_409_not_400(db):
    """WriteOnceConflict 是 ValueError 子类：except 顺序必须先 409 后 400。"""
    from routers.assets import batch_update as route_batch
    a = asset_service.create_asset(db, "示例-标的G", 1, date(2026, 3, 1))
    with pytest.raises(HTTPException) as ei:
        route_batch({"ids": [a.id], "amount": "9"}, db)
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "write_once"


def test_route_batch_update_side_flip_returns_updated_count(db):
    from routers.assets import batch_update as route_batch
    a = asset_service.create_asset(db, "示例-标的H", 66, date(2026, 3, 1))
    out = route_batch({"ids": [a.id], "side": "liability"}, db,
                      reason="normalization")
    assert "updated_count" in out and out["updated_count"] >= 1
    assert db.get(Asset, a.id).superseded_by is not None
