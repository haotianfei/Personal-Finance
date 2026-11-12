"""P4a 标签后端测试（ADR-0019 项级标签版）。

标签已从记录级 EAV（`asset_attributes`）上移到资产项（`asset_item_tags`）：
- 端点入参仍是**记录 id**，但作用对象是该项（按 asset_id 反查 asset_item_id）；
- 变更留痕走 `asset_item_attr_history`（dim='tag'），不再落 `audit_log:update_tags`；
- 一次标注覆盖全历史，批量端点按 item 分组，同一项只写一次。

导出列（`export_service` 的 tags 字段）仍读旧记录级属性，属导出车道，不在本文件断言。
数据一律合成（示例- 前缀、虚构机构）。
"""
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import (Asset, AssetClass, AssetItem, AssetItemAttrHistory, AssetItemTag,
                    Institution, Member, Tag)
from conftest import legacy_eav_tables
from routers import assets as assets_router
from services import asset_service, classification_service
from services.asset_service import create_asset
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware

M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
TODAY = date.today()


def _tag(db, name):
    row = db.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
    if row is None:
        row = Tag(name=name, kind="free")
        db.add(row)
        db.commit()
    return row


def _asset(db, name="示例-资产", on_date=TODAY):
    return create_asset(db, name, Decimal("100"), on_date)


def _item_tag_ids(db, item_id):
    return sorted(t.id for t in db.get(AssetItem, item_id).tags)


def _tag_history(db, item_id=None):
    q = select(AssetItemAttrHistory).where(AssetItemAttrHistory.dim == "tag")
    if item_id is not None:
        q = q.where(AssetItemAttrHistory.item_id == item_id)
    return db.execute(q.order_by(AssetItemAttrHistory.id)).scalars().all()


# ---------------------------------------------------------------- 单记录替换


def test_put_tags_replaces_only_tags_and_audits(db):
    tags = [_tag(db, f"标签{i}") for i in range(2)]
    asset = _asset(db)
    result = asset_service.replace_asset_tags(db, asset.id, [tags[0].id, tags[1].id])
    assert result is not None and result[1] is True
    # 作用对象是资产项（标签挂项不挂记录）
    assert _item_tag_ids(db, asset.asset_item_id) == sorted(t.id for t in tags)
    history = _tag_history(db, asset.asset_item_id)
    assert len(history) == 1
    assert history[0].new_value == "标签0,标签1" and history[0].old_value == ""

    # 同值再写：changed=False 且不追加变更史
    again = asset_service.replace_asset_tags(db, asset.id, [tags[1].id, tags[0].id])
    assert again[1] is False
    assert len(_tag_history(db, asset.asset_item_id)) == 1

    # 记录级 EAV 绑定已无处可写（0027 删除两表）
    db.refresh(asset)
    assert legacy_eav_tables(db) == set()


def test_put_tags_limit(db):
    tags = [_tag(db, f"标签{i}") for i in range(6)]
    asset = _asset(db)
    from errors import ApiError
    with pytest.raises(ApiError) as ei:
        asset_service.replace_asset_tags(db, asset.id, [t.id for t in tags])
    assert ei.value.status_code == 422
    assert ei.value.code == "TAG_LIMIT_EXCEEDED"


def test_put_tags_archived_and_not_found(db):
    from errors import ApiError
    asset = _asset(db)
    asset_service.set_item_status(db, asset.asset_item_id, "archived",
                                  reason="normalization")
    tag = _tag(db, "标签")
    with pytest.raises(ApiError) as ei:
        asset_service.replace_asset_tags(db, asset.id, [tag.id])
    assert ei.value.status_code == 409 and ei.value.code == "ITEM_ARCHIVED"
    assert asset_service.replace_asset_tags(db, 999999, [tag.id]) is None


def test_put_tags_unknown_tag_rejected(db):
    """非 tags 字典里的 id（旧 fund_type/account 属性 id）→ 422，不静默丢弃。"""
    from errors import ApiError
    asset = _asset(db)
    with pytest.raises(ApiError) as ei:
        asset_service.replace_asset_tags(db, asset.id, [999999])
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "tag_ids"


# ---------------------------------------------------------------- 批量增删


