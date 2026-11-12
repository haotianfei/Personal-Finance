"""识别快照落库（evidence-naming D7 / ADR-0023）：spec §3 第 8–11 条。

覆盖范围（逐条对齐票 03 与 spec 验收）：

8. `/preview` 后 `recognition_json` 可解析，`recognized.items` 与预览行同数、`blocks` 非空；
9. `/apply` 后 `adopted.items` 与落库账本行名称/金额逐条一致、`corrected_rows` == 人改行数；
10. AI 路径：带 `recognition` → `items` 与传入一致且 `source="client"`；不带 → `items=null`
    且**不报错**（记 `note="client-side"`）；
11. 体积上限：超 1 MiB → 存证成功、`blocks_dropped=true`、JSON < 1 MiB、`items` 仍完整。

外加三写入点的第三个（异步预览 job → 含 `/preview-jobs` 回传通道与续跑工厂透传）、
同凭证二次采纳的覆盖语义、凭证已删除/不存在时的静默 no-op、`recognition_summary`
摘要与本体分离、非法 `recognition` → 422 且不存证（**仅同步 `/preview` `/apply`；
`/preview-jobs` 非法只记 warning 不报错**，见 preview-job-recognition spec D3）。

识别一律 monkeypatch（不跑真 OCR）；凭证根与临时文件全走 `tmp_path`，绝不触碰真实
`data/`。数据合成（`示例-` 前缀、虚构金额），不含真实机构名、金额或本机路径。
"""
import asyncio
import io
import json
import logging
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile

import routers.screenshots as shots
from errors import ApiError, ErrorCode
from models import Asset, AuditLog, ScreenshotEvidence
from services import evidence_service
from services.screenshot_service import (
    PARSER_VERSION,
    ParsedItem,
    ParsedScreenshot,
    get_source_group,
    seed_default_source_groups,
)

PREFIX = "示例-"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MAX = evidence_service.RECOGNITION_MAX_BYTES
SCHEMA = evidence_service.RECOGNITION_SCHEMA


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _default_source_group(db):
    """0029 起凭证只存 `source_group_id`：上传存证前必须有组行（真库靠启动种子）。"""
    seed_default_source_groups(db)


@pytest.fixture()
def evidence_root(tmp_path, monkeypatch):
    """凭证根指向 tmp（`_evidence_root()` 既有测试替身惯例）。"""
    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(root))
    return root


# ---------------------------------------------------------------- 替身 / 造数

def upload(name="示例-截图.png"):
    return UploadFile(filename=name, file=io.BytesIO(PNG))


def patch_pipeline(monkeypatch, parsed, existing=()):
    """OCR 管线替身：组配置可读、组内既有资产可控、解析直接返回给定 parsed。"""
    ss = shots.screenshot_service
    monkeypatch.setattr(
        ss, "get_group_config",
        lambda db_, key: {"asset_prefix": PREFIX, "account_ids": [], "rules": None})
    monkeypatch.setattr(ss, "get_group_existing",
                        lambda db_, prefix: list(existing))
    monkeypatch.setattr(ss, "parse_screenshot", lambda *a, **k: parsed)


def patch_ai_pipeline(monkeypatch):
    """AI 管线替身：OCR 必炸（AI 路径不得调解析），组内无既有资产。"""
    ss = shots.screenshot_service
    monkeypatch.setattr(
        ss, "get_group_config",
        lambda db_, key: {"asset_prefix": PREFIX, "account_ids": [], "rules": None})
    monkeypatch.setattr(ss, "get_group_existing", lambda db_, prefix: [])

    def boom(*a, **k):
        raise AssertionError("engine=ai 不得调用 OCR 解析（parse_screenshot）")

    monkeypatch.setattr(ss, "parse_screenshot", boom)


def parsed_two_items(raw_blocks=None):
    """两条识别行（第一条金额带千分位形态的常见值，第二条整千）。"""
    return ParsedScreenshot(
        items=[ParsedItem(name="示例基金A", amount=Decimal("1234.56")),
               ParsedItem(name="示例基金B", amount=Decimal("2000.00"))],
        total=Decimal("3234.56"), date="2026-11-04",
        raw_blocks=(raw_blocks if raw_blocks is not None else [
            (10, 20, "示例基金A", 0.99),
            (40, 120, "1,234.56", 0.98),
            (70, 20, "示例基金B", 0.97),
        ]),
        date_text="2026-11-04")


def huge_blocks(count=20000, text="示例识别行" * 5):
    """体积累计超 1 MiB 的 OCR 块（每块 ~100 字节序列化后）。"""
    return [(i * 10, 0, text, 0.99) for i in range(count)]


def call_preview(db, *, engine="ocr", recognition=""):
    return asyncio.run(shots.screenshot_preview(
        file=upload(), source_group="bantuou", unrecognized="keep",
        force_ocr=False, engine=engine, recognition=recognition, db=db))


def confirms(*keys) -> dict:
    """D4：create 行必须显式表态（走路由时拿不到 preview 行对象，按预览行号表态）。"""
    return {str(k): {"action": "create"} for k in keys}


def call_apply(db, options, *, data_date="2026-11-04", recognition=""):
    return asyncio.run(shots.screenshot_apply(
        file=upload(), options=json.dumps(options), data_date=data_date,
        recognition=recognition, db=db))


def snapshot_of(db, evidence_id):
    """凭证行的快照本体（过期重读：写入走的是同一 Session 的另一次 commit）。"""
    db.expire_all()
    ev = db.get(ScreenshotEvidence, evidence_id)
    assert ev is not None and ev.recognition_json, "凭证行没有识别快照"
    return json.loads(ev.recognition_json)


def raw_snapshot(db, evidence_id) -> str:
    db.expire_all()
    return db.get(ScreenshotEvidence, evidence_id).recognition_json


def add_evidence_row(db, *, sha_char="c") -> ScreenshotEvidence:
    """直接造一条凭证行（不经上传）——给「写入函数」的单元级用例用。"""
    group = get_source_group(db, "bantuou")
    ev = ScreenshotEvidence(source_group_id=group.id,
                            file_path=f"2026/11/{sha_char * 8}.png",
                            file_sha256=sha_char * 64)
    db.add(ev)
    db.commit()
    return ev


# ================================================================ 8. /preview（OCR）

def test_preview_records_ocr_snapshot(db, evidence_root, monkeypatch):
    """`POST /preview` 后：`recognized` 段可解析、条数与预览行同数、`blocks` 非空。"""
    patch_pipeline(monkeypatch, parsed_two_items())
    resp = call_preview(db)

    assert resp.evidence_id is not None
    snap = snapshot_of(db, resp.evidence_id)
    assert snap["schema"] == SCHEMA
    rec = snap["recognized"]
    assert (rec["engine"], rec["source"]) == ("ocr", "server")
    assert rec["parser_version"] == PARSER_VERSION          # 归因锚点（ADR-0023 D5）
    assert rec["parsed_at"] and "T" in rec["parsed_at"]
    # 空组场景：预览行 == 识别行（spec §3.8 的「条数一致」）
    assert len(rec["items"]) == len(resp.rows) == 2
    assert [i["name"] for i in rec["items"]] == ["示例基金A", "示例基金B"]
    assert rec["items"][0]["amount"] == "1234.56"           # 金额一律字符串（无 float 尾巴）
    assert rec["total"] == "3234.56"
    assert rec["date_text"] == "2026-11-04"
    # blocks 首次被序列化：文本 + 坐标，且不含 score（组织层不消费）
    assert rec["blocks"] and rec["blocks"][0] == {"text": "示例基金A", "x": 20, "y": 10}
    assert set(rec["blocks"][0]) == {"text", "x", "y"}
    assert rec["blocks_dropped"] is False
    # 预览阶段还没采纳：不留 adopted 段（不是空对象，是根本没写）
    assert "adopted" not in snap


# ================================================================ 9. /apply

def test_apply_records_adopted_matching_ledger(db, evidence_root, monkeypatch):
    """`POST /apply` 后：`adopted.items` 与落库行名称/金额逐条一致、`corrected_rows` 准。"""
    patch_pipeline(monkeypatch, parsed_two_items())
    res = call_apply(db, {"source_group": "bantuou", "amounts": {"0": "999.99"},
                          "identity_decisions": confirms(0, 1)})

    snap = snapshot_of(db, res.evidence_id)
    rec, adp = snap["recognized"], snap["adopted"]
    assert rec["engine"] == "ocr" and len(rec["items"]) == 2    # 本次重跑结果
    assert adp["engine"] == "ocr"
    assert adp["item_count"] == res.rows_written == 2
    assert adp["corrected_rows"] == 1                          # 只有第 0 行被改数

    ledger = db.execute(
        select(Asset).where(Asset.source_evidence_id == res.evidence_id)
    ).scalars().all()
    assert sorted((i["name"], Decimal(i["amount"])) for i in adp["items"]) == \
        sorted((a.asset_name, Decimal(str(a.amount))) for a in ledger)
    amounts = {i["name"]: i["amount"] for i in adp["items"]}
    assert amounts[PREFIX + "示例基金A"] == "999.99"            # 人改后的值
    assert amounts[PREFIX + "示例基金B"] == "2000.00"


