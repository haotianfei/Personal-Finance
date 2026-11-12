from datetime import date
from bisect import bisect_right
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import select, func, distinct, and_, text

from models import Asset, AssetClass, AssetItem, PrincipalChange
from errors import ApiError, ErrorCode
from schemas import (
    TrendPoint, ComparisonPoint, MixedChartData, DriftAddedItem, DriftVanishedItem,
    ItemTrendPoint, SummaryData,
)
from services.period_service import (
    group_dates_by_period, get_representative_date, get_period_label,
    get_period_start_end,
)
from services.meta_service import (
    build_meta, NO_DATA, RATE_UNAVAILABLE, TWR_LARGE_FLOW_APPROX,
    PRINCIPAL_BASIS_VERSION,
)
from services.snapshot_identity import (
    active_item_predicate, aggregate_flag_expression, measuring_at, row_identity,
)
from services import returns_service
from services import classification_service

ZERO = Decimal("0")

# 漂移疑似阈值（spec §3）：max(added, vanished) / max(|净值|, 1) >= 此处即标记。
# 首版 10%，集中在此常量；观察一周真实标注后再调参（不散落到调用方/前端）。
DRIFT_THRESHOLD = Decimal("0.10")


def _get_all_snapshot_dates(db: Session, date_from: date | None = None, date_to: date | None = None) -> list[date]:
    # x 轴日期＝active 行口径：只有 superseded（被更正）行的日期不撑出趋势点
    # （spec §4 P0-1；与 _get_latest_records_as_of 的 superseded 过滤同语义）
    q = (select(distinct(Asset.asset_date))
         .where(Asset.superseded_by.is_(None))
         .order_by(Asset.asset_date))
    if date_from:
        q = q.where(Asset.asset_date >= date_from)
    if date_to:
        q = q.where(Asset.asset_date <= date_to)
    return list(db.execute(q).scalars().all())


# 行归属身份的唯一定义处（改名双计 bug 的成因就是此处曾被复制两份）
_row_identity = row_identity


def _get_latest_records_as_of(db: Session, as_of: date, *,
                              include_archived: bool = False) -> list[tuple]:
    """截至 as_of 每个**资产项**的最近已知值（as-of 语义，CONTEXT.md:17-22）。

    Logic: for each unique identity（asset_item_id，缺失时回退 asset_name）, find
    the active record with the maximum asset_date <= as_of, and use that record's
    amount. 期内未更新的资产沿用旧值（跨期 carry），首录前的资产不出现（不计入）。
    改名只改 item 显示名、不回写历史 name 快照，按 item id 分组可避免新旧名
    被当成两个资产而双计（改名双计回归）。

    归档排除（ADR-0017 D2 修订，`.scratch/item-identity-drift/spec.md` §4 P0-2）：
    谓词来自 `snapshot_identity.active_item_predicate(as_of)`（与 `row_identity()`
    同处定义，占比聚合复用同一份）——**归档＝自归档生效日起终止计量**，故只有
    `archived_effective_from <= as_of` 的项退出本日聚合，生效日之前的真实历史值
    照常计入（不得追溯抹除）。归档生效日为 NULL（存量形态）＝日期未知 → 该日也剔除。
    **`include_archived=True` 仅供逐项陈列类读模型**（如资产当前值）：陈列是
    「每项一行 + 其最后记录」，不是求和，归档项也应显示自己的最后一条记录值；
    是否列出该行由调用方的 item 状态过滤决定（`asset_service.get_current_values`
    :880-882）。其余一切求和/派生口径必须保持默认 False。

    Returns list of (asset_name, amount, asset_id, side, asset_date) tuples；
    asset_name 为该组最新行的 name 快照；行内 asset_date 供沿用天数（`_carry_stats`）
    与陈旧度计算——**不是**覆盖度分母（该语义已废止，ADR-0019 D12）。
    """
    identity = _row_identity()
    # 行级口径（两处复用）：superseded 行恒不入；归档项自生效日起不入
    # （两处必须传同一个 as_of，否则「最新日期」与「取值」会取到不同子集）
    row_filters = [Asset.superseded_by.is_(None)]
    if not include_archived:
        row_filters.append(active_item_predicate(as_of))

    # Subquery: for each identity, find the max date <= as_of
    # （v5 写语义：active 行口径，superseded 行不入净值；归档项默认不入净值）
    max_date_sub = (
        select(
            identity.label("identity"),
            func.max(Asset.asset_date).label("max_date"),
        )
        .where(Asset.asset_date <= as_of, *row_filters)
        .group_by(identity)
        .subquery()
    )

    # Main query: join back to get the amount at the max date for each identity
    result = db.execute(
        select(
            Asset.asset_name,
            Asset.amount,
            Asset.id,
            Asset.side,
            Asset.asset_date,
        )
        .join(
            max_date_sub,
            and_(
                identity == max_date_sub.c.identity,
                Asset.asset_date == max_date_sub.c.max_date,
                *row_filters,
            ),
        )
    ).all()

    return result


def _get_latest_records_for_items(db: Session, as_of: date,
                                  item_ids: set[int] | list[int]) -> list[tuple]:
    """`_get_latest_records_as_of` 的族内版本：一次查询取指定 item 的 as-of 值。

    返回 (asset_item_id, asset_name, amount, side, asset_date)；口径完全复用
    `active_item_predicate(as_of)`（归档项自生效日起退出），并按 item id 分组
    避免历史 name 快照造成双计。整族一次查询 = O(points × 1)，无逐成员 N+1。
    """
    ids = list(item_ids)
    if not ids:
        return []
    row_filters = [Asset.superseded_by.is_(None),
                   active_item_predicate(as_of),
                   Asset.asset_item_id.in_(ids)]
    max_date_sub = (
        select(
            Asset.asset_item_id.label("item_id"),
            func.max(Asset.asset_date).label("max_date"),
        )
        .where(Asset.asset_date <= as_of, *row_filters)
        .group_by(Asset.asset_item_id)
        .subquery()
    )
    return db.execute(
        select(
            Asset.asset_item_id,
            Asset.asset_name,
            Asset.amount,
            Asset.side,
            Asset.asset_date,
        )
        .join(
            max_date_sub,
            and_(
                Asset.asset_item_id == max_date_sub.c.item_id,
                Asset.asset_date == max_date_sub.c.max_date,
                *row_filters,
            ),
        )
    ).all()


