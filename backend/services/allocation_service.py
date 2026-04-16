from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from models import AllocationTarget, AssetRecord, FundType, Account, LiquidityRating
from schemas import (
    AllocationTargetCreate, AllocationTargetUpdate, AllocationTargetOut,
    AllocationAnalysis, AllocationAnalysisItem
)


def get_allocation_targets(db: Session, dimension: Optional[str] = None) -> list[AllocationTarget]:
    """Get all allocation targets, optionally filtered by dimension."""
    query = select(AllocationTarget)
    if dimension:
        query = query.where(AllocationTarget.dimension == dimension)
    return list(db.execute(query).scalars().all())


def get_allocation_target(db: Session, target_id: int) -> Optional[AllocationTarget]:
    """Get a single allocation target by ID."""
    return db.execute(
        select(AllocationTarget).where(AllocationTarget.id == target_id)
    ).scalar_one_or_none()


def create_allocation_target(db: Session, target: AllocationTargetCreate) -> AllocationTarget:
    """Create a new allocation target."""
    db_target = AllocationTarget(**target.model_dump())
    db.add(db_target)
    db.commit()
    db.refresh(db_target)
    return db_target


def get_child_allocation_targets(db: Session, parent_id: int) -> list[AllocationTarget]:
    """Get all child allocation targets for a given parent."""
    return list(db.execute(
        select(AllocationTarget).where(AllocationTarget.parent_id == parent_id)
    ).scalars().all())


def _auto_adjust_children_recursive(
    db: Session,
    parent_id: int,
    old_parent_percent: Decimal,
    new_parent_percent: Decimal,
    auto_adjusted_children: list[dict]
) -> None:
    """Recursively auto-adjust children and grandchildren when parent target is decreased.
    
    Args:
        db: Database session
        parent_id: Parent target ID
        old_parent_percent: Parent's old percentage
        new_parent_percent: Parent's new percentage
        auto_adjusted_children: List to collect all adjusted children
    """
    from decimal import Decimal
    
    # Get direct children
    children = get_child_allocation_targets(db, parent_id)
    if not children:
        return
    
    # Calculate scaling ratio based on parent change
    if old_parent_percent > 0:
        ratio = new_parent_percent / old_parent_percent
    else:
        return
    
    # Adjust each child
    for child in children:
        old_child_percent = child.target_percent
        new_child_percent = (old_child_percent * ratio).quantize(Decimal("0.01"))
        
        # Allow zero, but ensure we don't go below 0
        if new_child_percent < Decimal("0"):
            new_child_percent = Decimal("0")
        
        # Only record if actually changed
        if new_child_percent != old_child_percent:
            child.target_percent = new_child_percent
            auto_adjusted_children.append({
                "child_id": child.target_id,
                "old_percent": str(old_child_percent),
                "new_percent": str(new_child_percent),
            })
            
            # Recursively adjust grandchildren
            _auto_adjust_children_recursive(
                db,
                child.id,
                old_child_percent,
                new_child_percent,
                auto_adjusted_children
            )


def update_allocation_target(
    db: Session, target_id: int, target: AllocationTargetUpdate
) -> tuple[Optional[AllocationTarget], list[dict]]:
    """Update an existing allocation target and auto-adjust children if parent target is decreased.
    
    Returns:
        tuple: (updated_target, list of auto-adjusted children with old/new percentages)
    """
    from decimal import Decimal
    
    db_target = get_allocation_target(db, target_id)
    if not db_target:
        return None, []
    
    old_percent = db_target.target_percent
    auto_adjusted_children = []
    
    # Check if target_percent is being decreased
    new_percent = target.target_percent
    if new_percent is not None and new_percent < old_percent:
        # Recursively adjust all children and grandchildren
        _auto_adjust_children_recursive(
            db, target_id, old_percent, new_percent, auto_adjusted_children
        )
    
    # Update the parent target
    update_data = target.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_target, key, value)
    
    db.commit()
    db.refresh(db_target)
    return db_target, auto_adjusted_children


def delete_allocation_target(db: Session, target_id: int) -> bool:
    """Delete an allocation target."""
    db_target = get_allocation_target(db, target_id)
    if not db_target:
        return False
    
    db.delete(db_target)
    db.commit()
    return True


def get_total_assets(db: Session, snapshot_date: date) -> Decimal:
    """Get total assets for a specific date."""
    result = db.execute(
        select(func.coalesce(func.sum(AssetRecord.amount), 0))
        .where(AssetRecord.asset_date == snapshot_date)
    ).scalar()
    return Decimal(str(result)) if result else Decimal("0")


