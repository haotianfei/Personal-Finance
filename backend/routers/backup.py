import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError
from services import backup_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


@router.post("/create", dependencies=[Depends(idempotency_guard("POST /api/backup/create"))])
def create_backup(db: Session = Depends(get_db)):
    """创建完整数据库备份

    创建当前数据库的完整副本，按时间戳命名。

    留痕（ADR-0027 离场桶）：`entity='backup', action='create'`，只记文件壳
    （文件名/字节数/哈希前缀/来源/库世代）；顺序＝先造文件、成功后写审计行。
    """
    try:
        backup_info = backup_service.create_full_backup(db, source="manual")
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

    backup_dir = backup_service.get_backup_dir()
    backup_path = os.path.join(backup_dir, filename)

    # 确保文件在备份目录内
    if not os.path.exists(backup_path) or not backup_path.startswith(backup_dir):
        raise HTTPException(status_code=404, detail="备份文件不存在")

    return FileResponse(
        path=backup_path,
        filename=filename,
        media_type="application/octet-stream"
    )


@router.delete("/delete/{filename}")
def delete_backup(filename: str, db: Session = Depends(get_db)):
    """删除备份文件

    留痕（ADR-0027 离场桶）：`entity='backup', action='delete'`，`before` 记文件壳；
    顺序＝先写审计行并提交、再删文件（删文件失败抛错，审计行留下）。文件不存在 →
    幂等返回 404 且零审计行。

    **不向用户索取理由**（业主 2026-11-08 裁决）：删旧备份是家务事，服务端记
    `reason='normalization'` + `after.source='manual_cleanup'`（保留策略那条 job 记
    `'retention'`）。故本端点**没有** reason 入参，也不会 422。

    Args:
        filename: 备份文件名

    Returns:
        删除结果
    """
    # 安全检查：防止路径遍历
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    try:
        # reason/source 由服务层归一化（normalization + manual_cleanup），
        # 端点不接理由入参、也不校验（业主裁决：不向用户索取）。
        if backup_service.delete_backup(filename, db=db):
            return {
                "success": True,
                "message": "备份文件已删除"
            }
        else:
            raise HTTPException(status_code=404, detail="备份文件不存在")
    except (HTTPException, ApiError):
        # 已定语义的异常必须原样透传（404＝客户端问题，ApiError＝业务码），
        # 不得被下面的 500 兜底改写成「服务器错误」。
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.post("/restore", dependencies=[Depends(idempotency_guard("POST /api/backup/restore"))])
async def restore_backup(
    filename: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    confirm_rollback: bool = Form(False),
):
    """从备份文件恢复数据库

    支持两种方式：
    1. 从服务器上的备份文件恢复（提供 filename）
    2. 从上传的备份文件恢复（提供 file）

    **警告：恢复操作将替换当前数据库，请确保已备份重要数据！**

    恢复前会做库外锚对账（`services/audit_integrity_service`）：目标包的
    `manifest.audit_head` 比当前库更旧（整库回退）或同长度不同 hash（分叉）→
    409 `AUDIT_ROLLBACK_DETECTED`，**现库一个字节都不动**。确认要回退时带
    `confirm_rollback=true` 重试，该次回退会连同 `integrity` 与
    `rollback_confirmed` 一起留痕在新库的 `backup/restore` 审计行里。

    Args:
        filename: 服务器上的备份文件名（与 file 二选一）
        file: 上传的备份文件（与 filename 二选一）
        confirm_rollback: 显式确认整库回退（默认 False＝阻断）

    Returns:
        恢复结果
    """
    if not filename and not file:
        raise HTTPException(status_code=400, detail="请提供备份文件名或上传备份文件")

    temp_path = None

    try:
        if file:
            # 从上传文件恢复（.zip bundle 或旧裸 .db/.sqlite）
            if not file.filename.endswith(('.db', '.sqlite', '.zip')):
                raise HTTPException(status_code=400, detail="只支持 .zip / .db 或 .sqlite 文件")

            # 保存上传的文件到临时位置（保留原后缀，service 按后缀分流 bundle/裸库）
            suffix = os.path.splitext(file.filename)[1] or '.db'
            content = await file.read()
            temp_dir = os.path.join(backup_service.get_backup_dir(), "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}")

            with open(temp_path, 'wb') as f:
                f.write(content)

            source_path = temp_path
        else:
            # 从服务器备份恢复
            # 安全检查：防止路径遍历
            if ".." in filename or "/" in filename or "\\" in filename:
                raise HTTPException(status_code=400, detail="无效的文件名")

            source_path = os.path.join(backup_service.get_backup_dir(), filename)

            if not os.path.exists(source_path):
                raise HTTPException(status_code=404, detail="备份文件不存在")

        # 执行恢复（留痕：entity='backup', action='restore'，写在替换后的新库里）
        result = backup_service.restore_database(
            source_path, action="restore",
            source_label=(file.filename if file else None),
            confirm_rollback=confirm_rollback)

        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

        response = {
            "success": True,
            "message": "数据库恢复成功，请刷新页面"
        }
        # 附加字段（非破坏性）：旧裸 .db 恢复时附「不含凭证」警示
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except (HTTPException, ApiError):
        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail=f"恢复失败: {str(e)}")


@router.post("/upload-db", dependencies=[Depends(idempotency_guard("POST /api/backup/upload-db"))])
async def upload_database_file(
    file: UploadFile = File(...),
):
    """上传数据库文件，按 V2 格式直接恢复

    只接受 V2 数据库文件（当前系统 schema），直接替换当前数据库。

    Args:
        file: 数据库文件 (.db)

    Returns:
        恢复结果
    """
    if not file.filename.endswith('.db') and not file.filename.endswith('.sqlite'):
        raise HTTPException(status_code=400, detail="只支持 .db 或 .sqlite 文件")

    temp_path = None

    try:
        # 保存上传的文件到临时位置
        content = await file.read()
        temp_dir = os.path.join(backup_service.BACKUP_DIR, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"upload_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")

        with open(temp_path, 'wb') as f:
            f.write(content)

        # 直接执行恢复（替换当前数据库）
        # 留痕：entity='backup', action='upload_db'（写在替换后的新库里）
        backup_service.restore_database(temp_path, action="upload_db",
                                        source_label=file.filename)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return {
            "action": "restore",
            "success": True,
            "message": "数据库恢复成功，请刷新页面"
        }

    except HTTPException:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