def _latest_records_by_as_of(db: Session, as_of_dates) -> dict[date, list[tuple]]:
    """一次取数 + 内存扫掠：每个 as_of 日期 → 该日「每身份最近一条」记录。

    `_get_latest_records_as_of(db, d)` 的**批量等价物**：返回逐元组同形状的
    `(asset_name, amount, asset_id, side, asset_date)` 列表，只是把「每期一条
    相关子查询」换成「一条全量取数 + 内存按日期扫掠」。day 粒度 730 个账期原本
    要发 730 条 SQL，每条的 `max(asset_date) GROUP BY identity` 都要扫一遍
    active 行，耗时随账期线性增长；这里账期数与数据库往返次数解耦。

    口径**逐字复用既有定义**，不另立新规：

    - `superseded_by IS NULL`：行级口径，与 `_get_latest_records_as_of` 同一 filter；
    - 归档排除：SQL 谓词 `active_item_predicate(as_of)`（归档项自
      `archived_effective_from` **当日起**退出，NULL＝归档日未知 → 该 as_of 也剔除）。
      Python 侧只调用 `snapshot_identity.measuring_at`——那是该谓词的**唯一**等值
      实现（同一处定义），本函数不手写第三份判断，避免口径分叉；
    - **汇总项排除**（ADR-0029 D1/D2，叶子计量）：父项标记由
      `snapshot_identity.aggregate_flag_expression()` **内联在同一条 SELECT** 里取回
      （不额外发查询，SQL 条数仍与账期数解耦），逐身份传给
      `measuring_at(is_aggregate=…)`——与 SQL 谓词里那条 `NOT EXISTS` 等价，
      于是**总额线＝叶子之和**，父项金额不参与任何 as-of 合计；
    - 身份分组键＝`row_identity()` 的等值映射：有 item 用 `("i", item_id)`，
      无 item 回退 `("n", asset_name)`（`'i:'+id` / `'n:'+name` 与该二元组一一对应，
      两种前缀互不碰撞）。按身份（而非名字）分组，改名不会把同一项拆成两笔。

    同身份同日可能存在多行（无 item 的存量行不受 `uq_assets_item_date_active`
    约束），故「最近一天」按**行块**整块返回——与 SQL 里 `asset_date == max_date`
    联接出多行的行为一致。

    调用方：`get_total_trend`（唯一调用方）。`_get_latest_records_as_of` 本体保持
    不变，其余调用方（summary/占比/告警等）不受影响。
    """
    target = sorted(set(as_of_dates))
    if not target:
        return {}

    rows = db.execute(
        select(
            Asset.asset_item_id,
            Asset.asset_name,
            Asset.asset_date,
            Asset.amount,
            Asset.id,
            Asset.side,
            AssetItem.status,
            AssetItem.archived_effective_from,
            # 汇总项标记内联在同一 SELECT（口径定义在 snapshot_identity，见其 docstring）
            aggregate_flag_expression().label("is_aggregate"),
        )
        .join(AssetItem, AssetItem.id == Asset.asset_item_id, isouter=True)
        .where(Asset.superseded_by.is_(None))
        .order_by(Asset.asset_date, Asset.id)
    ).all()

    # identity -> [(asset_date, asset_name, amount, asset_id, side)]，日期升序
    history: dict[tuple, list[tuple]] = {}
    # identity -> (status, archived_effective_from)：项级，同身份各行同值
    lifetime: dict[tuple, tuple] = {}
    # identity -> 是否汇总项（项级）
    is_aggregate: dict[tuple, bool] = {}
    for (item_id, name, d, amount, asset_id, side, status, arch_from,
         agg_flag) in rows:
        key = ("i", item_id) if item_id is not None else ("n", name)
        history.setdefault(key, []).append((d, name, amount, asset_id, side))
        lifetime[key] = (status, arch_from)
        # 汇总项（父项）退出**合计**口径（ADR-0029 D1）：总额线＝叶子之和，父项不在其中。
        # 父项自己的曲线走展示路径（`get_trend_by_item`），不受这里影响（D14）。
        is_aggregate[key] = bool(agg_flag)

    dates_by_identity = {key: [r[0] for r in hist] for key, hist in history.items()}

    out: dict[date, list[tuple]] = {}
    for as_of in target:
        records: list[tuple] = []
        for key, hist in history.items():
            if not measuring_at(*lifetime[key], as_of,
                                is_aggregate=is_aggregate[key]):
                continue                      # 归档自生效日起退出（此前照常计入）
            dates = dates_by_identity[key]
            i = bisect_right(dates, as_of)
            if i == 0:
                continue                      # 首录在 as_of 之后 → 该日尚未计入
            last = dates[i - 1]               # 该身份的最近估值日
            start = i - 1
            while start > 0 and dates[start - 1] == last:
                start -= 1                    # 同日多行 → 整块返回（同 SQL 联接）
            for k in range(start, i):
                _d, name, amount, asset_id, side = hist[k]
                records.append((name, amount, asset_id, side, last))
        out[as_of] = records
    return out


def _aggregate_records(records: list[tuple]) -> tuple[Decimal, Decimal, Decimal]:
    """as-of 记录集 → (net_worth, assets, liabilities)；金额恒正，按 side 拆分。"""
    if not records:
        return Decimal("0"), Decimal("0"), Decimal("0")

    pos = Decimal("0")
    liab = Decimal("0")
    for _name, amount, _asset_id, side, _d in records:
        amt = Decimal(str(amount))
        if side == "liability":
            liab += amt
        else:
            pos += amt

    return pos - liab, pos, liab


def _sum_as_of(db: Session, as_of: date) -> tuple[Decimal, Decimal, Decimal]:
    """Sum 截至 as_of 各资产最近已知值。

    Returns (net_worth, assets, liabilities)；金额恒正，按 side 拆分。
    """
    return _aggregate_records(_get_latest_records_as_of(db, as_of))


