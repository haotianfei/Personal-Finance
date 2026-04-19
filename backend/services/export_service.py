import csv
import io
import json
import logging
import os
import shutil
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import select, and_, inspect

from models import (
    AssetRecord, FundType, Account, LiquidityRating,
    AssetOwner, AlertRule, AllocationTarget, ExportHistory, AutoExportRule
)

# 配置日志
logger = logging.getLogger(__name__)


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
    "auto_export_rules": AutoExportRule,
    "export_history": ExportHistory,
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
        table_name: 表名 (accounts, fund_types, liquidity_ratings, asset_owners, alert_rules, allocation_targets, asset_records, auto_export_rules, export_history)
        include_schema: 是否包含schema信息

    Returns:
        包含schema和数据的字典
    """
    logger.info(f"Exporting table '{table_name}' to JSON")

    if table_name not in TABLE_MAP:
        logger.error(f"Unknown table: {table_name}. Supported tables: {list(TABLE_MAP.keys())}")
        raise ValueError(f"Unknown table: {table_name}. Supported tables: {list(TABLE_MAP.keys())}")

    model_class = TABLE_MAP[table_name]

    try:
        # 获取schema
        result = {}
        if include_schema:
            result["schema"] = _get_table_schema(model_class)

        # 获取数据
        records = db.execute(select(model_class)).scalars().all()
        result["data"] = [_model_to_dict(r) for r in records]
        result["count"] = len(result["data"])
        result["exported_at"] = datetime.now().isoformat()

        logger.info(f"Successfully exported {result['count']} records from '{table_name}'")
        return result

    except Exception as e:
        logger.error(f"Failed to export table '{table_name}': {str(e)}")
        raise


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
    logger.info(f"Exporting tables to JSON: {table_names}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.json"
    filepath = os.path.join(EXPORTS_DIR, filename)

    try:
        export_data = {
            "exported_at": datetime.now().isoformat(),
            "tables": {}
        }

        for table_name in table_names:
            if table_name in TABLE_MAP:
                try:
                    export_data["tables"][table_name] = export_table_to_json(db, table_name, include_schema=True)
                except Exception as e:
                    logger.error(f"Failed to export table '{table_name}': {str(e)}")
                    raise
            else:
                logger.warning(f"Skipping unknown table: {table_name}")

        # 写入文件
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        logger.info(f"Successfully exported to: {filepath}")
        return filepath

    except Exception as e:
        logger.error(f"Failed to export tables to JSON: {str(e)}")
        raise


def export_tables_to_csv(
    db: Session,
    table_names: List[str],
    filename_prefix: str = "export"
) -> str:
    """导出多个表到CSV文件（打包成ZIP）

    Args:
        db: 数据库会话
        table_names: 表名列表
        filename_prefix: 文件名前缀

    Returns:
        生成的ZIP文件路径
    """
    import zipfile
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"{filename_prefix}_csv_{timestamp}.zip"
    zip_filepath = os.path.join(EXPORTS_DIR, zip_filename)
    
    # 创建ZIP文件
    with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for table_name in table_names:
            if table_name not in TABLE_MAP:
                continue

            csv_content = export_table_to_csv(db, table_name)
            csv_filename = f"{table_name}.csv"
            
            # 将CSV内容写入ZIP
            zipf.writestr(csv_filename, csv_content.encode('utf-8'))

    return zip_filepath


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


def export_tables_to_sqlite(db: Session, tables: List[str], filename_prefix: str = "export") -> str:
    """导出指定表到 SQLite 数据库文件

    Args:
        db: 数据库会话
        tables: 要导出的表名列表
        filename_prefix: 文件名前缀

    Returns:
        导出文件路径
    """
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.db"
    filepath = os.path.join(EXPORTS_DIR, filename)

    # 创建新的 SQLite 数据库
    conn = sqlite3.connect(filepath)
    cursor = conn.cursor()

    try:
        for table_name in tables:
            if table_name not in TABLE_MAP:
                continue

            model_class = TABLE_MAP[table_name]

            # 获取表结构
            inspector = inspect(db.bind)
            columns_info = inspector.get_columns(table_name)

            # 创建表
            column_defs = []
            for col in columns_info:
                col_name = col['name']
                col_type = str(col['type'])
                # 简化类型映射
                if 'INTEGER' in col_type.upper() or 'INT' in col_type.upper():
                    sql_type = 'INTEGER'
                elif 'FLOAT' in col_type.upper() or 'REAL' in col_type.upper() or 'NUMERIC' in col_type.upper() or 'DECIMAL' in col_type.upper():
                    sql_type = 'REAL'
                elif 'BLOB' in col_type.upper():
                    sql_type = 'BLOB'
                else:
                    sql_type = 'TEXT'

                nullable = '' if col.get('nullable', True) else ' NOT NULL'
                default = f" DEFAULT {col['default']}" if col.get('default') is not None else ''
                column_defs.append(f"{col_name} {sql_type}{nullable}{default}")

            create_sql = f"CREATE TABLE {table_name} ({', '.join(column_defs)})"
            cursor.execute(create_sql)

            # 获取数据
            records = db.query(model_class).all()

            # 插入数据
            if records:
                # 获取列名
                col_names = [col['name'] for col in columns_info]
                placeholders = ', '.join(['?' for _ in col_names])
                insert_sql = f"INSERT INTO {table_name} ({', '.join(col_names)}) VALUES ({placeholders})"

                for record in records:
                    row_data = []
                    for col in columns_info:
                        col_name = col['name']
                        value = getattr(record, col_name, None)
                        # 处理特殊类型
                        if isinstance(value, datetime):
                            value = value.isoformat()
                        elif isinstance(value, date):
                            value = value.isoformat()
                        elif isinstance(value, Decimal):
                            value = float(value)
                        row_data.append(value)
                    cursor.execute(insert_sql, row_data)

        conn.commit()
        logger.info(f"Exported {len(tables)} tables to SQLite: {filepath}")
        return filepath

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to export to SQLite: {str(e)}")
        raise
    finally:
        conn.close()


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
