"""凭证反查筛选 `source_evidence_id`（spec D8 / §3 验收 10，票 05）。

覆盖：
- `asset_service.list_assets(source_evidence_id=)` 命中集合与 `assets` 表直算一致；
- `GET /api/assets?source_evidence_id=` 的 `total` 与 SQL 直算一致，且等于凭证侧
  `used_rows` 的口径（D1/D4：`count(assets) where source_evidence_id = e.id`；
  票 02 的凭证列表端点尚未落地，故此断言直接对同一 SQL 定义比对）；
- 不带该参数时 `total` 与改造前一致（固定 fixture 的基线 4 条 active 行）；
- 非法/不存在 id → 空集，不抛错（不存在的凭证不等于 500）。

数据一律合成（`示例-` 前缀、100/200 式金额、虚构文件名）。
"""
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import Asset, ScreenshotEvidence
from routers import assets as assets_router
from routers.assets import list_assets as list_assets_route
from services import asset_service
from services.screenshot_service import get_source_group, seed_default_source_groups

D = date(2026, 3, 1)
BASELINE_ROWS = 4          # 改造前的固定基线：4 条 active 行


def _seed(session) -> dict:
    """固定数据集：凭证 A 关联 2 行、凭证 B 关联 1 行、1 行无凭证（共 4 行）。"""
    seed_default_source_groups(session)
    sg = get_source_group(session, "bantuou")
    ev_a = ScreenshotEvidence(source_group_id=sg.id, file_path="2026/03/a.png",
                              file_sha256="a" * 64, created_at=datetime.now())
    ev_b = ScreenshotEvidence(source_group_id=sg.id, file_path="2026/03/b.png",
                              file_sha256="b" * 64, created_at=datetime.now())
    session.add_all([ev_a, ev_b])
    session.flush()

    ids = {
        "ev_a": ev_a.id,
        "ev_b": ev_b.id,
        "a1": asset_service.create_asset(session, "示例-凭证A-货币基金", 100, D).id,
        "a2": asset_service.create_asset(session, "示例-凭证A-债券", 200, D).id,
        "b1": asset_service.create_asset(session, "示例-凭证B-股票", 300, D).id,
        "none": asset_service.create_asset(session, "示例-无凭证-黄金", 400, D).id,
    }
    for key, ev_id in (("a1", ev_a.id), ("a2", ev_a.id), ("b1", ev_b.id)):
        session.get(Asset, ids[key]).source_evidence_id = ev_id
    session.commit()
    return ids


