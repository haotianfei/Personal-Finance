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
    
    # 复制数据库文件
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
        print(f"Backup created: {backup_path}")
        return backup_path
    else:
        raise FileNotFoundError(f"Database file not found: {DB_PATH}")


def list_backups() -> list[dict]:
    """列出所有备份文件
    
    Returns:
        备份文件列表，包含文件名、大小、创建时间等信息
    """
    ensure_backup_dir()
    
    backups = []
    for filename in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if filename.endswith('.db'):
            filepath = os.path.join(BACKUP_DIR, filename)
            stat = os.stat(filepath)
            backups.append({
                "filename": filename,
                "path": filepath,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    
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