def _carry_stats(records: list[tuple], as_of: date) -> tuple[int, int]:
    """该 as-of 日的**沿用事实**（不做「部分录入」判断）→ (carried_items, carried_max_days)。

    账本语义是「变更日志」（ADR-0019 D12 / `.scratch/db-rebuild/spec.md` D4）：
    「这一天这个项没有记录」＝ **值没变**，不是「没核对」。因此这里不判完整/缺失，
    只把可核对的事实原样给出：

    - `carried_items`＝本次计入的项里，当前值来自**更早**估值日的项数（沿用项数）；
    - `carried_max_days`＝这些沿用项中最久的一个已沿用多少天；无沿用项 → 0。

    「计入的项」与求和同源（同一份 `_get_latest_records_as_of` 结果：superseded 行
    恒不入、归档项自 `archived_effective_from` 起由 `active_item_predicate` 排除），
    故 carried_items ≤ 计入项数，且归档项的沿用天数不会计入。
    """
    if not records:
        return 0, 0
    days = [(as_of - row[4]).days for row in records if row[4] is not None]
    carried = [d for d in days if d > 0]
    return len(carried), (max(carried) if carried else 0)


def _sum_for_date(db: Session, d: date) -> tuple[Decimal, Decimal, Decimal]:
    """Shortcut for single-date sum（as-of 语义，与 _sum_as_of 同义）。"""
    return _sum_as_of(db, d)


def _unclassified_as_of(db: Session, as_of: date) -> tuple[int, Decimal, Decimal]:
    """分类未补齐（`asset_items.status='draft'`）项在 as_of 当日的**规模披露**。

    口径前提（产品决策）：draft 项**照常计入净值**——钱是真实存在的，`_sum_as_of`
    也从不排除它们（`active_item_predicate` 只排除归档项）。本函数**只额外统计**
    「合计里有多少来自未补齐项」，绝不参与/改动合计本身。

    Returns (count, asset_amount, liability_amount)：
    - `count`＝该日有有效记录（未 supersede、非归档项）的 draft 项数；
    - 金额＝这些项的 as-of 值按 `side` 拆的**毛额**（asset 侧、liability 侧分别给出，
      不做净值正负相抵——两个数更适合被一句人话念出来）。
    draft 且当日无任何记录（或记录全被 supersede）的项不进 count，也不贡献金额：
    它没有可披露的金额，净值里本来就没有它的份额。

    取数形状与 `_get_latest_records_as_of` 完全同源（同样叠加
    `active_item_predicate(as_of)`，两处必须传同一个 as_of）：内层按
    `asset_item_id` 分组取 ≤as_of 的最新日期，外层回联该日行——**一条 SQL** 拿到
    全部 draft 项，计数与分侧求和在 Python 侧一次完成，不逐项查库（无 N+1）。
    """
    max_date_sub = (
        select(
            Asset.asset_item_id.label("item_id"),
            func.max(Asset.asset_date).label("max_date"),
        )
        .join(AssetItem, AssetItem.id == Asset.asset_item_id)
        .where(
            Asset.asset_date <= as_of,
            Asset.superseded_by.is_(None),
            AssetItem.status == "draft",
            active_item_predicate(as_of),
        )
        .group_by(Asset.asset_item_id)
        .subquery()
    )
    rows = db.execute(
        select(Asset.asset_item_id, Asset.amount, Asset.side)
        .join(
            max_date_sub,
            and_(
                Asset.asset_item_id == max_date_sub.c.item_id,
                Asset.asset_date == max_date_sub.c.max_date,
            ),
        )
        .where(
            Asset.superseded_by.is_(None),
            active_item_predicate(as_of),
        )
    ).all()

    asset_amount = ZERO
    liability_amount = ZERO
    item_ids: set[int] = set()
    for item_id, amount, side in rows:
        item_ids.add(item_id)
        amt = Decimal(str(amount))
        if side == "liability":
            liability_amount += amt
        else:
            asset_amount += amt
    return len(item_ids), asset_amount, liability_amount


def get_summary(db: Session) -> SummaryData:
    dates = _get_all_snapshot_dates(db)
    if not dates:
        # 空库：口径元数据标注 NO_DATA（宁缺勿滥，其余字段维持零值形状）
        return SummaryData(meta=build_meta(db, None, [
            {"code": "NO_DATA", "scope": None, "message": "尚无快照数据，净值为零值占位"}]))

    latest = dates[-1]
    total, assets, liabilities = _sum_as_of(db, latest)
    # 披露：合计里有多少来自「分类未补齐」的项（它们**本来就已计入**上面的合计，
    # 这里只额外统计，不改任何合计口径）
    unclassified_count, unclassified_asset, unclassified_liability = (
        _unclassified_as_of(db, latest))

    change = Decimal("0")
    change_pct = None
    if len(dates) >= 2:
        prev_date = dates[-2]
        prev_total, _, _ = _sum_as_of(db, prev_date)
        change = total - prev_total
        if prev_total != 0:
            change_pct = float(change / prev_total * 100)

    record_count = db.execute(
        select(func.count()).where(Asset.asset_date == latest)
    ).scalar() or 0

    return SummaryData(
        latest_date=latest,
        total_assets=assets,  # 资产总和（正数）
        total_liabilities=liabilities,  # 负债总和（正数）
        net_worth=total,  # 净值 = 资产总和 - 负债总和
        change_amount=change,
        change_percent=change_pct,
        record_count=record_count,
        snapshot_count=len(dates),
        unclassified_count=unclassified_count,
        unclassified_asset_amount=unclassified_asset,
        unclassified_liability_amount=unclassified_liability,
        meta=build_meta(db, latest),
    )


def _drift_sort_key(item: DriftAddedItem) -> tuple:
    """漂移明细排序键：金额降序，同额按 item_id 升序（无 item 的存量行置末）。"""
    return (-item.amount, item.item_id if item.item_id is not None else -1)