@pytest.fixture()
def api(tmp_path):
    """真 HTTP 栈：验证 query 参数确实接到路由（而非只测函数签名）。"""
    from main import register_exception_handlers

    engine = create_engine(f"sqlite:///{tmp_path / 'evidence_backref.db'}",
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
    client.Session = Session        # 测试直取 session 造数据
    return client


def _sql_count(db, ev_id: int, active_only: bool = True) -> int:
    """`assets` 表直算（验收「SQL 直算」那一列）。"""
    sql = "SELECT count(*) FROM assets WHERE source_evidence_id = :e"
    if active_only:
        sql += " AND superseded_by IS NULL"
    return db.execute(text(sql), {"e": ev_id}).scalar()


# ---------------------------------------------------------------- 服务层

def test_filter_set_matches_assets_table_direct_sql(db):
    """命中集合 == SQL 直算；且不夹带其它凭证的行。"""
    ids = _seed(db)
    items, total = asset_service.list_assets(db, source_evidence_id=ids["ev_a"])

    assert total == _sql_count(db, ids["ev_a"]) == 2
    assert {r.id for r in items} == {ids["a1"], ids["a2"]}
    assert all(r.source_evidence_id == ids["ev_a"] for r in items)
    assert ids["b1"] not in {r.id for r in items}
    assert ids["none"] not in {r.id for r in items}


def test_filter_count_equals_used_rows_definition(db):
    """与凭证侧 `used_rows` 同一定义（D1/D4：`count(assets) where source_evidence_id=e.id`）。

    本 fixture 无更正历史行，故「仅 active」与「全行」一致；两者之差只在存在
    superseded 历史行的凭证上出现（见 spec D8 下钻语义：列表默认 active 口径）。
    """
    ids = _seed(db)
    for key in ("ev_a", "ev_b"):
        used_rows = _sql_count(db, ids[key], active_only=False)
        _, total = asset_service.list_assets(db, source_evidence_id=ids[key])
        assert total == used_rows, key


def test_no_param_total_unchanged(db):
    """不带参数（或显式 None）时 total 与改造前一致。"""
    _seed(db)
    _, total_default = asset_service.list_assets(db)
    _, total_explicit_none = asset_service.list_assets(db, source_evidence_id=None)
    baseline = db.execute(text(
        "SELECT count(*) FROM assets WHERE superseded_by IS NULL")).scalar()

    assert total_default == total_explicit_none == baseline == BASELINE_ROWS


def test_unknown_evidence_id_returns_empty(db):
    """不存在的凭证 id → 空集，不抛错（凭证被删后深链不应炸页面）。"""
    _seed(db)
    items, total = asset_service.list_assets(db, source_evidence_id=99999)
    assert items == [] and total == 0


def test_superseded_history_rows_differ_from_used_rows(db):
    """**口径差（已知，待裁决，勿在本票内修）**：`used_rows` 数全部行，下钻列表默认只数 active 行。

    凭证 A 关联的一行被更正后（v5「更正＝追加」）：该凭证 `used_rows` = 3（含历史行），
    而 `GET /api/assets?source_evidence_id=` 的 `total` = 2（默认仅 active 行）。
    这是既有「列表默认仅 active」口径与本筛选组合的必然结果——两侧口径都不属本票范围
    （票 02 的 `used_rows` / 列表 active 默认），故这里只**钉住现状**并在
    `.scratch/evidence-manager/05-report.md` 里报给编排者裁决。
    """
    ids = _seed(db)
    corrected, _ = asset_service.correct_asset(db, ids["a1"], "150",
                                               source_evidence_id=ids["ev_a"],
        reason="correction")

    used_rows = _sql_count(db, ids["ev_a"], active_only=False)
    _, total = asset_service.list_assets(db, source_evidence_id=ids["ev_a"])

    assert used_rows == 3                      # a1(历史) + 更正新行 + a2
    assert total == 2                          # 默认仅 active：新行 + a2
    assert corrected.source_evidence_id == ids["ev_a"]


def test_filter_composes_with_pagination(db):
    """只加筛选，不改分页语义：page_size=1 时分页 total 仍是筛选后的总数。"""
    ids = _seed(db)
    items, total = asset_service.list_assets(db, source_evidence_id=ids["ev_a"],
                                             page=1, page_size=1)
    assert total == 2 and len(items) == 1
    assert items[0].source_evidence_id == ids["ev_a"]


# ---------------------------------------------------------------- 路由层

def test_route_passes_filter_and_keeps_response_fields(db):
    ids = _seed(db)

    out = list_assets_route(page=1, page_size=100, include_superseded=False,
                            source_evidence_id=ids["ev_a"], db=db)
    assert out["total"] == 2
    assert {i["source_evidence_id"] for i in out["items"]} == {ids["ev_a"]}
    # 响应字段不变：`source_evidence_id` 在改造前就已回显
    assert "source_evidence_id" in out["items"][0]

    out_plain = list_assets_route(page=1, page_size=100, include_superseded=False, db=db)
    assert out_plain["total"] == BASELINE_ROWS


def test_api_query_param_filters(api):
    session = api.Session()
    try:
        ids = _seed(session)
    finally:
        session.close()

    resp = api.get("/api/assets", params={"source_evidence_id": ids["ev_b"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [i["source_evidence_id"] for i in body["items"]] == [ids["ev_b"]]

    plain = api.get("/api/assets")
    assert plain.status_code == 200
    assert plain.json()["total"] == BASELINE_ROWS