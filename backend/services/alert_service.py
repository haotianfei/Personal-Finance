"""预警（异动）服务。

维度口径（ADR-0019 D11）：规则的「维度」已从记录级 EAV（`attributes(type)` ＋
`asset_attributes`）换成**资产项级**分类列。`alert_rules.dimension` 的合法取值与
`target_id` 语义：

| dimension     | target_id 语义                                                    |
|---------------|-------------------------------------------------------------------|
| `asset_name`  | 记录上的名称快照（历史行带当时的名字，故仍按 `assets.asset_name`） |
| `asset_class` | `asset_classes.id`（树：命中类别含**全部后代**，同旧 fund_type）   |
| `liquidity`   | 档位天数整数（0/1/7/30/90/180/365/9999）                           |
| `institution` | `institutions.id`                                                 |
| `channel`     | `channels.id`                                                     |
| `owner`       | `members.id`                                                      |
| `tag`         | `tags.id`（项级多值：命中任一即计入）                              |
| `risk_level`  | `R1`..`R5`（`risk` 作为同义词继续接受；新写入统一落 `risk_level`）  |

旧值（`fund_type` / `liquidity_rating` / `account`）的**两侧处理不同**：

- 写路径（`create_alert_rule` / `update_alert_rule`）→ 422，提示新名，要求用户改；
- 读路径（`check_alert` / `get_alert_results`）→ 该规则标 `invalid`：返回
  `triggered=False` ＋ `message` 说明替换维度，**不自动删除规则**（删掉等于替用户
  决定放弃一条他配过的告警）。

为什么不"静默兼容"旧值：旧维度在新结构下命中空集，而空集在预警里表现成
「变化 0，一切正常」——比直接报错更危险。
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, distinct, func, and_, or_

from errors import ApiError, ErrorCode
from models import (
    AlertRule, Asset, AssetClass, AssetItem, AssetItemTag, Channel,
    Institution, Member, Tag,
)
from schemas import AlertRuleCreate, AlertRuleUpdate, AlertRuleOut, AlertResult, AlertDetailItem
from services import audit_service, classification_service
from services.period_service import (
    get_period_label, get_period_start_end, get_previous_period_end,
    group_dates_by_period,
)
from services.snapshot_identity import active_item_predicate

ZERO = Decimal("0")

# ---------------------------------------------------------------- 维度值域
DIM_ASSET_NAME = "asset_name"
DIM_ASSET_CLASS = "asset_class"
DIM_LIQUIDITY = "liquidity"
DIM_INSTITUTION = "institution"
DIM_CHANNEL = "channel"
DIM_OWNER = "owner"
DIM_TAG = "tag"
DIM_RISK_LEVEL = "risk_level"

SUPPORTED_DIMENSIONS: tuple[str, ...] = (
    DIM_ASSET_NAME, DIM_ASSET_CLASS, DIM_LIQUIDITY, DIM_INSTITUTION,
    DIM_CHANNEL, DIM_OWNER, DIM_TAG, DIM_RISK_LEVEL,
)

# 项级维度：条件落在 `asset_items` 的列 / `asset_item_tags` 上
ITEM_DIMENSIONS = frozenset(SUPPORTED_DIMENSIONS) - {DIM_ASSET_NAME}

# 旧值 → 新值（只用于文案，不做兼容解析）
LEGACY_DIMENSION_HINTS: dict[str, str] = {
    "fund_type": DIM_ASSET_CLASS,
    "liquidity_rating": DIM_LIQUIDITY,
    "account": f"{DIM_INSTITUTION}（渠道请用 {DIM_CHANNEL}）",
}

# 同义拼写（schema 层两条都接受）：**主名是 `risk_level`**，`risk` 作为同义词继续
# 接受（存量规则行落的是 `risk`）——读侧两种都算同一维度，写侧统一落主名。
DIMENSION_ALIASES: dict[str, str] = {"risk": DIM_RISK_LEVEL}

_LIQUIDITY_DAYS = tuple(days for days, _label in classification_service.LIQUIDITY_LEVELS)
_RISK_LEVELS = classification_service.RISK_LEVELS

# 条件解析哨兵：维度/取值已失效 → 该规则不得按「全部」求和（那会伪造异动）
_INVALID = object()

_DICT_MODELS = {
    DIM_INSTITUTION: Institution,
    DIM_CHANNEL: Channel,
    DIM_OWNER: Member,
    DIM_TAG: Tag,
}


def normalize_dimension(dimension: str | None) -> str:
    """同义拼写归一到主名（`risk` → `risk_level`）。"""
    name = (dimension or "").strip()
    return DIMENSION_ALIASES.get(name, name)


def require_dimension(dimension: str | None) -> str:
    """写路径校验：只接受新值；旧值/未知值 422 并给出替换名。"""
    name = normalize_dimension(dimension)
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


def rule_invalidation(rule: AlertRule, db: Session) -> Optional[str]:
    """该规则的口径是否已失效（旧维度/未知维度/非法取值）→ 人读原因，否则 None。

    读路径用它把规则标成 invalid；设置页可直接展示这些原因，提示用户改成新维度。
    """
    raw = (rule.dimension or "").strip()
    hint = LEGACY_DIMENSION_HINTS.get(raw)
    if hint:
        return (f"规则维度 {raw} 已废弃（ADR-0019）：请改用 {hint}；"
                f"本规则暂不参与预警计算")
    dimension = normalize_dimension(raw)
    if dimension not in SUPPORTED_DIMENSIONS:
        return (f"规则维度 {dimension or '(空)'} 不受支持：可用维度 "
                f"{list(SUPPORTED_DIMENSIONS)}；本规则暂不参与预警计算")
    if rule.target_id is None:
        return None
    problem = _target_ref_problem(db, dimension, rule.target_id)
    if problem:
        return f"{problem}；本规则暂不参与预警计算"
    return None


def invalid_rules(db: Session) -> list[dict]:
    """读时投影：口径失效的规则清单（不改库、不删规则）。"""
    out = []
    for rule in get_alert_rules(db):
        reason = rule_invalidation(rule, db)
        if reason:
            out.append({"rule_id": rule.id, "rule_name": rule.name,
                        "dimension": rule.dimension,
                        "target_id": rule.target_id, "message": reason})
    return out


def _target_ref_problem(db: Optional[Session], dimension: str,
                        target_id) -> Optional[str]:
    """目标引用不合法的原因（None＝合法）。`db=None` 时只校验枚举，不查表。"""
    if dimension == DIM_ASSET_NAME or target_id is None:
        return None
    condition = _condition_for_target(db, dimension, target_id)
    if condition is _INVALID:
        return f"目标 {target_id} 不是维度 {dimension} 的合法取值"
    model = _DICT_MODELS.get(dimension)
    if model is not None and db is not None:
        if db.get(model, _parse_int(target_id)) is None:
            return f"{dimension} 目标 {target_id} 不存在"
    return None


def _parse_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _condition_for_target(db: Optional[Session], dimension: str, target_id):
    """`target_id` → SQL 条件 / `_INVALID`（取值非法）。target_id=None 由调用方处理。

    `db=None` 时不做类别树展开（只判取值形态），供写入前的枚举校验复用。
    """
    if dimension == DIM_ASSET_NAME:
        return Asset.asset_name == target_id
    if dimension == DIM_ASSET_CLASS:
        class_id = _parse_int(target_id)
        if class_id is None:
            return _INVALID
        return AssetItem.asset_class_id.in_(asset_class_subtree_ids(db, class_id))
    if dimension == DIM_LIQUIDITY:
        days = _parse_int(target_id)
        if days not in _LIQUIDITY_DAYS:
            return _INVALID
        return AssetItem.liquidity_days == days
    if dimension == DIM_INSTITUTION:
        obj_id = _parse_int(target_id)
        return _INVALID if obj_id is None else AssetItem.institution_id == obj_id
    if dimension == DIM_CHANNEL:
        obj_id = _parse_int(target_id)
        return _INVALID if obj_id is None else AssetItem.channel_id == obj_id
    if dimension == DIM_OWNER:
        obj_id = _parse_int(target_id)
        return _INVALID if obj_id is None else AssetItem.owner_id == obj_id
    if dimension == DIM_TAG:
        tag_id = _parse_int(target_id)
        if tag_id is None:
            return _INVALID
        return AssetItem.id.in_(
            select(AssetItemTag.item_id).where(AssetItemTag.tag_id == tag_id))
    if dimension == DIM_RISK_LEVEL:
        if str(target_id) not in _RISK_LEVELS:
            return _INVALID
        return AssetItem.risk_level == str(target_id)
    return _INVALID          # 旧维度 / 未知维度


def _condition_for(db: Session, dimension: str, target_id):
    """同上，但旧维度一律 `_INVALID`；target_id=None（「全部」）→ None。"""
    dimension = normalize_dimension(dimension)
    if dimension not in SUPPORTED_DIMENSIONS:
        return _INVALID
    if target_id is None:
        return None
    return _condition_for_target(db, dimension, target_id)


def asset_class_subtree_ids(db: Optional[Session], class_id: int) -> list[int]:
    """类别子树 id（含自身）。无 session 时（如写入前的纯校验）退回单节点。"""
    if db is None:
        return [class_id]
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
    """写路径：目标引用必须是该维度下的合法取值（None＝整维度，合法）。"""
    problem = _target_ref_problem(db, dimension, target_id)
    if problem:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422, problem,
            {"dimension": dimension, "target_id": str(target_id)})


# ---------------------------------------------------------------- 规则 CRUD


def get_alert_rules(db: Session, active_only: bool = False) -> list[AlertRule]:
    query = select(AlertRule).order_by(AlertRule.created_at.desc())
    if active_only:
        query = query.where(AlertRule.is_active == True)
    return list(db.execute(query).scalars().all())


def get_alert_rule(db: Session, rule_id: int) -> Optional[AlertRule]:
    return db.execute(select(AlertRule).where(AlertRule.id == rule_id)).scalar_one_or_none()


# ---- 留痕（ADR-0027 ②口径桶） ----
#
# 阈值规则改的是「读数怎么算」：同一批账本行，维度/阈值/方向一变，预警结论就变，
# 所以它是口径而不是缓存。快照分型（ADR-0027 D5）：
#   create → 关键字段全集落 after；update → **仅变更字段**；delete → 关键字段全集落 before。
# 关键字段名与 API 契约同名（便于审计读端与前端表单对齐）；Decimal 原样给
# `audit_service`，由 `_dump(default=str)` 转文本，不经 float 丢精度。

# 关键字段（不含 id：id 只在 create/delete 的快照里显式带出）
_ALERT_RULE_FIELDS = (
    "name", "dimension", "target_id", "period_type", "compare_type",
    "compare_period", "amount_threshold", "percent_threshold",
    "direction", "is_active",
)


def _rule_fields(rule: AlertRule) -> dict:
    """关键字段快照。"""
    return {field: getattr(rule, field) for field in _ALERT_RULE_FIELDS}


def _rule_snapshot(rule: AlertRule) -> dict:
    """create/delete 用的全集快照（带 id，取证时能直指被删的那一行）。"""
    return {"id": rule.id, **_rule_fields(rule)}


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """只留值真的变了的键（update 分型：before/after 两侧保持同一组键）。"""
    changed = [key for key in after if before.get(key) != after.get(key)]
    return ({key: before.get(key) for key in changed},
            {key: after[key] for key in changed})


def create_alert_rule(db: Session, rule: AlertRuleCreate) -> AlertRule:
    dimension = require_dimension(rule.dimension)
    _require_target_ref(db, dimension, rule.target_id)
    db_rule = AlertRule(**{**rule.model_dump(), "dimension": dimension})
    db.add(db_rule)
    db.flush()                       # 拿 id 后再记审计行（同事务；回滚不留痕）
    audit_service.record(db, audit_service.make_change(
        "alert", db_rule.id, "create", None, _rule_snapshot(db_rule)))
    db.commit()
    db.refresh(db_rule)
    return db_rule


def update_alert_rule(db: Session, rule_id: int, rule: AlertRuleUpdate) -> Optional[AlertRule]:
    db_rule = get_alert_rule(db, rule_id)
    if not db_rule:
        return None
    update_data = rule.model_dump(exclude_unset=True)
    # dimension 显式传 null ＝「不改」（导入路径会整包带上 None），不能让 NOT NULL 崩
    if "dimension" in update_data and update_data["dimension"] is None:
        update_data.pop("dimension")
    if "dimension" in update_data:
        update_data["dimension"] = require_dimension(update_data["dimension"])
        _require_target_ref(db, update_data["dimension"],
                            update_data.get("target_id", db_rule.target_id))
    elif "target_id" in update_data:
        _require_target_ref(db, db_rule.dimension, update_data["target_id"])
    before = _rule_fields(db_rule)
    for key, value in update_data.items():
        setattr(db_rule, key, value)
    before_changed, after_changed = _changed_fields(before, _rule_fields(db_rule))
    if after_changed:                # 空变更＝没有写入事实，不产审计行
        audit_service.record(db, audit_service.make_change(
            "alert", db_rule.id, "update", before_changed, after_changed))
    db.commit()
    db.refresh(db_rule)
    return db_rule


def delete_alert_rule(db: Session, rule_id: int, *,
                      audit_reason: str | None = None,
                      audit_reason_note: str | None = None) -> bool:
    """删除告警规则（`action='delete'` 属纠正类：缺 reason 由 `record()` 422 拦下）。"""
    db_rule = get_alert_rule(db, rule_id)
    if not db_rule:
        return False
    before = _rule_snapshot(db_rule)
    db.delete(db_rule)
    db.flush()
    audit_service.record(db, audit_service.make_change(
        "alert", rule_id, "delete", before, None,
        reason=audit_reason, reason_note=audit_reason_note))
    db.commit()
    return True


# ---------------------------------------------------------------- 取数


def _get_all_snapshot_dates(db: Session) -> list[date]:
    # 与 analysis_service._get_all_snapshot_dates（该口径的定义处）保持同一过滤：
    # 只有 superseded（被更正）行的日期不撑出账期（spec §4 P0-1）。改口径必须
    # 同时改那边，否则预警与净值序列会分叉（issue 06）。
    result = db.execute(
        select(distinct(Asset.asset_date))
        .where(Asset.superseded_by.is_(None))
        .order_by(Asset.asset_date)
    ).scalars().all()
    return list(result)


def _sum_at(db: Session, dimension: str, target_id: Optional[str],
            at_date: date) -> Decimal:
    """该日、该维度取值的资产合计（as-of 行口径；`target_id=None` ＝整维度）。

    维度/取值失效 → 0（**不是**「全部求和」）：旧实现里 owner/tag 这类未实现分支
    会悄悄退化成总和，等于给用户一条会误报的规则。

    **维度必须先归一化**（`risk` → `risk_level`）：下面的 join 判断用的是归一化后的
    名字，而 `_condition_for` 内部也会归一化——两者不一致时条件落在 `asset_items`
    上、join 却没加，`assets, asset_items` 就成了交叉积，一条「R1 合计」会放大成
    总资产（issue 01）。
    """
    dimension = normalize_dimension(dimension)
    condition = _condition_for(db, dimension, target_id)
    if condition is _INVALID:
        return ZERO

    query = select(func.coalesce(func.sum(Asset.amount), 0)).select_from(Asset)
    query = query.where(Asset.asset_date == at_date)
    query = query.where(Asset.superseded_by.is_(None))
    # 归档项自生效日起不入 as-of（ADR-0017 D2 修订）：谓词单点定义在 snapshot_identity
    query = query.where(active_item_predicate(at_date))
    query = query.where(Asset.side == "asset")
    if condition is not None:
        if dimension in ITEM_DIMENSIONS:
            query = query.join(AssetItem, AssetItem.id == Asset.asset_item_id)
        query = query.where(condition)

    result = db.execute(query).scalar()
    return Decimal(str(result)) if result else ZERO


def _get_amount_for_period(
    db: Session, 
    period: str, 
    dimension: str, 
    target_id: Optional[str],
    dates: Optional[list[date]] = None,
    groups: Optional[dict] = None,
    amount_cache: Optional[dict] = None,
) -> Decimal:
    cache_key = (period, dimension, target_id)
    if amount_cache is not None and cache_key in amount_cache:
        return amount_cache[cache_key]
    value = _compute_amount_for_period(db, period, dimension, target_id,
                                       dates, groups)
    if amount_cache is not None:
        amount_cache[cache_key] = value
    return value


def _compute_amount_for_period(
    db: Session, 
    period: str, 
    dimension: str, 
    target_id: Optional[str],
    dates: Optional[list[date]] = None,
    groups: Optional[dict] = None,
) -> Decimal:
    if dates is None:
        dates = _get_all_snapshot_dates(db)
    
    # Determine period type from period string format
    if groups is None:
        if period_type := _detect_period_type(period):
            groups = group_dates_by_period(dates, period_type)
        else:
            return ZERO
    
    if period not in groups:
        return ZERO
    
    period_dates = groups[period]
    if not period_dates:
        return ZERO
    
    max_date = max(period_dates)
    return _sum_at(db, dimension, target_id, max_date)


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
    dates: Optional[list[date]] = None,
    groups: Optional[dict] = None,
) -> Optional[str]:
    """Find the most recent period with data before current_period.
    
    Args:
        db: Database session
        current_period: Current period label (e.g., "2024-03", "2024-Q1", "2024", "2024-03-15")
        period_type: Period type ("day", "month", "quarter", "year")
    
    Returns:
        The most recent period label with data, or None if no previous data exists
    """
    if dates is None:
        dates = _get_all_snapshot_dates(db)
    if not dates:
        return None
    
    if groups is None:
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


# ---------------------------------------------------------------- 展示名


def _get_target_name(db: Session, dimension: str, target_id: Optional[str]) -> str:
    dimension = normalize_dimension(dimension)
    if not target_id:
        return "全部"

    if dimension == DIM_ASSET_NAME:
        return target_id
    if dimension == DIM_LIQUIDITY:
        label = classification_service.liquidity_label(_parse_int(target_id))
        return label or str(target_id)
    if dimension == DIM_ASSET_CLASS:
        node = db.get(AssetClass, _parse_int(target_id) or 0)
        return node.name if node else str(target_id)
    if dimension == DIM_RISK_LEVEL:
        return str(target_id)
    model = _DICT_MODELS.get(dimension)
    if model is not None:
        obj = db.get(model, _parse_int(target_id) or 0)
        if obj is not None:
            return obj.name
        return str(target_id)
    # 旧维度：带上失效提示，避免设置页把「fund_type: 3」当成有效规则展示
    hint = LEGACY_DIMENSION_HINTS.get(dimension)
    if hint:
        return f"{target_id}（维度 {dimension} 已失效，请改用 {hint}）"
    return str(target_id)


# ---------------------------------------------------------------- 明细


def _detail_values(db: Session, dimension: str,
                   current_max_date: date, compare_max_date: date) -> list[tuple[str, str]]:
    """明细行候选：(target_id, 名称)。按维度从对应字典/枚举枚举，不发明新取值。"""
    dimension = normalize_dimension(dimension)
    if dimension == DIM_ASSET_NAME:
        # 候选名列表（派生读）：同样叠加归档谓词，否则已归档身份会以 0/0 的
        # 幽灵明细行出现在预警结果里（金额侧已由下方聚合排除）。两个账期各自
        # 按「该账期 max_date 当日是否仍在计量」判定（ADR-0017 D2 修订）。
        names = db.execute(
            select(distinct(Asset.asset_name))
            .where(or_(
                and_(Asset.asset_date == current_max_date,
                     active_item_predicate(current_max_date)),
                and_(Asset.asset_date == compare_max_date,
                     active_item_predicate(compare_max_date)),
            ))
            .order_by(Asset.asset_name)
        ).scalars().all()
        return [(name, name) for name in names]

    if dimension == DIM_LIQUIDITY:
        return [(str(days), label)
                for days, label in classification_service.LIQUIDITY_LEVELS]
    if dimension == DIM_RISK_LEVEL:
        return [(level, level) for level in _RISK_LEVELS]
    if dimension == DIM_ASSET_CLASS:
        rows = db.execute(
            select(AssetClass).order_by(AssetClass.level, AssetClass.code)
        ).scalars().all()
        return [(str(row.id), row.name) for row in rows]
    model = _DICT_MODELS.get(dimension)
    if model is not None:
        rows = db.execute(select(model).order_by(model.name)).scalars().all()
        return [(str(row.id), row.name) for row in rows]
    return []


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
    for target_id, name in _detail_values(db, dimension, current_max_date,
                                          compare_max_date):
        current = _sum_at(db, dimension, target_id, current_max_date)
        compare = _sum_at(db, dimension, target_id, compare_max_date)
        change = current - compare
        change_pct = float(change / abs(compare) * 100) if compare != 0 else None

        details.append(AlertDetailItem(
            id=target_id,
            name=name,
            current_amount=current,
            compare_amount=compare,
            change_amount=change,
            change_percent=change_pct,
            triggered=_check_triggered(change, change_pct, amount_threshold,
                                       percent_threshold, direction),
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


def check_alert(db: Session, rule_id: int, current_period: Optional[str] = None,
                include_details: bool = True,
                dates: Optional[list[date]] = None,
                groups_by_type: Optional[dict] = None,
                amount_cache: Optional[dict] = None) -> Optional[AlertResult]:
    rule = get_alert_rule(db, rule_id)
    if not rule or not rule.is_active:
        return None
    
    if dates is None:
        dates = _get_all_snapshot_dates(db)
    if not dates:
        return None
    
    groups = (groups_by_type or {}).get(rule.period_type)
    if groups is None:
        groups = group_dates_by_period(dates, rule.period_type)
    if not groups:
        return None
    
    if current_period is None:
        current_period = list(groups.keys())[-1]

    # 口径失效的规则：读时标 invalid（triggered=False ＋ 说明），不删规则、不算金额
    invalidation = rule_invalidation(rule, db)
    if invalidation:
        return AlertResult(
            rule_id=rule.id,
            rule_name=rule.name,
            dimension=rule.dimension,
            target_name=_get_target_name(db, rule.dimension, rule.target_id),
            current_period=current_period,
            compare_period=None,
            current_amount=ZERO,
            compare_amount=ZERO,
            change_amount=ZERO,
            change_percent=None,
            amount_threshold=rule.amount_threshold,
            percent_threshold=rule.percent_threshold,
            direction=rule.direction,
            triggered=False,
            details=[],
            message=invalidation,
        )

    if rule.compare_type == "previous":
        # Use new logic: find the most recent period with data
        compare_period = _get_last_period_with_data(
            db, current_period, rule.period_type, dates=dates, groups=groups)
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
            current_amount=_get_amount_for_period(db, current_period, rule.dimension, rule.target_id,
                                                  dates=dates, groups=groups,
                                                  amount_cache=amount_cache),
            compare_amount=ZERO,
            change_amount=ZERO,
            change_percent=None,
            amount_threshold=rule.amount_threshold,
            percent_threshold=rule.percent_threshold,
            direction=rule.direction,
            triggered=False,
            details=[],
            message="无上一期数据可供对比",
        )
    
    current_amount = _get_amount_for_period(db, current_period, rule.dimension, rule.target_id,
                                            dates=dates, groups=groups,
                                            amount_cache=amount_cache)
    compare_amount = _get_amount_for_period(db, compare_period, rule.dimension, rule.target_id,
                                            dates=dates, groups=groups,
                                            amount_cache=amount_cache)
    
    change_amount = current_amount - compare_amount
    change_percent = None
    if compare_amount != 0:
        change_percent = float(change_amount / abs(compare_amount) * 100)
    
    triggered = _check_triggered(change_amount, change_percent, rule.amount_threshold, rule.percent_threshold, rule.direction)
    
    # Get detailed changes if target_id is null (all items)
    details = []
    if include_details and rule.target_id is None:
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
    """Get alert results for all active rules（口径失效的规则以 invalid 结果出现）。"""
    rules = get_alert_rules(db, active_only=True)
    results = []
    for rule in rules:
        result = check_alert(db, rule.id, current_period)
        if result:
            results.append(result)
    return results


def check_all_alerts(db: Session) -> list[AlertResult]:
    """Check all active alert rules."""
    return get_alert_results(db)


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