def _drift_for_points(db: Session, points: list[TrendPoint]) -> list[TrendPoint]:
    """给趋势点补身份漂移诊断（spec §3 / §4 P1-5，issue 02）。

    只诊断、不改写（D3）：as-of 沿用本身不错，可疑的是「同一持仓换了新 item id」
    导致旧项被永久沿用（2026-09-16 事故形状，spec §0）。

    判定（定义见 spec §3 精化版）：
    - `added`：该点计入的身份中，**历史首行日期 == 该点代表日**者（真·今日新增）。
      首点无前序快照时不做此判定——否则「库内最早一天」会把初始持仓全部标成新增，
      per-item 首行日期在那一刻必然等于代表日，纯误报。
    - `vanished`：该点计入且**沿用**（最后一行日期 < 代表日）的身份中，
      代表日之后的下一条记录金额为 0、或其后再无记录但库里仍有更晚快照日者
      ——即该沿用值「无后继」，是幽灵值。`last_date`＝沿用行的日期。

    噪声抑制：`amount == 0` 的候选两个列表都不收——零值不是「价值漂移」，它是一次
    测量（ADR-0015 零额快照合法化）；零值行若入榜会把真正的大事挤出视野
    （线上 2026-09-16 的 11 条 vanished 里多数是 0 值遗留行）。

    排序：金额降序、同额按 item_id 升序（无 item 的存量行置末）——首屏即信号。

    - `drift_amount` = max(Σadded, Σvanished)（**剔除零值之后**求和，
      因此只剩零值候选的点 drift_amount=0、不再 suspect）；
      `drift_suspect` = drift_amount / max(|total_amount|, 1) >= DRIFT_THRESHOLD。

    性能：整条序列只加 **1 条** SQL（全部 active 行的 identity/日期/金额 ＋ 其资产项
    的归档形状），首行日期、下一条记录、更晚快照是否存在都在 Python 侧用二分从这份
    数据推出；不新增任何 as-of 聚合 pass（get_mixed_chart 仍恰好一次
    `_get_latest_records_as_of`）。口径与 as-of 逐点同源：`row_identity()` ＋
    `active_item_predicate()` 的规则——一条 SQL 覆盖多日期，故逐点用其 Python 镜像
    `measuring_at()` 判定「该项在 rep 当日是否仍在计量」：归档生效日之前的点照常
    参与（历史测量值不得被追溯抹除），生效日及之后才退出；汇总项（父项）同样不参与
    ——它已退出**合计**口径，若还进漂移榜就会把「建了一条构成边」伪装成「持仓消失」
    （ADR-0029 D1/D14）。
    """
    if not points:
        return points

    identity = _row_identity()
    rows = db.execute(
        select(
            identity.label("identity"),
            Asset.asset_item_id,
            Asset.asset_name,
            Asset.asset_date,
            Asset.amount,
            AssetItem.status,
            AssetItem.archived_effective_from,
            # 汇总项标记内联（口径定义在 snapshot_identity）
            aggregate_flag_expression().label("is_aggregate"),
        )
        .join(AssetItem, AssetItem.id == Asset.asset_item_id, isouter=True)
        .where(Asset.superseded_by.is_(None))
        .order_by(identity, Asset.asset_date)
    ).all()

    # identity -> [(date, item_id, name, amount)]，按日期升序（SQL 已 order_by）
    history: dict[str, list[tuple]] = {}
    # identity -> (status, archived_effective_from)：逐点判定是否仍在计量
    lifetime: dict[str, tuple] = {}
    # identity -> 是否汇总项（项级）：汇总项不参与合计，也不该被诊断成「新增/消失」
    is_aggregate: dict[str, bool] = {}
    for ident, item_id, name, d, amount, status, arch_from, agg_flag in rows:
        history.setdefault(ident, []).append(
            (d, item_id, name, Decimal(str(amount or 0))))
        lifetime[ident] = (status, arch_from)
        # 汇总项（父项）退出**合计**口径 → 也不能进漂移榜（ADR-0029 D1/D14）：
        # 建一条构成边会让父项退出总额，若无此标记，每个父项都会在榜上伪装成
        # 「凭空消失的持仓」。父项自己的曲线仍走展示路径（get_trend_by_item）。
        is_aggregate[ident] = bool(agg_flag)
    if not history:
        return points

    dates_by_identity = {ident: [r[0] for r in hist] for ident, hist in history.items()}
    earliest_snapshot = min(hist[0][0] for hist in history.values())
    latest_snapshot = max(hist[-1][0] for hist in history.values())

    out: list[TrendPoint] = []
    for pt in points:
        rep = pt.date
        # 首点无基线：跳过 added 判定（避免把初始持仓全标成「今日新增」）
        has_baseline = earliest_snapshot < rep
        added: list[DriftAddedItem] = []
        vanished: list[DriftVanishedItem] = []

        for ident, hist in history.items():
            if not measuring_at(*lifetime[ident], rep,
                                is_aggregate=is_aggregate[ident]):
                continue                      # 归档生效日起终止计量（此前照常参与）
            dates = dates_by_identity[ident]
            i = bisect_right(dates, rep)
            if i == 0:
                continue                      # 首录在 rep 之后 → 该点尚未计入
            last_date, item_id, name, amount = hist[i - 1]
            next_row = hist[i] if i < len(hist) else None

            if has_baseline and dates[0] == rep:
                if amount != ZERO:            # 0 是测量不是漂移（ADR-0015）
                    added.append(DriftAddedItem(item_id=item_id, name=name,
                                                amount=amount))
                continue                      # 首录即今日，不可能是沿用值
            if last_date >= rep:
                continue                      # 本点当日有行（更新/显式归零）→ 非沿用

            ghost = ((next_row is not None and next_row[3] == ZERO)
                     or (next_row is None and latest_snapshot > rep))
            if ghost and amount != ZERO:      # 0 是测量不是漂移（ADR-0015）
                vanished.append(DriftVanishedItem(
                    item_id=item_id, name=name, amount=amount, last_date=last_date))

        # 降噪后排序（金额降序、item_id 升序）：首屏即可读
        added.sort(key=_drift_sort_key)
        vanished.sort(key=_drift_sort_key)
        # drift_amount 在剔除零值之后计算：只剩零值候选的点不再 suspect
        added_sum = sum((i.amount for i in added), ZERO)
        vanished_sum = sum((i.amount for i in vanished), ZERO)

        drift_amount = max(added_sum, vanished_sum)
        base = abs(pt.total_amount)
        ratio = drift_amount / (base if base > ZERO else Decimal("1"))
        out.append(pt.model_copy(update={
            "added": added,
            "vanished": vanished,
            "drift_amount": drift_amount,
            "drift_suspect": ratio >= DRIFT_THRESHOLD,
        }))

    return out


