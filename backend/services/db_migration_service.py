"""数据库迁移服务

用于在应用启动时自动更新数据库结构
"""

from sqlalchemy import inspect, text
from database import engine
import logging

logger = logging.getLogger(__name__)


def migrate_database():
    """执行数据库迁移"""
    inspector = inspect(engine)
    
    # 获取所有表名
    tables = inspector.get_table_names()
    logger.info(f"Found tables: {tables}")
    
    # 迁移 asset_records 表
    if "asset_records" in tables:
        migrate_asset_records_table()
    
    # 迁移 accounts 表
    if "accounts" in tables:
        migrate_accounts_table()
    
    logger.info("Database migration completed")


def migrate_asset_records_table():
    """迁移 asset_records 表，添加缺失的列"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('asset_records')]
    
    with engine.connect() as conn:
        # 添加 owner_id 列
        if 'owner_id' not in columns:
            logger.info("Adding owner_id column to asset_records table")
            conn.execute(text("ALTER TABLE asset_records ADD COLUMN owner_id INTEGER"))
            conn.commit()
        
        # 添加其他可能缺失的列
        if 'created_at' not in columns:
            logger.info("Adding created_at column to asset_records table")
            conn.execute(text("ALTER TABLE asset_records ADD COLUMN created_at TIMESTAMP"))
            conn.commit()
        
        if 'updated_at' not in columns:
            logger.info("Adding updated_at column to asset_records table")
            conn.execute(text("ALTER TABLE asset_records ADD COLUMN updated_at TIMESTAMP"))
            conn.commit()


def migrate_accounts_table():
    """迁移 accounts 表，添加缺失的列"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('accounts')]
    
    with engine.connect() as conn:
        # 添加 created_at 列
        if 'created_at' not in columns:
            logger.info("Adding created_at column to accounts table")
            conn.execute(text("ALTER TABLE accounts ADD COLUMN created_at TIMESTAMP"))
            conn.commit()
        
        if 'updated_at' not in columns:
            logger.info("Adding updated_at column to accounts table")
            conn.execute(text("ALTER TABLE accounts ADD COLUMN updated_at TIMESTAMP"))
            conn.commit()


def check_table_exists(table_name: str) -> bool:
    """检查表是否存在"""
    inspector = inspect(engine)
    return table_name in inspector.get_table_names()


def check_column_exists(table_name: str, column_name: str) -> bool:
    """检查列是否存在"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns
