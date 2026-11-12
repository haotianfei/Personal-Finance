"""资产配置（目标达成度）服务。

维度口径（ADR-0019 D11）：配置目标的维度已从记录级 EAV（`asset_attributes`）换成
**资产项级**分类列（`asset_items` 四列 ＋ `risk_level` ＋ `asset_item_tags`）。
`allocation_targets.dimension` 的合法取值与 `target_id` 语义：

| dimension     | target_id 语义                                                        |
|---------------|-----------------------------------------------------------------------|
| `asset_class` | `asset_classes.id`（树：命中的类别含**全部后代**，与旧 fund_type 同义） |
| `liquidity`   | 档位天数整数（0/1/7/30/90/180/365/9999）——档位是产品常量，不建 lookup 表 |
| `institution` | `institutions.id`                                                     |
| `channel`     | `channels.id`                                                         |
| `risk_level`  | `R1`..`R5`（`risk` 作为同义词继续接受；新写入统一落 `risk_level`）      |

旧值（`fund_type` / `liquidity_rating` / `account`）**一律 422 拒绝**并提示新名：
静默兼容会让老目标悄悄命中空集，而空集在页面上看起来「没有偏差」——比直接报错危险。

聚合口径：金额按 `Asset.asset_item_id` 关联到项，再按项级分类列分组（`GROUP BY`），
不再是「每节点一条 EXISTS 子查询」。分类挂在项上之后，一次 `GROUP BY` 就能拿到全部
叶子金额，树在 Python 侧自底向上汇总。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import (
    AllocationTarget, AllocationTargetRevision, Asset, AssetItem,
    AssetClass, Channel, Institution,
)
from schemas import (
    AllocationTargetCreate, AllocationTargetUpdate, AllocationTargetOut,
    AllocationAnalysis, AllocationAnalysisItem, AllocationBand
)
from services import analysis_service, audit_service, band_service, classification_service
from services.audit_service import make_change as _change
from services.meta_service import build_meta

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# ---------------------------------------------------------------- 维度值域
# 维度名是**内部口径**（存库、供前端 query），改动必须同步 routers/allocations.py
# 与前端常量表；旧值只用于组织 422 提示文案，绝不参与解析。
DIM_ASSET_CLASS = "asset_class"
DIM_LIQUIDITY = "liquidity"
DIM_INSTITUTION = "institution"
DIM_CHANNEL = "channel"
DIM_RISK_LEVEL = "risk_level"

SUPPORTED_DIMENSIONS: tuple[str, ...] = (
    DIM_ASSET_CLASS, DIM_LIQUIDITY, DIM_INSTITUTION, DIM_CHANNEL, DIM_RISK_LEVEL,
)

# 有父子层级的维度：目标可挂 parent_id，子项总和受父项目标约束。
TREE_DIMENSIONS = frozenset({DIM_ASSET_CLASS})

# 旧值 → 新值（**仅**用于 422 文案；不做任何兼容解析）
LEGACY_DIMENSION_HINTS: dict[str, str] = {
    "fund_type": DIM_ASSET_CLASS,
    "liquidity_rating": DIM_LIQUIDITY,
    "account": f"{DIM_INSTITUTION}（渠道请用 {DIM_CHANNEL}）",
    # 旧结构里 tag/owner 目标从未实现过分组，迁移后语义明确化，同样要求显式改用新口径
    "tag": "asset_item_tags（标签维度暂不提供目标分组）",
    "owner": "members.id（归属维度暂不提供目标分组）",
}

# 同义拼写（schema 层两条都接受，见 models.AllocationTarget 的 ck_targets_dimension）：
# **主名是 `risk_level`（ADR-0019 的词汇），`risk` 作为同义词继续接受**——存量
# `allocation_targets.dimension='risk'` 行照旧可读（`_stored_dimension_names()` 两种
# 拼写都查），但新写入一律落主名，避免同一个维度长期有两种落库拼写。
DIMENSION_ALIASES: dict[str, str] = {"risk": DIM_RISK_LEVEL}

_LIQUIDITY_DAYS = tuple(days for days, _label in classification_service.LIQUIDITY_LEVELS)
_RISK_LEVELS = classification_service.RISK_LEVELS


def require_dimension(dimension: str | None) -> str:
    """校验维度名：只接受新值；旧值/未知值一律 422 并给出替换名。"""
    name = (dimension or "").strip()
    name = DIMENSION_ALIASES.get(name, name)
    if name in SUPPORTED_DIMENSIONS:
        return name
    hint = LEGACY_DIMENSION_HINTS.get(name)
    if hint:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            f"维度 {name} 已废弃（ADR-0019：分类从记录级上移到资产项）：请改用 {hint}",
            {"dimension": name, "replacement": hint},
        )
    raise ApiError(
        ErrorCode.VALIDATION_ERROR, 422,
        f"不支持的维度 {name or '(空)'}；可用维度：{list(SUPPORTED_DIMENSIONS)}",
        {"dimension": name, "supported": list(SUPPORTED_DIMENSIONS)},
    )


def _stored_dimension_names(dimension: str) -> tuple[str, ...]:
    """该维度在库里可能出现的拼写（主名 ＋ 同义名）。"""
    return tuple(name for name, canonical in
                 {dimension: dimension, **DIMENSION_ALIASES}.items()
                 if canonical == dimension)


def _invalid_target(dimension: str, target_id: str, message: str) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_ERROR, 422, message,
        {"dimension": dimension, "target_id": str(target_id)},
    )


def _parse_int_target(target_id) -> int | None:
    try:
        return int(str(target_id).strip())
    except (TypeError, ValueError):
        return None


def asset_class_subtree_ids(db: Session, class_id: int) -> list[int]:
    """类别子树 id（含自身）。迭代 + seen 防脏数据成环。"""
    ids: list[int] = []
    frontier = [class_id]
    seen: set[int] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        ids.append(current)
        frontier.extend(db.execute(
            select(AssetClass.id).where(AssetClass.parent_id == current)
        ).scalars().all())
    return ids


def _require_target_ref(db: Session, dimension: str, target_id) -> None:
    """目标引用必须是该维度下的合法取值（防「配了但不命中任何项」的空集目标）。"""
    if dimension == DIM_LIQUIDITY:
        days = _parse_int_target(target_id)
        if days not in _LIQUIDITY_DAYS:
            raise _invalid_target(
                dimension, target_id,
                f"流动性目标须为档位天数 {list(_LIQUIDITY_DAYS)} 之一")
        return
    if dimension == DIM_RISK_LEVEL:
        if str(target_id) not in _RISK_LEVELS:
            raise _invalid_target(
                dimension, target_id, f"风险目标须为 {list(_RISK_LEVELS)} 之一")
        return

    model = {DIM_ASSET_CLASS: AssetClass,
             DIM_INSTITUTION: Institution,
             DIM_CHANNEL: Channel}.get(dimension)
    if model is None:                       # require_dimension 已挡；防御未覆盖分支
        raise _invalid_target(dimension, target_id, f"维度 {dimension} 不支持目标引用")
    obj_id = _parse_int_target(target_id)
    if obj_id is None or db.get(model, obj_id) is None:
        raise _invalid_target(dimension, target_id,
                              f"{dimension} 目标 {target_id} 不存在")


def get_allocation_targets(db: Session, dimension: Optional[str] = None) -> list[AllocationTarget]:
    """Get all allocation targets, optionally filtered by dimension.

    `dimension` 只接受新值：传旧值 422（不返回空列表——空列表会被前端当成「没配置」）。
    不带 dimension 时原样返回库内全部行（存量旧维度行不迁移、也不自动删除）。
    """
    query = select(AllocationTarget)
    if dimension:
        names = _stored_dimension_names(require_dimension(dimension))
        query = query.where(AllocationTarget.dimension.in_(names))
    return list(db.execute(query).scalars().all())


def get_allocation_target(db: Session, target_id: int) -> Optional[AllocationTarget]:
    """Get a single allocation target by ID."""
    return db.execute(
        select(AllocationTarget).where(AllocationTarget.id == target_id)
    ).scalar_one_or_none()


def _append_revision(db: Session, target: AllocationTarget, op: str,
                     reason: str | None = None,
                     effective_date: date | None = None,
                     origin: str = "manual") -> AllocationTargetRevision:
    """阈值史单点追加（与业务写入/审计同事务；append-only 由 0020 触发器兜底）。"""
    revision = AllocationTargetRevision(
        allocation_target_id=target.id,
        op=op,
        dimension=target.dimension,
        target_id=target.target_id,
        parent_id=target.parent_id,
        target_percent=target.target_percent,
        alert_threshold=target.alert_threshold,
        alert_threshold_red=target.alert_threshold_red,
        relative_threshold=target.relative_threshold,
        effective_date=effective_date or date.today(),
        reason=reason,
        origin=origin,
        created_at=datetime.now().isoformat(),
    )
    db.add(revision)
    return revision


def _threshold_payload(target: AllocationTarget) -> dict:
    """审计 before/after：比例＋四阈值字段（ADR-0012 §4.2，不变量 8）。"""
    def _str(value):
        if value is None:
            return None
        return str(Decimal(str(value)).quantize(Decimal("0.01")))

    return {
        "dimension": target.dimension,
        "target_id": target.target_id,
        "parent_id": target.parent_id,
        "target_percent": _str(target.target_percent),
        "alert_threshold": _str(target.alert_threshold),
        "alert_threshold_red": _str(target.alert_threshold_red),
        "relative_threshold": _str(target.relative_threshold),
    }


def _merged_thresholds(db_target: AllocationTarget, fields: dict) -> tuple:
    """Partial update 与库内现有值合并后再校验（跨字段黄<红不可绕过）。"""
    def pick(name):
        return fields[name] if name in fields else getattr(db_target, name)

    return (pick("alert_threshold"), pick("alert_threshold_red"),
            pick("relative_threshold"))


def create_allocation_target(db: Session, target: AllocationTargetCreate) -> AllocationTarget:
    """Create a new allocation target（维度/引用校验＋阈值校验＋同事务审计）。"""
    dimension = require_dimension(target.dimension)
    _require_target_ref(db, dimension, target.target_id)
    band_service.validate_thresholds(
        target.alert_threshold, target.alert_threshold_red,
        target.relative_threshold)
    db_target = AllocationTarget(**{**target.model_dump(), "dimension": dimension})
    db.add(db_target)
    db.flush()
    audit_service.record(db, _change(
        "allocation_target", db_target.id, "create", None,
        _threshold_payload(db_target)))
    _append_revision(db, db_target, "create", "create")
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
            _append_revision(db, child, "update", "auto_adjust:parent")
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

    update_data = target.model_dump(exclude_unset=True)
    band_service.validate_thresholds(*_merged_thresholds(db_target, update_data))

    before = _threshold_payload(db_target)
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
    for key, value in update_data.items():
        setattr(db_target, key, value)
    
    audit_service.record(db, _change(
        "allocation_target", db_target.id, "update", before,
        _threshold_payload(db_target)))
    _append_revision(db, db_target, "update", "update")
    db.commit()
    db.refresh(db_target)
    return db_target, auto_adjusted_children


def delete_allocation_target(db: Session, target_id: int, *,
                             audit_reason: str | None = None,
                             audit_reason_note: str | None = None) -> bool:
    """Delete an allocation target（删除留审计行）。

    `action='delete'` 属纠正类：缺 reason 由 `audit_service.record()` 422 拦下
    （`AUDIT_REASON_REQUIRED`）。**无 UI 入口，供维护脚本**（spec E4 孤儿端点）。
    """
    db_target = get_allocation_target(db, target_id)
    if not db_target:
        return False

    before = _threshold_payload(db_target)
    row_id = db_target.id
    _append_revision(db, db_target, "delete", "delete")
    db.delete(db_target)
    db.flush()
    audit_service.record(db, _change(
        "allocation_target", row_id, "delete", before, None,
        reason=audit_reason, reason_note=audit_reason_note))
    db.commit()
    return True


def _as_of_records(db: Session, snapshot_date: date):
    """该日的 as-of 记录集——**一次分析只取一次**，合计与分列金额复用。

    取数函数是 `analysis_service._get_latest_records_as_of`（`_sum_as_of` 的唯一
    数据源）。此前 `get_total_assets` 与 `_amounts_by_item_column` 各自调用一次，
    同一份记录被取两遍；合并不改变口径（合计仍由 `_aggregate_records` 产出），
    只是少一次全表 as-of 扫描，并消除两次取数之间可能读到不同快照的缝隙。
    """
    return analysis_service._get_latest_records_as_of(db, snapshot_date)


def get_total_assets(db: Session, snapshot_date: date, *,
                     records: Optional[list] = None) -> Decimal:
    """该日总资产（仅计资产，负债不计入）——**直接取权威实现，不再自行求和**。

    口径（ADR-0020 D4 同源不变量）：`analysis_service._sum_as_of` 是唯一权威总额
    实现，本函数的资产侧合计＝它的返回值第 2 项，故与 `get_summary.total_assets`
    **精确相等**（Decimal 相等）。该口径包含：

    - superseded 行恒不入（同日被更正的旧行不与现行行重复计）；
    - 归档项自 `archived_effective_from` 起退出（ADR-0017 D2），生效日前照常计入；
    - `draft` 项照常计入（ADR-0020 D1：分类未补齐≠钱不存在）；
    - **as-of 沿用（carry-forward）**：某日某项没有行＝值没变（ADR-0019 D12），
      取该项 ≤ 该日的最新 active 行；按 `asset_date == 该日` 求和会静默少算
      「当天没更新的持仓」（ADR-0020 D4 对账用例锁定）。

    历史实现是本文件里第二套求和（只有 `asset_date == d` + `side='asset'`），
    与 summary 不同源；这也是本函数改为委托的**唯一**原因。

    `records` 供同一请求内的其他聚合复用（见 `_as_of_records`）；不传则自行取数。
    """
    recs = _as_of_records(db, snapshot_date) if records is None else records
    return analysis_service._aggregate_records(recs)[1]


# ---------------------------------------------------------------- 项级金额聚合


def _amounts_by_item_column(db: Session, snapshot_date: date, column, *,
                            records: Optional[list] = None) -> dict:
    """`column → 金额`（as-of 该日、资产侧、项级分类列分组）。

    行选择与金额**全部取自** `analysis_service._get_latest_records_as_of`
    （`_sum_as_of` 的取数函数）——superseded 恒不入、归档项自生效日起退出、每项取
    ≤该日最新行、draft 照常计入。这样分子与 `get_total_assets` 的分母同源，不会
    出现「分母含沿用项、分子只有当日行」的结构性缺口（ADR-0020 D4）。

    `asset_item_id` 为 NULL 的存量行没有项，无法归属任何分类取值 → 不计入分子
    （与旧的「记录上没有该属性就不命中」同义；分母仍含它们，与 `_sum_as_of` 的
    按名回退口径一致——该差异是「未归属分类」的既有事实，不是本次口径）。
    """
    amount_by_asset = {
        asset_id: Decimal(str(amount))
        for _name, amount, asset_id, _side, _row_date
        in (_as_of_records(db, snapshot_date) if records is None else records)
    }
    if not amount_by_asset:
        return {}
    rows = db.execute(
        select(Asset.id, column)
        .select_from(Asset)
        .join(AssetItem, AssetItem.id == Asset.asset_item_id)
        .where(Asset.id.in_(list(amount_by_asset)),
               Asset.side == "asset",
               column.is_not(None))
    ).all()
    out: dict = {}
    for asset_id, key in rows:
        out[key] = out.get(key, ZERO) + amount_by_asset[asset_id]
    return out


def _priority(state: str, abs_deviation: Decimal) -> int:
    """严重度优先：red=200+/yellow=100+/green=0；同档按 |dev| 降序（封顶 99 保档序）。"""
    base = {"red": 200, "yellow": 100}.get(state, 0)
    return base + min(int(abs_deviation * 10), 99)


def _recommendation(state: str, deviation: Decimal,
                    deviation_amount: Optional[Decimal]) -> str:
    """按带型生成方向性引导：只给方向与金额，不给执行动作（F2）。"""
    if state == "green":
        return "在带内"
    if state == "yellow":
        return "近阈值，下次调仓时校正"
    action = "超配减持" if deviation > 0 else "低配增持"
    amount = ("" if deviation_amount is None
              else f"约{deviation_amount:,.0f}元")
    return f"超阈，建议回调至目标（{action}{amount}）"


def _calculate_deviation_metrics(
    actual_percent: Decimal,
    target: Optional[AllocationTarget],
    total_amount: Decimal,
) -> dict:
    """逐目标行偏离指标（带型单点：`band_service`，禁止就地比较）。"""
    if target is None or target.target_percent is None:
        return {
            "deviation": actual_percent,
            "deviation_percent": None,
            "deviation_amount": None,
            "recommendation": "未设目标",
            "priority": 0,
            "state": None,
            "band": None,
            "abs_yellow": Decimal("5.00"),
        }

    target_percent = Decimal(str(target.target_percent))
    deviation = actual_percent - target_percent
    deviation_percent = (deviation / target_percent * 100) if target_percent > 0 else None
    deviation_amount = (total_amount * abs(deviation) / 100) if total_amount > 0 else None

    band = band_service.resolve_band(target)
    state = band_service.evaluate(deviation, band)
    abs_deviation = abs(deviation)

    return {
        "deviation": deviation,
        "deviation_percent": deviation_percent,
        "deviation_amount": deviation_amount,
        "recommendation": _recommendation(state, deviation, deviation_amount),
        "priority": _priority(state, abs_deviation),
        "state": state,
        "band": band,
        "abs_yellow": Decimal(band["components"]["abs_yellow"]),
    }


def _band_out(metrics: dict) -> Optional[AllocationBand]:
    band = metrics.get("band")
    if band is None:
        return None
    return AllocationBand(
        state=metrics["state"], yellow=band["threshold"], red=band["red"],
        source=band["source"], components=band["components"])


def _make_item(item_id: str, name: str, level: int,
               target: Optional[AllocationTarget], actual_amount: Decimal,
               total_amount: Decimal, *,
               parent_id: Optional[str] = None,
               children: Optional[list[AllocationAnalysisItem]] = None,
               ) -> AllocationAnalysisItem:
    """一行分析项（五个维度共用，避免每维度抄一遍偏离计算）。"""
    actual_percent = ((actual_amount / total_amount * 100)
                      if total_amount > 0 else ZERO)
    target_percent = (Decimal(str(target.target_percent))
                      if target is not None else None)
    metrics = _calculate_deviation_metrics(actual_percent, target, total_amount)
    return AllocationAnalysisItem(
        id=item_id,
        name=name,
        parent_id=parent_id,
        level=level,
        target_percent=target_percent,
        actual_percent=actual_percent,
        actual_amount=actual_amount,
        deviation=metrics["deviation"],
        deviation_percent=metrics["deviation_percent"],
        deviation_amount=metrics["deviation_amount"],
        recommendation=metrics["recommendation"],
        priority=metrics["priority"],
        alert_threshold=metrics["abs_yellow"],
        band=_band_out(metrics),
        children=children or [],
    )


def _unallocated_percent(all_targets: list[AllocationTarget]) -> Decimal:
    """未配置比例＝100 − 根目标之和（分层目标只数根，否则重复计）。"""
    root_targets = [t for t in all_targets if t.parent_id is None]
    allocated = sum((t.target_percent for t in root_targets), ZERO)
    return max(HUNDRED - allocated, ZERO)


def _targets_by_target_id(all_targets: list[AllocationTarget]) -> dict[str, AllocationTarget]:
    """目标按 `target_id` 建索引（`target_id` 在库里是字符串，比较时统一 str）。"""
    return {str(t.target_id): t for t in all_targets}


# ---- asset_class（树）


def analyze_asset_class_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """按资产类别树分组：节点金额＝自身＋全部后代类别（项级聚合，一次 GROUP BY）。"""
    records = _as_of_records(db, snapshot_date)
    total_amount = get_total_assets(db, snapshot_date, records=records)
    all_targets = get_allocation_targets(db, DIM_ASSET_CLASS)
    targets = _targets_by_target_id(all_targets)

    nodes = db.execute(
        select(AssetClass).order_by(AssetClass.level, AssetClass.code)
    ).scalars().all()
    children_of: dict[Optional[int], list[AssetClass]] = {}
    for node in nodes:
        children_of.setdefault(node.parent_id, []).append(node)

    direct_amounts = _amounts_by_item_column(
        db, snapshot_date, AssetItem.asset_class_id, records=records)
    subtree_cache: dict[int, Decimal] = {}

    def subtree_amount(node: AssetClass) -> Decimal:
        cached = subtree_cache.get(node.id)
        if cached is not None:
            return cached
        total = direct_amounts.get(node.id, ZERO)
        for child in children_of.get(node.id, []):
            total += subtree_amount(child)
        subtree_cache[node.id] = total
        return total

    def build(node: AssetClass, parent_id: Optional[str]) -> AllocationAnalysisItem:
        children = [build(child, str(node.id))
                    for child in children_of.get(node.id, [])]
        return _make_item(
            str(node.id), node.name, node.level,
            targets.get(str(node.id)), subtree_amount(node), total_amount,
            parent_id=parent_id, children=children)

    items = [build(root, None) for root in children_of.get(None, [])]
    items.sort(key=lambda x: x.priority or 0, reverse=True)

    return AllocationAnalysis(
        dimension=DIM_ASSET_CLASS,
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=_unallocated_percent(all_targets),
    )


# ---- liquidity（days 档位）


def analyze_liquidity_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """按流动性档位分组：id/target_id 一律是档位**天数**（整数），label 是展示名。"""
    records = _as_of_records(db, snapshot_date)
    total_amount = get_total_assets(db, snapshot_date, records=records)
    all_targets = get_allocation_targets(db, DIM_LIQUIDITY)
    targets = _targets_by_target_id(all_targets)

    amounts = _amounts_by_item_column(db, snapshot_date, AssetItem.liquidity_days,
                                      records=records)
    items = [
        _make_item(str(days), label, 0, targets.get(str(days)),
                   amounts.get(days, ZERO), total_amount)
        for days, label in classification_service.LIQUIDITY_LEVELS
    ]
    items.sort(key=lambda x: x.priority or 0, reverse=True)

    return AllocationAnalysis(
        dimension=DIM_LIQUIDITY,
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=_unallocated_percent(all_targets),
    )


# ---- institution / channel（平表字典）


def _analyze_dictionary_dimension(
    db: Session, snapshot_date: date, dimension: str, model, column,
) -> AllocationAnalysis:
    """机构/渠道共用：字典行全枚举（含 0 金额），项级列分组取金额。"""
    records = _as_of_records(db, snapshot_date)
    total_amount = get_total_assets(db, snapshot_date, records=records)
    all_targets = get_allocation_targets(db, dimension)
    targets = _targets_by_target_id(all_targets)

    rows = db.execute(select(model).order_by(model.name)).scalars().all()
    amounts = _amounts_by_item_column(db, snapshot_date, column, records=records)
    items = [
        _make_item(str(row.id), row.name, 0, targets.get(str(row.id)),
                   amounts.get(row.id, ZERO), total_amount)
        for row in rows
    ]
    items.sort(key=lambda x: x.priority or 0, reverse=True)

    return AllocationAnalysis(
        dimension=dimension,
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=_unallocated_percent(all_targets),
    )


def analyze_institution_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    return _analyze_dictionary_dimension(
        db, snapshot_date, DIM_INSTITUTION, Institution, AssetItem.institution_id)


def analyze_channel_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    return _analyze_dictionary_dimension(
        db, snapshot_date, DIM_CHANNEL, Channel, AssetItem.channel_id)


# ---- risk（R1..R5 有序枚举）


def analyze_risk_allocation(
    db: Session, snapshot_date: date
) -> AllocationAnalysis:
    """维度=风险等级（`R1`–`R5`，未评级项不进任何档）。"""
    records = _as_of_records(db, snapshot_date)
    total_amount = get_total_assets(db, snapshot_date, records=records)
    all_targets = get_allocation_targets(db, DIM_RISK_LEVEL)
    targets = _targets_by_target_id(all_targets)

    amounts = _amounts_by_item_column(db, snapshot_date, AssetItem.risk_level,
                                      records=records)
    items = [
        _make_item(level, level, 0, targets.get(level),
                   amounts.get(level, ZERO), total_amount)
        for level in _RISK_LEVELS
    ]
    items.sort(key=lambda x: x.priority or 0, reverse=True)

    return AllocationAnalysis(
        dimension=DIM_RISK_LEVEL,
        snapshot_date=snapshot_date,
        total_amount=total_amount,
        items=items,
        unallocated_percent=_unallocated_percent(all_targets),
    )


_ANALYZERS = {
    DIM_ASSET_CLASS: analyze_asset_class_allocation,
    DIM_LIQUIDITY: analyze_liquidity_allocation,
    DIM_INSTITUTION: analyze_institution_allocation,
    DIM_CHANNEL: analyze_channel_allocation,
    DIM_RISK_LEVEL: analyze_risk_allocation,
}


def analyze_allocation(
    db: Session, dimension: str, snapshot_date: date
) -> AllocationAnalysis:
    """Analyze allocation for a specific dimension.

    旧维度名（`fund_type` / `liquidity_rating` / `account`）→ 422 并提示新名，
    不返回空分析（空分析等于「所有目标达成」，会把老配置伪装成没问题）。
    """
    name = require_dimension(dimension)
    result = _ANALYZERS[name](db, snapshot_date)
    result.meta = build_meta(db, snapshot_date)
    return result


def validate_allocation_target(
    db: Session, dimension: str, target_id: str, target_percent: Decimal, parent_id: Optional[int] = None
) -> tuple[bool, str]:
    """Validate allocation target constraints with enhanced rules.
    
    Rules:
    1. Root level: 0% < value ≤ 100%
    2. Child level: 0% < value ≤ parent target percent
    3. Root level sum ≤ 100%
    4. Child level sum within same parent ≤ parent target

    维度名与目标引用的问题直接 raise 422（`VALIDATION_ERROR`）：它们是「配置口径错了」，
    不是「比例不合规」——后者沿用 (False, 文案) 让路由转 400，与旧契约一致。
    """
    name = require_dimension(dimension)
    _require_target_ref(db, name, target_id)

    # Rule 1 & 2: Value range validation
    if target_percent <= 0:
        return False, "比例必须大于0%"
    
    if target_percent > 100:
        return False, "比例不能超过100%"
    
    # Get existing targets for this dimension
    existing = get_allocation_targets(db, name)
    target_key = str(target_id)
    
    # Rule 2: Child level constraint (value ≤ parent target)
    if name in TREE_DIMENSIONS and parent_id:
        parent_target = get_allocation_target(db, parent_id)
        if parent_target and target_percent > parent_target.target_percent:
            return False, f"不能超过父级目标比例 ({parent_target.target_percent:.2f}%)"
    
    # Rule 3: Root level sum ≤ 100%
    # Calculate current total excluding the target being updated
    current_total = sum(
        t.target_percent for t in existing
        if str(t.target_id) != target_key and t.parent_id is None
    )
    
    # If this is a root level target (no parent), check total constraint
    if parent_id is None:
        new_total = current_total + target_percent
        if new_total > 100:
            return False, f"该层级总配置比例不能超过100%，当前已配置{current_total:.2f}%"
    
    # Rule 4: Child level sum within same parent ≤ parent target
    if name in TREE_DIMENSIONS and parent_id:
        siblings_sum = sum(
            t.target_percent for t in existing
            if t.parent_id == parent_id and str(t.target_id) != target_key
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