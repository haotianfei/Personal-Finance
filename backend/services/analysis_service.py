from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import select, func, distinct, case, and_, text

from models import AssetRecord, FundType, LiquidityRating
from schemas import (
    TrendPoint, ComparisonPoint, MixedChartData,
    ItemTrendPoint, SummaryData,
)
from services.period_service import (
    group_dates_by_period, get_representative_date, get_period_label,
)


def _get_all_snapshot_dates(db: Session, date_from: date | None = None, date_to: date | None = None) -> list[date]:
    q = select(distinct(AssetRecord.asset_date)).order_by(AssetRecord.asset_date)
    if date_from:
        q = q.where(AssetRecord.asset_date >= date_from)
    if date_to:
        q = q.where(AssetRecord.asset_date <= date_to)
    return list(db.execute(q).scalars().all())


def _get_latest_records_for_dates(db: Session, snapshot_dates: list[date]) -> list[tuple]:
    """For a set of dates within a period, get each asset's latest record value.

    Logic: for each unique asset_name, find the record with the maximum asset_date
    among the given snapshot_dates, and use that record's amount.

    Returns list of (asset_name, amount, fund_type_id, liquidity_rating_id, account_id) tuples.
    """
    if not snapshot_dates:
        return []

    # Subquery: for each asset_name, find the max date within the given dates
    max_date_sub = (
        select(
            AssetRecord.asset_name,
            func.max(AssetRecord.asset_date).label("max_date"),
        )
        .where(AssetRecord.asset_date.in_(snapshot_dates))
        .group_by(AssetRecord.asset_name)
        .subquery()
    )

    # Main query: join back to get the amount at the max date for each asset
    result = db.execute(
        select(
            AssetRecord.asset_name,
            AssetRecord.amount,
            AssetRecord.fund_type_id,
            AssetRecord.liquidity_rating_id,
            AssetRecord.account_id,
        )
        .join(
            max_date_sub,
            and_(
                AssetRecord.asset_name == max_date_sub.c.asset_name,
                AssetRecord.asset_date == max_date_sub.c.max_date,
            ),
        )
    ).all()

    return result


def _sum_for_period_dates(db: Session, snapshot_dates: list[date]) -> tuple[Decimal, Decimal, Decimal]:
    """Sum the latest value of each asset across the given dates in a period.

    Returns (total, positive_assets, negative_liabilities).
    """
    records = _get_latest_records_for_dates(db, snapshot_dates)
    if not records:
        return Decimal("0"), Decimal("0"), Decimal("0")

    total = Decimal("0")
    pos = Decimal("0")
    neg = Decimal("0")
    for _name, amount, _ft_id, _liq_rating, _acc_id in records:
        amt = Decimal(str(amount))
        total += amt
        if amt > 0:
            pos += amt
        elif amt < 0:
            neg += amt

    return total, pos, neg


def _sum_for_date(db: Session, d: date) -> tuple[Decimal, Decimal, Decimal]:
    """Shortcut for single-date sum (backward compat for dashboard summary)."""
    return _sum_for_period_dates(db, [d])


def get_summary(db: Session) -> SummaryData:
    dates = _get_all_snapshot_dates(db)
    if not dates:
        return SummaryData()

    latest = dates[-1]
    total, assets, liabilities = _sum_for_date(db, latest)

    change = Decimal("0")
    change_pct = None
    if len(dates) >= 2:
        prev_date = dates[-2]
        prev_total, _, _ = _sum_for_date(db, prev_date)
        change = total - prev_total
        if prev_total != 0:
            change_pct = float(change / prev_total * 100)

    record_count = db.execute(
        select(func.count()).where(AssetRecord.asset_date == latest)
    ).scalar() or 0

    return SummaryData(
        latest_date=latest,
        total_assets=assets,  # 正资产总和
        total_liabilities=liabilities,  # 负资产总和（绝对值）
        net_worth=total,  # 净值 = 总资产 + 总负债（代数和）
        change_amount=change,
        change_percent=change_pct,
        record_count=record_count,
        snapshot_count=len(dates),
    )


