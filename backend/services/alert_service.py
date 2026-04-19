from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, distinct, func, and_

from models import AlertRule, AssetRecord, FundType, Account, LiquidityRating
from schemas import AlertRuleCreate, AlertRuleUpdate, AlertRuleOut, AlertResult, AlertDetailItem
from services.period_service import (
    get_period_label, get_period_start_end, get_previous_period_end,
    group_dates_by_period,
)


def get_alert_rules(db: Session, active_only: bool = False) -> list[AlertRule]:
    query = select(AlertRule).order_by(AlertRule.created_at.desc())
    if active_only:
        query = query.where(AlertRule.is_active == True)
    return list(db.execute(query).scalars().all())


def get_alert_rule(db: Session, rule_id: int) -> Optional[AlertRule]:
    return db.execute(select(AlertRule).where(AlertRule.id == rule_id)).scalar_one_or_none()


def create_alert_rule(db: Session, rule: AlertRuleCreate) -> AlertRule:
    db_rule = AlertRule(**rule.model_dump())
    db.add(db_rule)
    db.commit()
    db.refresh(db_rule)
    return db_rule


def update_alert_rule(db: Session, rule_id: int, rule: AlertRuleUpdate) -> Optional[AlertRule]:
    db_rule = get_alert_rule(db, rule_id)
    if not db_rule:
        return None
    update_data = rule.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_rule, key, value)
    db.commit()
    db.refresh(db_rule)
    return db_rule


def delete_alert_rule(db: Session, rule_id: int) -> bool:
    db_rule = get_alert_rule(db, rule_id)
    if not db_rule:
        return False
    db.delete(db_rule)
    db.commit()
    return True


def _get_all_snapshot_dates(db: Session) -> list[date]:
    result = db.execute(
        select(distinct(AssetRecord.asset_date)).order_by(AssetRecord.asset_date)
    ).scalars().all()
    return list(result)


def _get_amount_for_period(
    db: Session, 
    period: str, 
    dimension: str, 
    target_id: Optional[str]
) -> Decimal:
    dates = _get_all_snapshot_dates(db)
    
    # Determine period type from period string format
    if period_type := _detect_period_type(period):
        groups = group_dates_by_period(dates, period_type)
    else:
        return Decimal("0")
    
    if period not in groups:
        return Decimal("0")
    
    period_dates = groups[period]
    if not period_dates:
        return Decimal("0")
    
    max_date = max(period_dates)
    
    query = select(func.coalesce(func.sum(AssetRecord.amount), 0))
    
    # Always filter by date
    query = query.where(AssetRecord.asset_date == max_date)
    
    # Apply dimension-specific filters
    if dimension == "asset_name" and target_id:
        query = query.where(AssetRecord.asset_name == target_id)
    elif dimension == "fund_type" and target_id:
        try:
            type_id = int(target_id)
            type_ids = _get_descendant_ids(db, type_id)
            query = query.where(AssetRecord.fund_type_id.in_(type_ids))
        except ValueError:
            return Decimal("0")
    elif dimension == "liquidity_rating" and target_id:
        rating = db.execute(
            select(LiquidityRating.id).where(LiquidityRating.name == target_id)
        ).scalar_one_or_none()
        if rating:
            query = query.where(AssetRecord.liquidity_rating_id == rating)
        else:
            return Decimal("0")
    elif dimension == "account" and target_id:
        try:
            account_id = int(target_id)
            query = query.where(AssetRecord.account_id == account_id)
        except ValueError:
            return Decimal("0")
    # If target_id is None, we sum all records for that date (no additional filter)
    
    result = db.execute(query).scalar()
    return Decimal(str(result)) if result else Decimal("0")


def _detect_period_type(period: str) -> Optional[str]:
    """Detect period type from period string format."""
    if "-Q" in period:
        return "quarter"
    elif "-W" in period:
        return "week"
    elif len(period) == 4 and period.isdigit():
        return "year"
    elif period.count("-") == 2:
        return "day"
    elif period.count("-") == 1:
        return "month"
    return None


