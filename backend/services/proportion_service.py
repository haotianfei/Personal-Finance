"""占比分析服务——分类口径读**资产项级**（ADR-0019 D2）。

为什么整体重写读数口径：旧实现每一处都是 `join(Asset.attributes)`，把占比维度读自
记录级 EAV（`asset_attributes` 挂在每一条 `assets` 快照行上）。ADR-0019 把分类上移到
`asset_items` 的列 ＋ `asset_item_tags`，所以这里所有分类维度统一走
`assets.asset_item_id → asset_items`（需要展示名再 join 三张字典表）：

- 记录（`assets`）只承载**账本事实**：金额、侧别、快照日；
- 分类（类别/流动性/机构/渠道/归属/风险/标签）是**资产项**的固有属性。

维度值（`get_proportion_data` / `get_all_proportions` 的 `dimension`）：

| dimension     | 分组键                                         | 展示名           |
|---------------|------------------------------------------------|------------------|
| `asset_class` | `asset_items.asset_class_id`（树，可 `level` 上卷） | `asset_classes.name` |
| `liquidity`   | `asset_items.liquidity_days`（整数档位）        | `liquidity_label()` |
| `institution` | `asset_items.institution_id`                    | `institutions.name` |
| `channel`     | `asset_items.channel_id`                        | `channels.name` |
| `asset_name`  | `assets.asset_name`（历史行的名称快照）          | 快照名           |
| `owner`       | `asset_items.owner_id`                          | `members.name`   |
| `risk_level`  | `asset_items.risk_level`（R1..R5）              | 档位本身         |
| `tag`         | `asset_item_tags.tag_id`（项级多值）             | `tags.name`      |

旧值（`fund_type` / `liquidity_rating` / `account`）**一律 422 并给出新名**，不做静默
兼容：旧维度在新结构下命中空集，而空集在占比图里表现成「这笔钱不存在」，比报错更危险
（与 `alert_service.require_dimension` / `allocation_service.require_dimension` 同一处理）。

`tag` 是多值维度：同一笔金额同时计入其所有标签，故各标签占比之和可超过 100%——这是
多值语义本身决定的，不是双计（同一标签内 (项, 标签) 唯一，金额不会被重复累加）。
另有一行派生行「未打标」（`key="untagged"`）：没有任何标签的项不会被标签 join 命中，
单列一行才不会让占比图看似漏算（D9，见 `get_proportion_by_tag`）。
"""
from datetime import date
from decimal import Decimal
from typing import Optional, Literal

from sqlalchemy.orm import Session
from sqlalchemy import select, func, distinct, and_, literal

from errors import ApiError, ErrorCode
from models import (Asset, AssetClass, AssetItem, AssetItemTag, Channel,
                    Institution, Member, Tag)
from services import classification_service
from services.period_service import get_period_label, get_representative_date, group_dates_by_period
from services.meta_service import build_meta
from services.snapshot_identity import active_item_predicate, row_identity


# ---------------------------------------------------------------- 维度值域

DIM_ASSET_CLASS = "asset_class"
DIM_LIQUIDITY = "liquidity"
DIM_INSTITUTION = "institution"
DIM_CHANNEL = "channel"
DIM_ASSET_NAME = "asset_name"
DIM_OWNER = "owner"
DIM_RISK_LEVEL = "risk_level"
DIM_TAG = "tag"

#: 「未打标」派生行的行标识与展示文案（`tag` 维度专用，D9）。
#: 行标识给机器用（前端按键识别），label 可随时改文案而不破坏调用方。
UNTAGGED_ROW_KEY = "untagged"
UNTAGGED_ROW_LABEL = "未打标"

SUPPORTED_DIMENSIONS: tuple[str, ...] = (
    DIM_ASSET_CLASS, DIM_LIQUIDITY, DIM_INSTITUTION, DIM_CHANNEL,
    DIM_ASSET_NAME, DIM_OWNER, DIM_RISK_LEVEL, DIM_TAG,
)

