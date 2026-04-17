import os
import json
from typing import Optional, List
from pydantic import BaseModel

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas import ImportPreviewResponse, ImportConfirmRequest, ImportBatchOut, NewAttribute
from services import import_service, backup_service
from models import ImportBatch

router = APIRouter()

IMPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "import")


# ==================== CSV Import Endpoints ====================

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


# ==================== Database Import Endpoints ====================

class DbAnalyzeResponse(BaseModel):
    """数据库分析响应"""
    tables: list[dict]
    common_tables: list[str]
    missing_tables: list[str]
    structure_differences: dict
    table_row_counts: dict
    temp_file_id: str


class DbPreviewRequest(BaseModel):
    """数据库导入预览请求"""
    temp_file_id: str
    table_name: str
    limit: int = 10


class DbPreviewResponse(BaseModel):
    """数据库导入预览响应"""
    table_name: str
    total_rows: int
    preview_rows: list[dict]
    common_columns: list[str]
    source_only_columns: list[str]
    target_only_columns: list[str]
    conflict_count: int = 0


class TableImportConfig(BaseModel):
    """表导入配置"""
    table_name: str
    conflict_strategy: str = "skip"  # skip, overwrite, merge
    merge_rules: dict = {}  # 合并规则


class DbImportRequest(BaseModel):
    """数据库导入请求"""
    temp_file_id: str
    table_configs: List[TableImportConfig]


class TableImportResult(BaseModel):
    """表导入结果"""
    table_name: str
    total_rows: int
    imported_rows: int
    skipped_rows: int
    overwritten_rows: int
    merged_rows: int
    errors: list[str]


class DbImportResponse(BaseModel):
    """数据库导入响应"""
    success: bool
    results: list[TableImportResult]
    backup_path: Optional[str] = None
    message: str


class ConflictCheckResponse(BaseModel):
    """冲突检查响应"""
    table_name: str
    conflict_count: int
    primary_key_values: list[dict]


# 临时存储上传的数据库文件路径
_temp_db_files = {}


