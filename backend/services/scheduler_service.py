from datetime import datetime, timedelta
from typing import Optional, Tuple, List

try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False


def validate_cron_expression(cron_expression: str) -> Tuple[bool, str]:
    """验证 Cron 表达式是否有效
    
    Args:
        cron_expression: Cron 表达式，如 "0 2 * * *"
        
    Returns:
        (是否有效, 描述信息)
    """
    if not CRONITER_AVAILABLE:
        # 如果没有 croniter，使用简单的验证
        parts = cron_expression.split()
        if len(parts) != 5:
            return False, "Cron 表达式必须包含 5 个字段（分 时 日 月 周）"
        return True, "基本格式正确（未安装 croniter，无法详细验证）"
    
    try:
        croniter(cron_expression)
        description = describe_cron_expression(cron_expression)
        return True, description
    except Exception as e:
        return False, f"无效的 Cron 表达式: {str(e)}"


def describe_cron_expression(cron_expression: str) -> str:
    """将 Cron 表达式转换为人类可读的描述
    
    Args:
        cron_expression: Cron 表达式
        
    Returns:
        人类可读的描述
    """
    parts = cron_expression.split()
    if len(parts) != 5:
        return "无效的 Cron 表达式"
    
    minute, hour, day, month, weekday = parts
    
    # 简单的描述逻辑
    descriptions = []
    
    # 分钟
    if minute == "0":
        minute_desc = "整点"
    elif minute.startswith("*/"):
        minute_desc = f"每 {minute[2:]} 分钟"
    else:
        minute_desc = f"{minute} 分"
    
    # 小时
    if hour == "*":
        hour_desc = "每小时"
    elif hour.startswith("*/"):
        hour_desc = f"每 {hour[2:]} 小时"
    else:
        hour_desc = f"{hour} 点"
    
    # 天
    if day == "*":
        day_desc = "每天"
    elif day.startswith("*/"):
        day_desc = f"每 {day[2:]} 天"
    else:
        day_desc = f"{day} 日"
    
    # 月
    if month == "*":
        month_desc = "每月"
    elif month.startswith("*/"):
        month_desc = f"每 {month[2:]} 个月"
    else:
        month_desc = f"{month} 月"
    
    # 周
    weekday_names = {
        "0": "周日", "1": "周一", "2": "周二", "3": "周三",
        "4": "周四", "5": "周五", "6": "周六", "7": "周日",
        "*": "每天"
    }
    if weekday in weekday_names:
        weekday_desc = weekday_names[weekday]
    else:
        weekday_desc = f"周 {weekday}"
    
    # 组合描述
    if day == "*" and month == "*":
        if hour == "*":
            return f"每天每小时 {minute_desc}"
        else:
            return f"每天 {hour_desc}{minute_desc}"
    elif day.startswith("*/"):
        return f"每 {day[2:]} 天 {hour_desc}{minute_desc}"
    else:
        return f"{month_desc}{day_desc} {hour_desc}{minute_desc} ({weekday_desc})"


def get_next_run_time(cron_expression: str, base_time: Optional[datetime] = None) -> Optional[datetime]:
    """获取下次执行时间
    
    Args:
        cron_expression: Cron 表达式
        base_time: 基准时间，默认为当前时间
        
    Returns:
        下次执行时间
    """
    if not CRONITER_AVAILABLE:
        return None
    
    try:
        itr = croniter(cron_expression, base_time or datetime.now())
        return itr.get_next(datetime)
    except Exception:
        return None


def get_next_run_times(cron_expression: str, count: int = 5, base_time: Optional[datetime] = None) -> List[datetime]:
    """获取接下来多次执行时间
    
    Args:
        cron_expression: Cron 表达式
        count: 获取次数
        base_time: 基准时间，默认为当前时间
        
    Returns:
        执行时间列表
    """
    if not CRONITER_AVAILABLE:
        return []
    
    try:
        itr = croniter(cron_expression, base_time or datetime.now())
        return [itr.get_next(datetime) for _ in range(count)]
    except Exception:
        return []


# 简单的内存调度器（用于测试）
class SimpleScheduler:
    """简单的调度器，用于在无法使用 APScheduler 时的备选方案"""
    
    def __init__(self):
        self.jobs = []
    
    def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=False, **trigger_args):
        """添加任务
        
        Args:
            func: 要执行的函数
            trigger: 触发器类型（忽略，SimpleScheduler 只支持 cron）
            args: 函数参数
            kwargs: 函数关键字参数
            id: 任务 ID
            replace_existing: 是否替换现有任务（忽略）
            **trigger_args: 触发器参数（包含 cron_expression）
        """
        # 从 trigger_args 中获取 cron 表达式
        cron_expression = trigger_args.get('cron_expression', '* * * * *')
        
        # 如果 replace_existing 为 True，先移除相同 ID 的任务
        if replace_existing and id:
            self.jobs = [j for j in self.jobs if j["id"] != id]
        
        self.jobs.append({
            "id": id,
            "func": func,
            "cron_expression": cron_expression,
            "args": args or [],
            "kwargs": kwargs or {},
            "next_run": get_next_run_time(cron_expression)
        })
    
    def remove_job(self, job_id):
        """移除任务"""
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
    
    def get_jobs(self):
        """获取所有任务"""
        return self.jobs
    
    def run_pending(self):
        """运行待执行的任务"""
        now = datetime.now()
        for job in self.jobs:
            if job["next_run"] and job["next_run"] <= now:
                try:
                    job["func"](*job["args"], **job["kwargs"])
                except Exception as e:
                    print(f"Job {job['id']} failed: {e}")
                # 更新下次执行时间
                job["next_run"] = get_next_run_time(job["cron_expression"], now)
    
    def shutdown(self):
        """关闭调度器"""
        self.jobs = []


# 全局调度器实例
scheduler = None

def get_scheduler():
    """获取调度器实例"""
    global scheduler
    if scheduler is None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            scheduler = BackgroundScheduler()
            scheduler.start()
        except ImportError:
            scheduler = SimpleScheduler()
    return scheduler


def init_scheduler():
    """初始化调度器"""
    return get_scheduler()


def shutdown_scheduler():
    """关闭调度器"""
    global scheduler
    if scheduler:
        try:
            scheduler.shutdown()
        except:
            pass
        scheduler = None