def _get_fund_type_tree(db: Session) -> dict[int, dict]:
    """Get fund type hierarchy as a tree."""
    fund_types = db.execute(select(FundType)).scalars().all()
    ft_map = {ft.id: {"id": ft.id, "name": ft.name, "parent_id": ft.parent_id, "level": ft.level, "children": []} for ft in fund_types}
    
    # Build tree
    root_items = []
    for ft_id, ft_data in ft_map.items():
        if ft_data["parent_id"] is None:
            root_items.append(ft_data)
        else:
            parent = ft_map.get(ft_data["parent_id"])
            if parent:
                parent["children"].append(ft_data)
    
    return ft_map, root_items


def _get_descendant_ids(db: Session, fund_type_id: int) -> list[int]:
    """Get all descendant fund type IDs including self."""
    ids = [fund_type_id]
    children = db.execute(
        select(FundType.id).where(FundType.parent_id == fund_type_id)
    ).scalars().all()
    for child_id in children:
        ids.extend(_get_descendant_ids(db, child_id))
    return ids


def _get_fund_type_amount(db: Session, fund_type_id: int, snapshot_date: date) -> Decimal:
    """Get total amount for a fund type including descendants."""
    type_ids = _get_descendant_ids(db, fund_type_id)
    result = db.execute(
        select(func.coalesce(func.sum(AssetRecord.amount), 0))
        .where(
            AssetRecord.fund_type_id.in_(type_ids),
            AssetRecord.asset_date == snapshot_date
        )
    ).scalar()
    return Decimal(str(result)) if result else Decimal("0")


def _calculate_deviation_metrics(
    actual_percent: Decimal,
    target_percent: Optional[Decimal],
    total_amount: Decimal
) -> dict:
    """Calculate deviation metrics including deviation amount and priority."""
    if target_percent is None:
        return {
            "deviation": actual_percent,
            "deviation_percent": None,
            "deviation_amount": None,
            "recommendation": "未设置目标",
            "priority": 0,
        }
    
    # Absolute deviation
    deviation = actual_percent - target_percent
    
    # Relative deviation rate
    deviation_percent = (deviation / target_percent * 100) if target_percent > 0 else None
    
    # Deviation amount
    deviation_amount = (total_amount * abs(deviation) / 100) if total_amount > 0 else None
    
    # Generate recommendation and priority
    abs_deviation = abs(deviation)
    if abs_deviation <= 5:
        recommendation = "配置合理"
        priority = 0
    elif deviation > 5:
        # 超配：实际 > 目标，需要减持
        adjust_percent = deviation
        adjust_amount = deviation_amount if deviation_amount else Decimal("0")
        recommendation = f"超配{adjust_percent:.1f}%，建议减持约{adjust_amount:,.0f}元"
        priority = int(abs_deviation * 10)  # Higher deviation = higher priority
    else:
        # 低配：实际 < 目标，需要增持
        adjust_percent = abs_deviation
        adjust_amount = deviation_amount if deviation_amount else Decimal("0")
        recommendation = f"低配{adjust_percent:.1f}%，建议增持约{adjust_amount:,.0f}元"
        priority = int(abs_deviation * 10)
    
    return {
        "deviation": deviation,
        "deviation_percent": deviation_percent,
        "deviation_amount": deviation_amount,
        "recommendation": recommendation,
        "priority": priority,
    }


def _build_fund_type_analysis(
    db: Session,
    snapshot_date: date,
    total_amount: Decimal,
    targets: dict[str, Decimal],
    ft_data: dict,
) -> AllocationAnalysisItem:
    """Recursively build fund type analysis with enhanced metrics."""
    actual_amount = _get_fund_type_amount(db, ft_data["id"], snapshot_date)
    actual_percent = (actual_amount / total_amount * 100) if total_amount > 0 else Decimal("0")
    
    target_id_str = str(ft_data["id"])
    target_percent = targets.get(target_id_str)
    
    # Calculate deviation metrics
    metrics = _calculate_deviation_metrics(actual_percent, target_percent, total_amount)
    
    # Build children
    children = []
    for child_data in ft_data.get("children", []):
        child = _build_fund_type_analysis(db, snapshot_date, total_amount, targets, child_data)
        children.append(child)
    
    return AllocationAnalysisItem(
        id=target_id_str,
        name=ft_data["name"],
        parent_id=str(ft_data["parent_id"]) if ft_data["parent_id"] else None,
        level=ft_data["level"],
        target_percent=target_percent,
        actual_percent=actual_percent,
        actual_amount=actual_amount,
        deviation=metrics["deviation"],
        deviation_percent=metrics["deviation_percent"],
        deviation_amount=metrics["deviation_amount"],
        recommendation=metrics["recommendation"],
        priority=metrics["priority"],
        children=children,
    )