def test_apply_corrected_rows_counts_skip_and_zero(db, evidence_root, monkeypatch):
    """`corrected_rows` 口径：取消勾选 1 行 + 单独清0 1 行 = 2 行（原样行不计）。"""
    existing = [(PREFIX + "示例基金A", Decimal("1234.56")),
                (PREFIX + "示例基金C", Decimal("500.00"))]
    patch_pipeline(monkeypatch, parsed_two_items(), existing=existing)
    # 行 0 = A（update，被取消勾选）；行 1 = B（create）；行 2 = C（keep→单独清0）
    res = call_apply(db, {"source_group": "bantuou", "skipped": [0],
                          "zero_names": [PREFIX + "示例基金C"],
                          # 行 0 被撤销后只剩行 1（create）需表态
                          "identity_decisions": confirms(1)})
    assert res.rows_written == 2                   # B 新增 + C 清零
    adp = snapshot_of(db, res.evidence_id)["adopted"]
    assert adp["item_count"] == 2
    assert adp["corrected_rows"] == 2              # 跳过 1 + 清0 1
    assert {i["name"]: i["amount"] for i in adp["items"]} == {
        PREFIX + "示例基金B": "2000.00", PREFIX + "示例基金C": "0.00"}


# ================================================================ 10. AI 路径

def test_ai_preview_without_recognition_records_null_items(db, evidence_root, monkeypatch):
    """未回传 `recognition` → `items=null` + note，**不报错**（ADR-0023 D7）。"""
    patch_ai_pipeline(monkeypatch)
    resp = call_preview(db, engine="ai")

    rec = snapshot_of(db, resp.evidence_id)["recognized"]
    assert (rec["engine"], rec["source"]) == ("ai", "client")
    assert rec["items"] is None
    assert rec["note"] == "client-side"
    assert rec["blocks"] is None
    assert rec["parser_version"] == PARSER_VERSION


def test_ai_preview_with_recognition_stores_client_items(db, evidence_root, monkeypatch):
    """回传 `recognition`（数组形态）→ `items` 与传入一致且 `source="client"`。"""
    patch_ai_pipeline(monkeypatch)
    payload = [{"name": "示例基金A", "amount": "1234.56"},
               {"name": "示例基金C", "amount": "88.00"}]
    resp = call_preview(db, engine="ai",
                        recognition=json.dumps(payload, ensure_ascii=False))

    rec = snapshot_of(db, resp.evidence_id)["recognized"]
    assert rec["items"] == payload
    assert rec["source"] == "client"
    assert "note" not in rec                       # 存下来了就不打缺口标记


def test_ai_preview_accepts_object_payload_with_total(db, evidence_root, monkeypatch):
    """也接受 `{items, total, date_text}` 形态（可选锚点一起留）。"""
    patch_ai_pipeline(monkeypatch)
    payload = {"items": [{"name": "示例基金A", "amount": "1234.56"}],
               "total": "1234.56", "date_text": "2026-11-04"}
    resp = call_preview(db, engine="ai",
                        recognition=json.dumps(payload, ensure_ascii=False))

    rec = snapshot_of(db, resp.evidence_id)["recognized"]
    assert rec["items"] == payload["items"]
    assert rec["total"] == "1234.56"
    assert rec["date_text"] == "2026-11-04"


def test_ai_apply_records_client_items_and_adopted(db, evidence_root, monkeypatch):
    """AI 采纳：`recognized` 来自回传、`adopted` 来自真正落账本的行。"""
    patch_ai_pipeline(monkeypatch)
    payload = [{"name": "示例基金A", "amount": "1234.56"}]
    res = call_apply(db, {"source_group": "bantuou", "engine": "ai",
                          "supplements": [{"name": "示例基金A", "amount": "1234.56"}],
                          # AI 空骨架 + 组内无既有资产 → 唯一 create 行是补录行（行 0）
                          "identity_decisions": confirms(0)},
                     recognition=json.dumps(payload, ensure_ascii=False))

    assert res.supplements_written == 1
    snap = snapshot_of(db, res.evidence_id)
    assert snap["recognized"]["source"] == "client"
    assert snap["recognized"]["items"] == payload
    adp = snap["adopted"]
    assert adp["engine"] == "ai"
    assert adp["item_count"] == 1
    assert adp["corrected_rows"] == 1               # 补录＝人新增的行
    assert adp["items"] == [{"name": PREFIX + "示例基金A", "amount": "1234.56"}]