@router.post("/db/analyze", response_model=DbAnalyzeResponse)
async def analyze_database(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """上传并分析源数据库文件

    分析源数据库的结构，与当前数据库进行比较，返回表结构差异等信息。
    """
    # 检查文件类型
    if not file.filename.endswith('.db') and not file.filename.endswith('.sqlite'):
        raise HTTPException(status_code=400, detail="Only .db or .sqlite files are supported")

    # 读取上传的文件
    content = await file.read()

    # 保存到临时文件
    temp_path = import_service.save_uploaded_db_file(content, file.filename)
    temp_id = os.path.basename(os.path.dirname(temp_path))
    _temp_db_files[temp_id] = temp_path

    try:
        # 分析数据库
        analysis = import_service.analyze_database(temp_path, db)
        analysis["temp_file_id"] = temp_id
        return DbAnalyzeResponse(**analysis)
    except Exception as e:
        # 清理临时文件
        import_service.cleanup_temp_db_file(temp_path)
        if temp_id in _temp_db_files:
            del _temp_db_files[temp_id]
        raise HTTPException(status_code=500, detail=f"Failed to analyze database: {str(e)}")


@router.post("/db/preview", response_model=DbPreviewResponse)
async def preview_db_import(
    request: DbPreviewRequest,
    db: Session = Depends(get_db)
):
    """预览要导入的数据

    预览指定表的数据，包括冲突检测。
    """
    temp_path = _temp_db_files.get(request.temp_file_id)
    if not temp_path or not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Temporary file not found or expired")

    try:
        # 获取预览数据
        preview = import_service.preview_table_import(
            temp_path,
            db,
            request.table_name,
            request.limit
        )

        # 检测冲突
        conflict_info = import_service.detect_conflicts(temp_path, db, request.table_name)
        preview["conflict_count"] = conflict_info.conflict_count

        return DbPreviewResponse(**preview)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to preview import: {str(e)}")


@router.post("/db/check-conflicts", response_model=ConflictCheckResponse)
async def check_conflicts(
    temp_file_id: str = Form(...),
    table_name: str = Form(...),
    db: Session = Depends(get_db)
):
    """检查指定表的主键冲突"""
    temp_path = _temp_db_files.get(temp_file_id)
    if not temp_path or not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Temporary file not found or expired")

    try:
        conflict_info = import_service.detect_conflicts(temp_path, db, table_name)
        return ConflictCheckResponse(
            table_name=conflict_info.table_name,
            conflict_count=conflict_info.conflict_count,
            primary_key_values=conflict_info.primary_key_values[:100]  # 限制返回数量
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check conflicts: {str(e)}")


@router.post("/db/execute", response_model=DbImportResponse)
async def execute_db_import(
    request: DbImportRequest,
    db: Session = Depends(get_db)
):
    """执行数据库导入操作

    根据配置导入指定的表，支持冲突处理策略。
    """
    temp_path = _temp_db_files.get(request.temp_file_id)
    if not temp_path or not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Temporary file not found or expired")

    # 在导入前创建备份
    backup_path = None
    try:
        backup_path = backup_service.create_backup_before_import("db_import")
    except Exception as e:
        print(f"Warning: Failed to create backup before import: {e}")

    try:
        # 转换配置
        table_configs = [
            {
                "table_name": config.table_name,
                "conflict_strategy": config.conflict_strategy,
                "merge_rules": config.merge_rules
            }
            for config in request.table_configs
        ]

        # 执行导入
        results = import_service.import_multiple_tables(temp_path, db, table_configs)

        # 转换结果为响应格式
        result_list = []
        for result in results:
            result_list.append(TableImportResult(
                table_name=result.table_name,
                total_rows=result.total_rows,
                imported_rows=result.imported_rows,
                skipped_rows=result.skipped_rows,
                overwritten_rows=result.overwritten_rows,
                merged_rows=result.merged_rows,
                errors=result.errors
            ))

        # 清理临时文件
        import_service.cleanup_temp_db_file(temp_path)
        if request.temp_file_id in _temp_db_files:
            del _temp_db_files[request.temp_file_id]

        # 计算总导入数
        total_imported = sum(r.imported_rows for r in results)
        total_skipped = sum(r.skipped_rows for r in results)
        total_overwritten = sum(r.overwritten_rows for r in results)
        total_merged = sum(r.merged_rows for r in results)
        total_errors = sum(len(r.errors) for r in results)

        message = f"Import completed: {total_imported} imported, {total_skipped} skipped, {total_overwritten} overwritten, {total_merged} merged"

        # 返回与前端期望一致的格式
        return {
            "success": total_errors == 0,
            "imported_count": total_imported,
            "skipped_count": total_skipped,
            "overwritten_count": total_overwritten,
            "error_count": total_errors,
            "message": message,
            "results": result_list,
            "backup_path": os.path.basename(backup_path) if backup_path else None
        }

    except Exception as e:
        # 清理临时文件
        import_service.cleanup_temp_db_file(temp_path)
        if request.temp_file_id in _temp_db_files:
            del _temp_db_files[request.temp_file_id]
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.post("/db/cleanup")
async def cleanup_temp_file(temp_file_id: str = Form(...)):
    """清理临时数据库文件"""
    temp_path = _temp_db_files.get(temp_file_id)
    if temp_path:
        import_service.cleanup_temp_db_file(temp_path)
        del _temp_db_files[temp_file_id]
        return {"message": "Temporary file cleaned up successfully"}
    return {"message": "File already cleaned up or not found"}


# ==================== Import Status and Management ====================

@router.get("/db/temp-files")
async def list_temp_files():
    """列出当前临时文件（用于调试）"""
    return {
        "temp_files": [
            {"id": k, "path": v, "exists": os.path.exists(v)}
            for k, v in _temp_db_files.items()
        ]
    }


# ==================== CSV/JSON Import Endpoints ====================

# 临时存储上传的 CSV/JSON 文件
_temp_data_files = {}


class DataFileTableInfo(BaseModel):
    """数据文件表信息"""
    name: str
    row_count: int
    columns: list[str]


class DataFileAnalyzeResponse(BaseModel):
    """数据文件分析响应"""
    temp_file_id: str
    file_type: str  # csv 或 json
    tables: list[DataFileTableInfo]
    structure_diffs: dict = {}


class DataFileImportRequest(BaseModel):
    """数据文件导入请求"""
    temp_file_id: str
    table_name: str
    conflict_strategy: str = "skip"


@router.post("/data/analyze", response_model=DataFileAnalyzeResponse)
async def analyze_data_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """上传并分析 CSV/JSON 数据文件

    支持导入之前导出的 CSV 或 JSON 文件。
    """
    # 检查文件类型
    filename = file.filename or ""
    if filename.endswith('.csv'):
        file_type = 'csv'
    elif filename.endswith('.json'):
        file_type = 'json'
    else:
        raise HTTPException(status_code=400, detail="Only .csv or .json files are supported")

    # 读取上传的文件
    content = await file.read()

    # 保存到临时文件
    temp_path = import_service.save_uploaded_data_file(content, filename)
    temp_id = os.path.basename(os.path.dirname(temp_path))
    _temp_data_files[temp_id] = {"path": temp_path, "type": file_type}

    try:
        # 分析文件内容
        if file_type == 'csv':
            preview = import_service.analyze_csv_file(temp_path)
        else:
            preview = import_service.analyze_json_file(temp_path)

        # 构建表信息列表
        tables_info = []
        for table_name in preview.get("tables", []):
            if file_type == 'csv':
                tables_info.append(DataFileTableInfo(
                    name=table_name,
                    row_count=preview.get("total_rows", 0),
                    columns=preview.get("headers", [])
                ))
            else:
                table_preview = preview.get("preview", {}).get(table_name, {})
                tables_info.append(DataFileTableInfo(
                    name=table_name,
                    row_count=table_preview.get("row_count", 0),
                    columns=table_preview.get("columns", [])
                ))

        return DataFileAnalyzeResponse(
            temp_file_id=temp_id,
            file_type=file_type,
            tables=tables_info,
            structure_diffs={}
        )
    except Exception as e:
        # 清理临时文件
        import_service.cleanup_temp_data_file(temp_path)
        if temp_id in _temp_data_files:
            del _temp_data_files[temp_id]
        raise HTTPException(status_code=500, detail=f"Failed to analyze file: {str(e)}")


class DataFilePreviewRequest(BaseModel):
    """数据文件预览请求"""
    temp_file_id: str
    table_name: str


@router.post("/data/preview")
async def preview_data_file_import(
    request: DataFilePreviewRequest,
    db: Session = Depends(get_db)
):
    """预览数据文件导入"""
    file_info = _temp_data_files.get(request.temp_file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="Temporary file not found or expired")

    temp_path = file_info["path"]
    file_type = file_info["type"]

    if not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Temporary file not found")

    try:
        if file_type == 'csv':
            preview = import_service.preview_csv_import(temp_path, db, request.table_name)
        else:
            preview = import_service.preview_json_import(temp_path, db, request.table_name)

        return preview
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to preview import: {str(e)}")


class DataFileExecuteRequest(BaseModel):
    """数据文件执行导入请求"""
    temp_file_id: str
    table_name: str
    conflict_strategy: str = "skip"


@router.post("/data/execute")
async def execute_data_file_import(
    request: DataFileExecuteRequest,
    db: Session = Depends(get_db)
):
    """执行数据文件导入"""
    file_info = _temp_data_files.get(request.temp_file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="Temporary file not found or expired")

    temp_path = file_info["path"]
    file_type = file_info["type"]

    if not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Temporary file not found")

    # 在导入前创建备份
    backup_path = None
    try:
        backup_path = backup_service.create_backup_before_import(f"data_import_{file_type}")
    except Exception as e:
        print(f"Warning: Failed to create backup before import: {e}")

    try:
        if file_type == 'csv':
            result = import_service.import_csv_data_file(temp_path, db, request.table_name, request.conflict_strategy)
        else:
            result = import_service.import_json_data_file(temp_path, db, request.table_name, request.conflict_strategy)

        result["backup_path"] = backup_path
        return result

    except Exception as e:
        import traceback
        print(f"Import error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.post("/data/cleanup")
async def cleanup_data_temp_file(temp_file_id: str = Form(...)):
    """清理临时数据文件"""
    file_info = _temp_data_files.get(temp_file_id)
    if file_info:
        import_service.cleanup_temp_data_file(file_info["path"])
        del _temp_data_files[temp_file_id]
        return {"message": "Temporary file cleaned up successfully"}
    return {"message": "File already cleaned up or not found"}