def analyze_fund_type_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """Analyze fund type allocation with enhanced metrics."""
    total_amount = get_total_assets(db, snapshot_date)
    
    # Get all targets for fund_type dimension
    all_targets = get_allocation_targets(db, "fund_type")
    targets = {
        t.target_id: t.target_percent
        for t in all_targets
    }
    
    # Get fund type tree
    ft_map, root_items = _get_fund_type_tree(db)
    
    # Build analysis for root items
    items = []
    for root_data in root_items:
        item = _build_fund_type_analysis(db, snapshot_date, total_amount, targets, root_data)
        items.append(item)
    
    # Sort items by priority (descending)
    items.sort(key=lambda x: x.priority or 0, reverse=True)
    
    # Calculate unallocated percent - only count root level targets (parent_id is None)
    root_targets = [t for t in all_targets if t.parent_id is None]
    allocated = sum(t.target_percent for t in root_targets)
    unallocated = max(Decimal("100") - allocated, Decimal("0"))
    
    return AllocationAnalysis(
        dimension="fund_type",
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=unallocated,
    )


def analyze_liquidity_rating_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """Analyze liquidity rating allocation with enhanced metrics."""
    total_amount = get_total_assets(db, snapshot_date)
    
    # Get all targets for liquidity_rating dimension
    all_targets = get_allocation_targets(db, "liquidity_rating")
    targets = {
        t.target_id: t.target_percent
        for t in all_targets
    }
    
    # Get all liquidity ratings
    ratings = db.execute(select(LiquidityRating).order_by(LiquidityRating.sort_order)).scalars().all()
    
    items = []
    for rating in ratings:
        actual_amount = db.execute(
            select(func.coalesce(func.sum(AssetRecord.amount), 0))
            .where(
                AssetRecord.liquidity_rating_id == rating.id,
                AssetRecord.asset_date == snapshot_date
            )
        ).scalar() or 0
        actual_amount = Decimal(str(actual_amount))
        actual_percent = (actual_amount / total_amount * 100) if total_amount > 0 else Decimal("0")
        
        target_percent = targets.get(rating.name)
        
        # Calculate deviation metrics
        metrics = _calculate_deviation_metrics(actual_percent, target_percent, total_amount)
        
        items.append(AllocationAnalysisItem(
            id=rating.name,
            name=rating.name,
            parent_id=None,
            level=0,
            target_percent=target_percent,
            actual_percent=actual_percent,
            actual_amount=actual_amount,
            deviation=metrics["deviation"],
            deviation_percent=metrics["deviation_percent"],
            deviation_amount=metrics["deviation_amount"],
            recommendation=metrics["recommendation"],
            priority=metrics["priority"],
            children=[],
        ))
    
    # Sort items by priority (descending)
    items.sort(key=lambda x: x.priority or 0, reverse=True)
    
    # Calculate unallocated percent - only count root level targets (parent_id is None)
    root_targets = [t for t in all_targets if t.parent_id is None]
    allocated = sum(t.target_percent for t in root_targets)
    unallocated = max(Decimal("100") - allocated, Decimal("0"))
    
    return AllocationAnalysis(
        dimension="liquidity_rating",
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=unallocated,
    )


def analyze_account_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """Analyze account allocation with enhanced metrics."""
    total_amount = get_total_assets(db, snapshot_date)
    
    # Get all targets for account dimension
    all_targets = get_allocation_targets(db, "account")
    targets = {
        t.target_id: t.target_percent
        for t in all_targets
    }
    
    # Get all accounts
    accounts = db.execute(select(Account).order_by(Account.name)).scalars().all()
    
    items = []
    for account in accounts:
        actual_amount = db.execute(
            select(func.coalesce(func.sum(AssetRecord.amount), 0))
            .where(
                AssetRecord.account_id == account.id,
                AssetRecord.asset_date == snapshot_date
            )
        ).scalar() or 0
        actual_amount = Decimal(str(actual_amount))
        actual_percent = (actual_amount / total_amount * 100) if total_amount > 0 else Decimal("0")
        
        target_id_str = str(account.id)
        target_percent = targets.get(target_id_str)
        
        # Calculate deviation metrics
        metrics = _calculate_deviation_metrics(actual_percent, target_percent, total_amount)
        
        items.append(AllocationAnalysisItem(
            id=target_id_str,
            name=account.name,
            parent_id=None,
            level=0,
            target_percent=target_percent,
            actual_percent=actual_percent,
            actual_amount=actual_amount,
            deviation=metrics["deviation"],
            deviation_percent=metrics["deviation_percent"],
            deviation_amount=metrics["deviation_amount"],
            recommendation=metrics["recommendation"],
            priority=metrics["priority"],
            children=[],
        ))
    
    # Sort items by priority (descending)
    items.sort(key=lambda x: x.priority or 0, reverse=True)
    
    # Calculate unallocated percent - only count root level targets (parent_id is None)
    root_targets = [t for t in all_targets if t.parent_id is None]
    allocated = sum(t.target_percent for t in root_targets)
    unallocated = max(Decimal("100") - allocated, Decimal("0"))
    
    return AllocationAnalysis(
        dimension="account",
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=unallocated,
    )