@pytest.mark.parametrize("bad", [
    "{不是 JSON",                                            # 解析失败
    json.dumps({"items": "不是数组"}),                        # items 形状错
    json.dumps([{"amount": "1.00"}]),                        # 缺 name
    json.dumps([{"name": "   ", "amount": "1.00"}]),          # 空 name
    json.dumps(42),                                          # 顶层非数组/对象
])
def test_invalid_recognition_field_is_422_and_not_stored(
        db, evidence_root, monkeypatch, bad):
    """非法 `recognition` → 422 `VALIDATION_ERROR`，且**不存证**（不静默吞）。"""
    patch_ai_pipeline(monkeypatch)
    with pytest.raises(ApiError) as ei:
        call_preview(db, engine="ai", recognition=bad)
    assert ei.value.status_code == 422
    assert ei.value.code == str(ErrorCode.VALIDATION_ERROR)
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0


def test_invalid_recognition_on_apply_is_422(db, evidence_root, monkeypatch):
    patch_ai_pipeline(monkeypatch)
    with pytest.raises(ApiError) as ei:
        call_apply(db, {"source_group": "bantuou", "engine": "ai"},
                   recognition="{不是 JSON")
    assert ei.value.status_code == 422
    assert ei.value.code == str(ErrorCode.VALIDATION_ERROR)


# ================================================================ 写入点 2/3：异步预览 job

def test_preview_job_records_snapshot(tmp_path, db, evidence_root, monkeypatch):
    """异步预览完成时同样留快照（与 `/preview` 同口径）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_pipeline(monkeypatch, parsed_two_items())
    ev = add_evidence_row(db, sha_char="d")
    img = tmp_path / "示例-预览.png"
    img.write_bytes(PNG)

    work = shots.run_preview_job(str(img), "d" * 64, "bantuou", "keep",
                                 "示例-预览.png", evidence_id=ev.id)
    result = work(lambda **fields: None)          # 当前线程直接驱动闭包

    assert result["evidence_id"] == ev.id
    rec = snapshot_of(db, ev.id)["recognized"]
    assert rec["engine"] == "ocr" and len(rec["items"]) == 2
    assert rec["blocks"] and rec["blocks_dropped"] is False
    assert not img.exists()                       # cleanup=True 语义不变


def test_preview_job_ai_records_null_items(tmp_path, db, evidence_root, monkeypatch):
    """异步 job 未回传 `recognition` → items=null + note（不是「没识别」而是「没存」）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    ev = add_evidence_row(db, sha_char="e")
    img = tmp_path / "示例-预览.png"
    img.write_bytes(PNG)

    work = shots.run_preview_job(str(img), "e" * 64, "bantuou", "keep",
                                 "示例-预览.png", engine="ai", evidence_id=ev.id)
    work(lambda **fields: None)

    rec = snapshot_of(db, ev.id)["recognized"]
    assert (rec["engine"], rec["source"], rec["items"]) == ("ai", "client", None)
    assert rec["note"] == "client-side"


# ------------------------- 写入点 2/3 的回传通道（preview-job-recognition 票 01）

def capture_submit(monkeypatch):
    """把 `preview_jobs.submit` 换成「只记录不入队」的替身：端点层用例不起线程、
    不碰持久层，只验「载荷里有什么 + 闭包拿到的值」这两件事。"""
    captured: dict = {}

    def fake_submit(work, payload=None, db=None):
        captured["work"] = work
        captured["payload"] = payload
        return "job-under-test"

    monkeypatch.setattr(shots.preview_jobs, "submit", fake_submit)
    return captured


def call_create_job(db, *, engine="ai", recognition=None):
    """端点直调（async 路由函数）。`recognition=None` = **完全不传**该表单字段
    （走 FastAPI 的默认值），与「传空串」区分开：前者才是老客户端的真实载荷。"""
    kwargs = dict(file=upload(), source_group="bantuou", unrecognized="keep",
                  force_ocr=False, engine=engine, db=db)
    if recognition is not None:
        kwargs["recognition"] = recognition
    return asyncio.run(shots.create_preview_job(**kwargs))


