import os
import shutil
from datetime import datetime
from pathlib import Path

from database import DB_PATH

# 使用与数据库相同的基础目录，确保在 Docker 中正确映射
# DB_PATH 格式为: /path/to/data/person_fin.db
DATA_DIR = os.path.dirname(DB_PATH)
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
AUTO_EXPORT_DIR = os.path.join(DATA_DIR, "auto-export")


def ensure_backup_dir():
    """确保备份目录存在"""
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def ensure_auto_export_dir():
    """确保自动导出目录存在"""
    Path(AUTO_EXPORT_DIR).mkdir(parents=True, exist_ok=True)
    return AUTO_EXPORT_DIR


def create_backup_before_import(import_filename: str) -> str:
    """在导入前创建数据库备份
    
    Args:
        import_filename: 导入文件名
        
    Returns:
        备份文件的完整路径
    """
    ensure_backup_dir()
    
    # 清理导入文件名，移除扩展名和特殊字符
    clean_name = Path(import_filename).stem
    clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in clean_name)
    
    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_before_import_{clean_name}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    # 复制数据库文件（使用 copy 而不是 copy2，避免继承源文件时间戳）
    if os.path.exists(DB_PATH):
        shutil.copy(DB_PATH, backup_path)
        print(f"Backup created: {backup_path}")
        return backup_path
    else:
        raise FileNotFoundError(f"Database file not found: {DB_PATH}")


def list_backups() -> list[dict]:
    """列出所有备份文件

    Returns:
        备份文件列表，包含文件名、大小、创建时间等信息，按创建时间倒序排列（最近的在最上面）
    """
    ensure_backup_dir()

    backups = []
    for filename in os.listdir(BACKUP_DIR):
        if filename.endswith('.db'):
            filepath = os.path.join(BACKUP_DIR, filename)
            stat = os.stat(filepath)
            backups.append({
                "filename": filename,
                "path": filepath,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "mtime": stat.st_mtime,  # 用于排序的时间戳
            })

    # 按修改时间倒序排列，最近的在最上面
    backups.sort(key=lambda x: x["mtime"], reverse=True)

    # 移除排序用的临时字段
    for backup in backups:
        del backup["mtime"]

    return backups


def delete_backup(filename: str) -> bool:
    """删除指定的备份文件

    Args:
        filename: 备份文件名

    Returns:
        是否成功删除
    """
    filepath = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(filepath) and filepath.startswith(BACKUP_DIR):
        os.remove(filepath)
        return True
    return False


def create_full_backup() -> dict:
    """创建完整数据库备份

    创建当前数据库的完整副本，按时间戳命名。

    Returns:
        备份文件信息
    """
    ensure_backup_dir()

    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    # 检查数据库文件存在
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"数据库文件不存在: {DB_PATH}")

    # 复制数据库文件（使用 copy 而不是 copy2，避免继承源文件时间戳）
    shutil.copy(DB_PATH, backup_path)
    print(f"完整备份已创建: {backup_path}")

    # 获取文件信息
    stat = os.stat(backup_path)

    return {
        "filename": backup_filename,
        "path": backup_path,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def restore_database(source_path: str):
    """从备份文件恢复数据库

    **警告：此操作将替换当前数据库！**

    恢复流程：
    1. 验证备份文件有效性
    2. 关闭当前数据库连接
    3. 备份当前数据库（可选，用于回滚）
    4. 用备份文件替换当前数据库
    5. 重新建立数据库连接

    Args:
        source_path: 备份文件路径

    Raises:
        FileNotFoundError: 备份文件不存在
        ValueError: 备份文件无效
        RuntimeError: 恢复过程中出错
    """
    import sqlite3
    from database import engine

    # 验证备份文件存在
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"备份文件不存在: {source_path}")

    # 验证备份文件是有效的 SQLite 数据库
    try:
        conn = sqlite3.connect(source_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        conn.close()

        if not tables:
            raise ValueError("备份文件不包含任何表")
    except sqlite3.Error as e:
        raise ValueError(f"无效的 SQLite 备份文件: {e}")

    # 确保当前数据库目录存在
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)

    # 关闭当前数据库连接（通过 dispose 方法）
    engine.dispose()
    print("数据库连接已关闭")

    try:
        # 备份当前数据库（用于回滚）
        if os.path.exists(DB_PATH):
            rollback_path = os.path.join(
                BACKUP_DIR,
                f"rollback_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            )
            # 使用 copy 而不是 copy2，避免继承源文件时间戳
            shutil.copy(DB_PATH, rollback_path)
            print(f"当前数据库已备份到: {rollback_path}")

        # 用备份文件替换当前数据库
        shutil.copy2(source_path, DB_PATH)
        print(f"数据库已恢复: {DB_PATH}")

    except Exception as e:
        raise RuntimeError(f"恢复数据库失败: {e}")

    # 重新初始化数据库连接
    # 注意：FastAPI 的依赖注入会自动使用新的连接
    print("数据库恢复完成，新连接将在下次请求时建立")
