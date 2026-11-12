"""分类接口契约修复的回归测试（前端对接实测出的问题）。

覆盖：
1. `_auto_code`：删过子类别后仍取「兄弟最大序号 + 1」，不再撞 `UNIQUE(code)` → 500；
   根节点 A-Z 用尽后继续递增探测，探测到上限才 409（不抛 500）；
2. `CLASS_HAS_CHILDREN` / `CLASS_IN_USE` / `DICT_IN_USE` 的 `details` 带数量（前端
   靠这些数字说人话，不再从 message 里抠中文数字）；
3. `classification_missing` 是 `missing_dims` 的兼容别名（同值）；
4. `record_count`（有效记录行数）与实际一致，批量端点一次 GROUP BY（无 N+1）；
5. 写路径（POST/PUT asset-classes）的响应与列表项同形（含 `path`/`leaf`）。

数据一律合成（`示例-` 前缀、虚构机构、100 式金额）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import Asset, AssetClass, AssetItem, AssetItemTag
from routers import classification as classification_router


@pytest.fixture()
def api(tmp_path):
    from main import register_exception_handlers

    path = str(tmp_path / "classification_contract.db")
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
    app.include_router(classification_router.item_router, prefix="/api/asset-items")
    app.include_router(classification_router.router, prefix="/api/classification")

    client = TestClient(app)
    client.Session = Session        # 测试直取 session 造数
    client.engine = engine          # N+1 断言用（挂 SQL 监听）
    return client


# ---------------------------------------------------------------- 造数

def _post_class(api, name, *, parent_id=None, code=None):
    payload = {"name": name}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if code is not None:
        payload["code"] = code
    return api.post("/api/classification/asset-classes", json=payload)


def _mk_item(api, name, **cols) -> int:
    """直插一个 draft 资产项（draft 免四维校验，只填被引用的那一列）。"""
    db = api.Session()
    item = AssetItem(display_name=name, status="draft", **cols)
    db.add(item)
    db.commit()
    item_id = item.id
    db.close()
    return item_id


def _mk_assets(api, item_id, *, active=0, superseded=0) -> None:
    """造记录行：active 条有效（`superseded_by IS NULL`）+ superseded 条被更正的历史行。"""
    db = api.Session()
    rows = [
        Asset(asset_name="示例-项", amount=Decimal("100"), side="asset",
              asset_date=date(2026, 1, 1) + _days(i), asset_item_id=item_id)
        for i in range(active)
    ]
    db.add_all(rows)
    db.flush()
    db.add_all([
        Asset(asset_name="示例-项", amount=Decimal("100"), side="asset",
              asset_date=date(2026, 2, 1) + _days(i), asset_item_id=item_id,
              superseded_by=rows[0].id if rows else None)
        for i in range(superseded)
    ])
    db.commit()
    db.close()


def _days(i: int):
    from datetime import timedelta
    return timedelta(days=i)


def _alpha_labels(count: int) -> list[str]:
    """A、B…Z、AA、AB…（独立实现，避免用被测代码验证被测代码）。"""
    labels = []
    for idx in range(1, count + 1):
        label, rest = "", idx
        while rest > 0:
            rest, rem = divmod(rest - 1, 26)
            label = chr(ord("A") + rem) + label
        labels.append(label)
    return labels


# ---------------------------------------------------------------- 1. 自动编号

def test_auto_code_takes_max_sibling_sequence(api):
    """A5/A6/A7 删掉 A6 后再建 → A8（按「兄弟数量 + 1」会重发 A3，编号也是错的）。"""
    root = _post_class(api, "示例-现金", code="A")
    assert root.status_code == 201, root.text
    parent_id = root.json()["id"]

    made = {}
    for seq in (5, 6, 7):
        resp = _post_class(api, f"示例-子{seq}", parent_id=parent_id, code=f"A{seq}")
        assert resp.status_code == 201, resp.text
        made[seq] = resp.json()
    assert [made[s]["code"] for s in (5, 6, 7)] == ["A5", "A6", "A7"]

    deleted = api.delete(
        f"/api/classification/asset-classes/{made[6]['id']}", params={"reason": "normalization"})
    assert deleted.status_code == 204, deleted.text

    resp = _post_class(api, "示例-新子", parent_id=parent_id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "A8"


def test_auto_code_no_unique_conflict_after_middle_delete(api):
    """原 500 复现：A1..A7 建满、删 A6、再建。

    旧逻辑取「兄弟数 + 1」= 6 + 1 = A7 → 撞 `UNIQUE(code)` → 500 INTERNAL。
    新逻辑取最大序号 + 1 = A8，201。
    """
    root = _post_class(api, "示例-现金", code="A").json()
    made = []
    for idx in range(7):
        resp = _post_class(api, f"示例-子{idx + 1}", parent_id=root["id"])
        assert resp.status_code == 201, resp.text
        made.append(resp.json())
    assert [row["code"] for row in made] == [f"A{i + 1}" for i in range(7)]

    assert api.delete(
        f"/api/classification/asset-classes/{made[5]['id']}",
        params={"reason": "normalization"}).status_code == 204   # A6

    resp = _post_class(api, "示例-新子", parent_id=root["id"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "A8"


def test_auto_code_root_probes_past_z(api):
    """根节点 A-Z 用尽 → 继续递增探测（AA…），不是直接判死。"""
    db = api.Session()
    for label in _alpha_labels(26):
        db.add(AssetClass(code=label, name=f"示例-{label}", parent_id=None,
                          level=1, side_scope="any"))
    db.commit()
    db.close()

    resp = _post_class(api, "示例-新根")
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "AA"


def test_auto_code_exhausted_is_409_not_500(api):
    """探测到上限仍无空位 → 409 + 可读原因（绝不允许 UNIQUE 冲突冒成 500）。"""
    db = api.Session()
    for label in _alpha_labels(1000):          # 与实现同量级的候选全部占满
        db.add(AssetClass(code=label, name=f"示例-{label}", parent_id=None,
                          level=1, side_scope="any"))
    db.commit()
    db.close()

    resp = _post_class(api, "示例-无位")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "CLASS_CODE_EXHAUSTED"
    assert "code" in err["message"] or "编号" in err["message"]
    assert err["details"]["probed"] == 1000


# ---------------------------------------------------------------- 2. details 带数量

def test_delete_class_with_children_reports_details(api):
    root = _post_class(api, "示例-有子", code="A").json()
    _post_class(api, "示例-子", parent_id=root["id"], code="A1")

    resp = api.delete(f"/api/classification/asset-classes/{root['id']}")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "CLASS_HAS_CHILDREN"
    assert err["details"] == {"children": 1}


def test_delete_class_in_use_reports_details(api):
    leaf = _post_class(api, "示例-叶子", code="A1").json()
    _mk_item(api, "示例-项", asset_class_id=leaf["id"])

    resp = api.delete(f"/api/classification/asset-classes/{leaf['id']}")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "CLASS_IN_USE"
    assert err["details"] == {"items": 1, "field": "asset_class_id"}


@pytest.mark.parametrize("path,payload,item_col,field", [
    ("/api/classification/institutions", {"name": "示例银行", "kind": "bank"},
     "institution_id", "institution_id"),
    ("/api/classification/channels", {"name": "示例App"},
     "channel_id", "channel_id"),
    ("/api/classification/members", {"name": "示例成员", "role": "self"},
     "owner_id", "owner_id"),
])
def test_dict_in_use_reports_details(api, path, payload, item_col, field):
    created = api.post(path, json=payload)
    assert created.status_code == 201, created.text
    obj_id = created.json()["id"]
    _mk_item(api, f"示例-项-{field}", **{item_col: obj_id})

    resp = api.delete(f"{path}/{obj_id}")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "DICT_IN_USE"
    assert err["details"] == {"items": 1, "field": field}


def test_delete_tag_in_use_reports_details(api):
    tag = api.post("/api/classification/tags", json={"name": "示例标签"}).json()
    item_id = _mk_item(api, "示例-项")
    db = api.Session()
    db.add(AssetItemTag(item_id=item_id, tag_id=tag["id"]))
    db.commit()
    db.close()

    resp = api.delete(f"/api/classification/tags/{tag['id']}")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "DICT_IN_USE"
    assert err["details"] == {"items": 1, "field": "tag_id"}


# ---------------------------------------------------------------- 3. 字段名别名

def test_classification_missing_is_alias_of_missing_dims(api):
    item_id = _mk_item(api, "示例-draft")
    resp = api.get(f"/api/asset-items/{item_id}/classification")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["missing_dims"] == [
        "asset_class_id", "liquidity_days", "institution_id", "owner_id"]
    assert body["classification_missing"] == body["missing_dims"]

    put = api.put(f"/api/asset-items/{item_id}/classification",
                  json={"risk_level": "R2"})
    assert put.status_code == 200, put.text
    assert put.json()["classification_missing"] == put.json()["missing_dims"]


# ---------------------------------------------------------------- 4. 影响面 record_count

def test_record_count_matches_active_rows(api):
    item_id = _mk_item(api, "示例-项")
    _mk_assets(api, item_id, active=2, superseded=3)

    db = api.Session()
    expected = db.query(Asset).filter(
        Asset.asset_item_id == item_id, Asset.superseded_by.is_(None)).count()
    db.close()
    assert expected == 2                       # 被更正的历史行不计入影响面

    body = api.get(f"/api/asset-items/{item_id}/classification").json()
    assert body["record_count"] == expected


def test_bulk_classifications_record_count_and_no_n_plus_one(api):
    """批量端点：record_count 与单条一致，且记录行数只查一次（无 N+1）。"""
    ids = []
    for n in range(4):
        item_id = _mk_item(api, f"示例-项{n}")
        _mk_assets(api, item_id, active=n, superseded=1 if n else 0)
        ids.append(item_id)

    bulk = api.get("/api/asset-items/classifications?ids="
                   + ",".join(str(i) for i in ids))
    assert bulk.status_code == 200, bulk.text
    items = bulk.json()["items"]
    assert items[str(ids[0])]["record_count"] == 0
    assert items[str(ids[3])]["record_count"] == 3

    db = api.Session()
    for item_id in ids:
        expected = db.query(Asset).filter(
            Asset.asset_item_id == item_id, Asset.superseded_by.is_(None)).count()
        assert items[str(item_id)]["record_count"] == expected
    db.close()

    statements: list[str] = []

    def _spy(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    api.get("/api/asset-items/classifications?ids=" + str(ids[0]))   # 预热连接
    event.listen(api.engine, "before_cursor_execute", _spy)
    try:
        statements.clear()
        api.get("/api/asset-items/classifications?ids="
                + ",".join(str(i) for i in ids[:2]))
        two = len(statements)
        statements.clear()
        api.get("/api/asset-items/classifications?ids="
                + ",".join(str(i) for i in ids))
        four = len(statements)
        count_sql = [s for s in statements if "from assets" in s.lower()]
    finally:
        event.remove(api.engine, "before_cursor_execute", _spy)

    assert four == two                         # 项数翻倍，SQL 条数不变
    assert len(count_sql) == 1                 # 一次 GROUP BY，不是逐项 COUNT
    assert "group by" in count_sql[0].lower()


# ---------------------------------------------------------------- 5. 写路径响应完整性

def test_asset_class_write_response_matches_list_item(api):
    created = _post_class(api, "示例-根", code="A")
    assert created.status_code == 201, created.text
    node = created.json()
    assert set(node) == {"id", "code", "name", "parent_id", "level",
                         "side_scope", "path", "leaf"}
    assert node["path"] == "A 示例-根" and node["leaf"] is True

    listing = api.get("/api/classification/asset-classes").json()["items"]
    assert node == next(row for row in listing if row["id"] == node["id"])

    child = _post_class(api, "示例-子", parent_id=node["id"], code="A1").json()
    assert child["path"] == "A 示例-根/A1 示例-子"
    assert child["leaf"] is True

    updated = api.put(f"/api/classification/asset-classes/{node['id']}",
                      json={"name": "示例-根改"})
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert set(body) == set(node)              # 与列表项 / 创建返回值同形
    assert body["path"] == "A 示例-根改"
    assert body["leaf"] is False               # 有子节点者不可直接挂资产项