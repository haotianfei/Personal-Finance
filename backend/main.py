import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine, Base
from routers import assets, analysis, imports, dimensions, management, exports, proportion, liquidity_ratings, alerts, allocations, export_history
from services.scheduler_service import init_scheduler, shutdown_scheduler
from services.auto_export_service import load_auto_export_rules


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 确保数据目录存在
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)
    
    # 创建数据库表
    Base.metadata.create_all(bind=engine)
    
    # 初始化调度器
    init_scheduler()
    
    # 加载自动导出规则
    try:
        rule_count = load_auto_export_rules()
        print(f"Loaded {rule_count} auto export rules")
    except Exception as e:
        print(f"Failed to load auto export rules: {e}")
    
    yield
    
    # 关闭调度器
    shutdown_scheduler()


app = FastAPI(title="个人资产管理系统", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(assets.router, prefix="/api/assets", tags=["资产记录"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["分析"])
app.include_router(imports.router, prefix="/api/imports", tags=["导入"])
app.include_router(exports.router, prefix="/api/exports", tags=["导出"])
app.include_router(dimensions.router, prefix="/api/dimensions", tags=["维度"])
app.include_router(management.router, prefix="/api/management", tags=["基础数据管理"])
app.include_router(proportion.router, prefix="/api/proportion", tags=["占比分析"])
app.include_router(liquidity_ratings.router, prefix="/api/liquidity-ratings", tags=["流动性评级"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["预警"])
app.include_router(allocations.router, prefix="/api/allocation", tags=["资产配置"])
app.include_router(export_history.router, prefix="/api/export-history", tags=["导出历史"])


@app.get("/api/health")
def health_check():
    return {"status": "ok"}
