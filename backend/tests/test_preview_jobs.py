"""异步预览任务：ocr_engine 双路线（fake det/rec，不依赖真 paddle）+
preview_jobs 状态机/串行性 + 端点纯函数。直调惯例同前。"""
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import HTTPException

import routers.screenshots as shots
from services import ocr_engine, preview_jobs
from services.screenshot_service import ParsedItem, ParsedScreenshot


def write_img(path, w=600, h=300):
    img = np.full((h, w, 3), 255, np.uint8)
    assert cv2.imwrite(str(path), img)
    return str(path)


def quad(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype="float32")


class FakeDet:
    """TextDetection 替身：predict(path) → [result with .json['res']['dt_polys']]。"""

    def __init__(self, polys):
        self.polys = polys
        self.calls = []
        self.kwargs = []

    def predict(self, img, **kw):
        self.calls.append(img)
        self.kwargs.append(kw)
        return [SimpleNamespace(json={"res": {
            "dt_polys": self.polys, "dt_scores": [0.9] * len(self.polys)}})]


class FakeRec:
    """TextRecognition 替身：predict_iter 逐条 yield，记录 batch_size。"""

    def __init__(self, texts):
        self.texts = texts
        self.batch_sizes = []

    def predict_iter(self, crops, batch_size=None):
        self.batch_sizes.append(batch_size)
        for i, _c in enumerate(crops):
            yield SimpleNamespace(json={"res": {
                "rec_text": self.texts[i % len(self.texts)], "rec_score": 0.95}})


@pytest.fixture()
def fake_ll(monkeypatch):
    """lowlevel 路线 + 假引擎：每片 2 个文本框。"""
    monkeypatch.setenv("OCR_ENGINE", "lowlevel")
    det = FakeDet([quad(10, 5, 80, 20), quad(10, 30, 90, 45)])
    rec = FakeRec(["支付宝", "123.45"])
    monkeypatch.setattr(ocr_engine, "get_lowlevel_engines", lambda: (det, rec))
    return det, rec


# =====================================================================
# ocr_engine.run_ocr（lowlevel 路线）
# =====================================================================
def test_run_ocr_lowlevel_blocks_progress_and_batch(tmp_path, fake_ll):
    det, rec = fake_ll
    img = write_img(tmp_path / "s.png")
    events = []
    blocks = ocr_engine.run_ocr(img, progress_cb=events.append)

    assert rec.batch_sizes == [6]  # 必须显式 batch_size=6
    assert det.kwargs == [ocr_engine.DET_PARAMS]  # det 参数须对齐 pipeline 运行时默认
    assert blocks == [(5, 10, "支付宝", 0.95), (30, 10, "123.45", 0.95)]
    # 事件序列：先 chunk 后逐 line；line 携带文本与置信度；done ≤ total
    assert events == [("chunk", 0, 1),
                      ("line", 1, 2, "支付宝", 0.95),
                      ("line", 2, 2, "123.45", 0.95)]


def test_run_ocr_lowlevel_multi_chunk(tmp_path, fake_ll):
    img = write_img(tmp_path / "s.png", w=600, h=400)
    events = []
    blocks = ocr_engine.run_ocr(img, chunk=150, overlap=50, progress_cb=events.append)
    chunks = [e for e in events if e[0] == "chunk"]
    lines = [e for e in events if e[0] == "line"]
    assert chunks == [("chunk", 0, 4), ("chunk", 1, 4), ("chunk", 2, 4), ("chunk", 3, 4)]
    assert len(lines) == 8  # 每片 2 行
    for _ev in lines:
        assert _ev[1] <= _ev[2]           # line done ≤ total
    assert blocks[0][1] == 10 and blocks[-1][0] == 30 + 300  # y 偏移回全图坐标


def test_run_ocr_holds_engine_lock(tmp_path, fake_ll, monkeypatch):
    det, _rec = fake_ll
    seen = []
    orig = det.predict

    def spy(p, **kw):
        got = ocr_engine._ENGINE_LOCK.acquire(blocking=False)
        seen.append(got)
        if got:
            ocr_engine._ENGINE_LOCK.release()
        return orig(p, **kw)

    monkeypatch.setattr(det, "predict", spy)
    ocr_engine.run_ocr(write_img(tmp_path / "s.png"))
    assert seen and not any(seen)  # 推理全程持锁 → 外部抢占必失败