def test_preview_job_without_recognition_keeps_today_behavior(
        db, evidence_root, monkeypatch):
    """不传 `recognition` → 载荷记 None、快照回落到 `items=null` + note（验收 1）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    captured = capture_submit(monkeypatch)

    out = call_create_job(db)                      # 不传字段

    # 显式 None 而不是「缺键」：续跑工厂用 .get 取值，两种都安全，但这里锁住语义
    assert captured["payload"]["recognition"] is None
    assert out["job_id"] == "job-under-test"
    captured["work"](lambda **fields: None)
    rec = snapshot_of(db, out["evidence_id"])["recognized"]
    assert (rec["engine"], rec["source"], rec["items"]) == ("ai", "client", None)
    assert rec["note"] == "client-side"


def test_preview_job_with_recognition_stores_client_items(
        db, evidence_root, monkeypatch):
    """传合法 `recognition` → 规范化后进载荷，job 完成时落库（验收 2）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    captured = capture_submit(monkeypatch)
    items = [{"name": "示例基金A", "amount": "1234.56"},
             {"name": "示例基金C", "amount": "88.00"}]

    out = call_create_job(db, recognition=json.dumps(items, ensure_ascii=False))

    normalized = {"items": items, "total": None, "date_text": None}
    assert captured["payload"]["recognition"] == normalized   # 续跑要用同一份
    captured["work"](lambda **fields: None)
    snap = snapshot_of(db, out["evidence_id"])
    rec = snap["recognized"]
    assert rec["source"] == "client"
    assert rec["items"] == items
    assert "note" not in rec                       # 存下来了就不打缺口标记
    assert "adopted" not in snap                   # 预览阶段还没采纳（验收 2 后半句）


def test_preview_job_invalid_recognition_is_ignored_not_422(
        db, evidence_root, monkeypatch, caplog):
    """非法 JSON → 照常入队（不 422）、记 warning、快照回落缺省形态（验收 3/D3）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    captured = capture_submit(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="routers.screenshots"):
        out = call_create_job(db, recognition="{不是 JSON")

    assert out["job_id"] == "job-under-test"       # 预览是主路径：绝不因补录信息失败
    assert captured["payload"]["recognition"] is None
    assert any("recognition" in r.getMessage() for r in caplog.records)
    captured["work"](lambda **fields: None)
    rec = snapshot_of(db, out["evidence_id"])["recognized"]
    assert rec["items"] is None and rec["note"] == "client-side"


def test_resume_factory_carries_recognition(tmp_path, db, evidence_root, monkeypatch):
    """job 重排（续跑工厂从 payload 重建）→ 仍带回传行，快照不丢（验收 4/D2）。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    ev = add_evidence_row(db, sha_char="7")
    img = tmp_path / "示例-续跑.png"
    img.write_bytes(PNG)
    monkeypatch.setattr(shots, "_abs_evidence_path", lambda eid: str(img))
    items = [{"name": "示例基金A", "amount": "12.00"}]
    payload = {
        "evidence_id": ev.id, "sha": "7" * 64, "source_group": "bantuou",
        "unrecognized": "keep", "filename": "示例-续跑.png",
        "force_ocr": False, "engine": "ai",
        "recognition": {"items": items, "total": None, "date_text": None},
    }

    shots.work_from_payload(payload)(lambda **fields: None)

    rec = snapshot_of(db, ev.id)["recognized"]
    assert rec["items"] == items and rec["source"] == "client"
    assert img.exists()                            # cleanup=False：凭证原件绝不删


def test_resume_factory_legacy_payload_without_recognition(
        tmp_path, db, evidence_root, monkeypatch):
    """本字段上线前入队的老载荷（无 `recognition` 键）→ 按「没回传」处理，不 KeyError。"""
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    patch_ai_pipeline(monkeypatch)
    ev = add_evidence_row(db, sha_char="8")
    img = tmp_path / "示例-老载荷.png"
    img.write_bytes(PNG)
    monkeypatch.setattr(shots, "_abs_evidence_path", lambda eid: str(img))
    payload = {
        "evidence_id": ev.id, "sha": "8" * 64, "source_group": "bantuou",
        "unrecognized": "keep", "filename": "示例-老载荷.png",
        "force_ocr": False, "engine": "ai",
    }

    shots.work_from_payload(payload)(lambda **fields: None)

    rec = snapshot_of(db, ev.id)["recognized"]
    assert rec["items"] is None and rec["note"] == "client-side"


# ================================================================ 覆盖语义

def test_later_preview_keeps_existing_adopted_segment(db, evidence_root, monkeypatch):
    """同 sha 再次预览（复用同一凭证行）只刷新 `recognized`，不清掉既有 `adopted`。"""
    patch_pipeline(monkeypatch, parsed_two_items())
    res = call_apply(db, {"source_group": "bantuou",
                          "identity_decisions": confirms(0, 1)})
    before = snapshot_of(db, res.evidence_id)["adopted"]
    assert before["item_count"] == 2

    resp = call_preview(db)                       # 同字节 → 同 sha → 同一凭证行
    assert resp.evidence_id == res.evidence_id
    snap = snapshot_of(db, res.evidence_id)
    assert snap["adopted"] == before              # 采纳留痕未被预览抹掉
    assert len(snap["recognized"]["items"]) == 2