def get_total_trend(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[TrendPoint]:
    """全量趋势（as-of 口径）：**一次取数**供所有账期复用，不逐期查库。

    改前为每个账期调一次 `_get_latest_records_as_of`（day 粒度 730 期＝730 条
    相关子查询，耗时随账期线性）；现改为 `_latest_records_by_as_of` 一次取全量
    active 行后在内存按代表日扫掠，结果与逐期实现逐点逐字段相等
    （回归：`tests/test_trend_single_pass.py` 用旧实现做参考对账）。
    """
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    # 分组口径不变：label → 组内最大日期（唯一决定「该 point 的 date」）
    rep_dates = {label: get_representative_date(group_dates)
                 for label, group_dates in groups.items()}
    # 一次全量取数 + 内存扫掠（不逐期 as-of 聚合）
    records_by_rep = _latest_records_by_as_of(db, rep_dates.values())

    points = []
    for label, rep_date in rep_dates.items():
        # 同一份 records 同时供求和与沿用事实使用（同源，不跑第二遍 as-of 聚合）
        records = records_by_rep[rep_date]
        _total, assets, liabilities = _aggregate_records(records)
        carried_items, carried_max_days = _carry_stats(records, rep_date)
        # net_worth = assets - liabilities (liabilities is positive)
        net_worth = assets - liabilities
        points.append(TrendPoint(
            period=label,
            date=rep_date,
            total_amount=net_worth,  # 使用净资产作为总资产趋势
            net_assets=assets,
            liabilities=liabilities,
            carried_items=carried_items,
            carried_max_days=carried_max_days,
        ))

    # 漂移诊断（spec §3）：一次批量取数后逐点分类，不新增 as-of pass
    return _drift_for_points(db, points)


def get_period_comparison(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
    trend: list[TrendPoint] | None = None,
) -> list[ComparisonPoint]:
    """环比点派生自趋势点；`trend` 可注入已算好的列表（避免二次 as-of 聚合）。

    `trend=None` 时行为与改前一致（内部自取 get_total_trend），故既有调用方无需改。
    `get_mixed_chart` 传入同一次聚合的 trend（spec §4 P0-3：一次请求一次 as-of pass）。
    """
    if trend is None:
        trend = get_total_trend(db, period_type, date_from, date_to)
    if not trend:
        return []

    result = []
    for i, pt in enumerate(trend):
        if i == 0:
            result.append(ComparisonPoint(
                period=pt.period, date=pt.date, total=pt.total_amount,
                change_amount=Decimal("0"), change_percent=None,
                carried_items=pt.carried_items,
                carried_max_days=pt.carried_max_days,
            ))
        else:
            prev = trend[i - 1]
            change = pt.total_amount - prev.total_amount
            pct = float(change / prev.total_amount * 100) if prev.total_amount != 0 else None
            result.append(ComparisonPoint(
                period=pt.period, date=pt.date, total=pt.total_amount,
                change_amount=change, change_percent=pct,
                carried_items=pt.carried_items,
                carried_max_days=pt.carried_max_days,
            ))
    return result


def get_mixed_chart(
    db: Session, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> MixedChartData:
    # 单次 as-of 聚合：comparison 从同一批 trend 点派生，不再重跑一遍（P0-3）。
    # 这样折线与柱状同源，并发写入下也不会自相矛盾。
    trend = get_total_trend(db, period_type, date_from, date_to)
    comparison = get_period_comparison(db, period_type, date_from, date_to,
                                       trend=trend)
    return MixedChartData(trend=trend, comparison=comparison,
                          meta=build_meta(db, drift_enabled=True))


def get_trend_by_item(
    db: Session, asset_name: str, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """单个名称的 as-of 曲线（**展示口径**：汇总项照常出点，只是不进合计）。

    与所有合计出口的唯一差别是 `active_item_predicate(..., leaves_only=False)`：
    这里问的是「这一个项自己的历史金额」，父项必须照常画出来（ADR-0029 D14：隐藏
    父项会让用户以为数据丢了）；它不进总额线由 `_aggregate_records` 侧保证（那里
    的记录集已按叶子口径取数）。归档排除语义两处相同。
    """
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates:
        return []

    groups = group_dates_by_period(dates, period_type)
    points = []
    prev_amount = None

    for label, group_dates in groups.items():
        rep_date = get_representative_date(group_dates)
        # 该项截至 rep 的最近已知值（as-of：期内未更新则沿用旧值；首录前 0）
        # （v5 写语义：active 行口径；归档项自生效日起退出，ADR-0017 D2 修订）
        max_date_sub = (
            select(func.max(Asset.asset_date))
            .where(
                Asset.asset_name == asset_name,
                Asset.asset_date <= rep_date,
                Asset.superseded_by.is_(None),
                active_item_predicate(rep_date, leaves_only=False),
            )
        ).scalar_subquery()

        result = db.execute(
            select(func.coalesce(func.sum(Asset.amount), 0))
            .where(
                Asset.asset_name == asset_name,
                Asset.asset_date == max_date_sub,
                Asset.superseded_by.is_(None),
                active_item_predicate(rep_date, leaves_only=False),
            )
        ).scalar()
        amount = Decimal(str(result))
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=amount, change_amount=change,
        ))
        prev_amount = amount

    return points


def get_item_trend(
    db: Session, item_id: int, family: bool = True,
    period_type: str = "day",
    date_from: date | None = None, date_to: date | None = None,
) -> dict:
    """分项趋势·身份族模式（`.scratch/lineage-attach/spec.md` §1.3）。

    族内成员按 as-of 各自取值求和：前身自 `archived_effective_from` 起退出，
    后继自其首条记录起进入 → 切换日曲线连续、无双计。整族每期一次批量查询，
    无逐成员 N+1。
    """
    from services.lineage_service import family_item_ids  # 局部导入破环

    root = db.get(AssetItem, item_id)
    if root is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       details={"field": "item_id"})

    family_ids = family_item_ids(db, item_id)
    item_rows = {it.id: it for it in db.execute(
        select(AssetItem).where(AssetItem.id.in_(family_ids))).scalars().all()}
    query_ids = family_ids if family else [item_id]

    dates = _get_all_snapshot_dates(db, date_from, date_to)
    points = []
    for label, group_dates in group_dates_by_period(dates, period_type).items():
        rep_date = get_representative_date(group_dates)
        records = _get_latest_records_for_items(db, rep_date, query_ids)
        amounts = {r[0]: Decimal(str(r[2])) for r in records}

        members = []
        for iid in (family_ids if family else [item_id]):
            item = item_rows.get(iid)
            if item is None:
                continue
            if iid in amounts:
                amount = amounts[iid]
            elif iid == item_id:
                amount = ZERO
            elif (item.status == "archived"
                  and item.archived_effective_from is not None
                  and item.archived_effective_from <= rep_date):
                # 已退出计量的前身：显式补 0，便于前端 tooltip 展示构成；
                # 0 不影响 total，也不会与后继双算。
                amount = ZERO
            else:
                continue
            members.append({"item_id": iid, "name": item.display_name,
                            "amount": str(amount)})

        self_amount = amounts.get(item_id, ZERO)
        total = (sum((amounts[i] for i in query_ids if i in amounts), ZERO)
                 if family else self_amount)
        points.append({
            "period": label,
            "date": rep_date.isoformat(),
            "total": str(total),
            "self": str(self_amount),
            "members": members,
        })

    return {
        "item": {"item_id": item_id, "name": root.display_name,
                 "family_size": len(family_ids)},
        "points": points,
    }