def get_total_trend(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[TrendPoint]:
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        total, assets, liabilities = _sum_for_period_dates(db, group_dates)
        # net_worth = assets + liabilities (liabilities is negative)
        net_worth = assets + liabilities
        points.append(TrendPoint(
            period=label,
            date=rep_date,
            total_amount=net_worth,  # 使用净资产作为总资产趋势
            net_assets=assets,
            liabilities=liabilities,
        ))

    return points


def get_period_comparison(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ComparisonPoint]:
    trend = get_total_trend(db, period_type, date_from, date_to)
    if not trend:
        return []

    result = []
    for i, pt in enumerate(trend):
        if i == 0:
            result.append(ComparisonPoint(
                period=pt.period, date=pt.date, total=pt.total_amount,
                change_amount=Decimal("0"), change_percent=None,
            ))
        else:
            prev = trend[i - 1]
            change = pt.total_amount - prev.total_amount
            pct = float(change / prev.total_amount * 100) if prev.total_amount != 0 else None
            result.append(ComparisonPoint(
                period=pt.period, date=pt.date, total=pt.total_amount,
                change_amount=change, change_percent=pct,
            ))
    return result


def get_mixed_chart(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> MixedChartData:
    trend = get_total_trend(db, period_type, date_from, date_to)
    comparison = get_period_comparison(db, period_type, date_from, date_to)
    return MixedChartData(trend=trend, comparison=comparison)


def get_trend_by_item(
    db: Session, asset_name: str, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []
    prev_amount = None

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        # For a specific item, find its latest record within the period dates
        max_date_sub = (
            select(func.max(AssetRecord.asset_date))
            .where(
                AssetRecord.asset_name == asset_name,
                AssetRecord.asset_date.in_(group_dates),
            )
        ).scalar_subquery()

        result = db.execute(
            select(func.coalesce(func.sum(AssetRecord.amount), 0))
            .where(
                AssetRecord.asset_name == asset_name,
                AssetRecord.asset_date == max_date_sub,
            )
        ).scalar()
        amount = Decimal(str(result))
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=amount, change_amount=change,
        ))
        prev_amount = amount

    return points


def get_trend_by_type(
    db: Session, fund_type_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    type_ids = _get_descendant_ids(db, fund_type_id)

    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []
    prev_amount = None

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        # For assets matching the type filter, get each one's latest value in the period
        records = _get_latest_records_for_dates(db, group_dates)
        amount = sum(
            Decimal(str(r[1])) for r in records if r[2] in type_ids
        )
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=Decimal(str(amount)),
            change_amount=Decimal(str(change)) if change is not None else None,
        ))
        prev_amount = amount

    return points


def get_trend_by_liquidity_rating(
    db: Session, liquidity_rating: str, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    # Get liquidity_rating_id by name
    rating = db.execute(
        select(LiquidityRating).where(LiquidityRating.name == liquidity_rating)
    ).scalar_one_or_none()
    if not rating:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []
    prev_amount = None

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        # For assets matching the liquidity rating, get each one's latest value in the period
        records = _get_latest_records_for_dates(db, group_dates)
        amount = sum(
            Decimal(str(r[1])) for r in records if r[3] == rating.id
        )
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=Decimal(str(amount)),
            change_amount=Decimal(str(change)) if change is not None else None,
        ))
        prev_amount = amount

    return points


def get_trend_by_account(
    db: Session, account_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []
    prev_amount = None

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        # For assets matching the account, get each one's latest value in the period
        records = _get_latest_records_for_dates(db, group_dates)
        amount = sum(
            Decimal(str(r[1])) for r in records if r[4] == account_id
        )
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=Decimal(str(amount)),
            change_amount=Decimal(str(change)) if change is not None else None,
        ))
        prev_amount = amount

    return points


def _get_descendant_ids(db: Session, fund_type_id: int) -> list[int]:
    ids = [fund_type_id]
    children = db.execute(
        select(FundType.id).where(FundType.parent_id == fund_type_id)
    ).scalars().all()
    for child_id in children:
        ids.extend(_get_descendant_ids(db, child_id))
    return ids
