import csv
import io
import json
import os
import shutil
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import select, and_, inspect

from models import (
    AssetRecord, FundType, Account, LiquidityRating,
    AssetOwner, AlertRule, AllocationTarget, ExportHistory
)


# 导出目录配置
EXPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "exports")

# 确保导出目录存在
os.makedirs(EXPORTS_DIR, exist_ok=True)

# 支持的表名映射
TABLE_MAP = {
    "accounts": Account,
    "fund_types": FundType,
    "liquidity_ratings": LiquidityRating,
    "asset_owners": AssetOwner,
    "alert_rules": AlertRule,
    "allocation_targets": AllocationTarget,
    "asset_records": AssetRecord,
}


def get_export_records(
    db: Session,
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> list[dict]:
    """Get records for export with optional period filtering.

    Args:
        period_type: 'all', 'day', 'month', 'quarter', 'year', 'custom'
        year: Filter by year (required for month, quarter, day)
        month: Filter by month (1-12, required for day)
        quarter: Filter by quarter (1-4)
        day: Filter by day (1-31)
        date_from: Start date for custom range filter
        date_to: End date for custom range filter
    """
    query = (
        select(
            AssetRecord.asset_date,
            LiquidityRating.name.label("liquidity_rating"),
            FundType.name.label("fund_type"),
            AssetRecord.asset_name,
            Account.name.label("account"),
            AssetRecord.amount,
        )
        .join(LiquidityRating, AssetRecord.liquidity_rating_id == LiquidityRating.id)
        .join(FundType, AssetRecord.fund_type_id == FundType.id)
        .join(Account, AssetRecord.account_id == Account.id)
        .order_by(AssetRecord.asset_date, AssetRecord.asset_name)
    )

    # Apply period filters
    if period_type and period_type != "all":
        conditions = []

        if year:
            conditions.append(AssetRecord.asset_date >= date(year, 1, 1))
            conditions.append(AssetRecord.asset_date <= date(year, 12, 31))

        if period_type == "month" and month:
            conditions.append(AssetRecord.asset_date >= date(year, month, 1))
            if month == 12:
                conditions.append(AssetRecord.asset_date <= date(year + 1, 1, 1))
            else:
                conditions.append(AssetRecord.asset_date < date(year, month + 1, 1))

        elif period_type == "quarter" and quarter:
            start_month = (quarter - 1) * 3 + 1
            end_month = quarter * 3 + 1
            conditions.append(AssetRecord.asset_date >= date(year, start_month, 1))
            if end_month > 12:
                conditions.append(AssetRecord.asset_date < date(year + 1, 1, 1))
            else:
                conditions.append(AssetRecord.asset_date < date(year, end_month, 1))

        elif period_type == "day" and month and day:
            conditions.append(AssetRecord.asset_date == date(year, month, day))

        elif period_type == "custom" and date_from and date_to:
            conditions.append(AssetRecord.asset_date >= date_from)
            conditions.append(AssetRecord.asset_date <= date_to)

        if conditions:
            query = query.where(and_(*conditions))

    results = db.execute(query).all()

    records = []
    for r in results:
        records.append({
            "asset_date": r.asset_date.strftime("%Y-%m-%d"),
            "liquidity_rating": r.liquidity_rating,
            "fund_type": r.fund_type,
            "asset_name": r.asset_name,
            "account": r.account,
            "amount": str(r.amount),
        })

    return records


def generate_csv(records: list[dict]) -> str:
    """Generate CSV content from records."""
    if not records:
        return ""

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["asset_date", "liquidity_rating", "fund_type", "asset_name", "account", "amount"],
        lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def get_export_filename(
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> str:
    """Generate export filename based on filters."""
    if period_type == "all" or not period_type:
        return "assets_export_all.csv"

    if period_type == "custom" and date_from and date_to:
        return f"assets_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.csv"

    parts = ["assets"]
    if year:
        parts.append(str(year))
    if period_type == "quarter" and quarter:
        parts.append(f"Q{quarter}")
    elif period_type == "month" and month:
        parts.append(f"{month:02d}")
    elif period_type == "day" and month and day:
        parts.append(f"{month:02d}{day:02d}")

    return "_".join(parts) + ".csv"


def _serialize_value(value: Any) -> Any:
    """序列化值，处理特殊类型如日期、Decimal等"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _get_table_schema(model_class) -> Dict[str, Any]:
    """获取表的schema信息"""
    mapper = inspect(model_class)
    columns = {}

    for column in mapper.columns:
        col_info = {
            "type": str(column.type),
            "nullable": column.nullable,
            "primary_key": column.primary_key,
        }
        if column.default:
            col_info["default"] = str(column.default)
        columns[column.name] = col_info

    return {
        "table_name": model_class.__tablename__,
        "columns": columns,
    }


def _model_to_dict(instance) -> Dict[str, Any]:
    """将模型实例转换为字典"""
    result = {}
    for column in instance.__table__.columns:
        value = getattr(instance, column.name)
        result[column.name] = _serialize_value(value)
    return result


def export_table_to_json(
    db: Session,
    table_name: str,
    include_schema: bool = True
) -> Dict[str, Any]:
    """导出指定表到JSON格式

    Args:
        db: 数据库会话
        table_name: 表名 (accounts, fund_types, liquidity_ratings, asset_owners, alert_rules, allocation_targets, asset_records)
        include_schema: 是否包含schema信息

    Returns:
        包含schema和数据的字典
    """
    if table_name not in TABLE_MAP:
        raise ValueError(f"Unknown table: {table_name}. Supported tables: {list(TABLE_MAP.keys())}")

    model_class = TABLE_MAP[table_name]

    # 获取schema
    result = {}
    if include_schema:
        result["schema"] = _get_table_schema(model_class)

    # 获取数据
    records = db.execute(select(model_class)).scalars().all()
    result["data"] = [_model_to_dict(r) for r in records]
    result["count"] = len(result["data"])
    result["exported_at"] = datetime.now().isoformat()

    return result


def export_table_to_csv(
    db: Session,
    table_name: str
) -> str:
    """导出指定表到CSV格式

    Args:
        db: 数据库会话
        table_name: 表名

    Returns:
        CSV内容字符串
    """
    if table_name not in TABLE_MAP:
        raise ValueError(f"Unknown table: {table_name}. Supported tables: {list(TABLE_MAP.keys())}")

    model_class = TABLE_MAP[table_name]

    # 获取数据
    records = db.execute(select(model_class)).scalars().all()

    if not records:
        return ""

    # 获取列名
    columns = [c.name for c in model_class.__table__.columns]

    # 生成CSV
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    # 写入表头
    writer.writerow(columns)

    # 写入数据
    for record in records:
        row = [_serialize_value(getattr(record, col)) for col in columns]
        writer.writerow(row)

    return output.getvalue()


def export_tables_to_json(
    db: Session,
    table_names: List[str],
    filename_prefix: str = "export"
) -> str:
    """导出多个表到JSON文件

    Args:
        db: 数据库会话
        table_names: 表名列表
        filename_prefix: 文件名前缀

    Returns:
        生成的文件路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.json"
    filepath = os.path.join(EXPORTS_DIR, filename)

    export_data = {
        "exported_at": datetime.now().isoformat(),
        "tables": {}
    }

    for table_name in table_names:
        if table_name in TABLE_MAP:
            export_data["tables"][table_name] = export_table_to_json(db, table_name, include_schema=True)

    # 写入文件
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    return filepath


def export_tables_to_csv(
    db: Session,
    table_names: List[str],
    filename_prefix: str = "export"
) -> List[str]:
    """导出多个表到CSV文件（每个表一个文件）

    Args:
        db: 数据库会话
        table_names: 表名列表
        filename_prefix: 文件名前缀

    Returns:
        生成的文件路径列表
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepaths = []

    for table_name in table_names:
        if table_name not in TABLE_MAP:
            continue

        filename = f"{filename_prefix}_{table_name}_{timestamp}.csv"
        filepath = os.path.join(EXPORTS_DIR, filename)

        csv_content = export_table_to_csv(db, table_name)

        with open(filepath, 'w', encoding='utf-8', newline='') as f:
            f.write(csv_content)

        filepaths.append(filepath)

    return filepaths


def create_full_backup(db: Session) -> str:
    """创建完整数据库备份（包含所有表）

    Args:
        db: 数据库会话

    Returns:
        备份文件路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"full_backup_{timestamp}.json"
    filepath = os.path.join(EXPORTS_DIR, filename)

    export_data = {
        "backup_type": "full",
        "created_at": datetime.now().isoformat(),
        "tables": {}
    }

    # 导出所有支持的表
    for table_name, model_class in TABLE_MAP.items():
        export_data["tables"][table_name] = export_table_to_json(db, table_name, include_schema=True)

    # 写入文件
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    return filepath


def list_export_files() -> List[Dict[str, Any]]:
    """列出所有导出文件

    Returns:
        导出文件信息列表
    """
    files = []

    if not os.path.exists(EXPORTS_DIR):
        return files

    for filename in os.listdir(EXPORTS_DIR):
        filepath = os.path.join(EXPORTS_DIR, filename)
        if os.path.isfile(filepath):
            stat = os.stat(filepath)
            files.append({
                "filename": filename,
                "file_path": filepath,
                "file_size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    # 按创建时间倒序排列
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files


def get_export_file_path(filename: str) -> Optional[str]:
    """获取导出文件的完整路径

    Args:
        filename: 文件名

    Returns:
        文件路径，如果文件不存在则返回None
    """
    filepath = os.path.join(EXPORTS_DIR, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return filepath
    return None


def delete_export_file(filename: str) -> bool:
    """删除导出文件

    Args:
        filename: 文件名

    Returns:
        是否删除成功
    """
    filepath = os.path.join(EXPORTS_DIR, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        os.remove(filepath)
        return True
    return False
