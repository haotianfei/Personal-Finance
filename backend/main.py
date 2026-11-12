import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi import HTTPException
import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from logging_conf import setup_logging

setup_logging()

logger = structlog.get_logger("main")

from database import engine, Base, DB_PATH, DB_ROLE
from errors import ApiError, build_envelope_from_detail, envelope, ErrorCode
from routers import assets, analysis, imports, dimensions, management, classification, exports, proportion, liquidity_ratings, alerts, allocations, export_history, asset_owners, backup, attributes, screenshots, screenshot_batches, source_groups, llm_providers, expense_categories, expense_templates, expenses, asset_items, audit_log, transfer_events, lineage_links, metrics, goals, advisor, reviews, parser
from services.scheduler_service import init_scheduler, shutdown_scheduler
from services.auto_export_service import load_auto_export_rules
from services.db_migration_service import migrate_database
from services.idempotency_service import (
    IdempotencyCaptureMiddleware, ReplayStored)


http_logger = structlog.get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = rid
            http_logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 连的是哪套库：路径 + 角色（dev/staging/prod/unknown），可 grep 的事实
    # （spec .scratch/db-dev-suffix D3）；只打日志，不做启动硬失败。
    logger.info("database_role", db_path=DB_PATH, db_role=DB_ROLE)

    # 确保数据目录存在
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)
    
    # 创建数据库表（新表）
    Base.metadata.create_all(bind=engine)

    # 数据库迁移（runner：schema_version 记账 + 增量 + 前置备份）→ seed
    # fail-fast：迁移/备份失败即中止启动（不再"仅记日志照常启动"），避免在半迁移库上跑业务。
    report = migrate_database()
    logger.info(
        "startup_migration_completed",
        applied=report.applied,
        baseline=report.baseline,
        noop=report.noop,
    )
    
    # 初始化调度器
    init_scheduler()

    # v5 步骤 6：启用 preview_jobs 持久层 + 重启续跑（扫 queued/running 重入队，
    # 从凭证 file_path 取图重跑）。恢复失败仅告警、不阻塞启动（降级不阻塞）。
    try:
        from routers import screenshots as _screenshots
        n = _screenshots.init_jobs_recovery()
        if n:
            logger.info("preview_jobs_recovery", resumed=n)
    except Exception:
        logger.exception("preview_jobs_recovery_failed")

    # 三期-3 W4：advisor 任务续跑（queued/running → 复位并重跑 generating）
    try:
        from database import SessionLocal
        from services.advisor import service as advisor_service
        with SessionLocal() as _db:
            resumed = advisor_service.resume_pending(_db)
        if resumed:
            logger.info("advisor_jobs_recovery", resumed=resumed)
    except Exception:
        logger.exception("advisor_jobs_recovery_failed")
    
    # 加载自动导出规则
    try:
        rule_count = load_auto_export_rules()
        logger.info("startup_auto_export_rules_loaded", rule_count=rule_count)
    except Exception:
        logger.exception("startup_auto_export_rules_failed")
    
    yield
    
    # 关闭调度器
    shutdown_scheduler()


app = FastAPI(title="个人资产管理系统", version="5.5.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 幂等 done 捕获：只对 guard 标记过的请求缓冲响应（选择性，见 idempotency_service）
app.add_middleware(IdempotencyCaptureMiddleware)
# 后注册者为外层：RequestContext 包在 CORS 外，保证所有请求都绑定 request_id
app.add_middleware(RequestContextMiddleware)


def register_exception_handlers(app: FastAPI):
    """v5 契约层（spec §2）：全部非 2xx 统一错误信封；测试可复用本函数。"""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        logger.warning("api_error", code=exc.code, status=exc.status_code,
                       path=request.url.path)
        return JSONResponse(status_code=exc.status_code,
                            content=envelope(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e.get("loc", [])),
                   "message": str(e.get("msg", ""))} for e in exc.errors()]
        return JSONResponse(status_code=422, content=envelope(
            ErrorCode.VALIDATION_ERROR, "请求参数校验失败", {"fields": fields}))

    @app.exception_handler(ReplayStored)
    async def _replay_handler(request: Request, exc: ReplayStored):
        # 同键同体：逐字节回放首次响应（原状态码＋原 body）
        return Response(content=exc.body, status_code=exc.status,
                        media_type="application/json")

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # 存量 176 处不逐一改造：含 code 的 dict 透传 / 准信封精确映射 /
        # 字符串按状态码转码，无映射归 GENERIC_<status>
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(build_envelope_from_detail(exc.status_code, exc.detail)),
            headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("unhandled_error", path=request.url.path)
        return JSONResponse(status_code=500,
                            content=envelope(ErrorCode.INTERNAL, "服务器内部错误", None))


