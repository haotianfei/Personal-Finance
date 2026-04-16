import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from database import SessionLocal, DB_PATH
from models import AutoExportRule, ExportHistory
from services import export_service, backup_service
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
    """执行自动导出任务
    
    Args:
        rule_id: 自动导出规则 ID
    """
    db = SessionLocal()
    try:
        rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
        if not rule or not rule.is_active:
            print(f"Rule {rule_id} not found or inactive")
            return
        
        print(f"Executing auto export rule: {rule.name}")
        
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
        
        # 更新规则状态
        rule.last_run_at = datetime.now()
        rule.next_run_at = get_next_run_time(rule.cron_expression)
        
        db.commit()
        
        print(f"Auto export completed: {file_path}")
        
    except Exception as e:
        print(f"Auto export failed for rule {rule_id}: {e}")
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
                print(f"Scheduled auto export rule: {rule.name} ({rule.cron_expression})")
            except Exception as e:
                print(f"Failed to schedule rule {rule.name}: {e}")
        
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
