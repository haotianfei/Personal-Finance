"""分析/占比路由的维度词汇（ADR-0019）。

锁三件事：
1. 七个新维度名在路由层可用（`asset_class` 含后代展开、`liquidity` 收整数天数）；
2. 旧路径/旧参数**保留但 410**：`details.use` 指路新路径，而不是 404/空序列——
   「看起来像这个类别没有钱」比报错危险得多；
3. 占比的 `dimension` 由 `proportion_service.require_dimension()` 单点校验：
   新名全通、旧名 422 且 `details.replacement` 给出新名。

数据全部合成；DB＝内存 SQLite（`db` fixture）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import get_db
from errors import ApiError
from models import (AllocationTarget, AssetClass, Channel, Institution, Member,
                    Tag)
from routers import allocations as allocations_router
from routers import analysis as analysis_router
from routers import proportion as proportion_router
from schemas import AllocationTargetCreate
from services import allocation_service, classification_service
from services.asset_service import create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)


def _client(db):
    from main import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(analysis_router.router, prefix="/api/analysis")
    app.include_router(proportion_router.router, prefix="/api/proportions")
    app.include_router(allocations_router.router, prefix="/api/allocations")
    return TestClient(app)


def _mk(db, name, amount, on_date, *, liquidity_days=0, **dims):
    row = create_asset(db, name, Decimal(str(amount)), on_date)
    payload = {"asset_class_id": dims["klass"].id,
               "liquidity_days": liquidity_days,
               "institution_id": dims["institution"].id,
               "owner_id": dims["owner"].id}
    if dims.get("channel") is not None:
        payload["channel_id"] = dims["channel"].id
    classification_service.set_classification(db, row.asset_item_id, payload)
    return row


@pytest.fixture()
def seeded(db):
    root = AssetClass(code="A", name="示例大类", level=1, side_scope="any")
    db.add(root)
    db.commit()
    leaf = AssetClass(code="A1", name="示例子类甲", parent_id=root.id, level=2,
                      side_scope="any")
    leaf2 = AssetClass(code="A2", name="示例子类乙", parent_id=root.id, level=2,
                       side_scope="any")
    other = AssetClass(code="B", name="示例他类", level=1, side_scope="any")
    institution = Institution(name="示例机构", kind="bank")
    channel = Channel(name="示例渠道")
    owner = Member(name="本人", role="self")
    tag = Tag(name="示例标签", kind="controlled")
    db.add_all([leaf, leaf2, other, institution, channel, owner, tag])
    db.commit()

    first = _mk(db, "示例-子类甲", 1000, D_JAN, klass=leaf, institution=institution,
                channel=channel, owner=owner)
    _mk(db, "示例-子类甲", 1000, D_FEB, klass=leaf, institution=institution,
        channel=channel, owner=owner)
    _mk(db, "示例-子类乙", 300, D_FEB, klass=leaf2, institution=institution,
        owner=owner)
    _mk(db, "示例-他类", 400, D_FEB, klass=other, institution=institution,
        owner=owner, liquidity_days=30)
    classification_service.set_tags(db, first.asset_item_id, [tag.id])
    return {"root": root, "leaf": leaf, "leaf2": leaf2, "other": other,
            "institution": institution, "channel": channel,
            "owner": owner, "tag": tag}


def _amounts(resp):
    assert resp.status_code == 200, resp.text
    return {p["period"]: Decimal(p["amount"]) for p in resp.json()}


# ---------------------------------------------------------------- 新端点


def test_by_class_expands_descendants(db, seeded):
    client = _client(db)
    # 选中大类＝大类及其全部后代（ADR-0019 保住旧 by-type 的树语义）
    parent = _amounts(client.get("/api/analysis/by-class",
                                 params={"asset_class_id": seeded["root"].id}))
    assert parent["2026-02"] == Decimal("1300")      # 两个子类 1000 + 300
    leaf = _amounts(client.get("/api/analysis/by-class",
                               params={"asset_class_id": seeded["leaf"].id}))
    assert leaf["2026-02"] == Decimal("1000")       # 只算自己（兄弟类不牵连）
    other = _amounts(client.get("/api/analysis/by-class",
                                params={"asset_class_id": seeded["other"].id}))
    assert other["2026-02"] == Decimal("400")


def test_by_liquidity_takes_int_days_and_rejects_names(db, seeded):
    client = _client(db)
    zero = _amounts(client.get("/api/analysis/by-liquidity", params={"days": 0}))
    assert zero["2026-02"] == Decimal("1300")
    thirty = _amounts(client.get("/api/analysis/by-liquidity", params={"days": 30}))
    assert thirty["2026-02"] == Decimal("400")

    # 档位名称/非白名单天数 → 422（绝不静默返回空序列）
    for bad in ("T+0 随时", "2", 12345):
        resp = client.get("/api/analysis/by-liquidity", params={"days": bad})
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_institution_channel_owner_tag_endpoints(db, seeded):
    client = _client(db)
    assert _amounts(client.get("/api/analysis/by-institution",
                               params={"institution_id": seeded["institution"].id})
                    )["2026-02"] == Decimal("1700")
    assert _amounts(client.get("/api/analysis/by-channel",
                               params={"channel_id": seeded["channel"].id})
                    )["2026-02"] == Decimal("1000")
    assert _amounts(client.get("/api/analysis/by-owner",
                               params={"owner_id": seeded["owner"].id})
                    )["2026-02"] == Decimal("1700")
    # 标签是项级多值维度：只算挂了该标签的项
    assert _amounts(client.get("/api/analysis/by-tag",
                               params={"tag_id": seeded["tag"].id})
                    )["2026-02"] == Decimal("1000")


# ---------------------------------------------------------------- 旧端点 410


LEGACY_ENDPOINTS = (
    ("/api/analysis/by-type", {"fund_type_id": 1}, "/api/analysis/by-class"),
    ("/api/analysis/by-liquidity-rating", {"liquidity_rating": "T+0 随时"},
     "/api/analysis/by-liquidity"),
    ("/api/analysis/by-account", {"account_id": 1},
     "/api/analysis/by-institution"),
)


@pytest.mark.parametrize("path,params,use", LEGACY_ENDPOINTS)
def test_legacy_analysis_endpoints_are_gone_with_hint(db, seeded, path, params, use):
    resp = _client(db).get(path, params=params)
    assert resp.status_code == 410, resp.text      # 不是 404：名字永久没了
    err = resp.json()["error"]
    assert err["code"] == "ANALYSIS_DIMENSION_REMOVED"
    assert err["details"]["use"].startswith(use)
    # 值域怎么查也一并指出，否则调用方只能猜
    assert "asset_class" in err["details"]["vocabulary"]


def test_legacy_analysis_endpoints_gone_even_without_params(db):
    """旧路径本身即废弃：不带旧参数也 410，避免「少传参数就好用」的假象。"""
    resp = _client(db).get("/api/analysis/by-type")
    assert resp.status_code == 410


# ---------------------------------------------------------------- 占比维度


def test_proportion_new_dimensions_all_available(db, seeded):
    client = _client(db)
    for dim in ("asset_class", "liquidity", "institution", "channel",
                "asset_name", "owner", "risk_level", "tag"):
        resp = client.get("/api/proportions/by-dimension", params={"dimension": dim})
        assert resp.status_code == 200, (dim, resp.text)
        assert resp.json()["dimension"] == dim
    # `risk` 与 `risk_level` 同指一列（预警/配置目标的写法）
    resp = client.get("/api/proportions/by-dimension", params={"dimension": "risk"})
    assert resp.status_code == 200
    assert resp.json()["dimension"] == "risk_level"


@pytest.mark.parametrize("legacy,replacement", (
    ("fund_type", "asset_class"),
    ("liquidity_rating", "liquidity"),
    ("account", "institution"),
))
def test_proportion_legacy_dimensions_422_with_replacement(db, seeded,
                                                           legacy, replacement):
    resp = _client(db).get("/api/proportions/by-dimension",
                           params={"dimension": legacy})
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["replacement"].startswith(replacement)
    assert "已废弃" in err["message"]


# ---------------------------------------------------------------- 配置维度
# 主名统一为 `risk_level`（`risk` 继续作为同义词接受）：
# 落库只认一个拼写，读侧两种都命中——否则同一个维度会长期有两种落库写法。


def test_allocation_analysis_route_422_envelope_not_400(db, seeded):
    """维度错误走服务层 422 ＋ 全局信封（路由层不再有 `except ValueError` → 400）。"""
    client = _client(db)
    resp = client.get("/api/allocations/analysis",
                      params={"dimension": "fund_type", "snapshot_date": D_FEB.isoformat()})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # 主名与同义名都可用，返回的维度一律是主名
    for name in ("risk_level", "risk"):
        ok = client.get("/api/allocations/analysis",
                        params={"dimension": name, "snapshot_date": D_FEB.isoformat()})
        assert ok.status_code == 200, (name, ok.text)
        assert ok.json()["dimension"] == "risk_level"


def test_allocation_target_write_normalizes_and_reads_both_spellings(db, seeded):
    created = allocation_service.create_allocation_target(
        db, AllocationTargetCreate(dimension="risk", target_id="R3",
                                   target_percent="10"))
    assert created.dimension == "risk_level"          # 写入归一（主名）

    # 存量行：历史上落的是 `risk`，两种入参都要能读到它
    legacy = AllocationTarget(dimension="risk", target_id="R4",
                              target_percent=Decimal("10"))
    db.add(legacy)
    db.commit()
    for name in ("risk_level", "risk"):
        ids = {t.id for t in allocation_service.get_allocation_targets(db, name)}
        assert ids == {created.id, legacy.id}, name

    # 分析按主名输出，两种拼写的目标都命中（不只是「能查出来」）
    risk = allocation_service.analyze_allocation(db, "risk_level", D_FEB)
    assert risk.dimension == "risk_level"
    assert {i.id for i in risk.items if i.target_percent is not None} == {"R3", "R4"}

    # 旧值仍然是 422（不静默返回空列表）
    for gone in ("fund_type", "liquidity_rating", "account"):
        with pytest.raises(ApiError) as ei:
            allocation_service.get_allocation_targets(db, gone)
        assert ei.value.status_code == 422