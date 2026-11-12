"""凭证只读端点 + 删除/清理 + 删组守卫（evidence-manager spec D1/D2/D4/D5/D6）。

覆盖（逐条对应票 02 / 票 03 的验收）：

票 02（只读端点，D1/D2/D4）：

1. 列表分页：`page_size=2` 时 `total_pages` / 切片 / `total` 正确；
2. `usage` 三分：`all == used + unused`，`unused` 条数 == **独立 SQL 直算**的零引用条数；
3. 四类筛选：`source_group_id` / `date_from` / `date_to` / `q`，命中集逐条满足条件；
4. `used_rows` 为**全行**口径：被更正过的引用算 2（历史行 + 现行行），active 只有 1；
5. `used_rows` 不 N+1：列表 SQL 语句数不随条目数变化；
6. 响应字段集合（契约快照，多键少键都算破坏）；
7. 原图 200：`content-type` 为 image/*、`content-length > 0`、`Cache-Control` 与
   `Content-Disposition: inline`；
8. `?download=1` → `Content-Disposition` 以 `attachment` 开头且带原始文件名；
9. 不存在的凭证 id → 404；行在盘空 → 404 `EVIDENCE_FILE_MISSING`；
10. `file_path='../../etc/passwd'` → 400 `EVIDENCE_PATH_INVALID`，且**响应体不含
    该文件内容**（越界拦截必须在读文件之前）。

票 03（删除/批量/删组守卫，D5/D6）：

11. 删未使用：行 + 文件消失、审计行存在且 **`evidence_ref` 为 NULL**（否则悬空 FK）；
12. 删已使用：409 `EVIDENCE_IN_USE`、行与文件都还在、details 无金额；
13. 删不存在 → 404；盘上没文件 → 删除仍成功；越界 file_path → 行删但根外文件不删；
14. `purge-unused` 默认 `dry_run=true`：库与盘零变化、清单 == SQL 直算未使用集合、
    `limit` 截断时 `truncated` + `total` 标注总数；
15. `dry_run=false`：磁盘字节下降 == `freed_bytes`；单条失败进 `failed[]` 其余照删；
16. 删组默认永不静默级联（409 两码）；`purge_unused=true` 才连废图一起删，且
    其他组的凭证行与文件不受影响。

D4 的四路引用里 `audit_log.evidence_ref` 单独钉一条（资产 0 行但审计挂着 → 仍算已使用），
证明判定不是只看 `assets`。

数据一律合成：`示例-A/示例-B` 文件名、`示例组二`、100/200 式金额；不含真实机构名、
真实金额或本机路径。
"""
import json
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from database import get_db
from errors import ApiError
from models import Asset, AuditLog, ScreenshotEvidence, SourceGroup
from services import asset_service, evidence_service
from services.screenshot_service import get_source_group, seed_default_source_groups

API = "/api/screenshots/evidence"

# 合成"图片"字节：只要 content-length > 0 且 content-type 对，内容形状不影响断言
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def evidence_root(tmp_path, monkeypatch):
    """把路由的凭证根指到 tmp（`_evidence_root()` 既有测试替身惯例）。

    路由侧取图时**显式**把该函数的返回值传进服务层，所以这里 patch 路由模块即可
    生效——服务层不 import 路由（会成环）。
    """
    from routers import screenshots as shots

    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(root))
    return root


@pytest.fixture()
def client(db):
    """最小应用：只挂截图路由（装配方式同 test_learned_rules_table）。"""
    from main import register_exception_handlers
    from routers import screenshots as shots

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(shots.router, prefix="/api/screenshots")
    return TestClient(app)


# ---------------------------------------------------------------- 造数

def _write(root, rel: str, data: bytes = PNG_BYTES):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _add_evidence(db, *, group_id, file_path, sha_char, data_date, filename=None,
                  engine=None, created_at=None):
    ev = ScreenshotEvidence(
        source_group_id=group_id, file_path=file_path,
        file_sha256=sha_char * 64, original_filename=filename,
        data_date=data_date, engine=engine,
        created_at=created_at or datetime(2026, 3, 1, 9, 0, 0),
    )
    db.add(ev)
    return ev


