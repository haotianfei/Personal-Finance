"""口径元数据（v5 契约层，spec §4）。

计算类读响应顶层 `meta`：{snapshot_date, basis, degradations:[...]}。
降级事件由**计算过程收集**（非依赖注入）；列表类响应不挂 meta（避免过度设计）。

枚举：
- DATA_MISSING_CARRY_FORWARD：某日期沿用前值（scope=日期/资产项）
- NO_DATA：空库（无任何快照）
- RATE_UNAVAILABLE：「率不可得」——本步仅预留，无消费方（二期 TWR/XIRR 启用）
"""
from datetime import date
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Asset
from schemas import Meta, MetaBasis, Degradation

# 降级码常量（单源，前端 types 对齐）
DATA_MISSING_CARRY_FORWARD = "DATA_MISSING_CARRY_FORWARD"
NO_DATA = "NO_DATA"
RATE_UNAVAILABLE = "RATE_UNAVAILABLE"   # 二期·2.1 首次消费：升值率不可得（口径缺失/≤0）；2.2 复用：TWR/XIRR 不可得
PRINCIPAL_NOT_SET = "PRINCIPAL_NOT_SET"  # 二期·2.1：组合级降级，scope=未设口径项清单
TWR_LARGE_FLOW_APPROX = "TWR_LARGE_FLOW_APPROX"  # 二期·2.2：大额现金流无当日重估值，TWR 为 MD 近似（scope=区间）

# 口径契约版本（meta.basis.principal 激活值，二期·2.1）
PRINCIPAL_BASIS_VERSION = "pc-1"

SCHEMA_GENERATION = 5


def _latest_date(db: Session) -> Optional[date]:
    return db.execute(select(func.max(Asset.asset_date))).scalar()


def build_meta(db: Session, snapshot_date: Optional[date] = None,
               degradations: Optional[list[dict]] = None,
               principal: Optional[str] = None,
               drift_enabled: bool = False) -> Meta:
    """构造口径元数据。

    - snapshot_date 省略时取全库最新快照日。
    - 无任何数据（snapshot_date 仍为 None）→ 自动附 NO_DATA 降级。
    - 计算过程可显式传入 degradations（如 carry-forward 事件）。
    - principal：口径契约版本（二期·2.1 起升值端点传 PRINCIPAL_BASIS_VERSION）；
      其余端点缺省 None（预留位语义不变）。
    - drift_enabled：该响应是否携带身份漂移诊断字段（spec §3；仅
      total-trend / mixed-chart 为 true，供前端探测，旧前端忽略即兼容）。
    """
    if snapshot_date is None:
        snapshot_date = _latest_date(db)

    degrs = [Degradation(**d) for d in (degradations or [])]
    if snapshot_date is None and not any(
            d.code == NO_DATA for d in degrs):
        degrs.append(Degradation(code=NO_DATA, scope=None,
                                 message="库内暂无快照数据，金额为占位零值"))

    return Meta(
        snapshot_date=snapshot_date,
        basis=MetaBasis(schema_generation=SCHEMA_GENERATION,
                        identity="asset_item", principal=principal),
        degradations=degrs,
        drift_enabled=drift_enabled,
    )
