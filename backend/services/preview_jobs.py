"""截图预览异步任务：进程内单 worker FIFO ＋ DB 落库（v5 步骤 6 升级）。

契约（GET /api/screenshots/preview-jobs/{id} 响应，字段冻结）：
    {id, status: queued|running|done|error,
     stage: queued|cache|ocr|organize|match|done,
     ocr: {chunk_index, chunk_total, line_done, line_total} | null,
     lines: [{t, s, c}],           # 已识别原始行（t文本/s置信度/c片号），上限
                                    # 1000 条超出丢最旧，由 work 经 report 透传
     queue_position, elapsed, result, error}

设计（内存层，同既往）：
- 模块级 dict + RLock；job id = uuid4 hex，FIFO（单调 seq 定队列位次）。
- 单 worker 线程惰性启动，串行跑 work——根治多图并发共享无锁 OCR 引擎竞态。
- work 闭包 work(report) 在工作线程执行；report(**fields) 增量合并；返回即
  job.result；异常 → error 终态。
- 内存保留 _MAX_JOBS=50 / _TTL=600s（purge 只清已结束；DB 行由 cleanup 7 天清理，
  内存淘汰后 get() 回落 DB，轮询不断）。

设计（持久层，configure 后启用；未 configure＝纯内存，行为与既往逐字节一致）：
- submit(work, payload, db)：INSERT queued 行；**INSERT 失败 → 抛出（路由 500，同现状）**。
- report 节流落库：status/stage 变迁必写；ocr/lines 高频进度按「≥2s 或 ≥20 行」
  合并写（WAL 短事务）；done/error 终态必写 result_json/error/finished_at。
- 进度落库失败仅 warning，内存态照常（降级不阻塞）。
- resume_pending()：lifespan 启动钩子扫 queued/running → running 复位 queued
  （陈旧态）→ 按 created_at 用 work_factory(payload) 重建闭包重入队
  （识别无副作用、preview 只读，天然幂等；OCR 缓存按 sha 命中即快）。
- cleanup_expired()：done/error 且 finished_at > 7 天删行（scheduler 日任务）。
- get()：内存 miss 且已 configure → 查 DB 重建同形快照（重启后旧 id 不再 404；
  仅 DB 也无 → None，路由 404 依旧）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_MAX_JOBS = 50
_TTL_SECONDS = 600.0
_DB_TTL_DAYS = 7
# 进度节流：非 status/stage 变迁时，≥2s 或 line_done 增量 ≥20 才合并写
_DB_FLUSH_SECONDS = 2.0
_DB_FLUSH_LINES = 20

_LOCK = threading.RLock()
_JOBS: dict[str, dict] = {}
_ORDER: deque[str] = deque()          # 提交序（FIFO），含内部字段
_QUEUE: deque[str] = deque()          # 待执行 job id
_QUEUE_COND = threading.Condition(_LOCK)
_SEQ = 0
_WORKER: Optional[threading.Thread] = None

# 持久层配置（configure 注入；None＝纯内存回退）
_CFG: dict[str, Any] = {"session_factory": None, "work_factory": None}


def configure(session_factory=None, work_factory=None) -> None:
    """启用/更新持久层：session_factory 产出可执行裸 SQL 的 Session；
    work_factory(payload: dict) -> work 闭包（续跑重建）。"""
    _CFG["session_factory"] = session_factory
    _CFG["work_factory"] = work_factory


def _persist_enabled() -> bool:
    return _CFG["session_factory"] is not None


def reset_state() -> None:
    """测试隔离用：清空模块态并禁用持久层（生产不调用）。"""
    global _SEQ
    with _LOCK:
        _JOBS.clear()
        _ORDER.clear()
        _QUEUE.clear()
        _SEQ = 0
        _CFG["session_factory"] = None
        _CFG["work_factory"] = None


# ==================== 提交 / 上报 / 查询 ====================

def submit(work: Callable[[Callable[[dict], None]], Any],
           payload: Optional[dict] = None, db=None) -> str:
    """登记一个任务并惰性启动 worker，返回 job id。

    payload（续跑参数）给且持久层启用 → 先 INSERT(queued)（失败上抛，不落内存），
    再入内存队；db 传调用方会话（路由 get_db），否则自开短事务。
    """
    global _WORKER, _SEQ
    job_id = uuid.uuid4().hex
    now = time.time()
    persist = payload is not None and _persist_enabled()
    if persist:
        _insert_row(job_id, payload, _iso(now), db)
    with _LOCK:
        _SEQ += 1
        _JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "ocr": None,
            "lines": [],
            "result": None,
            "error": None,
            "_seq": _SEQ,
            "_created": now,
            "_finished": None,
            "_work": work,
            "_persist": persist,
            "_db_state": ("queued", "queued") if persist else None,
            "_db_ts": now,
            "_db_line": 0,
            "_db_started": False,
        }
        _ORDER.append(job_id)
        _QUEUE.append(job_id)
        _purge_locked()
        _ensure_worker_locked()
    return job_id


def report(job_id: str, **fields) -> None:
    """增量合并 job 进度字段（work(report) 的 report 实现）。未知 id 忽略。

    持久层启用时按节流策略落库（锁外写，避免长事务阻塞内存路径）。
    """
    write_fields = None
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        if job.get("_persist"):
            write_fields = _throttle_locked(job)
    if write_fields is not None:
        _safe_persist(job_id, write_fields)


def get(job_id: str) -> Optional[dict]:
    """任务快照（对外冻结形状）；内存 miss 回落 DB；均无 → None。"""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            return {
                "id": job["id"],
                "status": job["status"],
                "stage": job["stage"],
                "ocr": dict(job["ocr"]) if job["ocr"] else None,
                "lines": list(job["lines"]),
                "queue_position": _queue_position_locked(job),
                "elapsed": round(
                    (job["_finished"] if job["_finished"] is not None
                     else time.time()) - job["_created"], 2),
                "result": job["result"],
                "error": job["error"],
            }
    if _persist_enabled():
        return _db_get(job_id)
    return None


# ==================== 内部：内存机制（同既往） ====================

def _queue_position_locked(job: dict) -> int:
    """排队位次：0=执行中/已完成；queued 时 = 排在其前且未结束的任务数。"""
    if job["status"] != "queued":
        return 0
    pos = 0
    for other_id in list(_ORDER):
        other = _JOBS.get(other_id)
        if other is None or other is job:
            continue
        if other["_seq"] < job["_seq"] and other["status"] in ("queued", "running"):
            pos += 1
    return pos


def _purge_locked() -> None:
    """内存 TTL + 上限清理（只清已结束任务；DB 行由 cleanup_expired 管）。"""
    now = time.time()
    for job_id in list(_ORDER):
        job = _JOBS.get(job_id)
        if job is None:
            _ORDER.remove(job_id)
            continue
        if job["_finished"] is not None and now - job["_finished"] > _TTL_SECONDS:
            _JOBS.pop(job_id, None)
            _ORDER.remove(job_id)
    done_jobs = [(_JOBS[i], i) for i in list(_ORDER)
                 if i in _JOBS and _JOBS[i]["_finished"] is not None]
    done_jobs.sort(key=lambda t: t[0]["_finished"])
    while len(_JOBS) > _MAX_JOBS and done_jobs:
        _, victim_id = done_jobs.pop(0)
        _JOBS.pop(victim_id, None)
        if victim_id in _ORDER:
            _ORDER.remove(victim_id)


def _ensure_worker_locked() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker_loop, name="preview-jobs",
                                   daemon=True)
        _WORKER.start()
    else:
        _QUEUE_COND.notify()


def _worker_loop() -> None:
    """单线程 FIFO：取 job → running（report 驱动节流/变迁落库）→ work → done/error。"""
    while True:
        with _LOCK:
            while not _QUEUE:
                _QUEUE_COND.wait()
            job_id = _QUEUE.popleft()
            job = _JOBS.get(job_id)
            if job is None:
                continue
        # status 变迁走 report → 持久层在 transition 时必写（started_at 一并）
        report(job_id, status="running")
        work = job.get("_work")
        try:
            result = work(lambda **fields: report(job_id, **fields))
            report(job_id, status="done", stage="done", result=result)
        except Exception as e:  # 兜底：任何异常落 error，绝不让 worker 线程死掉
            report(job_id, status="error", stage="done", error=str(e))
        finally:
            with _LOCK:
                j = _JOBS.get(job_id)
                if j is not None:
                    j["_finished"] = time.time()
                _purge_locked()


# ==================== 内部：持久层 ====================

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat()


def _extract_db_fields(job: dict, *, terminal: bool) -> dict:
    """内存 job → preview_jobs 列更新字典（None 值 = 不覆盖已有列）。"""
    f: dict[str, Any] = {"status": job["status"], "stage": job["stage"]}
    f["ocr_json"] = json.dumps(job["ocr"], ensure_ascii=False) if job.get("ocr") else None
    f["lines_json"] = json.dumps(job.get("lines") or [], ensure_ascii=False)
    if terminal:
        f["result_json"] = (json.dumps(job["result"], ensure_ascii=False)
                            if job.get("result") is not None else None)
        f["error"] = job.get("error")
        f["finished_at"] = _iso(job["_finished"] if job.get("_finished") else time.time())
    if job["status"] == "running" and not job.get("_db_started"):
        f["started_at"] = _iso(time.time())
    return f


def _throttle_locked(job: dict) -> Optional[dict]:
    """锁内判定是否落库并返回字段包（调用方锁外写）。status/stage 变迁必写；
    终态必写；进度按 ≥2s 或 ≥20 行合并写。"""
    st = (job["status"], job["stage"])
    terminal = job["status"] in ("done", "error")
    now = time.time()
    line_done = (job.get("ocr") or {}).get("line_done", 0)
    due = (terminal
           or st != job.get("_db_state")
           or now - job.get("_db_ts", job["_created"]) >= _DB_FLUSH_SECONDS
           or line_done - job.get("_db_line", 0) >= _DB_FLUSH_LINES)
    if not due:
        return None
    fields = _extract_db_fields(job, terminal=terminal)
    job["_db_state"] = st
    job["_db_ts"] = now
    job["_db_line"] = line_done
    if fields.get("started_at"):
        job["_db_started"] = True
    if terminal:
        job["_finished"] = job["_finished"] or now
        fields["finished_at"] = _iso(job["_finished"])
    return fields


def _safe_persist(job_id: str, fields: dict) -> None:
    """进度落库失败仅 warning（内存态照常，轮询不断）。"""
    try:
        _update_row(job_id, fields)
    except Exception as e:
        logger.warning("preview_job_persist_failed job=%s err=%s", job_id, e)


def _update_row(job_id: str, fields: dict) -> None:
    from sqlalchemy import text
    sf = _CFG["session_factory"]
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    params = dict(fields)
    params["id"] = job_id
    with sf() as db:
        db.execute(text(f"UPDATE preview_jobs SET {sets} WHERE job_id = :id"), params)
        db.commit()


def _insert_row(job_id: str, payload: dict, created_iso: str, db=None) -> None:
    from sqlalchemy import text
    params = {
        "id": job_id,
        "payload": json.dumps(payload, ensure_ascii=False),
        "created": created_iso,
    }
    stmt = text(
        "INSERT INTO preview_jobs (job_id, status, stage, payload_json, created_at)"
        " VALUES (:id, 'queued', 'queued', :payload, :created)"
        " ON CONFLICT(job_id) DO NOTHING")
    if db is not None:
        db.execute(stmt, params)
        db.commit()
    else:
        with _CFG["session_factory"]() as own:
            own.execute(stmt, params)
            own.commit()


def _row_to_snapshot(row, now_ts: float) -> dict:
    """DB 行 → 冻结契约形状（queue_position 按 created_at 现算）。"""
    def _j(v):
        return json.loads(v) if v else None
    created = datetime.fromisoformat(row["created_at"]).timestamp()
    fin = row["finished_at"]
    elapsed = round(((datetime.fromisoformat(fin).timestamp() if fin else now_ts)
                     - created), 2)
    return {
        "id": row["job_id"],
        "status": row["status"],
        "stage": row["stage"],
        "ocr": _j(row["ocr_json"]),
        "lines": _j(row["lines_json"]) or [],
        "queue_position": 0,   # 兜底 0；queued 时由 _db_get 现算
        "elapsed": elapsed,
        "result": _j(row["result_json"]),
        "error": row["error"],
    }


def _db_get(job_id: str) -> Optional[dict]:
    from sqlalchemy import text
    try:
        with _CFG["session_factory"]() as db:
            row = db.execute(text(
                "SELECT * FROM preview_jobs WHERE job_id = :id"), {"id": job_id}
            ).mappings().first()
            if row is None:
                return None
            snap = _row_to_snapshot(row, time.time())
            if snap["status"] == "queued":
                ahead = db.execute(text(
                    "SELECT COUNT(*) FROM preview_jobs"
                    " WHERE status IN ('queued','running') AND created_at < :c"
                    " AND job_id != :id"),
                    {"c": row["created_at"], "id": job_id}).scalar() or 0
                snap["queue_position"] = int(ahead)
            return snap
    except Exception as e:
        logger.warning("preview_job_db_get_failed job=%s err=%s", job_id, e)
        return None


# ==================== 重启续跑 / TTL 清理 ====================

def resume_pending() -> int:
    """lifespan 启动钩子：扫 queued/running → running 复位 queued → 按 created_at
    重入内存队（work_factory 用 payload 重建闭包，从凭证 file_path 取图重跑）。

    返回恢复条数；持久层未启用返回 0。单条恢复失败仅 warning（降级不阻塞启动）。
    """
    if not _persist_enabled() or _CFG["work_factory"] is None:
        return 0
    from sqlalchemy import text
    global _SEQ
    recovered = 0
    try:
        with _CFG["session_factory"]() as db:
            rows = db.execute(text(
                "SELECT * FROM preview_jobs WHERE status IN ('queued','running')"
                " ORDER BY created_at")).mappings().all()
            if not rows:
                return 0
            db.execute(text(
                "UPDATE preview_jobs SET status='queued'"
                " WHERE status='running'"))
            db.commit()
    except Exception as e:
        logger.warning("preview_jobs_resume_failed: %s", e)
        return 0
    with _LOCK:
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                work = _CFG["work_factory"](payload)
            except Exception as e:
                logger.warning("preview_jobs_resume_item_failed job=%s err=%s",
                               row["job_id"], e)
                continue
            if row["job_id"] in _JOBS:
                continue  # 理论上启动后内存为空；防御重入
            _SEQ += 1
            created = datetime.fromisoformat(row["created_at"]).timestamp()
            _JOBS[row["job_id"]] = {
                "id": row["job_id"],
                "status": "queued",
                "stage": row["stage"] or "queued",
                "ocr": json.loads(row["ocr_json"]) if row["ocr_json"] else None,
                "lines": json.loads(row["lines_json"]) if row["lines_json"] else [],
                "result": None,
                "error": None,
                "_seq": _SEQ,
                "_created": created,
                "_finished": None,
                "_work": work,
                "_persist": True,
                "_db_state": ("queued", row["stage"] or "queued"),
                "_db_ts": time.time(),
                "_db_line": (json.loads(row["ocr_json"]) or {}).get("line_done", 0)
                if row["ocr_json"] else 0,
                "_db_started": bool(row["started_at"]),
            }
            _ORDER.append(row["job_id"])
            _QUEUE.append(row["job_id"])
            recovered += 1
        if recovered:
            _ensure_worker_locked()
    if recovered:
        logger.info("preview_jobs_resumed count=%s", recovered)
    return recovered


def cleanup_expired() -> int:
    """done/error 且 finished_at > 7 天删行（scheduler 日任务）；queued/running 永不清。"""
    if not _persist_enabled():
        return 0
    from sqlalchemy import text
    cutoff = (datetime.now() - timedelta(days=_DB_TTL_DAYS)).isoformat()
    try:
        with _CFG["session_factory"]() as db:
            n = db.execute(text(
                "DELETE FROM preview_jobs"
                " WHERE status IN ('done','error') AND finished_at IS NOT NULL"
                " AND finished_at < :c"), {"c": cutoff}).rowcount
            db.commit()
            if n:
                logger.info("preview_jobs_ttl_cleaned rows=%s", n)
            return n
    except Exception as e:
        logger.warning("preview_jobs_cleanup_failed: %s", e)
        return 0
