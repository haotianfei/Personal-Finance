from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select, func, distinct, delete, asc, desc

from models import AssetRecord, FundType, Account, LiquidityRating, AssetOwner
from schemas import AssetRecordCreate, AssetRecordOut, AssetRecordTemplate
from services.period_service import get_period_start_end, get_previous_period_end


def parse_id_list(id_str: str | None) -> list[int] | None:
    """解析逗号分隔的 ID 字符串为整数列表
    
    Args:
        id_str: 逗号分隔的 ID 字符串，如 "1,2,3"
        
    Returns:
        整数列表或 None
    """
    if not id_str:
        return None
    try:
        return [int(x.strip()) for x in id_str.split(',') if x.strip()]
    except ValueError:
        return None


def list_records(
    db: Session,
    asset_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    fund_type_id: str | None = None,
    account_id: str | None = None,
    owner_id: str | None = None,
    liquidity_rating_id: str | None = None,
    asset_name: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    period_type: str | None = None,
    year: int | None = None,
    quarter: int | None = None,
    month: int | None = None,
    day: int | None = None,
    sort_field: str | None = None,
    sort_order: str | None = "desc",
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[AssetRecord], int]:
    q = select(AssetRecord)

    # Handle period type filtering
    if period_type and period_type != 'all':
        if period_type == 'year' and year:
            date_from = date(year, 1, 1)
            date_to = date(year, 12, 31)
        elif period_type == 'quarter' and year and quarter:
            month_start = (quarter - 1) * 3 + 1
            month_end = quarter * 3
            date_from = date(year, month_start, 1)
            # Calculate last day of end month
            if month_end == 12:
                date_to = date(year, 12, 31)
            else:
                date_to = date(year, month_end + 1, 1) - __import__('datetime').timedelta(days=1)
        elif period_type == 'month' and year and month:
            date_from = date(year, month, 1)
            # Calculate last day of month
            if month == 12:
                date_to = date(year, 12, 31)
            else:
                date_to = date(year, month + 1, 1) - __import__('datetime').timedelta(days=1)
        elif period_type == 'day' and year and month and day:
            asset_date = date(year, month, day)

    if asset_date:
        q = q.where(AssetRecord.asset_date == asset_date)
    if date_from:
        q = q.where(AssetRecord.asset_date >= date_from)
    if date_to:
        q = q.where(AssetRecord.asset_date <= date_to)

    # 处理多值筛选
    fund_type_ids = parse_id_list(fund_type_id)
    if fund_type_ids:
        if len(fund_type_ids) == 1:
            q = q.where(AssetRecord.fund_type_id == fund_type_ids[0])
        else:
            q = q.where(AssetRecord.fund_type_id.in_(fund_type_ids))

    account_ids = parse_id_list(account_id)
    if account_ids:
        if len(account_ids) == 1:
            q = q.where(AssetRecord.account_id == account_ids[0])
        else:
            q = q.where(AssetRecord.account_id.in_(account_ids))

    owner_ids = parse_id_list(owner_id)
    if owner_ids:
        if len(owner_ids) == 1:
            q = q.where(AssetRecord.owner_id == owner_ids[0])
        else:
            q = q.where(AssetRecord.owner_id.in_(owner_ids))

    # 处理流动性评级筛选
    liquidity_rating_ids = parse_id_list(liquidity_rating_id)
    if liquidity_rating_ids:
        if len(liquidity_rating_ids) == 1:
            q = q.where(AssetRecord.liquidity_rating_id == liquidity_rating_ids[0])
        else:
            q = q.where(AssetRecord.liquidity_rating_id.in_(liquidity_rating_ids))

    if asset_name:
        q = q.where(AssetRecord.asset_name.contains(asset_name))

    # 处理金额区间筛选
    if amount_min is not None:
        q = q.where(AssetRecord.amount >= amount_min)
    if amount_max is not None:
        q = q.where(AssetRecord.amount <= amount_max)

    count_q = select(func.count()).select_from(q.subquery())
    total = db.execute(count_q).scalar() or 0

    # 处理排序 - 支持关联表字段排序
    # 定义关联字段映射：前端字段名 -> (关联表, 关联表字段名)
    related_sort_fields = {
        'fund_type_name': (FundType, 'name'),
        'account_name': (Account, 'name'),
        'liquidity_rating_name': (LiquidityRating, 'name'),
        'owner_name': (AssetOwner, 'name'),
    }

    if sort_field:
        if sort_field in related_sort_fields:
            # 关联表字段排序
            related_model, related_column_name = related_sort_fields[sort_field]
            related_column = getattr(related_model, related_column_name)
            
            # 添加 join 和排序
            if sort_field == 'fund_type_name':
                q = q.join(FundType, AssetRecord.fund_type_id == FundType.id, isouter=True)
            elif sort_field == 'account_name':
                q = q.join(Account, AssetRecord.account_id == Account.id, isouter=True)
            elif sort_field == 'liquidity_rating_name':
                q = q.join(LiquidityRating, AssetRecord.liquidity_rating_id == LiquidityRating.id, isouter=True)
            elif sort_field == 'owner_name':
                q = q.join(AssetOwner, AssetRecord.owner_id == AssetOwner.id, isouter=True)
            
            if sort_order == "asc":
                q = q.order_by(asc(related_column))
            else:
                q = q.order_by(desc(related_column))
        else:
            # 主表字段排序
            sort_column = getattr(AssetRecord, sort_field, None)
            if sort_column is not None:
                if sort_order == "asc":
                    q = q.order_by(sort_column.asc())
                else:
                    q = q.order_by(sort_column.desc())
            else:
                # 默认排序
                q = q.order_by(AssetRecord.asset_date.desc(), AssetRecord.asset_name)
    else:
        # 默认排序
        q = q.order_by(AssetRecord.asset_date.desc(), AssetRecord.asset_name)

    q = q.offset((page - 1) * page_size).limit(page_size)

    records = db.execute(q).scalars().all()
    return records, total