def test_second_apply_overwrites_instead_of_appending(db, evidence_root, monkeypatch):
    """同一凭证的第二次采纳覆盖同段（快照答「最近一次」，不按次追加历史）。"""
    patch_pipeline(monkeypatch, parsed_two_items())
    first = call_apply(db, {"source_group": "bantuou",
                            "amounts": {"0": "111.11", "1": "222.22"},
                            # 组内无既有资产 → 两行都是 create（D4）
                            "identity_decisions": confirms(0, 1)})
    snap1 = snapshot_of(db, first.evidence_id)["adopted"]
    assert [i["amount"] for i in snap1["items"]] == ["111.11", "222.22"]

    # 第二次采纳：组内已有上一批的资产 → 两行都是 update（同日再覆盖）
    patch_pipeline(monkeypatch, parsed_two_items(), existing=[
        (PREFIX + "示例基金A", Decimal("111.11")),
        (PREFIX + "示例基金B", Decimal("222.22")),
    ])
    second = call_apply(db, {"source_group": "bantuou",
                             "amounts": {"0": "333.33", "1": "444.44"}})
    assert second.evidence_id == first.evidence_id
    snap2 = snapshot_of(db, first.evidence_id)
    assert [i["amount"] for i in snap2["adopted"]["items"]] == ["333.33", "444.44"]
    assert snap2["adopted"]["item_count"] == 2     # 不是 4：没有按次累加
    assert isinstance(snap2["recognized"], dict)   # 段仍是对象，不是数组


# ================================================================ 11. 体积上限

def test_oversized_blocks_dropped_and_marked(db, evidence_root, monkeypatch):
    """超 1 MiB → 丢 blocks 并置 `blocks_dropped=true`，存证成功且识别行仍完整。"""
    patch_pipeline(monkeypatch, parsed_two_items(raw_blocks=huge_blocks()))
    resp = call_preview(db)

    raw = raw_snapshot(db, resp.evidence_id)
    assert len(raw.encode("utf-8")) < MAX
    assert len(raw) < MAX                          # 字符数同样在限内（bytes ≥ chars）
    rec = json.loads(raw)["recognized"]
    assert rec["blocks_dropped"] is True
    assert rec["blocks"] is None                   # 留键不留值：读侧一眼看出块没存
    assert [i["name"] for i in rec["items"]] == ["示例基金A", "示例基金B"]
    assert rec["items"][0]["amount"] == "1234.56"


def test_snapshot_falls_back_to_counts_when_items_also_huge(db):
    """二级降级：丢块后仍超限 → 只留条数与摘要（`items=null` + `item_count`）。"""
    ev = add_evidence_row(db, sha_char="f")
    huge_items = [{"name": "示例" + "长" * 200, "amount": "1.00"} for _ in range(5000)]
    evidence_service.record_recognition(
        db, ev.id,
        recognized={"engine": "ai", "source": "client", "parser_version": "x",
                    "parsed_at": "2026-11-05T00:00:00", "items": huge_items,
                    "total": None, "date_text": None, "blocks": None,
                    "blocks_dropped": False},
        adopted={"engine": "ocr", "item_count": 1, "corrected_rows": 0,
                 "items": [{"name": "示例基金A", "amount": "1.00"}]})

    raw = raw_snapshot(db, ev.id)
    assert len(raw.encode("utf-8")) < MAX
    snap = json.loads(raw)
    assert snap["recognized"]["items"] is None
    assert snap["recognized"]["item_count"] == 5000
    assert snap["adopted"]["items"] is None
    assert snap["adopted"]["item_count"] == 1
    assert snap["adopted"]["corrected_rows"] == 0   # 摘要里的关键数字不丢


# ================================================================ no-op / 摘要分离

def test_record_recognition_missing_row_is_noop(db):
    """凭证已被清理/从未存在 → 静默 no-op（绝不抛错阻断主流程）。"""
    evidence_service.record_recognition(
        db, 987654, recognized={"engine": "ocr", "items": []},
        adopted={"engine": "ocr", "item_count": 0, "corrected_rows": 0, "items": []})
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0


def test_record_recognition_none_evidence_id_is_noop(db):
    evidence_service.record_recognition(
        db, None, recognized={"engine": "ocr", "items": []})   # legacy 直调路径
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0


