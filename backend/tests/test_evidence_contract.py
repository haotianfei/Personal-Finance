"""凭证读契约收敛（record-provenance spec D7，票 06）。

单条详情 = 列表项 ∪ `{file_sha256}`（spec §3 第 5 条 + §6「冻结后修订」第 4 条）。
两侧必须出自**同一构造器**，所以这里做逐字段 diff（不是抽样比对）：只要有一侧
另写了一份 dict，diff 就会漏出某个字段的缺/异。

覆盖：

1. 同一 id：单条键集 − 列表项键集 == `{file_sha256}`（唯一加法字段），反向为空集
   （列表项是单条的真子集）；
2. 共有字段逐个 == （含 `source_group` 对象、`used_rows`、`image_meta`、
   `recognition_summary`）；
3. `source_group` 是 `{id, slug, label}` 对象（不是 slug 字符串）；
4. `used_rows` / `image_meta` / `recognition_summary` 三字段在单条里存在；
5. 不存在 id → 404，响应体走既有信封形状，文案「凭证不存在」。

数据一律合成：`示例组甲/乙`、`示例-A.png`、100/200 式金额；不含真实机构名、
真实金额或本机路径。
"""
import json
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import get_db
from errors import ErrorCode
from models import Asset, ScreenshotEvidence, SourceGroup
from services import asset_service

API = "/api/screenshots/evidence"


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def client(db):
    """最小应用：只挂截图路由（装配方式同 test_evidence_manager）。"""
    from main import register_exception_handlers
    from routers import screenshots as shots

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(shots.router, prefix="/api/screenshots")
    return TestClient(app)


@pytest.fixture()
def seeded(db):
    """两条凭证，覆盖「字段有值」与「字段为空」两种形态。

    ① 有引用（`used_rows`=1）+ 有 `image_meta` + 有识别快照；
    ② 另一来源组、无引用、两列 NULL、`original_filename/engine/data_date` 全空。

    `used_rows` 必须非零：0 对 0 的 diff 恒为空，测不出漏字段。
    """
    sg1 = SourceGroup(slug="demo-a", label="示例组甲", asset_prefix="示例甲-")
    sg2 = SourceGroup(slug="demo-b", label="示例组乙", asset_prefix="示例乙-")
    db.add_all([sg1, sg2])
    db.flush()

    meta = {"width": 1080, "height": 2400, "bytes": 219933, "format": "JPEG",
            "exif": {"DateTimeOriginal": "2026:10:30 23:22:13", "Make": "示例厂商"}}
    snapshot = {
        "schema": 1,
        "recognized": {"engine": "ocr", "source": "server",
                       "parser_version": "2026.11-1",
                       "parsed_at": "2026-11-05T16:40:12",
                       "items": [{"name": "示例-A", "amount": "100.00"}],
                       "total": "100.00", "date_text": "2026-10-30",
                       "blocks": [{"text": "示例-A 100.00", "x": 5, "y": 7}],
                       "blocks_dropped": False},
        "adopted": {"engine": "ocr", "item_count": 1, "corrected_rows": 0,
                    "items": [{"name": "示例-A", "amount": "100.00"}]},
    }
    ev1 = ScreenshotEvidence(
        source_group_id=sg1.id, file_path="2026/03/a.png", file_sha256="a" * 64,
        original_filename="示例-A.png", data_date=date(2026, 3, 1), engine="ocr",
        created_at=datetime(2026, 3, 1, 9, 0, 0),
        image_meta=json.dumps(meta, ensure_ascii=False),
        recognition_json=json.dumps(snapshot, ensure_ascii=False),
    )
    ev2 = ScreenshotEvidence(
        source_group_id=sg2.id, file_path="2026/05/c.jpg", file_sha256="c" * 64,
        original_filename=None, data_date=None, engine=None,
        created_at=datetime(2026, 5, 20, 9, 0, 0),
    )
    db.add_all([ev1, ev2])
    db.commit()

    asset = asset_service.create_asset(db, "示例-货币基金", 100, date(2026, 3, 1))
    db.get(Asset, asset.id).source_evidence_id = ev1.id
    db.commit()

    return {"sg1": sg1.id, "sg2": sg2.id, "ev1": ev1.id, "ev2": ev2.id}


def _list_item(client, evidence_id: int) -> dict:
    body = client.get(API).json()
    return next(i for i in body["items"] if i["id"] == evidence_id)


def _detail(client, evidence_id: int) -> dict:
    resp = client.get(f"{API}/{evidence_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 1. 逐字段 diff

@pytest.mark.parametrize("key", ["ev1", "ev2"])
def test_detail_equals_list_item_plus_file_sha(client, seeded, key):
    """单条 = 列表项 ∪ {file_sha256}：键集与每个共有字段值都要对得上。

    两条都跑：一条字段全有值、一条全空——只测有值的那条会漏掉「空值序列化
    两侧写法不同」（如 `None` vs 缺键）这类分叉。
    """
    ev_id = seeded[key]
    item = _list_item(client, ev_id)
    detail = _detail(client, ev_id)

    # 唯一加法字段是 file_sha256；列表项不得有单条没有的字段
    assert set(detail) - set(item) == {"file_sha256"}
    assert set(item) - set(detail) == set()

    for field in item:
        assert detail[field] == item[field], f"{field} 与列表项不一致"

    # 加法字段本身仍是真值（不是空占位）
    assert detail["file_sha256"] == ("a" if key == "ev1" else "c") * 64


# ---------------------------------------------------------------- 2. 来源组形状

def test_detail_source_group_is_object_not_slug(client, seeded):
    """`source_group` 是 `{id, slug, label}` 对象：前端按 `.label` 渲染。"""
    detail = _detail(client, seeded["ev1"])
    assert detail["source_group"] == {"id": seeded["sg1"], "slug": "demo-a",
                                      "label": "示例组甲"}


# ---------------------------------------------------------------- 3. 三个加法字段

def test_detail_carries_used_rows_image_meta_recognition_summary(client, seeded):
    """单条必须带 `used_rows` / `image_meta` / `recognition_summary` 三字段。

    旧的扁平形状缺这三项，详情页/表弹层就只能拿列表项凑——正是 D7 要根除的分叉。
    这里连同「空值不报错」一起钉住：`used_rows` 落 0（不是 None），另两项落 None。
    """
    rich = _detail(client, seeded["ev1"])
    assert rich["used_rows"] == 1
    assert rich["image_meta"]["width"] == 1080
    summary = rich["recognition_summary"]
    assert summary["engine"] == "ocr" and summary["recognized_items"] == 1
    # 快照本体（OCR 块）不回详情，与列表项同一条瘦身口径
    assert "blocks" not in summary and "items" not in summary

    plain = _detail(client, seeded["ev2"])
    assert plain["used_rows"] == 0
    assert plain["image_meta"] is None and plain["recognition_summary"] is None


# ---------------------------------------------------------------- 4. 404 形状

def test_detail_unknown_id_404_envelope(client):
    """不存在 id → 404 + 既有信封形状（不是裸 `{"detail": ...}`）。"""
    resp = client.get(f"{API}/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == str(ErrorCode.NOT_FOUND)
    assert body["error"]["message"] == "凭证不存在"