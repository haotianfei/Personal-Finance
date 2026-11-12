import os
import json

import structlog
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

from database import get_db
from services.idempotency_service import idempotency_guard
from schemas import ImportPreviewResponse, ImportBatchOut
from services import import_service, backup_service
from models import ImportBatch

router = APIRouter()

IMPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "import")


# ==================== CSV Import Endpoints ====================

@router.post("/upload", response_model=ImportPreviewResponse, dependencies=[Depends(idempotency_guard("POST /api/imports/upload"))])
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    text = content.decode("utf-8-sig")
    try:
        batch, valid, invalid, new_attributes = import_service.preview_import(db, text, file.filename or "upload.csv")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conflict_count = sum(1 for r in valid if r.has_conflict)
    return ImportPreviewResponse(
        batch_id=batch.id,
        filename=batch.filename,
        valid_rows=valid,
        invalid_rows=invalid,
        total_rows=len(valid) + len(invalid),
        conflict_count=conflict_count,
        new_attributes=new_attributes,
    )


@router.post("/confirm", response_model=ImportBatchOut, dependencies=[Depends(idempotency_guard("POST /api/imports/confirm"))])
async def confirm_import(
    file: UploadFile = File(...),
    conflict_resolution: str = "skip",
    attribute_actions: str = "{}",  # JSON string of attribute actions
    db: Session = Depends(get_db),
):
    """确认导入 CSV（留痕：`entity='import', action='import_confirm'` 头行 + 逐行 create 明细）。"""
    content = await file.read()
    text = content.decode("utf-8-sig")

    # 先验证CSV格式
    try:
        import_service.parse_csv_content(text, file.filename or "upload.csv")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 在导入前创建备份
    backup_path = None
    try:
        backup_path = backup_service.create_backup_before_import(file.filename or "upload.csv")
    except Exception as e:
        logger.warning("backup_before_import_failed", detail=str(e), exc_info=True)
        # 备份失败不阻止导入流程，但记录警告

    actions = json.loads(attribute_actions) if attribute_actions else {}
    try:
        batch = import_service.import_csv_records(db, text, file.filename or "upload.csv", conflict_resolution, actions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 将备份信息添加到返回结果中
    if backup_path:
        batch.backup_filename = os.path.basename(backup_path)

    return batch


@router.get("/history", response_model=list[ImportBatchOut])
def import_history(db: Session = Depends(get_db)):
    batches = db.query(ImportBatch).order_by(ImportBatch.imported_at.desc()).all()
    return batches


@router.get("/backups", response_model=list[dict])
def list_backups():
    """列出所有导入前的备份文件"""
    return backup_service.list_backups()


@router.delete("/backups/{filename}")
def delete_backup(filename: str, db: Session = Depends(get_db)):
    """删除指定的备份文件

    留痕（ADR-0027 离场桶）：`entity='backup', action='delete'`（与
    `DELETE /api/backup/delete/{filename}` 同一 entity/action 与同一份快照形状），
    顺序＝先写审计行并提交、再删文件。文件不存在 → 404 且零审计行。

    **不向用户索取理由**（业主 2026-11-08 裁决）：服务端记
    `reason='normalization'` + `after.source='manual_cleanup'`，本端点无 reason 入参。
    """
    if backup_service.delete_backup(filename, db=db):
        return {"message": "Backup deleted successfully"}
    raise HTTPException(status_code=404, detail="Backup not found")


@router.post("/seed")
def seed_data(db: Session = Depends(get_db)):
    """把 `import/` 目录的历史 CSV 逐个导入（留痕：`action='import_seed'` 头行 + 逐行 create 明细）。"""
    if not os.path.isdir(IMPORT_DIR):
        raise HTTPException(status_code=404, detail=f"Import directory not found: {IMPORT_DIR}")
    batches = import_service.seed_import_directory(db, IMPORT_DIR)
    return {
        "message": f"Imported {len(batches)} files",
        "batches": [
            {"filename": b.filename, "record_count": b.record_count, "status": b.status}
            for b in batches
        ],
    }