def _get_descendant_ids(db: Session, fund_type_id: int) -> list[int]:
    ids = [fund_type_id]
    children = db.execute(
        select(FundType.id).where(FundType.parent_id == fund_type_id)
    ).scalars().all()
    for child_id in children:
        ids.extend(_get_descendant_ids(db, child_id))
    return ids


def _get_previous_period(period: str, period_type: str) -> Optional[str]:
    if period_type == "day":
        parts = period.split("-")
        if len(parts) == 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            d = date(year, month, day)
            prev_date = get_previous_period_end(d, "day")
            return prev_date.isoformat()
    elif period_type == "week":
        parts = period.split("-W")
        if len(parts) == 2:
            year, week = int(parts[0]), int(parts[1])
            # Get the first day of this week
            d = date.fromisocalendar(year, week, 1)
            prev_date = get_previous_period_end(d, "week")
            prev_year, prev_week, _ = prev_date.isocalendar()
            return f"{prev_year}-W{prev_week:02d}"
    elif period_type == "month":
        parts = period.split("-")
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            d = date(year, month, 1)
            prev_date = get_previous_period_end(d, "month")
            return f"{prev_date.year}-{prev_date.month:02d}"
    elif period_type == "quarter":
        parts = period.split("-Q")
        if len(parts) == 2:
            year, q = int(parts[0]), int(parts[1])
            prev_q = q - 1
            prev_year = year
            if prev_q < 1:
                prev_q = 4
                prev_year = year - 1
            return f"{prev_year}-Q{prev_q}"
    elif period_type == "year":
        try:
            year = int(period)
            return str(year - 1)
        except ValueError:
            return None
    return None


def _get_last_period_with_data(
    db: Session,
    current_period: str,
    period_type: str,
) -> Optional[str]:
    """Find the most recent period with data before current_period.
    
    Args:
        db: Database session
        current_period: Current period label (e.g., "2024-03", "2024-Q1", "2024", "2024-03-15")
        period_type: Period type ("day", "month", "quarter", "year")
    
    Returns:
        The most recent period label with data, or None if no previous data exists
    """
    dates = _get_all_snapshot_dates(db)
    if not dates:
        return None
    
    groups = group_dates_by_period(dates, period_type)
    if not groups:
        return None
    
    # Get sorted periods (ascending)
    sorted_periods = sorted(groups.keys())
    
    # Find current period index
    try:
        current_idx = sorted_periods.index(current_period)
    except ValueError:
        # Current period not in list, find where it would be inserted
        for i, p in enumerate(sorted_periods):
            if p > current_period:
                current_idx = i
                break
        else:
            current_idx = len(sorted_periods)
    
    # Return the previous period if exists
    if current_idx > 0:
        return sorted_periods[current_idx - 1]
    
    return None


def _get_target_name(db: Session, dimension: str, target_id: Optional[str]) -> str:
    if not target_id:
        return "全部"
    
    if dimension == "asset_name":
        return target_id
    elif dimension == "fund_type":
        try:
            type_id = int(target_id)
            ft = db.execute(select(FundType).where(FundType.id == type_id)).scalar_one_or_none()
            return ft.name if ft else target_id
        except ValueError:
            return target_id
    elif dimension == "liquidity_rating":
        return target_id
    elif dimension == "account":
        try:
            account_id = int(target_id)
            acc = db.execute(select(Account).where(Account.id == account_id)).scalar_one_or_none()
            return acc.name if acc else target_id
        except ValueError:
            return target_id
    return target_id


