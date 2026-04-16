from datetime import date
from decimal import Decimal
from typing import Optional, Literal

from sqlalchemy.orm import Session
from sqlalchemy import select, func, distinct

from models import AssetRecord, FundType, Account, LiquidityRating
from services.period_service import get_period_label, get_representative_date


ProportionDimension = Literal["liquidity_rating", "fund_type", "asset_name", "account"]


def _get_latest_snapshot_date(db: Session) -> Optional[date]:
    """获取最新的快照日期"""
    result = db.execute(
        select(func.max(AssetRecord.asset_date))
    ).scalar()
    return result


def get_available_periods(db: Session, period_type: str = "month") -> list[dict]:
    """获取可用的账期列表
    
    Args:
        period_type: 账期类型 - day(日), month(月), quarter(季度), year(年)
    
    Returns:
        账期列表，每个账期包含 label 和代表性日期
    """
    dates = db.execute(
        select(distinct(AssetRecord.asset_date)).order_by(AssetRecord.asset_date.desc())
    ).scalars().all()
    
    if not dates:
        return []
    
    # 按账期分组
    period_groups: dict[str, list[date]] = {}
    for d in dates:
        label = get_period_label(d, period_type)
        if label not in period_groups:
            period_groups[label] = []
        period_groups[label].append(d)
    
    # 为每个账期获取代表性日期（该账期内最新的日期）
    periods = []
    for label, period_dates in period_groups.items():
        rep_date = get_representative_date(period_dates)
        periods.append({
            "label": label,
            "date": rep_date.isoformat(),
            "count": len(period_dates)
        })
    
    # 按日期倒序排列
    periods.sort(key=lambda x: x["date"], reverse=True)
    return periods