def test_list_summary_is_digest_not_body(db, evidence_root, monkeypatch):
    """列表只吐摘要：条数/引擎/块标记都有，`blocks` 与 `items` 本体一条不回。"""
    patch_pipeline(monkeypatch, parsed_two_items())
    res = call_apply(db, {"source_group": "bantuou", "amounts": {"0": "999.99"},
                          "identity_decisions": confirms(0, 1)})

    items, _total = evidence_service.list_evidence(db)
    summary = next(i for i in items if i["id"] == res.evidence_id)["recognition_summary"]
    assert summary["schema"] == SCHEMA
    assert (summary["engine"], summary["source"]) == ("ocr", "server")
    assert summary["parser_version"] == PARSER_VERSION
    assert summary["recognized_items"] == 2
    assert summary["adopted_items"] == 2
    assert summary["corrected_rows"] == 1
    assert summary["blocks_dropped"] is False
    assert "blocks" not in summary and "items" not in summary   # 本体不回列表
    # 本体确实在库里（摘要不是靠删数据换来的）
    assert snapshot_of(db, res.evidence_id)["recognized"]["blocks"]

# ================================================================ PATCH /evidence/{id}/recognition
# AI 识别完成后的快照回写（.scratch/evidence-recognition-writeback，spec D1–D5 / §3.1–6）

_MISSING = object()


def call_writeback(db, evidence_id, recognition=_MISSING):
    """端点直调：`_MISSING` = 请求体整个没给（FastAPI 的缺体场景），
    `None` = 传了 `{"recognition": null}`，两者都必须 422。"""
    body = None if recognition is _MISSING else \
        shots.EvidenceRecognitionRequest(recognition=recognition)
    return shots.writeback_evidence_recognition(evidence_id, body, db)


def test_writeback_stores_recognized_and_keeps_adopted(db):
    """验收 1：合法结构 → 200；`recognized.items` 条数一致，`adopted` 段原值不变。"""
    ev = add_evidence_row(db, sha_char="a")
    adopted = {"engine": "ocr", "item_count": 1, "corrected_rows": 0,
               "items": [{"name": PREFIX + "示例基金A", "amount": "111.11"}]}
    evidence_service.record_recognition(db, ev.id, adopted=adopted)

    items = [{"name": "示例基金A", "amount": "1234.56"},
             {"name": "示例基金C", "amount": "88.00"}]
    out = call_writeback(db, ev.id, items)

    assert out == {"ok": True, "saved": True, "evidence_id": ev.id,
                   "recognized_items": 2}
    snap = snapshot_of(db, ev.id)
    rec = snap["recognized"]
    assert (rec["engine"], rec["source"]) == ("ai", "client")
    assert rec["items"] == items
    assert "note" not in rec                       # 存下来了就不打缺口标记
    assert snap["adopted"] == adopted              # 只写 recognized：采纳段一字未动


def test_writeback_twice_keeps_last_only(db):
    """验收 2 / D7：连续回写两次（行数不同）→ 只保留第二次，不累加。"""
    ev = add_evidence_row(db, sha_char="b")
    call_writeback(db, ev.id, [{"name": "示例基金A", "amount": "1.00"},
                               {"name": "示例基金B", "amount": "2.00"}])
    out = call_writeback(db, ev.id, [{"name": "示例基金C", "amount": "3.00"}])

    assert out["recognized_items"] == 1
    assert snapshot_of(db, ev.id)["recognized"]["items"] == \
        [{"name": "示例基金C", "amount": "3.00"}]     # 不是 3 条


@pytest.mark.parametrize("bad", [
    "{不是 JSON",                                    # 字符串 JSON 解析失败
    json.dumps({"items": "不是数组"}),                # items 形状错
    json.dumps([{"amount": "1.00"}]),                # 缺 name
    json.dumps([{"name": "   ", "amount": "1.00"}]),  # 空 name
    json.dumps(42),                                  # 顶层非数组/对象
    42,                                              # 直接给标量
    {"items": "不是数组"},                            # 对象形态、items 形状错
    None,                                            # 显式 null
    _MISSING,                                        # 请求体/字段整个没给
])
def test_writeback_invalid_recognition_is_422_and_row_untouched(db, bad):
    """验收 3 / D4：缺失或非法 → 422 `VALIDATION_ERROR`，该行 `recognition_json` 一字未改。"""
    ev = add_evidence_row(db, sha_char="c")
    call_writeback(db, ev.id, [{"name": "示例基金A", "amount": "1234.56"}])
    before = raw_snapshot(db, ev.id)

    with pytest.raises(ApiError) as ei:
        call_writeback(db, ev.id, **_param_kwargs(bad))

    assert ei.value.status_code == 422
    assert ei.value.code == str(ErrorCode.VALIDATION_ERROR)
    assert raw_snapshot(db, ev.id) == before


def _param_kwargs(bad):
    return {} if bad is _MISSING else {"recognition": bad}


def test_writeback_missing_row_is_404(db):
    """验收 4：不存在的 `evidence_id` → 404 `EVIDENCE_NOT_FOUND`（且不建行）。"""
    with pytest.raises(ApiError) as ei:
        call_writeback(db, 987654, [{"name": "示例基金A", "amount": "1.00"}])

    assert ei.value.status_code == 404
    assert ei.value.code == str(ErrorCode.EVIDENCE_NOT_FOUND)
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0


