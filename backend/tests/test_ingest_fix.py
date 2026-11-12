"""v5 一期·步骤 6「ingest 微调」测试（spec §5）。

存证前置（preview 即落盘建行 / 同 sha 去重 / apply 富化 / 孤儿判定 / 500 阻断无泄漏）、
任务落库（submit 即有行 / 续跑 / 进度节流 / 轮询形状回落 DB）、0011 双跑幂等。
识别一律 monkeypatch（不跑真 OCR）；证据/任务库全走 tmp_path，绝不触碰真实 data/。
数据合成（示例- 前缀、虚构机构）。
"""
import asyncio
import hashlib
import io
import json
import os
import sqlite3
import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401
import routers.screenshots as shots
from database import Base, get_db
from models import Asset, AuditLog, ScreenshotEvidence
from errors import ApiError, ErrorCode
from services import preview_jobs, screenshot_service
from services.screenshot_service import ParsedItem, ParsedScreenshot

PREFIX = "示例-"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
arun = asyncio.run


def _sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """文件库（含 0011 preview_jobs 表）+ evidence_root/SessionLocal 指向 tmp。"""
    from services.db_migration_service import _MIGRATIONS_DIR
    import importlib.util
    db_file = tmp_path / "ingest.db"
    engine = create_engine(f"sqlite:///{db_file}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    # 0029 起凭证只存 `source_group_id`：真库启动时同样会种默认组，先种再跑用例
    _seed = sessionmaker(bind=engine)()
    screenshot_service.seed_default_source_groups(_seed)
    _seed.close()
    spec = importlib.util.spec_from_file_location(
        "m0011_store", f"{_MIGRATIONS_DIR}/0011_preview_jobs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    conn = sqlite3.connect(db_file)
    conn.isolation_level = None
    conn.execute("BEGIN")
    m.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    monkeypatch.setattr(shots, "SessionLocal", Session)
    yield engine, Session, str(tmp_path / "ev")
    preview_jobs.reset_state()


def _upload(content=PNG, name="shot.png"):
    return UploadFile(filename=name, file=io.BytesIO(content))


@pytest.fixture(autouse=True)
def _default_source_group(db):
    """0029 起凭证只存 `source_group_id`：上传存证前必须有组行（真库靠启动种子）。"""
    screenshot_service.seed_default_source_groups(db)


def _fake_pipeline(monkeypatch, parsed=None):
    """替识别替身：不跑 OCR/组内查询，返回给定 parsed。"""
    ss = screenshot_service
    monkeypatch.setattr(ss, "get_group_config",
                        lambda db_, key: {"asset_prefix": PREFIX, "account_ids": [],
                                          "rules": None})
    monkeypatch.setattr(ss, "parse_screenshot",
                        lambda *a, **k: parsed or ParsedScreenshot(
                            items=[ParsedItem(name="示例基金",
                                              amount=Decimal("100"))],
                            total=None, date="2026-09-13", raw_blocks=[]))
    monkeypatch.setattr(ss, "get_group_existing", lambda db_, prefix: [])


def confirms(*keys) -> dict:
    """D4：create 行必须显式表态（走路由时拿不到 preview 行对象，按预览行号表态）。"""
    return {str(k): {"action": "create"} for k in keys}


# ---------------------------------------------------------------- 0011 幂等

def test_0011_double_run_idempotent(tmp_path):
    db_file = tmp_path / "m.db"
    conn = sqlite3.connect(db_file)
    conn.isolation_level = None
    import importlib.util
    from services.db_migration_service import _MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m0011b", f"{_MIGRATIONS_DIR}/0011_preview_jobs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")  # 二次无异常
    assert conn.execute("SELECT COUNT(*) FROM preview_jobs").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------- 存证前置

def test_persist_upload_creates_evidence_row_and_file(tmp_path, db, monkeypatch):
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    tmp, sha, ev_id = shots.persist_upload(_upload(PNG), "bantuou", db)
    ev = db.get(ScreenshotEvidence, ev_id)
    assert ev.file_sha256 == sha
    assert ev.data_date is None            # 存证时数据日期未知
    assert ev.engine is None
    assert ev.file_path.startswith(f"{date.today().year}/")  # 上传年月落位
    dest = tmp_path / "ev" / ev.file_path
    assert dest.exists() and _sha(dest.read_bytes()) == sha
    os.remove(tmp)


def test_same_sha_three_uploads_one_row_one_file(tmp_path, db, monkeypatch):
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    ids = []
    for _ in range(3):
        tmp, _sha_, ev_id = shots.persist_upload(_upload(PNG), "bantuou", db)
        ids.append(ev_id)
        os.remove(tmp)
    assert len(set(ids)) == 1
    assert db.query(ScreenshotEvidence).count() == 1
    ev = db.query(ScreenshotEvidence).one()
    files = list((tmp_path / "ev").rglob("*.png"))
    assert len(files) == 1


def test_preview_route_attaches_evidence_id(db, tmp_path, monkeypatch):
    """preview 后即有凭证行（data_date NULL）＋响应带 evidence_id（additive）。"""
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    _fake_pipeline(monkeypatch)
    resp = arun(shots.screenshot_preview(
        file=_upload(), source_group="bantuou", unrecognized="keep",
        force_ocr=False, engine="ocr", recognition="", db=db))
    assert resp.evidence_id is not None
    ev = db.get(ScreenshotEvidence, resp.evidence_id)
    assert ev.data_date is None
    assert ev.file_path  # 落盘成功


def test_apply_enriches_same_evidence_and_audit_ref(db, tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    _fake_pipeline(monkeypatch)
    prev = arun(shots.screenshot_preview(
        file=_upload(), source_group="bantuou", unrecognized="keep",
        force_ocr=False, engine="ocr", recognition="", db=db))
    ev_id = prev.evidence_id
    opts = {"source_group": "bantuou", "skipped": [], "amounts": {},
            "identity_decisions": confirms(0)}   # 唯一识别行 → create 行 0（D4）
    res = arun(shots.screenshot_apply(
        file=_upload(), options=json.dumps(opts), data_date="2026-01-20",
        recognition="", db=db))
    assert res.evidence_id == ev_id               # 复用同一凭证行（同 sha）
    ev = db.get(ScreenshotEvidence, ev_id)
    assert ev.data_date == date(2026, 1, 20)     # 富化：会话属性补齐
    assert db.query(ScreenshotEvidence).count() == 1
    audit = db.execute(select(AuditLog).where(
        AuditLog.action == "apply_row")).scalars().all()
    assert audit and all(r.evidence_ref == ev_id for r in audit)


def test_second_apply_overrides_session_attrs_file_immutable(db, tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    _fake_pipeline(monkeypatch)
    opts = json.dumps({"source_group": "bantuou",
                       "identity_decisions": confirms(0)})  # 每次都是 create 行 0（D4）
    r1 = arun(shots.screenshot_apply(file=_upload(), options=opts,
                                     data_date="2026-01-20", recognition="", db=db))
    ev = db.get(ScreenshotEvidence, r1.evidence_id)
    path1, sha1, created1 = ev.file_path, ev.file_sha256, ev.created_at
    r2 = arun(shots.screenshot_apply(file=_upload(), options=opts,
                                     data_date="2026-03-05", recognition="", db=db))
    assert r2.evidence_id == r1.evidence_id
    ev = db.get(ScreenshotEvidence, r1.evidence_id)
    assert ev.data_date == date(2026, 3, 5)          # 会话属性随最近采纳
    assert (ev.file_path, ev.file_sha256, ev.created_at) == (path1, sha1, created1)  # 文件本体不可变


def test_orphans_query(db, tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_evidence_root", lambda: str(tmp_path / "ev"))
    _fake_pipeline(monkeypatch)
    prev = arun(shots.screenshot_preview(file=_upload(), source_group="bantuou",
                                         unrecognized="keep", force_ocr=False,
                                         engine="ocr", recognition="", db=db))
    out = shots.list_orphan_evidence(db=db)
    assert out["total"] == 1 and out["items"][0]["id"] == prev.evidence_id
    arun(shots.screenshot_apply(file=_upload(),
                                options=json.dumps({"source_group": "bantuou",
                                                    "identity_decisions": confirms(0)}),
                                data_date="2026-01-20", recognition="", db=db))
    assert shots.list_orphan_evidence(db=db)["total"] == 0   # apply 后脱离孤儿集


def test_persist_failure_blocks_500_no_tempfile_leak(db, tmp_path, monkeypatch):
    tmpfile = tmp_path / "leak.png"
    tmpfile.write_bytes(PNG)
    monkeypatch.setattr(shots, "_save_upload", lambda f: (str(tmpfile), "deadbeef"))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(screenshot_service, "get_or_create_evidence", boom)
    with pytest.raises(ApiError) as ei:
        arun(shots.screenshot_preview(file=_upload(), source_group="bantuou",
                                      unrecognized="keep", force_ocr=False,
                                      engine="ocr", recognition="", db=db))
    assert ei.value.status_code == 500
    assert ei.value.code == ErrorCode.EVIDENCE_WRITE_FAILED
    assert not tmpfile.exists()                        # tempfile 未泄漏
    assert db.query(ScreenshotEvidence).count() == 0  # 识别未执行、无半行


# ---------------------------------------------------------------- 任务落库

def test_submit_inserts_queued_row_then_done(store, monkeypatch):
    _engine, Session, _root = store
    preview_jobs.configure(session_factory=Session)
    db = Session()
    jid = preview_jobs.submit(
        lambda report: report(status="running", stage="ocr") or "R",
        payload={"evidence_id": 1, "sha": "s"}, db=db)
    row = db.execute(text("SELECT status FROM preview_jobs WHERE job_id=:i"),
                     {"i": jid}).mappings().first()
    # submit 即有行；worker 可能已瞬时拉起（queued/running 均证 INSERT 生效）
    assert row and row["status"] in ("queued", "running")
    db.close()
    snap = _wait_job(jid)
    assert snap["status"] == "done" and snap["result"] == "R"
    st, result = _wait_db_status(Session, jid)
    assert st == "done" and result == "R"


def test_progress_throttle_not_per_line(store):
    _engine, Session, _root = store
    preview_jobs.configure(session_factory=Session)
    updates = {"n": 0}
    orig = preview_jobs._update_row

    def counting(job_id, fields):
        updates["n"] += 1
        orig(job_id, fields)
    preview_jobs._update_row = counting

    def work(report):
        report(status="running", stage="ocr")
        for i in range(1, 51):
            report(stage="ocr", ocr={"chunk_index": 0, "chunk_total": 1,
                                     "line_done": i, "line_total": 50})
        return "DONE"
    db = Session()
    jid = preview_jobs.submit(work, payload={"evidence_id": 1, "sha": "s"}, db=db)
    db.close()
    _wait_job(jid)
    preview_jobs._update_row = orig
    assert updates["n"] < 51                    # 50 次 line 上报被合并
    ocr = preview_jobs.get(jid)["ocr"]
    assert ocr["line_done"] == 50               # 终态进度不丢


def test_poll_contract_fields_and_db_fallback(store):
    _engine, Session, _root = store
    preview_jobs.configure(session_factory=Session)
    db = Session()
    jid = preview_jobs.submit(lambda report: "X",
                               payload={"evidence_id": 1, "sha": "s"}, db=db)
    db.close()
    _wait_job(jid)
    _wait_db_status(Session, jid)   # 等 DB 落终态再淘汰内存，验证 DB 回落路径
    # 内存淘汰后 → get() 回落 DB，字段形状与冻结契约逐键一致
    with preview_jobs._LOCK:
        preview_jobs._JOBS.pop(jid, None)
    snap = preview_jobs.get(jid)
    assert set(snap) == {"id", "status", "stage", "ocr", "lines", "queue_position",
                         "elapsed", "result", "error"}
    assert snap["status"] == "done" and snap["result"] == "X"


def test_resume_running_from_evidence_path(store, tmp_path, monkeypatch):
    """续跑演练：job running 中重启（重调恢复钩子）→ 从凭证 file_path 重跑至 done。"""
    _engine, Session, root = store
    preview_jobs.configure(session_factory=Session,
                           work_factory=shots.work_from_payload)
    # 先造一个已存证凭证（图落在 evidence 目录）
    tmpf = tmp_path / "src.png"
    tmpf.write_bytes(PNG)
    ev_id = screenshot_service.get_or_create_evidence(
        Session(), _sha(PNG), str(tmpf), "bantuou", "src.png", root)
    payload = {"evidence_id": ev_id, "sha": _sha(PNG), "source_group": "bantuou",
               "unrecognized": "keep", "filename": "src.png", "force_ocr": False,
               "engine": "ocr"}
    db = Session()
    stale = "stale-running-1"
    db.execute(text("INSERT INTO preview_jobs (job_id, status, stage, payload_json, created_at,"
                    " started_at) VALUES (:i,'running','ocr',:p,:c,:c)"),
               {"i": stale, "p": json.dumps(payload),
                "c": "2026-01-01T00:00:00"})
    db.commit()
    db.close()
    # 识别替身：续跑无需真 OCR
    _fake_pipeline(monkeypatch)
    monkeypatch.setattr(shots, "_run_pipeline_sync", lambda *a, **k: (
        ParsedScreenshot(items=[], total=None, date=None, raw_blocks=[]),
        [], screenshot_service.compute_preview([], [], PREFIX), PREFIX))
    n = preview_jobs.resume_pending()
    assert n == 1
    db = Session()
    st = db.execute(text("SELECT status FROM preview_jobs WHERE job_id=:i"),
                    {"i": stale}).scalar()
    db.close()
    # 陈旧 running 已被复位重入队：此刻可能尚在队列（queued）、已被 worker
    # 拉起（running）或（识别替身秒速）直接跑完（done）——三者均证明恢复生效，
    # 真正锁死语义的是下方 done + result + 原件未删断言
    assert st in ("queued", "running", "done")
    snap = _wait_job(stale)
    assert snap["status"] == "done"
    assert snap["result"]["evidence_id"] == ev_id
    # 凭证原件仍在（续跑不得删凭证文件）
    sess = Session()
    ev = sess.get(ScreenshotEvidence, ev_id)
    sess.close()
    assert (tmp_path / "ev" / ev.file_path).exists()


def _wait_job(jid, timeout=5.0):
    """轮询到 done/error 或超时（工作线程在独立线程跑，读走 get→DB 回落）。"""
    end = time.time() + timeout
    while time.time() < end:
        snap = preview_jobs.get(jid)
        if snap and snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {jid} 未在超时内结束")


def _wait_db_status(Session, jid, timeout=5.0):
    """轮询 DB 行到 done/error（worker 先更新内存、DB 写在同一 report 内随后完成，
    内存可见 done 不保证 DB 已落终态——避免断言竞态）。"""
    end = time.time() + timeout
    while time.time() < end:
        db = Session()
        st = db.execute(text("SELECT status FROM preview_jobs WHERE job_id=:i"),
                        {"i": jid}).scalar()
        r = db.execute(text("SELECT result_json FROM preview_jobs WHERE job_id=:i"),
                       {"i": jid}).mappings().first()
        db.close()
        if st in ("done", "error"):
            return st, (json.loads(r["result_json"]) if r and r["result_json"] else None)
        time.sleep(0.05)
    raise AssertionError(f"job {jid} DB 未在超时内落终态")