def _item_ids_matching(db: Session, *conditions) -> list[int]:
    """满足条件的资产项 id（分类维度过滤的唯一取数口）。"""
    return list(db.execute(
        select(AssetItem.id).where(*conditions)).scalars().all())


def _descendant_class_ids(db: Session, class_id: int) -> list[int]:
    """类别子树 id（含自身）——一次载入全树在内存展开，不逐层回查。

    类别树是几十个节点的小表；旧实现（`_get_descendant_ids`）对每个节点递归发一条
    SQL，且每条都带 `attributes.type` 过滤。迭代 + seen 防脏数据成环。
    """
    children: dict[int | None, list[int]] = {}
    for cid, pid in db.execute(
            select(AssetClass.id, AssetClass.parent_id)).all():
        children.setdefault(pid, []).append(cid)
    out: list[int] = []
    frontier = [class_id]
    seen: set[int] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        frontier.extend(children.get(current, ()))
    return out


def _item_amount_trend(
    db: Session, item_ids: list[int], period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """指定资产项族在每期的 as-of 合计（各期一次批量查询，无逐项 N+1）。

    口径与 `_get_latest_records_as_of` 完全同源：`superseded_by IS NULL` ＋
    `active_item_predicate(rep)`（归档项自生效日起退出、历史值保留），按
    `asset_item_id` 分组取 ≤rep 的最新一行。金额恒正、不按 side 拆分（沿用改前口径：
    该维度的「总额」＝命中项的市值合计，负债侧也计入）。
    """
    dates = _get_all_snapshot_dates(db, date_from, date_to)
    if not dates or not item_ids:
        return []

    points = []
    prev_amount = None
    for label, group_dates in group_dates_by_period(dates, period_type).items():
        rep_date = get_representative_date(group_dates)
        records = _get_latest_records_for_items(db, rep_date, item_ids)
        amount = sum((Decimal(str(r[2])) for r in records), ZERO)
        change = amount - prev_amount if prev_amount is not None else None
        points.append(ItemTrendPoint(
            period=label, date=rep_date, amount=amount,
            change_amount=change,
        ))
        prev_amount = amount
    return points


def get_trend_by_type(
    db: Session, asset_class_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按资产类别（**含其所有后代**）的 as-of 趋势。

    ADR-0019：类别是**资产项级**属性（`asset_items.asset_class_id`），不再是记录级
    `attributes(type='fund_type')`。参数语义随之从「属性节点 id」变成「类别 id」，
    树展开语义不变（选中父类别＝父类别及其全部后代）。
    """
    class_ids = _descendant_class_ids(db, asset_class_id)
    item_ids = _item_ids_matching(db, AssetItem.asset_class_id.in_(class_ids))
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


def _liquidity_days_arg(days) -> int:
    """流动性档位入参规范化：只接受白名单整数天数；名称/非法值 → 422。"""
    allowed = [d for d, _label in classification_service.LIQUIDITY_LEVELS]
    if isinstance(days, str) and not days.strip().lstrip("-").isdigit():
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            "流动性维度已改为整数档位天数（ADR-0019）："
            f"days 必须是 {allowed} 之一，不再是档位名称；"
            "名称仅用于展示（`liquidity_label()`）",
            {"field": "days", "value": days, "allowed": allowed},
        )
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            f"days 必须是整数档位天数 {allowed} 之一",
            {"field": "days", "value": days, "allowed": allowed},
        )
    if value not in allowed:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            f"days 必须是档位天数 {allowed} 之一（ADR-0019 D4 白名单）",
            {"field": "days", "value": days, "allowed": allowed},
        )
    return value


def get_trend_by_liquidity(
    db: Session, days, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按流动性档位（`liquidity_days` 整数天数）的 as-of 趋势。

    ADR-0019 D4：档位唯一权威是整数天数，旧 `attributes(type='liquidity_rating')` 的
    **名称**不再是入参（名称只用于展示）。传名称 → 422，绝不静默返回空序列
    ——空序列在趋势图上显示「一分钱都没有」，比报错更危险。
    """
    value = _liquidity_days_arg(days)
    item_ids = _item_ids_matching(db, AssetItem.liquidity_days == value)
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


def get_trend_by_institution(
    db: Session, institution_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按机构的 as-of 趋势（`asset_items.institution_id`）。"""
    item_ids = _item_ids_matching(db, AssetItem.institution_id == institution_id)
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


def get_trend_by_channel(
    db: Session, channel_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按渠道的 as-of 趋势（`asset_items.channel_id`）。"""
    item_ids = _item_ids_matching(db, AssetItem.channel_id == channel_id)
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


def get_trend_by_owner(
    db: Session, owner_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按归属（`asset_items.owner_id` → `members`）的 as-of 趋势。"""
    item_ids = _item_ids_matching(db, AssetItem.owner_id == owner_id)
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


def get_trend_by_tag(
    db: Session, tag_id: int, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """按标签（`asset_item_tags`，项级多值）的 as-of 趋势。"""
    from models import AssetItemTag  # 局部导入：本模块只在标签维度用
    item_ids = _item_ids_matching(
        db, AssetItem.id.in_(
            select(AssetItemTag.item_id).where(AssetItemTag.tag_id == tag_id)))
    return _item_amount_trend(db, item_ids, period_type, date_from, date_to)


# ---------- 已废弃的旧维度读法（ADR-0019）----------
#
# 三个旧函数名**保留**只为把「静默返回空序列」变成显式 422：记录级 EAV
# （`attributes` / `asset_attributes`）已随 0027 迁移删除，旧 id 空间（属性节点 id）
# 与新 id 空间（类别/机构/渠道/成员）毫无关系——沿用旧名会被解析成另一个实体的 id，
# 悄悄返回空序列，看起来像「这个类别没有钱」。宁可报错，也不给假数据。

_DEPRECATED_TREND_HINT = (
    "端点 /api/analysis/by-type?asset_class_id=<资产类别 id>、"
    "/by-liquidity?days=<整数天数>（0/1/7/30/90/180/365/9999）、"
    "/by-institution?institution_id=<机构 id>；"
    "值域见 GET /api/asset-items/classification/meta")


def _reject_deprecated_trend(legacy: str, replacement: str) -> None:
    raise ApiError(
        ErrorCode.VALIDATION_ERROR, 422,
        f"{legacy} 已废弃（ADR-0019：分类从记录级上移到资产项）：请改用 {replacement}。"
        f"{_DEPRECATED_TREND_HINT}",
        {"deprecated": legacy, "replacement": replacement},
    )


def get_trend_by_liquidity_rating(
    db: Session, liquidity_rating=None, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """【已废弃】旧「按流动性评级名称」趋势 → 422，请改用 `get_trend_by_liquidity(days)`。"""
    _reject_deprecated_trend("by-liquidity-rating?rating=<档位名称>",
                             "by-liquidity?days=<整数天数>")


def get_trend_by_account(
    db: Session, account_id=None, period_type: str = "month",
    date_from: date | None = None, date_to: date | None = None,
) -> list[ItemTrendPoint]:
    """【已废弃】旧「按账户」趋势 → 422，请改用 `get_trend_by_institution` / `get_trend_by_channel`。"""
    _reject_deprecated_trend("by-account?account_id=<旧账户 id>",
                             "by-institution?institution_id / 渠道用 by-channel")


# ---------- 双口径收益（v5 二期·子步 2.2，spec §2.2）：I/O 组装层 ----------
# 计算契约全部在 returns_service（valuation 纯函数，零 I/O）；本层只做取数、
# 定号（口径增量→现金流）、窗口解析与降级收集。


def _all_snapshot_dates(db: Session) -> list[date]:
    return sorted(db.execute(select(distinct(Asset.asset_date))).scalars().all())


def _resolve_returns_window(db: Session, period: str | None, period_type: str,
                            date_from: date | None, date_to: date | None):
    """窗口解析：period 账期标签 ＞ date_from/date_to 显式 ＞ 全历史。返回 (start, end|None)。"""
    all_dates = _all_snapshot_dates(db)
    if not all_dates:
        return None, None
    if period:
        matched = [d for d in all_dates if get_period_label(d, period_type) == period]
        if not matched:
            return None, None
        return get_period_start_end(max(matched), period_type)
    if date_from or date_to:
        return (date_from or all_dates[0]), (date_to or all_dates[-1])
    return all_dates[0], all_dates[-1]


def _carry_value(rows: list[tuple[date, str, Decimal]], i: int, d: date) -> tuple[str, Decimal] | None:
    """rows（按日期升序）中 ≤d 的最后一行 → (side, amount)；无则 None。"""
    found = None
    while i < len(rows) and rows[i][0] <= d:
        found = rows[i]
        i += 1
    return (found[1], found[2]) if found else None


def _returns_item_rows(db: Session, end: date,
                       item_id: int | None) -> dict[int, list[tuple[date, str, Decimal]]]:
    """各项 ≤end 的 active 行（升序，(date, side, amount)）。负债/资产混合按行 side。"""
    q = (select(Asset.asset_item_id, Asset.asset_date, Asset.side, Asset.amount)
         .where(Asset.superseded_by.is_(None), Asset.asset_date <= end))
    if item_id is not None:
        q = q.where(Asset.asset_item_id == item_id)
    per: dict[int, list[tuple[date, str, Decimal]]] = {}
    for iid, d, side, amount in db.execute(
            q.order_by(Asset.asset_item_id, Asset.asset_date)).all():
        per.setdefault(iid, []).append((d, side or "asset", Decimal(str(amount or 0))))
    return per


def _returns_external_flows(db: Session, end: date, series_item_ids: set[int],
                            origins: tuple[str, ...] = ("external",)
                            ) -> list[tuple[date, Decimal]]:
    """现金流＝principal_changes 相邻**全链**增量按 origin 分类取用（注入正/撤出负）。

    增量必须沿**所有** origin 行（external/transfer/lineage）链式累计——new_amount
    是累计标量，跳过 transfer 行会把内部移动错算成外部注入（等额不变量测试锁定）。
    取用时只保留 origins 集合内的行：组合级＝('external',)（内部移动非外部流，
    spec §2.3 计算契约）；item 级＝('external','transfer')（转移对该持仓即现金流）。
    首笔增量＝new_amount−0；Δ=0 跳过。**负债侧不计**：side 取各项 ≤end 最新
    active 行，为 liability 的项整支剔除；无快照行的项亦剔除（无市值承接其注入）。
    """
    latest_side: dict[int, str] = {}
    for iid, rows in _returns_item_rows(db, end, None).items():
        latest_side[iid] = rows[-1][1]
    included = {iid for iid in series_item_ids if latest_side.get(iid) == "asset"}
    changes = db.execute(
        select(PrincipalChange.asset_item_id, PrincipalChange.effective_date,
               PrincipalChange.new_amount, PrincipalChange.origin)
        .where(PrincipalChange.effective_date <= end)
        .order_by(PrincipalChange.asset_item_id, PrincipalChange.effective_date,
                  PrincipalChange.id)
    ).all()
    agg: dict[date, Decimal] = {}
    prev_by_item: dict[int, Decimal] = {}
    for iid, d, amount, origin in changes:
        if iid not in included:
            continue
        new = Decimal(str(amount))
        delta = new - prev_by_item.get(iid, ZERO)
        prev_by_item[iid] = new
        if delta != ZERO and origin in origins:
            agg[d] = agg.get(d, ZERO) + delta
    return sorted(agg.items())


def get_returns(db: Session, scope: str = "portfolio", item_id: int | None = None,
                period: str | None = None, period_type: str = "month",
                date_from: date | None = None, date_to: date | None = None) -> dict:
    """双口径收益：TWR（衡量策略）＋XIRR（衡量本人操作）。只读，无副作用。

    - 估值序列＝R-2 回溯口径（每快照日取各项最新 active 行；组合＝资产侧项合计，
      负债侧不计）；外部现金流＝口径史 origin='external' 增量。
    - 不可算一律 rate=None＋RATE_UNAVAILABLE 降级（绝不编数）；大额现金流无当日
      重估值 → TWR_LARGE_FLOW_APPROX（scope=区间）。
    """
    if scope == "item":
        if item_id is None or db.get(AssetItem, item_id) is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项 {item_id} 不存在",
                           details={"field": "id"})

    start, end = _resolve_returns_window(db, period, period_type, date_from, date_to)
    window_label = f"{start}..{end}" if start and end else None
    degradations: list[dict] = []

    twr_out: dict = {"period": {"start": None, "end": None, "subperiods": [],
                                "period_return": None}, "annualized": None}
    xirr_out: str | None = None
    series: list[tuple[date, Decimal]] = []

    if start is None or end is None:
        degradations.append({"code": NO_DATA, "scope": scope,
                             "message": "窗口内无估值数据，收益不可得"})
    else:
        per = _returns_item_rows(db, end, item_id if scope == "item" else None)
        dates = sorted({d for rows in per.values() for d, _, _ in rows
                        if d >= start})
        # 估值序列：组合＝资产侧项合计（carry 行为负债的项该日不计）；逐项＝自身
        series = []
        for d in dates:
            total = ZERO
            any_asset = False
            for iid, rows in per.items():
                cv = _carry_value(rows, 0, d)
                if cv and cv[0] == "asset":
                    total += cv[1]
                    any_asset = True
            if any_asset:
                series.append((d, total))
        # item 级视转移为该持仓的现金流；组合级过滤内部转移（spec §2.3 计算契约）
        origins = ("external", "transfer") if scope == "item" else ("external",)
        flows = _returns_external_flows(db, end, set(per.keys()), origins=origins)

        twr = returns_service.twr_modified_dietz(series, flows)
        twr_out = {
            "period": {"start": series[0][0].isoformat() if series else None,
                       "end": series[-1][0].isoformat() if series else None,
                       "subperiods": twr["subperiods"],
                       "period_return": twr["period_return"]},
            "annualized": twr["annualized"],
        }
        if twr["reason"]:
            degradations.append({"code": RATE_UNAVAILABLE, "scope": window_label,
                                 "message": f"TWR：{twr['reason']}"})
        if twr["large_flow_approx"]:
            degradations.append({
                "code": TWR_LARGE_FLOW_APPROX, "scope": window_label,
                "message": ("大额现金流（≥期初 15%）当日无重估值："
                            f"{', '.join(twr['large_flow_dates'])}，TWR 为 MD 近似")})

        # XIRR：期初市值负 / 现金流按投资者视角定号（注入负/撤出正）/ 期末市值一笔正
        if (len(series) >= 2 and series[0][1] > ZERO
                and twr["period_return"] is not None):
            t0, v0 = series[0]
            t1 = series[-1][0]
            cfs: list[tuple[date, Decimal]] = [(t0, -v0)]
            cfs += [(d, -a) for d, a in flows if t0 < d <= t1]
            cfs.append((series[-1][0], series[-1][1]))
            rate = returns_service.xirr_rate(cfs)
            if rate is not None:
                xirr_out = str(Decimal(str(rate)).quantize(Decimal("0.0001")))
            else:
                degradations.append({
                    "code": RATE_UNAVAILABLE, "scope": window_label,
                    "message": "XIRR：不收敛或无正负现金流对，率不可得"})
        elif not series or len(series) < 2:
            degradations.append({
                "code": RATE_UNAVAILABLE, "scope": window_label,
                "message": "XIRR：估值序列不足两个时点，率不可得"})

    meta = build_meta(db, series[-1][0] if series else None,
                      degradations, principal=PRINCIPAL_BASIS_VERSION)
    return {
        "scope": scope,
        "item_id": item_id if scope == "item" else None,
        "window": {"start": start.isoformat() if start else None,
                   "end": end.isoformat() if end else None,
                   "period": period},
        "twr": twr_out,
        "xirr": xirr_out,
        "assumptions": [
            "双口径分工：TWR＝时间加权，衡量**策略**（剔除加减仓时点影响）；"
            "XIRR＝货币加权，衡量**本人操作**（资金进出时点计入）",
            "外部现金流＝本金口径变更史 origin='external' 相邻增量（注入正/撤出负）；"
            "负债侧不计",
            "估值序列＝R-2 回溯口径（每快照日取各项 ≤当日最新 active 行），"
            "与 summary/proportion/allocation 同读法",
            "TWR＝Modified Dietz 逐段（现金流日切段）＋几何链接；"
            "年化＝(1+R)^(365/区间天数)−1；大额现金流＝单笔≥期初组合 15%",
            "XIRR＝365 天贴现；期末市值一笔正（勿与逐笔现金流重复）；"
            "不收敛或无一正一负 → 率不可得（绝不编数）",
        ],
        "meta": meta.model_dump(mode="json"),
    }
