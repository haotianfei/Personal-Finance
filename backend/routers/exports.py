from datetime import date
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

from database import get_db
from services import export_service
from models import ExportHistory

router = APIRouter()


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
        logger.warning("export_history_record_failed", detail=str(e), exc_info=True)

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
