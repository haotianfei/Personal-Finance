"""资产记录行的稳定身份表达式——**单一定义处**。

改名只改资产项的显示名、不回写历史行上的 `asset_name` 快照，因此名字不能作为
「同一资产项」的分组键：用名字分组会把改名拆成两个资产而双计。稳定身份是
`asset_item_id`；双轨窗口内尚无 item 的存量行（迁移前、测试直插）回退按名字。

分析聚合（`analysis_service`）与占比聚合（`proportion_service`）曾经各持一份
等价实现——那正是改名双计 bug 的成因，故收敛到这里，任何一处改口径必须同时
作用于两处。见 ADR-0016 / `.scratch/current-value/spec.md`。

「哪些行参与 as-of 口径」同样只在此处定义：`row_identity()`（同一身份）＋
`active_item_predicate(as_of)`（该项在 as_of 当日仍在计量）。ADR-0017 D2：
**归档＝自归档生效日起终止计量，历史测量值保留**——归档项只退出其生效日及之后
的 as-of 聚合，不得追溯抹除此前的真实序列（初版「整支排除」会伪造出一整年的
净值塌陷）。两个服务都必须叠加同一谓词，否则口径会分叉。

ADR-0029 D1/D2（**叶子计量**）再加一条：项若在 `item_compositions` 里作为
`parent_item_id` 出现（＝汇总项，有子项构成它），则不计入计量口径——子项照常计入。
判定**派生自构成关系**，不在 `asset_items` 上存布尔位：存标记则解除关系忘清一次
就永久漏计，且界面上看不出异常。解除关系/子项清空后父项**自动恢复**计量。
展示口径（资产列表/详情/趋势曲线）**照常**返回父项，只是不进合计（ADR-0029 D14）。
"""

from datetime import date

from sqlalchemy import String, and_, case, cast, literal, or_, select

from models import Asset, AssetItem, ItemComposition


def row_identity():
    """返回「行归属身份」SQL 表达式：`i:<asset_item_id>` 或 `n:<asset_name>`。

    用法：作为 `group_by` / `join` 的比较键，不要与 `asset_name` 混用。
    """
    return case(
        (Asset.asset_item_id.isnot(None),
         literal("i:") + cast(Asset.asset_item_id, String)),
        else_=literal("n:") + Asset.asset_name,
    )


def archived_item_ids(as_of: date):
    """在 as_of 当日已终止计量的资产项 id 子查询（R-37 状态机 ＋ 0025 生效日）。

    命中条件：`status='archived'` 且（`archived_effective_from <= as_of`
    或 `archived_effective_from IS NULL`）。NULL＝归档日未知（D2 落地前的存量
    形态）→ 对一切日期保守剔除，与改前行为一致。
    """
    return select(AssetItem.id).where(
        AssetItem.status == "archived",
        or_(AssetItem.archived_effective_from.is_(None),
            AssetItem.archived_effective_from <= as_of),
    )


def aggregate_item_ids():
    """在构成关系里作为**父项**出现的资产项 id 子查询（ADR-0029 D1，叶子计量）。

    与 `archived_item_ids()` 同形：都是「谁的 id 不参与计量」的集合，供
    `active_item_predicate()` 组合。派生自 `item_compositions`，不存布尔位——
    解除构成边后本集合自然少了这一项，父项自动恢复计量（D4）。
    """
    return select(ItemComposition.parent_item_id)


def aggregate_flag_expression():
    """`Asset.asset_item_id` 是否汇总项——`aggregate_item_ids()` 的**内联布尔表达式**。

    只给「一次取数、多日期判定」的批量路径用（`analysis_service` 的
    `_latest_records_by_as_of` / `_drift_for_points`）：把标记塞进**同一条 SELECT**，
    而不是再发一条查询取父 id 集合——那些路径的硬约束是「SQL 条数与账期数解耦」
    （`tests/test_trend_single_pass.py`），多一条往返就等于让口径查询随实现方式漂移。

    口径仍只有一处定义（`aggregate_item_ids`），调用方只负责把结果传给
    `measuring_at(is_aggregate=...)`，**不得**在调用侧另写过滤条件（D2）。
    """
    return Asset.asset_item_id.in_(aggregate_item_ids())


def active_item_predicate(as_of: date, *, leaves_only: bool = True):
    """as-of 口径的「as_of 当日仍在计量」SQL 谓词（ADR-0017 D2 修订版）。

    归档＝人裁决该计量**自生效日起**终止（R-37 ＋ 0025），故该项只退出
    `archived_effective_from <= as_of` 的 as-of 聚合；生效日之前的日期照常
    计入其真实历史测量值。无 `asset_item_id` 的存量行（双轨窗口/测试直插）
    不受影响，继续按名字回退口径（与 `row_identity()` 的 else 分支一致）。

    ADR-0029 D1（叶子计量）在此**再加一条**：该项不得作为 `item_compositions`
    的父项出现（＝汇总项退出计量，子项照常计入）。父项金额仍在库里、仍在展示
    口径（列表/详情/趋势曲线），只是不进任何合计。

    `leaves_only=False` 只服务**展示口径的单身份曲线**（`get_trend_by_item`）：
    那里问的是「这一个项自己的历史金额」，父项必须照常出点（ADR-0029 D14：隐藏
    父项会让用户以为数据丢了），它只是在合计里不算数。**任何合计/占比/告警/导出
    出口都必须用默认的 `leaves_only=True`**——口径仍然只有这一处实现，调用方不得
    另写过滤（D2）。

    用法：同时叠加到「取身份最新日期」的子查询与主查询的 join 条件上，且
    **两处必须传同一个 as_of**（`analysis_service._get_latest_records_as_of`、
    `proportion_service._latest_snapshot_subq` / `_join_latest_snapshot`、
    `alert_service` 各账期查询）。
    """
    clauses = [Asset.asset_item_id.notin_(archived_item_ids(as_of))]
    if leaves_only:
        clauses.append(Asset.asset_item_id.notin_(aggregate_item_ids()))
    return or_(
        Asset.asset_item_id.is_(None),
        and_(*clauses),
    )


def measuring_at(status: str | None, archived_effective_from: date | None,
                 as_of: date, *, is_aggregate: bool = False) -> bool:
    """`active_item_predicate(as_of)` 的等值 Python 版——**仅供「一次取数、多日期
    判定」的路径**：漂移诊断（`analysis_service._drift_for_points` 一条 SQL 覆盖整条
    序列）与趋势批量扫掠（`analysis_service._latest_records_by_as_of`，730 期 SQL 条数
    与账期数解耦）。除这两处外的单日期查询一律走 SQL 谓词 `active_item_predicate`。

    语义严格镜像 SQL：非 archived 恒在计量；archived 且生效日未知（NULL）恒不计量；
    archived 且生效日 > as_of 时仍在计量。其余任何路径都必须用 SQL 谓词，禁止在
    Python 侧二次过滤（口径分叉的成因就是存在两份实现）。

    `is_aggregate` 镜像 SQL 谓词里那条「父项退出计量」（ADR-0029 D1）：批量路径的
    调用方必须把「该项是否有子项」（`composition_service.is_aggregate` 或
    `aggregate_item_ids()` 的 id 集合）作为关键字参数传进来，**不得**在调用方
    另写一套 `if item_id in parents: continue`。默认 False ＝ 该项非汇总项
    （未接线前的既有行为，不会误伤任何现网调用）。
    """
    if is_aggregate:
        return False
    if status != "archived":
        return True
    return archived_effective_from is not None and archived_effective_from > as_of