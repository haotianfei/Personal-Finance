from datetime import date, timedelta
from calendar import monthrange


def get_period_label(d: date, period_type: str) -> str:
    if period_type == "day":
        return d.isoformat()
    elif period_type == "week":
        # ISO week format: YYYY-WNN
        year, week, _ = d.isocalendar()
        return f"{year}-W{week:02d}"
    elif period_type == "month":
        return f"{d.year}-{d.month:02d}"
    elif period_type == "quarter":
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    elif period_type == "year":
        return str(d.year)
    return d.isoformat()


def get_period_start_end(d: date, period_type: str) -> tuple[date, date]:
    if period_type == "day":
        return d, d
    elif period_type == "week":
        # ISO week: Monday is the first day
        year, week, weekday = d.isocalendar()
        # Calculate the Monday of this week
        monday = d - timedelta(days=weekday - 1)
        # Calculate the Sunday of this week
        sunday = monday + timedelta(days=6)
        return monday, sunday
    elif period_type == "month":
        last_day = monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 1), date(d.year, d.month, last_day)
    elif period_type == "quarter":
        q = (d.month - 1) // 3
        start_month = q * 3 + 1
        end_month = start_month + 2
        last_day = monthrange(d.year, end_month)[1]
        return date(d.year, start_month, 1), date(d.year, end_month, last_day)
    elif period_type == "year":
        return date(d.year, 1, 1), date(d.year, 12, 31)
    return d, d


def get_previous_period_end(d: date, period_type: str) -> date:
    start, _ = get_period_start_end(d, period_type)
    return start - timedelta(days=1)


def group_dates_by_period(dates: list[date], period_type: str) -> dict[str, list[date]]:
    groups: dict[str, list[date]] = {}
    for d in sorted(dates):
        label = get_period_label(d, period_type)
        groups.setdefault(label, []).append(d)
    return groups


def get_representative_date(dates: list[date]) -> date:
    return max(dates)