def test_batch_add_and_remove(db):
    tag_a, tag_b, tag_c = (_tag(db, "标签A"), _tag(db, "标签B"),
                           _tag(db, "标签C"))
    first, second = _asset(db, "示例-甲"), _asset(db, "示例-乙")
    asset_service.replace_asset_tags(db, first.id, [tag_a.id])
    db.commit()

    result = asset_service.batch_tag_records(
        db, [first.id, second.id], [tag_b.id], [tag_a.id])
    assert result == {"updated": 2, "skipped": 0, "failed": []}
    assert _item_tag_ids(db, first.asset_item_id) == [tag_b.id]
    assert _item_tag_ids(db, second.asset_item_id) == [tag_b.id]


def test_batch_same_item_grouped_once(db):
    """同一资产项的多条记录 → 只写一次（按 item 分组），计数仍按记录条数。"""
    tag = _tag(db, "标签A")
    first = _asset(db, "示例-分组", TODAY - timedelta(days=1))
    second = create_asset(db, "示例-分组", Decimal("120"), TODAY, item_id=first.asset_item_id)
    assert second.asset_item_id == first.asset_item_id

    result = asset_service.batch_tag_records(
        db, [first.id, second.id], [tag.id], [])
    assert result == {"updated": 2, "skipped": 0, "failed": []}
    assert _item_tag_ids(db, first.asset_item_id) == [tag.id]
    assert len(_tag_history(db, first.asset_item_id)) == 1      # 只写一次
    assert db.execute(select(func.count()).select_from(AssetItemTag)).scalar() == 1


def test_batch_same_value_skipped(db):
    tag = _tag(db, "标签A")
    asset = _asset(db)
    asset_service.replace_asset_tags(db, asset.id, [tag.id])
    db.commit()
    assert asset_service.batch_tag_records(
        db, [asset.id], [tag.id], [])["skipped"] == 1
    assert len(_tag_history(db, asset.asset_item_id)) == 1


def test_batch_limit_archived_superseded_failures(db):
    tags = [_tag(db, f"标签{i}") for i in range(6)]
    full = _asset(db, "示例-满")
    asset_service.replace_asset_tags(db, full.id, [t.id for t in tags[:5]])
    archived = _asset(db, "示例-归档")
    asset_service.set_item_status(db, archived.asset_item_id, "archived",
                                  reason="normalization")
    old = _asset(db, "示例-更正")
    asset_service.correct_asset(db, old.id, "200",
        reason="correction")
    ok = _asset(db, "示例-正常")
    db.commit()

    result = asset_service.batch_tag_records(
        db, [full.id, archived.id, old.id, 999999, ok.id], [tags[5].id], [])
    codes = {(f["id"], f["code"]) for f in result["failed"]}
    assert result["updated"] == 1
    assert (full.id, "TAG_LIMIT_EXCEEDED") in codes
    assert (archived.id, "ITEM_ARCHIVED") in codes
    assert (old.id, "SUPERSEDED") in codes
    assert (999999, "NOT_FOUND") in codes


# ---------------------------------------------------------------- 筛选


def test_tag_filter_is_item_level_and_and_semantics(db):
    tag = _tag(db, "标签A")
    other = _tag(db, "标签B")
    tagged = _asset(db, "示例-带标签")
    _asset(db, "示例-无标签")
    asset_service.replace_asset_tags(db, tagged.id, [tag.id, other.id])
    db.commit()

    assets, total = asset_service.list_assets(db, tag_ids=[tag.id])
    assert total == 1 and assets[0].id == tagged.id
    # AND 语义：两个标签都要有
    assets2, total2 = asset_service.list_assets(db, tag_ids=[tag.id, other.id])
    assert total2 == 1 and assets2[0].id == tagged.id
    missing = _tag(db, "标签C")
    assert asset_service.list_assets(db, tag_ids=[tag.id, missing.id])[1] == 0
    assert asset_service.list_assets(db)[1] == 2


def test_batch_create_by_asset_item_id_and_name_fallback(db):
    item = AssetItem(display_name="示例-正式名", status="draft")  # ADR-0019
    db.add(item)
    db.commit()
    records = [
        {"asset_name": "随意名", "asset_item_id": item.id,
         "amount": "100", "asset_date": TODAY, "attribute_ids": []},
        {"asset_name": "示例-回退名", "amount": "50",
         "asset_date": TODAY, "attribute_ids": []},
    ]
    assets, skipped = asset_service.batch_create_assets(db, records, "skip")
    assert not skipped and len(assets) == 2
    by_item = next(a for a in assets if a.asset_item_id == item.id)
    assert by_item.asset_name == "示例-正式名"
    fallback = next(a for a in assets if a.asset_item_id != item.id)
    assert fallback.asset_name == "示例-回退名"


