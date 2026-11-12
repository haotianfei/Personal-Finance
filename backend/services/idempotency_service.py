"""幂等键服务（v5 步骤 5，spec §3）。

- 传递：请求头 `Idempotency-Key`（客户端每个用户动作一个 uuid；失败重试复用同键）。
- 去重：同键同体（body sha256 相等）→ **回放**首次响应（原状态码＋原 body 字节）；
  同键异体 → 422 IDEMPOTENCY_KEY_REUSED；单用户不做并发锁。
- pending 超 60s 视为 failed 可重跑；≥500 的响应记 failed（下次可重跑），
  <500（含 4xx 业务码）记 done 供回放；TTL 每日清理 >7 天（scheduler 注册）。
- 存储：`idempotency_keys`（0010）。
- 形态：`idempotency_guard(scope)` 作路由级 `dependencies=[Depends(...)]` 注入写端点
  （直接函数调用天然旁路，不扰既有测试）；done 回写由 `IdempotencyCaptureMiddleware`
  完成，复用 guard 存进 request.state 的同一 DB 会话（测试可覆盖 get_db）。
- 旁路：本层任何存储故障 → passthrough＋warning，不阻断业务
  （天然幂等唯一索引才是正确性兜底）。
- 审计衔接：`active_idem_key` contextvar 与 request_id 并列落 audit_log。
"""
import hashlib
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

PENDING_STALE_SECONDS = 60   # pending 超时视为 failed，可重跑
TTL_DAYS = 7                 # 每日清理 >7 天行

active_idem_key: ContextVar[str | None] = ContextVar("active_idem_key", default=None)


class ReplayStored(Exception):
    """同键同体命中 done：携带首次响应原样回放（main handler 渲染）。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"replay {status}")
        self.status = status
        self.body = body  # 原始响应文本（逐字节回放）


def _body_hash(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()


def _row_get(db: Session, key: str):
    return db.execute(
        text("SELECT scope, body_hash, status, response_status, response_json, created_at"
             " FROM idempotency_keys WHERE key=:k"), {"k": key}
    ).mappings().first()


def _upsert_pending(db: Session, key: str, scope: str, h: str):
    now = datetime.now().isoformat()
    db.execute(
        text("INSERT INTO idempotency_keys (key, scope, body_hash, status, created_at)"
             " VALUES (:k,:s,:h,'pending',:t)"
             " ON CONFLICT(key) DO UPDATE SET scope=:s, body_hash=:h,"
             " status='pending', response_status=NULL, response_json=NULL, created_at=:t"),
        {"k": key, "s": scope, "h": h, "t": now})
    db.commit()


def idempotency_guard(scope: str, required: bool = False):
    """路由级依赖工厂。用法：`@router.post(..., dependencies=[Depends(idempotency_guard("POST /api/assets"))])`。

    无 Idempotency-Key 头 → 默认放行（幂等层可选，天然幂等兜底正确性）；
    `required=True`（批量写等）→ 缺失时 422 VALIDATION_ERROR（details.fields）。
    """
    from database import get_db  # 局部导入避免环

    async def _guard(request: Request, db: Session = Depends(get_db)):
        key = request.headers.get("idempotency-key")
        request.state.idem_key = None
        if not key:
            if required:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR, 422,
                    "缺少 Idempotency-Key 请求头",
                    details={"fields": [{"field": "Idempotency-Key",
                                         "message": "该写端点必须携带幂等键"}]})
            return None
        try:
            # 仅对 JSON 体读原始字节做 sha256（body() 有缓存，安全）；
            # multipart 跳过读取（避免破坏 UploadFile/Form 解析），退化为键+scope 去重。
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype == "application/json" or request.method in ("GET", "HEAD", "DELETE"):
                raw = await request.body()
                h = _body_hash(raw)
            else:
                h = _body_hash(b"")
            row = _row_get(db, key)
            if row is not None and row["status"] == "done":
                if row["body_hash"] == h:
                    request.state.idem_replayed = True
                    raise ReplayStored(row["response_status"] or 200,
                                       row["response_json"] or "")
                raise ApiError(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED, 422,
                    "幂等键已被不同的请求体使用", {"key": key, "scope": scope})
            if row is not None and row["status"] == "pending":
                age = (datetime.now()
                       - datetime.fromisoformat(row["created_at"])).total_seconds()
                if age <= PENDING_STALE_SECONDS:
                    # 单用户无并发锁：in-flight 重复仅告警，交给天然幂等挡
                    logger.warning("idempotency_pending_inflight key=%s age=%.0fs", key, age)
            _upsert_pending(db, key, scope, h)
            request.state.idem_key = key
            request.state.idem_db = db        # 中间件复用同一会话（可被 get_db 覆盖）
            active_idem_key.set(key)
        except (ApiError, ReplayStored):
            raise
        except Exception as e:  # 旁路：本层故障不阻断业务
            request.state.idem_key = None
            logger.warning("idempotency_bypass scope=%s err=%s", scope, e)
        return None

    return _guard


def mark_result(db: Session, key: str, status: int, body_text: str):
    """<500 记 done（可回放）；≥500 记 failed（下次同键可重跑）。"""
    state = "done" if status < 500 else "failed"
    db.execute(
        text("UPDATE idempotency_keys SET status=:st, response_status=:s,"
             " response_json=:b WHERE key=:k"),
        {"k": key, "st": state, "s": status, "b": body_text})
    db.commit()


def cleanup_expired() -> int:
    """TTL：清理 >7 天行（复用既有 scheduler，每日一次）。"""
    from database import SessionLocal
    cutoff = (datetime.now() - timedelta(days=TTL_DAYS)).isoformat()
    try:
        with SessionLocal() as db:
            n = db.execute(
                text("DELETE FROM idempotency_keys WHERE created_at < :c"), {"c": cutoff}
            ).rowcount
            db.commit()
            if n:
                logger.info("idempotency_ttl_cleaned rows=%s", n)
            return n
    except Exception as e:
        logger.warning("idempotency_ttl_cleanup_failed: %s", e)
        return 0


def current_idem_key() -> str | None:
    return active_idem_key.get()


# ==================== done 回写（响应捕获） ====================

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import Response  # noqa: E402


class IdempotencyCaptureMiddleware(BaseHTTPMiddleware):
    """仅对 guard 标记 idem_key 且非回放的请求缓冲响应体并回写结果。

    选择性由依赖（request.state.idem_key）决定——非全量拦截；捕获失败旁路
    （行停留 pending，超时后天然幂等兜底）。
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        key = getattr(request.state, "idem_key", None)
        replayed = getattr(request.state, "idem_replayed", False)
        db = getattr(request.state, "idem_db", None)
        if not key or replayed or db is None:
            return response
        body = b""
        try:
            chunks = [c async for c in response.body_iterator]
            body = b"".join(chunks)
            mark_result(db, key, response.status_code,
                        body.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.warning("idempotency_capture_failed key=%s err=%s", key, e)
            body = b""  # 未读完则不重放内容，交天然幂等兜底
            return response
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code,
                        headers=headers, media_type=response.media_type)
