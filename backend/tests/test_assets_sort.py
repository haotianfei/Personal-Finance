"""`GET /api/assets` 显式排序的确定性兜底键（spec `records-updated-at` D1–D3）。

覆盖：
1. `sort_field=updated_at&sort_order=desc`：较新组在前、同刻组在后，**两组组内均 id 降序**；
2. `sort_order=asc`：时间升序，但同刻组内**仍** id 降序（D2：兜底方向固定，不随主键翻转）；
3. 25 行同一 `updated_at` + `page_size=10` 逐页取 3 页：id 无重复、无缺失且全局降序；
4. 响应键集合不变（仍含 `updated_at`，无新增/删除字段）。

造数一律合成（`示例-` 前缀、100/50 式金额、固定时刻），不取自真库。
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import Asset
from routers import assets as assets_router
from routers.assets import build_asset_response

TIE_AT = datetime(2026, 3, 1, 10, 0, 0)      # 同刻组（较旧）
NEWER_AT = datetime(2026, 3, 2, 10, 0, 0)    # 较新组（组内同样完全同刻）

# `build_asset_response` 的契约键集合（ADR-0019）。本票只改排序，键集合不得变动。
EXPECTED_KEYS = {
    "id", "asset_item_id", "asset_name", "display_name", "amount", "side",
    "asset_date", "source_evidence_id", "superseded_by", "is_superseded",
    "asset_class_id", "asset_class_name", "asset_class_code", "asset_class_path",
    "liquidity_days", "liquidity_label", "institution_id", "institution_name",
    "channel_id", "channel_name", "owner_id", "owner_name", "owner_kind",
    "risk_level", "tags", "classification_missing", "created_at", "updated_at",
}


@pytest.fixture()
def api(tmp_path):
    """独立临时库 + 仅挂 assets 路由的 TestClient（与 test_assets_classification_api 同款）。"""
    from main import register_exception_handlers

    engine = create_engine(f"sqlite:///{tmp_path / 'sort_api.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = _override
    app.include_router(assets_router.router, prefix="/api/assets")
    client = TestClient(app)
    client.Session = Session          # 测试直取 session 造数据
    return client


def _row(db, name, *, updated_at, amount="100", asset_date=date(2026, 3, 1)):
    """直插一行并提交（显式指定 updated_at，绕过 onupdate 的时钟）。

    行的 `asset_item_id` 留空：唯一索引 `uq_assets_item_date_active` 在 SQLite 里
    把 NULL 视为互不相同，因此可以同日期插多行，无需造资产项。
    """
    row = Asset(asset_name=name, amount=Decimal(amount), side="asset",
                asset_date=asset_date, created_at=updated_at, updated_at=updated_at)
    db.add(row)
    db.commit()
    return row.id


def _ids(payload):
    return [item["id"] for item in payload["items"]]


# ------------------------------------------------------------ 1) desc + 兜底

def test_updated_at_desc_breaks_tie_by_id_desc(api):
    """较新组（组内 id 降序）+ 同刻组（id 降序）。

    故意**先**插较新组（id 小）、后插同刻组（id 大），使期望序列不等于「全表
    id 降序」——只按 updated_at 单键排序的实现无法得到该序列（改前为红）。
    """
    db = api.Session()
    newer = [_row(db, f"示例-较新-{i}", updated_at=NEWER_AT) for i in range(2)]
    tie = [_row(db, f"示例-同刻-{i}", updated_at=TIE_AT) for i in range(3)]
    db.close()

    body = api.get("/api/assets", params={
        "sort_field": "updated_at", "sort_order": "desc", "page_size": 100}).json()

    assert body["total"] == 5
    assert _ids(body) == sorted(newer, reverse=True) + sorted(tie, reverse=True)


# ------------------------------------------------------------ 2) asc 方向

def test_updated_at_asc_keeps_id_desc_within_tie(api):
    """升序：时间升序，但同刻组内仍是 id 降序（D2 兜底方向固定）。"""
    db = api.Session()
    newer = [_row(db, f"示例-较新-{i}", updated_at=NEWER_AT) for i in range(2)]
    tie = [_row(db, f"示例-同刻-{i}", updated_at=TIE_AT) for i in range(3)]
    db.close()

    body = api.get("/api/assets", params={
        "sort_field": "updated_at", "sort_order": "asc", "page_size": 100}).json()

    assert _ids(body) == sorted(tie, reverse=True) + sorted(newer, reverse=True)


# ------------------------------------------------------------ 3) 分页稳定性

def test_updated_at_tie_pagination_has_no_duplicate_or_missing(api):
    """25 行同一 updated_at，page_size=10 翻 3 页：25 个 id 无重无缺、全局降序。"""
    db = api.Session()
    ids = [_row(db, f"示例-同刻-{i:02d}", updated_at=TIE_AT, amount="50")
           for i in range(25)]
    db.close()

    collected = []
    for page in (1, 2, 3):
        body = api.get("/api/assets", params={
            "sort_field": "updated_at", "sort_order": "desc",
            "page": page, "page_size": 10}).json()
        assert body["total"] == 25
        assert body["total_pages"] == 3
        collected.extend(_ids(body))

    assert len(collected) == 25
    assert len(set(collected)) == 25                       # 无重复
    assert set(collected) == set(ids)                      # 无缺失
    assert collected == sorted(ids, reverse=True)          # 全局 id 降序


# ------------------------------------------------------------ 4) 契约未变

def test_response_key_set_unchanged(api):
    """`build_asset_response` 与列表响应仍含 updated_at，键集合无增删。"""
    db = api.Session()
    rid = _row(db, "示例-键集", updated_at=TIE_AT)
    payload = build_asset_response(db, db.get(Asset, rid))
    db.close()

    assert set(payload) == EXPECTED_KEYS
    assert "updated_at" in payload
    assert payload["updated_at"]                 # 非空（format_date 后为 ISO 串）

    body = api.get("/api/assets").json()
    assert set(body["items"][0]) == EXPECTED_KEYS