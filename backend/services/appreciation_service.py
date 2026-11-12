"""升值计算服务（v5 二期·子步 2.1，规格 .scratch/v5-appreciation/spec.md §2.1 计算契约）。

- 行选择＝**R-2 回溯口径（carry-forward）**：每资产项取 ≤as_of 的最新 active 行
  （与 summary/proportion/allocation 一致；当日无行的项以更早最新行在场，不落整支）。
- 逐项：升值额＝该最新 active 快照 amount − 生效口径 resolve_cost_basis(item, as_of)；
  升值率＝升值额/口径（资产侧）；**口径 ≤0 或缺失 → RATE_UNAVAILABLE 降级**（scope=项，
  首次消费预留码）。负债侧不计率（spec §3 修订建议 1：按「负债不算率」执行，仅列市值）。
- 组合级：Σ 资产侧项（市值合计/口径合计/升值额合计/总率），与逐项之和一致；
  有未设口径项 → 附 `PRINCIPAL_NOT_SET`（scope=未设项清单）。
- 历史期定格（R-36/决定6）：resolve 按 as_of 取生效口径 → 天然定格（改口径后
  历史 as_of 结果不变），无需缓存。
- meta.basis.principal="pc-1"（口径契约版本，激活预留位）。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from models import Asset, AssetItem
from services import meta_service
from services.principal_service import resolve_cost_basis

ZERO = Decimal("0")


def _dec(v) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def _latest_as_of(db: Session) -> date | None:
    return db.execute(select(func.max(Asset.asset_date))).scalar()


def _active_rows(db: Session, as_of: date):
    """as_of 日各资产项的**回溯口径**快照行（R-2 carry-forward）。

    每资产项取 `asset_date <= as_of` 的最新 active 行——as_of 当日无行的项以其
    更早的最新行在场，不落整支。子查询按 item 取 max(date)，回连后仍过滤 superseded
    （同日多行时部分唯一索引保证当日仅一条 active，回连恰一条）。
    无任何 ≤as_of 行的项不出现。

    **只看有效行，不看资产项状态**：本函数不过滤 `AssetItem.status`，归档项在此
    仍然在场。需要「归档项自归档生效日起退出」的调用方（ADR-0017）必须自行叠加
    `snapshot_identity.measuring_at`（Python 镜像）或 `active_item_predicate`（SQL）。
    `attribution_service` 的两处调用（`_net_at`、覆盖率分母）均已叠加；旧注释自称「与 summary/proportion/allocation
    读路径同口径」在归档过滤补上后即不成立，勿据此引用本函数当权威口径。
    """
    latest = (
        select(Asset.asset_item_id.label("iid"),
               func.max(Asset.asset_date).label("md"))
        .where(Asset.asset_date <= as_of, Asset.superseded_by.is_(None))
        .group_by(Asset.asset_item_id)
        .subquery()
    )
    return db.execute(
        select(Asset, AssetItem)
        .join(AssetItem, AssetItem.id == Asset.asset_item_id)
        .join(latest, and_(latest.c.iid == Asset.asset_item_id,
                           latest.c.md == Asset.asset_date))
        .where(Asset.superseded_by.is_(None))
        .order_by(AssetItem.id)
    ).all()


def get_appreciation(db: Session, as_of: date | None = None) -> dict:
    """升值表：组合卡片＋逐项表（口径缺失/≤0 逐项 RATE_UNAVAILABLE，组合 PRINCIPAL_NOT_SET）。"""
    if as_of is None:
        as_of = _latest_as_of(db)

    items: list[dict] = []
    degradations: list[dict] = []
    principal_missing: list[str] = []

    mkt_sum = Decimal("0")      # 资产侧市值合计
    basis_sum = Decimal("0")    # 有口径（>0）项的口径合计
    appr_sum = Decimal("0")     # 有口径（>0）项的升值额合计

    if as_of is not None:
        for a, item in _active_rows(db, as_of):
            if item.status == "archived":
                continue  # 归档项不入现状升值视图（R-37）
            market = _dec(a.amount)
            is_asset = (a.side or "asset") == "asset"
            basis = resolve_cost_basis(db, item.id, as_of) if is_asset else None
            row_degr: list[dict] = []
            appreciation = None
            rate = None
            if not is_asset:
                pass  # 负债侧仅列市值，不算率（spec §3 修订建议 1）
            elif basis is None:
                row_degr.append({"code": meta_service.RATE_UNAVAILABLE,
                                 "scope": item.display_name,
                                 "message": "未登记本金口径，升值率不可得"})
                principal_missing.append(item.display_name)
            elif basis <= ZERO:
                row_degr.append({"code": meta_service.RATE_UNAVAILABLE,
                                 "scope": item.display_name,
                                 "message": "口径 ≤0，升值率不可得"})
                appreciation = market - basis
            else:
                appreciation = market - basis
                rate = (appreciation / basis).quantize(Decimal("0.0001"))

            if is_asset:
                mkt_sum += market
                if basis is not None and basis > ZERO:
                    basis_sum += basis
                    appr_sum += _dec(appreciation)

            items.append({
                "item_id": item.id,
                "display_name": item.display_name,
                "side": a.side or "asset",
                "market": str(market),
                "cost_basis": str(basis) if basis is not None else None,
                "appreciation": str(appreciation) if appreciation is not None else None,
                "rate": str(rate) if rate is not None else None,
                "degradations": row_degr,
            })

    portfolio_rate = None
    if basis_sum > ZERO:
        portfolio_rate = str((appr_sum / basis_sum).quantize(Decimal("0.0001")))

    if principal_missing:
        degradations.append({
            "code": meta_service.PRINCIPAL_NOT_SET,
            "scope": ",".join(principal_missing),
            "message": f"{len(principal_missing)} 个资产项未登记本金口径",
        })

    meta = meta_service.build_meta(
        db, as_of, degradations, principal=meta_service.PRINCIPAL_BASIS_VERSION)

    return {
        "as_of": as_of.isoformat() if as_of else None,
        "portfolio": {
            "market_total": str(mkt_sum),
            "basis_total": str(basis_sum),
            "appreciation_total": str(appr_sum),
            "rate": portfolio_rate,
            "items_missing_principal": principal_missing,
        },
        "items": items,
        "meta": meta.model_dump(mode="json"),  # 序列化后为纯 dict（HTTP/直调一致）
    }