def get_proportion_by_liquidity_rating(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按流动性评级分析占比，同时返回资产和负债"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return {"asset_items": [], "liability_items": [], "total_assets": Decimal("0"), "total_liabilities": Decimal("0")}

    # 获取每个资产在指定日期的最新记录
    subq = (
        select(
            AssetRecord.asset_name,
            func.max(AssetRecord.asset_date).label("max_date")
        )
        .where(AssetRecord.asset_date <= snapshot_date)
        .group_by(AssetRecord.asset_name)
    ).subquery()

    # 统计正资产（amount > 0）
    asset_result = db.execute(
        select(
            LiquidityRating.name.label("liquidity_rating_name"),
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(LiquidityRating, AssetRecord.liquidity_rating_id == LiquidityRating.id)
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount > 0)
        .group_by(LiquidityRating.id, LiquidityRating.name)
        .order_by(func.sum(AssetRecord.amount).desc())
    ).all()

    # 统计负资产/负债（amount < 0）
    liability_result = db.execute(
        select(
            LiquidityRating.name.label("liquidity_rating_name"),
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(LiquidityRating, AssetRecord.liquidity_rating_id == LiquidityRating.id)
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount < 0)
        .group_by(LiquidityRating.id, LiquidityRating.name)
        .order_by(func.sum(AssetRecord.amount).asc())
    ).all()

    total_assets = sum(r.total_amount for r in asset_result) or Decimal("0")
    total_liabilities = abs(sum(r.total_amount for r in liability_result)) or Decimal("0")
    # 资产和负债各自占100%，使用不同的分母
    total_for_asset_percent = total_assets if total_assets > 0 else Decimal("1")
    total_for_liability_percent = total_liabilities if total_liabilities > 0 else Decimal("1")

    asset_items = [
        {
            "name": r.liquidity_rating_name,
            "amount": r.total_amount,
            "count": r.asset_count,
            "percent": round(float(r.total_amount / total_for_asset_percent * 100), 2),
            "type": "asset"
        }
        for r in asset_result
    ]

    liability_items = [
        {
            "name": r.liquidity_rating_name,
            "amount": abs(r.total_amount),
            "count": r.asset_count,
            "percent": round(float(abs(r.total_amount) / total_for_liability_percent * 100), 2),
            "type": "liability"
        }
        for r in liability_result
    ]

    return {
        "asset_items": asset_items,
        "liability_items": liability_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities
    }


def get_proportion_by_fund_type(
    db: Session,
    snapshot_date: Optional[date] = None,
    level: Optional[int] = None
) -> dict:
    """按资产类型分析占比，同时返回资产和负债
    
    Args:
        level: 指定层级，None表示显示所有层级，0表示根级，1表示一级子级，以此类推
               选择层级后，会聚合该层级下的所有子级数据
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return {"asset_items": [], "liability_items": [], "total_assets": Decimal("0"), "total_liabilities": Decimal("0")}

    # 获取每个资产在指定日期的最新记录
    subq = (
        select(
            AssetRecord.asset_name,
            func.max(AssetRecord.asset_date).label("max_date")
        )
        .where(AssetRecord.asset_date <= snapshot_date)
        .group_by(AssetRecord.asset_name)
    ).subquery()

    # 获取所有 fund_type 的层级关系
    fund_types = db.execute(select(FundType)).scalars().all()
    fund_type_map = {ft.id: ft for ft in fund_types}

    def get_node_at_or_above_level(ft_id: int, target_level: int) -> Optional[int]:
        """获取指定层级或以上的节点ID
        
        - 如果节点层级 == target_level: 返回自己
        - 如果节点层级 > target_level: 向上查找到 target_level 的祖先
        - 如果节点层级 < target_level: 返回自己（高于目标层级的也显示）
        """
        ft = fund_type_map.get(ft_id)
        if not ft:
            return None
        
        # 如果当前类型层级 <= 目标层级，返回自己（显示该节点）
        if ft.level <= target_level:
            return ft.id
        
        # 如果当前类型层级 > 目标层级，向上查找目标层级的祖先
        current = ft
        while current and current.parent_id:
            parent = fund_type_map.get(current.parent_id)
            if parent and parent.level == target_level:
                return parent.id
            current = parent
        
        # 如果没找到目标层级的祖先，返回None
        return None

    # 统计正资产（amount > 0）
    asset_result_raw = db.execute(
        select(
            AssetRecord.fund_type_id,
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount > 0)
        .group_by(AssetRecord.fund_type_id)
    ).all()

    # 统计负资产/负债（amount < 0）
    liability_result_raw = db.execute(
        select(
            AssetRecord.fund_type_id,
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount < 0)
        .group_by(AssetRecord.fund_type_id)
    ).all()

    # 按层级聚合数据
    from collections import defaultdict
    
    asset_by_level = defaultdict(lambda: {"amount": Decimal("0"), "count": 0})
    liability_by_level = defaultdict(lambda: {"amount": Decimal("0"), "count": 0})

    for r in asset_result_raw:
        ft = fund_type_map.get(r.fund_type_id)
        if not ft:
            continue
            
        if level is not None:
            # 获取指定层级或以上的节点
            node_id = get_node_at_or_above_level(r.fund_type_id, level)
            if node_id:
                ft = fund_type_map.get(node_id)
            else:
                continue  # 跳过没有该层级祖先的记录
        
        asset_by_level[ft.id]["amount"] += r.total_amount
        asset_by_level[ft.id]["count"] += r.asset_count

    for r in liability_result_raw:
        ft = fund_type_map.get(r.fund_type_id)
        if not ft:
            continue
            
        if level is not None:
            # 获取指定层级或以上的节点
            node_id = get_node_at_or_above_level(r.fund_type_id, level)
            if node_id:
                ft = fund_type_map.get(node_id)
            else:
                continue  # 跳过没有该层级祖先的记录
        
        liability_by_level[ft.id]["amount"] += abs(r.total_amount)
        liability_by_level[ft.id]["count"] += r.asset_count

    total_assets = sum(v["amount"] for v in asset_by_level.values()) or Decimal("0")
    total_liabilities = sum(v["amount"] for v in liability_by_level.values()) or Decimal("0")
    
    # 资产和负债各自占100%，使用不同的分母
    total_for_asset_percent = total_assets if total_assets > 0 else Decimal("1")
    total_for_liability_percent = total_liabilities if total_liabilities > 0 else Decimal("1")

    asset_items = [
        {
            "name": fund_type_map[ft_id].name,
            "amount": v["amount"],
            "count": v["count"],
            "percent": round(float(v["amount"] / total_for_asset_percent * 100), 2),
            "type": "asset",
            "level": fund_type_map[ft_id].level
        }
        for ft_id, v in sorted(asset_by_level.items(), key=lambda x: x[1]["amount"], reverse=True)
    ]

    liability_items = [
        {
            "name": fund_type_map[ft_id].name,
            "amount": v["amount"],
            "count": v["count"],
            "percent": round(float(v["amount"] / total_for_liability_percent * 100), 2),
            "type": "liability",
            "level": fund_type_map[ft_id].level
        }
        for ft_id, v in sorted(liability_by_level.items(), key=lambda x: x[1]["amount"], reverse=True)
    ]

    return {
        "asset_items": asset_items,
        "liability_items": liability_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "level": level
    }


def get_proportion_by_asset_name(
    db: Session,
    snapshot_date: Optional[date] = None,
    top_n: int = 20
) -> dict:
    """按资产名称分析占比，返回Top N + 其他，同时返回资产和负债"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return {"asset_items": [], "liability_items": [], "total_assets": Decimal("0"), "total_liabilities": Decimal("0")}

    # 获取每个资产在指定日期的最新记录
    subq = (
        select(
            AssetRecord.asset_name,
            func.max(AssetRecord.asset_date).label("max_date")
        )
        .where(AssetRecord.asset_date <= snapshot_date)
        .group_by(AssetRecord.asset_name)
    ).subquery()

    # 统计正资产（amount > 0）
    asset_result = db.execute(
        select(
            AssetRecord.asset_name,
            func.sum(AssetRecord.amount).label("total_amount")
        )
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount > 0)
        .group_by(AssetRecord.asset_name)
        .order_by(func.sum(AssetRecord.amount).desc())
    ).all()

    # 统计负资产/负债（amount < 0）
    liability_result = db.execute(
        select(
            AssetRecord.asset_name,
            func.sum(AssetRecord.amount).label("total_amount")
        )
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount < 0)
        .group_by(AssetRecord.asset_name)
        .order_by(func.sum(AssetRecord.amount).asc())
    ).all()

    total_assets = sum(r.total_amount for r in asset_result) or Decimal("0")
    total_liabilities = abs(sum(r.total_amount for r in liability_result)) or Decimal("0")
    # 资产和负债各自占100%，使用不同的分母
    total_for_asset_percent = total_assets if total_assets > 0 else Decimal("1")
    total_for_liability_percent = total_liabilities if total_liabilities > 0 else Decimal("1")

    # 处理资产项 - Top N + 其他
    asset_items = []
    others_amount = Decimal("0")

    for i, r in enumerate(asset_result):
        if i < top_n:
            asset_items.append({
                "name": r.asset_name,
                "amount": r.total_amount,
                "count": 1,
                "percent": round(float(r.total_amount / total_for_asset_percent * 100), 2),
                "type": "asset"
            })
        else:
            others_amount += r.total_amount

    if others_amount > 0:
        asset_items.append({
            "name": "其他资产",
            "amount": others_amount,
            "count": len(asset_result) - top_n,
            "percent": round(float(others_amount / total_for_asset_percent * 100), 2),
            "type": "asset"
        })

    # 处理负债项
    liability_items = [
        {
            "name": r.asset_name,
            "amount": abs(r.total_amount),
            "count": 1,
            "percent": round(float(abs(r.total_amount) / total_for_liability_percent * 100), 2),
            "type": "liability"
        }
        for r in liability_result
    ]

    return {
        "asset_items": asset_items,
        "liability_items": liability_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities
    }