# 旧值 → 新值（**仅**用于 422 文案，绝不参与解析）
LEGACY_DIMENSION_HINTS: dict[str, str] = {
    "fund_type": DIM_ASSET_CLASS,
    "liquidity_rating": DIM_LIQUIDITY,
    "account": f"{DIM_INSTITUTION}（渠道请用 {DIM_CHANNEL}）",
}

# `risk` 是预警/配置目标维度用的写法（alert_service / allocation_service），
# 与 `risk_level` 同指 `asset_items.risk_level`——两个都是新名，都接受，
# 避免同一维度在两处各叫一个名字。
_DIMENSION_SYNONYMS: dict[str, str] = {"risk": DIM_RISK_LEVEL}

ProportionDimension = Literal[
    "asset_class", "liquidity", "institution", "channel",
    "asset_name", "owner", "risk_level", "tag",
]

# 分类维度都必须先 join 到资产项（分类是项级属性）；`asset_name` 是行级快照，不 join。
_ITEM_JOIN = (AssetItem, AssetItem.id == Asset.asset_item_id)


def require_dimension(dimension: str | None) -> str:
    """校验维度名：只接受新值；旧值/未知值 → 422 并给出替换名。"""
    name = (dimension or "").strip()
    name = _DIMENSION_SYNONYMS.get(name, name)
    if name in SUPPORTED_DIMENSIONS:
        return name
    hint = LEGACY_DIMENSION_HINTS.get((dimension or "").strip())
    if hint:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            f"占比维度 {dimension} 已废弃（ADR-0019：分类从记录级上移到资产项）："
            f"请改用 {hint}",
            {"dimension": dimension, "replacement": hint},
        )
    raise ApiError(
        ErrorCode.VALIDATION_ERROR, 422,
        f"不支持的占比维度 {dimension or '(空)'}；可用维度：{list(SUPPORTED_DIMENSIONS)}",
        {"dimension": dimension, "supported": list(SUPPORTED_DIMENSIONS)},
    )


def _get_latest_snapshot_date(db: Session) -> Optional[date]:
    """获取最新的快照日期"""
    result = db.execute(
        select(func.max(Asset.asset_date))
    ).scalar()
    return result


# 行归属身份的唯一定义处（改名双计 bug 的成因就是此处曾被复制两份）
_row_identity = row_identity


def _latest_snapshot_subq(snapshot_date: date):
    """每个资产项（身份见 _row_identity）在指定日期（含）前的最新一条 active 快照。

    v5 写语义：superseded（被更正）行不入净值/占比口径。
    分组键是 asset_item_id（稳定身份），不是 asset_name 快照。
    归档项自生效日起不入占比（ADR-0017 D2 修订）：谓词取自
    `snapshot_identity.active_item_predicate(snapshot_date)`，与
    `analysis_service._get_latest_records_as_of` 共用同一份，杜绝口径分叉；
    归档生效日之前的账期照常计入其历史值。
    """
    identity = _row_identity()
    return (
        select(
            identity.label("identity"),
            func.max(Asset.asset_date).label("max_date")
        )
        .where(Asset.asset_date <= snapshot_date, Asset.superseded_by.is_(None),
               active_item_predicate(snapshot_date))
        .group_by(identity)
    ).subquery()


def _join_latest_snapshot(identity, subq, snapshot_date: date):
    """把主查询的行重新连回「该身份的最新日期」子查询（active 行口径 + 归档排除）。

    谓词必须与 `_latest_snapshot_subq(snapshot_date)` 传同一个日期，否则
    「取最新日期」与「取该日金额」两处子集会不一致（同一项可能出现 max_date
    行被这里滤掉 → 该身份整体消失或金额错配）。
    """
    return and_(
        identity == subq.c.identity,
        Asset.asset_date == subq.c.max_date,
        Asset.superseded_by.is_(None),
        active_item_predicate(snapshot_date),
    )