def test_run_ocr_pipeline_mode_matches_legacy_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "pipeline")
    calls = []

    class FakePipeEngine:
        def predict(self, input=None):
            calls.append(input)
            return [SimpleNamespace(json={"res": {
                "rec_texts": ["甲", "乙"], "rec_scores": [0.9, 0.8],
                "rec_boxes": [[10, 5, 80, 20], [10, 30, 90, 45]]}})]

    monkeypatch.setattr(ocr_engine, "get_pipeline_engine", lambda: FakePipeEngine())
    events = []
    blocks = ocr_engine.run_ocr(write_img(tmp_path / "s.png"), progress_cb=events.append)
    assert len(calls) == 1
    assert blocks == [(5, 10, "甲", 0.9), (30, 10, "乙", 0.8)]
    assert events == [("chunk", 0, 1)]  # pipeline 路线无 line 事件


def test_engine_mode_default_and_fallback(monkeypatch):
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    assert ocr_engine.engine_mode() == "lowlevel"
    monkeypatch.setenv("OCR_ENGINE", "nonsense")
    assert ocr_engine.engine_mode() == "lowlevel"
    monkeypatch.setenv("OCR_ENGINE", "PIPELINE")
    assert ocr_engine.engine_mode() == "pipeline"


# =====================================================================
# parse_screenshot 进度透传
# =====================================================================
def test_parse_cache_hit_emits_cache_then_organize(monkeypatch):
    from services import screenshot_service as ss
    payload = ss._OcrPayload(blocks=[(5, 20, "稳健宝", 0.9), (5, 400, "1.00", 0.9)],
                             date=None, total=None)
    key = "0" * 64
    monkeypatch.setitem(ss._PARSE_CACHE, key, payload)

    def boom(*a, **k):
        raise AssertionError("缓存命中不得触发 OCR")

    monkeypatch.setattr(ss, "_ocr_blocks", boom)
    events = []
    out = ss.parse_screenshot("whatever.png", cache_key=key, progress_cb=events.append)
    assert events == [("cache",), ("organize",)]   # 命中也重跑组织层
    assert out.raw_blocks == payload.blocks
    assert out.raw_blocks is not payload.blocks     # 不与缓存共享可变引用
    assert [i.name for i in out.items] == ["稳健宝"]


def test_parse_forwards_events_and_organize(monkeypatch):
    from services import screenshot_service as ss
    blocks = [(5, 10, "投顾总金额", 0.9), (30, 10, "1.00", 0.9)]

    def fake_ocr(path, progress_cb=None):
        if progress_cb:
            progress_cb(("chunk", 0, 1))
            progress_cb(("line", 1, 1, "投顾总金额", 0.9))
        return blocks

    monkeypatch.setattr(ss, "_ocr_blocks", fake_ocr)
    events = []
    ss.parse_screenshot("x.png", progress_cb=events.append)
    # 纯透传核实：5 元组 line 事件原样穿过 parse_screenshot（含文本/分数）
    assert events == [("chunk", 0, 1), ("line", 1, 1, "投顾总金额", 0.9),
                      ("organize",)]


# =====================================================================
# 事件 → job 字段映射（纯函数）
# =====================================================================
def test_progress_event_to_fields():
    assert shots.progress_event_to_fields(("cache",)) == {"stage": "cache"}
    assert shots.progress_event_to_fields(("organize",)) == {"stage": "organize"}
    assert shots.progress_event_to_fields(("match",)) == {"stage": "match"}
    assert shots.progress_event_to_fields(("chunk", 2, 9)) == {
        "stage": "ocr", "ocr": {"chunk_index": 2, "chunk_total": 9,
                                "line_done": 0, "line_total": 0}}
    line = shots.progress_event_to_fields(
        ("line", 3, 9, "1,234.56", 0.9876), chunk_index=2)
    assert line == {"stage": "ocr", "line": {"line_done": 3, "line_total": 9},
                    "row": {"t": "1,234.56", "s": 0.988, "c": 2}}
    # 非数值 score → s=0.0（不炸进度链）
    assert shots.progress_event_to_fields(
        ("line", 1, 1, "x", None))["row"]["s"] == 0.0
    assert shots.progress_event_to_fields(("未知",)) == {}


def test_merge_line_into_ocr_keeps_chunk():
    merged = shots.merge_line_into_ocr(
        {"chunk_index": 2, "chunk_total": 9, "line_done": 0, "line_total": 0},
        {"line_done": 3, "line_total": 9})
    assert merged == {"chunk_index": 2, "chunk_total": 9,
                      "line_done": 3, "line_total": 9}
    assert shots.merge_line_into_ocr(None, {"line_done": 1, "line_total": 1}) == {
        "chunk_index": 0, "chunk_total": 0, "line_done": 1, "line_total": 1}