def _get_detail_changes(
    db: Session,
    dimension: str,
    current_period: str,
    compare_period: str,
    amount_threshold: Optional[Decimal],
    percent_threshold: Optional[Decimal],
    direction: str,
) -> list[AlertDetailItem]:
    """Get detailed changes for each item when target_id is null (all)."""
    dates = _get_all_snapshot_dates(db)
    period_type = _detect_period_type(current_period)
    if not period_type:
        return []
    
    groups = group_dates_by_period(dates, period_type)
    if current_period not in groups or compare_period not in groups:
        return []
    
    current_dates = groups[current_period]
    compare_dates = groups[compare_period]
    if not current_dates or not compare_dates:
        return []
    
    current_max_date = max(current_dates)
    compare_max_date = max(compare_dates)
    
    details = []
    
    if dimension == "asset_name":
        # Get all asset names from both periods
        asset_names = db.execute(
            select(distinct(AssetRecord.asset_name))
            .where(AssetRecord.asset_date.in_([current_max_date, compare_max_date]))
        ).scalars().all()
        
        for asset_name in asset_names:
            current = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.asset_name == asset_name, AssetRecord.asset_date == current_max_date)
            ).scalar() or 0
            
            compare = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.asset_name == asset_name, AssetRecord.asset_date == compare_max_date)
            ).scalar() or 0
            
            current = Decimal(str(current))
            compare = Decimal(str(compare))
            change = current - compare
            change_pct = float(change / abs(compare) * 100) if compare != 0 else None
            
            # Check if this item triggers the alert
            item_triggered = _check_triggered(change, change_pct, amount_threshold, percent_threshold, direction)
            
            details.append(AlertDetailItem(
                id=asset_name,
                name=asset_name,
                current_amount=current,
                compare_amount=compare,
                change_amount=change,
                change_percent=change_pct,
                triggered=item_triggered,
            ))
    
    elif dimension == "fund_type":
        # Get all fund types
        fund_types = db.execute(select(FundType)).scalars().all()
        
        for ft in fund_types:
            type_ids = _get_descendant_ids(db, ft.id)
            
            current = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.fund_type_id.in_(type_ids), AssetRecord.asset_date == current_max_date)
            ).scalar() or 0
            
            compare = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.fund_type_id.in_(type_ids), AssetRecord.asset_date == compare_max_date)
            ).scalar() or 0
            
            current = Decimal(str(current))
            compare = Decimal(str(compare))
            change = current - compare
            change_pct = float(change / abs(compare) * 100) if compare != 0 else None
            
            item_triggered = _check_triggered(change, change_pct, amount_threshold, percent_threshold, direction)
            
            details.append(AlertDetailItem(
                id=str(ft.id),
                name=ft.name,
                current_amount=current,
                compare_amount=compare,
                change_amount=change,
                change_percent=change_pct,
                triggered=item_triggered,
            ))
    
    elif dimension == "liquidity_rating":
        # Get all liquidity ratings
        ratings = db.execute(select(LiquidityRating)).scalars().all()
        
        for rating in ratings:
            current = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.liquidity_rating_id == rating.id, AssetRecord.asset_date == current_max_date)
            ).scalar() or 0
            
            compare = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.liquidity_rating_id == rating.id, AssetRecord.asset_date == compare_max_date)
            ).scalar() or 0
            
            current = Decimal(str(current))
            compare = Decimal(str(compare))
            change = current - compare
            change_pct = float(change / abs(compare) * 100) if compare != 0 else None
            
            item_triggered = _check_triggered(change, change_pct, amount_threshold, percent_threshold, direction)
            
            details.append(AlertDetailItem(
                id=rating.name,
                name=rating.name,
                current_amount=current,
                compare_amount=compare,
                change_amount=change,
                change_percent=change_pct,
                triggered=item_triggered,
            ))
    
    elif dimension == "account":
        # Get all accounts
        accounts = db.execute(select(Account)).scalars().all()
        
        for acc in accounts:
            current = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.account_id == acc.id, AssetRecord.asset_date == current_max_date)
            ).scalar() or 0
            
            compare = db.execute(
                select(func.coalesce(func.sum(AssetRecord.amount), 0))
                .where(AssetRecord.account_id == acc.id, AssetRecord.asset_date == compare_max_date)
            ).scalar() or 0
            
            current = Decimal(str(current))
            compare = Decimal(str(compare))
            change = current - compare
            change_pct = float(change / abs(compare) * 100) if compare != 0 else None
            
            item_triggered = _check_triggered(change, change_pct, amount_threshold, percent_threshold, direction)
            
            details.append(AlertDetailItem(
                id=str(acc.id),
                name=acc.name,
                current_amount=current,
                compare_amount=compare,
                change_amount=change,
                change_percent=change_pct,
                triggered=item_triggered,
            ))
    
    # Sort by absolute change amount (descending)
    details.sort(key=lambda x: abs(x.change_amount), reverse=True)
    return details


