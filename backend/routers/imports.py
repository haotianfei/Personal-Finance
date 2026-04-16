import os

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import ImportPreviewResponse, ImportConfirmRequest, ImportBatchOut, NewAttribute
from services import import_service, backup_service
from models import ImportBatch

router = APIRouter()

IMPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "import")


@router.post("/upload", response_model=ImportPreviewResponse)
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    text = content.decode("utf-8-sig")
    batch, valid, invalid, new_attributes = import_service.preview_import(db, text, file.filename or "upload.csv")
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


@router.post("/confirm", response_model=ImportBatchOut)
async def confirm_import(
    file: UploadFile = File(...),
    conflict_resolution: str = "skip",
    attribute_actions: str = "{}",  # JSON string of attribute actions
    db: Session = Depends(get_db),
):
    import json
    
    # 在导入前创建备份
    backup_path = None
    try:
        backup_path = backup_service.create_backup_before_import(file.filename or "upload.csv")
    except Exception as e:
        print(f"Warning: Failed to create backup before import: {e}")
        # 备份失败不阻止导入流程，但记录警告
    
    content = await file.read()
    text = content.decode("utf-8-sig")
    actions = json.loads(attribute_actions) if attribute_actions else {}
    batch = import_service.import_csv_records(db, text, file.filename or "upload.csv", conflict_resolution, actions)
    
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
def delete_backup(filename: str):
    """删除指定的备份文件"""
    if backup_service.delete_backup(filename):
        return {"message": "Backup deleted successfully"}
    raise HTTPException(status_code=404, detail="Backup not found")


@router.post("/seed")
def seed_data(db: Session = Depends(get_db)):
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
