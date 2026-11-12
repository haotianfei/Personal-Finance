"""净资产变动归因瀑布（v5 二期增强，规格 .scratch/v5-attribution/spec.md）。

只读派生、零迁移（仅依赖 0012 `origin` 列＋2.3 转移事件表）。不变量（定义性成立）：
`Δ净资产 = 外部净投入 + 内部转移净额 + 内生变动`，全程 Decimal 2dp，
**内生＝残差**（吸收市值涨跌、未覆盖项效应与始末快照时滞），无独立舍入误差——
不得误读为「投资收益」（assumptions/coverage 明示）。

- 始末净资产＝R-2 回溯口径（复用 `appreciation_service._active_rows`，并在
  `_net_at` 内补归档谓词），资产侧−负债侧；**数值恒等于
  `analysis_service._sum_as_of(as_of)` 的同侧合计**（ADR-0020 D4 同源不变量）。
  **回显实际取值快照日**（carry-forward 显式化）。归档项按 ADR-0017 D2 自
  `archived_effective_from` 起退出始末值，生效日前的真实历史值保留（不追溯抹除）。
- 外部流＝origin='external' 增量，半开区间 **(start, end]**（start 当日已含于始值、
  不重复计），**按 side 定号**：资产侧注入正；负债侧口径增加（新增借贷）为负——
  借贷场景（现金+X/负债+X）Δ=0、external=0。
- 转移段＝origin='transfer' 增量（原始定号不翻 side）：组合层 from −/to ＋
  **精确净零** amount="0.00"＋gross＝区间内转移事件单边上界总额；item 层＝该项净额。
- 增量沿**全部 origin 链式计算**（new_amount 是累计标量，2.3 修正同因）。
- 覆盖率：未设口径项（窗口末生效口径 resolve 为 NULL）→ PRINCIPAL_NOT_SET＋
  coverage{principal_set_ratio, uncovered_items[]}；无任何快照 → NO_DATA；
  meta 照传 pc-1。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AssetItem, PrincipalChange
from errors import ApiError, ErrorCode
from services import meta_service
from services.analysis_service import (
    _carry_value, _resolve_returns_window, _returns_item_rows,
)
from services.appreciation_service import _active_rows
from services.principal_service import resolve_cost_basis
from services.snapshot_identity import measuring_at

ZERO = Decimal("0")
CENT = Decimal("0.01")
RATIO_Q = Decimal("0.0001")


def _dec(v) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def _money(d: Decimal | None) -> str:
    return str(_dec(d).quantize(CENT))


def _net_at(db: Session, as_of: date, item_id: int | None) -> tuple[Decimal | None, date | None]:
    """R-2 回溯口径始/末净资产：Σ(资产侧)−Σ(负债侧)；返回 (值|无行 None, 实际取值快照日|None)。

    值与日期同生同灭（有行才有值）；None 表示窗口界前无任何在场项（净资产视为 0，
    实际快照日无从回显）。

    归档过滤在本函数内补齐（ADR-0017 D2 ＋ ADR-0020 D4 同源不变量）：`_active_rows`
    只做 superseded 过滤与「每项最新行」，**不**判 `AssetItem.status`（它另有调用方，
    故不在此改动）。这里用 `snapshot_identity.measuring_at`——`active_item_predicate
    (as_of)` 的 Python 镜像，语义相同：归档项自 `archived_effective_from` 起退出，
    生效日之前照常计入（不追溯抹除历史）；归档日未知（NULL）的存量形态恒剔除。
    因此本值恒等于 `analysis_service._sum_as_of(as_of)` 的同侧合计，与
    summary/proportion/allocation 同口径（此处不再是「声称」）。
    """
    net = ZERO
    latest: date | None = None
    found = False
    for a, item in _active_rows(db, as_of):
        if not measuring_at(item.status, item.archived_effective_from, as_of):
            continue  # 归档项自生效日起终止计量：净值里不得再出现
        if item_id is not None and item.id != item_id:
            continue
        found = True
        amt = _dec(a.amount)
        net += -amt if (a.side or "asset") == "liability" else amt
        if latest is None or a.asset_date > latest:
            latest = a.asset_date
    return (net if found else None), latest


def _flow_chain(db: Session, item_id: int, end: date) -> list[tuple[date, str, Decimal]]:
    """该项 ≤end 全部口径变更（所有 origin，登记序）→ [(date, origin, Δ)]。"""
    rows = db.execute(
        select(PrincipalChange.effective_date, PrincipalChange.new_amount,
               PrincipalChange.origin)
        .where(PrincipalChange.asset_item_id == item_id,
               PrincipalChange.effective_date <= end)
        .order_by(PrincipalChange.effective_date, PrincipalChange.id)
    ).all()
    out: list[tuple[date, str, Decimal]] = []
    prev = ZERO
    for d, amount, origin in rows:
        new = _dec(amount)
        delta = new - prev
        prev = new
        if delta != ZERO:
            out.append((d, origin, delta))
    return out


def get_attribution(db: Session, scope: str = "portfolio", item_id: int | None = None,
                    period: str | None = None, period_type: str = "month",
                    date_from: date | None = None, date_to: date | None = None) -> dict:
    """期间归因：Δ＝外部净投入＋内部转移净额＋内生变动（残差）。只读，无副作用。"""
    if scope == "item":
        item = db.get(AssetItem, item_id) if item_id is not None else None
        if item is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                           details={"field": "id"})

    start, end = _resolve_returns_window(db, period, period_type, date_from, date_to)
    degradations: list[dict] = []

    net_start, start_used = (None, None)
    net_end, end_used = (None, None)
    external = ZERO
    inflows = ZERO
    outflows = ZERO
    transfer_net = ZERO
    gross = ZERO

    if start is None or end is None:
        degradations.append({"code": meta_service.NO_DATA, "scope": scope,
                             "message": "库内无任何快照数据，归因不可得"})

    if start is not None and end is not None:
        net_start, start_used = _net_at(db, start, item_id if scope == "item" else None)
        net_end, end_used = _net_at(db, end, item_id if scope == "item" else None)
        net_start = _dec(net_start)
        net_end = _dec(net_end)

        # ——外部/转移段：沿全部 origin 链式增量，按窗口半开区间 (start, end] 取用
        scope_items = ([item_id] if scope == "item"
                       else sorted(_returns_item_rows(db, end, None).keys()))
        rows_by_item = _returns_item_rows(db, end, None)
        for iid in scope_items:
            for d, origin, delta in _flow_chain(db, iid, end):
                if not (start < d <= end):
                    continue
                if origin == "external":
                    rows = rows_by_item.get(iid, [])
                    side = (_carry_value(rows, 0, d) or ("asset", ZERO))[0]
                    signed = -delta if side == "liability" else delta
                    external += signed
                    if signed >= ZERO:
                        inflows += signed
                    else:
                        outflows += signed
                elif origin == "transfer":
                    transfer_net += delta
                    gross += abs(delta)

        # gross：组合层＝单边转移总额（|Δ|/2，from/to 成对精确净零）；item 层＝|Δ| 直和
        if scope == "portfolio":
            gross = gross / 2
            transfer_net = ZERO   # 组合层结构性净零（配对两条同事件）

    delta_net = net_end - net_start if (start is not None and end is not None) else ZERO
    endogenous = delta_net - external - transfer_net

    # ——覆盖率：窗口末在场项（末行 active 行）的口径登记情况
    uncovered: list[str] = []
    total_items = 0
    if end is not None:
        seen: set[int] = set()
        for a, it in _active_rows(db, end):
            if not measuring_at(it.status, it.archived_effective_from, end):
                continue  # 归档项自生效日起不在场：既不该进分母，也不该进 uncovered
            if scope == "item" and it.id != item_id:
                continue
            if it.id in seen:
                continue
            seen.add(it.id)
            total_items += 1
            if resolve_cost_basis(db, it.id, end) is None:
                uncovered.append(it.display_name)
    set_cnt = total_items - len(uncovered)
    ratio = (Decimal(set_cnt) / Decimal(total_items)).quantize(RATIO_Q) \
        if total_items else ZERO.quantize(RATIO_Q)

    if total_items and uncovered:
        degradations.append({
            "code": meta_service.PRINCIPAL_NOT_SET,
            "scope": ",".join(uncovered),
            "message": f"{len(uncovered)} 个资产项未登记本金口径，其市值变动全部落入内生残差",
        })

    # ——分段（顺序恒等式呈现：始→外→转移→内生→末）
    if scope == "portfolio":
        segments = [
            {"key": "external", "label": "外部净投入", "amount": _money(external),
             "inflows": _money(inflows), "outflows": _money(outflows)},
            {"key": "transfer", "label": "内部转移", "amount": _money(ZERO),
             "gross": _money(gross), "note": "组合层面净零（转出 −/转入 ＋ 同事件配对）"},
            {"key": "endogenous", "label": "内生变动（残差）", "amount": _money(endogenous)},
        ]
    else:
        segments = [
            {"key": "external", "label": "外部净投入", "amount": _money(external),
             "inflows": _money(inflows), "outflows": _money(outflows)},
            {"key": "transfer", "label": "内部转移", "amount": _money(transfer_net),
             "gross": _money(gross), "note": "item 层为该项净额（≠0 正常）"},
            {"key": "endogenous", "label": "内生变动（残差）", "amount": _money(endogenous)},
        ]

    meta = meta_service.build_meta(
        db, end_used if end_used else None, degradations,
        principal=meta_service.PRINCIPAL_BASIS_VERSION)

    return {
        "scope": scope,
        "item_id": item_id if scope == "item" else None,
        "date_from": start.isoformat() if start else None,
        "date_to": end.isoformat() if end else None,
        "net_worth_start": _money(net_start) if net_start is not None else "0.00",
        "net_worth_start_date": start_used.isoformat() if start_used else None,
        "net_worth_end": _money(net_end) if net_end is not None else "0.00",
        "net_worth_end_date": end_used.isoformat() if end_used else None,
        "delta": _money(delta_net),
        "segments": segments,
        "coverage": {
            "principal_set_ratio": str(ratio),
            "uncovered_items": uncovered,
        },
        "assumptions": [
            "恒等式：Δ净资产＝外部净投入＋内部转移净额＋内生变动；**内生＝残差**"
            "（含市值涨跌、未登记口径项的市值效应、始末快照 carry-forward 时滞），"
            "不得误读为『投资收益』",
            "始末净资产＝R-2 回溯口径（资产侧−负债侧，与 summary 同源相等，ADR-0020 D4），"
            "回显实际取值快照日；归档项自归档生效日起退出始末值（ADR-0017 D2，"
            "生效日前的历史值保留）",
            "外部流＝本金口径变更史 origin='external' 增量，窗口半开区间 (start, end]"
            "（start 当日已含于始值，不重复计）；按 side 定号：资产侧注入正、"
            "负债侧口径增加（新增借贷）为负",
            "内部转移段＝origin='transfer'：组合层精确净零（from −/to ＋），"
            "gross＝单边转移总额；item 层＝该项净额（≠0 正常）",
            f"coverage＝已登记口径项/窗口末在场项＝{ratio}"
            "；ratio 偏低时归因参考价值有限（未覆盖项全部沉入内生残差）",
        ],
        "meta": meta.model_dump(mode="json"),
    }