def test_batch_create_assets_rejects_attribute_ids(db):
    """记录级分类写入已废弃：批量创建传 attribute_ids → 422。"""
    from errors import ApiError
    records = [{"asset_name": "示例-非法", "amount": "1", "asset_date": TODAY,
                "attribute_ids": [1]}]
    with pytest.raises(ApiError) as ei:
        asset_service.batch_create_assets(db, records, "skip")
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "attribute_ids"


# ---------------------------------------------------------------- HTTP/幂等


@pytest.fixture()
def tags_app(tmp_path):
    path = str(tmp_path / "tags.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0010.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()
    Session = sessionmaker(bind=engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override
    app.include_router(assets_router.router, prefix="/api/assets")
    return {"app": app, "Session": Session}


def test_batch_tags_requires_key_and_replays(tags_app):
    db = tags_app["Session"]()
    tag = _tag(db, "标签A")
    asset = _asset(db)
    asset_id, tag_id, item_id = asset.id, tag.id, asset.asset_item_id
    db.close()
    client = TestClient(tags_app["app"])
    body = {"record_ids": [asset_id], "add_tag_ids": [tag_id]}

    missing = client.post("/api/assets/tags/batch", json=body)
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["fields"][0]["field"] == \
        "Idempotency-Key"

    headers = {"Idempotency-Key": "tags-1"}
    first = client.post("/api/assets/tags/batch", json=body, headers=headers)
    second = client.post("/api/assets/tags/batch", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert first.json()["scope"] == "asset_item"      # 作用对象声明
    db = tags_app["Session"]()
    assert len(_tag_history(db, item_id)) == 1
    db.close()

    reused = client.post("/api/assets/tags/batch",
                         json={"record_ids": [asset_id],
                               "remove_tag_ids": [tag_id]},
                         headers=headers)
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_batch_validation(db):
    from errors import ApiError
    tag = _tag(db, "标签A")
    asset = _asset(db)
    asset_service.replace_asset_tags(db, asset.id, [tag.id])
    db.commit()

    with pytest.raises(ApiError) as ei:
        asset_service.batch_tag_records(db, list(range(1, 202)), [tag.id], [])
    assert ei.value.status_code == 422
    with pytest.raises(ApiError) as ei:
        asset_service.batch_tag_records(db, [asset.id], [999999], [])
    assert ei.value.status_code == 422
    with pytest.raises(ApiError):
        asset_service.batch_tag_records(db, ["x"], [], [])


def test_batch_remove_only(db):
    tag = _tag(db, "标签A")
    asset = _asset(db)
    asset_service.replace_asset_tags(db, asset.id, [tag.id])
    db.commit()
    result = asset_service.batch_tag_records(db, [asset.id], [], [tag.id])
    assert result == {"updated": 1, "skipped": 0, "failed": []}
    assert _item_tag_ids(db, asset.asset_item_id) == []


def test_http_put_tags_and_errors(tags_app):
    db = tags_app["Session"]()
    tag = _tag(db, "标签A")
    tags6 = [_tag(db, f"标签{i}") for i in range(6)]
    asset = _asset(db)
    asset_id, tag_id = asset.id, tag.id
    tag_ids6 = [t.id for t in tags6]
    archived = _asset(db, "示例-归档")
    asset_service.set_item_status(db, archived.asset_item_id, "archived",
                                  reason="normalization")
    archived_id = archived.id
    db.close()

    client = TestClient(tags_app["app"])
    ok = client.put(f"/api/assets/{asset_id}/tags", json={"tag_ids": [tag_id]})
    assert ok.status_code == 200
    body = ok.json()
    assert body["tag_ids"] == [tag_id] and body["changed"] is True
    assert body["scope"] == "asset_item"
    assert [t["name"] for t in body["tags"]] == ["标签A"]
    # 旧 EAV 扁平字段已移除
    assert "fund_type_id" not in body and "attributes" not in body

    too_many = client.put(f"/api/assets/{asset_id}/tags",
                          json={"tag_ids": tag_ids6})
    assert too_many.status_code == 422

    archived_resp = client.put(f"/api/assets/{archived_id}/tags",
                               json={"tag_ids": [tag_id]})
    assert archived_resp.status_code == 409
    assert archived_resp.json()["error"]["code"] == "ITEM_ARCHIVED"
    missing = client.put("/api/assets/999999/tags", json={"tag_ids": [tag_id]})
    assert missing.status_code == 404