def analyze_allocation(
    db: Session, dimension: str, snapshot_date: date
) -> AllocationAnalysis:
    """Analyze allocation for a specific dimension."""
    if dimension == "fund_type":
        return analyze_fund_type_allocation(db, snapshot_date)
    elif dimension == "liquidity_rating":
        return analyze_liquidity_rating_allocation(db, snapshot_date)
    elif dimension == "account":
        return analyze_account_allocation(db, snapshot_date)
    else:
        raise ValueError(f"Unknown dimension: {dimension}")


def validate_allocation_target(
    db: Session, dimension: str, target_id: str, target_percent: Decimal, parent_id: Optional[int] = None
) -> tuple[bool, str]:
    """Validate allocation target constraints with enhanced rules.
    
    Rules:
    1. Root level: 0% < value ≤ 100%
    2. Child level: 0% < value ≤ parent target percent
    3. Root level sum ≤ 100%
    4. Child level sum within same parent ≤ parent target
    """
    # Rule 1 & 2: Value range validation
    if target_percent <= 0:
        return False, "比例必须大于0%"
    
    if target_percent > 100:
        return False, "比例不能超过100%"
    
    # Get existing targets for this dimension
    existing = get_allocation_targets(db, dimension)
    
    # Rule 2: Child level constraint (value ≤ parent target)
    if dimension == "fund_type" and parent_id:
        parent_target = get_allocation_target(db, parent_id)
        if parent_target and target_percent > parent_target.target_percent:
            return False, f"不能超过父级目标比例 ({parent_target.target_percent:.2f}%)"
    
    # Rule 3: Root level sum ≤ 100%
    # Calculate current total excluding the target being updated
    current_total = sum(
        t.target_percent for t in existing
        if t.target_id != target_id and t.parent_id is None
    )
    
    # If this is a root level target (no parent), check total constraint
    if parent_id is None:
        new_total = current_total + target_percent
        if new_total > 100:
            return False, f"该层级总配置比例不能超过100%，当前已配置{current_total:.2f}%"
    
    # Rule 4: Child level sum within same parent ≤ parent target
    if dimension == "fund_type" and parent_id:
        siblings_sum = sum(
            t.target_percent for t in existing
            if t.parent_id == parent_id and t.target_id != target_id
        )
        parent_target = get_allocation_target(db, parent_id)
        if parent_target:
            new_siblings_sum = siblings_sum + target_percent
            if new_siblings_sum > parent_target.target_percent:
                remaining = parent_target.target_percent - siblings_sum
                return False, f"该父级下子项总和不能超过{parent_target.target_percent:.2f}%，还可配置{remaining:.2f}%"
    
    return True, ""


def get_adjustment_suggestions(
    db: Session, dimension: str, snapshot_date: date
) -> list[dict]:
    """Get prioritized adjustment suggestions for allocation deviations."""
    analysis = analyze_allocation(db, dimension, snapshot_date)
    
    suggestions = []
    
    def collect_suggestions(items: list[AllocationAnalysisItem]):
        for item in items:
            if item.target_percent and item.priority and item.priority > 0:
                suggestions.append({
                    "id": item.id,
                    "name": item.name,
                    "target_percent": item.target_percent,
                    "actual_percent": item.actual_percent,
                    "deviation": item.deviation,
                    "deviation_amount": item.deviation_amount,
                    "recommendation": item.recommendation,
                    "priority": item.priority,
                })
            if item.children:
                collect_suggestions(item.children)
    
    collect_suggestions(analysis.items)
    
    # Sort by priority (descending)
    suggestions.sort(key=lambda x: x["priority"], reverse=True)
    
    return suggestions
