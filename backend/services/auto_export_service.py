import os
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

from database import SessionLocal, DB_PATH
from models import AutoExportRule, ExportHistory
from services import audit_service, export_service, backup_service
from services.audit_service import make_change
from services.scheduler_service import get_scheduler, get_next_run_time

# 使用与数据库相同的基础目录，确保在 Docker 中正确映射
# DB_PATH 格式为: /path/to/data/person_fin.db
DATA_DIR = os.path.dirname(DB_PATH)
AUTO_EXPORT_DIR = os.path.join(DATA_DIR, "auto-export")


def ensure_auto_export_dir():
    """确保自动导出目录存在"""
    Path(AUTO_EXPORT_DIR).mkdir(parents=True, exist_ok=True)
    return AUTO_EXPORT_DIR


def execute_auto_export(rule_id: int):
    """执行自动导出任务（调度器触发，**不经 HTTP 路由**）。

    Args:
        rule_id: 自动导出规则 ID

    留痕（ADR-0027「离场桶」）：与手工 `POST /api/export-history/history` **同形**——
    `entity='export'` / `action='create'`，`after` 只记**壳**：`source='auto'` /
    `rule_id` / `rule_name` / `filename` / `file_size` / `operator` / `file_path` /
    `filters`，**不记文件内容**（审计是永久保留、可整库导出的表面，把离场数据再抄
    一份进库等于自造第二个离场面）。靠 `after.source='auto'` 与手工导出的
    `source` 缺省值区分。

    **契约表不覆盖本函数**：它不是路由，D12 的「写路由集合」枚举看不到调度器触发；
    形状按 `export/create` 对齐即可，无需（也不该）在 `ENDPOINT_RULES` 里造一行。

    事务语义（ADR-0007 可信 > 便利）：审计行与导出历史、规则时间戳**同事务**提交；
    导出本身失败（读记录/生成 CSV/写盘）→ 一行不写；审计写失败 → 整事务回滚。
    """
    # 延迟 import：export_history_service 反过来要 reload_scheduler（本模块），
    # 模块级互引会成环；这里只在需要「壳」形状时取一次。
    from services.export_history_service import history_key_fields

    db = SessionLocal()
    try:
        rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
        if not rule or not rule.is_active:
            logger.warning("auto_export_rule_missing", rule_id=rule_id)
            return
        
        logger.info("auto_export_started", rule_id=rule_id, rule_name=rule.name)
        
        # 确保目录存在
        ensure_auto_export_dir()
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if rule.filename_template:
            filename = rule.filename_template.replace("{timestamp}", timestamp)
            filename = filename.replace("{date}", datetime.now().strftime("%Y%m%d"))
        else:
            filename = f"auto_export_{rule.name}_{timestamp}.csv"
        
        if not filename.endswith('.csv'):
            filename += '.csv'
        
        file_path = os.path.join(AUTO_EXPORT_DIR, filename)
        
        # 获取所有记录
        records = export_service.get_export_records(db, period_type="all")
        
        # 生成 CSV
        csv_content = export_service.generate_csv(records)
        
        # 保存文件
        with open(file_path, 'w', encoding='utf-8-sig') as f:
            f.write(csv_content)
        
        file_size = os.path.getsize(file_path)
        
        # 记录导出历史
        history = ExportHistory(
            export_type="auto",
            filename=filename,
            file_size=file_size,
            operator=None,
            rule_name=rule.name,
            file_path=file_path,
        )
        db.add(history)
        db.flush()                       # 先取主键：审计行要 row_id
        # 离场留痕：壳形状复用手工导出的 key_fields（单点口径），再补 auto 侧标识
        audit_service.record(db, make_change("export", history.id, "create", after={
            **history_key_fields(history),
            "source": "auto",
            "rule_id": rule.id,
            "rule_name": rule.name,
            "filters": {"period_type": "all"},
        }))
        
        # 更新规则状态
        rule.last_run_at = datetime.now()
        rule.next_run_at = get_next_run_time(rule.cron_expression)
        
        db.commit()
        
        logger.info("auto_export_completed", rule_id=rule_id, file=file_path, file_size=file_size)
        
    except Exception:
        logger.exception("auto_export_failed", rule_id=rule_id)
        db.rollback()
    finally:
        db.close()


def load_auto_export_rules():
    """加载所有启用的自动导出规则到调度器"""
    db = SessionLocal()
    try:
        scheduler = get_scheduler()
        
        # 清除现有任务
        try:
            for job in scheduler.get_jobs():
                if job.id and job.id.startswith("auto_export_"):
                    scheduler.remove_job(job.id)
        except:
            pass
        
        # 加载启用的规则
        rules = db.query(AutoExportRule).filter(AutoExportRule.is_active == True).all()
        
        for rule in rules:
            job_id = f"auto_export_{rule.id}"
            try:
                scheduler.add_job(
                    execute_auto_export,
                    'cron',
                    args=[rule.id],
                    id=job_id,
                    replace_existing=True,
                    **parse_cron_to_scheduler_kwargs(rule.cron_expression)
                )
                logger.info("auto_export_scheduled", rule_name=rule.name, cron=rule.cron_expression)
            except Exception:
                logger.warning("auto_export_schedule_failed", rule_name=rule.name, exc_info=True)
        
        return len(rules)
    finally:
        db.close()


def parse_cron_to_scheduler_kwargs(cron_expression: str) -> dict:
    """将 Cron 表达式解析为 APScheduler 的 kwargs
    
    Args:
        cron_expression: Cron 表达式，如 "0 2 * * *"
        
    Returns:
        APScheduler 的 kwargs 字典
    """
    parts = cron_expression.split()
    if len(parts) != 5:
        raise ValueError("Invalid cron expression")
    
    minute, hour, day, month, day_of_week = parts
    
    kwargs = {}
    
    # 分钟
    if minute != "*":
        kwargs['minute'] = minute
    
    # 小时
    if hour != "*":
        kwargs['hour'] = hour
    
    # 日
    if day != "*":
        kwargs['day'] = day
    
    # 月
    if month != "*":
        kwargs['month'] = month
    
    # 周几
    if day_of_week != "*":
        kwargs['day_of_week'] = day_of_week
    
    return kwargs


def reload_scheduler():
    """重新加载调度器（在规则变更后调用）"""
    return load_auto_export_rules()