def _seed(db, root):
    """固定数据集：2 个来源组 / 3 张凭证 / 1 条被更正过的引用（used_rows=2）。

    凭证 ①：有文件、被 1 个资产引用（该资产后续被更正 → 全行 2 条）；
    凭证 ②：**盘上无文件**（行在盘空的诊断态）、无引用；
    凭证 ③：另一来源组、有文件、无引用。
    """
    seed_default_source_groups(db)
    sg1 = get_source_group(db, "bantuou")
    sg2 = SourceGroup(slug="demo-group", label="示例组二", asset_prefix="示例二-")
    db.add(sg2)
    db.flush()

    _write(root, "2026/03/a.png")
    _write(root, "2026/05/c.jpg")
    ev1 = _add_evidence(db, group_id=sg1.id, file_path="2026/03/a.png",
                        sha_char="a", data_date=date(2026, 3, 1),
                        filename="示例-A.png", engine="ocr",
                        created_at=datetime(2026, 3, 1, 9, 0, 0))
    ev2 = _add_evidence(db, group_id=sg1.id, file_path="2026/04/missing.png",
                        sha_char="b", data_date=date(2026, 4, 10),
                        filename=None, engine=None,
                        created_at=datetime(2026, 4, 10, 9, 0, 0))
    ev3 = _add_evidence(db, group_id=sg2.id, file_path="2026/05/c.jpg",
                        sha_char="c", data_date=date(2026, 5, 20),
                        filename="示例-C.jpg", engine="ai",
                        created_at=datetime(2026, 5, 20, 9, 0, 0))
    db.commit()

    asset = asset_service.create_asset(db, "示例-货币基金", 100, date(2026, 3, 1))
    db.get(Asset, asset.id).source_evidence_id = ev1.id
    db.commit()
    # 更正＝追加（v5）：旧行留 source_evidence_id 并打 superseded_by，新行继承同一凭证
    asset_service.correct_asset(db, asset.id, "150",
        reason="correction")

    return {"sg1": sg1.id, "sg2": sg2.id,
            "ev1": ev1.id, "ev2": ev2.id, "ev3": ev3.id, "asset": asset.id}


def _zero_ref_count(db) -> int:
    """独立 SQL 直算的「零引用」条数（不复用被测代码，逐字对齐 D4 四处引用）。"""
    return db.execute(text("""
        SELECT count(*) FROM screenshot_evidence e
        WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.source_evidence_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM audit_log au WHERE au.evidence_ref = e.id)
          AND NOT EXISTS (SELECT 1 FROM principal_changes p WHERE p.evidence_ref = e.id)
          AND NOT EXISTS (SELECT 1 FROM transfer_events t WHERE t.evidence_ref = e.id)
    """)).scalar()


def _rows_for(db, ev_id: int, *, active_only: bool) -> int:
    sql = "SELECT count(*) FROM assets WHERE source_evidence_id = :e"
    if active_only:
        sql += " AND superseded_by IS NULL"
    return db.execute(text(sql), {"e": ev_id}).scalar()


# ================================================================ 1. 列表 / 分页