register_exception_handlers(app)

app.include_router(assets.router, prefix="/api/assets", tags=["资产记录"])
app.include_router(asset_items.router, prefix="/api/asset-items", tags=["资产项"])
app.include_router(classification.item_router, prefix="/api/asset-items", tags=["资产项·分类"])
app.include_router(classification.router, prefix="/api/classification", tags=["资产分类"])
app.include_router(audit_log.router, prefix="/api/audit-log", tags=["审计日志"])
app.include_router(transfer_events.router, prefix="/api/transfer-events", tags=["转移事件"])
app.include_router(lineage_links.router, prefix="/api/lineage-links", tags=["血缘链接"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["分析"])
app.include_router(metrics.router, prefix="/api/metrics", tags=["指标"])
app.include_router(goals.router, prefix="/api/planning", tags=["规划·目标"])
app.include_router(advisor.router, prefix="/api/planning/advice", tags=["规划·建议"])
app.include_router(reviews.router, prefix="/api/planning/reviews", tags=["规划·复盘"])
app.include_router(imports.router, prefix="/api/imports", tags=["导入"])
app.include_router(exports.router, prefix="/api/exports", tags=["导出"])
app.include_router(dimensions.router, prefix="/api/dimensions", tags=["维度"])
app.include_router(management.router, prefix="/api/management", tags=["基础数据管理"])
app.include_router(proportion.router, prefix="/api/proportions", tags=["占比分析"])
app.include_router(liquidity_ratings.router, prefix="/api/liquidity-ratings", tags=["流动性评级"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["预警"])
app.include_router(allocations.router, prefix="/api/allocation", tags=["资产配置"])
app.include_router(export_history.router, prefix="/api/export-history", tags=["导出历史"])
app.include_router(asset_owners.router, prefix="/api/asset-owners", tags=["资产所有者"])
app.include_router(backup.router, prefix="/api/backup", tags=["备份与恢复"])
app.include_router(attributes.router, prefix="/api/attributes", tags=["属性管理"])
app.include_router(screenshots.router, prefix="/api/screenshots", tags=["截图录入"])
app.include_router(screenshot_batches.router, prefix="/api/screenshot-batches", tags=["截图批次"])
app.include_router(source_groups.router, prefix="/api/source-groups", tags=["来源组管理"])
app.include_router(parser.router, prefix="/api/parser", tags=["解析词表"])
app.include_router(llm_providers.router, prefix="/api/llm-providers", tags=["LLM供应商"])
app.include_router(expense_categories.router, prefix="/api/expense-categories", tags=["支出分类"])
app.include_router(expense_templates.router, prefix="/api/expense-templates", tags=["固定支出项目"])
app.include_router(expenses.router, prefix="/api/expenses", tags=["支出记录"])

# 来源组示例图静态服务：URL 必须在 /api 前缀下（前端 next.config 以
# /api/:path* rewrite 转发，Docker/本地皆成立）；目录与 routers/source_groups
# 上传落盘共用 guide_image_root()（项目根/data/guide_images，__file__ 绝对路径
# 约定），StaticFiles 要求目录存在故先 mkdir。
_GUIDE_IMAGES_DIR = source_groups.guide_image_root()
os.makedirs(_GUIDE_IMAGES_DIR, exist_ok=True)
app.mount("/api/static/guide-images",
          StaticFiles(directory=_GUIDE_IMAGES_DIR), name="guide-images")


@app.get("/api/health")
def health_check():
    return {"status": "ok"}