def test_writeback_oversize_degrades_instead_of_failing(db):
    """验收 5 / D4：> 1 MiB → 200；`_shrink_snapshot` 降级后照存（只留条数）。"""
    ev = add_evidence_row(db, sha_char="d")
    huge = [{"name": "示例" + "长" * 200, "amount": "1.00"} for _ in range(5000)]
    assert len(json.dumps(huge, ensure_ascii=False).encode("utf-8")) > MAX

    out = call_writeback(db, ev.id, huge)

    assert out["saved"] is True and out["recognized_items"] == 5000
    raw = raw_snapshot(db, ev.id)
    assert len(raw.encode("utf-8")) < MAX
    rec = json.loads(raw)["recognized"]
    assert rec["items"] is None and rec["item_count"] == 5000   # 只留条数
    assert rec["parsed_at"]                          # 归因锚点仍在：知道是哪一次写的


def test_writeback_db_error_reports_not_saved(db, monkeypatch):
    """验收 D4：写库异常 → 仍 200 但 `saved=false`（不回 5xx、不阻断识别流程）。"""
    ev = add_evidence_row(db, sha_char="e")

    def boom():
        raise RuntimeError("示例-写库失败")

    monkeypatch.setattr(db, "commit", boom)
    out = call_writeback(db, ev.id, [{"name": "示例基金A", "amount": "1.00"}])

    assert out["ok"] is True and out["saved"] is False
    assert out["recognized_items"] is None           # 没存下就不报条数
    assert raw_snapshot(db, ev.id) is None           # 快照没落库（rollback 只丢快照）


def test_writeback_writes_no_audit_row(db):
    """验收 6 / D5：回写不产生 `audit_log` 行（快照是旁证，不是业务操作）。"""
    ev = add_evidence_row(db, sha_char="f")
    before = db.execute(select(func.count()).select_from(AuditLog)).scalar()

    out = call_writeback(db, ev.id, [{"name": "示例基金A", "amount": "1.00"}])

    assert out["saved"] is True
    assert db.execute(
        select(func.count()).select_from(AuditLog)).scalar() == before


# ================================================================ GET /evidence/{id}/recognition
# 详情弹层「查看识别行」的数据源（.scratch/evidence-recognition-view，spec D2 / §3.5）
# 三个用例分别对应 D2 的三种结果：有快照 / 快照为空 / 行不存在。


def call_read(db, evidence_id):
    """端点直调（同步路由函数）：返回响应 dict，异常原样抛。"""
    return shots.get_evidence_recognition(evidence_id, db)


def test_read_recognition_returns_snapshot_body(db):
    """验收 5 上：行在且有快照 → 200，`snapshot` 就是库里那份本体（原样不加工）。"""
    ev = add_evidence_row(db, sha_char="1")
    call_writeback(db, ev.id, [{"name": "示例基金A", "amount": "1234.56"},
                               {"name": "示例基金B", "amount": "2000.00"}])
    stored = snapshot_of(db, ev.id)
    audit_before = db.execute(
        select(func.count()).select_from(AuditLog)).scalar()

    out = call_read(db, ev.id)

    assert set(out) == {"snapshot"}                  # 不多吐字段
    assert out["snapshot"] == stored                 # 逐键一致：本体原样返回
    assert out["snapshot"]["schema"] == SCHEMA
    assert out["snapshot"]["recognized"]["items"] == [
        {"name": "示例基金A", "amount": "1234.56"},
        {"name": "示例基金B", "amount": "2000.00"}]
    # 纯读：这次 GET 既不写库也不写审计（D2）
    assert snapshot_of(db, ev.id) == stored
    assert db.execute(
        select(func.count()).select_from(AuditLog)).scalar() == audit_before


def test_read_recognition_empty_snapshot_is_null(db):
    """验收 5 下：行在但从未识别 → 200 + `snapshot: null`（未识别是合法状态）。"""
    ev = add_evidence_row(db, sha_char="2")

    assert call_read(db, ev.id) == {"snapshot": None}
    assert db.get(ScreenshotEvidence, ev.id).recognition_json is None


def test_read_recognition_missing_row_is_404(db):
    """验收 5：不存在的 `evidence_id` → 404 `EVIDENCE_NOT_FOUND`（不建行、不 500）。"""
    with pytest.raises(ApiError) as ei:
        call_read(db, 987654)

    assert ei.value.status_code == 404
    assert ei.value.code == str(ErrorCode.EVIDENCE_NOT_FOUND)
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0