def get_record(db: Session, record_id: int) -> AssetRecord | None:
    return db.get(AssetRecord, record_id)


def create_record(db: Session, data: AssetRecordCreate) -> AssetRecord:
    record = AssetRecord(
        asset_date=data.asset_date,
        liquidity_rating_id=data.liquidity_rating_id,
        fund_type_id=data.fund_type_id,
        asset_name=data.asset_name,
        account_id=data.account_id,
        owner_id=data.owner_id,
        amount=data.amount,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def batch_create_records(db: Session, records_data: list[AssetRecordCreate]) -> list[AssetRecord]:
    records = []
    for data in records_data:
        record = AssetRecord(
            asset_date=data.asset_date,
            liquidity_rating_id=data.liquidity_rating_id,
            fund_type_id=data.fund_type_id,
            asset_name=data.asset_name,
            account_id=data.account_id,
            owner_id=data.owner_id,
            amount=data.amount,
        )
        db.add(record)
        records.append(record)
    db.commit()
    for r in records:
        db.refresh(r)
    return records


def update_record(db: Session, record_id: int, data: dict) -> AssetRecord | None:
    record = db.get(AssetRecord, record_id)
    if not record:
        return None
    for key, value in data.items():
        if value is not None:
            setattr(record, key, value)
    record.updated_at = datetime.now()
    db.commit()
    db.refresh(record)
    return record


def delete_record(db: Session, record_id: int) -> bool:
    record = db.get(AssetRecord, record_id)
    if not record:
        return False
    db.delete(record)
    db.commit()
    return True


def get_snapshot_dates(db: Session) -> list[date]:
    result = db.execute(
        select(distinct(AssetRecord.asset_date)).order_by(AssetRecord.asset_date.desc())
    ).scalars().all()
    return list(result)


def get_snapshot_records(db: Session, snapshot_date: date) -> list[AssetRecord]:
    return db.execute(
        select(AssetRecord)
        .where(AssetRecord.asset_date == snapshot_date)
        .order_by(AssetRecord.fund_type_id, AssetRecord.asset_name)
    ).scalars().all()


def find_last_snapshot_date(db: Session, before_date: date, period_type: str) -> date | None:
    if period_type == "day":
        target_before = before_date
    else:
        prev_end = get_previous_period_end(before_date, period_type)
        prev_start, _ = get_period_start_end(prev_end, period_type)
        target_before = prev_end + __import__("datetime").timedelta(days=1)

    result = db.execute(
        select(AssetRecord.asset_date)
        .where(AssetRecord.asset_date < target_before)
        .order_by(AssetRecord.asset_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return result


def copy_from_last_period(
    db: Session, target_date: date, period_type: str
) -> list[dict]:
    last_date = find_last_snapshot_date(db, target_date, period_type)
    if not last_date:
        return []

    records = get_snapshot_records(db, last_date)
    drafts = []
    for r in records:
        drafts.append({
            "asset_date": target_date.isoformat(),
            "liquidity_rating_id": r.liquidity_rating_id,
            "liquidity_rating_name": r.liquidity_rating.name if r.liquidity_rating else "",
            "fund_type_id": r.fund_type_id,
            "fund_type_name": r.fund_type.name if r.fund_type else "",
            "asset_name": r.asset_name,
            "account_id": r.account_id,
            "account_name": r.account.name if r.account else "",
            "amount": str(r.amount),
            "source_date": last_date.isoformat(),
        })
    return drafts


def record_to_out(record: AssetRecord) -> AssetRecordOut:
    return AssetRecordOut(
        id=record.id,
        asset_date=record.asset_date,
        liquidity_rating_id=record.liquidity_rating_id,
        liquidity_rating_name=record.liquidity_rating.name if record.liquidity_rating else None,
        fund_type_id=record.fund_type_id,
        fund_type_name=record.fund_type.name if record.fund_type else None,
        asset_name=record.asset_name,
        account_id=record.account_id,
        account_name=record.account.name if record.account else None,
        owner_id=record.owner_id,
        owner_name=record.owner.name if record.owner else None,
        amount=record.amount,
        import_batch_id=record.import_batch_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def batch_update_records(db: Session, record_ids: list[int], updates: dict) -> int:
    """批量更新资产记录
    
    Args:
        record_ids: 要更新的记录ID列表
        updates: 要更新的字段，字段值为None表示不更新
        
    Returns:
        更新的记录数量
    """
    from sqlalchemy import update
    
    # 过滤掉值为None的字段
    update_data = {k: v for k, v in updates.items() if v is not None}
    
    if not update_data or not record_ids:
        return 0
    
    # 添加更新时间
    update_data['updated_at'] = datetime.now()
    
    # 执行批量更新
    result = db.execute(
        update(AssetRecord)
        .where(AssetRecord.id.in_(record_ids))
        .values(**update_data)
    )
    
    db.commit()
    return result.rowcount


def get_records_by_asset_names(db: Session, asset_names: list[str]) -> list[AssetRecord]:
    """根据资产名称列表获取记录

    Args:
        db: 数据库会话
        asset_names: 资产名称列表

    Returns:
        匹配的记录列表
    """
    if not asset_names:
        return []

    result = db.execute(
        select(AssetRecord)
        .where(AssetRecord.asset_name.in_(asset_names))
        .order_by(AssetRecord.asset_date.desc(), AssetRecord.asset_name)
    ).scalars().all()

    return list(result)


def batch_delete_records(db: Session, record_ids: list[int]) -> int:
    """批量删除资产记录

    Args:
        db: 数据库会话
        record_ids: 要删除的记录ID列表

    Returns:
        删除的记录数量
    """
    if not record_ids:
        return 0

    result = db.execute(
        delete(AssetRecord)
        .where(AssetRecord.id.in_(record_ids))
    )

    db.commit()
    return result.rowcount


def get_asset_history_by_name(db: Session, asset_name: str) -> dict:
    """根据资产名称查询所有历史记录

    Args:
        db: 数据库会话
        asset_name: 资产名称

    Returns:
        包含 total_count, sample_records, all_ids 的字典
    """
    # 获取所有记录（只查询ID用于批量更新）
    all_records_q = select(AssetRecord).where(AssetRecord.asset_name == asset_name)
    all_records = db.execute(all_records_q).scalars().all()

    total_count = len(all_records)
    all_ids = [r.id for r in all_records]

    # 获取示例记录（最新的3条）
    sample_q = (
        select(AssetRecord)
        .where(AssetRecord.asset_name == asset_name)
        .order_by(AssetRecord.asset_date.desc())
        .limit(3)
    )
    sample_records = db.execute(sample_q).scalars().all()

    return {
        "total_count": total_count,
        "sample_records": list(sample_records),
        "all_ids": all_ids
    }


def batch_create_by_period(
    db: Session,
    record_template: AssetRecordTemplate,
    period_type: str,
    start_period: str | None = None,
    end_period: str | None = None,
    conflict_resolution: str = "skip",
) -> dict:
    """批量按账期添加记录
    
    根据模板记录，在历史记录的每个账期中自动添加一条相同的记录。
    日期设置为对应账期的最后一天。
    
    Args:
        db: 数据库会话
        record_template: 记录模板（不含日期）
        period_type: 账期类型 (day, month, quarter, year)
        start_period: 开始账期，None表示从最早账期开始
        end_period: 结束账期，None表示到最新账期结束
    
    Returns:
        包含创建结果的字典
    """
    from services.period_service import group_dates_by_period
    
    # 获取所有历史记录的日期
    dates = get_snapshot_dates(db)
    if not dates:
        return {
            "success": False,
            "created_count": 0,
            "periods": [],
            "records": [],
            "message": "没有历史数据可供参考"
        }
    
    # 按账期类型分组
    period_groups = group_dates_by_period(dates, period_type)
    if not period_groups:
        return {
            "success": False,
            "created_count": 0,
            "periods": [],
            "records": [],
            "message": "无法按指定账期类型分组"
        }
    
    # 获取所有账期并排序
    all_periods = sorted(period_groups.keys())
    
    # 根据 start_period 和 end_period 过滤
    if start_period:
        all_periods = [p for p in all_periods if p >= start_period]
    if end_period:
        all_periods = [p for p in all_periods if p <= end_period]
    
    if not all_periods:
        return {
            "success": False,
            "created_count": 0,
            "periods": [],
            "records": [],
            "message": "指定账期范围内没有数据"
        }
    
    # 为每个账期创建记录
    created_records = []
    skipped_periods = []
    
    for period in all_periods:
        # 获取该账期的所有日期
        period_dates = period_groups[period]
        # 取最后一天作为记录日期
        last_date = max(period_dates)
        
        # 检查是否已存在相同日期和资产名称的记录
        existing_q = select(AssetRecord).where(
            AssetRecord.asset_date == last_date,
            AssetRecord.asset_name == record_template.asset_name
        )
        existing = db.execute(existing_q).scalar_one_or_none()
        
        if existing:
            if conflict_resolution == "overwrite":
                # 覆盖已有记录
                existing.liquidity_rating_id = record_template.liquidity_rating_id
                existing.fund_type_id = record_template.fund_type_id
                existing.account_id = record_template.account_id
                existing.owner_id = record_template.owner_id
                existing.amount = record_template.amount
                existing.updated_at = datetime.now()
                db.commit()
                db.refresh(existing)
                created_records.append(existing)
            else:
                # 跳过
                skipped_periods.append(period)
            continue

        # 创建记录
        record_data = AssetRecordCreate(
            asset_date=last_date,
            liquidity_rating_id=record_template.liquidity_rating_id,
            fund_type_id=record_template.fund_type_id,
            asset_name=record_template.asset_name,
            account_id=record_template.account_id,
            owner_id=record_template.owner_id,
            amount=record_template.amount,
        )
        
        record = create_record(db, record_data)
        created_records.append(record)
    
    # 构建结果消息
    if skipped_periods:
        message = f"成功创建 {len(created_records)} 条记录，跳过 {len(skipped_periods)} 个已存在记录的账期"
    else:
        message = f"成功在 {len(created_records)} 个账期添加记录"
    
    return {
        "success": True,
        "created_count": len(created_records),
        "periods": all_periods,
        "records": created_records,
        "message": message
    }