def test_list_shape_pagination_and_ordering(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    body = client.get(API).json()
    assert body["total"] == 3
    assert (body["page"], body["page_size"], body["total_pages"]) == (1, 100, 1)
    # id DESC：新的在前
    assert [i["id"] for i in body["items"]] == [ids["ev3"], ids["ev2"], ids["ev1"]]

    page1 = client.get(API, params={"page_size": 2, "page": 1}).json()
    page2 = client.get(API, params={"page_size": 2, "page": 2}).json()
    assert page1["total"] == page2["total"] == 3
    assert page1["total_pages"] == page2["total_pages"] == 2
    assert [i["id"] for i in page1["items"]] == [ids["ev3"], ids["ev2"]]
    assert [i["id"] for i in page2["items"]] == [ids["ev1"]]
    # total 是筛选后的总数，不是当前页条数
    assert page2["page_size"] == 2 and len(page2["items"]) == 1


def test_list_response_fields_are_contract(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    item = next(i for i in client.get(API).json()["items"] if i["id"] == ids["ev1"])
    assert set(item) == {
        "id", "source_group", "file_path", "original_filename",
        "data_date", "engine", "created_at", "used_rows",
        # evidence-naming 加法字段（ADR-0022 / ADR-0023）：未回填/未识别过 → null
        "image_meta", "recognition_summary"}
    assert item["image_meta"] is None and item["recognition_summary"] is None
    assert set(item["source_group"]) == {"id", "slug", "label"}
    assert item["source_group"] == {"id": ids["sg1"], "slug": "bantuou",
                                    "label": "支付宝"}
    assert item["data_date"] == "2026-03-01"
    assert item["engine"] == "ocr"
    assert item["original_filename"] == "示例-A.png"

    # 未富化的行：原始文件名与引擎可为 null，不报错
    plain = next(i for i in client.get(API).json()["items"] if i["id"] == ids["ev2"])
    assert plain["original_filename"] is None and plain["engine"] is None


def test_list_serializes_image_meta_and_recognition_summary(db, client, evidence_root):
    """evidence-naming 加法字段（ADR-0022 / ADR-0023）：`image_meta` 反序列化成对象，
    `recognition_summary` 只给摘要——完整快照（OCR 原始块 + 全部识别行）不回列表，
    否则 100 条一页能把 1 MiB/条的 JSON 全搬到响应里。坏 JSON 退化为 null，不 500。
    """
    ids = _seed(db, evidence_root)
    meta = {"width": 1080, "height": 2400, "bytes": 219933, "format": "JPEG",
            "exif": {"DateTimeOriginal": "2026:10:30 23:22:13", "Make": "示例厂商"}}
    snapshot = {
        "schema": 1,
        "recognized": {"engine": "ocr", "source": "server",
                       "parser_version": "2026.11-1",
                       "parsed_at": "2026-11-05T16:40:12",
                       "items": [{"name": "示例-A", "amount": "100.00"},
                                 {"name": "示例-B", "amount": "200.00"}],
                       "total": "300.00", "date_text": "2026-10-30",
                       "blocks": [{"text": "示例-A 100.00", "x": 5, "y": 7}],
                       "blocks_dropped": False},
        "adopted": {"engine": "ocr", "item_count": 2, "corrected_rows": 1,
                    "items": [{"name": "示例-A", "amount": "100.00"}]},
    }
    row = db.get(ScreenshotEvidence, ids["ev1"])
    row.image_meta = json.dumps(meta, ensure_ascii=False)
    row.recognition_json = json.dumps(snapshot, ensure_ascii=False)
    broken = db.get(ScreenshotEvidence, ids["ev2"])
    broken.image_meta = "{not json"
    broken.recognition_json = "[1, 2, 3]"      # 合法 JSON 但不是对象
    db.commit()

    items = {i["id"]: i for i in client.get(API).json()["items"]}
    assert items[ids["ev1"]]["image_meta"] == meta
    summary = items[ids["ev1"]]["recognition_summary"]
    assert summary == {
        "schema": 1, "engine": "ocr", "source": "server",
        "parser_version": "2026.11-1", "parsed_at": "2026-11-05T16:40:12",
        "blocks_dropped": False, "recognized_items": 2,
        "adopted_items": 1, "corrected_rows": 1}
    assert "blocks" not in summary and "items" not in summary   # 快照本体不回列表

    assert items[ids["ev2"]]["image_meta"] is None
    assert items[ids["ev2"]]["recognition_summary"] is None


def test_invalid_usage_is_422_not_silently_all(db, client, evidence_root):
    _seed(db, evidence_root)

    resp = client.get(API, params={"usage": "bogus"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ================================================================ 2. usage 三分

def test_usage_partition_matches_direct_sql(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    all_body = client.get(API, params={"usage": "all"}).json()
    used = client.get(API, params={"usage": "used"}).json()
    unused = client.get(API, params={"usage": "unused"}).json()

    assert used["total"] + unused["total"] == all_body["total"] == 3
    assert [i["id"] for i in used["items"]] == [ids["ev1"]]
    assert {i["id"] for i in unused["items"]} == {ids["ev2"], ids["ev3"]}
    # 验收原文：「usage=unused 条数 == SQL 直算的零引用条数」
    assert unused["total"] == _zero_ref_count(db) == 2


def test_usage_used_covers_non_asset_references(db, client, evidence_root):
    """审计/本金/转移三处引用也算「已使用」（spec D4：四处任一非空）。

    凭证 ③ 没有任何资产行（`used_rows` 仍为 0），但挂了一条审计引用——
    判定若不看 `audit_log.evidence_ref`，这张图就会被当成垃圾清掉。
    """
    ids = _seed(db, evidence_root)
    db.add(AuditLog(occurred_at=datetime(2026, 5, 21, 10, 0, 0).isoformat(),
                    actor="owner", entity="asset", action="create",
                    evidence_ref=ids["ev3"]))
    db.commit()

    used = client.get(API, params={"usage": "used"}).json()
    unused = client.get(API, params={"usage": "unused"}).json()
    assert {i["id"] for i in used["items"]} == {ids["ev1"], ids["ev3"]}
    assert [i["id"] for i in unused["items"]] == [ids["ev2"]]
    assert unused["total"] == _zero_ref_count(db) == 1
    # 被审计引用的凭证可以是「已使用但零资产行」——两个口径本就不同，不许合并
    ev3 = next(i for i in used["items"] if i["id"] == ids["ev3"])
    assert ev3["used_rows"] == 0


# ================================================================ 3. 四类筛选

def test_filter_by_source_group(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    body = client.get(API, params={"source_group_id": ids["sg2"]}).json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [ids["ev3"]]
    assert all(i["source_group"]["id"] == ids["sg2"] for i in body["items"])

    # 不存在的组 → 空集，不报错
    assert client.get(API, params={"source_group_id": 99999}).json()["total"] == 0


def test_filter_by_date_range(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    both = client.get(API, params={"date_from": "2026-04-01",
                                   "date_to": "2026-05-31"}).json()
    assert {i["id"] for i in both["items"]} == {ids["ev2"], ids["ev3"]}
    assert all("2026-04-01" <= i["data_date"] <= "2026-05-31" for i in both["items"])

    only_march = client.get(API, params={"date_to": "2026-03-01"}).json()
    assert [i["id"] for i in only_march["items"]] == [ids["ev1"]]

    only_may = client.get(API, params={"date_from": "2026-05-01"}).json()
    assert [i["id"] for i in only_may["items"]] == [ids["ev3"]]

    assert client.get(API, params={"date_from": "2027-01-01"}).json()["total"] == 0


def test_filter_by_q_matches_filename_and_path(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    by_name = client.get(API, params={"q": "示例-A"}).json()
    assert [i["id"] for i in by_name["items"]] == [ids["ev1"]]

    by_path = client.get(API, params={"q": "2026/05"}).json()
    assert [i["id"] for i in by_path["items"]] == [ids["ev3"]]

    assert client.get(API, params={"q": "查无此名"}).json()["total"] == 0
    # 空/空白 q 视同不筛选（不因前端清空输入而空集）
    assert client.get(API, params={"q": "  "}).json()["total"] == 3


# ================================================================ 4. used_rows 口径

def test_used_rows_counts_superseded_history_rows(db, client, evidence_root):
    """全行口径：被更正过的引用算 2（历史行 + 现行行），active 只有 1。

    spec D1/D8：下钻链接固定带 `view=ledger`（含 superseded），两边条数必须对齐。
    """
    ids = _seed(db, evidence_root)

    assert _rows_for(db, ids["ev1"], active_only=False) == 2
    assert _rows_for(db, ids["ev1"], active_only=True) == 1

    items, total = evidence_service.list_evidence(db)
    by_id = {i["id"]: i for i in items}
    assert total == 3
    assert by_id[ids["ev1"]]["used_rows"] == 2          # 不是 1
    assert by_id[ids["ev2"]]["used_rows"] == 0
    assert by_id[ids["ev3"]]["used_rows"] == 0

    # HTTP 层同一口径
    body = client.get(API, params={"usage": "used"}).json()
    assert body["items"][0]["used_rows"] == 2


# ================================================================ 5. 不 N+1

def _count_statements(db, fn):
    """统计 fn() 期间真正发到数据库的 SQL 语句（before_cursor_execute）。"""
    engine = db.get_bind()
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _before)
    return seen


def test_used_rows_statement_count_does_not_grow_with_items(db, evidence_root):
    """列表 SQL 条数固定为 2（count + 明细），与条目数无关——`used_rows` 走子查询。"""
    _seed(db, evidence_root)

    one = _count_statements(db, lambda: evidence_service.list_evidence(db, page_size=1))
    three = _count_statements(db, lambda: evidence_service.list_evidence(db, page_size=3))

    assert len(one) == len(three) == 2
    assert all("screenshot_evidence" in sql for sql in three)


# ================================================================ 6. 原图

def test_image_ok_headers_and_bytes(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    resp = client.get(f"{API}/{ids['ev1']}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert int(resp.headers["content-length"]) > 0
    assert resp.content == PNG_BYTES
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert resp.headers["content-disposition"].startswith("inline")


def test_image_media_type_follows_extension(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    resp = client.get(f"{API}/{ids['ev3']}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"   # .jpg → image/jpeg


def test_image_download_disposition_is_attachment(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    resp = client.get(f"{API}/{ids['ev1']}/image", params={"download": 1})
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    # ASCII 兜底名（非 ASCII 字符逐个替换为 _）+ RFC 5987 的原始中文名
    assert 'filename="__-A.png"' in disposition
    assert "filename*=UTF-8''%E7%A4%BA%E4%BE%8B-A.png" in disposition  # 示例-A.png

    # 默认（download=0）仍是 inline
    plain = client.get(f"{API}/{ids['ev1']}/image", params={"download": 0})
    assert plain.headers["content-disposition"].startswith("inline")


def test_image_unknown_evidence_404(db, client, evidence_root):
    _seed(db, evidence_root)

    resp = client.get(f"{API}/99999/image")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "凭证不存在: 99999"


def test_image_row_without_file_404_file_missing(db, client, evidence_root):
    ids = _seed(db, evidence_root)          # 凭证 ② 是故意不落盘的

    resp = client.get(f"{API}/{ids['ev2']}/image")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EVIDENCE_FILE_MISSING"
    assert resp.json()["error"]["details"]["evidence_id"] == ids["ev2"]


def test_image_path_traversal_blocked_before_read(db, client, evidence_root):
    """`../../etc/passwd` 必须 400 且**不返回该文件内容**（拦截在读文件之前）。"""
    ids = _seed(db, evidence_root)
    ev = _add_evidence(db, group_id=ids["sg1"], file_path="../../etc/passwd",
                       sha_char="d", data_date=date(2026, 6, 1))
    db.commit()

    resp = client.get(f"{API}/{ev.id}/image")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EVIDENCE_PATH_INVALID"
    assert b"root:" not in resp.content
    assert b"/bin/" not in resp.content

    # 绝对路径同样被拦（join 时绝对路径会覆盖 root，等价穿越）
    ev2 = _add_evidence(db, group_id=ids["sg1"], file_path="/etc/passwd",
                        sha_char="e", data_date=date(2026, 6, 2))
    db.commit()
    resp2 = client.get(f"{API}/{ev2.id}/image")
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "EVIDENCE_PATH_INVALID"


def test_image_symlink_escape_blocked(db, client, evidence_root, tmp_path):
    """符号链接逃逸（realpath 而非 abspath 的理由）：链在根内、指向根外。"""
    ids = _seed(db, evidence_root)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    link = evidence_root / "link.png"
    link.symlink_to(outside)

    ev = _add_evidence(db, group_id=ids["sg1"], file_path="link.png",
                       sha_char="f", data_date=date(2026, 6, 3))
    db.commit()

    resp = client.get(f"{API}/{ev.id}/image")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EVIDENCE_PATH_INVALID"


# ================================================================ 7. 服务层直连

def test_service_resolve_image_path_returns_abs_path(db, evidence_root):
    ids = _seed(db, evidence_root)

    path = evidence_service.resolve_image_path(
        db, ids["ev1"], evidence_root=str(evidence_root))
    assert path == str((evidence_root / "2026/03/a.png").resolve())

    # 缺省 root 时用仓库内默认路径：本 fixture 的文件不在那儿 → 404 而非越界
    ev = _add_evidence(db, group_id=ids["sg1"],
                       file_path="2026/03/__no_such_file__.png",
                       sha_char="g", data_date=date(2026, 6, 4))
    db.commit()
    with pytest.raises(ApiError) as err:
        evidence_service.resolve_image_path(db, ev.id)
    assert err.value.code == "EVIDENCE_FILE_MISSING"


def test_route_declaration_order_before_int_param_route():
    """`/evidence` 与 `/evidence/{id}/image` 必须声明在 `/evidence/{evidence_id}` 之前。

    路由表顺序是 FastAPI 匹配顺序：静态段被 int 参数先匹配走 → 404/422。
    票 03 的两条写路由同样受这条约束（`/evidence/purge-unused` 更要排在前面）。
    """
    from routers import screenshots as shots

    paths = [r.path for r in shots.router.routes
             if getattr(r, "path", "").startswith("/evidence")]
    assert paths.index("/evidence") < paths.index("/evidence/{evidence_id}")
    assert paths.index("/evidence/{evidence_id}/image") < \
        paths.index("/evidence/{evidence_id}")
    # 票 03：静态段 purge-unused 与 DELETE 单条都必须在 GET 单条之前
    assert paths.index("/evidence/purge-unused") < \
        paths.index("/evidence/{evidence_id}")
    # 既有孤儿端点保持原位（本票不动）
    assert "/evidence/orphans/list" in paths


# ================================================================ 8. 删单条（D5）

def test_delete_unused_removes_row_file_and_writes_audit(db, client, evidence_root):
    """删未使用：行消失、磁盘文件消失、审计行存在且 **evidence_ref 为 NULL**。

    成功响应必须是 **204 + 空 body**（前端 `request<T>()` 对 204 直接 `undefined`，
    「200 + 空 body」会让它 `res.json()` 解析失败并误报删除失败）。
    """
    ids = _seed(db, evidence_root)
    target = evidence_root / "2026/05/c.jpg"

    resp = client.delete(f"{API}/{ids['ev3']}",
                         params={"reason": "normalization",
                                 "reason_note": "示例：清理重复截图"})
    assert resp.status_code == 204
    assert resp.content == b""                                 # 不是 200+空体，是 204 无内容

    assert db.get(ScreenshotEvidence, ids["ev3"]) is None      # 行没了
    assert not target.exists()                                 # 文件没了

    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "screenshot_evidence",
        AuditLog.action == "delete")).scalars().all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.row_id == ids["ev3"]
    # 关键：被操作的凭证行随后被删，审计若指回去就是悬空外键（FK ON 时直接删不掉）
    assert audit.evidence_ref is None
    assert audit.after_json is None                             # 删除＝after NULL
    before = json.loads(audit.before_json)
    assert before == {"file_sha256": "c" * 64, "file_path": "2026/05/c.jpg",
                      "source_group_id": ids["sg2"]}
    assert "amount" not in json.dumps(before).lower()           # 不夹带业务金额


def test_delete_used_evidence_409_keeps_row_and_file(db, client, evidence_root):
    ids = _seed(db, evidence_root)              # ev1 已被 2 行（含更正历史）引用

    resp = client.delete(f"{API}/{ids['ev1']}",
                         params={"reason": "normalization"})
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "EVIDENCE_IN_USE"
    # details 只给引用计数与首条引用类型——**不含任何金额**
    assert set(err["details"]) == {"evidence_id", "ref_counts", "first_ref_kind"}
    # 2 个资产行（现行 + 被更正的历史行）+ 1 条更正审计（asset_service 把
    # source_evidence_id 带进了审计行）；后两者为 0 也照样列出，缺项即契约破坏
    assert err["details"]["ref_counts"] == {"assets": 2, "audit_log": 1,
                                           "principal_changes": 0,
                                           "transfer_events": 0}
    assert err["details"]["first_ref_kind"] == "assets"

    assert db.get(ScreenshotEvidence, ids["ev1"]) is not None   # 行还在
    assert (evidence_root / "2026/03/a.png").exists()           # 文件还在
    assert db.execute(select(func.count(AuditLog.id)).where(
        AuditLog.entity == "screenshot_evidence")).scalar() == 0


def test_delete_blocked_by_non_asset_reference(db, client, evidence_root):
    """审计引用也算已使用（D4 四处）：只有 assets 之外一处引用也照样 409。"""
    ids = _seed(db, evidence_root)
    db.add(AuditLog(occurred_at=datetime(2026, 5, 21, 10, 0, 0).isoformat(),
                    actor="owner", entity="asset", action="create",
                    evidence_ref=ids["ev3"]))
    db.commit()

    resp = client.delete(f"{API}/{ids['ev3']}",
                         params={"reason": "normalization",
                                 "reason_note": "示例：清理重复截图"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVIDENCE_IN_USE"
    assert resp.json()["error"]["details"]["first_ref_kind"] == "audit_log"
    assert db.get(ScreenshotEvidence, ids["ev3"]) is not None
    assert (evidence_root / "2026/05/c.jpg").exists()


def test_delete_missing_evidence_404(db, client, evidence_root):
    _seed(db, evidence_root)

    resp = client.delete(f"{API}/99999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_delete_row_without_file_succeeds(db, client, evidence_root):
    """盘上本来就没文件（凭证 ②）：删除仍算成功（204），只是没释放字节。"""
    ids = _seed(db, evidence_root)

    resp = client.delete(f"{API}/{ids['ev2']}",
                         params={"reason": "normalization"})
    assert resp.status_code == 204 and resp.content == b""
    assert db.get(ScreenshotEvidence, ids["ev2"]) is None


def test_delete_service_reports_freed_bytes_and_file_flag(db, evidence_root):
    """HTTP 层 204 无 body，诊断信息（释放字节 / 是否真删到文件）由服务层返回值承载。"""
    ids = _seed(db, evidence_root)

    result = evidence_service.delete_evidence(
        db, ids["ev3"], evidence_root=str(evidence_root),
        reason="normalization")
    assert result == {"ok": True, "id": ids["ev3"],
                      "freed_bytes": len(PNG_BYTES), "file_deleted": True}

    # 盘空的那条：删除仍成功，只是 file_deleted=False、freed_bytes=0
    missing = evidence_service.delete_evidence(
        db, ids["ev2"], evidence_root=str(evidence_root),
        reason="normalization")
    assert missing["file_deleted"] is False and missing["freed_bytes"] == 0


def test_delete_never_removes_file_outside_root(db, client, evidence_root):
    """脏 file_path 越界：行照删（已判为垃圾），但绝不删根外文件。"""
    ids = _seed(db, evidence_root)
    outside = evidence_root.parent / "outside.png"     # ../outside.png 解析到根外
    outside.write_bytes(PNG_BYTES)
    ev = _add_evidence(db, group_id=ids["sg1"], file_path="../outside.png",
                       sha_char="h", data_date=date(2026, 6, 5))
    db.commit()

    resp = client.delete(f"{API}/{ev.id}",
                         params={"reason": "normalization"})
    assert resp.status_code == 204 and resp.content == b""
    assert outside.exists()                            # 根外文件毫发无伤
    assert db.get(ScreenshotEvidence, ev.id) is None


# ================================================================ 9. 批量清理（D5）

def _unused_ids_direct_sql(db) -> set[int]:
    """独立 SQL 直算的未使用 id 集合（不复用被测代码，逐字对齐 D4 四处引用）。"""
    return set(db.execute(text("""
        SELECT e.id FROM screenshot_evidence e
        WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.source_evidence_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM audit_log au WHERE au.evidence_ref = e.id)
          AND NOT EXISTS (SELECT 1 FROM principal_changes p WHERE p.evidence_ref = e.id)
          AND NOT EXISTS (SELECT 1 FROM transfer_events t WHERE t.evidence_ref = e.id)
    """)).scalars().all())


def _disk_bytes(root) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _snapshot(db, root) -> dict:
    return {
        "rows": db.execute(select(func.count(ScreenshotEvidence.id))).scalar(),
        "files": sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()),
        "audit": db.execute(select(func.count(AuditLog.id))).scalar(),
        "bytes": _disk_bytes(root),
    }


PURGE = "/api/screenshots/evidence/purge-unused"


def test_purge_dry_run_is_zero_side_effect(db, client, evidence_root):
    """默认 `dry_run=true`：库与盘零变化，清单 == SQL 直算的未使用集合。"""
    ids = _seed(db, evidence_root)
    before = _snapshot(db, evidence_root)

    resp = client.post(PURGE, json={})           # 空 body → 全默认（dry_run=true）
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["deleted"] == 0 and body["failed"] == []
    assert {i["id"] for i in body["items"]} == _unused_ids_direct_sql(db) \
        == {ids["ev2"], ids["ev3"]}
    assert body["total"] == 2 and body["truncated"] is False and body["limit"] == 200
    # 文件字节按磁盘实际大小求和（凭证 ② 盘上没文件 → 0）
    assert body["freed_bytes"] == len(PNG_BYTES)
    assert [i["file_bytes"] for i in body["items"]] == [0, len(PNG_BYTES)]

    assert _snapshot(db, evidence_root) == before


def test_purge_limit_truncates_and_reports_total(db, client, evidence_root):
    _seed(db, evidence_root)
    before = _snapshot(db, evidence_root)

    body = client.post(PURGE, json={"limit": 1}).json()
    assert len(body["items"]) == 1
    assert body["total"] == 2 and body["truncated"] is True
    assert _snapshot(db, evidence_root) == before


def test_purge_actual_deletes_only_unused_and_freed_matches_disk(
        db, client, evidence_root):
    """`dry_run=false`：磁盘字节下降 == `freed_bytes`；已使用凭证零变化。"""
    ids = _seed(db, evidence_root)
    disk_before = _disk_bytes(evidence_root)

    body = client.post(PURGE, json={"dry_run": False},
                       params={"reason": "normalization"}).json()
    assert body["dry_run"] is False
    assert body["deleted"] == 2 and body["failed"] == []
    assert body["freed_bytes"] == len(PNG_BYTES)
    assert _disk_bytes(evidence_root) == disk_before - body["freed_bytes"]

    assert db.get(ScreenshotEvidence, ids["ev1"]) is not None     # 已使用：不动
    assert (evidence_root / "2026/03/a.png").exists()
    assert db.get(ScreenshotEvidence, ids["ev2"]) is None
    assert db.get(ScreenshotEvidence, ids["ev3"]) is None
    assert not (evidence_root / "2026/05/c.jpg").exists()
    # 每条删除各留一条审计（与单条删除同源），且 evidence_ref 均留空
    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "screenshot_evidence",
        AuditLog.action == "delete")).scalars().all()
    assert len(audits) == 2
    assert all(a.evidence_ref is None for a in audits)


def test_purge_isolates_per_item_failures(db, client, evidence_root, monkeypatch):
    """一条失败不拖累其余：失败的进 `failed[]`，其行与文件原样保留。"""
    ids = _seed(db, evidence_root)
    original = evidence_service._audit_evidence_delete

    def _flaky(session, ev, **kwargs):      # spec E3：purge 每条都透传 reason
        if ev.id == ids["ev3"]:
            raise RuntimeError("模拟单条故障")
        return original(session, ev, **kwargs)

    monkeypatch.setattr(evidence_service, "_audit_evidence_delete", _flaky)

    body = client.post(PURGE, json={"dry_run": False},
                       params={"reason": "normalization"}).json()
    assert body["deleted"] == 1
    assert [f["id"] for f in body["failed"]] == [ids["ev3"]]
    assert "模拟单条故障" in body["failed"][0]["reason"]
    assert body["freed_bytes"] == 0                 # 凭证 ② 没文件可删
    assert db.get(ScreenshotEvidence, ids["ev2"]) is None            # 其余照删
    assert db.get(ScreenshotEvidence, ids["ev3"]) is not None        # 失败项原样保留
    assert (evidence_root / "2026/05/c.jpg").exists()


def test_purge_scoped_to_source_group(db, client, evidence_root):
    ids = _seed(db, evidence_root)

    body = client.post(PURGE, json={"source_group_id": ids["sg2"]}).json()
    assert [i["id"] for i in body["items"]] == [ids["ev3"]]
    assert body["total"] == 1
    # 其它组的未使用凭证不在本次范围内（连清单里都不该出现）
    assert ids["ev2"] not in {i["id"] for i in body["items"]}


def test_purge_accepts_idempotency_key_and_is_naturally_idempotent(
        db, client, evidence_root):
    """带 `Idempotency-Key` 可用；第二次同键调用不会再删到东西（天然幂等）。

    回放（同键同体返回首次响应字节）由 `IdempotencyCaptureMiddleware` 完成，
    本测试的最小应用没装中间件，故只验「guard 不挡路 + 真幂等」。
    """
    _seed(db, evidence_root)
    headers = {"Idempotency-Key": "purge-demo-key"}

    first = client.post(PURGE, json={"dry_run": False}, headers=headers,
                        params={"reason": "normalization"})
    second = client.post(PURGE, json={"dry_run": False}, headers=headers,
                         params={"reason": "normalization"})
    assert first.status_code == second.status_code == 200
    assert first.json()["deleted"] == 2
    assert second.json()["deleted"] == 0            # 已经没得删了


# ================================================================ 10. 删组守卫（D6）

GROUPS = "/api/source-groups"


@pytest.fixture()
def client_all(db):
    """最小应用：截图路由 + 来源组路由（删组守卫要经 HTTP 验 query 参数）。"""
    from main import register_exception_handlers
    from routers import screenshots as shots
    from routers import source_groups as sgr

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(shots.router, prefix="/api/screenshots")
    app.include_router(sgr.router, prefix=GROUPS)
    return TestClient(app)


@pytest.fixture()
def group_evidence_root(evidence_root, monkeypatch):
    """删组路径不走路由的 `_evidence_root()`（会成环），用服务层默认根。

    生产环境两者算出来是同一个目录（都由 `__file__` 推项目根 + data/evidence），
    测试里把服务层默认根指到 tmp 即可让文件断言落地。
    """
    monkeypatch.setattr(evidence_service, "default_evidence_root",
                        lambda: str(evidence_root))
    return evidence_root


def _group(db, *, slug, label):
    sg = SourceGroup(slug=slug, label=label, asset_prefix=f"{label}-")
    db.add(sg)
    db.flush()
    return sg


def _group_evidence(db, root, group, *, rel, sha_char, data_date=date(2026, 7, 1)):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(PNG_BYTES)
    ev = _add_evidence(db, group_id=group.id, file_path=rel, sha_char=sha_char,
                       data_date=data_date, filename=f"示例-{sha_char}.png")
    db.commit()
    return ev


def test_group_delete_blocked_by_used_evidence_409(db, client_all, group_evidence_root):
    sg = _group(db, slug="demo-used", label="示例已用组")
    ev = _group_evidence(db, group_evidence_root, sg, rel="2026/07/used.png",
                         sha_char="u")
    asset = asset_service.create_asset(db, "示例-已用项", 100, date(2026, 7, 1))
    db.get(Asset, asset.id).source_evidence_id = ev.id
    db.commit()

    resp = client_all.delete(f"{GROUPS}/{sg.id}")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "SOURCE_GROUP_HAS_USED_EVIDENCE"
    assert err["details"]["count"] == 1
    # 已使用凭证是证据链，任何参数都不删
    assert db.get(SourceGroup, sg.id) is not None
    assert db.get(ScreenshotEvidence, ev.id) is not None
    assert (group_evidence_root / "2026/07/used.png").exists()


def test_group_delete_blocked_by_unused_evidence_409(db, client_all, group_evidence_root):
    """默认永不静默级联：只有废图的组也先 409，提示两条出路。"""
    sg = _group(db, slug="demo-unused", label="示例废图组")
    ev = _group_evidence(db, group_evidence_root, sg, rel="2026/07/unused.png",
                         sha_char="n")

    resp = client_all.delete(f"{GROUPS}/{sg.id}")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "SOURCE_GROUP_HAS_UNUSED_EVIDENCE"
    assert err["details"]["count"] == 1
    assert "purge_unused=true" in err["details"]["hint"]
    assert db.get(SourceGroup, sg.id) is not None
    assert db.get(ScreenshotEvidence, ev.id) is not None
    assert (group_evidence_root / "2026/07/unused.png").exists()


def test_group_delete_with_purge_unused_clears_group_evidence(db, client_all,
                                                             group_evidence_root):
    """`purge_unused=true`：该组凭证行与文件清零，**其他组毫发无伤**。"""
    target = _group(db, slug="demo-target", label="示例目标组")
    ev_a = _group_evidence(db, group_evidence_root, target, rel="2026/07/a.png",
                           sha_char="a")
    ev_b = _group_evidence(db, group_evidence_root, target, rel="2026/07/b.png",
                           sha_char="b")
    other = _group(db, slug="demo-other", label="示例旁组")
    ev_other = _group_evidence(db, group_evidence_root, other, rel="2026/08/other.png",
                               sha_char="o")

    resp = client_all.delete(f"{GROUPS}/{target.id}",
                             params={"purge_unused": True,
                                     "reason": "normalization"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["purged_evidence"] == 2
    assert body["freed_bytes"] == 2 * len(PNG_BYTES)

    assert db.get(SourceGroup, target.id) is None                # 组没了
    assert db.get(ScreenshotEvidence, ev_a.id) is None           # 凭证行没了
    assert db.get(ScreenshotEvidence, ev_b.id) is None
    assert not (group_evidence_root / "2026/07/a.png").exists()  # 文件没了
    assert not (group_evidence_root / "2026/07/b.png").exists()

    # 其他组：组、凭证行、文件全部不变
    assert db.get(SourceGroup, other.id) is not None
    assert db.get(ScreenshotEvidence, ev_other.id).source_group_id == other.id
    assert (group_evidence_root / "2026/08/other.png").exists()
    # 该组凭证的审计行照旧留痕（evidence_ref 留空，故 FK 不因删行而悬空）
    audits = db.execute(select(AuditLog).where(
        AuditLog.entity == "screenshot_evidence",
        AuditLog.action == "delete")).scalars().all()
    assert {a.row_id for a in audits} == {ev_a.id, ev_b.id}
    assert all(a.evidence_ref is None for a in audits)


def test_group_delete_purge_unused_still_blocks_used_evidence(db, client_all,
                                                              group_evidence_root):
    """`purge_unused=true` 不是万能钥匙：组内有已使用凭证照样 409，且一条都不删。"""
    sg = _group(db, slug="demo-mixed", label="示例混合组")
    used = _group_evidence(db, group_evidence_root, sg, rel="2026/07/m-used.png",
                           sha_char="m")
    unused = _group_evidence(db, group_evidence_root, sg, rel="2026/07/m-unused.png",
                             sha_char="z")
    asset = asset_service.create_asset(db, "示例-混合项", 200, date(2026, 7, 2))
    db.get(Asset, asset.id).source_evidence_id = used.id
    db.commit()

    resp = client_all.delete(f"{GROUPS}/{sg.id}", params={"purge_unused": True})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SOURCE_GROUP_HAS_USED_EVIDENCE"
    assert db.get(SourceGroup, sg.id) is not None
    assert db.get(ScreenshotEvidence, used.id) is not None
    assert db.get(ScreenshotEvidence, unused.id) is not None     # 守卫先于清理
    assert (group_evidence_root / "2026/07/m-unused.png").exists()


def test_group_delete_without_evidence_reports_zero_purged(db, client_all,
                                                           group_evidence_root):
    sg = _group(db, slug="demo-empty", label="示例空组")

    resp = client_all.delete(f"{GROUPS}/{sg.id}",
                             params={"reason": "normalization"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "purged_evidence": 0, "freed_bytes": 0}
    assert db.get(SourceGroup, sg.id) is None