def _check_triggered(
    change_amount: Decimal,
    change_percent: Optional[float],
    amount_threshold: Optional[Decimal],
    percent_threshold: Optional[Decimal],
    direction: str,
) -> bool:
    """Check if a change triggers the alert thresholds."""
    if amount_threshold is not None:
        if abs(change_amount) >= amount_threshold:
            if direction == "both":
                return True
            elif direction == "up" and change_amount > 0:
                return True
            elif direction == "down" and change_amount < 0:
                return True
    
    if percent_threshold is not None and change_percent is not None:
        if abs(change_percent) >= float(percent_threshold):
            if direction == "both":
                return True
            elif direction == "up" and change_percent > 0:
                return True
            elif direction == "down" and change_percent < 0:
                return True
    
    return False


def check_alert(db: Session, rule_id: int, current_period: Optional[str] = None) -> Optional[AlertResult]:
    rule = get_alert_rule(db, rule_id)
    if not rule or not rule.is_active:
        return None
    
    dates = _get_all_snapshot_dates(db)
    if not dates:
        return None
    
    groups = group_dates_by_period(dates, rule.period_type)
    if not groups:
        return None
    
    if current_period is None:
        current_period = list(groups.keys())[-1]
    
    if rule.compare_type == "previous":
        # Use new logic: find the most recent period with data
        compare_period = _get_last_period_with_data(db, current_period, rule.period_type)
    else:
        compare_period = rule.compare_period
    
    if not compare_period:
        # Return a result indicating no previous data available
        return AlertResult(
            rule_id=rule.id,
            rule_name=rule.name,
            dimension=rule.dimension,
            target_name=_get_target_name(db, rule.dimension, rule.target_id),
            current_period=current_period,
            compare_period=None,
            current_amount=_get_amount_for_period(db, current_period, rule.dimension, rule.target_id),
            compare_amount=Decimal("0"),
            change_amount=Decimal("0"),
            change_percent=None,
            amount_threshold=rule.amount_threshold,
            percent_threshold=rule.percent_threshold,
            direction=rule.direction,
            triggered=False,
            details=[],
            message="无上一期数据可供对比",
        )
    
    current_amount = _get_amount_for_period(db, current_period, rule.dimension, rule.target_id)
    compare_amount = _get_amount_for_period(db, compare_period, rule.dimension, rule.target_id)
    
    change_amount = current_amount - compare_amount
    change_percent = None
    if compare_amount != 0:
        change_percent = float(change_amount / abs(compare_amount) * 100)
    
    triggered = _check_triggered(change_amount, change_percent, rule.amount_threshold, rule.percent_threshold, rule.direction)
    
    # Get detailed changes if target_id is null (all items)
    details = []
    if rule.target_id is None:
        details = _get_detail_changes(
            db, rule.dimension, current_period, compare_period,
            rule.amount_threshold, rule.percent_threshold, rule.direction
        )
    
    return AlertResult(
        rule_id=rule.id,
        rule_name=rule.name,
        dimension=rule.dimension,
        target_name=_get_target_name(db, rule.dimension, rule.target_id),
        current_period=current_period,
        compare_period=compare_period,
        current_amount=current_amount,
        compare_amount=compare_amount,
        change_amount=change_amount,
        change_percent=change_percent,
        amount_threshold=rule.amount_threshold,
        percent_threshold=rule.percent_threshold,
        direction=rule.direction,
        triggered=triggered,
        details=details,
    )


def get_alert_results(db: Session, current_period: Optional[str] = None) -> list[AlertResult]:
    rules = get_alert_rules(db, active_only=True)
    results = []
    for rule in rules:
        result = check_alert(db, rule.id, current_period)
        if result:
            results.append(result)
    return results


def get_available_periods(db: Session, period_type: str) -> list[dict]:
    dates = _get_all_snapshot_dates(db)
    groups = group_dates_by_period(dates, period_type)
    
    periods = []
    for label, period_dates in groups.items():
        periods.append({
            "label": label,
            "date": max(period_dates).isoformat(),
            "count": len(period_dates),
        })
    
    return sorted(periods, key=lambda x: x["date"], reverse=True)
