import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db, engine, DB_PATH
from services import backup_service

router = APIRouter()


@router.post("/create")
def create_backup():
    """创建完整数据库备份

    创建当前数据库的完整副本，按时间戳命名。
    """
    try:
        backup_info = backup_service.create_full_backup()
        return {
            "success": True,
            "message": "备份创建成功",
            "backup": backup_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"备份失败: {str(e)}")


@router.get("/list")
def list_backups():
    """获取备份文件列表

    返回所有备份文件的详细信息，按创建时间倒序排列。
    """
    try:
        backups = backup_service.list_backups()
        return {
            "success": True,
            "backups": backups
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取备份列表失败: {str(e)}")


@router.get("/download/{filename}")
def download_backup(filename: str):
    """下载备份文件

    Args:
        filename: 备份文件名

    Returns:
        备份文件下载
    """
    # 安全检查：防止路径遍历
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    backup_path = os.path.join(backup_service.BACKUP_DIR, filename)

    # 确保文件在备份目录内
    if not os.path.exists(backup_path) or not backup_path.startswith(backup_service.BACKUP_DIR):
        raise HTTPException(status_code=404, detail="备份文件不存在")

    return FileResponse(
        path=backup_path,
        filename=filename,
        media_type="application/octet-stream"
    )


@router.delete("/delete/{filename}")
def delete_backup(filename: str):
    """删除备份文件

    Args:
        filename: 备份文件名

    Returns:
        删除结果
    """
    # 安全检查：防止路径遍历
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    try:
        if backup_service.delete_backup(filename):
            return {
                "success": True,
                "message": "备份文件已删除"
            }
        else:
            raise HTTPException(status_code=404, detail="备份文件不存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.post("/restore")
async def restore_backup(
    filename: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    """从备份文件恢复数据库

    支持两种方式：
    1. 从服务器上的备份文件恢复（提供 filename）
    2. 从上传的备份文件恢复（提供 file）

    **警告：恢复操作将替换当前数据库，请确保已备份重要数据！**

    Args:
        filename: 服务器上的备份文件名（与 file 二选一）
        file: 上传的备份文件（与 filename 二选一）

    Returns:
        恢复结果
    """
    if not filename and not file:
        raise HTTPException(status_code=400, detail="请提供备份文件名或上传备份文件")

    temp_path = None

    try:
        if file:
            # 从上传文件恢复
            if not file.filename.endswith('.db') and not file.filename.endswith('.sqlite'):
                raise HTTPException(status_code=400, detail="只支持 .db 或 .sqlite 文件")

            # 保存上传的文件到临时位置
            content = await file.read()
            temp_dir = os.path.join(backup_service.BACKUP_DIR, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")

            with open(temp_path, 'wb') as f:
                f.write(content)

            source_path = temp_path
        else:
            # 从服务器备份恢复
            # 安全检查：防止路径遍历
            if ".." in filename or "/" in filename or "\\" in filename:
                raise HTTPException(status_code=400, detail="无效的文件名")

            source_path = os.path.join(backup_service.BACKUP_DIR, filename)

            if not os.path.exists(source_path):
                raise HTTPException(status_code=404, detail="备份文件不存在")

        # 执行恢复
        backup_service.restore_database(source_path)

        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

        return {
            "success": True,
            "message": "数据库恢复成功，请刷新页面"
        }

    except HTTPException:
        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail=f"恢复失败: {str(e)}")