def _empty_proportion_result() -> dict:
    return {
        "asset_items": [],
        "liability_items": [],
        "total_assets": Decimal("0"),
        "total_liabilities": Decimal("0"),
    }


def get_snapshot_date_by_period(db: Session, period: str, period_type: str) -> Optional[date]:
    """根据账期和账期类型获取代表性快照日期

    Args:
        period: 账期标签，如 "2026", "2026-05", "2026-Q1", "2026-W01"
        period_type: 账期类型，如 "year", "month", "quarter", "week", "day"

    Returns:
        该账期对应的代表性日期（该账期内最新的数据日期），如果没有数据则返回None
    """
    dates = db.execute(
        select(distinct(Asset.asset_date))
        .where(Asset.superseded_by.is_(None))   # x 轴 active 行口径（与 P0-1 一致）
    ).scalars().all()

    if not dates:
        return None

    # 按账期分组
    period_groups = group_dates_by_period(dates, period_type)

    # 查找匹配的账期
    if period in period_groups:
        # 返回该账期内最新的日期
        return max(period_groups[period])

    return None


def get_available_periods(db: Session, period_type: str = "month") -> list[dict]:
    """获取可用的账期列表

    Args:
        period_type: 账期类型 - day(日), month(月), quarter(季度), year(年)

    Returns:
        账期列表，每个账期包含 label 和代表性日期
    """
    dates = db.execute(
        select(distinct(Asset.asset_date))
        .where(Asset.superseded_by.is_(None))   # x 轴 active 行口径（与 P0-1 一致）
        .order_by(Asset.asset_date.desc())
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


# ---------------------------------------------------------------- 聚合口径（单一定义处）


def _aggregate(db: Session, snapshot_date: date, side: str, columns,
               joins=(), desc: bool = True):
    """按 `columns` 聚合某一侧的 as-of 金额与资产项数。

    所有维度共用这一条语句，杜绝「每个维度各写一份 as-of 口径」——旧实现每个函数
    各抄一遍 join 与过滤，正是改名双计与归档口径分叉的成因。

    Args:
        columns: 分组键 + 展示名（全部进 `GROUP BY`）。
        joins: [(model, onclause)]，按顺序 INNER JOIN。分类维度必须
            join `asset_items`（分类是项级属性）。**不**用 outer join 到
            `asset_items`：没有资产项的存量行没有分类，把它们塞进「未分类」组
            会把「未迁移的行」伪装成「已分类为未分类」。
        desc: 资产侧按金额降序（首屏即大头），负债侧升序（旧行为）。
    """
    identity = _row_identity()
    subq = _latest_snapshot_subq(snapshot_date)
    stmt = (
        select(
            *columns,
            func.sum(Asset.amount).label("total_amount"),
            func.count(func.distinct(identity)).label("asset_count"),
        )
        .select_from(Asset)
    )
    for model, onclause in joins:
        stmt = stmt.join(model, onclause)
    stmt = (
        stmt.join(subq, _join_latest_snapshot(identity, subq, snapshot_date))
        .where(Asset.side == side)
        .group_by(*columns)
    )
    amount_sum = func.sum(Asset.amount)
    return db.execute(stmt.order_by(amount_sum.desc() if desc else amount_sum.asc())).all()


def _pair(name, amount, count, **extra) -> tuple:
    return (name, amount, count, extra or None)


def _to_items(pairs: list[tuple], side: str) -> tuple[Decimal, list[dict]]:
    """(name, amount, count, extra) 列表 → 占比条目（同侧各自 100%）。

    分母＝该侧各分组金额之和（与改前一致：占比图内各扇区之和为 100%）。
    `tag` 是多值维度，同侧各标签之和可超过 100%，见模块 docstring。
    """
    total = sum((p[1] for p in pairs), Decimal("0"))
    denominator = total if total > 0 else Decimal("1")
    items = []
    for name, amount, count, extra in pairs:
        item = {
            "name": name,
            "amount": amount,
            "count": count,
            "percent": round(float(amount / denominator * 100), 2),
            "type": side,
        }
        if extra:
            item.update(extra)
        items.append(item)
    return total, items


def _result(asset_pairs: list[tuple], liability_pairs: list[tuple]) -> dict:
    total_assets, asset_items = _to_items(asset_pairs, "asset")
    total_liabilities, liability_items = _to_items(liability_pairs, "liability")
    return {
        "asset_items": asset_items,
        "liability_items": liability_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
    }


def _both_sides(db: Session, snapshot_date: date, columns, joins=()) -> tuple[list, list]:
    return (_aggregate(db, snapshot_date, "asset", columns, joins, desc=True),
            _aggregate(db, snapshot_date, "liability", columns, joins, desc=False))


# ---------------------------------------------------------------- 维度实现


def get_proportion_by_liquidity(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按流动性档位（`liquidity_days` 整数天数）分析占比，同时返回资产和负债。

    ADR-0019 D4：档位唯一权威是 `asset_items.liquidity_days`，展示名由
    `classification_service.liquidity_label()` 派生（不建 lookup 表）。
    条目上额外带 `days`（整数），供前端按档位排序而不是按中文名排序。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return _empty_proportion_result()

    columns = (AssetItem.liquidity_days,)
    asset_result, liability_result = _both_sides(
        db, snapshot_date, columns, joins=(_ITEM_JOIN,))

    def pairs(rows):
        return [
            _pair(classification_service.liquidity_label(r.liquidity_days)
                  or "未指定档位",
                  r.total_amount, r.asset_count, days=r.liquidity_days)
            for r in rows
        ]

    return _result(pairs(asset_result), pairs(liability_result))


def get_proportion_by_asset_class(
    db: Session,
    snapshot_date: Optional[date] = None,
    level: Optional[int] = None
) -> dict:
    """按资产类别分析占比（类别树，ADR-0019），同时返回资产和负债。

    Args:
        level: 树层级（根＝1）。None＝按叶子类别原样分组；给定层级时把每个叶子
            上卷到该层的祖先（`asset_classes.parent_id` 链），语义与旧
            `fund_type` 的 level 上卷一致。

    上卷在内存里做：类别树是几十个节点的小表，一次全量载入并建 `id → node` 映射，
    绝不逐行回查（旧实现在 Python 侧持有 Attribute 对象还带延迟加载）。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return _empty_proportion_result()

    if level is not None and level < 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            "level 从 1 开始（根类别＝1）；旧 fund_type 的 0 基层级随 attributes 表废弃",
            {"field": "level", "level": level},
        )

    nodes = {c.id: c for c in db.execute(select(AssetClass)).scalars().all()}

    columns = (AssetItem.asset_class_id,)
    asset_result, liability_result = _both_sides(
        db, snapshot_date, columns, joins=(_ITEM_JOIN,))

    def rollup(class_id, target_level: Optional[int]) -> Optional[int]:
        """叶子（或其自身层级）→ target_level 的祖先；无该层祖先返回 None。"""
        node = nodes.get(class_id)
        if node is None:
            return None
        if target_level is None or node.level <= target_level:
            return node.id
        seen = {node.id}
        while node.parent_id is not None:
            node = nodes.get(node.parent_id)
            if node is None or node.id in seen:
                return None
            seen.add(node.id)
            if node.level == target_level:
                return node.id
        return None

    def pairs(rows, reverse: bool):
        buckets: dict[int, dict] = {}
        for r in rows:
            node_id = rollup(r.asset_class_id, level)
            if node_id is None:
                continue                 # 未分类行 / 无该层祖先 → 不进任何桶
            bucket = buckets.setdefault(
                node_id, {"amount": Decimal("0"), "count": 0})
            bucket["amount"] += r.total_amount
            bucket["count"] += r.asset_count
        ordered = sorted(buckets.items(),
                         key=lambda kv: kv[1]["amount"], reverse=reverse)
        return [
            _pair(nodes[node_id].name, v["amount"], v["count"],
                  level=nodes[node_id].level)
            for node_id, v in ordered
        ]

    return _result(pairs(asset_result, True), pairs(liability_result, False))


def _by_dictionary(db: Session, snapshot_date: date, dimension: str) -> dict:
    """机构 / 渠道 / 归属三个「项上单值外键 → 字典表」维度的共用实现。

    字典表用 **LEFT JOIN**：这些列可空（渠道/归属在 draft 或历史迁移态可能为空），
    空值要显式归到「未指定 X」组，而不是让整行从占比里消失——钱不能因为缺一个
    展示名就不见了。
    """
    model, column, join_col, missing_label = {
        DIM_INSTITUTION: (Institution, AssetItem.institution_id,
                          AssetItem.institution_id == Institution.id, "未指定机构"),
        DIM_CHANNEL: (Channel, AssetItem.channel_id,
                      AssetItem.channel_id == Channel.id, "未指定渠道"),
        DIM_OWNER: (Member, AssetItem.owner_id,
                    AssetItem.owner_id == Member.id, "未指定归属"),
    }[dimension]

    identity = _row_identity()
    subq = _latest_snapshot_subq(snapshot_date)

    def rows(side: str, desc: bool):
        stmt = (
            select(
                column.label("key"),
                model.name.label("label"),
                func.sum(Asset.amount).label("total_amount"),
                func.count(func.distinct(identity)).label("asset_count"),
            )
            .select_from(Asset)
            .join(*_ITEM_JOIN)
            .outerjoin(model, join_col)
            .join(subq, _join_latest_snapshot(identity, subq, snapshot_date))
            .where(Asset.side == side)
            .group_by(column, model.name)
        )
        amount_sum = func.sum(Asset.amount)
        return db.execute(
            stmt.order_by(amount_sum.desc() if desc else amount_sum.asc())).all()

    def pairs(result):
        return [_pair(r.label or missing_label, r.total_amount, r.asset_count)
                for r in result]

    return _result(pairs(rows("asset", True)), pairs(rows("liability", False)))


def get_proportion_by_institution(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按机构分析占比（`asset_items.institution_id`）。

    旧 `account` 维度里同时混着账户、机构、渠道与「现金/物资」等非账户值
    （ADR-0019 §1(3)）；拆分后机构口径是纯粹的外键聚合，不再靠字符串前缀匹配。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)
    if not snapshot_date:
        return _empty_proportion_result()
    return _by_dictionary(db, snapshot_date, DIM_INSTITUTION)


def get_proportion_by_channel(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按渠道分析占比（`asset_items.channel_id`，可空 → 「未指定渠道」）。"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)
    if not snapshot_date:
        return _empty_proportion_result()
    return _by_dictionary(db, snapshot_date, DIM_CHANNEL)


def get_proportion_by_owner(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按归属分析占比（`asset_items.owner_id` → `members`）。

    归属变更史仍在 `asset_item_owner_changes`（append-only）；占比读的是**当前**
    归属列，与「当前值/列表」读模型同一口径，不回溯历史归属。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)
    if not snapshot_date:
        return _empty_proportion_result()
    return _by_dictionary(db, snapshot_date, DIM_OWNER)


def get_proportion_by_risk_level(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按风险等级分析占比（`asset_items.risk_level`，R1..R5，可空 → 「未评级」）。"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return _empty_proportion_result()

    columns = (AssetItem.risk_level,)
    asset_result, liability_result = _both_sides(
        db, snapshot_date, columns, joins=(_ITEM_JOIN,))

    def pairs(rows):
        return [_pair(r.risk_level or "未评级", r.total_amount, r.asset_count)
                for r in rows]

    return _result(pairs(asset_result), pairs(liability_result))


def _untagged_tag_row(db: Session, snapshot_date: date, side: str):
    """某一侧「未打标」项的金额合计与项数（D9 派生行的唯一取数处）。

    补集用 `NOT EXISTS (SELECT 1 FROM asset_item_tags WHERE item_id = asset_items.id)`：
    标签聚合必须 inner join 关联表才能分组，若在同一条语句上再叠一个
    `LEFT JOIN … IS NULL` 求补集，第一次 join 已把行按标签放大成 N 份
    （同一项 2 个标签 → 金额计 2 次），补集口径就对不上了。补集单独一条语句算，
    主查询保持「一笔金额在每个标签下各出现一次」的唯一结构。

    归档/汇总项的排除由 `active_item_predicate(snapshot_date)` 承担——它与标签聚合
    走的是同一个 `_join_latest_snapshot`（同一份谓词、同一个 as_of），故「未打标」
    行与各标签行的在册项集合天然一致，不会有「归档项只在某一边消失」的分叉。
    """
    identity = _row_identity()
    subq = _latest_snapshot_subq(snapshot_date)
    has_tag = select(literal(1)).where(
        AssetItemTag.item_id == AssetItem.id).exists()
    stmt = (
        select(
            func.sum(Asset.amount).label("total_amount"),
            func.count(func.distinct(identity)).label("asset_count"),
        )
        .select_from(Asset)
        .join(*_ITEM_JOIN)
        .join(subq, _join_latest_snapshot(identity, subq, snapshot_date))
        .where(Asset.side == side, ~has_tag)
    )
    return db.execute(stmt).one()


def get_proportion_by_tag(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """按标签分析占比（`asset_item_tags`，项级多值：命中任一即计入）＋「未打标」派生行。

    多值语义：同一笔金额会同时出现在它的每个标签下，故各标签占比之和可超过
    100%（同一标签内 (项, 标签) 唯一，组内不重复累加）。

    「未打标」派生行（D9）：没有任何标签的项不会被标签 join 命中，单列一行承接，
    否则占比图看起来像漏算了一整块钱（项级标签覆盖率远未到 100%）。该行由
    `_untagged_tag_row` 以 `NOT EXISTS` 独立取数，`name`＝展示文案「未打标」、
    `key`＝`untagged`（行标识，供前端按键识别而非按中文文案匹配）。它的项集与
    各标签行的项集互补且不重叠：**各标签行并集 ＋ 未打标行 ＝ 该侧全部在计量项**。
    派生行排在各标签之后（它不是标签，不参与标签间排序）。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return _empty_proportion_result()

    joins = (_ITEM_JOIN,
             (AssetItemTag, AssetItemTag.item_id == AssetItem.id),
             (Tag, Tag.id == AssetItemTag.tag_id))
    columns = (Tag.id, Tag.name)
    asset_result, liability_result = _both_sides(db, snapshot_date, columns, joins=joins)

    def pairs(rows, side: str):
        out = [_pair(r.name, r.total_amount, r.asset_count) for r in rows]
        untagged = _untagged_tag_row(db, snapshot_date, side)
        if untagged.asset_count:       # 无未打标项 → 不出空行（按项数判定，金额 0 仍出）
            out.append(_pair(UNTAGGED_ROW_LABEL, untagged.total_amount,
                             untagged.asset_count, key=UNTAGGED_ROW_KEY))
        return out

    return _result(pairs(asset_result, "asset"),
                   pairs(liability_result, "liability"))


def get_proportion_by_asset_name(
    db: Session,
    snapshot_date: Optional[date] = None,
    top_n: int = 20
) -> dict:
    """按资产名称分析占比，返回Top N + 其他，同时返回资产和负债。

    名称维度保持读 `assets.asset_name`（历史行的名称快照）：这是「当时叫什么」的
    记录级事实，改名不回写历史（拍板 10）。同一资产项按身份聚合，改名不会双计。
    """
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    if not snapshot_date:
        return _empty_proportion_result()

    identity = _row_identity()
    subq = _latest_snapshot_subq(snapshot_date)

    def rows(side: str, desc: bool):
        stmt = (
            select(
                Asset.asset_name,
                func.sum(Asset.amount).label("total_amount"),
            )
            .select_from(Asset)
            .join(subq, _join_latest_snapshot(identity, subq, snapshot_date))
            .where(Asset.side == side)
            .group_by(Asset.asset_name)
        )
        amount_sum = func.sum(Asset.amount)
        return db.execute(
            stmt.order_by(amount_sum.desc() if desc else amount_sum.asc())).all()

    asset_result = rows("asset", True)
    liability_result = rows("liability", False)

    # 处理资产项 - Top N + 其他
    asset_pairs = []
    others_amount = Decimal("0")
    for i, r in enumerate(asset_result):
        if i < top_n:
            asset_pairs.append(_pair(r.asset_name, r.total_amount, 1))
        else:
            others_amount += r.total_amount

    if others_amount > 0:
        asset_pairs.append(_pair("其他资产", others_amount,
                                 len(asset_result) - top_n))

    liability_pairs = [_pair(r.asset_name, r.total_amount, 1)
                       for r in liability_result]
    return _result(asset_pairs, liability_pairs)


_DIMENSION_HANDLERS = {
    DIM_ASSET_CLASS: get_proportion_by_asset_class,
    DIM_LIQUIDITY: get_proportion_by_liquidity,
    DIM_INSTITUTION: get_proportion_by_institution,
    DIM_CHANNEL: get_proportion_by_channel,
    DIM_ASSET_NAME: get_proportion_by_asset_name,
    DIM_OWNER: get_proportion_by_owner,
    DIM_RISK_LEVEL: get_proportion_by_risk_level,
    DIM_TAG: get_proportion_by_tag,
}

#: 可上卷层级的维度（只有类别树有父子）
_TREE_DIMENSIONS = frozenset({DIM_ASSET_CLASS})


def get_proportion_data(
    db: Session,
    dimension: ProportionDimension,
    snapshot_date: Optional[date] = None,
    level: Optional[int] = None
) -> dict:
    """获取指定维度的占比数据，包含资产和负债。

    Args:
        dimension: 见模块 docstring 的维度表；旧值（fund_type / liquidity_rating /
            account）→ 422 并给出新名。
        level: 仅 `asset_class` 维度有效，指定树层级上卷（根＝1）。
    """
    dimension = require_dimension(dimension)

    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    handler = _DIMENSION_HANDLERS[dimension]
    if dimension in _TREE_DIMENSIONS:
        result = handler(db, snapshot_date, level)
    else:
        result = handler(db, snapshot_date)

    # 合并资产和负债项用于图表显示
    all_items = result.get("asset_items", []) + result.get("liability_items", [])
    total_count = sum(item.get("count", 0) for item in all_items)

    return {
        "dimension": dimension,
        "snapshot_date": snapshot_date,
        "level": level if dimension in _TREE_DIMENSIONS else None,
        "total_assets": result.get("total_assets", Decimal("0")),
        "total_liabilities": result.get("total_liabilities", Decimal("0")),
        "net_worth": result.get("net_worth", Decimal("0")),
        "total_count": total_count,
        "asset_items": result.get("asset_items", []),
        "liability_items": result.get("liability_items", []),
        "items": all_items,  # 兼容旧接口
        "meta": build_meta(db, snapshot_date),
    }


def get_all_proportions(
    db: Session,
    snapshot_date: Optional[date] = None
) -> dict:
    """获取所有维度的占比数据（键＝新维度名，与 `SUPPORTED_DIMENSIONS` 一一对应）。"""
    if snapshot_date is None:
        snapshot_date = _get_latest_snapshot_date(db)

    return {
        "snapshot_date": snapshot_date,
        **{dim: _DIMENSION_HANDLERS[dim](db, snapshot_date)
           for dim in SUPPORTED_DIMENSIONS},
        "meta": build_meta(db, snapshot_date),
    }