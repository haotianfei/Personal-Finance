"""`GET /api/assets` 分类契约与筛选（ADR-0019 记录侧只读分类）。

覆盖：
- 响应契约：新旧字段（旧 EAV 扁平字段移除、项级分类只读回显、classification_missing）；
- 筛选：asset_class_id **含后代**（树展开不退化）、liquidity_days 精确、tag_ids AND、
  institution_id/channel_id/owner_id；
- 废弃参数：attribute_ids / fund_type_id / account_id / liquidity_rating_id / tag_id → 422。

数据一律合成（示例- 前缀、虚构机构、100/50 式金额）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import AssetClass, AssetItem, Channel, Institution, Member, Tag
from routers import assets as assets_router
from services import asset_service, classification_service
from services.asset_service import create_asset

TODAY = date(2026, 3, 1)


@pytest.fixture()
def api(tmp_path):
    from main import register_exception_handlers

    path = str(tmp_path / "class_api.db")
    engine = create_engine(f"sqlite:///{path}",
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
    api = TestClient(app)
    api.Session = Session          # 测试直取 session 造数据
    return api


# ---------------------------------------------------------------- 造数

def _tree(db):
    """两棵树：B 固定收益（B1 货币基金 / B2 债券）与 C 权益（C1 股票）。"""
    fixed = AssetClass(code="B", name="固定收益", parent_id=None, level=1,
                       side_scope="asset")
    db.add(fixed)
    db.flush()
    money = AssetClass(code="B1", name="货币基金", parent_id=fixed.id, level=2,
                       side_scope="asset")
    bond = AssetClass(code="B2", name="债券", parent_id=fixed.id, level=2,
                      side_scope="asset")
    equity = AssetClass(code="C1", name="股票", parent_id=None, level=1,
                        side_scope="asset")
    db.add_all([money, bond, equity])
    db.commit()
    return {"B": fixed.id, "B1": money.id, "B2": bond.id, "C1": equity.id}


def _dicts(db):
    db.add_all([Institution(name="示例银行", kind="bank"),
                Channel(name="示例App"),
                Member(name="本人", role="self"),
                Member(name="配偶", role="spouse")])
    db.commit()
    inst = db.query(Institution).filter_by(name="示例银行").one()
    chan = db.query(Channel).filter_by(name="示例App").one()
    self_ = db.query(Member).filter_by(name="本人").one()
    spouse = db.query(Member).filter_by(name="配偶").one()
    return {"inst": inst.id, "chan": chan.id, "self": self_.id, "spouse": spouse.id}


def _tag(db, name):
    row = Tag(name=name, kind="free")
    db.add(row)
    db.commit()
    return row.id


def _seed(db, name, class_id, dicts, *, liquidity=0, owner=None, amount="100",
          tags=(), channel=True):
    row = create_asset(db, name, Decimal(amount), TODAY)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": class_id,
        "liquidity_days": liquidity,
        "institution_id": dicts["inst"],
        "channel_id": dicts["chan"] if channel else None,
        "owner_id": owner or dicts["self"],
        "risk_level": "R2",
    })
    if tags:
        classification_service.set_tags(db, row.asset_item_id, list(tags))
    return row


@pytest.fixture()
def seeded(api):
    db = api.Session()
    classes = _tree(db)
    dicts = _dicts(db)
    tag_hot = _tag(db, "标签-热门")
    tag_new = _tag(db, "标签-新")
    rows = {
        "money": _seed(db, "示例-货币基金", classes["B1"], dicts,
                       liquidity=0, tags=[tag_hot, tag_new]),
        "bond": _seed(db, "示例-债券", classes["B2"], dicts,
                      liquidity=30, owner=dicts["spouse"], tags=[tag_hot]),
        "equity": _seed(db, "示例-股票", classes["C1"], dicts,
                        liquidity=7, amount="50", channel=False),
    }
    db.close()
    return {**rows, "classes": classes, "dicts": dicts,
            "tags": {"hot": tag_hot, "new": tag_new}}


def _names(payload):
    """命中项名集合（顺序由排序字段决定，与契约无关）。"""
    return {i["asset_name"] for i in payload["items"]}


# ---------------------------------------------------------------- 响应契约

def test_response_contract_fields(api, seeded):
    body = api.get("/api/assets").json()
    row = next(i for i in body["items"] if i["asset_name"] == "示例-货币基金")

    # 契约字段齐备
    for field in ("id", "asset_item_id", "asset_name", "amount", "side", "asset_date",
                  "source_evidence_id", "superseded_by", "created_at", "updated_at",
                  "asset_class_id", "asset_class_name", "asset_class_code",
                  "asset_class_path", "liquidity_days", "liquidity_label",
                  "institution_id", "institution_name", "channel_id", "channel_name",
                  "owner_id", "owner_name", "owner_kind", "risk_level", "tags",
                  "classification_missing"):
        assert field in row, field

    # 旧 EAV 字段全部移除
    for gone in ("fund_type_id", "fund_type_name", "account_id", "account_name",
                 "liquidity_rating_id", "liquidity_rating_name", "attributes",
                 "attribute_ids"):
        assert gone not in row, gone

    assert row["asset_class_name"] == "货币基金"
    assert row["asset_class_code"] == "B1"
    assert row["asset_class_path"] == "B 固定收益/B1 货币基金"   # 复用 list_asset_classes 的 path
    assert row["liquidity_days"] == 0 and row["liquidity_label"] == "T+0 随时"
    assert row["institution_name"] == "示例银行"
    assert row["channel_name"] == "示例App"
    assert row["owner_name"] == "本人" and row["owner_kind"] == "self"
    assert row["risk_level"] == "R2"
    assert {t["name"] for t in row["tags"]} == {"标签-热门", "标签-新"}
    assert row["classification_missing"] == []
    assert row["source_evidence_id"] is None


def test_single_record_endpoint_same_contract(api, seeded):
    rid = api.Session().query(models.Asset).filter_by(
        asset_name="示例-债券").one().id
    row = api.get(f"/api/assets/{rid}").json()
    assert row["asset_class_code"] == "B2"
    assert row["owner_kind"] == "spouse"
    assert row["liquidity_label"] == "30 天"


# ---------------------------------------------------------------- 筛选

def test_asset_class_filter_includes_descendants(api, seeded):
    """只选父节点 B 必须命中 B1/B2 两个叶子下的资产项（树展开不退化）。"""
    body = api.get("/api/assets",
                   params={"asset_class_id": str(seeded["classes"]["B"])}).json()
    assert _names(body) == {"示例-货币基金", "示例-债券"}
    # 选叶子只命中自己
    body = api.get("/api/assets",
                   params={"asset_class_id": str(seeded["classes"]["B1"])}).json()
    assert _names(body) == {"示例-货币基金"}


def test_liquidity_days_exact_match(api, seeded):
    body = api.get("/api/assets", params={"liquidity_days": 7}).json()
    assert _names(body) == {"示例-股票"}
    assert api.get("/api/assets", params={"liquidity_days": 1}).json()["total"] == 0


def test_tag_filter_is_and_semantics(api, seeded):
    hot, new = seeded["tags"]["hot"], seeded["tags"]["new"]
    assert _names(api.get("/api/assets", params={"tag_ids": str(hot)}).json()) == \
        {"示例-货币基金", "示例-债券"}
    assert _names(api.get("/api/assets",
                          params={"tag_ids": f"{hot},{new}"}).json()) == \
        {"示例-货币基金"}        # AND：两个标签都要有


def test_institution_channel_owner_filters(api, seeded):
    d = seeded["dicts"]
    assert api.get("/api/assets", params={"institution_id": str(d["inst"])}
                   ).json()["total"] == 3
    assert _names(api.get("/api/assets", params={"channel_id": str(d["chan"])}
                          ).json()) == {"示例-货币基金", "示例-债券"}
    assert _names(api.get("/api/assets", params={"owner_id": str(d["spouse"])}
                          ).json()) == {"示例-债券"}
    assert _names(api.get("/api/assets", params={"owner_kind": "self"}).json()) == \
        {"示例-股票", "示例-货币基金"}


def test_filters_combine(api, seeded):
    d, c = seeded["dicts"], seeded["classes"]
    body = api.get("/api/assets", params={
        "asset_class_id": str(c["B"]), "owner_id": str(d["self"]),
        "liquidity_days": 0}).json()
    assert _names(body) == {"示例-货币基金"}


# ---------------------------------------------------------------- 废弃参数

@pytest.mark.parametrize("params,field", [
    ({"attribute_ids": "1"}, "attribute_ids"),
    ({"fund_type_id": "1"}, "fund_type_id"),
    ({"account_id": "1"}, "account_id"),
    ({"liquidity_rating_id": "1"}, "liquidity_rating_id"),
    ({"tag_id": "1"}, "tag_id"),
])
def test_deprecated_filters_return_422(api, seeded, params, field):
    resp = api.get("/api/assets", params=params)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["details"]["fields"][0]["field"] == field


def test_attribute_ids_message_names_replacements(api, seeded):
    """任务要求：错误信息里写明替代参数清单。"""
    resp = api.get("/api/assets", params={"attribute_ids": "1"})
    message = resp.json()["error"]["message"]
    for repl in ("asset_class_id", "liquidity_days", "institution_id",
                 "owner_id", "tag_ids"):
        assert repl in message


# ---------------------------------------------------------------- 写入路径拒绝分类

def test_create_record_rejects_classification_fields(api, seeded):
    resp = api.post("/api/assets", json={
        "asset_name": "示例-新项", "amount": "1", "asset_date": "2026-03-02",
        "attribute_ids": [1]})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "attribute_ids"


def test_put_record_rejects_classification_fields(api, seeded):
    rid = api.Session().query(models.Asset).filter_by(
        asset_name="示例-债券").one().id
    resp = api.put(f"/api/assets/{rid}", json={"account_id": 3})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "account_id"


def test_batch_update_rejects_classification_fields(api, seeded):
    rid = api.Session().query(models.Asset).filter_by(
        asset_name="示例-债券").one().id
    resp = api.post("/api/assets/batch-update",
                    json={"ids": [rid], "owner_id": seeded["dicts"]["self"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["fields"][0]["field"] == "owner_id"


def test_create_record_still_works_without_classification(api, seeded):
    before = api.get("/api/assets").json()["total"]
    resp = api.post("/api/assets", json={
        "asset_name": "示例-待分类", "amount": "12.30", "asset_date": "2026-03-02"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["amount"] == "12.30"
    # 新项 draft：分类四维未补齐 → 只读回显为空 + classification_missing 全列
    assert body["classification_missing"] == [
        "asset_class_id", "liquidity_days", "institution_id", "owner_id"]
    assert api.get("/api/assets").json()["total"] == before + 1