# =====================================================================
# preview_jobs：状态机 / 形状 / 串行 / 队列位 / 清理
# =====================================================================
def wait_job(job_id, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = preview_jobs.get(job_id)
        if snap and snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 超时未结束")


def test_job_state_machine_and_shape():
    def work(report):
        report(stage="ocr", ocr={"chunk_index": 0, "chunk_total": 2,
                                 "line_done": 0, "line_total": 0})
        report(stage="ocr", ocr={"chunk_index": 1, "chunk_total": 2,
                                 "line_done": 3, "line_total": 3},
               lines=[{"t": "行一", "s": 0.912, "c": 1},
                      {"t": "行二", "s": 0.5, "c": 1}])
        return {"ok": 1}

    jid = preview_jobs.submit(work)
    early = preview_jobs.get(jid)
    assert early["status"] in ("queued", "running")
    if early["status"] == "queued":
        assert early["stage"] == "queued"
        assert early["lines"] == []  # 契约：queued 默认空列表

    snap = wait_job(jid)
    assert snap["status"] == "done" and snap["stage"] == "done"
    assert snap["result"] == {"ok": 1} and snap["error"] is None
    assert set(snap) == {"id", "status", "stage", "ocr", "lines",
                         "queue_position", "elapsed", "result", "error"}
    assert snap["ocr"] == {"chunk_index": 1, "chunk_total": 2,
                           "line_done": 3, "line_total": 3}
    assert snap["lines"] == [{"t": "行一", "s": 0.912, "c": 1},
                             {"t": "行二", "s": 0.5, "c": 1}]  # report 透传
    assert snap["elapsed"] >= 0


def test_job_error_recorded():
    def work(report):
        raise RuntimeError("blown")
    snap = wait_job(preview_jobs.submit(work))
    assert snap["status"] == "error" and "blown" in snap["error"]
    assert snap["result"] is None


def test_jobs_serial_no_overlap():
    lock = threading.Lock()
    active, overlaps = [], []

    def make(name, dur=0.12):
        def work(report):
            with lock:
                if active:
                    overlaps.append((name, tuple(active)))
                active.append(name)
            time.sleep(dur)
            with lock:
                active.remove(name)
            return name
        return work

    a = preview_jobs.submit(make("A"))
    b = preview_jobs.submit(make("B"))
    assert wait_job(a)["result"] == "A"
    assert wait_job(b)["result"] == "B"
    assert overlaps == []  # 单 worker 串行：两 job 的「OCR 段」绝不交叠


def test_queue_position_counts_ahead():
    gate = threading.Event()

    def hold(report):
        gate.wait(10)
        return "hold"

    j1 = preview_jobs.submit(hold)
    j2 = preview_jobs.submit(lambda report: 2)
    j3 = preview_jobs.submit(lambda report: 3)
    assert preview_jobs.get(j2)["queue_position"] == 1  # j1(running/queued) 在前
    assert preview_jobs.get(j3)["queue_position"] == 2
    assert preview_jobs.get(j2)["lines"] == []  # 排队中的 job lines 恒 []
    gate.set()
    for j in (j1, j2, j3):
        assert wait_job(j)["queue_position"] == 0


def test_purge_keeps_max_50_finished():
    ids = [preview_jobs.submit(lambda report: "x") for _ in range(55)]
    # FIFO 串行：最后一个结束即全部结束（其间第 51 次 submit 起已触发淘汰）
    assert wait_job(ids[-1])["status"] == "done"
    jn = preview_jobs.submit(lambda report: "y")
    wait_job(jn)
    with preview_jobs._LOCK:
        assert len(preview_jobs._JOBS) <= preview_jobs._MAX_JOBS
    # 最旧的已结束项被淘汰、当前任务可查
    assert preview_jobs.get(ids[0]) is None
    assert preview_jobs.get(jn) is not None


def test_get_preview_job_404():
    with pytest.raises(HTTPException) as ei:
        shots.get_preview_job("no-such-id")
    assert ei.value.status_code == 404
    assert "不存在" in ei.value.detail


# =====================================================================
# work 闭包端到端（自建 Session + 结果形状 + 临时文件清理）
# =====================================================================
def test_run_preview_job_end_to_end(tmp_path, db, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(shots, "SessionLocal", sessionmaker(bind=db.get_bind()))
    ss = shots.screenshot_service
    monkeypatch.setattr(ss, "get_group_config",
                        lambda db_, key: {"asset_prefix": "A-", "account_ids": [], "rules": None})

    parsed = ParsedScreenshot(items=[ParsedItem(name="余额宝", amount=Decimal("10"))],
                              total=None, date="2026-09-13")

    def fake_parse(path, sha, rules, progress_cb=None, force_ocr=False, vocabularies=None):
        if progress_cb:
            progress_cb(("chunk", 0, 1))
            progress_cb(("line", 1, 1, "余额宝", 0.9123))
            progress_cb(("organize",))
        return parsed

    monkeypatch.setattr(ss, "parse_screenshot", fake_parse)
    monkeypatch.setattr(ss, "get_group_existing", lambda db_, prefix: [])

    img = tmp_path / "upload.jpg"
    write_img(img)
    jid = preview_jobs.submit(shots.run_preview_job(
        str(img), "sha", "bantuou", "keep", "upload.jpg"))
    snap = wait_job(jid)
    assert snap["status"] == "done", snap["error"]
    assert snap["ocr"] == {"chunk_index": 0, "chunk_total": 1,
                           "line_done": 1, "line_total": 1}
    # 实时原始行：文本/置信度（三位小数）/所属片号，顺序与产出一致
    assert snap["lines"] == [{"t": "余额宝", "s": 0.912, "c": 0}]
    result = snap["result"]
    # 与 POST /preview 的 ScreenshotPreviewResponse 逐字段一致的形状
    assert result["source_group"] == "bantuou"
    assert result["data_date"] == "2026-09-13"
    assert result["existing_count"] == 0
    assert result["image_filename"] == "upload.jpg"
    assert len(result["rows"]) == 1 and result["rows"][0]["action"] == "create"
    assert not img.exists()  # worker 已清理临时文件


def test_run_preview_job_error_keeps_no_temp_leak(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ocr exploded")
    monkeypatch.setattr(shots, "_run_pipeline_sync", boom)
    img = tmp_path / "upload.jpg"
    write_img(img)
    jid = preview_jobs.submit(shots.run_preview_job(
        str(img), "sha", "g", "keep", "f.jpg"))
    snap = wait_job(jid)
    assert snap["status"] == "error" and "ocr exploded" in snap["error"]
    assert not img.exists()


# =====================================================================
# lines（实时原始行）：累积顺序 / 1000 上限 / pipeline 路线恒空
# =====================================================================
class _FakeDb:
    def close(self):
        pass


def test_lines_cap_drops_oldest_keeps_counts(tmp_path, monkeypatch):
    parsed = ParsedScreenshot(items=[], total=None, date=None)
    preview = shots.screenshot_service.compute_preview([], [], "A-")

    def fake_sync(path, sha, sg, db, unrecognized="keep", progress_cb=None,
                  force_ocr=False):
        progress_cb(("chunk", 0, 2))
        for i in range(1, 1003):           # 第 0 片 1002 行（超上限 2 条）
            progress_cb(("line", i, 1002, f"t{i}", 0.5))
        progress_cb(("chunk", 1, 2))
        progress_cb(("line", 1, 2, "tail", 0.4))
        return parsed, [], preview, "A-"

    monkeypatch.setattr(shots, "_run_pipeline_sync", fake_sync)
    monkeypatch.setattr(shots, "SessionLocal", _FakeDb)
    captured = {}
    shots.run_preview_job("p", "s", "g", "keep", "f")(lambda **f: captured.update(f))

    lines = captured["lines"]
    assert len(lines) == 1000                     # 上限封顶
    assert lines[0] == {"t": "t4", "s": 0.5, "c": 0}    # 最旧 3 条已丢
    assert lines[-1] == {"t": "tail", "s": 0.4, "c": 1} # 新片的行 c=片号 1
    assert [l["t"] for l in lines[-3:]] == ["t1001", "t1002", "tail"]  # 顺序保持
    # 计数照常（不受丢行影响，按最后一组事件）：chunk1 内 line_done=1/total=2
    assert captured["ocr"] == {"chunk_index": 1, "chunk_total": 2,
                               "line_done": 1, "line_total": 2}


def test_lines_empty_when_no_line_events(tmp_path, monkeypatch):
    """pipeline 路线只有 chunk 事件 → job.lines 全程恒空（GET 快照默认 []）。"""
    parsed = ParsedScreenshot(items=[], total=None, date=None)
    preview = shots.screenshot_service.compute_preview([], [], "A-")

    def fake_sync(path, sha, sg, db, unrecognized="keep", progress_cb=None,
                  force_ocr=False):
        for i in range(3):
            progress_cb(("chunk", i, 3))
        return parsed, [], preview, "A-"

    monkeypatch.setattr(shots, "_run_pipeline_sync", fake_sync)
    monkeypatch.setattr(shots, "SessionLocal", _FakeDb)
    img = tmp_path / "u.jpg"
    write_img(img)
    snap = wait_job(preview_jobs.submit(
        shots.run_preview_job(str(img), "s", "g", "keep", "u.jpg")))
    assert snap["status"] == "done"
    assert snap["lines"] == []
