import os
from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Body
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from services import export_service
from models import ExportHistory

router = APIRouter()


# 支持的表名列表
SUPPORTED_TABLES = ["accounts", "fund_types", "liquidity_ratings", "asset_owners",
                   "alert_rules", "allocation_targets", "asset_records",
                   "auto_export_rules", "export_history"]


class TableExportRequest(BaseModel):
    """表导出请求"""
    tables: List[str]
    format: str = "json"  # json 或 csv
    filename_prefix: Optional[str] = None


class ExportResponse(BaseModel):
    """导出响应"""
    success: bool
    message: str
    files: List[dict]


class ExportFileInfo(BaseModel):
    """导出文件信息"""
    filename: str
    file_path: str
    file_size: int
    created_at: str
    modified_at: str


class ExportListResponse(BaseModel):
    """导出列表响应"""
    total: int
    files: List[ExportFileInfo]


@router.post("/tables", response_model=ExportResponse)
def export_tables(
    request: TableExportRequest,
    operator: Optional[str] = Query(None, description="操作人名称"),
    db: Session = Depends(get_db),
):
    """导出指定表到文件

    - tables: 要导出的表名列表，支持: accounts, fund_types, liquidity_ratings, asset_owners, alert_rules, allocation_targets, asset_records
    - format: 导出格式，json 或 csv
    - filename_prefix: 文件名前缀（可选）
    """
    # 验证表名
    invalid_tables = [t for t in request.tables if t not in SUPPORTED_TABLES]
    if invalid_tables:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid table names: {invalid_tables}. Supported: {SUPPORTED_TABLES}"
        )

    # 验证格式
    if request.format not in ["json", "csv", "db"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format: {request.format}. Supported: json, csv, db"
        )

    prefix = request.filename_prefix or "export"
    exported_files = []

    try:
        if request.format == "json":
            # 导出为单个JSON文件
            filepath = export_service.export_tables_to_json(
                db, request.tables, filename_prefix=prefix
            )
            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)

            # 记录导出历史
            history = ExportHistory(
                export_type="manual",
                filename=filename,
                file_size=file_size,
                operator=operator,
                rule_name=None,
                file_path=filepath,
            )
            db.add(history)
            db.commit()

            exported_files.append({
                "filename": filename,
                "file_path": filepath,
                "file_size": file_size,
            })
        elif request.format == "db":
            # 导出为SQLite数据库文件
            filepath = export_service.export_tables_to_sqlite(
                db, request.tables, filename_prefix=prefix
            )
            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)

            # 记录导出历史
            history = ExportHistory(
                export_type="manual",
                filename=filename,
                file_size=file_size,
                operator=operator,
                rule_name=None,
                file_path=filepath,
            )
            db.add(history)
            db.commit()

            exported_files.append({
                "filename": filename,
                "file_path": filepath,
                "file_size": file_size,
            })
        else:
            # 导出为多个CSV文件
            filepaths = export_service.export_tables_to_csv(
                db, request.tables, filename_prefix=prefix
            )

            for filepath in filepaths:
                filename = os.path.basename(filepath)
                file_size = os.path.getsize(filepath)

                # 记录导出历史
                history = ExportHistory(
                    export_type="manual",
                    filename=filename,
                    file_size=file_size,
                    operator=operator,
                    rule_name=None,
                    file_path=filepath,
                )
                db.add(history)

                exported_files.append({
                    "filename": filename,
                    "file_path": filepath,
                    "file_size": file_size,
                })

            db.commit()

        return ExportResponse(
            success=True,
            message=f"Successfully exported {len(request.tables)} table(s) to {request.format}",
            files=exported_files
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.post("/backup", response_model=ExportResponse)
def create_full_backup(
    operator: Optional[str] = Query(None, description="操作人名称"),
    db: Session = Depends(get_db),
):
    """创建完整数据库备份（包含所有表）"""
    try:
        filepath = export_service.create_full_backup(db)
        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)

        # 记录导出历史
        history = ExportHistory(
            export_type="manual",
            filename=filename,
            file_size=file_size,
            operator=operator,
            rule_name="full_backup",
            file_path=filepath,
        )
        db.add(history)
        db.commit()

        return ExportResponse(
            success=True,
            message="Full backup created successfully",
            files=[{
                "filename": filename,
                "file_path": filepath,
                "file_size": file_size,
            }]
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@router.get("/download/{filename}")
def download_export_file(
    filename: str,
    db: Session = Depends(get_db),
):
    """下载导出的文件"""
    filepath = export_service.get_export_file_path(filename)

    if not filepath:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    # 根据文件扩展名设置媒体类型
    if filename.endswith('.json'):
        media_type = "application/json"
    elif filename.endswith('.csv'):
        media_type = "text/csv"
    else:
        media_type = "application/octet-stream"

    return FileResponse(
        filepath,
        media_type=media_type,
        filename=filename
    )


@router.get("/", response_model=ExportListResponse)
def list_export_files(
    db: Session = Depends(get_db),
):
    """列出所有导出文件"""
    files = export_service.list_export_files()

    return ExportListResponse(
        total=len(files),
        files=[ExportFileInfo(**f) for f in files]
    )


@router.get("/list", response_model=ExportListResponse)
def list_export_files_alias(
    db: Session = Depends(get_db),
):
    """列出所有导出文件（别名）"""
    files = export_service.list_export_files()

    return ExportListResponse(
        total=len(files),
        files=[ExportFileInfo(**f) for f in files]
    )


@router.delete("/files/{filename}")
def delete_export_file(
    filename: str,
    db: Session = Depends(get_db),
):
    """删除导出文件"""
    success = export_service.delete_export_file(filename)

    if not success:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    return {"success": True, "message": f"File {filename} deleted successfully"}


@router.get("/supported-tables")
def get_supported_tables():
    """获取支持的表名列表"""
    return {
        "tables": SUPPORTED_TABLES,
        "descriptions": {
            "accounts": "账户信息",
            "fund_types": "基金类型",
            "liquidity_ratings": "流动性评级",
            "asset_owners": "资产所有者",
            "alert_rules": "预警规则",
            "allocation_targets": "配置目标",
            "asset_records": "资产记录",
            "auto_export_rules": "自动导出规则",
            "export_history": "导出历史",
        }
    }


# 保留原有的导出端点以保持兼容性
@router.get("/download")
def export_csv(
    period_type: Optional[str] = Query("all", description="Export period type: all, day, month, quarter, year, custom"),
    year: Optional[int] = Query(None, description="Year filter"),
    month: Optional[int] = Query(None, description="Month filter (1-12)"),
    quarter: Optional[int] = Query(None, description="Quarter filter (1-4)"),
    day: Optional[int] = Query(None, description="Day filter (1-31)"),
    date_from: Optional[date] = Query(None, description="Start date for custom range (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="End date for custom range (YYYY-MM-DD)"),
    operator: Optional[str] = Query(None, description="Operator name for manual export"),
    db: Session = Depends(get_db),
):
    """Export asset records to CSV with optional period filtering."""
    records = export_service.get_export_records(
        db,
        period_type=period_type,
        year=year,
        month=month,
        quarter=quarter,
        day=day,
        date_from=date_from,
        date_to=date_to,
    )

    csv_content = export_service.generate_csv(records)
    filename = export_service.get_export_filename(
        period_type=period_type,
        year=year,
        month=month,
        quarter=quarter,
        day=day,
        date_from=date_from,
        date_to=date_to,
    )

    # 记录导出历史
    try:
        file_size = len(csv_content.encode('utf-8'))
        history = ExportHistory(
            export_type="manual",
            filename=filename,
            file_size=file_size,
            operator=operator,
            rule_name=None,
            file_path=None,  # 手动导出不存储文件路径
        )
        db.add(history)
        db.commit()
    except Exception as e:
        print(f"Warning: Failed to record export history: {e}")

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/preview")
def preview_export(
    period_type: Optional[str] = Query("all", description="Export period type: all, day, month, quarter, year, custom"),
    year: Optional[int] = Query(None, description="Year filter"),
    month: Optional[int] = Query(None, description="Month filter (1-12)"),
    quarter: Optional[int] = Query(None, description="Quarter filter (1-4)"),
    day: Optional[int] = Query(None, description="Day filter (1-31)"),
    date_from: Optional[date] = Query(None, description="Start date for custom range (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="End date for custom range (YYYY-MM-DD)"),
    limit: int = Query(10, description="Number of records to preview"),
    db: Session = Depends(get_db),
):
    """Preview export data (first N records) without downloading."""
    records = export_service.get_export_records(
        db,
        period_type=period_type,
        year=year,
        month=month,
        quarter=quarter,
        day=day,
        date_from=date_from,
        date_to=date_to,
    )

    return {
        "total_count": len(records),
        "preview": records[:limit],
        "filename": export_service.get_export_filename(
            period_type=period_type,
            year=year,
            month=month,
            quarter=quarter,
            day=day,
            date_from=date_from,
            date_to=date_to,
        ),
    }