def get_proportion_by_account(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按账户分析占比，同时返回资产和负债"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return {"asset_items": [], "liability_items": [], "total_assets": Decimal("0"), "total_liabilities": Decimal("0")}

    # 获取每个资产在指定日期的最新记录
    subq = (
        select(
            AssetRecord.asset_name,
            func.max(AssetRecord.asset_date).label("max_date")
        )
        .where(AssetRecord.asset_date <= snapshot_date)
        .group_by(AssetRecord.asset_name)
    ).subquery()

    # 统计正资产（amount > 0）
    asset_result = db.execute(
        select(
            Account.name.label("account_name"),
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(Account, AssetRecord.account_id == Account.id)
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount > 0)
        .group_by(Account.id, Account.name)
        .order_by(func.sum(AssetRecord.amount).desc())
    ).all()

    # 统计负资产/负债（amount < 0）
    liability_result = db.execute(
        select(
            Account.name.label("account_name"),
            func.sum(AssetRecord.amount).label("total_amount"),
            func.count(func.distinct(AssetRecord.asset_name)).label("asset_count")
        )
        .join(Account, AssetRecord.account_id == Account.id)
        .join(
            subq,
            (AssetRecord.asset_name == subq.c.asset_name) &
            (AssetRecord.asset_date == subq.c.max_date)
        )
        .where(AssetRecord.amount < 0)
        .group_by(Account.id, Account.name)
        .order_by(func.sum(AssetRecord.amount).asc())
    ).all()

    total_assets = sum(r.total_amount for r in asset_result) or Decimal("0")
    total_liabilities = abs(sum(r.total_amount for r in liability_result)) or Decimal("0")
    # 资产和负债各自占100%，使用不同的分母
    total_for_asset_percent = total_assets if total_assets > 0 else Decimal("1")
    total_for_liability_percent = total_liabilities if total_liabilities > 0 else Decimal("1")

    asset_items = [
        {
            "name": r.account_name,
            "amount": r.total_amount,
            "count": r.asset_count,
            "percent": round(float(r.total_amount / total_for_asset_percent * 100), 2),
            "type": "asset"
        }
        for r in asset_result
    ]

    liability_items = [
        {
            "name": r.account_name,
            "amount": abs(r.total_amount),
            "count": r.asset_count,
            "percent": round(float(abs(r.total_amount) / total_for_liability_percent * 100), 2),
            "type": "liability"
        }
        for r in liability_result
    ]

    return {
        "asset_items": asset_items,
        "liability_items": liability_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities
    }


def get_proportion_data(
    db: Session,
    dimension: ProportionDimension,
    snapshot_date: Optional[date] = None,
    level: Optional[int] = None
) -> dict:
    """获取指定维度的占比数据，包含资产和负债
    
    Args:
        level: 仅对 fund_type 维度有效，指定层级进行聚合分析
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if dimension == "liquidity_rating":
        result = get_proportion_by_liquidity_rating(db, snapshot_date)
    elif dimension == "fund_type":
        result = get_proportion_by_fund_type(db, snapshot_date, level)
    elif dimension == "asset_name":
        result = get_proportion_by_asset_name(db, snapshot_date)
    elif dimension == "account":
        result = get_proportion_by_account(db, snapshot_date)
    else:
        result = {
            "asset_items": [],
            "liability_items": [],
            "total_assets": Decimal("0"),
            "total_liabilities": Decimal("0"),
            "net_worth": Decimal("0")
        }

    # 合并资产和负债项用于图表显示
    all_items = result.get("asset_items", []) + result.get("liability_items", [])
    total_count = sum(item.get("count", 0) for item in all_items)

    return {
        "dimension": dimension,
        "snapshot_date": snapshot_date,
        "level": level,
        "total_assets": result.get("total_assets", Decimal("0")),
        "total_liabilities": result.get("total_liabilities", Decimal("0")),
        "net_worth": result.get("net_worth", Decimal("0")),
        "total_count": total_count,
        "asset_items": result.get("asset_items", []),
        "liability_items": result.get("liability_items", []),
        "items": all_items  # 兼容旧接口
    }


def get_all_proportions(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """获取所有维度的占比数据"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    return {
        "snapshot_date": snapshot_date,
        "liquidity_rating": get_proportion_by_liquidity_rating(db, snapshot_date),
        "fund_type": get_proportion_by_fund_type(db, snapshot_date),
        "asset_name": get_proportion_by_asset_name(db, snapshot_date),
        "account": get_proportion_by_account(db, snapshot_date),
    }