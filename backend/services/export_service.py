import csv
import io
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, and_

from models import AssetRecord, FundType, Account, LiquidityRating


def get_export_records(
    db: Session,
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> list[dict]:
    """Get records for export with optional period filtering.
    
    Args:
        period_type: 'all', 'day', 'month', 'quarter', 'year', 'custom'
        year: Filter by year (required for month, quarter, day)
        month: Filter by month (1-12, required for day)
        quarter: Filter by quarter (1-4)
        day: Filter by day (1-31)
        date_from: Start date for custom range filter
        date_to: End date for custom range filter
    """
    query = (
        select(
            AssetRecord.asset_date,
            LiquidityRating.name.label("liquidity_rating"),
            FundType.name.label("fund_type"),
            AssetRecord.asset_name,
            Account.name.label("account"),
            AssetRecord.amount,
        )
        .join(LiquidityRating, AssetRecord.liquidity_rating_id == LiquidityRating.id)
        .join(FundType, AssetRecord.fund_type_id == FundType.id)
        .join(Account, AssetRecord.account_id == Account.id)
        .order_by(AssetRecord.asset_date, AssetRecord.asset_name)
    )
    
    # Apply period filters
    if period_type and period_type != "all":
        conditions = []
        
        if year:
            conditions.append(AssetRecord.asset_date >= date(year, 1, 1))
            conditions.append(AssetRecord.asset_date <= date(year, 12, 31))
        
        if period_type == "month" and month:
            conditions.append(AssetRecord.asset_date >= date(year, month, 1))
            if month == 12:
                conditions.append(AssetRecord.asset_date <= date(year + 1, 1, 1))
            else:
                conditions.append(AssetRecord.asset_date < date(year, month + 1, 1))
        
        elif period_type == "quarter" and quarter:
            start_month = (quarter - 1) * 3 + 1
            end_month = quarter * 3 + 1
            conditions.append(AssetRecord.asset_date >= date(year, start_month, 1))
            if end_month > 12:
                conditions.append(AssetRecord.asset_date < date(year + 1, 1, 1))
            else:
                conditions.append(AssetRecord.asset_date < date(year, end_month, 1))
        
        elif period_type == "day" and month and day:
            conditions.append(AssetRecord.asset_date == date(year, month, day))
        
        elif period_type == "custom" and date_from and date_to:
            conditions.append(AssetRecord.asset_date >= date_from)
            conditions.append(AssetRecord.asset_date <= date_to)
        
        if conditions:
            query = query.where(and_(*conditions))
    
    results = db.execute(query).all()
    
    records = []
    for r in results:
        records.append({
            "asset_date": r.asset_date.strftime("%Y-%m-%d"),
            "liquidity_rating": r.liquidity_rating,
            "fund_type": r.fund_type,
            "asset_name": r.asset_name,
            "account": r.account,
            "amount": str(r.amount),
        })
    
    return records


def generate_csv(records: list[dict]) -> str:
    """Generate CSV content from records."""
    if not records:
        return ""
    
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["asset_date", "liquidity_rating", "fund_type", "asset_name", "account", "amount"],
        lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def get_export_filename(
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> str:
    """Generate export filename based on filters."""
    if period_type == "all" or not period_type:
        return "assets_export_all.csv"
    
    if period_type == "custom" and date_from and date_to:
        return f"assets_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.csv"
    
    parts = ["assets"]
    if year:
        parts.append(str(year))
    if period_type == "quarter" and quarter:
        parts.append(f"Q{quarter}")
    elif period_type == "month" and month:
        parts.append(f"{month:02d}")
    elif period_type == "day" and month and day:
        parts.append(f"{month:02d}{day:02d}")
    
    return "_".join(parts) + ".csv"
